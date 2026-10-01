"""Real PostgreSQL proof for GOO-316 statements, ORCID states, venue checks
and the anonymized package variant.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now
ends at ``f6a8c0d2e4b5``: the four tables, their CHECKs, the approval's
composite set-hash FK and the insert-only triggers come from the migration.
Seeds: GOO-315's run, figure, claim and draft-release helpers, one GOO-314
round whose reviewer has a display name, an adjudicator renamed to a
distinctive name, and author-linked user U (a workspace editor with no
project role). Step 8 (legacy, no statement set) runs first, because any
later statement set changes the rebuilt snapshot by design.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-316 section):
``record_orcid`` storing the whole token response (step 2), ``scan_leaks``
skipped with reviewer names dropped from ``identities`` (step 5), approvals
counted from any set (step 6), the venue check ignoring the package hash
(step 7).

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_statements_venue_postgres.py``.
"""

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeAlias, TypeVar, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.citation import Citation
from src.models.draft_citation import DraftCitation
from src.models.generated_draft import GeneratedDraft
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.user import User
from src.models.workspace import WorkspaceMember, WorkspaceRole
from src.services.artifacts.storage import LocalArtifactStorage
from src.services.research import manuscript_release_service as mr
from src.services.research import peer_review_service as reviews
from src.services.research import statements_service as st
from src.services.research_decisions import replay_decisions
from src.services.research_engine import experiment_service, step_executor
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from src.shared.manuscript_release_schemas import (
    CandidateCreate,
    ManuscriptReleaseResponse,
    PromoteRequest,
)
from src.shared.peer_review_schemas import (
    CommentCreate,
    DecisionCreate,
    ResponseCreate,
    ReviewerCreate,
    RoundCreate,
)
from src.shared.statements_schemas import (
    ApprovalCreate,
    StatementBody,
    StatementSetCreate,
    VenueCheckCreate,
)
from tests.integration.test_draft_release_postgres import (
    _assess,
    _claim,
    _link,
    _promote,
)
from tests.integration.test_run_manifest_postgres import (
    SECRET,
    _FakeSandbox,
    _register,
    _run,
    _setup,
)
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
T = TypeVar("T")
VIEW, EDIT = ResearchAction.VIEW, ResearchAction.EDIT
RELEASE, ADJUDICATE = ResearchAction.RELEASE, ResearchAction.ADJUDICATE
REVIEW = ResearchAction.REVIEW
S1 = "Mean y rose across the three conditions (Figure 1) [Doc 1] [Doc 2]."
REVIEWER = "Rosalind Featherstonehaugh"
ADJUDICATOR = "Zebulon Quartermaine"
D1 = (
    "# Abstract\nA fixture study.\n## Introduction\nWhy it matters.\n"
    f"## Methods\nMethods were refined after comments from {REVIEWER}.\n"
    f"## Results\n{S1}\n"
    f"## Acknowledgements\nWe thank {ADJUDICATOR} and {REVIEWER}.\n"
)
D2 = D1 + "## Discussion\nIt holds.\n"
ORCID_A, ORCID_B = "0000-0002-1825-0097", "0000-0001-5109-3700"
# Built by concatenation so no token-shaped literal is committed.
ACCESS = "acc" + "ess-" + uuid4().hex
REFRESH = "ref" + "resh-" + uuid4().hex
AUTHORS = {
    "a": ("Imogen Vasquez-Thorne", "Fenwick Institute of Hydrology"),
    "b": ("Bartholomew Okonkwo", "Okapi Biostatistics Lab"),
    "c": ("Cressida Lindqvist", "Lindqvist Data Collective"),
}
EMAIL_A = "imogen.vt@fenwick.example"
AWARD = "FF-2026-0042"


def _sha(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


async def _as(
    w: Any,
    user: str,
    action: ResearchAction,
    call: Callable[[AsyncSession, ProjectContext], Awaitable[T]],
) -> T:
    """One route-shaped request: resolve, then the service (which commits)."""
    async with w.factory() as db:
        context = await resolve_project(db, w.p1, w.ids[user], action)
        return await call(db, context)


async def _refused(awaitable: Awaitable[Any], status: int) -> None:
    with pytest.raises(HTTPException) as error:
        await awaitable
    assert error.value.status_code == status, error.value.detail


async def _scalar(factory: Factory, sql: str, **params: Any) -> Any:
    async with factory() as db:
        return (await db.execute(text(sql), params)).scalar_one()


def _body(**overrides: Any) -> StatementBody:
    (name_a, aff_a), (name_b, aff_b), (name_c, aff_c) = AUTHORS.values()
    body: dict[str, Any] = {
        "authors": [
            {
                "author_key": "a",
                "order": 1,
                "display_name": name_a,
                "affiliations": [aff_a],
                "email": EMAIL_A,
                "corresponding": True,
                "user_id": None,
                "orcid": ORCID_A,
                "credit_roles": ["conceptualization", "writing-original-draft"],
            },
            {
                "author_key": "b",
                "order": 2,
                "display_name": name_b,
                "affiliations": [aff_b],
                "orcid": ORCID_B,
                "credit_roles": ["formal-analysis"],
            },
            {
                "author_key": "c",
                "order": 3,
                "display_name": name_c,
                "affiliations": [aff_c],
                "credit_roles": ["data-curation"],
            },
        ],
        "funding": {
            "text": "Funded by the Fixture Foundation.",
            "grants": [{"funder": "Fixture Foundation", "award_id": AWARD}],
        },
        "conflicts": "None declared.",
        "ethics": None,
        "limitations": None,
        "data_availability": "Data are in the package.",
        "code_availability": "Code is in the package.",
        "licenses": {"text": "CC-BY-4.0", "data": "CC0-1.0", "code": None},
    }
    body.update(overrides)
    return cast(StatementBody, StatementBody.model_validate(body))


async def _version(w: Any, key: str, supersedes: Any, **overrides: Any) -> Any:
    body = _body(**overrides)
    assert body.authors is not None
    body.authors[0].user_id = w.ids["U"]
    result, _ = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: st.version_set(
            db,
            ctx,
            w.ids["O"],
            StatementSetCreate(
                body=body, supersedes_set_id=supersedes, idempotency_key=key
            ),
        ),
    )
    return result


def _approve(
    w: Any, user: str, statement_set: Any, author: str, method: str, key: str
) -> Awaitable[Any]:
    data = ApprovalCreate(
        author_key=author,
        set_hash=statement_set.set_hash,
        method=cast(Any, method),
        attestation_note=(
            None if method == "in_app_self" else f"Approved by email ({author})"
        ),
        idempotency_key=key,
    )
    return _as(
        w,
        user,
        EDIT,
        lambda db, ctx: st.approve(db, ctx, w.ids[user], statement_set.id, data),
    )


async def _approve_all(w: Any, statement_set: Any, tag: str) -> None:
    await _approve(w, "U", statement_set, "a", "in_app_self", f"{tag}-a")
    for author in ("b", "c"):
        await _approve(
            w, "O", statement_set, author, "recorded_attestation", f"{tag}-{author}"
        )


async def _candidate(w: Any, draft: UUID, content: str, key: str) -> Any:
    body = CandidateCreate(
        draft_id=draft, expected_content_hash=_sha(content), idempotency_key=key
    )
    result, _ = await _as(
        w, "O", EDIT, lambda db, ctx: mr.create_candidate(db, ctx, w.ids["O"], body)
    )
    return result


def _promote_release(
    w: Any, candidate: ManuscriptReleaseResponse, key: str
) -> Awaitable[tuple[ManuscriptReleaseResponse, bool]]:
    body = PromoteRequest(
        expected_snapshot_hash=candidate.snapshot_hash,
        expected_content_hash=candidate.content_hash,
        idempotency_key=key,
    )
    return _as(
        w,
        "A",
        RELEASE,
        lambda db, ctx: mr.promote(db, ctx, w.ids["A"], candidate.id, body),
    )


async def _failing(w: Any, candidate: Any, key: str) -> list[str]:
    with pytest.raises(mr.ObligationsNotMet) as refused:
        await _promote_release(w, candidate, key)
    return list(refused.value.failing)


async def _members(w: Any, release: UUID, variant: str) -> dict[str, bytes]:
    data, sha = await _as(
        w,
        "O",
        VIEW,
        lambda db, ctx: mr.package_bytes(db, ctx, release, cast(Any, variant)),
    )
    assert _sha(data) == sha
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


async def _checks(w: Any, release: UUID) -> list[Any]:
    listing = await _as(
        w, "O", VIEW, lambda db, ctx: st.list_venue_checks(db, ctx, release)
    )
    return list(listing.checks)


async def _seed_drafts(w: Any) -> None:
    """D1 (no Discussion) and D2 (with it), both citing one saved Citation
    and one document's metadata as [Doc 1] and [Doc 2]."""
    async with w.factory() as db:
        d1 = GeneratedDraft(
            project_id=w.p1, version=1, title="Results", content=D1, is_current=False
        )
        d2 = GeneratedDraft(
            project_id=w.p1, version=2, title="Results", content=D2, is_current=True
        )
        citation = Citation(
            document_title="Original fixture title",
            document_type="article-journal",
            authors=["Ada Lovelace"],
            year=2024,
            venue="Journal of Fixtures",
            doi="10.1000/fixture",
        )
        db.add_all([d1, d2, citation])
        await db.flush()
        for draft in (d1, d2):
            db.add_all(
                [
                    DraftCitation(
                        draft_id=draft.id, citation_index=1, citation_id=citation.id
                    ),
                    DraftCitation(
                        draft_id=draft.id, citation_index=2, document_id=w.doc
                    ),
                ]
            )
        await db.commit()
        w.d1, w.d2 = d1.id, d2.id


async def _seed_people(w: Any) -> None:
    """U: author-linked workspace editor, no project role; A renamed."""
    w.ids["U"] = uuid4()
    async with w.factory() as db:
        name, _ = AUTHORS["a"]
        first, last = name.split(" ", 1)
        db.add(
            User(
                id=w.ids["U"],
                email=f"{w.ids['U']}@test.invalid",
                password_hash="unused",
                first_name=first,
                last_name=last,
                organization_id=w.ids["org"],
            )
        )
        await db.flush()
        db.add(
            WorkspaceMember(
                workspace_id=w.ids["workspace"],
                user_id=w.ids["U"],
                role=WorkspaceRole.EDITOR,
            )
        )
        adjudicator = cast(Any, await db.get(User, w.ids["A"]))
        adjudicator.first_name, adjudicator.last_name = ADJUDICATOR.split(" ")
        await db.commit()


async def _review_round(w: Any) -> None:
    """One resolved comment from a reviewer with a display name."""
    round_, _ = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: reviews.create_round(
            db,
            ctx,
            w.ids["O"],
            RoundCreate(
                draft_id=w.d1,
                draft_content_hash=_sha(D1),
                label="Round 1",
                reviewers=[ReviewerCreate(label="Reviewer 1", display_name=REVIEWER)],
                idempotency_key="round-1",
            ),
        ),
    )
    comment, _ = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: reviews.add_comment(
            db,
            ctx,
            w.ids["O"],
            round_.id,
            CommentCreate(
                reviewer_id=round_.reviewers[0].id,
                number=1,
                body="Report the variance.",
                idempotency_key="comment-1",
            ),
        ),
    )
    root = cast(UUID, comment.comment_root_id)
    response, _ = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: reviews.respond(
            db,
            ctx,
            w.ids["O"],
            root,
            ResponseCreate(
                kind="no_change",
                body="The variance is in the figure.",
                rationale="Shown in Figure 1",
                idempotency_key="response-1",
            ),
        ),
    )
    await _as(
        w,
        "A",
        ADJUDICATE,
        lambda db, ctx: reviews.decide(
            db,
            ctx,
            w.ids["A"],
            root,
            DecisionCreate(
                kind="resolved", response_id=response.id, idempotency_key="resolve-1"
            ),
        ),
    )


async def _verified_claims(w: Any, figure: UUID) -> None:
    """A claim on each draft, linked to the figure, assessed, and each draft
    promoted (GOO-307), so claim support passes for both content hashes."""
    for draft, content, version, tag in ((w.d1, D1, 1, "1"), (w.d2, D2, 2, "2")):
        # One claim over all the prose: GOO-307 needs every assertion covered.
        prose = content[content.index("A fixture") :].rstrip("\n")
        claim = await _claim(w, draft, content, prose, f"c{tag}")
        link = await _link(w, claim, f"l{tag}", kind="figure", figure_id=figure)
        await _assess(w, claim, f"as{tag}", [link])
        await _promote(w, "A", draft, version, f"dr{tag}", _sha(content))


def _identity_strings(w: Any) -> list[str]:
    strings = [EMAIL_A, ORCID_A, ORCID_B, AWARD, REVIEWER, ADJUDICATOR]
    for name, affiliation in AUTHORS.values():
        strings += [name, affiliation]
    strings += [str(w.ids[k]) for k in ("O", "R", "A", "V", "U")]
    return strings


def _leaks(members: dict[str, bytes], strings: list[str]) -> list[tuple[str, str]]:
    return [
        (path, s)
        for path, data in members.items()
        for s in strings
        if s.lower() in data.decode("utf-8", errors="ignore").lower()
    ]


async def test_statements_identity_venue_anonymization_and_stale_checks(
    screening_factory: Factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = screening_factory
    storage = LocalArtifactStorage(tmp_path / "artifacts")
    sandbox = _FakeSandbox()
    monkeypatch.setattr(step_executor, "get_sandbox_manager", lambda: sandbox)
    monkeypatch.setattr(step_executor, "get_artifact_storage", lambda: storage)
    monkeypatch.setattr(experiment_service, "get_artifact_storage", lambda: storage)
    monkeypatch.setattr(mr, "get_artifact_storage", lambda: storage)
    monkeypatch.setattr(
        "src.api.research_engine.runs._build_connectors", lambda **_: object()
    )

    async def admitted(**_: Any) -> bool:
        return True

    monkeypatch.setattr("src.api.research_engine.runs.admit_expensive_work", admitted)
    monkeypatch.setenv("E2B_API_KEY", SECRET)

    w = await _setup(factory, tmp_path)
    run1, stream = await _run(w)
    assert "event: run_complete" in stream, stream
    async with factory() as db:
        figure_artifact = (
            await db.execute(
                text("""SELECT id FROM research_run_artifacts
                        WHERE run_id = :r AND role = 'output' AND name = 'fig.svg'"""),
                {"r": run1},
            )
        ).scalar_one()
    fig1, _ = await _register(w, "fig1", figure_artifact)
    await _seed_people(w)
    await _seed_drafts(w)
    await _review_round(w)
    await _verified_claims(w, fig1.id)

    # 8. Legacy first (no statement set yet): statements and venue are not
    # applicable and the GOO-315 obligations alone promote.
    legacy = await _candidate(w, w.d1, D1, "cand-legacy")
    assert legacy.checks["statements"].state == "not_applicable"
    assert legacy.checks["venue"].state == "not_applicable"
    assert legacy.failing_obligations == []
    legacy_verified, _ = await _promote_release(w, legacy, "promote-legacy")
    assert legacy_verified.stage == "verified"
    assert await _checks(w, legacy.id) == []
    # A GOO-315-shaped row (plain package_files list, no anonymized variant).
    old_id = uuid4()
    async with factory() as db:
        await db.execute(
            text("""INSERT INTO manuscript_releases
                    (id, collection_id, draft_id, draft_version, content_hash,
                     stage, snapshot, snapshot_hash, checks, checks_hash,
                     package_files, package_sha256, package_storage_key,
                     created_by_id, actor_role)
                    SELECT :new, collection_id, draft_id, draft_version,
                     content_hash, stage, snapshot, snapshot_hash, checks,
                     checks_hash, package_files->'identified', package_sha256,
                     package_storage_key, created_by_id, actor_role
                    FROM manuscript_releases WHERE id = :old"""),
            {"new": old_id, "old": legacy.id},
        )
        await db.commit()
    listed = await _as(w, "O", VIEW, lambda db, ctx: mr.list_releases(db, ctx))
    old = next(r for r in listed.releases if r.id == old_id)
    assert old.anonymized_sha256 is None and old.package_files == legacy.package_files
    await _refused(_members(w, old_id, "anonymized"), 404)
    old_check = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: mr.run_venue_check(
            db, ctx, w.ids["O"], old_id, VenueCheckCreate(idempotency_key="old")
        ),
    )
    assert old_check.rules["anonymization"] == "unknown"
    assert old_check.status == "fail"

    # 1. Statement set v1: explicit nulls are listed as missing.
    v1 = await _version(w, "v1", None)
    assert v1.missing_fields == ["ethics", "limitations"]
    assert v1.body["ethics"] is None and v1.body["limitations"] is None
    assert v1.grants_permissions is False

    # 2. ORCID: only a receipt for the linked user authenticates; a name (and
    # iD) match on another user's receipt does not; no iD is unknown.
    async with factory() as db:
        receipt = await st.record_orcid(
            db,
            w.ids["U"],
            {
                "orcid": ORCID_A,
                "name": AUTHORS["a"][0],
                "scope": "/authenticate",
                "access_token": ACCESS,
                "refresh_token": REFRESH,
            },
            environment="sandbox",
            client_id="APP-FIXTURE",
            state_hash="a" * 64,
        )
    async with factory() as db:
        await st.record_orcid(
            db,
            w.ids["O"],
            {"orcid": ORCID_B, "name": AUTHORS["b"][0], "scope": "/authenticate"},
            environment="sandbox",
            client_id="APP-FIXTURE",
            state_hash="b" * 64,
        )
    listing = await _as(w, "O", VIEW, lambda db, ctx: st.list_sets(db, ctx))
    assert listing.tip is not None
    views = {a.author_key: a for a in listing.tip.authors}
    assert views["a"].orcid_status == "authenticated"
    assert views["a"].orcid_receipt is not None
    assert views["a"].orcid_receipt.authentication_id == receipt.id
    assert views["a"].orcid_receipt.environment == "sandbox"
    assert (views["b"].orcid_status, views["b"].orcid_receipt) == (
        "unauthenticated",
        None,
    )
    assert views["c"].orcid_status == "unknown"
    for secret in (ACCESS, REFRESH):
        hits = await _scalar(
            factory,
            """SELECT (SELECT count(*) FROM orcid_authentications o
                       WHERE o::text LIKE :p)
                    + (SELECT count(*) FROM research_decision_events e
                       WHERE e.payload::text LIKE :p)""",
            p=f"%{secret}%",
        )
        assert hits == 0

    # 3. Approvals: self-approval only by the author's own user.
    await _approve(w, "U", v1, "a", "in_app_self", "v1-a")
    await _refused(_approve(w, "U", v1, "b", "in_app_self", "v1-b-self"), 403)
    await _refused(_approve(w, "O", v1, "c", "in_app_self", "v1-c-self"), 403)
    for author in ("b", "c"):
        await _approve(w, "O", v1, author, "recorded_attestation", f"v1-{author}")
    listing = await _as(w, "O", VIEW, lambda db, ctx: st.list_sets(db, ctx))
    assert listing.tip is not None and listing.tip.all_approved
    methods = {
        a.author_key: a.approval.method for a in listing.tip.authors if a.approval
    }
    assert methods == {
        "a": "in_app_self",
        "b": "recorded_attestation",
        "c": "recorded_attestation",
    }

    # 4. Authorship grants no role; a role adds no authorship.
    async def resolve(action: ResearchAction) -> None:
        async with factory() as db:
            await resolve_project(db, w.p1, w.ids["U"], action)

    await _refused(resolve(ADJUDICATE), 403)
    await _refused(resolve(REVIEW), 403)
    async with factory() as db:
        db.add(
            ResearchProjectRoleAssignment(
                collection_id=w.p1,
                user_id=w.ids["U"],
                role=ResearchProjectRole.REVIEWER,
                assigned_by_id=w.ids["O"],
            )
        )
        await db.commit()
    await resolve(REVIEW)
    sets_sql = "SELECT count(*) || ':' || max(set_hash) FROM manuscript_statement_sets"
    assert await _scalar(factory, sets_sql) == f"1:{v1.set_hash}"
    async with factory() as db:
        await db.execute(
            text("""DELETE FROM research_project_role_assignments
                    WHERE user_id = :u"""),
            {"u": w.ids["U"]},
        )
        await db.commit()
    await _refused(resolve(REVIEW), 403)
    assert await _scalar(factory, sets_sql) == f"1:{v1.set_hash}"

    # 5. Candidate 1 binds v1; the automatic venue check fails with fixes;
    # the anonymized variant holds no identity string.
    c1 = await _candidate(w, w.d1, D1, "cand-1")
    snapshot = await _scalar(
        factory, "SELECT snapshot FROM manuscript_releases WHERE id = :r", r=c1.id
    )
    assert snapshot["statements"]["statement_set_id"] == str(v1.id)
    assert snapshot["statements"]["set_hash"] == v1.set_hash
    assert c1.checks["statements"].state == "pass"
    (check1,) = await _checks(w, c1.id)
    assert check1.status == "fail" and check1.package_sha256 == c1.package_sha256
    assert (check1.profile_id, check1.profile_version) == ("generic-icmje-credit", 1)
    assert {i.field for i in check1.items} == {
        "ethics",
        "limitations",
        "heading:Discussion",
    }
    assert all(i.fix for i in check1.items)
    assert c1.checks["venue"].state == "fail"
    assert "venue" in c1.failing_obligations
    identified = await _members(w, c1.id, "identified")
    anonymized = await _members(w, c1.id, "anonymized")
    assert c1.anonymized_sha256 is not None
    assert set(anonymized) == set(identified)
    strings = _identity_strings(w)
    assert _leaks(anonymized, strings) == []
    assert "Acknowledg" not in anonymized["manuscript.md"].decode()
    statements_part = identified["statements.json"].decode()
    assert all(name in statements_part for name, _ in AUTHORS.values())
    assert "venue" in await _failing(w, c1, "promote-1")

    # 6. v2 fills the nulls; v1 approvals do not carry over.
    v2 = await _version(
        w, "v2", v1.id, ethics="Approved by the fixture board.", limitations="Small n."
    )
    assert v2.missing_fields == [] and not v2.all_approved
    c2a = await _candidate(w, w.d2, D2, "cand-2a")
    assert c2a.checks["statements"].state == "fail"
    assert await _failing(w, c2a, "promote-2a") == ["statements"]
    await _approve_all(w, v2, "v2")
    c2 = await _candidate(w, w.d2, D2, "cand-2")
    (check2,) = await _checks(w, c2.id)
    assert check2.status == "pass", check2.items
    verified, _ = await _promote_release(w, c2, "promote-2")
    assert (verified.stage, verified.status) == ("verified", "verified")
    assert verified.checks["statements"].state == "pass"
    assert verified.checks["venue"].state == "pass"
    assert verified.anonymized_sha256 == c2.anonymized_sha256

    # 7. Stale check: candidate 2's passing check replayed onto candidate 3
    # (another package hash) never authorizes it.
    v3 = await _version(
        w,
        "v3",
        v2.id,
        ethics="Approved by the fixture board.",
        limitations="Small n.",
        licenses={"text": "MIT", "data": None, "code": None},
    )
    await _approve_all(w, v3, "v3")
    c3 = await _candidate(w, w.d2, D2, "cand-3")
    assert c3.package_sha256 != c2.package_sha256
    async with factory() as db:
        await db.execute(
            text("""INSERT INTO venue_checks
                    (id, collection_id, release_id, profile_id, profile_version,
                     package_sha256, anonymized_sha256, result, status,
                     checked_by_id)
                    SELECT :id, collection_id, :c3, profile_id, profile_version,
                     package_sha256, anonymized_sha256, result, status,
                     checked_by_id
                    FROM venue_checks WHERE id = :c2check"""),
            {"id": uuid4(), "c3": c3.id, "c2check": check2.id},
        )
        await db.commit()
    assert await _failing(w, c3, "promote-3") == ["venue"]
    fresh = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: mr.run_venue_check(
            db, ctx, w.ids["O"], c3.id, VenueCheckCreate(idempotency_key="c3")
        ),
    )
    assert fresh.package_sha256 == c3.package_sha256
    assert [i.field for i in fresh.items] == ["licenses.text"]
    assert await _failing(w, c3, "promote-3b") == ["venue"]
    listed = await _as(w, "O", VIEW, lambda db, ctx: mr.list_releases(db, ctx))
    again = next(r for r in listed.releases if r.id == verified.id)
    assert again.status == "verified"
    assert (again.snapshot_hash, again.package_sha256) == (
        verified.snapshot_hash,
        verified.package_sha256,
    )

    # 9. Insert-only and replay.
    for table in (
        "manuscript_statement_sets",
        "manuscript_statement_approvals",
        "orcid_authentications",
        "venue_checks",
    ):
        for statement in (
            f"UPDATE {table} SET created_at = created_at",
            f"DELETE FROM {table}",
        ):
            async with factory() as db:
                with pytest.raises(DBAPIError) as blocked:
                    await db.execute(text(statement))
                assert getattr(blocked.value.orig, "sqlstate", None) == "55000"
    async with factory() as db:
        events = await replay_decisions(
            db,
            collection_id=w.p1,
            aggregate_type="research_statements",
            aggregate_id=w.p1,
        )
    types = [str(e.event_type) for e in events]
    assert types.count("statements.versioned") == 3
    assert types.count("statements.approved") == 9
    assert (
        types.count("venue.checked")
        == await _scalar(factory, "SELECT count(*) FROM venue_checks") - 1
    )  # the replayed row was inserted directly, outside the ledger

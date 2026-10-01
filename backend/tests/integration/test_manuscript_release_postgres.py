"""Real PostgreSQL proof for GOO-315 candidate and verified manuscript releases.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now
ends at ``e4c6a8b0d2f3``: ``manuscript_releases``, its CHECKs, the
``UNIQUE(candidate_release_id)`` backstop and the insert-only trigger come
from the migration, not ``Base.metadata``. Seeds: GOO-312's experiment run
and figure (``test_run_manifest_postgres``), GOO-307's claim, link,
assessment and draft promotion helpers, and one GOO-314 round. GOO-299's
users: owner O (no roles), adjudicator A.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-315 section):
the snapshot rebuild equality skipped (step 5), the existing-verified check
plus ``UNIQUE(candidate_release_id)`` removed (step 2's concurrent promote),
``verify`` re-reading the live ``Citation`` (step 4).

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_manuscript_release_postgres.py``.
"""

import asyncio
import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeAlias, TypeVar, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.citation import Citation
from src.models.draft_citation import DraftCitation
from src.models.generated_draft import GeneratedDraft
from src.models.research_protocol import ResearchProtocol, ResearchProtocolVersion
from src.schemas.research_engine import ProtocolDeviationCreate
from src.services.artifacts.storage import LocalArtifactStorage
from src.services.research import claims_service as claims
from src.services.research import manuscript_release_service as mr
from src.services.research import manuscript_rules
from src.services.research import peer_review_service as reviews
from src.services.research.draft_generation_service import (
    DraftGenerationService,
    DraftRetainedError,
)
from src.services.research_decisions import replay_decisions
from src.services.research_engine import (
    experiment_service,
    protocol_service,
    step_executor,
)
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from src.shared.claim_schemas import ClaimVersionCreate
from src.shared.manuscript_release_schemas import (
    CandidateCreate,
    ManuscriptReleaseResponse,
    PromoteRequest,
    ReleaseVerification,
)
from src.shared.peer_review_schemas import (
    CommentCreate,
    DecisionCreate,
    ResponseCreate,
    ReviewerCreate,
    RoundCreate,
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
S1 = "Mean y rose across the three conditions (Figure 1) [Doc 1] [Doc 2]."
SHORT = "Mean y rose across the three conditions"
V1 = f"## Results\n{S1}\n"
PLAIN = "## Notes\nA plain note.\n"
TITLE = "Original fixture title"


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


async def _refused(awaitable: Awaitable[Any], status: int, detail: str = "") -> None:
    with pytest.raises(HTTPException) as error:
        await awaitable
    assert error.value.status_code == status, error.value.detail
    assert detail in str(error.value.detail)


async def _scalar(factory: Factory, sql: str, **params: Any) -> Any:
    async with factory() as db:
        return (await db.execute(text(sql), params)).scalar_one()


async def _candidate(w: Any, draft: UUID, content: str, key: str) -> Any:
    body = CandidateCreate(
        draft_id=draft, expected_content_hash=_sha(content), idempotency_key=key
    )
    result, _ = await _as(
        w, "O", EDIT, lambda db, ctx: mr.create_candidate(db, ctx, w.ids["O"], body)
    )
    return result


def _promote_release(
    w: Any, user: str, candidate: ManuscriptReleaseResponse, key: str
) -> Awaitable[tuple[ManuscriptReleaseResponse, bool]]:
    body = PromoteRequest(
        expected_snapshot_hash=candidate.snapshot_hash,
        expected_content_hash=candidate.content_hash,
        idempotency_key=key,
    )
    return _as(
        w,
        user,
        RELEASE,
        lambda db, ctx: mr.promote(db, ctx, w.ids[user], candidate.id, body),
    )


def _verify(w: Any, release: UUID) -> Awaitable[ReleaseVerification]:
    return _as(w, "O", VIEW, lambda db, ctx: mr.verify(db, ctx, release))


async def _members(w: Any, release: UUID) -> dict[str, bytes]:
    data, sha = await _as(
        w, "O", VIEW, lambda db, ctx: mr.package_bytes(db, ctx, release)
    )
    assert _sha(data) == sha
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


async def _listing(w: Any) -> dict[UUID, ManuscriptReleaseResponse]:
    listing = await _as(w, "O", VIEW, lambda db, ctx: mr.list_releases(db, ctx))
    return {r.id: r for r in listing.releases}


async def _export(w: Any, draft: UUID) -> bytes:
    async with w.factory() as db:
        result = await DraftGenerationService(db).export_draft(
            w.p1, draft, format="markdown"
        )
    return cast(str, result["content"]).encode("utf-8")


async def _seed_drafts(w: Any) -> None:
    """Draft v1 cites one saved Citation and one document's metadata; a
    plain draft carries nothing but a release (for retention)."""
    async with w.factory() as db:
        v1 = GeneratedDraft(
            project_id=w.p1, version=1, title="Results", content=V1, is_current=True
        )
        plain = GeneratedDraft(
            project_id=w.p1, version=3, title="Plain", content=PLAIN, is_current=False
        )
        citation = Citation(
            document_title=TITLE,
            document_type="article-journal",
            authors=["Ada Lovelace", "Grace Hopper"],
            year=2024,
            venue="Journal of Fixtures",
            doi="10.1000/fixture",
            snippet="an evidence snippet, never a title",
        )
        db.add_all([v1, plain, citation])
        await db.flush()
        db.add_all(
            [
                DraftCitation(
                    draft_id=v1.id,
                    citation_index=1,
                    citation_id=citation.id,
                    snippet="an evidence snippet, never a title",
                ),
                DraftCitation(draft_id=v1.id, citation_index=2, document_id=w.doc),
            ]
        )
        await db.commit()
        w.v1, w.plain, w.citation = v1.id, plain.id, citation.id


async def _open_comment(w: Any) -> UUID:
    round_, _ = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: reviews.create_round(
            db,
            ctx,
            w.ids["O"],
            RoundCreate(
                draft_id=w.v1,
                draft_content_hash=_sha(V1),
                label="Round 1",
                reviewers=[ReviewerCreate(label="Reviewer 1")],
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
    return cast(UUID, comment.comment_root_id)


async def _resolve(w: Any, root: UUID) -> None:
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


def _codes(check: Any) -> list[tuple[str, str | None]]:
    return [(item.code, item.ref) for item in check.items]


async def test_candidate_verified_reproducible_and_refusals(
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

    # Seed: one protocol-bound run with a figure, draft v1 citing it through a
    # claim (not yet assessed), two references, one open review comment.
    w = await _setup(factory, tmp_path)
    run1, stream = await _run(w)
    assert "event: run_complete" in stream, stream
    async with factory() as db:
        outputs = {
            name: artifact_id
            for artifact_id, name in (
                await db.execute(
                    text("""SELECT id, name FROM research_run_artifacts
                            WHERE run_id = :r AND role = 'output'"""),
                    {"r": run1},
                )
            ).all()
        }
    fig1, _ = await _register(w, "fig1", outputs["fig.svg"])
    await _seed_drafts(w)
    claim1 = await _claim(w, w.v1, V1, S1, "c1")
    link1 = await _link(w, claim1, "l1", kind="figure", figure_id=fig1.id)
    root = await _open_comment(w)
    baseline = await _export(w, w.v1)

    # 1. Candidate A: claim support and peer review fail; the package reconciles.
    a = await _candidate(w, w.v1, V1, "cand-a")
    assert (a.stage, a.status, a.external_submission) == (
        "candidate",
        "candidate",
        "not_authorized",
    )
    assert a.checks["claim_support"].state == "fail"
    assert ("unassessed", str(claim1.version.id)) in _codes(a.checks["claim_support"])
    assert a.checks["peer_review"].state == "fail"
    assert _codes(a.checks["peer_review"]) == [("open_comment", str(root))]
    assert a.checks["experiment_reproducibility"].state == "unknown"
    assert a.checks["method_adherence"].state == "pass"
    assert a.checks["synthesis_appraisal"].state == "not_applicable"
    assert a.failing_obligations == ["claim_support", "peer_review"]
    members = await _members(w, a.id)
    assert {f.path: f.sha256 for f in a.package_files} == {
        path: _sha(data) for path, data in members.items()
    }
    assert {
        "manuscript.md",
        "manuscript.source.md",
        "references.bib",
        "snapshot.json",
        "checks.json",
        "methods.json",
        "figures/fig-1.svg",
        "figures/lineage.json",
        "manifest.json",
        "SHA256SUMS",
    } <= set(members)
    assert members["manuscript.md"] == baseline
    assert _sha(members["manuscript.source.md"]) == a.content_hash == _sha(V1)
    assert TITLE in members["references.bib"].decode()
    assert "evidence snippet" not in members["references.bib"].decode()
    with pytest.raises(mr.ObligationsNotMet) as refused:
        await _promote_release(w, "A", a, "promote-a")
    assert refused.value.failing == ["claim_support", "peer_review"]
    assert (await _listing(w))[a.id] == a
    assert (
        await _scalar(
            factory, "SELECT count(*) FROM manuscript_releases WHERE stage = 'verified'"
        )
        == 0
    )
    # 10. The ad-hoc export is unchanged by packaging.
    assert await _export(w, w.v1) == baseline

    # 2. Candidate B after the claim is assessed, the draft promoted (GOO-307)
    # and the comment resolved; two concurrent promotions, one verified row.
    await _assess(w, claim1, "as1", [link1])
    draft_release, _ = await _promote(w, "A", w.v1, 1, "dr1", _sha(V1))
    await _resolve(w, root)
    b = await _candidate(w, w.v1, V1, "cand-b")
    assert b.snapshot_hash != a.snapshot_hash
    assert b.failing_obligations == []
    assert b.checks["claim_support"].state == "pass"
    assert b.checks["peer_review"].state == "pass"
    results = await asyncio.gather(
        _promote_release(w, "A", b, "promote-b1"),
        _promote_release(w, "A", b, "promote-b2"),
    )
    assert sorted(replayed for _, replayed in results) == [False, True]
    assert len({release.id for release, _ in results}) == 1
    verified = results[0][0]
    assert (verified.stage, verified.status) == ("verified", "verified")
    assert verified.package_sha256 == b.package_sha256
    assert verified.package_files == b.package_files
    assert verified.candidate_release_id == b.id
    assert verified.draft_release_id == draft_release.id
    assert verified.actor_role == "adjudicator"
    assert (
        await _scalar(
            factory,
            "SELECT count(*) FROM manuscript_releases WHERE candidate_release_id = :c",
            c=b.id,
        )
        == 1
    )
    await _refused(_promote_release(w, "O", b, "promote-owner"), 403)

    # 3. Reproduce after reload: every hash and the docN mapping recompute.
    check = await _verify(w, verified.id)
    assert check.package_sha256_ok and check.bundle_ok, check.bundle_error
    assert check.references_ok and all(m.ok for m in check.members)
    snapshot = await _scalar(
        factory, "SELECT snapshot FROM manuscript_releases WHERE id = :r", r=b.id
    )
    keys = [r["key"] for r in snapshot["references"]]
    assert keys == ["doc1", "doc2"] == [m.key for m in check.reference_mapping]
    first, second = snapshot["references"]
    assert (first["title"], first["type"], first["source"]) == (
        TITLE,
        "article-journal",
        "citation",
    )
    assert first["authors"] == ["Ada Lovelace", "Grace Hopper"]
    assert second["source"] == "document_metadata"
    assert snapshot["claims"]["draft_release_id"] == str(draft_release.id)
    assert [f["figure_id"] for f in snapshot["figures"]] == [str(fig1.id)]

    # 4. Upstream edits: a citation title and a superseded figure.
    async with factory() as db:
        await db.execute(
            text("UPDATE citations SET document_title = 'Edited' WHERE id = :c"),
            {"c": w.citation},
        )
        await db.commit()
    again = await _verify(w, verified.id)
    assert again.references_ok and again.package_sha256_ok
    assert again.reference_mapping == check.reference_mapping
    bib = (await _members(w, verified.id))["references.bib"].decode()
    assert TITLE in bib and "Edited" not in bib
    await _register(w, "fig2", outputs["table.csv"], supersedes=fig1.id)
    listed = await _listing(w)
    assert listed[verified.id].status == "stale"
    assert listed[verified.id].stale_cause == "draft_release_invalidated"
    assert listed[b.id].status == "candidate"
    after = await _verify(w, verified.id)
    assert after.package_sha256_ok and after.bundle_ok and after.references_ok

    # 5. Stale candidate: a claim version superseded after the build.
    c = await _candidate(w, w.v1, V1, "cand-c")
    start = V1.index(SHORT)
    await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: claims.create_version(
            db,
            ctx,
            claim1.id,
            w.ids["O"],
            ClaimVersionCreate(
                draft_id=w.v1,
                start_char=start,
                end_char=start + len(SHORT),
                text=SHORT,
                supersedes_claim_version_id=claim1.version.id,
                idempotency_key="c1-v2",
            ),
        ),
    )
    await _refused(_promote_release(w, "A", c, "promote-c"), 409, mr.CANDIDATE_STALE)

    # 6. Conditional methods: an unamended deviation fails method adherence,
    # whatever its free-text disposition; an approved amendment covers it.
    async with factory() as db:
        version = cast(Any, await db.get(ResearchProtocolVersion, w.version))
        deviation = await protocol_service.record_deviation(
            db,
            cast(UUID, version.protocol_id),
            w.p1,
            w.ids["O"],
            ProtocolDeviationCreate(
                protocol_version_id=w.version,
                run_id=run1,
                observed_difference="Seed changed after approval",
                rationale="Sandbox default",
                disposition="approved",
            ),
        )
    d = await _candidate(w, w.v1, V1, "cand-d")
    assert d.checks["method_adherence"].state == "fail"
    assert set(_codes(d.checks["method_adherence"])) == {
        ("run_not_conformant", str(run1)),
        ("deviation_unamended", str(deviation.id)),
    }
    with pytest.raises(mr.ObligationsNotMet) as refused:
        await _promote_release(w, "A", d, "promote-d")
    assert "method_adherence" in refused.value.failing
    async with factory() as db:
        parent = cast(Any, await db.get(ResearchProtocolVersion, w.version))
        amendment = ResearchProtocolVersion(
            id=uuid4(),
            protocol_id=parent.protocol_id,
            version=parent.version + 1,
            parent_version_id=parent.id,
            question_version_id=parent.question_version_id,
            blueprint_id=parent.blueprint_id,
            execution_plan=parent.execution_plan,
            snapshot=parent.snapshot,
            content_hash=_sha("amended"),
            status="approved",
            change_kind="amendment",
            amendment_reason="Cover the seed deviation",
            author_user_id=w.ids["O"],
            approved_by_user_id=w.ids["O"],
        )
        db.add(amendment)
        await db.flush()
        protocol = cast(Any, await db.get(ResearchProtocol, parent.protocol_id))
        protocol.current_approved_version_id = amendment.id
        await db.commit()
    e = await _candidate(w, w.v1, V1, "cand-e")
    assert e.checks["method_adherence"].state == "pass", e.checks["method_adherence"]

    # 8. Retention: a packaged version is never deleted.
    await _candidate(w, w.plain, PLAIN, "cand-plain")
    async with factory() as db:
        with pytest.raises(DraftRetainedError):
            await DraftGenerationService(db).delete_draft(w.p1, w.plain)

    # 9. Insert-only and replay.
    for statement in (
        "UPDATE manuscript_releases SET created_at = created_at",
        "DELETE FROM manuscript_releases",
    ):
        async with factory() as db:
            with pytest.raises(DBAPIError) as blocked:
                await db.execute(text(statement))
            assert getattr(blocked.value.orig, "sqlstate", None) == "55000"
    async with factory() as db:
        events = await replay_decisions(
            db,
            collection_id=w.p1,
            aggregate_type="research_manuscript",
            aggregate_id=w.p1,
        )
        rows = (
            await db.execute(
                select(text("stage")).select_from(text("manuscript_releases"))
            )
        ).all()
    types = [str(e.event_type) for e in events]
    assert types.count("manuscript.verified") == 1
    assert types.count("manuscript.candidate_created") == len(rows) - 1
    assert (
        manuscript_rules.PACKAGE_SCHEMA
        in (await _members(w, b.id))["manifest.json"].decode()
    )

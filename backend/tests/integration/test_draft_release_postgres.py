"""Real PostgreSQL proof for GOO-307 verified draft gating and invalidation.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now
ends at ``d7f9b1c3e5a8``: ``draft_releases``, its partial unique index and
CHECKs come from the migration, not ``Base.metadata``. GOO-299's seed: owner
O (no roles), reviewer R, adjudicator A (J in the plan), foreign-org F; this
file adds supervisor S.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-307 section):

- the content-hash binding in ``promote`` skipped: step 5's wrong-hash
  promotion is recorded instead of refused;
- the ``stale_at IS NULL`` predicate dropped from ``invalidate_dependents``:
  step 9's ``release.staled`` names v1's already-stale first release again;
- ``dependents`` walking from every node: step 2's candidate blockers
  already include ``stale_evidence`` (and the unit ``-k selective`` fails);
- the post-lock role reload in ``resolve_project``: the ``committed-first``
  race promotes after the revocation;
- the live-release short-circuit alone survives by design: the
  ``uq_draft_releases_live`` backstop answers the same replay. With the
  backstop re-raising as well, step 6 dies with an unhandled IntegrityError.

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_draft_release_postgres.py``.
"""

import asyncio
import hashlib
import importlib.util
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, TypeAlias, TypeVar, cast
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from fastapi.responses import Response
from sqlalchemy import select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research import drafts as drafts_api
from src.api.research import extraction_matrix as matrix_routes
from src.models.collection import CollectionDocument
from src.models.document import Document, DocumentType
from src.models.draft_citation import DraftCitation
from src.models.draft_task_result import DraftTaskResult
from src.models.evidence import StanceClassificationModel, StanceEnum
from src.models.extraction_matrix import ExtractionMatrix
from src.models.generated_draft import GeneratedDraft
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.user import User
from src.models.workspace import WorkspaceMember, WorkspaceRole
from src.services.research import claim_rules
from src.services.research import claims_service as claims
from src.services.research import draft_release_service as svc
from src.services.research import extraction_forms_service as forms
from src.services.research import release_rules as rr
from src.services.research.draft_generation_service import (
    DraftGenerationService,
    DraftRetainedError,
)
from src.services.research_decisions import replay_decisions
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from src.services.threads import workspace_service
from src.shared.claim_schemas import (
    ClaimAssessmentCreate,
    ClaimCreate,
    ClaimLinkCreate,
    StanceObservationCreate,
)
from src.shared.research_schemas import DraftPromoteRequest
from src.shared.scispace_schemas import (
    ExtractionAcceptCreate,
    ExtractionObservationCreate,
)
from tests.integration.test_report_identity_postgres import _seed, _wait_until_blocked
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    VERSIONS,
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
T = TypeVar("T")
D1_TEXT = (
    "Methods. We enrolled 412 participants. "
    "Mortality fell by twelve percent in the treated arm. Discussion follows."
)
D2_TEXT = "Design. This was a randomized crossover trial. Nothing else."
SAMPLE = "We enrolled 412 participants."
DESIGN = "This was a randomized crossover trial."
QUOTE = "Mortality fell by twelve percent in the treated arm."
S1 = "The trial enrolled 412 participants [Doc 1]."
S2 = "The effect remains uncertain in the crossover trial [Doc 2]."
S3 = "We read this as a promising signal."
S4 = "Mortality fell by 40 percent [Doc 1]."  # dev-unsupported-number
S5 = "Earlier reviews agreed [Doc 1]."
V1 = f"## Results\n{S1} {S2} {S3} {S4} {S5}\n"
V2_S = "The effect remains uncertain in the crossover trial [Doc 1]."
V2 = f"## Results\n{V2_S}\n"
EDIT, VIEW, ADJUDICATE, RELEASE = (
    ResearchAction.EDIT,
    ResearchAction.VIEW,
    ResearchAction.ADJUDICATE,
    ResearchAction.RELEASE,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _user(user_id: UUID) -> Any:
    return SimpleNamespace(id=user_id)


async def _as(
    factory: Factory,
    w: Any,
    user: str,
    action: ResearchAction,
    call: Callable[[AsyncSession, ProjectContext], Awaitable[T]],
) -> T:
    """One route-shaped request: resolve, then the service (which commits)."""
    async with factory() as db:
        context = await resolve_project(db, w.p1, w.ids[user], action)
        return await call(db, context)


async def _refused(awaitable: Awaitable[Any], status: int, detail: str = "") -> None:
    with pytest.raises(HTTPException) as error:
        await awaitable
    assert error.value.status_code == status, error.value.detail
    assert detail in str(error.value.detail)


async def _count(factory: Factory, sql: str, **params: Any) -> int:
    async with factory() as db:
        return int((await db.execute(text(sql), params)).scalar_one())


async def _document(db: AsyncSession, org: UUID, name: str, body: str) -> UUID:
    document = Document(
        title=name,
        filename=f"{name}.pdf",
        file_path="local:///d.pdf",
        file_size_bytes=1,
        mime_type="application/pdf",
        document_type=DocumentType.PDF,
        organization_id=org,
        checksum_sha256=_sha(body),
        content_text=body,
    )
    db.add(document)
    await db.flush()
    return cast(UUID, document.id)


async def _add_supervisor(factory: Factory, ids: dict[str, UUID]) -> None:
    ids["S"] = uuid4()
    async with factory() as db:
        db.add(
            User(
                id=ids["S"],
                email=f"{ids['S']}@test.invalid",
                password_hash="unused",
                first_name="Test",
                last_name="S",
                organization_id=ids["org"],
            )
        )
        await db.flush()
        db.add(
            WorkspaceMember(
                workspace_id=ids["workspace"],
                user_id=ids["S"],
                role=WorkspaceRole.VIEWER,
            )
        )
        db.add(
            ResearchProjectRoleAssignment(
                collection_id=ids["collection"],
                user_id=ids["S"],
                role=ResearchProjectRole.SUPERVISOR,
                assigned_by_id=ids["O"],
            )
        )
        await db.commit()


def _body(content_hash: str, key: str) -> DraftPromoteRequest:
    return DraftPromoteRequest(content_hash=content_hash, idempotency_key=key)


def _promote(w: Any, user: str, draft: UUID, version: int, key: str, h: str) -> Any:
    return _as(
        w.factory,
        w,
        user,
        RELEASE,
        lambda db, ctx: svc.promote(
            db, ctx, w.ids[user], draft, version, _body(h, key)
        ),
    )


def _check(w: Any, draft: UUID, version: int) -> Any:
    return _as(
        w.factory, w, "O", VIEW, lambda db, ctx: svc.check(db, ctx, draft, version)
    )


async def _accept(
    w: Any, field: str, document: UUID, value: str, citation: str, key: str
) -> tuple[UUID, UUID]:
    """A verified human observation and its acceptance: (accepted, observation)."""
    async with w.factory() as db:
        observation = await matrix_routes.create_observation(
            w.matrix,
            ExtractionObservationCreate(
                document_id=document,
                field_id=w.fields[field],
                form_version_id=w.form_version,
                value=value,
                citation=citation,
                idempotency_key=f"obs-{key}",
            ),
            current_user=_user(w.ids["R"]),
            db=db,
        )
    async with w.factory() as db:
        accepted = await matrix_routes.accept_extraction_value(
            w.matrix,
            ExtractionAcceptCreate(
                document_id=document,
                field_id=w.fields[field],
                form_version_id=w.form_version,
                observation_ids=[observation.id],
                value=value,
                rationale="methods section",
                idempotency_key=f"acc-{key}",
            ),
            current_user=_user(w.ids["A"]),
            db=db,
        )
    return cast(UUID, accepted.id), cast(UUID, observation.id)


async def _claim(
    w: Any, draft: UUID, content: str, passage: str, key: str, kind: str = "factual"
) -> Any:
    start = content.index(passage)
    return (
        await _as(
            w.factory,
            w,
            "O",
            EDIT,
            lambda db, ctx: claims.create_claim(
                db,
                ctx,
                w.ids["O"],
                ClaimCreate(
                    draft_id=draft,
                    start_char=start,
                    end_char=start + len(passage),
                    text=passage,
                    kind=cast(Any, kind),
                    idempotency_key=key,
                ),
            ),
        )
    )[0]


async def _link(w: Any, claim: Any, key: str, **data: Any) -> UUID:
    row, _ = await _as(
        w.factory,
        w,
        "O",
        EDIT,
        lambda db, ctx: claims.link(
            db,
            ctx,
            claim.id,
            w.ids["O"],
            ClaimLinkCreate(
                claim_version_id=claim.version.id, idempotency_key=key, **data
            ),
        ),
    )
    return cast(UUID, row.id)


async def _assess(
    w: Any,
    claim: Any,
    key: str,
    links: list[UUID],
    stance: str = "supporting",
    **kw: Any,
) -> UUID:
    row, _ = await _as(
        w.factory,
        w,
        "A",
        ADJUDICATE,
        lambda db, ctx: claims.assess(
            db,
            ctx,
            claim.id,
            w.ids["A"],
            ClaimAssessmentCreate(
                claim_version_id=claim.version.id,
                stance=cast(Any, stance),
                link_ids=links,
                rationale="adjudicated",
                idempotency_key=key,
                **kw,
            ),
        ),
    )
    return cast(UUID, row.id)


def _span_link(document: UUID, text_: str, quote: str) -> dict[str, Any]:
    start = text_.index(quote)
    return {
        "kind": "source_span",
        "document_id": document,
        "start_char": start,
        "end_char": start + len(quote),
        "quote": quote,
    }


async def _setup(factory: Factory) -> Any:
    ids = await _seed(factory)
    await _add_supervisor(factory, ids)
    p1 = ids["collection"]
    async with factory() as db:
        d1 = await _document(db, ids["org"], "d1", D1_TEXT)
        d2 = await _document(db, ids["org"], "d2", D2_TEXT)
        for document in (d1, d2):
            db.add(CollectionDocument(collection_id=p1, document_id=document))
        draft = GeneratedDraft(
            project_id=p1, version=1, title="v1", content=V1, is_current=True
        )
        db.add(draft)
        await db.flush()
        citation = DraftCitation(draft_id=draft.id, citation_index=1, document_id=d1)
        db.add(citation)
        db.add(
            DraftTaskResult(
                task_id="task-v1",
                collection_id=p1,
                actor_user_id=ids["O"],
                state="completed",
                artifact_id=draft.id,
                artifact_version=1,
                artifact_hash=_sha(V1),
                request_fingerprint="f" * 64,
            )
        )
        db.add(
            StanceClassificationModel(
                claim_hash=claim_rules.normalized_hash(S4),
                claim_text=S4,
                source_id=d1,
                source_content_hash=_sha(D1_TEXT),
                organization_id=ids["org"],
                stance=StanceEnum.SUPPORTING,
                confidence=0.62,
                justification_excerpt=QUOTE,
                model_version=claims._classifier.classifier_version,
                inference_model_version="stub-model-1",
            )
        )
        await db.commit()
    columns = [{"name": "Sample size"}, {"name": "Design"}]
    async with factory() as db:
        context = await resolve_project(db, p1, ids["O"], EDIT)
        matrix = ExtractionMatrix(project_id=p1, name="release", columns=columns)
        db.add(matrix)
        version = await forms.create_version(db, context, matrix, columns, ids["O"])
        fields = {f["name"]: UUID(f["field_id"]) for f in version.fields}
        matrix_id, version_id = cast(UUID, matrix.id), cast(UUID, version.id)
    w = SimpleNamespace(
        factory=factory,
        ids=ids,
        p1=p1,
        d1=d1,
        d2=d2,
        v1=cast(UUID, draft.id),
        citation=cast(UUID, citation.id),
        matrix=matrix_id,
        form_version=version_id,
        fields=fields,
    )
    w.sample, w.sample_obs = await _accept(w, "Sample size", d1, "412", SAMPLE, "s")
    w.design, _ = await _accept(w, "Design", d2, "randomized crossover", DESIGN, "d")
    return w


async def _events(factory: Factory, event_type: str) -> list[dict[str, Any]]:
    async with factory() as db:
        rows = await db.execute(
            text("""SELECT payload FROM research_decision_events
                    WHERE event_type = :t ORDER BY seq"""),
            {"t": event_type},
        )
        return [dict(r) for r in rows.scalars()]


async def _row(factory: Factory, release: UUID) -> dict[str, Any]:
    async with factory() as db:
        return dict(
            (
                await db.execute(
                    text("SELECT * FROM draft_releases WHERE id = :id"), {"id": release}
                )
            )
            .mappings()
            .one()
        )


async def _export(factory: Factory, w: Any, draft: UUID) -> str:
    async with factory() as db:
        result = await DraftGenerationService(db).export_draft(
            w.p1, draft, include_bibliography=False
        )
    return cast(str, result["content"])


def _migration(
    connection: Connection,
    direction: str,
    filename: str = "d7f9b1c3e5a8_create_draft_releases.py",
) -> None:
    spec = importlib.util.spec_from_file_location(filename[:-3], VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "op", Operations(MigrationContext.configure(connection)))
    cast(Callable[[], None], getattr(module, direction))()


async def test_release_gate_invalidation_graph_and_races(
    screening_factory: Factory,
) -> None:
    factory = screening_factory
    # 1. Seed: five sentences, each claimed.
    w = await _setup(factory)
    c1 = await _claim(w, w.v1, V1, S1, "c1")
    c2 = await _claim(w, w.v1, V1, S2, "c2")
    c3 = await _claim(w, w.v1, V1, S3, "c3", kind="interpretation")
    c4 = await _claim(w, w.v1, V1, S4, "c4")
    c5 = await _claim(w, w.v1, V1, S5, "c5")
    l1 = await _link(w, c1, "l1", kind="extraction", accepted_value_id=w.sample)
    a1 = await _assess(w, c1, "a1", [l1])
    l2 = await _link(w, c2, "l2", kind="extraction", accepted_value_id=w.design)
    a2 = await _assess(w, c2, "a2", [l2])
    l4 = await _link(w, c4, "l4", **_span_link(w.d1, D1_TEXT, QUOTE))
    observed, _ = await _as(
        factory,
        w,
        "O",
        EDIT,
        lambda db, ctx: claims.observe_stance(
            db,
            ctx,
            c4.id,
            l4,
            w.ids["O"],
            StanceObservationCreate(idempotency_key="o4"),
        ),
    )
    await _link(w, c5, "l5", kind="legacy_unanchored", draft_citation_id=w.citation)
    v1_hash = _sha(V1)

    # 2. Candidate: labelled export, stored bytes untouched.
    candidate = await _check(w, w.v1, 1)
    assert candidate.release_status == "candidate" and candidate.release is None
    assert {(b.code, b.claim_version_id) for b in candidate.blockers} == {
        ("model_only", c4.version.id),
        ("legacy_only", c5.version.id),
    }
    exported = await _export(factory, w, w.v1)
    assert "CANDIDATE" in exported
    assert f"{S4} **[UNRESOLVED: model_only]**" in exported
    assert f"{S5} **[UNRESOLVED: legacy_only]**" in exported
    assert f"{S3} *[Interpretation — Test O]*" in exported
    assert (
        await _count(
            factory, "SELECT count(*) FROM generated_drafts WHERE content = :c", c=V1
        )
        == 1
    )
    # C5 gets an anchored span and a supporting assessment.
    l5 = await _link(w, c5, "l5-span", **_span_link(w.d1, D1_TEXT, SAMPLE))
    await _assess(w, c5, "a5", [l5])

    # 3. Blocked: exactly the seeded unsupported number, through the route.
    async with factory() as db:
        with pytest.raises(HTTPException) as blocked:
            await drafts_api.promote_draft(
                w.p1,
                w.v1,
                1,
                _body(v1_hash, "j-blocked"),
                Response(),
                current_user=_user(w.ids["A"]),
                db=db,
            )
    assert blocked.value.status_code == 409
    detail = cast(dict[str, Any], blocked.value.detail)
    assert detail["code"] == "release_blocked"
    assert [(b["code"], b["claim_version_id"]) for b in detail["blockers"]] == [
        ("model_only", str(c4.version.id))
    ]
    assert await _count(factory, "SELECT count(*) FROM draft_releases") == 0
    assert not await _events(factory, "release.promoted")

    # 4. Roles: owner without a role and reviewer 403; foreign org 404.
    for user in ("O", "R"):
        await _refused(
            _promote(w, user, w.v1, 1, f"{user}-try", v1_hash),
            403,
            "adjudicator or supervisor role required",
        )
    await _refused(_promote(w, "F", w.v1, 1, "f-try", v1_hash), 404)

    # 5. Fix C4; a wrong content hash is refused.
    await _assess(w, c4, "a4", [l4], stance_observation_ids=[observed.id])
    await _refused(
        _promote(w, "S", w.v1, 1, "s-wrong", "0" * 64),
        409,
        "Draft content changed; reload",
    )

    # 6. Concurrent promotion: one live release, one event, one replay.
    results = await asyncio.gather(
        _promote(w, "A", w.v1, 1, "j-1", v1_hash),
        _promote(w, "S", w.v1, 1, "s-1", v1_hash),
    )
    assert len({release.id for release, _ in results}) == 1, results
    assert sorted(replayed for _, replayed in results) == [False, True]
    release1 = results[0][0].id
    assert (
        await _count(
            factory,
            "SELECT count(*) FROM draft_releases WHERE stale_at IS NULL"
            " AND draft_id = :d",
            d=w.v1,
        )
        == 1
    )
    promotion_events = await _events(factory, "release.promoted")
    assert len(promotion_events) == 1
    assert promotion_events[0]["policy_version"] == 2
    assert await _count(factory, "SELECT 1") == 1
    again, replayed = await _promote(w, "A", w.v1, 1, "j-1", v1_hash)
    assert replayed and again.id == release1

    # 7. Commit/reopen: the snapshot and the verified export.
    row = await _row(factory, release1)
    assert set(row["claim_version_ids"]) == {
        str(c.version.id) for c in (c1, c2, c4, c5)
    }
    assert row["interpretation_claim_version_ids"] == [str(c3.version.id)]
    assert row["content_hash"] == v1_hash and row["draft_version"] == 1
    assert row["policy_version"] == 2
    assert row["actor_role"] in ("adjudicator", "supervisor")
    assert (await _check(w, w.v1, 1)).release_status == "verified"
    assert f"VERIFIED release {release1}" in await _export(factory, w, w.v1)

    # 8. A superseded assessment stales the release, which is otherwise kept.
    a2b = await _assess(
        w, c2, "a2b", [l2], stance="unresolved", supersedes_assessment_id=a2
    )
    after = await _row(factory, release1)
    assert after["stale_at"] is not None and after["stale_event_id"] is not None
    assert {k: v for k, v in after.items() if not k.startswith("stale_")} == {
        k: v for k, v in row.items() if not k.startswith("stale_")
    }
    (staled,) = await _events(factory, "release.staled")
    assert staled["release_ids"] == [str(release1)]
    assert staled["cause"]["family"] == "research_claims"
    assert staled["changed_nodes"] == [f"assessment:{a2}"]
    stale_check = await _check(w, w.v1, 1)
    assert stale_check.release_status == "stale"
    assert stale_check.invalidation is not None
    assert stale_check.invalidation.cause["kind"] == "claim.assessed"
    with pytest.raises(svc.ReleaseBlocked) as reblocked:
        await _promote(w, "A", w.v1, 1, "j-2", v1_hash)
    assert [b.code for b in reblocked.value.blockers] == ["unresolved"]
    await _assess(w, c2, "a2c", [l2], supersedes_assessment_id=a2b)
    release2, replayed = await _promote(w, "A", w.v1, 1, "j-3", v1_hash)
    assert not replayed and release2.id != release1
    assert (await _row(factory, release1))["stale_at"] is not None
    assert (
        await _count(
            factory, "SELECT count(*) FROM draft_releases WHERE draft_id = :d", d=w.v1
        )
        == 2
    )

    # 9. Selective invalidation: a source change reaches v1, not v2.
    async with factory() as db:
        await db.execute(
            text("UPDATE generated_drafts SET is_current = false WHERE id = :d"),
            {"d": w.v1},
        )
        v2_draft = GeneratedDraft(
            project_id=w.p1, version=2, title="v2", content=V2, is_current=True
        )
        db.add(v2_draft)
        await db.flush()
        db.add(DraftCitation(draft_id=v2_draft.id, citation_index=1, document_id=w.d2))
        await db.commit()
    v2 = cast(UUID, v2_draft.id)
    c6 = await _claim(w, v2, V2, V2_S, "c6")
    l6 = await _link(w, c6, "l6", kind="extraction", accepted_value_id=w.design)
    await _assess(w, c6, "a6", [l6])
    v2_release, _ = await _promote(w, "S", v2, 2, "s-v2", _sha(V2))
    d1_old = rr.source_node(w.d1, _sha(D1_TEXT), _sha(D1_TEXT))
    async with factory() as db:
        edges = await svc.load_edges(db, w.p1)
    reached = rr.dependents(edges, {d1_old})
    assert {
        rr.node("link", l1),
        rr.node("assessment", a1),
        rr.node("release", release2.id),
    } <= reached
    v2_nodes = {rr.node("release", v2_release.id), rr.node("link", l6)}
    c2_nodes = {rr.node("link", l2), rr.node("assessment", a2)}
    assert not reached & (v2_nodes | c2_nodes)
    changed_text = D1_TEXT + " Appendix."
    async with factory() as db:
        await db.execute(
            text("""UPDATE documents SET content_text = :t, checksum_sha256 = :h
                    WHERE id = :d"""),
            {"t": changed_text, "h": _sha(changed_text), "d": w.d1},
        )
        await db.commit()
    # Derived on read before any writer runs.
    assert (await _check(w, w.v1, 1)).release_status == "stale"
    async with factory() as db:
        await _refused(
            matrix_routes.accept_extraction_value(
                w.matrix,
                ExtractionAcceptCreate(
                    document_id=w.d1,
                    field_id=w.fields["Sample size"],
                    form_version_id=w.form_version,
                    observation_ids=[w.sample_obs],
                    value="412",
                    rationale="re-accept",
                    supersedes_accepted_value_id=w.sample,
                    idempotency_key="acc-again",
                ),
                current_user=_user(w.ids["A"]),
                db=db,
            ),
            409,
            "Source changed",
        )
    assert (await _row(factory, release2.id))["stale_at"] is not None
    assert (await _row(factory, v2_release.id))["stale_at"] is None
    assert (await _check(w, v2, 2)).release_status == "verified"
    source_staled = (await _events(factory, "release.staled"))[-1]
    assert source_staled["release_ids"] == [str(release2.id)]
    assert source_staled["cause"]["family"] == "research_extraction"

    # 10. Form change: only releases citing Design are stamped.
    async with factory() as db:
        context = await resolve_project(db, w.p1, w.ids["O"], EDIT)
        matrix = await db.get(ExtractionMatrix, w.matrix)
        await forms.create_version(
            db,
            context,
            matrix,
            [{"name": "Sample size"}, {"name": "Design", "unit": "arm"}],
            w.ids["O"],
        )
    form_staled = (await _events(factory, "release.staled"))[-1]
    assert form_staled["release_ids"] == [str(v2_release.id)]
    assert form_staled["cause"]["family"] == "research_extraction"
    assert form_staled["cause"]["kind"] == "extraction.staled"
    assert (await _check(w, v2, 2)).release_status == "stale"

    # 12. A blocked revision leaves the current draft and its status alone.
    async with factory() as db:
        service = DraftGenerationService(db)
        review = {
            "docs_skipped": 0,
            "coverage": {"complete": True},
            "uncited_assertions": [],
            "verdicts": [{"doc_index": 1, "verdict": "major", "evidence": "No."}],
        }
        with (
            pytest.MonkeyPatch.context() as patch,
            pytest.raises(ValueError, match="blocked persistence"),
        ):
            patch.setattr(
                service,
                "_build_revision_with_llm",
                _returns("Unsupported revised statement [Doc 1]."),
            )
            patch.setattr(service, "_review_citations", _returns(review))
            await service.revise_draft(project_id=w.p1, instructions="Unsupported")
    async with factory() as db:
        current = (
            await db.execute(
                select(GeneratedDraft).where(
                    GeneratedDraft.project_id == w.p1,
                    GeneratedDraft.is_current.is_(True),
                )
            )
        ).scalar_one()
    assert current.id == v2 and current.content == V2
    assert (await _check(w, v2, 2)).release_status == "stale"

    # 13. A released version is retained.
    async with factory() as db:
        with pytest.raises(DraftRetainedError):
            await DraftGenerationService(db).delete_draft(w.p1, w.v1)
    assert await _count(factory, "SELECT count(*) FROM draft_releases") == 3

    # 14. Replay: contiguous and valid.
    async with factory() as db:
        events = await replay_decisions(
            db, collection_id=w.p1, aggregate_type="research_release", aggregate_id=w.p1
        )
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert [str(e.event_type) for e in events].count("release.promoted") == 3

    # 15. Downgrade drops only draft_releases (GOO-315's empty
    # manuscript_releases references it, so it comes off first, after
    # GOO-316's empty venue checks, GOO-318's empty deposit tables and
    # GOO-320's empty review release links that reference it).
    async with factory() as db:
        connection = await db.connection()
        await connection.run_sync(
            lambda sync: _migration(
                sync, "downgrade", "d4a6c8e0f2b3_create_review_versions.py"
            )
        )
        await connection.run_sync(
            lambda sync: _migration(
                sync, "downgrade", "b0e2a4c6d8f9_create_archive_deposits.py"
            )
        )
        await connection.run_sync(
            lambda sync: _migration(
                sync, "downgrade", "f6a8c0d2e4b5_create_statements_venue.py"
            )
        )
        await connection.run_sync(
            lambda sync: _migration(
                sync, "downgrade", "e4c6a8b0d2f3_create_manuscript_releases.py"
            )
        )
        await connection.run_sync(lambda sync: _migration(sync, "downgrade"))
        await db.commit()
        remaining = set((await db.execute(text("""SELECT tablename FROM pg_tables
                            WHERE schemaname = current_schema()"""))).scalars())
    assert "draft_releases" not in remaining
    assert {"research_claim_versions", "generated_drafts"} <= remaining


def _returns(value: Any) -> Callable[..., Awaitable[Any]]:
    async def call(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return call


@pytest.mark.parametrize("order", ["release-first", "committed-first"])
@pytest.mark.parametrize("revoke", ["membership", "adjudicator"])
async def test_release_role_revocation_race(
    screening_factory: Factory, revoke: str, order: str
) -> None:
    """11. A draft with no assertions passes the gate; only the locks decide."""
    factory = screening_factory
    ids = await _seed(factory)
    content = "## Notes\n"
    async with factory() as db:
        draft = GeneratedDraft(
            project_id=ids["collection"],
            version=1,
            title="notes",
            content=content,
            is_current=True,
        )
        db.add(draft)
        await db.commit()
    draft_id = cast(UUID, draft.id)
    body = _body(_sha(content), "race")

    async def revoke_authority(db: AsyncSession) -> None:
        if revoke == "membership":
            await workspace_service.remove_member(
                db, ids["workspace"], ids["A"], ids["O"]
            )
            return
        await resolve_project(db, ids["collection"], ids["O"], ResearchAction.MANAGE)
        role = (
            await db.execute(
                select(ResearchProjectRoleAssignment).where(
                    ResearchProjectRoleAssignment.collection_id == ids["collection"],
                    ResearchProjectRoleAssignment.user_id == ids["A"],
                )
            )
        ).scalar_one()
        role.soft_delete()
        await db.flush()

    async with factory() as approver, factory() as revoker, factory() as observer:
        if order == "release-first":
            context = await resolve_project(
                approver, ids["collection"], ids["A"], RELEASE
            )
            pid = (await revoker.execute(text("SELECT pg_backend_pid()"))).scalar_one()

            async def revocation() -> None:
                await revoke_authority(revoker)
                await revoker.commit()

            attempt = asyncio.create_task(revocation())
            try:
                await _wait_until_blocked(observer, pid)
                assert not attempt.done()
                await svc.promote(approver, context, ids["A"], draft_id, 1, body)
                await attempt
            finally:
                if not attempt.done():
                    attempt.cancel()
                    await asyncio.gather(attempt, return_exceptions=True)
            expected = 1
        else:
            await revoke_authority(revoker)
            pid = (await approver.execute(text("SELECT pg_backend_pid()"))).scalar_one()

            async def promotion() -> Any:
                context = await resolve_project(
                    approver, ids["collection"], ids["A"], RELEASE
                )
                return await svc.promote(approver, context, ids["A"], draft_id, 1, body)

            attempt = asyncio.create_task(promotion())
            try:
                await _wait_until_blocked(observer, pid)
                await revoker.commit()
                with pytest.raises(HTTPException) as denied:
                    await attempt
                assert denied.value.status_code == (
                    404 if revoke == "membership" else 403
                )
            finally:
                if not attempt.done():
                    attempt.cancel()
                    await asyncio.gather(attempt, return_exceptions=True)
                await approver.rollback()
            expected = 0
    assert await _count(factory, "SELECT count(*) FROM draft_releases") == expected
    assert (
        len(await _events(factory, "release.promoted")) == expected
    ), "promotion must follow the lock order"
    if expected:
        async with factory() as db:
            with pytest.raises(DraftRetainedError):
                await DraftGenerationService(db).delete_draft(
                    ids["collection"], draft_id
                )

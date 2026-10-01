"""Real PostgreSQL proof for GOO-306 versioned claims and evidence links.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain ends at
``c4e6a8b0d2f5``: the five claim tables, their composite FKs and partial
unique indexes come from the migration, not ``Base.metadata``. GOO-299's
seed: owner O is the editor (E in the plan), reviewer R, adjudicator A (J),
foreign-org user F. P2 is a second project in the same workspace.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-306 section):

- ``create_version`` tip check -> ``if False:``: step 4's stale-``supersedes``
  post is no longer refused as stale (it falls through to ``No change``).
- ``assess`` tip check off and ``_flush_or_conflict`` re-raising: step 7's
  ``supersedes=None`` post dies with an unhandled ``IntegrityError`` on
  ``uq_research_claim_assessments_initial``. With the tip check off alone the
  unique-index backstop still answers the same 409, so the test passes.
- ``_extraction_target`` without ``ExtractionMatrix.project_id == ...``:
  step 5 links P2's accepted value (on the shared document D3) with 201.
- The migration without ``fk_research_claim_links_version``: step 5's raw
  cross-project ``INSERT`` succeeds.

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_research_claims_postgres.py``.
"""

import asyncio
import hashlib
import importlib.util
import json
from collections import Counter
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, TypeAlias, TypeVar, cast
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research import claims as claim_routes
from src.api.research import extraction_matrix as matrix_routes
from src.models.collection import Collection, CollectionDocument
from src.models.document import Document, DocumentType
from src.models.draft_citation import DraftCitation
from src.models.draft_review import DraftReview
from src.models.evidence import StanceClassificationModel, StanceEnum
from src.models.extraction_matrix import ExtractionMatrix
from src.models.generated_draft import GeneratedDraft
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.services.research import claim_rules
from src.services.research import claims_service as svc
from src.services.research import extraction_forms_service as forms
from src.services.research.draft_generation_service import (
    DraftGenerationService,
    DraftRetainedError,
)
from src.services.research_decisions import replay_decisions
from src.services.research_engine.contracts import canonical_json_sha256
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from src.shared.claim_schemas import (
    ClaimAssessmentCreate,
    ClaimCreate,
    ClaimLinkCreate,
    ClaimVersionCreate,
    StanceObservationCreate,
)
from src.shared.scispace_schemas import (
    ExtractionAcceptCreate,
    ExtractionObservationCreate,
)
from tests.integration.test_report_identity_postgres import _seed
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    VERSIONS,
    screening_factory,
)

pytestmark = pytest.mark.integration

Factory: TypeAlias = async_sessionmaker[AsyncSession]
T = TypeVar("T")
D1_TEXT = (
    "Methods. We enrolled 412 participants. "
    "Mortality fell by twelve percent in the treated arm. Discussion follows."
)
QUOTE = "Mortality fell by twelve percent in the treated arm."
SAMPLE = "We enrolled 412 participants."
V1_DRAFT = "Intro. Treatment reduced mortality by 12% [Doc 1]. End."
V1_TEXT = "Treatment reduced mortality by 12% [Doc 1]."
V2_DRAFT = "Intro. Treatment lowered mortality by twelve percent [Doc 1]. End."
V2_TEXT = "Treatment lowered mortality by twelve percent [Doc 1]."


def _user(user_id: UUID) -> Any:
    """A route-shaped current user: only ``id`` is read."""
    return SimpleNamespace(id=user_id)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _span(content: str, passage: str) -> tuple[int, int]:
    start = content.index(passage)
    return start, start + len(passage)


class _World(SimpleNamespace):
    ids: dict[str, UUID]
    p1: UUID
    p2: UUID
    d1: UUID
    d2: UUID
    d3: UUID
    draft1: UUID
    draft2: UUID
    citation1: UUID
    citation2: UUID
    review1: UUID
    accepted_p1: UUID
    accepted_p2: UUID


async def _as(
    factory: Factory,
    world: _World,
    user: str,
    action: ResearchAction,
    call: Callable[[AsyncSession, ProjectContext], Awaitable[T]],
    project: UUID | None = None,
) -> T:
    """One route-shaped request: resolve, then the service (which commits)."""
    async with factory() as db:
        context = await resolve_project(
            db, project or world.p1, world.ids[user], action
        )
        return await call(db, context)


async def _refused(awaitable: Awaitable[Any], status: int, detail: str = "") -> None:
    with pytest.raises(HTTPException) as error:
        await awaitable
    assert error.value.status_code == status, error.value.detail
    assert detail in str(error.value.detail)


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


async def _accepted_value(
    factory: Factory, ids: dict[str, UUID], project: UUID, document: UUID
) -> UUID:
    """A GOO-304/305 accepted tip: matrix, form version, a verified human
    observation of SAMPLE, and the adjudicator's acceptance of it."""
    columns = [{"name": "Sample size"}]
    async with factory() as db:
        context = await resolve_project(db, project, ids["O"], ResearchAction.EDIT)
        matrix = ExtractionMatrix(project_id=project, name="claims", columns=columns)
        db.add(matrix)
        version = await forms.create_version(db, context, matrix, columns, ids["O"])
        matrix_id, version_id = cast(UUID, matrix.id), cast(UUID, version.id)
        field_id = UUID(version.fields[0]["field_id"])
    async with factory() as db:
        observation = await matrix_routes.create_observation(
            matrix_id,
            ExtractionObservationCreate(
                document_id=document,
                field_id=field_id,
                form_version_id=version_id,
                value="412",
                citation=SAMPLE,
                idempotency_key=f"obs-{project}",
            ),
            current_user=_user(ids["R"]),
            db=db,
        )
    async with factory() as db:
        accepted = await matrix_routes.accept_extraction_value(
            matrix_id,
            ExtractionAcceptCreate(
                document_id=document,
                field_id=field_id,
                form_version_id=version_id,
                observation_ids=[observation.id],
                value="412",
                rationale="methods section",
                idempotency_key=f"acc-{project}",
            ),
            current_user=_user(ids["A"]),
            db=db,
        )
    return cast(UUID, accepted.id)


async def _setup(factory: Factory) -> _World:
    ids = await _seed(factory)
    p1, p2 = ids["collection"], uuid4()
    async with factory() as db:
        db.add(Collection(id=p2, workspace_id=ids["workspace"], name="p2"))
        await db.flush()
        for user, role in (
            ("R", ResearchProjectRole.REVIEWER),
            ("A", ResearchProjectRole.ADJUDICATOR),
        ):
            db.add(
                ResearchProjectRoleAssignment(
                    collection_id=p2,
                    user_id=ids[user],
                    role=role,
                    assigned_by_id=ids["O"],
                )
            )
        d1 = await _document(db, ids["org"], "d1", D1_TEXT)
        d2 = await _document(db, ids["org"], "d2", "P2 only. " + D1_TEXT)
        d3 = await _document(db, ids["org"], "d3", "Shared. " + D1_TEXT)
        for project, document in ((p1, d1), (p2, d2), (p1, d3), (p2, d3)):
            db.add(CollectionDocument(collection_id=project, document_id=document))
        drafts, citations = [], []
        for number, content in ((1, V1_DRAFT), (2, V2_DRAFT)):
            draft = GeneratedDraft(
                project_id=p1,
                version=number,
                title=f"v{number}",
                content=content,
                is_current=number == 2,
            )
            db.add(draft)
            await db.flush()
            citation = DraftCitation(
                draft_id=draft.id, citation_index=1, document_id=d1
            )
            db.add(citation)
            await db.flush()
            drafts.append(draft)
            citations.append(cast(UUID, citation.id))
        review = DraftReview(
            project_id=p1,
            base_draft_id=None,
            candidate_content_hash=_sha(V1_DRAFT),
            candidate_content=V1_DRAFT,
            source_document_ids=[str(d1)],
            review={"claims": [{"text": V1_TEXT, "support_status": "exact"}]},
            outcome="passed",
        )
        db.add(review)
        await db.flush()
        setattr(drafts[0], "generation_params", {"citation_review_id": str(review.id)})
        db.add(
            StanceClassificationModel(
                claim_hash=claim_rules.normalized_hash(V2_TEXT),
                claim_text=V2_TEXT,
                source_id=d1,
                source_content_hash=_sha(D1_TEXT),
                organization_id=ids["org"],
                stance=StanceEnum.SUPPORTING,
                confidence=0.91,
                justification_excerpt=QUOTE,
                model_version=svc._classifier.classifier_version,
                inference_model_version="stub-model-1",
            )
        )
        await db.commit()
    return _World(
        ids=ids,
        p1=p1,
        p2=p2,
        d1=d1,
        d2=d2,
        d3=d3,
        draft1=drafts[0].id,
        draft2=drafts[1].id,
        citation1=citations[0],
        citation2=citations[1],
        review1=review.id,
        accepted_p1=await _accepted_value(factory, ids, p1, d1),
        accepted_p2=await _accepted_value(factory, ids, p2, d3),
    )


def _claim_body(draft: UUID, content: str, passage: str, key: str) -> dict[str, Any]:
    start, end = _span(content, passage)
    return {
        "draft_id": draft,
        "start_char": start,
        "end_char": end,
        "text": passage,
        "idempotency_key": key,
    }


async def _count(factory: Factory, sql: str, **params: Any) -> int:
    async with factory() as db:
        return int((await db.execute(text(sql), params)).scalar_one())


def _migration(connection: Connection, direction: str) -> None:
    spec = importlib.util.spec_from_file_location(
        "claims_migration", VERSIONS / "c4e6a8b0d2f5_create_research_claims.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "op", Operations(MigrationContext.configure(connection)))
    cast(Callable[[], None], getattr(module, direction))()


async def test_claims_versions_links_assessments_tenancy_and_export(
    screening_factory: Factory,
) -> None:
    factory = screening_factory
    # 1. Seed.
    w = await _setup(factory)
    edit, view, adjudicate = (
        ResearchAction.EDIT,
        ResearchAction.VIEW,
        ResearchAction.ADJUDICATE,
    )

    # 2. Create + idempotency.
    body = _claim_body(w.draft1, V1_DRAFT, V1_TEXT, "create-1")

    def create(data: dict[str, Any]) -> Callable[..., Awaitable[Any]]:
        return lambda db, ctx: svc.create_claim(
            db, ctx, w.ids["O"], ClaimCreate(**data)
        )

    claim, replayed = await _as(factory, w, "O", edit, create(body))
    assert not replayed and claim.version.version_no == 1
    again, replayed = await _as(factory, w, "O", edit, create(body))
    assert replayed and again.id == claim.id and again.version.id == claim.version.id
    assert (
        await _count(
            factory,
            "SELECT count(*) FROM research_decision_events"
            " WHERE event_type = 'claim.versioned'",
        )
        == 1
    )
    await _refused(
        _as(factory, w, "O", edit, create({**body, "kind": "interpretation"})),
        409,
        "Idempotency conflict",
    )
    v1_id = claim.version.id

    # 3. Commit/reopen: the passage and the draft hash hold in SQL.
    async with factory() as db:
        row = (
            await db.execute(
                text("""SELECT substring(d.content from v.start_char + 1
                                          for v.end_char - v.start_char) = v.text,
                               encode(sha256(convert_to(d.content, 'UTF8')), 'hex')
                                   = v.draft_content_hash,
                               v.draft_review_id
                        FROM research_claim_versions v
                        JOIN generated_drafts d ON d.id = v.draft_id
                        WHERE v.id = :id"""),
                {"id": v1_id},
            )
        ).one()
    assert row[0] is True and row[1] is True and row[2] == w.review1
    listed = await _as(
        factory, w, "O", view, lambda db, ctx: svc.list_claims(db, ctx, w.draft1)
    )
    assert listed.items[0].citation_review_status == "exact"

    # 4. Version history.
    def version(data: dict[str, Any]) -> Callable[..., Awaitable[Any]]:
        return lambda db, ctx: svc.create_version(
            db, ctx, claim.id, w.ids["O"], ClaimVersionCreate(**data)
        )

    async with factory() as db:
        v1_before = dict(
            (
                await db.execute(
                    text("SELECT * FROM research_claim_versions WHERE id = :id"),
                    {"id": v1_id},
                )
            )
            .mappings()
            .one()
        )
    v2_body = {
        **_claim_body(w.draft2, V2_DRAFT, V2_TEXT, "v2"),
        "supersedes_claim_version_id": v1_id,
    }
    v2, replayed = await _as(factory, w, "O", edit, version(v2_body))
    assert not replayed and v2.version_no == 2 and v2.draft_review_id is None
    await _refused(
        _as(factory, w, "O", edit, version({**v2_body, "idempotency_key": "stale"})),
        409,
        "Claim version is stale; reload",
    )
    await _refused(
        _as(
            factory,
            w,
            "O",
            edit,
            version(
                {
                    **v2_body,
                    "supersedes_claim_version_id": v2.id,
                    "idempotency_key": "same",
                }
            ),
        ),
        409,
        "No change",
    )
    # Two concurrent posts superseding v2 (different wording): one wins.
    v3_draft = V2_DRAFT.replace("lowered", "cut")
    async with factory() as db:
        draft3 = GeneratedDraft(
            project_id=w.p1, version=3, title="v3", content=v3_draft, is_current=False
        )
        db.add(draft3)
        await db.commit()
    v3_text = V2_TEXT.replace("lowered", "cut")
    racers = [
        {
            **_claim_body(cast(UUID, draft3.id), v3_draft, v3_text, f"race-{n}"),
            "supersedes_claim_version_id": v2.id,
        }
        for n in (1, 2)
    ]
    results = await asyncio.gather(
        *(_as(factory, w, "O", edit, version(r)) for r in racers),
        return_exceptions=True,
    )
    won = [r for r in results if isinstance(r, tuple)]
    lost = [r for r in results if isinstance(r, HTTPException)]
    assert len(won) == 1 and len(lost) == 1, results
    assert lost[0].status_code == 409
    assert await _count(factory, "SELECT 1") == 1
    v3 = won[0][0]
    async with factory() as db:
        v1_after = dict(
            (
                await db.execute(
                    text("SELECT * FROM research_claim_versions WHERE id = :id"),
                    {"id": v1_id},
                )
            )
            .mappings()
            .one()
        )
    assert v1_after == v1_before
    tip = v3  # links, observations and assessments target the tip

    # 5. Links on the tip.
    def link(data: dict[str, Any], user: str = "O") -> Callable[..., Awaitable[Any]]:
        return lambda db, ctx: svc.link(
            db,
            ctx,
            claim.id,
            w.ids[user],
            ClaimLinkCreate(claim_version_id=tip.id, **data),
        )

    extraction, _ = await _as(
        factory,
        w,
        "O",
        edit,
        link(
            {
                "kind": "extraction",
                "accepted_value_id": w.accepted_p1,
                "idempotency_key": "l-ex",
            }
        ),
    )
    assert extraction.document_id == w.d1 and extraction.text_sha256 == _sha(D1_TEXT)
    await _refused(
        _as(
            factory,
            w,
            "O",
            edit,
            link(
                {
                    "kind": "extraction",
                    "accepted_value_id": w.accepted_p2,
                    "idempotency_key": "l-p2",
                }
            ),
        ),
        404,
        "Evidence not found",
    )
    start, end = _span(D1_TEXT, QUOTE)
    span_body: dict[str, Any] = {
        "kind": "source_span",
        "document_id": w.d1,
        "start_char": start,
        "end_char": end,
        "quote": QUOTE,
    }
    span, _ = await _as(
        factory, w, "O", edit, link({**span_body, "idempotency_key": "l-span"})
    )
    assert span.source_hash == _sha(D1_TEXT) and span.text_sha256 == _sha(D1_TEXT)
    await _refused(
        _as(
            factory,
            w,
            "O",
            edit,
            link(
                {
                    **span_body,
                    "start_char": start + 1,
                    "end_char": end + 1,
                    "idempotency_key": "l-off",
                }
            ),
        ),
        422,
        "Span does not match the source",
    )
    await _refused(
        _as(
            factory,
            w,
            "O",
            edit,
            link({**span_body, "document_id": w.d2, "idempotency_key": "l-d2"}),
        ),
        404,
        "Evidence not found",
    )
    await _refused(
        _as(
            factory,
            w,
            "O",
            edit,
            link(
                {
                    "kind": "legacy_unanchored",
                    "draft_citation_id": w.citation1,
                    "idempotency_key": "l-c1",
                }
            ),
        ),
        422,
        "Citation belongs to another draft version",
    )
    # v3 lives in draft3, which has no citations; v2's citation is foreign too.
    await _refused(
        _as(
            factory,
            w,
            "O",
            edit,
            link(
                {
                    "kind": "legacy_unanchored",
                    "draft_citation_id": w.citation2,
                    "idempotency_key": "l-c2",
                }
            ),
        ),
        422,
        "Citation belongs to another draft version",
    )
    async with factory() as db:
        citation3 = DraftCitation(
            draft_id=draft3.id, citation_index=1, document_id=w.d1
        )
        db.add(citation3)
        await db.commit()
    legacy, _ = await _as(
        factory,
        w,
        "O",
        edit,
        link(
            {
                "kind": "legacy_unanchored",
                "draft_citation_id": citation3.id,
                "idempotency_key": "l-c3",
            }
        ),
    )
    assert legacy.source_hash is None and legacy.text_sha256 is None
    assert legacy.start_char is None and legacy.document_id == w.d1
    await _refused(
        _as(
            factory,
            w,
            "O",
            edit,
            lambda db, ctx: svc.link(
                db,
                ctx,
                claim.id,
                w.ids["O"],
                ClaimLinkCreate(
                    claim_version_id=v1_id, **span_body, idempotency_key="l-v1"
                ),
            ),
        ),
        409,
        "Claim version is stale; reload",
    )
    async with factory() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                text("""INSERT INTO research_claim_evidence_links
                        (id, collection_id, claim_version_id, kind,
                         draft_citation_id, status, created_by_id)
                        VALUES (:id, :p2, :v, 'legacy_unanchored', :c,
                                'linked', :u)"""),
                {
                    "id": uuid4(),
                    "p2": w.p2,
                    "v": tip.id,
                    "c": citation3.id,
                    "u": w.ids["O"],
                },
            )

    # 6. Observations: immutable snapshots of the meter row.
    async with factory() as db:
        await db.execute(
            text("""UPDATE stance_classifications SET claim_hash = :h,
                    claim_text = :t"""),
            {"h": claim_rules.normalized_hash(v3_text), "t": v3_text},
        )
        await db.commit()

    def observe(link_id: UUID, key: str) -> Callable[..., Awaitable[Any]]:
        return lambda db, ctx: svc.observe_stance(
            db,
            ctx,
            claim.id,
            link_id,
            w.ids["O"],
            StanceObservationCreate(idempotency_key=key),
        )

    first, _ = await _as(factory, w, "O", edit, observe(span.id, "o1"))
    assert first.stance == "supporting"
    assert first.classifier_version == svc._classifier.classifier_version
    async with factory() as db:
        await db.execute(text("UPDATE stance_classifications SET stance = 'OPPOSING'"))
        await db.commit()
    async with factory() as db:
        kept = (
            await db.execute(
                text(
                    "SELECT stance FROM research_claim_stance_observations"
                    " WHERE id = :id"
                ),
                {"id": first.id},
            )
        ).scalar_one()
    assert kept == "supporting"
    second, _ = await _as(factory, w, "O", edit, observe(span.id, "o2"))
    assert second.id != first.id and second.stance == "opposing"
    assert (
        await _count(factory, "SELECT count(*) FROM research_claim_stance_observations")
        == 2
    )
    await _refused(
        _as(factory, w, "O", edit, observe(legacy.id, "o3")),
        409,
        "Legacy links cannot be assessed",
    )

    # 7. Assessments.
    def assess(user: str, **data: Any) -> Callable[..., Awaitable[Any]]:
        return lambda db, ctx: svc.assess(
            db,
            ctx,
            claim.id,
            w.ids[user],
            ClaimAssessmentCreate(
                claim_version_id=tip.id, rationale="trial reports it", **data
            ),
        )

    cited = {
        "stance": "supporting",
        "link_ids": [span.id, extraction.id],
        "stance_observation_ids": [second.id],
    }
    await _refused(
        _as(factory, w, "R", adjudicate, assess("R", **cited, idempotency_key="r")),
        403,
        "adjudicator role required",
    )
    first_assessment, _ = await _as(
        factory, w, "A", adjudicate, assess("A", **cited, idempotency_key="j1")
    )
    await _refused(
        _as(factory, w, "A", adjudicate, assess("A", **cited, idempotency_key="j2")),
        409,
        "Assessment is stale; reload",
    )
    results = await asyncio.gather(
        *(
            _as(
                factory,
                w,
                "A",
                adjudicate,
                assess(
                    "A",
                    **cited,
                    supersedes_assessment_id=first_assessment.id,
                    idempotency_key=f"j-race-{n}",
                ),
            )
            for n in (1, 2)
        ),
        return_exceptions=True,
    )
    won = [r for r in results if isinstance(r, tuple)]
    assert len(won) == 1, results
    assert [r.status_code for r in results if isinstance(r, HTTPException)] == [409]
    assessment_tip = won[0][0]
    await _as(
        factory,
        w,
        "O",
        edit,
        link(
            {
                "kind": "source_span",
                "status": "withdrawn",
                "supersedes_link_id": span.id,
                "idempotency_key": "l-withdraw",
            }
        ),
    )
    await _refused(
        _as(
            factory,
            w,
            "A",
            adjudicate,
            assess(
                "A",
                **cited,
                supersedes_assessment_id=assessment_tip.id,
                idempotency_key="j-dead",
            ),
        ),
        422,
        "live link",
    )

    # 8. Tenancy and lifecycle.
    for action in (view, edit):
        await _refused(
            _as(
                factory, w, "F", action, lambda db, ctx: svc.list_claims(db, ctx, None)
            ),
            404,
            "Project not found",
        )
    async with factory() as db:
        await _refused(
            claim_routes.export_claims(
                w.p1, None, current_user=_user(w.ids["F"]), db=db
            ),
            404,
        )
    async with factory() as db:
        await db.execute(
            text("UPDATE collections SET research_status = 'archived' WHERE id = :p"),
            {"p": w.p1},
        )
        await db.commit()
    await _refused(
        _as(factory, w, "O", edit, create({**body, "idempotency_key": "archived"})),
        409,
        "Project is not writable",  # lock_active_project on an archived project
    )
    archived_list = await _as(
        factory, w, "O", view, lambda db, ctx: svc.list_claims(db, ctx, None)
    )
    assert archived_list.counts.claims == 1
    async with factory() as db:
        archived_export = await claim_routes.export_claims(
            w.p1, None, current_user=_user(w.ids["O"]), db=db
        )
    assert archived_export.status_code == 200
    async with factory() as db:
        await db.execute(
            text("UPDATE collections SET research_status = 'active' WHERE id = :p"),
            {"p": w.p1},
        )
        await db.commit()
    async with factory() as db:
        with pytest.raises(DraftRetainedError):
            await DraftGenerationService(db).delete_draft(w.p1, w.draft1)
    assert (
        await _count(
            factory, "SELECT count(*) FROM generated_drafts WHERE id = :d", d=w.draft1
        )
        == 1
    )
    async with factory() as db:
        await db.execute(
            text("""UPDATE collection_documents SET is_deleted = true
                    WHERE collection_id = :p AND document_id = :d"""),
            {"p": w.p1, "d": w.d1},
        )
        await db.commit()
    await _refused(
        _as(
            factory,
            w,
            "O",
            edit,
            link({**span_body, "idempotency_key": "l-detached"}),
        ),
        404,
        "Evidence not found",
    )
    detail = await _as(
        factory, w, "O", view, lambda db, ctx: svc.get_claim(db, ctx, claim.id)
    )
    changed = {row.id: row.source_changed for row in detail.links}
    assert changed[extraction.id] is False and changed[legacy.id] is False
    async with factory() as db:
        await db.execute(
            text(
                "UPDATE documents SET content_text = content_text || 'x' WHERE id = :d"
            ),
            {"d": w.d1},
        )
        await db.commit()
    detail = await _as(
        factory, w, "O", view, lambda db, ctx: svc.get_claim(db, ctx, claim.id)
    )
    changed = {row.id: row.source_changed for row in detail.links}
    assert changed[extraction.id] is True and changed[span.id] is True
    assert changed[legacy.id] is False

    # 9. Replay: the stream validates and mirrors the rows.
    async with factory() as db:
        events = await replay_decisions(
            db, collection_id=w.p1, aggregate_type="research_claims", aggregate_id=w.p1
        )
    by_type = Counter(str(event.event_type) for event in events)
    for event_type, table in (
        ("claim.versioned", "research_claim_versions"),
        ("claim.linked", "research_claim_evidence_links"),
        ("claim.observed", "research_claim_stance_observations"),
        ("claim.assessed", "research_claim_assessments"),
    ):
        assert by_type[event_type] == await _count(
            factory, f"SELECT count(*) FROM {table}"  # noqa: S608 - fixed names
        ), event_type

    # 10. Export hash reconciliation; the export writes nothing.
    async def export() -> tuple[dict[str, Any], str]:
        async with factory() as db:
            response = await claim_routes.export_claims(
                w.p1, None, current_user=_user(w.ids["O"]), db=db
            )
        return (
            json.loads(bytes(response.body)),
            response.headers["content-disposition"],
        )

    def totals_sql() -> str:
        return (
            "SELECT (SELECT count(*) FROM research_claim_versions)"
            " + (SELECT count(*) FROM research_claim_evidence_links)"
            " + (SELECT count(*) FROM research_claim_stance_observations)"
            " + (SELECT count(*) FROM research_claim_assessments)"
            " + (SELECT coalesce(sum(next_seq), 0) FROM research_decision_streams)"
        )

    before = await _count(factory, totals_sql())
    package, disposition = await export()
    body_ = package["body"]
    assert canonical_json_sha256(body_) == package["body_sha256"]
    assert package["body_sha256"][:12] in disposition
    drafts = {d["id"]: d["content"] for d in body_["drafts"]}
    versions = {v["id"]: v["text"] for v in body_["claim_versions"]}
    assert body_["resolution"]
    for walk in body_["resolution"]:
        draft_id, start_, end_ = walk["passage"]
        assert drafts[draft_id][start_:end_] == versions[walk["claim_version_id"]]
    observation_ids = {o["id"] for o in body_["extraction"]["observations"]}
    for walk in body_["resolution"]:
        if walk["kind"] == "extraction":
            assert walk["observation_id"] in observation_ids
    assert "content_text" not in json.dumps(body_["documents"])
    counts = body_["counts"]
    assert counts["claims"] == await _count(
        factory, "SELECT count(*) FROM research_claims"
    )
    assert counts["claim_versions"] == await _count(
        factory, "SELECT count(*) FROM research_claim_versions"
    )
    for kind, number in counts["links_by_kind"].items():
        assert number == await _count(
            factory,
            "SELECT count(*) FROM research_claim_evidence_links WHERE kind = :k",
            k=kind,
        )
    assert counts["withdrawn_links"] == await _count(
        factory,
        "SELECT count(*) FROM research_claim_evidence_links WHERE status = 'withdrawn'",
    )
    assert counts["stance_observations"] == 2
    assert counts["assessments_by_stance"] == {"supporting": 2}
    assert counts["unassessed_tip_versions"] == 0
    assert body_["stream_head"] == len(events)
    second_package, _ = await export()
    assert second_package["body_sha256"] == package["body_sha256"]
    assert await _count(factory, totals_sql()) == before

    # 11. Downgrade drops only this ticket's tables.
    async with factory() as db:
        connection = await db.connection()
        await connection.run_sync(lambda sync: _migration(sync, "downgrade"))
        await db.commit()
        remaining = set((await db.execute(text("""SELECT tablename FROM pg_tables
                            WHERE schemaname = current_schema()"""))).scalars())
    assert not {t for t in remaining if t.startswith("research_claim")}
    assert {
        "stance_classifications",
        "generated_drafts",
        "extraction_accepted_values",
        "extraction_observations",
    } <= remaining
    assert await _count(factory, "SELECT count(*) FROM stance_classifications") == 1
    assert await _count(factory, "SELECT count(*) FROM extraction_accepted_values") == 2

"""GOO-303 full-text acquisition, the screening gate and the PRISMA loader on
SQLite (PostgreSQL race proofs live in the integration suite)."""

from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401  (registers every FK target table)
from src.models.base import Base
from src.models.collection import Collection, CollectionDocument
from src.models.document import Document, DocumentType
from src.models.research_decision import ResearchDecisionEvent
from src.models.research_fulltext import (
    ResearchFulltextAttempt,
    ResearchFulltextRequest,
)
from src.models.research_report import ResearchReport, ResearchReportObservation
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource
from src.models.screening import ScreeningObservation
from src.models.workspace import Workspace
from src.schemas.research_engine import (
    FulltextAttemptCreate,
    FulltextOutcome,
    FulltextRequestCreate,
)
from src.services.research_engine import acquisition_service, prisma, prisma_service
from tests.unit.services.test_screening_service import (
    _TABLES,
    _USERS,
    _assign,
    _context,
    _Project,
    _queue,
    _raises,
    _seed,
    _submit,
)

_EXTRA_TABLES = (Workspace, Collection, Document, CollectionDocument)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    event.listen(
        engine.sync_engine,
        "connect",
        lambda conn, _record: conn.create_function(
            "now", 0, lambda: datetime.now(timezone.utc).isoformat(" ")
        ),
    )
    tables = [getattr(m, "__table__") for m in (*_TABLES, *_EXTRA_TABLES)]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
        await connection.run_sync(_USERS.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _project(db: AsyncSession) -> _Project:
    """GOO-301's seed plus the Workspace/Collection project_documents_query joins."""
    project = await _seed(db)
    workspace_id = uuid4()
    db.add(Workspace(id=workspace_id, name="w", owner_id=project.owner))
    await db.flush()
    db.add(Collection(id=project.collection_id, name="p", workspace_id=workspace_id))
    await db.flush()
    return project


def _editor(project: _Project) -> Any:
    return _context(project)


async def _document(
    db: AsyncSession,
    project: _Project,
    *,
    attach: bool = True,
    organization_id: UUID | None = None,
    checksum: str | None = "a" * 64,
) -> UUID:
    document = Document(
        title="pdf",
        filename="f.pdf",
        file_path="local:///f.pdf",
        file_size_bytes=1,
        mime_type="application/pdf",
        document_type=DocumentType.PDF,
        organization_id=organization_id or project.organization_id,
        checksum_sha256=checksum,
    )
    db.add(document)
    await db.flush()
    if attach:
        db.add(
            CollectionDocument(
                collection_id=project.collection_id, document_id=document.id
            )
        )
        await db.flush()
    return cast(UUID, document.id)


async def _request(
    db: AsyncSession, project: _Project, report: UUID, key: str = "rq"
) -> Any:
    state, _ = await acquisition_service.request_fulltext(
        db,
        _editor(project),
        project.owner,
        FulltextRequestCreate(report_id=report, idempotency_key=key),
    )
    return state


async def _attempt(
    db: AsyncSession,
    project: _Project,
    request_id: UUID,
    key: str,
    outcome: FulltextOutcome = "unavailable",
    **fields: Any,
) -> Any:
    if outcome == "unavailable":
        fields.setdefault("reason", "not held by library")
    state, _ = await acquisition_service.record_attempt(
        db,
        _editor(project),
        request_id,
        project.owner,
        FulltextAttemptCreate(
            outcome=outcome,
            attempted_on=date(2026, 9, 29),
            idempotency_key=key,
            **fields,
        ),
    )
    return state


async def _count(db: AsyncSession, model: Any) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def _flow(db: AsyncSession, project: _Project) -> dict[str, Any]:
    inputs = await prisma_service.load_inputs(db, _editor(project))
    return prisma.derive_prisma_flow(inputs)


@pytest.mark.asyncio
async def test_request_is_idempotent_and_unique_per_report(db: AsyncSession) -> None:
    project = await _project(db)
    report = project.reports[0]
    first = await _request(db, project, report)
    assert (first.state, first.attempts, first.head_attempt_id) == ("pending", [], None)
    replay, created = await acquisition_service.request_fulltext(
        db,
        _editor(project),
        project.owner,
        FulltextRequestCreate(report_id=report, idempotency_key="rq"),
    )
    assert (replay.request_id, created) == (first.request_id, False)
    await _raises(
        _request(db, project, report, "rq-2"), 409, "Full text already requested"
    )
    await _raises(_request(db, project, uuid4(), "rq-3"), 404, "Report not found")
    assert await _count(db, ResearchFulltextRequest) == 1
    assert await _count(db, ResearchDecisionEvent) == 1

    await db.execute(
        update(ResearchReport)
        .where(ResearchReport.id == project.reports[1])
        .values(merged_into_report_id=report)
    )
    await _raises(_request(db, project, project.reports[1], "rq-4"), 409)


@pytest.mark.asyncio
async def test_unavailable_does_not_create_observation_or_exclusion(
    db: AsyncSession,
) -> None:
    project = await _project(db)
    report = project.reports[0]
    request = await _request(db, project, report)
    unavailable = await _attempt(db, project, request.request_id, "a1")
    assert unavailable.state == "unavailable"
    assert unavailable.attempts[0].reason == "not held by library"
    assert await _count(db, ScreeningObservation) == 0
    flow = await _flow(db, project)
    assert flow["counts"]["reports_not_retrieved"] == 1
    assert flow["counts"]["reports_excluded_by_reason"] == {}
    assert flow["counts"]["records_excluded"] == 0

    # Still eligible for full-text screening once retrieved.
    queue = await _queue(db, project, stage="full_text", report_ids=[report])
    assignment = await _assign(db, project, queue.id, project.reviewer)
    document = await _document(db, project)
    retrieved = await _attempt(
        db,
        project,
        request.request_id,
        "a2",
        "retrieved",
        document_id=document,
        previous_attempt_id=unavailable.head_attempt_id,
    )
    assert [a.outcome for a in retrieved.attempts] == ["unavailable", "retrieved"]
    observation = await _submit(
        db,
        project,
        queue,
        assignment.id,
        report,
        "s1",
        "exclude",
        exclusion_reason="wrong design",
    )
    assert observation.exclusion_reason == "wrong design"


@pytest.mark.asyncio
async def test_retrieved_requires_project_scoped_document(db: AsyncSession) -> None:
    project = await _project(db)
    request = await _request(db, project, project.reports[0])

    async def retrieve(document_id: UUID, key: str) -> Any:
        return await _attempt(
            db, project, request.request_id, key, "retrieved", document_id=document_id
        )

    foreign = await _document(db, project, organization_id=uuid4())
    await _raises(retrieve(foreign, "k1"), 404, "Document not found")
    detached = await _document(db, project, attach=False)
    await _raises(retrieve(detached, "k2"), 404, "Document not found")
    unhashed = await _document(db, project, checksum=None)
    await _raises(retrieve(unhashed, "k3"), 422, "Document has no content hash yet")
    assert await _count(db, ResearchFulltextAttempt) == 0

    document = await _document(db, project, checksum="b" * 64)
    state = await retrieve(document, "k4")
    head = state.attempts[-1]
    assert (state.state, head.document_id, head.document_content_hash) == (
        "retrieved",
        document,
        "b" * 64,
    )
    assert head.document_available is True

    # Soft delete keeps id and hash; the list reports the document unavailable.
    await db.execute(
        update(Document).where(Document.id == document).values(is_deleted=True)
    )
    [listed] = await acquisition_service.list_fulltext(db, _editor(project))
    assert listed.attempts[-1].document_content_hash == "b" * 64
    assert listed.attempts[-1].document_available is False


@pytest.mark.asyncio
async def test_stale_previous_attempt_is_409(db: AsyncSession) -> None:
    project = await _project(db)
    request = await _request(db, project, project.reports[0])
    first = await _attempt(db, project, request.request_id, "a1", "requested")
    stale = "Attempt is stale; reload acquisition state"
    await _raises(_attempt(db, project, request.request_id, "a2"), 409, stale)
    await _raises(
        _attempt(db, project, request.request_id, "a3", previous_attempt_id=uuid4()),
        409,
        stale,
    )
    # An identical retry replays; the same key with another body conflicts.
    again, created = await acquisition_service.record_attempt(
        db,
        _editor(project),
        request.request_id,
        project.owner,
        FulltextAttemptCreate(
            outcome="requested", attempted_on=date(2026, 9, 29), idempotency_key="a1"
        ),
    )
    assert (again.head_attempt_id, created) == (first.head_attempt_id, False)
    await _raises(
        _attempt(db, project, request.request_id, "a1", "requested", reason="x"),
        409,
        "Idempotency conflict",
    )
    document = await _document(db, project)
    done = await _attempt(
        db,
        project,
        request.request_id,
        "a4",
        "retrieved",
        document_id=document,
        previous_attempt_id=first.head_attempt_id,
    )
    await _raises(
        _attempt(
            db,
            project,
            request.request_id,
            "a5",
            "requested",
            previous_attempt_id=done.head_attempt_id,
        ),
        409,
        "Full text already retrieved",
    )
    await _raises(
        acquisition_service.record_attempt(
            db,
            _context(await _seed(db)),
            request.request_id,
            project.owner,
            FulltextAttemptCreate(
                outcome="requested", attempted_on=date(2026, 9, 29), idempotency_key="z"
            ),
        ),
        404,
        "Full text request not found",
    )
    assert await _count(db, ResearchFulltextAttempt) == 2


@pytest.mark.asyncio
async def test_full_text_submit_requires_retrieved(db: AsyncSession) -> None:
    project = await _project(db)
    pending, unavailable, merged_away = project.reports
    queue = await _queue(
        db, project, stage="full_text", report_ids=[pending, unavailable]
    )
    assignment = await _assign(db, project, queue.id, project.reviewer)

    async def submit(report: UUID, key: str) -> Any:
        return await _submit(db, project, queue, assignment.id, report, key)

    await _request(db, project, pending, "rq-p")
    request = await _request(db, project, unavailable, "rq-u")
    await _attempt(db, project, request.request_id, "a1")
    for report in (pending, unavailable):
        await _raises(submit(report, f"s-{report}"), 409, "Full text not retrieved")

    # A retrieval recorded on a report later merged into this one counts.
    loser_request = await _request(db, project, merged_away, "rq-m")
    await _attempt(
        db,
        project,
        loser_request.request_id,
        "a2",
        "retrieved",
        document_id=await _document(db, project),
    )
    await db.execute(
        update(ResearchReport)
        .where(ResearchReport.id == merged_away)
        .values(merged_into_report_id=pending)
    )
    assert (await submit(pending, "s-ok")).decision == "include"
    assert await _count(db, ScreeningObservation) == 1


@pytest.mark.asyncio
async def test_metadata_url_and_evidence_level_never_imply_retrieved(
    db: AsyncSession,
) -> None:
    project = await _project(db)
    report = project.reports[0]
    run_id, source_id = uuid4(), uuid4()
    db.add(ResearchRun(id=run_id, blueprint_id=uuid4(), blueprint_version=1))
    await db.flush()
    db.add(
        ResearchSource(
            id=source_id,
            run_id=run_id,
            connector_type="openalex",
            title="open access",
            url="https://example.org/oa.pdf",
            metadata_={
                "evidence_level": "full_text",
                "provenance": [
                    {"connector_type": "openalex", "full_text": "..."},
                    {"connector_type": "pubmed"},
                ],
            },
        )
    )
    await db.flush()
    db.add(
        ResearchReportObservation(
            collection_id=project.collection_id,
            report_id=report,
            source_id=source_id,
            match_method="doi",
        )
    )
    await db.flush()

    assert await acquisition_service.list_fulltext(db, _editor(project)) == []
    assert not await acquisition_service.retrieved_report_ids(
        db, project.collection_id, [report]
    )
    queue = await _queue(db, project, stage="full_text", report_ids=[report])
    assignment = await _assign(db, project, queue.id, project.reviewer)
    await _raises(
        _submit(db, project, queue, assignment.id, report, "s"),
        409,
        "Full text not retrieved",
    )
    counts = (await _flow(db, project))["counts"]
    assert counts["records_by_source"] == {"openalex": 1, "pubmed": 1}
    assert (counts["reports_sought"], counts["reports_assessed"]) == (0, 0)

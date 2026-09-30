"""GOO-301/302 screening service on SQLite (PostgreSQL race proofs live in
``tests/integration/test_screening_queue_postgres.py``)."""

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import Column, MetaData, Table, event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401  (registers every FK target table)
from src.models.base import GUID, Base
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_fulltext import (
    ResearchFulltextAttempt,
    ResearchFulltextRequest,
)
from src.models.research_import import ResearchImportReceipt, ResearchImportRecord
from src.models.research_project import ResearchProject
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.research_protocol import ResearchProtocol, ResearchProtocolVersion
from src.models.research_report import (
    ResearchReport,
    ResearchReportIdentifier,
    ResearchReportObservation,
)
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource
from src.models.research_step import ResearchStep
from src.models.screening import (
    ScreeningAssignment,
    ScreeningObservation,
    ScreeningQueue,
    ScreeningResolution,
    ScreeningSuggestion,
)
from src.schemas.research_engine import (
    ScreeningAdjudicateRequest,
    ScreeningAssignmentCreate,
    ScreeningDecisionValue,
    ScreeningObservationCreate,
    ScreeningQueueCreate,
    ScreeningReopenRequest,
    ScreeningRevokeRequest,
)
from src.services.research_decisions import DecisionReplayError
from src.services.research_engine import screening_service
from src.services.research_engine.project_access import ProjectContext

REASONS = ["wrong population", "wrong design"]
SNAPSHOT: dict[str, Any] = {
    "eligibility": {"population": "adults"},
    "selection": {"full_text_exclusion_reasons": REASONS},
    "reviewer_mode": {"mode": "dual_independent"},
}

_TABLES = (
    ResearchProtocol,
    ResearchProtocolVersion,
    ResearchDecisionStream,
    ResearchDecisionEvent,
    ResearchReport,
    ResearchReportIdentifier,
    ResearchReportObservation,
    ResearchSource,
    ResearchImportReceipt,
    ResearchImportRecord,
    ResearchProject,
    ResearchBlueprint,
    ResearchRun,
    ResearchStep,
    ResearchProjectRoleAssignment,
    ScreeningQueue,
    ScreeningAssignment,
    ScreeningObservation,
    ScreeningSuggestion,
    ScreeningResolution,
    ResearchFulltextRequest,
    ResearchFulltextAttempt,
)
# Only the columns the service reads: the real User model encrypts its PII
# columns, which needs key material unit tests do not have.
_USERS = Table(
    "users",
    MetaData(),
    Column("id", GUID(), primary_key=True),
    Column("organization_id", GUID()),
)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # created_at defaults are server-side now(); give SQLite the same function.
    event.listen(
        engine.sync_engine,
        "connect",
        lambda conn, _record: conn.create_function(
            "now", 0, lambda: datetime.now(timezone.utc).isoformat(" ")
        ),
    )
    tables = [model.__table__ for model in _TABLES]  # type: ignore[attr-defined]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
        await connection.run_sync(_USERS.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


class _Project(SimpleNamespace):
    collection_id: UUID
    owner: UUID
    supervisor: UUID
    reviewer: UUID
    reviewer2: UUID
    outsider: UUID  # REVIEWER role, but not a workspace member
    foreigner: UUID  # member with REVIEWER role, but in another organization
    organization_id: UUID
    protocol_id: UUID
    version_id: UUID
    reports: list[UUID]


def _context(project: _Project, *roles: ResearchProjectRole) -> ProjectContext:
    members = [
        SimpleNamespace(user_id=user, is_deleted=False)
        for user in (
            project.supervisor,
            project.reviewer,
            project.reviewer2,
            project.foreigner,
        )
    ]
    return cast(
        ProjectContext,
        SimpleNamespace(
            collection=SimpleNamespace(id=project.collection_id),
            workspace=SimpleNamespace(owner_id=project.owner, members=members),
            organization_id=project.organization_id,
            effective_roles=frozenset(roles),
        ),
    )


async def _version(
    db: AsyncSession, protocol_id: UUID, number: int, snapshot: dict[str, Any]
) -> UUID:
    version_id = uuid4()
    db.add(
        ResearchProtocolVersion(
            id=version_id,
            protocol_id=protocol_id,
            version=number,
            question_version_id=uuid4(),
            blueprint_id=uuid4(),
            execution_plan={},
            snapshot=snapshot,
            content_hash="a" * 64,
            status="approved",
            author_user_id=uuid4(),
        )
    )
    await db.flush()
    return version_id


async def _approve(db: AsyncSession, protocol_id: UUID, version_id: UUID) -> None:
    await db.execute(
        update(ResearchProtocol)
        .where(ResearchProtocol.id == protocol_id)
        .values(current_approved_version_id=version_id)
    )


async def _seed(db: AsyncSession, snapshot: dict[str, Any] = SNAPSHOT) -> _Project:
    project = _Project(
        collection_id=uuid4(),
        owner=uuid4(),
        supervisor=uuid4(),
        reviewer=uuid4(),
        reviewer2=uuid4(),
        outsider=uuid4(),
        foreigner=uuid4(),
        organization_id=uuid4(),
        protocol_id=uuid4(),
        reports=[],
    )
    for user in (
        project.owner,
        project.supervisor,
        project.reviewer,
        project.reviewer2,
        project.outsider,
        project.foreigner,
    ):
        foreign = user == project.foreigner
        await db.execute(
            _USERS.insert().values(
                id=user,
                organization_id=uuid4() if foreign else project.organization_id,
            )
        )
    db.add(
        ResearchProtocol(
            id=project.protocol_id, collection_id=project.collection_id, name="p"
        )
    )
    await db.flush()
    project.version_id = await _version(db, project.protocol_id, 1, snapshot)
    await _approve(db, project.protocol_id, project.version_id)
    for title in ("Alpha", "Beta", "Gamma"):
        report_id = uuid4()
        db.add(
            ResearchReport(
                id=report_id, collection_id=project.collection_id, title_snapshot=title
            )
        )
        project.reports.append(report_id)
        await db.flush()
    db.add(
        ResearchReportIdentifier(
            collection_id=project.collection_id,
            report_id=project.reports[0],
            kind="doi",
            value="10.1000/alpha",
        )
    )
    for user in (
        project.reviewer,
        project.reviewer2,
        project.outsider,
        project.foreigner,
    ):
        db.add(
            ResearchProjectRoleAssignment(
                collection_id=project.collection_id,
                user_id=user,
                role=ResearchProjectRole.REVIEWER,
                assigned_by_id=project.owner,
            )
        )
    await db.flush()
    return project


def _supervisor(project: _Project) -> ProjectContext:
    return _context(project, ResearchProjectRole.SUPERVISOR)


def _reviewer(project: _Project) -> ProjectContext:
    return _context(project, ResearchProjectRole.REVIEWER)


async def _queue(
    db: AsyncSession, project: _Project, key: str = "q1", **fields: Any
) -> Any:
    return await screening_service.create_queue(
        db,
        _supervisor(project),
        project.supervisor,
        ScreeningQueueCreate(
            protocol_version_id=fields.pop("protocol_version_id", project.version_id),
            stage=fields.pop("stage", "title_abstract"),
            idempotency_key=key,
            **fields,
        ),
    )


async def _assign(
    db: AsyncSession,
    project: _Project,
    queue_id: UUID,
    reviewer: UUID,
    key: str | None = None,
) -> Any:
    return await screening_service.assign(
        db,
        _supervisor(project),
        queue_id,
        project.supervisor,
        ScreeningAssignmentCreate(
            reviewer_user_id=reviewer, idempotency_key=key or f"assign-{reviewer}"
        ),
    )


async def _submit(
    db: AsyncSession,
    project: _Project,
    queue: Any,
    assignment_id: UUID,
    report_id: UUID,
    key: str,
    decision: ScreeningDecisionValue = "include",
    reviewer: UUID | None = None,
    **fields: Any,
) -> Any:
    return await screening_service.submit(
        db,
        _reviewer(project),
        queue.id,
        reviewer or project.reviewer,
        ScreeningObservationCreate(
            report_id=report_id,
            assignment_id=assignment_id,
            criteria_hash=fields.pop("criteria_hash", queue.criteria_hash),
            decision=decision,
            idempotency_key=key,
            **fields,
        ),
    )


async def _retrieved(db: AsyncSession, project: _Project, report_id: UUID) -> None:
    """A retrieved acquisition head for the report (GOO-303 full-text gate)."""
    request_id = uuid4()
    db.add(
        ResearchFulltextRequest(
            id=request_id,
            collection_id=project.collection_id,
            report_id=report_id,
            requested_by_id=project.owner,
        )
    )
    await db.flush()
    db.add(
        ResearchFulltextAttempt(
            request_id=request_id,
            outcome="retrieved",
            attempted_on=datetime.now(timezone.utc).date(),
            actor_id=project.owner,
            document_id=uuid4(),
            document_content_hash="d" * 64,
        )
    )
    await db.flush()


async def _count(db: AsyncSession, model: Any, **where: Any) -> int:
    query = select(func.count()).select_from(model)
    for field, value in where.items():
        query = query.where(getattr(model, field) == value)
    return int((await db.execute(query)).scalar_one())


async def _raises(awaitable: Any, status: int, detail: str | None = None) -> None:
    with pytest.raises(HTTPException) as error:
        await awaitable
    assert error.value.status_code == status
    if detail is not None:
        assert error.value.detail == detail


@pytest.mark.asyncio
async def test_ai_suggestions_never_become_observations(db: AsyncSession) -> None:
    project = await _seed(db)
    engine_id, blueprint_id, run_id, step_id = uuid4(), uuid4(), uuid4(), uuid4()
    db.add(
        ResearchProject(
            id=engine_id,
            name="engine",
            owner_id=project.owner,
            collection_id=project.collection_id,
        )
    )
    await db.flush()
    db.add(ResearchBlueprint(id=blueprint_id, project_id=engine_id, name="bp"))
    await db.flush()
    db.add(ResearchRun(id=run_id, blueprint_id=blueprint_id, blueprint_version=1))
    await db.flush()
    sources = [uuid4(), uuid4(), uuid4()]
    for index, source_id in enumerate(sources):
        db.add(
            ResearchSource(
                id=source_id,
                run_id=run_id,
                connector_type="openalex",
                title=f"s{index}",
                abstract=f"abstract {index}",
            )
        )
    await db.flush()
    for source_id, report_id in zip(sources[:2], project.reports):
        db.add(
            ResearchReportObservation(
                collection_id=project.collection_id,
                report_id=report_id,
                source_id=source_id,
                match_method="doi",
            )
        )
    db.add(
        ResearchStep(
            id=step_id,
            run_id=run_id,
            step_index=0,
            step_type="screen",
            model_id="model-x",
            completed_at=datetime.now(timezone.utc),
            output={
                "screening": [
                    {"source_id": str(s), "part_id": "1", "included": i == 0}
                    | {"reason": f"r{i}"}
                    for i, s in enumerate(sources)
                ]
            },
        )
    )
    await db.flush()

    queue = await _queue(db, project, suggestion_step_id=step_id)

    assert (queue.suggestion_count, queue.suggestions_skipped) == (2, 1)
    replayed = await _queue(db, project, suggestion_step_id=step_id)
    assert (replayed.id, replayed.suggestions_skipped) == (queue.id, 1)
    suggestions = (
        (
            await db.execute(
                select(ScreeningSuggestion).order_by(ScreeningSuggestion.decision)
            )
        )
        .scalars()
        .all()
    )
    assert [(s.report_id, s.decision, s.model_id) for s in suggestions] == [
        (project.reports[1], "exclude", "model-x"),
        (project.reports[0], "include", "model-x"),
    ]
    assert await _count(db, ScreeningObservation) == 0
    events = (await db.execute(select(ResearchDecisionEvent.event_type))).scalars()
    assert list(events) == ["screening.queue_created"]

    assignment = await _assign(db, project, queue.id, project.reviewer)
    mine = await screening_service.my_queue(
        db, _reviewer(project), queue.id, project.reviewer
    )
    assert mine.assignment_id == assignment.id
    assert "suggest" not in mine.model_dump_json()
    first = mine.items[0]
    assert (first.title_snapshot, first.abstract) == ("Alpha", "abstract 0")
    assert first.identifiers == {"doi": ["10.1000/alpha"]}
    assert mine.counts.model_dump() == {
        "total": 3,
        "screened": 0,
        "remaining": 3,
        "revealed": 0,
        "conflicts": 0,
    }

    non_screen = uuid4()
    db.add(ResearchStep(id=non_screen, run_id=run_id, step_index=1, step_type="search"))
    await db.flush()
    await _raises(_queue(db, project, "q2", suggestion_step_id=non_screen), 422)
    await _raises(_queue(db, project, "q3", suggestion_step_id=uuid4()), 404)


@pytest.mark.asyncio
async def test_submit_requires_human_actor_with_assignment(db: AsyncSession) -> None:
    project = await _seed(db)
    queue = await _queue(db, project)
    peer = await _assign(db, project, queue.id, project.reviewer2)

    # Holding REVIEWER is not enough; nor is someone else's assignment.
    await _raises(
        _submit(db, project, queue, uuid4(), project.reports[0], "k1"),
        403,
        "Not assigned to this queue",
    )
    await _raises(
        _submit(db, project, queue, peer.id, project.reports[0], "k2"),
        403,
        "Not assigned to this queue",
    )
    await _raises(
        screening_service.my_queue(db, _context(project), queue.id, project.owner),
        403,
        "reviewer role required",
    )
    await _raises(
        screening_service.my_queue(db, _reviewer(project), queue.id, project.reviewer),
        403,
        "Not assigned to this queue",
    )
    assert await _count(db, ScreeningObservation) == 0


@pytest.mark.asyncio
async def test_assignment_revisions(db: AsyncSession) -> None:
    project = await _seed(db)
    queue = await _queue(db, project)
    await _raises(_assign(db, project, queue.id, project.owner), 422)
    assignment = await _assign(db, project, queue.id, project.reviewer)
    await _raises(
        screening_service.assign(
            db,
            _supervisor(project),
            queue.id,
            project.supervisor,
            ScreeningAssignmentCreate(
                reviewer_user_id=project.reviewer, idempotency_key="again"
            ),
        ),
        409,
        "Reviewer already assigned",
    )
    revoke = ScreeningRevokeRequest(reason="left", idempotency_key="revoke")
    revoked = await screening_service.revoke(
        db, _supervisor(project), queue.id, assignment.id, project.supervisor, revoke
    )
    assert revoked.revoked_by_id == project.supervisor
    await _raises(
        _submit(db, project, queue, assignment.id, project.reports[0], "k"),
        409,
        "Assignment revoked",
    )
    # The original key replays the (now revoked) revision; a new key re-assigns.
    replayed = await _assign(db, project, queue.id, project.reviewer)
    assert (replayed.id, replayed.revoked_by_id) == (assignment.id, project.supervisor)
    renewed = await _assign(db, project, queue.id, project.reviewer, "renew")
    assert renewed.id != assignment.id
    await _submit(db, project, queue, renewed.id, project.reports[0], "k")
    history = await screening_service.history(
        db, _supervisor(project), queue.id, project.supervisor
    )
    assert [e.event_type for e in history] == [
        "screening.queue_created",
        "screening.assigned",
        "screening.unassigned",
        "screening.assigned",
        "screening.observed",
    ]


@pytest.mark.asyncio
async def test_full_text_reason_validated_against_protocol_list(
    db: AsyncSession,
) -> None:
    project = await _seed(db)
    await _raises(_queue(db, project, stage="full_text"), 422)
    queue = await _queue(
        db, project, stage="full_text", report_ids=project.reports[1:2]
    )
    assignment = await _assign(db, project, queue.id, project.reviewer)
    report = project.reports[1]
    await _raises(
        _submit(db, project, queue, assignment.id, report, "a", "exclude"), 422
    )
    await _raises(
        _submit(
            db,
            project,
            queue,
            assignment.id,
            report,
            "b",
            "exclude",
            exclusion_reason="too old",
        ),
        422,
    )
    await _raises(
        _submit(db, project, queue, assignment.id, project.reports[0], "c"), 404
    )
    # GOO-303: full text must be retrieved before any full-text decision.
    await _raises(
        _submit(
            db,
            project,
            queue,
            assignment.id,
            report,
            "d",
            "exclude",
            exclusion_reason="wrong design",
        ),
        409,
        "Full text not retrieved",
    )
    await _retrieved(db, project, report)
    observation = await _submit(
        db,
        project,
        queue,
        assignment.id,
        report,
        "d",
        "exclude",
        exclusion_reason="wrong design",
    )
    assert observation.exclusion_reason == "wrong design"

    no_reasons = await _seed(db, {**SNAPSHOT, "selection": {"method": "dual"}})
    await _raises(
        _queue(
            db,
            no_reasons,
            stage="full_text",
            report_ids=no_reasons.reports,
        ),
        422,
        "Protocol defines no full-text exclusion reasons",
    )


@pytest.mark.asyncio
async def test_changed_decision_supersedes_and_keeps_history(db: AsyncSession) -> None:
    project = await _seed(db)
    queue = await _queue(db, project)
    assert queue.report_count == 3
    assignment = await _assign(db, project, queue.id, project.reviewer)
    report = project.reports[0]
    first = await _submit(db, project, queue, assignment.id, report, "k1")

    # Retry replays; a reused key with other content conflicts.
    retry = await _submit(db, project, queue, assignment.id, report, "k1")
    assert retry.id == first.id
    await _raises(
        _submit(db, project, queue, assignment.id, report, "k1", "exclude"),
        409,
        "Idempotency conflict",
    )
    await _raises(
        _submit(db, project, queue, assignment.id, report, "k2", "exclude"),
        409,
        "Observation exists; supersede the current observation",
    )

    second = await _submit(
        db,
        project,
        queue,
        assignment.id,
        report,
        "k3",
        "uncertain",
        note="needs full text",
        supersedes_observation_id=first.id,
    )
    await _raises(
        _submit(
            db,
            project,
            queue,
            assignment.id,
            report,
            "k4",
            supersedes_observation_id=first.id,
        ),
        409,
        "Observation exists; supersede the current observation",
    )

    rows = (
        (
            await db.execute(
                select(ScreeningObservation).order_by(ScreeningObservation.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert [(r.id, r.decision, r.supersedes_observation_id) for r in rows] == [
        (first.id, "include", None),
        (second.id, "uncertain", first.id),
    ]
    history = await screening_service.history(
        db, _supervisor(project), queue.id, project.supervisor
    )
    # GOO-302: supervising grants no decisions; unrevealed ones are redacted.
    assert history[-1].redacted and history[-1].reason is None
    history = await screening_service.history(
        db, _reviewer(project), queue.id, project.reviewer
    )
    assert [e.event_type for e in history][-2:] == [
        "screening.observed",
        "screening.superseded",
    ]
    assert history[-1].reason == "needs full text"
    assert history[-1].payload["superseded_observation_id"] == str(first.id)

    mine = await screening_service.my_queue(
        db, _reviewer(project), queue.id, project.reviewer
    )
    assert mine.items[0].observation is not None
    assert mine.items[0].observation.id == second.id
    assert mine.counts.model_dump() == {
        "total": 3,
        "screened": 1,
        "remaining": 2,
        "revealed": 0,
        "conflicts": 0,
    }

    listed = await screening_service.list_queues(db, _reviewer(project))
    assert [(q.id, q.assignment_count, q.observation_count) for q in listed] == [
        (queue.id, 1, 1)
    ]


@pytest.mark.asyncio
async def test_stale_criteria_hash_is_409(db: AsyncSession) -> None:
    project = await _seed(db)
    queue = await _queue(db, project)
    assignment = await _assign(db, project, queue.id, project.reviewer)
    report = project.reports[0]
    await _raises(
        _submit(db, project, queue, assignment.id, report, "a", criteria_hash="0" * 64),
        409,
        "Screening criteria changed; reload the queue",
    )

    # An approved amendment makes the queue stale until a supervisor reconciles.
    amended = await _version(
        db, project.protocol_id, 2, {**SNAPSHOT, "eligibility": {"population": "all"}}
    )
    await _approve(db, project.protocol_id, amended)
    await _raises(
        _submit(db, project, queue, assignment.id, report, "b"),
        409,
        "Protocol version changed; reconcile queue",
    )
    await _raises(
        _queue(db, project, "stale-version"), 409, "Protocol version is not current"
    )
    reconciled = await _queue(
        db, project, "q2", protocol_version_id=amended, supersedes_queue_id=queue.id
    )
    assert reconciled.criteria_hash != queue.criteria_hash
    await _raises(
        _queue(
            db, project, "q3", protocol_version_id=amended, supersedes_queue_id=queue.id
        ),
        409,
        "Screening queue already reconciled",
    )
    mine = await screening_service.my_queue(
        db, _reviewer(project), queue.id, project.reviewer
    )
    assert mine.queue.stale == "Screening queue superseded; use the reconciled queue"
    await _raises(
        _submit(db, project, queue, assignment.id, report, "c"),
        409,
        "Screening queue superseded; use the reconciled queue",
    )
    fresh = await _assign(db, project, reconciled.id, project.reviewer)
    await _submit(db, project, reconciled, fresh.id, report, "d")


@pytest.mark.asyncio
async def test_queue_without_reviewer_mode_is_422(db: AsyncSession) -> None:
    for mode in ({}, {"mode": "independent"}):
        project = await _seed(db, {**SNAPSHOT, "reviewer_mode": mode})
        await _raises(
            _queue(db, project), 422, "Protocol declares no screening reviewer mode"
        )
    assert await _count(db, ScreeningQueue) == 0


@pytest.mark.asyncio
async def test_foreign_queue_and_protocol_are_404(db: AsyncSession) -> None:
    project, other = await _seed(db), await _seed(db)
    queue = await _queue(db, project)
    await _raises(
        screening_service.history(db, _supervisor(other), queue.id, other.supervisor),
        404,
        "Screening queue not found",
    )
    await _raises(_queue(db, other, protocol_version_id=project.version_id), 404)
    await _raises(_queue(db, other, report_ids=project.reports), 404)
    # A reused create key replays the same queue.
    assert (await _queue(db, project)).id == queue.id


@pytest.mark.asyncio
async def test_unique_index_backstop_is_a_409_not_a_500(db: AsyncSession) -> None:
    """A write that slips past the service checks still gets a stable 409."""
    project = await _seed(db)
    queue = await _queue(db, project)
    await _assign(db, project, queue.id, project.reviewer)
    db.add(
        ScreeningAssignment(
            queue_id=queue.id,
            reviewer_id=project.reviewer,
            assigned_by_id=project.supervisor,
        )
    )
    await _raises(
        screening_service._flush_unique(db, "Reviewer already assigned"),
        409,
        "Reviewer already assigned",
    )


async def _screen_step(
    db: AsyncSession,
    project: _Project,
    reports: list[UUID],
    *,
    collection_id: UUID | None = None,
    completed: bool = True,
) -> UUID:
    """A screen step whose run belongs to ``collection_id``'s engine project;
    it includes one observed source per report."""
    engine_id, blueprint_id, run_id, step_id = uuid4(), uuid4(), uuid4(), uuid4()
    db.add(
        ResearchProject(
            id=engine_id,
            name="engine",
            owner_id=project.owner,
            collection_id=collection_id or project.collection_id,
        )
    )
    await db.flush()
    db.add(ResearchBlueprint(id=blueprint_id, project_id=engine_id, name="bp"))
    await db.flush()
    db.add(ResearchRun(id=run_id, blueprint_id=blueprint_id, blueprint_version=1))
    await db.flush()
    sources = [uuid4() for _ in reports]
    for source_id in sources:
        db.add(
            ResearchSource(
                id=source_id, run_id=run_id, connector_type="openalex", title="s"
            )
        )
    await db.flush()
    for source_id, report_id in zip(sources, reports):
        db.add(
            ResearchReportObservation(
                collection_id=project.collection_id,
                report_id=report_id,
                source_id=source_id,
                match_method="doi",
            )
        )
    db.add(
        ResearchStep(
            id=step_id,
            run_id=run_id,
            step_index=0,
            step_type="screen",
            completed_at=datetime.now(timezone.utc) if completed else None,
            output={
                "screening": [
                    {"source_id": str(s), "part_id": "1", "included": True}
                    | {"reason": "fits"}
                    for s in sources
                ]
            },
        )
    )
    await db.flush()
    return step_id


@pytest.mark.asyncio
async def test_supervisor_and_reviewer_roles_are_required(db: AsyncSession) -> None:
    """An owner/editor context holds no decision role and is refused."""
    project = await _seed(db)
    no_role = _context(project)
    await _raises(
        screening_service.create_queue(
            db,
            no_role,
            project.owner,
            ScreeningQueueCreate(
                protocol_version_id=project.version_id,
                stage="title_abstract",
                report_ids=None,
                idempotency_key="q",
            ),
        ),
        403,
        "supervisor role required",
    )
    queue = await _queue(db, project)
    assignment = await _assign(db, project, queue.id, project.reviewer)
    await _raises(
        screening_service.assign(
            db,
            no_role,
            queue.id,
            project.owner,
            ScreeningAssignmentCreate(
                reviewer_user_id=project.reviewer2, idempotency_key="a"
            ),
        ),
        403,
        "supervisor role required",
    )
    await _raises(
        screening_service.revoke(
            db,
            no_role,
            queue.id,
            assignment.id,
            project.owner,
            ScreeningRevokeRequest(reason="r", idempotency_key="r"),
        ),
        403,
        "supervisor role required",
    )
    await _raises(
        screening_service.submit(
            db,
            _context(project, ResearchProjectRole.SUPERVISOR),
            queue.id,
            project.reviewer,
            ScreeningObservationCreate(
                report_id=project.reports[0],
                assignment_id=assignment.id,
                criteria_hash=queue.criteria_hash,
                decision="include",
                exclusion_reason=None,
                note=None,
                idempotency_key="s",
            ),
        ),
        403,
        "reviewer role required",
    )
    assert await _count(db, ScreeningObservation) == 0


@pytest.mark.asyncio
async def test_suggestion_step_must_be_this_projects_completed_screen(
    db: AsyncSession,
) -> None:
    project = await _seed(db)
    foreign = await _screen_step(db, project, project.reports, collection_id=uuid4())
    await _raises(
        _queue(db, project, "a", suggestion_step_id=foreign), 404, "Step not found"
    )
    running = await _screen_step(db, project, project.reports, completed=False)
    await _raises(
        _queue(db, project, "b", suggestion_step_id=running),
        422,
        "Step is not a completed screen step",
    )
    assert await _count(db, ScreeningQueue) == 0


@pytest.mark.asyncio
async def test_suggestions_outside_the_frozen_corpus_are_skipped(
    db: AsyncSession,
) -> None:
    project = await _seed(db)
    step = await _screen_step(db, project, project.reports[:2])
    queue = await _queue(
        db, project, report_ids=project.reports[:1], suggestion_step_id=step
    )
    assert (queue.suggestion_count, queue.suggestions_skipped) == (1, 1)
    (row,) = (await db.execute(select(ScreeningSuggestion))).scalars().all()
    assert row.report_id == project.reports[0]


@pytest.mark.asyncio
async def test_assign_requires_a_member_in_the_project_organization(
    db: AsyncSession,
) -> None:
    project = await _seed(db)
    queue = await _queue(db, project)
    for user in (project.outsider, project.foreigner):
        await _raises(
            _assign(db, project, queue.id, user),
            422,
            "User is not an eligible reviewer",
        )
    await _assign(db, project, queue.id, project.reviewer)


@pytest.mark.asyncio
async def test_merged_report_is_409(db: AsyncSession) -> None:
    project = await _seed(db)
    queue = await _queue(db, project)
    assignment = await _assign(db, project, queue.id, project.reviewer)
    await db.execute(
        update(ResearchReport)
        .where(ResearchReport.id == project.reports[1])
        .values(merged_into_report_id=project.reports[0])
    )
    await _raises(
        _submit(db, project, queue, assignment.id, project.reports[1], "k"),
        409,
        "Report merged; reconcile queue",
    )


@pytest.mark.asyncio
async def test_create_key_reused_for_another_queue_is_409(db: AsyncSession) -> None:
    project = await _seed(db)
    await _queue(db, project, "same")
    await _raises(
        _queue(db, project, "same", report_ids=project.reports[:1]),
        409,
        "Idempotency conflict",
    )
    assert await _count(db, ScreeningQueue) == 1


class _Orig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("constraint violated")
        self.sqlstate = sqlstate


@pytest.mark.asyncio
@pytest.mark.parametrize(("sqlstate", "mapped"), [("23505", True), ("23503", False)])
async def test_flush_backstop_maps_only_unique_violations(
    sqlstate: str, mapped: bool
) -> None:
    from unittest.mock import AsyncMock

    from sqlalchemy.exc import IntegrityError

    session = AsyncMock()
    session.flush.side_effect = IntegrityError("INSERT", {}, _Orig(sqlstate))
    if mapped:
        await _raises(screening_service._flush_unique(session, "taken"), 409, "taken")
    else:
        with pytest.raises(IntegrityError):
            await screening_service._flush_unique(session, "taken")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redistribution", "shown"), [("restricted", False), ("allowed", True)]
)
async def test_imported_abstract_respects_redistribution(
    db: AsyncSession, redistribution: str, shown: bool
) -> None:
    """A restricted import's abstract stays in the database (GOO-300 rule)."""
    project = await _seed(db)
    receipt_id = uuid4()
    db.add(
        ResearchImportReceipt(
            id=receipt_id,
            collection_id=project.collection_id,
            kind="file_import",
            dedup_key=f"file:{receipt_id.hex}",
            lineage_key="l" * 64,
            version=1,
            declared={"redistribution": redistribution},
            observed={},
            parsed_count=1,
            accepted_count=1,
            rejected_count=0,
            actor_user_id=project.owner,
        )
    )
    await db.flush()
    db.add(
        ResearchImportRecord(
            collection_id=project.collection_id,
            receipt_id=receipt_id,
            record_index=0,
            status="accepted",
            raw="TI  - Alpha",
            parsed={"title": "Alpha", "abstract": "Licensed abstract text."},
            report_id=project.reports[0],
            match_method="doi",
            evidence={},
        )
    )
    await db.flush()
    queue = await _queue(db, project)
    await _assign(db, project, queue.id, project.reviewer)

    mine = await screening_service.my_queue(
        db, _reviewer(project), queue.id, project.reviewer
    )

    assert mine.items[0].abstract == ("Licensed abstract text." if shown else None)


# --- GOO-302: blind reveal, derived resolutions and adjudication ------------


def _adjudicator(project: _Project) -> ProjectContext:
    return _context(project, ResearchProjectRole.ADJUDICATOR)


class _Dual(SimpleNamespace):
    project: _Project
    queue: Any
    a: Any  # reviewer's assignment
    a2: Any  # reviewer2's assignment


async def _dual(db: AsyncSession, stage: str = "title_abstract") -> _Dual:
    project = await _seed(db)
    queue = await _queue(
        db,
        project,
        stage=stage,
        report_ids=project.reports if stage == "full_text" else None,
    )
    return _Dual(
        project=project,
        queue=queue,
        a=await _assign(db, project, queue.id, project.reviewer),
        a2=await _assign(db, project, queue.id, project.reviewer2),
    )


async def _vote(
    db: AsyncSession,
    dual: _Dual,
    who: str,
    report: UUID,
    key: str,
    decision: ScreeningDecisionValue = "include",
    **fields: Any,
) -> Any:
    first = who == "r"
    return await _submit(
        db,
        dual.project,
        dual.queue,
        (dual.a if first else dual.a2).id,
        report,
        key,
        decision,
        reviewer=dual.project.reviewer if first else dual.project.reviewer2,
        **fields,
    )


def _adjudicate_body(
    conflict: Any, key: str, decision: ScreeningDecisionValue = "exclude", **fields: Any
) -> ScreeningAdjudicateRequest:
    return ScreeningAdjudicateRequest(
        resolution_id=fields.pop("resolution_id", conflict.resolution.id),
        input_observation_ids=fields.pop(
            "input_observation_ids", conflict.resolution.input_observation_ids
        ),
        criteria_hash=conflict.resolution.criteria_hash,
        decision=decision,
        rationale="protocol 3.2 excludes it",
        idempotency_key=key,
        **fields,
    )


async def _conflicts(db: AsyncSession, dual: _Dual) -> list[Any]:
    return await screening_service.conflicts(
        db, _adjudicator(dual.project), dual.queue.id, dual.project.supervisor
    )


@pytest.mark.asyncio
async def test_pre_reveal_peer_observation_absent_from_mine_and_history(
    db: AsyncSession,
) -> None:
    dual = await _dual(db)
    project, report = dual.project, dual.project.reports[0]
    peer = await _vote(db, dual, "r2", report, "p1", "exclude", note="R2-SENTINEL-7f3")
    assert peer.resolution is None
    await _vote(db, dual, "r", project.reports[1], "own")

    mine = await screening_service.my_queue(
        db, _reviewer(project), dual.queue.id, project.reviewer
    )
    body = mine.model_dump_json()
    assert "R2-SENTINEL-7f3" not in body and str(peer.id) not in body
    item = mine.items[0]
    assert (item.reveal_state, item.others, item.resolution) == ("hidden", [], None)

    for viewer, context in (
        (project.reviewer, _reviewer(project)),
        (project.owner, _context(project)),  # a role-less VIEW member
    ):
        history = await screening_service.history(db, context, dual.queue.id, viewer)
        body = "".join(event.model_dump_json() for event in history)
        assert "R2-SENTINEL-7f3" not in body
        [hidden] = [
            e for e in history if e.payload.get("observation_id") == str(peer.id)
        ]
        assert hidden.redacted and hidden.reason is None
        assert "decision" not in hidden.payload
        assert "exclusion_reason" not in hidden.payload
        own = [
            e
            for e in history
            if "observation_id" in e.payload and e.payload["reviewer_id"] == str(viewer)
        ]
        assert all(not e.redacted and "decision" in e.payload for e in own)
    # The author still sees their own note.
    history = await screening_service.history(
        db, _reviewer(project), dual.queue.id, project.reviewer2
    )
    assert any(e.reason == "R2-SENTINEL-7f3" for e in history)
    listed = await screening_service.list_queues(db, _reviewer(project))
    assert (listed[0].resolved_count, listed[0].conflict_count) == (0, 0)


@pytest.mark.asyncio
async def test_second_dual_submission_creates_one_resolution_and_reveals(
    db: AsyncSession,
) -> None:
    dual = await _dual(db)
    project, report = dual.project, dual.project.reports[0]
    first = await _vote(db, dual, "r", report, "k1", note="looks relevant")
    second = await _vote(db, dual, "r2", report, "k2", "exclude")
    resolution = second.resolution
    assert resolution is not None
    assert (resolution.basis, resolution.outcome) == ("conflict", None)
    assert resolution.input_observation_ids == sorted([first.id, second.id], key=str)
    assert await _count(db, ScreeningResolution) == 1
    event_id = (
        await db.execute(
            select(ResearchDecisionEvent.id).where(
                ResearchDecisionEvent.idempotency_key == "k2"
            )
        )
    ).scalar_one()
    row = await db.get(ScreeningResolution, resolution.id)
    assert row is not None and row.event_id == event_id
    # An identical retry replays the same resolution; nothing new is written.
    retry = await _vote(db, dual, "r2", report, "k2", "exclude")
    assert retry.resolution is not None and retry.resolution.id == resolution.id
    assert await _count(db, ScreeningResolution) == 1

    mine = await screening_service.my_queue(
        db, _reviewer(project), dual.queue.id, project.reviewer
    )
    item = mine.items[0]
    assert item.reveal_state == "revealed"
    assert [o.id for o in item.others] == [second.id]
    assert item.resolution is not None and item.resolution.id == resolution.id
    assert (mine.counts.revealed, mine.counts.conflicts) == (1, 1)
    history = await screening_service.history(
        db, _context(project), dual.queue.id, project.owner
    )
    assert not any(e.redacted for e in history)
    assert any(e.reason == "looks relevant" for e in history)
    listed = await screening_service.list_queues(db, _reviewer(project))
    assert (listed[0].resolved_count, listed[0].conflict_count) == (0, 1)

    # Agreement on another report resolves with the shared decision.
    await _vote(db, dual, "r", project.reports[1], "k3")
    agreed = await _vote(db, dual, "r2", project.reports[1], "k4")
    assert agreed.resolution is not None
    assert (agreed.resolution.basis, agreed.resolution.outcome) == (
        "agreement",
        "include",
    )
    listed = await screening_service.list_queues(db, _reviewer(project))
    assert (listed[0].resolved_count, listed[0].conflict_count) == (1, 1)


@pytest.mark.asyncio
async def test_submit_after_resolution_409_until_reopen(db: AsyncSession) -> None:
    dual = await _dual(db)
    project, report = dual.project, dual.project.reports[0]
    first = await _vote(db, dual, "r", report, "k1")
    second = await _vote(db, dual, "r2", report, "k2")
    await _raises(
        _vote(
            db, dual, "r", report, "k3", "exclude", supersedes_observation_id=first.id
        ),
        409,
        "Report resolved; reopen to change",
    )
    assert second.resolution is not None
    await _raises(
        screening_service.reopen(
            db,
            _reviewer(project),
            dual.queue.id,
            report,
            project.reviewer,
            ScreeningReopenRequest(
                resolution_id=second.resolution.id, rationale="x", idempotency_key="o"
            ),
        ),
        403,
        "adjudicator role required",
    )
    reopened = await screening_service.reopen(
        db,
        _adjudicator(project),
        dual.queue.id,
        report,
        project.supervisor,
        ScreeningReopenRequest(
            resolution_id=second.resolution.id,
            rationale="new evidence",
            idempotency_key="reopen",
        ),
    )
    assert (reopened.basis, reopened.input_observation_ids) == ("reopened", [])
    assert reopened.supersedes_resolution_id == second.resolution.id
    await _raises(
        screening_service.reopen(
            db,
            _adjudicator(project),
            dual.queue.id,
            report,
            project.supervisor,
            ScreeningReopenRequest(
                resolution_id=reopened.id, rationale="again", idempotency_key="o2"
            ),
        ),
        409,
        "Report is not resolved",
    )
    changed = await _vote(
        db, dual, "r", report, "k3", "exclude", supersedes_observation_id=first.id
    )
    assert changed.resolution is None
    # Reopen and observations are ledger events; nothing was edited in place.
    rows = (await db.execute(select(ScreeningObservation))).scalars().all()
    assert {(r.id, r.decision) for r in rows} >= {
        (first.id, "include"),
        (second.id, "include"),
    }
    history = await screening_service.history(
        db, _supervisor(project), dual.queue.id, project.supervisor
    )
    assert [e.event_type for e in history][-2:] == [
        "screening.reopened",
        "screening.superseded",
    ]
    assert history[-2].reason == "new evidence"


@pytest.mark.asyncio
async def test_reopen_requires_fresh_observations_from_both(db: AsyncSession) -> None:
    dual = await _dual(db)
    project, report = dual.project, dual.project.reports[0]
    first = await _vote(db, dual, "r", report, "k1")
    second = await _vote(db, dual, "r2", report, "k2")
    assert second.resolution is not None
    await screening_service.reopen(
        db,
        _adjudicator(project),
        dual.queue.id,
        report,
        project.supervisor,
        ScreeningReopenRequest(
            resolution_id=second.resolution.id, rationale="recheck", idempotency_key="o"
        ),
    )
    reopened = await screening_service.my_queue(
        db, _reviewer(project), dual.queue.id, project.reviewer
    )
    # A reopen starts a new blind cycle: hidden again, the reopen still shown.
    item = reopened.items[0]
    assert item.reveal_state == "hidden"
    assert item.resolution is not None and item.resolution.basis == "reopened"
    assert (reopened.counts.revealed, reopened.counts.conflicts) == (0, 0)
    again = await _vote(
        db, dual, "r", report, "k3", "exclude", supersedes_observation_id=first.id
    )
    assert again.resolution is None  # r2's old input is not fresh
    mine = await screening_service.my_queue(
        db, _reviewer(project), dual.queue.id, project.reviewer2
    )
    # r's new observation is not an input yet: hidden from r2.
    assert str(again.id) not in mine.model_dump_json()
    fresh = await _vote(
        db, dual, "r2", report, "k4", "exclude", supersedes_observation_id=second.id
    )
    assert fresh.resolution is not None
    assert fresh.resolution.basis == "agreement"
    assert fresh.resolution.input_observation_ids == sorted(
        [again.id, fresh.id], key=str
    )
    assert await _count(db, ScreeningResolution) == 3
    history = await screening_service.history(
        db, _supervisor(project), dual.queue.id, project.supervisor
    )
    assert history[-1].event_type == "screening.superseded"


async def _conflict(db: AsyncSession, dual: _Dual) -> Any:
    report = dual.project.reports[0]
    await _vote(db, dual, "r", report, "c1")
    await _vote(db, dual, "r2", report, "c2", "exclude")
    [conflict] = await _conflicts(db, dual)
    return conflict


@pytest.mark.asyncio
async def test_adjudicate_stale_inputs_409(db: AsyncSession) -> None:
    dual = await _dual(db)
    project, report = dual.project, dual.project.reports[0]
    conflict = await _conflict(db, dual)
    assert conflict.report_id == report and conflict.title_snapshot == "Alpha"
    assert conflict.identifiers == {"doi": ["10.1000/alpha"]}
    assert conflict.exclusion_reasons == REASONS
    assert {o.id for o in conflict.observations} == set(
        conflict.resolution.input_observation_ids
    )

    async def adjudicate(body: ScreeningAdjudicateRequest) -> Any:
        return await screening_service.adjudicate(
            db, _adjudicator(project), dual.queue.id, report, project.supervisor, body
        )

    events = await _count(db, ResearchDecisionEvent)
    for stale in (
        {"resolution_id": uuid4()},
        {"input_observation_ids": conflict.resolution.input_observation_ids[:1]},
    ):
        await _raises(
            adjudicate(_adjudicate_body(conflict, "s", **stale)),
            409,
            "Adjudication inputs are stale",
        )
    await _raises(
        adjudicate(
            _adjudicate_body(conflict, "c").model_copy(
                update={"criteria_hash": "0" * 64}
            )
        ),
        409,
        "Screening criteria changed; reload the queue",
    )
    await _raises(
        adjudicate(_adjudicate_body(conflict, "r", exclusion_reason="wrong design")),
        422,
    )
    assert await _count(db, ResearchDecisionEvent) == events
    assert await _count(db, ScreeningResolution) == 1

    before = {
        (r.id, r.decision, r.note)
        for r in (await db.execute(select(ScreeningObservation))).scalars()
    }
    done = await adjudicate(_adjudicate_body(conflict, "ok"))
    assert (done.basis, done.outcome) == ("adjudicated", "exclude")
    assert done.supersedes_resolution_id == conflict.resolution.id
    assert done.input_observation_ids == conflict.resolution.input_observation_ids
    assert (await adjudicate(_adjudicate_body(conflict, "ok"))).id == done.id
    after = {
        (r.id, r.decision, r.note)
        for r in (await db.execute(select(ScreeningObservation))).scalars()
    }
    assert after == before
    await _raises(
        adjudicate(_adjudicate_body(conflict, "again", resolution_id=done.id)),
        409,
        "Report is not in conflict",
    )
    assert await _conflicts(db, dual) == []
    history = await screening_service.history(
        db, _supervisor(project), dual.queue.id, project.supervisor
    )
    assert history[-1].event_type == "screening.adjudicated"
    assert history[-1].reason == "protocol 3.2 excludes it"


@pytest.mark.asyncio
async def test_adjudicator_who_reviewed_is_403(db: AsyncSession) -> None:
    dual = await _dual(db)
    project = dual.project
    conflict = await _conflict(db, dual)
    await _raises(
        screening_service.adjudicate(
            db,
            _adjudicator(project),
            dual.queue.id,
            conflict.report_id,
            project.reviewer2,
            _adjudicate_body(conflict, "self"),
        ),
        403,
        "Adjudicator reviewed this report",
    )
    assert await _count(db, ScreeningResolution) == 1


@pytest.mark.asyncio
async def test_owner_without_role_cannot_list_conflicts(db: AsyncSession) -> None:
    dual = await _dual(db)
    project = dual.project
    conflict = await _conflict(db, dual)
    for context in (_context(project), _supervisor(project), _reviewer(project)):
        await _raises(
            screening_service.conflicts(db, context, dual.queue.id, project.owner),
            403,
            "adjudicator role required",
        )
        await _raises(
            screening_service.adjudicate(
                db,
                context,
                dual.queue.id,
                conflict.report_id,
                project.owner,
                _adjudicate_body(conflict, "own"),
            ),
            403,
            "adjudicator role required",
        )
    other = await _seed(db)
    await _raises(
        screening_service.conflicts(
            db, _adjudicator(other), dual.queue.id, other.supervisor
        ),
        404,
        "Screening queue not found",
    )


@pytest.mark.asyncio
async def test_single_mode_resolves_immediately(db: AsyncSession) -> None:
    project = await _seed(db, {**SNAPSHOT, "reviewer_mode": {"mode": "single"}})
    queue = await _queue(db, project)
    assignment = await _assign(db, project, queue.id, project.reviewer)
    report = project.reports[0]
    done = await _submit(db, project, queue, assignment.id, report, "k1", "exclude")
    assert done.resolution is not None
    assert (done.resolution.basis, done.resolution.outcome) == ("single", "exclude")
    await _raises(
        _submit(
            db,
            project,
            queue,
            assignment.id,
            report,
            "k2",
            supersedes_observation_id=done.id,
        ),
        409,
        "Report resolved; reopen to change",
    )
    unsure = await _submit(
        db, project, queue, assignment.id, project.reports[1], "k3", "uncertain"
    )
    assert unsure.resolution is not None
    assert (unsure.resolution.basis, unsure.resolution.outcome) == ("conflict", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift",
    [{"basis": "agreement", "outcome": "include"}, {"criteria_hash": "0" * 64}],
)
async def test_history_rejects_a_resolution_that_drifted_from_its_inputs(
    db: AsyncSession, drift: dict[str, str]
) -> None:
    dual = await _dual(db)
    project, report = dual.project, dual.project.reports[0]
    await _vote(db, dual, "r", report, "k1")
    await _vote(db, dual, "r2", report, "k2", "exclude")
    await db.execute(update(ScreeningResolution).values(**drift))
    with pytest.raises(DecisionReplayError, match="does not match inputs"):
        await screening_service.history(
            db, _supervisor(project), dual.queue.id, project.supervisor
        )


@pytest.mark.asyncio
async def test_reopen_with_a_stale_resolution_id_is_409(db: AsyncSession) -> None:
    dual = await _dual(db)
    project = dual.project
    conflict = await _conflict(db, dual)
    await _raises(
        screening_service.reopen(
            db,
            _adjudicator(project),
            dual.queue.id,
            conflict.report_id,
            project.supervisor,
            ScreeningReopenRequest(
                resolution_id=uuid4(), rationale="x", idempotency_key="stale"
            ),
        ),
        409,
        "Resolution changed; reload the queue",
    )
    assert await _count(db, ScreeningResolution) == 1


@pytest.mark.asyncio
async def test_adjudicator_who_reviewed_an_earlier_cycle_is_403(
    db: AsyncSession,
) -> None:
    """The supervisor reviews cycle 1, is not an input of cycle 2, and still
    may not adjudicate that report (any observation, any cycle)."""
    dual = await _dual(db)
    project, report = dual.project, dual.project.reports[0]
    db.add(
        ResearchProjectRoleAssignment(
            collection_id=project.collection_id,
            user_id=project.supervisor,
            role=ResearchProjectRole.REVIEWER,
            assigned_by_id=project.owner,
        )
    )
    await db.flush()
    third = await _assign(db, project, dual.queue.id, project.supervisor)
    both = _context(
        project, ResearchProjectRole.REVIEWER, ResearchProjectRole.ADJUDICATOR
    )
    await screening_service.submit(
        db,
        both,
        dual.queue.id,
        project.supervisor,
        ScreeningObservationCreate(
            report_id=report,
            assignment_id=third.id,
            criteria_hash=dual.queue.criteria_hash,
            decision="include",
            idempotency_key="s1",
        ),
    )
    first = await _vote(db, dual, "r", report, "k1", "exclude")
    assert first.resolution is not None and first.resolution.basis == "conflict"
    await screening_service.reopen(
        db,
        _adjudicator(project),
        dual.queue.id,
        report,
        project.owner,
        ScreeningReopenRequest(
            resolution_id=first.resolution.id, rationale="r", idempotency_key="o"
        ),
    )
    await _vote(db, dual, "r", report, "k2", supersedes_observation_id=first.id)
    await _vote(db, dual, "r2", report, "k3", "exclude")
    [conflict] = await _conflicts(db, dual)
    assert not any(o.reviewer_id == project.supervisor for o in conflict.observations)
    await _raises(
        screening_service.adjudicate(
            db,
            both,
            dual.queue.id,
            report,
            project.supervisor,
            _adjudicate_body(conflict, "self"),
        ),
        403,
        "Adjudicator reviewed this report",
    )
    history = await screening_service.history(
        db, _supervisor(project), dual.queue.id, project.supervisor
    )
    assert history[-1].event_type == "screening.observed"


@pytest.mark.asyncio
async def test_adjudicator_who_reviewed_in_a_reconciled_queue_is_403(
    db: AsyncSession,
) -> None:
    """The self-check follows ``supersedes_queue_id``: reviewing a report in
    the queue this one reconciles also disqualifies the adjudicator."""
    project = await _seed(db)
    report = project.reports[0]
    db.add(
        ResearchProjectRoleAssignment(
            collection_id=project.collection_id,
            user_id=project.supervisor,
            role=ResearchProjectRole.REVIEWER,
            assigned_by_id=project.owner,
        )
    )
    await db.flush()
    old = await _queue(db, project)
    old_assignment = await _assign(db, project, old.id, project.supervisor)
    both = _context(
        project, ResearchProjectRole.REVIEWER, ResearchProjectRole.ADJUDICATOR
    )
    await screening_service.submit(
        db,
        both,
        old.id,
        project.supervisor,
        ScreeningObservationCreate(
            report_id=report,
            assignment_id=old_assignment.id,
            criteria_hash=old.criteria_hash,
            decision="include",
            idempotency_key="old",
        ),
    )
    amended = await _version(
        db, project.protocol_id, 2, {**SNAPSHOT, "eligibility": {"population": "all"}}
    )
    await _approve(db, project.protocol_id, amended)
    new = await _queue(
        db, project, "q2", protocol_version_id=amended, supersedes_queue_id=old.id
    )
    dual = _Dual(
        project=project,
        queue=new,
        a=await _assign(db, project, new.id, project.reviewer),
        a2=await _assign(db, project, new.id, project.reviewer2),
    )
    await _vote(db, dual, "r", report, "n1")
    await _vote(db, dual, "r2", report, "n2", "exclude")
    [conflict] = await _conflicts(db, dual)
    await _raises(
        screening_service.adjudicate(
            db,
            both,
            new.id,
            report,
            project.supervisor,
            _adjudicate_body(conflict, "x"),
        ),
        403,
        "Adjudicator reviewed this report",
    )
    done = await screening_service.adjudicate(
        db,
        _adjudicator(project),
        new.id,
        report,
        project.owner,
        _adjudicate_body(conflict, "y"),
    )
    assert done.basis == "adjudicated"

"""Real PostgreSQL proof for GOO-301 screening queue guards.

The four screening tables are created by revision ``e1f3a5c7d9b2`` itself (on
top of the GOO-299/GOO-300 revisions), not by ``Base.metadata``, so the
migration's partial unique indexes are what these tests exercise.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-301 section):

- ``screening_service.submit`` current-observation check
  (``if data.supersedes_observation_id != current_id``) disabled ->
  ``-k concurrent_duplicate`` fails: the second, different-key submission is
  silently recorded as a supersession of the first instead of a 409. The
  test also runs ``SELECT 1`` after the 409, so a refusal that came from the
  ``uq_screening_observation_initial`` backstop (aborted transaction) fails too.
- ``screening_service.submit`` idempotency replay (``if replayed is not
  None``) disabled -> ``-k concurrent_duplicate`` fails: the same-key retry
  gets 409 "Observation exists..." instead of the replayed observation.
- The post-lock role reload in ``resolve_project`` is already mutation-verified
  by ``test_research_authorization_concurrency.py``; not repeated here.
"""

import asyncio
import importlib.util
import os
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar, cast
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models import Base
from src.models.collection import Collection, CollectionDocument
from src.models.document import Document, DocumentType
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.research_report import ResearchReportObservation
from src.models.research_run import ResearchRun
from src.models.screening import ScreeningObservation, ScreeningQueue
from src.models.user import User
from src.models.workspace import WorkspaceMember, WorkspaceRole
from src.schemas.research_engine import (
    FulltextAttemptCreate,
    FulltextRequestCreate,
    ScreeningAssignmentCreate,
    ScreeningObservationCreate,
    ScreeningQueueCreate,
    ScreeningRevokeRequest,
)
from src.services.research_engine import acquisition_service, screening_service
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from tests.integration.research_engine_postgres_support import (
    _PROTOCOL_SNAPSHOT,
    seed_approved_protocol_binding,
)
from tests.integration.test_report_identity_postgres import (
    _observe,
    _seed,
    _sources,
    _wait_until_blocked,
)

pytestmark = pytest.mark.integration

VERSIONS = Path(__file__).parents[2] / "alembic" / "versions"
_REBUILT_TABLES = (
    # GOO-304: extraction tables reference documents, users and protocol
    # versions only; rebuilt by their own migration below.
    "extraction_accepted_values",
    "extraction_observations",
    "extraction_form_versions",
    # GOO-303: the full-text gate reads acquisition, which references reports.
    "research_fulltext_attempts",
    "research_fulltext_requests",
    "screening_resolutions",  # GOO-302: references the screening tables
    "screening_suggestions",
    "screening_observations",
    "screening_assignments",
    "screening_queues",
    "research_import_records",
    "research_import_receipts",
    "research_report_observations",
    "research_report_identifiers",
    "research_reports",
    "research_studies",
)
SNAPSHOT = {
    **_PROTOCOL_SNAPSHOT,
    "selection": {"full_text_exclusion_reasons": ["wrong population", "wrong design"]},
    "reviewer_mode": {"mode": "dual_independent"},
}
T = TypeVar("T")


def _upgrade(connection: Connection) -> None:
    for filename in (
        "c9d2e4f6a8b1_create_report_identities.py",
        "d4e6f8a0b2c3_create_search_imports.py",
        "e1f3a5c7d9b2_create_screening_queues.py",
        "f3b5d7e9a1c4_create_screening_resolutions.py",
        "f2a4c6e8b0d3_create_fulltext_acquisition.py",
        "a3c5e7f9b1d4_version_extraction_forms.py",
    ):
        spec = importlib.util.spec_from_file_location(
            filename[:-3], VERSIONS / filename
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        setattr(module, "op", Operations(MigrationContext.configure(connection)))
        cast(Callable[[], None], module.upgrade)()


@pytest.fixture
async def screening_factory(
    request: pytest.FixtureRequest,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    configured = os.getenv("RESEARCH_DECISION_DATABASE_URL")
    url = make_url(
        configured or str(request.getfixturevalue("postgres_container")["url"])
    ).set(drivername="postgresql+asyncpg")
    schema = f"test_screening_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        for table in _REBUILT_TABLES:
            await connection.exec_driver_sql(f'DROP TABLE "{table}"')
        await connection.run_sync(_upgrade)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


class _World:
    """ids from GOO-299's seed plus R2, the protocol version and reports r1-r3."""

    def __init__(self, ids: dict[str, UUID], version_id: UUID, reports: list[UUID]):
        self.ids, self.version_id, self.reports = ids, version_id, reports
        self.collection = ids["collection"]


async def _world(factory: async_sessionmaker[AsyncSession]) -> _World:
    ids = await _seed(factory)
    ids["R2"] = uuid4()
    async with factory() as db:
        db.add(
            User(
                id=ids["R2"],
                email=f"{ids['R2']}@test.invalid",
                password_hash="unused",
                first_name="Test",
                last_name="R2",
                organization_id=ids["org"],
            )
        )
        await db.flush()
        db.add(
            WorkspaceMember(
                workspace_id=ids["workspace"],
                user_id=ids["R2"],
                role=WorkspaceRole.VIEWER,
            )
        )
        for user, role in (
            ("O", ResearchProjectRole.SUPERVISOR),
            ("R2", ResearchProjectRole.REVIEWER),
        ):
            db.add(
                ResearchProjectRoleAssignment(
                    collection_id=ids["collection"],
                    user_id=ids[user],
                    role=role,
                    assigned_by_id=ids["O"],
                )
            )
        run = await db.get(ResearchRun, ids["run"])
        assert run is not None
        binding = await seed_approved_protocol_binding(
            db,
            blueprint_id=cast(UUID, run.blueprint_id),
            collection_id=ids["collection"],
            author_id=ids["O"],
            steps=[],
            parameters={},
            snapshot=SNAPSHOT,
        )
        await db.commit()
    sources = await _sources(
        factory,
        ids["run"],
        {"doi": "10.1000/s1"},
        {"doi": "10.1000/s2"},
        {"doi": "10.1000/s3"},
    )
    await _observe(factory, ids["collection"], sources)
    async with factory() as db:
        report_of = dict(
            (
                await db.execute(
                    select(
                        ResearchReportObservation.source_id,
                        ResearchReportObservation.report_id,
                    )
                )
            )
            .tuples()
            .all()
        )
    return _World(ids, binding.protocol_version_id, [report_of[s.id] for s in sources])


async def _act(
    factory: async_sessionmaker[AsyncSession],
    world: _World,
    user: str,
    action: ResearchAction,
    call: Callable[[AsyncSession, ProjectContext], Awaitable[T]],
) -> T:
    """One route-shaped transaction: resolve, call the service, commit."""
    async with factory() as db:
        context = await resolve_project(db, world.collection, world.ids[user], action)
        result = await call(db, context)
        await db.commit()
        return result


async def _create_queue(
    factory: async_sessionmaker[AsyncSession], world: _World, key: str, **fields: Any
) -> Any:
    body = ScreeningQueueCreate(
        protocol_version_id=fields.pop("protocol_version_id", world.version_id),
        stage=fields.pop("stage", "title_abstract"),
        idempotency_key=key,
        **fields,
    )
    return await _act(
        factory,
        world,
        "O",
        ResearchAction.SUPERVISE,
        lambda db, ctx: screening_service.create_queue(db, ctx, world.ids["O"], body),
    )


async def _assign(
    factory: async_sessionmaker[AsyncSession], world: _World, queue_id: UUID, user: str
) -> Any:
    body = ScreeningAssignmentCreate(
        reviewer_user_id=world.ids[user], idempotency_key=f"assign-{uuid4()}"
    )
    return await _act(
        factory,
        world,
        "O",
        ResearchAction.SUPERVISE,
        lambda db, ctx: screening_service.assign(
            db, ctx, queue_id, world.ids["O"], body
        ),
    )


def _body(
    queue: Any, assignment: Any, report: UUID, key: str, **fields: Any
) -> ScreeningObservationCreate:
    return ScreeningObservationCreate(
        report_id=report,
        assignment_id=assignment.id,
        criteria_hash=queue.criteria_hash,
        decision=fields.pop("decision", "include"),
        idempotency_key=key,
        **fields,
    )


async def _submit(
    factory: async_sessionmaker[AsyncSession],
    world: _World,
    user: str,
    queue: Any,
    body: ScreeningObservationCreate,
) -> Any:
    return await _act(
        factory,
        world,
        user,
        ResearchAction.REVIEW,
        lambda db, ctx: screening_service.submit(
            db, ctx, queue.id, world.ids[user], body
        ),
    )


async def _retrieve(
    factory: async_sessionmaker[AsyncSession], world: _World, report: UUID
) -> None:
    """GOO-303 gate: attach a hashed document and record it as retrieved."""
    async with factory() as db:
        document = Document(
            title="full text",
            filename="full.pdf",
            file_path="local:///full.pdf",
            file_size_bytes=1,
            mime_type="application/pdf",
            document_type=DocumentType.PDF,
            organization_id=world.ids["org"],
            checksum_sha256="e" * 64,
        )
        db.add(document)
        await db.flush()
        db.add(
            CollectionDocument(collection_id=world.collection, document_id=document.id)
        )
        await db.commit()
    state, _ = await _act(
        factory,
        world,
        "O",
        ResearchAction.EDIT,
        lambda db, ctx: acquisition_service.request_fulltext(
            db,
            ctx,
            world.ids["O"],
            FulltextRequestCreate(report_id=report, idempotency_key=f"rq-{report}"),
        ),
    )
    await _act(
        factory,
        world,
        "O",
        ResearchAction.EDIT,
        lambda db, ctx: acquisition_service.record_attempt(
            db,
            ctx,
            state.request_id,
            world.ids["O"],
            FulltextAttemptCreate(
                outcome="retrieved",
                attempted_on=date.today(),
                document_id=document.id,
                idempotency_key=f"rt-{report}",
            ),
        ),
    )


async def _count(db: AsyncSession, query: Any) -> int:
    return int((await db.execute(query)).scalar_one())


def _observations(queue_id: UUID) -> Any:
    return (
        select(func.count())
        .select_from(ScreeningObservation)
        .where(ScreeningObservation.queue_id == queue_id)
    )


def _events(event_type: str) -> Any:
    return (
        select(func.count())
        .select_from(ResearchDecisionEvent)
        .where(ResearchDecisionEvent.event_type == event_type)
    )


async def _status(awaitable: Awaitable[Any]) -> tuple[int, str]:
    with pytest.raises(HTTPException) as error:
        await awaitable
    return error.value.status_code, cast(str, error.value.detail)


@pytest.mark.asyncio
async def test_commit_reopen_and_event_state_atomicity(
    screening_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = screening_factory
    world = await _world(factory)
    r1, r2, _r3 = world.reports
    queue = await _create_queue(factory, world, "q1")
    assert queue.report_count == 3
    mine_r = await _assign(factory, world, queue.id, "R")
    mine_r2 = await _assign(factory, world, queue.id, "R2")
    first = await _submit(factory, world, "R", queue, _body(queue, mine_r, r1, "r-1"))
    # GOO-302: R supersedes before R2's observation reveals (and resolves) r1.
    changed = await _submit(
        factory,
        world,
        "R",
        queue,
        _body(
            queue,
            mine_r,
            r1,
            "r-2",
            decision="uncertain",
            note="needs full text",
            supersedes_observation_id=first.id,
        ),
    )
    await _submit(
        factory,
        world,
        "R2",
        queue,
        _body(queue, mine_r2, r1, "r2-1", decision="exclude"),
    )

    async with factory() as db:  # a fresh session reloads committed state
        rows = (
            (
                await db.execute(
                    select(ScreeningObservation).where(
                        ScreeningObservation.queue_id == queue.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 3
        assert [r.id for r in rows if r.supersedes_observation_id] == [changed.id]
        original = next(r for r in rows if r.id == first.id)
        assert (original.decision, original.note) == ("include", None)
        # GOO-302: any VIEW member reads history, redacted per viewer; r1 is
        # revealed (both dual observations are in), R's superseded row is not.
        context = await resolve_project(
            db, world.collection, world.ids["V"], ResearchAction.VIEW
        )
        history = await screening_service.history(db, context, queue.id, world.ids["V"])
    assert [e.event_type for e in history] == [
        "screening.queue_created",
        "screening.assigned",
        "screening.assigned",
        "screening.observed",
        "screening.superseded",
        "screening.observed",
    ]
    assert [e.seq for e in history] == list(range(1, 7))
    assert history[0].payload["reviewer_mode"] == "dual_independent"
    assert [e.redacted for e in history[3:]] == [True, False, False]
    assert "decision" not in history[3].payload

    # Atomicity: the ledger append fails after the observation flush.
    async def exploding_append(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("ledger down")

    monkeypatch.setattr(screening_service, "append_decision", exploding_append)
    with pytest.raises(RuntimeError, match="ledger down"):
        await _submit(factory, world, "R", queue, _body(queue, mine_r, r2, "r-3"))
    monkeypatch.undo()
    async with factory() as db:
        assert await _count(db, _observations(queue.id)) == 3
        next_seq = (
            await db.execute(
                select(ResearchDecisionStream.next_seq).where(
                    ResearchDecisionStream.aggregate_id == queue.id
                )
            )
        ).scalar_one()
        assert next_seq == 7

    # Full text: the reason must come from the pinned protocol's list.
    full = await _create_queue(factory, world, "q2", stage="full_text", report_ids=[r2])
    mine_full = await _assign(factory, world, full.id, "R")
    assert await _status(
        _submit(
            factory,
            world,
            "R",
            full,
            _body(full, mine_full, r2, "f-1", decision="exclude"),
        )
    ) == (422, "Full-text exclusion needs a protocol exclusion reason")
    assert (
        await _status(
            _submit(
                factory,
                world,
                "R",
                full,
                _body(
                    full,
                    mine_full,
                    r2,
                    "f-2",
                    decision="exclude",
                    exclusion_reason="too old",
                ),
            )
        )
    )[0] == 422
    # GOO-303: a full-text decision needs a retrieved full text first.
    assert await _status(
        _submit(
            factory,
            world,
            "R",
            full,
            _body(
                full,
                mine_full,
                r2,
                "f-3",
                decision="exclude",
                exclusion_reason="wrong design",
            ),
        )
    ) == (409, "Full text not retrieved")
    await _retrieve(factory, world, r2)
    excluded = await _submit(
        factory,
        world,
        "R",
        full,
        _body(
            full,
            mine_full,
            r2,
            "f-3",
            decision="exclude",
            exclusion_reason="wrong design",
        ),
    )
    assert excluded.exclusion_reason == "wrong design"


async def _race(
    factory: async_sessionmaker[AsyncSession],
    world: _World,
    queue: Any,
    first_body: ScreeningObservationCreate,
    second_body: ScreeningObservationCreate,
) -> tuple[Any, Any, AsyncSession]:
    """Session one holds the Collection lock; session two is seen waiting on it.

    Returns (first result, second result or HTTPException, second session);
    the caller inspects and closes the second session.
    """
    reviewer = world.ids["R"]
    one, observer = factory(), factory()
    two = factory()
    async with one, observer:
        pid = cast(
            int, (await two.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        context = await resolve_project(
            one, world.collection, reviewer, ResearchAction.REVIEW
        )

        async def second() -> Any:
            ctx = await resolve_project(
                two, world.collection, reviewer, ResearchAction.REVIEW
            )
            try:
                return await screening_service.submit(
                    two, ctx, queue.id, reviewer, second_body
                )
            except HTTPException as error:
                return error

        attempt = asyncio.create_task(second())
        try:
            await _wait_until_blocked(observer, pid)
            first = await screening_service.submit(
                one, context, queue.id, reviewer, first_body
            )
            await one.commit()
            result = await attempt
        finally:
            if not attempt.done():
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
    return first, result, two


@pytest.mark.asyncio
async def test_concurrent_duplicate_submission_yields_one_observation(
    screening_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = screening_factory
    world = await _world(factory)
    r1, r2, _r3 = world.reports
    queue = await _create_queue(factory, world, "q1")
    mine = await _assign(factory, world, queue.id, "R")

    # Same body and key: the waiting retry replays the committed observation.
    same = _body(queue, mine, r1, "dup")
    first, second, two = await _race(factory, world, queue, same, same)
    async with two:
        assert not isinstance(second, HTTPException), second.detail
        assert second.id == first.id
        await two.commit()

    # Different keys: the service refuses the second initial observation
    # cleanly, before the unique index is ever reached.
    first, refused, two = await _race(
        factory,
        world,
        queue,
        _body(queue, mine, r2, "k-a"),
        _body(queue, mine, r2, "k-b", decision="exclude"),
    )
    async with two:
        assert isinstance(refused, HTTPException)
        assert (refused.status_code, refused.detail) == (
            409,
            "Observation exists; supersede the current observation",
        )
        # Still usable: an index hit would have aborted this transaction.
        assert (await two.execute(text("SELECT 1"))).scalar_one() == 1
        await two.rollback()

    async with factory() as db:
        assert await _count(db, _observations(queue.id)) == 2
        assert await _count(db, _events("screening.observed")) == 2
        with pytest.raises(IntegrityError, match="uq_screening_observation_initial"):
            async with db.begin_nested():
                db.add(
                    ScreeningObservation(
                        queue_id=queue.id,
                        report_id=r1,
                        reviewer_id=world.ids["R"],
                        assignment_id=mine.id,
                        decision="exclude",
                    )
                )
                await db.flush()
        # The service backstop maps asyncpg's SQLSTATE 23505 to a stable 409.
        with pytest.raises(HTTPException) as backstop:
            async with db.begin_nested():
                db.add(
                    ScreeningObservation(
                        queue_id=queue.id,
                        report_id=r1,
                        reviewer_id=world.ids["R"],
                        assignment_id=mine.id,
                        decision="exclude",
                    )
                )
                await screening_service._flush_unique(db, "taken")
        assert (backstop.value.status_code, backstop.value.detail) == (409, "taken")


@pytest.mark.asyncio
async def test_role_revocation_race_denies_submission(
    screening_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = screening_factory
    world = await _world(factory)
    r1 = world.reports[0]
    queue = await _create_queue(factory, world, "q1")
    mine = await _assign(factory, world, queue.id, "R")
    peer = await _assign(factory, world, queue.id, "R2")

    async with factory() as revoker, factory() as reviewer, factory() as observer:
        await resolve_project(
            revoker, world.collection, world.ids["O"], ResearchAction.MANAGE
        )
        role = (
            await revoker.execute(
                select(ResearchProjectRoleAssignment).where(
                    ResearchProjectRoleAssignment.collection_id == world.collection,
                    ResearchProjectRoleAssignment.user_id == world.ids["R"],
                    ResearchProjectRoleAssignment.role == ResearchProjectRole.REVIEWER,
                )
            )
        ).scalar_one()
        role.soft_delete()
        await revoker.flush()
        pid = cast(
            int, (await reviewer.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )

        async def attempt_submit() -> Any:
            ctx = await resolve_project(
                reviewer, world.collection, world.ids["R"], ResearchAction.REVIEW
            )
            return await screening_service.submit(
                reviewer, ctx, queue.id, world.ids["R"], _body(queue, mine, r1, "k")
            )

        attempt = asyncio.create_task(attempt_submit())
        try:
            await _wait_until_blocked(observer, pid)
            await revoker.commit()
            assert await _status(attempt) == (403, "reviewer role required")
        finally:
            if not attempt.done():
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
            await reviewer.rollback()
    async with factory() as db:
        assert await _count(db, _observations(queue.id)) == 0
        assert await _count(db, _events("screening.observed")) == 0

    # A revoked queue assignment is a stale revision, not a missing one.
    await _act(
        factory,
        world,
        "O",
        ResearchAction.SUPERVISE,
        lambda db, ctx: screening_service.revoke(
            db,
            ctx,
            queue.id,
            peer.id,
            world.ids["O"],
            ScreeningRevokeRequest(reason="left", idempotency_key="revoke"),
        ),
    )
    assert await _status(
        _submit(factory, world, "R2", queue, _body(queue, peer, r1, "k2"))
    ) == (409, "Assignment revoked")


@pytest.mark.asyncio
async def test_archived_and_foreign_project(
    screening_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = screening_factory
    world = await _world(factory)
    r1 = world.reports[0]
    queue = await _create_queue(factory, world, "q1")
    mine = await _assign(factory, world, queue.id, "R")

    # Foreign-org user and a queue of another Collection both look missing.
    async with factory() as db:
        assert await _status(
            resolve_project(db, world.collection, world.ids["F"], ResearchAction.VIEW)
        ) == (404, "Project not found")
    other = uuid4()
    async with factory() as db:
        db.add(Collection(id=other, workspace_id=world.ids["workspace"], name="other"))
        await db.flush()
        db.add(
            ScreeningQueue(
                id=(foreign_queue := uuid4()),
                collection_id=other,
                protocol_version_id=world.version_id,
                criteria_hash=queue.criteria_hash,
                stage="title_abstract",
                report_ids=[str(r1)],
                created_by_id=world.ids["O"],
            )
        )
        await db.commit()
    assert await _status(
        _act(
            factory,
            world,
            "R",
            ResearchAction.REVIEW,
            lambda db, ctx: screening_service.submit(
                db, ctx, foreign_queue, world.ids["R"], _body(queue, mine, r1, "x")
            ),
        )
    ) == (404, "Screening queue not found")

    # An approved amendment makes the queue stale until it is reconciled.
    amended = uuid4()
    async with factory() as db:
        await db.execute(
            text("""INSERT INTO research_protocol_versions (
                       id, protocol_id, version, parent_version_id,
                       question_version_id, blueprint_id, execution_plan, snapshot,
                       content_hash, status, change_kind, amendment_reason,
                       author_user_id, approved_by_user_id, approved_at, created_at)
                   SELECT :new, protocol_id, version + 1, id, question_version_id,
                       blueprint_id, execution_plan, snapshot, content_hash,
                       'approved', 'amendment', 'fixture amendment',
                       author_user_id, author_user_id, now(), now()
                   FROM research_protocol_versions WHERE id = :old"""),
            {"new": amended, "old": world.version_id},
        )
        await db.execute(
            text("""UPDATE research_protocols SET current_approved_version_id = :new
                   WHERE current_approved_version_id = :old"""),
            {"new": amended, "old": world.version_id},
        )
        await db.commit()
    body = _body(queue, mine, r1, "k1")
    assert await _status(_submit(factory, world, "R", queue, body)) == (
        409,
        "Protocol version changed; reconcile queue",
    )
    reconciled = await _create_queue(
        factory, world, "q2", protocol_version_id=amended, supersedes_queue_id=queue.id
    )
    fresh = await _assign(factory, world, reconciled.id, "R")
    await _submit(factory, world, "R", reconciled, _body(reconciled, fresh, r1, "k2"))
    assert (await _status(_submit(factory, world, "R", queue, body)))[0] == 409

    # Archiving commits while R waits on the Collection lock: R is refused.
    async with factory() as archiver, factory() as db, factory() as observer:
        await archiver.execute(
            text("SELECT id FROM collections WHERE id = :id FOR UPDATE"),
            {"id": world.collection},
        )
        await archiver.execute(
            text("UPDATE collections SET research_status = 'archived' WHERE id = :id"),
            {"id": world.collection},
        )
        pid = cast(
            int, (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )

        async def attempt_submit() -> Any:
            ctx = await resolve_project(
                db, world.collection, world.ids["R"], ResearchAction.REVIEW
            )
            return await screening_service.submit(
                db,
                ctx,
                reconciled.id,
                world.ids["R"],
                _body(reconciled, fresh, world.reports[1], "k3"),
            )

        attempt = asyncio.create_task(attempt_submit())
        try:
            await _wait_until_blocked(observer, pid)
            await archiver.commit()
            # lock_active_project re-evaluates the locked row after the wait.
            assert await _status(attempt) == (409, "Project is not writable")
        finally:
            if not attempt.done():
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
            await db.rollback()
    async with factory() as db:
        assert await _count(db, _observations(reconciled.id)) == 1

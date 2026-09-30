"""Real PostgreSQL proof for retained research decision ordering and replay."""

import asyncio
import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import AsyncIterator, Callable, cast
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from src.models import Base
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    DecisionReplayError,
    append_decision,
    decision_request_fingerprint,
    replay_decisions,
)

VERSIONS = Path(__file__).parents[2] / "alembic" / "versions"


def _load_migration() -> ModuleType:
    path = VERSIONS / "a3c5e7f901b2_create_research_decision_ledger.py"
    spec = importlib.util.spec_from_file_location("research_decision_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade(connection: Connection) -> None:
    module = _load_migration()
    setattr(module, "op", Operations(MigrationContext.configure(connection)))
    cast(Callable[[], None], module.upgrade)()


@pytest.fixture
async def decision_engine(
    request: pytest.FixtureRequest,
) -> AsyncIterator[AsyncEngine]:
    configured = os.getenv("RESEARCH_DECISION_DATABASE_URL")
    url = make_url(
        configured or str(request.getfixturevalue("postgres_container")["url"])
    ).set(drivername="postgresql+asyncpg")
    schema = f"test_research_decisions_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql("DROP TABLE research_decision_events")
        await connection.exec_driver_sql("DROP TABLE research_decision_streams")
        await connection.run_sync(_upgrade)
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


async def _seed(engine: AsyncEngine) -> dict[str, UUID]:
    ids = {name: uuid4() for name in ("org", "user", "workspace", "collection")}
    async with engine.begin() as connection:
        await connection.execute(
            text("""INSERT INTO organizations
                (id,name,storage_tier,storage_used_bytes,storage_limit_bytes,
                 is_active,created_at,updated_at,is_deleted)
                VALUES (:org,'decisions','FREE',0,1,true,now(),now(),false)"""),
            ids,
        )
        await connection.execute(
            text("""INSERT INTO users
                (id,email,password_hash,first_name,last_name,role,is_active,
                 login_count,organization_id,created_at,updated_at,is_deleted)
                VALUES (:user,:email,'x','x','x','USER',true,0,:org,
                        now(),now(),false)"""),
            {**ids, "email": f"decision-{uuid4()}@test.invalid"},
        )
        await connection.execute(
            text("""INSERT INTO workspaces
                (id,name,is_archived,is_public,owner_id,organization_id,
                 created_at,updated_at,is_deleted)
                VALUES (:workspace,'decisions',false,false,:user,:org,
                        now(),now(),false)"""),
            ids,
        )
        await connection.execute(
            text("""INSERT INTO collections
                (id,workspace_id,name,project_type,research_status,tags,is_private,
                 created_at,updated_at,is_deleted)
                VALUES (:collection,:workspace,'decisions','research','active',
                        '[]',true,now(),now(),false)"""),
            ids,
        )
    return ids


def _approved_kwargs(ids: dict[str, UUID], protocol_id: UUID, version_id: UUID) -> dict:
    request = {
        "protocol_id": str(protocol_id),
        "question_version_id": str(uuid4()),
        "blueprint_id": str(uuid4()),
        "previous_approved_version_id": None,
        "expected_protocol_version": 1,
        "canonicalization_version": "research-protocol-v1",
    }
    return {
        "collection_id": ids["collection"],
        "aggregate_type": "research_protocol",
        "aggregate_id": protocol_id,
        "event_type": "protocol.approved",
        "event_schema_version": 1,
        "actor_user_id": ids["user"],
        "actor_role": "supervisor",
        "subject_type": "research_protocol_version",
        "subject_id": version_id,
        "subject_version_id": version_id,
        "subject_hash": "a" * 64,
        "reason": "approved exact protocol",
        "payload": request,
        "idempotency_key": "approve-request:approved",
        "request_fingerprint": decision_request_fingerprint(request),
    }


@pytest.mark.asyncio
async def test_append_idempotency_replay_rollback_and_retention(
    decision_engine: AsyncEngine,
) -> None:
    ids = await _seed(decision_engine)
    protocol_id, version_id = uuid4(), uuid4()
    kwargs = _approved_kwargs(ids, protocol_id, version_id)

    async with decision_engine.connect() as connection:
        protected = (
            await connection.execute(
                text("""SELECT relname, relrowsecurity FROM pg_class
                    WHERE relname IN (
                        'research_decision_streams','research_decision_events'
                    )""")
            )
        ).all()
        assert dict(protected) == {
            "research_decision_events": True,
            "research_decision_streams": True,
        }
        inherited_soft_delete = await connection.scalar(
            text("""SELECT count(*) FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name IN (
                      'research_decision_streams','research_decision_events'
                  )
                  AND column_name IN ('is_deleted','deleted_at')""")
        )
        assert inherited_soft_delete == 0

    async with AsyncSession(decision_engine, expire_on_commit=False) as db:
        first = await append_decision(db, **kwargs)
        retried = await append_decision(db, **kwargs)
        assert not first.replayed
        assert retried.replayed
        assert retried.event.id == first.event.id
        await db.commit()

    conflicting = dict(kwargs)
    conflicting["request_fingerprint"] = "b" * 64
    async with AsyncSession(decision_engine) as db:
        with pytest.raises(DecisionIdempotencyConflict):
            await append_decision(db, **conflicting)
        await db.rollback()

    rollback_kwargs = _approved_kwargs(ids, protocol_id, uuid4())
    rollback_kwargs["idempotency_key"] = "rolled-back:approved"
    async with AsyncSession(decision_engine) as db:
        await append_decision(db, **rollback_kwargs)
        await db.rollback()
    async with AsyncSession(decision_engine) as db:
        history = await replay_decisions(
            db,
            collection_id=ids["collection"],
            aggregate_type="research_protocol",
            aggregate_id=protocol_id,
            subject_version_hash=lambda _version_id: asyncio.sleep(0, result="a" * 64),
        )
        assert [(event.seq, event.id) for event in history] == [(1, first.event.id)]
        with pytest.raises(DecisionReplayError, match="subject version is missing"):
            await replay_decisions(
                db,
                collection_id=ids["collection"],
                aggregate_type="research_protocol",
                aggregate_id=protocol_id,
                subject_version_hash=lambda _version_id: asyncio.sleep(0, result=None),
            )
        with pytest.raises(DecisionReplayError, match="subject hash no longer matches"):
            await replay_decisions(
                db,
                collection_id=ids["collection"],
                aggregate_type="research_protocol",
                aggregate_id=protocol_id,
                subject_version_hash=lambda _version_id: asyncio.sleep(
                    0, result="f" * 64
                ),
            )

    async with decision_engine.begin() as connection:
        with pytest.raises(DBAPIError):
            await connection.execute(
                text("UPDATE research_decision_events SET reason='changed'")
            )
        await connection.rollback()
    async with decision_engine.begin() as connection:
        with pytest.raises(DBAPIError):
            await connection.execute(text("DELETE FROM research_decision_events"))
        await connection.rollback()
    async with decision_engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                text("DELETE FROM collections WHERE id=:collection"), ids
            )
        await connection.rollback()
    async with decision_engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(text("DELETE FROM users WHERE id=:user"), ids)
        await connection.rollback()


@pytest.mark.asyncio
async def test_concurrent_appends_allocate_contiguous_stream_sequence(
    decision_engine: AsyncEngine,
) -> None:
    # Mutation proof (2026-09-28): removing `.with_for_update()` from
    # ledger._locked_stream made this test fail with a duplicate `(stream_id,
    # seq)` IntegrityError. Command: RESEARCH_DECISION_DATABASE_URL=<postgres>
    # PYTHONPATH=backend python -m pytest -q
    # backend/tests/integration/test_research_decision_ledger.py::
    # test_concurrent_appends_allocate_contiguous_stream_sequence
    # -c backend/pytest.ini --no-cov
    ids = await _seed(decision_engine)
    protocol_id = uuid4()
    async with AsyncSession(decision_engine) as db:
        db.add(
            ResearchDecisionStream(
                id=uuid4(),
                collection_id=ids["collection"],
                aggregate_type="research_protocol",
                aggregate_id=protocol_id,
                next_seq=1,
            )
        )
        await db.commit()

    async def append_once(index: int) -> int:
        kwargs = _approved_kwargs(ids, protocol_id, uuid4())
        kwargs["idempotency_key"] = f"concurrent-{index}:approved"
        async with AsyncSession(decision_engine) as db:
            result = await append_decision(db, **kwargs)
            sequence = cast(int, result.event.seq)
            await db.commit()
            return sequence

    sequences = await asyncio.gather(append_once(1), append_once(2))
    assert sorted(sequences) == [1, 2]
    async with AsyncSession(decision_engine) as db:
        assert (
            await db.scalar(select(func.count()).select_from(ResearchDecisionEvent))
            == 2
        )


@pytest.mark.asyncio
async def test_replay_rejects_supersession_target_hash_disagreement(
    decision_engine: AsyncEngine,
) -> None:
    # Mutation proof (2026-09-28): comparing only the pending old/new IDs and
    # omitting the target hash made this test fail because replay accepted the
    # contradictory retained pair. The production hash comparison was restored.
    ids = await _seed(decision_engine)
    protocol_id = uuid4()
    previous_id, replacement_id = uuid4(), uuid4()
    initial = _approved_kwargs(ids, protocol_id, previous_id)
    superseded_payload = {
        "protocol_id": str(protocol_id),
        "superseded_by_version_id": str(replacement_id),
        "superseded_by_hash": "b" * 64,
    }
    replacement = _approved_kwargs(ids, protocol_id, replacement_id)
    replacement.update(
        subject_hash="c" * 64,
        idempotency_key="replacement:approved",
        payload={
            **replacement["payload"],
            "previous_approved_version_id": str(previous_id),
            "expected_protocol_version": 2,
        },
    )
    replacement["request_fingerprint"] = decision_request_fingerprint(
        replacement["payload"]
    )
    async with AsyncSession(decision_engine) as db:
        await append_decision(db, **initial)
        await append_decision(
            db,
            collection_id=ids["collection"],
            aggregate_type="research_protocol",
            aggregate_id=protocol_id,
            event_type="protocol.superseded",
            event_schema_version=1,
            actor_user_id=ids["user"],
            actor_role="supervisor",
            subject_type="research_protocol_version",
            subject_id=previous_id,
            subject_version_id=previous_id,
            subject_hash="a" * 64,
            reason="replace approved protocol",
            payload=superseded_payload,
            idempotency_key="replacement:superseded",
            request_fingerprint=decision_request_fingerprint(superseded_payload),
        )
        await append_decision(db, **replacement)
        await db.commit()

    async with AsyncSession(decision_engine) as db:
        with pytest.raises(DecisionReplayError, match="missing its exact supersession"):
            await replay_decisions(
                db,
                collection_id=ids["collection"],
                aggregate_type="research_protocol",
                aggregate_id=protocol_id,
                subject_version_hash=lambda subject_id: asyncio.sleep(
                    0, result="a" * 64 if subject_id == previous_id else "c" * 64
                ),
            )


@pytest.mark.asyncio
async def test_decisions_survive_agent_run_cleanup(
    decision_engine: AsyncEngine,
) -> None:
    ids = await _seed(decision_engine)
    kwargs = _approved_kwargs(ids, uuid4(), uuid4())
    async with AsyncSession(decision_engine, expire_on_commit=False) as db:
        appended = await append_decision(db, **kwargs)
        await db.commit()

    job_id = str(uuid4())
    async with decision_engine.begin() as connection:
        await connection.execute(
            text("""INSERT INTO agent_runs
                    (job_id, user_id, project_id, status, created_at, updated_at)
                    VALUES (:job_id, :user, :collection, 'completed', now(), now())"""),
            {"job_id": job_id, **ids},
        )
    # disposable agent-run cleanup is a hard delete
    async with decision_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM agent_runs WHERE job_id=:job_id"), {"job_id": job_id}
        )
    async with decision_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM research_decision_events WHERE id=:id"),
                {"id": appended.event.id},
            )
            == 1
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM agent_runs WHERE job_id=:job_id"),
                {"job_id": job_id},
            )
            == 0
        )

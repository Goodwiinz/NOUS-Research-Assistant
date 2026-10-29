"""``delegated``: a parent run paused while a child Codex run works."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import event, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AGENT_RUN_STATUS_CHECK, AgentRun
from src.models.thread import Thread
from src.services.agent import agent_run_service, agent_submission_service
from src.shared.enums import JobStatus

pytestmark = pytest.mark.unit

THREAD = uuid.UUID("55555555-5555-5555-5555-555555555555")


def test_delegated_is_active_and_constrained() -> None:
    assert JobStatus("delegated") is JobStatus.DELEGATED
    assert not JobStatus.DELEGATED.is_terminal
    assert "'delegated'" in AGENT_RUN_STATUS_CHECK
    assert "delegated" in agent_submission_service._ACTIVE_RUN_STATUSES
    assert "delegated" in agent_run_service._ACTIVE_RUN_STATUSES


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _no_driver_tx(connection, _record):  # type: ignore[no-untyped-def]
        connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(connection):  # type: ignore[no-untyped-def]
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Thread.__table__.create)
        await conn.run_sync(AgentRun.__table__.create)
        await conn.run_sync(AgentOutbox.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _run(status: str, *, parent: str | None = None, provider: str = "nous") -> dict:
    return {
        "job_id": str(uuid.uuid4()),
        "thread_id": THREAD,
        "status": status,
        "execution_provider": provider,
        "parent_run_id": parent,
    }


async def test_child_may_run_while_parent_delegated(db: AsyncSession) -> None:
    parent = _run("delegated")
    await db.execute(insert(AgentRun).values(**parent))
    await db.execute(
        insert(AgentRun).values(
            **_run("running", parent=parent["job_id"], provider="codex")
        )
    )
    await db.commit()


async def test_second_top_level_active_run_is_refused(db: AsyncSession) -> None:
    await db.execute(insert(AgentRun).values(**_run("delegated")))
    await db.commit()
    with pytest.raises(IntegrityError):
        await db.execute(insert(AgentRun).values(**_run("queued")))
    await db.rollback()


async def test_outbox_allows_one_row_per_run_and_kind(db: AsyncSession) -> None:
    run = _run("completed", provider="codex")
    await db.execute(insert(AgentRun).values(**run))
    db.add(AgentOutbox(run_id=run["job_id"], kind="harness.execute", payload={}))
    db.add(AgentOutbox(run_id=run["job_id"], kind="handoff.resume", payload={}))
    await db.commit()
    db.add(AgentOutbox(run_id=run["job_id"], kind="handoff.resume", payload={}))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

"""Real-Postgres proof for atomic agent-run status transitions."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Update

from src.models.agent_run import AgentRun
from src.services.agent import agent_run_service as svc
from src.shared.enums import JobStatus

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


class _PausedUpdateSession:
    def __init__(self, db: Any, reached: asyncio.Event, release: asyncio.Event) -> None:
        self._db = db
        self._reached = reached
        self._release = release

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(statement, Update):
            self._reached.set()
            await self._release.wait()
        return await self._db.execute(statement, *args, **kwargs)


def _async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be a PostgreSQL URL")


async def test_postgres_conditional_transition_preserves_terminal_winner() -> None:
    """Postgres serializes the terminal winner against a delayed stale writer."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    schema = "agent_run_" + uuid.uuid4().hex
    admin_engine = create_async_engine(_async_dsn(dsn))
    try:
        async with admin_engine.begin() as conn:
            await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

        scoped_engine = create_async_engine(
            _async_dsn(dsn),
            connect_args={"server_settings": {"search_path": schema}},
        )
        try:
            async with scoped_engine.begin() as conn:
                for table in (
                    "threads",
                    "conversations",
                    "collections",
                    "chat_messages",
                    "agent_runtime_snapshots",
                ):
                    await conn.exec_driver_sql(
                        f'CREATE TABLE "{table}" (id UUID PRIMARY KEY)'
                    )
                await conn.run_sync(AgentRun.__table__.create)

            factory = async_sessionmaker(scoped_engine, expire_on_commit=False)
            job_id = str(uuid.uuid4())
            thread_id = uuid.uuid4()
            user_id = uuid.uuid4()
            async with factory() as setup:
                await svc.upsert_run(
                    setup,
                    job_id=job_id,
                    status=JobStatus.RUNNING,
                    user_id=user_id,
                    thread_id=str(thread_id),
                )

            reached, release = asyncio.Event(), asyncio.Event()
            async with factory() as delayed_db, factory() as finisher_db:
                stale = asyncio.create_task(
                    svc.upsert_run(
                        cast(
                            AsyncSession,
                            _PausedUpdateSession(delayed_db, reached, release),
                        ),
                        job_id=job_id,
                        status=JobStatus.AWAITING_CONFIRMATION,
                    )
                )
                await asyncio.wait_for(reached.wait(), timeout=3)
                await svc.upsert_run(
                    finisher_db, job_id=job_id, status=JobStatus.COMPLETED
                )
                release.set()
                await stale

            async with factory() as verify:
                run = await verify.get(AgentRun, job_id)
                assert run is not None
                assert run.status == JobStatus.COMPLETED.value
                assert run.user_id == user_id
                assert run.cancel_requested_at is None
                active = (
                    (
                        await verify.execute(
                            select(AgentRun).where(
                                AgentRun.thread_id == thread_id,
                                AgentRun.status.in_(svc._ACTIVE_RUN_STATUSES),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert active == []
        finally:
            await scoped_engine.dispose()
    finally:
        async with admin_engine.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin_engine.dispose()

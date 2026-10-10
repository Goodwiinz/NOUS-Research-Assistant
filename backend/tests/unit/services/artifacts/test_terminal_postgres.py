"""Real PostgreSQL proof of terminal absorption for artifact announcements.

Set ORCHESTRATION_TEST_DATABASE_URL to a disposable PostgreSQL database.
Removing append_event's run-row lock must fail concurrent-terminal with a
reopened ledger; serial-control remains passing. Scheduling alone is injected.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

os.environ.setdefault("ENVIRONMENT", "testing")
os.environ.setdefault("DEBUG", "false")

import pytest
from sqlalchemy import Column, MetaData, Table, event, insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.artifact import ArtifactLifecycleOutbox
from src.services.agent import run_event_store as store
from src.services.agent.run_event_types import RunEventType
from src.services.artifacts.lifecycle import drain_artifact_outbox

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "interleave", [False, True], ids=["serial-control", "concurrent-terminal"]
)
async def test_artifact_cannot_append_after_terminal(
    monkeypatch: pytest.MonkeyPatch, interleave: bool
) -> None:
    url = os.environ.get("ORCHESTRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Set ORCHESTRATION_TEST_DATABASE_URL to an isolated PostgreSQL database"
        )
    assert url is not None
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "agent_architecture_audit_" + uuid4().hex
    engine = create_async_engine(
        url,
        isolation_level="READ COMMITTED",
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    models: list[Any] = [AgentRun, AgentRunEvent, ArtifactLifecycleOutbox]
    actual_names = {model.__tablename__ for model in models}
    parents = MetaData()
    for model in models:
        for fk in model.__table__.foreign_keys:
            target = fk.column
            name = target.table.name
            if name not in actual_names and name not in parents.tables:
                Table(name, parents, Column(target.name, target.type, primary_key=True))
    run_id = str(uuid4())
    org_id, artifact_id, version_id, outbox_id = (uuid4() for _ in range(4))
    attempted, release = asyncio.Event(), asyncio.Event()
    producer = None
    try:
        async with engine.begin() as conn:
            # Identifier is exclusively generated above, never caller input.
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(parents.create_all)
            for model in models:
                await conn.run_sync(model.__table__.create)
            for name, value in (
                ("organizations", org_id),
                ("artifacts", artifact_id),
                ("artifact_versions", version_id),
            ):
                await conn.execute(insert(parents.tables[name]).values(id=value))
        async with factory() as setup:
            setup.add(AgentRun(job_id=run_id, status="running", organization_id=org_id))
            await setup.flush()
            await store.append_event(
                setup,
                run_id=run_id,
                event_type=RunEventType.RUN_CREATED,
                organization_id=org_id,
            )
            setup.add(
                ArtifactLifecycleOutbox(
                    id=outbox_id,
                    organization_id=org_id,
                    artifact_id=artifact_id,
                    version_id=version_id,
                    kind="version_created",
                    run_id=run_id,
                )
            )
            await setup.commit()

        async def finish_run(finalizer: Any) -> None:
            run = await finalizer.scalar(
                select(AgentRun).where(AgentRun.job_id == run_id).with_for_update()
            )
            run.status = "completed"
            await store.append_event(
                finalizer,
                run_id=run_id,
                event_type=RunEventType.RUN_COMPLETED,
                organization_id=org_id,
            )

        async with factory() as producer_session:
            if interleave:
                original_check = store.has_terminal_event

                async def pause_after_check(db: Any, checked_run: str) -> bool:
                    result = await original_check(db, checked_run)
                    if db is producer_session and not result:
                        attempted.set()
                        await asyncio.wait_for(release.wait(), timeout=10)
                    return result

                @event.listens_for(engine.sync_engine, "before_cursor_execute")
                def observe_lock(
                    connection: Any,
                    cursor: Any,
                    statement: str,
                    parameters: Any,
                    context: Any,
                    executemany: bool,
                ) -> None:
                    if "FOR UPDATE" in statement and "agent_runs" in statement:
                        attempted.set()

                monkeypatch.setattr(store, "has_terminal_event", pause_after_check)
                async with factory() as finalizer:
                    await finish_run(finalizer)
                    attempted.clear()
                    producer = asyncio.create_task(
                        drain_artifact_outbox(producer_session)
                    )
                    await asyncio.wait_for(attempted.wait(), timeout=10)
                    await finalizer.commit()
                    release.set()
                    await asyncio.wait_for(producer, timeout=10)
            else:
                async with factory() as finalizer:
                    await finish_run(finalizer)
                    await finalizer.commit()
                assert await drain_artifact_outbox(producer_session) == 0

        async with factory() as inspect:
            rows: Any = (
                await inspect.execute(
                    select(AgentRunEvent.seq, AgentRunEvent.event_type)
                    .where(AgentRunEvent.run_id == run_id)
                    .order_by(AgentRunEvent.seq)
                )
            ).all()
            outbox = await inspect.get(ArtifactLifecycleOutbox, outbox_id)
            assert outbox is not None
            assert (
                rows[-1].event_type == "run.completed" and outbox.status == "skipped"
            ), f"Ledger reopened after completion: {rows!r}; outbox={outbox.status}"
    finally:
        release.set()
        if producer is not None and not producer.done():
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()

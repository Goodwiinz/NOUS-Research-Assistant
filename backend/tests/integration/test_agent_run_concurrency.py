"""Real-Postgres proof for atomic agent-run status transitions."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator, cast
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Update

from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.thread import Thread, ThreadStatus
from src.models.user import User, UserRole
from src.services.agent import agent_run_service as svc
from src.services.agent.agent_submission_service import request_run_cancellation
from src.services.agent.confirmation_service import pending_approval
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


def _async_dsn(dsn: str | None) -> str:
    if dsn is None:
        raise ValueError("ORCHESTRATION_TEST_DATABASE_URL is required")
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be a PostgreSQL URL")


@asynccontextmanager
async def _postgres_run_schema(
    dsn: str | None,
) -> AsyncIterator[
    tuple[
        async_sessionmaker[AsyncSession],
        uuid.UUID,
        uuid.UUID,
        uuid.UUID,
        uuid.UUID,
    ]
]:
    """Create an isolated schema with one real thread and run-event ledger."""
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
                    "conversations",
                    "collections",
                    "users",
                    "chat_messages",
                    "agent_runtime_snapshots",
                ):
                    await conn.exec_driver_sql(
                        f'CREATE TABLE "{table}" (id UUID PRIMARY KEY)'
                    )
                await conn.run_sync(Thread.__table__.create)
                await conn.run_sync(AgentRun.__table__.create)
                await conn.run_sync(AgentRunEvent.__table__.create)
                await conn.run_sync(AgentOutbox.__table__.create)

            factory = async_sessionmaker(scoped_engine, expire_on_commit=False)
            user_id = uuid.uuid4()
            organization_id = uuid.uuid4()
            conversation_id = uuid.uuid4()
            thread_id = uuid.uuid4()
            async with factory() as setup:
                await setup.execute(
                    text("INSERT INTO users (id) VALUES (:id)"),
                    {"id": user_id},
                )
                await setup.execute(
                    text("INSERT INTO conversations (id) VALUES (:id)"),
                    {"id": conversation_id},
                )
                setup.add(
                    Thread(
                        id=thread_id,
                        conversation_id=conversation_id,
                        created_by_id=user_id,
                        status=ThreadStatus.ACTIVE,
                    )
                )
                await setup.commit()
            yield factory, user_id, organization_id, conversation_id, thread_id
        finally:
            await scoped_engine.dispose()
    finally:
        async with admin_engine.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin_engine.dispose()


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


class _PauseFirstRollback:
    """Expose the point after PostgreSQL rolled back a losing writer."""

    def __init__(
        self,
        db: Any,
        rolled_back: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._db = db
        self._rolled_back = rolled_back
        self._release = release
        self._paused = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    async def rollback(self) -> None:
        await self._db.rollback()
        if not self._paused:
            self._paused = True
            self._rolled_back.set()
            await self._release.wait()


class _PostMonitorGraph:
    """Minimal graph that pauses only at the runner's post-monitor read."""

    def __init__(self, *, resume: bool, user_id: uuid.UUID) -> None:
        self.resume = resume
        self.user_id = str(user_id)
        self.post_monitor_reached = asyncio.Event()
        self.release_post_monitor = asyncio.Event()
        self.state_reads = 0
        self.messages = [
            HumanMessage(content="current question"),
            AIMessage(content="CURRENT TURN ANSWER"),
        ]
        self.pending_snapshot = SimpleNamespace(
            values={"user_id": self.user_id, "messages": self.messages},
            config={"configurable": {"checkpoint_id": "resume-checkpoint"}},
            tasks=[
                SimpleNamespace(
                    interrupts=[
                        SimpleNamespace(
                            id="post-monitor-approval",
                            value={"tools": [], "message": "Continue the action?"},
                        )
                    ]
                )
            ],
        )

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"messages": self.messages, "tool_executions": []}

    async def aget_state(self, _config: dict[str, Any]) -> Any:
        self.state_reads += 1
        if self.resume and self.state_reads == 1:
            return self.pending_snapshot
        self.post_monitor_reached.set()
        await self.release_post_monitor.wait()
        return None


class _DelayRunningRedisPipeline:
    """Pause one Redis publication after WATCH/GET and before MULTI."""

    def __init__(
        self,
        pipeline: Any,
        owner: _DelayRunningRedis,
    ) -> None:
        self.pipeline = pipeline
        self.owner = owner

    async def __aenter__(self) -> _DelayRunningRedisPipeline:
        await self.pipeline.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> Any:
        return await self.pipeline.__aexit__(*exc_info)

    async def watch(self, *keys: str) -> Any:
        return await self.pipeline.watch(*keys)

    async def get(self, key: str) -> Any:
        value = await self.pipeline.get(key)
        if key == self.owner.job_key and not self.owner.paused:
            self.owner.paused = True
            self.owner.reached.set()
            await self.owner.release.wait()
        return value

    def multi(self) -> Any:
        return self.pipeline.multi()

    def setex(self, *args: Any, **kwargs: Any) -> Any:
        return self.pipeline.setex(*args, **kwargs)

    async def execute(self) -> Any:
        return await self.pipeline.execute()

    async def reset(self) -> Any:
        return await self.pipeline.reset()


class _DelayRunningRedis:
    """Redis client proxy pausing its first WATCH/GET for the target key."""

    def __init__(self, redis_client: Any, job_key: str) -> None:
        self.redis_client = redis_client
        self.job_key = job_key
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.paused = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.redis_client, name)

    def pipeline(self, *args: Any, **kwargs: Any) -> _DelayRunningRedisPipeline:
        return _DelayRunningRedisPipeline(
            self.redis_client.pipeline(*args, **kwargs),
            self,
        )


@asynccontextmanager
async def _no_heartbeat(_job_id: str) -> AsyncIterator[None]:
    yield


async def _run_post_monitor_terminal_race(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: str,
    stop_first: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None, int, int, Any]:
    """Run one real-runner/real-Postgres publication ordering."""
    import json

    import redis.asyncio as redis_asyncio

    from src.core import database
    from src.services.agent import agent_execution_service as execution_service
    from src.services.agent import agent_run_service, job_store
    from src.services.agent.schemas import (
        AgentExecuteRequest,
        AgentMessage,
        PageContextRequest,
    )

    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    redis_url = os.getenv("ORCHESTRATION_TEST_REDIS_URL", "redis://127.0.0.1:32775/15")

    async with _postgres_run_schema(dsn) as (
        factory,
        user_id,
        organization_id,
        conversation_id,
        thread_id,
    ):
        job_id = str(uuid.uuid4())
        graph = _PostMonitorGraph(resume=runner == "resume", user_id=user_id)
        approval = pending_approval(
            graph.pending_snapshot,
            thread_id=str(thread_id),
            run_id=job_id,
            user_id=user_id,
        )
        assert approval is not None
        async with factory() as setup:
            initial = await svc.upsert_run(
                setup,
                job_id=job_id,
                status=JobStatus.RUNNING,
                user_id=user_id,
                organization_id=organization_id,
                thread_id=str(thread_id),
                run_metadata={"approval_id": approval.approval_id},
            )
            assert initial is not None and initial.thread_id == thread_id

        request = AgentExecuteRequest(
            messages=[AgentMessage(role="user", content="current question")],
            page_context=PageContextRequest(),
            model="model-router",
            use_rag=True,
            max_context_docs=5,
            thread_id=str(thread_id),
        )
        user = User(
            id=user_id,
            organization_id=organization_id,
            email="race@example.com",
            password_hash="unused-test-hash",
            first_name="Test",
            last_name="User",
            role=UserRole.USER,
            is_active=True,
        )
        live_payload: dict[str, Any] = {
            "status": (
                JobStatus.AWAITING_CONFIRMATION.value
                if runner == "resume"
                else JobStatus.RUNNING.value
            ),
            "user_id": str(user_id),
            "organization_id": str(organization_id),
            "thread_id": str(thread_id),
            "request": request.model_dump(mode="json"),
            "confirmation": approval.confirmation(),
        }
        with job_store._l1_lock:
            job_store._l1[job_id] = live_payload

        redis_client = redis_asyncio.from_url(redis_url, decode_responses=True)
        await redis_client.ping()
        redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
        await redis_client.delete(redis_key)
        monkeypatch.setattr(
            job_store, "_get_redis", AsyncMock(return_value=redis_client)
        )

        monkeypatch.setattr(execution_service, "AsyncSessionLocal", factory)
        monkeypatch.setattr(database, "AsyncSessionLocal", factory)
        monkeypatch.setattr(
            execution_service,
            "_resolve_thread",
            AsyncMock(
                return_value=(SimpleNamespace(id=thread_id), str(conversation_id))
            ),
        )
        monkeypatch.setattr(
            execution_service, "_resolve_and_bind_project", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            execution_service, "_persist_user_message_guarded", AsyncMock()
        )
        monkeypatch.setattr(
            execution_service,
            "_persist_assistant_message_safe",
            AsyncMock(return_value="assistant-row"),
        )
        monkeypatch.setattr(
            execution_service, "_clear_stale_pending_confirmation", AsyncMock()
        )
        monkeypatch.setattr(
            execution_service,
            "_run_heartbeat",
            _no_heartbeat,
        )
        monkeypatch.setattr(
            "src.services.agent.checkpointer.get_checkpointer", AsyncMock()
        )
        monkeypatch.setattr("src.services.agent.memory.get_memory_store", AsyncMock())
        monkeypatch.setattr(
            "src.services.agent.graph.compile_agent_graph", lambda **_kwargs: graph
        )
        monkeypatch.setattr(
            "src.services.agent.runtime_snapshot.create_runtime_snapshot",
            AsyncMock(return_value=SimpleNamespace(id="snapshot-race")),
        )
        monkeypatch.setattr(
            "src.services.agent.runtime_snapshot.runtime_state_fields",
            lambda *_args: {},
        )
        monkeypatch.setattr(
            "src.services.agent.runtime_snapshot.runtime_config_fields",
            lambda *_args: {},
        )
        monkeypatch.setattr(
            "src.services.agent.runtime_snapshot.resume_runtime_config_fields",
            lambda *_args: {},
        )

        original_record = agent_run_service.record_job_status
        completion_decided = asyncio.Event()
        release_completion_publish = asyncio.Event()
        cancellation_acks: list[Any] = []

        async def record_with_completion_barrier(
            key: str, data: dict[str, Any], *, raise_on_error: bool = False
        ) -> Any:
            decision = await original_record(key, data, raise_on_error=raise_on_error)
            if data.get("status") == JobStatus.CANCELLED:
                cancellation_acks.append(decision)
            if data.get("status") == JobStatus.COMPLETED and not stop_first:
                completion_decided.set()
                await release_completion_publish.wait()
            return decision

        monkeypatch.setattr(
            agent_run_service, "record_job_status", record_with_completion_barrier
        )

        async def cancel_owned_run() -> Any:
            async with factory() as stop_db:
                result = await request_run_cancellation(
                    stop_db,
                    run_id=job_id,
                    thread_id=thread_id,
                    organization_id=organization_id,
                    user_id=user_id,
                    reason="user_requested",
                    request_id="post-monitor-race",
                )
                await stop_db.commit()
                return result

        runner_task = asyncio.create_task(
            execution_service._run_agent_graph(job_id, request, user)
            if runner == "initial"
            else execution_service._resume_agent_graph(
                job_id, True, user, approval_id=approval.approval_id
            )
        )
        try:
            await asyncio.wait_for(graph.post_monitor_reached.wait(), timeout=5)
            if stop_first:
                stop_result = await cancel_owned_run()
                assert stop_result is not None
                assert stop_result.status == JobStatus.STOPPING
                assert stop_result.claimed is True
                graph.release_post_monitor.set()
            else:
                graph.release_post_monitor.set()
                await asyncio.wait_for(completion_decided.wait(), timeout=5)
                stop_result = await cancel_owned_run()
                assert stop_result is not None
                assert stop_result.status == JobStatus.COMPLETED
                assert stop_result.claimed is False
                release_completion_publish.set()
            await asyncio.wait_for(runner_task, timeout=10)

            async with factory() as verify:
                durable = await verify.get(AgentRun, job_id)
                assert durable is not None
                active_rows = (
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
                stopping_events = (
                    (
                        await verify.execute(
                            select(AgentRunEvent).where(
                                AgentRunEvent.run_id == job_id,
                                AgentRunEvent.event_type == "run.stopping",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            live = (
                job_store._get_job_sync(job_id)
                if hasattr(job_store, "_get_job_sync")
                else None
            )
            if live is None:
                with job_store._l1_lock:
                    live = dict(job_store._l1[job_id])
            redis_raw = await redis_client.get(redis_key)
            assert redis_raw is not None
            redis_live = json.loads(redis_raw)
            return (
                {
                    "status": durable.status,
                    "cancel_requested_at": durable.cancel_requested_at,
                    "active_count": len(active_rows),
                    "stopping_count": len(stopping_events),
                    "live": live,
                },
                redis_live,
                len(cancellation_acks),
                len(stopping_events),
                job_id,
            )
        finally:
            if not runner_task.done():
                graph.release_post_monitor.set()
                release_completion_publish.set()
                runner_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await runner_task
            await redis_client.delete(redis_key)
            await redis_client.close()
            with job_store._l1_lock:
                job_store._l1.pop(job_id, None)


@pytest.mark.parametrize("runner", ["initial", "resume"])
@pytest.mark.asyncio
async def test_post_monitor_stop_wins_through_real_runner_publication(
    runner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late accepted Stop is acknowledged before either producer exits."""
    durable, redis_live, ack_count, event_count, _job_id = (
        await _run_post_monitor_terminal_race(
            monkeypatch, runner=runner, stop_first=True
        )
    )
    assert durable["status"] == JobStatus.CANCELLED.value
    assert durable["cancel_requested_at"] is not None
    assert durable["active_count"] == 0
    assert event_count == 1
    assert ack_count == 1
    assert durable["live"]["status"] == JobStatus.CANCELLED.value
    assert "result" not in durable["live"]
    assert redis_live is not None
    assert redis_live["status"] == JobStatus.CANCELLED.value
    assert "result" not in redis_live


@pytest.mark.parametrize("runner", ["initial", "resume"])
@pytest.mark.asyncio
async def test_post_monitor_completion_wins_against_late_stop(
    runner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed completion is not converted into fabricated cancellation."""
    durable, redis_live, ack_count, event_count, _job_id = (
        await _run_post_monitor_terminal_race(
            monkeypatch, runner=runner, stop_first=False
        )
    )
    assert durable["status"] == JobStatus.COMPLETED.value
    assert durable["cancel_requested_at"] is None
    assert durable["active_count"] == 0
    assert event_count == 0
    assert ack_count == 0
    assert durable["live"]["status"] == JobStatus.COMPLETED.value
    assert durable["live"]["result"]["message"]["content"] == ("CURRENT TURN ANSWER")
    assert redis_live is not None
    assert redis_live["status"] == JobStatus.COMPLETED.value
    assert redis_live["result"]["message"]["content"] == "CURRENT TURN ANSWER"


@pytest.mark.asyncio
@pytest.mark.requires_redis
async def test_delayed_real_redis_running_writer_cannot_replace_cancelled_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused stale writer is rejected atomically after durable cancellation."""
    import redis.asyncio as redis_asyncio

    from src.core import database
    from src.services.agent import job_store

    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    redis_url = os.getenv("ORCHESTRATION_TEST_REDIS_URL", "redis://127.0.0.1:32775/15")

    async with _postgres_run_schema(dsn) as (
        factory,
        user_id,
        organization_id,
        _conversation_id,
        thread_id,
    ):
        job_id = str(uuid.uuid4())
        async with factory() as setup:
            run = await svc.upsert_run(
                setup,
                job_id=job_id,
                status=JobStatus.RUNNING,
                user_id=user_id,
                organization_id=organization_id,
                thread_id=str(thread_id),
            )
            assert run is not None
        async with factory() as stop_db:
            stop = await request_run_cancellation(
                stop_db,
                run_id=job_id,
                thread_id=thread_id,
                organization_id=organization_id,
                user_id=user_id,
                reason="user_requested",
                request_id="delayed-redis-writer",
            )
            await stop_db.commit()
            assert stop is not None and stop.status == JobStatus.STOPPING

        redis_client = redis_asyncio.from_url(redis_url, decode_responses=True)
        await redis_client.ping()
        redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
        await redis_client.delete(redis_key)
        prior = {
            "status": JobStatus.RUNNING.value,
            "user_id": str(user_id),
            "organization_id": str(organization_id),
            "thread_id": str(thread_id),
            "created_at": 1.0,
            "_seq": 1,
        }
        await redis_client.setex(
            redis_key, job_store._JOB_TTL_SECONDS, json.dumps(prior)
        )
        delayed_redis = _DelayRunningRedis(redis_client, redis_key)
        monkeypatch.setattr(database, "AsyncSessionLocal", factory)
        monkeypatch.setattr(
            job_store, "_get_redis", AsyncMock(return_value=delayed_redis)
        )
        with job_store._l1_lock:
            job_store._l1[job_id] = dict(prior)

        stale_write = asyncio.create_task(
            job_store.set_job(job_id, dict(prior), project=False)
        )
        try:
            await asyncio.wait_for(delayed_redis.reached.wait(), timeout=3)
            decision = await job_store.set_job(
                job_id,
                {
                    "status": JobStatus.CANCELLED,
                    "error": "execution cancelled",
                    "user_id": str(user_id),
                    "organization_id": str(organization_id),
                    "thread_id": str(thread_id),
                },
                require_durable_decision=True,
            )
            assert decision is not None
            delayed_redis.release.set()
            await asyncio.wait_for(stale_write, timeout=3)
            live = await job_store.get_job(job_id)
            redis_raw = await redis_client.get(redis_key)
            assert redis_raw is not None
            redis_live = json.loads(redis_raw)
            async with factory() as verify:
                durable = await verify.get(AgentRun, job_id)
            assert durable is not None
            assert durable.status == JobStatus.CANCELLED.value
            assert durable.cancel_requested_at is not None
            assert live is not None and live["status"] == JobStatus.CANCELLED
            assert redis_live["status"] == JobStatus.CANCELLED.value
        finally:
            delayed_redis.release.set()
            if not stale_write.done():
                await stale_write
            await redis_client.delete(redis_key)
            await redis_client.close()
            with job_store._l1_lock:
                job_store._l1.pop(job_id, None)


@pytest.mark.asyncio
async def test_real_sweeper_acknowledges_old_stop_and_corrects_cached_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real sweeper uses the durable cancellation decision over old cache."""
    import redis.asyncio as redis_asyncio

    from src.core import database
    from src.services.agent import job_store
    from src.tasks import agent_run_tasks

    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    redis_url = os.getenv("ORCHESTRATION_TEST_REDIS_URL", "redis://127.0.0.1:32775/15")
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    monkeypatch.setattr(agent_run_tasks, "_utcnow", lambda: now)
    monkeypatch.setattr(svc, "_utcnow", lambda: now)
    monkeypatch.setattr(
        agent_run_tasks,
        "get_settings",
        lambda: SimpleNamespace(
            AGENT_RUN_STALE_AFTER_SECONDS=1800,
            AGENT_RUN_HEARTBEAT_SECONDS=60,
            AGENT_RUN_STALE_AWAITING_AFTER_SECONDS=7200,
        ),
    )

    async with _postgres_run_schema(dsn) as (
        factory,
        user_id,
        organization_id,
        _conversation_id,
        thread_id,
    ):
        job_id = str(uuid.uuid4())
        async with factory() as setup:
            run = await svc.upsert_run(
                setup,
                job_id=job_id,
                status=JobStatus.RUNNING,
                user_id=user_id,
                organization_id=organization_id,
                thread_id=str(thread_id),
            )
            assert run is not None
        async with factory() as stop_db:
            stop = await request_run_cancellation(
                stop_db,
                run_id=job_id,
                thread_id=thread_id,
                organization_id=organization_id,
                user_id=user_id,
                reason="user_requested",
                request_id="old-stop-sweep",
            )
            await stop_db.commit()
            assert stop is not None and stop.status is JobStatus.STOPPING
        async with factory() as backdate:
            await backdate.execute(
                update(AgentRun)
                .where(AgentRun.job_id == job_id)
                .values(
                    updated_at=old,
                    cancel_requested_at=old,
                    lease_owner="celery:crashed",
                    lease_expires_at=now - timedelta(minutes=20),
                )
            )
            await backdate.commit()

        redis_client = redis_asyncio.from_url(redis_url, decode_responses=True)
        await redis_client.ping()
        redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
        await redis_client.delete(redis_key)
        await redis_client.setex(
            redis_key,
            job_store._JOB_TTL_SECONDS,
            json.dumps(
                {
                    "status": JobStatus.COMPLETED.value,
                    "user_id": str(user_id),
                    "organization_id": str(organization_id),
                    "thread_id": str(thread_id),
                    "created_at": 2.0,
                    "_seq": 1,
                    "result": {"message": "historical wrong terminal"},
                }
            ),
        )
        monkeypatch.setattr(database, "AsyncSessionLocal", factory)
        monkeypatch.setattr(
            job_store, "_get_redis", AsyncMock(return_value=redis_client)
        )
        with job_store._l1_lock:
            job_store._l1.pop(job_id, None)
        try:
            result = await agent_run_tasks._sweep_stale_agent_runs(
                lease_owner="sweeper:terminal-test"
            )
            async with factory() as verify:
                durable = await verify.get(AgentRun, job_id)
                active_rows = (
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
            live = await job_store.get_job_fresh(job_id)
            redis_raw = await redis_client.get(redis_key)
            assert redis_raw is not None
            redis_live = json.loads(redis_raw)
            assert durable is not None
            assert durable.status == JobStatus.CANCELLED.value
            assert durable.cancel_requested_at == old
            assert durable.lease_owner is None
            assert active_rows == []
            assert live is not None and live["status"] == JobStatus.CANCELLED
            assert "result" not in live
            assert redis_live["status"] == JobStatus.CANCELLED.value
            assert result["cancelled"] == 1
            assert result["failed"] == 0
            assert "result" not in redis_live
        finally:
            await redis_client.delete(redis_key)
            await redis_client.close()
            with job_store._l1_lock:
                job_store._l1.pop(job_id, None)


@pytest.mark.parametrize("winner", ["stopping", "completed"])
@pytest.mark.asyncio
async def test_real_sweeper_consumes_stop_or_completion_winning_after_refresh(
    winner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-refresh winner is honored; a fresh Stop gets its stale grace."""
    from src.core import database
    from src.services.agent import agent_run_service, job_store
    from src.tasks import agent_run_tasks

    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    now = datetime.now(timezone.utc)
    stale_at = now - timedelta(hours=2)
    monkeypatch.setattr(agent_run_tasks, "_utcnow", lambda: now)
    monkeypatch.setattr(agent_run_service, "_utcnow", lambda: now)
    monkeypatch.setattr(
        agent_run_tasks,
        "get_settings",
        lambda: SimpleNamespace(
            AGENT_RUN_STALE_AFTER_SECONDS=1800,
            AGENT_RUN_HEARTBEAT_SECONDS=60,
            AGENT_RUN_STALE_AWAITING_AFTER_SECONDS=7200,
        ),
    )
    reached = asyncio.Event()
    release = asyncio.Event()

    async with _postgres_run_schema(dsn) as (
        factory,
        user_id,
        organization_id,
        _conversation_id,
        thread_id,
    ):
        job_id = str(uuid.uuid4())
        async with factory() as setup:
            await svc.upsert_run(
                setup,
                job_id=job_id,
                status=JobStatus.RUNNING,
                user_id=user_id,
                organization_id=organization_id,
                thread_id=str(thread_id),
            )
            await setup.execute(
                update(AgentRun)
                .where(AgentRun.job_id == job_id)
                .values(
                    updated_at=stale_at,
                    lease_owner="celery:crashed",
                    lease_expires_at=now - timedelta(minutes=20),
                )
            )
            await setup.commit()

        live_payload = {
            "status": JobStatus.RUNNING.value,
            "user_id": str(user_id),
            "organization_id": str(organization_id),
            "thread_id": str(thread_id),
            "created_at": 1.0,
        }
        get_job = AsyncMock(return_value=live_payload)
        set_job = AsyncMock()
        monkeypatch.setattr(database, "AsyncSessionLocal", factory)
        monkeypatch.setattr(job_store, "get_job_fresh", get_job)
        monkeypatch.setattr(job_store, "set_job", set_job)
        original_upsert = agent_run_service.upsert_run
        original_terminalize = agent_run_service.terminalize_stale_run

        async def pause_before_stale_failure(
            db: AsyncSession,
            *,
            job_id: str,
            status: JobStatus,
            **kwargs: Any,
        ) -> Any:
            if job_id == target_job_id and status is JobStatus.FAILED:
                reached.set()
                await release.wait()
            return await original_terminalize(
                db, job_id=job_id, status=status, **kwargs
            )

        target_job_id = job_id
        monkeypatch.setattr(
            agent_run_service, "terminalize_stale_run", pause_before_stale_failure
        )
        sweep_task = asyncio.create_task(
            agent_run_tasks._sweep_stale_agent_runs(lease_owner="sweeper:race-test")
        )
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            if winner == "stopping":
                async with factory() as stop_db:
                    stop = await request_run_cancellation(
                        stop_db,
                        run_id=job_id,
                        thread_id=thread_id,
                        organization_id=organization_id,
                        user_id=user_id,
                        reason="user_requested",
                        request_id="stop-after-refresh",
                    )
                    await stop_db.commit()
                    assert stop is not None and stop.claimed is True
            else:
                async with factory() as finish_db:
                    completed = await original_upsert(
                        finish_db,
                        job_id=job_id,
                        status=JobStatus.COMPLETED,
                        user_id=user_id,
                        organization_id=organization_id,
                        thread_id=str(thread_id),
                        error=None,
                    )
                    assert completed is not None
            release.set()
            result = await asyncio.wait_for(sweep_task, timeout=5)
            async with factory() as verify:
                durable = await verify.get(AgentRun, job_id)
            assert durable is not None
            if winner == "stopping":
                assert durable.status == JobStatus.STOPPING.value
                assert durable.cancel_requested_at is not None
                assert durable.lease_owner == "sweeper:race-test"
                assert result["failed"] == 0
                assert result["cancelled"] == 0
                assert result["skipped"] == 1
                set_job.assert_not_awaited()

                # The newly arrived request remains active until both its
                # grace window and the sweeper lease expire.
                later = now + timedelta(hours=2)
                monkeypatch.setattr(agent_run_tasks, "_utcnow", lambda: later)
                monkeypatch.setattr(agent_run_service, "_utcnow", lambda: later)
                next_result = await agent_run_tasks._sweep_stale_agent_runs(
                    lease_owner="sweeper:race-test"
                )
                async with factory() as verify_cancel:
                    acknowledged = await verify_cancel.get(AgentRun, job_id)
                    active_rows = (
                        (
                            await verify_cancel.execute(
                                select(AgentRun).where(
                                    AgentRun.thread_id == thread_id,
                                    AgentRun.status.in_(svc._ACTIVE_RUN_STATUSES),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                assert next_result["cancelled"] == 1
                assert acknowledged is not None
                assert acknowledged.status == JobStatus.CANCELLED.value
                assert acknowledged.cancel_requested_at is not None
                assert active_rows == []
                set_job.assert_awaited_once()
            else:
                assert durable.status == JobStatus.COMPLETED.value
                assert durable.lease_owner is None
                assert result["failed"] == 0
                assert result["cancelled"] == 0
                assert result["repaired"] == 1
                set_job.assert_awaited_once()
                await_args = set_job.await_args
                assert await_args is not None
                assert await_args.args[1]["status"] is JobStatus.COMPLETED
                assert (
                    await_args.kwargs["decision"].effective_status
                    is JobStatus.COMPLETED
                )
        finally:
            release.set()
            if not sweep_task.done():
                await sweep_task


@pytest.mark.asyncio
async def test_postgres_slot_collision_never_drops_valid_thread_after_winner_finishes() -> (
    None
):
    """A valid thread conflict cannot become an uncorrelated active writer."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    async with _postgres_run_schema(dsn) as (
        factory,
        user_id,
        organization_id,
        _conversation_id,
        thread_id,
    ):
        job_a, job_b, job_c = (str(uuid.uuid4()) for _ in range(3))
        async with factory() as setup:
            first = await svc.upsert_run(
                setup,
                job_id=job_a,
                status=JobStatus.RUNNING,
                user_id=user_id,
                organization_id=organization_id,
                thread_id=str(thread_id),
            )
            assert first is not None and first.thread_id == thread_id

        rolled_back, release = asyncio.Event(), asyncio.Event()
        async with factory() as contender_db, factory() as finisher_db:
            contender = asyncio.create_task(
                svc.upsert_run(
                    cast(
                        AsyncSession,
                        _PauseFirstRollback(contender_db, rolled_back, release),
                    ),
                    job_id=job_b,
                    status=JobStatus.RUNNING,
                    user_id=user_id,
                    organization_id=organization_id,
                    thread_id=str(thread_id),
                )
            )
            await asyncio.wait_for(rolled_back.wait(), timeout=3)
            await svc.upsert_run(finisher_db, job_id=job_a, status=JobStatus.COMPLETED)
            release.set()
            try:
                accepted_b = await contender
            except svc.ActiveRunConflict:
                accepted_b = None

        async with factory() as check_b:
            row_b = await check_b.get(AgentRun, job_b)
        if accepted_b is None:
            assert row_b is None
            async with factory() as third:
                accepted_c = await svc.upsert_run(
                    third,
                    job_id=job_c,
                    status=JobStatus.RUNNING,
                    user_id=user_id,
                    organization_id=organization_id,
                    thread_id=str(thread_id),
                )
            assert accepted_c is not None and accepted_c.thread_id == thread_id
        else:
            assert row_b is not None and row_b.thread_id == thread_id
            async with factory() as third:
                with pytest.raises(svc.ActiveRunConflict):
                    await svc.upsert_run(
                        third,
                        job_id=job_c,
                        status=JobStatus.RUNNING,
                        user_id=user_id,
                        organization_id=organization_id,
                        thread_id=str(thread_id),
                    )

        async with factory() as verify:
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
            assert len(active) == 1


@pytest.mark.asyncio
async def test_postgres_thread_backfill_collision_keeps_or_rejects_correlation() -> (
    None
):
    """A failed NULL→thread backfill may not retry as status-only."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    async with _postgres_run_schema(dsn) as (
        factory,
        user_id,
        organization_id,
        _conversation_id,
        thread_id,
    ):
        job_a, job_b, job_c = (str(uuid.uuid4()) for _ in range(3))
        async with factory() as setup:
            await svc.upsert_run(
                setup,
                job_id=job_a,
                status=JobStatus.RUNNING,
                user_id=user_id,
                organization_id=organization_id,
                thread_id=str(thread_id),
            )
            uncorrelated = await svc.upsert_run(
                setup,
                job_id=job_b,
                status=JobStatus.RUNNING,
                user_id=user_id,
                organization_id=organization_id,
            )
            assert uncorrelated is not None and uncorrelated.thread_id is None

        rolled_back, release = asyncio.Event(), asyncio.Event()
        async with factory() as contender_db, factory() as finisher_db:
            contender = asyncio.create_task(
                svc.upsert_run(
                    cast(
                        AsyncSession,
                        _PauseFirstRollback(contender_db, rolled_back, release),
                    ),
                    job_id=job_b,
                    status=JobStatus.RUNNING,
                    user_id=user_id,
                    organization_id=organization_id,
                    thread_id=str(thread_id),
                )
            )
            await asyncio.wait_for(rolled_back.wait(), timeout=3)
            await svc.upsert_run(finisher_db, job_id=job_a, status=JobStatus.COMPLETED)
            release.set()
            try:
                accepted_b = await contender
            except svc.ActiveRunConflict:
                accepted_b = None

        async with factory() as check_b:
            row_b = await check_b.get(AgentRun, job_b)
        if accepted_b is None:
            assert row_b is not None and row_b.thread_id is None
            async with factory() as third:
                accepted_c = await svc.upsert_run(
                    third,
                    job_id=job_c,
                    status=JobStatus.RUNNING,
                    user_id=user_id,
                    organization_id=organization_id,
                    thread_id=str(thread_id),
                )
            assert accepted_c is not None and accepted_c.thread_id == thread_id
        else:
            assert row_b is not None and row_b.thread_id == thread_id
            async with factory() as third:
                with pytest.raises(svc.ActiveRunConflict):
                    await svc.upsert_run(
                        third,
                        job_id=job_c,
                        status=JobStatus.RUNNING,
                        user_id=user_id,
                        organization_id=organization_id,
                        thread_id=str(thread_id),
                    )

        async with factory() as verify:
            accepted_on_thread = (
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
        assert len(accepted_on_thread) <= 1


@pytest.mark.asyncio
async def test_postgres_missing_thread_fk_retains_legacy_uncorrelated_insert() -> None:
    """A positively identified dangling-thread FK keeps its legacy fallback."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    async with _postgres_run_schema(dsn) as (
        factory,
        user_id,
        organization_id,
        _conversation_id,
        _thread_id,
    ):
        async with factory() as db:
            result = await svc.upsert_run(
                db,
                job_id=str(uuid.uuid4()),
                status=JobStatus.RUNNING,
                user_id=user_id,
                organization_id=organization_id,
                thread_id=str(uuid.uuid4()),
            )
        assert result is not None
        assert result.status == JobStatus.RUNNING.value
        assert result.thread_id is None

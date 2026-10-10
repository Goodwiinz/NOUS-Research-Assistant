"""Real PostgreSQL checkpoints + run CAS across the job and SSE adapters.

Run with ORCHESTRATION_TEST_DATABASE_URL pointing at a disposable local DB.
All tables live in a generated schema that is dropped on exit.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from sqlalchemy import text

from src.api.agent import execute, streaming
from src.services.agent import agent_execution_service as runner
from src.services.agent import agent_run_service as runs
from src.services.agent import checkpointer
from src.services.agent import graph as graph_module
from src.services.agent import job_store, memory, runtime_snapshot
from src.services.agent.confirmation_service import pending_approval
from src.shared.enums import JobStatus
from tests.integration.test_agent_run_concurrency import _postgres_run_schema
from tests.unit.agent.test_confirmation_service import approval_graph
from tests.utils.agent_stream import sse_data, sse_event_name

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.mark.parametrize("winner", ["job", "stream"])
async def test_adapters_reject_stale_and_competing_decisions(
    monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    async with _postgres_run_schema(dsn) as (factory, user_id, org_id, _, thread_id):
        async with factory() as db:
            schema = (await db.execute(text("SHOW search_path"))).scalar_one()
        async with await AsyncConnection.connect(
            dsn.replace("postgresql+asyncpg://", "postgresql://"),
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
            options=f"-c search_path={schema}",
        ) as connection:
            saver = AsyncPostgresSaver(connection)
            await saver.setup()
            graph = approval_graph(saver)
            config = {"configurable": {"thread_id": str(thread_id)}}
            await graph.ainvoke({"user_id": str(user_id)}, config)
            run_id = str(uuid.uuid4())
            user: Any = SimpleNamespace(id=user_id, organization_id=org_id)
            first = pending_approval(
                await graph.aget_state(config),
                thread_id=str(thread_id),
                run_id=run_id,
                user_id=user_id,
            )
            assert first is not None
            request = execute.AgentExecuteRequest(
                messages=[execute.AgentMessage(role="user", content="Two actions")],
                thread_id=str(thread_id),
            )
            job = {
                "status": JobStatus.AWAITING_CONFIRMATION,
                "user_id": str(user_id),
                "organization_id": str(org_id),
                "thread_id": str(thread_id),
                "request": request.model_dump(),
                "confirmation": first.confirmation(),
            }
            async with factory() as db:
                await runs.upsert_run(
                    db,
                    job_id=run_id,
                    status=JobStatus.AWAITING_CONFIRMATION,
                    organization_id=org_id,
                    user_id=user_id,
                    thread_id=str(thread_id),
                    run_metadata={"approval_id": first.approval_id},
                )

            # Infrastructure doubles only: graph/checkpoint, receipt validation,
            # durable claims and both transport entry points are real.
            monkeypatch.setattr(
                checkpointer, "get_checkpointer", AsyncMock(return_value=saver)
            )
            monkeypatch.setattr(
                memory, "get_memory_store", AsyncMock(return_value=None)
            )
            monkeypatch.setattr(graph_module, "compile_agent_graph", lambda **_: graph)
            monkeypatch.setattr(streaming, "AsyncSessionLocal", factory)
            monkeypatch.setattr(runner, "AsyncSessionLocal", factory)
            monkeypatch.setattr("src.core.database.AsyncSessionLocal", factory)
            monkeypatch.setattr(streaming, "_bootstrap_langsmith", lambda: None)
            monkeypatch.setattr(
                streaming, "_cancel_current_task_on_disconnect", lambda _: None
            )
            monkeypatch.setattr(
                streaming,
                "process_local_confirmation_coordination_allowed",
                lambda: True,
            )
            monkeypatch.setattr(job_store, "get_redis", AsyncMock(return_value=None))
            monkeypatch.setattr(
                streaming._stream_buffer,
                "start_stream",
                AsyncMock(side_effect=RuntimeError("no Redis")),
            )
            monkeypatch.setattr(
                streaming._stream_buffer,
                "stream_id_for_run",
                AsyncMock(return_value=None),
            )
            monkeypatch.setattr(
                runtime_snapshot,
                "hydrate_runtime_state_from_snapshot",
                AsyncMock(return_value={}),
            )
            monkeypatch.setattr(runner, "_run_heartbeat", lambda _: nullcontext())
            for module in (execute, streaming, runner):
                monkeypatch.setattr(
                    module, "_resolve_thread", AsyncMock(return_value=(None, None))
                )
            monkeypatch.setattr(execute, "_enforce_rate_limit", AsyncMock())
            monkeypatch.setattr(
                job_store, "get_job_fresh", AsyncMock(side_effect=lambda _: dict(job))
            )
            monkeypatch.setattr(
                job_store, "compare_and_set_status", AsyncMock(return_value="claimed")
            )
            monkeypatch.setattr(runner, "_get_job", lambda _: dict(job))

            # Keep the real job-store write-through so the job adapter must
            # persist its receipt through _projection_payload/record_job_status.
            calls: list[Any] = []
            stream_commands: list[Any] = []
            original_stream = graph.astream_events

            async def stream_events(
                command: Any, *args: Any, **kwargs: Any
            ) -> AsyncIterator[dict[str, Any]]:
                stream_commands.append(command)
                async for event in original_stream(command, *args, **kwargs):
                    yield event

            monkeypatch.setattr(graph, "astream_events", stream_events)
            original_invoke = runner._invoke_graph_with_cancellation_monitor

            async def invoke(*args: Any, **kwargs: Any) -> Any:
                calls.append(args[1])
                return await original_invoke(*args, **kwargs)

            monkeypatch.setattr(
                runner, "_invoke_graph_with_cancellation_monitor", invoke
            )

            async def stream(receipt: str) -> list[str]:
                return [
                    frame
                    async for frame in streaming.stream_confirm_event_generator(
                        execute.StreamConfirmRequest(
                            thread_id=str(thread_id),
                            confirmed=True,
                            approval_id=receipt,
                        ),
                        SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
                        user,
                    )
                ]

            async def job_confirm(receipt: str) -> BackgroundTasks:
                tasks = BackgroundTasks()
                async with factory() as db:
                    await execute.confirm_agent_action(
                        run_id,
                        execute.ConfirmationRequest(
                            confirmed=False, approval_id=receipt
                        ),
                        tasks,
                        user,
                        db,
                    )
                return tasks

            if winner == "job":
                tasks = await job_confirm(first.approval_id)
                # The graph is still parked while BackgroundTasks waits. The
                # other adapter must lose on the DB claim, before dispatch.
                loser_frames = await stream(first.approval_id)
                assert all(
                    sse_event_name(frame) != "confirmation" for frame in loser_frames
                )
                assert any(sse_event_name(frame) == "error" for frame in loser_frames)
                await tasks()
                assert calls[0].resume == first.resume(False)
            else:
                frames = await stream(first.approval_id)
                assert stream_commands[0].resume == first.resume(True)
                assert any(sse_event_name(frame) == "confirmation" for frame in frames)
                with pytest.raises(HTTPException) as conflict:
                    await job_confirm(first.approval_id)
                assert conflict.value.status_code == 409

            second_snapshot = await graph.aget_state(config)
            second = pending_approval(
                second_snapshot,
                thread_id=str(thread_id),
                run_id=run_id,
                user_id=user_id,
            )
            assert second is not None and second.approval_id != first.approval_id
            async with factory() as db:
                run = await runs.get_run(
                    db, run_id, organization_id=org_id, user_id=user_id
                )
                assert run is not None
                assert run.status == JobStatus.AWAITING_CONFIRMATION.value
                assert run.run_metadata["approval_id"] == second.approval_id
                replay = await execute._pending_confirmation_frame(
                    str(thread_id), user, db=db
                )
                assert replay is not None
                assert (
                    sse_data(replay)["confirmation"]["approval_id"]
                    == second.approval_id
                )

            # Stale cards and delayed background tasks cannot consume B or
            # change its status, even though the run and interrupt id repeat.
            before_calls = len(calls)
            with pytest.raises(HTTPException) as conflict:
                await job_confirm(first.approval_id)
            assert conflict.value.status_code == 409
            stale_frames = await stream(first.approval_id)
            assert any(sse_event_name(frame) == "error" for frame in stale_frames)
            await runner._resume_agent_graph(
                run_id, True, user, approval_id=first.approval_id
            )
            assert len(calls) == before_calls
            async with factory() as db:
                run = await runs.get_run(
                    db, run_id, organization_id=org_id, user_id=user_id
                )
                assert run is not None
                assert run.status == JobStatus.AWAITING_CONFIRMATION.value
                assert run.run_metadata["approval_id"] == second.approval_id

            # Two different decisions racing on B can claim it only once.
            async def claim_second() -> bool:
                async with factory() as db:
                    return await runs.claim_awaiting_run_for_confirmation(
                        db,
                        run_id,
                        approval_id=second.approval_id,
                        organization_id=org_id,
                        user_id=user_id,
                    )

            assert sorted(await asyncio.gather(claim_second(), claim_second())) == [
                False,
                True,
            ]
            await runner._resume_agent_graph(
                run_id, True, user, approval_id=first.approval_id
            )
            assert len(calls) == before_calls
            async with factory() as db:
                assert not await runs.release_confirmation_claim(
                    db,
                    run_id,
                    approval_id=first.approval_id,
                    organization_id=org_id,
                    user_id=user_id,
                )

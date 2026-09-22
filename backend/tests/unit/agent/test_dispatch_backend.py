"""AGENT_DISPATCH_BACKEND — flag-gated Celery dispatch for /execute (P1.3).

Covers the audit X1 dispatch-half contract:

- backend selection: default "background" (today's BackgroundTasks path),
  "celery" opt-in, unknown values degrade to background with a warning;
- _celery_dispatch ordering: the agent_runs row commits BEFORE the broker
  publish (flush-before-external — proven by making .delay() explode and
  observing the durable row);
- idempotency-key dedupe: a retried /execute with the same client_message_id
  resolves to the run it already created instead of enqueueing twice;
- enqueue failure marks the job failed (never falls back in-process next to a
  possibly-published message — that would double-run the turn);
- row-write failure falls back to the in-process path (nothing was enqueued,
  so in-process is safe).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api.agent.execute import (
    AgentExecuteRequest,
    JobStatus,
    _celery_dispatch,
    _client_idempotency_key,
    _resolve_dispatch_backend,
)
from src.models.agent_run import AgentRun
from src.services.agent.agent_run_service import ActiveRunConflict

pytestmark = pytest.mark.unit


def _settings(backend):
    return SimpleNamespace(AGENT_DISPATCH_BACKEND=backend)


class TestResolveDispatchBackend:
    def test_default_is_background(self):
        with patch(
            "src.core.config.get_settings", return_value=_settings("background")
        ):
            assert _resolve_dispatch_backend() == "background"

    def test_celery_opt_in(self):
        with patch("src.core.config.get_settings", return_value=_settings("celery")):
            assert _resolve_dispatch_backend() == "celery"

    def test_case_and_whitespace_normalized(self):
        with patch("src.core.config.get_settings", return_value=_settings(" Celery ")):
            assert _resolve_dispatch_backend() == "celery"

    @pytest.mark.parametrize("bad", ["kafka", "", None, "backgroundd"])
    def test_unknown_value_degrades_to_background(self, bad):
        with patch("src.core.config.get_settings", return_value=_settings(bad)):
            assert _resolve_dispatch_backend() == "background"


class TestClientIdempotencyKey:
    def test_derived_from_newest_user_cmid_scoped_by_user(self):
        cmid = uuid.uuid4()
        user = SimpleNamespace(id=uuid.uuid4())
        request = AgentExecuteRequest(
            messages=[{"role": "user", "content": "hi", "client_message_id": str(cmid)}]
        )
        assert (
            _client_idempotency_key(request, user) == f"agent-execute:{user.id}:{cmid}"
        )

    def test_none_without_cmid(self):
        user = SimpleNamespace(id=uuid.uuid4())
        request = AgentExecuteRequest(messages=[{"role": "user", "content": "hi"}])
        assert _client_idempotency_key(request, user) is None


# ---------------------------------------------------------------------------
# _celery_dispatch — ordering, dedupe, failure modes (sqlite-backed)
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AgentRun.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _user():
    return SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())


def _request(cmid=None, *, thread_id=None):
    msg = {"role": "user", "content": "find papers"}
    if cmid is not None:
        msg["client_message_id"] = str(cmid)
    return AgentExecuteRequest(messages=[msg], thread_id=thread_id)


def _patched(session_factory, delay=None):
    """Patch the dispatch collaborators: DB session, job store write, task."""
    task = MagicMock()
    task.delay = delay if delay is not None else MagicMock()
    return (
        patch("src.core.database.AsyncSessionLocal", session_factory),
        patch("src.api.agent.execute._set_job"),
        patch("src.tasks.agent_run_tasks.run_agent_job", task),
        task,
    )


async def _get_run(session_factory, job_id):
    async with session_factory() as db:
        return await db.get(AgentRun, job_id)


@pytest.mark.asyncio
async def test_dispatch_commits_row_before_enqueue(session_factory):
    """Flush-before-external: .delay() exploding must still leave the durable
    row (proving the commit happened first) and mark the job failed."""
    user = _user()
    job_id = str(uuid.uuid4())
    boom = MagicMock(side_effect=RuntimeError("broker down"))
    p_db, p_set_job, p_task, task = _patched(session_factory, delay=boom)
    with p_db, p_set_job as set_job_mock, p_task:
        outcome, returned_id = await _celery_dispatch(
            job_id, {"status": JobStatus.RUNNING}, _request(uuid.uuid4()), user
        )

    assert outcome == "failed"
    assert returned_id == job_id
    task.delay.assert_called_once()
    run = await _get_run(session_factory, job_id)
    assert run is not None  # the row was durable before the publish attempt
    assert run.status == "failed"  # record_job_status marked the orphan
    # The job store saw the initial write and then the failed overwrite.
    statuses = [c.args[1]["status"] for c in set_job_mock.call_args_list]
    assert statuses == [JobStatus.QUEUED, JobStatus.FAILED]


@pytest.mark.asyncio
async def test_dispatch_success_row_then_job_then_delay(session_factory):
    user = _user()
    job_id = str(uuid.uuid4())
    p_db, p_set_job, p_task, task = _patched(session_factory)
    with p_db, p_set_job as set_job_mock, p_task:
        outcome, returned_id = await _celery_dispatch(
            job_id, {"status": JobStatus.RUNNING}, _request(uuid.uuid4()), user
        )

    assert (outcome, returned_id) == ("dispatched", job_id)
    run = await _get_run(session_factory, job_id)
    assert run is not None and run.status == "queued"
    assert run.user_id == user.id and run.organization_id == user.organization_id
    assert run.idempotency_key is not None
    set_job_mock.assert_called_once()
    assert set_job_mock.call_args.args[1]["status"] == JobStatus.QUEUED
    assert set_job_mock.call_args.kwargs == {"project": False}
    kwargs = task.delay.call_args.kwargs
    assert kwargs["job_id"] == job_id
    assert kwargs["user_id"] == str(user.id)
    assert kwargs["request_payload"]["messages"][0]["content"] == "find papers"


@pytest.mark.asyncio
async def test_dispatch_dedupes_on_idempotency_key(session_factory):
    """A retry carrying the same client_message_id resolves to the existing
    run: same job_id back, nothing enqueued, no phantom job record."""
    user = _user()
    cmid = uuid.uuid4()
    first_id = str(uuid.uuid4())
    p_db, p_set_job, p_task, task = _patched(session_factory)
    with p_db, p_set_job as set_job_mock, p_task:
        outcome1, id1 = await _celery_dispatch(
            first_id, {"status": JobStatus.RUNNING}, _request(cmid), user
        )
        outcome2, id2 = await _celery_dispatch(
            str(uuid.uuid4()), {"status": JobStatus.RUNNING}, _request(cmid), user
        )

    assert outcome1 == "dispatched"
    assert (outcome2, id2) == ("dedup", first_id)
    assert task.delay.call_count == 1  # retry enqueued nothing new
    assert set_job_mock.call_count == 1  # and wrote no second job record


@pytest.mark.asyncio
async def test_dispatch_rejects_a_second_writer_for_the_same_thread(session_factory):
    user = _user()
    thread_id = str(uuid.uuid4())
    p_db, p_set_job, p_task, task = _patched(session_factory)
    with p_db, p_set_job, p_task:
        first, _ = await _celery_dispatch(
            str(uuid.uuid4()),
            {"status": JobStatus.RUNNING},
            _request(uuid.uuid4(), thread_id=thread_id),
            user,
        )
        second, _ = await _celery_dispatch(
            str(uuid.uuid4()),
            {"status": JobStatus.RUNNING},
            _request(uuid.uuid4(), thread_id=thread_id),
            user,
        )

    assert first == "dispatched"
    assert second == "conflict"
    assert task.delay.call_count == 1


@pytest.mark.asyncio
async def test_dedup_is_tenant_scoped(session_factory):
    """Another user reusing the same cmid must NOT resolve to the first
    user's run (the key embeds the user id, so no collision is possible)."""
    cmid = uuid.uuid4()
    p_db, p_set_job, p_task, task = _patched(session_factory)
    with p_db, p_set_job, p_task:
        _, id_a = await _celery_dispatch(
            str(uuid.uuid4()), {"status": JobStatus.RUNNING}, _request(cmid), _user()
        )
        outcome_b, id_b = await _celery_dispatch(
            str(uuid.uuid4()), {"status": JobStatus.RUNNING}, _request(cmid), _user()
        )
    assert outcome_b == "dispatched"
    assert id_b != id_a
    assert task.delay.call_count == 2


@pytest.mark.asyncio
async def test_row_write_failure_falls_back_in_process(session_factory):
    """Postgres down at dispatch time: nothing enqueued → in-process is safe."""

    def _boom():
        raise RuntimeError("pg down")

    task = MagicMock()
    with (
        patch("src.core.database.AsyncSessionLocal", _boom),
        patch("src.api.agent.execute._set_job") as set_job_mock,
        patch("src.tasks.agent_run_tasks.run_agent_job", task),
    ):
        outcome, _ = await _celery_dispatch(
            str(uuid.uuid4()),
            {"status": JobStatus.RUNNING},
            _request(uuid.uuid4()),
            _user(),
        )
    assert outcome == "fallback"
    task.delay.assert_not_called()
    set_job_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Endpoint routing — which path does /execute take per flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestExecuteEndpointRouting:
    async def _call_execute(
        self,
        backend: str,
        dispatch_outcome=None,
        request=None,
        resolved_thread=None,
    ):
        from src.api.agent.execute import execute_agent

        user = _user()
        background_tasks = MagicMock()
        celery_dispatch = AsyncMock(
            return_value=dispatch_outcome or ("dispatched", "job-from-celery")
        )
        db = MagicMock()
        db.get = AsyncMock(return_value=None)
        db.commit = AsyncMock()
        resolve_thread = AsyncMock(return_value=resolved_thread or (None, ""))
        with (
            patch(
                "src.api.agent.execute._resolve_dispatch_backend",
                return_value=backend,
            ),
            patch("src.api.agent.execute._celery_dispatch", celery_dispatch),
            patch("src.api.agent.execute._resolve_thread", new=resolve_thread),
            patch("src.api.agent.execute._set_job") as set_job_mock,
            patch(
                "src.api.agent.execute._agent_rate_limiter.check_rate_limit",
                new=AsyncMock(return_value=(True, 0)),
            ),
            patch(
                "src.api.agent.execute._agent_rate_limiter.record_attempt",
                new=AsyncMock(),
            ),
        ):
            response = await execute_agent(
                request or _request(uuid.uuid4()),
                background_tasks,
                current_user=user,
                db=db,
            )
        return response, celery_dispatch, background_tasks, set_job_mock

    async def test_background_mode_never_touches_celery(self):
        response, celery_dispatch, background_tasks, set_job = await self._call_execute(
            "background"
        )
        celery_dispatch.assert_not_awaited()
        background_tasks.add_task.assert_called_once()
        set_job.assert_called_once()
        assert response.job_id

    async def test_celery_mode_dispatches_and_skips_background(self):
        response, celery_dispatch, background_tasks, _ = await self._call_execute(
            "celery"
        )
        celery_dispatch.assert_awaited_once()
        background_tasks.add_task.assert_not_called()
        assert response.job_id == "job-from-celery"

    async def test_celery_fallback_outcome_runs_in_process(self):
        response, celery_dispatch, background_tasks, set_job = await self._call_execute(
            "celery", dispatch_outcome=("fallback", "x")
        )
        celery_dispatch.assert_awaited_once()
        background_tasks.add_task.assert_called_once()
        set_job.assert_called_once()
        assert response.job_id  # fresh id, not the fallback marker

    @pytest.mark.parametrize(
        ("outcome", "status_code"),
        [("conflict", 409), ("unavailable", 503)],
    )
    async def test_celery_writer_slot_failures_are_http_errors(
        self, outcome, status_code
    ):
        with pytest.raises(HTTPException) as exc_info:
            await self._call_execute("celery", dispatch_outcome=(outcome, "x"))
        assert exc_info.value.status_code == status_code

    async def test_background_mode_rejects_an_active_thread_writer(self):
        thread_id = str(uuid.uuid4())
        with (
            patch(
                "src.services.agent.agent_run_service.upsert_run",
                new=AsyncMock(side_effect=ActiveRunConflict("active")),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await self._call_execute(
                "background",
                request=_request(uuid.uuid4(), thread_id=thread_id),
                resolved_thread=(SimpleNamespace(id=thread_id), ""),
            )
        assert exc_info.value.status_code == 409

    async def test_background_dedup_lookup_failure_is_safe_503(self):
        from src.api.agent.execute import execute_agent

        user = _user()
        background_tasks = MagicMock()
        db = MagicMock()
        db.rollback = AsyncMock()
        with (
            patch(
                "src.api.agent.execute._resolve_dispatch_backend",
                return_value="background",
            ),
            patch(
                "src.api.agent.execute._resolve_thread",
                new=AsyncMock(return_value=(None, "")),
            ),
            patch(
                "src.api.agent.execute._agent_rate_limiter.check_rate_limit",
                new=AsyncMock(return_value=(True, 0)),
            ),
            patch(
                "src.api.agent.execute._agent_rate_limiter.record_attempt",
                new=AsyncMock(),
            ),
            patch(
                "src.services.agent.agent_run_service.upsert_run",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "src.services.agent.agent_run_service.get_run_by_idempotency_key",
                new=AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await execute_agent(
                _request(uuid.uuid4()),
                background_tasks,
                current_user=user,
                db=db,
            )

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "Unable to persist this run. Please retry."
        db.rollback.assert_awaited_once()
        background_tasks.add_task.assert_not_called()

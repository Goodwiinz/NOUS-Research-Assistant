from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.agent import execute as execute_mod
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.shared.enums import JobStatus

THREAD_ID = "55555555-5555-5555-5555-555555555555"
USER_ID = UUID("33333333-3333-3333-3333-333333333333")
ORG_ID = UUID("11111111-1111-1111-1111-111111111111")


def _client(*, owns_thread: bool = True) -> tuple[TestClient, AsyncMock]:
    app = FastAPI()
    app.include_router(execute_mod.router)
    db = AsyncMock()
    db.execute.return_value = Mock(
        scalar_one_or_none=Mock(
            return_value=SimpleNamespace(id=THREAD_ID) if owns_thread else None
        )
    )

    async def override_db() -> AsyncIterator[AsyncMock]:
        yield db

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=USER_ID,
        organization_id=ORG_ID,
    )
    app.dependency_overrides[get_db] = override_db
    return TestClient(app), db


def test_cancel_awaiting_confirmation_is_durable_and_clears_checkpoint() -> None:
    client, db = _client()
    active = SimpleNamespace(
        job_id="run-1", status=JobStatus.AWAITING_CONFIRMATION.value
    )
    graph = object()
    abandon = AsyncMock(return_value="run-1")
    clear = AsyncMock(return_value=["create_note"])
    mirror = AsyncMock(return_value="claimed")

    with (
        patch.object(
            execute_mod, "get_active_run_for_thread", new=AsyncMock(return_value=active)
        ),
        patch.object(execute_mod, "abandon_awaiting_submission", new=abandon),
        patch(
            "src.services.agent.job_store.compare_and_set_status",
            new=mirror,
        ),
        patch.object(execute_mod, "_clear_stale_pending_confirmation", new=clear),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
    ):
        response = client.post(f"/api/v1/agent/stream/cancel/{THREAD_ID}")

    assert response.status_code == 204
    abandon.assert_awaited_once_with(
        db,
        thread_id=UUID(THREAD_ID),
        organization_id=ORG_ID,
        user_id=USER_ID,
        reason="user_stopped_confirmation",
    )
    db.commit.assert_awaited_once()
    clear.assert_awaited_once()
    mirror.assert_awaited_once_with(
        "run-1",
        JobStatus.AWAITING_CONFIRMATION,
        JobStatus.CANCELLED,
        project=False,
    )


def test_cancel_rejects_a_run_that_is_actively_executing() -> None:
    client, _db = _client()
    active = SimpleNamespace(job_id="run-1", status=JobStatus.RUNNING.value)
    abandon = AsyncMock()

    with (
        patch.object(
            execute_mod, "get_active_run_for_thread", new=AsyncMock(return_value=active)
        ),
        patch.object(execute_mod, "abandon_awaiting_submission", new=abandon),
    ):
        response = client.post(f"/api/v1/agent/stream/cancel/{THREAD_ID}")

    assert response.status_code == 409
    abandon.assert_not_awaited()


def test_cancel_body_without_identity_cannot_stop_a_newer_running_run() -> None:
    client, _db = _client()
    active = SimpleNamespace(job_id="run-new", status=JobStatus.RUNNING.value)
    stop = AsyncMock()

    with (
        patch.object(
            execute_mod, "get_active_run_for_thread", new=AsyncMock(return_value=active)
        ),
        patch.object(execute_mod, "request_run_cancellation", new=stop),
    ):
        response = client.post(
            f"/api/v1/agent/stream/cancel/{THREAD_ID}",
            json={},
        )

    assert response.status_code == 409
    stop.assert_not_awaited()


def test_cancel_reports_an_already_completed_exact_run() -> None:
    client, _db = _client()
    completed = SimpleNamespace(status=JobStatus.COMPLETED.value)

    with (
        patch.object(
            execute_mod, "get_active_run_for_thread", new=AsyncMock(return_value=None)
        ),
        patch.object(execute_mod, "get_run", new=AsyncMock(return_value=completed)),
    ):
        response = client.post(
            f"/api/v1/agent/stream/cancel/{THREAD_ID}",
            json={"expected_run_id": "run-completed"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Run is already complete"


def test_repeated_cancel_of_an_exact_cancelled_run_is_idempotent() -> None:
    client, _db = _client()
    cancelled = SimpleNamespace(status=JobStatus.CANCELLED.value)

    with (
        patch.object(
            execute_mod, "get_active_run_for_thread", new=AsyncMock(return_value=None)
        ),
        patch.object(execute_mod, "get_run", new=AsyncMock(return_value=cancelled)),
    ):
        response = client.post(
            f"/api/v1/agent/stream/cancel/{THREAD_ID}",
            json={"expected_run_id": "run-cancelled"},
        )

    assert response.status_code == 204


def test_cancel_running_run_claims_durable_stop_with_expected_identity() -> None:
    client, db = _client()
    active = SimpleNamespace(job_id="run-1", status=JobStatus.RUNNING.value)
    stop = AsyncMock(
        return_value=SimpleNamespace(
            run_id="run-1", status=JobStatus.STOPPING, claimed=True
        )
    )

    with (
        patch.object(
            execute_mod, "get_active_run_for_thread", new=AsyncMock(return_value=active)
        ),
        patch.object(execute_mod, "request_run_cancellation", new=stop),
    ):
        response = client.post(
            f"/api/v1/agent/stream/cancel/{THREAD_ID}",
            json={"expected_run_id": "run-1"},
        )

    assert response.status_code == 204
    stop.assert_awaited_once_with(
        db,
        run_id="run-1",
        thread_id=UUID(THREAD_ID),
        organization_id=ORG_ID,
        user_id=USER_ID,
        reason="user_requested",
        request_id=None,
    )
    db.commit.assert_awaited_once()


def test_cancel_rejects_stale_identity_without_touching_newer_run() -> None:
    client, _db = _client()
    active = SimpleNamespace(job_id="run-2", status=JobStatus.RUNNING.value)
    stop = AsyncMock()

    with (
        patch.object(
            execute_mod, "get_active_run_for_thread", new=AsyncMock(return_value=active)
        ),
        patch.object(execute_mod, "request_run_cancellation", new=stop),
    ):
        response = client.post(
            f"/api/v1/agent/stream/cancel/{THREAD_ID}",
            json={"expected_run_id": "run-1"},
        )

    assert response.status_code == 409
    stop.assert_not_awaited()


def test_repeated_running_stop_is_idempotent_after_ack_claim() -> None:
    client, db = _client()
    active = SimpleNamespace(job_id="run-1", status=JobStatus.STOPPING.value)
    stop = AsyncMock(
        return_value=SimpleNamespace(
            run_id="run-1", status=JobStatus.STOPPING, claimed=False
        )
    )

    with (
        patch.object(
            execute_mod, "get_active_run_for_thread", new=AsyncMock(return_value=active)
        ),
        patch.object(execute_mod, "request_run_cancellation", new=stop),
    ):
        response = client.post(
            f"/api/v1/agent/stream/cancel/{THREAD_ID}",
            json={"expected_run_id": "run-1"},
        )

    assert response.status_code == 204
    stop.assert_awaited_once()
    db.commit.assert_awaited_once()


def test_cancel_reports_completion_when_terminal_race_wins_after_lookup() -> None:
    client, db = _client()
    active = SimpleNamespace(job_id="run-1", status=JobStatus.RUNNING.value)
    stop = AsyncMock(
        return_value=SimpleNamespace(
            run_id="run-1", status=JobStatus.COMPLETED, claimed=False
        )
    )

    with (
        patch.object(
            execute_mod, "get_active_run_for_thread", new=AsyncMock(return_value=active)
        ),
        patch.object(execute_mod, "request_run_cancellation", new=stop),
    ):
        response = client.post(
            f"/api/v1/agent/stream/cancel/{THREAD_ID}",
            json={"expected_run_id": "run-1"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Run is already complete"
    db.commit.assert_awaited_once()


def test_cancel_does_not_report_success_when_confirmation_wins_the_race() -> None:
    client, _db = _client()
    awaiting = SimpleNamespace(
        job_id="run-1", status=JobStatus.AWAITING_CONFIRMATION.value
    )
    running = SimpleNamespace(job_id="run-1", status=JobStatus.RUNNING.value)

    with (
        patch.object(
            execute_mod,
            "get_active_run_for_thread",
            new=AsyncMock(side_effect=[awaiting, running]),
        ),
        patch.object(
            execute_mod,
            "abandon_awaiting_submission",
            new=AsyncMock(return_value=None),
        ),
    ):
        response = client.post(f"/api/v1/agent/stream/cancel/{THREAD_ID}")

    assert response.status_code == 409


def test_cancel_hides_unowned_threads() -> None:
    client, _db = _client(owns_thread=False)

    response = client.post(f"/api/v1/agent/stream/cancel/{THREAD_ID}")

    assert response.status_code == 404


def test_job_confirm_fails_when_durable_stop_won() -> None:
    client, _db = _client()
    resume = AsyncMock()
    job = {
        "status": JobStatus.AWAITING_CONFIRMATION,
        "user_id": str(USER_ID),
    }

    with (
        patch(
            "src.services.agent.job_store.get_job_fresh",
            new=AsyncMock(return_value=job),
        ),
        patch.object(
            execute_mod,
            "claim_awaiting_run_for_confirmation",
            new=AsyncMock(return_value=False),
        ),
        patch.object(execute_mod, "_resume_agent_graph", new=resume),
    ):
        response = client.post(
            "/api/v1/agent/confirm/run-1",
            json={"confirmed": True},
        )

    assert response.status_code == 409
    resume.assert_not_awaited()


def test_job_confirm_releases_durable_claim_on_redis_conflict() -> None:
    client, _db = _client()
    release = AsyncMock(return_value=True)
    resume = AsyncMock()
    job = {
        "status": JobStatus.AWAITING_CONFIRMATION,
        "user_id": str(USER_ID),
    }

    with (
        patch(
            "src.services.agent.job_store.get_job_fresh",
            new=AsyncMock(return_value=job),
        ),
        patch(
            "src.services.agent.job_store.compare_and_set_status",
            new=AsyncMock(return_value="conflict"),
        ),
        patch.object(
            execute_mod,
            "claim_awaiting_run_for_confirmation",
            new=AsyncMock(return_value=True),
        ),
        patch.object(execute_mod, "release_confirmation_claim", new=release),
        patch.object(execute_mod, "_resume_agent_graph", new=resume),
    ):
        response = client.post(
            "/api/v1/agent/confirm/run-1",
            json={"confirmed": True},
        )

    assert response.status_code == 409
    release.assert_awaited_once_with(
        _db,
        "run-1",
        organization_id=ORG_ID,
        user_id=USER_ID,
    )
    resume.assert_not_awaited()

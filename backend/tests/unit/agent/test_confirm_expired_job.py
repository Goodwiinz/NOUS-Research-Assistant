"""R7-L13: a confirm whose job payload has expired must fail closed.

Redis holds the original request; PostgreSQL's ``agent_runs`` projection holds
only status + ownership. When the payload aged out, /confirm used the
projection to admit the confirm anyway, claimed the run (awaiting ->
running), and handed ``_resume_agent_graph`` no thread_id — which fell back to
``thread_id=job_id``, read an empty checkpoint, skipped the ownership and
interrupt-consumed guards, and failed the run. The turn was unrecoverable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.agent import execute as execute_mod
from src.shared.enums import JobStatus
from tests.utils.agent_thread_access import editable_thread_getter

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _allow_durable_thread_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.services.threads.workspace_access.get_thread",
        editable_thread_getter(),
    )


@pytest.fixture
def user() -> Mock:
    u = Mock()
    u.id = uuid4()
    u.organization_id = uuid4()
    u.is_active = True
    return u


@pytest.fixture
def client(user: Mock) -> Iterator[TestClient]:
    from src.core.database import get_db
    from src.core.dependencies import get_current_user

    db = AsyncMock()
    db.rollback = AsyncMock()

    app = FastAPI()
    app.include_router(execute_mod.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    @asynccontextmanager
    async def _no_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app.router.lifespan_context = _no_lifespan
    with TestClient(app) as c:
        yield c


def test_expired_job_is_refused_without_claiming_the_run(
    client: TestClient, user: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = str(uuid4())

    # Redis lost the record; only the projection survives.
    monkeypatch.setattr(
        "src.services.agent.job_store.get_job_fresh", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "src.services.agent.agent_run_service.get_run",
        AsyncMock(
            return_value=SimpleNamespace(
                status=JobStatus.AWAITING_CONFIRMATION.value,
                user_id=user.id,
            )
        ),
    )
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(execute_mod, "claim_awaiting_run_for_confirmation", claim)

    response = client.post(
        f"/api/v1/agent/confirm/{job_id}",
        json={"approval_id": "a" * 64, "confirmed": True},
    )

    assert response.status_code == 409
    assert "expired" in response.json()["detail"].lower()
    # the run must stay awaiting_confirmation
    claim.assert_not_awaited()


def test_live_job_payload_still_confirms(
    client: TestClient, user: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the guard must only fire when the payload is actually gone."""
    job_id = str(uuid4())
    monkeypatch.setattr(
        "src.services.agent.job_store.get_job_fresh",
        AsyncMock(
            return_value={
                "status": JobStatus.AWAITING_CONFIRMATION,
                "user_id": str(user.id),
                "request": {
                    "thread_id": str(uuid4()),
                    "messages": [{"role": "user", "content": "confirm ingest"}],
                },
            }
        ),
    )
    monkeypatch.setattr(
        execute_mod,
        "claim_awaiting_run_for_confirmation",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.services.agent.job_store.compare_and_set_status",
        AsyncMock(return_value="ok"),
    )
    monkeypatch.setattr(execute_mod, "_resume_agent_graph", MagicMock())

    response = client.post(
        f"/api/v1/agent/confirm/{job_id}",
        json={"approval_id": "a" * 64, "confirmed": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == JobStatus.RUNNING


# ---------------------------------------------------------------------------
# R2-H3 follow-up: rebuild the lost payload from durable state
# ---------------------------------------------------------------------------


def _parked_run(user: Mock, thread_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        status=JobStatus.AWAITING_CONFIRMATION.value,
        user_id=user.id,
        thread_id=thread_id,
    )


def _recovered_payload(thread_id: str) -> dict:
    return {
        "thread_id": thread_id,
        "messages": [{"role": "user", "content": "ingest the paper"}],
        "page_context": {"type": "chat"},
        "model": "",
    }


def _redis_lost(monkeypatch: pytest.MonkeyPatch, run: SimpleNamespace) -> None:
    monkeypatch.setattr(
        "src.services.agent.job_store.get_job_fresh", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "src.services.agent.agent_run_service.get_run", AsyncMock(return_value=run)
    )
    monkeypatch.setattr(
        "src.services.agent.job_store.compare_and_set_status",
        AsyncMock(return_value="missing"),
    )


def test_lost_payload_is_rebuilt_from_durable_state_and_resumes(
    client: TestClient, user: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Redis flush inside the 24h window must not strand the run: the
    thread id comes from the tenant-filtered agent_runs row, the payload from
    the checkpoint, and the mirror is restored before the resume runs."""
    job_id, thread_id = str(uuid4()), str(uuid4())
    approval_id = "b" * 64
    _redis_lost(monkeypatch, _parked_run(user, thread_id))
    recover = AsyncMock(return_value=_recovered_payload(thread_id))
    monkeypatch.setattr(execute_mod, "recover_confirm_request_payload", recover)
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(execute_mod, "claim_awaiting_run_for_confirmation", claim)
    restore = AsyncMock(return_value="claimed")
    monkeypatch.setattr(
        "src.services.agent.job_store.restore_confirmation_mirror", restore
    )
    resume = MagicMock()
    monkeypatch.setattr(execute_mod, "_resume_agent_graph", resume)

    response = client.post(
        f"/api/v1/agent/confirm/{job_id}",
        json={"approval_id": approval_id, "confirmed": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == JobStatus.RUNNING
    recover.assert_awaited_once_with(
        job_id=job_id,
        thread_id=thread_id,
        approval_id=approval_id,
        current_user=user,
    )
    claim.assert_awaited_once()
    restore.assert_awaited_once()
    restored_job_id, restored = restore.await_args.args
    assert restored_job_id == job_id
    assert restored["status"] == JobStatus.RUNNING
    assert restored["user_id"] == str(user.id)
    assert restored["thread_id"] == thread_id
    assert restored["request"] == _recovered_payload(thread_id)
    resume.assert_called_once()


def test_unrecoverable_checkpoint_still_refuses_before_the_claim(
    client: TestClient, user: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checkpoint missing, owned by someone else, or its interrupt does not
    match the approval: the helper returns None and R7-L13 still holds."""
    _redis_lost(monkeypatch, _parked_run(user, str(uuid4())))
    monkeypatch.setattr(
        execute_mod, "recover_confirm_request_payload", AsyncMock(return_value=None)
    )
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(execute_mod, "claim_awaiting_run_for_confirmation", claim)

    response = client.post(
        f"/api/v1/agent/confirm/{uuid4()}",
        json={"approval_id": "a" * 64, "confirmed": True},
    )

    assert response.status_code == 409
    assert "expired" in response.json()["detail"].lower()
    claim.assert_not_awaited()


def test_threadless_run_is_never_rebuilt_from_job_id(
    client: TestClient, user: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No durable thread id → no rebuild. job_id is never used as a thread
    id (the R7-L13 fallback stays dead)."""
    _redis_lost(monkeypatch, _parked_run(user, None))
    recover = AsyncMock(return_value=_recovered_payload(str(uuid4())))
    monkeypatch.setattr(execute_mod, "recover_confirm_request_payload", recover)
    monkeypatch.setattr(execute_mod, "_resume_agent_graph", MagicMock())
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(execute_mod, "claim_awaiting_run_for_confirmation", claim)

    response = client.post(
        f"/api/v1/agent/confirm/{uuid4()}",
        json={"approval_id": "a" * 64, "confirmed": True},
    )

    assert response.status_code == 409
    recover.assert_not_awaited()
    claim.assert_not_awaited()


def test_rebuilt_confirm_still_loses_to_the_postgres_claim(
    client: TestClient, user: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Double-confirm and confirm-after-cancel: PostgreSQL refuses the claim,
    so the rebuilt payload never restores the mirror or resumes anything."""
    thread_id = str(uuid4())
    _redis_lost(monkeypatch, _parked_run(user, thread_id))
    monkeypatch.setattr(
        execute_mod,
        "recover_confirm_request_payload",
        AsyncMock(return_value=_recovered_payload(thread_id)),
    )
    monkeypatch.setattr(
        execute_mod,
        "claim_awaiting_run_for_confirmation",
        AsyncMock(return_value=False),
    )
    restore = AsyncMock(return_value="claimed")
    monkeypatch.setattr(
        "src.services.agent.job_store.restore_confirmation_mirror", restore
    )
    resume = MagicMock()
    monkeypatch.setattr(execute_mod, "_resume_agent_graph", resume)

    response = client.post(
        f"/api/v1/agent/confirm/{uuid4()}",
        json={"approval_id": "a" * 64, "confirmed": True},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Job is not awaiting confirmation"
    restore.assert_not_awaited()
    resume.assert_not_called()


def test_reappeared_mirror_backs_out_the_rebuilt_confirm(
    client: TestClient, user: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record that reappeared between the read and the restore (a delayed
    worker or Stop write) wins: release the claim, 409, no resume."""
    thread_id = str(uuid4())
    _redis_lost(monkeypatch, _parked_run(user, thread_id))
    monkeypatch.setattr(
        execute_mod,
        "recover_confirm_request_payload",
        AsyncMock(return_value=_recovered_payload(thread_id)),
    )
    monkeypatch.setattr(
        execute_mod,
        "claim_awaiting_run_for_confirmation",
        AsyncMock(return_value=True),
    )
    release = AsyncMock(return_value=True)
    monkeypatch.setattr(execute_mod, "release_confirmation_claim", release)
    monkeypatch.setattr(
        "src.services.agent.job_store.restore_confirmation_mirror",
        AsyncMock(return_value="conflict"),
    )
    resume = MagicMock()
    monkeypatch.setattr(execute_mod, "_resume_agent_graph", resume)

    response = client.post(
        f"/api/v1/agent/confirm/{uuid4()}",
        json={"approval_id": "a" * 64, "confirmed": True},
    )

    assert response.status_code == 409
    release.assert_awaited_once()
    resume.assert_not_called()

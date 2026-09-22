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

    response = client.post(f"/api/v1/agent/confirm/{job_id}", json={"confirmed": True})

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

    response = client.post(f"/api/v1/agent/confirm/{job_id}", json={"confirmed": True})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == JobStatus.RUNNING

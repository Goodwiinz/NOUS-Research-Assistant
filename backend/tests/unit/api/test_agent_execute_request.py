"""Unit tests for ``AgentExecuteRequest.model`` validation.

The allowlist is intentionally tiny: empty string falls back to the
deployment configured in ``AZURE_OPENAI_CHAT_DEPLOYMENT_NAME``, and
``model-router`` routes the request through Azure's model-router
deployment (which selects the underlying model per request). Any other
value is rejected because no other deployments are provisioned in the
Azure resource — accepting them produces a 404 ``DeploymentNotFound``
at request time.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _build(**overrides):
    """Construct an AgentExecuteRequest with sensible defaults for the field
    under test."""
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage

    payload = {
        "messages": [AgentMessage(role="user", content="hi")],
    }
    payload.update(overrides)
    return AgentExecuteRequest(**payload)


def test_default_model_is_empty_string_meaning_server_default():
    request = _build()
    assert request.model == ""


def test_model_router_passthrough_is_accepted():
    request = _build(model="model-router")
    assert request.model == "model-router"


def test_unknown_model_raises_validation_error_naming_supported_set():
    with pytest.raises(ValidationError) as excinfo:
        _build(model="gpt-7-ultra")

    detail = str(excinfo.value)
    assert "gpt-7-ultra" in detail
    assert "model-router" in detail


def test_gpt_5_mini_is_accepted():
    request = _build(model="gpt-5-mini")
    assert request.model == "gpt-5-mini"


def test_request_rejects_missing_user_message():
    with pytest.raises(ValidationError) as excinfo:
        _build(messages=[])
    assert excinfo.value.errors()[0]["loc"] == ("messages",)


def test_request_rejects_non_uuid_thread_id():
    with pytest.raises(ValidationError) as excinfo:
        _build(thread_id="not-a-uuid")
    assert excinfo.value.errors()[0]["loc"] == ("thread_id",)


def test_page_context_accepts_only_uuid_workspace_id():
    workspace_id = "11111111-1111-1111-1111-111111111111"
    request = _build(page_context={"workspace_id": workspace_id})
    assert str(request.page_context.workspace_id) == workspace_id

    with pytest.raises(ValidationError) as excinfo:
        _build(page_context={"workspace_id": "not-a-uuid"})
    assert excinfo.value.errors()[0]["loc"] == ("page_context", "workspace_id")


@pytest.mark.parametrize("backend", ["background", "celery", "celery-fallback"])
async def test_execute_rejects_codex_before_side_effects(monkeypatch, backend):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from fastapi import BackgroundTasks, HTTPException

    from src.api.agent import execute

    resolve = AsyncMock(
        side_effect=AssertionError("thread created before provider rejection")
    )
    monkeypatch.setattr(execute, "_resolve_thread", resolve)
    rate_limit = AsyncMock()
    monkeypatch.setattr(execute, "_enforce_rate_limit", rate_limit)
    monkeypatch.setattr(execute, "_resolve_dispatch_backend", lambda: backend)
    req = _build(
        execution_provider="codex",
        device_id=uuid4(),
        workspace_id=uuid4(),
        thread_id=str(uuid4()),
    )
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        await execute.execute_agent(req, tasks, SimpleNamespace(id=uuid4()), None)
    assert exc.value.status_code == 422
    assert exc.value.detail == "Local Codex requires /api/v1/agent/stream."
    resolve.assert_not_awaited()
    rate_limit.assert_not_awaited()
    assert tasks.tasks == []


@pytest.mark.parametrize("invalid_request", ["codex", "request-validation"])
def test_execute_422_contract_matches_application_handlers(
    monkeypatch, invalid_request
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from fastapi import FastAPI, HTTPException
    from fastapi.exceptions import RequestValidationError
    from fastapi.testclient import TestClient
    from jsonschema import validate

    from src.api.agent import execute
    from src.core.database import get_db
    from src.core.dependencies import get_current_user
    from src.main import http_exception_handler, validation_exception_handler

    application = FastAPI()
    application.add_exception_handler(HTTPException, http_exception_handler)
    application.add_exception_handler(
        RequestValidationError, validation_exception_handler
    )
    application.include_router(execute.router)
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid4()
    )
    application.dependency_overrides[get_db] = lambda: None
    monkeypatch.setattr(execute, "_enforce_rate_limit", AsyncMock())
    payload = {"messages": [{"role": "user", "content": "private input"}]}
    if invalid_request == "codex":
        payload.update(
            execution_provider="codex",
            device_id=str(uuid4()),
            workspace_id=str(uuid4()),
            thread_id=str(uuid4()),
        )
    else:
        payload["model"] = "unsupported-private-model"
    with TestClient(application) as client:
        response = client.post("/api/v1/agent/execute", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["type"] == (
        "http_error" if invalid_request == "codex" else "validation_error"
    )
    document = application.openapi()
    schema = document["paths"]["/api/v1/agent/execute"]["post"]["responses"]["422"][
        "content"
    ]["application/json"]["schema"]
    validate(body, {**schema, "components": document["components"]})
    assert "private input" not in response.text
    assert "unsupported-private-model" not in response.text

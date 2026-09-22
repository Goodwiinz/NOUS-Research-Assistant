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

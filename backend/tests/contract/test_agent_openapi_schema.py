"""Semantic contract checks for the agent request and streaming APIs."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.api.agent.execute import StreamConfirmRequest
from src.main import app
from src.services.agent._uuid import UUID_STRICT_PATTERN
from src.services.agent.schemas import SUPPORTED_MODELS, AgentExecuteRequest


def test_execute_request_schema_matches_runtime_constraints() -> None:
    properties = AgentExecuteRequest.model_json_schema()["properties"]

    messages = properties["messages"]
    assert messages["minItems"] == 1
    assert messages["minContains"] == 1
    assert messages["contains"]["properties"]["role"]["const"] == "user"

    assert set(properties["model"]["enum"]) == SUPPORTED_MODELS
    thread_schema = next(
        schema
        for schema in properties["thread_id"]["anyOf"]
        if schema.get("type") == "string"
    )
    assert thread_schema["pattern"] == UUID_STRICT_PATTERN


def test_stream_confirm_uses_the_same_thread_id_contract() -> None:
    thread_schema = StreamConfirmRequest.model_json_schema()["properties"]["thread_id"]
    assert thread_schema["pattern"] == UUID_STRICT_PATTERN
    assert StreamConfirmRequest(
        thread_id=str(uuid4()), confirmed=True, approval_id="a" * 64
    )
    with pytest.raises(ValidationError):
        StreamConfirmRequest(
            thread_id="not-a-uuid", confirmed=True, approval_id="a" * 64
        )


def test_stream_success_responses_are_declared_as_sse() -> None:
    paths = app.openapi()["paths"]
    for path in ("/api/v1/agent/stream", "/api/v1/agent/stream/confirm"):
        content = paths[path]["post"]["responses"]["200"]["content"]
        assert set(content) == {"text/event-stream"}
        assert content["text/event-stream"]["schema"] == {"type": "string"}
    assert "429" in paths["/api/v1/agent/stream"]["post"]["responses"]


def test_cancel_documents_every_runtime_status() -> None:
    responses = app.openapi()["paths"]["/api/v1/agent/stream/cancel/{thread_id}"][
        "post"
    ]["responses"]
    assert set(responses) == {"204", "404", "409", "422", "503"}

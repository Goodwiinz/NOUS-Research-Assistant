"""Wire contract tests; canonical persistence is tested in the service suite."""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.compat import alphaxiv_history as route
from src.models.user import User
from src.schemas.alphaxiv_history import (
    AlphaXivChatSummary,
    AlphaXivHistoryMessage,
    AlphaXivSelectedMessage,
    AlphaXivSelection,
)
from src.services.compat.alphaxiv import CompatibilityError

ACTOR = UUID("10000000-0000-4000-8000-000000000001")
PAPER = UUID("10000000-0000-4000-8000-000000000002")
CHAT = UUID("10000000-0000-4000-8000-000000000003")
MESSAGE = UUID("10000000-0000-4000-8000-000000000004")
WHEN = datetime(2026, 10, 13, tzinfo=timezone.utc)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(route.router)
    user = cast(User, SimpleNamespace(id=ACTOR, organization_id=PAPER))
    app.dependency_overrides[route.get_current_user] = lambda: user
    monkeypatch.setattr(route.execute, "_enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(route, "AsyncSessionLocal", lambda: AsyncMock())
    monkeypatch.setattr(
        route.history,
        "list_paper_chats",
        AsyncMock(
            return_value=[
                AlphaXivChatSummary(id=CHAT, title="PDF", newest_message=WHEN)
            ]
        ),
    )
    monkeypatch.setattr(
        route.history,
        "get_chat_messages",
        AsyncMock(
            return_value=[
                AlphaXivHistoryMessage(
                    id=MESSAGE,
                    parent_message_id=None,
                    selected_at=WHEN,
                    type="input_text",
                    content="Explain this PDF",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        route.history,
        "select_chat_message",
        AsyncMock(
            return_value=AlphaXivSelection(
                selections=[AlphaXivSelectedMessage(id=MESSAGE, selected_at=WHEN)]
            )
        ),
    )
    monkeypatch.setattr(
        route.history, "get_context_window_usage", AsyncMock(return_value=None)
    )
    with TestClient(app) as connection:
        yield connection


def test_recent_list_uses_observed_query_and_direct_array(client: TestClient) -> None:
    response = client.get(f"/assistant/v2?variant=paper&paperVersion={PAPER}")
    assert response.status_code == 200
    assert response.json() == [
        {"id": str(CHAT), "title": "PDF", "newestMessage": "2026-10-13T00:00:00Z"}
    ]
    call = cast(AsyncMock, route.history.list_paper_chats).await_args
    assert call is not None and call.kwargs == {"actor_id": ACTOR, "paper_id": PAPER}


def test_message_fields_use_recovered_camel_case(client: TestClient) -> None:
    response = client.get(f"/assistant/v2/{CHAT}/messages")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(MESSAGE),
            "parentMessageId": None,
            "selectedAt": "2026-10-13T00:00:00.000000Z",
            "type": "input_text",
            "content": "Explain this PDF",
            "kind": None,
            "feedbackType": None,
        }
    ]


def test_selection_needs_no_invented_request_body(client: TestClient) -> None:
    response = client.post(f"/assistant/v2/{CHAT}/messages/{MESSAGE}/select")
    assert response.status_code == 200
    assert response.json() == {
        "selections": [
            {"id": str(MESSAGE), "selectedAt": "2026-10-13T00:00:00.000000Z"}
        ]
    }
    call = cast(AsyncMock, route.history.select_chat_message).await_args
    assert call is not None and call.kwargs == {
        "actor_id": ACTOR,
        "chat_id": CHAT,
        "message_id": MESSAGE,
    }


def test_missing_current_context_measurement_is_json_null(client: TestClient) -> None:
    response = client.get(f"/assistant/v2/{CHAT}/context-window-usage")
    assert response.status_code == 200
    assert response.content == b"null"
    assert response.headers["content-type"] == "application/json"


def test_selection_timestamp_wire_normalizes_offsets_and_preserves_precision() -> None:
    offset = datetime(2026, 10, 13, 4, 0, 0, 1, tzinfo=timezone(timedelta(hours=4)))
    selection = AlphaXivSelectedMessage(id=MESSAGE, selected_at=offset)
    message = AlphaXivHistoryMessage(
        id=MESSAGE,
        parent_message_id=None,
        selected_at=offset,
        type="input_text",
        content="Question",
    )
    assert (
        selection.model_dump(mode="json", by_alias=True)["selectedAt"]
        == "2026-10-13T00:00:00.000001Z"
    )
    assert (
        message.model_dump(mode="json", by_alias=True)["selectedAt"]
        == "2026-10-13T00:00:00.000001Z"
    )


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?variant=paper",
        f"?variant=homepage&paperVersion={PAPER}",
        "?variant=paper&paperVersion=not-a-uuid",
    ],
)
def test_recent_list_rejects_unsupported_or_incomplete_scope(
    client: TestClient, query: str
) -> None:
    assert client.get(f"/assistant/v2{query}").status_code == 422
    cast(AsyncMock, route.history.list_paper_chats).assert_not_awaited()


@pytest.mark.parametrize(
    "method,path,service_name",
    [
        (
            "get",
            f"/assistant/v2?variant=paper&paperVersion={PAPER}",
            "list_paper_chats",
        ),
        ("get", f"/assistant/v2/{CHAT}/messages", "get_chat_messages"),
        (
            "post",
            f"/assistant/v2/{CHAT}/messages/{MESSAGE}/select",
            "select_chat_message",
        ),
        (
            "get",
            f"/assistant/v2/{CHAT}/context-window-usage",
            "get_context_window_usage",
        ),
    ],
)
def test_all_history_routes_require_auth_and_native_rate_limit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    service_name: str,
) -> None:
    limiter = cast(AsyncMock, route.execute._enforce_rate_limit)
    limiter.side_effect = HTTPException(429, "Too many requests")
    assert client.request(method, path).status_code == 429
    limiter.assert_awaited_once_with(
        route.execute._agent_read_rate_limiter,
        str(ACTOR),
        route.execute._AGENT_READ_PREFIX,
    )
    cast(AsyncMock, getattr(route.history, service_name)).assert_not_awaited()
    app = cast(FastAPI, client.app)
    app.dependency_overrides.clear()
    limiter.reset_mock()
    assert client.request(method, path).status_code in (401, 403)
    limiter.assert_not_awaited()


@pytest.mark.parametrize("status", [404, 409, 503])
def test_service_safe_error_preserves_http_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(
        route.history,
        "get_chat_messages",
        AsyncMock(
            side_effect=CompatibilityError(status, "Chat history is unavailable")
        ),
    )
    response = client.get(f"/assistant/v2/{CHAT}/messages")
    assert response.status_code == status
    assert response.json() == {"detail": "Chat history is unavailable"}


def test_openapi_describes_arrays_aliases_and_nullable_usage(
    client: TestClient,
) -> None:
    schema: dict[str, Any] = client.get("/openapi.json").json()
    properties = schema["components"]["schemas"]["AlphaXivHistoryMessage"]["properties"]
    assert "parentMessageId" in properties and "parent_message_id" not in properties
    assert properties["feedbackType"]["type"] == "null"
    assert (
        schema["paths"]["/assistant/v2/{chat_id}/messages"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["type"]
        == "array"
    )
    usage = schema["paths"]["/assistant/v2/{chat_id}/context-window-usage"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    assert {"type": "null"} in usage["anyOf"]

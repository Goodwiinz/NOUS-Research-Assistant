"""Message presenters preserve persisted identity and terminal state.

The response schema gives both fields nullable defaults, so omitting either
constructor keyword silently turns a real persisted value into ``None``.  The
tests below exercise the ORM -> presenter -> JSON boundary with literal UUIDs;
removing either keyword from ``_message_to_response`` must fail here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from src.api.threads.threads import _format_message_response
from src.api.threads.workspace_routes.presenters import (
    _message_to_response,
    _thread_to_detail_response,
)
from src.models.chat_message import ChatMessage, MessageRole
from src.models.citation import Citation
from src.models.message_attachment import MessageAttachment

pytestmark = pytest.mark.unit

DATABASE_USER_ID = UUID("10000000-0000-0000-0000-000000000001")
DATABASE_ASSISTANT_ID = UUID("20000000-0000-0000-0000-000000000002")
CLIENT_USER_ID = UUID("30000000-0000-0000-0000-000000000003")
CLIENT_ASSISTANT_ID = UUID("40000000-0000-0000-0000-000000000004")
THREAD_ID = UUID("50000000-0000-0000-0000-000000000005")
USER_ID = UUID("60000000-0000-0000-0000-000000000006")
CREATED_AT = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def _message(
    *,
    database_id: UUID,
    role: MessageRole,
    client_message_id: UUID | None,
    stopped: bool | None,
) -> ChatMessage:
    return ChatMessage(
        id=database_id,
        thread_id=THREAD_ID,
        user_id=USER_ID if role is MessageRole.USER else None,
        role=role,
        content=f"{role.value} content",
        token_count=7,
        latency_ms=1250,
        client_message_id=client_message_id,
        stopped=stopped,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


@pytest.mark.parametrize(
    ("role", "database_id", "client_id", "stopped", "expected_client_json"),
    [
        (
            MessageRole.USER,
            DATABASE_USER_ID,
            CLIENT_USER_ID,
            True,
            "30000000-0000-0000-0000-000000000003",
        ),
        (
            MessageRole.ASSISTANT,
            DATABASE_ASSISTANT_ID,
            CLIENT_ASSISTANT_ID,
            False,
            "40000000-0000-0000-0000-000000000004",
        ),
    ],
)
def test_message_response_preserves_distinct_client_identity_and_stopped_state(
    role: MessageRole,
    database_id: UUID,
    client_id: UUID,
    stopped: bool,
    expected_client_json: str,
) -> None:
    """Dropping either presenter keyword must lose a literal persisted value.

    Mutation proof: disable presenters.py:209 or presenters.py:216, then run
    ``pytest -q backend/tests/unit/api/test_message_presenter_identity.py
    -k preserves_distinct`` from the repository root.
    """
    message = _message(
        database_id=database_id,
        role=role,
        client_message_id=client_id,
        stopped=stopped,
    )

    response = _message_to_response(message)

    assert message.id != message.client_message_id
    assert response.client_message_id == message.client_message_id
    assert response.stopped == message.stopped
    payload = response.model_dump(mode="json")
    assert payload["id"] == str(database_id)
    assert payload["client_message_id"] == expected_client_json
    assert payload["stopped"] is stopped


def test_message_response_preserves_legacy_null_identity_and_stopped_state() -> None:
    """Legacy nullable values remain JSON null instead of becoming strings."""
    message = _message(
        database_id=DATABASE_ASSISTANT_ID,
        role=MessageRole.ASSISTANT,
        client_message_id=None,
        stopped=None,
    )

    response = _message_to_response(message)

    assert response.client_message_id is None
    assert response.stopped is None
    payload = response.model_dump(mode="json")
    assert payload["client_message_id"] is None
    assert payload["stopped"] is None


def test_deprecated_thread_route_formatter_preserves_message_identity() -> None:
    """The separate /api/v2/threads formatter cannot drop runtime identity.

    Mutation proof: disable threads.py:1069, then run ``pytest -q
    backend/tests/unit/api/test_message_presenter_identity.py -k
    deprecated_thread_route_formatter`` from the repository root.
    """
    message = _message(
        database_id=DATABASE_ASSISTANT_ID,
        role=MessageRole.ASSISTANT,
        client_message_id=CLIENT_ASSISTANT_ID,
        stopped=True,
    )

    response = _format_message_response(message)

    assert response.client_message_id == CLIENT_ASSISTANT_ID
    assert response.stopped is True
    payload = response.model_dump(mode="json")
    assert payload["client_message_id"] == "40000000-0000-0000-0000-000000000004"
    assert payload["stopped"] is True


def test_thread_detail_uses_message_presenter_without_losing_rich_provenance() -> None:
    """Thread-detail nesting keeps identity, state, and existing provenance."""
    message = _message(
        database_id=DATABASE_ASSISTANT_ID,
        role=MessageRole.ASSISTANT,
        client_message_id=CLIENT_ASSISTANT_ID,
        stopped=True,
    )
    message.tool_executions = [
        {
            "id": "tool-1",
            "tool_name": "search_documents",
            "args": {"query": "identity"},
            "status": "completed",
        }
    ]
    message.plan = [
        {
            "step": 1,
            "description": "Search documents",
            "tool": "search_documents",
            "args_hint": {},
            "depends_on": [],
        }
    ]
    message.plan_reasoning = "Search before answering."
    message.token_usage = {"input_tokens": 11, "output_tokens": 7}
    message.progress_steps = [{"phase": "writing", "detail": "Drafting"}]
    message.citations = [
        Citation(
            id=UUID("70000000-0000-0000-0000-000000000007"),
            external_reference_id="arXiv:2609.00001",
            source_position=1,
            snippet="Identity provenance.",
            document_title="Stable Message Identity",
            document_type="paper",
            created_at=CREATED_AT,
            updated_at=CREATED_AT,
        )
    ]
    message.attachments = [
        MessageAttachment(
            id=UUID("90000000-0000-0000-0000-000000000009"),
            document_id=UUID("a0000000-0000-0000-0000-00000000000a"),
            display_name="identity-notes.pdf",
            thumbnail_url=None,
            created_at=CREATED_AT,
            updated_at=CREATED_AT,
        )
    ]
    message.is_deleted = False
    message.superseded_by_message_id = None
    thread = SimpleNamespace(
        id=THREAD_ID,
        conversation_id=UUID("80000000-0000-0000-0000-000000000008"),
        title="Identity thread",
        summary=None,
        status=None,
        last_message_at=CREATED_AT,
        message_count=1,
        token_count=7,
        created_by_id=USER_ID,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        source_project_id=None,
        messages=[message],
    )

    nested = _thread_to_detail_response(thread).messages[0]

    assert nested.client_message_id == CLIENT_ASSISTANT_ID
    assert nested.stopped is True
    assert nested.plan_reasoning == "Search before answering."
    assert nested.token_usage == {"input_tokens": 11, "output_tokens": 7}
    assert nested.progress_steps == [{"phase": "writing", "detail": "Drafting"}]
    assert nested.citations[0].document_title == "Stable Message Identity"
    assert nested.attachments[0].display_name == "identity-notes.pdf"

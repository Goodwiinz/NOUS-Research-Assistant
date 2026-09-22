"""Integration tests for the split persistence helpers in jobs.py.

The two helpers under test:

* ``_persist_user_message`` — INSERT ... ON CONFLICT DO NOTHING against the
  partial unique index on ``chat_messages (thread_id, client_message_id)
  WHERE client_message_id IS NOT NULL AND role = 'user'``. Idempotent: a
  retry with the same ``client_message_id`` must NOT raise and must NOT
  produce a duplicate row.
* ``_persist_assistant_message`` — straightforward insert + thread bookkeeping
  (``message_count`` += 1, ``last_message_at`` = now).

Both helpers commit independently. These tests exercise that behavior
against the local Postgres test DB (the partial unique index is a Postgres
feature, so SQLite cannot model it).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from src.api.agent.execute import AgentExecuteRequest, AgentMessage
from src.api.threads.threads import _format_message_response
from src.models.chat_message import ChatMessage, MessageRole
from src.models.document import Document, DocumentType
from src.services.agent.agent_execution_service import (
    _persist_assistant_message,
    _persist_user_message,
)

pytestmark = pytest.mark.integration


async def test_duplicate_user_turn_is_idempotent(
    db_session, thread_factory, user_factory
):
    thread = await thread_factory()
    user = await user_factory()
    cmid = uuid4()
    req = AgentExecuteRequest(
        messages=[
            AgentMessage(role="user", content="hello", client_message_id=str(cmid))
        ],
        thread_id=str(thread.id),
    )

    inserted_a = await _persist_user_message(db_session, user, req)
    inserted_b = await _persist_user_message(db_session, user, req)  # retry
    assert inserted_a is True
    assert inserted_b is False

    count = await db_session.scalar(
        select(func.count(ChatMessage.id)).where(
            ChatMessage.thread_id == thread.id,
            ChatMessage.role == MessageRole.USER,
        )
    )
    assert count == 1

    # Track for cleanup
    row_ids = (
        (
            await db_session.execute(
                select(ChatMessage.id).where(ChatMessage.thread_id == thread.id)
            )
        )
        .scalars()
        .all()
    )
    db_session.info["_created"]["chat_messages"].extend(row_ids)


async def test_user_then_assistant_round_trip(db_session, thread_factory, user_factory):
    thread = await thread_factory()
    user = await user_factory()
    cmid = uuid4()
    req = AgentExecuteRequest(
        messages=[
            AgentMessage(role="user", content="ping", client_message_id=str(cmid))
        ],
        thread_id=str(thread.id),
    )

    await _persist_user_message(db_session, user, req)
    await _persist_assistant_message(
        db_session,
        thread_id=str(thread.id),
        content="pong",
        model_name="gpt-5-mini",
        tool_executions_out=None,
    )

    rows = (
        await db_session.execute(
            select(ChatMessage.role)
            .where(ChatMessage.thread_id == thread.id)
            .order_by(ChatMessage.created_at)
        )
    ).all()
    assert [r[0] for r in rows] == [MessageRole.USER, MessageRole.ASSISTANT]

    # Track for cleanup
    ids = (
        (
            await db_session.execute(
                select(ChatMessage.id).where(ChatMessage.thread_id == thread.id)
            )
        )
        .scalars()
        .all()
    )
    db_session.info["_created"]["chat_messages"].extend(ids)


async def test_user_message_no_op_without_thread_id(db_session, user_factory):
    """When request.thread_id is None, the helper must be a no-op and return False."""
    user = await user_factory()
    req = AgentExecuteRequest(
        messages=[
            AgentMessage(role="user", content="orphan", client_message_id=str(uuid4()))
        ],
        thread_id=None,
    )
    inserted = await _persist_user_message(db_session, user, req)
    assert inserted is False


async def test_assistant_plan_and_token_usage_round_trip(db_session, thread_factory):
    """Plan + per-turn token usage persist on the assistant row and read back
    intact, so a page reload can rehydrate turn provenance from the DB."""
    from uuid import UUID

    thread = await thread_factory()
    plan = [
        {
            "step": 1,
            "description": "Search documents for transformers",
            "tool": "search_documents",
            "args_hint": {"query": "transformers"},
            "depends_on": [],
        },
        {
            "step": 2,
            "description": "Summarize findings",
            "tool": "summarize",
            "args_hint": {},
            "depends_on": [1],
        },
    ]
    usage = {"input_tokens": 1200, "output_tokens": 340}
    reasoning = "Search documents first, then summarize the findings."
    provider_summary = "I compared the strongest retrieved sources."
    progress = [
        {"phase": "accepted", "detail": "Request accepted"},
        {"phase": "writing", "detail": "Drafting the response"},
    ]

    msg_id = await _persist_assistant_message(
        db_session,
        thread_id=str(thread.id),
        content="answer",
        model_name="gpt-5-mini",
        tool_executions_out=None,
        plan=plan,
        plan_reasoning=reasoning,
        reasoning_summary=provider_summary,
        token_usage=usage,
        progress_steps=progress,
    )

    row = await db_session.scalar(
        select(ChatMessage)
        .where(ChatMessage.id == UUID(msg_id))
        .options(
            selectinload(ChatMessage.citations),
            selectinload(ChatMessage.attachments),
        )
    )
    assert row is not None
    assert row.plan == plan
    assert row.plan_reasoning == reasoning
    assert row.reasoning_summary == provider_summary
    assert row.token_usage == usage
    assert row.progress_steps == progress

    # Full round trip: persist kwarg -> column -> read serializer. Same
    # formatter threads.py's list-messages endpoint uses.
    response = _format_message_response(row)
    assert response.plan_reasoning == reasoning
    assert response.reasoning_summary == provider_summary
    assert response.progress_steps == progress

    db_session.info["_created"]["chat_messages"].append(row.id)


async def test_assistant_without_provenance_persists_null_columns(
    db_session, thread_factory
):
    """Rows persisted without plan/usage (legacy path, stop path) stay NULL."""
    from uuid import UUID

    thread = await thread_factory()
    msg_id = await _persist_assistant_message(
        db_session,
        thread_id=str(thread.id),
        content="answer",
        model_name="gpt-5-mini",
        tool_executions_out=None,
    )

    row = await db_session.get(ChatMessage, UUID(msg_id))
    assert row is not None
    assert row.plan is None
    assert row.plan_reasoning is None
    assert row.reasoning_summary is None
    assert row.token_usage is None
    assert row.progress_steps is None

    db_session.info["_created"]["chat_messages"].append(row.id)


async def test_stopped_assistant_citations_round_trip_in_source_order_with_locators(
    db_session, thread_factory, user_factory
):
    """A partial-stop row reloads canonical [Doc N] order and contents."""
    from uuid import UUID

    thread = await thread_factory()
    user = await user_factory()
    documents = [
        Document(
            title=title,
            filename=f"{title.lower()}.txt",
            file_path=f"/test/{title.lower()}.txt",
            file_size_bytes=10,
            mime_type="text/plain",
            document_type=DocumentType.TEXT,
            organization_id=user.organization_id,
            uploaded_by_user_id=user.id,
        )
        for title in ("First", "Second")
    ]
    db_session.add_all(documents)
    await db_session.commit()
    first_document_id, second_document_id = (document.id for document in documents)
    message_id = await _persist_assistant_message(
        db_session,
        thread_id=str(thread.id),
        content="Answer [Doc 1] and [Doc 2].",
        model_name="gpt-5-mini",
        tool_executions_out=None,
        stopped=True,
        retrieved_contexts=[
            {
                "document_id": str(first_document_id),
                "title": "First",
                "content": "first evidence",
                "score": 0.9,
                "chunk_id": "first-chunk",
                "chunk_index": 7,
                "page_number": 3,
            },
            {
                "document_id": str(second_document_id),
                "title": "Second",
                "content": "second evidence",
                "score": 0.8,
                "chunk_id": "second-chunk",
                "chunk_index": 8,
                "page_number": 4,
            },
        ],
    )

    row = await db_session.scalar(
        select(ChatMessage)
        .where(ChatMessage.id == UUID(message_id))
        .options(
            selectinload(ChatMessage.citations),
            selectinload(ChatMessage.attachments),
        )
    )
    assert row is not None
    assert row.stopped is True
    assert [citation.source_position for citation in row.citations] == [1, 2]
    assert [citation.document_title for citation in row.citations] == [
        "First",
        "Second",
    ]
    assert [citation.snippet for citation in row.citations] == [
        "first evidence",
        "second evidence",
    ]
    assert [citation.chunk_id for citation in row.citations] == [
        "first-chunk",
        "second-chunk",
    ]

    response = _format_message_response(row)
    assert [citation.source_position for citation in response.citations] == [1, 2]
    assert [citation.chunk_id for citation in response.citations] == [
        "first-chunk",
        "second-chunk",
    ]
    assert [citation.page_number for citation in response.citations] == [3, 4]

    # Documents are local to this regression and not tracked by the shared
    # factory cleanup, so remove this test's dependent rows explicitly.
    await db_session.delete(row)
    await db_session.flush()
    for document in documents:
        await db_session.delete(document)
    await db_session.commit()

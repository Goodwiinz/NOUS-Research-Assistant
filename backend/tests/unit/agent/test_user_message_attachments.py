"""Linking uploaded documents to the user turn the agent stream persists.

The composer uploads files to /documents and sends the resulting ids as
``attachment_ids``; this is where they become MessageAttachment rows. The read
side (eager-load, presenter, runtime mapping, renderer) already existed — only
the write was missing, so an attached file showed as a composer chip and then
vanished.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.services.agent.agent_execution_service import _persist_user_message


def _user() -> Any:
    # Annotated Any, not User: these stand in for ORM objects the function only
    # reads attributes off, and the blocking mypy lane rejects a SimpleNamespace
    # passed to a User-typed parameter.
    return SimpleNamespace(id=uuid4())


def _request(attachment_ids: Any = None, thread_id: Any = None) -> Any:
    return SimpleNamespace(
        thread_id=thread_id or str(uuid4()),
        messages=[
            SimpleNamespace(
                role="user", content="see attached", client_message_id=uuid4()
            )
        ],
        supersedes_client_message_id=None,
        attachment_ids=attachment_ids,
    )


def _db(returned_row_id: Any) -> Any:
    """Lock a linear thread, then return the requested INSERT result."""
    thread_result = MagicMock()
    thread_result.scalar_one_or_none.return_value = SimpleNamespace(
        message_branching=False
    )
    result = MagicMock(rowcount=1 if returned_row_id else 0)
    result.scalar_one_or_none.return_value = returned_row_id
    return SimpleNamespace(
        execute=AsyncMock(side_effect=[thread_result, result], return_value=result),
        commit=AsyncMock(),
        get=AsyncMock(return_value=None),
        add=MagicMock(),
    )


@pytest.mark.asyncio
async def test_attaches_owned_documents_to_the_inserted_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_id, doc_a, doc_b = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(
        "src.services.threads.workspace_access.filter_owned_document_ids",
        AsyncMock(return_value=[doc_a, doc_b]),
    )
    db = _db(row_id)

    inserted = await _persist_user_message(db, _user(), _request([doc_a, doc_b]))

    assert inserted is True
    attached = [c.args[0] for c in db.add.call_args_list]
    assert {a.document_id for a in attached} == {doc_a, doc_b}
    assert {a.message_id for a in attached} == {row_id}


@pytest.mark.asyncio
async def test_drops_documents_the_caller_does_not_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A guessed foreign UUID must not attach: an unscoped attach leaks the
    # document's title and mime into the thread through the attachment
    # response. The owned half of a mixed batch still lands.
    row_id, mine, theirs = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(
        "src.services.threads.workspace_access.filter_owned_document_ids",
        AsyncMock(return_value=[mine]),
    )
    db = _db(row_id)

    await _persist_user_message(db, _user(), _request([mine, theirs]))

    attached = [c.args[0] for c in db.add.call_args_list]
    assert [a.document_id for a in attached] == [mine]


@pytest.mark.asyncio
async def test_a_deduped_retry_attaches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An SSE retry re-sends the same turn; the insert dedups on
    # (thread_id, client_message_id) and RETURNING yields no row. Attaching
    # again would double every attachment on the turn that already owns them.
    doc = uuid4()
    monkeypatch.setattr(
        "src.services.threads.workspace_access.filter_owned_document_ids",
        AsyncMock(return_value=[doc]),
    )
    db = _db(None)

    inserted = await _persist_user_message(db, _user(), _request([doc]))

    assert inserted is False
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_turn_without_attachments_keeps_the_rowcount_path() -> None:
    # RETURNING is added only where a row id is needed. A plain turn must stay
    # on rowcount so the hot path is unchanged.
    db = _db(None)
    db.execute.return_value.rowcount = 1

    inserted = await _persist_user_message(db, _user(), _request(None))

    assert inserted is True
    assert "RETURNING" not in str(db.execute.await_args.args[0]).upper()
    db.add.assert_not_called()

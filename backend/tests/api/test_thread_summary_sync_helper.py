"""Regression: the /threads/{id}/summarize HTTP path must drive the
summarization service with a SYNC session.

The service uses the sync SQLAlchemy API (db.query / db.commit) and is shared
with the Celery task. The endpoint previously handed it the request's
AsyncSession, which has no .query → AttributeError → 500 on every call. The fix
runs `_run_thread_summary_sync` in a worker thread with its own sync
SessionLocal + asyncio.run (mirroring Celery). These tests pin that contract.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.api.threads.threads import _run_thread_summary_sync


@pytest.mark.unit
def test_runs_service_with_sync_session_and_closes_it():
    tid = uuid.uuid4()
    sync_session = MagicMock(name="SyncSession")
    service = MagicMock()

    async def _fake_generate(thread_id, force):
        # Must be invoked with the thread id + force=True, and the session
        # handed to the service must be the sync SessionLocal one.
        assert thread_id == tid
        assert force is True
        return "a summary"

    service.generate_summary.side_effect = _fake_generate

    with (
        patch("src.core.database.SessionLocal", return_value=sync_session) as mk_sess,
        patch(
            "src.services.threads.thread_summarization_service."
            "get_thread_summarization_service",
            return_value=service,
        ) as mk_factory,
    ):
        result = _run_thread_summary_sync(tid)

    assert result == "a summary"
    mk_sess.assert_called_once()
    # The service is built from the sync session, not the request AsyncSession.
    mk_factory.assert_called_once_with(sync_session)
    # Session is always closed (finally), even on the happy path.
    sync_session.close.assert_called_once()


@pytest.mark.unit
def test_session_closed_when_service_raises():
    tid = uuid.uuid4()
    sync_session = MagicMock(name="SyncSession")
    service = MagicMock()

    async def _boom(thread_id, force):
        raise RuntimeError("llm down")

    service.generate_summary.side_effect = _boom

    with (
        patch("src.core.database.SessionLocal", return_value=sync_session),
        patch(
            "src.services.threads.thread_summarization_service."
            "get_thread_summarization_service",
            return_value=service,
        ),
    ):
        with pytest.raises(RuntimeError, match="llm down"):
            _run_thread_summary_sync(tid)

    # The session must still be closed on the error path.
    sync_session.close.assert_called_once()


# --- Route: a None summary must not be reported as a 200 regeneration -------
#
# ``generate_summary`` returns None both when the LLM call fails and when the
# thread has no live messages. The route used to log a warning and still return
# HTTP 200 with the thread's *previous* summary (or null), so API/CLI clients
# were told "regenerated" when nothing had been. Guard: threads.py
# regenerate_thread_summary, the ``if not summary: raise HTTPException`` block.
# Focused command:
#   pytest -q backend/tests/api/test_thread_summary_sync_helper.py -k none_summary


def _thread_stub(summary):
    from datetime import datetime, timezone

    from src.models.thread import ThreadStatus

    now = datetime.now(timezone.utc)
    thread = MagicMock(name="Thread")
    thread.id = uuid.uuid4()
    thread.conversation_id = uuid.uuid4()
    thread.title = "t"
    thread.summary = summary
    thread.status = ThreadStatus.ACTIVE
    thread.last_message_at = now
    thread.message_count = 0
    thread.token_count = 0
    thread.created_by_id = uuid.uuid4()
    thread.created_at = now
    thread.updated_at = now
    return thread


def _async_db():
    from unittest.mock import AsyncMock

    db = MagicMock(name="AsyncSession")
    db.refresh = AsyncMock()
    return db


def _call_regenerate(thread, sync_result, db=None):
    import asyncio
    from unittest.mock import AsyncMock

    from src.api.threads import threads as threads_mod

    user = MagicMock()
    user.id = uuid.uuid4()
    db = db if db is not None else _async_db()
    chat_service = MagicMock()
    chat_service.get_thread = AsyncMock(return_value=thread)

    with (
        patch.object(threads_mod, "get_chat_service", return_value=chat_service),
        patch.object(threads_mod, "_run_thread_summary_sync", return_value=sync_result),
    ):
        result = asyncio.run(
            threads_mod.regenerate_thread_summary(thread.id, current_user=user, db=db)
        )
    return result, db


@pytest.mark.unit
def test_none_summary_is_an_error_not_a_stale_200():
    from fastapi import HTTPException

    thread = _thread_stub(summary="previous summary")

    with pytest.raises(HTTPException) as exc_info:
        _call_regenerate(thread, sync_result=None)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Summary generation failed"


@pytest.mark.unit
def test_none_summary_does_not_refresh_or_echo_previous_summary():
    from fastapi import HTTPException

    thread = _thread_stub(summary="previous summary")
    db = _async_db()

    with pytest.raises(HTTPException):
        _call_regenerate(thread, sync_result=None, db=db)

    # Nothing was written, so there is nothing to refresh — and the previous
    # summary must not be re-read and handed back as if it were new.
    db.refresh.assert_not_called()


@pytest.mark.unit
def test_generated_summary_still_returns_the_thread():
    thread = _thread_stub(summary="fresh summary")

    result, db = _call_regenerate(thread, sync_result="fresh summary")

    assert result.summary == "fresh summary"
    assert result.id == thread.id
    db.refresh.assert_awaited_once_with(thread)

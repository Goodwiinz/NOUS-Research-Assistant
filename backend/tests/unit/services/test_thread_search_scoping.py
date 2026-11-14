"""Thread/message search must scope results to the caller's workspaces.

The raw-SQL builders accepted ``user_id`` but never applied it, so any
authenticated user could full-text search every workspace's threads and
message content across all tenants. These tests pin the access predicate
(owner / public / active member) and its bound param into every builder.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from src.services.threads.thread_message_search_service import (
    MessageSearchRequest,
    ThreadMessageSearchService,
    ThreadSearchRequest,
)

pytestmark = pytest.mark.unit

_ACCESS_MARKERS = (
    "w.owner_id = :access_user_id",
    "w.is_public = true",
    "workspace_members wm",
)


def _svc():
    return ThreadMessageSearchService()


def _assert_scoped(sql: str, params: dict, user_id):
    for marker in _ACCESS_MARKERS:
        assert marker in sql, f"missing access marker {marker!r}"
    assert params["access_user_id"] == str(user_id)


def test_thread_search_query_is_scoped():
    uid = uuid4()
    sql, params = _svc()._build_thread_search_query(
        "neural", ThreadSearchRequest(query="neural"), uid
    )
    _assert_scoped(sql, params, uid)


def test_thread_count_query_is_scoped():
    uid = uuid4()
    sql, params = _svc()._build_thread_count_query(
        "neural", ThreadSearchRequest(query="neural"), uid
    )
    _assert_scoped(sql, params, uid)


def test_message_search_query_is_scoped():
    uid = uuid4()
    sql, params = _svc()._build_message_search_query(
        "neural", MessageSearchRequest(query="neural"), uid
    )
    _assert_scoped(sql, params, uid)


def test_message_count_query_is_scoped():
    uid = uuid4()
    sql, params = _svc()._build_message_count_query(
        "neural", MessageSearchRequest(query="neural"), uid
    )
    _assert_scoped(sql, params, uid)


def test_combined_search_both_ctes_are_scoped():
    """combined_search builds its UNION SQL inline (not via the _build_*
    helpers), so it needs its own pin: both the thread and message CTE must
    carry the access predicate and the bound caller id."""
    from unittest.mock import MagicMock

    uid = uuid4()
    db = MagicMock()
    executed = {}

    def _capture(clause, params=None):
        executed["sql"] = str(clause)
        executed["params"] = params
        result = MagicMock()
        result.fetchall.return_value = []
        return result

    db.execute.side_effect = _capture
    _svc().combined_search("neural", user_id=uid, db=db)

    sql, params = executed["sql"], executed["params"]
    assert params["access_user_id"] == str(uid)
    # Per-CTE: split on the message CTE boundary and assert EACH half carries
    # the predicate — a plain count==2 would still pass if both predicates
    # landed in the same CTE (the real leak: one CTE left unscoped).
    assert "message_matches AS (" in sql
    thread_cte, message_cte = sql.split("message_matches AS (", 1)
    for half, label in ((thread_cte, "thread"), (message_cte, "message")):
        assert "w.owner_id = :access_user_id" in half, f"{label} CTE unscoped"
        assert "workspace_members wm" in half, f"{label} CTE missing member check"


def test_suggestions_query_is_scoped_to_caller_not_org():
    """get_search_suggestions previously leaked NULL-org workspace titles; it
    must now scope by the caller's id (owner/public/member), not organization."""
    from unittest.mock import MagicMock

    from src.api.threads.thread_search import get_search_suggestions

    user = MagicMock()
    user.id = uuid4()
    user.organization_id = uuid4()
    db = MagicMock()
    executed = {}

    def _capture(clause, params=None):
        executed["sql"] = str(clause)
        executed["params"] = params
        return []

    db.execute.side_effect = _capture
    get_search_suggestions(
        query="neur", workspace_id=None, limit=5, current_user=user, db=db
    )

    sql, params = executed["sql"], executed["params"]
    for marker in _ACCESS_MARKERS:
        assert marker in sql
    assert params["access_user_id"] == str(user.id)
    # The old org-leak clause must be gone.
    assert "organization_id IS NULL" not in sql


def test_message_author_filter_does_not_collide_with_access_param():
    """The message-author search filter binds ``:user_id``; the access
    predicate binds ``:access_user_id`` — both must be present and distinct."""
    author_id = uuid4()
    caller_id = uuid4()
    sql, params = _svc()._build_message_search_query(
        "neural",
        MessageSearchRequest(query="neural", filters={"user_id": author_id}),
        caller_id,
    )
    assert params["access_user_id"] == str(caller_id)
    assert params["user_id"] == str(author_id)
    assert "m.user_id = :user_id" in sql


def test_combined_search_both_ctes_exclude_legacy_agent_threads():
    """Legacy shadow agent threads duplicate every /chat turn. The thread,
    message and count builders exclude them, but combined_search builds its
    own SQL and left them in, so /api/v2/search/combined returned two hits
    per match. Each CTE must carry the exclusion."""
    from unittest.mock import MagicMock

    db = MagicMock()
    executed = {}

    def _capture(clause, params=None):
        executed["sql"] = str(clause)
        result = MagicMock()
        result.fetchall.return_value = []
        return result

    db.execute.side_effect = _capture
    _svc().combined_search("neural", user_id=uuid4(), db=db)

    thread_cte, message_cte = executed["sql"].split("message_matches AS (", 1)
    marker = """@> '{"source": "agent"}'::jsonb"""
    for half, label in ((thread_cte, "thread"), (message_cte, "message")):
        assert marker in half, f"{label} CTE keeps legacy agent threads"

"""Every display / model-context / export read must exclude superseded turns.

Durable edit-and-resend tombstones a user turn and everything after it by
setting ``chat_messages.superseded_by_message_id``. A tombstone is decoration
until something reads it: miss one surface and the edited-away question (or its
answer) comes back — on reload, in the thread-list preview, in an export, or,
worst, in the model's own context.

Asserted against compiled SQL / the actual filtered output rather than mocked
rows, because the defect shape here is a MISSING PREDICATE and only the
statement itself proves a predicate is present. The DO-NOT-FILTER surfaces are
asserted too, in the opposite direction — an over-eager sweep would break
feedback-on-an-edited-turn and the idempotency dedup.

Self-test: ``test_sweep_detects_a_missing_predicate`` proves the assertion
helper actually fails when the predicate is absent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, List
from uuid import uuid4

import pytest

from src.models.chat_message import ChatMessage, MessageRole

pytestmark = pytest.mark.unit

PREDICATE = "chat_messages.superseded_by_message_id IS NULL"


def _sql(stmt: Any) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": False}))


def _assert_filters_superseded(stmt: Any, label: str) -> None:
    sql = " ".join(_sql(stmt).split())
    assert PREDICATE in sql, f"{label} does not exclude superseded rows:\n{sql}"


# --------------------------------------------------------------------------- #
# Self-test for the helper above (a static guard needs its own self-test).
# --------------------------------------------------------------------------- #


def test_sweep_detects_a_missing_predicate() -> None:
    from sqlalchemy import select

    unfiltered = select(ChatMessage.id).where(ChatMessage.thread_id == uuid4())
    with pytest.raises(AssertionError):
        _assert_filters_superseded(unfiltered, "control")

    filtered = unfiltered.where(ChatMessage.superseded_by_message_id.is_(None))
    _assert_filters_superseded(filtered, "control")  # must not raise


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, value: Any = None, rows: List[Any] | None = None) -> None:
        self._value = value
        self._rows = rows or []

    def scalar(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        return self._value if self._value is not None else 0

    def scalar_one_or_none(self) -> Any:
        return self._value

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> List[Any]:
        return self._rows

    def scalars(self) -> "_Result":
        return self

    def unique(self) -> "_Result":
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _CapturingDB:
    """Records every statement; returns scripted results in call order."""

    def __init__(self, script: List[_Result] | None = None) -> None:
        self.statements: List[Any] = []
        self._script = script or []
        self._n = 0

    async def execute(self, stmt: Any, *a: Any, **k: Any) -> _Result:
        self.statements.append(stmt)
        result = (
            self._script[self._n] if self._n < len(self._script) else _Result(value=0)
        )
        self._n += 1
        return result

    async def get(self, *a: Any, **k: Any) -> Any:
        return None

    async def commit(self) -> None:
        return None


def _msg(
    *, superseded: bool, deleted: bool = False, role: Any = MessageRole.USER
) -> Any:
    """A row-shaped stand-in for the Python-side (relationship-loaded) loops."""
    return SimpleNamespace(
        id=uuid4(),
        thread_id=uuid4(),
        role=role,
        content="c",
        created_at=datetime.now(timezone.utc),
        is_deleted=deleted,
        superseded_by_message_id=uuid4() if superseded else None,
        token_count=1,
        citations=[],
        attachments=[],
        model_name=None,
        latency_ms=None,
        feedback_rating=None,
        feedback_text=None,
        has_attachments=False,
        tool_name=None,
        tool_call_id=None,
        tool_executions=None,
        plan=None,
        token_usage=None,
        stopped=False,
        user_id=None,
        to_llm_format=lambda: {"role": "user", "content": "c"},
    )


# --------------------------------------------------------------------------- #
# SQL surfaces
# --------------------------------------------------------------------------- #


async def test_build_thread_seed_messages_excludes_superseded() -> None:
    """Model-visible reseed — the highest-stakes one on this list."""
    from src.services.agent.agent_execution_service import build_thread_seed_messages

    db = _CapturingDB([_Result(rows=[])])
    await build_thread_seed_messages(db, str(uuid4()))
    _assert_filters_superseded(db.statements[0], "build_thread_seed_messages")


async def test_chat_user_row_count_matches_seed_semantics() -> None:
    """Counting rows the seed excludes trips the dual-store divergence WARN."""
    from src.services.agent.agent_execution_service import _chat_user_row_count

    db = _CapturingDB([_Result(value=0)])
    await _chat_user_row_count(db, str(uuid4()))
    _assert_filters_superseded(db.statements[0], "_chat_user_row_count")


async def test_latest_user_client_message_id_excludes_superseded() -> None:
    from src.services.agent.agent_execution_service import (
        _latest_user_client_message_id,
    )

    db = _CapturingDB([_Result(value=None)])
    await _latest_user_client_message_id(db, str(uuid4()))
    _assert_filters_superseded(db.statements[0], "_latest_user_client_message_id")


def test_last_message_preview_expression_excludes_superseded() -> None:
    from src.services.threads.thread_service import last_message_preview_expression

    _assert_filters_superseded(
        last_message_preview_expression(), "last_message_preview_expression"
    )


async def test_list_messages_page_and_count_are_in_lockstep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """base_conditions is shared, so a drifted count is structurally impossible."""
    from src.services.threads import message_service, workspace_access

    async def _thread(*a: Any, **k: Any) -> Any:
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(workspace_access, "get_thread", _thread)

    db = _CapturingDB([_Result(value=0), _Result(rows=[])])
    await message_service.list_messages(db, uuid4(), uuid4())

    assert len(db.statements) == 2  # count, then page
    _assert_filters_superseded(db.statements[0], "list_messages count")
    _assert_filters_superseded(db.statements[1], "list_messages page")


async def test_get_thread_messages_full_history_excludes_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.agent.execute import get_thread_messages
    from src.services.threads import workspace_access

    async def _thread(*a: Any, **k: Any) -> Any:
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(workspace_access, "get_thread", _thread)

    db = _CapturingDB([_Result(rows=[])])
    await get_thread_messages(
        thread_id=uuid4(),
        limit=None,
        before=None,
        current_user=SimpleNamespace(id=uuid4()),
        db=db,
    )
    # Access goes through workspace_access (stubbed); [0] is the message page.
    _assert_filters_superseded(db.statements[0], "get_thread_messages full history")


async def test_get_thread_messages_window_page_and_count_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-filters rule: a drifted count over-reports total + has_more."""
    from src.api.agent.execute import get_thread_messages
    from src.services.threads import workspace_access

    async def _thread(*a: Any, **k: Any) -> Any:
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(workspace_access, "get_thread", _thread)

    db = _CapturingDB([_Result(rows=[]), _Result(value=0)])
    await get_thread_messages(
        thread_id=uuid4(),
        limit=10,
        before=None,
        current_user=SimpleNamespace(id=uuid4()),
        db=db,
    )
    _assert_filters_superseded(db.statements[0], "get_thread_messages window page")
    _assert_filters_superseded(db.statements[1], "get_thread_messages window count")


async def test_thread_summarization_query_excludes_superseded() -> None:
    """Sync SQLAlchemy API (self.db.query/.filter) — not the async one."""
    from src.models.thread import Thread
    from src.services.threads.thread_summarization_service import (
        ThreadSummarizationService,
    )

    captured: List[Any] = []

    class _Query:
        def __init__(self, model: Any) -> None:
            self.model = model

        def filter(self, clause: Any) -> "_Query":
            captured.append(clause)
            return self

        def order_by(self, *a: Any) -> "_Query":
            return self

        def all(self) -> List[Any]:
            return []  # no messages -> generate_summary short-circuits after

        def first(self) -> Any:
            if self.model is Thread:
                return SimpleNamespace(id=uuid4(), summary=None, message_count=99)
            return None

    class _SyncDB:
        def query(self, model: Any, *a: Any, **k: Any) -> _Query:
            return _Query(model)

    svc = ThreadSummarizationService(_SyncDB())
    assert await svc.generate_summary(uuid4(), force=True) is None

    joined = " ".join(" ".join(str(c).split()) for c in captured)
    assert PREDICATE in joined, f"summarization query does not filter:\n{joined}"


# --------------------------------------------------------------------------- #
# Python-side (relationship-loaded) surfaces
# --------------------------------------------------------------------------- #


def test_thread_detail_loops_drop_superseded() -> None:
    """Both copies: api/threads/threads.py and workspace_routes/presenters.py."""
    import inspect

    from src.api.threads import threads as threads_router
    from src.api.threads.workspace_routes import presenters

    for label, fn in (
        ("threads.get_thread", threads_router.get_thread),
        (
            "presenters._thread_to_detail_response",
            presenters._thread_to_detail_response,
        ),
    ):
        src = inspect.getsource(fn)
        assert (
            "superseded_by_message_id is None" in src
        ), f"{label} renders superseded messages"


def test_get_thread_context_skips_superseded() -> None:
    """Model context: skip a superseded turn exactly like a soft-deleted one."""
    import inspect

    from src.services.threads.chat_service import ChatService

    src = inspect.getsource(ChatService.get_thread_context)
    # The per-message skip inside the token-budget loop...
    assert "msg.superseded_by_message_id is not None" in src
    # ...and the reported total, which must not count what it excludes.
    assert "m.superseded_by_message_id is None" in src

    # And the predicate itself behaves: live in, tombstoned out.
    live, doomed = _msg(superseded=False), _msg(superseded=True)
    kept = [
        m
        for m in (live, doomed)
        if not m.is_deleted and m.superseded_by_message_id is None
    ]
    assert kept == [live]


def test_export_loader_skips_superseded() -> None:
    import inspect

    from src.services.research.export_service import ExportService

    src = inspect.getsource(ExportService._load_thread)
    assert (
        "msg.superseded_by_message_id is not None" in src
    ), "exported documents would contain turns the user replaced"


# --------------------------------------------------------------------------- #
# Raw-SQL surfaces (thread/message full-text search)
# --------------------------------------------------------------------------- #

RAW_PREDICATE = "superseded_by_message_id IS NULL"


def _assert_raw_filters_superseded(sql: str, label: str) -> None:
    flat = " ".join(sql.split())
    assert RAW_PREDICATE in flat, f"{label} does not exclude superseded rows:\n{flat}"


def _search_service() -> Any:
    from src.services.threads.thread_message_search_service import (
        ThreadMessageSearchService,
    )

    return ThreadMessageSearchService()


def _message_request() -> Any:
    from src.services.threads.thread_message_search_service import MessageSearchRequest

    return MessageSearchRequest(query="q")


def _thread_request() -> Any:
    from src.services.threads.thread_message_search_service import ThreadSearchRequest

    return ThreadSearchRequest(query="q")


def test_message_search_page_and_count_both_exclude_superseded() -> None:
    """Same-filters rule on raw SQL: a drifted count invents has_more pages."""
    svc, req, uid = _search_service(), _message_request(), uuid4()

    page, _ = svc._build_message_search_query("q", req, uid)
    count, _ = svc._build_message_count_query("q", req, uid)

    _assert_raw_filters_superseded(page, "message search page")
    _assert_raw_filters_superseded(count, "message search count")


def test_thread_search_message_predicates_exclude_superseded() -> None:
    """A thread must not surface (or claim matches) for an edited-away message."""
    svc, req, uid = _search_service(), _thread_request(), uuid4()

    page, _ = svc._build_thread_search_query("q", req, uid)
    count, _ = svc._build_thread_count_query("q", req, uid)

    # Both the EXISTS that makes the thread match at all...
    assert page.count("cm.superseded_by_message_id IS NULL") == 2  # EXISTS + tally
    assert count.count("cm.superseded_by_message_id IS NULL") == 1  # EXISTS only
    _assert_raw_filters_superseded(page, "thread search page")
    _assert_raw_filters_superseded(count, "thread search count")


def test_combined_search_message_cte_excludes_superseded() -> None:
    """combined_search's UNION arm over chat_messages."""
    import inspect

    from src.services.threads.thread_message_search_service import (
        ThreadMessageSearchService,
    )

    src = inspect.getsource(ThreadMessageSearchService.combined_search)
    assert "m.superseded_by_message_id IS NULL" in src


def test_save_thread_to_note_excludes_superseded() -> None:
    """A note is a durable artifact: it must capture what was actually asked."""
    import inspect

    from src.api.research import project_chat

    src = inspect.getsource(project_chat.save_thread_to_note)
    assert (
        "ChatMessage.superseded_by_message_id.is_(None)" in src
    ), "a saved note would contain the turn the user replaced"


def test_export_preview_counts_only_non_superseded_messages() -> None:
    """export.py's preview counts the FILTERED list, not Thread.message_count."""
    import inspect

    from src.api.research import export

    src = inspect.getsource(export)
    assert "message_count = len(thread.messages)" in src
    # The denormalised counter would over-promise; it must not be used here.
    assert "message_count = thread.message_count" not in src


# --------------------------------------------------------------------------- #
# DO NOT FILTER — the opposite direction
# --------------------------------------------------------------------------- #


async def test_get_message_by_id_still_returns_a_superseded_row() -> None:
    """Feedback / edit load a turn BY ID; a tombstone must not hide it."""
    from src.services.threads import workspace_access

    db = _CapturingDB([_Result(rows=[])])
    await workspace_access.get_message(db, uuid4(), uuid4())
    sql = " ".join(_sql(db.statements[0]).split())
    assert PREDICATE not in sql


def test_workspace_stats_message_count_keeps_superseded() -> None:
    """Analytics: an edit tombstones a turn for display, it did still happen."""
    import inspect

    from src.services.threads.chat_service import ChatService

    src = inspect.getsource(ChatService.get_workspace_stats)
    code = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )
    assert "superseded_by_message_id" not in code

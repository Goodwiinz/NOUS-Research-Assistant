"""M12 + L10 — count/page filter drift and unescaped LIKE search.

M12 (``GET /agent/threads/{id}/messages`` windowed page, execute.py): the
page query filters ``thread_id`` + ``superseded_by_message_id IS NULL`` +
(when ``before`` is set) ``created_at < before``; the count query built its
own inline filter list and silently dropped the ``before`` predicate, so any
``before=`` page reported the full-thread total instead of the filtered one
— "count queries must apply the same filters as the result query" (the
documented invariant this codebase enforces via shared ``_apply_*_filters``
helpers elsewhere).

L10 (``ProjectService.list_projects`` ``search``, project_service.py):
forwarded straight into ``Collection.name.ilike(f"%{search}%")`` with no
escaping, so a user/LLM-supplied search containing ``%``/``_`` acted as a
wildcard instead of a literal character — the LIKE-escaping invariant
already enforced at every other agent-facing ilike() site
(``tool_helpers._escape_like``, pinned by ``test_tool_helpers_like_safety.py``)
didn't reach this one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _where_clause(sql: str) -> str:
    """Isolate + normalize a compiled statement's WHERE clause so the page
    query (which also carries ORDER BY/LIMIT) can be compared against the
    count query (which doesn't) without those expected differences."""
    where = sql.split("WHERE", 1)[1]
    where = where.split("ORDER BY")[0]
    return " ".join(where.split())


class TestThreadMessagesCountFilterParity:
    """M12: the windowed ``total`` must reflect the same ``before`` cursor
    as the page it describes, or clients computing "older messages
    remaining" from ``total - received`` drift."""

    @staticmethod
    def _capture_db(thread: Any) -> tuple[Any, list[str]]:
        captured: list[str] = []

        async def _execute(stmt: Any, *a: Any, **kw: Any) -> Any:
            captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
            call_num = len(captured)
            result = MagicMock()
            if call_num == 1:
                # Ownership check.
                result.scalar_one_or_none = MagicMock(return_value=thread)
            elif call_num == 2:
                # Page query.
                result.scalars = MagicMock(
                    return_value=MagicMock(all=MagicMock(return_value=[]))
                )
            else:
                # Count query.
                result.scalar = MagicMock(return_value=0)
            return result

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_execute)
        return db, captured

    async def test_count_query_applies_before_cursor(self) -> None:
        from src.api.agent.execute import get_thread_messages

        thread_id = uuid4()
        before = datetime(2026, 1, 1, 12, 0, 0)
        db, captured = self._capture_db(thread=MagicMock())
        user = MagicMock()
        user.id = uuid4()

        await get_thread_messages(
            thread_id=thread_id, limit=10, before=before, current_user=user, db=db
        )

        assert len(captured) == 3, captured
        page_sql, count_sql = captured[1], captured[2]

        # The bug: the count statement was built independently of the page
        # query and never picked up the ``before`` predicate.
        assert "created_at <" in count_sql, count_sql
        # Control: page and count must filter identically (mod SELECT
        # columns / ORDER BY / LIMIT, which are expected to differ).
        assert _where_clause(page_sql) == _where_clause(count_sql), (
            page_sql,
            count_sql,
        )

    async def test_full_history_path_has_single_query_no_drift_possible(
        self,
    ) -> None:
        """Control: omitting both ``limit``/``before`` takes the unwindowed
        branch (``total = len(messages)``), which has no separate count
        statement to drift — only the windowed branch above is at risk."""
        from src.api.agent.execute import get_thread_messages

        thread_id = uuid4()
        db, captured = self._capture_db(thread=MagicMock())
        user = MagicMock()
        user.id = uuid4()

        response = await get_thread_messages(
            thread_id=thread_id, limit=None, before=None, current_user=user, db=db
        )

        assert len(captured) == 2  # ownership check + single messages query
        assert response.total == 0
        assert response.messages == []


class TestListProjectsSearchEscaping:
    """L10: ``ProjectService.list_projects`` must escape LIKE metacharacters
    in ``search`` like every other agent-facing ilike() site does."""

    @staticmethod
    def _capture_db(workspace_id: Any) -> tuple[Any, list[str]]:
        captured: list[str] = []

        async def _execute(stmt: Any, *a: Any, **kw: Any) -> Any:
            sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            captured.append(sql)
            result = MagicMock()
            if "count(" in sql.lower():
                result.scalar = MagicMock(return_value=0)
            else:
                result.scalars = MagicMock(
                    return_value=MagicMock(all=MagicMock(return_value=[]))
                )
            return result

        db = MagicMock()
        db.execute = AsyncMock(side_effect=_execute)
        return db, captured

    async def test_search_wildcards_are_escaped(self) -> None:
        from unittest.mock import patch

        from src.services.research.project_service import ProjectService

        db, captured = self._capture_db(uuid4())
        service = ProjectService(db)

        with patch(
            "src.services.research.project_service.accessible_research_workspace_ids",
            new=AsyncMock(return_value=[uuid4()]),
        ):
            await service.list_projects(user_id=uuid4(), search="50%_done")

        # Both the count and page queries share ``filters`` — assert both to
        # guard against the escaping being applied to only one of them.
        assert len(captured) == 2, captured
        assert all("50\\%\\_done" in sql for sql in captured), captured

    async def test_plain_search_term_is_unaffected(self) -> None:
        """Control: a search with no LIKE metacharacters compiles to the same
        substring match as before escaping was added — the fix must not
        narrow ordinary searches."""
        from unittest.mock import patch

        from src.services.research.project_service import ProjectService

        db, captured = self._capture_db(uuid4())
        service = ProjectService(db)

        with patch(
            "src.services.research.project_service.accessible_research_workspace_ids",
            new=AsyncMock(return_value=[uuid4()]),
        ):
            await service.list_projects(user_id=uuid4(), search="hello")

        assert all("%hello%" in sql for sql in captured), captured

"""Locking coverage for unlink_thread_from_project (audit B5).

Two concurrent unlinks of the same thread previously both read
``thread.source_project_id`` / ``rag_document_scope`` with a plain SELECT,
computed ``remaining_link`` from the same baseline, and raced on commit —
last writer won, leaving stale ``document_ids`` feeding RAG scope.

The transaction that mutates those columns must take the thread row lock
(``SELECT ... FOR UPDATE OF threads``) BEFORE computing ``remaining_link``.
SQLite drops FOR UPDATE at compile time, so the assertion is made on the
statement object itself (dialect-independent), plus the PostgreSQL rendering.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.research.project_chat import unlink_thread_from_project
from src.models import ProjectThread, Thread, User


@pytest.fixture
def mock_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = uuid4()
    return user


@pytest.fixture
def mock_thread() -> MagicMock:
    thread = MagicMock(spec=Thread)
    thread.id = uuid4()
    thread.is_deleted = False
    return thread


@pytest.fixture
def mock_project_thread(mock_thread: MagicMock) -> MagicMock:
    pt = MagicMock(spec=ProjectThread)
    pt.id = uuid4()
    pt.project_id = uuid4()
    pt.thread_id = mock_thread.id
    pt.is_deleted = False
    return pt


class TestUnlinkThreadRowLock:
    """B5: the thread-row read inside unlink must be SELECT ... FOR UPDATE."""

    @pytest.mark.asyncio
    async def test_unlink_locks_thread_row_before_computing_remaining_link(
        self,
        mock_user: MagicMock,
        mock_thread: MagicMock,
        mock_project_thread: MagicMock,
    ) -> None:
        """Regression B5: thread row selected FOR UPDATE before remaining_link."""
        mock_thread.source_project_id = mock_project_thread.project_id
        mock_thread.rag_document_scope = {"document_ids": [str(uuid4())]}

        link_result = MagicMock()
        link_result.scalar_one_or_none.return_value = mock_project_thread
        workspace_access_result = MagicMock()
        workspace_access_result.scalars.return_value.all.return_value = [uuid4()]
        thread_result = MagicMock()
        thread_result.scalar_one_or_none.return_value = mock_thread
        remaining_result = MagicMock()
        remaining_result.scalars.return_value.first.return_value = None

        statements = []
        canned_results = [
            link_result,
            workspace_access_result,
            thread_result,
            remaining_result,
        ]

        db = AsyncMock(spec=AsyncSession)

        async def _capture_and_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            statements.append(stmt)
            return canned_results[len(statements) - 1]

        db.execute.side_effect = _capture_and_execute

        # Only the project auth check is patched (repo convention); the real
        # _get_thread_with_auth runs — its statement is the code under test.
        with patch(
            "src.api.research.project_chat._get_project_with_auth",
            new=AsyncMock(return_value=MagicMock()),
        ):
            await unlink_thread_from_project(
                mock_project_thread.project_id,
                mock_thread.id,
                current_user=mock_user,
                db=db,
            )

        def _entity(stmt: Any) -> Any:
            return stmt.column_descriptions[0]["entity"]

        # The thread-row read whose result feeds the source_project_id /
        # rag_document_scope rewrite must be locked...
        thread_stmts = [s for s in statements if _entity(s) is Thread]
        assert len(thread_stmts) == 1, "expected exactly one Thread-row select"
        thread_stmt = thread_stmts[0]

        # ...via .with_for_update() applied to that select.
        assert thread_stmt._for_update_arg is not None, (
            "unlink reads the thread row without FOR UPDATE "
            "(audit B5: concurrent unlinks race on last-commit-wins)"
        )
        compiled_pg = str(thread_stmt.compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE OF threads" in compiled_pg

        # Lock must be acquired BEFORE the remaining-link computation, else
        # two unlinks still read the same baseline through the window. The
        # remaining-link query is the ProjectThread select that joins
        # workspaces (the plain link lookup does not).
        remaining_idx = next(
            i
            for i, s in enumerate(statements)
            if _entity(s) is ProjectThread
            and "workspace" in str(s.compile(dialect=postgresql.dialect())).lower()
        )
        assert statements.index(thread_stmt) < remaining_idx

        # Sanity: mutation path still ran against the locked row.
        assert mock_thread.source_project_id is None
        db.commit.assert_awaited_once()


class TestUnlinkIgnoresSoftDeletedLink:
    """B7: a second DELETE must 404, not re-delete the dead link row."""

    @pytest.mark.asyncio
    async def test_unlink_fetch_filters_soft_deleted_links(
        self,
        mock_user: MagicMock,
        mock_thread: MagicMock,
    ) -> None:
        """The link lookup itself excludes is_deleted rows (dialect-independent)."""
        from sqlalchemy.dialects import postgresql as pg

        statements = []
        db = AsyncMock(spec=AsyncSession)

        async def _capture(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            statements.append(stmt)
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result

        db.execute.side_effect = _capture

        with (
            patch(
                "src.api.research.project_chat._get_project_with_auth",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "src.api.research.project_chat._get_thread_with_auth",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await unlink_thread_from_project(
                    uuid4(), mock_thread.id, current_user=mock_user, db=db
                )

        assert exc_info.value.status_code == 404

        # The dead-row exclusion must live in the FIRST ProjectThread select
        # (the plain link lookup) — not just somewhere in the call, e.g. the
        # remaining-link query which already filters.
        link_stmt = next(
            s for s in statements if s.column_descriptions[0]["entity"] is ProjectThread
        )
        # Compile ONLY the WHERE clause: is_deleted also appears in every
        # SELECT column list, which would make a whole-statement match vacuous.
        where_sql = str(link_stmt.whereclause.compile(dialect=pg.dialect())).lower()
        assert "is_deleted" in where_sql, (
            "unlink link lookup does not filter is_deleted — "
            "(audit B7: double DELETE re-deletes a dead row and re-runs "
            "scope reassignment against it)"
        )

    @pytest.mark.asyncio
    async def test_unlink_of_filtered_out_row_is_404_and_mutates_nothing(
        self, mock_user: MagicMock
    ) -> None:
        """When the dead row is filtered out, nothing is mutated or committed."""
        db = AsyncMock(spec=AsyncSession)
        db.commit = AsyncMock()

        async def _none_result(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result

        db.execute.side_effect = _none_result

        # Simulate the SQL filter having dropped the row.
        with patch(
            "src.api.research.project_chat._get_project_with_auth",
            new=AsyncMock(return_value=MagicMock()),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await unlink_thread_from_project(
                    uuid4(), uuid4(), current_user=mock_user, db=db
                )

        assert exc_info.value.status_code == 404
        db.commit.assert_not_awaited()
        db.rollback.assert_not_awaited()

"""Audit B9/B10 — LIKE-pattern safety in tool_helpers resolvers.

B9 (``_resolve_document_id`` title fallback): the input must be wrapped in
``%...%`` so it matches substrings — the documented "Title ILIKE fallback" —
not only whole-title case-insensitive equality.

B10 (``_resolve_project_id`` name lookup): the input must pass through
``_escape_like`` so a project name containing ``%``/``_``/``\\`` can't act as
a wildcard and silently resolve to the wrong project within the owner scope.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.services.agent.tool_helpers import _resolve_document_id, _resolve_project_id


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _capture_db():
    """A db mock whose execute() records the statement and returns no rows."""
    captured: dict = {}

    async def capture(stmt):
        captured["stmt"] = stmt
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=capture)
    return db, captured


@pytest.mark.unit
@pytest.mark.asyncio
async def test_title_fallback_uses_exact_match_not_wildcards():
    user = MagicMock()
    user.organization_id = "org-1"
    db, captured = _capture_db()

    # Not a UUID and not an arXiv ID → falls through to the title branch.
    # R7-L8: exact case-insensitive equality, never a substring ILIKE — a
    # wildcard match let "report" resolve to the newest "Q3 Report (draft)".
    await _resolve_document_id("transformers review", db, user)

    sql = _compiled(captured["stmt"])
    assert "%transformers review%" not in sql, sql
    assert "lower(documents.title)" in sql, sql
    assert "transformers review" in sql, sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_project_name_escapes_like_metacharacters():
    user = MagicMock()
    user.id = "user-1"
    db, captured = _capture_db()

    # A literal '%' in the name must be escaped, not treated as a wildcard.
    await _resolve_project_id("Report%2024", db, user)

    sql = _compiled(captured["stmt"])
    # Escaped form (backslash before the %) proves the input was sanitised.
    assert "Report\\%2024" in sql, sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_project_name_resolves_unique_collaborator_match():
    user = MagicMock()
    user.id = uuid4()
    project_id = uuid4()
    db = MagicMock()
    workspace_result = MagicMock()
    workspace_result.scalars.return_value.all.return_value = [uuid4()]
    project_result = MagicMock()
    project_result.scalars.return_value.all.return_value = [project_id]
    db.execute = AsyncMock(side_effect=[workspace_result, project_result])

    resolved = await _resolve_project_id("Shared Study", db, user)

    assert resolved == project_id
    project_stmt = db.execute.await_args_list[1].args[0]
    assert "LIMIT 2" in _compiled(project_stmt)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_project_name_rejects_ambiguous_authorized_matches():
    user = MagicMock()
    user.id = uuid4()
    db = MagicMock()
    workspace_result = MagicMock()
    workspace_result.scalars.return_value.all.return_value = [uuid4()]
    project_result = MagicMock()
    project_result.scalars.return_value.all.return_value = [uuid4(), uuid4()]
    db.execute = AsyncMock(side_effect=[workspace_result, project_result])

    assert await _resolve_project_id("Duplicate", db, user) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_project_uuid_passthrough_does_not_query():
    user = MagicMock()
    db = MagicMock()
    db.execute = AsyncMock()
    project_id = uuid4()

    assert await _resolve_project_id(str(project_id), db, user) == project_id
    db.execute.assert_not_awaited()

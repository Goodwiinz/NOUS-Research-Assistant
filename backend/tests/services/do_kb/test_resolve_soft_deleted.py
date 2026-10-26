"""F4: DO KB chunk resolution must exclude soft-deleted documents.

``resolve_and_filter_chunks`` resolved provider keys to ``Document`` rows by
organization only; a soft-deleted document's chunks (still present in the
external DO index until the async cleanup lands) resolved to a live-looking
title/id. The resolution query now carries the ``is_deleted = false``
eligibility predicate alongside the organization predicate.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.services.do_kb.models import Chunk
from src.services.do_kb.resolve import resolve_and_filter_chunks

pytestmark = pytest.mark.unit


def _session(doc_rows: list[tuple], membership: list | None = None) -> AsyncMock:
    session = AsyncMock()
    statements: list = []

    async def _execute(stmt: Any) -> Any:
        statements.append(stmt)
        if len(statements) == 1:
            return doc_rows
        return [(d,) for d in (membership or [])]

    session.execute = AsyncMock(side_effect=_execute)
    session.statements = statements
    return session


def _compiled_where(stmt: Any) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect())).lower()


@pytest.mark.asyncio
async def test_resolution_query_excludes_soft_deleted_documents() -> None:
    doc_id = uuid4()
    org_id = uuid4()
    session = _session(doc_rows=[(doc_id, "report.pdf", "Report")])

    await resolve_and_filter_chunks(
        chunks=[Chunk(text="t", score=0.9, document_id="report.pdf", metadata={})],
        org_id=org_id,
        session=session,
    )

    sql = _compiled_where(session.statements[0])
    assert "documents.organization_id = " in sql
    assert "documents.is_deleted = false" in sql


@pytest.mark.asyncio
async def test_project_scoped_resolution_keeps_both_predicates() -> None:
    """The project path reuses the same resolution query, so the deleted
    predicate must be present there too, ahead of membership filtering."""
    doc_id = uuid4()
    session = _session(doc_rows=[(doc_id, "a.pdf", "A")], membership=[doc_id])

    _, chunks_to_emit = await resolve_and_filter_chunks(
        chunks=[Chunk(text="t", score=0.9, document_id="a.pdf", metadata={})],
        org_id=uuid4(),
        session=session,
        project_id=str(uuid4()),
    )

    assert len(chunks_to_emit) == 1
    sql = _compiled_where(session.statements[0])
    assert "documents.is_deleted = false" in sql
    assert "documents.organization_id = " in sql
    assert len(session.statements) == 2


@pytest.mark.asyncio
async def test_project_membership_query_excludes_soft_deleted_memberships() -> None:
    """A document removed from a project leaves a soft-deleted
    ``CollectionDocument`` row behind. The membership subquery must not treat
    that row as live membership, or a removed document keeps resolving under
    project scope."""
    doc_id = uuid4()
    session = _session(doc_rows=[(doc_id, "a.pdf", "A")], membership=[doc_id])

    await resolve_and_filter_chunks(
        chunks=[Chunk(text="t", score=0.9, document_id="a.pdf", metadata={})],
        org_id=uuid4(),
        session=session,
        project_id=str(uuid4()),
    )

    assert len(session.statements) == 2
    membership_sql = _compiled_where(session.statements[1])
    assert "collection_documents.collection_id = " in membership_sql
    assert "collection_documents.is_deleted = false" in membership_sql


@pytest.mark.asyncio
async def test_active_documents_without_project_stay_org_wide() -> None:
    """No project requested → every active org document is eligible."""
    doc_a, doc_b = uuid4(), uuid4()
    session = _session(doc_rows=[(doc_a, "a.pdf", "A"), (doc_b, "b.pdf", "B")])
    chunks = [
        Chunk(text="a", score=0.9, document_id="a.pdf", metadata={}),
        Chunk(text="b", score=0.8, document_id="b.pdf", metadata={}),
    ]

    title_by_key, chunks_to_emit = await resolve_and_filter_chunks(
        chunks=chunks, org_id=uuid4(), session=session
    )

    assert set(title_by_key) == {"a.pdf", "b.pdf"}
    assert chunks_to_emit == chunks
    assert len(session.statements) == 1

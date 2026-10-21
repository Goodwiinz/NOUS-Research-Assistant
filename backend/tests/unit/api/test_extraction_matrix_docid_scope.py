"""trigger_extraction must scope client document_ids to the matrix's project.

POST /matrices/{matrix_id}/extract validated matrix-project ownership but then
fetched each client-supplied document_id with `select(Document).where(id==...)`
— no org/project filter. A caller (owning matrix M in their own project) could
pass another tenant's document UUIDs and have that document's content_text read
into extraction cells (cross-tenant document-content leak). The fetch now joins
collection_documents on collection_id == matrix.project_id, dropping any id not
in the (owned) project. This test pins the scoping on the built query.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit

from src.api.research.extraction_matrix import _scoped_document_query


def _sql(q):
    return str(q.compile(compile_kwargs={"literal_binds": False})).lower()


def test_query_joins_project_collection():
    sql = _sql(_scoped_document_query(uuid4(), uuid4()))
    assert "join collection_documents" in sql
    assert "collection_id" in sql
    assert "documents.id" in sql  # still keyed by the requested doc id


def test_query_never_bare_id_select():
    # regression: old code was select(Document).where(Document.id == doc_id)
    # with NO collection join.
    sql = _sql(_scoped_document_query(uuid4(), uuid4()))
    assert "collection_documents" in sql


@pytest.mark.asyncio
async def test_trigger_seeds_zero_counters(monkeypatch: pytest.MonkeyPatch) -> None:
    """The status seeded by POST /matrices/{id}/extract must carry the counters
    the UI renders ("Extracting completed/total") so a poll that lands before
    the worker's first write shows 0/N, not "/N". ``extracted`` stays for
    compatibility. Mutation: drop the zero counters from the seed and this
    fails on the (completed, failed, skipped) assertion with KeyError."""
    from types import SimpleNamespace
    from typing import Any, cast

    from sqlalchemy.ext.asyncio import AsyncSession

    from src.api.research import extraction_matrix as routes
    from src.core.config import settings
    from src.models.user import User
    from src.shared.scispace_schemas import TriggerExtractionRequest

    matrix_id = uuid4()
    writes: list[tuple[str, dict[str, Any]]] = []

    async def record(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        writes.append((task_id, payload))
        return payload

    async def allow(*_: Any, **__: Any) -> None:
        return None

    async def task_kwargs(*_: Any, **__: Any) -> dict[str, Any]:
        return {}

    class _DB:
        async def execute(self, _query: Any) -> Any:
            matrix = SimpleNamespace(id=matrix_id, project_id=uuid4())
            return SimpleNamespace(scalar_one_or_none=lambda: matrix)

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(routes, "_validate_project_ownership", allow)
    monkeypatch.setattr(
        routes.extraction_forms_service, "extraction_task_kwargs", task_kwargs
    )
    monkeypatch.setattr(routes.run_extraction_matrix, "apply_async", lambda **_: None)
    monkeypatch.setattr(
        routes.ExtractionMatrixService, "set_extraction_status", staticmethod(record)
    )

    response = await routes.trigger_extraction(
        matrix_id,
        TriggerExtractionRequest(document_ids=[uuid4(), uuid4()]),
        current_user=cast(User, SimpleNamespace(id=uuid4())),
        db=cast(AsyncSession, _DB()),
    )

    assert response["task_id"] and len(writes) == 1
    task_id, seeded = writes[0]
    assert task_id == response["task_id"]
    assert (seeded["status"], seeded["total"]) == ("running", 2)
    assert (seeded["completed"], seeded["failed"], seeded["skipped"]) == (0, 0, 0)
    assert seeded["extracted"] == 0  # pre-existing key, kept for compatibility

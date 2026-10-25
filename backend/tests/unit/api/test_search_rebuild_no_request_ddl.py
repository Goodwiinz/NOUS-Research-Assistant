"""F2: a tenant admin's POST /search/indexes/rebuild must not run global DDL.

``rebuild_search_indexes`` used to call
``fulltext_search_service.create_search_indexes(db)`` — five global
``CREATE INDEX`` statements against the shared ``documents`` table — gated
only on the per-user ``role == admin`` (a role boundary, not a tenant
boundary). Index DDL belongs to the deployment-owned schema path (Alembic:
``p3q4r5s6t7u8`` owns the GIN index on ``search_vector``). The endpoint now
only rebuilds the caller's organization's search vectors and says so.

The companion KG endpoint, POST /knowledge-graph/schema/reset, likewise
re-ran ``_ensure_schema()`` (global Neo4j constraint/index DDL) after the
org-scoped delete. Startup (``_connect`` → ``_maybe_ensure_schema``) and
``get_session`` already own that bootstrap, so the per-request call is gone.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.api.search import knowledge_graph as kg
from src.api.search import search as search_api
from src.models.user import UserRole

pytestmark = pytest.mark.unit


def _admin(org_id: str | None = "org-1") -> SimpleNamespace:
    return SimpleNamespace(id="user-1", role=UserRole.ADMIN, organization_id=org_id)


async def test_rebuild_does_not_create_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    service = search_api.fulltext_search_service
    monkeypatch.setattr(
        service,
        "create_search_indexes",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("request path must not run index DDL")
        ),
    )
    docs = [SimpleNamespace(id="d1"), SimpleNamespace(id="d2")]
    rebuilt: list[str] = []
    monkeypatch.setattr(service, "get_rebuild_documents", lambda org, db: docs)
    monkeypatch.setattr(
        service,
        "update_document_search_vector",
        lambda doc_id, db: rebuilt.append(doc_id),
    )

    response = await search_api.rebuild_search_indexes(
        current_user=_admin(), db=MagicMock()
    )

    assert rebuilt == ["d1", "d2"]
    # Same response keys as before; the flag/message are now truthful.
    assert set(response) == {
        "message",
        "indexes_created",
        "documents_updated",
        "total_documents",
    }
    assert response["indexes_created"] is False
    assert response["documents_updated"] == 2
    assert response["total_documents"] == 2
    assert "index" in response["message"].lower()
    assert "rebuilt successfully" not in response["message"]


async def test_rebuild_rejects_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    service = search_api.fulltext_search_service
    monkeypatch.setattr(
        service,
        "get_rebuild_documents",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no work")),
    )
    user = SimpleNamespace(id="u", role=UserRole.USER, organization_id="org-1")
    with pytest.raises(HTTPException) as exc:
        await search_api.rebuild_search_indexes(current_user=user, db=MagicMock())
    assert exc.value.status_code == 403


def test_rebuild_route_source_has_no_ddl_call() -> None:
    import inspect

    src = inspect.getsource(search_api.rebuild_search_indexes)
    assert "create_search_indexes" not in src


def test_kg_schema_reset_does_not_rerun_schema_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    session = MagicMock()
    result = MagicMock()
    result.single.return_value = {"deleted": 3}
    session.run.return_value = result

    @contextmanager
    def _cm() -> Iterator[MagicMock]:
        yield session

    monkeypatch.setattr(kg.knowledge_graph_service, "get_session", _cm)
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "_ensure_schema",
        lambda: (_ for _ in ()).throw(
            AssertionError("schema DDL must not run per request")
        ),
    )

    user = MagicMock()
    user.role = UserRole.ADMIN
    user.organization_id = "org-1"

    response = kg.reset_graph_schema(confirm=True, current_user=user)

    assert response["deleted_entities"] == 3
    query, params = session.run.call_args.args
    assert "e.organization_id = $org_id" in query
    assert params == {"org_id": "org-1"}

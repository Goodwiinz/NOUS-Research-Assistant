"""F5: FullTextSearchService.search must fail closed without an organization.

The SQL builder applies the tenant predicate only ``if organization_id:`` —
so a falsy organization_id used to run an org-unfiltered query over every
tenant's documents. Now it returns an empty response and never touches the DB.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.models.search_schemas import SearchQuery, SearchType
from src.services.search.fulltext_search_service import FullTextSearchService

pytestmark = pytest.mark.unit

ORG_ID = "11111111-1111-1111-1111-111111111111"


def _db_that_must_not_run() -> MagicMock:
    db = MagicMock()
    db.execute.side_effect = AssertionError("no SQL may run without an org")
    return db


@pytest.mark.parametrize("organization_id", [None, ""])
def test_search_without_organization_runs_no_sql(organization_id: str | None) -> None:
    db = _db_that_must_not_run()
    service = FullTextSearchService()

    response = service.search(
        search_request=SearchQuery(query="quarterly report"),
        user_id="user-1",
        organization_id=organization_id,
        db=db,
    )

    assert response.results == []
    assert response.total_results == 0
    assert response.has_more is False
    assert response.search_type == SearchType.FULLTEXT
    db.execute.assert_not_called()


async def test_legacy_query_search_without_organization_runs_no_sql() -> None:
    """The ``search(query=...)`` compatibility path (no search_request) goes
    through ``_legacy_search`` and must fail closed the same way: an empty
    result and no SQL when the caller has no organization."""
    db = _db_that_must_not_run()
    service = FullTextSearchService()

    results = await service.search(
        search_request=None,
        query="quarterly report",
        organization_id=None,
        db=db,
    )

    assert results == []
    db.execute.assert_not_called()


def test_search_with_organization_still_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legitimate org-scoped callers keep working (the guard is org-only)."""
    db = MagicMock()
    rows = MagicMock()
    rows.fetchall.return_value = []
    count = MagicMock()
    count.scalar.return_value = 0
    db.execute.side_effect = [rows, count]

    service = FullTextSearchService()
    monkeypatch.setattr(service, "_get_search_suggestions", lambda *a, **k: [])

    response = service.search(
        search_request=SearchQuery(query="quarterly report"),
        user_id="user-1",
        organization_id=ORG_ID,
        db=db,
    )

    assert response.results == []
    assert db.execute.call_count == 2
    params = db.execute.call_args_list[0].args[1]
    assert params["organization_id"] == ORG_ID

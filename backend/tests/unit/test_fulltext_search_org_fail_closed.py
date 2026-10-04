"""GOO-351: document full-text search fails closed without an organization.

``_build_search_query`` / ``_build_count_query`` silently dropped the
``d.organization_id`` predicate when ``organization_id`` was falsy, so a
caller without tenant scope issued a global document search/count.

Mutation check: restore the ``if organization_id:`` conditional in either
builder (or remove the early return in ``_search_response``) and
``pytest -q backend/tests/unit/test_fulltext_search_org_fail_closed.py`` fails.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from src.models.search_schemas import SearchQuery, SearchSortOrder, SearchType
from src.services.search.fulltext_search_service import FullTextSearchService

pytestmark = pytest.mark.unit

BAD_SCOPES = [None, "", "None", "not-a-uuid"]


def _request() -> SearchQuery:
    return SearchQuery(
        query="transformers",
        search_type=SearchType.FULLTEXT,
        sort_order=SearchSortOrder.RELEVANCE,
        filters=None,
    )


@pytest.mark.parametrize("org", BAD_SCOPES)
@pytest.mark.parametrize("builder", ["_build_search_query", "_build_count_query"])
def test_builders_refuse_missing_or_invalid_org(
    builder: str, org: Optional[str]
) -> None:
    svc = FullTextSearchService()
    with pytest.raises(ValueError):
        getattr(svc, builder)(_request(), "u-1", org)


@pytest.mark.parametrize("org", BAD_SCOPES)
def test_search_returns_empty_without_touching_db(org: Optional[str]) -> None:
    db = MagicMock()
    out = FullTextSearchService().search(
        search_request=_request(), user_id="u-1", organization_id=org, db=db
    )
    assert out.results == [] and out.total_results == 0 and out.has_more is False
    db.execute.assert_not_called()


@pytest.mark.parametrize("builder", ["_build_search_query", "_build_count_query"])
def test_builders_scope_valid_org(builder: str) -> None:
    org = str(uuid4())
    sql, params = getattr(FullTextSearchService(), builder)(_request(), "u-1", org)
    assert "AND d.organization_id = :organization_id" in sql
    assert params["organization_id"] == org


def test_search_with_valid_org_queries_scoped_rows() -> None:
    org = str(uuid4())
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.scalar.return_value = 0
    FullTextSearchService().search(
        search_request=_request(), user_id="u-1", organization_id=org, db=db
    )
    search_params = db.execute.call_args_list[0].args[1]
    count_params = db.execute.call_args_list[1].args[1]
    assert search_params["organization_id"] == org
    assert count_params["organization_id"] == org

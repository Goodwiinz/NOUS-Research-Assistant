"""Full-text search retries with any-term match when the all-term match is empty.

``plainto_tsquery`` ANDs every lexeme, so a natural-language question needed
every stem in one document and usually returned nothing. This is the only live
retrieval backend on the AWS dev lane, so the agent read that as "no documents".

Mutation check: drop the retry in ``_search_response`` (or build the retry with
``match_any=False``) and ``test_multi_term_zero_hit_retries_with_any_term_match``
fails.
"""

from __future__ import annotations

from typing import Any, List
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from src.models.search_schemas import SearchQuery, SearchSortOrder, SearchType
from src.services.search.fulltext_search_service import FullTextSearchService

pytestmark = pytest.mark.unit

ANY_TERM_TSQUERY = (
    "replace(plainto_tsquery('english', :query)::text, ' & ', ' | ')::tsquery"
)


def _request(query: str) -> SearchQuery:
    return SearchQuery(
        query=query,
        search_type=SearchType.FULLTEXT,
        sort_order=SearchSortOrder.RELEVANCE,
        filters=None,
    )


def _db(counts: List[int]) -> MagicMock:
    """Session whose result queries return no rows and whose counts follow ``counts``."""
    db = MagicMock()
    results: List[Any] = []
    for count in counts:
        rows = MagicMock()
        rows.fetchall.return_value = []
        total = MagicMock()
        total.scalar.return_value = count
        results.extend([rows, total])
    # Trailing calls (the suggestions lookup) get an empty result.
    tail = MagicMock()
    tail.fetchall.return_value = []
    tail.scalar.return_value = 0
    results.extend([tail] * 4)
    db.execute.side_effect = results
    return db


def _sql(db: MagicMock, call_index: int) -> str:
    return str(db.execute.call_args_list[call_index].args[0])


def _match_calls(db: MagicMock) -> int:
    """Result and count queries only; the suggestions lookup is not a match query."""
    return sum(
        "search_vector @@" in str(call.args[0]) for call in db.execute.call_args_list
    )


def test_multi_term_zero_hit_retries_with_any_term_match() -> None:
    db = _db([0, 3])

    response = FullTextSearchService().search(
        search_request=_request("how do transformers handle long context"),
        user_id="u-1",
        organization_id=str(uuid4()),
        db=db,
    )

    assert _match_calls(db) == 4
    for strict_call in (0, 1):
        assert ANY_TERM_TSQUERY not in _sql(db, strict_call)
    for retry_call in (2, 3):
        assert ANY_TERM_TSQUERY in _sql(db, retry_call)
        assert "@@ plainto_tsquery('english', :query)" not in _sql(db, retry_call)
    # The count comes from the same mode as the results, so pagination agrees.
    assert response.total_results == 3


def test_retry_keeps_the_organization_predicate() -> None:
    org = str(uuid4())
    db = _db([0, 0])

    FullTextSearchService().search(
        search_request=_request("attention mechanism"),
        user_id="u-1",
        organization_id=org,
        db=db,
    )

    for retry_call in (2, 3):
        assert "AND d.organization_id = :organization_id" in _sql(db, retry_call)
        assert db.execute.call_args_list[retry_call].args[1]["organization_id"] == org


def test_strict_hit_does_not_retry() -> None:
    db = _db([2])

    response = FullTextSearchService().search(
        search_request=_request("attention mechanism"),
        user_id="u-1",
        organization_id=str(uuid4()),
        db=db,
    )

    assert _match_calls(db) == 2
    assert response.total_results == 2


def test_single_term_zero_hit_does_not_retry() -> None:
    db = _db([0])

    FullTextSearchService().search(
        search_request=_request("transformers"),
        user_id="u-1",
        organization_id=str(uuid4()),
        db=db,
    )

    assert _match_calls(db) == 2


@pytest.mark.parametrize("builder", ["_build_search_query", "_build_count_query"])
def test_builders_default_to_all_term_match(builder: str) -> None:
    sql, _ = getattr(FullTextSearchService(), builder)(
        _request("attention mechanism"), "u-1", str(uuid4())
    )
    assert ANY_TERM_TSQUERY not in sql

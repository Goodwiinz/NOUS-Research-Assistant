"""keyword_search (GOO-435 S5c): the SQL it builds and when it answers ``[]``.

Like ``test_semantic_search_service.py``, the statement is compiled for
PostgreSQL and inspected: the tenant predicates and the publication window are
properties of the SQL text (they must sit in the WHERE, before ORDER BY ...
LIMIT). ``tests/integration/test_discover_papers_postgres.py`` runs the same
statement on PostgreSQL.

Mutation check (2026-10-13). With the ``organization_id`` predicate, the
``project_scope_condition`` call, or either ``published_on`` bound removed from
``build_keyword_search_statement``, the test named after it fails
(``test_the_organization_is_always_filtered``,
``test_project_scope_is_an_exists_over_live_links_collections_and_workspaces``,
``test_the_window_is_in_the_where_before_order_and_limit``). Restored; focused
command: ``pytest -q backend/tests/unit/services/test_keyword_search_service.py``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from src.services.search.keyword_search_service import (
    MAX_LIMIT,
    build_keyword_search_statement,
    keyword_search,
    search_terms,
)

pytestmark = pytest.mark.unit

ORG = uuid.uuid4()
PROJECT = uuid.uuid4()


def _compile(**overrides: Any) -> str:
    arguments: dict[str, Any] = {
        "terms": "grpo reward",
        "organization_id": ORG,
        "project_ids": None,
        "published_after": None,
        "published_before": None,
        "limit": 20,
    }
    arguments.update(overrides)
    statement = build_keyword_search_statement(**arguments)
    return str(statement.compile(dialect=postgresql.dialect()))


def test_search_terms_match_the_fulltext_service_preparation() -> None:
    assert search_terms("GRPO, Reward-Models (v2)!") == "grpo reward models v2"
    assert search_terms("a b I x") == ""  # one-letter words are not indexed
    assert search_terms("") == "" and search_terms(None) == ""  # type: ignore[arg-type]
    from src.services.search.fulltext_search_service import fulltext_search_service

    for query in ("Attention is all you need", "q-learning, in 2020?", "LoRA/QLoRA"):
        assert search_terms(query) == fulltext_search_service._prepare_search_terms(
            query
        )


def test_the_organization_is_always_filtered() -> None:
    sql = _compile()

    assert "documents.organization_id = " in sql
    assert sql.index("WHERE") < sql.index("documents.organization_id = ")
    assert sql.index("documents.organization_id = ") < sql.index("ORDER BY")


def test_deleted_and_unfinished_documents_are_excluded() -> None:
    sql = _compile()

    assert "documents.is_deleted IS false" in sql
    assert "documents.processing_status = " in sql


def test_matching_is_plainto_tsquery_over_the_english_search_vector() -> None:
    sql = _compile()

    assert "documents.search_vector @@ plainto_tsquery('english'::regconfig" in sql
    assert "ts_rank_cd(documents.search_vector, plainto_tsquery(" in sql


def test_project_scope_is_an_exists_over_live_links_collections_and_workspaces() -> (
    None
):
    sql = _compile(project_ids={PROJECT})

    exists = sql[sql.index("EXISTS") : sql.index("ORDER BY")]
    assert "collection_documents.collection_id IN" in exists
    assert "collection_documents.is_deleted IS false" in exists
    assert "collections.is_deleted IS false" in exists
    assert "workspaces.is_deleted IS false" in exists


def test_no_project_scope_means_no_collection_join() -> None:
    assert "collection_documents" not in _compile(project_ids=None)


def test_the_window_is_in_the_where_before_order_and_limit() -> None:
    sql = _compile(
        published_after=date(2020, 1, 1), published_before=date(2020, 12, 31)
    )
    where, order = sql.index("WHERE"), sql.index("ORDER BY")

    assert where < sql.index(" >= ") < order
    assert where < sql.index(" <= ") < order
    # One flat statement: no outer query filtering what an inner LIMIT kept.
    assert sql.count("LIMIT") == 1 and sql.count("SELECT") == 1
    params = (
        build_keyword_search_statement(
            terms="x",
            organization_id=ORG,
            project_ids=None,
            published_after=date(2020, 1, 1),
            published_before=date(2020, 12, 31),
            limit=5,
        )
        .compile(dialect=postgresql.dialect())
        .params
    )
    assert "2020-01-01" in params.values() and "2020-12-31" in params.values()


def test_no_window_adds_no_date_predicate() -> None:
    sql = _compile()

    assert " >= " not in sql and " <= " not in sql


def test_the_order_is_rank_then_newest_then_id() -> None:
    sql = _compile()
    order = sql[sql.index("ORDER BY") :]

    assert order.startswith("ORDER BY ts_rank_cd(")
    assert order.index(" DESC, coalesce(") < order.index(" DESC, documents.id")


async def test_an_invalid_organization_fails_closed() -> None:
    session = AsyncMock()
    for bad in (None, "", "not-a-uuid"):
        with pytest.raises(ValueError, match="organization"):
            await keyword_search(session, query="grpo", organization_id=bad)
    with pytest.raises(ValueError, match="UUIDs"):
        await keyword_search(
            session, query="grpo", organization_id=ORG, project_ids=["nope"]
        )
    session.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "arguments",
    [
        {"project_ids": []},  # an empty scope is no access, not the whole org
        {"query": "   "},
        {"query": "a b"},  # no word of two letters
        {"limit": 0},
        {
            "published_after": date(2021, 1, 1),
            "published_before": date(2020, 1, 1),
        },
    ],
    ids=["empty-scope", "blank", "no-words", "zero-limit", "reversed-window"],
)
async def test_unsearchable_requests_return_nothing_without_a_query(
    arguments: dict[str, Any],
) -> None:
    session = AsyncMock()
    hits = await keyword_search(
        session, **{"query": "grpo", "organization_id": ORG, **arguments}
    )

    assert hits == []
    session.execute.assert_not_awaited()


async def test_the_limit_is_clamped() -> None:
    session = AsyncMock()
    session.execute.return_value = []
    await keyword_search(session, query="grpo", organization_id=ORG, limit=10_000)

    statement = session.execute.await_args.args[0]
    assert statement._limit_clause.value == MAX_LIMIT

"""semantic_search (GOO-435 S5b): the SQL it builds and when it answers ``[]``.

The statement is compiled for PostgreSQL and inspected, because the properties
that matter (tenant predicates, the date window sitting before ``ORDER BY ...
LIMIT``) are properties of the SQL text. ``tests/integration/
test_semantic_search_postgres.py`` runs the same statement against pgvector.

Mutation check (2026-10-13). With either ``organization_id`` predicate removed
from ``build_semantic_search_statement`` (or both), ``test_both_tables_filter_
the_organization``, ``test_tenant_predicates_precede_order_and_limit`` and
``test_the_query_is_org_and_project_scoped_with_the_window`` fail
("...organization_id = " missing from the compiled SQL). Restored; focused
command: ``pytest -q backend/tests/unit/services/test_semantic_search_service.py
-k "organization or tenant"``. The other guards (project ``EXISTS``, the date
window, soft-delete, the empty-scope return, the plan switch) were mutated the
same way; each fails the test named after it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from src.core.config import settings
from src.models.collection import Collection
from src.models.document_embedding import DOCUMENT_EMBEDDING_DIMENSIONS
from src.models.vector import EmbeddingResponse
from src.services.embedding.document_embedding_service import DocumentEmbeddingService
from src.services.search import semantic_search_service as svc
from src.services.search.semantic_search_service import (
    MAX_LIMIT,
    SemanticSearchHit,
    SemanticSearchUnavailable,
    build_semantic_search_statement,
    semantic_search,
    semantic_search_strict,
)

pytestmark = pytest.mark.unit

DIM = DOCUMENT_EMBEDDING_DIMENSIONS
ORG = uuid.uuid4()
PROJECT = uuid.uuid4()


def _compile(**overrides: Any) -> str:
    arguments: dict[str, Any] = {
        "query_vector": [0.1] * DIM,
        "organization_id": ORG,
        "project_ids": None,
        "published_after": None,
        "published_before": None,
        "limit": 20,
    }
    arguments.update(overrides)
    statement = build_semantic_search_statement(**arguments)
    return str(statement.compile(dialect=postgresql.dialect()))


# --- the compiled statement ----------------------------------------------------


def test_both_tables_filter_the_organization() -> None:
    sql = _compile()

    assert "document_embeddings.organization_id = " in sql
    assert "documents.organization_id = " in sql


def test_soft_deleted_and_unfinished_documents_are_excluded() -> None:
    sql = _compile()

    assert "documents.is_deleted IS false" in sql
    assert "documents.processing_status = " in sql


def test_tenant_predicates_precede_order_and_limit() -> None:
    sql = _compile()
    where, order, limit = sql.index("WHERE"), sql.index("ORDER BY"), sql.index("LIMIT")

    assert where < sql.index("document_embeddings.organization_id = ") < order < limit
    assert where < sql.index("documents.organization_id = ") < order < limit


def test_date_window_is_in_the_where_before_order_and_limit() -> None:
    """The alphaXiv defect: filtering a bounded pool after the fact."""
    sql = _compile(
        published_after=date(2020, 1, 1), published_before=date(2020, 12, 31)
    )
    where, order, limit = sql.index("WHERE"), sql.index("ORDER BY"), sql.index("LIMIT")
    after = sql.index(" >= ")
    before = sql.index(" <= ")

    assert where < after < order < limit
    assert where < before < order < limit
    # One flat statement: no outer query filtering what an inner LIMIT kept.
    assert sql.count("LIMIT") == 1 and sql.count("SELECT") == 1
    assert "FROM (SELECT" not in sql


def test_window_bounds_are_iso_day_strings_inclusive() -> None:
    statement = build_semantic_search_statement(
        query_vector=[0.1] * DIM,
        organization_id=ORG,
        project_ids=None,
        published_after=date(2020, 1, 1),
        published_before=date(2020, 12, 31),
        limit=5,
    )
    params = statement.compile(dialect=postgresql.dialect()).params

    assert "2020-01-01" in params.values() and "2020-12-31" in params.values()


def test_no_window_adds_no_date_predicate() -> None:
    sql = _compile()

    assert " >= " not in sql and " <= " not in sql


def test_project_scope_is_an_exists_over_live_links_collections_and_workspaces() -> (
    None
):
    sql = _compile(project_ids={PROJECT})

    exists = sql[sql.index("EXISTS") : sql.index("ORDER BY")]
    assert "collection_documents.document_id = documents.id" in exists
    assert "collection_documents.collection_id IN" in exists
    assert "collection_documents.is_deleted IS false" in exists
    assert "collections.is_deleted IS false" in exists
    assert "workspaces.is_deleted IS false" in exists
    assert sql.index("EXISTS") < sql.index("ORDER BY") < sql.index("LIMIT")


def test_no_project_scope_means_no_collection_join() -> None:
    assert "collection_documents" not in _compile(project_ids=None)


def test_a_collection_scope_is_one_condition_not_an_id_list() -> None:
    """A workspace scope binds its one workspace id, however many Collections
    the workspace holds (asyncpg refuses more than 32,767 parameters)."""
    workspace = uuid.uuid4()
    statement = build_semantic_search_statement(
        query_vector=[0.1] * DIM,
        organization_id=ORG,
        project_ids=None,
        published_after=None,
        published_before=None,
        limit=20,
        collection_scope=Collection.workspace_id == workspace,
    )
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    exists = sql[sql.index("EXISTS") : sql.index("ORDER BY")]
    assert "collections.workspace_id = " in exists
    assert "collection_documents.collection_id IN" not in exists
    assert "collections.is_deleted IS false" in exists
    assert "workspaces.is_deleted IS false" in exists
    assert list(compiled.params.values()).count(workspace) == 1


def test_ordering_is_the_cosine_operator_so_the_hnsw_index_can_serve_it() -> None:
    sql = _compile()
    order_by = sql[sql.index("ORDER BY") :]

    assert order_by.startswith("ORDER BY document_embeddings.embedding <=> ")
    assert "+" not in order_by.split("LIMIT")[0]


def test_exact_plan_wraps_the_distance_so_no_index_can_serve_it() -> None:
    sql = _compile(exact=True)
    order_by = sql[sql.index("ORDER BY") :].split("LIMIT")[0]

    assert "document_embeddings.embedding <=> " in order_by and " + " in order_by


def test_limit_is_bound() -> None:
    statement = build_semantic_search_statement(
        query_vector=[0.1] * DIM,
        organization_id=ORG,
        project_ids=None,
        published_after=None,
        published_before=None,
        limit=7,
    )

    assert statement._limit == 7


# --- semantic_search -------------------------------------------------------------


class FakeEmbedder:
    embedding_provider = "cohere"

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector if vector is not None else [0.2] * DIM
        self.fail: Exception | None = None
        self.queries: list[str] = []

    def get_model_info(self) -> dict[str, Any]:
        return {}

    async def generate_embedding(self, request: Any) -> EmbeddingResponse:
        self.queries.append(request.text)
        if self.fail is not None:
            raise self.fail
        return EmbeddingResponse(
            embedding=self.vector,
            model="embed-v-4-0",
            dimension=len(self.vector),
            processing_time=0.0,
            provider="cohere",
        )


class FakeSession:
    """Answers the version probe, records statements, returns canned rows."""

    def __init__(self, rows: list[Any] | None = None, version: str = "0.8.0") -> None:
        self.rows = rows or []
        self.version = version
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        text = str(statement)
        if "extversion" in text:
            return SimpleNamespace(scalar=lambda: self.version)
        if "ORDER BY" in text:
            return iter(self.rows)
        return SimpleNamespace(scalar=lambda: None)

    @property
    def search(self) -> Any:
        (statement,) = [s for s in self.statements if "ORDER BY" in str(s)]
        return statement

    @property
    def settings_sql(self) -> list[str]:
        return [
            str(s.compile(dialect=postgresql.dialect()))
            for s in self.statements
            if "set_config" in str(s)
        ]


@pytest.fixture
def channel_on(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    monkeypatch.setattr(settings, "SEMANTIC_SEARCH_ENABLED", True)
    embedder = FakeEmbedder()
    monkeypatch.setattr(
        svc, "document_embedding_service", DocumentEmbeddingService(embedder=embedder)
    )
    return embedder


async def test_flag_off_returns_nothing_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SEMANTIC_SEARCH_ENABLED", False)
    embedder = FakeEmbedder()
    monkeypatch.setattr(
        svc, "document_embedding_service", DocumentEmbeddingService(embedder=embedder)
    )
    session = FakeSession()

    hits = await semantic_search(session, query="graphs", organization_id=ORG)  # type: ignore[arg-type]

    assert hits == []
    assert session.statements == [] and embedder.queries == []


async def test_query_embedding_failure_returns_nothing(
    channel_on: FakeEmbedder,
) -> None:
    channel_on.fail = RuntimeError("cohere is down")
    session = FakeSession()

    assert await semantic_search(session, query="graphs", organization_id=ORG) == []  # type: ignore[arg-type]
    assert session.statements == []


@pytest.mark.parametrize("length", [384, 1536, DIM + 1])
async def test_query_vector_of_the_wrong_length_returns_nothing(
    channel_on: FakeEmbedder, length: int
) -> None:
    channel_on.vector = [0.2] * length
    session = FakeSession()

    assert await semantic_search(session, query="graphs", organization_id=ORG) == []  # type: ignore[arg-type]
    assert session.statements == []


async def test_a_non_storable_provider_returns_nothing(
    channel_on: FakeEmbedder,
) -> None:
    channel_on.embedding_provider = "sentence_transformers"
    session = FakeSession()

    assert await semantic_search(session, query="graphs", organization_id=ORG) == []  # type: ignore[arg-type]
    assert channel_on.queries == [] and session.statements == []


# --- semantic_search_strict: "cannot answer" is not "no hits" ----------------------


async def test_strict_search_says_why_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SEMANTIC_SEARCH_ENABLED", False)
    session = FakeSession()

    with pytest.raises(SemanticSearchUnavailable) as unavailable:
        await semantic_search_strict(session, query="graphs", organization_id=ORG)  # type: ignore[arg-type]

    assert unavailable.value.reason == "disabled"
    assert session.statements == []


async def test_strict_search_raises_when_the_provider_fails(
    channel_on: FakeEmbedder,
) -> None:
    channel_on.fail = RuntimeError("cohere is down: key=sk-secret")
    session = FakeSession()

    with pytest.raises(SemanticSearchUnavailable) as unavailable:
        await semantic_search_strict(session, query="graphs", organization_id=ORG)  # type: ignore[arg-type]

    assert unavailable.value.reason == "embedding_failed"
    assert "sk-secret" not in str(unavailable.value)  # only the stable token
    assert session.statements == []
    # The default contract is unchanged for every other caller.
    assert await semantic_search(session, query="graphs", organization_id=ORG) == []  # type: ignore[arg-type]


@pytest.mark.parametrize("length", [384, DIM + 1])
async def test_strict_search_raises_for_a_vector_the_column_cannot_hold(
    channel_on: FakeEmbedder, length: int
) -> None:
    channel_on.vector = [0.2] * length
    session = FakeSession()

    with pytest.raises(SemanticSearchUnavailable) as unavailable:
        await semantic_search_strict(session, query="graphs", organization_id=ORG)  # type: ignore[arg-type]

    assert unavailable.value.reason == "embedding_rejected"
    assert await semantic_search(session, query="graphs", organization_id=ORG) == []  # type: ignore[arg-type]


async def test_strict_search_with_no_hits_is_an_empty_list_not_unavailable(
    channel_on: FakeEmbedder,
) -> None:
    session = FakeSession(rows=[])

    assert (
        await semantic_search_strict(session, query="graphs", organization_id=ORG)  # type: ignore[arg-type]
        == []
    )
    assert len(session.statements) >= 1  # it did search


async def test_blank_query_and_inverted_window_return_nothing(
    channel_on: FakeEmbedder,
) -> None:
    session = FakeSession()

    assert await semantic_search(session, query="   ", organization_id=ORG) == []  # type: ignore[arg-type]
    assert (
        await semantic_search(
            session,  # type: ignore[arg-type]
            query="graphs",
            organization_id=ORG,
            published_after=date(2021, 1, 1),
            published_before=date(2020, 1, 1),
        )
        == []
    )
    assert session.statements == [] and channel_on.queries == []


async def test_an_empty_project_scope_is_no_access_not_the_whole_organization(
    channel_on: FakeEmbedder,
) -> None:
    session = FakeSession()

    assert (
        await semantic_search(
            session, query="graphs", organization_id=ORG, project_ids=[]  # type: ignore[arg-type]
        )
        == []
    )
    assert session.statements == [] and channel_on.queries == []


@pytest.mark.parametrize("organization", [None, "", "not-a-uuid", "None"])
async def test_an_invalid_organization_fails_closed(
    channel_on: FakeEmbedder, organization: Any
) -> None:
    with pytest.raises(ValueError, match="organization scope"):
        await semantic_search(
            FakeSession(), query="graphs", organization_id=organization  # type: ignore[arg-type]
        )


async def test_project_ids_and_a_collection_scope_are_exclusive(
    channel_on: FakeEmbedder,
) -> None:
    session = FakeSession()
    with pytest.raises(ValueError, match="collection_scope"):
        await semantic_search(
            session,  # type: ignore[arg-type]
            query="graphs",
            organization_id=ORG,
            project_ids=[PROJECT],
            collection_scope=Collection.id == PROJECT,
        )
    assert session.statements == [] and channel_on.queries == []


async def test_an_invalid_project_id_raises(channel_on: FakeEmbedder) -> None:
    with pytest.raises(ValueError, match="project_ids"):
        await semantic_search(
            FakeSession(), query="graphs", organization_id=ORG, project_ids=["x"]  # type: ignore[arg-type]
        )


async def test_the_query_is_org_and_project_scoped_with_the_window(
    channel_on: FakeEmbedder,
) -> None:
    session = FakeSession()

    await semantic_search(
        session,  # type: ignore[arg-type]
        query="  graph\nnetworks ",
        organization_id=str(ORG),
        project_ids=[str(PROJECT)],
        published_after=datetime(2020, 1, 1, 12, 30, tzinfo=timezone.utc),
        published_before=date(2020, 12, 31),
    )

    compiled = session.search.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert channel_on.queries == ["graph networks"]  # whitespace normalised
    assert "document_embeddings.organization_id = " in sql
    assert "documents.organization_id = " in sql
    assert "EXISTS" in sql and "2020-01-01" in compiled.params.values()
    assert "2020-12-31" in compiled.params.values()
    assert str(ORG) in {str(v) for v in compiled.params.values()}


@pytest.mark.parametrize(
    ("asked", "bound"), [(1000, MAX_LIMIT), (3, 3), (MAX_LIMIT, MAX_LIMIT)]
)
async def test_limit_is_clamped(
    channel_on: FakeEmbedder, asked: int, bound: int
) -> None:
    session = FakeSession()

    await semantic_search(session, query="g", organization_id=ORG, limit=asked)  # type: ignore[arg-type]

    assert session.search._limit == bound


async def test_non_positive_limit_returns_nothing(channel_on: FakeEmbedder) -> None:
    session = FakeSession()

    assert await semantic_search(session, query="g", organization_id=ORG, limit=0) == []  # type: ignore[arg-type]
    assert session.statements == []


# --- index plan --------------------------------------------------------------------


@pytest.mark.parametrize(("asked", "ef"), [(5, 40), (10, 40), (11, 44), (50, 200)])
async def test_new_pgvector_sets_ef_search_and_iterates_the_index(
    channel_on: FakeEmbedder, asked: int, ef: int
) -> None:
    session = FakeSession(version="0.8.0")

    await semantic_search(session, query="g", organization_id=ORG, limit=asked)  # type: ignore[arg-type]

    (settings_sql,) = session.settings_sql
    compiled = [
        s.compile(dialect=postgresql.dialect())
        for s in session.statements
        if "set_config" in str(s)
    ][0]
    assert "hnsw.ef_search" in compiled.params.values()
    assert str(ef) in compiled.params.values()
    assert "strict_order" in compiled.params.values()
    assert "set_config" in settings_sql
    sql = str(session.search.compile(dialect=postgresql.dialect()))
    assert "+" not in sql[sql.index("ORDER BY") :].split("LIMIT")[0]  # index-eligible


@pytest.mark.parametrize("version", ["0.7.4", "0.6.2", "0.5.1", None])
async def test_old_pgvector_gets_the_exact_plan_and_no_hnsw_settings(
    channel_on: FakeEmbedder, version: str | None
) -> None:
    session = FakeSession(version=version)  # type: ignore[arg-type]

    await semantic_search(session, query="g", organization_id=ORG)  # type: ignore[arg-type]

    assert session.settings_sql == []
    sql = str(session.search.compile(dialect=postgresql.dialect()))
    order_by = sql[sql.index("ORDER BY") :].split("LIMIT")[0]
    assert " + " in order_by


@pytest.mark.parametrize("version", ["0.8.0", "0.8.5", "0.10.0", "1.0.0", "0.9"])
async def test_iterative_scan_is_used_from_0_8(
    channel_on: FakeEmbedder, version: str
) -> None:
    session = FakeSession(version=version)

    await semantic_search(session, query="g", organization_id=ORG)  # type: ignore[arg-type]

    assert len(session.settings_sql) == 1


# --- hits --------------------------------------------------------------------------


def _row(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "document_id": uuid.uuid4(),
        "title": "A paper",
        "published_on": "2020-03-15",
        "published_source": "metadata",
        "distance": 0.25,
        "summary": None,
        "description": None,
        "head": "# Abstract\n\nWe study   graphs.\n\n# Authors",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


async def test_rows_become_hits(channel_on: FakeEmbedder) -> None:
    row = _row()
    session = FakeSession(rows=[row])

    hits = await semantic_search(session, query="g", organization_id=ORG)  # type: ignore[arg-type]

    assert hits == [
        SemanticSearchHit(
            document_id=str(row.document_id),
            title="A paper",
            publication_date=date(2020, 3, 15),
            publication_date_source="metadata",
            similarity=0.75,
            snippet="We study graphs. # Authors",
        )
    ]


async def test_hit_dates_snippets_and_sources(channel_on: FakeEmbedder) -> None:
    rows = [
        _row(published_source="created_at", summary="  The summary. ", head="ignored"),
        _row(published_on="2020-02-31", description="From metadata.", summary=None),
        _row(published_on=None, summary=None, description=None, head=None),
        _row(summary="x" * 500),
    ]
    session = FakeSession(rows=rows)

    hits = await semantic_search(session, query="g", organization_id=ORG)  # type: ignore[arg-type]

    assert hits[0].publication_date_source == "created_at"
    assert hits[0].snippet == "The summary."  # summary wins over the text head
    assert hits[1].publication_date is None  # not a calendar date: no crash
    assert hits[1].snippet == "From metadata."
    assert hits[2].publication_date is None and hits[2].snippet == ""
    assert len(hits[3].snippet) == svc.SNIPPET_CHARS and hits[3].snippet.endswith("…")

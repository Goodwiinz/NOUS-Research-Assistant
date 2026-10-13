"""GOO-435 S5c on a real PostgreSQL with pgvector: keyword search and ``discover_papers``.

The unit tests (``tests/unit/services/test_keyword_search_service.py``,
``tests/unit/services/integrations/test_discover_papers.py``) fake the
channels. This module runs the real library channels against a scratch database
built by ``alembic upgrade head``, because what matters here is SQL: the grant
scope and the publication window as predicates of one statement, the tsquery
match, the identity query's JSON paths and the SAVEPOINT that keeps a session
usable when ``document_embeddings`` does not exist.

Uses the same lane as ``test_semantic_search_postgres.py`` (skips unless
``TWO_ACCOUNT_PG_TEST_DATABASE_URL`` names a disposable server, and unless that
server has pgvector).

Mutation-verified (2026-10-13) on PostgreSQL 18.4 with pgvector 0.8.5: each guard
removed in turn, the focused tests observed failing on the named defect, the guard
restored (the source file byte-identical to its backup) and the tests passing
again. Focused command: ``TWO_ACCOUNT_PG_TEST_DATABASE_URL=... pytest
backend/tests/integration/test_discover_papers_postgres.py -c backend/pytest.ini
-m integration -k "<expr>"``.

- the ``project_scope_condition`` call in ``build_keyword_search_statement``:
  ``test_keyword_search_is_scoped_to_the_grant_and_the_window`` fails on another
  project's documents being returned. ``discover_papers`` repeats the scope in
  its identity query, so with only the first removed the end-to-end tests still
  pass (defence in depth); with both removed
  ``test_a_document_outside_the_grant_never_appears[sibling]`` fails on the
  sibling project's document being returned;
- the ``Document.organization_id`` predicate in the same statement:
  ``test_keyword_search_is_scoped_to_the_grant_and_the_window`` fails on the
  other organization's document (searched without a project scope, where the
  scope predicate cannot back it up);
- the two ``published_on`` bounds: the same test fails on recent documents inside a
  2020 window (``published_before`` also fails ``test_discover_papers_end_to_end``;
  ``published_after`` also ``test_keyword_search_dates_and_matching``);
- the SAVEPOINT around ``semantic_search`` in ``_library_semantic``: with the table
  missing, ``test_a_missing_embeddings_table_leaves_the_session_usable`` fails on
  the follow-up query ("current transaction is aborted");
- ``_embeddings_table_missing`` always false: the same test fails (the missing
  table is an error, not "unavailable").
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from src.core.config import settings
from src.models import (
    Collection,
    CollectionDocument,
    Document,
    Organization,
    StorageTier,
    User,
    UserRole,
    Workspace,
)
from src.models.document import DocumentType, ProcessingStatus
from src.models.document_embedding import DocumentEmbedding
from src.services.embedding.document_embedding_service import DocumentEmbeddingService
from src.services.integrations import discover_papers as dp
from src.services.search import semantic_search_service
from src.services.search.keyword_search_service import keyword_search
from tests.integration.test_semantic_search_postgres import (  # noqa: F401
    QUERY_VECTOR,
    FakeEmbedder,
    async_engine,
    scratch_database,
    sid,
    sync_engine,
    vector,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

RAG_TITLE = "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks"
QUERY = "retrieval augmented generation"


def _document(
    key: str,
    org: str,
    *,
    title: str | None = None,
    published: str | None = None,
    created: datetime | None = None,
    status: ProcessingStatus = ProcessingStatus.COMPLETED,
    deleted: bool = False,
    arxiv_id: str | None = None,
    authors: list[str] | None = None,
) -> Document:
    metadata: dict[str, Any] = {}
    if published:
        metadata["publication_date"] = published
    if authors:
        metadata["authors"] = authors
    return Document(
        id=sid(f"disc-{key}"),
        title=title or f"Note {key}",
        filename=f"{key}.txt",
        file_path=f"/tmp/{key}.txt",
        file_size_bytes=10,
        mime_type="text/plain",
        document_type=DocumentType.TEXT,
        content_text=f"# Abstract\n\nRetrieval augmented generation notes, item {key}.",
        document_metadata=metadata,
        processing_status=status,
        organization_id=sid(f"disc-org-{org}"),
        is_deleted=deleted,
        arxiv_id=arxiv_id,
        created_at=created or datetime.now(timezone.utc),
    )


@pytest.fixture(scope="module")
def seeded(sync_engine: Engine) -> dict[str, uuid.UUID]:
    """Two organizations; org-a has two projects (alpha, beta), org-b one."""
    now = datetime.now(timezone.utc)
    docs: list[tuple[Document, float, str]] = [
        (
            _document(
                "rag2020",
                "a",
                title=RAG_TITLE,
                published="2020-12-07T00:00:00+00:00",
                arxiv_id="2005.11401",
                authors=["Patrick Lewis", "Ethan Perez"],
            ),
            0.40,
            "alpha",
        ),
        (_document("nodate", "a", created=now - timedelta(days=3)), 0.85, "alpha"),
        # In the same organization, but another project: outside an alpha grant.
        (_document("sibling", "a", published="2024-01-01"), 0.95, "beta"),
        (
            _document("deleted", "a", published="2020-04-01", deleted=True),
            0.99,
            "alpha",
        ),
        (
            _document(
                "processing",
                "a",
                published="2020-04-02",
                status=ProcessingStatus.PROCESSING,
            ),
            0.98,
            "alpha",
        ),
        (_document("other-org", "b", published="2020-04-03"), 0.999, "gamma"),
    ]
    # 120 recent papers in alpha, all closer to the query than the 2020 one.
    for index in range(120):
        docs.append(
            (
                _document(f"recent-{index:03d}", "a", published="2024-05-01"),
                0.90 - index * 0.002,
                "alpha",
            )
        )
    with Session(sync_engine) as session:
        session.add_all(
            [
                Organization(
                    id=sid(f"disc-org-{name}"),
                    name=f"Discover org {name}",
                    storage_tier=StorageTier.FREE,
                    storage_limit_bytes=Organization.get_default_storage_limit(
                        StorageTier.FREE
                    ),
                    is_active=True,
                )
                for name in ("a", "b")
            ]
        )
        session.flush()
        session.add_all(
            [
                User(
                    id=sid(f"disc-user-{name}"),
                    email=f"discover-{name}@example.invalid",
                    password_hash="unused",
                    first_name=name,
                    last_name="Discover",
                    role=UserRole.USER,
                    is_active=True,
                    organization_id=sid(f"disc-org-{name}"),
                )
                for name in ("a", "b")
            ]
        )
        session.flush()
        session.add_all(
            [
                Workspace(
                    id=sid("disc-ws-a"),
                    name="ws a",
                    owner_id=sid("disc-user-a"),
                    organization_id=sid("disc-org-a"),
                ),
                Workspace(
                    id=sid("disc-ws-b"),
                    name="ws b",
                    owner_id=sid("disc-user-b"),
                    organization_id=sid("disc-org-b"),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Collection(
                    id=sid("disc-col-alpha"),
                    workspace_id=sid("disc-ws-a"),
                    name="alpha",
                ),
                Collection(
                    id=sid("disc-col-beta"), workspace_id=sid("disc-ws-a"), name="beta"
                ),
                Collection(
                    id=sid("disc-col-gamma"),
                    workspace_id=sid("disc-ws-b"),
                    name="gamma",
                ),
            ]
        )
        session.flush()
        session.add_all([document for document, _, _ in docs])
        session.flush()
        for seed, (document, similarity, collection) in enumerate(docs, start=1):
            session.add(
                DocumentEmbedding(
                    document_id=document.id,
                    organization_id=document.organization_id,
                    embedding=vector(similarity, seed),
                    model="cohere:embed-v-4-0",
                    content_hash="0" * 64,
                )
            )
            session.add(
                CollectionDocument(
                    collection_id=sid(f"disc-col-{collection}"), document_id=document.id
                )
            )
        session.flush()
        # The same vector the ingest paths build (FullTextSearchService).
        session.execute(
            text(
                "UPDATE documents SET search_vector = "
                "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
                "setweight(to_tsvector('english', coalesce(content_text, '')), 'B') "
                "WHERE id::text IN (SELECT document_id::text FROM document_embeddings "
                "WHERE organization_id IN (:a, :b))"
            ),
            {"a": sid("disc-org-a"), "b": sid("disc-org-b")},
        )
        session.commit()
    return {
        "org-a": sid("disc-org-a"),
        "org-b": sid("disc-org-b"),
        "alpha": sid("disc-col-alpha"),
        "beta": sid("disc-col-beta"),
        "gamma": sid("disc-col-gamma"),
    }


@pytest.fixture(autouse=True)
def semantic_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SEMANTIC_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "DOCUMENT_EMBEDDINGS_ENABLED", True)
    monkeypatch.setattr(
        semantic_search_service,
        "document_embedding_service",
        DocumentEmbeddingService(embedder=FakeEmbedder(QUERY_VECTOR)),
    )
    monkeypatch.setattr(dp, "_unprovisioned_logged", False)


def _ids(*names: str) -> list[str]:
    return [str(sid(f"disc-{name}")) for name in names]


# --- keyword_search ------------------------------------------------------------------


async def test_keyword_search_is_scoped_to_the_grant_and_the_window(
    async_engine: AsyncEngine, seeded: dict[str, uuid.UUID]
) -> None:
    async def search(**kwargs: Any) -> list[Any]:
        async with AsyncSession(async_engine) as session:
            return await keyword_search(session, query=QUERY, **kwargs)

    alpha = [seeded["alpha"]]
    # The 2020 window: 120 recent papers match the words, one 2020 paper is in
    # the window. A window applied after LIMIT would return nothing.
    windowed = await search(
        organization_id=seeded["org-a"],
        project_ids=alpha,
        published_after=date(2020, 1, 1),
        published_before=date(2020, 12, 31),
        limit=5,
    )
    assert [hit.document_id for hit in windowed] == _ids("rag2020")
    assert windowed[0].publication_date == date(2020, 12, 7)
    assert windowed[0].publication_date_source == "metadata"
    assert windowed[0].rank > 0 and "Retrieval" in windowed[0].title

    # Tenancy: the grant's project only. The sibling project's document,
    # another organization's, a deleted and an unfinished document never match.
    everything = await search(
        organization_id=seeded["org-a"], project_ids=alpha, limit=50
    )
    found = {hit.document_id for hit in everything}
    assert found and not found & set(
        _ids("sibling", "other-org", "deleted", "processing")
    )
    assert str(sid("disc-rag2020")) in found and str(sid("disc-nodate")) in found
    # Without a project scope it is the whole organization (the sibling shows
    # up) and still never another organization's document.
    january = {
        "published_after": date(2024, 1, 1),
        "published_before": date(2024, 1, 31),
    }
    org_wide = {
        hit.document_id
        for hit in await search(organization_id=seeded["org-a"], **january)
    }
    assert org_wide == {str(sid("disc-sibling"))}
    # ... and the other organization's document is in no organization-wide search.
    april = {"published_after": date(2020, 4, 3), "published_before": date(2020, 4, 3)}
    assert await search(organization_id=seeded["org-a"], **april) == []
    assert [
        hit.document_id
        for hit in await search(organization_id=seeded["org-b"], **april)
    ] == _ids("other-org")
    other = await search(organization_id=seeded["org-b"], limit=50)
    assert [hit.document_id for hit in other] == _ids("other-org")
    # An empty scope is no access.
    assert await search(organization_id=seeded["org-a"], project_ids=[]) == []
    # Another organization's project yields nothing for this organization.
    assert (
        await search(organization_id=seeded["org-a"], project_ids=[seeded["gamma"]])
        == []
    )


async def test_keyword_search_dates_and_matching(
    async_engine: AsyncEngine, seeded: dict[str, uuid.UUID]
) -> None:
    async with AsyncSession(async_engine) as session:
        recent = await keyword_search(
            session,
            query=QUERY,
            organization_id=seeded["org-a"],
            project_ids=[seeded["alpha"]],
            published_after=date.today() - timedelta(days=5),
        )
        every_word = await keyword_search(
            session,
            query="retrieval augmented quokka",
            organization_id=seeded["org-a"],
        )
        title_rank = await keyword_search(
            session,
            query="knowledge intensive",
            organization_id=seeded["org-a"],
            project_ids=[seeded["alpha"]],
        )

    # Documents without a publication date use the day they were ingested.
    assert {hit.document_id for hit in recent} == set(_ids("nodate"))
    assert recent[0].publication_date_source == "created_at"
    assert every_word == []  # every query word must match
    assert [hit.document_id for hit in title_rank] == _ids("rag2020")


# --- discover_papers ---------------------------------------------------------------------


@pytest_asyncio.fixture
async def channels(async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(async_engine, expire_on_commit=False) as opened:
            yield opened

    monkeypatch.setattr(dp, "tool_session", session)


async def _arxiv(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "papers": [
            {
                "id": "2005.11401v4",
                "title": "Retrieval-Augmented Generation",
                "authors": ["P. Lewis"],
                "abstract": "RAG.",
                "published": "2020-05-22T17:41:13Z",
            },
            {
                "id": "2406.00001",
                "title": "Another arXiv paper that is not in the library",
                "authors": [],
                "abstract": "Other.",
                "published": "2024-06-01T00:00:00Z",
            },
        ]
    }


def _request(**overrides: Any) -> dp.DiscoverRequest:
    arguments: dict[str, Any] = {
        "keywords": [QUERY],
        "question": "How can a language model retrieve before it generates?",
        "difficulty": 5,
    }
    arguments.update(overrides)
    return dp.parse_arguments(arguments)


async def test_discover_papers_end_to_end(
    channels: None, seeded: dict[str, uuid.UUID]
) -> None:
    payload = await dp.discover(
        _request(published_after="2020-01-01", published_before="2020-12-31"),
        organization_id=seeded["org-a"],
        project_ids=[seeded["alpha"]],
        arxiv_search=_arxiv,
        today=date(2026, 10, 13),
    )

    assert {call["name"]: call["status"] for call in payload["calls"]} == {
        "library_keyword": "ok",
        "library_semantic": "ok",
        "arxiv_keyword": "ok",
    }
    # The 2020 paper is found by the keyword and the semantic search, although
    # 120 recent papers are closer to the question, and the arXiv copy of it is
    # shown as the library document.
    by_id = {item["id"]: item for item in payload["papers"]}
    rag = by_id[str(sid("disc-rag2020"))]
    assert rag["source"] == "library" and rag["title"] == RAG_TITLE
    assert rag["published"] == "2020-12-07" and rag["date_source"] == "metadata"
    assert rag["authors"] == ["Patrick Lewis", "Ethan Perez"]
    assert rag["found_by"] == ["library_keyword", "library_semantic", "arxiv_keyword"]
    assert "2005.11401v4" not in by_id
    # Tenancy: nothing outside the grant's project, the organization, or live.
    for hidden in ("sibling", "other-org", "deleted", "processing"):
        assert str(sid(f"disc-{hidden}")) not in by_id
    assert payload["papers"][0]["id"] == str(sid("disc-rag2020"))
    # Every id came from a call of this round (the arXiv fake is unwindowed, so
    # its second paper is allowed here).
    assert set(by_id) <= {str(sid("disc-rag2020")), "2406.00001"}


@pytest.mark.parametrize("hidden", ["sibling", "other-org", "deleted", "processing"])
async def test_a_document_outside_the_grant_never_appears(
    channels: None, seeded: dict[str, uuid.UUID], hidden: str
) -> None:
    # Each hidden document is the only match of its own keyword, and (the fake
    # embedder answers the same vector for every question) the semantic search
    # would rank all four first: 0.95-0.999 against 0.90 for the best visible one.
    # An alpha grant must still see none of them, from either library search.
    words = hidden.replace("-", " ").split()
    payload = await dp.discover(
        _request(
            keywords=words,
            sources=["library"],
            difficulty=10,
            published_after="2020-01-01",
        ),
        organization_id=seeded["org-a"],
        project_ids=[seeded["alpha"]],
        arxiv_search=_arxiv,
        today=date(2026, 10, 13),
    )

    assert str(sid(f"disc-{hidden}")) not in {p["id"] for p in payload["papers"]}
    statuses = {c["name"]: c["status"] for c in payload["calls"]}
    assert statuses["library_keyword"] == "empty"
    # The sibling's own project does find it, so the keyword is not the reason.
    if hidden == "sibling":
        both = await dp.discover(
            _request(keywords=words, sources=["library"]),
            organization_id=seeded["org-a"],
            project_ids=[seeded["alpha"], seeded["beta"]],
            arxiv_search=_arxiv,
            today=date(2026, 10, 13),
        )
        assert str(sid("disc-sibling")) in {p["id"] for p in both["papers"]}


async def test_discover_papers_without_a_window_is_capped_and_scoped(
    channels: None, seeded: dict[str, uuid.UUID]
) -> None:
    payload = await dp.discover(
        _request(difficulty=10),
        organization_id=seeded["org-a"],
        project_ids=[seeded["alpha"]],
        arxiv_search=_arxiv,
        today=date(2026, 10, 13),
    )

    papers = payload["papers"]
    assert 5 <= len(papers) <= 15
    library = {p["id"] for p in papers if p["source"] == "library"}
    assert str(sid("disc-sibling")) not in library
    assert str(sid("disc-other-org")) not in library
    assert payload["followup_rounds_allowed"] == 2


async def test_a_workspace_wide_scope_adds_every_project_of_the_workspace(
    channels: None, seeded: dict[str, uuid.UUID]
) -> None:
    payload = await dp.discover(
        _request(sources=["library"], difficulty=10, keywords=["note"]),
        organization_id=seeded["org-a"],
        project_ids=[seeded["alpha"], seeded["beta"]],
        arxiv_search=_arxiv,
        today=date(2026, 10, 13),
    )

    assert str(sid("disc-sibling")) in {p["id"] for p in payload["papers"]}
    assert "arxiv_keyword" not in {c["name"] for c in payload["calls"]}


# --- document_embeddings is missing ----------------------------------------------------------


async def test_a_missing_embeddings_table_leaves_the_session_usable(
    async_engine: AsyncEngine,
    seeded: dict[str, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Transactional DDL: drop the table inside a transaction that is rolled back,
    # so the other tests of this module still have it.
    async with async_engine.connect() as connection:
        outer = await connection.begin()
        try:
            await connection.execute(text("DROP TABLE document_embeddings"))
            session = AsyncSession(bind=connection, expire_on_commit=False)

            @asynccontextmanager
            async def shared() -> AsyncIterator[AsyncSession]:
                yield session

            monkeypatch.setattr(dp, "tool_session", shared)
            kwargs: dict[str, Any] = {
                "query": "retrieval augmented generation",
                "organization_id": seeded["org-a"],
                "project_ids": [seeded["alpha"]],
                "after": None,
                "before": None,
                "limit": 10,
            }
            with caplog.at_level("WARNING", logger=dp.logger.name):
                for _ in range(2):
                    with pytest.raises(dp.ChannelUnavailable) as unavailable:
                        await dp._library_semantic(**kwargs)
                    assert unavailable.value.reason == "not_provisioned"
                    # The SAVEPOINT absorbed the failed statement: the
                    # transaction is not aborted, so the session still works
                    # for the next call, library keyword included.
                    assert (await session.execute(text("SELECT 1"))).scalar() == 1
                    found = await dp._library_keyword(**kwargs)
                    assert str(sid("disc-rag2020")) in {c.id for c in found}
            assert len([r for r in caplog.records if r.levelno >= 30]) == 1

            # Through the round: unavailable, not an error, and the rest answers.
            record = await dp._run_call(
                dp._CallSpec(
                    "library_semantic",
                    "q",
                    10.0,
                    lambda: dp._library_semantic(**kwargs),
                )
            )
            assert (record.status, record.reason) == ("unavailable", "not_provisioned")
        finally:
            await session.close()
            await outer.rollback()
    # The table is back for the rest of the module.
    async with AsyncSession(async_engine) as check:
        count = (
            await check.execute(text("SELECT count(*) FROM document_embeddings"))
        ).scalar()
    assert count and count > 100


async def test_the_flag_off_is_unavailable_without_touching_the_database(
    channels: None, seeded: dict[str, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "SEMANTIC_SEARCH_ENABLED", False)
    payload = await dp.discover(
        _request(),
        organization_id=seeded["org-a"],
        project_ids=[seeded["alpha"]],
        arxiv_search=_arxiv,
        today=date(2026, 10, 13),
    )

    statuses = {c["name"]: (c["status"], c.get("reason")) for c in payload["calls"]}
    assert statuses["library_semantic"] == ("unavailable", "disabled")
    assert statuses["library_keyword"][0] == "ok"
    assert payload["papers"]

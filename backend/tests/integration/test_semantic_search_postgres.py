"""GOO-435 S5a/S5b on a real PostgreSQL with pgvector: schema, upsert, search.

The unit tests (``tests/unit/services/test_document_embedding_service.py``,
``test_semantic_search_service.py``) mock the session. This module runs the
same code against a scratch database built by the real ``alembic upgrade head``
(migration ``jd01_document_embeddings``) and the real asyncpg driver, because
the SQL it guards (``<=>``, HNSW, the ``set_config`` calls, the ``vector``
parameter binding, the date window) only means something there.

Uses the two-account PostgreSQL lane: skips unless
``TWO_ACCOUNT_PG_TEST_DATABASE_URL`` names a disposable server whose user may
``CREATE DATABASE``. It also skips, with its own reason, when that server has no
pgvector (the migration then deliberately creates no table; CI's stock
``postgres:15`` service is such a server). Every scratch database is dropped in
a ``finally``.

Mutation-verified (2026-10-13) on PostgreSQL 18.4 with pgvector 0.8.5 and, where
noted, PostgreSQL 16.2 with pgvector 0.6.2: each guard in
``backend/src/services/search/semantic_search_service.py`` removed in turn, the
focused tests observed failing on the named defect, the guard restored (file
byte-identical to the backup) and the tests passing again. Focused command:
``TWO_ACCOUNT_PG_TEST_DATABASE_URL=... pytest
backend/tests/integration/test_semantic_search_postgres.py -c backend/pytest.ini
-m integration -k "<expr>"``.

- both ``organization_id`` predicates in ``build_semantic_search_statement``:
  ``test_tenant_isolation_and_order`` fails on the other organization's closest
  document being returned (``test_project_scope`` and the window test fail with
  it). Removing only one of the two still passes here, because they back each
  other up; ``tests/unit/services/test_semantic_search_service.py`` fails on
  either alone.
- the ``exists()`` project predicate: ``test_project_scope`` fails on documents
  outside the Collection being returned.
- the two ``published_on`` predicates: ``test_window_reaches_past_the_most_
  similar_recent_documents`` fails on recent documents outside the 2020 window.
- the ``Document.is_deleted`` predicate: ``test_deleted_and_unprocessed_
  documents_never_match`` fails.
- ``hnsw.iterative_scan`` (0.8.5): the same window test fails, because the
  forced index scan then stops at ``hnsw.ef_search`` recent documents and the
  2020 window comes back empty.
- the exact plan for pgvector < 0.8 (0.6.2): the same window test fails the
  same way.
"""

from __future__ import annotations

import math
import os
import random
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, cast

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

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
from src.models.document_embedding import (
    DOCUMENT_EMBEDDING_DIMENSIONS,
    DocumentEmbedding,
)
from src.models.vector import BatchEmbeddingResponse, EmbeddingResponse
from src.services.embedding.document_embedding_service import (
    DocumentEmbeddingService,
    EmbedOutcome,
)
from src.services.search import semantic_search_service
from src.services.search.semantic_search_service import semantic_search
from tests.integration.two_account.conftest import pg_url

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DIM = DOCUMENT_EMBEDDING_DIMENSIONS
NS = uuid.UUID("3b1f0a3c-0b5d-4c6e-9f1a-2a7d3c4b5e61")


def sid(name: str) -> uuid.UUID:
    return uuid.uuid5(NS, name)


def vector(similarity: float, seed: int = 0) -> list[float]:
    """A unit vector whose cosine similarity to ``QUERY_VECTOR`` is ``similarity``.

    The rest of it is seeded noise, so the documents spread over the sphere the
    way real embeddings do (vectors on a single plane make HNSW unreachable
    pockets that real data does not have).
    """
    rng = random.Random(seed)
    noise = [rng.gauss(0.0, 1.0) for _ in range(DIM - 1)]
    scale = math.sqrt(max(0.0, 1.0 - similarity * similarity)) / math.sqrt(
        sum(value * value for value in noise)
    )
    return [similarity] + [value * scale for value in noise]


QUERY_VECTOR = vector(1.0)


class FakeEmbedder:
    """Stands in for ``EmbeddingService``: Cohere-shaped, returns ``answer``."""

    embedding_provider = "cohere"

    def __init__(self, answer: list[float]) -> None:
        self.answer = answer
        self.calls = 0

    def get_model_info(self) -> dict[str, Any]:
        return {}

    async def generate_embedding(self, request: Any) -> EmbeddingResponse:
        self.calls += 1
        return EmbeddingResponse(
            embedding=self.answer,
            model="embed-v-4-0",
            dimension=len(self.answer),
            processing_time=0.0,
            provider="cohere",
        )

    async def generate_batch_embeddings(self, request: Any) -> BatchEmbeddingResponse:
        self.calls += 1
        return BatchEmbeddingResponse(
            embeddings=[self.answer],
            model="embed-v-4-0",
            dimension=len(self.answer),
            processing_time=0.0,
            failed_count=0,
            provider="cohere",
        )


# --- scratch database --------------------------------------------------------


def _admin(*statements: str) -> None:
    admin = create_engine(
        pg_url("psycopg2"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)
    finally:
        admin.dispose()


@pytest.fixture(scope="module")
def scratch_database() -> Iterator[str]:
    name = f"sem_{uuid.uuid4().hex[:12]}"
    _admin(f'CREATE DATABASE "{name}"')
    try:
        url = make_url(pg_url("psycopg2")).set(database=name)
        env = {
            key: value for key, value in os.environ.items() if key != "SUPABASE_DB_URL"
        }
        env["DATABASE_URL"] = url.set(drivername="postgresql").render_as_string(
            hide_password=False
        )
        done = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert done.returncode == 0, done.stderr[-4000:]
        sync = create_engine(url, poolclass=NullPool)
        try:
            with sync.connect() as connection:
                present = connection.exec_driver_sql(
                    "SELECT to_regclass('document_embeddings') IS NOT NULL"
                ).scalar()
        finally:
            sync.dispose()
        if not present:
            pytest.skip("pgvector is not installed on this server (jd01 skipped)")
        yield name
    finally:
        _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="module")
def sync_engine(scratch_database: str) -> Iterator[Engine]:
    engine = create_engine(
        make_url(pg_url("psycopg2")).set(database=scratch_database),
        poolclass=NullPool,
    )
    yield engine
    engine.dispose()


@pytest_asyncio.fixture
async def async_engine(scratch_database: str) -> Any:
    engine: AsyncEngine = create_async_engine(
        make_url(pg_url("asyncpg")).set(database=scratch_database),
        poolclass=NullPool,
    )
    yield engine
    await engine.dispose()


# --- seed --------------------------------------------------------------------


def _document(
    key: str,
    org: str,
    *,
    published: str | None = None,
    created: datetime | None = None,
    status: ProcessingStatus = ProcessingStatus.COMPLETED,
    deleted: bool = False,
) -> Document:
    metadata: dict[str, Any] = {"title": key}
    if published:
        metadata["publication_date"] = published
    return Document(
        id=sid(f"doc-{key}"),
        title=f"Title {key}",
        filename=f"{key}.txt",
        file_path=f"/tmp/{key}.txt",
        file_size_bytes=10,
        mime_type="text/plain",
        document_type=DocumentType.TEXT,
        content_text=f"# Abstract\n\nBody text of {key}.",
        document_metadata=metadata,
        processing_status=status,
        organization_id=sid(f"org-{org}"),
        is_deleted=deleted,
        created_at=created or datetime.now(timezone.utc),
    )


@pytest.fixture(scope="module")
def seeded(sync_engine: Engine) -> dict[str, uuid.UUID]:
    """Two organizations; org-a has two Collections, org-b one closer document."""
    now = datetime.now(timezone.utc)
    docs: list[tuple[Document, float, str | None]] = (
        []
    )  # (document, similarity, collection)
    # Three 2020 papers, farther from the query than 120 recent ones.
    for index, similarity in enumerate((0.40, 0.35, 0.30)):
        docs.append(
            (
                _document(
                    f"y2020-{index}",
                    "a",
                    published="2020-03-1%dT00:00:00+00:00" % index,
                ),
                similarity,
                "alpha" if index < 2 else "beta",
            )
        )
    for index in range(120):
        docs.append(
            (
                _document(
                    f"recent-{index:03d}", "a", published="2024-05-01T00:00:00+00:00"
                ),
                0.90 - index * 0.002,
                "beta",
            )
        )
    docs.append(
        (_document("nodate", "a", created=now - timedelta(days=3)), 0.85, "alpha")
    )
    docs.append((_document("year-only", "a", published="2019"), 0.60, "alpha"))
    docs.append((_document("garbage-date", "a", published="not a date"), 0.59, "alpha"))
    docs.append(
        (_document("deleted", "a", published="2020-04-01", deleted=True), 0.99, "alpha")
    )
    docs.append(
        (
            _document(
                "processing",
                "a",
                published="2020-04-02",
                status=ProcessingStatus.PROCESSING,
            ),
            0.98,
            "alpha",
        )
    )
    docs.append((_document("other-org", "b", published="2020-04-03"), 0.999, "gamma"))
    with Session(sync_engine) as session:
        session.add_all(
            [
                Organization(
                    id=sid(f"org-{name}"),
                    name=f"Semantic org {name}",
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
                    id=sid(f"user-{name}"),
                    email=f"semantic-{name}@example.invalid",
                    password_hash="unused",
                    first_name=name,
                    last_name="Semantic",
                    role=UserRole.USER,
                    is_active=True,
                    organization_id=sid(f"org-{name}"),
                )
                for name in ("a", "b")
            ]
        )
        session.flush()
        session.add_all(
            [
                Workspace(
                    id=sid("ws-a"),
                    name="ws a",
                    owner_id=sid("user-a"),
                    organization_id=sid("org-a"),
                ),
                Workspace(
                    id=sid("ws-a-deleted"),
                    name="ws a deleted",
                    owner_id=sid("user-a"),
                    organization_id=sid("org-a"),
                    is_deleted=True,
                ),
                Workspace(
                    id=sid("ws-b"),
                    name="ws b",
                    owner_id=sid("user-b"),
                    organization_id=sid("org-b"),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Collection(id=sid("col-alpha"), workspace_id=sid("ws-a"), name="alpha"),
                Collection(id=sid("col-beta"), workspace_id=sid("ws-a"), name="beta"),
                Collection(
                    id=sid("col-gone"),
                    workspace_id=sid("ws-a"),
                    name="gone",
                    is_deleted=True,
                ),
                Collection(
                    id=sid("col-orphan"),
                    workspace_id=sid("ws-a-deleted"),
                    name="orphan",
                ),
                Collection(id=sid("col-gamma"), workspace_id=sid("ws-b"), name="gamma"),
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
            if collection:
                session.add(
                    CollectionDocument(
                        collection_id=sid(f"col-{collection}"), document_id=document.id
                    )
                )
        # Links that must not count: a soft-deleted link, a deleted Collection
        # and a Collection in a deleted Workspace.
        session.add_all(
            [
                CollectionDocument(
                    collection_id=sid("col-alpha"),
                    document_id=sid("doc-recent-005"),
                    is_deleted=True,
                ),
                CollectionDocument(
                    collection_id=sid("col-gone"), document_id=sid("doc-recent-006")
                ),
                CollectionDocument(
                    collection_id=sid("col-orphan"), document_id=sid("doc-recent-007")
                ),
            ]
        )
        session.commit()
    return {name: sid(name) for name in ("org-a", "org-b")}


@pytest.fixture(autouse=True)
def channel_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SEMANTIC_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "DOCUMENT_EMBEDDINGS_ENABLED", True)
    monkeypatch.setattr(
        semantic_search_service,
        "document_embedding_service",
        DocumentEmbeddingService(embedder=FakeEmbedder(QUERY_VECTOR)),
    )


async def _search(engine: AsyncEngine, **kwargs: Any) -> list[Any]:
    async with AsyncSession(engine) as session:
        return await semantic_search(session, query="graph neural networks", **kwargs)


# --- search ------------------------------------------------------------------


async def test_tenant_isolation_and_order(
    async_engine: AsyncEngine, seeded: dict[str, uuid.UUID]
) -> None:
    hits = await _search(async_engine, organization_id=seeded["org-a"], limit=5)
    ids = [hit.document_id for hit in hits]
    # org-b's document is the closest of all (0.999) and must never appear.
    assert str(sid("doc-other-org")) not in ids
    assert ids[0] == str(sid("doc-recent-000"))
    assert [hit.similarity for hit in hits] == sorted(
        (hit.similarity for hit in hits), reverse=True
    )
    assert hits[0].similarity == pytest.approx(0.90, abs=1e-4)
    assert hits[0].publication_date == date(2024, 5, 1)
    assert hits[0].publication_date_source == "metadata"
    assert "Body text of recent-000" in hits[0].snippet
    other = await _search(async_engine, organization_id=seeded["org-b"], limit=5)
    assert [hit.document_id for hit in other] == [str(sid("doc-other-org"))]


async def test_deleted_and_unprocessed_documents_never_match(
    async_engine: AsyncEngine, seeded: dict[str, uuid.UUID]
) -> None:
    hits = await _search(async_engine, organization_id=seeded["org-a"], limit=50)
    ids = {hit.document_id for hit in hits}
    assert str(sid("doc-deleted")) not in ids
    assert str(sid("doc-processing")) not in ids


async def test_project_scope(
    async_engine: AsyncEngine, seeded: dict[str, uuid.UUID]
) -> None:
    alpha = await _search(
        async_engine,
        organization_id=seeded["org-a"],
        project_ids=[sid("col-alpha")],
        limit=50,
    )
    ids = {hit.document_id for hit in alpha}
    assert str(sid("doc-nodate")) in ids and str(sid("doc-y2020-0")) in ids
    assert str(sid("doc-recent-000")) not in ids  # linked to beta only
    assert str(sid("doc-recent-005")) not in ids  # its alpha link is soft-deleted
    assert str(sid("doc-other-org")) not in ids
    # A Collection that is deleted, or sits in a deleted Workspace, scopes to nothing.
    for dead in ("col-gone", "col-orphan"):
        assert (
            await _search(
                async_engine,
                organization_id=seeded["org-a"],
                project_ids=[sid(dead)],
            )
            == []
        )
    # Another organization's Collection yields nothing for this organization.
    assert (
        await _search(
            async_engine,
            organization_id=seeded["org-a"],
            project_ids=[sid("col-gamma")],
        )
        == []
    )
    # No project ids at all is no access, not the whole organization.
    assert (
        await _search(async_engine, organization_id=seeded["org-a"], project_ids=[])
        == []
    )


async def test_window_reaches_past_the_most_similar_recent_documents(
    async_engine: AsyncEngine, seeded: dict[str, uuid.UUID]
) -> None:
    """The alphaXiv defect: 120 recent papers are all closer than the 2020 ones."""
    hits = await _search(
        async_engine,
        organization_id=seeded["org-a"],
        published_after=date(2020, 1, 1),
        published_before=date(2020, 12, 31),
        limit=10,
    )
    ids = [hit.document_id for hit in hits]
    assert ids == [str(sid(f"doc-y2020-{index}")) for index in range(3)]
    assert all(hit.publication_date.year == 2020 for hit in hits)  # type: ignore[union-attr]
    # Same window, forced through the HNSW index (the planner prefers a scan
    # on a table this small).
    async with AsyncSession(async_engine) as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        await session.execute(text("SET LOCAL enable_sort = off"))
        indexed = await semantic_search(
            session,
            query="graph neural networks",
            organization_id=seeded["org-a"],
            published_after=date(2020, 1, 1),
            published_before=date(2020, 12, 31),
            limit=10,
        )
    assert [hit.document_id for hit in indexed] == ids


async def test_publication_date_sources(
    async_engine: AsyncEngine, seeded: dict[str, uuid.UUID]
) -> None:
    hits = await _search(
        async_engine,
        organization_id=seeded["org-a"],
        project_ids=[sid("col-alpha")],
        limit=50,
    )
    by_id = {hit.document_id: hit for hit in hits}
    nodate = by_id[str(sid("doc-nodate"))]
    assert nodate.publication_date_source == "created_at"
    assert (
        nodate.publication_date
        == (datetime.now(timezone.utc) - timedelta(days=3)).date()
    )
    year_only = by_id[str(sid("doc-year-only"))]
    assert (year_only.publication_date, year_only.publication_date_source) == (
        date(2019, 1, 1),
        "metadata",
    )
    # A value that is not a date falls back to the ingestion day; it never
    # breaks the query.
    garbage = by_id[str(sid("doc-garbage-date"))]
    assert garbage.publication_date_source == "created_at"
    after = await _search(
        async_engine,
        organization_id=seeded["org-a"],
        project_ids=[sid("col-alpha")],
        published_after=date.today() - timedelta(days=5),
    )
    assert {hit.document_id for hit in after} == {
        str(sid("doc-nodate")),
        str(sid("doc-garbage-date")),
    }


# --- documents ---------------------------------------------------------------


async def test_embed_document_upserts_and_skips_unchanged(
    async_engine: AsyncEngine, sync_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    key = f"embed-{uuid.uuid4().hex[:8]}"
    with Session(sync_engine) as session:
        session.add(_document(key, "a", published="2021-01-01"))
        session.commit()
    embedder = FakeEmbedder(vector(0.5))
    service = DocumentEmbeddingService(embedder=embedder)

    async def run() -> EmbedOutcome:
        async with AsyncSession(async_engine, expire_on_commit=False) as session:
            document = await session.get(Document, sid(f"doc-{key}"))
            assert document is not None
            return await service.embed_document(session, document)

    def stored() -> list[Any]:
        with sync_engine.connect() as connection:
            return list(
                connection.execute(
                    text(
                        "SELECT organization_id, model, content_hash, "
                        "vector_dims(embedding) FROM document_embeddings "
                        "WHERE document_id = :id"
                    ),
                    {"id": sid(f"doc-{key}")},
                )
            )

    assert await run() is EmbedOutcome.EMBEDDED
    rows = stored()
    assert len(rows) == 1
    assert (str(rows[0][0]), rows[0][1], rows[0][3]) == (
        str(sid("org-a")),
        "cohere:embed-v-4-0",
        DIM,
    )
    assert embedder.calls == 1
    assert await run() is EmbedOutcome.UNCHANGED
    assert embedder.calls == 1  # the provider was not called again
    with Session(sync_engine) as session:
        session.execute(
            text("UPDATE documents SET title = 'A different title' WHERE id = :id"),
            {"id": sid(f"doc-{key}")},
        )
        session.commit()
    assert await run() is EmbedOutcome.EMBEDDED
    assert embedder.calls == 2
    assert len(stored()) == 1  # replaced in place, not duplicated
    assert stored()[0][2] != rows[0][2]


async def test_embed_document_skips_a_vector_of_the_wrong_length(
    async_engine: AsyncEngine, sync_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    key = f"short-{uuid.uuid4().hex[:8]}"
    with Session(sync_engine) as session:
        session.add(_document(key, "a"))
        session.commit()
    service = DocumentEmbeddingService(embedder=FakeEmbedder([0.1] * 384))
    async with AsyncSession(async_engine, expire_on_commit=False) as session:
        document = await session.get(Document, sid(f"doc-{key}"))
        assert document is not None
        assert await service.embed_document(session, document) is EmbedOutcome.SKIPPED
    with sync_engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM document_embeddings WHERE document_id = :id"),
            {"id": sid(f"doc-{key}")},
        ).scalar()
    assert count == 0


# --- schema ------------------------------------------------------------------


def test_schema_has_cosine_hnsw_index_cascades_and_locks_the_data_api(
    sync_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    with sync_engine.connect() as connection:
        index = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'document_embeddings' "
                "AND indexname = 'ix_document_embeddings_embedding_hnsw'"
            )
        ).scalar()
        rls = connection.execute(
            text(
                "SELECT relrowsecurity FROM pg_class WHERE relname = 'document_embeddings'"
            )
        ).scalar()
    assert index is not None and "USING hnsw" in index and "vector_cosine_ops" in index
    assert rls is True
    key = f"cascade-{uuid.uuid4().hex[:8]}"
    with Session(sync_engine) as session:
        session.add(_document(key, "a"))
        session.flush()
        session.add(
            DocumentEmbedding(
                document_id=sid(f"doc-{key}"),
                organization_id=sid("org-a"),
                embedding=vector(0.1),
                model="m",
                content_hash="1" * 64,
            )
        )
        session.commit()
        session.execute(
            text("DELETE FROM documents WHERE id = :id"), {"id": sid(f"doc-{key}")}
        )
        session.commit()
        remaining = session.execute(
            text("SELECT count(*) FROM document_embeddings WHERE document_id = :id"),
            {"id": sid(f"doc-{key}")},
        ).scalar()
    assert cast(int, remaining) == 0

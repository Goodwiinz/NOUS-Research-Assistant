"""Metadata cache isolation, revision sync and safe source PDF promotion."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.models.arxiv_paper import ArxivPaper
from src.services.arxiv import papers, recent

pytestmark = pytest.mark.unit
PAPER: dict[str, Any] = {
    "id": "2401.00001v1",
    "title": "First title",
    "abstract": "First abstract",
    "authors": ["A"],
    "categories": ["cs.AI"],
}
HTML = """<meta name="citation_arxiv_id" content="2401.00001">
<meta name="citation_title" content="Latest title">
<meta name="citation_author" content="B">
<blockquote class="abstract"><span>Abstract:</span> This paper has been withdrawn by the authors.</blockquote>
<div class="submission-history">[v1] Monday [v2] Tuesday</div>"""


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(ArxivPaper.__table__.create)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.get_papers_by_ids = AsyncMock(return_value=[PAPER])
    client._make_async_request = AsyncMock(return_value=HTML)
    client.download_paper_pdf = AsyncMock(return_value=b"%PDF-1.7 test")
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        papers, "ArXivIngestionService", MagicMock(return_value=context)
    )
    return client


async def test_cache_hit_and_tenant_isolation(
    db: AsyncSession, upstream: MagicMock
) -> None:
    org = uuid4()
    first = await papers.resolve_metadata(db, org, "arXiv:2401.00001")
    second = await papers.resolve_metadata(db, org, "2401.00001")
    assert first == second
    assert first["withdrawn"] is None
    upstream.get_papers_by_ids.assert_awaited_once_with(["2401.00001"])
    await papers.resolve_metadata(db, uuid4(), "2401.00001")
    assert upstream.get_papers_by_ids.await_count == 2


async def test_revision_pin_never_resolves_a_different_revision(
    db: AsyncSession, upstream: MagicMock
) -> None:
    with pytest.raises(papers.PaperNotFound):
        await papers.resolve_metadata(db, uuid4(), "2401.00001v2")


async def test_explicit_sync_reports_latest_without_rewriting_pin(
    db: AsyncSession, upstream: MagicMock
) -> None:
    result = await papers.sync_metadata(db, uuid4(), "2401.00001v1")
    assert result["id"] == "2401.00001v1"
    assert result["title"] == "First title"
    assert result["latest_id"] == "2401.00001v2"
    assert result["latest_withdrawn"] is True
    assert result["withdrawn"] is None
    upstream._make_async_request.assert_awaited_once_with(
        "https://arxiv.org/abs/2401.00001", {}
    )


async def test_unpinned_sync_updates_metadata(
    db: AsyncSession, upstream: MagicMock
) -> None:
    upstream.get_papers_by_ids.side_effect = [
        [PAPER],
        [
            {
                **PAPER,
                "id": "2401.00001v2",
                "updated": "2024-02-01T00:00:00Z",
                "categories": ["cs.CL"],
            }
        ],
    ]
    result = await papers.sync_metadata(db, uuid4(), "2401.00001")
    assert result["id"] == "2401.00001v2"
    assert result["title"] == "Latest title"
    assert result["withdrawn"] is True
    assert result["updated"] == "2024-02-01T00:00:00Z"
    assert result["categories"] == ["cs.CL"]


async def test_failed_sync_preserves_cached_data(
    db: AsyncSession, upstream: MagicMock
) -> None:
    org = uuid4()
    first = await papers.resolve_metadata(db, org, "2401.00001")
    upstream._make_async_request.return_value = "<html>Challenge</html>"
    with pytest.raises(papers.PaperSourceError):
        await papers.sync_metadata(db, org, "2401.00001")
    assert await papers.resolve_metadata(db, org, "2401.00001") == first


async def test_older_lookup_cannot_overwrite_sync(db: AsyncSession) -> None:
    org = uuid4()
    now = datetime.now(timezone.utc)
    await papers._store(db, org, "2401.00001", {"title": "Synced"}, now)
    result = await papers._store(
        db, org, "2401.00001", {"title": "Stale"}, now - timedelta(seconds=1)
    )
    assert result["title"] == "Synced"


@pytest.mark.parametrize(
    "identifier",
    ["../../etc/passwd", "https://example.com/a", "2401.00001?x=1", "2401.00001v0"],
)
async def test_invalid_ids_never_reach_network(
    db: AsyncSession, upstream: MagicMock, identifier: str
) -> None:
    with pytest.raises(ValueError):
        await papers.resolve_metadata(db, uuid4(), identifier)
    upstream.get_papers_by_ids.assert_not_awaited()


def test_historical_id_and_non_withdrawal_page() -> None:
    html = HTML.replace("2401.00001", "math/0309136").replace(
        "This paper has been withdrawn by the authors.",
        "A method for detecting withdrawn publications.",
    )
    result = papers.parse_abstract_page(html, "math/0309136v1")
    assert result["id"] == "math/0309136v2"
    assert result["withdrawn"] is None


async def test_pdf_promotion_requires_pdf_and_cleans_temporary_file(
    db: AsyncSession, upstream: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(papers, "existing_arxiv_document", AsyncMock(return_value=None))
    paths: list[Path] = []

    async def persist(
        sources: list[SimpleNamespace], **kwargs: object
    ) -> SimpleNamespace:
        path = Path(sources[0].document_metadata["pdf_path"])
        paths.append(path)
        assert path.read_bytes() == b"%PDF-1.7 test"
        assert kwargs["require_pdf"] is True
        assert sources[0].document_metadata["has_full_text"] is False
        return SimpleNamespace(document_ids=["document-1"])

    monkeypatch.setattr(papers, "persist_arxiv_documents", persist)
    assert await papers.store_pdf(db, uuid4(), uuid4(), "2401.00001") == "document-1"
    upstream.download_paper_pdf.assert_awaited_once_with("2401.00001v1")
    assert not paths[0].exists()


async def test_html_response_is_never_persisted_as_pdf(
    db: AsyncSession, upstream: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(papers, "existing_arxiv_document", AsyncMock(return_value=None))
    persist = AsyncMock()
    monkeypatch.setattr(papers, "persist_arxiv_documents", persist)
    upstream.download_paper_pdf.return_value = b"<html>not a pdf</html>"
    with pytest.raises(papers.PaperSourceError):
        await papers.store_pdf(db, uuid4(), uuid4(), "2401.00001")
    persist.assert_not_awaited()


async def test_revoked_actor_cannot_start_recent_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recent, "_authorized", AsyncMock(return_value=False))
    upstream = MagicMock()
    monkeypatch.setattr(recent, "ArXivIngestionService", upstream)
    with pytest.raises(PermissionError):
        await recent.import_recent_papers(uuid4(), uuid4(), ["cs.AI"], 20, 1)
    upstream.assert_not_called()


async def test_recent_import_rechecks_actor_and_bounds_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorized = AsyncMock(side_effect=[True, True, False])
    monkeypatch.setattr(recent, "_authorized", authorized)
    client = MagicMock()
    client.search_papers = AsyncMock(
        return_value=[PAPER, {**PAPER, "id": "2401.00002v1"}]
    )
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(recent, "ArXivIngestionService", MagicMock(return_value=client))
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(recent, "AsyncSessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(recent, "_store", AsyncMock())
    store = AsyncMock()
    monkeypatch.setattr(recent, "store_pdf", store)
    with pytest.raises(PermissionError):
        await recent.import_recent_papers(uuid4(), uuid4(), ["cs.AI"], 2, 1)
    assert store.await_count == 1
    assert client.search_papers.call_args.kwargs["max_results"] == 2


async def test_worker_rejects_unbounded_requests() -> None:
    with pytest.raises(ValueError):
        await recent.import_recent_papers(uuid4(), uuid4(), ["cs.AI"], 500, 1)


async def test_cached_metadata_does_not_consume_admission(
    db: AsyncSession, upstream: MagicMock
) -> None:
    org = uuid4()
    await papers.resolve_metadata(db, org, "2401.00001")
    admit = AsyncMock(side_effect=RuntimeError("quota exhausted"))
    for _ in range(6):
        await papers.resolve_metadata(db, org, "2401.00001", admit=admit)
    admit.assert_not_awaited()


async def test_stored_pdf_does_not_consume_admission(
    db: AsyncSession, upstream: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.models.document import DocumentType

    org = uuid4()
    await papers.resolve_metadata(db, org, "2401.00001")
    existing = SimpleNamespace(id=uuid4(), document_type=DocumentType.PDF)
    monkeypatch.setattr(
        papers, "existing_arxiv_document", AsyncMock(return_value=existing)
    )
    monkeypatch.setattr(papers, "_has_durable_storage", lambda _: True)
    admit = AsyncMock(side_effect=RuntimeError("quota exhausted"))
    assert await papers.store_pdf(db, org, uuid4(), "2401.00001", admit=admit) == str(
        existing.id
    )
    admit.assert_not_awaited()


async def test_recent_import_preserves_explicit_sync(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = uuid4()
    synced = {
        **PAPER,
        "withdrawn": True,
        "latest_id": PAPER["id"],
        "latest_withdrawn": True,
        "source": "arxiv_abstract_page",
    }
    await papers._store(
        db, org, PAPER["id"], synced, datetime.now(timezone.utc) - timedelta(days=1)
    )
    monkeypatch.setattr(recent, "_authorized", AsyncMock(return_value=True))
    client = MagicMock()
    client.search_papers = AsyncMock(return_value=[PAPER])
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(recent, "ArXivIngestionService", MagicMock(return_value=client))
    monkeypatch.setattr(recent, "AsyncSessionLocal", lambda: db)
    monkeypatch.setattr(recent, "store_pdf", AsyncMock())
    result = await recent.import_recent_papers(
        org, uuid4(), ["physics.flu-dyn", "cond-mat.mtrl-sci"], 2, 1
    )
    assert result == {"imported": 1, "failed": 0}
    cached = await papers._cached(db, org, PAPER["id"])
    assert cached is not None
    assert cached.get("latest_withdrawn") is True
    assert cached["source"] == "arxiv_abstract_page"


@pytest.mark.parametrize(
    "identifier,canonical",
    [
        ("2401.00001V1", "2401.00001v1"),
        ("MATH/0309136", "math/0309136"),
        ("math.co/0309136V2", "math.CO/0309136v2"),
    ],
)
def test_case_variant_normalization(identifier: str, canonical: str) -> None:
    assert papers.normalize_paper_id(identifier) == canonical


async def test_empty_dates_and_pdf_link_are_normalized(
    db: AsyncSession, upstream: MagicMock
) -> None:
    upstream.get_papers_by_ids.return_value = [
        {**PAPER, "published": "", "updated": ""}
    ]
    result = await papers.resolve_metadata(db, uuid4(), "2401.00001")
    assert result["published"] is None
    assert result["updated"] is None
    assert result["pdf_url"] == "https://arxiv.org/pdf/2401.00001v1"


@pytest.mark.parametrize(
    "status",
    [
        "The article has been withdrawn.",
        "Withdrawn.",
        "This paper was withdrawn by the author.",
    ],
)
def test_withdrawal_status_phrasings(status: str) -> None:
    assert (
        papers.parse_abstract_page(
            HTML.replace("This paper has been withdrawn by the authors.", status),
            "2401.00001",
        )["withdrawn"]
        is True
    )


async def test_invalid_pdf_response_does_not_poison_retry(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attempts = 0

    def upstream(config: dict[str, str] | None = None) -> MagicMock:
        directory = Path((config or {}).get("arxiv_download_dir", str(tmp_path)))
        directory.mkdir(exist_ok=True)

        async def download(identifier: str) -> bytes:
            nonlocal attempts
            cache = directory / (identifier + ".pdf")
            if cache.exists():
                return cache.read_bytes()
            attempts += 1
            payload = (
                b"<html>transient error</html>"
                if attempts == 1
                else b"%PDF-1.7 valid retry"
            )
            cache.write_bytes(payload)
            return payload

        client = MagicMock()
        client.get_papers_by_ids = AsyncMock(return_value=[PAPER])
        client.download_paper_pdf = download
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    monkeypatch.setattr(papers, "ArXivIngestionService", upstream)
    monkeypatch.setattr(papers, "existing_arxiv_document", AsyncMock(return_value=None))
    monkeypatch.setattr(
        papers,
        "persist_arxiv_documents",
        AsyncMock(return_value=SimpleNamespace(document_ids=["stored-pdf"])),
    )
    org = uuid4()
    with pytest.raises(papers.PaperSourceError):
        await papers.store_pdf(db, org, uuid4(), "2401.00001")
    assert await papers.store_pdf(db, org, uuid4(), "2401.00001") == "stored-pdf"
    assert attempts == 2

"""Metadata resolution, explicit sync and durable source-PDF delivery."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from bs4 import BeautifulSoup
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.arxiv_paper import ArxivPaper
from src.models.document import DocumentType

from .arxiv_service import ArXivIngestionService
from .persistence import (
    _has_durable_storage,
    existing_arxiv_document,
    persist_arxiv_documents,
)

_ID = re.compile(
    r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9-]*(?:\.[a-z]{2})?/\d{7})(?:v[1-9]\d*)?",
    re.IGNORECASE,
)
CATEGORY_PATTERN = r"[a-z][a-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)?"
Admission = Callable[[], Awaitable[None]]


class PaperNotFound(Exception):
    """The requested revision was not returned by arXiv."""


class PaperSourceError(Exception):
    """Upstream data cannot safely satisfy the requested operation."""


def normalize_paper_id(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("arxiv:"):
        value = value[6:]
    if not _ID.fullmatch(value):
        raise ValueError("Invalid arXiv identifier")
    value = re.sub(r"V(?=[1-9]\d*$)", "v", value)
    if "/" in value:
        archive, number = value.split("/", 1)
        parts = archive.split(".", 1)
        archive = parts[0].lower() + ("." + parts[1].upper() if len(parts) == 2 else "")
        value = archive + "/" + number
    return value


def base_id(value: str) -> str:
    return re.sub(r"v\d+$", "", value)


def parse_abstract_page(html: str, paper_id: str) -> dict[str, Any]:
    """Fail closed on challenge/error pages; never replace good cached metadata."""
    soup = BeautifulSoup(html, "html.parser")
    identity = soup.find("meta", attrs={"name": "citation_arxiv_id"})
    identity_value = str(identity.get("content", "")) if identity else ""
    if base_id(normalize_paper_id(identity_value)) != base_id(paper_id):
        raise PaperSourceError("Abstract page identity mismatch")
    title = soup.find("meta", attrs={"name": "citation_title"})
    abstract = soup.select_one("blockquote.abstract")
    history = soup.select_one(".submission-history")
    revisions = re.findall(r"\[v([1-9]\d*)\]", history.get_text() if history else "")
    if not title or not abstract or not revisions:
        raise PaperSourceError("Incomplete arXiv abstract page")
    abstract_text = " ".join(abstract.get_text(" ", strip=True).split())
    abstract_text = re.sub(r"^Abstract:\s*", "", abstract_text)
    comments = soup.select_one("td.comments")
    status_text = abstract_text + " " + (comments.get_text(" ") if comments else "")
    withdrawal_marker = bool(
        re.search(
            r"\b(?:(?:this|the) )?(?:paper|submission|article) (?:has been|was|is) withdrawn\b|^Withdrawn[.!]?(?:\s|$)",
            status_text,
            re.I,
        )
    )
    withdrawn = (
        True
        if withdrawal_marker
        else (None if re.search(r"\bwithdrawn\b", status_text, re.I) else False)
    )
    return {
        "id": f"{base_id(paper_id)}v{max(map(int, revisions))}",
        "title": " ".join(str(title.get("content", "")).split()),
        "authors": [
            str(meta.get("content", ""))
            for meta in soup.find_all("meta", attrs={"name": "citation_author"})
        ],
        "abstract": abstract_text,
        "comment": comments.get_text(" ", strip=True) if comments else None,
        "withdrawn": withdrawn,
    }


async def _cached(
    db: AsyncSession, organization_id: UUID, paper_id: str
) -> dict[str, Any] | None:
    row = (
        await db.execute(
            select(ArxivPaper.metadata_payload).where(
                ArxivPaper.organization_id == organization_id,
                ArxivPaper.paper_id == paper_id,
            )
        )
    ).scalar_one_or_none()
    return dict(row) if row is not None else None


async def _store(
    db: AsyncSession,
    organization_id: UUID,
    paper_id: str,
    metadata: dict[str, Any],
    observed_at: datetime,
    *,
    if_missing: bool = False,
) -> dict[str, Any]:
    # Atomic upsert handles simultaneous misses without rolling back caller state.
    # An older lookup cannot overwrite a newer explicit sync.
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    metadata = dict(metadata)
    for field in ("published", "updated"):
        if field in metadata and not metadata[field]:
            metadata[field] = None
    if metadata.get("id"):
        metadata["pdf_url"] = (
            f"https://arxiv.org/pdf/{normalize_paper_id(metadata['id'])}"
        )
    payload = jsonable_encoder({**metadata, "checked_at": observed_at})
    insert: Any = (
        pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    )
    statement = insert(ArxivPaper).values(
        organization_id=organization_id,
        paper_id=paper_id,
        metadata_payload=payload,
        checked_at=observed_at,
    )
    statement = (
        statement.on_conflict_do_nothing(
            index_elements=[ArxivPaper.organization_id, ArxivPaper.paper_id]
        )
        if if_missing
        else statement.on_conflict_do_update(
            index_elements=[ArxivPaper.organization_id, ArxivPaper.paper_id],
            set_={"metadata_payload": payload, "checked_at": observed_at},
            where=ArxivPaper.checked_at < observed_at,
        )
    )
    await db.execute(statement)
    await db.commit()
    # Scalar columns avoid an identity-map copy from before the upsert.
    return dict(
        (
            await db.execute(
                select(ArxivPaper.metadata_payload).where(
                    ArxivPaper.organization_id == organization_id,
                    ArxivPaper.paper_id == paper_id,
                )
            )
        ).scalar_one()
    )


async def resolve_metadata(
    db: AsyncSession,
    organization_id: UUID,
    paper_id: str,
    *,
    admit: Admission | None = None,
) -> dict[str, Any]:
    paper_id = normalize_paper_id(paper_id)
    cached = await _cached(db, organization_id, paper_id)
    if cached is not None:
        return cached
    if admit is not None:
        await admit()
    observed_at = datetime.now(timezone.utc)
    async with ArXivIngestionService() as upstream:
        papers = await upstream.get_papers_by_ids([paper_id])
    paper = next(
        (
            p
            for p in papers
            if (
                normalize_paper_id(p["id"]) == paper_id
                if re.search(r"v\d+$", paper_id)
                else base_id(normalize_paper_id(p["id"])) == paper_id
            )
        ),
        None,
    )
    if paper is None:
        raise PaperNotFound(paper_id)
    return await _store(
        db,
        organization_id,
        paper_id,
        {
            **paper,
            "withdrawn": None,
            "latest_id": None,
            "source": "arxiv_atom",
        },
        observed_at,
        if_missing=True,
    )


async def sync_metadata(
    db: AsyncSession, organization_id: UUID, paper_id: str
) -> dict[str, Any]:
    paper_id = normalize_paper_id(paper_id)
    metadata = await resolve_metadata(db, organization_id, paper_id)
    observed_at = datetime.now(timezone.utc)
    # A pinned revision retains its own content. Latest status is separately named.
    pinned = bool(re.search(r"v\d+$", paper_id))
    async with ArXivIngestionService() as upstream:
        html = await upstream._make_async_request(
            f"https://arxiv.org/abs/{base_id(paper_id)}", {}
        )
        try:
            latest = parse_abstract_page(html, paper_id)
        except ValueError as error:
            raise PaperSourceError("Invalid abstract page identity") from error
        if not pinned and latest["id"] != metadata["id"]:
            revisions = await upstream.get_papers_by_ids([latest["id"]])
            matched = next((p for p in revisions if p["id"] == latest["id"]), None)
            if matched is None:
                raise PaperSourceError("Latest revision metadata is unavailable")
            metadata = matched
    refreshed = metadata if pinned else {**metadata, **latest}
    return await _store(
        db,
        organization_id,
        paper_id,
        {
            **refreshed,
            "latest_id": latest["id"],
            "latest_withdrawn": latest["withdrawn"],
            "withdrawn": (
                metadata.get("withdrawn")
                if pinned and metadata["id"] != latest["id"]
                else latest["withdrawn"]
            ),
            "source": "arxiv_atom+abstract_page" if pinned else "arxiv_abstract_page",
        },
        observed_at,
    )


async def store_pdf(
    db: AsyncSession,
    organization_id: UUID,
    user_id: UUID,
    paper_id: str,
    *,
    admit: Admission | None = None,
) -> str:
    metadata = await resolve_metadata(db, organization_id, paper_id, admit=admit)
    revision_id = normalize_paper_id(metadata["id"])
    existing = await existing_arxiv_document(db, organization_id, revision_id)
    if (
        existing is not None
        and existing.document_type == DocumentType.PDF
        and _has_durable_storage(existing)
    ):
        return str(existing.id)
    if admit is not None:
        await admit()
    with TemporaryDirectory(prefix="nous-arxiv-") as directory:
        async with ArXivIngestionService({"arxiv_download_dir": directory}) as upstream:
            # A private temporary cache cannot retain a failed HTML response.
            content = await upstream.download_paper_pdf(revision_id)
        if (
            not content
            or len(content) > 50 * 1024 * 1024
            or not content.startswith(b"%PDF-")
        ):
            raise PaperSourceError("arXiv did not return a valid PDF")
        path = Path(directory) / (revision_id.replace("/", "_") + ".pdf")
        path.write_bytes(content)
        source = SimpleNamespace(
            title=metadata["title"],
            content_text=metadata["abstract"],
            document_metadata={
                **metadata,
                "arxiv_id": revision_id,
                "has_full_text": False,
                "pdf_path": str(path),
            },
        )
        result = await persist_arxiv_documents(
            [source], user_id=user_id, organization_id=organization_id, require_pdf=True
        )
    if not result.document_ids:
        raise PaperSourceError("PDF could not be stored")
    return result.document_ids[0]

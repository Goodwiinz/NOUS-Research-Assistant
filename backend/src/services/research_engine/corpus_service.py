"""Immutable receipts for imported search results and citation chases (GOO-300).

Every function runs inside the caller's transaction and never commits. Callers
first run ``resolve_project(..., EDIT)`` for writes, which holds the Collection
row lock, so the ``dedup_key`` lookup below and the insert that follows are
serialized per project; ``UNIQUE(collection_id, dedup_key)`` is the database
guard underneath.
"""

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.research_import import ResearchImportReceipt, ResearchImportRecord
from src.models.research_protocol import ResearchProtocolVersion
from src.models.research_report import ResearchReportIdentifier
from src.schemas.research_engine import (
    CitationChaseRequest,
    ImportDeclaration,
    ImportReceiptDetail,
    ImportReceiptResponse,
    ImportRecordResponse,
)
from src.services.research_engine import identity_service, search_import
from src.services.research_engine.connectors.base import SearchTrace
from src.services.research_engine.connectors.openalex_connector import (
    OpenAlexConnector,
    work_document,
)
from src.services.research_engine.discovery import (
    SEARCH_TIMEOUT_SECONDS,
    extract_identifiers,
)
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)

_IMPORT_NOT_FOUND = "Import not found"
_FORMAT_MESSAGES = {
    "unsupported_format": "Supported formats are RIS, CSV and NBIB.",
    "unsupported_encoding": "The file must be UTF-8 encoded.",
    "file_too_large": "The file exceeds the 5 MiB import limit.",
    "too_many_records": "The file exceeds the 5,000 record import limit.",
    "no_records": "The file contains no records.",
    "csv_missing_title_column": "The CSV file needs a title column.",
}


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _lineage_key(database: str, query_text: str | None) -> str:
    return hashlib.sha256(
        f"{database}\x1f{query_text or ''}".encode("utf-8")
    ).hexdigest()


def receipt_response(
    receipt: ResearchImportReceipt, *, replayed: bool = False
) -> ImportReceiptResponse:
    response: ImportReceiptResponse = ImportReceiptResponse.model_validate(
        {
            **{
                column: getattr(receipt, column)
                for column in ImportReceiptResponse.model_fields
                if column != "replayed"
            },
            "replayed": replayed,
        }
    )
    return response


async def _receipt(
    db: AsyncSession, collection_id: UUID, **where: Any
) -> ResearchImportReceipt | None:
    return cast(
        ResearchImportReceipt | None,
        (
            await db.execute(
                select(ResearchImportReceipt).where(
                    ResearchImportReceipt.collection_id == collection_id,
                    *(getattr(ResearchImportReceipt, k) == v for k, v in where.items()),
                )
            )
        ).scalar_one_or_none(),
    )


async def insert_receipt(
    db: AsyncSession,
    *,
    collection_id: UUID,
    actor_user_id: UUID,
    kind: str,
    dedup_key: str,
    lineage_key: str,
    declared: dict[str, Any],
    observed: dict[str, Any],
    records: list[ResearchImportRecord],
) -> ResearchImportReceipt:
    """Insert one receipt version plus its records and attach report identities.

    The caller holds the Collection lock and has already checked ``dedup_key``.
    """
    previous = (
        await db.execute(
            select(ResearchImportReceipt)
            .where(
                ResearchImportReceipt.collection_id == collection_id,
                ResearchImportReceipt.lineage_key == lineage_key,
            )
            .order_by(ResearchImportReceipt.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    accepted = sum(record.status == "accepted" for record in records)
    receipt = ResearchImportReceipt(
        id=uuid4(),
        collection_id=collection_id,
        kind=kind,
        dedup_key=dedup_key,
        lineage_key=lineage_key,
        version=(cast(int, previous.version) + 1) if previous is not None else 1,
        previous_receipt_id=previous.id if previous is not None else None,
        declared=declared,
        observed={
            **observed,
            "counts": {
                "parsed": len(records),
                "accepted": accepted,
                "rejected": len(records) - accepted,
            },
        },
        parsed_count=len(records),
        accepted_count=accepted,
        rejected_count=len(records) - accepted,
        actor_user_id=actor_user_id,
    )
    db.add(receipt)
    await db.flush()
    for record in records:
        record.receipt_id = receipt.id
    await identity_service.observe_import_records(
        db, collection_id=collection_id, records=records
    )
    db.add_all(records)
    await db.flush()
    await db.refresh(receipt, ["created_at"])
    return receipt


async def import_file(
    db: AsyncSession,
    context: ProjectContext,
    actor_user_id: UUID,
    declaration: ImportDeclaration,
    *,
    fmt: str,
    filename: str,
    data: bytes,
) -> tuple[ImportReceiptResponse, bool]:
    """Store one immutable receipt per distinct file; ``(receipt, created)``."""
    collection_id = cast(UUID, context.collection.id)
    sha = hashlib.sha256(data).hexdigest()
    dedup_key = f"file:{sha}:{fmt}:{search_import.PARSER_VERSION}"
    declared = declaration.model_dump(mode="json")
    # Idempotency guard: under the Collection lock, identical bytes replay.
    existing = await _receipt(db, collection_id, dedup_key=dedup_key)
    if existing is not None:
        if existing.declared != declared:
            raise _error(
                409,
                "import_declaration_conflict",
                "This file was already imported with a different declaration.",
            )
        return receipt_response(existing, replayed=True), False
    try:
        parsed = search_import.parse(fmt, data)
    except search_import.ImportFormatError as exc:
        raise _error(exc.status, exc.code, _FORMAT_MESSAGES[exc.code]) from exc
    records = [
        ResearchImportRecord(
            id=uuid4(),
            collection_id=collection_id,
            record_index=record.index,
            status="rejected" if record.rejection_reason else "accepted",
            rejection_reason=record.rejection_reason,
            raw=record.raw,
            parsed=record.parsed,
        )
        for record in parsed
    ]
    receipt = await insert_receipt(
        db,
        collection_id=collection_id,
        actor_user_id=actor_user_id,
        kind="file_import",
        dedup_key=dedup_key,
        lineage_key=_lineage_key(declaration.database, declaration.query_text),
        declared=declared,
        observed={
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "actor_user_id": str(actor_user_id),
            "filename": re.split(r"[\\/]", filename)[-1][:255],
            "byte_size": len(data),
            "sha256": sha,
            "format": fmt,
            "parser_version": search_import.PARSER_VERSION,
            "protocol_version_id": await identity_service.current_protocol_version_id(
                db, collection_id
            ),
        },
        records=records,
    )
    return receipt_response(receipt), True


async def list_receipts(
    db: AsyncSession, *, collection_id: UUID
) -> list[ImportReceiptResponse]:
    rows = (
        (
            await db.execute(
                select(ResearchImportReceipt)
                .where(ResearchImportReceipt.collection_id == collection_id)
                .order_by(ResearchImportReceipt.created_at, ResearchImportReceipt.id)
            )
        )
        .scalars()
        .all()
    )
    return [receipt_response(row) for row in rows]


def raw_allowed(receipt: ResearchImportReceipt) -> bool:
    return (
        cast(dict[str, Any], receipt.declared or {}).get("redistribution") == "allowed"
    )


async def get_receipt(
    db: AsyncSession, *, collection_id: UUID, receipt_id: UUID
) -> ImportReceiptDetail:
    """One receipt with every record, rejections first; foreign ids are 404."""
    receipt = await _receipt(db, collection_id, id=receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=_IMPORT_NOT_FOUND)
    records = (
        (
            await db.execute(
                select(ResearchImportRecord).where(
                    ResearchImportRecord.receipt_id == receipt_id
                )
            )
        )
        .scalars()
        .all()
    )
    show_raw = raw_allowed(receipt)
    return ImportReceiptDetail(
        **receipt_response(receipt).model_dump(),
        records=[
            ImportRecordResponse(
                id=cast(UUID, record.id),
                record_index=cast(int, record.record_index),
                status=cast(Any, record.status),
                rejection_reason=cast(str | None, record.rejection_reason),
                parsed=cast(dict[str, Any], record.parsed),
                report_id=cast(UUID | None, record.report_id),
                raw=cast(str, record.raw) if show_raw else None,
            )
            for record in sorted(
                records, key=lambda r: (r.status != "rejected", r.record_index)
            )
        ],
    )


# --- Citation chasing (OpenAlex only) ----------------------------------------


class CitationConnector(Protocol):
    async def citations(
        self,
        work_id: str,
        direction: Any,
        max_results: int,
        *,
        search_trace: SearchTrace,
    ) -> list[dict[str, Any]]: ...


def _chase_parsed(work: dict[str, Any]) -> dict[str, Any]:
    document = work_document(work)
    date = document.metadata.get("publication_date")
    parsed = {
        "title": document.title,
        "authors": document.authors,
        "year": date[:4] if isinstance(date, str) and date[:4].isdigit() else None,
        "abstract": document.abstract,
        "url": document.url,
        "identifiers": extract_identifiers(document),
    }
    return {key: value for key, value in parsed.items() if value not in (None, "", [])}


async def _protocol_requirement(
    db: AsyncSession, collection_id: UUID
) -> tuple[str | None, Any]:
    """Governing approved version and its ``sources_search.citation_chasing``."""
    version_id = await identity_service.current_protocol_version_id(db, collection_id)
    if version_id is None:
        return None, None
    snapshot = (
        await db.execute(
            select(ResearchProtocolVersion.snapshot).where(
                ResearchProtocolVersion.id == UUID(version_id)
            )
        )
    ).scalar_one_or_none()
    sources_search = cast(dict[str, Any], snapshot or {}).get("sources_search") or {}
    return version_id, sources_search.get("citation_chasing")


async def chase_citations(
    db: AsyncSession,
    *,
    project_id: UUID,
    user_id: UUID,
    data: CitationChaseRequest,
    connector: CitationConnector | None = None,
) -> tuple[ImportReceiptResponse, bool]:
    """Record one OpenAlex citation chase as an immutable receipt.

    Three phases so no Collection or stream lock is held across provider I/O:
    (1) EDIT access, replay check and seed resolution, then roll back;
    (2) the network call; (3) EDIT access again (authority may have changed
    while waiting), replay recheck, insert. The caller commits.
    """
    # The client key can be 240 chars; hash it to fit dedup_key (160).
    dedup_key = "chase:" + hashlib.sha256(data.idempotency_key.encode()).hexdigest()
    declared = {
        "seed_report_id": str(data.seed_report_id),
        "direction": data.direction,
        "requested_limit": data.max_results,
        # OpenAlex metadata is CC0.
        "redistribution": "allowed",
    }

    async def replayed(collection_id: UUID) -> ImportReceiptResponse | None:
        existing = await _receipt(db, collection_id, dedup_key=dedup_key)
        if existing is None:
            return None
        if existing.declared != declared:
            raise _error(
                409,
                "idempotency_conflict",
                "This idempotency key was already used for a different chase.",
            )
        return receipt_response(existing, replayed=True)

    context = await resolve_project(db, project_id, user_id, ResearchAction.EDIT)
    collection_id = cast(UUID, context.collection.id)
    if (hit := await replayed(collection_id)) is not None:
        return hit, False
    await identity_service._live_reports(
        db, collection_id, [data.seed_report_id], merged_status=404
    )
    seed_ids = dict(
        (
            await db.execute(
                select(
                    ResearchReportIdentifier.kind, ResearchReportIdentifier.value
                ).where(
                    ResearchReportIdentifier.report_id == data.seed_report_id,
                    ResearchReportIdentifier.collection_id == collection_id,
                    ResearchReportIdentifier.kind.in_(("openalex", "doi")),
                )
            )
        )
        .tuples()
        .all()
    )
    if "openalex" in seed_ids:
        work_id = seed_ids["openalex"]
    elif "doi" in seed_ids:
        work_id = f"doi:{seed_ids['doi']}"
    else:
        raise _error(
            422,
            "seed_not_resolvable",
            "The seed report needs an OpenAlex id or a DOI to chase citations.",
        )
    protocol_version_id, requirement = await _protocol_requirement(db, collection_id)
    await db.rollback()

    trace = SearchTrace(
        execution_id=str(uuid4()), provider="openalex", requested_limit=data.max_results
    )
    source = connector or OpenAlexConnector(api_key=settings.OPENALEX_API_KEY)
    try:
        works = await asyncio.wait_for(
            source.citations(
                work_id, data.direction, data.max_results, search_trace=trace
            ),
            SEARCH_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        await trace.mark_interrupted()
        raise
    except Exception as exc:
        # A failed chase is evidence too; provider text may hold credentials.
        trace.error_type = type(exc).__name__
        await trace.record_failed_request(trace.error_type)
        works = []

    context = await resolve_project(db, project_id, user_id, ResearchAction.EDIT)
    collection_id = cast(UUID, context.collection.id)
    if (hit := await replayed(collection_id)) is not None:
        return hit, False
    receipt_trace = trace.as_receipt(returned_count=len(works))
    records = []
    for index, work in enumerate(works):
        parsed = _chase_parsed(work)
        records.append(
            ResearchImportRecord(
                id=uuid4(),
                collection_id=collection_id,
                record_index=index,
                status="accepted" if parsed.get("title") else "rejected",
                rejection_reason=None if parsed.get("title") else "missing_title",
                raw=json.dumps(work, sort_keys=True),
                parsed=parsed,
            )
        )
    receipt = await insert_receipt(
        db,
        collection_id=collection_id,
        actor_user_id=user_id,
        kind="citation_chase",
        dedup_key=dedup_key,
        lineage_key=hashlib.sha256(
            f"chase\x1f{data.seed_report_id}\x1f{data.direction}".encode()
        ).hexdigest(),
        declared=declared,
        observed={
            "provider": "openalex",
            "started_at": trace.started_at,
            "actor_user_id": str(user_id),
            "seed_work_id": work_id,
            "trace": receipt_trace,
            "completion": receipt_trace["completion"],
            "error_type": trace.error_type,
            "protocol_version_id": protocol_version_id,
            "protocol_requires": requirement,
        },
        records=records,
    )
    return receipt_response(receipt), True

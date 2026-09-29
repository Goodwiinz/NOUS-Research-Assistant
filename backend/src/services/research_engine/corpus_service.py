"""Immutable receipts for imported search results and citation chases (GOO-300).

Every function runs inside the caller's transaction and never commits. Callers
first run ``resolve_project(..., EDIT)`` for writes, which holds the Collection
row lock, so the ``dedup_key`` lookup below and the insert that follows are
serialized per project; ``UNIQUE(collection_id, dedup_key)`` is the database
guard underneath.
"""

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_import import ResearchImportReceipt, ResearchImportRecord
from src.schemas.research_engine import (
    ImportDeclaration,
    ImportReceiptDetail,
    ImportReceiptResponse,
    ImportRecordResponse,
)
from src.services.research_engine import identity_service, search_import
from src.services.research_engine.project_access import ProjectContext

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
    return ImportReceiptResponse.model_validate(
        {
            **{
                column: getattr(receipt, column)
                for column in ImportReceiptResponse.model_fields
                if column != "replayed"
            },
            "replayed": replayed,
        }
    )


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

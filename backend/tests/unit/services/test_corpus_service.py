"""Idempotent external search-result import receipts (GOO-300).

SQLite unit coverage; the real Collection-lock/unique-constraint proof is
``tests/integration/test_search_import_postgres.py``.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_import import ResearchImportReceipt, ResearchImportRecord
from src.models.research_protocol import ResearchProtocol
from src.models.research_report import (
    ResearchReport,
    ResearchReportIdentifier,
    ResearchReportObservation,
    ResearchStudy,
)
from src.models.research_source import ResearchSource
from src.schemas.research_engine import ImportDeclaration
from src.services.research_engine import corpus_service, search_import
from src.services.research_engine.project_access import ProjectContext

FIXTURES = Path(__file__).parents[2] / "fixtures" / "search_import"
PARTIAL = (FIXTURES / "partial.ris").read_bytes()


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    tables = [
        model.__table__  # type: ignore[attr-defined]
        for model in (
            ResearchProtocol,
            ResearchDecisionStream,
            ResearchDecisionEvent,
            ResearchStudy,
            ResearchReport,
            ResearchReportIdentifier,
            ResearchSource,
            ResearchReportObservation,
            ResearchImportReceipt,
            ResearchImportRecord,
        )
    ]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    event.listen(
        engine.sync_engine,
        "connect",
        lambda conn, _record: conn.create_function(
            "now", 0, lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
        ),
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _context(collection_id: UUID) -> ProjectContext:
    return cast(
        ProjectContext, SimpleNamespace(collection=SimpleNamespace(id=collection_id))
    )


def _declaration(**overrides: Any) -> ImportDeclaration:
    return ImportDeclaration(
        **{"database": "Embase (Ovid)", "query_text": "aspirin", **overrides}
    )


async def _import(
    db: AsyncSession,
    collection_id: UUID,
    data: bytes = PARTIAL,
    declaration: ImportDeclaration | None = None,
) -> tuple[Any, bool]:
    return await corpus_service.import_file(
        db,
        _context(collection_id),
        uuid4(),
        declaration or _declaration(),
        fmt="ris",
        filename="C:\\exports\\partial.ris",
        data=data,
    )


async def _count(db: AsyncSession, model: Any) -> int:
    return cast(
        int, (await db.execute(select(func.count()).select_from(model))).scalar_one()
    )


@pytest.mark.asyncio
async def test_identical_bytes_return_same_receipt(db: AsyncSession) -> None:
    collection_id = uuid4()
    first, created = await _import(db, collection_id)
    again, created_again = await _import(db, collection_id)

    assert (created, created_again) == (True, False)
    assert again.id == first.id
    assert (first.replayed, again.replayed) == (False, True)
    assert await _count(db, ResearchImportReceipt) == 1
    assert await _count(db, ResearchImportRecord) == 5


@pytest.mark.asyncio
async def test_changed_bytes_create_version_2_with_back_pointer(
    db: AsyncSession,
) -> None:
    collection_id = uuid4()
    first, _ = await _import(db, collection_id)
    second, created = await _import(db, collection_id, PARTIAL + b"\n")

    assert created is True
    assert second.id != first.id
    assert (second.version, second.previous_receipt_id) == (2, first.id)
    assert second.observed["sha256"] != first.observed["sha256"]
    # A different declared search is a different lineage.
    other, _ = await _import(
        db, collection_id, PARTIAL + b"\n\n", _declaration(query_text="statins")
    )
    assert (other.version, other.previous_receipt_id) == (1, None)


@pytest.mark.asyncio
async def test_same_bytes_new_declaration_is_409(db: AsyncSession) -> None:
    collection_id = uuid4()
    await _import(db, collection_id)

    with pytest.raises(HTTPException) as conflict:
        await _import(db, collection_id, declaration=_declaration(query_text="other"))

    assert conflict.value.status_code == 409
    assert cast(dict, conflict.value.detail)["code"] == "import_declaration_conflict"
    assert await _count(db, ResearchImportReceipt) == 1


@pytest.mark.asyncio
async def test_parser_version_bump_creates_new_receipt(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection_id = uuid4()
    first, _ = await _import(db, collection_id)
    monkeypatch.setattr(search_import, "PARSER_VERSION", "nous.search-import.v2")
    second, created = await _import(db, collection_id)

    assert created is True
    assert second.id != first.id
    assert second.observed["parser_version"] == "nous.search-import.v2"


@pytest.mark.asyncio
async def test_malformed_file_persists_nothing(db: AsyncSession) -> None:
    with pytest.raises(HTTPException) as bad:
        await _import(db, uuid4(), b"\xff\xfe not utf-8")

    assert bad.value.status_code == 422
    assert bad.value.detail == {
        "code": "unsupported_encoding",
        "message": "The file must be UTF-8 encoded.",
    }
    assert await _count(db, ResearchImportReceipt) == 0
    assert await _count(db, ResearchImportRecord) == 0
    assert await _count(db, ResearchReport) == 0


@pytest.mark.asyncio
async def test_counts_match_rows(db: AsyncSession) -> None:
    collection_id = uuid4()
    receipt, _ = await _import(db, collection_id)

    assert (receipt.parsed_count, receipt.accepted_count, receipt.rejected_count) == (
        5,
        3,
        2,
    )
    assert receipt.observed["counts"] == {"parsed": 5, "accepted": 3, "rejected": 2}
    assert receipt.observed["filename"] == "partial.ris"
    assert receipt.declared.model_dump() == {
        "database": "Embase (Ovid)",
        "query_text": "aspirin",
        "search_date": None,
        "exported_at": None,
        "redistribution": "restricted",
        "notes": None,
    }
    assert "query_text" not in receipt.observed
    detail = await corpus_service.get_receipt(
        db, collection_id=collection_id, receipt_id=receipt.id
    )
    assert [r.status for r in detail.records] == ["rejected"] * 2 + ["accepted"] * 3
    assert [r.rejection_reason for r in detail.records[:2]] == [
        "missing_title",
        "unterminated_record",
    ]
    # Restricted (the default): the original text stays in the database only,
    # including the tag values (parsed.fields) that reproduce it.
    assert all(r.raw is None for r in detail.records)
    assert all(not {"fields", "abstract"} & set(r.parsed) for r in detail.records)
    assert all(r.report_id for r in detail.records[2:])
    assert await _count(db, ResearchReport) == 3
    assert [
        r.id
        for r in await corpus_service.list_receipts(db, collection_id=collection_id)
    ] == [receipt.id]


@pytest.mark.asyncio
async def test_allowed_receipt_exposes_raw_and_foreign_receipt_is_404(
    db: AsyncSession,
) -> None:
    collection_id = uuid4()
    receipt, _ = await _import(
        db, collection_id, declaration=_declaration(redistribution="allowed")
    )
    detail = await corpus_service.get_receipt(
        db, collection_id=collection_id, receipt_id=receipt.id
    )
    assert detail.records[0].raw is not None
    assert "fields" in detail.records[0].parsed

    with pytest.raises(HTTPException) as missing:
        await corpus_service.get_receipt(
            db, collection_id=uuid4(), receipt_id=receipt.id
        )
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_restricted_tagged_line_sentinel_never_leaves_the_api(
    db: AsyncSession,
) -> None:
    """A sentinel in an RIS tag (N1 notes / AB) survives only in the database."""
    collection_id = uuid4()
    data = (
        b"TY  - JOUR\nTI  - Tagged\nAB  - ABSTRACT-SENTINEL\n"
        b"N1  - NOTES-SENTINEL\nER  - \n"
    )
    receipt, _ = await _import(db, collection_id, data)
    detail = await corpus_service.get_receipt(
        db, collection_id=collection_id, receipt_id=receipt.id
    )

    dumped = detail.model_dump_json()
    assert "ABSTRACT-SENTINEL" not in dumped
    assert "NOTES-SENTINEL" not in dumped
    stored = (await db.execute(select(ResearchImportRecord.raw))).scalar_one()
    assert "NOTES-SENTINEL" in stored


@pytest.mark.asyncio
async def test_unique_violation_after_a_bypassed_lookup_is_409_not_500(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection_id = uuid4()
    await _import(db, collection_id)
    await db.commit()
    real_receipt = corpus_service._receipt

    async def lookup_misses(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(corpus_service, "_receipt", lookup_misses)
    with pytest.raises(HTTPException) as conflict:
        await _import(db, collection_id)
    monkeypatch.setattr(corpus_service, "_receipt", real_receipt)

    assert conflict.value.status_code == 409
    assert cast(dict, conflict.value.detail)["code"] == "import_conflict"
    # The savepoint rolled back only the failed insert; the session still works.
    assert await _count(db, ResearchImportReceipt) == 1
    assert await _count(db, ResearchImportRecord) == 5

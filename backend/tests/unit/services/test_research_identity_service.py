"""Focused guards for the GOO-299 identity service (PostgreSQL proof lives in
``tests/integration/test_report_identity_postgres.py``)."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.research_project_role import ResearchProjectRole
from src.models.research_protocol import ResearchProtocol
from src.schemas.research_engine import StudyLinkRequest
from src.services.research_engine import identity_service
from src.services.research_engine.project_access import ProjectContext


@pytest.mark.asyncio
@pytest.mark.parametrize("adjudicated", ["confirmed", "disputed"])
async def test_reviewer_proposal_cannot_overwrite_an_adjudicated_link(
    monkeypatch: pytest.MonkeyPatch, adjudicated: str
) -> None:
    collection_id, report_id, study_id = uuid4(), uuid4(), uuid4()
    report = SimpleNamespace(
        id=report_id,
        title_snapshot="Paper",
        study_id=study_id,
        study_link_status=adjudicated,
        study_link_actor_id=None,
        study_link_rationale="adjudicated",
    )

    async def live_reports(*_args: Any, **_kwargs: Any) -> dict[UUID, Any]:
        return {report_id: report}

    monkeypatch.setattr(identity_service, "_lock", AsyncMock())
    monkeypatch.setattr(
        identity_service, "_replayed_event", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(identity_service, "live_reports", live_reports)
    append = AsyncMock()
    monkeypatch.setattr(identity_service, "_append", append)
    context = cast(
        ProjectContext,
        SimpleNamespace(
            collection=SimpleNamespace(id=collection_id),
            effective_roles=frozenset({ResearchProjectRole.REVIEWER}),
        ),
    )

    with pytest.raises(HTTPException) as blocked:
        await identity_service.link_study(
            cast(AsyncSession, AsyncMock()),
            context,
            report_id,
            uuid4(),
            StudyLinkRequest(
                study_id=uuid4(),
                status="proposed",
                rationale="re-point",
                idempotency_key="k",
            ),
        )

    assert blocked.value.status_code == 409
    assert (report.study_id, report.study_link_status) == (study_id, adjudicated)
    append.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispute_without_a_target_study_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disputing needs an existing study; it must never mint a new one."""
    collection_id, report_id = uuid4(), uuid4()
    report = SimpleNamespace(
        id=report_id,
        title_snapshot="Paper",
        study_id=None,
        study_link_status=None,
        study_link_actor_id=None,
        study_link_rationale=None,
    )

    async def live_reports(*_args: Any, **_kwargs: Any) -> dict[UUID, Any]:
        return {report_id: report}

    monkeypatch.setattr(identity_service, "_lock", AsyncMock())
    monkeypatch.setattr(
        identity_service, "_replayed_event", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(identity_service, "live_reports", live_reports)
    append = AsyncMock()
    monkeypatch.setattr(identity_service, "_append", append)
    db = AsyncMock()
    db.add = MagicMock(side_effect=AssertionError("dispute minted a new study"))
    context = cast(
        ProjectContext,
        SimpleNamespace(
            collection=SimpleNamespace(id=collection_id),
            effective_roles=frozenset({ResearchProjectRole.ADJUDICATOR}),
        ),
    )

    with pytest.raises(HTTPException) as blocked:
        await identity_service.link_study(
            cast(AsyncSession, db),
            context,
            report_id,
            uuid4(),
            StudyLinkRequest(status="disputed", rationale="no", idempotency_key="k"),
        )

    assert blocked.value.status_code == 409
    assert blocked.value.detail == "No study link to dispute"
    assert (report.study_id, report.study_link_status) == (None, None)
    append.assert_not_awaited()


@pytest.fixture
async def protocol_db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[ResearchProtocol.__table__]
            )
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_governing_protocol_version_is_an_approved_one(
    protocol_db: AsyncSession,
) -> None:
    collection_id, approved = uuid4(), uuid4()
    older = datetime(2026, 9, 1, tzinfo=timezone.utc)
    protocol_db.add_all(
        [
            ResearchProtocol(
                collection_id=collection_id,
                name="draft only",
                created_at=older,
                updated_at=older,
            ),
            ResearchProtocol(
                collection_id=collection_id,
                name="approved",
                current_approved_version_id=approved,
                created_at=older + timedelta(days=1),
                updated_at=older + timedelta(days=1),
            ),
        ]
    )
    await protocol_db.flush()

    assert await identity_service.current_protocol_version_id(
        protocol_db, collection_id
    ) == str(approved)
    assert (
        await identity_service.current_protocol_version_id(protocol_db, uuid4()) is None
    )


# --- GOO-300: imported records join report identity ------------------------


@pytest.fixture
async def identity_db() -> AsyncIterator[AsyncSession]:
    from src.models.research_decision import (
        ResearchDecisionEvent,
        ResearchDecisionStream,
    )
    from src.models.research_import import ResearchImportReceipt, ResearchImportRecord
    from src.models.research_report import (
        ResearchReport,
        ResearchReportIdentifier,
        ResearchReportObservation,
        ResearchStudy,
    )
    from src.models.research_source import ResearchSource

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
    # created_at defaults are server-side now(); give SQLite the same function.
    event.listen(
        engine.sync_engine,
        "connect",
        lambda conn, _record: conn.create_function(
            "now", 0, lambda: datetime.now(timezone.utc).isoformat(" ")
        ),
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _import_record(
    collection_id: UUID,
    receipt_id: UUID,
    index: int,
    title: str,
    identifiers: dict[str, str],
    rejection_reason: str | None = None,
) -> Any:
    from src.models.research_import import ResearchImportRecord

    return ResearchImportRecord(
        id=uuid4(),
        collection_id=collection_id,
        receipt_id=receipt_id,
        record_index=index,
        status="rejected" if rejection_reason else "accepted",
        rejection_reason=rejection_reason,
        raw=f"raw {index}",
        parsed={"title": title, "identifiers": identifiers},
    )


async def _seed_imports(
    db: AsyncSession, collection_id: UUID, *records: Any
) -> list[Any]:
    from src.models.research_import import ResearchImportReceipt

    receipt_id = records[0].receipt_id if records else uuid4()
    db.add(
        ResearchImportReceipt(
            id=receipt_id,
            collection_id=collection_id,
            kind="file_import",
            dedup_key=f"file:{uuid4().hex}",
            lineage_key="l" * 64,
            version=1,
            declared={},
            observed={},
            parsed_count=len(records),
            accepted_count=sum(r.status == "accepted" for r in records),
            rejected_count=sum(r.status == "rejected" for r in records),
            actor_user_id=uuid4(),
        )
    )
    await identity_service.observe_import_records(
        db, collection_id=collection_id, records=list(records)
    )
    db.add_all(records)
    await db.flush()
    return list(records)


def _adjudicator(collection_id: UUID) -> ProjectContext:
    return cast(
        ProjectContext,
        SimpleNamespace(
            collection=SimpleNamespace(id=collection_id),
            effective_roles=frozenset({ResearchProjectRole.ADJUDICATOR}),
        ),
    )


@pytest.mark.asyncio
async def test_observe_import_records_attaches_by_identifier_and_skips_rejected(
    identity_db: AsyncSession,
) -> None:
    from src.models.research_report import ResearchReport, ResearchReportIdentifier

    collection_id, receipt_id, existing = uuid4(), uuid4(), uuid4()
    identity_db.add(
        ResearchReport(id=existing, collection_id=collection_id, title_snapshot="Old")
    )
    await identity_db.flush()
    identity_db.add(
        ResearchReportIdentifier(
            collection_id=collection_id,
            report_id=existing,
            kind="doi",
            value="10.1000/shared",
        )
    )
    await identity_db.flush()

    shared, fresh, rejected = await _seed_imports(
        identity_db,
        collection_id,
        _import_record(
            collection_id, receipt_id, 0, "Shared", {"doi": "10.1000/SHARED"}
        ),
        _import_record(collection_id, receipt_id, 1, "Fresh", {"pmid": "123"}),
        _import_record(
            collection_id,
            receipt_id,
            2,
            "",
            {"doi": "10.1000/shared"},
            rejection_reason="missing_title",
        ),
    )

    assert (shared.report_id, shared.match_method) == (existing, "doi")
    assert shared.evidence["matched"] == {"kind": "doi", "value": "10.1000/shared"}
    assert fresh.report_id not in (None, existing)
    assert fresh.match_method == "new"
    assert (rejected.report_id, rejected.match_method) == (None, None)
    report = (
        await identity_service.list_reports(identity_db, collection_id=collection_id)
    )[0]
    assert report.id == existing
    assert [r.import_record_id for r in report.imported_records] == [shared.id]
    assert report.imported_records[0].receipt_id == receipt_id


@pytest.mark.asyncio
async def test_merge_moves_import_records_and_records_them(
    identity_db: AsyncSession,
) -> None:
    from src.schemas.research_engine import ReportMergeRequest

    collection_id, receipt_id = uuid4(), uuid4()
    first, second = await _seed_imports(
        identity_db,
        collection_id,
        _import_record(collection_id, receipt_id, 0, "One", {"doi": "10.1000/one"}),
        _import_record(collection_id, receipt_id, 1, "Two", {"doi": "10.1000/two"}),
    )

    survivor = await identity_service.merge_reports(
        identity_db,
        _adjudicator(collection_id),
        uuid4(),
        ReportMergeRequest(
            surviving_report_id=first.report_id,
            merged_report_ids=[second.report_id],
            rationale="same paper",
            idempotency_key="merge-1",
        ),
    )

    await identity_db.refresh(second)
    assert second.report_id == first.report_id
    assert {r.import_record_id for r in survivor.imported_records} == {
        first.id,
        second.id,
    }
    (event,) = await identity_service.history(identity_db, collection_id=collection_id)
    assert event.event_type == "identity.report_merged"
    assert event.payload["moved_import_record_ids"] == [str(second.id)]
    assert event.payload["moved_source_ids"] == []


@pytest.mark.asyncio
async def test_split_can_move_only_import_records(
    identity_db: AsyncSession,
) -> None:
    from src.schemas.research_engine import ReportSplitRequest

    collection_id, receipt_id = uuid4(), uuid4()
    kept, moved = await _seed_imports(
        identity_db,
        collection_id,
        _import_record(collection_id, receipt_id, 0, "Kept", {"doi": "10.1000/k"}),
        _import_record(
            collection_id,
            receipt_id,
            1,
            "Moved",
            {"doi": "10.1000/k", "pmid": "999"},
        ),
    )
    assert kept.report_id == moved.report_id
    original = kept.report_id

    new_report = await identity_service.split_report(
        identity_db,
        _adjudicator(collection_id),
        original,
        uuid4(),
        ReportSplitRequest(
            import_record_ids=[moved.id],
            rationale="different paper",
            idempotency_key="split-1",
        ),
    )

    await identity_db.refresh(moved)
    assert moved.report_id == new_report.id != original
    assert new_report.title_snapshot == "Moved"
    assert new_report.identifiers == {"pmid": ["999"]}
    (event,) = await identity_service.history(identity_db, collection_id=collection_id)
    assert event.payload["moved_import_record_ids"] == [str(moved.id)]
    assert event.payload["moved_source_ids"] == []

    with pytest.raises(HTTPException) as last:
        await identity_service.split_report(
            identity_db,
            _adjudicator(collection_id),
            original,
            uuid4(),
            ReportSplitRequest(
                import_record_ids=[kept.id],
                rationale="nothing would stay",
                idempotency_key="split-2",
            ),
        )
    assert last.value.status_code == 409


def test_split_request_needs_sources_or_import_records() -> None:
    from pydantic import ValidationError

    from src.schemas.research_engine import ReportSplitRequest

    with pytest.raises(ValidationError):
        ReportSplitRequest(rationale="empty", idempotency_key="k")


@pytest.mark.asyncio
async def test_v1_split_request_fingerprint_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-GOO-300 split retries must keep their stored request fingerprint."""
    from src.schemas.research_engine import ReportSplitRequest
    from src.services.research_decisions import decision_request_fingerprint

    collection_id, report_id, actor, source_id = uuid4(), uuid4(), uuid4(), uuid4()
    seen: list[str] = []

    async def capture(_db: Any, _stream: Any, _key: str, fingerprint: str) -> None:
        seen.append(fingerprint)
        raise RuntimeError("stop")

    monkeypatch.setattr(identity_service, "_lock", AsyncMock())
    monkeypatch.setattr(identity_service, "_replayed_event", capture)
    with pytest.raises(RuntimeError):
        await identity_service.split_report(
            cast(AsyncSession, AsyncMock()),
            _adjudicator(collection_id),
            report_id,
            actor,
            ReportSplitRequest(
                source_ids=[source_id], rationale="r", idempotency_key="k"
            ),
        )

    # The exact dict GOO-299 fingerprinted: no import_record_ids key.
    assert seen == [
        decision_request_fingerprint(
            {
                "operation": "split",
                "report_id": str(report_id),
                "actor_user_id": str(actor),
                "source_ids": [str(source_id)],
                "rationale": "r",
                "idempotency_key": "k",
            }
        )
    ]

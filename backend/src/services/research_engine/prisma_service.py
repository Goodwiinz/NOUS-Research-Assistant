"""Load the raw rows ``prisma.derive_prisma_flow`` counts (GOO-303).

One read-only pass, one SELECT per input kind, every query filtered by the
Collection. Screening input is GOO-302's ``screening_resolutions`` chain only
(rows exist only after reveal); raw observations are never read, so aggregate
counts cannot leak an unrevealed vote. Nothing is stored.
"""

from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_fulltext import (
    ResearchFulltextAttempt,
    ResearchFulltextRequest,
)
from src.models.research_import import ResearchImportReceipt, ResearchImportRecord
from src.models.research_project import ResearchProject
from src.models.research_protocol import ResearchProtocol
from src.models.research_report import ResearchReport, ResearchReportObservation
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource
from src.models.screening import ScreeningQueue, ScreeningResolution
from src.services.research_engine.prisma import (
    Attempt,
    Merge,
    Outcome,
    PrismaInconsistency,
    PrismaInputs,
    Record,
    Report,
)
from src.services.research_engine.project_access import ProjectContext

_RESOLVED = ("single", "agreement", "adjudicated")


async def _begin_snapshot(db: AsyncSession) -> None:
    """Every loader SELECT reads one REPEATABLE READ snapshot (PostgreSQL).

    A GET's access check has already opened a read-only transaction, which
    this rolls back: that discards no write, but it expires every ORM object
    loaded so far (``context.*``, ``current_user``), so callers read what they
    need before calling ``load_inputs``. A caller that has written (pending
    objects, or an assigned transaction id after a flush or row lock) is
    refused, never rolled back.
    """
    if db.get_bind().dialect.name != "postgresql":
        return  # ponytail: SQLite unit tests share one connection, no torn reads
    if db.in_transaction():
        level = (await db.execute(text("SHOW transaction_isolation"))).scalar_one()
        if level == "repeatable read":
            return  # the caller's snapshot (GOO-308 journey and audit bundle)
        wrote = (
            await db.execute(text("SELECT txid_current_if_assigned()"))
        ).scalar_one_or_none()
        if db.new or db.dirty or db.deleted or wrote is not None:
            raise RuntimeError("load PRISMA inputs outside a writing transaction")
        await db.rollback()
    await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})


async def _events(
    db: AsyncSession, collection_id: UUID, aggregate_type: str
) -> list[Any]:
    return list(
        (
            await db.execute(
                select(
                    ResearchDecisionEvent.id,
                    ResearchDecisionEvent.seq,
                    ResearchDecisionEvent.event_type,
                    ResearchDecisionEvent.payload,
                )
                .join(
                    ResearchDecisionStream,
                    ResearchDecisionStream.id == ResearchDecisionEvent.stream_id,
                )
                .where(
                    ResearchDecisionStream.collection_id == collection_id,
                    ResearchDecisionStream.aggregate_type == aggregate_type,
                    ResearchDecisionStream.aggregate_id == collection_id,
                )
            )
        ).all()
    )


async def load_inputs(db: AsyncSession, context: ProjectContext) -> PrismaInputs:
    """Read every PRISMA input for the Collection (VIEW; takes no locks)."""
    cid = cast(UUID, context.collection.id)  # before the snapshot expires it
    await _begin_snapshot(db)

    records: list[Record] = []
    for source_id, report_id, connector, metadata in (
        await db.execute(
            select(
                ResearchReportObservation.source_id,
                ResearchReportObservation.report_id,
                ResearchSource.connector_type,
                ResearchSource.metadata_,
            )
            .join(
                ResearchSource, ResearchSource.id == ResearchReportObservation.source_id
            )
            .where(ResearchReportObservation.collection_id == cid)
        )
    ).all():
        # In-run DOI merges keep every provider snapshot in provenance.
        provenance = (metadata or {}).get("provenance") or [{}]
        for index, entry in enumerate(provenance):
            origin = entry.get("connector_type") if isinstance(entry, dict) else None
            records.append(
                Record(
                    f"source:{source_id}:{index}",
                    "provider",
                    str(origin or connector),
                    report_id,
                )
            )

    rejected = 0
    for record_id, status, report_id, declared, kind in (
        await db.execute(
            select(
                ResearchImportRecord.id,
                ResearchImportRecord.status,
                ResearchImportRecord.report_id,
                ResearchImportReceipt.declared,
                ResearchImportReceipt.kind,
            )
            .join(
                ResearchImportReceipt,
                ResearchImportReceipt.id == ResearchImportRecord.receipt_id,
            )
            .where(ResearchImportRecord.collection_id == cid)
        )
    ).all():
        if status != "accepted":
            rejected += 1
            continue
        if report_id is None:
            raise PrismaInconsistency("accepted import record has no report")
        origin = (declared or {}).get("database") or kind
        records.append(Record(f"import:{record_id}", "import", str(origin), report_id))

    workspace_documents = (
        await db.execute(
            select(func.count(ResearchSource.id))
            .join(ResearchRun, ResearchRun.id == ResearchSource.run_id)
            .join(ResearchBlueprint, ResearchBlueprint.id == ResearchRun.blueprint_id)
            .join(ResearchProject, ResearchProject.id == ResearchBlueprint.project_id)
            .where(
                ResearchProject.collection_id == cid,
                ResearchSource.connector_type == "rag_store",
            )
        )
    ).scalar_one()

    reports = tuple(
        Report(*row)
        for row in (
            await db.execute(
                select(
                    ResearchReport.id,
                    ResearchReport.merged_into_report_id,
                    ResearchReport.study_id,
                    ResearchReport.study_link_status,
                ).where(ResearchReport.collection_id == cid)
            )
        ).all()
    )

    # Per (stage, report): the latest non-superseded queue containing it.
    queues = (
        (
            await db.execute(
                select(ScreeningQueue)
                .where(ScreeningQueue.collection_id == cid)
                .order_by(ScreeningQueue.created_at, ScreeningQueue.id)
            )
        )
        .scalars()
        .all()
    )
    superseded = {q.supersedes_queue_id for q in queues}
    chosen: dict[tuple[str, str], UUID] = {}
    for queue in queues:
        if queue.id not in superseded:
            for report in cast(list[str], queue.report_ids):
                chosen[(cast(str, queue.stage), str(report))] = cast(UUID, queue.id)
    stage_of = {cast(UUID, q.id): cast(str, q.stage) for q in queues}
    outcomes = []
    for row, seq in (
        await db.execute(
            select(ScreeningResolution, ResearchDecisionEvent.seq)
            .join(ScreeningQueue, ScreeningQueue.id == ScreeningResolution.queue_id)
            .join(
                ResearchDecisionEvent,
                ResearchDecisionEvent.id == ScreeningResolution.event_id,
            )
            .where(ScreeningQueue.collection_id == cid)
        )
    ).all():
        stage = stage_of[row.queue_id]
        if chosen.get((stage, str(row.report_id))) != row.queue_id:
            continue
        resolved = row.basis in _RESOLVED
        outcomes.append(
            Outcome(
                stage,
                row.report_id,
                row.outcome if resolved else None,
                row.exclusion_reason if resolved else None,
                row.event_id,
                seq,
                row.supersedes_resolution_id is not None,
                row.basis,
                row.created_at,
            )
        )

    acquisition = {}
    for event_id, seq, event_type, payload in await _events(
        db, cid, "research_acquisition"
    ):
        field = "request_id" if event_type == "acquisition.requested" else "attempt_id"
        acquisition[(field, payload[field])] = (event_id, seq)
    attempts = []
    for request_id, report_id, attempt in (
        await db.execute(
            select(
                ResearchFulltextRequest.id,
                ResearchFulltextRequest.report_id,
                ResearchFulltextAttempt,
            )
            .outerjoin(
                ResearchFulltextAttempt,
                ResearchFulltextAttempt.request_id == ResearchFulltextRequest.id,
            )
            .where(ResearchFulltextRequest.collection_id == cid)
        )
    ).all():
        key = ("request_id", str(request_id))
        if attempt is not None:
            key = ("attempt_id", str(attempt.id))
        if key not in acquisition:
            raise PrismaInconsistency("acquisition row has no ledger event")
        event_id, seq = acquisition[key]
        attempts.append(
            Attempt(
                request_id,
                report_id,
                None if attempt is None else attempt.id,
                None if attempt is None else attempt.outcome,
                seq,
                None if attempt is None else attempt.previous_attempt_id,
                None if attempt is None else event_id,
            )
        )

    merges = tuple(
        Merge(
            event_id,
            seq,
            UUID(payload["surviving_report_id"]),
            tuple(UUID(value) for value in payload["merged_report_ids"]),
        )
        for event_id, seq, event_type, payload in await _events(
            db, cid, "research_identity"
        )
        if event_type == "identity.report_merged"
    )

    heads = {
        f"{aggregate_type}:{aggregate_id}": next_seq - 1
        for aggregate_type, aggregate_id, next_seq in (
            await db.execute(
                select(
                    ResearchDecisionStream.aggregate_type,
                    ResearchDecisionStream.aggregate_id,
                    ResearchDecisionStream.next_seq,
                ).where(ResearchDecisionStream.collection_id == cid)
            )
        ).all()
    }
    protocol_versions = {str(q.protocol_version_id) for q in queues} | {
        str(v)
        for v in (
            await db.execute(
                select(ResearchProtocol.current_approved_version_id).where(
                    ResearchProtocol.collection_id == cid,
                    ResearchProtocol.is_deleted.is_(False),
                    ResearchProtocol.current_approved_version_id.is_not(None),
                )
            )
        ).scalars()
    }
    return PrismaInputs(
        records=tuple(records),
        rejected_imports=rejected,
        workspace_documents=int(workspace_documents),
        reports=reports,
        outcomes=tuple(outcomes),
        attempts=tuple(attempts),
        merges=merges,
        versions={
            "protocol_version_ids": sorted(protocol_versions),
            "stream_heads": dict(sorted(heads.items())),
        },
    )

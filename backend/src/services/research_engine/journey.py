"""Plan -> Discover -> Select -> Extract -> Write journey state (GOO-308).

``derive_stages`` is pure: each stage's status comes from its own facts only,
never from the stage before it, because real reviews loop back (an
amendment, a new import). The rail reports state and never enforces order;
the server gates on each write stay authoritative. ``facts`` reads the counts.
"""

from dataclasses import asdict, dataclass
from typing import Any, Callable, Literal, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.models.document import Document
from src.models.extraction_matrix import ExtractionMatrix
from src.models.generated_draft import GeneratedDraft
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_import import ResearchImportReceipt
from src.models.research_protocol import (
    ResearchProtocol,
    ResearchProtocolVersion,
    ResearchQuestion,
    ResearchQuestionVersion,
)
from src.models.research_report import ResearchReport
from src.models.research_run import ResearchRun
from src.models.screening import ScreeningQueue, ScreeningResolution
from src.services.research import draft_release_service, extraction_forms_service
from src.services.research_engine.prisma import PrismaInconsistency, derive_prisma_flow
from src.services.research_engine.prisma_service import load_inputs
from src.services.research_engine.project_access import (
    ProjectContext,
    project_documents_query,
)
from src.services.research_engine.run_conformance import _verify_plan

STAGES = ("plan", "discover", "select", "extract", "write")
StageKey = Literal["plan", "discover", "select", "extract", "write"]
Status = Literal["not_started", "in_progress", "attention", "complete"]
_CONFORMANT = ("plan_verified", "conformant")
_RESOLVED = ("single", "agreement", "adjudicated")


@dataclass(frozen=True)
class PlanFacts:
    question_versions: int = 0
    protocol_versions: int = 0
    approved_version: bool = False
    plan_error: str | None = None  # run_conformance._verify_plan's detail


@dataclass(frozen=True)
class DiscoverFacts:
    reports: int = 0
    runs: int = 0
    conformant_runs: int = 0  # completed and plan_verified/conformant
    failed_runs: int = 0
    import_receipts: int = 0


@dataclass(frozen=True)
class SelectFacts:
    # Resolutions (GOO-302 tips) and queue report ids only: a per-reviewer
    # observation count would tell reviewer A that reviewer B has submitted.
    queued_reports: int = 0
    resolved_reports: int = 0
    open_conflicts: int = 0
    pending_fulltext: int = 0  # requests whose head is not retrieved/unavailable
    not_retrieved: int = 0  # ``unavailable`` heads: terminal, never an exclusion
    excluded: int = 0
    prisma_error: str | None = None


@dataclass(frozen=True)
class ExtractFacts:
    matrices: int = 0
    cells: int = 0  # documents x fields of each current form version
    accepted_cells: int = 0
    stale_cells: int = 0
    disagreements: int = 0  # unaccepted cells with more than one observed value


@dataclass(frozen=True)
class WriteFacts:
    current_draft: bool = False
    release_status: str | None = None  # candidate | verified | stale
    release_blockers: int = 0


@dataclass(frozen=True)
class StageFacts:
    plan: PlanFacts = PlanFacts()
    discover: DiscoverFacts = DiscoverFacts()
    select: SelectFacts = SelectFacts()
    extract: ExtractFacts = ExtractFacts()
    write: WriteFacts = WriteFacts()


@dataclass(frozen=True)
class Stage:
    key: str
    status: Status
    facts: dict[str, Any]
    blockers: list[str]


def _plan(f: PlanFacts) -> tuple[Status, list[str]]:
    if f.plan_error is not None:
        return "attention", [f.plan_error]
    if f.question_versions and f.approved_version:
        return "complete", []
    if f.question_versions or f.protocol_versions:
        return "in_progress", []
    return "not_started", []


def _discover(f: DiscoverFacts) -> tuple[Status, list[str]]:
    if f.failed_runs:
        return "attention", [f"{f.failed_runs} failed run(s)"]
    if f.reports and (f.conformant_runs or f.import_receipts):
        return "complete", []
    if f.reports or f.runs or f.import_receipts:
        return "in_progress", []
    return "not_started", []


def _select(f: SelectFacts) -> tuple[Status, list[str]]:
    blockers = []
    if f.open_conflicts:
        blockers.append(f"{f.open_conflicts} open conflict(s)")
    if f.prisma_error is not None:
        blockers.append(f.prisma_error)
    if blockers:
        return "attention", blockers
    if f.pending_fulltext:
        return "in_progress", [f"{f.pending_fulltext} full text request(s) pending"]
    if f.queued_reports and f.resolved_reports >= f.queued_reports:
        return "complete", []
    if f.queued_reports:
        unresolved = f.queued_reports - f.resolved_reports
        return "in_progress", [f"{unresolved} report(s) unresolved"]
    return "not_started", []


def _extract(f: ExtractFacts) -> tuple[Status, list[str]]:
    blockers = []
    if f.stale_cells:
        blockers.append(f"{f.stale_cells} stale cell(s)")
    if f.disagreements:
        blockers.append(f"{f.disagreements} disagreement(s)")
    if blockers:
        return "attention", blockers
    if f.cells and f.accepted_cells >= f.cells:
        return "complete", []
    if f.matrices or f.accepted_cells:
        return "in_progress", [f"{f.cells - f.accepted_cells} cell(s) unaccepted"]
    return "not_started", []


def _write(f: WriteFacts) -> tuple[Status, list[str]]:
    if not f.current_draft:
        return "not_started", []
    if f.release_status == "verified":
        return "complete", []
    if f.release_status == "stale":
        return "attention", ["current draft release is stale"]
    if f.release_blockers:
        return "attention", [f"{f.release_blockers} release blocker(s)"]
    return "in_progress", []


def derive_stages(facts: StageFacts) -> list[Stage]:
    rules: tuple[Callable[[Any], tuple[Status, list[str]]], ...] = (
        _plan,
        _discover,
        _select,
        _extract,
        _write,
    )
    stages = []
    for key, rule in zip(STAGES, rules):
        stage_facts = getattr(facts, key)
        status, blockers = rule(stage_facts)
        stages.append(Stage(key, status, asdict(stage_facts), blockers))
    return stages


def current_stage(stages: list[Stage]) -> str | None:
    return next((s.key for s in stages if s.status != "complete"), None)


# --- reads -------------------------------------------------------------------


async def begin_read_snapshot(db: AsyncSession) -> None:
    """Start one REPEATABLE READ snapshot for every read after it.

    Call it before ``resolve_project`` (VIEW takes no locks): the rollback
    ends the request's implicit transaction and expires every ORM object
    loaded so far, so callers read ``current_user.id`` first. PRISMA's
    ``load_inputs`` then reuses this snapshot instead of opening its own, so
    a commit between two parts can never make them disagree.
    """
    await db.rollback()
    if db.get_bind().dialect.name == "postgresql":
        # ponytail: SQLite unit tests share one connection; no torn reads.
        # Not READ ONLY: GOO-299's ``replay_decisions`` reads its stream FOR
        # SHARE. ponytail: an identity decision committed mid-download makes
        # that lock fail with a serialization error (500); retry on 40001 if
        # downloads ever race identity work.
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))


async def _count(db: AsyncSession, query: Any) -> int:
    return int((await db.execute(query)).scalar_one())


async def _plan_facts(db: AsyncSession, cid: UUID) -> PlanFacts:
    questions = await _count(
        db,
        select(func.count(ResearchQuestionVersion.id))
        .join(
            ResearchQuestion, ResearchQuestion.id == ResearchQuestionVersion.question_id
        )
        .where(
            ResearchQuestion.collection_id == cid,
            ResearchQuestion.is_deleted.is_(False),
        ),
    )
    protocols = await _count(
        db,
        select(func.count(ResearchProtocolVersion.id))
        .join(
            ResearchProtocol, ResearchProtocol.id == ResearchProtocolVersion.protocol_id
        )
        .where(
            ResearchProtocol.collection_id == cid,
            ResearchProtocol.is_deleted.is_(False),
        ),
    )
    row = (
        await db.execute(
            select(ResearchProtocolVersion, ResearchBlueprint)
            .join(
                ResearchProtocol,
                ResearchProtocol.current_approved_version_id
                == ResearchProtocolVersion.id,
            )
            .join(
                ResearchBlueprint,
                ResearchBlueprint.id == ResearchProtocolVersion.blueprint_id,
            )
            .where(
                ResearchProtocol.collection_id == cid,
                ResearchProtocol.is_deleted.is_(False),
            )
            .order_by(ResearchProtocolVersion.created_at.desc())
            .limit(1)
        )
    ).first()
    error = None
    if row is not None:
        try:
            _verify_plan(row[0], row[1])
        except HTTPException as exc:  # the server's own 409/404/422 detail
            error = str(exc.detail)
    return PlanFacts(questions, protocols, row is not None, error)


async def _discover_facts(
    db: AsyncSession, context: ProjectContext, cid: UUID
) -> DiscoverFacts:
    reports = await _count(
        db,
        select(func.count(ResearchReport.id)).where(
            ResearchReport.collection_id == cid,
            ResearchReport.merged_into_report_id.is_(None),
        ),
    )
    receipts = await _count(
        db,
        select(func.count(ResearchImportReceipt.id)).where(
            ResearchImportReceipt.collection_id == cid
        ),
    )
    runs = conformant = failed = 0
    if context.engine is not None:
        for status, conformance in (
            await db.execute(
                select(ResearchRun.status, ResearchRun.conformance_status)
                .join(
                    ResearchBlueprint, ResearchBlueprint.id == ResearchRun.blueprint_id
                )
                .where(
                    ResearchBlueprint.project_id == context.engine.id,
                    ResearchRun.is_deleted.is_(False),
                )
            )
        ).all():
            runs += 1
            failed += status == "failed"
            conformant += status == "completed" and conformance in _CONFORMANT
    return DiscoverFacts(reports, runs, conformant, failed, receipts)


async def _select_facts(db: AsyncSession, context: ProjectContext) -> SelectFacts:
    """Resolution tips and queue report ids only, never observations."""
    cid = cast(UUID, context.collection.id)
    queues = (
        (
            await db.execute(
                select(ScreeningQueue).where(ScreeningQueue.collection_id == cid)
            )
        )
        .scalars()
        .all()
    )
    superseded = {q.supersedes_queue_id for q in queues}
    live = [q for q in queues if q.id not in superseded]
    newer = aliased(ScreeningResolution)
    bases = list(
        (
            await db.execute(
                select(ScreeningResolution.basis).where(
                    ScreeningResolution.queue_id.in_([q.id for q in live]),
                    ~exists().where(
                        newer.supersedes_resolution_id == ScreeningResolution.id
                    ),
                )
            )
        ).scalars()
    )
    counts: dict[str, Any] = {}
    error = None
    try:
        counts = derive_prisma_flow(await load_inputs(db, context))["counts"]
    except PrismaInconsistency:
        error = "PRISMA flow inconsistent"
    return SelectFacts(
        queued_reports=sum(len(cast(list[str], q.report_ids)) for q in live),
        resolved_reports=sum(b in _RESOLVED for b in bases),
        open_conflicts=sum(b == "conflict" for b in bases),
        pending_fulltext=int(counts.get("reports_awaiting_retrieval", 0)),
        not_retrieved=int(counts.get("reports_not_retrieved", 0)),
        excluded=int(counts.get("records_excluded", 0))
        + sum((counts.get("reports_excluded_by_reason") or {}).values()),
        prisma_error=error,
    )


async def _extract_facts(db: AsyncSession, cid: UUID) -> ExtractFacts:
    matrices = (
        (
            await db.execute(
                select(ExtractionMatrix).where(
                    ExtractionMatrix.project_id == cid,
                    ExtractionMatrix.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    allowed = sorted(
        (await db.execute(project_documents_query(cid).with_only_columns(Document.id)))
        .scalars()
        .all(),
        key=str,
    )
    cells = accepted = stale = disputed = 0
    for matrix in matrices:
        form, grid = await extraction_forms_service.cell_view(db, matrix, allowed)
        fields = form["fields"] if form else matrix.columns
        cells += len(allowed) * len(fields)
        for cell in grid:
            if cell["source"] == "accepted":
                accepted += 1
                stale += bool(cell["stale"])
            elif cell.get("observed_values", 0) > 1:
                disputed += 1
    return ExtractFacts(len(matrices), cells, accepted, stale, disputed)


async def _write_facts(db: AsyncSession, context: ProjectContext) -> WriteFacts:
    draft = (
        await db.execute(
            select(GeneratedDraft.id, GeneratedDraft.version)
            .where(
                GeneratedDraft.project_id == context.collection.id,
                GeneratedDraft.is_current.is_(True),
                GeneratedDraft.is_deleted.is_(False),
            )
            .order_by(GeneratedDraft.version.desc())
            .limit(1)
        )
    ).first()
    if draft is None:
        return WriteFacts()
    check = await draft_release_service.check(db, context, draft[0], draft[1])
    return WriteFacts(True, check.release_status, len(check.blockers))


async def facts(db: AsyncSession, context: ProjectContext) -> StageFacts:
    """Count queries over what GOO-299..307 persist (VIEW, no writes)."""
    cid = cast(UUID, context.collection.id)
    plan = await _plan_facts(db, cid)
    discover = await _discover_facts(db, context, cid)
    extract = await _extract_facts(db, cid)
    write = await _write_facts(db, context)
    select_ = await _select_facts(db, context)  # PRISMA last (see audit_bundle)
    return StageFacts(plan, discover, select_, extract, write)

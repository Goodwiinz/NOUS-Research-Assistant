"""Plan -> Discover -> Select -> Extract -> Write journey state (GOO-308).

``derive_stages`` is pure: each stage's status comes from its own facts only,
never from the stage before it, because real reviews loop back (an
amendment, a new import). The rail reports state and never enforces order;
the server gates on each write stay authoritative. ``facts`` reads the counts.
"""

from dataclasses import asdict, dataclass
from typing import Any, Callable, Literal

STAGES = ("plan", "discover", "select", "extract", "write")
Status = Literal["not_started", "in_progress", "attention", "complete"]


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

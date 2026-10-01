"""Pure journey stage derivation (GOO-308): each stage from its own facts."""

from dataclasses import replace

from src.services.research_engine.journey import (
    STAGES,
    DiscoverFacts,
    ExtractFacts,
    PlanFacts,
    SelectFacts,
    StageFacts,
    WriteFacts,
    current_stage,
    derive_stages,
)

DRIFT = "Blueprint changes require a protocol amendment"
DONE = StageFacts(
    plan=PlanFacts(question_versions=1, protocol_versions=1, approved_version=True),
    discover=DiscoverFacts(reports=3, runs=1, conformant_runs=1),
    select=SelectFacts(queued_reports=3, resolved_reports=3),
    extract=ExtractFacts(matrices=1, cells=4, accepted_cells=4),
    write=WriteFacts(current_draft=True, release_status="verified"),
)


def _status(facts: StageFacts) -> dict[str, str]:
    return {s.key: s.status for s in derive_stages(facts)}


def test_empty_project_all_not_started_current_is_plan() -> None:
    stages = derive_stages(StageFacts())
    assert [s.key for s in stages] == list(STAGES)
    assert {s.status for s in stages} == {"not_started"}
    assert current_stage(stages) == "plan"
    assert current_stage(derive_stages(DONE)) is None


def test_plan_drift_is_attention_with_server_detail() -> None:
    facts = replace(DONE, plan=replace(DONE.plan, plan_error=DRIFT))
    [plan] = [s for s in derive_stages(facts) if s.key == "plan"]
    assert plan.status == "attention"
    assert plan.blockers == [DRIFT]
    assert plan.facts["plan_error"] == DRIFT


def test_discover_requires_reports_and_a_conformant_run_or_import() -> None:
    def discover(**kw: int) -> str:
        return _status(StageFacts(discover=DiscoverFacts(**kw)))["discover"]

    assert discover(reports=2) == "in_progress"
    assert discover(runs=1, conformant_runs=1) == "in_progress"  # no reports
    assert discover(reports=2, runs=1) == "in_progress"  # run not conformant
    assert discover(reports=2, runs=1, conformant_runs=1) == "complete"
    assert discover(reports=2, import_receipts=1) == "complete"


def test_failed_run_marks_discover_attention_not_complete() -> None:
    facts = replace(DONE, discover=replace(DONE.discover, runs=2, failed_runs=1))
    [discover] = [s for s in derive_stages(facts) if s.key == "discover"]
    assert discover.status == "attention"
    assert discover.blockers == ["1 failed run(s)"]


def test_select_open_conflict_or_pending_fulltext_blocks_complete() -> None:
    conflict = replace(DONE, select=replace(DONE.select, open_conflicts=1))
    pending = replace(DONE, select=replace(DONE.select, pending_fulltext=1))
    broken = replace(DONE, select=replace(DONE.select, prisma_error="inconsistent"))
    unresolved = replace(DONE, select=replace(DONE.select, resolved_reports=2))
    assert _status(conflict)["select"] == "attention"
    assert _status(pending)["select"] == "in_progress"
    assert _status(broken)["select"] == "attention"
    assert _status(unresolved)["select"] == "in_progress"
    assert _status(DONE)["select"] == "complete"


def test_unavailable_fulltext_is_terminal_not_exclusion() -> None:
    facts = replace(DONE, select=replace(DONE.select, not_retrieved=1, excluded=2))
    [select] = [s for s in derive_stages(facts) if s.key == "select"]
    assert select.status == "complete"
    assert select.facts["not_retrieved"] == 1
    assert select.facts["excluded"] == 2  # unchanged by the unavailable head
    assert select.blockers == []


def test_extract_stale_cell_is_attention() -> None:
    stale = replace(DONE, extract=replace(DONE.extract, stale_cells=1))
    disputed = replace(DONE, extract=replace(DONE.extract, disagreements=1))
    partial = replace(DONE, extract=replace(DONE.extract, accepted_cells=3))
    empty = replace(DONE, extract=ExtractFacts(matrices=1))
    assert _status(stale)["extract"] == "attention"
    assert _status(disputed)["extract"] == "attention"
    assert _status(partial)["extract"] == "in_progress"
    assert _status(empty)["extract"] == "in_progress"  # zero cells never complete


def test_write_complete_only_when_current_draft_verified() -> None:
    def write(**kw: object) -> str:
        return _status(StageFacts(write=WriteFacts(**kw)))["write"]  # type: ignore[arg-type]

    assert write() == "not_started"
    assert write(current_draft=True, release_status="candidate") == "in_progress"
    assert write(current_draft=True, release_status="stale") == "attention"
    assert (
        write(current_draft=True, release_status="candidate", release_blockers=2)
        == "attention"
    )
    assert write(current_draft=True, release_status="verified") == "complete"


def test_stages_independent_current_is_first_incomplete() -> None:
    facts = replace(DONE, select=replace(DONE.select, open_conflicts=1))
    stages = derive_stages(facts)
    assert _status(facts) == {
        "plan": "complete",
        "discover": "complete",
        "select": "attention",
        "extract": "complete",
        "write": "complete",
    }
    assert current_stage(stages) == "select"

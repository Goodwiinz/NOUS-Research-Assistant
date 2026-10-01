"""Pure superseding-review rules (GOO-320). No I/O.

- **Carry-forward.** A parent screening decision is carried **by reference**
  (resolution and event ids, never copied actor fields) when the report's
  accepted delta class is ``unchanged``, the parent tip has a resolved
  basis, the successor protocol's criteria hash equals the resolution's, and
  the report was not merged or split since the parent. Full text is only
  considered after a carried title/abstract ``include``.
- **Required work.** ``new``, ``changed`` and ``corrected_retracted`` reports
  need title/abstract review; an ``unchanged`` report that fails the criteria
  or identity test needs that stage again. ``unknown`` (and a report missing
  from the delta) is ``needs_attention``: neither carried nor queued, unless
  the supervisor names it ``carried_with_uncertainty``, which carries it with
  an explicit flag. Unchanged title/DOI with a changed record is ``changed``
  upstream (GOO-319), so it is re-reviewed.
- **Missing history.** A report without a resolved tip (or whose tip no longer
  replays to an actor) is labelled, never defaulted to ``exclude``.
- **Accounting.** Two independent derivations must agree: each version's
  ``prisma.derive_prisma_flow`` counts and the study units of the decisions
  it rests on; then ``total == previous - amended_out + new + amended_in`` and
  ``new records == |successor keys - parent keys|``. Anything else raises
  ``PrismaInconsistency``.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Collection, Iterable, Mapping, Sequence
from uuid import UUID

from src.services.research_engine.contracts import canonical_json_sha256
from src.services.research_engine.prisma import PrismaInconsistency, PrismaInputs

STAGES = ("title_abstract", "full_text")
RESOLVED_BASES = ("single", "agreement", "adjudicated")
REQUIRED_CLASSES = ("new", "changed", "corrected_retracted")
NOT_IN_DELTA = "not_in_delta"
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ParentDecision:
    """One parent resolution tip for ``(stage, report)``."""

    report_id: UUID
    stage: str
    resolution_id: UUID
    event_id: UUID
    outcome: str | None
    basis: str
    criteria_hash: str

    def ref(self) -> dict[str, Any]:
        """The by-reference record: attribution resolves through ``event_id``."""
        return {
            "report_id": str(self.report_id),
            "stage": self.stage,
            "resolution_id": str(self.resolution_id),
            "event_id": str(self.event_id),
            "outcome": self.outcome,
            "basis": self.basis,
        }


def _tips(parents: Iterable[ParentDecision]) -> dict[tuple[str, UUID], ParentDecision]:
    return {(p.stage, p.report_id): p for p in parents}


def carry_forward(
    delta: Mapping[str, Mapping[str, Any]],
    parents: Sequence[ParentDecision],
    criteria: Mapping[str, str],
    identity_changed: Collection[UUID],
    *,
    reports: Sequence[UUID],
    uncertain: Collection[UUID] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, list[UUID]], list[dict[str, Any]]]:
    """``(carried, required_work, needs_attention)`` for the successor corpus
    ``reports``; ``delta`` maps report id -> its GOO-319 item."""
    tips = _tips(parents)
    carried: list[dict[str, Any]] = []
    required: dict[str, list[UUID]] = {stage: [] for stage in STAGES}
    attention: list[dict[str, Any]] = []
    for report in sorted(set(reports), key=str):
        item = delta.get(str(report))
        cls = None if item is None else item.get("class")
        if cls in REQUIRED_CLASSES:
            required[STAGES[0]].append(report)
            continue
        flagged = cls == "unknown" and report in uncertain
        if cls != "unchanged" and not flagged:
            reason = NOT_IN_DELTA if item is None else item.get("reason")
            attention.append({"report_id": str(report), "class": cls, "reason": reason})
            continue
        for stage in STAGES:
            parent = tips.get((stage, report))
            if parent is None or parent.basis not in RESOLVED_BASES:
                break  # missing history, labelled by ``missing_history``
            if parent.criteria_hash != criteria[stage] or report in identity_changed:
                required[stage].append(report)
                break
            ref = parent.ref()
            if flagged:
                ref["uncertain"] = True
            carried.append(ref)
            if parent.outcome != "include":
                break
    return carried, required, attention


def missing_history(
    reports: Sequence[UUID],
    parents: Sequence[ParentDecision],
    stages: Sequence[str] = STAGES,
    *,
    unattributed: Collection[UUID] = frozenset(),
) -> list[dict[str, Any]]:
    """``decision_missing`` per stage a report never got a resolved decision
    in (full text only after a title/abstract include), and
    ``attribution_missing`` for a tip whose event no longer replays."""
    tips = _tips(parents)
    missing: list[dict[str, Any]] = []
    for report in sorted(set(reports), key=str):
        for stage in stages:
            parent = tips.get((stage, report))
            if parent is None or parent.basis not in RESOLVED_BASES:
                kind = "decision_missing"
            elif parent.resolution_id in unattributed:
                kind = "attribution_missing"
            else:
                kind = None
            if kind is not None:
                missing.append({"report_id": str(report), "stage": stage, "kind": kind})
            if (
                parent is None
                or kind == "decision_missing"
                or parent.outcome != "include"
            ):
                break
    return missing


def included_units(inputs: PrismaInputs) -> dict[str, str]:
    """``report_id -> study unit`` for every included report, by the same
    rule as ``prisma.derive_prisma_flow`` (full-text tip per survivor, a
    confirmed study link is one study, anything else its own report)."""
    reports = {r.id: r for r in inputs.reports}

    def final(report_id: UUID) -> UUID:
        seen: set[UUID] = set()
        while (row := reports.get(report_id)) is not None and row.merged_into:
            if report_id in seen:
                raise PrismaInconsistency("report merge cycle")
            seen.add(report_id)
            report_id = row.merged_into
        return report_id

    universe = {final(r.report_id) for r in inputs.records}
    chains: dict[UUID, list[Any]] = {}
    for outcome in inputs.outcomes:
        if outcome.stage == STAGES[1]:
            chains.setdefault(outcome.report_id, []).append(outcome)
    current: dict[UUID, dict[UUID, Any]] = {}
    for report_id, chain in chains.items():
        tip = max(chain, key=lambda o: o.seq)
        current.setdefault(final(report_id), {})[report_id] = tip
    units: dict[str, str] = {}
    for survivor, rows in current.items():
        if survivor not in universe:
            continue
        winner = rows.get(survivor) or max(
            rows.values(), key=lambda o: (o.created_at or _EPOCH, str(o.event_id))
        )
        if winner.decision != "include":
            continue
        report = reports[survivor]
        confirmed = report.study_id is not None and report.study_status == "confirmed"
        units[str(survivor)] = (
            f"study:{report.study_id}" if confirmed else f"report:{survivor}"
        )
    return units


def _counter_diff(
    after: Mapping[str, int], before: Mapping[str, int]
) -> dict[str, int]:
    diff = Counter(after)
    diff.subtract(Counter(before))
    return {k: v for k, v in sorted(diff.items()) if v}


def accounting(
    *,
    parent_flow: Mapping[str, Any],
    successor_flow: Mapping[str, Any],
    delta_counts: Mapping[str, int],
    carried: Sequence[Mapping[str, Any]],
    parent_keys: Iterable[str],
    successor_keys: Iterable[str],
    parent_units: Mapping[str, str],
    successor_units: Mapping[str, str],
    parent_reports: Collection[str],
) -> dict[str, Any]:
    """PRISMA 2020 boxes for an updated review; raises ``PrismaInconsistency``
    when the parent and successor do not reconcile."""
    pc, sc = parent_flow["counts"], successor_flow["counts"]
    before, after = set(parent_keys), set(successor_keys)
    previous = set(parent_units.values())
    total = set(successor_units.values())
    amended_out = previous - total
    gained = total - previous
    reopened = {
        unit
        for report, unit in successor_units.items()
        if unit in gained and report in parent_reports
    }
    new = gained - reopened
    new_records = after - before
    checks = {
        "parent_flow_matches_decisions": len(previous) == pc["included_studies"],
        "successor_flow_matches_decisions": len(total) == sc["included_studies"],
        "parent_records_retained": before <= after,
        "new_records_match_flows": len(new_records)
        == sc["records_identified"] - pc["records_identified"],
        "studies_reconcile": sc["included_studies"]
        == pc["included_studies"] - len(amended_out) + len(new) + len(reopened),
    }
    failed = sorted(name for name, ok in checks.items() if not ok)
    if failed:
        raise PrismaInconsistency(
            "update accounting does not reconcile: " + ", ".join(failed)
        )
    carried_ta = [c for c in carried if c.get("stage") == STAGES[0]]
    amended = [
        {"report_id": report, "from": "include", "to": "not_included"}
        for report in sorted(set(parent_units) - set(successor_units))
    ] + [
        {"report_id": report, "from": "not_included", "to": "include"}
        for report in sorted(set(successor_units) - set(parent_units))
        if report in parent_reports
    ]
    return {
        "studies_in_previous_version": pc["included_studies"],
        "reports_in_previous_version": pc["included_reports"],
        "new_records_identified": len(new_records),
        "new_records_by_source": _counter_diff(
            {**sc["records_by_source"], **sc["records_by_import"]},
            {**pc["records_by_source"], **pc["records_by_import"]},
        ),
        "duplicates_removed": sc["duplicates_removed"] - pc["duplicates_removed"],
        "records_screened": sc["records_screened"] - len(carried_ta),
        "records_excluded": sc["records_excluded"]
        - sum(c.get("outcome") == "exclude" for c in carried_ta),
        "reports_sought": sc["reports_sought"] - pc["reports_sought"],
        "reports_not_retrieved": sc["reports_not_retrieved"]
        - pc["reports_not_retrieved"],
        "reports_excluded_by_reason_change": _counter_diff(
            sc["reports_excluded_by_reason"], pc["reports_excluded_by_reason"]
        ),
        "changed_sources": int(delta_counts.get("changed", 0)),
        "corrected_retracted": int(delta_counts.get("corrected_retracted", 0)),
        "carried_decisions": len(carried),
        "amended_inclusion": amended,
        "amended_out": len(amended_out),
        "amended_in": len(reopened),
        "new_studies_included": len(new),
        "total_studies_included": sc["included_studies"],
        "checks": checks,
    }


def version_hash(body: Mapping[str, Any]) -> str:
    """``content_hash``: canonical JSON SHA-256 of the version body."""
    return canonical_json_sha256(body)

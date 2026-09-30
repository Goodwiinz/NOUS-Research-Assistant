"""Pure PRISMA 2020 flow derivation (GOO-303).

No database access: ``prisma_service.load_inputs`` reads the rows once and this
module does all the counting. Totals are never stored; the same rows always
give the same body, and a violated reconciliation check raises
``PrismaInconsistency`` instead of returning a wrong flow.
"""

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from uuid import UUID

from src.services.research_engine.contracts import (
    canonical_json_bytes,
    canonical_json_sha256,
)

SCHEMA = "nous.academic.prisma-flow.v1"
_DECIDED = ("include", "exclude")


@dataclass(frozen=True)
class Record:
    """One identified record: a provider snapshot or an accepted import row."""

    key: str
    origin_kind: str  # "provider" | "import"
    origin: str  # connector_type, or the import's declared database
    report_id: UUID


@dataclass(frozen=True)
class Report:
    id: UUID
    merged_into: UUID | None
    study_id: UUID | None
    study_status: str | None


@dataclass(frozen=True)
class Outcome:
    """One resolution row of the report's current queue for ``stage``.

    ``decision`` is set only for a resolved basis (single / agreement /
    adjudicated); the row with the highest ``seq`` is the chain tip.
    """

    stage: str
    report_id: UUID
    decision: str | None
    reason: str | None
    event_id: UUID
    seq: int
    supersedes: bool
    basis: str


@dataclass(frozen=True)
class Attempt:
    """One attempt row, or a bare request (``attempt_id`` None, no attempts)."""

    request_id: UUID
    report_id: UUID
    attempt_id: UUID | None
    outcome: str | None
    seq: int
    previous_attempt_id: UUID | None
    event_id: UUID | None = None


@dataclass(frozen=True)
class Merge:
    event_id: UUID
    seq: int
    surviving_report_id: UUID
    merged_report_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class PrismaInputs:
    records: tuple[Record, ...]
    rejected_imports: int
    workspace_documents: int
    reports: tuple[Report, ...]
    outcomes: tuple[Outcome, ...]
    attempts: tuple[Attempt, ...]
    merges: tuple[Merge, ...]
    versions: Mapping[str, Any] = field(default_factory=dict)


class PrismaInconsistency(ValueError):
    """The persisted rows cannot produce one consistent flow."""


def _unique(values: Iterable[Any], what: str) -> None:
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            raise PrismaInconsistency(f"duplicate {what}")
        seen.add(value)


def _label(outcome: Outcome) -> str:
    return outcome.decision or outcome.basis


def _amendment(
    aggregate: str,
    event: UUID | None,
    seq: int,
    kind: str,
    report: UUID,
    a: str,
    b: str,
) -> dict[str, Any]:
    return {
        "event_id": str(event),
        "aggregate_type": aggregate,
        "seq": seq,
        "kind": kind,
        "report_id": str(report),
        "from": a,
        "to": b,
    }


def derive_prisma_flow(inputs: PrismaInputs) -> dict[str, Any]:
    """The PRISMA 2020 flow body: counts, amendments, warnings, checks, versions."""
    _unique((r.key for r in inputs.records), "record")
    _unique((r.id for r in inputs.reports), "report")
    _unique((o.event_id for o in inputs.outcomes), "resolution event")
    _unique((a.attempt_id for a in inputs.attempts if a.attempt_id), "attempt")
    _unique((m.event_id for m in inputs.merges), "merge event")
    reports = {r.id: r for r in inputs.reports}
    warnings: list[str] = []
    amendments: list[dict[str, Any]] = []

    def final(report_id: UUID) -> UUID:
        seen = set()
        while (row := reports.get(report_id)) is not None and row.merged_into:
            if report_id in seen:
                raise PrismaInconsistency("report merge cycle")
            seen.add(report_id)
            report_id = row.merged_into
        if row is None:
            raise PrismaInconsistency("row names an unknown report")
        return report_id

    # Identification and duplicates.
    universe = {final(r.report_id) for r in inputs.records}
    by_kind: dict[str, Counter[str]] = {"provider": Counter(), "import": Counter()}
    for record in inputs.records:
        by_kind[record.origin_kind][record.origin] += 1
    corpus_hash = hashlib.sha256(
        canonical_json_bytes(
            sorted([r.key, str(final(r.report_id))] for r in inputs.records)
        )
    ).hexdigest()

    # Screening: tip per (stage, report); collisions after merges.
    chains: dict[tuple[str, UUID], list[Outcome]] = {}
    for outcome in inputs.outcomes:
        chains.setdefault((outcome.stage, outcome.report_id), []).append(outcome)
    current: dict[tuple[str, UUID], dict[UUID, Outcome]] = {}
    for (stage, report_id), chain in chains.items():
        chain.sort(key=lambda o: o.seq)
        _unique((o.seq for o in chain), "resolution seq")
        for previous, row in zip(chain, chain[1:]):
            if not row.supersedes:
                raise PrismaInconsistency("resolution chain has two roots")
            amendments.append(
                _amendment(
                    "research_screening",
                    row.event_id,
                    row.seq,
                    f"{stage}."
                    + (
                        row.basis
                        if row.basis in ("adjudicated", "reopened")
                        else "rederived"
                    ),
                    report_id,
                    _label(previous),
                    _label(row),
                )
            )
        current.setdefault((stage, final(report_id)), {})[report_id] = chain[-1]

    decided: dict[str, dict[UUID, Outcome]] = {"title_abstract": {}, "full_text": {}}
    for (stage, survivor), rows in current.items():
        if survivor not in universe:
            warnings.append(f"{stage} outcome on report {survivor} without records")
            continue
        winner = rows.get(survivor) or max(rows.values(), key=lambda o: o.seq)
        if len({_label(o) for o in rows.values()}) > 1:
            warnings.append(
                f"{stage} outcomes differ across merged reports of {survivor}; "
                f"using {winner.report_id}"
            )
        if winner.decision in _DECIDED:
            decided[stage][survivor] = winner

    # Acquisition: one linear chain per request.
    requests: dict[UUID, list[Attempt]] = {}
    for attempt in inputs.attempts:
        requests.setdefault(attempt.request_id, []).append(attempt)
    heads: dict[UUID, list[Attempt]] = {}
    for attempts in requests.values():
        if len({att.report_id for att in attempts}) != 1:
            raise PrismaInconsistency("request names several reports")
        if any(att.attempt_id is None for att in attempts):
            if len(attempts) != 1:
                raise PrismaInconsistency("bare request att alongside attempts")
            heads.setdefault(final(attempts[0].report_id), []).append(attempts[0])
            continue
        by_id = {att.attempt_id: att for att in attempts}
        links = [att.previous_attempt_id for att in attempts]
        _unique(links, "attempt chain link")
        if sum(p is None for p in links) != 1 or not {
            p for p in links if p is not None
        } <= set(by_id):
            raise PrismaInconsistency("attempt chain is not linear")
        head = [att for att in attempts if att.attempt_id not in links]
        heads.setdefault(final(attempts[0].report_id), []).append(head[0])
        for att in attempts:
            before = by_id.get(att.previous_attempt_id)
            if before is not None and before.outcome == "unavailable":
                amendments.append(
                    _amendment(
                        "research_acquisition",
                        att.event_id,
                        att.seq,
                        f"acquisition.{att.outcome}",
                        att.report_id,
                        "unavailable",
                        str(att.outcome),
                    )
                )
    state: dict[UUID, str] = {}
    for survivor, report_heads in heads.items():
        if any(h.outcome == "retrieved" for h in report_heads):
            state[survivor] = "retrieved"
        else:
            state[survivor] = (
                max(report_heads, key=lambda h: h.seq).outcome or "pending"
            )

    # Merges whose losers already carried an outcome.
    touched = {o.report_id for o in inputs.outcomes if o.decision} | {
        a.report_id for a in inputs.attempts if a.outcome
    }
    for merge in inputs.merges:
        for loser in merge.merged_report_ids:
            if loser in touched:
                amendments.append(
                    _amendment(
                        "research_identity",
                        merge.event_id,
                        merge.seq,
                        "identity.report_merged",
                        loser,
                        str(loser),
                        str(merge.surviving_report_id),
                    )
                )
    amendments.sort(key=lambda a: (a["aggregate_type"], a["seq"], a["event_id"]))

    # Counts.
    screened = decided["title_abstract"]
    assessed = decided["full_text"]
    included = [s for s, o in assessed.items() if o.decision == "include"]
    excluded_by_reason = Counter(
        str(o.reason) for o in assessed.values() if o.decision == "exclude"
    )
    studies: set[Any] = set()
    unconfirmed = 0
    for survivor in included:
        report = reports[survivor]
        if report.study_id is not None and report.study_status == "confirmed":
            studies.add(report.study_id)
        else:
            studies.add(survivor)
            unconfirmed += report.study_id is not None
    retrieved = {s for s, value in state.items() if value == "retrieved"}
    counts: dict[str, Any] = {
        "records_identified": len(inputs.records),
        "records_by_source": dict(sorted(by_kind["provider"].items())),
        "records_by_import": dict(sorted(by_kind["import"].items())),
        "import_rejected": inputs.rejected_imports,
        "duplicates_removed": len(inputs.records) - len(universe),
        "unique_reports": len(universe),
        "records_screened": len(screened),
        "records_excluded": sum(o.decision == "exclude" for o in screened.values()),
        "records_awaiting_screening": len(universe - set(screened)),
        "reports_sought": len(state),
        "reports_not_retrieved": sum(v == "unavailable" for v in state.values()),
        "reports_awaiting_retrieval": sum(
            v in ("requested", "pending") for v in state.values()
        ),
        "reports_assessed": len(assessed),
        "reports_excluded_by_reason": dict(sorted(excluded_by_reason.items())),
        "included_reports": len(included),
        "included_studies": len(studies),
        "unconfirmed_study_links": unconfirmed,
    }
    checks = {
        "screened_plus_awaiting_equals_unique": counts["records_screened"]
        + counts["records_awaiting_screening"]
        == counts["unique_reports"],
        "assessed_within_retrieved": set(assessed) <= retrieved
        and counts["reports_assessed"]
        <= counts["reports_sought"] - counts["reports_not_retrieved"],
        "included_plus_excluded_equals_assessed": counts["included_reports"]
        + sum(excluded_by_reason.values())
        == counts["reports_assessed"],
    }
    failed = sorted(name for name, ok in checks.items() if not ok)
    if failed:
        raise PrismaInconsistency("reconciliation failed: " + ", ".join(failed))
    return {
        "counts": counts,
        "excluded_from_flow": {"workspace_documents": inputs.workspace_documents},
        "amendments": amendments,
        "warnings": sorted(warnings),
        "checks": checks,
        "versions": {**inputs.versions, "corpus_hash": corpus_hash},
    }


def package(body: dict[str, Any]) -> dict[str, Any]:
    """The export envelope; ``body_sha256`` ignores ``generated_at``."""
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "body_sha256": canonical_json_sha256(body),
        "body": body,
    }


def render_markdown(body: dict[str, Any]) -> str:
    """A counts table plus a Mermaid flowchart of the PRISMA boxes."""
    c = body["counts"]
    lines = ["# PRISMA 2020 flow", "", "| Count | Value |", "|---|---|"]
    for key, value in c.items():
        if isinstance(value, dict):
            value = ", ".join(f"{k}: {v}" for k, v in value.items()) or "none"
        lines.append(f"| {key} | {value} |")
    lines += [
        "",
        "```mermaid",
        "flowchart TD",
        f'  I["Records identified: {c["records_identified"]}"]'
        f' --> D["Duplicates removed: {c["duplicates_removed"]}"]',
        f'  D --> S["Records screened: {c["records_screened"]}"]',
        f'  S --> SX["Records excluded: {c["records_excluded"]}"]',
        f'  S --> R["Reports sought: {c["reports_sought"]}"]',
        f'  R --> RN["Reports not retrieved: {c["reports_not_retrieved"]}"]',
        f'  R --> A["Reports assessed: {c["reports_assessed"]}"]',
        f'  A --> AX["Reports excluded: {sum(c["reports_excluded_by_reason"].values())}"]',
        f'  A --> N["Included: {c["included_studies"]} studies,'
        f' {c["included_reports"]} reports"]',
        "```",
        "",
    ]
    return "\n".join(lines)

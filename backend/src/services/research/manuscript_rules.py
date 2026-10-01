"""Pure manuscript release rules (GOO-315): no database, no FastAPI.

A release keeps six results apart (never one score). Four are obligations a
verified promotion needs on fresh re-evaluation; reporting completeness and
experiment reproducibility are reported only. Claim support is never decided
here: it is read from GOO-307's live ``draft_release`` for the same content
hash. GOO-316 adds ``statements`` and ``venue``: obligations only when the
snapshot binds a statement set (``obligations``), ``not_applicable``
otherwise, so a release without statements keeps GOO-315's semantics.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from src.services.research.venue_rules import PROFILE_VERSION
from src.services.research_engine.contracts import canonical_json_sha256

SNAPSHOT_SCHEMA = "nous.manuscript-snapshot/1"
PACKAGE_SCHEMA = "nous.manuscript-release.v1"
CHECK_KEYS = (
    "reporting_completeness",
    "method_adherence",
    "claim_support",
    "experiment_reproducibility",
    "peer_review",
    "synthesis_appraisal",
    "statements",
    "venue",
)
VERIFIED_OBLIGATIONS = (
    "claim_support",
    "method_adherence",
    "synthesis_appraisal",
    "peer_review",
)
STATEMENT_OBLIGATIONS = ("statements", "venue")
PASSING = frozenset({"pass", "not_applicable"})
AMENDMENT_STATUSES = frozenset({"approved", "superseded"})
State = Literal["pass", "fail", "unknown", "not_applicable"]
Prisma = Literal["consistent", "inconsistent", "none"]

_BIB_ENTRY = re.compile(r"^@\w+\{([^,\s]+),", re.MULTILINE)


@dataclass(frozen=True)
class ReleaseIn:
    """GOO-307's newest release row for the draft; ``live`` means verified
    (not stamped and not reached by the derived walk)."""

    id: str
    content_hash: str
    live: bool


@dataclass(frozen=True)
class RunIn:
    id: str
    conformance_status: str


@dataclass(frozen=True)
class DeviationIn:
    id: str
    protocol_version_id: str
    run_id: str | None
    disposition: str  # free text: reported, never trusted


@dataclass(frozen=True)
class VersionIn:
    """A protocol version that might amend a deviated parent."""

    id: str
    parent_version_id: str | None
    change_kind: str
    status: str


@dataclass(frozen=True)
class FigureIn:
    figure_key: str
    completeness: str
    reproduction: str  # reproduced | not_reproduced | not_attempted


@dataclass(frozen=True)
class VenueIn:
    """A ``venue_checks`` row of the release being evaluated (oldest first)."""

    id: str
    package_sha256: str
    profile_version: int
    status: str
    failing_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckInputs:
    content_hash: str
    draft_release: ReleaseIn | None = None
    claim_blockers: tuple[Mapping[str, Any], ...] = ()
    protocol_version_id: str | None = None
    runs: tuple[RunIn, ...] = ()
    deviations: tuple[DeviationIn, ...] = ()
    versions: tuple[VersionIn, ...] = ()
    synthesis_required: bool = False
    synthesis_current: bool = False
    appraisal_required: bool = False
    appraisal_complete: bool = False
    has_review_rounds: bool = False
    open_obligations: tuple[Mapping[str, Any], ...] = ()
    figures: tuple[FigureIn, ...] = ()
    prisma: Prisma = "none"
    statements_bound: bool = False
    unapproved_authors: tuple[str, ...] = ()
    package_sha256: str | None = None
    venue_checks: tuple[VenueIn, ...] = ()


def obligations(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    """GOO-315's obligations, plus ``statements`` and ``venue`` when the
    snapshot binds a statement set."""
    if snapshot.get("statements"):
        return (*VERIFIED_OBLIGATIONS, *STATEMENT_OBLIGATIONS)
    return VERIFIED_OBLIGATIONS


def snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    return canonical_json_sha256(snapshot)


def _item(code: str, detail: str, ref: str | None = None) -> dict[str, Any]:
    return {"code": code, "detail": detail, "ref": ref}


def _result(state: State, items: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    return {"state": state, "items": list(items)}


def _claim_support(inputs: CheckInputs) -> dict[str, Any]:
    release = inputs.draft_release
    if (
        release is not None
        and release.live
        and release.content_hash == inputs.content_hash
    ):
        return _result("pass")
    items = [
        _item(
            str(b.get("code")),
            str(b.get("detail") or ""),
            None if b.get("claim_version_id") is None else str(b["claim_version_id"]),
        )
        for b in inputs.claim_blockers
    ]
    return _result(
        "fail",
        items
        or [_item("no_live_release", "No verified draft release for this content")],
    )


def amended(deviation: DeviationIn, versions: Sequence[VersionIn]) -> bool:
    """An approved amendment: a child of the deviated version that is not an
    initial version and was approved (``superseded`` was approved before)."""
    return any(
        v.parent_version_id == deviation.protocol_version_id
        and v.change_kind != "initial"
        and v.status in AMENDMENT_STATUSES
        for v in versions
    )


def _method_adherence(inputs: CheckInputs) -> dict[str, Any]:
    if inputs.protocol_version_id is None:
        return _result("unknown", [_item("no_protocol", "No approved protocol")])
    open_ids = {d.id for d in inputs.deviations if not amended(d, inputs.versions)}
    items = []
    for run in inputs.runs:
        own = [d for d in inputs.deviations if d.run_id == run.id]
        excused = (
            run.conformance_status == "deviated"
            and bool(own)
            and not any(d.id in open_ids for d in own)
        )
        if run.conformance_status != "conformant" and not excused:
            items.append(_item("run_not_conformant", run.conformance_status, run.id))
    items += [
        _item("deviation_unamended", f"disposition: {d.disposition}", d.id)
        for d in inputs.deviations
        if d.id in open_ids
    ]
    return _result("fail" if items else "pass", items)


def _synthesis_appraisal(inputs: CheckInputs) -> dict[str, Any]:
    if not (inputs.synthesis_required or inputs.appraisal_required):
        return _result("not_applicable")
    items = []
    if inputs.synthesis_required and not inputs.synthesis_current:
        items.append(
            _item("synthesis_not_current", "No current computed synthesis result")
        )
    if inputs.appraisal_required and not inputs.appraisal_complete:
        items.append(_item("appraisal_incomplete", "Appraisals are not complete"))
    return _result("fail" if items else "pass", items)


def _peer_review(inputs: CheckInputs) -> dict[str, Any]:
    if not inputs.has_review_rounds:
        return _result("not_applicable")
    items = [
        _item(
            "open_comment",
            f"{o.get('state')}; anchor {o.get('anchor_state')}",
            str(o.get("comment_root_id")),
        )
        for o in inputs.open_obligations
    ]
    return _result("fail" if items else "pass", items)


def _reproducibility(inputs: CheckInputs) -> dict[str, Any]:
    if not inputs.figures:
        return _result("not_applicable")
    failed = [
        _item(
            "figure_not_reproducible",
            f"{f.completeness}; {f.reproduction}",
            f.figure_key,
        )
        for f in inputs.figures
        if f.completeness != "complete" or f.reproduction == "not_reproduced"
    ]
    if failed:
        return _result("fail", failed)
    pending = [
        _item("reproduction_not_attempted", "complete; not_attempted", f.figure_key)
        for f in inputs.figures
        if f.reproduction != "reproduced"
    ]
    return _result("unknown" if pending else "pass", pending)


def _reporting(inputs: CheckInputs) -> dict[str, Any]:
    if inputs.prisma == "none":
        return _result("not_applicable")
    if inputs.prisma == "inconsistent":
        return _result(
            "fail", [_item("prisma_inconsistent", "PRISMA flow inconsistent")]
        )
    return _result("pass")


def _statements(inputs: CheckInputs) -> dict[str, Any]:
    if not inputs.statements_bound:
        return _result("not_applicable")
    items = [
        _item("author_not_approved", "No approval of the bound statement set", key)
        for key in inputs.unapproved_authors
    ]
    return _result("fail" if items else "pass", items)


def _venue(inputs: CheckInputs) -> dict[str, Any]:
    """Only a check of this exact package hash and the current profile
    version counts; a check of any other package is stale."""
    if not inputs.statements_bound:
        return _result("not_applicable")
    current = [
        v
        for v in inputs.venue_checks
        if v.package_sha256 == inputs.package_sha256
        and v.profile_version == PROFILE_VERSION
    ]
    if inputs.package_sha256 is None or not current:
        stale = inputs.package_sha256 is not None and bool(inputs.venue_checks)
        return _result(
            "unknown",
            [
                _item(
                    "venue_check_stale" if stale else "venue_not_checked",
                    (
                        "No venue check of this exact package"
                        if inputs.package_sha256
                        else "The venue check runs on the packaged candidate"
                    ),
                )
            ],
        )
    latest = current[-1]
    if latest.status == "pass":
        return _result("pass")
    return _result(
        "fail",
        [_item("venue_rule_failed", rule, latest.id) for rule in latest.failing_rules],
    )


def evaluate(inputs: CheckInputs) -> dict[str, dict[str, Any]]:
    """Every result in ``CHECK_KEYS``, each ``{state, items}``."""
    return {
        "reporting_completeness": _reporting(inputs),
        "method_adherence": _method_adherence(inputs),
        "claim_support": _claim_support(inputs),
        "experiment_reproducibility": _reproducibility(inputs),
        "peer_review": _peer_review(inputs),
        "synthesis_appraisal": _synthesis_appraisal(inputs),
        "statements": _statements(inputs),
        "venue": _venue(inputs),
    }


def failing_obligations(
    checks: Mapping[str, Any], obligations: Sequence[str] = VERIFIED_OBLIGATIONS
) -> list[str]:
    """Obligations that are neither ``pass`` nor ``not_applicable`` (a missing
    result fails), in obligation order."""
    return [
        key
        for key in obligations
        if (checks.get(key) or {}).get("state") not in PASSING
    ]


def bib_entries(bibtex: str) -> dict[str, str]:
    """BibTeX text split into ``{key: entry text}``."""
    starts = list(_BIB_ENTRY.finditer(bibtex))
    return {
        m.group(1): bibtex[
            m.start() : (starts[i + 1].start() if i + 1 < len(starts) else len(bibtex))
        ].strip()
        for i, m in enumerate(starts)
    }


def reference_mapping(
    snapshot: Mapping[str, Any], bibtex: str
) -> list[tuple[str, str]]:
    """``docN`` -> sha256 of its ``references.bib`` entry, in snapshot order.
    ``ValueError`` when the keys differ either way."""
    entries = bib_entries(bibtex)
    keys = [str(r["key"]) for r in snapshot.get("references") or []]
    if sorted(keys) != sorted(entries) or len(set(keys)) != len(keys):
        raise ValueError("references.bib keys do not match the snapshot references")
    return [
        (key, hashlib.sha256(entries[key].encode("utf-8")).hexdigest()) for key in keys
    ]

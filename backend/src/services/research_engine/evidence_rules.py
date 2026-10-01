"""Pure evidence-table, contradiction and outcome-certainty rules (GOO-310).

Stdlib only: the decision ledger imports this module for replay, so it must
not import the ledger (or anything with I/O) back.

Three things stay apart here and everywhere downstream: outcome certainty
(``certainty_level``, GRADE's own arithmetic), evidence agreement
(``contradiction_status``, a human chain) and model output
(``stance_groups``, read-only suggestions). Only ``stance_groups`` may name
the classifier's fields; ``test_evidence_boundary`` enforces that.

GRADE is encoded as structure only: the two starting levels, the five
downgrade domains with ratings ``0 | -1 | -2 | None`` and the four levels.
``# ponytail: add large-effect/dose-response upgrades when observational
outcomes are graded.``
"""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal, Mapping, NamedTuple, Sequence
from uuid import UUID

CELL_STATES = ("value", "missingness", "missing", "conflict")
CONTRADICTION_KINDS = ("opened", "resolved", "acknowledged", "dissent")
DECIDING_KINDS = ("resolved", "acknowledged")  # adjudicator only
GRADE_DOMAINS = (
    "risk_of_bias",
    "inconsistency",
    "indirectness",
    "imprecision",
    "publication_bias",
)
LEVELS = ("very_low", "low", "moderate", "high")  # ordered
STARTING_LEVELS = ("high", "low")
RATINGS = (0, -1, -2)
RESOLVED_APPRAISAL = ("agreed", "adjudicated")
EXCLUSION_REASONS = ("study_link_unresolved", "no_report_identity")
MAX_TEXT = 4000

ContradictionStatus = Literal["unresolved", "resolved", "acknowledged"]


@dataclass(frozen=True)
class Tip:
    """One unsuperseded, non-stale GOO-304 accepted value on a unit's document."""

    accepted_value_id: UUID
    document_id: UUID
    report_id: UUID
    unit: str
    field_id: UUID
    source_hash: str
    text_sha256: str | None
    value: Any
    missingness: str | None


class ChainRow(NamedTuple):
    """One contradiction-chain row as status derivation sees it."""

    id: UUID
    kind: str
    actor_id: UUID
    actor_role: str
    explanation: str


def _canonical(value: Any) -> str:
    # Same bytes as contracts.canonical_json_bytes (asserted by the unit test).
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def cell_state(tips: Sequence[Tip]) -> str:
    """``missing`` (no tip) is never a blank value: it has its own state."""
    if not tips:
        return "missing"
    reasons = {tip.missingness for tip in tips}
    if None not in reasons:
        return "missingness" if len(reasons) == 1 else "conflict"
    if reasons != {None}:
        return "conflict"  # some tips report a value, others missingness
    values = {_canonical(tip.value) for tip in tips}
    return "value" if len(values) == 1 else "conflict"


def _tip_json(tip: Tip) -> dict[str, Any]:
    return {
        "accepted_value_id": str(tip.accepted_value_id),
        "document_id": str(tip.document_id),
        "report_id": str(tip.report_id),
        "source_hash": tip.source_hash,
        "text_sha256": tip.text_sha256,
        "value": tip.value,
        "missingness": tip.missingness,
    }


def _cell(tips: Sequence[Tip]) -> dict[str, Any]:
    state = cell_state(tips)
    ordered = sorted(tips, key=lambda t: (str(t.document_id), str(t.accepted_value_id)))
    return {
        "state": state,
        "value": ordered[0].value if state == "value" else None,
        "missingness": ordered[0].missingness if state == "missingness" else None,
        "tips": [_tip_json(tip) for tip in ordered],
    }


def build_rows(
    units: Mapping[str, Sequence[UUID]],
    tips: Sequence[Tip],
    field_ids: Sequence[UUID],
    outcome_key: str,
    timepoint: str,
) -> list[dict[str, Any]]:
    """Exactly one row per analysis unit (``units``: unit -> its report ids),
    so two reports of one study share a row and can never double-count."""
    by_cell: dict[tuple[str, str], list[Tip]] = defaultdict(list)
    for tip in tips:
        by_cell[(tip.unit, str(tip.field_id))].append(tip)
    return [
        {
            "row_key": f"{unit}|{outcome_key}|{timepoint}",
            "unit": unit,
            "report_ids": sorted({str(r) for r in units[unit]}),
            "cells": {
                str(field_id): _cell(by_cell.get((unit, str(field_id)), []))
                for field_id in field_ids
            },
        }
        for unit in sorted(units)
    ]


def table_hash(
    protocol_version_id: Any,
    form_version_id: Any,
    outcome_key: str,
    timepoint: str,
    field_ids: Sequence[Any],
    rows: Sequence[Mapping[str, Any]],
    excluded: Sequence[Mapping[str, Any]],
) -> str:
    body = {
        "protocol_version_id": str(protocol_version_id),
        "form_version_id": str(form_version_id),
        "outcome_key": outcome_key,
        "timepoint": timepoint,
        "field_ids": [str(f) for f in field_ids],  # order is part of the table
        "rows": list(rows),
        "excluded": list(excluded),
    }
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def cell_members(rows: Sequence[Mapping[str, Any]], field_id: Any) -> set[str]:
    """Every accepted-value id cited by the table's cells for one field."""
    return {
        tip["accepted_value_id"]
        for row in rows
        for tip in (row["cells"].get(str(field_id)) or {}).get("tips", [])
    }


def contradiction_status(chain: Sequence[ChainRow]) -> ContradictionStatus:
    """The last non-dissent kind; ``opened`` reads ``unresolved``."""
    if not chain or chain[0].kind != "opened":
        raise ValueError("A contradiction chain starts with its opened row")
    last = [row.kind for row in chain if row.kind != "dissent"][-1]
    return "unresolved" if last == "opened" else last  # type: ignore[return-value]


def dissent(chain: Sequence[ChainRow]) -> list[dict[str, Any]]:
    """Every dissent row, plus each resolution a later one superseded: an
    earlier opinion never disappears when the group is closed."""
    deciding = [row for row in chain if row.kind in DECIDING_KINDS]
    superseded = {row.id for row in deciding[:-1]}
    return [
        {
            "id": str(row.id),
            "kind": row.kind,
            "actor_id": str(row.actor_id),
            "actor_role": row.actor_role,
            "explanation": row.explanation,
            "superseded": row.id in superseded,
        }
        for row in chain
        if row.kind == "dissent" or row.id in superseded
    ]


def certainty_level(start: str, ratings: Mapping[str, int | None]) -> str | None:
    """GRADE's arithmetic over the five downgrade domains, floored at
    ``very_low``; any unknown (``None`` or absent) rating keeps it unknown."""
    values = [ratings.get(domain) for domain in GRADE_DOMAINS]
    if any(value is None for value in values):
        return None
    drop = sum(abs(int(value)) for value in values if value is not None)
    return LEVELS[max(LEVELS.index(start) - drop, 0)]


def check_certainty(
    start: str,
    ratings: Mapping[str, int | None],
    level: str | None,
    rob_cited: Sequence[Any],
    rob_required: Sequence[Any],
    rob_statuses: Mapping[str, str | None],
    contradiction_cited: Sequence[Any],
    contradiction_all: Sequence[Any],
) -> None:
    """Raise ``ValueError`` unless the assessment is well formed: the level is
    the derived one, a risk-of-bias rating rests on resolved appraisals of
    every unit (``rob_statuses``: unit -> status, ``None`` for none) and an
    inconsistency rating cites every contradiction on the table."""
    if start not in STARTING_LEVELS:
        raise ValueError("Unknown starting level")
    if set(ratings) != set(GRADE_DOMAINS):
        raise ValueError("Ratings name exactly the five GRADE domains")
    if any(value is not None and value not in RATINGS for value in ratings.values()):
        raise ValueError("A rating is 0, -1, -2 or unknown")
    if level != certainty_level(start, ratings):
        raise ValueError("Level is not derived from the ratings")
    if ratings["risk_of_bias"] is not None:
        for unit in sorted(rob_statuses):
            if rob_statuses[unit] not in RESOLVED_APPRAISAL:
                raise ValueError(f"Risk of bias unresolved for {unit}")
        if {str(v) for v in rob_cited} != {str(v) for v in rob_required}:
            raise ValueError("Risk of bias must cite the current appraisals")
    if ratings["inconsistency"] is not None and {
        str(v) for v in contradiction_cited
    } != {str(v) for v in contradiction_all}:
        raise ValueError("Inconsistency must cite every contradiction on this table")


def stance_groups(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Model stance rows grouped by claim; only claims with both a supporting
    and an opposing row. Labelled unreviewed: they never set any state."""
    by_claim: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_claim[str(row["claim_hash"])].append(row)
    groups = []
    for claim_hash in sorted(by_claim):
        members = by_claim[claim_hash]
        stances = {str(m["stance"]) for m in members}
        if not {"supporting", "opposing"} <= stances:
            continue
        groups.append(
            {
                "claim_hash": claim_hash,
                "claim_text": members[0].get("claim_text"),
                "review_state": "unreviewed_model_suggestion",
                "suggestions": [
                    {
                        "id": str(m["id"]),
                        "source_id": str(m["source_id"]),
                        "stance": str(m["stance"]),
                        "confidence": m["confidence"],
                        "model_version": m["model_version"],
                        "inference_model_version": m.get("inference_model_version"),
                    }
                    for m in sorted(members, key=lambda m: str(m["id"]))
                ],
            }
        )
    return groups

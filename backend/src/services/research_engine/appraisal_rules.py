"""Pure RoB 2 structure, answer validation and derived appraisal status (GOO-309).

Stdlib plus the pure ``screening_rules`` only: the decision ledger imports
this module, so it must not import the ledger back.

Only the instrument's published *structure* is encoded (RoB 2 is CC BY-NC-ND
4.0): domain ids and short names, signalling-question ids, the response and
judgement vocabularies, and the rule that the overall judgement is at least
the worst domain judgement. No question wording, guidance or domain algorithm
ships, and nothing here derives an answer: a missing answer stays ``None``
(unknown). ``# ponytail: encode the cluster/crossover variants or ROBINS-I
when a protocol needs them.``
"""

import hashlib
import json
from typing import AbstractSet, Any, Mapping, NamedTuple, Sequence
from uuid import UUID

from src.services.research_engine import screening_rules

RESPONSES = ("Y", "PY", "PN", "N", "NI", "NA")
JUDGMENTS = ("low", "some_concerns", "high")  # ordered, least to most concern
DESIGNS = (
    "randomized_parallel_group",
    "randomized_cluster",
    "randomized_crossover",
    "non_randomized_intervention",
    "cohort",
    "case_control",
    "cross_sectional",
    "other",
)
APPLICABILITY = ("applicable", "not_applicable")
EVIDENCE_KINDS = ("accepted_value", "observation")
MAX_EVIDENCE = 20
MAX_RATIONALE = 4000
_DOMAIN_FIELDS = frozenset({"judgment", "signals", "rationale", "evidence"})

SPEC: dict[str, Any] = {
    "key": "rob2",
    "version": "2019-08-22",
    "variant": "individually_randomized_parallel_group/assignment",
    "applies_to": ["randomized_parallel_group"],
    "licence": "CC BY-NC-ND 4.0 (riskofbias.info)",
    "encoding": "structure only; no text or algorithms",
    "source": "https://www.riskofbias.info",
    "domains": {
        "D1": {"name": "Randomization process", "signals": ["1.1", "1.2", "1.3"]},
        "D2": {
            "name": "Deviations from intended interventions",
            "signals": ["2.1", "2.2", "2.3", "2.4", "2.5", "2.6", "2.7"],
        },
        "D3": {"name": "Missing outcome data", "signals": ["3.1", "3.2", "3.3", "3.4"]},
        "D4": {
            "name": "Measurement of the outcome",
            "signals": ["4.1", "4.2", "4.3", "4.4", "4.5"],
        },
        "D5": {
            "name": "Selection of the reported result",
            "signals": ["5.1", "5.2", "5.3"],
        },
    },
}
# Same bytes as contracts.canonical_json_sha256 (asserted by the unit test).
SPEC_HASH = hashlib.sha256(
    json.dumps(SPEC, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
).hexdigest()

Key = tuple[str, str, str]  # (target_key, outcome_key, timepoint)


class Row(NamedTuple):
    """One assessment row as status derivation sees it."""

    id: UUID
    assessor_id: UUID
    study_design: str
    applicability: str
    domains: Mapping[str, Any]
    overall: str | None
    resolves: tuple[UUID, ...] = ()


class Status(NamedTuple):
    value: str  # awaiting_independent | agreed | conflict | adjudicated
    unresolved_domains: list[str]
    current_ids: list[UUID]  # the governing rows; [] while awaiting


def spec(key: str, version: str) -> Mapping[str, Any]:
    if (key, version) != (SPEC["key"], SPEC["version"]):
        raise ValueError("Unknown appraisal instrument")
    return SPEC


def _rank(judgment: str) -> int:
    return JUDGMENTS.index(judgment)


def _evidence(items: Any) -> list[dict[str, str]]:
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > MAX_EVIDENCE:
        raise ValueError(f"Evidence is a list of at most {MAX_EVIDENCE} items")
    out = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {"kind", "id"}:
            raise ValueError("Evidence items are {kind, id}")
        if item["kind"] not in EVIDENCE_KINDS:
            raise ValueError("Unknown evidence kind")
        try:
            out.append({"kind": str(item["kind"]), "id": str(UUID(str(item["id"])))})
        except ValueError as error:
            raise ValueError("Evidence id is not a UUID") from error
    return out


def normalize(domains: Mapping[str, Any]) -> dict[str, Any]:
    """Every domain and signal present; a missing answer is ``None``."""
    known = SPEC["domains"]
    unknown = sorted(set(domains) - set(known))
    if unknown:
        raise ValueError(f"Unknown domain {unknown[0]}")
    out: dict[str, Any] = {}
    for domain_id, definition in known.items():
        given = domains.get(domain_id) or {}
        if not isinstance(given, Mapping) or set(given) - _DOMAIN_FIELDS:
            raise ValueError(f"Unknown domain field in {domain_id}")
        signals = given.get("signals") or {}
        if not isinstance(signals, Mapping):
            raise ValueError(f"Signals of {domain_id} are an object")
        extra = sorted(set(signals) - set(definition["signals"]))
        if extra:
            raise ValueError(f"Unknown signalling question {extra[0]}")
        out[domain_id] = {
            "judgment": given.get("judgment"),
            "signals": {s: signals.get(s) for s in definition["signals"]},
            "rationale": given.get("rationale"),
            "evidence": _evidence(given.get("evidence")),
        }
    return out


def overall_floor(domains: Mapping[str, Any]) -> str | None:
    """The worst domain judgement; ``None`` while any judgement is unknown."""
    judgments = [d.get("judgment") for d in domains.values()]
    if not judgments or any(j is None for j in judgments):
        return None
    return str(max(judgments, key=_rank))


def validate(
    spec: Mapping[str, Any],
    design: str,
    applicability: str,
    domains: Mapping[str, Any],
    overall: str | None,
) -> dict[str, Any]:
    """The normalized domains to store; ``ValueError`` (a 422) otherwise."""
    if design not in DESIGNS:
        raise ValueError("Unknown study design")
    if applicability not in APPLICABILITY:
        raise ValueError("Unknown applicability")
    covered = design in spec["applies_to"]
    if applicability == "not_applicable":
        if covered:
            raise ValueError(f"RoB 2 applies to {design}; assess it")
        if domains or overall is not None:
            raise ValueError("A not-applicable appraisal has empty domains")
        return {}
    if not covered:
        raise ValueError(f"RoB 2 (parallel-group) does not apply to {design}")
    normalized = normalize(domains)
    for domain_id, domain in normalized.items():
        if domain["judgment"] is not None and domain["judgment"] not in JUDGMENTS:
            raise ValueError(f"Unknown judgement in {domain_id}")
        if any(
            v is not None and v not in RESPONSES for v in domain["signals"].values()
        ):
            raise ValueError(f"Unknown response in {domain_id}")
        rationale = domain["rationale"]
        if rationale is not None and (
            not isinstance(rationale, str)
            or not rationale.strip()
            or len(rationale) > MAX_RATIONALE
        ):
            raise ValueError(f"The rationale of {domain_id} is 1-{MAX_RATIONALE} chars")
        if domain["judgment"] is not None and rationale is None:
            raise ValueError(f"A judgement in {domain_id} needs a rationale")
    if overall is not None:
        if overall not in JUDGMENTS:
            raise ValueError("Unknown overall judgement")
        floor = overall_floor(normalized)
        if floor is None:
            raise ValueError("Overall must stay unknown while a domain is unknown")
        if _rank(overall) < _rank(floor):
            raise ValueError("Overall judgement cannot be below the worst domain")
    return normalized


def _judgments(row: Row) -> dict[str, Any]:
    return {d: v.get("judgment") for d, v in row.domains.items()}


def _unknown(row: Row) -> set[str]:
    return {d for d, j in _judgments(row).items() if j is None}


def _independent(rows: Sequence[Row]) -> int:
    return len({row.assessor_id for row in rows})


def status(
    mode: str, independent_tips: Sequence[Row], adjudicated_tip: Row | None
) -> Status:
    """Derived per result on every read; nothing is stamped.

    Only the judgements, overall, design and applicability decide agreement:
    signalling answers may differ without a conflict.
    """
    if _independent(independent_tips) < screening_rules.REQUIRED[mode]:
        return Status("awaiting_independent", [], [])
    tip_ids = sorted((row.id for row in independent_tips), key=str)
    if adjudicated_tip is not None and set(adjudicated_tip.resolves) == set(tip_ids):
        return Status(
            "adjudicated", sorted(_unknown(adjudicated_tip)), [adjudicated_tip.id]
        )
    unresolved = set().union(*(_unknown(row) for row in independent_tips))
    first = independent_tips[0]
    signature = (first.applicability, first.study_design, first.overall)
    differing = {
        d
        for row in independent_tips[1:]
        for d in set(_judgments(row)) | set(_judgments(first))
        if _judgments(row).get(d) != _judgments(first).get(d)
    }
    agreed = mode == "single" or (
        not differing
        and all(
            (row.applicability, row.study_design, row.overall) == signature
            for row in independent_tips
        )
    )
    if agreed:
        return Status("agreed", sorted(unresolved), tip_ids)
    return Status("conflict", sorted(unresolved | differing), tip_ids)


def revealed_keys(mode: str, tips_by_key: Mapping[Key, Sequence[Row]]) -> set[Key]:
    """Keys whose distinct independent tip assessors reach the mode's count."""
    required = screening_rules.REQUIRED[mode]
    return {key for key, rows in tips_by_key.items() if _independent(rows) >= required}


def visible(
    row_assessor: UUID, row_id: UUID, viewer: UUID | None, revealed: AbstractSet[UUID]
) -> bool:
    """GOO-302's predicate; a viewer-less reader (the bundle) sees revealed rows."""
    if viewer is None:
        return row_id in revealed
    return screening_rules.visible(row_assessor, row_id, viewer, revealed)

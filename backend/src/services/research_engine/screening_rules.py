"""Pure screening rules shared by the service and the ledger replay (GOO-301/302).

No database access and only stdlib imports at module level: the decision
ledger imports this module, so it must not import the ledger back.
"""

from typing import AbstractSet, Any, Mapping, NamedTuple, Sequence
from uuid import UUID, uuid5

STAGES = ("title_abstract", "full_text")
DECISIONS = ("include", "exclude", "uncertain")
MODES = ("single", "dual_independent")
# GOO-302: fresh observations from distinct reviewers needed to reveal a report.
REQUIRED = {"single": 1, "dual_independent": 2}  # keys == MODES
BASES = ("single", "agreement", "conflict", "adjudicated", "reopened")
_AUTO_RESOLUTION_NAMESPACE = UUID("5c7e1f0a-3b2d-4e8f-9a6c-2d1b0e3f4a5c")

_MAX_REASONS = 50
_MAX_REASON_LENGTH = 200


def reviewer_mode(snapshot: Mapping[str, Any]) -> str:
    """The protocol's ``reviewer_mode.mode``; ValueError unless it is in MODES."""
    section = snapshot.get("reviewer_mode")
    mode = section.get("mode") if isinstance(section, Mapping) else None
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("Protocol declares no screening reviewer mode")
    return mode


def exclusion_reasons(snapshot: Mapping[str, Any]) -> list[str]:
    """``selection.full_text_exclusion_reasons``; [] if absent, ValueError if malformed."""
    selection = snapshot.get("selection")
    raw = (
        selection.get("full_text_exclusion_reasons")
        if isinstance(selection, Mapping)
        else None
    )
    if raw is None:
        return []
    if (
        not isinstance(raw, list)
        or not 1 <= len(raw) <= _MAX_REASONS
        or len(set(map(str, raw))) != len(raw)
        or not all(
            isinstance(reason, str)
            and reason
            and reason == reason.strip()
            and len(reason) <= _MAX_REASON_LENGTH
            for reason in raw
        )
    ):
        raise ValueError("Protocol full-text exclusion reasons are malformed")
    return list(raw)


def criteria_hash(snapshot: Mapping[str, Any]) -> str:
    """The criterion version: eligibility plus the full-text exclusion reasons."""
    # Lazy: protocol_service imports the ledger, which imports this module.
    from src.services.research_engine.protocol_service import canonical_hash

    return canonical_hash(
        {
            "eligibility": snapshot.get("eligibility"),
            "full_text_exclusion_reasons": exclusion_reasons(snapshot),
        }
    )


def validate_observation(
    stage: str, decision: str, reason: str | None, reasons: Sequence[str]
) -> None:
    """A full-text exclusion needs a protocol reason; nothing else takes one."""
    if stage not in STAGES:
        raise ValueError("Unknown screening stage")
    if decision not in DECISIONS:
        raise ValueError("Unknown screening decision")
    if stage == "full_text" and decision == "exclude":
        if reason is None or reason not in reasons:
            raise ValueError("Full-text exclusion needs a protocol exclusion reason")
    elif reason is not None:
        raise ValueError("Exclusion reason only applies to a full-text exclusion")


def suggestion_rows(step_output: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """``(source_id, decision, reason)`` per source of a screen step's output.

    A source is suggested for inclusion if ANY of its parts was included; the
    reason is the first included part's, else the first part's.
    """
    rows: dict[str, tuple[str, str, str]] = {}
    for item in step_output.get("screening") or []:
        source_id = str(item["source_id"])
        reason = str(item.get("reason") or "")
        if item.get("included") is True:
            if source_id not in rows or rows[source_id][1] == "exclude":
                rows[source_id] = (source_id, "include", reason)
        elif source_id not in rows:
            rows[source_id] = (source_id, "exclude", reason)
    return list(rows.values())


class Obs(NamedTuple):
    """One fresh current observation, as derivation sees it."""

    id: UUID
    reviewer_id: UUID
    decision: str
    exclusion_reason: str | None


class Derived(NamedTuple):
    basis: str
    outcome: str | None
    exclusion_reason: str | None
    input_observation_ids: list[str]  # sorted


def derive(mode: str, fresh: Sequence[Obs]) -> Derived | None:
    """The one producer of an automatic resolution; None until enough reviewers.

    Unanimous ``(decision, exclusion_reason)`` that is not ``uncertain``
    resolves (``single`` / ``agreement``); anything else is a ``conflict`` for
    a human adjudicator.
    """
    if mode not in REQUIRED:
        raise ValueError("Unknown screening reviewer mode")
    if len({obs.reviewer_id for obs in fresh}) < REQUIRED[mode]:
        return None
    inputs = sorted(str(obs.id) for obs in fresh)
    votes = {(obs.decision, obs.exclusion_reason) for obs in fresh}
    if len(votes) == 1:
        decision, reason = next(iter(votes))
        if decision != "uncertain":
            basis = "single" if mode == "single" else "agreement"
            return Derived(basis, decision, reason, inputs)
    return Derived("conflict", None, None, inputs)


def visible(
    observation_reviewer: UUID,
    observation_id: UUID,
    viewer: UUID,
    revealed: AbstractSet[UUID],
) -> bool:
    """The reveal predicate: own observations, or inputs of any resolution."""
    return observation_reviewer == viewer or observation_id in revealed


def auto_resolution_id(event_id: UUID) -> UUID:
    """An automatic resolution's id, fixed by the event that triggered it.

    Auto resolutions get no ledger event of their own, so a deterministic id
    lets replay name the exact conflict tip an adjudication or reopen cites.
    """
    return uuid5(_AUTO_RESOLUTION_NAMESPACE, str(event_id))

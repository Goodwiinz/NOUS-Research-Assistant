"""Pure screening rules shared by the service and the ledger replay (GOO-301).

No database access and only stdlib imports at module level: the decision
ledger imports this module, so it must not import the ledger back.
"""

from typing import Any, Mapping, Sequence

STAGES = ("title_abstract", "full_text")
DECISIONS = ("include", "exclude", "uncertain")
MODES = ("single", "dual_independent")

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

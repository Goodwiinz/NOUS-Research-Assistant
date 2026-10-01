"""Pure parsers for an approved protocol snapshot's methods sections (GOO-309).

Stdlib plus the pure ``screening_rules`` only. Each parser raises
``ValueError``; the calling service turns that into a 409. GOO-310 adds
``certainty_method`` and GOO-311 ``synthesis_selection`` here.
"""

from typing import Any, Mapping

from src.services.research_engine import screening_rules

NO_APPRAISAL = "Protocol declares no appraisal instrument"
NO_OUTCOMES = "Protocol declares no outcomes"
_MAX_TEXT = 100


def _text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_TEXT


def appraisal_method(snapshot: Mapping[str, Any]) -> tuple[str, str, str]:
    """``appraisal_synthesis.appraisal`` as ``(instrument, version, mode)``."""
    section = snapshot.get("appraisal_synthesis")
    method = section.get("appraisal") if isinstance(section, Mapping) else None
    if not isinstance(method, Mapping):
        raise ValueError(NO_APPRAISAL)
    instrument, version, mode = (
        method.get(k) for k in ("instrument", "version", "mode")
    )
    if not (_text(instrument) and _text(version)) or mode not in screening_rules.MODES:
        raise ValueError(NO_APPRAISAL)
    return str(instrument), str(version), str(mode)


def declared_outcomes(snapshot: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """``outcomes.declared`` as ``{key: timepoints}``; keys and timepoints unique."""
    section = snapshot.get("outcomes")
    declared = section.get("declared") if isinstance(section, Mapping) else None
    if not isinstance(declared, list) or not declared:
        raise ValueError(NO_OUTCOMES)
    outcomes: dict[str, tuple[str, ...]] = {}
    for item in declared:
        key = item.get("key") if isinstance(item, Mapping) else None
        timepoints = item.get("timepoints") if isinstance(item, Mapping) else None
        if (
            not _text(key)
            or key in outcomes
            or not isinstance(timepoints, list)
            or not timepoints
            or not all(_text(t) for t in timepoints)
            or len(set(timepoints)) != len(timepoints)
        ):
            raise ValueError(NO_OUTCOMES)
        outcomes[str(key)] = tuple(str(t) for t in timepoints)
    return outcomes


NO_CERTAINTY = "Protocol declares no certainty method"
_CERTAINTY_METHODS = {"grade": ("handbook-2013",)}


def certainty_method(snapshot: Mapping[str, Any]) -> tuple[str, str]:
    """``appraisal_synthesis.certainty`` as ``(method, version)`` (GOO-310);
    only structure-encoded methods are accepted."""
    section = snapshot.get("appraisal_synthesis")
    method = section.get("certainty") if isinstance(section, Mapping) else None
    if not isinstance(method, Mapping):
        raise ValueError(NO_CERTAINTY)
    key, version = method.get("method"), method.get("version")
    if version not in _CERTAINTY_METHODS.get(str(key), ()):
        raise ValueError(NO_CERTAINTY)
    return str(key), str(version)

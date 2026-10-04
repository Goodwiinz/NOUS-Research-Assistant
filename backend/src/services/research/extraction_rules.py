"""Pure extraction-form rules (GOO-304): no database, no FastAPI.

A form version is an ordered list of typed fields. A field's id is derived
from ``(matrix_id, name)``, so the same name keeps its id in every version and
never collides across matrices. Observations carry exactly one of a typed
value or a missingness reason; an accepted value must equal one cited valid
observation (or declare ``unresolved_disagreement`` over two that differ).
Staleness is derived here and never stored. Callers turn ``ValueError`` into
a 422.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid5

from src.services.research_engine.contracts import canonical_json_sha256

# Frozen: alembic a3c5e7f9b1d4 copies this literal. Changing it re-keys every field.
FIELD_NAMESPACE = UUID("ffb3dc50-fe01-4981-93f4-b1d1f49683c5")
TYPES = ("text", "number", "boolean", "categorical")
PROVENANCES = ("legacy_unversioned", "authored")
MISSINGNESS_VALUES = (
    "not_reported",
    "not_applicable",
    "unavailable_text",
    "extraction_error",
    "unresolved_disagreement",
)
_REPORTED = frozenset({"not_reported", "not_applicable", "unavailable_text"})
MISSINGNESS: dict[str, frozenset[str]] = {
    "machine": _REPORTED | {"extraction_error"},
    "human": _REPORTED,
    "accepted": _REPORTED | {"unresolved_disagreement"},
}
DEF_KEYS = ("type", "unit", "timepoint", "categories")
MAX_CATEGORIES = 50
_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_TRUE = frozenset({"true", "yes", "y"})
_FALSE = frozenset({"false", "no", "n"})


@dataclass(frozen=True)
class Obs:
    """An observation as the rules see it (machine or human)."""

    id: UUID
    kind: str
    value: Any
    missingness: str | None
    validation_state: str
    form_version_id: UUID
    citation: str | None
    source_hash: str
    created_at: datetime


@dataclass(frozen=True)
class Accepted:
    """An accepted chain tip plus its field definition in its own version."""

    id: UUID
    value: Any
    missingness: str | None
    form_version_id: UUID
    source_hash: str
    field_def: dict[str, Any] | None


@dataclass(frozen=True)
class LegacyCell:
    """A frozen ``extraction_cells`` row (pre-GOO-304)."""

    value: str | None
    citation_snippet: str | None
    confidence: float | None
    form_version_id: UUID | None


@dataclass(frozen=True)
class CellView:
    value: str | None
    citation_snippet: str | None
    confidence: float | None
    form_version_id: UUID | None
    source: str  # accepted | machine | legacy
    missingness: str | None
    validation_state: str | None
    stale: bool


def field_id(matrix_id: UUID, name: str) -> UUID:
    return uuid5(FIELD_NAMESPACE, f"{matrix_id}:{name}")


def _optional_str(column: Mapping[str, Any], key: str, limit: int) -> str | None:
    value = column.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"Field {key} must be a string of at most {limit} characters")
    return value


def build_fields(
    matrix_id: UUID, columns: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Column definitions -> ordered, typed version fields with stable ids."""
    fields: list[dict[str, Any]] = []
    names: set[str] = set()
    for column in columns:
        name = column["name"]
        if name in names:
            raise ValueError("Field names must be unique")
        names.add(name)
        kind = column.get("type") or "text"
        if kind not in TYPES:
            raise ValueError(f"Field type must be one of {', '.join(TYPES)}")
        categories = column.get("categories")
        if (kind == "categorical") != (categories is not None):
            raise ValueError("categories are required for, and only for, categorical")
        if categories is not None and (
            not isinstance(categories, list)
            or not 1 <= len(categories) <= MAX_CATEGORIES
            or not all(isinstance(c, str) and c.strip() for c in categories)
            or len({c.casefold() for c in categories}) != len(categories)
        ):
            raise ValueError(
                f"categories must be 1-{MAX_CATEGORIES} distinct non-empty strings"
            )
        fields.append(
            {
                "field_id": str(field_id(matrix_id, name)),
                "name": name,
                "description": column.get("description"),
                "type": kind,
                "unit": _optional_str(column, "unit", 50),
                "timepoint": _optional_str(column, "timepoint", 100),
                "categories": categories,
            }
        )
    return fields


def form_hash(
    provenance: str, protocol_version_id: UUID | None, fields: list[dict[str, Any]]
) -> str:
    return canonical_json_sha256(
        {
            "provenance": provenance,
            "protocol_version_id": (
                None if protocol_version_id is None else str(protocol_version_id)
            ),
            "fields": fields,
        }
    )


def find_field(
    fields: Sequence[Mapping[str, Any]], fid: UUID | str
) -> Mapping[str, Any] | None:
    return next((f for f in fields if f["field_id"] == str(fid)), None)


def field_def(
    fields: Sequence[Mapping[str, Any]], fid: UUID | str
) -> dict[str, Any] | None:
    """The part of a field whose change makes accepted values stale."""
    field = find_field(fields, fid)
    return None if field is None else {key: field.get(key) for key in DEF_KEYS}


def _raw(raw: Any) -> Any:
    return raw if isinstance(raw, str) else json.dumps(raw)


def coerce(field: Mapping[str, Any], raw: Any) -> tuple[Any, bool]:
    """(typed value, True) or (the raw value kept as a string, False)."""
    kind = field.get("type") or "text"
    if kind == "number":
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            number: float | int = raw
        elif isinstance(raw, str) and _NUMBER_RE.fullmatch(raw.strip()):
            number = float(raw.strip())
        else:
            return _raw(raw), False
        if not math.isfinite(number):
            return _raw(raw), False
        return (int(number) if float(number).is_integer() else number), True
    if kind == "boolean":
        if isinstance(raw, bool):
            return raw, True
        if isinstance(raw, str) and raw.strip().casefold() in _TRUE | _FALSE:
            return raw.strip().casefold() in _TRUE, True
        return _raw(raw), False
    if kind == "categorical":
        if isinstance(raw, str):
            folded = raw.strip().casefold()
            for category in field.get("categories") or ():
                if category.casefold() == folded:
                    return category, True
        return _raw(raw), False
    if isinstance(raw, str) and raw.strip():
        return raw, True
    return _raw(raw), False


def check_missingness(writer: str, missingness: str) -> None:
    if missingness not in MISSINGNESS[writer]:
        raise ValueError(f"Missingness {missingness!r} is not allowed here")


def normalize(
    writer: str, field: Mapping[str, Any], value: Any, missingness: str | None
) -> tuple[Any, str | None, str]:
    """-> (stored value, missingness, validation_state) for one writer."""
    if (value is None) == (missingness is None):
        raise ValueError("Provide exactly one of value or missingness")
    if missingness is not None:
        check_missingness(writer, missingness)
        return None, missingness, "valid"
    typed, valid = coerce(field, value)
    if not valid and writer != "machine":
        raise ValueError("Value does not match the field type")
    return typed, None, "valid" if valid else "invalid"


def display(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def is_stale(
    accepted_def: Mapping[str, Any] | None,
    current_def: Mapping[str, Any] | None,
    accepted_hash: str,
    current_hash: str,
) -> bool:
    return (
        current_def is None
        or accepted_def != current_def
        or accepted_hash != current_hash
    )


def pick_cell(
    accepted: Sequence[Accepted],
    machine: Sequence[Obs],
    legacy: LegacyCell | None,
    current_def: Mapping[str, Any] | None,
    current_hash: str,
) -> CellView | None:
    """Accepted tip (non-stale first) -> latest machine -> legacy cell.

    ``machine`` is in creation order.
    """
    if accepted:
        ranked = [
            (is_stale(a.field_def, current_def, a.source_hash, current_hash), a)
            for a in accepted
        ]
        stale, tip = min(ranked, key=lambda pair: pair[0])
        return CellView(
            display(tip.value),
            None,
            None,
            tip.form_version_id,
            "accepted",
            tip.missingness,
            "valid",
            stale,
        )
    if machine:
        obs = machine[-1]
        return CellView(
            display(obs.value),
            obs.citation,
            None,
            obs.form_version_id,
            "machine",
            obs.missingness,
            obs.validation_state,
            False,
        )
    if legacy is not None:
        return CellView(
            legacy.value,
            legacy.citation_snippet,
            legacy.confidence,
            legacy.form_version_id,
            "legacy",
            None,
            None,
            False,
        )
    return None


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def check_acceptance(value: Any, missingness: str | None, cited: Sequence[Obs]) -> None:
    """An adjudicator reconciles evidence; it never authors a new value."""
    if not cited:
        raise ValueError("An accepted value must cite observations")
    if (value is None) == (missingness is None):
        raise ValueError("Provide exactly one of value or missingness")
    if missingness is not None:
        check_missingness("accepted", missingness)
    if missingness == "unresolved_disagreement":
        if len(cited) < 2:
            raise ValueError("Unresolved disagreement must cite at least two")
        if len({json.dumps([o.value, o.missingness]) for o in cited}) < 2:
            raise ValueError("Unresolved disagreement needs cited values that differ")
        return
    if not any(
        o.validation_state == "valid"
        and o.missingness == missingness
        and _same(o.value, value)
        for o in cited
    ):
        raise ValueError("Accepted value must equal one valid cited observation")

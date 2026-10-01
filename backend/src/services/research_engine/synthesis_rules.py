"""Deterministic SMD (Hedges' g) pooled by DerSimonian-Laird (GOO-311).

Stdlib only (``test_synthesis_boundary`` enforces it): the decision ledger
imports this module for replay. Inputs are GOO-310 evidence-table rows, one
per analysis unit, so two reports of one study can never become independent
weights; ``pool_dl`` asserts unique unit keys a second time.

Formulas (Borenstein et al., *Introduction to Meta-Analysis*, 2009, ch. 4):
``df = n1 + n2 - 2``, ``s_p = sqrt(((n1-1)s1^2 + (n2-1)s2^2)/df)``,
``d = (m1 - m2)/s_p``, ``J = 1 - 3/(4df - 1)``, ``g = J d``,
``Var(g) = J^2 ((n1+n2)/(n1 n2) + d^2/(2(n1+n2)))``; then inverse-variance
random effects with ``tau2 = max(0, (Q - df)/C)``. The sign is intervention
minus control and is never flipped. Sums use ``math.fsum``, so the result
does not depend on row order.

``# ponytail: DL under-covers with few studies; add HKSJ or REML as a new
ESTIMATOR_VERSION, never by editing this one. Prediction intervals, subgroup
or sensitivity analyses, change-from-baseline inputs and SD imputation are
out of scope.``
"""

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

MEASURE = "smd_hedges_g"
MODEL = "random_effects_dl"
ESTIMATOR_VERSION = "nous.smd-hedges-g.dl/1"
DIRECTION = "intervention_minus_control"
Z_975 = 1.959963984540054
ROLES = ("mean_i", "sd_i", "n_i", "mean_c", "sd_c", "n_c")
_UNIT_ROLES = ("mean_i", "sd_i", "mean_c", "sd_c")
RUN_REASONS = (
    "unit_mismatch",
    "timepoint_mismatch",
    "role_not_in_table",
    "wrong_field_type",
    "insufficient_studies",
)
STATUSES = ("computed", "validation_failed")


@dataclass(frozen=True)
class Arm:
    mean: float
    sd: float
    n: int


@dataclass(frozen=True)
class UnitInput:
    unit: str
    report_ids: tuple[str, ...]
    accepted_value_ids: tuple[str, ...]
    i: Arm
    c: Arm


@dataclass(frozen=True)
class Exclusion:
    """``unit is None`` marks a run-level failure."""

    unit: str | None
    reason: str
    detail: str
    report_ids: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "report_ids": list(self.report_ids),
            "reason": self.reason,
            "detail": self.detail,
        }


def _canonical(value: Any) -> str:
    # Same bytes as contracts.canonical_json_bytes (asserted by the unit test).
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def config(roles: Mapping[str, str]) -> dict[str, Any]:
    """The full numerical configuration; its hash enters ``input_hash``."""
    return {
        "measure": MEASURE,
        "model": MODEL,
        "direction": DIRECTION,
        "ci_level": 0.95,
        "z": Z_975,
        "roles": {role: str(roles[role]) for role in ROLES if role in roles},
        "j": "1-3/(4*df-1)",
        "tau2_floor": 0.0,
        "i2_when_q_zero": 0.0,
    }


def config_hash(cfg: Mapping[str, Any]) -> str:
    return _sha256(dict(cfg))


def check_config(
    fields_by_id: Mapping[str, Mapping[str, Any]],
    roles: Mapping[str, str],
    timepoint: str,
) -> list[Exclusion]:
    """Run-level failures: every role maps to a numeric table field at the
    selected timepoint, and means and SDs share one non-null unit."""
    failures: list[Exclusion] = []
    present: dict[str, Mapping[str, Any]] = {}
    for role in ROLES:
        field = fields_by_id.get(str(roles.get(role)))
        if field is None:
            failures.append(Exclusion(None, "role_not_in_table", role))
            continue
        present[role] = field
        if field.get("type") != "number":
            failures.append(Exclusion(None, "wrong_field_type", role))
        if field.get("timepoint") != timepoint:
            failures.append(Exclusion(None, "timepoint_mismatch", role))
    units = {present[r].get("unit") for r in _UNIT_ROLES if r in present}
    if None in units or len(units) > 1:
        failures.append(
            Exclusion(None, "unit_mismatch", ",".join(sorted(str(u) for u in units)))
        )
    return failures


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _unit_exclusion(
    row: Mapping[str, Any], roles: Mapping[str, str]
) -> tuple[Exclusion | None, dict[str, float]]:
    unit, reports = str(row["unit"]), tuple(row.get("report_ids") or ())
    values: dict[str, float] = {}
    for role in ROLES:
        cell = (row.get("cells") or {}).get(str(roles[role])) or {"state": "missing"}
        state = cell.get("state")
        if state in ("missing", "missingness"):
            return Exclusion(unit, f"missing_input:{role}", state, reports), {}
        if state == "conflict":
            return Exclusion(unit, f"conflicting_reports:{role}", state, reports), {}
        number = _number(cell.get("value"))
        if number is None:
            return Exclusion(unit, f"non_numeric:{role}", "value", reports), {}
        values[role] = number
    for arm in ("i", "c"):
        n = values[f"n_{arm}"]
        if not n.is_integer() or n < 2:
            return Exclusion(unit, f"invalid_sample_size:{arm}", repr(n), reports), {}
        sd = values[f"sd_{arm}"]
        if sd <= 0:
            return Exclusion(unit, f"invalid_variance:{arm}", repr(sd), reports), {}
    return None, values


def _accepted(row: Mapping[str, Any], roles: Mapping[str, str]) -> tuple[str, ...]:
    cells = row.get("cells") or {}
    return tuple(
        sorted(
            {
                str(tip["accepted_value_id"])
                for role in ROLES
                for tip in (cells.get(str(roles[role])) or {}).get("tips", [])
            }
        )
    )


def select_inputs(
    rows: Sequence[Mapping[str, Any]],
    roles: Mapping[str, str],
    table_excluded: Sequence[Mapping[str, Any]],
) -> tuple[list[UnitInput], list[Exclusion]]:
    """One input per table row (never per report), plus a structured
    exclusion for each unusable unit and each GOO-310 table exclusion."""
    included: list[UnitInput] = []
    excluded = [
        Exclusion(
            None if item.get("unit") is None else str(item["unit"]),
            str(item.get("reason")),
            str(item.get("document_id") or ""),
            tuple(str(r) for r in [item.get("report_id")] if r is not None),
        )
        for item in table_excluded
    ]
    for row in sorted(rows, key=lambda r: str(r["unit"])):
        exclusion, v = _unit_exclusion(row, roles)
        if exclusion is not None:
            excluded.append(exclusion)
            continue
        included.append(
            UnitInput(
                unit=str(row["unit"]),
                report_ids=tuple(sorted(str(r) for r in row.get("report_ids") or ())),
                accepted_value_ids=_accepted(row, roles),
                i=Arm(v["mean_i"], v["sd_i"], int(v["n_i"])),
                c=Arm(v["mean_c"], v["sd_c"], int(v["n_c"])),
            )
        )
    return included, sort_exclusions(excluded)


def sort_exclusions(excluded: Sequence[Exclusion]) -> list[Exclusion]:
    return sorted(excluded, key=lambda e: (e.unit or "", e.reason, e.detail))


def hedges_g(i: Arm, c: Arm) -> tuple[float, float]:
    """``(g, Var(g))`` for intervention minus control."""
    df = i.n + c.n - 2
    sp = math.sqrt(((i.n - 1) * i.sd**2 + (c.n - 1) * c.sd**2) / df)
    d = (i.mean - c.mean) / sp
    j = 1 - 3 / (4 * df - 1)
    v = j**2 * ((i.n + c.n) / (i.n * c.n) + d**2 / (2 * (i.n + c.n)))
    return j * d, v


def pool_dl(effects: Sequence[tuple[str, float, float]]) -> dict[str, Any]:
    """Inverse-variance random effects with DerSimonian-Laird tau2 over
    ``(unit, g, v)``; returns ``numbers`` and per-unit ``weights``."""
    units = [unit for unit, _, _ in effects]
    if len(set(units)) != len(units):
        raise ValueError("One weight per analysis unit")
    if len(units) < 2:
        raise ValueError("insufficient_studies")
    ordered = sorted(effects)
    g = [e[1] for e in ordered]
    v = [e[2] for e in ordered]
    w = [1 / x for x in v]
    sw = math.fsum(w)
    mu_f = math.fsum(wi * gi for wi, gi in zip(w, g)) / sw
    q = math.fsum(wi * (gi - mu_f) ** 2 for wi, gi in zip(w, g))
    df = len(g) - 1
    c = sw - math.fsum(wi**2 for wi in w) / sw
    tau2 = max(0.0, (q - df) / c)
    i2 = 0.0 if q == 0 else max(0.0, (q - df) / q)
    ws = [1 / (x + tau2) for x in v]
    sws = math.fsum(ws)
    mu = math.fsum(wi * gi for wi, gi in zip(ws, g)) / sws
    se = math.sqrt(1 / sws)
    return {
        "numbers": {
            "estimate": mu,
            "se": se,
            "ci_low": mu - Z_975 * se,
            "ci_high": mu + Z_975 * se,
            "q": q,
            "df": df,
            "i2": i2,
            "tau2": tau2,
        },
        "weights": {
            unit: {"w_fixed": wf, "w_random": wr}
            for (unit, _, _), wf, wr in zip(ordered, w, ws)
        },
    }


def _included_json(item: UnitInput) -> dict[str, Any]:
    return {
        "unit": item.unit,
        "report_ids": list(item.report_ids),
        "accepted_value_ids": list(item.accepted_value_ids),
        "inputs": {
            "mean_i": item.i.mean,
            "sd_i": item.i.sd,
            "n_i": item.i.n,
            "mean_c": item.c.mean,
            "sd_c": item.c.sd,
            "n_c": item.c.n,
        },
    }


def compute(
    included: Sequence[UnitInput],
    excluded: Sequence[Exclusion],
    run_failures: Sequence[Exclusion],
) -> dict[str, Any]:
    """``status``, ``included`` (with g, v and weights when computed),
    ``excluded`` (run-level failures carry ``unit: null``) and ``numbers``
    (``None`` unless computed)."""
    failures = list(run_failures)
    if not failures and len(included) < 2:
        failures.append(Exclusion(None, "insufficient_studies", str(len(included))))
    rows = [_included_json(item) for item in included]
    numbers: dict[str, Any] | None = None
    if not failures:
        effects = [(item.unit, *hedges_g(item.i, item.c)) for item in included]
        pooled = pool_dl(effects)
        numbers = pooled["numbers"]
        for row, (_, g, v) in zip(rows, effects):
            row.update({"g": g, "v": v, **pooled["weights"][row["unit"]]})
    return {
        "status": "validation_failed" if failures else "computed",
        "included": rows,
        "excluded": [e.as_json() for e in sort_exclusions([*failures, *excluded])],
        "numbers": numbers,
    }


def input_hash(table_hash: str, cfg_hash: str, protocol_version_id: Any) -> str:
    return _sha256(
        {
            "table_content_hash": table_hash,
            "config_hash": cfg_hash,
            "estimator_version": ESTIMATOR_VERSION,
            "protocol_version_id": str(protocol_version_id),
        }
    )


def result_hash(
    included: Sequence[Mapping[str, Any]],
    excluded: Sequence[Mapping[str, Any]],
    numbers: Mapping[str, Any] | None,
) -> str:
    return _sha256(
        {
            "included": list(included),
            "excluded": list(excluded),
            "numbers": None if numbers is None else dict(numbers),
        }
    )

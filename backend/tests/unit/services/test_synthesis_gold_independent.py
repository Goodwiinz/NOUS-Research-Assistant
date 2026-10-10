"""SMD/DL gold fixtures re-derived by an independent implementation (GOO-311).

``smd_dl_gold_v1.json`` records the expected values (10 decimals) and the
numpy implementation's own output (12 decimals). This test recomputes with
``tests/fixtures/synthesis/independent_numpy.py`` (vectorised numpy, no
``src`` import) and checks both records within the fixture's declared
tolerance, so the gold values are not self-referential. It stays
``expert_reviewed: false`` until a named methods expert signs it.
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/synthesis"
NUMBERS = ("q", "tau2", "i2", "estimate", "se", "ci_low", "ci_high")


def _gold() -> dict[str, Any]:
    raw = (FIXTURES / "smd_dl_gold_v1.json").read_text(encoding="utf-8")
    return cast(dict[str, Any], json.loads(raw))


def _independent() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "independent_numpy", FIXTURES / "independent_numpy.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_header_declares_tolerance_before_any_comparison() -> None:
    gold = _gold()
    assert gold["tolerance"] == {"abs": 1e-8}
    assert (gold["measure"], gold["model"]) == ("smd_hedges_g", "random_effects_dl")
    assert gold["expert_reviewed"] is (gold["reviewer"] is not None)
    assert "import src" not in (FIXTURES / "independent_numpy.py").read_text()
    assert "from src" not in (FIXTURES / "independent_numpy.py").read_text()


@pytest.mark.parametrize("case_id", ["heterogeneous", "homogeneous"])
def test_numpy_reproduces_recorded_gold(case_id: str) -> None:
    gold = _gold()
    tol = gold["tolerance"]["abs"]
    case = next(c for c in gold["cases"] if c["id"] == case_id)
    out = _independent().compute([s["arms"] for s in case["studies"]])
    for record in (case["expected"], case["independent"]):
        assert out["df"] == record["df"]
        for key in NUMBERS:
            assert out[key] == pytest.approx(record[key], abs=tol), key
        for key in ("g", "v"):
            assert out[key] == pytest.approx(record[key], abs=tol), key

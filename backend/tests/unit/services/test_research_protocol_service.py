"""Focused canonical protocol content tests."""

import math

import pytest

from src.services.research_engine.protocol_service import canonical_hash


def test_canonical_hash_normalizes_jsonb_equivalent_numbers() -> None:
    assert canonical_hash({"one": 1.0, "zero": -0.0}) == canonical_hash(
        {"zero": 0, "one": 1}
    )


def test_canonical_hash_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError, match="finite numbers"):
        canonical_hash({"invalid": math.nan})

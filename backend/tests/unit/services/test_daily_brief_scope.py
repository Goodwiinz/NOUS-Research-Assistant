"""Tests for canonical Daily Research Brief scope confirmation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from importlib import import_module
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest

from src.schemas import research_engine as research_schemas


def _scope_module() -> ModuleType:
    return import_module("src.services.research_engine.scope")


def _defaults() -> dict[str, object]:
    return {
        "contract_version": 1,
        "research_question": "",
        "inclusion_criteria": [],
        "exclusion_criteria": [],
        "providers": ["openalex", "crossref"],
        "limit_per_provider": 25,
        "notes": "",
    }


def _submitted(**overrides: Any) -> research_schemas.DailyBriefScopeConfirmation:
    values = {
        "research_question": "What changed in grounded generation?",
        "inclusion_criteria": ["Peer-reviewed empirical work"],
        "exclusion_criteria": ["Editorials"],
        "providers": ["openalex", "crossref"],
        "limit_per_provider": 20,
        "notes": "Focus on reproducible evaluations.",
        "confirmed": True,
    }
    values.update(overrides)
    return research_schemas.DailyBriefScopeConfirmation.model_validate(values)


def test_effective_parameters_merge_only_declared_scope_overrides() -> None:
    scope = _scope_module()
    defaults = _defaults()

    effective = scope.resolve_effective_daily_brief_parameters(
        defaults,
        {
            "research_question": "What changed in grounded generation?",
            "inclusion_criteria": ["Peer-reviewed empirical work"],
            "exclusion_criteria": ["Editorials"],
            "providers": ["pubmed"],
            "limit_per_provider": 10,
            "notes": "Last 24 hours.",
        },
    )

    assert effective == {
        "contract_version": 1,
        "research_question": "What changed in grounded generation?",
        "inclusion_criteria": ["Peer-reviewed empirical work"],
        "exclusion_criteria": ["Editorials"],
        "providers": ["pubmed"],
        "limit_per_provider": 10,
        "notes": "Last 24 hours.",
    }
    assert defaults["research_question"] == ""


@pytest.mark.parametrize(
    "overrides",
    [
        {"unexpected": True},
        {"contract_version": 2},
        {"providers": ["web"]},
        {"providers": ["rag_store"]},
        {"providers": ["openalex", "openalex"]},
        {"limit_per_provider": 51},
    ],
)
def test_effective_parameters_reject_undeclared_or_unsafe_overrides(
    overrides: dict[str, object],
) -> None:
    scope = _scope_module()
    with pytest.raises(ValueError):
        scope.resolve_effective_daily_brief_parameters(_defaults(), overrides)


def test_scope_confirmation_persists_server_hash_actor_and_time() -> None:
    scope = _scope_module()
    actor_id = uuid4()
    confirmed_at = datetime(2026, 9, 27, 12, 30, tzinfo=timezone.utc)
    effective = scope.resolve_effective_daily_brief_parameters(
        _defaults(),
        {
            "research_question": "What changed in grounded generation?",
            "inclusion_criteria": ["Peer-reviewed empirical work"],
            "exclusion_criteria": ["Editorials"],
            "providers": ["openalex", "crossref"],
            "limit_per_provider": 20,
            "notes": "Focus on reproducible evaluations.",
        },
    )

    confirmation = scope.canonicalize_scope_confirmation(
        effective=effective,
        submitted=_submitted(),
        actor_id=actor_id,
        confirmed_at=confirmed_at,
    )

    canonical_configuration = (
        '{"contract_version":1,"exclusion_criteria":["Editorials"],'
        '"inclusion_criteria":["Peer-reviewed empirical work"],'
        '"limit_per_provider":20,"notes":"Focus on reproducible evaluations.",'
        '"providers":["openalex","crossref"],'
        '"research_question":"What changed in grounded generation?"}'
    )
    expected_hash = hashlib.sha256(canonical_configuration.encode("utf-8")).hexdigest()
    assert confirmation == {
        **effective,
        "confirmed": True,
        "confirmed_by": str(actor_id),
        "confirmed_at": "2026-09-27T12:30:00+00:00",
        "configuration_hash": expected_hash,
    }
    assert json.dumps(confirmation, allow_nan=False)


def test_scope_confirmation_rejects_values_that_do_not_match_effective_scope() -> None:
    scope = _scope_module()
    effective = scope.resolve_effective_daily_brief_parameters(
        _defaults(),
        {
            "research_question": "What changed in grounded generation?",
            "inclusion_criteria": ["Peer-reviewed empirical work"],
            "exclusion_criteria": ["Editorials"],
            "providers": ["openalex", "crossref"],
            "limit_per_provider": 20,
            "notes": "Focus on reproducible evaluations.",
        },
    )

    with pytest.raises(ValueError, match="effective"):
        scope.canonicalize_scope_confirmation(
            effective=effective,
            submitted=_submitted(limit_per_provider=21),
            actor_id=uuid4(),
            confirmed_at=datetime.now(timezone.utc),
        )

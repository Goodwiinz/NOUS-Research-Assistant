"""Trusted Daily Research Brief scope resolution and confirmation."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Mapping
from uuid import UUID

from src.schemas.research_engine import DailyBriefScopeConfirmation
from src.services.research_engine.contracts import canonical_json_sha256

DAILY_BRIEF_SCOPE_FIELDS = (
    "research_question",
    "inclusion_criteria",
    "exclusion_criteria",
    "providers",
    "limit_per_provider",
    "notes",
)


def resolve_effective_daily_brief_parameters(
    defaults: Mapping[str, object],
    overrides: Mapping[str, object],
) -> dict[str, object]:
    """Merge declared user scope values into trusted template defaults."""
    unknown = set(overrides) - set(DAILY_BRIEF_SCOPE_FIELDS)
    if unknown:
        raise ValueError("Daily Brief overrides contain undeclared parameters")
    if defaults.get("contract_version") != 1:
        raise ValueError("Daily Brief contract version must be 1")

    effective = {
        "contract_version": 1,
        **{
            field: deepcopy(overrides.get(field, defaults.get(field)))
            for field in DAILY_BRIEF_SCOPE_FIELDS
        },
    }
    validated = DailyBriefScopeConfirmation.model_validate(
        {
            **{field: effective[field] for field in DAILY_BRIEF_SCOPE_FIELDS},
            "confirmed": True,
        }
    )
    return {
        "contract_version": 1,
        **validated.model_dump(exclude={"confirmed"}),
    }


def canonicalize_scope_confirmation(
    *,
    effective: Mapping[str, object],
    submitted: DailyBriefScopeConfirmation,
    actor_id: UUID,
    confirmed_at: datetime,
) -> dict[str, object]:
    """Verify a confirmation and add server-owned audit/hash metadata."""
    if effective.get("contract_version") != 1:
        raise ValueError("Daily Brief contract version must be 1")
    if confirmed_at.tzinfo is None or confirmed_at.utcoffset() is None:
        raise ValueError("confirmed_at must include a timezone")

    effective_scope = {
        field: deepcopy(effective.get(field)) for field in DAILY_BRIEF_SCOPE_FIELDS
    }
    submitted_scope = submitted.model_dump(exclude={"confirmed"})
    if submitted_scope != effective_scope:
        raise ValueError("scope confirmation does not match effective parameters")

    configuration = {
        "contract_version": 1,
        **effective_scope,
    }
    return {
        **configuration,
        "confirmed": True,
        "confirmed_by": str(actor_id),
        "confirmed_at": confirmed_at.isoformat(),
        "configuration_hash": canonical_json_sha256(configuration),
    }

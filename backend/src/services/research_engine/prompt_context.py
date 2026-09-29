"""Bounded prompt inputs for versioned Daily Research Brief stages.

The legacy workflow historically handed a mutable, ambient context mapping to
every executor.  Daily Brief stages instead receive a deliberately small
projection whose canonical hash can be recorded without persisting prompt or
research content.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from src.services.research_engine.contracts import canonical_json_sha256


@dataclass(frozen=True)
class PromptContext:
    """One immutable, content-bearing prompt input projection."""

    version: str
    hash: str
    payload: dict[str, Any]

    def persistence_metadata(self) -> dict[str, str]:
        """Return the only Daily Brief prompt metadata safe to persist or log."""

        return {
            "prompt_context_version": self.version,
            "prompt_context_hash": self.hash,
        }


class PromptContextBuilder:
    """Build explicit stage inputs for the Daily Brief contract."""

    VERSION: ClassVar[str] = "daily-brief-v1"
    _SCOPE_KEYS: ClassVar[tuple[str, ...]] = (
        "research_question",
        "inclusion_criteria",
        "exclusion_criteria",
        "providers",
        "limit_per_provider",
        "notes",
    )
    _CAPABILITY_KEYS: ClassVar[tuple[str, ...]] = (
        "id",
        "label",
        "daily_brief_eligible",
        "available",
        "features",
    )
    _FEATURE_KEYS: ClassVar[tuple[str, ...]] = (
        "full_text",
        "date_filter",
        "cursor",
    )
    _UPSTREAM_STAGE: ClassVar[dict[str, str]] = {
        "screen": "search",
        "extract": "screen",
        "synthesize": "extract",
        "verify": "synthesize",
    }
    _UPSTREAM_KEYS: ClassVar[dict[str, tuple[str, ...]]] = {
        "search": (
            "contract_version",
            "stage_type",
            "coverage",
            "selected_sources",
        ),
        "screen": (
            "contract_version",
            "stage_type",
            "included_source_ids",
            "processing_coverage",
        ),
        "extract": (
            "contract_version",
            "stage_type",
            "processing_coverage",
            "diagnostics",
        ),
        "synthesize": (
            "contract_version",
            "stage_type",
            "processing_coverage",
            "diagnostics",
        ),
    }

    @classmethod
    def build(
        cls,
        *,
        stage_type: str,
        scope_confirmation: Mapping[str, Any],
        capability_manifest: Sequence[Mapping[str, Any]],
        upstream_envelope: Mapping[str, Any],
        target_schema: Mapping[str, Any],
        budget: Mapping[str, Any],
    ) -> PromptContext:
        """Return a canonical, least-privilege prompt input projection."""

        if scope_confirmation.get("confirmed") is not True:
            raise ValueError("Daily Brief model stages require confirmed scope")
        expected_upstream = cls._UPSTREAM_STAGE.get(stage_type)
        if expected_upstream is None:
            raise ValueError("unsupported Daily Brief model stage")
        if upstream_envelope.get("stage_type") != expected_upstream:
            raise ValueError(
                "Daily Brief stage requires its immediate upstream envelope"
            )
        if upstream_envelope.get("contract_version") != 1:
            raise ValueError("Daily Brief stage requires a version-1 upstream envelope")

        confirmed_scope = {
            key: copy.deepcopy(scope_confirmation[key])
            for key in cls._SCOPE_KEYS
            if key in scope_confirmation
        }
        providers = confirmed_scope.get("providers")
        selected_ids = (
            {item for item in providers if isinstance(item, str)}
            if isinstance(providers, list)
            else set()
        )

        selected_capabilities: list[dict[str, Any]] = []
        for capability in capability_manifest:
            connector_id = capability.get("id")
            if connector_id not in selected_ids:
                continue
            projected: dict[str, Any] = {
                key: copy.deepcopy(capability[key])
                for key in cls._CAPABILITY_KEYS
                if key in capability and key != "features"
            }
            features = capability.get("features")
            if isinstance(features, Mapping):
                projected["features"] = {
                    key: bool(features[key])
                    for key in cls._FEATURE_KEYS
                    if key in features
                }
            selected_capabilities.append(projected)
        selected_capabilities.sort(key=lambda item: str(item.get("id", "")))

        upstream: dict[str, Any] = {}
        for key in cls._UPSTREAM_KEYS[expected_upstream]:
            if key not in upstream_envelope:
                continue
            value = upstream_envelope[key]
            if key == "processing_coverage":
                upstream[key] = cls._processing_coverage_summary(value)
            elif key == "diagnostics":
                upstream[key] = cls._reason_counts(value)
            else:
                upstream[key] = copy.deepcopy(value)
        # Evidence-bearing records are sent once through the stage's bounded
        # batch records.  The fixed context retains only identifiers and
        # immediate-envelope control metadata.
        evidence_ids = sorted(cls._evidence_ids(upstream_envelope))
        payload: dict[str, Any] = {
            "prompt_context_version": cls.VERSION,
            "stage_type": stage_type,
            "confirmed_scope": confirmed_scope,
            "selected_capabilities": selected_capabilities,
            "upstream_envelope": upstream,
            "evidence_ids": evidence_ids,
            "target_schema": copy.deepcopy(dict(target_schema)),
            "budget": copy.deepcopy(dict(budget)),
        }
        return PromptContext(
            version=cls.VERSION,
            hash=canonical_json_sha256(payload),
            payload=payload,
        )

    @staticmethod
    def build_legacy(context: dict[str, Any]) -> dict[str, Any]:
        """Preserve the legacy ambient-context object by identity."""

        return context

    @classmethod
    def _evidence_ids(cls, value: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(value, Mapping):
            evidence_id = value.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id:
                found.add(evidence_id)
            for child in value.values():
                found.update(cls._evidence_ids(child))
        elif isinstance(value, list):
            for child in value:
                found.update(cls._evidence_ids(child))
        return found

    @classmethod
    def _processing_coverage_summary(cls, value: Any) -> dict[str, Any]:
        """Project batch-sized coverage metadata to bounded deterministic counts."""

        if not isinstance(value, Mapping):
            return {}
        summary: dict[str, Any] = {}
        for stage_key in sorted(value, key=str):
            coverage = value[stage_key]
            if not isinstance(coverage, Mapping):
                continue
            projected: dict[str, Any] = {}
            if isinstance(coverage.get("complete"), bool):
                projected["complete"] = coverage["complete"]
            for source_key in (
                "seen_source_ids",
                "processed_source_ids",
                "excluded_source_ids",
                "source_parts",
            ):
                items = coverage.get(source_key)
                if isinstance(items, list):
                    projected[source_key.removesuffix("_ids") + "_count"] = len(items)
            projected["omitted_reason_counts"] = cls._reason_counts(
                coverage.get("omitted")
            )
            summary[str(stage_key)] = projected
        return summary

    @staticmethod
    def _reason_counts(value: Any) -> dict[str, int]:
        if not isinstance(value, list):
            return {}
        counts: dict[str, int] = {}
        for item in value:
            if not isinstance(item, Mapping):
                continue
            reason = item.get("reason")
            if isinstance(reason, str) and reason:
                counts[reason] = counts.get(reason, 0) + 1
        return {reason: counts[reason] for reason in sorted(counts)}


__all__ = ["PromptContext", "PromptContextBuilder"]

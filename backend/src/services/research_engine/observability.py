"""Content-safe logs and in-process metrics for research workflows."""

from __future__ import annotations

import copy
import logging
import math
from collections import defaultdict
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_MAX_CATEGORY_CHARS = 64
_MAX_COUNT = 1_000_000
_MAX_DURATION_SECONDS = 7 * 24 * 60 * 60
_EVENT_FIELDS = {
    "stage_duration": {
        "run_id",
        "organization_id",
        "step_index",
        "stage_type",
        "duration_seconds",
        "status",
    },
    "provider_outcome": {
        "run_id",
        "organization_id",
        "provider",
        "outcome",
        "returned_count",
    },
    "review": {
        "run_id",
        "organization_id",
        "review_kind",
        "outcome",
        "wait_seconds",
    },
    "final_status": {"run_id", "organization_id", "status"},
    "pause": {
        "run_id",
        "organization_id",
        "pause_kind",
        "review_kind",
    },
    "override": {
        "run_id",
        "organization_id",
        "override_kind",
        "outcome",
    },
    "validation_error": {
        "run_id",
        "organization_id",
        "stage_type",
        "error_kind",
        "count",
    },
}


class ResearchObservability:
    """Emit allowlisted operational data without accepting research content."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger or globals()["logger"]
        self._counters: defaultdict[str, defaultdict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._histograms: defaultdict[str, defaultdict[str, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )

    @staticmethod
    def _category(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value or len(value) > _MAX_CATEGORY_CHARS:
            raise ValueError(f"invalid observability category: {field}")
        return value

    @staticmethod
    def _count(value: Any, field: str) -> int:
        if type(value) is not int or not 0 <= value <= _MAX_COUNT:
            raise ValueError(f"invalid observability count: {field}")
        return value

    @staticmethod
    def _duration(value: Any, field: str) -> float:
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= float(value) <= _MAX_DURATION_SECONDS
        ):
            raise ValueError(f"invalid observability duration: {field}")
        return float(value)

    @classmethod
    def _validate_field(cls, field: str, value: Any) -> Any:
        if field in {"run_id", "organization_id"}:
            if value is not None and not isinstance(value, UUID):
                raise ValueError(f"invalid observability identifier: {field}")
            return str(value) if value is not None else None
        if field == "step_index":
            return cls._count(value, field)
        if field in {"returned_count", "count"}:
            return cls._count(value, field)
        if field in {"duration_seconds", "wait_seconds"}:
            return cls._duration(value, field)
        return cls._category(value, field)

    def record(self, event: str, **fields: Any) -> None:
        """Log one event after validating its complete field allowlist."""
        allowed = _EVENT_FIELDS.get(event)
        if allowed is None:
            raise ValueError("unsupported observability event")
        unexpected = set(fields) - allowed
        missing = allowed - set(fields)
        if unexpected:
            raise ValueError(
                f"unsupported observability field: {sorted(unexpected)[0]}"
            )
        if missing:
            raise ValueError(f"missing observability field: {sorted(missing)[0]}")
        safe_fields = {
            field: self._validate_field(field, value) for field, value in fields.items()
        }
        self._logger.info("research_event=%s fields=%s", event, safe_fields)

    def record_stage_duration(
        self,
        *,
        run_id: UUID,
        organization_id: UUID | None,
        step_index: int,
        stage_type: str,
        duration_seconds: float,
        status: str,
    ) -> None:
        self.record(
            "stage_duration",
            run_id=run_id,
            organization_id=organization_id,
            step_index=step_index,
            stage_type=stage_type,
            duration_seconds=duration_seconds,
            status=status,
        )
        self._histograms["stage_duration_seconds"][stage_type].append(
            float(duration_seconds)
        )

    def record_provider_outcome(
        self,
        *,
        run_id: UUID,
        organization_id: UUID | None,
        provider: str,
        outcome: str,
        returned_count: int,
    ) -> None:
        self.record(
            "provider_outcome",
            run_id=run_id,
            organization_id=organization_id,
            provider=provider,
            outcome=outcome,
            returned_count=returned_count,
        )
        self._counters["provider_outcomes"][outcome] += 1

    def record_review(
        self,
        *,
        run_id: UUID,
        organization_id: UUID | None,
        review_kind: str,
        outcome: str,
        wait_seconds: float,
    ) -> None:
        self.record(
            "review",
            run_id=run_id,
            organization_id=organization_id,
            review_kind=review_kind,
            outcome=outcome,
            wait_seconds=wait_seconds,
        )
        self._counters["reviews"][outcome] += 1
        self._histograms["review_wait_seconds"][review_kind].append(float(wait_seconds))

    def record_final_status(
        self,
        *,
        run_id: UUID,
        organization_id: UUID | None,
        status: str,
    ) -> None:
        self.record(
            "final_status",
            run_id=run_id,
            organization_id=organization_id,
            status=status,
        )
        self._counters["final_status"][status] += 1

    def record_pause(
        self,
        *,
        run_id: UUID,
        organization_id: UUID | None,
        pause_kind: str,
        review_kind: str,
    ) -> None:
        self.record(
            "pause",
            run_id=run_id,
            organization_id=organization_id,
            pause_kind=pause_kind,
            review_kind=review_kind,
        )
        self._counters["pauses"][pause_kind] += 1

    def record_override(
        self,
        *,
        run_id: UUID,
        organization_id: UUID | None,
        override_kind: str,
        outcome: str,
    ) -> None:
        self.record(
            "override",
            run_id=run_id,
            organization_id=organization_id,
            override_kind=override_kind,
            outcome=outcome,
        )
        self._counters["overrides"][outcome] += 1

    def record_validation_error(
        self,
        *,
        run_id: UUID,
        organization_id: UUID | None,
        stage_type: str,
        error_kind: str,
        count: int = 1,
    ) -> None:
        self.record(
            "validation_error",
            run_id=run_id,
            organization_id=organization_id,
            stage_type=stage_type,
            error_kind=error_kind,
            count=count,
        )
        self._counters["validation_errors"][error_kind] += count

    def snapshot(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Return an isolated metrics snapshot for collection or tests."""
        return {
            "counters": copy.deepcopy(
                {name: dict(values) for name, values in self._counters.items()}
            ),
            "histograms": copy.deepcopy(
                {name: dict(values) for name, values in self._histograms.items()}
            ),
        }


research_observability = ResearchObservability()

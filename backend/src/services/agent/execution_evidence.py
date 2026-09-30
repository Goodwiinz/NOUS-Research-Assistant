"""Shared classification of tool-result completion evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

ExecutionEvidenceState = Literal["completed", "pending", "none"]

_PENDING_STATUSES = frozenset(
    {
        "accepted",
        "in_progress",
        "pending",
        "processing",
        "queued",
        "running",
        "started",
        "submitted",
        "unknown",
        "waiting",
    }
)
_FAILED_STATUSES = frozenset(
    {"cancelled", "denied", "error", "failed", "rejected", "skipped", "timeout"}
)
_COMPLETED_STATUSES = frozenset(
    {
        "already_exists",
        "complete",
        "completed",
        "created",
        "deleted",
        "ingestion_complete",
        "success",
        "succeeded",
        "updated",
    }
)


def result_evidence_state(
    value: Any, *, structured_status_required: bool = False
) -> ExecutionEvidenceState:
    """Classify result evidence, treating unrecognized statuses as uncertain.

    ToolMessage content may contain ordinary successful read output without a
    status field, so callers can retain that legacy behavior. A stored
    external-effect execution must pass ``structured_status_required=True``;
    its transport success alone is not proof that the business effect finished.
    """
    if isinstance(value, str):
        content = value.strip()
        if not content:
            return "none"
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return "pending" if structured_status_required else "completed"
        if not isinstance(payload, Mapping):
            return "pending" if structured_status_required else "completed"
        value = payload
    if isinstance(value, Mapping):
        if value.get("error"):
            return "none"
        raw_status = value.get("status")
        if raw_status is None:
            return "pending" if structured_status_required else "completed"
        status = str(raw_status).strip().lower()
        if status in _FAILED_STATUSES:
            return "none"
        if status in _PENDING_STATUSES:
            return "pending"
        if status in _COMPLETED_STATUSES:
            return "completed"
        return "pending"
    if value is None:
        return "pending" if structured_status_required else "none"
    return "pending" if structured_status_required else "completed"


__all__ = ["ExecutionEvidenceState", "result_evidence_state"]

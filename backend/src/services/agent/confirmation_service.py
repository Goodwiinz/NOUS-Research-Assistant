"""Identity and dispatch contract shared by the job and SSE approval paths.

An approval authorizes one saved interrupt, never whichever action happens to
be pending on a thread. The durable claim and the checkpoint must agree on
the receipt before a producer may resume. Legacy cards without receipts expire.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any


class ApprovalExpired(ValueError):
    """The presented approval no longer identifies the pending action."""

    def __init__(self) -> None:
        super().__init__(
            "This approval has expired. Refresh the conversation or start a new request."
        )


@dataclass(frozen=True)
class PendingApproval:
    approval_id: str
    interrupt_id: str
    details: dict[str, Any]

    def confirmation(self) -> dict[str, Any]:
        return {**self.details, "approval_id": self.approval_id}

    def resume(self, confirmed: bool) -> dict[str, dict[str, bool]]:
        # A bare {confirmed: bool} resumes the next interrupt. Keying the value
        # by interrupt id also fences dispatch if the checkpoint advances.
        return {self.interrupt_id: {"confirmed": confirmed}}


def pending_approval(
    snapshot: Any,
    *,
    thread_id: str,
    run_id: str | None,
    user_id: Any,
) -> PendingApproval | None:
    """Bind the first pending interrupt to its saved state and full action.

    Hash before wire redaction so changes to hidden tool arguments also rotate
    the receipt. Only an opaque digest is published, never the canonical input.
    The same checkpoint produces the same receipt across workers and replay.
    """
    interrupts = [
        intr
        for task in (getattr(snapshot, "tasks", None) or ())
        for intr in (getattr(task, "interrupts", None) or ())
    ]
    if not interrupts:
        return None
    config = (getattr(snapshot, "config", None) or {}).get("configurable", {})
    values = getattr(snapshot, "values", None)
    intr = interrupts[0]
    interrupt_id = getattr(intr, "id", None)
    details = getattr(intr, "value", None)
    checkpoint_id = config.get("checkpoint_id")
    if (
        not isinstance(checkpoint_id, str)
        or not checkpoint_id
        or not isinstance(interrupt_id, str)
        or not interrupt_id
        or interrupt_id == "placeholder-id"
        or not isinstance(details, dict)
        or not isinstance(values, dict)
        or str(values.get("user_id", "")) != str(user_id)
    ):
        raise ApprovalExpired()
    try:
        canonical = json.dumps(
            {
                "version": 1,
                "thread_id": thread_id,
                "run_id": run_id,
                "user_id": str(user_id),
                "checkpoint_id": checkpoint_id,
                "checkpoint_ns": config.get("checkpoint_ns", ""),
                "interrupt_id": interrupt_id,
                "action": details,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ApprovalExpired() from exc
    return PendingApproval(hashlib.sha256(canonical).hexdigest(), interrupt_id, details)


def require_approval(
    snapshot: Any,
    *,
    approval_id: str,
    thread_id: str,
    run_id: str | None,
    user_id: Any,
) -> PendingApproval:
    approval = pending_approval(
        snapshot, thread_id=thread_id, run_id=run_id, user_id=user_id
    )
    if approval is None or not hmac.compare_digest(approval.approval_id, approval_id):
        raise ApprovalExpired()
    return approval


def pending_confirmation(
    snapshot: Any, *, thread_id: str, run_id: str | None, user_id: Any
) -> dict[str, Any] | None:
    approval = pending_approval(
        snapshot, thread_id=thread_id, run_id=run_id, user_id=user_id
    )
    return approval.confirmation() if approval is not None else None

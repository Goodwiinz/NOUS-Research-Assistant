"""Caller-owned append and replay operations for protocol decisions.

Idempotency is scoped to one aggregate stream. Protocol approval derives one
stable subkey per emitted event (for example ``<request>:superseded`` and
``<request>:approved``). Repeating a subkey with the same request fingerprint
returns the original event; reusing it for different request content fails.
This module never commits or rolls back the caller's transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROTOCOL_AGGREGATE = "research_protocol"
_PROTOCOL_SUBJECT = "research_protocol_version"
_PROTOCOL_CANONICALIZATION = "research-protocol-v1"

_EVENT_PAYLOAD_KEYS: dict[tuple[str, int], frozenset[str]] = {
    ("protocol.superseded", 1): frozenset(
        {"protocol_id", "superseded_by_version_id", "superseded_by_hash"}
    ),
    ("protocol.approved", 1): frozenset(
        {
            "protocol_id",
            "question_version_id",
            "blueprint_id",
            "previous_approved_version_id",
            "expected_protocol_version",
            "canonicalization_version",
        }
    ),
}


class ResearchDecisionError(RuntimeError):
    """Base error for the retained decision ledger."""


class DecisionValidationError(ResearchDecisionError):
    """An event does not satisfy the versioned ledger vocabulary."""


class DecisionIdempotencyConflict(ResearchDecisionError):
    """An idempotency key was reused for a different logical request."""


class DecisionReplayError(ResearchDecisionError):
    """Stored history cannot be replayed without guessing or skipping."""


@dataclass(frozen=True)
class AppendDecisionResult:
    event: ResearchDecisionEvent
    replayed: bool


def decision_request_fingerprint(value: Mapping[str, Any]) -> str:
    """Hash canonical JSON request content for idempotency conflict detection."""
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_event(
    *,
    aggregate_type: str,
    aggregate_id: UUID,
    event_type: str,
    event_schema_version: int,
    subject_type: str,
    subject_id: UUID,
    subject_version_id: UUID | None,
    subject_hash: str,
    payload: Mapping[str, Any],
    request_fingerprint: str,
) -> None:
    required = _EVENT_PAYLOAD_KEYS.get((event_type, event_schema_version))
    if required is None:
        raise DecisionValidationError(
            f"unsupported decision event {event_type!r} schema {event_schema_version}"
        )
    if aggregate_type != _PROTOCOL_AGGREGATE or subject_type != _PROTOCOL_SUBJECT:
        raise DecisionValidationError("decision aggregate or subject type is invalid")
    if subject_version_id is None or subject_id != subject_version_id:
        raise DecisionValidationError("protocol event must identify its exact version")
    if set(payload) != required:
        raise DecisionValidationError(
            "decision payload does not match its event schema"
        )
    if str(payload["protocol_id"]) != str(aggregate_id):
        raise DecisionValidationError("decision payload belongs to another protocol")
    if not _SHA256_RE.fullmatch(subject_hash):
        raise DecisionValidationError("subject_hash must be a lowercase SHA-256 digest")
    if not _SHA256_RE.fullmatch(request_fingerprint):
        raise DecisionValidationError(
            "request_fingerprint must be a lowercase SHA-256 digest"
        )
    _validate_protocol_payload(event_type, payload, subject_version_id)


def _validated_payload_uuid(value: Any, field: str) -> UUID:
    if not isinstance(value, str):
        raise DecisionValidationError(f"decision payload {field} is not a UUID")
    try:
        return UUID(value)
    except ValueError as error:
        raise DecisionValidationError(
            f"decision payload {field} is not a UUID"
        ) from error


def _validate_protocol_payload(
    event_type: str, payload: Mapping[str, Any], subject_version_id: UUID
) -> None:
    _validated_payload_uuid(payload["protocol_id"], "protocol_id")
    if event_type == "protocol.superseded":
        replacement = _validated_payload_uuid(
            payload["superseded_by_version_id"], "superseded_by_version_id"
        )
        if replacement == subject_version_id:
            raise DecisionValidationError("protocol cannot supersede itself")
        replacement_hash = payload["superseded_by_hash"]
        if not isinstance(replacement_hash, str) or not _SHA256_RE.fullmatch(
            replacement_hash
        ):
            raise DecisionValidationError(
                "superseded_by_hash must be a lowercase SHA-256 digest"
            )
        return

    _validated_payload_uuid(payload["question_version_id"], "question_version_id")
    _validated_payload_uuid(payload["blueprint_id"], "blueprint_id")
    previous = payload["previous_approved_version_id"]
    if previous is not None:
        _validated_payload_uuid(previous, "previous_approved_version_id")
    expected_version = payload["expected_protocol_version"]
    if (
        not isinstance(expected_version, int)
        or isinstance(expected_version, bool)
        or expected_version < 1
    ):
        raise DecisionValidationError(
            "expected_protocol_version must be a positive integer"
        )
    if payload["canonicalization_version"] != _PROTOCOL_CANONICALIZATION:
        raise DecisionValidationError("canonicalization_version is unsupported")


async def _locked_stream(
    db: AsyncSession,
    *,
    collection_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
) -> ResearchDecisionStream:
    create = (
        pg_insert(ResearchDecisionStream)
        .values(
            id=uuid4(),
            collection_id=collection_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            next_seq=1,
        )
        .on_conflict_do_nothing(index_elements=["aggregate_type", "aggregate_id"])
    )
    await db.execute(create)
    stream = cast(
        ResearchDecisionStream | None,
        (
            await db.execute(
                select(ResearchDecisionStream)
                .where(
                    ResearchDecisionStream.aggregate_type == aggregate_type,
                    ResearchDecisionStream.aggregate_id == aggregate_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one_or_none(),
    )
    if stream is None or cast(UUID, stream.collection_id) != collection_id:
        raise DecisionValidationError("decision stream collection mismatch")
    return stream


async def append_decision(
    db: AsyncSession,
    *,
    collection_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
    event_type: str,
    event_schema_version: int,
    actor_user_id: UUID,
    actor_role: str,
    subject_type: str,
    subject_id: UUID,
    subject_version_id: UUID | None,
    subject_hash: str,
    reason: str | None,
    payload: Mapping[str, Any],
    idempotency_key: str,
    request_fingerprint: str,
) -> AppendDecisionResult:
    """Append one event under a stream lock without ending the transaction."""
    _validate_event(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        event_schema_version=event_schema_version,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_version_id=subject_version_id,
        subject_hash=subject_hash,
        payload=payload,
        request_fingerprint=request_fingerprint,
    )
    if not idempotency_key or len(idempotency_key) > 255:
        raise DecisionValidationError("idempotency_key must contain 1-255 characters")
    stream = await _locked_stream(
        db,
        collection_id=collection_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
    )
    existing = cast(
        ResearchDecisionEvent | None,
        (
            await db.execute(
                select(ResearchDecisionEvent).where(
                    ResearchDecisionEvent.stream_id == stream.id,
                    ResearchDecisionEvent.idempotency_key == idempotency_key,
                )
            )
        )
        .scalars()
        .one_or_none(),
    )
    if existing is not None:
        same_event = (
            existing.request_fingerprint == request_fingerprint
            and existing.event_type == event_type
            and existing.event_schema_version == event_schema_version
            and existing.actor_user_id == actor_user_id
            and existing.subject_id == subject_id
            and existing.subject_hash == subject_hash
            and existing.payload == dict(payload)
        )
        if not same_event:
            raise DecisionIdempotencyConflict(
                "idempotency key was already used for different request content"
            )
        return AppendDecisionResult(existing, replayed=True)

    seq = cast(int, stream.next_seq)
    event = ResearchDecisionEvent(
        id=uuid4(),
        stream_id=stream.id,
        collection_id=collection_id,
        seq=seq,
        event_type=event_type,
        event_schema_version=event_schema_version,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_version_id=subject_version_id,
        subject_hash=subject_hash,
        reason=reason,
        payload=dict(payload),
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
    )
    db.add(event)
    stream.next_seq = seq + 1
    await db.flush()
    return AppendDecisionResult(event, replayed=False)


async def replay_decisions(
    db: AsyncSession,
    *,
    collection_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
    subject_version_hash: Callable[[UUID], Awaitable[str | None]],
) -> Sequence[ResearchDecisionEvent]:
    """Read and structurally validate complete ordered aggregate history."""
    stream = cast(
        ResearchDecisionStream | None,
        (
            await db.execute(
                select(ResearchDecisionStream)
                .where(
                    ResearchDecisionStream.aggregate_type == aggregate_type,
                    ResearchDecisionStream.aggregate_id == aggregate_id,
                )
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one_or_none(),
    )
    if stream is None:
        return ()
    if cast(UUID, stream.collection_id) != collection_id:
        raise DecisionReplayError("decision stream collection mismatch")
    events = list(
        (
            await db.execute(
                select(ResearchDecisionEvent)
                .where(ResearchDecisionEvent.stream_id == stream.id)
                .order_by(ResearchDecisionEvent.seq.asc())
            )
        )
        .scalars()
        .all()
    )
    for expected_seq, event in enumerate(events, start=1):
        if event.seq != expected_seq or event.collection_id != collection_id:
            raise DecisionReplayError("decision stream ordering or scope is invalid")
        try:
            _validate_event(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=cast(str, event.event_type),
                event_schema_version=cast(int, event.event_schema_version),
                subject_type=cast(str, event.subject_type),
                subject_id=cast(UUID, event.subject_id),
                subject_version_id=cast(UUID | None, event.subject_version_id),
                subject_hash=cast(str, event.subject_hash),
                payload=cast(dict[str, Any], event.payload),
                request_fingerprint=cast(str, event.request_fingerprint),
            )
        except DecisionValidationError as error:
            raise DecisionReplayError(str(error)) from error
        version_id = cast(UUID, event.subject_version_id)
        stored_hash = await subject_version_hash(version_id)
        if stored_hash is None:
            raise DecisionReplayError("decision subject version is missing")
        if stored_hash != event.subject_hash:
            raise DecisionReplayError("decision subject hash no longer matches")
    _validate_protocol_transitions(events, aggregate_id)
    if cast(int, stream.next_seq) != len(events) + 1:
        raise DecisionReplayError("decision stream high-water mark is invalid")
    return events


def _payload_uuid(value: Any, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise DecisionReplayError(f"decision payload {field} is not a UUID") from error


def _validate_protocol_transitions(
    events: Sequence[ResearchDecisionEvent], aggregate_id: UUID
) -> None:
    current: UUID | None = None
    pending: tuple[UUID, UUID, str] | None = None
    for event in events:
        payload = cast(dict[str, Any], event.payload)
        if _payload_uuid(payload["protocol_id"], "protocol_id") != aggregate_id:
            raise DecisionReplayError("decision payload belongs to another protocol")
        subject = cast(UUID, event.subject_version_id)
        if event.event_type == "protocol.superseded":
            if current is None or subject != current or pending is not None:
                raise DecisionReplayError("contradictory protocol supersession")
            replacement = _payload_uuid(
                payload["superseded_by_version_id"], "superseded_by_version_id"
            )
            pending = (
                current,
                replacement,
                cast(str, payload["superseded_by_hash"]),
            )
        elif event.event_type == "protocol.approved":
            previous_value = payload["previous_approved_version_id"]
            previous = (
                _payload_uuid(previous_value, "previous_approved_version_id")
                if previous_value is not None
                else None
            )
            if current is None:
                if previous is not None or pending is not None:
                    raise DecisionReplayError("contradictory initial protocol approval")
            elif (
                pending != (current, subject, event.subject_hash) or previous != current
            ):
                raise DecisionReplayError("approval is missing its exact supersession")
            current = subject
            pending = None
    if pending is not None:
        raise DecisionReplayError("protocol supersession has no matching approval")

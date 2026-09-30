"""Caller-owned append and replay operations for research decisions.

Each aggregate family (``research_protocol``, ``research_identity``,
``research_screening``) registers
its subject type, payload vocabulary, value validation and replay transition
rules in ``_FAMILIES``.

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
from src.services.research_engine import screening_rules

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

_IDENTITY_AGGREGATE = "research_identity"
_IDENTITY_SUBJECT = "research_report"
STUDY_LINK_STATUSES = frozenset({"proposed", "confirmed", "disputed"})
_IDENTITY_MOVE_KEYS = frozenset(
    {"collection_id", "moved_source_ids", "moved_identifiers", "protocol_version_id"}
)
_IDENTITY_PAYLOAD_KEYS: dict[tuple[str, int], frozenset[str]] = {
    ("identity.report_merged", 1): _IDENTITY_MOVE_KEYS
    | {"surviving_report_id", "merged_report_ids"},
    ("identity.report_split", 1): _IDENTITY_MOVE_KEYS
    | {"source_report_id", "new_report_id"},
    # Schema 2 (GOO-300): the move also names re-pointed imported records.
    ("identity.report_merged", 2): _IDENTITY_MOVE_KEYS
    | {"surviving_report_id", "merged_report_ids", "moved_import_record_ids"},
    ("identity.report_split", 2): _IDENTITY_MOVE_KEYS
    | {"source_report_id", "new_report_id", "moved_import_record_ids"},
    ("identity.study_linked", 1): frozenset(
        {
            "collection_id",
            "report_id",
            "study_id",
            "status",
            "prior_study_id",
            "prior_status",
            "match_evidence",
            "protocol_version_id",
        }
    ),
}

_SCREENING_AGGREGATE = "research_screening"
_SCREENING_SUBJECT = "screening_queue"
_SCREENING_ASSIGNMENT_KEYS = frozenset(
    {"collection_id", "queue_id", "assignment_id", "reviewer_id"}
)
_SCREENING_OBSERVATION_KEYS = _SCREENING_ASSIGNMENT_KEYS | {
    "observation_id",
    "report_id",
    "decision",
    "exclusion_reason",
}
_SCREENING_PAYLOAD_KEYS: dict[tuple[str, int], frozenset[str]] = {
    ("screening.queue_created", 1): frozenset(
        {
            "collection_id",
            "queue_id",
            "stage",
            "protocol_version_id",
            "criteria_hash",
            "report_ids",
            "exclusion_reasons",
            "supersedes_queue_id",
            "reviewer_mode",
            # Count of AI suggestion rows not imported (None: no import).
            "suggestions_skipped",
        }
    ),
    ("screening.assigned", 1): _SCREENING_ASSIGNMENT_KEYS,
    ("screening.unassigned", 1): _SCREENING_ASSIGNMENT_KEYS,
    ("screening.observed", 1): _SCREENING_OBSERVATION_KEYS,
    ("screening.superseded", 1): _SCREENING_OBSERVATION_KEYS
    | {"superseded_observation_id"},
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


@dataclass(frozen=True)
class _Family:
    subject_type: str
    payload_keys: dict[tuple[str, int], frozenset[str]]
    # (event_type, payload, aggregate_id, subject_version_id, subject_id)
    validate_payload: Callable[[str, Mapping[str, Any], UUID, UUID | None, UUID], None]
    validate_transitions: Callable[[Sequence[ResearchDecisionEvent], UUID], None]
    # False: subject_hash is the request fingerprint of the stored payload.
    requires_subject_version: bool


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
    key = (event_type, event_schema_version)
    if not any(key in family.payload_keys for family in _FAMILIES.values()):
        raise DecisionValidationError(
            f"unsupported decision event {event_type!r} schema {event_schema_version}"
        )
    family = _FAMILIES.get(aggregate_type)
    if (
        family is None
        or key not in family.payload_keys
        or subject_type != family.subject_type
    ):
        raise DecisionValidationError("decision aggregate or subject type is invalid")
    if family.requires_subject_version:
        if subject_version_id is None or subject_id != subject_version_id:
            raise DecisionValidationError(
                "protocol event must identify its exact version"
            )
    elif subject_version_id is not None:
        raise DecisionValidationError("identity event must not carry a subject version")
    if set(payload) != family.payload_keys[key]:
        raise DecisionValidationError(
            "decision payload does not match its event schema"
        )
    if aggregate_type == _PROTOCOL_AGGREGATE and str(payload["protocol_id"]) != str(
        aggregate_id
    ):
        raise DecisionValidationError("decision payload belongs to another protocol")
    if not _SHA256_RE.fullmatch(subject_hash):
        raise DecisionValidationError("subject_hash must be a lowercase SHA-256 digest")
    if not _SHA256_RE.fullmatch(request_fingerprint):
        raise DecisionValidationError(
            "request_fingerprint must be a lowercase SHA-256 digest"
        )
    family.validate_payload(
        event_type, payload, aggregate_id, subject_version_id, subject_id
    )
    if not family.requires_subject_version and (
        subject_hash != decision_request_fingerprint(payload)
    ):
        # Replay re-derives this hash from the stored payload; reject a
        # mismatch at append time instead of poisoning history forever.
        raise DecisionValidationError("subject_hash must fingerprint the payload")


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
    event_type: str,
    payload: Mapping[str, Any],
    aggregate_id: UUID,
    subject_version_id: UUID | None,
    subject_id: UUID,
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


def _validated_uuid_list(value: Any, field: str) -> list[UUID]:
    if not isinstance(value, list):
        raise DecisionValidationError(f"decision payload {field} must be a list")
    return [_validated_payload_uuid(item, field) for item in value]


def _validated_optional_uuid(value: Any, field: str) -> UUID | None:
    return None if value is None else _validated_payload_uuid(value, field)


def _validate_identity_payload(
    event_type: str,
    payload: Mapping[str, Any],
    aggregate_id: UUID,
    subject_version_id: UUID | None,
    subject_id: UUID,
) -> None:
    if _validated_payload_uuid(payload["collection_id"], "collection_id") != (
        aggregate_id
    ):
        raise DecisionValidationError("decision payload belongs to another collection")
    _validated_optional_uuid(payload["protocol_version_id"], "protocol_version_id")
    if event_type == "identity.study_linked":
        subject = _validated_payload_uuid(payload["report_id"], "report_id")
        _validated_payload_uuid(payload["study_id"], "study_id")
        prior = _validated_optional_uuid(payload["prior_study_id"], "prior_study_id")
        status = payload["status"]
        if not isinstance(status, str) or status not in STUDY_LINK_STATUSES:
            raise DecisionValidationError("study link status is invalid")
        prior_status = payload["prior_status"]
        if prior_status is not None and (
            not isinstance(prior_status, str) or prior_status not in STUDY_LINK_STATUSES
        ):
            raise DecisionValidationError("prior study link status is invalid")
        if (prior is None) != (prior_status is None):
            raise DecisionValidationError("prior study link is incomplete")
        if not isinstance(payload["match_evidence"], Mapping):
            raise DecisionValidationError("match_evidence must be an object")
    else:
        _validated_uuid_list(payload["moved_source_ids"], "moved_source_ids")
        if "moved_import_record_ids" in payload:
            _validated_uuid_list(
                payload["moved_import_record_ids"], "moved_import_record_ids"
            )
        moved_identifiers = payload["moved_identifiers"]
        if not isinstance(moved_identifiers, list) or not all(
            isinstance(item, Mapping)
            and set(item) == {"kind", "value"}
            and isinstance(item["kind"], str)
            and isinstance(item["value"], str)
            for item in moved_identifiers
        ):
            raise DecisionValidationError(
                "decision payload moved_identifiers must be kind/value pairs"
            )
        if event_type == "identity.report_merged":
            subject = _validated_payload_uuid(
                payload["surviving_report_id"], "surviving_report_id"
            )
            merged = _validated_uuid_list(
                payload["merged_report_ids"], "merged_report_ids"
            )
            if not merged:
                raise DecisionValidationError("merge must name at least one report")
            if subject in merged:
                raise DecisionValidationError("report cannot merge into itself")
        else:
            subject = _validated_payload_uuid(
                payload["source_report_id"], "source_report_id"
            )
            if _validated_payload_uuid(payload["new_report_id"], "new_report_id") == (
                subject
            ):
                raise DecisionValidationError("split must create a new report")
    if subject != subject_id:
        raise DecisionValidationError("identity event subject is not its report")


def _validate_screening_payload(
    event_type: str,
    payload: Mapping[str, Any],
    aggregate_id: UUID,
    subject_version_id: UUID | None,
    subject_id: UUID,
) -> None:
    _validated_payload_uuid(payload["collection_id"], "collection_id")
    queue_id = _validated_payload_uuid(payload["queue_id"], "queue_id")
    if not queue_id == aggregate_id == subject_id:
        raise DecisionValidationError(
            "decision payload belongs to another screening queue"
        )
    if event_type == "screening.queue_created":
        if payload["stage"] not in screening_rules.STAGES:
            raise DecisionValidationError("screening stage is invalid")
        if payload["reviewer_mode"] not in screening_rules.MODES:
            raise DecisionValidationError("screening reviewer_mode is invalid")
        _validated_payload_uuid(payload["protocol_version_id"], "protocol_version_id")
        criteria = payload["criteria_hash"]
        if not isinstance(criteria, str) or not _SHA256_RE.fullmatch(criteria):
            raise DecisionValidationError(
                "criteria_hash must be a lowercase SHA-256 digest"
            )
        reports = _validated_uuid_list(payload["report_ids"], "report_ids")
        if not reports or len(set(reports)) != len(reports):
            raise DecisionValidationError("report_ids must be non-empty and unique")
        reasons = payload["exclusion_reasons"]
        if not isinstance(reasons, list) or not all(
            isinstance(reason, str) for reason in reasons
        ):
            raise DecisionValidationError("exclusion_reasons must be a string list")
        _validated_optional_uuid(payload["supersedes_queue_id"], "supersedes_queue_id")
        skipped = payload["suggestions_skipped"]
        if skipped is not None and (
            not isinstance(skipped, int) or isinstance(skipped, bool) or skipped < 0
        ):
            raise DecisionValidationError(
                "suggestions_skipped must be a non-negative integer"
            )
        return
    _validated_payload_uuid(payload["assignment_id"], "assignment_id")
    _validated_payload_uuid(payload["reviewer_id"], "reviewer_id")
    if event_type in {"screening.assigned", "screening.unassigned"}:
        return
    observation = _validated_payload_uuid(payload["observation_id"], "observation_id")
    _validated_payload_uuid(payload["report_id"], "report_id")
    if payload["decision"] not in screening_rules.DECISIONS:
        raise DecisionValidationError("screening decision is invalid")
    reason = payload["exclusion_reason"]
    if reason is not None and not isinstance(reason, str):
        raise DecisionValidationError("exclusion_reason must be a string")
    if event_type == "screening.superseded" and observation == (
        _validated_payload_uuid(
            payload["superseded_observation_id"], "superseded_observation_id"
        )
    ):
        raise DecisionValidationError("observation cannot supersede itself")


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


# Public name: services take the aggregate lock before mutating state that the
# appended event will describe (e.g. the per-project research_identity stream).
lock_aggregate_stream = _locked_stream


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
    subject_version_hash: Callable[[UUID], Awaitable[str | None]] | None = None,
) -> Sequence[ResearchDecisionEvent]:
    """Read and structurally validate complete ordered aggregate history.

    ``subject_version_hash`` is required for families whose events name a
    subject version (protocols); identity events verify ``subject_hash``
    against the stored payload instead.
    """
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
    family = _FAMILIES.get(aggregate_type)
    if family is None:
        raise DecisionReplayError("decision aggregate or subject type is invalid")
    if family.requires_subject_version and subject_version_hash is None:
        raise ValueError("subject_version_hash is required for this aggregate")
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
        if not family.requires_subject_version:
            stored_hash: str | None = decision_request_fingerprint(
                cast(dict[str, Any], event.payload)
            )
        else:
            assert subject_version_hash is not None  # checked above
            version_id = cast(UUID, event.subject_version_id)
            stored_hash = await subject_version_hash(version_id)
            if stored_hash is None:
                raise DecisionReplayError("decision subject version is missing")
        if stored_hash != event.subject_hash:
            raise DecisionReplayError("decision subject hash no longer matches")
    family.validate_transitions(events, aggregate_id)
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


def _validate_identity_transitions(
    events: Sequence[ResearchDecisionEvent], aggregate_id: UUID
) -> None:
    """Study links chain through prior state; merged reports are terminal.

    A merge carries the first loser's study link onto a survivor without one,
    exactly as the identity service applies it.
    """
    links: dict[UUID, tuple[UUID | None, str | None]] = {}
    terminal: set[UUID] = set()

    def live(value: Any, field: str) -> UUID:
        report = _payload_uuid(value, field)
        if report in terminal:
            raise DecisionReplayError("merged report is terminal")
        return report

    for event in events:
        payload = cast(dict[str, Any], event.payload)
        if _payload_uuid(payload["collection_id"], "collection_id") != aggregate_id:
            raise DecisionReplayError("decision payload belongs to another collection")
        if event.event_type == "identity.study_linked":
            report = live(payload["report_id"], "report_id")
            prior_value = payload["prior_study_id"]
            prior = (
                None
                if prior_value is None
                else _payload_uuid(prior_value, "prior_study_id")
            )
            if (prior, payload["prior_status"]) != links.get(report, (None, None)):
                raise DecisionReplayError("contradictory study link")
            links[report] = (
                _payload_uuid(payload["study_id"], "study_id"),
                cast(str, payload["status"]),
            )
        elif event.event_type == "identity.report_merged":
            survivor = live(payload["surviving_report_id"], "surviving_report_id")
            for value in payload["merged_report_ids"]:
                loser = live(value, "merged_report_ids")
                if survivor not in links and loser in links:
                    links[survivor] = links[loser]
                terminal.add(loser)
        elif event.event_type == "identity.report_split":
            live(payload["source_report_id"], "source_report_id")
            live(payload["new_report_id"], "new_report_id")


def _validate_screening_transitions(
    events: Sequence[ResearchDecisionEvent], aggregate_id: UUID
) -> None:
    """Self-contained queue replay: event 1 carries the corpus and the reasons.

    Assignments must be active for their reviewer; an initial observation needs
    no current one, and a supersession must name the current one.
    """
    created: dict[str, Any] | None = None
    corpus: set[UUID] = set()
    active: dict[UUID, UUID] = {}  # assignment -> reviewer
    current: dict[tuple[UUID, UUID], UUID] = {}  # (reviewer, report) -> observation
    for event in events:
        payload = cast(dict[str, Any], event.payload)
        if _payload_uuid(payload["queue_id"], "queue_id") != aggregate_id:
            raise DecisionReplayError("decision payload belongs to another queue")
        if (event.event_type == "screening.queue_created") != (created is None):
            raise DecisionReplayError("screening stream must open with queue_created")
        if created is None:
            created = payload
            corpus = {_payload_uuid(r, "report_ids") for r in payload["report_ids"]}
            continue
        if payload["collection_id"] != created["collection_id"]:
            raise DecisionReplayError("decision payload belongs to another collection")
        assignment = _payload_uuid(payload["assignment_id"], "assignment_id")
        reviewer = _payload_uuid(payload["reviewer_id"], "reviewer_id")
        if event.event_type == "screening.assigned":
            if reviewer in active.values() or assignment in active:
                raise DecisionReplayError("contradictory screening assignment")
            active[assignment] = reviewer
            continue
        if active.get(assignment) != reviewer:
            raise DecisionReplayError("screening action without an active assignment")
        if event.event_type == "screening.unassigned":
            del active[assignment]
            continue
        if event.actor_user_id != reviewer:
            raise DecisionReplayError("screening observation actor is not its reviewer")
        report = _payload_uuid(payload["report_id"], "report_id")
        if report not in corpus:
            raise DecisionReplayError("screening observation outside corpus")
        try:
            screening_rules.validate_observation(
                created["stage"],
                payload["decision"],
                payload["exclusion_reason"],
                created["exclusion_reasons"],
            )
        except ValueError as error:
            raise DecisionReplayError(
                "screening observation violates protocol criteria"
            ) from error
        key = (reviewer, report)
        if event.event_type == "screening.observed":
            if key in current:
                raise DecisionReplayError("contradictory initial screening observation")
        elif current.get(key) != _payload_uuid(
            payload["superseded_observation_id"], "superseded_observation_id"
        ):
            raise DecisionReplayError("contradictory screening supersession")
        current[key] = _payload_uuid(payload["observation_id"], "observation_id")


_FAMILIES: dict[str, _Family] = {
    _PROTOCOL_AGGREGATE: _Family(
        subject_type=_PROTOCOL_SUBJECT,
        payload_keys=_EVENT_PAYLOAD_KEYS,
        validate_payload=_validate_protocol_payload,
        validate_transitions=_validate_protocol_transitions,
        requires_subject_version=True,
    ),
    _IDENTITY_AGGREGATE: _Family(
        subject_type=_IDENTITY_SUBJECT,
        payload_keys=_IDENTITY_PAYLOAD_KEYS,
        validate_payload=_validate_identity_payload,
        validate_transitions=_validate_identity_transitions,
        requires_subject_version=False,
    ),
    _SCREENING_AGGREGATE: _Family(
        subject_type=_SCREENING_SUBJECT,
        payload_keys=_SCREENING_PAYLOAD_KEYS,
        validate_payload=_validate_screening_payload,
        validate_transitions=_validate_screening_transitions,
        requires_subject_version=False,
    ),
}

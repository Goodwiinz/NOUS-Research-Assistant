"""Caller-owned append and replay operations for research decisions.

Each aggregate family (``research_protocol``, ``research_identity``,
``research_screening``, ``research_acquisition``, ``research_extraction``,
``research_claims``, ``research_release``) registers
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
from datetime import date
from typing import Any, Awaitable, Callable, Mapping, Sequence, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.services.research import claim_rules, extraction_rules
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
    # GOO-302: human adjudicator events; the rationale is the event reason.
    ("screening.adjudicated", 1): frozenset(
        {
            "collection_id",
            "queue_id",
            "report_id",
            "resolution_id",
            "conflict_resolution_id",
            "input_observation_ids",
            "criteria_hash",
            "decision",
            "exclusion_reason",
        }
    ),
    ("screening.reopened", 1): frozenset(
        {
            "collection_id",
            "queue_id",
            "report_id",
            "resolution_id",
            "reopened_resolution_id",
        }
    ),
}
_SCREENING_RATIONALE_EVENTS = frozenset({"screening.adjudicated", "screening.reopened"})

# GOO-303: full-text acquisition, one stream per Collection (like identity).
_ACQUISITION_AGGREGATE = "research_acquisition"
_ACQUISITION_ATTEMPT_KEYS = frozenset(
    {
        "collection_id",
        "request_id",
        "report_id",
        "attempt_id",
        "previous_attempt_id",
        "attempted_on",
        "reason",
    }
)
_ACQUISITION_PAYLOAD_KEYS: dict[tuple[str, int], frozenset[str]] = {
    ("acquisition.requested", 1): frozenset(
        {"collection_id", "request_id", "report_id", "protocol_version_id"}
    ),
    # Outcome "requested": asked (ILL, author email) and awaiting a response.
    ("acquisition.attempted", 1): _ACQUISITION_ATTEMPT_KEYS,
    ("acquisition.unavailable", 1): _ACQUISITION_ATTEMPT_KEYS,
    ("acquisition.retrieved", 1): _ACQUISITION_ATTEMPT_KEYS
    | {"document_id", "document_content_hash"},
}

# GOO-304: extraction forms, one stream per matrix. The rationale is the reason.
_EXTRACTION_AGGREGATE = "research_extraction"
_EXTRACTION_SUBJECT = "extraction_matrix"
_EXTRACTION_PAYLOAD_KEYS: dict[tuple[str, int], frozenset[str]] = {
    ("extraction.observed", 1): frozenset(
        {
            "collection_id",
            "matrix_id",
            "form_version_id",
            "form_content_hash",
            "document_id",
            "source_hash",
            "kind",
            "observations",  # {observation_id: field_id}
            "extractor_run_id",
            "extractor_model",
        }
    ),
    ("extraction.accepted", 1): frozenset(
        {
            "collection_id",
            "matrix_id",
            "accepted_value_id",
            "form_version_id",
            "document_id",
            "field_id",
            "observation_ids",
            "value",
            "missingness",
            "supersedes_accepted_value_id",
            "source_hash",
        }
    ),
    ("extraction.staled", 1): frozenset(
        {"collection_id", "matrix_id", "new_form_version_id", "accepted_value_ids"}
    ),
}
# GOO-305: source anchors as additive v2 payloads (v1 history still replays).
# observed v2: each observations{} value is an _ANCHOR_KEYS object; the quote
# stays on the row and is bound here by citation_sha256.
_ANCHOR_KEYS = frozenset(
    {
        "field_id",
        "anchor_status",
        "citation_sha256",
        "start",
        "end",
        "page",
        "occurrences",
    }
)
_ANCHOR_STATUSES = ("verified", "ambiguous", "unverified", "location_unavailable")
_ANCHOR_RESOLUTIONS = (
    "verified",
    "disambiguated",
    "accepted_unverified",
    "not_applicable",
)
_STALE_REASONS = ("form_changed", "source_changed")
_EXTRACTION_PAYLOAD_KEYS |= {
    ("extraction.observed", 2): _EXTRACTION_PAYLOAD_KEYS[("extraction.observed", 1)]
    | {"text_sha256", "inspected_coverage"},
    ("extraction.accepted", 2): _EXTRACTION_PAYLOAD_KEYS[("extraction.accepted", 1)]
    | {
        "anchor_observation_id",
        "anchor_resolution",
        "anchor_start_char",
        "text_sha256",
    },
    ("extraction.staled", 2): _EXTRACTION_PAYLOAD_KEYS[("extraction.staled", 1)]
    | {"reason", "document_id", "new_source_hash", "new_text_sha256"},
}

# GOO-306: versioned claims, one stream per Collection (so creation is
# idempotent too); the subject is the claim. The rationale is the reason.
_CLAIMS_AGGREGATE = "research_claims"
_CLAIMS_SUBJECT = "research_claim"
_CLAIMS_PAYLOAD_KEYS: dict[tuple[str, int], frozenset[str]] = {
    ("claim.versioned", 1): frozenset(
        {
            "collection_id",
            "claim_id",
            "claim_version_id",
            "version_no",
            "supersedes_claim_version_id",
            "kind",
            "attributed_to_user_id",
            "text_sha256",
            "normalized_hash",
            "draft_id",
            "draft_version",
            "draft_content_hash",
            "start_char",
            "end_char",
            "draft_review_id",
        }
    ),
    ("claim.linked", 1): frozenset(
        {
            "collection_id",
            "claim_id",
            "claim_version_id",
            "link_id",
            "supersedes_link_id",
            "status",
            "kind",
            "accepted_value_id",
            "draft_citation_id",
            "document_id",
            "source_hash",
            "text_sha256",
            "start_char",
            "end_char",
            "quote_sha256",
        }
    ),
    ("claim.observed", 1): frozenset(
        {
            "collection_id",
            "claim_id",
            "link_id",
            "observation_id",
            "stance",
            "stance_classification_id",
            "classifier_version",
            "inference_model_version",
            "source_content_hash",
            "classified_at",
        }
    ),
    ("claim.assessed", 1): frozenset(
        {
            "collection_id",
            "claim_id",
            "claim_version_id",
            "assessment_id",
            "supersedes_assessment_id",
            "stance",
            "link_ids",
            "stance_observation_ids",
        }
    ),
}
# GOO-307: verified draft releases, one stream per Collection so promotion
# and cross-draft invalidation share one total order. A promotion's subject is
# its release; a staling's subject is the Collection.
_RELEASE_AGGREGATE = "research_release"
_RELEASE_SUBJECT = "draft_release"
_RELEASE_PROMOTERS = frozenset({"adjudicator", "supervisor"})
_RELEASE_CAUSE_FAMILIES = frozenset(
    {"research_extraction", "research_claims", "research_release"}
)
_RELEASE_PAYLOAD_KEYS: dict[tuple[str, int], frozenset[str]] = {
    ("release.promoted", 1): frozenset(
        {
            "collection_id",
            "release_id",
            "draft_id",
            "draft_version",
            "content_hash",
            "claim_version_ids",
            "assessment_ids",
            "interpretation_claim_version_ids",
            "protocol_version_id",
            "policy_version",
            "dimensions",
        }
    ),
    ("release.staled", 1): frozenset(
        {"collection_id", "release_ids", "cause", "changed_nodes", "assessment_ids"}
    ),
}
_RATIONALE_EVENTS = _SCREENING_RATIONALE_EVENTS | {
    "extraction.accepted",
    "claim.assessed",
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
    reason: str | None = None,
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
    if event_type in _RATIONALE_EVENTS and not reason:
        raise DecisionValidationError("adjudicator event needs a rationale")
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
    if event_type in _SCREENING_RATIONALE_EVENTS:
        _validated_payload_uuid(payload["report_id"], "report_id")
        resolution = _validated_payload_uuid(payload["resolution_id"], "resolution_id")
        prior_field = (
            "conflict_resolution_id"
            if event_type == "screening.adjudicated"
            else "reopened_resolution_id"
        )
        if resolution == _validated_payload_uuid(payload[prior_field], prior_field):
            raise DecisionValidationError("resolution cannot supersede itself")
        if event_type == "screening.reopened":
            return
        inputs = _validated_uuid_list(
            payload["input_observation_ids"], "input_observation_ids"
        )
        if not inputs or len(set(inputs)) != len(inputs):
            raise DecisionValidationError(
                "input_observation_ids must be non-empty and unique"
            )
        criteria = payload["criteria_hash"]
        if not isinstance(criteria, str) or not _SHA256_RE.fullmatch(criteria):
            raise DecisionValidationError(
                "criteria_hash must be a lowercase SHA-256 digest"
            )
        if payload["decision"] not in screening_rules.DECISIONS:
            raise DecisionValidationError("screening decision is invalid")
        reason = payload["exclusion_reason"]
        if reason is not None and not isinstance(reason, str):
            raise DecisionValidationError("exclusion_reason must be a string")
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


def _validate_acquisition_payload(
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
    if _validated_payload_uuid(payload["report_id"], "report_id") != subject_id:
        raise DecisionValidationError("acquisition event subject is not its report")
    _validated_payload_uuid(payload["request_id"], "request_id")
    if event_type == "acquisition.requested":
        _validated_optional_uuid(payload["protocol_version_id"], "protocol_version_id")
        return
    attempt = _validated_payload_uuid(payload["attempt_id"], "attempt_id")
    if attempt == _validated_optional_uuid(
        payload["previous_attempt_id"], "previous_attempt_id"
    ):
        raise DecisionValidationError("attempt cannot follow itself")
    try:
        date.fromisoformat(payload["attempted_on"])
    except (TypeError, ValueError) as error:
        raise DecisionValidationError("attempted_on must be an ISO date") from error
    reason = payload["reason"]
    if reason is not None and not isinstance(reason, str):
        raise DecisionValidationError("acquisition reason must be a string")
    if event_type == "acquisition.unavailable" and not (reason or "").strip():
        raise DecisionValidationError("unavailable full text needs a reason")
    if event_type == "acquisition.retrieved":
        _validated_payload_uuid(payload["document_id"], "document_id")
        content_hash = payload["document_content_hash"]
        if not isinstance(content_hash, str) or not _SHA256_RE.fullmatch(content_hash):
            raise DecisionValidationError(
                "document_content_hash must be a lowercase SHA-256 digest"
            )


def _validated_sha256(value: Any, field: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise DecisionValidationError(f"{field} must be a lowercase SHA-256 digest")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validated_span(start: Any, end: Any, field: str) -> None:
    if start is None and end is None:
        return
    if not (_is_int(start) and _is_int(end) and 0 <= start < end):
        raise DecisionValidationError(f"{field} offsets must be 0 <= start < end")


def _validate_anchor(anchor: Any) -> None:
    if not isinstance(anchor, Mapping) or set(anchor) != _ANCHOR_KEYS:
        raise DecisionValidationError("observation anchor does not match its schema")
    _validated_payload_uuid(anchor["field_id"], "observations")
    status = anchor["anchor_status"]
    if status is not None and status not in _ANCHOR_STATUSES:
        raise DecisionValidationError("anchor_status is invalid")
    if anchor["citation_sha256"] is not None:
        _validated_sha256(anchor["citation_sha256"], "citation_sha256")
    _validated_span(anchor["start"], anchor["end"], "anchor")
    if (anchor["start"] is not None) != (status == "verified"):
        raise DecisionValidationError("only a verified anchor carries offsets")
    if anchor["page"] is not None and not _is_int(anchor["page"]):
        raise DecisionValidationError("anchor page must be an integer")
    occurrences = anchor["occurrences"]
    if (
        not isinstance(occurrences, list)
        or len(occurrences) > 20
        or not all(_is_int(o) and o >= 0 for o in occurrences)
    ):
        raise DecisionValidationError("anchor occurrences must be <=20 offsets")


def _validate_coverage(coverage: Any) -> None:
    if coverage is None:
        return
    if not isinstance(coverage, list):
        raise DecisionValidationError("inspected_coverage must be a list")
    previous_end = -1
    for pair in coverage:
        if not isinstance(pair, list) or len(pair) != 2:
            raise DecisionValidationError("inspected_coverage holds [start, end] pairs")
        _validated_span(pair[0], pair[1], "inspected_coverage")
        if pair[0] <= previous_end:
            raise DecisionValidationError(
                "inspected_coverage must be sorted and non-overlapping"
            )
        previous_end = pair[1]


def _validate_staled_v2(payload: Mapping[str, Any]) -> None:
    reason = payload["reason"]
    if reason not in _STALE_REASONS:
        raise DecisionValidationError("staled reason is invalid")
    source = (
        payload["document_id"],
        payload["new_source_hash"],
        payload["new_text_sha256"],
    )
    if reason == "form_changed":
        _validated_payload_uuid(payload["new_form_version_id"], "new_form_version_id")
        if source != (None, None, None):
            raise DecisionValidationError("form staling names no source")
        return
    if payload["new_form_version_id"] is not None:
        raise DecisionValidationError("source staling names no form version")
    _validated_payload_uuid(payload["document_id"], "document_id")
    _validated_sha256(payload["new_source_hash"], "new_source_hash")
    _validated_sha256(payload["new_text_sha256"], "new_text_sha256")


def _validate_accepted_v2(payload: Mapping[str, Any]) -> None:
    resolution = payload["anchor_resolution"]
    if resolution not in _ANCHOR_RESOLUTIONS:
        raise DecisionValidationError("anchor_resolution is invalid")
    anchor = _validated_optional_uuid(
        payload["anchor_observation_id"], "anchor_observation_id"
    )
    start = payload["anchor_start_char"]
    if start is not None and not (_is_int(start) and start >= 0):
        raise DecisionValidationError("anchor_start_char must be an offset")
    if resolution in ("verified", "disambiguated") and (
        anchor is None or start is None
    ):
        raise DecisionValidationError("a located acceptance names its anchor")
    if anchor is not None and str(anchor) not in payload["observation_ids"]:
        raise DecisionValidationError("anchor observation must be cited")
    if payload["text_sha256"] is not None:
        _validated_sha256(payload["text_sha256"], "text_sha256")


def _validate_extraction_payload(
    event_type: str,
    payload: Mapping[str, Any],
    aggregate_id: UUID,
    subject_version_id: UUID | None,
    subject_id: UUID,
) -> None:
    _validated_payload_uuid(payload["collection_id"], "collection_id")
    matrix_id = _validated_payload_uuid(payload["matrix_id"], "matrix_id")
    if not matrix_id == aggregate_id == subject_id:
        raise DecisionValidationError(
            "decision payload belongs to another extraction matrix"
        )
    # The exact key set was checked per (type, version), so a v2 key marks v2.
    if event_type == "extraction.staled":
        if "reason" in payload:
            _validate_staled_v2(payload)
        else:
            _validated_payload_uuid(
                payload["new_form_version_id"], "new_form_version_id"
            )
        staled = _validated_uuid_list(
            payload["accepted_value_ids"], "accepted_value_ids"
        )
        if not staled or len(set(staled)) != len(staled):
            raise DecisionValidationError(
                "accepted_value_ids must be non-empty and unique"
            )
        return
    _validated_payload_uuid(payload["form_version_id"], "form_version_id")
    _validated_payload_uuid(payload["document_id"], "document_id")
    _validated_sha256(payload["source_hash"], "source_hash")
    if event_type == "extraction.observed":
        _validated_sha256(payload["form_content_hash"], "form_content_hash")
        if payload["kind"] not in ("machine", "human"):
            raise DecisionValidationError("extraction observation kind is invalid")
        observations = payload["observations"]
        if not isinstance(observations, Mapping) or not observations:
            raise DecisionValidationError("observations must be a non-empty object")
        v2 = "text_sha256" in payload
        for observation, field in observations.items():
            _validated_payload_uuid(observation, "observations")
            if v2:
                _validate_anchor(field)
            else:
                _validated_payload_uuid(field, "observations")
        if v2:
            _validated_sha256(payload["text_sha256"], "text_sha256")
            _validate_coverage(payload["inspected_coverage"])
        for key in ("extractor_run_id", "extractor_model"):
            if payload[key] is not None and not isinstance(payload[key], str):
                raise DecisionValidationError(f"{key} must be a string")
        return
    accepted = _validated_payload_uuid(
        payload["accepted_value_id"], "accepted_value_id"
    )
    if accepted == _validated_optional_uuid(
        payload["supersedes_accepted_value_id"], "supersedes_accepted_value_id"
    ):
        raise DecisionValidationError("accepted value cannot supersede itself")
    _validated_payload_uuid(payload["field_id"], "field_id")
    cited = _validated_uuid_list(payload["observation_ids"], "observation_ids")
    if not 1 <= len(cited) <= 20 or len(set(cited)) != len(cited):
        raise DecisionValidationError("observation_ids must be 1-20 unique ids")
    missingness = payload["missingness"]
    if (payload["value"] is None) == (missingness is None):
        raise DecisionValidationError("accepted needs exactly one of value/missingness")
    if missingness is not None and (
        missingness not in extraction_rules.MISSINGNESS["accepted"]
    ):
        raise DecisionValidationError("accepted missingness is invalid")
    if "anchor_resolution" in payload:
        _validate_accepted_v2(payload)


def _validated_optional_sha256(value: Any, field: str) -> None:
    if value is not None:
        _validated_sha256(value, field)


def _validate_claims_payload(
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
    if _validated_payload_uuid(payload["claim_id"], "claim_id") != subject_id:
        raise DecisionValidationError("claim event subject is not its claim")
    if event_type == "claim.versioned":
        version = _validated_payload_uuid(
            payload["claim_version_id"], "claim_version_id"
        )
        supersedes = _validated_optional_uuid(
            payload["supersedes_claim_version_id"], "supersedes_claim_version_id"
        )
        number = payload["version_no"]
        if not _is_int(number) or number < 1:
            raise DecisionValidationError("version_no must be a positive integer")
        if version == supersedes or (supersedes is None) != (number == 1):
            raise DecisionValidationError("claim version chain is invalid")
        if payload["kind"] not in claim_rules.KINDS:
            raise DecisionValidationError("claim kind is invalid")
        attributed = _validated_optional_uuid(
            payload["attributed_to_user_id"], "attributed_to_user_id"
        )
        if (payload["kind"] == "interpretation") != (attributed is not None):
            raise DecisionValidationError("only an interpretation is attributed")
        for key in ("text_sha256", "normalized_hash", "draft_content_hash"):
            _validated_sha256(payload[key], key)
        _validated_payload_uuid(payload["draft_id"], "draft_id")
        if not _is_int(payload["draft_version"]) or payload["draft_version"] < 1:
            raise DecisionValidationError("draft_version must be a positive integer")
        if payload["start_char"] is None:
            raise DecisionValidationError("claim passage needs offsets")
        _validated_span(payload["start_char"], payload["end_char"], "passage")
        _validated_optional_uuid(payload["draft_review_id"], "draft_review_id")
        return
    if event_type == "claim.linked":
        _validated_payload_uuid(payload["claim_version_id"], "claim_version_id")
        link = _validated_payload_uuid(payload["link_id"], "link_id")
        supersedes = _validated_optional_uuid(
            payload["supersedes_link_id"], "supersedes_link_id"
        )
        if link == supersedes:
            raise DecisionValidationError("link cannot supersede itself")
        _validated_span(payload["start_char"], payload["end_char"], "span")
        for key in ("source_hash", "text_sha256", "quote_sha256"):
            _validated_optional_sha256(payload[key], key)
        try:
            claim_rules.check_link_shape(
                payload["kind"],
                accepted_value_id=_validated_optional_uuid(
                    payload["accepted_value_id"], "accepted_value_id"
                ),
                draft_citation_id=_validated_optional_uuid(
                    payload["draft_citation_id"], "draft_citation_id"
                ),
                document_id=_validated_optional_uuid(
                    payload["document_id"], "document_id"
                ),
                source_hash=payload["source_hash"],
                text_sha256=payload["text_sha256"],
                start_char=payload["start_char"],
                end_char=payload["end_char"],
                quote=payload["quote_sha256"],
                status=payload["status"],
                supersedes_link_id=supersedes,
            )
        except ValueError as error:
            raise DecisionValidationError(f"claim link: {error}") from error
        return
    if event_type == "claim.observed":
        _validated_payload_uuid(payload["link_id"], "link_id")
        _validated_payload_uuid(payload["observation_id"], "observation_id")
        if payload["stance"] not in claim_rules.OBSERVED_STANCES:
            raise DecisionValidationError("observed stance is invalid")
        _validated_optional_uuid(
            payload["stance_classification_id"], "stance_classification_id"
        )
        if not isinstance(payload["classifier_version"], str):
            raise DecisionValidationError("classifier_version must be a string")
        model = payload["inference_model_version"]
        if model is not None and not isinstance(model, str):
            raise DecisionValidationError("inference_model_version must be a string")
        _validated_sha256(payload["source_content_hash"], "source_content_hash")
        classified = payload["classified_at"]
        if classified is not None and not isinstance(classified, str):
            raise DecisionValidationError("classified_at must be an ISO timestamp")
        return
    _validated_payload_uuid(payload["claim_version_id"], "claim_version_id")
    assessment = _validated_payload_uuid(payload["assessment_id"], "assessment_id")
    if assessment == _validated_optional_uuid(
        payload["supersedes_assessment_id"], "supersedes_assessment_id"
    ):
        raise DecisionValidationError("assessment cannot supersede itself")
    if payload["stance"] not in claim_rules.STANCES:
        raise DecisionValidationError("assessed stance is invalid")
    for key in ("link_ids", "stance_observation_ids"):
        ids = _validated_uuid_list(payload[key], key)
        if len(ids) > 20 or len(set(ids)) != len(ids):
            raise DecisionValidationError(f"{key} must be 0-20 unique ids")
    if not payload["link_ids"] and payload["stance"] not in claim_rules.UNCITED_STANCES:
        raise DecisionValidationError("assessed stance must cite a link")


def _validate_release_payload(
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
    if event_type == "release.promoted":
        release = _validated_payload_uuid(payload["release_id"], "release_id")
        if release != subject_id:
            raise DecisionValidationError("release event subject is not its release")
        _validated_payload_uuid(payload["draft_id"], "draft_id")
        for key in ("draft_version", "policy_version"):
            if not _is_int(payload[key]) or payload[key] < 1:
                raise DecisionValidationError(f"{key} must be a positive integer")
        _validated_sha256(payload["content_hash"], "content_hash")
        for key in (
            "claim_version_ids",
            "assessment_ids",
            "interpretation_claim_version_ids",
        ):
            ids = _validated_uuid_list(payload[key], key)
            if len(set(ids)) != len(ids):
                raise DecisionValidationError(f"{key} must be unique")
        _validated_optional_uuid(payload["protocol_version_id"], "protocol_version_id")
        if not isinstance(payload["dimensions"], dict):
            raise DecisionValidationError("dimensions must be an object")
        return
    if subject_id != aggregate_id:
        raise DecisionValidationError("release staling subject is its collection")
    releases = _validated_uuid_list(payload["release_ids"], "release_ids")
    if not releases or len(set(releases)) != len(releases):
        raise DecisionValidationError("release_ids must be 1+ unique ids")
    _validated_uuid_list(payload["assessment_ids"], "assessment_ids")
    cause = payload["cause"]
    if (
        not isinstance(cause, dict)
        or set(cause) != {"family", "event_id", "kind"}
        or cause["family"] not in _RELEASE_CAUSE_FAMILIES
        or not isinstance(cause["kind"], str)
    ):
        raise DecisionValidationError("release staling cause is invalid")
    _validated_optional_uuid(cause["event_id"], "cause.event_id")
    nodes = payload["changed_nodes"]
    if not isinstance(nodes, list) or not nodes:
        raise DecisionValidationError("changed_nodes must be a non-empty list")
    if not all(isinstance(n, str) and ":" in n for n in nodes):
        raise DecisionValidationError("changed_nodes must be kind:id strings")


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
    event_id: UUID | None = None,
) -> AppendDecisionResult:
    """Append one event under a stream lock without ending the transaction.

    ``event_id`` lets a caller stamp rows with the event's id before the
    append (GOO-307 ``release.staled``); by default a new id is drawn."""
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
        reason=reason,
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
        id=event_id or uuid4(),
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
                reason=cast(str | None, event.reason),
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


@dataclass(frozen=True)
class ReplayedResolution:
    """One ``screening_resolutions`` row as replay derives it from events."""

    id: UUID
    event_id: UUID
    report_id: UUID
    basis: str
    outcome: str | None
    exclusion_reason: str | None
    input_observation_ids: list[str]  # sorted
    supersedes_resolution_id: UUID | None
    criteria_hash: str


def replay_screening_resolutions(
    events: Sequence[ResearchDecisionEvent], aggregate_id: UUID
) -> list[ReplayedResolution]:
    """Self-contained queue replay: event 1 carries the corpus, reasons and mode.

    Assignments must be active for their reviewer; an initial observation needs
    no current one, and a supersession must name the current one. After each
    observation ``screening_rules.derive`` runs over the report's fresh
    observations (current and not an input of an earlier resolution), exactly
    as the service does, so every automatic resolution is re-derived here. The
    resolutions come back in event order; the service compares them with the
    stored rows.
    """
    created: dict[str, Any] | None = None
    corpus: set[UUID] = set()
    active: dict[UUID, UUID] = {}  # assignment -> reviewer
    current: dict[tuple[UUID, UUID], UUID] = {}  # (reviewer, report) -> observation
    seen: dict[UUID, screening_rules.Obs] = {}  # observation -> as derive sees it
    consumed: set[str] = set()  # observation ids already input to a resolution
    tips: dict[UUID, ReplayedResolution] = {}  # report -> current resolution
    # report -> every reviewer who ever observed it (any cycle, superseded too)
    reviewed: dict[UUID, set[UUID]] = {}
    resolutions: list[ReplayedResolution] = []

    def criteria(decision: Any, reason: Any) -> None:
        assert created is not None
        try:
            screening_rules.validate_observation(
                created["stage"], decision, reason, created["exclusion_reasons"]
            )
        except ValueError as error:
            raise DecisionReplayError(
                "screening observation violates protocol criteria"
            ) from error

    def resolve(resolution: ReplayedResolution) -> None:
        if any(r.id == resolution.id for r in resolutions):
            raise DecisionReplayError("screening resolution id reused")
        tips[resolution.report_id] = resolution
        consumed.update(resolution.input_observation_ids)
        resolutions.append(resolution)

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
        if event.event_type in _SCREENING_RATIONALE_EVENTS:
            report = _payload_uuid(payload["report_id"], "report_id")
            if report not in corpus:
                raise DecisionReplayError("screening resolution outside corpus")
            tip = tips.get(report)
            new_id = _payload_uuid(payload["resolution_id"], "resolution_id")
            if event.event_type == "screening.reopened":
                if (
                    tip is None
                    or tip.basis == "reopened"
                    or tip.id
                    != _payload_uuid(
                        payload["reopened_resolution_id"], "reopened_resolution_id"
                    )
                ):
                    raise DecisionReplayError("screening report is not resolved")
                resolve(
                    ReplayedResolution(
                        new_id,
                        event.id,
                        report,
                        "reopened",
                        None,
                        None,
                        [],
                        tip.id,
                        created["criteria_hash"],
                    )
                )
                continue
            if tip is None or tip.basis != "conflict":
                raise DecisionReplayError("screening report is not in conflict")
            inputs = sorted(str(i) for i in payload["input_observation_ids"])
            if (
                _payload_uuid(
                    payload["conflict_resolution_id"], "conflict_resolution_id"
                )
                != tip.id
                or inputs != tip.input_observation_ids
            ):
                raise DecisionReplayError("screening adjudication inputs are stale")
            if event.actor_user_id in reviewed.get(report, set()):
                raise DecisionReplayError("screening adjudicator reviewed this report")
            if payload["criteria_hash"] != created["criteria_hash"]:
                raise DecisionReplayError("screening adjudication criteria changed")
            criteria(payload["decision"], payload["exclusion_reason"])
            resolve(
                ReplayedResolution(
                    new_id,
                    event.id,
                    report,
                    "adjudicated",
                    payload["decision"],
                    payload["exclusion_reason"],
                    inputs,
                    tip.id,
                    payload["criteria_hash"],
                )
            )
            continue
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
        criteria(payload["decision"], payload["exclusion_reason"])
        tip = tips.get(report)
        if tip is not None and tip.basis != "reopened":
            raise DecisionReplayError("screening observation after resolution")
        key = (reviewer, report)
        if event.event_type == "screening.observed":
            if key in current:
                raise DecisionReplayError("contradictory initial screening observation")
        elif current.get(key) != _payload_uuid(
            payload["superseded_observation_id"], "superseded_observation_id"
        ):
            raise DecisionReplayError("contradictory screening supersession")
        observation = _payload_uuid(payload["observation_id"], "observation_id")
        current[key] = observation
        reviewed.setdefault(report, set()).add(reviewer)
        seen[observation] = screening_rules.Obs(
            observation, reviewer, payload["decision"], payload["exclusion_reason"]
        )
        derived = screening_rules.derive(
            created["reviewer_mode"],
            [
                seen[oid]
                for (_, rep), oid in current.items()
                if rep == report and str(oid) not in consumed
            ],
        )
        if derived is not None:
            resolve(
                ReplayedResolution(
                    screening_rules.auto_resolution_id(cast(UUID, event.id)),
                    cast(UUID, event.id),
                    report,
                    derived.basis,
                    derived.outcome,
                    derived.exclusion_reason,
                    derived.input_observation_ids,
                    None if tip is None else tip.id,
                    created["criteria_hash"],
                )
            )
    return resolutions


def _validate_screening_transitions(
    events: Sequence[ResearchDecisionEvent], aggregate_id: UUID
) -> None:
    replay_screening_resolutions(events, aggregate_id)


def _validate_acquisition_transitions(
    events: Sequence[ResearchDecisionEvent], aggregate_id: UUID
) -> None:
    """One request per report; each request's attempts form one linear chain
    (``previous_attempt_id`` names the current head) that ends at retrieval."""
    request_of: dict[UUID, UUID] = {}  # report -> request
    report_of: dict[UUID, UUID] = {}  # request -> report
    heads: dict[UUID, UUID | None] = {}  # request -> head attempt
    attempts: set[UUID] = set()
    retrieved: set[UUID] = set()
    for event in events:
        payload = cast(dict[str, Any], event.payload)
        if _payload_uuid(payload["collection_id"], "collection_id") != aggregate_id:
            raise DecisionReplayError("decision payload belongs to another collection")
        report = _payload_uuid(payload["report_id"], "report_id")
        request = _payload_uuid(payload["request_id"], "request_id")
        if event.event_type == "acquisition.requested":
            if report in request_of or request in report_of:
                raise DecisionReplayError("contradictory acquisition chain")
            request_of[report], report_of[request], heads[request] = (
                request,
                report,
                None,
            )
            continue
        if report_of.get(request) != report:
            raise DecisionReplayError("contradictory acquisition chain")
        if request in retrieved:
            raise DecisionReplayError("acquisition after retrieval")
        attempt = _payload_uuid(payload["attempt_id"], "attempt_id")
        previous_value = payload["previous_attempt_id"]
        previous = (
            None
            if previous_value is None
            else _payload_uuid(previous_value, "previous_attempt_id")
        )
        if previous != heads[request] or attempt in attempts:
            raise DecisionReplayError("contradictory acquisition chain")
        attempts.add(attempt)
        heads[request] = attempt
        if event.event_type == "acquisition.retrieved":
            retrieved.add(request)


def _validate_extraction_transitions(
    events: Sequence[ResearchDecisionEvent], aggregate_id: UUID
) -> None:
    """Machine rows come only from the machine, human rows from a reviewer;
    only an adjudicator accepts, citing observations of that exact cell, and
    each ``(document, field)`` chain names its current tip. ``staled`` names
    current tips only, once, made stale by a different form version (or, v2,
    by a changed source). A v2 accept's anchor resolution must agree with the
    anchor its observation recorded (GOO-305)."""
    collection: Any = None
    observed: dict[UUID, tuple[UUID, UUID, UUID]] = {}  # obs -> (doc, form, field)
    anchors: dict[UUID, Mapping[str, Any]] = {}  # v2 obs -> recorded anchor
    tips: dict[tuple[UUID, UUID], UUID] = {}  # (doc, field) -> accepted tip
    form_of: dict[UUID, UUID] = {}  # accepted -> form version
    source_of: dict[UUID, tuple[UUID, str, str | None]] = {}  # (doc, hash, text)
    staled: set[UUID] = set()
    for event in events:
        payload = cast(dict[str, Any], event.payload)
        if _payload_uuid(payload["matrix_id"], "matrix_id") != aggregate_id:
            raise DecisionReplayError("decision payload belongs to another matrix")
        if collection is None:
            collection = payload["collection_id"]
        elif payload["collection_id"] != collection:
            raise DecisionReplayError("decision payload belongs to another collection")
        role = event.actor_role
        if event.event_type == "extraction.observed":
            extractor = (payload["extractor_run_id"], payload["extractor_model"])
            if payload["kind"] == "machine":
                consistent = role == "machine" and None not in extractor
            else:
                consistent = role == "reviewer" and extractor == (None, None)
            if not consistent:
                raise DecisionReplayError(
                    "extraction observation actor or extractor is inconsistent"
                )
            cell = (
                _payload_uuid(payload["document_id"], "document_id"),
                _payload_uuid(payload["form_version_id"], "form_version_id"),
            )
            for value, field in payload["observations"].items():
                observation = _payload_uuid(value, "observations")
                if observation in observed:
                    raise DecisionReplayError("extraction observation id reused")
                if isinstance(field, Mapping):  # v2
                    anchors[observation] = field
                    field = field["field_id"]
                observed[observation] = (*cell, _payload_uuid(field, "observations"))
            continue
        if event.event_type == "extraction.accepted":
            if role != "adjudicator":
                raise DecisionReplayError(
                    "extraction acceptance requires an adjudicator"
                )
            document = _payload_uuid(payload["document_id"], "document_id")
            form = _payload_uuid(payload["form_version_id"], "form_version_id")
            field = _payload_uuid(payload["field_id"], "field_id")
            for value in payload["observation_ids"]:
                if observed.get(_payload_uuid(value, "observation_ids")) != (
                    document,
                    form,
                    field,
                ):
                    raise DecisionReplayError(
                        "accepted value cites an observation of another cell"
                    )
            supersedes = payload["supersedes_accepted_value_id"]
            if tips.get((document, field)) != (
                None
                if supersedes is None
                else _payload_uuid(supersedes, "supersedes_accepted_value_id")
            ):
                raise DecisionReplayError("forked accepted value chain")
            accepted = _payload_uuid(payload["accepted_value_id"], "accepted_value_id")
            if accepted in form_of:
                raise DecisionReplayError("accepted value id reused")
            if "anchor_resolution" in payload:
                _check_accepted_anchor(payload, anchors)
            tips[(document, field)] = accepted
            form_of[accepted] = form
            source_of[accepted] = (
                document,
                payload["source_hash"],
                payload.get("text_sha256"),
            )
            continue
        current = set(tips.values())
        if payload.get("reason") == "source_changed":
            if role not in ("editor", "adjudicator", "machine"):
                raise DecisionReplayError("extraction staling actor is invalid")
            document = _payload_uuid(payload["document_id"], "document_id")
            new = (payload["new_source_hash"], payload["new_text_sha256"])
            for value in payload["accepted_value_ids"]:
                accepted = _payload_uuid(value, "accepted_value_ids")
                if accepted not in current or accepted in staled:
                    raise DecisionReplayError("staled value is not a current tip")
                tip_document, tip_hash, tip_text = source_of[accepted]
                if tip_document != document or (
                    tip_hash == new[0] and tip_text in (None, new[1])
                ):
                    raise DecisionReplayError("stale without source change")
                staled.add(accepted)
            continue
        if role != "editor":
            raise DecisionReplayError("extraction staling requires an editor")
        new_form = _payload_uuid(payload["new_form_version_id"], "new_form_version_id")
        for value in payload["accepted_value_ids"]:
            accepted = _payload_uuid(value, "accepted_value_ids")
            if (
                accepted not in current
                or accepted in staled
                or form_of[accepted] == new_form
            ):
                raise DecisionReplayError(
                    "staled value is not a current tip made stale by a new form"
                )
            staled.add(accepted)


def _validate_claims_transitions(
    events: Sequence[ResearchDecisionEvent], aggregate_id: UUID
) -> None:
    """Each claim's versions form one chain; links attach only to the claim's
    tip and each link chain names its tip; machine snapshots observe live,
    anchored links of their revision; only an adjudicator assesses a tip
    version, citing its live links and their observations."""
    version_tip: dict[UUID, UUID] = {}  # claim -> tip version
    version_no: dict[UUID, int] = {}  # claim -> tip version_no
    version_claim: dict[UUID, UUID] = {}  # version -> claim
    link_version: dict[UUID, UUID] = {}  # link (any) -> version
    link_row: dict[UUID, Mapping[str, Any]] = {}  # link -> payload
    superseded_links: set[UUID] = set()
    observation_link: dict[UUID, UUID] = {}
    assessment_tip: dict[UUID, UUID] = {}  # version -> tip assessment
    assessments: set[UUID] = set()

    def live(link: UUID) -> Mapping[str, Any] | None:
        row = link_row.get(link)
        if row is None or link in superseded_links or row["status"] != "linked":
            return None
        return row

    for event in events:
        payload = cast(dict[str, Any], event.payload)
        if _payload_uuid(payload["collection_id"], "collection_id") != aggregate_id:
            raise DecisionReplayError("decision payload belongs to another collection")
        claim = _payload_uuid(payload["claim_id"], "claim_id")
        if event.event_type == "claim.versioned":
            version = _payload_uuid(payload["claim_version_id"], "claim_version_id")
            prior = payload["supersedes_claim_version_id"]
            expected = (
                (None, 1)
                if claim not in version_tip
                else (str(version_tip[claim]), version_no[claim] + 1)
            )
            if (prior, payload["version_no"]) != expected:
                raise DecisionReplayError("forked claim version chain")
            if version in version_claim:
                raise DecisionReplayError("claim version id reused")
            version_tip[claim], version_no[claim] = version, payload["version_no"]
            version_claim[version] = claim
            continue
        if event.event_type == "claim.linked":
            version = _payload_uuid(payload["claim_version_id"], "claim_version_id")
            if version_claim.get(version) != claim or version_tip[claim] != version:
                raise DecisionReplayError("claim link targets a non-tip version")
            link = _payload_uuid(payload["link_id"], "link_id")
            if link in link_row:
                raise DecisionReplayError("claim link id reused")
            supersedes = payload["supersedes_link_id"]
            if supersedes is not None:
                prior_link = _payload_uuid(supersedes, "supersedes_link_id")
                if (
                    prior_link not in link_row
                    or prior_link in superseded_links
                    or link_version[prior_link] != version
                ):
                    raise DecisionReplayError("forked claim link chain")
                superseded_links.add(prior_link)
            link_row[link], link_version[link] = payload, version
            continue
        if event.event_type == "claim.observed":
            if event.actor_role != "machine":
                raise DecisionReplayError("stance observation requires the machine")
            link = _payload_uuid(payload["link_id"], "link_id")
            row = live(link)
            if (
                row is None
                or row["kind"] == "legacy_unanchored"
                or version_claim[link_version[link]] != claim
            ):
                raise DecisionReplayError("stance observation needs a live link")
            if payload["source_content_hash"] != row["text_sha256"]:
                raise DecisionReplayError("stance observation of another revision")
            observation = _payload_uuid(payload["observation_id"], "observation_id")
            if observation in observation_link:
                raise DecisionReplayError("stance observation id reused")
            observation_link[observation] = link
            continue
        if event.actor_role != "adjudicator":
            raise DecisionReplayError("claim assessment requires an adjudicator")
        version = _payload_uuid(payload["claim_version_id"], "claim_version_id")
        if version_claim.get(version) != claim or version_tip[claim] != version:
            raise DecisionReplayError("claim assessment of a non-tip version")
        cited = {_payload_uuid(v, "link_ids") for v in payload["link_ids"]}
        for link in cited:
            row = live(link)
            if (
                row is None
                or row["kind"] == "legacy_unanchored"
                or link_version[link] != version
            ):
                raise DecisionReplayError("claim assessment cites a dead link")
        for value in payload["stance_observation_ids"]:
            observed = _payload_uuid(value, "stance_observation_ids")
            if observation_link.get(observed) not in cited:
                raise DecisionReplayError(
                    "claim assessment cites a foreign observation"
                )
        supersedes = payload["supersedes_assessment_id"]
        tip = assessment_tip.get(version)
        if supersedes != (None if tip is None else str(tip)):
            raise DecisionReplayError("forked claim assessment chain")
        assessment = _payload_uuid(payload["assessment_id"], "assessment_id")
        if assessment in assessments:
            raise DecisionReplayError("claim assessment id reused")
        assessments.add(assessment)
        assessment_tip[version] = assessment


def _validate_release_transitions(
    events: Sequence[ResearchDecisionEvent], aggregate_id: UUID
) -> None:
    """Only an adjudicator or supervisor promotes; one live release per
    draft; a staling names only live releases promoted earlier."""
    live_draft: dict[UUID, UUID] = {}  # live release -> its draft
    for event in events:
        payload = cast(dict[str, Any], event.payload)
        if _payload_uuid(payload["collection_id"], "collection_id") != aggregate_id:
            raise DecisionReplayError("decision payload belongs to another collection")
        if event.event_type == "release.promoted":
            if event.actor_role not in _RELEASE_PROMOTERS:
                raise DecisionReplayError(
                    "release promotion requires an adjudicator or supervisor"
                )
            draft = _payload_uuid(payload["draft_id"], "draft_id")
            if draft in live_draft.values():
                raise DecisionReplayError("draft already has a live release")
            live_draft[_payload_uuid(payload["release_id"], "release_id")] = draft
            continue
        for value in payload["release_ids"]:
            release = _payload_uuid(value, "release_ids")
            if release not in live_draft:
                raise DecisionReplayError("release staling names a non-live release")
            del live_draft[release]


def _check_accepted_anchor(
    payload: Mapping[str, Any], anchors: Mapping[UUID, Mapping[str, Any]]
) -> None:
    """``verified`` needs a verified observation at that start; ``disambiguated``
    an ambiguous one whose recorded occurrences contain the chosen start."""
    resolution = payload["anchor_resolution"]
    if resolution not in ("verified", "disambiguated"):
        return
    anchor = anchors.get(
        _payload_uuid(payload["anchor_observation_id"], "anchor_observation_id")
    )
    start = payload["anchor_start_char"]
    if anchor is None or not (
        (
            resolution == "verified"
            and anchor["anchor_status"] == "verified"
            and anchor["start"] == start
        )
        or (
            resolution == "disambiguated"
            and anchor["anchor_status"] == "ambiguous"
            and start in anchor["occurrences"]
        )
    ):
        raise DecisionReplayError("accepted anchor contradicts observation")


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
    _ACQUISITION_AGGREGATE: _Family(
        subject_type=_IDENTITY_SUBJECT,
        payload_keys=_ACQUISITION_PAYLOAD_KEYS,
        validate_payload=_validate_acquisition_payload,
        validate_transitions=_validate_acquisition_transitions,
        requires_subject_version=False,
    ),
    _EXTRACTION_AGGREGATE: _Family(
        subject_type=_EXTRACTION_SUBJECT,
        payload_keys=_EXTRACTION_PAYLOAD_KEYS,
        validate_payload=_validate_extraction_payload,
        validate_transitions=_validate_extraction_transitions,
        requires_subject_version=False,
    ),
    _CLAIMS_AGGREGATE: _Family(
        subject_type=_CLAIMS_SUBJECT,
        payload_keys=_CLAIMS_PAYLOAD_KEYS,
        validate_payload=_validate_claims_payload,
        validate_transitions=_validate_claims_transitions,
        requires_subject_version=False,
    ),
    _RELEASE_AGGREGATE: _Family(
        subject_type=_RELEASE_SUBJECT,
        payload_keys=_RELEASE_PAYLOAD_KEYS,
        validate_payload=_validate_release_payload,
        validate_transitions=_validate_release_transitions,
        requires_subject_version=False,
    ),
}

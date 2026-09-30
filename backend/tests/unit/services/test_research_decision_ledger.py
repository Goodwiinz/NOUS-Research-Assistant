"""Focused vocabulary and fingerprint tests for the research decision ledger."""

from uuid import UUID, uuid4

import pytest

from src.models.research_decision import ResearchDecisionEvent
from src.services.research_decisions.ledger import (
    DecisionReplayError,
    DecisionValidationError,
    _validate_event,
    _validate_identity_transitions,
    _validate_screening_transitions,
    decision_request_fingerprint,
)


def test_request_fingerprint_is_canonical() -> None:
    assert decision_request_fingerprint({"b": 2, "a": 1}) == (
        decision_request_fingerprint({"a": 1, "b": 2})
    )


def test_unknown_event_schema_fails_closed() -> None:
    protocol_id = uuid4()
    version_id = uuid4()
    with pytest.raises(DecisionValidationError, match="unsupported decision event"):
        _validate_event(
            aggregate_type="research_protocol",
            aggregate_id=protocol_id,
            event_type="protocol.approved",
            event_schema_version=2,
            subject_type="research_protocol_version",
            subject_id=version_id,
            subject_version_id=version_id,
            subject_hash="a" * 64,
            payload={},
            request_fingerprint="b" * 64,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("question_version_id", "not-a-uuid", "question_version_id is not a UUID"),
        ("blueprint_id", 7, "blueprint_id is not a UUID"),
        (
            "expected_protocol_version",
            True,
            "expected_protocol_version must be a positive integer",
        ),
        (
            "canonicalization_version",
            "research-protocol-v2",
            "canonicalization_version is unsupported",
        ),
    ],
)
def test_protocol_approval_payload_values_fail_closed(
    field: str, value: object, message: str
) -> None:
    protocol_id = uuid4()
    version_id = uuid4()
    payload: dict[str, object] = {
        "protocol_id": str(protocol_id),
        "question_version_id": str(uuid4()),
        "blueprint_id": str(uuid4()),
        "previous_approved_version_id": None,
        "expected_protocol_version": 1,
        "canonicalization_version": "research-protocol-v1",
    }
    payload[field] = value
    with pytest.raises(DecisionValidationError, match=message):
        _validate_event(
            aggregate_type="research_protocol",
            aggregate_id=protocol_id,
            event_type="protocol.approved",
            event_schema_version=1,
            subject_type="research_protocol_version",
            subject_id=version_id,
            subject_version_id=version_id,
            subject_hash="a" * 64,
            payload=payload,
            request_fingerprint="b" * 64,
        )


def test_protocol_supersession_payload_hash_fails_closed() -> None:
    protocol_id = uuid4()
    version_id = uuid4()
    with pytest.raises(DecisionValidationError, match="superseded_by_hash"):
        _validate_event(
            aggregate_type="research_protocol",
            aggregate_id=protocol_id,
            event_type="protocol.superseded",
            event_schema_version=1,
            subject_type="research_protocol_version",
            subject_id=version_id,
            subject_version_id=version_id,
            subject_hash="a" * 64,
            payload={
                "protocol_id": str(protocol_id),
                "superseded_by_version_id": str(uuid4()),
                "superseded_by_hash": "invalid",
            },
            request_fingerprint="b" * 64,
        )


# --- GOO-299: research_identity family ------------------------------------


def _identity_event(
    event_type: str, payload: dict[str, object], subject_id: UUID
) -> dict[str, object]:
    return {
        "aggregate_type": "research_identity",
        "aggregate_id": UUID(str(payload["collection_id"])),
        "event_type": event_type,
        "event_schema_version": 1,
        "subject_type": "research_report",
        "subject_id": subject_id,
        "subject_version_id": None,
        "subject_hash": decision_request_fingerprint(payload),
        "payload": payload,
        "request_fingerprint": "b" * 64,
    }


def _link_payload(
    collection_id: UUID,
    report_id: UUID,
    study_id: UUID,
    status: str = "proposed",
    prior_study_id: UUID | None = None,
    prior_status: str | None = None,
) -> dict[str, object]:
    return {
        "collection_id": str(collection_id),
        "report_id": str(report_id),
        "study_id": str(study_id),
        "status": status,
        "prior_study_id": str(prior_study_id) if prior_study_id else None,
        "prior_status": prior_status,
        "match_evidence": {},
        "protocol_version_id": None,
    }


def test_identity_merge_rejects_self_merge() -> None:
    collection_id, report_id = uuid4(), uuid4()
    payload: dict[str, object] = {
        "collection_id": str(collection_id),
        "surviving_report_id": str(report_id),
        "merged_report_ids": [str(report_id)],
        "moved_source_ids": [],
        "moved_identifiers": [],
        "protocol_version_id": None,
    }
    with pytest.raises(DecisionValidationError, match="cannot merge into itself"):
        _validate_event(**_identity_event("identity.report_merged", payload, report_id))  # type: ignore[arg-type]


def test_identity_link_rejects_unknown_status() -> None:
    collection_id, report_id = uuid4(), uuid4()
    payload = _link_payload(collection_id, report_id, uuid4(), status="maybe")
    with pytest.raises(DecisionValidationError, match="study link status"):
        _validate_event(**_identity_event("identity.study_linked", payload, report_id))  # type: ignore[arg-type]


def test_identity_events_need_no_subject_version() -> None:
    collection_id, report_id = uuid4(), uuid4()
    payload = _link_payload(collection_id, report_id, uuid4())
    _validate_event(**_identity_event("identity.study_linked", payload, report_id))  # type: ignore[arg-type]

    wrong_collection = _identity_event("identity.study_linked", payload, report_id)
    wrong_collection["aggregate_id"] = uuid4()
    with pytest.raises(DecisionValidationError, match="another collection"):
        _validate_event(**wrong_collection)  # type: ignore[arg-type]


def _stored(event_type: str, payload: dict[str, object]) -> ResearchDecisionEvent:
    return ResearchDecisionEvent(event_type=event_type, payload=payload)


def test_identity_replay_rejects_contradictory_study_link() -> None:
    collection_id, report_id, study_id = uuid4(), uuid4(), uuid4()
    proposed = _link_payload(collection_id, report_id, study_id)
    confirmed = _link_payload(
        collection_id, report_id, study_id, "confirmed", study_id, "proposed"
    )
    _validate_identity_transitions(
        [
            _stored("identity.study_linked", proposed),
            _stored("identity.study_linked", confirmed),
        ],
        collection_id,
    )

    stale = _link_payload(collection_id, report_id, study_id, "confirmed")
    with pytest.raises(DecisionReplayError, match="contradictory study link"):
        _validate_identity_transitions(
            [
                _stored("identity.study_linked", proposed),
                _stored("identity.study_linked", stale),
            ],
            collection_id,
        )


def test_identity_replay_treats_merged_reports_as_terminal() -> None:
    collection_id, survivor, loser = uuid4(), uuid4(), uuid4()
    merged: dict[str, object] = {
        "collection_id": str(collection_id),
        "surviving_report_id": str(survivor),
        "merged_report_ids": [str(loser)],
        "moved_source_ids": [],
        "moved_identifiers": [],
        "protocol_version_id": None,
    }
    split: dict[str, object] = {
        "collection_id": str(collection_id),
        "source_report_id": str(loser),
        "new_report_id": str(uuid4()),
        "moved_source_ids": [],
        "moved_identifiers": [],
        "protocol_version_id": None,
    }
    with pytest.raises(DecisionReplayError, match="merged report is terminal"):
        _validate_identity_transitions(
            [
                _stored("identity.report_merged", merged),
                _stored("identity.report_split", split),
            ],
            collection_id,
        )


def _merge_payload(
    collection_id: UUID, survivor: UUID, loser: UUID, moved_identifiers: object = ()
) -> dict[str, object]:
    return {
        "collection_id": str(collection_id),
        "surviving_report_id": str(survivor),
        "merged_report_ids": [str(loser)],
        "moved_source_ids": [],
        "moved_identifiers": list(moved_identifiers),  # type: ignore[call-overload]
        "protocol_version_id": None,
    }


def test_identity_replay_carries_loser_link_onto_survivor() -> None:
    """Deleting the merge link-copy in replay makes the confirm contradictory."""
    collection_id, survivor, loser, study_id = uuid4(), uuid4(), uuid4(), uuid4()
    _validate_identity_transitions(
        [
            _stored(
                "identity.study_linked", _link_payload(collection_id, loser, study_id)
            ),
            _stored(
                "identity.report_merged",
                _merge_payload(collection_id, survivor, loser),
            ),
            _stored(
                "identity.study_linked",
                _link_payload(
                    collection_id, survivor, study_id, "confirmed", study_id, "proposed"
                ),
            ),
        ],
        collection_id,
    )


def test_identity_event_rejects_a_subject_version() -> None:
    collection_id, report_id = uuid4(), uuid4()
    event = _identity_event(
        "identity.study_linked",
        _link_payload(collection_id, report_id, uuid4()),
        report_id,
    )
    event["subject_version_id"] = uuid4()
    with pytest.raises(DecisionValidationError, match="must not carry a subject"):
        _validate_event(**event)  # type: ignore[arg-type]


def test_identity_subject_hash_must_fingerprint_its_payload() -> None:
    """Otherwise one bad append would make history() unreplayable forever."""
    collection_id, report_id = uuid4(), uuid4()
    event = _identity_event(
        "identity.study_linked",
        _link_payload(collection_id, report_id, uuid4()),
        report_id,
    )
    event["subject_hash"] = "a" * 64
    with pytest.raises(DecisionValidationError, match="fingerprint"):
        _validate_event(**event)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["status", "prior_status"])
def test_identity_link_status_must_be_a_string(field: str) -> None:
    collection_id, report_id = uuid4(), uuid4()
    payload = _link_payload(collection_id, report_id, uuid4(), prior_study_id=uuid4())
    payload["prior_status"] = "proposed"
    payload[field] = ["proposed"]
    with pytest.raises(DecisionValidationError, match="status is invalid"):
        _validate_event(
            **_identity_event("identity.study_linked", payload, report_id)  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "moved",
    [
        ["doi"],
        [{"kind": "doi"}],
        [{"kind": "doi", "value": 7}],
        [{"kind": "doi", "value": "x", "extra": 1}],
    ],
)
def test_identity_moved_identifiers_are_kind_value_pairs(moved: list[object]) -> None:
    collection_id, survivor = uuid4(), uuid4()
    payload = _merge_payload(collection_id, survivor, uuid4(), moved)
    with pytest.raises(DecisionValidationError, match="moved_identifiers"):
        _validate_event(
            **_identity_event("identity.report_merged", payload, survivor)  # type: ignore[arg-type]
        )


# --- GOO-300: schema 2 moves imported records too --------------------------


def test_identity_merge_v2_requires_import_ids_key() -> None:
    collection_id, survivor, loser = uuid4(), uuid4(), uuid4()
    v1_shape = _merge_payload(collection_id, survivor, loser)
    event = {
        **_identity_event("identity.report_merged", v1_shape, survivor),
        "event_schema_version": 2,
    }
    with pytest.raises(DecisionValidationError, match="does not match its event"):
        _validate_event(**event)  # type: ignore[arg-type]

    v2 = {**v1_shape, "moved_import_record_ids": [str(uuid4())]}
    _validate_event(
        **{  # type: ignore[arg-type]
            **_identity_event("identity.report_merged", v2, survivor),
            "event_schema_version": 2,
        }
    )
    bad = {**v1_shape, "moved_import_record_ids": ["not-a-uuid"]}
    with pytest.raises(DecisionValidationError, match="moved_import_record_ids"):
        _validate_event(
            **{  # type: ignore[arg-type]
                **_identity_event("identity.report_merged", bad, survivor),
                "event_schema_version": 2,
            }
        )


def test_identity_split_v2_accepts_import_ids() -> None:
    collection_id, source_report, new_report = uuid4(), uuid4(), uuid4()
    payload: dict[str, object] = {
        "collection_id": str(collection_id),
        "source_report_id": str(source_report),
        "new_report_id": str(new_report),
        "moved_source_ids": [],
        "moved_import_record_ids": [str(uuid4())],
        "moved_identifiers": [],
        "protocol_version_id": None,
    }
    _validate_event(
        **{  # type: ignore[arg-type]
            **_identity_event("identity.report_split", payload, source_report),
            "event_schema_version": 2,
        }
    )


def test_v1_merge_events_still_replay() -> None:
    collection_id, survivor, loser = uuid4(), uuid4(), uuid4()
    payload = _merge_payload(collection_id, survivor, loser)
    _validate_event(**_identity_event("identity.report_merged", payload, survivor))  # type: ignore[arg-type]
    with pytest.raises(DecisionValidationError, match="does not match its event"):
        _validate_event(
            **_identity_event(  # type: ignore[arg-type]
                "identity.report_merged",
                {**payload, "moved_import_record_ids": []},
                survivor,
            )
        )
    _validate_identity_transitions(
        [_stored("identity.report_merged", payload)], collection_id
    )


# --- GOO-301: research_screening family -----------------------------------


class _Queue:
    def __init__(self, stage: str = "title_abstract") -> None:
        self.collection_id, self.queue_id = uuid4(), uuid4()
        self.reports = [uuid4(), uuid4()]
        self.stage = stage

    def payload(self, **fields: object) -> dict[str, object]:
        return {
            "collection_id": str(self.collection_id),
            "queue_id": str(self.queue_id),
            **{
                key: str(value) if isinstance(value, UUID) else value
                for key, value in fields.items()
            },
        }

    def created(self) -> dict[str, object]:
        return self.payload(
            stage=self.stage,
            protocol_version_id=uuid4(),
            criteria_hash="c" * 64,
            report_ids=[str(report) for report in self.reports],
            exclusion_reasons=["wrong population", "wrong design"],
            supersedes_queue_id=None,
            reviewer_mode="dual_independent",
        )

    def assignment(self, assignment_id: UUID, reviewer: UUID) -> dict[str, object]:
        return self.payload(assignment_id=assignment_id, reviewer_id=reviewer)

    def observed(
        self,
        observation_id: UUID,
        assignment_id: UUID,
        reviewer: UUID,
        report: UUID,
        decision: str = "include",
        exclusion_reason: str | None = None,
        superseded: UUID | None = None,
    ) -> dict[str, object]:
        payload = self.payload(
            observation_id=observation_id,
            assignment_id=assignment_id,
            reviewer_id=reviewer,
            report_id=report,
            decision=decision,
            exclusion_reason=exclusion_reason,
        )
        if superseded is not None:
            payload["superseded_observation_id"] = str(superseded)
        return payload

    def event(self, event_type: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            "aggregate_type": "research_screening",
            "aggregate_id": self.queue_id,
            "event_type": event_type,
            "event_schema_version": 1,
            "subject_type": "screening_queue",
            "subject_id": self.queue_id,
            "subject_version_id": None,
            "subject_hash": decision_request_fingerprint(payload),
            "payload": payload,
            "request_fingerprint": "b" * 64,
        }

    def replay(self, *events: tuple[str, dict[str, object]]) -> None:
        for event_type, payload in events:
            _validate_event(**self.event(event_type, payload))  # type: ignore[arg-type]
        _validate_screening_transitions(
            [_stored(event_type, payload) for event_type, payload in events],
            self.queue_id,
        )


def test_screening_payload_rejects_foreign_queue() -> None:
    queue = _Queue()
    queue.replay(("screening.queue_created", queue.created()))

    foreign = queue.event("screening.queue_created", queue.created())
    foreign["aggregate_id"] = foreign["subject_id"] = uuid4()
    with pytest.raises(DecisionValidationError, match="another screening queue"):
        _validate_event(**foreign)  # type: ignore[arg-type]

    bad_mode = queue.created()
    bad_mode["reviewer_mode"] = "independent"
    with pytest.raises(DecisionValidationError, match="reviewer_mode"):
        _validate_event(**queue.event("screening.queue_created", bad_mode))  # type: ignore[arg-type]

    duplicate_corpus = queue.created()
    duplicate_corpus["report_ids"] = [str(queue.reports[0])] * 2
    with pytest.raises(DecisionValidationError, match="report_ids"):
        _validate_event(**queue.event("screening.queue_created", duplicate_corpus))  # type: ignore[arg-type]


def test_screening_replay_rejects_observation_without_assignment() -> None:
    queue = _Queue()
    reviewer, other, assignment = uuid4(), uuid4(), uuid4()
    with pytest.raises(DecisionReplayError, match="without an active assignment"):
        queue.replay(
            ("screening.queue_created", queue.created()),
            ("screening.assigned", queue.assignment(assignment, other)),
            (
                "screening.observed",
                queue.observed(uuid4(), assignment, reviewer, queue.reports[0]),
            ),
        )
    with pytest.raises(DecisionReplayError, match="without an active assignment"):
        queue.replay(
            ("screening.queue_created", queue.created()),
            ("screening.assigned", queue.assignment(assignment, reviewer)),
            ("screening.unassigned", queue.assignment(assignment, reviewer)),
            (
                "screening.observed",
                queue.observed(uuid4(), assignment, reviewer, queue.reports[0]),
            ),
        )
    with pytest.raises(DecisionReplayError, match="outside corpus"):
        queue.replay(
            ("screening.queue_created", queue.created()),
            ("screening.assigned", queue.assignment(assignment, reviewer)),
            (
                "screening.observed",
                queue.observed(uuid4(), assignment, reviewer, uuid4()),
            ),
        )


def test_screening_replay_rejects_second_initial_observation() -> None:
    queue = _Queue()
    reviewer, assignment = uuid4(), uuid4()
    report = queue.reports[0]
    with pytest.raises(DecisionReplayError, match="initial screening observation"):
        queue.replay(
            ("screening.queue_created", queue.created()),
            ("screening.assigned", queue.assignment(assignment, reviewer)),
            (
                "screening.observed",
                queue.observed(uuid4(), assignment, reviewer, report),
            ),
            (
                "screening.observed",
                queue.observed(uuid4(), assignment, reviewer, report),
            ),
        )
    with pytest.raises(DecisionReplayError, match="queue_created"):
        queue.replay(
            ("screening.queue_created", queue.created()),
            ("screening.queue_created", queue.created()),
        )


def test_screening_replay_rejects_reason_not_in_protocol() -> None:
    queue = _Queue(stage="full_text")
    reviewer, assignment = uuid4(), uuid4()
    report = queue.reports[0]
    with pytest.raises(DecisionReplayError, match="protocol criteria"):
        queue.replay(
            ("screening.queue_created", queue.created()),
            ("screening.assigned", queue.assignment(assignment, reviewer)),
            (
                "screening.observed",
                queue.observed(
                    uuid4(), assignment, reviewer, report, "exclude", "too old"
                ),
            ),
        )


def test_screening_replay_accepts_assign_observe_supersede_unassign() -> None:
    queue = _Queue(stage="full_text")
    reviewer, assignment, again = uuid4(), uuid4(), uuid4()
    first, second, third = uuid4(), uuid4(), uuid4()
    report = queue.reports[1]
    queue.replay(
        ("screening.queue_created", queue.created()),
        ("screening.assigned", queue.assignment(assignment, reviewer)),
        ("screening.observed", queue.observed(first, assignment, reviewer, report)),
        (
            "screening.superseded",
            queue.observed(
                second,
                assignment,
                reviewer,
                report,
                "exclude",
                "wrong design",
                superseded=first,
            ),
        ),
        ("screening.unassigned", queue.assignment(assignment, reviewer)),
        ("screening.assigned", queue.assignment(again, reviewer)),
        (
            "screening.superseded",
            queue.observed(third, again, reviewer, report, superseded=second),
        ),
    )

    with pytest.raises(
        DecisionReplayError, match="contradictory screening supersession"
    ):
        queue.replay(
            ("screening.queue_created", queue.created()),
            ("screening.assigned", queue.assignment(assignment, reviewer)),
            ("screening.observed", queue.observed(first, assignment, reviewer, report)),
            (
                "screening.superseded",
                queue.observed(
                    second, assignment, reviewer, report, superseded=uuid4()
                ),
            ),
        )

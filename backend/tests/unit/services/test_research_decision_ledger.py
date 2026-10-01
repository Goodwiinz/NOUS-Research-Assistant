"""Focused vocabulary and fingerprint tests for the research decision ledger."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from src.models.research_decision import ResearchDecisionEvent
from src.services.research_decisions.ledger import (
    DecisionReplayError,
    DecisionValidationError,
    _validate_acquisition_transitions,
    _validate_appraisal_transitions,
    _validate_claims_transitions,
    _validate_event,
    _validate_evidence_transitions,
    _validate_experiment_transitions,
    _validate_extraction_transitions,
    _validate_identity_transitions,
    _validate_release_transitions,
    _validate_screening_transitions,
    _validate_synthesis_transitions,
    decision_request_fingerprint,
    replay_screening_resolutions,
)
from src.services.research_engine.screening_rules import auto_resolution_id


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
            suggestions_skipped=None,
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

    def adjudicated(
        self,
        report: UUID,
        resolution_id: UUID,
        conflict_id: UUID,
        inputs: list[UUID],
        decision: str = "include",
        exclusion_reason: str | None = None,
    ) -> dict[str, object]:
        return self.payload(
            report_id=report,
            resolution_id=resolution_id,
            conflict_resolution_id=conflict_id,
            input_observation_ids=sorted(str(i) for i in inputs),
            criteria_hash="c" * 64,
            decision=decision,
            exclusion_reason=exclusion_reason,
        )

    def reopened(
        self, report: UUID, resolution_id: UUID, reopened_id: UUID
    ) -> dict[str, object]:
        return self.payload(
            report_id=report,
            resolution_id=resolution_id,
            reopened_resolution_id=reopened_id,
        )

    def event(
        self,
        event_type: str,
        payload: dict[str, object],
        reason: str | None = None,
    ) -> dict[str, object]:
        return {
            "reason": reason,
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

    def replay(self, *events: tuple[Any, ...], actor: UUID | None = None) -> list[Any]:
        """Observations are authored by their reviewer unless ``actor`` overrides.

        An event is ``(type, payload)`` or ``(type, payload, overrides)`` with
        optional ``id``/``actor``/``reason`` overrides; adjudications and
        reopens carry a rationale by default.
        """
        stored = []
        for event_type, payload, *rest in events:
            extra: dict[str, Any] = rest[0] if rest else {}
            reason = extra.get(
                "reason", "rationale" if event_type in _RATIONALE else None
            )
            _validate_event(**self.event(event_type, payload, reason))  # type: ignore[arg-type]
            row = _stored(event_type, payload)
            row.id = extra.get("id", uuid4())
            row.reason = reason
            row.actor_user_id = extra.get(
                "actor", actor or UUID(str(payload.get("reviewer_id", uuid4())))
            )
            stored.append(row)
        _validate_screening_transitions(stored, self.queue_id)
        return list(replay_screening_resolutions(stored, self.queue_id))


_RATIONALE = ("screening.adjudicated", "screening.reopened")


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


def test_screening_replay_rejects_reassign_while_active() -> None:
    queue = _Queue()
    reviewer = uuid4()
    with pytest.raises(DecisionReplayError, match="contradictory screening assignment"):
        queue.replay(
            ("screening.queue_created", queue.created()),
            ("screening.assigned", queue.assignment(uuid4(), reviewer)),
            ("screening.assigned", queue.assignment(uuid4(), reviewer)),
        )


def test_screening_replay_rejects_event_from_another_collection() -> None:
    queue = _Queue()
    reviewer, assignment = uuid4(), uuid4()
    foreign = queue.assignment(assignment, reviewer)
    foreign["collection_id"] = str(uuid4())
    with pytest.raises(DecisionReplayError, match="another collection"):
        queue.replay(
            ("screening.queue_created", queue.created()),
            ("screening.assigned", foreign),
        )


def test_screening_observation_cannot_supersede_itself() -> None:
    queue = _Queue()
    observation = uuid4()
    payload = queue.observed(
        observation, uuid4(), uuid4(), queue.reports[0], superseded=observation
    )
    with pytest.raises(DecisionValidationError, match="cannot supersede itself"):
        _validate_event(**queue.event("screening.superseded", payload))  # type: ignore[arg-type]


def test_screening_replay_requires_observation_actor_to_be_its_reviewer() -> None:
    queue = _Queue()
    reviewer, assignment = uuid4(), uuid4()
    events = (
        ("screening.queue_created", queue.created()),
        ("screening.assigned", queue.assignment(assignment, reviewer)),
        (
            "screening.observed",
            queue.observed(uuid4(), assignment, reviewer, queue.reports[0]),
        ),
    )
    queue.replay(*events)
    with pytest.raises(DecisionReplayError, match="actor is not its reviewer"):
        queue.replay(*events, actor=uuid4())


@pytest.mark.parametrize("skipped", [-1, True, "2"])
def test_screening_suggestions_skipped_is_a_count(skipped: object) -> None:
    queue = _Queue()
    payload = queue.created()
    payload["suggestions_skipped"] = skipped
    with pytest.raises(DecisionValidationError, match="suggestions_skipped"):
        _validate_event(**queue.event("screening.queue_created", payload))  # type: ignore[arg-type]


# --- GOO-302: resolutions, adjudication and reopen --------------------------


class _Dual:
    """A dual full-text queue with reviewers R, R2 assigned; R3 is spare."""

    def __init__(self) -> None:
        self.queue = _Queue(stage="full_text")
        self.report = self.queue.reports[0]
        self.r, self.r2 = uuid4(), uuid4()
        self.a, self.a2 = uuid4(), uuid4()
        self.base: list[tuple[Any, ...]] = [
            ("screening.queue_created", self.queue.created()),
            ("screening.assigned", self.queue.assignment(self.a, self.r)),
            ("screening.assigned", self.queue.assignment(self.a2, self.r2)),
        ]

    def observe(
        self,
        observation: UUID,
        reviewer: UUID,
        decision: str = "include",
        reason: str | None = None,
        superseded: UUID | None = None,
        event_id: UUID | None = None,
    ) -> tuple[Any, ...]:
        assignment = self.a if reviewer == self.r else self.a2
        return (
            "screening.observed" if superseded is None else "screening.superseded",
            self.queue.observed(
                observation,
                assignment,
                reviewer,
                self.report,
                decision,
                reason,
                superseded=superseded,
            ),
            {"id": event_id or uuid4()},
        )


def test_replay_rederives_agreement_and_conflict() -> None:
    dual = _Dual()
    o1, o2, trigger = uuid4(), uuid4(), uuid4()
    [agreement] = dual.queue.replay(
        *dual.base,
        dual.observe(o1, dual.r, "exclude", "wrong design"),
        dual.observe(o2, dual.r2, "exclude", "wrong design", event_id=trigger),
    )
    assert agreement.id == auto_resolution_id(trigger)
    assert agreement.event_id == trigger
    assert (agreement.basis, agreement.outcome, agreement.exclusion_reason) == (
        "agreement",
        "exclude",
        "wrong design",
    )
    assert agreement.input_observation_ids == sorted([str(o1), str(o2)])
    assert agreement.supersedes_resolution_id is None

    [conflict] = dual.queue.replay(
        *dual.base,
        dual.observe(o1, dual.r, "exclude", "wrong design"),
        dual.observe(o2, dual.r2, "exclude", "wrong population"),
    )
    assert (conflict.basis, conflict.outcome) == ("conflict", None)

    # One observation, even superseded twice, never resolves a dual report.
    o3 = uuid4()
    assert (
        dual.queue.replay(
            *dual.base,
            dual.observe(o1, dual.r),
            dual.observe(o3, dual.r, "uncertain", superseded=o1),
        )
        == []
    )


def test_replay_rejects_observation_after_resolution() -> None:
    dual = _Dual()
    o1, o2 = uuid4(), uuid4()
    with pytest.raises(DecisionReplayError, match="observation after resolution"):
        dual.queue.replay(
            *dual.base,
            dual.observe(o1, dual.r),
            dual.observe(o2, dual.r2),
            dual.observe(uuid4(), dual.r, "uncertain", superseded=o1),
        )


def _conflict(dual: _Dual) -> tuple[list[tuple[Any, ...]], UUID, list[UUID]]:
    o1, o2, trigger = uuid4(), uuid4(), uuid4()
    events = [
        *dual.base,
        dual.observe(o1, dual.r, "include"),
        dual.observe(o2, dual.r2, "exclude", "wrong design", event_id=trigger),
    ]
    return events, auto_resolution_id(trigger), [o1, o2]


def test_replay_rejects_adjudication_with_stale_inputs() -> None:
    dual = _Dual()
    events, tip, inputs = _conflict(dual)
    good = dual.queue.adjudicated(dual.report, uuid4(), tip, inputs)
    resolutions = dual.queue.replay(*events, ("screening.adjudicated", good))
    assert [r.basis for r in resolutions] == ["conflict", "adjudicated"]
    assert resolutions[1].supersedes_resolution_id == tip
    assert resolutions[1].input_observation_ids == resolutions[0].input_observation_ids

    stale_tip = dual.queue.adjudicated(dual.report, uuid4(), uuid4(), inputs)
    stale_ids = dual.queue.adjudicated(dual.report, uuid4(), tip, [inputs[0]])
    for payload in (stale_tip, stale_ids):
        with pytest.raises(DecisionReplayError, match="inputs are stale"):
            dual.queue.replay(*events, ("screening.adjudicated", payload))
    # A second adjudication: the tip is no longer a conflict.
    with pytest.raises(DecisionReplayError, match="not in conflict"):
        dual.queue.replay(
            *events,
            ("screening.adjudicated", good),
            (
                "screening.adjudicated",
                dual.queue.adjudicated(
                    dual.report, uuid4(), good["resolution_id"], inputs  # type: ignore[arg-type]
                ),
            ),
        )
    bad_reason = dual.queue.adjudicated(
        dual.report, uuid4(), tip, inputs, "exclude", "too old"
    )
    with pytest.raises(DecisionReplayError, match="protocol criteria"):
        dual.queue.replay(*events, ("screening.adjudicated", bad_reason))


def test_replay_rejects_self_adjudication() -> None:
    dual = _Dual()
    events, tip, inputs = _conflict(dual)
    payload = dual.queue.adjudicated(dual.report, uuid4(), tip, inputs)
    with pytest.raises(DecisionReplayError, match="adjudicator reviewed"):
        dual.queue.replay(
            *events, ("screening.adjudicated", payload, {"actor": dual.r2})
        )


def test_replay_accepts_reopen_then_fresh_resolution() -> None:
    dual = _Dual()
    events, tip, [o1, o2] = _conflict(dual)
    reopen_id = uuid4()
    reopen = ("screening.reopened", dual.queue.reopened(dual.report, reopen_id, tip))
    # Reopen consumes the old inputs: one fresh observation does not resolve.
    o3, o4 = uuid4(), uuid4()
    resolutions = dual.queue.replay(
        *events, reopen, dual.observe(o3, dual.r, "exclude", "wrong design", o1)
    )
    assert [r.basis for r in resolutions] == ["conflict", "reopened"]
    assert resolutions[1].id == reopen_id
    assert resolutions[1].input_observation_ids == []
    resolutions = dual.queue.replay(
        *events,
        reopen,
        dual.observe(o3, dual.r, "exclude", "wrong design", o1),
        dual.observe(o4, dual.r2, "exclude", "wrong design", o2),
    )
    assert [r.basis for r in resolutions] == ["conflict", "reopened", "agreement"]
    assert resolutions[2].input_observation_ids == sorted([str(o3), str(o4)])
    assert resolutions[2].supersedes_resolution_id == reopen_id

    with pytest.raises(DecisionReplayError, match="not resolved"):
        dual.queue.replay(
            *events,
            reopen,
            (
                "screening.reopened",
                dual.queue.reopened(dual.report, uuid4(), reopen_id),
            ),
        )
    with pytest.raises(DecisionReplayError, match="not resolved"):
        dual.queue.replay(
            *dual.base,
            ("screening.reopened", dual.queue.reopened(dual.report, uuid4(), uuid4())),
        )


@pytest.mark.parametrize("event_type", ["screening.adjudicated", "screening.reopened"])
@pytest.mark.parametrize("reason", [None, ""])
def test_adjudicated_requires_reason(event_type: str, reason: str | None) -> None:
    dual = _Dual()
    report, tip = dual.report, uuid4()
    payload = (
        dual.queue.adjudicated(report, uuid4(), tip, [uuid4()])
        if event_type == "screening.adjudicated"
        else dual.queue.reopened(report, uuid4(), tip)
    )
    _validate_event(**dual.queue.event(event_type, payload, "because"))  # type: ignore[arg-type]
    with pytest.raises(DecisionValidationError, match="rationale"):
        _validate_event(**dual.queue.event(event_type, payload, reason))  # type: ignore[arg-type]


# --- GOO-303: research_acquisition family ----------------------------------


class _Acquisition:
    """Hand-built acquisition events for one Collection and report."""

    def __init__(self) -> None:
        self.collection_id, self.report_id = uuid4(), uuid4()

    def requested(self, request_id: UUID) -> dict[str, object]:
        return {
            "collection_id": str(self.collection_id),
            "request_id": str(request_id),
            "report_id": str(self.report_id),
            "protocol_version_id": None,
        }

    def attempt(
        self,
        request_id: UUID,
        attempt_id: UUID,
        previous: UUID | None,
        outcome: str = "unavailable",
    ) -> tuple[str, dict[str, object]]:
        payload: dict[str, object] = {
            "collection_id": str(self.collection_id),
            "request_id": str(request_id),
            "report_id": str(self.report_id),
            "attempt_id": str(attempt_id),
            "previous_attempt_id": str(previous) if previous else None,
            "attempted_on": "2026-09-29",
            "reason": "not held by library" if outcome == "unavailable" else None,
        }
        if outcome == "retrieved":
            payload |= {"document_id": str(uuid4()), "document_content_hash": "c" * 64}
        event_type = {
            "requested": "acquisition.attempted",
            "unavailable": "acquisition.unavailable",
            "retrieved": "acquisition.retrieved",
        }[outcome]
        return event_type, payload

    def validate(self, event_type: str, payload: dict[str, object]) -> None:
        _validate_event(
            aggregate_type="research_acquisition",
            aggregate_id=UUID(str(payload["collection_id"])),
            event_type=event_type,
            event_schema_version=1,
            subject_type="research_report",
            subject_id=self.report_id,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            payload=payload,
            request_fingerprint="b" * 64,
        )

    def replay(self, *events: tuple[str, dict[str, object]]) -> None:
        _validate_acquisition_transitions(
            [_stored(event_type, payload) for event_type, payload in events],
            self.collection_id,
        )


def test_acquisition_payload_rejects_foreign_collection() -> None:
    acq = _Acquisition()
    request = acq.requested(uuid4())
    acq.validate("acquisition.requested", request)
    foreign = {**request, "collection_id": str(uuid4())}
    with pytest.raises(DecisionValidationError, match="another collection"):
        _validate_event(
            aggregate_type="research_acquisition",
            aggregate_id=acq.collection_id,
            event_type="acquisition.requested",
            event_schema_version=1,
            subject_type="research_report",
            subject_id=acq.report_id,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(foreign),
            payload=foreign,
            request_fingerprint="b" * 64,
        )
    other_report = {**request, "report_id": str(uuid4())}
    with pytest.raises(DecisionValidationError, match="subject is not its report"):
        acq.validate("acquisition.requested", other_report)


def test_unavailable_requires_reason() -> None:
    acq = _Acquisition()
    event_type, payload = acq.attempt(uuid4(), uuid4(), None)
    acq.validate(event_type, payload)
    for reason in (None, "", "   "):
        with pytest.raises(DecisionValidationError, match="needs a reason"):
            acq.validate(event_type, {**payload, "reason": reason})
    with pytest.raises(DecisionValidationError, match="attempted_on"):
        acq.validate(event_type, {**payload, "attempted_on": "yesterday"})
    retrieved_type, retrieved = acq.attempt(uuid4(), uuid4(), None, "retrieved")
    acq.validate(retrieved_type, retrieved)
    with pytest.raises(DecisionValidationError, match="SHA-256"):
        acq.validate(retrieved_type, {**retrieved, "document_content_hash": "C" * 64})


def test_acquisition_replay_rejects_forked_chain() -> None:
    acq = _Acquisition()
    request, first = uuid4(), uuid4()
    with pytest.raises(DecisionReplayError, match="contradictory acquisition chain"):
        acq.replay(
            ("acquisition.requested", acq.requested(request)),
            acq.attempt(request, first, None),
            acq.attempt(request, uuid4(), None, "requested"),
        )
    with pytest.raises(DecisionReplayError, match="contradictory acquisition chain"):
        acq.replay(acq.attempt(request, first, None))  # unknown request
    with pytest.raises(DecisionReplayError, match="contradictory acquisition chain"):
        acq.replay(
            ("acquisition.requested", acq.requested(request)),
            ("acquisition.requested", acq.requested(uuid4())),  # second per report
        )


def test_acquisition_replay_rejects_attempt_after_retrieved() -> None:
    acq = _Acquisition()
    request, first = uuid4(), uuid4()
    with pytest.raises(DecisionReplayError, match="acquisition after retrieval"):
        acq.replay(
            ("acquisition.requested", acq.requested(request)),
            acq.attempt(request, first, None, "retrieved"),
            acq.attempt(request, uuid4(), first, "requested"),
        )


def test_acquisition_replay_accepts_request_unavailable_retry_retrieved() -> None:
    acq = _Acquisition()
    request, first, second, third = uuid4(), uuid4(), uuid4(), uuid4()
    acq.replay(
        ("acquisition.requested", acq.requested(request)),
        acq.attempt(request, first, None),
        acq.attempt(request, second, first, "requested"),
        acq.attempt(request, third, second, "retrieved"),
    )


def test_replay_rejects_adjudicating_a_non_conflict_tip() -> None:
    dual = _Dual()
    o1, o2, trigger = uuid4(), uuid4(), uuid4()
    events = [
        *dual.base,
        dual.observe(o1, dual.r, "exclude", "wrong design"),
        dual.observe(o2, dual.r2, "exclude", "wrong design", event_id=trigger),
    ]
    payload = dual.queue.adjudicated(
        dual.report, uuid4(), auto_resolution_id(trigger), [o1, o2]
    )
    with pytest.raises(DecisionReplayError, match="not in conflict"):
        dual.queue.replay(*events, ("screening.adjudicated", payload))


def test_replay_rejects_adjudication_under_other_criteria() -> None:
    dual = _Dual()
    events, tip, inputs = _conflict(dual)
    payload = dual.queue.adjudicated(dual.report, uuid4(), tip, inputs)
    payload["criteria_hash"] = "d" * 64
    with pytest.raises(DecisionReplayError, match="criteria changed"):
        dual.queue.replay(*events, ("screening.adjudicated", payload))


def test_replay_rejects_double_reopen() -> None:
    dual = _Dual()
    events, tip, _inputs = _conflict(dual)
    first = uuid4()
    with pytest.raises(DecisionReplayError, match="not resolved"):
        dual.queue.replay(
            *events,
            ("screening.reopened", dual.queue.reopened(dual.report, first, tip)),
            ("screening.reopened", dual.queue.reopened(dual.report, uuid4(), first)),
        )


def test_replay_rejects_adjudicator_who_reviewed_an_earlier_cycle() -> None:
    """R3 was an input of cycle 1 only; after a reopen, R and R2 conflict and
    R3 (also an adjudicator) still may not adjudicate the report."""
    dual = _Dual()
    r3, a3 = uuid4(), uuid4()
    o_r3, o_r, o_r_new, o_r2, t1, t3 = (uuid4() for _ in range(6))
    events: list[tuple[Any, ...]] = [
        *dual.base,
        ("screening.assigned", dual.queue.assignment(a3, r3)),
        (
            "screening.observed",
            dual.queue.observed(o_r3, a3, r3, dual.report, "include"),
        ),
        dual.observe(o_r, dual.r, "exclude", "wrong design", event_id=t1),
    ]
    reopen = uuid4()
    events += [
        (
            "screening.reopened",
            dual.queue.reopened(dual.report, reopen, auto_resolution_id(t1)),
        ),
        dual.observe(o_r_new, dual.r, "include", superseded=o_r),
        dual.observe(o_r2, dual.r2, "exclude", "wrong design", event_id=t3),
    ]
    resolutions = dual.queue.replay(*events)
    assert [r.basis for r in resolutions] == ["conflict", "reopened", "conflict"]
    assert str(o_r3) not in resolutions[-1].input_observation_ids
    payload = dual.queue.adjudicated(
        dual.report, uuid4(), auto_resolution_id(t3), [o_r_new, o_r2]
    )
    dual.queue.replay(*events, ("screening.adjudicated", payload))
    with pytest.raises(DecisionReplayError, match="adjudicator reviewed"):
        dual.queue.replay(*events, ("screening.adjudicated", payload, {"actor": r3}))


# --- GOO-304: research_extraction family ------------------------------------


class _Extraction:
    """Hand-built extraction events for one matrix stream."""

    def __init__(self) -> None:
        self.collection_id, self.matrix_id = uuid4(), uuid4()
        self.version, self.document, self.field = uuid4(), uuid4(), uuid4()

    def observed(
        self, *obs_ids: UUID, kind: str = "machine", field: UUID | None = None
    ) -> dict[str, object]:
        machine = kind == "machine"
        return {
            "collection_id": str(self.collection_id),
            "matrix_id": str(self.matrix_id),
            "form_version_id": str(self.version),
            "form_content_hash": "c" * 64,
            "document_id": str(self.document),
            "source_hash": "d" * 64,
            "kind": kind,
            "observations": {str(o): str(field or self.field) for o in obs_ids},
            "extractor_run_id": "task-1" if machine else None,
            "extractor_model": "gpt-4o-mini" if machine else None,
        }

    def accepted(
        self,
        accepted_id: UUID,
        cited: list[UUID],
        supersedes: UUID | None = None,
        field: UUID | None = None,
    ) -> dict[str, object]:
        return {
            "collection_id": str(self.collection_id),
            "matrix_id": str(self.matrix_id),
            "accepted_value_id": str(accepted_id),
            "form_version_id": str(self.version),
            "document_id": str(self.document),
            "field_id": str(field or self.field),
            "observation_ids": [str(c) for c in cited],
            "value": 12,
            "missingness": None,
            "supersedes_accepted_value_id": str(supersedes) if supersedes else None,
            "source_hash": "d" * 64,
        }

    def staled(self, *accepted_ids: UUID) -> dict[str, object]:
        return {
            "collection_id": str(self.collection_id),
            "matrix_id": str(self.matrix_id),
            "new_form_version_id": str(uuid4()),
            "accepted_value_ids": [str(a) for a in accepted_ids],
        }

    def validate(
        self,
        event_type: str,
        payload: dict[str, object],
        reason: str | None = "ok",
        version: int = 1,
    ) -> None:
        _validate_event(
            aggregate_type="research_extraction",
            aggregate_id=self.matrix_id,
            event_type=event_type,
            event_schema_version=version,
            subject_type="extraction_matrix",
            subject_id=self.matrix_id,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            payload=payload,
            request_fingerprint="b" * 64,
            reason=reason,
        )

    def replay(self, *events: tuple[str, str, dict[str, object]]) -> None:
        stored = []
        for event_type, role, payload in events:
            event = _stored(event_type, payload)
            event.actor_role = role
            stored.append(event)
        _validate_extraction_transitions(stored, self.matrix_id)


def test_extraction_replay_rejects_worker_acceptance() -> None:
    ex = _Extraction()
    obs, acc = uuid4(), uuid4()
    observed = ("extraction.observed", "machine", ex.observed(obs))
    ex.replay(observed, ("extraction.accepted", "adjudicator", ex.accepted(acc, [obs])))
    for role in ("machine", "reviewer", "editor"):
        with pytest.raises(DecisionReplayError, match="adjudicator"):
            ex.replay(observed, ("extraction.accepted", role, ex.accepted(acc, [obs])))


def test_extraction_replay_rejects_accept_citing_unobserved_or_other_field() -> None:
    ex = _Extraction()
    obs, other = uuid4(), uuid4()
    events = (
        ("extraction.observed", "machine", ex.observed(obs)),
        (
            "extraction.observed",
            "reviewer",
            ex.observed(other, kind="human", field=uuid4()),
        ),
    )
    with pytest.raises(DecisionReplayError, match="cites"):
        ex.replay(
            *events,
            ("extraction.accepted", "adjudicator", ex.accepted(uuid4(), [uuid4()])),
        )
    with pytest.raises(DecisionReplayError, match="cites"):
        ex.replay(
            *events,
            ("extraction.accepted", "adjudicator", ex.accepted(uuid4(), [obs, other])),
        )
    with pytest.raises(DecisionReplayError, match="reused"):
        ex.replay(*events, ("extraction.observed", "machine", ex.observed(obs)))


def test_extraction_replay_rejects_forked_accept_chain() -> None:
    ex = _Extraction()
    obs, first, second = uuid4(), uuid4(), uuid4()
    base = (
        ("extraction.observed", "machine", ex.observed(obs)),
        ("extraction.accepted", "adjudicator", ex.accepted(first, [obs])),
    )
    ex.replay(
        *base, ("extraction.accepted", "adjudicator", ex.accepted(second, [obs], first))
    )
    with pytest.raises(DecisionReplayError, match="forked"):
        ex.replay(
            *base, ("extraction.accepted", "adjudicator", ex.accepted(second, [obs]))
        )
    with pytest.raises(DecisionReplayError, match="forked"):
        ex.replay(
            *base,
            ("extraction.accepted", "adjudicator", ex.accepted(second, [obs], first)),
            ("extraction.accepted", "adjudicator", ex.accepted(uuid4(), [obs], first)),
        )
    # staled names only current, not-yet-staled tips.
    ex.replay(*base, ("extraction.staled", "editor", ex.staled(first)))
    with pytest.raises(DecisionReplayError, match="stale"):
        ex.replay(
            *base,
            ("extraction.accepted", "adjudicator", ex.accepted(second, [obs], first)),
            ("extraction.staled", "editor", ex.staled(first)),
        )
    with pytest.raises(DecisionReplayError, match="stale"):
        ex.replay(
            *base,
            ("extraction.staled", "editor", ex.staled(first)),
            ("extraction.staled", "editor", ex.staled(first)),
        )
    same_version = {**ex.staled(first), "new_form_version_id": str(ex.version)}
    with pytest.raises(DecisionReplayError, match="stale"):
        ex.replay(*base, ("extraction.staled", "editor", same_version))


def test_extraction_replay_rejects_machine_without_extractor() -> None:
    ex = _Extraction()
    obs = uuid4()
    no_extractor = {**ex.observed(obs), "extractor_run_id": None}
    with pytest.raises(DecisionReplayError, match="extractor"):
        ex.replay(("extraction.observed", "machine", no_extractor))
    with pytest.raises(DecisionReplayError, match="extractor"):
        ex.replay(("extraction.observed", "reviewer", ex.observed(obs)))
    human_with_model = {**ex.observed(obs, kind="human"), "extractor_model": "x"}
    with pytest.raises(DecisionReplayError, match="extractor"):
        ex.replay(("extraction.observed", "reviewer", human_with_model))
    with pytest.raises(DecisionReplayError, match="extractor"):
        ex.replay(("extraction.observed", "machine", ex.observed(obs, kind="human")))
    ex.replay(("extraction.observed", "reviewer", ex.observed(obs, kind="human")))
    foreign = {**ex.observed(uuid4()), "collection_id": str(uuid4())}
    with pytest.raises(DecisionReplayError, match="another collection"):
        ex.replay(
            ("extraction.observed", "machine", ex.observed(obs)),
            ("extraction.observed", "machine", foreign),
        )


def test_extraction_payload_keys_exact() -> None:
    ex = _Extraction()
    obs = uuid4()
    ex.validate("extraction.observed", ex.observed(obs))
    ex.validate("extraction.accepted", ex.accepted(uuid4(), [obs]))
    ex.validate("extraction.staled", ex.staled(uuid4()))
    for event_type, payload in (
        ("extraction.observed", ex.observed(obs)),
        ("extraction.accepted", ex.accepted(uuid4(), [obs])),
        ("extraction.staled", ex.staled(uuid4())),
    ):
        with pytest.raises(DecisionValidationError, match="event schema"):
            ex.validate(event_type, {**payload, "extra": 1})
        trimmed = dict(payload)
        trimmed.pop("matrix_id")
        with pytest.raises(DecisionValidationError, match="event schema"):
            ex.validate(event_type, trimmed)
        with pytest.raises(DecisionValidationError, match="another extraction matrix"):
            ex.validate(event_type, {**payload, "matrix_id": str(uuid4())})
    with pytest.raises(DecisionValidationError, match="kind"):
        ex.validate("extraction.observed", {**ex.observed(obs), "kind": "agent"})
    with pytest.raises(DecisionValidationError, match="observations"):
        ex.validate("extraction.observed", {**ex.observed(obs), "observations": {}})
    with pytest.raises(DecisionValidationError, match="not a UUID"):
        ex.validate("extraction.observed", {**ex.observed(obs), "document_id": "x"})
    with pytest.raises(DecisionValidationError, match="missingness"):
        ex.validate(
            "extraction.accepted",
            {
                **ex.accepted(uuid4(), [obs]),
                "value": None,
                "missingness": "extraction_error",
            },
        )
    with pytest.raises(DecisionValidationError, match="exactly one"):
        ex.validate(
            "extraction.accepted",
            {**ex.accepted(uuid4(), [obs]), "missingness": "not_reported"},
        )
    with pytest.raises(DecisionValidationError, match="observation_ids"):
        ex.validate("extraction.accepted", ex.accepted(uuid4(), []))
    with pytest.raises(DecisionValidationError, match="rationale"):
        ex.validate("extraction.accepted", ex.accepted(uuid4(), [obs]), reason=None)


def test_extraction_replay_rejects_staled_by_non_editor() -> None:
    ex = _Extraction()
    obs, accepted = uuid4(), uuid4()
    base = (
        ("extraction.observed", "machine", ex.observed(obs)),
        ("extraction.accepted", "adjudicator", ex.accepted(accepted, [obs])),
    )
    ex.replay(*base, ("extraction.staled", "editor", ex.staled(accepted)))
    for role in ("machine", "reviewer", "adjudicator"):
        with pytest.raises(DecisionReplayError, match="editor"):
            ex.replay(*base, ("extraction.staled", role, ex.staled(accepted)))


# --- GOO-305: anchor fields as v2 extraction payloads -----------------------


def _anchor(
    ex: _Extraction,
    status: str | None = "verified",
    start: int | None = 10,
    occurrences: list[int] | None = None,
) -> dict[str, object]:
    return {
        "field_id": str(ex.field),
        "anchor_status": status,
        "citation_sha256": "e" * 64,
        "start": start,
        "end": None if start is None else start + 5,
        "page": 3 if start is not None else None,
        "occurrences": [start] if occurrences is None and start else occurrences or [],
    }


def _observed_v2(
    ex: _Extraction, anchors: dict[UUID, dict[str, object]]
) -> dict[str, object]:
    return {
        **ex.observed(),
        "observations": {str(o): a for o, a in anchors.items()},
        "text_sha256": "f" * 64,
        "inspected_coverage": [[0, 100]],
    }


def _accepted_v2(
    ex: _Extraction,
    accepted: UUID,
    obs: UUID,
    resolution: str = "verified",
    start: int | None = 10,
    supersedes: UUID | None = None,
) -> dict[str, object]:
    return {
        **ex.accepted(accepted, [obs], supersedes),
        "anchor_observation_id": str(obs),
        "anchor_resolution": resolution,
        "anchor_start_char": start,
        "text_sha256": "f" * 64,
    }


def _staled_v2(
    ex: _Extraction, accepted: UUID, source: str = "d" * 64, text: str = "0" * 64
) -> dict[str, object]:
    return {
        **ex.staled(accepted),
        "new_form_version_id": None,
        "reason": "source_changed",
        "document_id": str(ex.document),
        "new_source_hash": source,
        "new_text_sha256": text,
    }


def test_observed_v2_requires_anchor_keys() -> None:
    ex = _Extraction()
    obs = uuid4()
    payload = _observed_v2(ex, {obs: _anchor(ex)})
    ex.validate("extraction.observed", payload, version=2)
    with pytest.raises(DecisionValidationError, match="event schema"):
        ex.validate("extraction.observed", ex.observed(obs), version=2)
    with pytest.raises(DecisionValidationError, match="event schema"):
        ex.validate("extraction.observed", payload, version=1)
    bad_anchors: list[tuple[dict[str, object], str]] = [
        ({**_anchor(ex), "extra": 1}, "anchor does not match"),
        ({**_anchor(ex), "anchor_status": "fuzzy"}, "anchor_status"),
        ({**_anchor(ex), "end": 10}, "0 <= start < end"),
        ({**_anchor(ex), "end": None}, "0 <= start < end"),
        (_anchor(ex, "unverified"), "only a verified"),
        ({**_anchor(ex), "citation_sha256": "E" * 64}, "citation_sha256"),
        ({**_anchor(ex), "occurrences": list(range(21))}, "occurrences"),
    ]
    for anchor, message in bad_anchors:
        with pytest.raises(DecisionValidationError, match=message):
            ex.validate(
                "extraction.observed", _observed_v2(ex, {obs: anchor}), version=2
            )
    for coverage in ([[5, 5]], [[0, 10], [5, 20]], [[0, 10], [10, 20]], "x"):
        with pytest.raises(DecisionValidationError, match="inspected_coverage"):
            ex.validate(
                "extraction.observed",
                {**payload, "inspected_coverage": coverage},
                version=2,
            )
    with pytest.raises(DecisionValidationError, match="text_sha256"):
        ex.validate("extraction.observed", {**payload, "text_sha256": None}, version=2)
    missing = _observed_v2(ex, {obs: _anchor(ex, None, None)})
    ex.validate(
        "extraction.observed", {**missing, "inspected_coverage": None}, version=2
    )
    accepted = _accepted_v2(ex, uuid4(), obs)
    ex.validate("extraction.accepted", accepted, version=2)
    with pytest.raises(DecisionValidationError, match="names its anchor"):
        ex.validate(
            "extraction.accepted", {**accepted, "anchor_start_char": None}, version=2
        )
    with pytest.raises(DecisionValidationError, match="must be cited"):
        ex.validate(
            "extraction.accepted",
            {**accepted, "anchor_observation_id": str(uuid4())},
            version=2,
        )
    with pytest.raises(DecisionValidationError, match="anchor_resolution"):
        ex.validate(
            "extraction.accepted", {**accepted, "anchor_resolution": "x"}, version=2
        )
    staled = _staled_v2(ex, uuid4())
    ex.validate("extraction.staled", staled, version=2)
    with pytest.raises(DecisionValidationError, match="new_text_sha256"):
        ex.validate("extraction.staled", {**staled, "new_text_sha256": None}, version=2)
    with pytest.raises(DecisionValidationError, match="reason"):
        ex.validate("extraction.staled", {**staled, "reason": "bored"}, version=2)
    form = {
        **staled,
        "reason": "form_changed",
        "new_form_version_id": str(uuid4()),
    }
    with pytest.raises(DecisionValidationError, match="names no source"):
        ex.validate("extraction.staled", form, version=2)
    ex.validate(
        "extraction.staled",
        {**form, "document_id": None, "new_source_hash": None, "new_text_sha256": None},
        version=2,
    )


def test_v1_extraction_history_still_replays() -> None:
    ex = _Extraction()
    old, new, first, second = uuid4(), uuid4(), uuid4(), uuid4()
    ex.replay(
        ("extraction.observed", "machine", ex.observed(old)),
        ("extraction.accepted", "adjudicator", ex.accepted(first, [old])),
        ("extraction.observed", "machine", _observed_v2(ex, {new: _anchor(ex)})),
        (
            "extraction.accepted",
            "adjudicator",
            _accepted_v2(ex, second, new, supersedes=first),
        ),
        ("extraction.staled", "editor", ex.staled(second)),
    )


def test_accepted_verified_on_unverified_observation_fails_replay() -> None:
    ex = _Extraction()
    obs, accepted = uuid4(), uuid4()
    for anchor in (_anchor(ex, "unverified", None), _anchor(ex, start=11)):
        with pytest.raises(DecisionReplayError, match="contradicts observation"):
            ex.replay(
                ("extraction.observed", "machine", _observed_v2(ex, {obs: anchor})),
                ("extraction.accepted", "adjudicator", _accepted_v2(ex, accepted, obs)),
            )
    unverified = _accepted_v2(ex, accepted, obs, "accepted_unverified", None)
    ex.replay(
        (
            "extraction.observed",
            "machine",
            _observed_v2(ex, {obs: _anchor(ex, "unverified", None)}),
        ),
        ("extraction.accepted", "adjudicator", unverified),
    )


def test_disambiguated_start_must_be_a_recorded_occurrence() -> None:
    ex = _Extraction()
    obs, accepted = uuid4(), uuid4()
    observed = (
        "extraction.observed",
        "machine",
        _observed_v2(ex, {obs: _anchor(ex, "ambiguous", None, [4, 37])}),
    )
    ex.replay(
        observed,
        (
            "extraction.accepted",
            "adjudicator",
            _accepted_v2(ex, accepted, obs, "disambiguated", 37),
        ),
    )
    with pytest.raises(DecisionReplayError, match="contradicts observation"):
        ex.replay(
            observed,
            (
                "extraction.accepted",
                "adjudicator",
                _accepted_v2(ex, accepted, obs, "disambiguated", 5),
            ),
        )


def test_source_stale_requires_hash_change() -> None:
    ex = _Extraction()
    obs, accepted = uuid4(), uuid4()
    base = (
        ("extraction.observed", "machine", _observed_v2(ex, {obs: _anchor(ex)})),
        ("extraction.accepted", "adjudicator", _accepted_v2(ex, accepted, obs)),
    )
    for role in ("adjudicator", "machine", "editor"):
        ex.replay(*base, ("extraction.staled", role, _staled_v2(ex, accepted)))
    ex.replay(
        *base, ("extraction.staled", "machine", _staled_v2(ex, accepted, "a" * 64))
    )
    unchanged = _staled_v2(ex, accepted, "d" * 64, "f" * 64)
    with pytest.raises(DecisionReplayError, match="stale without source change"):
        ex.replay(*base, ("extraction.staled", "adjudicator", unchanged))
    with pytest.raises(DecisionReplayError, match="actor"):
        ex.replay(*base, ("extraction.staled", "reviewer", _staled_v2(ex, accepted)))
    twice = ("extraction.staled", "machine", _staled_v2(ex, accepted))
    with pytest.raises(DecisionReplayError, match="current tip"):
        ex.replay(*base, twice, twice)


class _Claims:
    """Hand-built events for one Collection's research_claims stream."""

    def __init__(self) -> None:
        self.collection_id, self.claim = uuid4(), uuid4()
        self.document = uuid4()

    def _base(self, claim: UUID | None = None) -> dict[str, Any]:
        return {
            "collection_id": str(self.collection_id),
            "claim_id": str(claim or self.claim),
        }

    def versioned(
        self, version: UUID, number: int = 1, supersedes: UUID | None = None
    ) -> dict[str, Any]:
        return self._base() | {
            "claim_version_id": str(version),
            "version_no": number,
            "supersedes_claim_version_id": str(supersedes) if supersedes else None,
            "kind": "factual",
            "attributed_to_user_id": None,
            "text_sha256": "a" * 64,
            "normalized_hash": "b" * 64,
            "draft_id": str(uuid4()),
            "draft_version": number,
            "draft_content_hash": "c" * 64,
            "start_char": 0,
            "end_char": 10,
            "draft_review_id": None,
        }

    def linked(
        self,
        version: UUID,
        link: UUID,
        kind: str = "source_span",
        supersedes: UUID | None = None,
        status: str = "linked",
    ) -> dict[str, Any]:
        span = kind == "source_span"
        return self._base() | {
            "claim_version_id": str(version),
            "link_id": str(link),
            "supersedes_link_id": str(supersedes) if supersedes else None,
            "status": status,
            "kind": kind,
            "accepted_value_id": str(uuid4()) if kind == "extraction" else None,
            "draft_citation_id": (
                str(uuid4()) if kind == "legacy_unanchored" else None
            ),
            "document_id": str(self.document),
            "source_hash": None if kind == "legacy_unanchored" else "d" * 64,
            "text_sha256": None if kind == "legacy_unanchored" else "e" * 64,
            "start_char": 3 if span else None,
            "end_char": 9 if span else None,
            "quote_sha256": "f" * 64 if span else None,
        }

    def observed(self, link: UUID, observation: UUID) -> dict[str, Any]:
        return self._base() | {
            "link_id": str(link),
            "observation_id": str(observation),
            "stance": "supporting",
            "stance_classification_id": str(uuid4()),
            "classifier_version": "stance-v1",
            "inference_model_version": "gpt-4.1",
            "source_content_hash": "e" * 64,
            "classified_at": "2026-09-30T00:00:00+00:00",
        }

    def assessed(
        self,
        version: UUID,
        assessment: UUID,
        links: list[UUID],
        observations: list[UUID] | None = None,
        supersedes: UUID | None = None,
        stance: str = "supporting",
    ) -> dict[str, Any]:
        return self._base() | {
            "claim_version_id": str(version),
            "assessment_id": str(assessment),
            "supersedes_assessment_id": str(supersedes) if supersedes else None,
            "stance": stance,
            "link_ids": [str(link) for link in links],
            "stance_observation_ids": [str(o) for o in observations or []],
        }

    def validate(
        self, event_type: str, payload: dict[str, Any], reason: str | None = "why"
    ) -> None:
        _validate_event(
            aggregate_type="research_claims",
            aggregate_id=self.collection_id,
            event_type=event_type,
            event_schema_version=1,
            subject_type="research_claim",
            subject_id=self.claim,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            payload=payload,
            request_fingerprint="b" * 64,
            reason=reason,
        )

    def replay(self, *events: tuple[str, str, dict[str, Any]]) -> None:
        stored = []
        for event_type, role, payload in events:
            event = _stored(event_type, payload)
            event.actor_role = role
            stored.append(event)
        _validate_claims_transitions(stored, self.collection_id)


def _claim_history(c: _Claims) -> tuple[UUID, UUID, list[tuple[str, str, Any]]]:
    """v1 with one span link observed once."""
    version, link, observation = uuid4(), uuid4(), uuid4()
    return (
        version,
        link,
        [
            ("claim.versioned", "editor", c.versioned(version)),
            ("claim.linked", "editor", c.linked(version, link)),
            ("claim.observed", "machine", c.observed(link, observation)),
        ],
    )


def test_claims_replay_rejects_machine_assessment() -> None:
    c = _Claims()
    version, link, history = _claim_history(c)
    assessed = c.assessed(version, uuid4(), [link])
    c.replay(*history, ("claim.assessed", "adjudicator", assessed))
    for role in ("machine", "editor", "reviewer"):
        with pytest.raises(DecisionReplayError, match="adjudicator"):
            c.replay(*history, ("claim.assessed", role, assessed))
    with pytest.raises(DecisionReplayError, match="machine"):
        c.replay(*history, ("claim.observed", "editor", c.observed(link, uuid4())))


def test_claims_replay_rejects_forked_version_chain() -> None:
    c = _Claims()
    v1, v2 = uuid4(), uuid4()
    first = ("claim.versioned", "editor", c.versioned(v1))
    c.replay(first, ("claim.versioned", "editor", c.versioned(v2, 2, v1)))
    for bad in (
        c.versioned(v2),  # a second v1
        c.versioned(v2, 3, v1),  # skips a number
        c.versioned(v2, 2, uuid4()),  # supersedes a non-tip
    ):
        with pytest.raises(DecisionReplayError, match="forked"):
            c.replay(first, ("claim.versioned", "editor", bad))
    with pytest.raises(DecisionReplayError, match="forked"):
        c.replay(
            first,
            ("claim.versioned", "editor", c.versioned(v2, 2, v1)),
            ("claim.versioned", "editor", c.versioned(uuid4(), 2, v1)),
        )
    other_claim = {**c.versioned(v2), "claim_id": str(uuid4())}
    with pytest.raises(DecisionReplayError, match="reused"):
        c.replay(
            first,
            ("claim.versioned", "editor", {**other_claim, "claim_version_id": str(v1)}),
        )


def test_claims_replay_rejects_link_to_non_tip_version() -> None:
    c = _Claims()
    v1, v2, link = uuid4(), uuid4(), uuid4()
    versions = (
        ("claim.versioned", "editor", c.versioned(v1)),
        ("claim.versioned", "editor", c.versioned(v2, 2, v1)),
    )
    c.replay(*versions, ("claim.linked", "editor", c.linked(v2, link)))
    with pytest.raises(DecisionReplayError, match="non-tip"):
        c.replay(*versions, ("claim.linked", "editor", c.linked(v1, link)))
    with pytest.raises(DecisionReplayError, match="non-tip"):
        c.replay(*versions, ("claim.linked", "editor", c.linked(uuid4(), link)))
    # A link chain names its tip, on the same version.
    first = ("claim.linked", "editor", c.linked(v2, link))
    c.replay(
        *versions,
        first,
        ("claim.linked", "editor", c.linked(v2, uuid4(), supersedes=link)),
    )
    with pytest.raises(DecisionReplayError, match="forked"):
        c.replay(
            *versions,
            first,
            ("claim.linked", "editor", c.linked(v2, uuid4(), supersedes=link)),
            ("claim.linked", "editor", c.linked(v2, uuid4(), supersedes=link)),
        )


def test_claims_replay_rejects_assessment_citing_withdrawn_link() -> None:
    c = _Claims()
    version, link, history = _claim_history(c)
    withdrawn = c.linked(version, uuid4(), supersedes=link, status="withdrawn")
    with pytest.raises(DecisionReplayError, match="dead link"):
        c.replay(
            *history,
            ("claim.linked", "editor", withdrawn),
            ("claim.assessed", "adjudicator", c.assessed(version, uuid4(), [link])),
        )
    with pytest.raises(DecisionReplayError, match="foreign observation"):
        c.replay(
            *history,
            (
                "claim.assessed",
                "adjudicator",
                c.assessed(version, uuid4(), [link], [uuid4()]),
            ),
        )
    first = uuid4()
    base = (
        *history,
        ("claim.assessed", "adjudicator", c.assessed(version, first, [link])),
    )
    c.replay(
        *base,
        (
            "claim.assessed",
            "adjudicator",
            c.assessed(version, uuid4(), [link], supersedes=first),
        ),
    )
    with pytest.raises(DecisionReplayError, match="forked"):
        c.replay(
            *base,
            ("claim.assessed", "adjudicator", c.assessed(version, uuid4(), [link])),
        )


def test_claims_replay_rejects_observation_on_legacy_link() -> None:
    c = _Claims()
    version, legacy = uuid4(), uuid4()
    base = (
        ("claim.versioned", "editor", c.versioned(version)),
        ("claim.linked", "editor", c.linked(version, legacy, "legacy_unanchored")),
    )
    c.replay(*base)
    with pytest.raises(DecisionReplayError, match="live link"):
        c.replay(*base, ("claim.observed", "machine", c.observed(legacy, uuid4())))
    span = uuid4()
    other_revision = {**c.observed(span, uuid4()), "source_content_hash": "9" * 64}
    with pytest.raises(DecisionReplayError, match="revision"):
        c.replay(
            *base,
            ("claim.linked", "editor", c.linked(version, span)),
            ("claim.observed", "machine", other_revision),
        )


def test_claims_payload_keys_exact() -> None:
    c = _Claims()
    version, link = uuid4(), uuid4()
    samples = (
        ("claim.versioned", c.versioned(version)),
        ("claim.linked", c.linked(version, link)),
        ("claim.linked", c.linked(version, link, "extraction")),
        ("claim.linked", c.linked(version, link, "legacy_unanchored")),
        ("claim.observed", c.observed(link, uuid4())),
        ("claim.assessed", c.assessed(version, uuid4(), [link])),
    )
    for event_type, payload in samples:
        c.validate(event_type, payload)
        with pytest.raises(DecisionValidationError, match="event schema"):
            c.validate(event_type, {**payload, "extra": 1})
        trimmed = dict(payload)
        trimmed.pop("claim_id")
        with pytest.raises(DecisionValidationError, match="event schema"):
            c.validate(event_type, trimmed)
        with pytest.raises(DecisionValidationError, match="another collection"):
            c.validate(event_type, {**payload, "collection_id": str(uuid4())})
    with pytest.raises(DecisionValidationError, match="rationale"):
        c.validate("claim.assessed", c.assessed(version, uuid4(), [link]), reason=None)
    with pytest.raises(DecisionValidationError, match="must cite"):
        c.validate("claim.assessed", c.assessed(version, uuid4(), []))
    c.validate("claim.assessed", c.assessed(version, uuid4(), [], stance="unresolved"))
    with pytest.raises(DecisionValidationError, match="claim link"):
        c.validate(
            "claim.linked",
            {**c.linked(version, link, "legacy_unanchored"), "source_hash": "d" * 64},
        )
    with pytest.raises(DecisionValidationError, match="chain"):
        c.validate("claim.versioned", {**c.versioned(version), "version_no": 2})
    with pytest.raises(DecisionValidationError, match="attributed"):
        c.validate(
            "claim.versioned", {**c.versioned(version), "kind": "interpretation"}
        )
    with pytest.raises(DecisionValidationError, match="stance"):
        c.validate(
            "claim.observed", {**c.observed(link, uuid4()), "stance": "unresolved"}
        )


class _Release:
    """Hand-built events for one Collection's research_release stream."""

    def __init__(self) -> None:
        self.collection_id = uuid4()

    def promoted(self, release: UUID, draft: UUID) -> dict[str, Any]:
        return {
            "collection_id": str(self.collection_id),
            "release_id": str(release),
            "draft_id": str(draft),
            "draft_version": 1,
            "content_hash": "a" * 64,
            "claim_version_ids": [str(uuid4())],
            "assessment_ids": [str(uuid4())],
            "interpretation_claim_version_ids": [],
            "protocol_version_id": None,
            "policy_version": 1,
            "dimensions": {"support": {"status": "passed"}},
        }

    def staled(self, *releases: UUID) -> dict[str, Any]:
        return {
            "collection_id": str(self.collection_id),
            "release_ids": [str(r) for r in releases],
            "cause": {
                "family": "research_claims",
                "event_id": str(uuid4()),
                "kind": "claim.assessed",
            },
            "changed_nodes": [f"assessment:{uuid4()}"],
            "assessment_ids": [],
        }

    def validate(self, event_type: str, payload: dict[str, Any], subject: UUID) -> None:
        _validate_event(
            aggregate_type="research_release",
            aggregate_id=self.collection_id,
            event_type=event_type,
            event_schema_version=1,
            subject_type="draft_release",
            subject_id=subject,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            payload=payload,
            request_fingerprint="b" * 64,
        )

    def replay(self, *events: tuple[str, str, dict[str, Any]]) -> None:
        stored = []
        for event_type, role, payload in events:
            event = _stored(event_type, payload)
            event.actor_role = role
            stored.append(event)
        _validate_release_transitions(stored, self.collection_id)


def test_release_replay_rejects_reviewer_promotion() -> None:
    r = _Release()
    promoted = r.promoted(uuid4(), uuid4())
    for role in ("adjudicator", "supervisor"):
        r.replay(("release.promoted", role, promoted))
    for role in ("reviewer", "editor", "machine"):
        with pytest.raises(DecisionReplayError, match="adjudicator or supervisor"):
            r.replay(("release.promoted", role, promoted))


def test_release_replay_rejects_second_live_release() -> None:
    r = _Release()
    draft, first, second = uuid4(), uuid4(), uuid4()
    with pytest.raises(DecisionReplayError, match="live release"):
        r.replay(
            ("release.promoted", "adjudicator", r.promoted(first, draft)),
            ("release.promoted", "supervisor", r.promoted(second, draft)),
        )
    # Re-promotion after staling inserts a new live release.
    r.replay(
        ("release.promoted", "adjudicator", r.promoted(first, draft)),
        ("release.staled", "adjudicator", r.staled(first)),
        ("release.promoted", "supervisor", r.promoted(second, draft)),
    )
    r.replay(
        ("release.promoted", "adjudicator", r.promoted(first, draft)),
        ("release.promoted", "adjudicator", r.promoted(second, uuid4())),
    )


def test_release_replay_rejects_double_stale() -> None:
    r = _Release()
    release = uuid4()
    promoted = ("release.promoted", "adjudicator", r.promoted(release, uuid4()))
    r.replay(promoted, ("release.staled", "editor", r.staled(release)))
    with pytest.raises(DecisionReplayError, match="non-live"):
        r.replay(
            promoted,
            ("release.staled", "editor", r.staled(release)),
            ("release.staled", "machine", r.staled(release)),
        )
    with pytest.raises(DecisionReplayError, match="non-live"):
        r.replay(("release.staled", "editor", r.staled(uuid4())))


def test_release_payload_keys_exact() -> None:
    r = _Release()
    release = uuid4()
    samples = (
        ("release.promoted", r.promoted(release, uuid4()), release),
        ("release.staled", r.staled(release), r.collection_id),
    )
    for event_type, payload, subject in samples:
        r.validate(event_type, payload, subject)
        with pytest.raises(DecisionValidationError, match="event schema"):
            r.validate(event_type, {**payload, "extra": 1}, subject)
        trimmed = dict(payload)
        trimmed.pop("collection_id")
        with pytest.raises(DecisionValidationError, match="event schema"):
            r.validate(event_type, trimmed, subject)
        with pytest.raises(DecisionValidationError, match="another collection"):
            r.validate(event_type, {**payload, "collection_id": str(uuid4())}, subject)
    promoted, staled = samples[0][1], samples[1][1]
    with pytest.raises(DecisionValidationError, match="subject"):
        r.validate("release.promoted", promoted, uuid4())
    with pytest.raises(DecisionValidationError, match="SHA-256"):
        r.validate("release.promoted", {**promoted, "content_hash": "x"}, release)
    with pytest.raises(DecisionValidationError, match="cause"):
        r.validate(
            "release.staled",
            {**staled, "cause": {**staled["cause"], "family": "research_protocol"}},
            r.collection_id,
        )
    with pytest.raises(DecisionValidationError, match="release_ids"):
        r.validate("release.staled", {**staled, "release_ids": []}, r.collection_id)


class _Appraisal:
    """Hand-built events for one Collection's research_appraisal stream."""

    def __init__(self, mode: str = "dual_independent") -> None:
        self.collection_id = uuid4()
        self.mode = mode
        self.events: list[ResearchDecisionEvent] = []

    def payload(
        self, assessment: UUID, supersedes: UUID | None = None, **extra: Any
    ) -> dict[str, Any]:
        return {
            "collection_id": str(self.collection_id),
            "assessment_id": str(assessment),
            "supersedes_assessment_id": None if supersedes is None else str(supersedes),
            "target_key": "study:" + str(self.collection_id),
            "outcome_key": "depressive_symptoms",
            "timepoint": "12 weeks",
            "instrument_key": "rob2",
            "instrument_version": "2019-08-22",
            "instrument_spec_hash": "a" * 64,
            "protocol_version_id": str(uuid4()),
            "mode": self.mode,
            "study_design": "randomized_parallel_group",
            "applicability": "applicable",
            "overall": None,
            "unresolved_domains": ["D3"],
            "input_hash": "b" * 64,
            **extra,
        }

    def add(
        self,
        event_type: str,
        role: str,
        actor: UUID,
        assessment: UUID,
        supersedes: UUID | None = None,
        **extra: Any,
    ) -> UUID:
        event = _stored(event_type, self.payload(assessment, supersedes, **extra))
        event.actor_role = role
        event.actor_user_id = actor
        self.events.append(event)
        return assessment

    def submit(self, actor: UUID, supersedes: UUID | None = None) -> UUID:
        return self.add("appraisal.submitted", "reviewer", actor, uuid4(), supersedes)

    def adjudicate(
        self, actor: UUID, resolves: list[UUID], supersedes: UUID | None = None
    ) -> UUID:
        return self.add(
            "appraisal.adjudicated",
            "adjudicator",
            actor,
            uuid4(),
            supersedes,
            resolves_assessment_ids=[str(r) for r in resolves],
        )

    def replay(self) -> None:
        _validate_appraisal_transitions(self.events, self.collection_id)

    def validate(self, event_type: str, payload: dict[str, Any]) -> None:
        _validate_event(
            aggregate_type="research_appraisal",
            aggregate_id=self.collection_id,
            event_type=event_type,
            event_schema_version=1,
            subject_type="appraisal_assessment",
            subject_id=UUID(payload["assessment_id"]),
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            payload=payload,
            request_fingerprint="c" * 64,
            reason="adjudicated",
        )


def test_appraisal_replay_rejects_post_reveal_independent_edit() -> None:
    a, b = uuid4(), uuid4()
    r = _Appraisal()
    first = r.submit(a)
    first = r.submit(a, supersedes=first)  # before reveal: allowed
    r.submit(b)
    r.replay()
    r.submit(a, supersedes=first)
    with pytest.raises(DecisionReplayError, match="after reveal"):
        r.replay()
    # A forked chain is refused too; single mode may keep superseding.
    fork = _Appraisal()
    fork.submit(a)
    fork.submit(a, supersedes=uuid4())
    with pytest.raises(DecisionReplayError, match="non-tip"):
        fork.replay()
    single = _Appraisal("single")
    tip = single.submit(a)
    single.submit(a, supersedes=tip)
    single.replay()
    wrong_role = _Appraisal()
    wrong_role.add("appraisal.submitted", "adjudicator", a, uuid4())
    with pytest.raises(DecisionReplayError, match="requires a reviewer"):
        wrong_role.replay()


def test_appraisal_replay_rejects_self_adjudication() -> None:
    a, b, j = uuid4(), uuid4(), uuid4()
    r = _Appraisal()
    tips = [r.submit(a), r.submit(b)]
    r.adjudicate(a, tips)
    with pytest.raises(DecisionReplayError, match="assessed this result"):
        r.replay()
    ok = _Appraisal()
    tips = [ok.submit(a), ok.submit(b)]
    first = ok.adjudicate(j, tips)
    ok.adjudicate(j, tips, supersedes=first)
    ok.replay()
    reviewer = _Appraisal()
    tips = [reviewer.submit(a), reviewer.submit(b)]
    reviewer.add(
        "appraisal.adjudicated",
        "reviewer",
        j,
        uuid4(),
        resolves_assessment_ids=[str(t) for t in tips],
    )
    with pytest.raises(DecisionReplayError, match="requires an adjudicator"):
        reviewer.replay()


def test_appraisal_replay_rejects_resolving_stale_tips() -> None:
    a, b, j = uuid4(), uuid4(), uuid4()
    r = _Appraisal()
    old = r.submit(a)
    new = r.submit(a, supersedes=old)
    other = r.submit(b)
    r.adjudicate(j, [old, other])
    with pytest.raises(DecisionReplayError, match="stale tips"):
        r.replay()
    r.events.pop()
    r.adjudicate(j, [new, other])
    r.replay()


def test_appraisal_payload_keys_exact() -> None:
    r = _Appraisal()
    submitted = r.payload(uuid4())
    adjudicated = {**r.payload(uuid4()), "resolves_assessment_ids": [str(uuid4())]}
    for event_type, payload in (
        ("appraisal.submitted", submitted),
        ("appraisal.adjudicated", adjudicated),
    ):
        r.validate(event_type, payload)
        with pytest.raises(DecisionValidationError, match="event schema"):
            r.validate(event_type, {**payload, "extra": 1})
        trimmed = dict(payload)
        trimmed.pop("input_hash")
        with pytest.raises(DecisionValidationError, match="event schema"):
            r.validate(event_type, trimmed)
        with pytest.raises(DecisionValidationError, match="another collection"):
            r.validate(event_type, {**payload, "collection_id": str(uuid4())})
    bad = (
        ({"overall": "medium"}, "overall"),
        ({"mode": "triple"}, "mode"),
        ({"study_design": "anecdote"}, "study_design"),
        ({"target_key": "document:1"}, "target_key"),
        ({"unresolved_domains": ["D9"]}, "unresolved_domains"),
        ({"input_hash": "x"}, "SHA-256"),
    )
    for change, message in bad:
        with pytest.raises(DecisionValidationError, match=message):
            r.validate("appraisal.submitted", {**submitted, **change})
    with pytest.raises(DecisionValidationError, match="1\\+ unique"):
        r.validate(
            "appraisal.adjudicated", {**adjudicated, "resolves_assessment_ids": []}
        )


class _Evidence:
    """Hand-built events for one Collection's research_evidence stream."""

    def __init__(self) -> None:
        self.collection_id = uuid4()
        self.events: list[ResearchDecisionEvent] = []

    def _add(self, event_type: str, role: str, payload: dict[str, Any]) -> None:
        event = _stored(event_type, payload)
        event.actor_role = role
        event.actor_user_id = uuid4()
        self.events.append(event)

    def table_payload(
        self,
        table: UUID,
        supersedes: UUID | None = None,
        outcome: str = "depressive_symptoms",
    ) -> dict[str, Any]:
        return {
            "collection_id": str(self.collection_id),
            "table_version_id": str(table),
            "supersedes_table_id": None if supersedes is None else str(supersedes),
            "outcome_key": outcome,
            "timepoint": "12 weeks",
            "protocol_version_id": str(uuid4()),
            "matrix_id": str(uuid4()),
            "form_version_id": str(uuid4()),
            "field_ids": [str(uuid4())],
            "content_hash": "a" * 64,
            "row_count": 2,
            "excluded_count": 1,
        }

    def table(
        self, supersedes: UUID | None = None, outcome: str = "depressive_symptoms"
    ) -> UUID:
        table = uuid4()
        self._add(
            "evidence.table_versioned",
            "reviewer",
            self.table_payload(table, supersedes, outcome),
        )
        return table

    def contradiction_payload(
        self,
        kind: str,
        table: UUID,
        group: UUID | None = None,
        previous: UUID | None = None,
    ) -> dict[str, Any]:
        row = uuid4()
        opened = kind == "opened"
        return {
            "collection_id": str(self.collection_id),
            "contradiction_id": str(row if opened else group),
            "row_id": str(row),
            "previous_id": None if previous is None else str(previous),
            "kind": kind,
            "table_version_id": str(table),
            "field_id": str(uuid4()),
            "accepted_value_ids": [str(uuid4()), str(uuid4())] if opened else None,
            "suggestion_ids": [],
        }

    def contradiction(
        self,
        kind: str,
        role: str,
        table: UUID,
        group: UUID | None = None,
        previous: UUID | None = None,
    ) -> UUID:
        payload = self.contradiction_payload(kind, table, group, previous)
        self._add("evidence.contradiction_recorded", role, payload)
        return UUID(payload["row_id"])

    def certainty_payload(
        self,
        table: UUID,
        level: str | None = "moderate",
        supersedes: UUID | None = None,
    ) -> dict[str, Any]:
        return {
            "collection_id": str(self.collection_id),
            "certainty_id": str(uuid4()),
            "supersedes_certainty_id": None if supersedes is None else str(supersedes),
            "table_version_id": str(table),
            "outcome_key": "depressive_symptoms",
            "timepoint": "12 weeks",
            "method_key": "grade",
            "method_version": "handbook-2013",
            "starting_level": "high",
            "ratings": {
                "risk_of_bias": -1,
                "inconsistency": 0,
                "indirectness": 0,
                "imprecision": 0,
                "publication_bias": 0,
            },
            "level": level,
            "appraisal_assessment_ids": [str(uuid4())],
            "contradiction_ids": [],
            "input_hash": "b" * 64,
        }

    def certainty(self, table: UUID, level: str | None = "moderate") -> None:
        self._add(
            "evidence.certainty_assessed",
            "reviewer",
            self.certainty_payload(table, level),
        )

    def replay(self) -> None:
        _validate_evidence_transitions(self.events, self.collection_id)

    def validate(self, event_type: str, payload: dict[str, Any], subject: str) -> None:
        _validate_event(
            aggregate_type="research_evidence",
            aggregate_id=self.collection_id,
            event_type=event_type,
            event_schema_version=1,
            subject_type="evidence_outcome",
            subject_id=UUID(payload[subject]),
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            payload=payload,
            request_fingerprint="c" * 64,
            reason="explained",
        )


def test_evidence_replay_rejects_reviewer_resolution() -> None:
    r = _Evidence()
    table = r.table()
    group = r.contradiction("opened", "reviewer", table)
    resolved = r.contradiction("resolved", "adjudicator", table, group, group)
    r.contradiction("dissent", "reviewer", table, group, resolved)
    r.replay()
    bad = _Evidence()
    table = bad.table()
    group = bad.contradiction("opened", "reviewer", table)
    bad.contradiction("resolved", "reviewer", table, group, group)
    with pytest.raises(DecisionReplayError, match="needs an adjudicator"):
        bad.replay()
    fork = _Evidence()
    table = fork.table()
    group = fork.contradiction("opened", "reviewer", table)
    fork.contradiction("dissent", "reviewer", table, group, group)
    fork.contradiction("acknowledged", "adjudicator", table, group, group)
    with pytest.raises(DecisionReplayError, match="non-tip"):
        fork.replay()


def test_evidence_replay_rejects_forked_table_chain() -> None:
    r = _Evidence()
    first = r.table()
    r.table(supersedes=first)
    r.table(outcome="mean_age")  # another outcome's chain starts fresh
    r.replay()
    r.table(supersedes=first)
    with pytest.raises(DecisionReplayError, match="supersedes a non-tip"):
        r.replay()
    initial_twice = _Evidence()
    initial_twice.table()
    initial_twice.table()
    with pytest.raises(DecisionReplayError, match="supersedes a non-tip"):
        initial_twice.replay()
    foreign = _Evidence()
    foreign.certainty(foreign.table(outcome="mean_age"))
    with pytest.raises(DecisionReplayError, match="another outcome"):
        foreign.replay()


def test_evidence_replay_rejects_level_not_derived() -> None:
    r = _Evidence()
    table = r.table()
    r.certainty(table, "moderate")
    r.replay()
    wrong = _Evidence()
    wrong.certainty(wrong.table(), "high")
    with pytest.raises(DecisionReplayError, match="not derived"):
        wrong.replay()


def test_evidence_payload_keys_exact() -> None:
    r = _Evidence()
    table = uuid4()
    cases = (
        ("evidence.table_versioned", r.table_payload(table), "table_version_id"),
        (
            "evidence.contradiction_recorded",
            r.contradiction_payload("opened", table),
            "row_id",
        ),
        ("evidence.certainty_assessed", r.certainty_payload(table), "certainty_id"),
    )
    for event_type, payload, subject in cases:
        r.validate(event_type, payload, subject)
        with pytest.raises(DecisionValidationError, match="event schema"):
            r.validate(event_type, {**payload, "extra": 1}, subject)
        trimmed = dict(payload)
        trimmed.pop("collection_id")
        with pytest.raises(DecisionValidationError, match="event schema"):
            r.validate(event_type, trimmed, subject)
        with pytest.raises(DecisionValidationError, match="another collection"):
            r.validate(event_type, {**payload, "collection_id": str(uuid4())}, subject)
    certainty = r.certainty_payload(table)
    bad = (
        ({"level": "certain"}, "level"),
        ({"starting_level": "moderate"}, "starting_level"),
        ({"ratings": {**certainty["ratings"], "confidence": 0}}, "ratings"),
        ({"ratings": {**certainty["ratings"], "imprecision": -3}}, "ratings"),
        ({"input_hash": "x"}, "SHA-256"),
    )
    for change, message in bad:
        with pytest.raises(DecisionValidationError, match=message):
            r.validate(
                "evidence.certainty_assessed", {**certainty, **change}, "certainty_id"
            )
    opened = r.contradiction_payload("opened", table)
    with pytest.raises(DecisionValidationError, match="2\\+ unique"):
        r.validate(
            "evidence.contradiction_recorded",
            {**opened, "accepted_value_ids": [str(uuid4())]},
            "row_id",
        )
    dissent = r.contradiction_payload("dissent", table, uuid4(), uuid4())
    with pytest.raises(DecisionValidationError, match="only an opened row"):
        r.validate(
            "evidence.contradiction_recorded",
            {**dissent, "suggestion_ids": [str(uuid4())]},
            "row_id",
        )
    table_payload = r.table_payload(table)
    with pytest.raises(DecisionValidationError, match="field_ids"):
        r.validate(
            "evidence.table_versioned",
            {**table_payload, "field_ids": []},
            "table_version_id",
        )
    # A contradiction row and a certainty row always carry their explanation.
    for event_type, payload, subject in cases[1:]:
        with pytest.raises(DecisionValidationError, match="needs a rationale"):
            _validate_event(
                aggregate_type="research_evidence",
                aggregate_id=r.collection_id,
                event_type=event_type,
                event_schema_version=1,
                subject_type="evidence_outcome",
                subject_id=UUID(payload[subject]),
                subject_version_id=None,
                subject_hash=decision_request_fingerprint(payload),
                payload=payload,
                request_fingerprint="c" * 64,
                reason=None,
            )


# --- GOO-311: research_synthesis and claim.linked v2 --------------------------


class _Synthesis:
    """Hand-built events for one Collection's research_synthesis stream."""

    def __init__(self) -> None:
        self.collection_id = uuid4()

    def executed(
        self,
        result: UUID,
        supersedes: UUID | None = None,
        input_hash: str = "a" * 64,
        units: tuple[str, ...] = ("study:A", "study:B"),
        status: str = "computed",
    ) -> dict[str, Any]:
        return {
            "collection_id": str(self.collection_id),
            "result_id": str(result),
            "supersedes_result_id": None if supersedes is None else str(supersedes),
            "table_version_id": str(uuid4()),
            "protocol_version_id": str(uuid4()),
            "outcome_key": "depressive_symptoms",
            "timepoint": "12 weeks",
            "measure": "smd_hedges_g",
            "model": "random_effects_dl",
            "config_hash": "b" * 64,
            "estimator_version": "nous.smd-hedges-g.dl/1",
            "status": status,
            "input_hash": input_hash,
            "result_hash": "c" * 64,
            "included_units": list(units),
            "excluded": [
                {
                    "unit": "study:E",
                    "report_ids": [str(uuid4())],
                    "reason": "invalid_variance:c",
                    "detail": "0.0",
                }
            ],
        }

    def validate(self, payload: dict[str, Any]) -> None:
        _validate_event(
            aggregate_type="research_synthesis",
            aggregate_id=self.collection_id,
            event_type="synthesis.executed",
            event_schema_version=1,
            subject_type="synthesis_result",
            subject_id=UUID(payload["result_id"]),
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            payload=payload,
            request_fingerprint="d" * 64,
        )

    def replay(self, *events: tuple[str, dict[str, Any]]) -> None:
        stored = []
        for role, payload in events:
            event = _stored("synthesis.executed", payload)
            event.actor_role = role
            stored.append(event)
        _validate_synthesis_transitions(stored, self.collection_id)


def test_synthesis_payload_keys_exact() -> None:
    s = _Synthesis()
    payload = s.executed(uuid4())
    s.validate(payload)
    with pytest.raises(DecisionValidationError, match="event schema"):
        s.validate({**payload, "extra": 1})
    with pytest.raises(DecisionValidationError, match="another collection"):
        s.validate({**payload, "collection_id": str(uuid4())})
    with pytest.raises(DecisionValidationError, match="unsupported"):
        s.validate({**payload, "model": "reml"})
    with pytest.raises(DecisionValidationError, match="unique unit keys"):
        s.validate({**payload, "included_units": ["study:A", "study:A"]})
    with pytest.raises(DecisionValidationError, match="exclusions"):
        s.validate({**payload, "excluded": [{"unit": None}]})


def test_synthesis_replay_rejects_successor_with_same_input_hash() -> None:
    s = _Synthesis()
    first = uuid4()
    s.replay(
        ("reviewer", s.executed(first)),
        ("reviewer", s.executed(uuid4(), first, input_hash="e" * 64)),
    )
    with pytest.raises(DecisionReplayError, match="unchanged input"):
        s.replay(
            ("reviewer", s.executed(first)), ("reviewer", s.executed(uuid4(), first))
        )
    with pytest.raises(DecisionReplayError, match="non-tip"):
        s.replay(("reviewer", s.executed(first)), ("reviewer", s.executed(uuid4())))
    with pytest.raises(DecisionReplayError, match="reviewer"):
        s.replay(("supervisor", s.executed(first)))
    with pytest.raises(DecisionReplayError, match="units"):
        s.replay(("reviewer", s.executed(first, units=("study:A",))))
    s.replay(
        ("reviewer", s.executed(first, units=("study:A",), status="validation_failed"))
    )
    with pytest.raises(DecisionReplayError, match="units"):
        s.replay(("reviewer", s.executed(first, status="validation_failed")))


def test_claim_linked_v1_still_replays_v2_requires_result_id() -> None:
    c = _Claims()
    version, link, result = uuid4(), uuid4(), uuid4()
    c.validate("claim.linked", c.linked(version, link))  # v1 keeps validating

    def validate_v2(payload: dict[str, Any]) -> None:
        _validate_event(
            aggregate_type="research_claims",
            aggregate_id=c.collection_id,
            event_type="claim.linked",
            event_schema_version=2,
            subject_type="research_claim",
            subject_id=c.claim,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            payload=payload,
            request_fingerprint="b" * 64,
        )

    synthesis = {
        **c.linked(version, link, "legacy_unanchored"),
        "draft_citation_id": None,
        "document_id": None,
        "kind": "synthesis_result",
        "synthesis_result_id": str(result),
    }
    validate_v2(synthesis)
    validate_v2({**c.linked(version, link), "synthesis_result_id": None})
    with pytest.raises(DecisionValidationError, match="claim link"):
        validate_v2({**synthesis, "synthesis_result_id": None})
    with pytest.raises(DecisionValidationError, match="claim link"):
        validate_v2({**synthesis, "document_id": str(uuid4())})
    with pytest.raises(DecisionValidationError, match="event schema"):
        validate_v2(c.linked(version, link))  # v2 requires the key
    # A synthesis link is live for assessment but carries no model stance.
    versioned = ("claim.versioned", "editor", c.versioned(version))
    c.replay(
        versioned,
        ("claim.linked", "editor", synthesis),
        ("claim.assessed", "adjudicator", c.assessed(version, uuid4(), [link])),
    )
    with pytest.raises(DecisionReplayError, match="live link"):
        c.replay(
            versioned,
            ("claim.linked", "editor", synthesis),
            ("claim.observed", "machine", c.observed(link, uuid4())),
        )


def test_release_staled_accepts_synthesis_cause() -> None:
    r = _Release()
    payload = r.staled(uuid4())
    payload["cause"] = {
        "family": "research_synthesis",
        "event_id": str(uuid4()),
        "kind": "synthesis.executed",
    }
    payload["changed_nodes"] = [f"synthesis:{uuid4()}"]
    r.validate("release.staled", payload, r.collection_id)
    payload["cause"] = {**payload["cause"], "family": "research_nonsense"}
    with pytest.raises(DecisionValidationError, match="cause"):
        r.validate("release.staled", payload, r.collection_id)


# --- GOO-312: research_experiment and claim.linked v3 -------------------------


class _Experiment:
    """Hand-built events for one Collection's research_experiment stream."""

    def __init__(self) -> None:
        self.collection_id = uuid4()

    def manifest(
        self, run: UUID, manifest: UUID, outputs: list[UUID]
    ) -> dict[str, Any]:
        return {
            "collection_id": str(self.collection_id),
            "run_id": str(run),
            "manifest_id": str(manifest),
            "manifest_hash": "a" * 64,
            "completeness": "complete",
            "missing": [],
            "status": "completed",
            "output_artifact_ids": [str(o) for o in outputs],
        }

    def figure(
        self,
        figure: UUID,
        run: UUID,
        manifest: UUID,
        output: UUID,
        supersedes: UUID | None = None,
    ) -> dict[str, Any]:
        return {
            "collection_id": str(self.collection_id),
            "figure_id": str(figure),
            "figure_key": "fig-1",
            "kind": "figure",
            "output_artifact_id": str(output),
            "run_id": str(run),
            "manifest_id": str(manifest),
            "supersedes_figure_id": None if supersedes is None else str(supersedes),
        }

    def validate(self, event_type: str, payload: dict[str, Any]) -> None:
        subject = payload["manifest_id" if "missing" in payload else "figure_id"]
        _validate_event(
            aggregate_type="research_experiment",
            aggregate_id=self.collection_id,
            event_type=event_type,
            event_schema_version=1,
            subject_type="experiment",
            subject_id=UUID(subject),
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            payload=payload,
            request_fingerprint="d" * 64,
        )

    def replay(self, *events: tuple[str, str, dict[str, Any]]) -> None:
        stored = []
        for event_type, role, payload in events:
            event = _stored(event_type, payload)
            event.actor_role = role
            stored.append(event)
        _validate_experiment_transitions(stored, self.collection_id)


def test_experiment_payloads_validate() -> None:
    e = _Experiment()
    run, manifest, output = uuid4(), uuid4(), uuid4()
    recorded = e.manifest(run, manifest, [output])
    e.validate("run.manifest_recorded", recorded)
    e.validate("figure.registered", e.figure(uuid4(), run, manifest, output))
    with pytest.raises(DecisionValidationError, match="event schema"):
        e.validate("run.manifest_recorded", {**recorded, "storage_key": "k"})
    with pytest.raises(DecisionValidationError, match="disagrees"):
        e.validate("run.manifest_recorded", {**recorded, "missing": ["seed"]})
    with pytest.raises(DecisionValidationError, match="kind"):
        e.validate(
            "figure.registered",
            {**e.figure(uuid4(), run, manifest, output), "kind": "chart"},
        )


def test_experiment_replay_rejects_second_manifest_for_run() -> None:
    e = _Experiment()
    run = uuid4()
    first = ("run.manifest_recorded", "machine", e.manifest(run, uuid4(), []))
    e.replay(first)
    with pytest.raises(DecisionReplayError, match="already has a manifest"):
        e.replay(
            first, ("run.manifest_recorded", "machine", e.manifest(run, uuid4(), []))
        )
    with pytest.raises(DecisionReplayError, match="machine"):
        e.replay(("run.manifest_recorded", "editor", e.manifest(run, uuid4(), [])))


def test_figure_replay_requires_prior_manifest() -> None:
    e = _Experiment()
    run, manifest, output, fig = uuid4(), uuid4(), uuid4(), uuid4()
    recorded = ("run.manifest_recorded", "machine", e.manifest(run, manifest, [output]))
    registered = (
        "figure.registered",
        "editor",
        e.figure(fig, run, manifest, output),
    )
    successor = e.figure(uuid4(), run, manifest, output, supersedes=fig)
    e.replay(recorded, registered, ("figure.registered", "editor", successor))
    with pytest.raises(DecisionReplayError, match="no prior manifest"):
        e.replay(registered, recorded)
    with pytest.raises(DecisionReplayError, match="not in its manifest"):
        e.replay(
            recorded,
            ("figure.registered", "editor", e.figure(fig, run, manifest, uuid4())),
        )
    with pytest.raises(DecisionReplayError, match="non-tip"):
        e.replay(
            recorded,
            registered,
            ("figure.registered", "editor", e.figure(uuid4(), run, manifest, output)),
        )
    with pytest.raises(DecisionReplayError, match="editor"):
        e.replay(recorded, ("figure.registered", "machine", registered[2]))


def test_claim_linked_v3_requires_figure_id_for_figure_kind() -> None:
    c = _Claims()
    version, link = uuid4(), uuid4()

    def validate_v3(payload: dict[str, Any]) -> None:
        _validate_event(
            aggregate_type="research_claims",
            aggregate_id=c.collection_id,
            event_type="claim.linked",
            event_schema_version=3,
            subject_type="research_claim",
            subject_id=c.claim,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            payload=payload,
            request_fingerprint="b" * 64,
        )

    figure = {
        **c.linked(version, link, "legacy_unanchored"),
        "draft_citation_id": None,
        "document_id": None,
        "kind": "figure",
        "synthesis_result_id": None,
        "figure_id": str(uuid4()),
    }
    validate_v3(figure)
    validate_v3(
        {**c.linked(version, link), "synthesis_result_id": None, "figure_id": None}
    )
    with pytest.raises(DecisionValidationError, match="claim link"):
        validate_v3({**figure, "figure_id": None})
    with pytest.raises(DecisionValidationError, match="event schema"):
        validate_v3({**c.linked(version, link), "synthesis_result_id": None})
    # A figure link is live for assessment but carries no model stance.
    versioned = ("claim.versioned", "editor", c.versioned(version))
    c.replay(
        versioned,
        ("claim.linked", "editor", figure),
        ("claim.assessed", "adjudicator", c.assessed(version, uuid4(), [link])),
    )
    with pytest.raises(DecisionReplayError, match="live link"):
        c.replay(
            versioned,
            ("claim.linked", "editor", figure),
            ("claim.observed", "machine", c.observed(link, uuid4())),
        )

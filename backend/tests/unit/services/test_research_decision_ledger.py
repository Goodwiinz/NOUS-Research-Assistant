"""Focused vocabulary and fingerprint tests for the research decision ledger."""

from typing import Any
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

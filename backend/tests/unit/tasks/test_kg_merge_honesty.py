"""kg_merge_entities_job: redelivery idempotency + delete-failure honesty.

Two guards from the Postgres<->Neo4j dual-write audit:

1. acks_late redelivery (celery_app sets task_acks_late=True) of a COMPLETED
   merge job must short-circuit — re-running would regress COMPLETED -> RUNNING
   and re-process every group (re-deleting already-merged nodes, duplicating
   re-pointed edges). Same guard its sibling kg_extract_entities_job has.

2. delete_entity swallows Neo4j errors and returns False. The relationships are
   re-pointed onto the primary BEFORE the delete, so a failed delete leaves the
   duplicate node alive alongside duplicated edges — that group is NOT merged
   and must be counted failed, not a false success.

3. The converse: the duplicate's edges must be re-pointed BEFORE it is deleted.
   If reading them fails (get_relationships swallows errors and returns []) or
   re-pointing one fails, the DETACH DELETE would destroy those edges for good
   while the group is counted merged — so the duplicate must be kept.

4. Job-level honesty (mirrors kg_extract_entities_job): per-group failures
   only bump ``failed_groups``, so a job in which EVERY group failed (Neo4j
   down, every primary missing) used to COMPLETE with ``merged_groups=0`` —
   and the EntityMergeTool frontend toasts "Merge job completed" on status
   alone. Such a job must be FAILED (``error_type="merge_failed"``); a
   partial success still completes with its counts.

Mutation check for guard 4 (docs/engineering/testing.md): neutralize the
``if success_count == 0 and failure_count > 0`` branch before
``job.complete_job`` in ``kg_merge_entities_job``
(backend/src/tasks/processing_tasks.py) and run
``pytest -q tests/unit/tasks/test_kg_merge_honesty.py -k all_groups_fail``
— it must fail on ``fail_job`` not being called.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from src.models.graph import RelationshipType
from src.models.processing import JobStatus
from src.services.knowledge_graph.knowledge_graph_service import KnowledgeGraphService
from src.tasks import processing_tasks as pt


def test_merge_short_circuits_completed_job(monkeypatch: pytest.MonkeyPatch) -> None:
    job = MagicMock()
    job.status = JobStatus.COMPLETED
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)

    result = pt.kg_merge_entities_job.apply(args=("job-1",)).result

    assert result == {
        "status": "completed",
        "job_id": "job-1",
        "skipped": "duplicate_delivery",
    }
    job.start_job.assert_not_called()


def test_failed_delete_counts_group_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delete_entity returning False must yield failed_groups=1, merged=0 —
    not a false success."""
    job = MagicMock()
    job.status = JobStatus.PENDING
    job.organization_id = "org-1"
    job.parameters = {
        "groups": [
            {
                "suggested_primary": "primary-1",
                "entities": [{"id": "primary-1"}, {"id": "dup-1"}],
            }
        ]
    }
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)

    kg = MagicMock()
    kg.get_relationships.return_value = []
    kg.delete_entity.return_value = False  # Neo4j outage / node already gone
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    result = pt.kg_merge_entities_job.apply(args=("job-2",)).result

    # The only group failed, so the job itself is failed (guard 4), and the
    # counts travel in the returned result instead of complete_job.
    assert result["status"] == "failed"
    assert result["result"]["merged_groups"] == 0
    assert result["result"]["failed_groups"] == 1
    job.complete_job.assert_not_called()
    kg.delete_entity.assert_called_once_with("dup-1", organization_id="org-1")


def test_successful_delete_counts_group_as_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = MagicMock()
    job.status = JobStatus.PENDING
    job.organization_id = "org-1"
    job.parameters = {
        "groups": [
            {
                "suggested_primary": "primary-1",
                "entities": [{"id": "primary-1"}, {"id": "dup-1"}],
            }
        ]
    }
    captured = {}
    job.complete_job.side_effect = lambda result: captured.update(result)

    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)

    kg = MagicMock()
    kg.get_relationships.return_value = []
    kg.delete_entity.return_value = True
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    pt.kg_merge_entities_job.apply(args=("job-3",))

    assert captured["merged_groups"] == 1
    assert captured["failed_groups"] == 0


def _run_merge_job(monkeypatch: pytest.MonkeyPatch, kg) -> dict:
    """Run kg_merge_entities_job for one {primary-1, dup-1} group against ``kg``
    and return the task's return dict. With a single group that fails, the job
    is failed (guard 4) and the counts live under ``["result"]``."""
    job = MagicMock()
    job.status = JobStatus.PENDING
    job.organization_id = "org-1"
    job.parameters = {
        "groups": [
            {
                "suggested_primary": "primary-1",
                "entities": [{"id": "primary-1"}, {"id": "dup-1"}],
            }
        ]
    }

    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    return pt.kg_merge_entities_job.apply(args=("job-x",)).result


def test_failed_edge_repoint_keeps_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_relationship raising (Neo4j transient error, scope error) must
    NOT be followed by DETACH DELETE of the duplicate — that destroys the edge
    that failed to move. The group is failed and the duplicate kept."""
    rel = MagicMock(
        source_entity_id="dup-1",
        target_entity_id="x-1",
        relationship_type=RelationshipType.RELATED_TO,
        strength=0.5,
        confidence_score=0.8,
        context=None,
        evidence=[],
        metadata={},
        source_document_id=None,
    )
    kg = MagicMock()
    kg.get_relationships.return_value = [rel]
    kg.create_relationship.side_effect = RuntimeError("Neo4j transient error")
    kg.delete_entity.return_value = True

    result = _run_merge_job(monkeypatch, kg)

    kg.delete_entity.assert_not_called()
    assert result["status"] == "failed"
    assert result["result"]["merged_groups"] == 0
    assert result["result"]["failed_groups"] == 1


def test_unreadable_relationships_keep_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real KnowledgeGraphService.get_relationships swallows Neo4j errors and
    returns []. The merge must not read that as "no edges to move" and delete
    the duplicate (with the edges it could not read)."""

    @contextmanager
    def _down(*_args, **_kwargs):
        raise RuntimeError("Neo4j unavailable")
        yield  # pragma: no cover

    kg = KnowledgeGraphService()
    monkeypatch.setattr(kg, "get_session", _down)
    kg.delete_entity = MagicMock(return_value=True)  # type: ignore[method-assign]

    result = _run_merge_job(monkeypatch, kg)

    kg.delete_entity.assert_not_called()
    assert result["status"] == "failed"
    assert result["result"]["merged_groups"] == 0
    assert result["result"]["failed_groups"] == 1


def test_get_relationships_strict_raises_default_stays_lenient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read endpoints keep the empty-list fallback; ``strict=True`` is the
    opt-in for callers (entity merge) that must not mistake an outage for
    "no relationships"."""

    @contextmanager
    def _down(*_args, **_kwargs):
        raise RuntimeError("Neo4j unavailable")
        yield  # pragma: no cover

    kg = KnowledgeGraphService()
    monkeypatch.setattr(kg, "get_session", _down)

    assert kg.get_relationships("dup-1", organization_id="org-1") == []
    with pytest.raises(RuntimeError, match="Neo4j unavailable"):
        kg.get_relationships("dup-1", organization_id="org-1", strict=True)


def _job_with_groups(groups: list) -> MagicMock:
    job = MagicMock()
    job.status = JobStatus.PENDING
    job.organization_id = "org-1"
    job.parameters = {"groups": groups}
    return job


def _two_groups() -> list:
    return [
        {
            "suggested_primary": f"primary-{n}",
            "entities": [{"id": f"primary-{n}"}, {"id": f"dup-{n}"}],
        }
        for n in (1, 2)
    ]


def test_all_groups_fail_fails_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard 4: Neo4j down for every group -> 0 merged / N failed. The job
    must be FAILED (error_type="merge_failed"), not COMPLETED with
    merged_groups=0 — the frontend toasts success on status alone."""
    job = _job_with_groups(_two_groups())
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)

    kg = MagicMock()
    kg.get_relationships.side_effect = RuntimeError("Neo4j unavailable")
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    result = pt.kg_merge_entities_job.apply(args=("job-all-fail",)).result

    job.complete_job.assert_not_called()
    job.fail_job.assert_called_once()
    message, kwargs = job.fail_job.call_args.args[0], job.fail_job.call_args.kwargs
    assert kwargs == {"error_type": "merge_failed"}
    assert "2/2" in message
    assert result == {
        "status": "failed",
        "job_id": "job-all-fail",
        "result": {"total_groups": 2, "merged_groups": 0, "failed_groups": 2},
    }
    kg.delete_entity.assert_not_called()


def test_zero_groups_keeps_existing_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty group list is rejected before any merge runs (ValueError ->
    generic fail path). Guard 4 must not re-label that as ``merge_failed`` —
    the 0-merged/0-failed case never reaches the final block."""
    job = _job_with_groups([])
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)
    kg = MagicMock()
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    eager = pt.kg_merge_entities_job.apply(args=("job-empty",))

    assert eager.state == "FAILURE"
    assert isinstance(eager.result, ValueError)
    job.complete_job.assert_not_called()
    job.fail_job.assert_called_once()
    assert job.fail_job.call_args.kwargs == {"error_type": "ValueError"}
    job.start_job.assert_not_called()


def test_partial_success_still_completes_with_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard 4 must not over-reach: one merged + one failed group is a
    COMPLETED job carrying both counts, exactly as before."""
    job = _job_with_groups(_two_groups())
    captured: dict = {}
    job.complete_job.side_effect = lambda result: captured.update(result)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)

    kg = MagicMock()
    kg.get_relationships.return_value = []
    kg.delete_entity.side_effect = [True, False]  # group 1 merged, group 2 not
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    result = pt.kg_merge_entities_job.apply(args=("job-partial",)).result

    job.fail_job.assert_not_called()
    assert result == {"status": "completed", "job_id": "job-partial"}
    assert captured == {"total_groups": 2, "merged_groups": 1, "failed_groups": 1}


@pytest.mark.parametrize("terminal", [JobStatus.FAILED, JobStatus.CANCELLED])
def test_merge_short_circuits_terminal_failed_job(
    monkeypatch: pytest.MonkeyPatch, terminal: JobStatus
) -> None:
    """Guard 5: now that an all-groups-failed merge commits FAILED, a worker
    killed between that commit and the late ack redelivers the message. The
    redelivery must not start_job() a terminal job (FAILED -> RUNNING) and
    repeat every Neo4j delete/re-point."""
    job = _job_with_groups(_two_groups())
    job.status = terminal
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)
    kg = MagicMock()
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    result = pt.kg_merge_entities_job.apply(args=("job-term",)).result

    assert result == {
        "status": terminal.value,
        "job_id": "job-term",
        "skipped": "duplicate_delivery",
    }
    job.start_job.assert_not_called()
    job.complete_job.assert_not_called()
    job.fail_job.assert_not_called()
    assert kg.method_calls == []


def test_merge_runs_retrying_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """RETRYING is non-terminal: an explicitly re-queued job must still run."""
    job = _job_with_groups(_two_groups())
    job.status = JobStatus.RETRYING
    captured: dict = {}
    job.complete_job.side_effect = lambda result: captured.update(result)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)
    kg = MagicMock()
    kg.get_relationships.return_value = []
    kg.delete_entity.return_value = True
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    result = pt.kg_merge_entities_job.apply(args=("job-retry",)).result

    job.start_job.assert_called_once()
    assert result == {"status": "completed", "job_id": "job-retry"}
    assert captured == {"total_groups": 2, "merged_groups": 2, "failed_groups": 0}


def test_repeated_duplicate_id_in_group_merges_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard 6: a group listing the same duplicate twice ([primary, dup, dup])
    must delete dup once. Processing it twice makes the second delete_entity
    return False (node already gone), marking a successful merge as failed —
    and with guard 4 a single such group fails the whole job."""
    job = _job_with_groups(
        [
            {
                "suggested_primary": "primary-1",
                "entities": [
                    {"id": "primary-1"},
                    {"id": "dup-1"},
                    {"id": "dup-1"},
                    {"id": "primary-1"},
                ],
            }
        ]
    )
    captured: dict = {}
    job.complete_job.side_effect = lambda result: captured.update(result)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)
    kg = MagicMock()
    kg.get_relationships.return_value = []
    # Real delete_entity: True on the first delete, False once the node is gone.
    kg.delete_entity.side_effect = [True, False, False]
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    result = pt.kg_merge_entities_job.apply(args=("job-dupdup",)).result

    kg.delete_entity.assert_called_once_with("dup-1", organization_id="org-1")
    job.fail_job.assert_not_called()
    assert result == {"status": "completed", "job_id": "job-dupdup"}
    assert captured == {"total_groups": 1, "merged_groups": 1, "failed_groups": 0}

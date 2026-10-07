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
    captured = {}
    job.complete_job.side_effect = lambda result: captured.update(result)

    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)

    kg = MagicMock()
    kg.get_relationships.return_value = []
    kg.delete_entity.return_value = False  # Neo4j outage / node already gone
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    result = pt.kg_merge_entities_job.apply(args=("job-2",)).result

    assert result["status"] == "completed"
    assert captured["merged_groups"] == 0
    assert captured["failed_groups"] == 1
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
    and return the result dict handed to ``complete_job``."""
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
    captured: dict = {}
    job.complete_job.side_effect = lambda result: captured.update(result)

    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    pt.kg_merge_entities_job.apply(args=("job-x",))
    return captured


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

    captured = _run_merge_job(monkeypatch, kg)

    kg.delete_entity.assert_not_called()
    assert captured["merged_groups"] == 0
    assert captured["failed_groups"] == 1


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

    captured = _run_merge_job(monkeypatch, kg)

    kg.delete_entity.assert_not_called()
    assert captured["merged_groups"] == 0
    assert captured["failed_groups"] == 1


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

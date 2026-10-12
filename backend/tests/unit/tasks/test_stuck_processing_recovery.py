"""GOO-359: PostgreSQL recovery races, scoped rows and transaction rollback.

Requires ORCHESTRATION_TEST_DATABASE_URL pointing to disposable PostgreSQL.
Each case owns a schema. Candidate-selection hooks commit competing changes
before the sweep reloads its rows; NOWAIT probes prove real row locks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from src.models.document import Document, ProcessingStatus
from src.models.organization import Organization
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.tasks import processing_tasks as pt
from src.tasks import reconcile_jobs as rj
from tests.unit.tasks.test_ingestion_stage_guard_postgres import _ingestion

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.fixture
def recovery(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    with _ingestion() as env:
        with env.Session() as db:
            job = db.get(ProcessingJob, env.job_id)
            job.status = JobStatus.RUNNING
            job.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
            db.get(Document, env.doc_id).processing_status = ProcessingStatus.PROCESSING
            db.commit()
        monkeypatch.setattr(pt, "SessionLocal", env.Session)
        monkeypatch.setattr(
            "src.core.config.get_settings",
            lambda: SimpleNamespace(
                SWEEPERS_ENABLED=True, PROCESSING_JOB_STUCK_AFTER_SECONDS=1800
            ),
        )
        yield env


def _after_candidates(
    env: SimpleNamespace, change: Callable[[Any], None]
) -> tuple[Any, Callable[..., None]]:
    """Commit after the candidate cursor executes, without timing sleeps."""
    engine = env.Session.kw["bind"]
    fired = False

    def interfere(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        nonlocal fired
        if fired or "FROM processing_jobs" not in statement or "LIMIT" not in statement:
            return
        fired = True
        with env.Session() as other:
            change(other)
            other.commit()

    event.listen(engine, "after_cursor_execute", interfere)
    return engine, interfere


@pytest.mark.parametrize("status", [JobStatus.RUNNING, JobStatus.QUEUED])
def test_sweep_fails_job_and_document_atomically(
    recovery: SimpleNamespace, status: JobStatus
) -> None:
    with recovery.Session() as db:
        job = db.get(ProcessingJob, recovery.job_id)
        job.status = status
        job.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()
    assert pt.sweep_stuck_processing_jobs() == {"swept": 1}
    with recovery.Session() as db:
        job = db.get(ProcessingJob, recovery.job_id)
        document = db.get(Document, recovery.doc_id)
        assert job.status == JobStatus.FAILED
        assert document.processing_status == ProcessingStatus.FAILED
        assert document.processing_error == job.error_message
        assert job.error_type == "StuckJobSweep"
        assert document.processing_completed_at is not None


@pytest.mark.parametrize(
    "change", ["progress", "complete", "cancel", "delete", "retry"]
)
def test_sweep_rechecks_job_after_candidate_selection(
    recovery: SimpleNamespace, change: str
) -> None:
    def transition(db: Any) -> None:
        job = db.get(ProcessingJob, recovery.job_id)
        if change == "progress":
            job.progress_percentage = 40
            job.updated_at = datetime.now(timezone.utc)
        elif change == "complete":
            job.complete_job()
        elif change == "cancel":
            job.cancel_job()
        elif change == "delete":
            job.soft_delete()
        else:
            job.status = JobStatus.RETRYING
            job.celery_task_id = "new-attempt"
            job.updated_at = datetime.now(timezone.utc)
        if change in ("complete", "cancel", "delete"):
            # Status/deletion guards must work independently of freshness,
            # including rows written with an explicitly retained timestamp.
            job.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)

    engine, hook = _after_candidates(recovery, transition)
    try:
        assert pt.sweep_stuck_processing_jobs() == {"swept": 0}
    finally:
        event.remove(engine, "after_cursor_execute", hook)
    with recovery.Session() as db:
        assert db.get(ProcessingJob, recovery.job_id).status != JobStatus.FAILED
        assert (
            db.get(Document, recovery.doc_id).processing_status
            == ProcessingStatus.PROCESSING
        )


def test_sweep_preserves_new_active_ingestion(recovery: SimpleNamespace) -> None:
    new_id = uuid4()

    def retry(db: Any) -> None:
        db.add(
            ProcessingJob(
                id=new_id,
                document_id=recovery.doc_id,
                organization_id=recovery.org_id,
                job_type=JobType.DOCUMENT_INGESTION,
                status=JobStatus.QUEUED,
                celery_task_id="new-ingestion",
            )
        )

    engine, hook = _after_candidates(recovery, retry)
    try:
        assert pt.sweep_stuck_processing_jobs() == {"swept": 1}
    finally:
        event.remove(engine, "after_cursor_execute", hook)
    with recovery.Session() as db:
        assert db.get(ProcessingJob, recovery.job_id).status == JobStatus.FAILED
        assert db.get(ProcessingJob, new_id).status == JobStatus.QUEUED
        assert (
            db.get(Document, recovery.doc_id).processing_status
            == ProcessingStatus.PROCESSING
        )


@pytest.mark.parametrize("field", ["organization_id", "document_id", "job_type"])
def test_sweep_rechecks_candidate_association(
    recovery: SimpleNamespace, field: str
) -> None:
    def transition(db: Any) -> None:
        job = db.get(ProcessingJob, recovery.job_id)
        if field == "organization_id":
            org_id = uuid4()
            db.add(Organization(id=org_id, name="Other", storage_limit_bytes=10_000))
            db.flush()
            job.organization_id = org_id
        elif field == "document_id":
            job.document_id = None
        else:
            job.job_type = JobType.GRAPH_INDEXING
        job.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)

    engine, hook = _after_candidates(recovery, transition)
    try:
        assert pt.sweep_stuck_processing_jobs() == {"swept": 0}
    finally:
        event.remove(engine, "after_cursor_execute", hook)
    with recovery.Session() as db:
        assert db.get(ProcessingJob, recovery.job_id).status == JobStatus.RUNNING
        assert (
            db.get(Document, recovery.doc_id).processing_status
            == ProcessingStatus.PROCESSING
        )


@pytest.mark.parametrize("change", ["complete", "delete"])
def test_sweep_rechecks_document_after_candidate_selection(
    recovery: SimpleNamespace, change: str
) -> None:
    def transition(db: Any) -> None:
        document = db.get(Document, recovery.doc_id)
        if change == "complete":
            document.processing_status = ProcessingStatus.COMPLETED
        else:
            document.soft_delete()

    engine, hook = _after_candidates(recovery, transition)
    try:
        assert pt.sweep_stuck_processing_jobs() == {"swept": 1}
    finally:
        event.remove(engine, "after_cursor_execute", hook)
    with recovery.Session() as db:
        document = db.get(Document, recovery.doc_id)
        assert document.processing_status == (
            ProcessingStatus.COMPLETED
            if change == "complete"
            else ProcessingStatus.PROCESSING
        )


def test_sweep_fails_last_of_multiple_stale_attempts(recovery: SimpleNamespace) -> None:
    with recovery.Session() as db:
        db.add(
            ProcessingJob(
                document_id=recovery.doc_id,
                organization_id=recovery.org_id,
                job_type=JobType.DOCUMENT_INGESTION,
                status=JobStatus.QUEUED,
                updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
        )
        db.commit()
    assert pt.sweep_stuck_processing_jobs() == {"swept": 2}
    with recovery.Session() as db:
        assert (
            db.get(Document, recovery.doc_id).processing_status
            == ProcessingStatus.FAILED
        )


@pytest.mark.parametrize("kind", ["completed", "deleted", "foreign", "non-ingestion"])
def test_sweep_preserves_documents_it_does_not_own(
    recovery: SimpleNamespace, kind: str
) -> None:
    with recovery.Session() as db:
        document = db.get(Document, recovery.doc_id)
        if kind == "completed":
            document.processing_status = ProcessingStatus.COMPLETED
        elif kind == "deleted":
            document.soft_delete()
        elif kind == "foreign":
            org_id = uuid4()
            db.add(Organization(id=org_id, name="Other", storage_limit_bytes=10_000))
            db.flush()
            document.organization_id = org_id
        else:
            job = db.get(ProcessingJob, recovery.job_id)
            job.job_type = JobType.GRAPH_INDEXING
            job.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()
        expected = document.processing_status
    assert pt.sweep_stuck_processing_jobs() == {"swept": 1}
    with recovery.Session() as db:
        assert db.get(Document, recovery.doc_id).processing_status == expected


@pytest.mark.parametrize("kind", ["terminal", "deleted", "foreign", "non-ingestion"])
def test_invalid_competing_job_does_not_hide_stuck_document(
    recovery: SimpleNamespace, kind: str
) -> None:
    with recovery.Session() as db:
        org_id = recovery.org_id
        if kind == "foreign":
            org_id = uuid4()
            db.add(Organization(id=org_id, name="Other", storage_limit_bytes=10_000))
            db.flush()
        db.add(
            ProcessingJob(
                document_id=recovery.doc_id,
                organization_id=org_id,
                job_type=(
                    JobType.GRAPH_INDEXING
                    if kind == "non-ingestion"
                    else JobType.DOCUMENT_INGESTION
                ),
                status=JobStatus.COMPLETED if kind == "terminal" else JobStatus.QUEUED,
                is_deleted=kind == "deleted",
            )
        )
        db.commit()
    assert pt.sweep_stuck_processing_jobs() == {"swept": 1}
    with recovery.Session() as db:
        assert (
            db.get(Document, recovery.doc_id).processing_status
            == ProcessingStatus.FAILED
        )


def test_sweep_holds_document_then_job_locks_through_commit(
    recovery: SimpleNamespace,
) -> None:
    engine = recovery.Session.kw["bind"]
    locks = []
    probes: list[str] = []

    def probe(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "FOR UPDATE" in statement:
            if "FROM documents" in statement:
                locks.append("document")
            elif "FROM processing_jobs" in statement:
                locks.append("job")
        if not statement.startswith("UPDATE ") or probes:
            return
        for model, row_id in [
            (Document, recovery.doc_id),
            (ProcessingJob, recovery.job_id),
        ]:
            with recovery.Session() as other:
                with pytest.raises(OperationalError) as raised:
                    other.execute(
                        select(model.id)
                        .where(model.id == row_id)
                        .with_for_update(nowait=True)
                    )
                assert getattr(raised.value.orig, "pgcode", None) == "55P03"
                probes.append(model.__name__)
                other.rollback()

    event.listen(engine, "before_cursor_execute", probe)
    try:
        assert pt.sweep_stuck_processing_jobs() == {"swept": 1}
    finally:
        event.remove(engine, "before_cursor_execute", probe)
    assert locks[:2] == ["document", "job"]
    assert probes == ["Document", "ProcessingJob"]


def test_sweep_rolls_back_both_rows(
    recovery: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = recovery.Session()

    def fail_commit() -> None:
        session.flush()
        raise RuntimeError("commit rejected")

    monkeypatch.setattr(session, "commit", fail_commit)
    monkeypatch.setattr(pt, "SessionLocal", lambda: session)
    with pytest.raises(RuntimeError, match="commit rejected"):
        pt.sweep_stuck_processing_jobs()
    with recovery.Session() as db:
        assert db.get(ProcessingJob, recovery.job_id).status == JobStatus.RUNNING
        document = db.get(Document, recovery.doc_id)
        assert document.processing_status == ProcessingStatus.PROCESSING
        assert document.processing_error is None


def test_disabled_sweep_does_not_open_database(
    recovery: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "src.core.config.get_settings", lambda: SimpleNamespace(SWEEPERS_ENABLED=False)
    )

    def unexpected_session() -> None:
        pytest.fail("disabled sweeper opened a database session")

    monkeypatch.setattr(pt, "SessionLocal", unexpected_session)
    assert pt.sweep_stuck_processing_jobs() == {"skipped": "sweepers-disabled"}


@pytest.fixture
def lost(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """An upload job re-enqueued three times without a worker ever claiming it
    (PENDING, no Celery task id), with its PENDING document."""
    with _ingestion() as env:
        with env.Session() as db:
            job = db.get(ProcessingJob, env.job_id)
            job.status = JobStatus.PENDING
            job.retry_count = 3
            job.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
            db.commit()
        monkeypatch.setattr(rj, "SessionLocal", env.Session)
        monkeypatch.setattr(
            "src.core.config.get_settings",
            lambda: SimpleNamespace(
                LOST_JOB_RECONCILER_ENABLED=True,
                LOST_JOB_RECONCILE_AFTER_MINUTES=10,
                LOST_JOB_RECONCILE_MAX_ATTEMPTS=3,
                LOST_JOB_RECONCILE_MAX_PER_RUN=100,
            ),
        )
        yield env


def test_reconciler_give_up_fails_document_with_job(lost: SimpleNamespace) -> None:
    """IN-1 path A: the give-up failed only the job, leaving the document
    PENDING forever (re-upload 409, no UI retry)."""
    assert rj.reconcile_lost_processing_jobs() == {
        "reenqueued": 0,
        "failed": 1,
        "skipped": 0,
    }
    with lost.Session() as db:
        job = db.get(ProcessingJob, lost.job_id)
        document = db.get(Document, lost.doc_id)
        assert job.status == JobStatus.FAILED
        assert job.error_type == "LostJobReconcileGaveUp"
        assert document.processing_status == ProcessingStatus.FAILED
        assert document.processing_error == job.error_message


def test_reconciler_give_up_keeps_a_job_claimed_after_selection(
    lost: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """Verifier V4 race: a worker claims the job between the reconciler's
    unlocked SELECT and its give-up; the bare UPDATE ... WHERE id overwrote
    RUNNING with FAILED and the worker then stopped at its next stage guard."""

    def claim(db: Any) -> None:
        db.get(ProcessingJob, lost.job_id).start_job(
            worker_id="worker-A", celery_task_id="task-A"
        )

    engine, hook = _after_candidates(lost, claim)
    try:
        with caplog.at_level("INFO", logger=rj.__name__):
            assert rj.reconcile_lost_processing_jobs() == {
                "reenqueued": 0,
                "failed": 0,
                "skipped": 1,
            }
    finally:
        event.remove(engine, "after_cursor_execute", hook)
    assert f"job {lost.job_id} changed since selection; give-up skipped" in (
        caplog.text
    )
    with lost.Session() as db:
        job = db.get(ProcessingJob, lost.job_id)
        assert (job.status, job.celery_task_id) == (JobStatus.RUNNING, "task-A")
        assert (
            db.get(Document, lost.doc_id).processing_status == ProcessingStatus.PENDING
        )


def test_reconciler_give_up_keeps_a_job_the_sweep_ended_after_selection(
    lost: SimpleNamespace,
) -> None:
    """The stuck sweep also selects PENDING jobs and leaves the Celery task id
    NULL, so only the locked status re-check stops the give-up from failing
    the job a second time over the sweep's outcome."""

    def sweep(db: Any) -> None:
        db.get(ProcessingJob, lost.job_id).fail_job("swept", error_type="StuckJobSweep")
        db.get(Document, lost.doc_id).update_processing_status(
            ProcessingStatus.FAILED, "swept"
        )

    engine, hook = _after_candidates(lost, sweep)
    try:
        assert rj.reconcile_lost_processing_jobs() == {
            "reenqueued": 0,
            "failed": 0,
            "skipped": 1,
        }
    finally:
        event.remove(engine, "after_cursor_execute", hook)
    with lost.Session() as db:
        job = db.get(ProcessingJob, lost.job_id)
        assert (job.status, job.error_type) == (JobStatus.FAILED, "StuckJobSweep")
        assert db.get(Document, lost.doc_id).processing_error == "swept"


def test_reconciler_give_up_holds_document_then_job_locks(
    lost: SimpleNamespace,
) -> None:
    engine = lost.Session.kw["bind"]
    locks: list[str] = []
    probes: list[str] = []

    def probe(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "FOR UPDATE" in statement:
            if "FROM documents" in statement:
                locks.append("document")
            elif "FROM processing_jobs" in statement:
                locks.append("job")
        if not statement.startswith("UPDATE ") or probes:
            return
        for model, row_id in [
            (Document, lost.doc_id),
            (ProcessingJob, lost.job_id),
        ]:
            with lost.Session() as other:
                with pytest.raises(OperationalError) as raised:
                    other.execute(
                        select(model.id)
                        .where(model.id == row_id)
                        .with_for_update(nowait=True)
                    )
                assert getattr(raised.value.orig, "pgcode", None) == "55P03"
                probes.append(model.__name__)
                other.rollback()

    event.listen(engine, "before_cursor_execute", probe)
    try:
        assert rj.reconcile_lost_processing_jobs()["failed"] == 1
    finally:
        event.remove(engine, "before_cursor_execute", probe)
    assert locks[:2] == ["document", "job"]
    assert probes == ["Document", "ProcessingJob"]


@pytest.fixture
def ended(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A document left PROCESSING by a job-only terminal write (IN-1): its
    only ingestion job is FAILED, so the job sweep never selects it."""
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    with _ingestion() as env:
        with env.Session() as db:
            job = db.get(ProcessingJob, env.job_id)
            job.fail_job(
                "Lost job: post-commit dispatch never reached a worker",
                error_type="LostJobReconcileGaveUp",
            )
            job.updated_at = stale
            document = db.get(Document, env.doc_id)
            document.processing_status = ProcessingStatus.PROCESSING
            document.updated_at = stale
            db.commit()
        monkeypatch.setattr(pt, "SessionLocal", env.Session)
        monkeypatch.setattr(
            "src.core.config.get_settings",
            lambda: SimpleNamespace(
                SWEEPERS_ENABLED=True, PROCESSING_JOB_STUCK_AFTER_SECONDS=1800
            ),
        )
        yield env


def test_sweep_repairs_document_whose_ingestion_already_ended(
    ended: SimpleNamespace,
) -> None:
    assert pt.sweep_stuck_processing_jobs() == {"swept": 0, "repaired_documents": 1}
    with ended.Session() as db:
        job = db.get(ProcessingJob, ended.job_id)
        document = db.get(Document, ended.doc_id)
        assert document.processing_status == ProcessingStatus.FAILED
        assert document.processing_error == job.error_message


@pytest.mark.parametrize("kind", ["completed", "retried", "fresh", "deleted"])
def test_sweep_repair_leaves_documents_it_cannot_prove_ended(
    ended: SimpleNamespace, kind: str
) -> None:
    with ended.Session() as db:
        document = db.get(Document, ended.doc_id)
        if kind == "completed":
            db.get(ProcessingJob, ended.job_id).status = JobStatus.COMPLETED
        elif kind == "retried":
            db.add(
                ProcessingJob(
                    document_id=ended.doc_id,
                    organization_id=ended.org_id,
                    job_type=JobType.DOCUMENT_INGESTION,
                    status=JobStatus.QUEUED,
                    updated_at=datetime.now(timezone.utc),
                )
            )
        elif kind == "fresh":
            document.updated_at = datetime.now(timezone.utc)
        else:
            document.soft_delete()
        db.commit()
    assert pt.sweep_stuck_processing_jobs() == {"swept": 0}
    with ended.Session() as db:
        assert (
            db.get(Document, ended.doc_id).processing_status
            == ProcessingStatus.PROCESSING
        )

"""Terminal sibling retries must serialize with the recovery ownership decision.

PostgreSQL regression for the active-only sibling lock selection in GOO-359.
Requires ORCHESTRATION_TEST_DATABASE_URL; uses the existing isolated fixture.

Mutation receipt (2026-10-04): restoring the non-terminal status predicate in
sweep_stuck_processing_jobs' sibling job_scope failed this test with
"Failed sibling retried after recovery took ownership". After restoration,
the test passed in the disposable PostgreSQL/Redis runner. Exact selector:
  PYTHONPATH=backend pytest -q backend/tests/unit/tasks/test_stuck_recovery_terminal_retry.py
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from src.models.document import Document, ProcessingStatus
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.tasks import processing_tasks as pt
from tests.unit.tasks.test_ingestion_stage_guard_postgres import _ingestion

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


def test_failed_sibling_cannot_retry_during_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _ingestion() as env:
        sibling_id = uuid4()
        with env.Session() as db:
            stale = db.get(ProcessingJob, env.job_id)
            stale.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
            db.get(Document, env.doc_id).processing_status = ProcessingStatus.PROCESSING
            db.add(
                ProcessingJob(
                    id=sibling_id,
                    document_id=env.doc_id,
                    organization_id=env.org_id,
                    job_type=JobType.DOCUMENT_INGESTION,
                    status=JobStatus.FAILED,
                    celery_task_id="old-sibling-attempt",
                )
            )
            db.commit()
        original = ProcessingJob.fail_job
        blocked = []

        def fail_and_attempt_retry(job: Any, *args: Any, **kwargs: Any) -> None:
            original(job, *args, **kwargs)
            if job.id != env.job_id:
                return
            # retry_failed_jobs and the processing retry API can update an
            # existing failed row without first acquiring a document lock.
            with env.Session() as retry:
                try:
                    sibling = (
                        retry.query(ProcessingJob)
                        .filter(ProcessingJob.id == sibling_id)
                        .with_for_update(nowait=True)
                        .one()
                    )
                except OperationalError as exc:
                    assert getattr(exc.orig, "pgcode", None) == "55P03"
                    blocked.append(True)
                    retry.rollback()
                else:
                    sibling.retry_job()
                    retry.commit()
                    blocked.append(False)

        monkeypatch.setattr(pt, "SessionLocal", env.Session)
        monkeypatch.setattr(ProcessingJob, "fail_job", fail_and_attempt_retry)
        with patch("src.core.config.get_settings") as settings:
            settings.return_value.SWEEPERS_ENABLED = True
            settings.return_value.PROCESSING_JOB_STUCK_AFTER_SECONDS = 1800
            assert pt.sweep_stuck_processing_jobs() == {"swept": 1}
        assert blocked == [True], "Failed sibling retried after recovery took ownership"

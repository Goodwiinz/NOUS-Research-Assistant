"""Celery beat schedules from every task module must coexist.

Three task modules register periodic tasks on the SHARED ``app.conf.beat_schedule``.
Each used a full assignment (``conf.beat_schedule = {...}``), so whichever module
Celery imported last clobbered the others — leaving only ``cleanup-old-evaluations``
live and silently dropping ``cleanup-old-jobs``, ``cleanup-artifacts``,
``health-check`` and ``generate-reports`` from the beat schedule. Switching to
``.update({...})`` lets them all coexist regardless of import order.

This test imports the three modules (their module-level registration runs on
import) and asserts every schedule key survives.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

_EXPECTED_KEYS = {
    "cleanup-old-jobs",  # processing_tasks
    "cleanup-artifacts",  # document_processing_tasks
    "health-check",  # document_processing_tasks
    "generate-reports",  # document_processing_tasks
    "cleanup-old-evaluations",  # evaluation_tasks
    # Defined centrally in celery_app.py — a module-level full assignment would
    # clobber these too, so they belong in this guard.
    "sweep-stale-agent-runs",  # celery_app (task in agent_run_tasks)
    "drain-integration-actions",  # celery_app (task in integration_action_tasks)
    "sweep-stale-integration-actions",  # celery_app (integration_action_tasks)
    "sweep-stuck-processing-jobs",  # celery_app (task in processing_tasks)
    "retention-purge-soft-deleted-threads",  # celery_app (retention_tasks)
    "retention-purge-synthetic-threads",  # celery_app (retention_tasks)
    "retention-purge-append-only-events",  # celery_app (retention_tasks)
    "reconcile-satellite-indexes",  # celery_app (reconcile_tasks)
    "reconcile-lost-processing-jobs",  # celery_app (reconcile_jobs, Task 1.4)
    "drain-deposits",  # celery_app (deposit_tasks, GOO-318)
}


def test_all_task_modules_beat_schedules_coexist():
    # Import order intentionally matches celery_app.include; with the old
    # full-assignment the last import wins and only its keys remain.
    import src.tasks.agent_run_tasks  # noqa: F401
    import src.tasks.document_processing_tasks  # noqa: F401
    import src.tasks.evaluation_tasks  # noqa: F401
    import src.tasks.processing_tasks  # noqa: F401
    import src.tasks.reconcile_jobs  # noqa: F401
    import src.tasks.reconcile_tasks  # noqa: F401
    import src.tasks.retention_tasks  # noqa: F401
    from src.tasks.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    missing = _EXPECTED_KEYS - set(schedule)
    assert not missing, f"beat schedules clobbered — missing {sorted(missing)}"


def test_scheduled_tasks_resolve_to_registered_tasks():
    import src.tasks.agent_run_tasks  # noqa: F401
    import src.tasks.deposit_tasks  # noqa: F401
    import src.tasks.document_processing_tasks  # noqa: F401
    import src.tasks.evaluation_tasks  # noqa: F401
    import src.tasks.integration_action_tasks  # noqa: F401
    import src.tasks.processing_tasks  # noqa: F401
    import src.tasks.reconcile_jobs  # noqa: F401
    import src.tasks.reconcile_tasks  # noqa: F401
    import src.tasks.retention_tasks  # noqa: F401
    from src.tasks.celery_app import celery_app

    for key in _EXPECTED_KEYS:
        entry = celery_app.conf.beat_schedule[key]
        task_path = entry["task"]
        assert (
            task_path in celery_app.tasks
        ), f"beat entry {key!r} points at unregistered task {task_path!r}"

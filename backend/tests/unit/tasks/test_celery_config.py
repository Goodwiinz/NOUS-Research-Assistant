"""Celery config guards.

``task_acks_late=True`` means a task is only ACKed after it finishes — but the
replay-guard architecture (see ``src/tasks/replay_guard.py``) assumes the
broker actually *redelivers* the message when a worker child dies mid-task
(e.g. OOM-killed by the ``worker_max_memory_per_child`` limit). Celery's
default for ``task_reject_on_worker_lost`` is False, which ACKs the message
on child death anyway — silently dropping it instead of redelivering. This
test guards against that default creeping back in.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_task_reject_on_worker_lost_is_enabled() -> None:
    from src.tasks.celery_app import celery_app

    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_worker_process_init_configures_langsmith(monkeypatch) -> None:
    """R8-C8: each forked worker child applies the LangSmith hide-IO guard
    before its first task, not only when an agent turn happens to run."""
    import src.tasks.celery_app  # noqa: F401 - registers the receiver
    from celery.signals import worker_process_init
    from src.services.agent import observability

    calls: list[str] = []
    monkeypatch.setattr(
        observability, "configure_langsmith", lambda: calls.append("configured")
    )

    worker_process_init.send(sender=None)

    assert calls == ["configured"]


def _registered_hard_time_limits() -> dict[str, int]:
    from src.tasks.celery_app import celery_app

    celery_app.loader.import_default_modules()
    limits = {
        name: int(task.time_limit)
        for name, task in celery_app.tasks.items()
        if getattr(task, "time_limit", None)
    }
    limits["<global task_time_limit>"] = int(celery_app.conf.task_time_limit)
    return limits


def test_visibility_timeout_outlives_every_task_and_the_stale_claim_window() -> None:
    """An unacked message is redelivered after ``visibility_timeout``.

    If that is shorter than a task's hard limit, a task that is still running
    gets a second copy; replay_guard skips a RUNNING claim younger than
    DEFAULT_STALE_RUNNING_SECONDS, so the timeout must exceed that too.
    """
    from src.tasks.celery_app import celery_app
    from src.tasks.replay_guard import DEFAULT_STALE_RUNNING_SECONDS

    visibility = celery_app.conf.broker_transport_options["visibility_timeout"]
    limits = _registered_hard_time_limits()

    too_long = {name: limit for name, limit in limits.items() if limit >= visibility}
    assert not too_long, f"tasks outlive the broker visibility timeout: {too_long}"
    assert visibility > DEFAULT_STALE_RUNNING_SECONDS


def test_worker_grace_period_covers_the_global_task_time_limit() -> None:
    """SIGTERM starts a warm shutdown; the pod must live until tasks finish.

    Kubernetes' 30s default SIGKILLed in-flight ingestion on every rollout.
    """
    import re
    from pathlib import Path

    from src.tasks.celery_app import celery_app

    chart = (
        Path(__file__).resolve().parents[4]
        / "infrastructure/helm/knowledge-graph-analytics"
    )
    template = (chart / "templates/celery-worker-deployment.yaml").read_text()
    values = (chart / "values.yaml").read_text()

    default = re.search(
        r"terminationGracePeriodSeconds: \{\{ \.Values\.celeryWorker\."
        r"terminationGracePeriodSeconds \| default (\d+) \}\}",
        template,
    )
    assert default, "worker template must render terminationGracePeriodSeconds"
    worker_block = values.split("\nceleryWorker:\n", 1)[1].split("\n\n", 1)[0]
    configured = re.search(
        r"^  terminationGracePeriodSeconds: (\d+)$", worker_block, re.M
    )
    assert configured, "values.yaml must set celeryWorker.terminationGracePeriodSeconds"

    hard_limit = int(celery_app.conf.task_time_limit)
    assert int(default.group(1)) > hard_limit
    assert int(configured.group(1)) > hard_limit

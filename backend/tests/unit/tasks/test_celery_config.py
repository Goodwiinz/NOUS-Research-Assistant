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
    from celery.signals import worker_process_init

    import src.tasks.celery_app  # noqa: F401 - registers the receiver
    from src.services.agent import observability

    calls: list[str] = []
    monkeypatch.setattr(
        observability, "configure_langsmith", lambda: calls.append("configured")
    )

    worker_process_init.send(sender=None)

    assert calls == ["configured"]

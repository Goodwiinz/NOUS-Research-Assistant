"""
Unified Celery application configuration.

All task modules should import celery_app from this module:
    from src.tasks.celery_app import celery_app
"""

import logging
import ssl
from typing import Any

from celery import Celery
from celery.signals import worker_process_init

from src.core.config import settings

logger = logging.getLogger(__name__)

# Build SSL config when using rediss:// (TLS) connections
_redis_uses_tls = settings.REDIS_URL.startswith("rediss://")
_broker_use_ssl = {"ssl_cert_reqs": ssl.CERT_NONE} if _redis_uses_tls else None

_backend_url = (
    f"{settings.REDIS_URL}?ssl_cert_reqs=CERT_NONE"
    if _redis_uses_tls
    else settings.REDIS_URL
)

celery_app = Celery(
    "nous",
    broker=settings.REDIS_URL,
    backend=_backend_url,
    include=[
        "src.tasks.processing_tasks",
        "src.tasks.document_processing_tasks",
        "src.tasks.summarize_thread_task",
        "src.tasks.evaluation_tasks",
        "src.tasks.research_tasks",
        "src.tasks.agent_run_tasks",
        "src.tasks.harness_dispatch",
        "src.tasks.retention_tasks",
        "src.tasks.reconcile_tasks",
        "src.tasks.reconcile_jobs",
        "src.tasks.research_run_tasks",
        "src.tasks.artifact_tasks",
        "src.tasks.integration_action_tasks",
        "src.tasks.deposit_tasks",
    ],
)

_ssl_conf = {}
if _redis_uses_tls:
    _ssl_conf = {
        "broker_use_ssl": _broker_use_ssl,
        "redis_backend_use_ssl": _broker_use_ssl,
    }

celery_app.conf.update(
    **_ssl_conf,
    # Bound the publish path. Kombu's Redis transport takes its socket
    # timeouts from broker_transport_options, NOT from
    # broker_connection_timeout, so without these a blackholed broker (a DO
    # Managed Redis partition or a hung TLS handshake — not the fast
    # connection-refused case) falls through to OS TCP retries, ~130s per
    # attempt. Any request thread that publishes inline would block for
    # minutes. Paired with a bounded retry policy so a publish fails fast
    # enough for the caller to be told.
    broker_transport_options={
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
    },
    task_publish_retry_policy={
        "max_retries": 1,
        "interval_start": 0,
        "interval_step": 0.2,
        "interval_max": 0.2,
    },
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    # replay_guard.py's claim-based idempotency assumes the broker redelivers a
    # task when the worker child dies mid-run (e.g. OOM-killed by the memory
    # limits below). Without this, task_acks_late's default reject_on_worker_lost
    # (False) ACKs the message anyway on child death, so it's silently dropped
    # instead of redelivered. claim_job_for_processing already makes redelivery
    # idempotent, so redelivering is safe.
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Bound worker memory to stop unbounded RSS growth from OOM-evicting the pod.
    # A child process is recycled after 100 tasks, or once its resident memory
    # crosses ~900MB (value is in KB). With --concurrency=2 this keeps total
    # worker RSS (2 x ~900MB) under the pod's 2Gi memory limit.
    worker_max_tasks_per_child=100,
    worker_max_memory_per_child=900000,
    task_default_queue="celery",
    task_queues={
        "celery": {"exchange": "celery", "routing_key": "celery"},
        "document_processing": {
            "exchange": "document_processing",
            "routing_key": "document_processing",
        },
        "high_priority": {
            "exchange": "high_priority",
            "routing_key": "high_priority",
        },
        "low_priority": {
            "exchange": "low_priority",
            "routing_key": "low_priority",
        },
        # Dedicated queue for AGENT_DISPATCH_BACKEND=celery turns (audit
        # P1.3): agent turns must not sit behind heavy document-processing
        # backlogs. The helm worker command consumes it (-Q ...,agent_runs).
        "agent_runs": {
            "exchange": "agent_runs",
            "routing_key": "agent_runs",
        },
    },
    task_routes={
        "src.tasks.agent_run_tasks.run_agent_job": {
            "queue": "agent_runs",
        },
        "src.tasks.artifact_tasks.drain_artifacts": {"queue": "agent_runs"},
        "src.tasks.artifact_tasks.sweep_artifact_uploads": {"queue": "agent_runs"},
        "src.tasks.deposit_tasks.drain_deposits": {"queue": "agent_runs"},
        "src.tasks.integration_action_tasks.drain_integration_actions": {
            "queue": "agent_runs"
        },
        "src.tasks.integration_action_tasks.sweep_stale_actions": {
            "queue": "agent_runs"
        },
    },
    # Audit P1.4 sweepers (flag-gated at runtime by SWEEPERS_ENABLED; the
    # tasks self-skip when disabled). Task modules merge additional entries
    # via conf.beat_schedule.update(...) — this assignment runs first, at
    # celery_app import time, so nothing is clobbered.
    beat_schedule={
        "drain-artifacts": {
            "task": "src.tasks.artifact_tasks.drain_artifacts",
            "schedule": 2.0,
        },
        "drain-deposits": {  # GOO-318: one phase per queued deposit
            "task": "src.tasks.deposit_tasks.drain_deposits",
            "schedule": 10.0,
        },
        "sweep-artifact-uploads": {
            "task": "src.tasks.artifact_tasks.sweep_artifact_uploads",
            "schedule": 60.0,
        },
        "drain-integration-actions": {
            "task": "src.tasks.integration_action_tasks.drain_integration_actions",
            "schedule": 2.0,
        },
        "sweep-stale-integration-actions": {
            "task": "src.tasks.integration_action_tasks.sweep_stale_actions",
            "schedule": 60.0,
        },
        "dispatch-harness": {
            "task": "src.tasks.harness_dispatch.dispatch_harness",
            "schedule": 5.0,
        },
        "sweep-stale-agent-runs": {
            "task": "src.tasks.agent_run_tasks.sweep_stale_agent_runs",
            "schedule": 600.0,  # every 10 min; stale threshold is 30 min
        },
        "sweep-stuck-processing-jobs": {
            "task": "src.tasks.processing_tasks.sweep_stuck_processing_jobs",
            "schedule": 900.0,  # every 15 min; stuck threshold is 30 min
        },
        # Audit D5 (P2.5) retention riders. Two-stage safe: no-op unless
        # RETENTION_ENABLED, dry-run (log-only) unless RETENTION_APPLY=true.
        "retention-purge-soft-deleted-threads": {
            "task": "src.tasks.retention_tasks.purge_soft_deleted_threads",
            "schedule": 86400.0,  # daily
        },
        "retention-purge-synthetic-threads": {
            "task": "src.tasks.retention_tasks.purge_synthetic_threads",
            "schedule": 86400.0,  # daily; closes the synthetic checkpoint leak
        },
        "retention-purge-append-only-events": {
            "task": "src.tasks.retention_tasks.purge_append_only_events",
            "schedule": 86400.0,  # daily
        },
        # Audit P2.3 (D1): satellite reconciler. Report-only by default
        # (RECONCILER_APPLY=false); RECONCILER_ENABLED is the kill switch.
        "reconcile-satellite-indexes": {
            "task": "src.tasks.reconcile_tasks.reconcile_satellite_indexes",
            "schedule": 1800.0,  # every 30 min; rate-capped per run
        },
        # Task 1.4: re-enqueue processing_jobs whose post-commit Celery dispatch
        # was lost to a broker outage (PENDING/celery_task_id=NULL). Runs at the
        # lost-job window (10 min), ahead of the 30-min stuck-job sweep.
        "sweep-stale-research-runs": {
            # R5-M17: worker death bricked runs in RUNNING forever.
            "task": "src.tasks.research_run_tasks.sweep_stale_research_runs",
            "schedule": 1800.0,  # every 30 min; stale threshold is 2h
        },
        "sweep-expired-reruns": {  # GOO-313: expired rerun leases
            "task": "src.tasks.research_run_tasks.sweep_expired_reruns",
            "schedule": 60.0,
        },
        "reconcile-lost-processing-jobs": {
            "task": "src.tasks.reconcile_jobs.reconcile_lost_processing_jobs",
            "schedule": 600.0,  # every 10 min
        },
    },
    task_default_retry_delay=60,
    task_max_retries=3,
    task_soft_time_limit=300,
    task_time_limit=600,
)

logger.info(f"Celery app configured with broker: {settings.REDIS_URL[:20]}...")


@worker_process_init.connect
def _configure_langsmith_in_worker(**_kwargs: Any) -> None:
    """Apply the LangSmith hide-IO guard in each worker child (R8-C8).

    The API process does this at startup; without it here, a non-agent task
    could trace (and build the shared client) before any agent turn ran
    ``configure_langsmith``.
    """
    try:
        from src.services.agent.observability import configure_langsmith

        configure_langsmith()
    except Exception:
        logger.warning("LangSmith configuration failed in worker", exc_info=True)

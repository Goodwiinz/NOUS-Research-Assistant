# Celery tasks

Background work for the backend. The single Celery app is `celery_app` in `celery_app.py` (broker and result backend are `REDIS_URL`, with TLS options when the URL is `rediss://`). Every task module is listed in its `include=[...]` and every module imports `celery_app` from there. The app sets `task_acks_late=True`, so a worker killed mid-task has its message redelivered; tasks must be idempotent or guard themselves with `replay_guard.py`. The beat schedule is declared in `celery_app.py` (for example `drain-artifacts` every 2 seconds).

## Task modules

| File | Purpose |
| --- | --- |
| `processing_tasks.py`, `document_processing_tasks.py`, `processing_lifecycle.py` | Document ingestion and processing pipeline; `processing_lifecycle.py` is the stage-write guard. |
| `document_embedding_tasks.py` | pgvector document embeddings; only meaningful when the semantic-search flags are on. |
| `agent_run_tasks.py`, `harness_dispatch.py` | Agent `/execute` turns, the agent-run sweeper, and database-backed harness dispatch with terminal-projection retry. |
| `artifact_tasks.py`, `integration_action_tasks.py`, `deposit_tasks.py`, `search_update_tasks.py` | Beat tasks: artifact version announcements, approved integration actions, Zenodo sandbox deposits, scheduled search updates (no-op unless `SEARCH_UPDATES_ENABLED`). |
| `research_tasks.py`, `research_run_tasks.py` | Research workflow execution and the sweeper for runs stuck in RUNNING. |
| `reconcile_jobs.py`, `reconcile_tasks.py` | Lost-job reconciler and scheduled satellite-index reconciler. |
| `retention_tasks.py` | Data retention beat tasks, including the soft-deleted thread purge. |
| `summarize_thread_task.py`, `evaluation_tasks.py` | Thread summarization and evaluation metrics. |
| `analytics_processor.py`, `data_aggregator.py`, `recommendation_generator.py` | Analytics job processing and aggregation. |

## Helpers

- `enqueue.py`: `enqueue_after_commit` and `enqueue_after_commit_apply_async` defer an enqueue until the owning session commits and drop it on rollback. Use them instead of enqueueing before a commit, which lets a worker read a row that is not yet durable.
- `replay_guard.py`: `claim_job_for_processing` makes the redelivery decision atomic for document processing (never redo finished work, never double-run a live job, take over an abandoned one).
- `_async_utils.py`: the shared async boundary for running coroutines inside sync Celery tasks.

## Rules

- Never pass a request session or ORM user into a task; pass scalar ids and load them inside the task, scoped to the organization.
- A task that mutates state commits its own transaction; callers enqueue only after their commit.
- `backend/celery_worker.py` and `backend/celery_beat.py` are the process entry points.
- Report a task test that needs Redis or a database as NOT RUN if the service is unavailable. Tests live under `backend/tests/unit/tasks` and `backend/tests/tasks`.

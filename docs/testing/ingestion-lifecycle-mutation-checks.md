# Ingestion lifecycle mutation checks

Guards added by the ingestion lifecycle repairs (Linear plan "Ingestion
lifecycle repairs — Astra audit fix plan", 2026-10-01). Each proof disabled one
guard, ran the focused test and read the assertion failure, restored the file
from a byte-for-byte copy (`git diff` on it empty), and reran the test green.
No mutant was committed.

Postgres tests need `ORCHESTRATION_TEST_DATABASE_URL` pointing at a disposable
PostgreSQL database; each test creates and drops its own schema. Without it
they skip, so treat them as NOT RUN, not passing. Run from `backend/`.

## GOO-356: deleted ingestion cannot start or double-release quota

Verified in PR #1798 against local PostgreSQL 2026-10-01.

### G1. Claim refuses deleted work

- **Guard:** `backend/src/tasks/replay_guard.py:127`,
  `claim_job_for_processing`: `if locked.is_deleted or (<document deleted>)`.
- **Mutation:** condition replaced by `if False and (`.
- **Command:** `pytest -q
  tests/unit/tasks/test_replay_idempotency.py::test_claim_refuses_deleted_ingestion`
- **Observed:** both `[job]` and `[document]` cases fail with
  `assert True is False` (the deleted job was claimed).
- **Note:** `test_non_cascade_delete_still_cancels_ingestion` still passes
  under this mutation because the delete already moved the job to CANCELLED.
  The guard matters for jobs that were deleted before this fix, or by any
  writer that does not cancel. The SQLite cases seed exactly that state.

### G2. Document row lock serializes concurrent deletes

- **Guard:** `backend/src/services/documents/file_service.py:1016`,
  `FileService.soft_delete_documents`: `.with_for_update()` on the live
  Document select. A waiting delete re-evaluates `is_deleted == False` once
  the winner commits and finds nothing.
- **Mutation:** that `.with_for_update()` removed.
- **Command:** `pytest -q
  tests/integration/test_document_deletion_postgres.py::test_concurrent_deletes_release_quota_once`
- **Observed:** `AssertionError: [None, None]`: both deletes succeeded, and the
  quota was released twice.

### G3. Delete cancels unfinished jobs

- **Guard:** `backend/src/services/documents/file_service.py:1045`,
  `if not job.is_finished: job.cancel_job() ...`.
- **Mutation:** condition replaced by `if False:`.
- **Command:** `pytest -q
  tests/integration/test_document_deletion_postgres.py::test_deleted_ingestion_is_not_claimed`
- **Observed:** `assert <JobStatus.QUEUED> == <JobStatus.CANCELLED>`: the
  deleted document's job stayed queued.

## GOO-357: a running worker cannot write after it lost the job

Verified in PR #1800 against local PostgreSQL 2026-10-01. Task-level tests
are in `tests/unit/tasks/test_ingestion_stage_guard_postgres.py` (here, not
under `tests/integration/`, because that conftest mocks Redis, which breaks
Celery's eager `.apply()`). The helper matrix is
`tests/unit/tasks/test_processing_lifecycle.py`.

**Red before the fix:** with the pre-fix `processing_tasks.py` from the GOO-356
branch, 5 of 8 task-level tests fail. Cancelled, deleted and superseded
attempts all finish `completed` with live entities; a cancel at finalize still
completes; a crash after cancellation writes FAILED over CANCELLED.

| Guard | Mutation | Focused test | Observed failure |
| --- | --- | --- | --- |
| G4 stage commits, `processing_tasks.py:318` `commit_if_active` | `return db.commit()` before the check | `test_cancel_after_extraction_blocks_stage_commit` (3 cases) | `assert 2 == 0` live entities |
| G5 completion check, `processing_tasks.py:456` | check call replaced by a no-op | `test_cancel_before_final_completion_keeps_cancelled` | `KeyError: 'skipped'` (task completed) |
| G6 failure-path ownership, `processing_tasks.py:516` | check replaced by plain `db.get` | `test_failure_after_cancellation_does_not_overwrite_cancelled` | `FAILED == CANCELLED` |
| G7 `on_failure`, `processing_tasks.py:217` | `not job.is_finished` → `True` | same | `FAILED == CANCELLED` |
| G8 task-id check, `processing_lifecycle.py:92` | `if False:` | `[_newer_retry-superseded]` + `test_lost_attempt_is_stopped[_supersede-*]` | completed / `DID NOT RAISE` |
| G9 explicit flush, `processing_lifecycle.py:54` | removed | `test_live_attempt_passes_and_keeps_pending_stage_changes` | `assert None == 'extracted'` (refresh discarded the stage result) |

An earlier G6 mutant (`rollback` → `raise`) survived because the raise landed
in the outer `except` and wrote nothing. It was not a guard removal, so it was
replaced by the plain-`db.get` mutant above.

### GOO-357 review fixes (PR #1800)

Both defects were reproduced on PostgreSQL before the fix: a real
`DeadlockDetected`, and a live run stopped as `queued`.

| Guard | Mutation | Focused test | Observed failure |
| --- | --- | --- | --- |
| G10 lock before flush, `processing_lifecycle.py:71` | `db.flush()` moved back above the lock selects | `test_guard_does_not_deadlock_with_a_concurrent_delete` | `DeadlockDetected` |
| G11 lock before entity reset, `processing_tasks.py:402` | guard call replaced by a no-op | `test_entity_reset_does_not_deadlock_with_a_cascading_delete` | `DeadlockDetected` |
| G12 late producer QUEUED, `processing_lifecycle.py:97` | `if False:` | `test_producer_late_queued_write_does_not_stop_live_run` | `'stopped' == 'completed'` |

## GOO-358: remote writes that land after deletion are cleaned up

Verified in the GOO-358 PR against local PostgreSQL 2026-10-02. Task-level
tests are in `tests/unit/tasks/test_ingestion_stage_guard_postgres.py`;
reconciler tests are in `tests/unit/tasks/test_reconcile_tasks.py`.

**Red before the fix:** with the pre-fix `processing_tasks.py` and
`reconcile_tasks.py` from the GOO-357 branch, all 5 new task-level tests fail.
A late DO KB data source was never removed (`[] == ['ds-late']`). A late graph
write was never cleaned up. The DO KB bridge's `merge()` reverted a swept
FAILED document to PENDING.

| Guard | Mutation | Focused test | Observed failure |
| --- | --- | --- | --- |
| G13 fresh row in DO KB bridge, `processing_tasks.py:92` | `kb_db.get` → `kb_db.merge(document)` | `test_stale_snapshot_does_not_revert_state_through_do_kb_sync` | `PENDING == FAILED` |
| G14 compensation on deletion stop, `processing_tasks.py:560` | condition → `False` | `test_late_do_kb_success_is_compensated[*]`, `test_late_graph_write_is_compensated[*]` | no unsync / no graph delete |
| G15 reconciler selects deleted + neo4j failed, `reconcile_tasks.py` `_reconcilable_filters` | clause → `False` | `test_late_graph_write_is_compensated[False]`, `test_apply_retries_deleted_document_graph_cleanup` | `0 == 1` |
| G16 failure marker, `reconcile_tasks.py:148` | `_FAILED` → `_COMPLETED` | `test_late_graph_write_is_compensated[False]`, `test_failed_deleted_document_graph_cleanup_remains_retryable` | `'completed' == 'failed'` |

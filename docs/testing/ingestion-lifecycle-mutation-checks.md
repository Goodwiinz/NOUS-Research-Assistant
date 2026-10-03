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
| G15 reconciler selects deleted + neo4j failed, `reconcile_tasks.py:89` `_reconcilable_filters` | clause → `False` | `test_late_graph_write_is_compensated[False]`, `test_apply_retries_deleted_document_graph_cleanup` | `0 == 1` |
| G16 cleanup failure marker, `reconcile_tasks.py:246` `_cleanup_deleted_document_graph` | `_FAILED` → `_COMPLETED` | `test_late_graph_write_is_compensated[False]`, `test_failed_deleted_document_graph_cleanup_remains_retryable` | `'completed' == 'failed'` |

### GOO-358 review fixes

The independent review found three more gaps, and each was reproduced red
first. The CodeRabbit review flagged the wrong G16 line number above, which is
now corrected.

| Guard | Mutation | Focused test | Observed failure |
| --- | --- | --- | --- |
| G17 compensation on the generic failure path, `processing_tasks.py:585` | removed | `test_late_graph_write_is_compensated_when_the_stage_then_crashes` | no graph delete |
| G18 re-drive re-checks deletion, `reconcile_tasks.py:143` `_redrive_neo4j` | check removed | `test_redrive_racing_a_delete_cleans_up_instead_of_completing` | no graph delete; row marked completed |
| G19 deleted + neo4j pending selected, `reconcile_tasks.py:89` | `(_FAILED, _PENDING)` → `(_FAILED,)` | `test_deleted_document_left_pending_is_cleaned_up` | `0 == 1` |



## GOO-356 cancellation serialization follow-up — 2026-10-03

## GOO-356 cancellation and claim follow-up, 2026-10-03

This follow-up extends merged PR #1798, starting at develop commit
`4555c24066e476ee19f17dda6156b5a1b83a2ef9`. The dated checks above retain
their original source references. References below describe this follow-up.

All 27 mutations below failed their focused tests, then passed after restoring
the exact source bytes. The initial repair also had 11 behavioral failures
before implementation. Only broker, physical storage and satellite transports
were replaced; PostgreSQL queries, row locks, commits and independent observer
sessions were real. These checks do not exercise a deployed broker or provider.

Run from the repository root with its installed test dependencies and a
disposable database. Each PostgreSQL case creates and drops a unique schema
containing only Organization, User, Document, ProcessingJob and Entity tables:

```bash
export ORCHESTRATION_TEST_DATABASE_URL='postgresql://root@/codex_goo356_20261003'
python3 -m pytest -q -o addopts= -o log_cli=false \
  backend/tests/integration/test_document_deletion_postgres.py
python3 -m pytest -q -o addopts= -o log_cli=false \
  backend/tests/unit/tasks/test_replay_idempotency.py
```

To reproduce one row, temporarily apply its mutation and run its named test
with `FILE::TEST` instead of the whole file. Restore the exact source and rerun
the same selector. Quote selectors containing parameter brackets.

In the matrix, **claim** means
`backend/src/tasks/replay_guard.py:claim_job_for_processing`; **cancel** means
`backend/src/services/documents/file_service.py:FileService.cancel_upload_job`;
**delete** means that service's `soft_delete_documents`. Tests are in the
PostgreSQL file above except the three explicitly marked **unit**, which are
in `test_replay_idempotency.py`. Line references are the unmutated source.

| Guard | Mutation | Focused test | Failure with mutation; restored result |
| --- | --- | --- | --- |
| G13 claim:142 deletion denial | Disable condition | **unit** `test_claim_refuses_deleted_ingestion` | Deleted work claimed; 2 pass |
| G14 claim:125 Document lock | Remove `with_for_update()` | `test_claim_locks_document_before_starting` | Expected lock timeout absent; 1 pass |
| G15 claim:116 lock order | Lock job before document | `test_claim_does_not_lock_job_while_waiting_for_document` | Competing job lock gets PostgreSQL 55P03; 1 pass |
| G16 claim:124 Document refresh | Remove `populate_existing()` | `test_claim_refreshes_deleted_document` | Stale live document claimed; 1 pass |
| G17 claim:131 job refresh | Remove `populate_existing()` | **unit** `test_claim_reads_freshly_locked_row_not_stale_cache` | Running job claimed twice; 1 pass |
| G18 claim:150 association/missing-document denial | Disable condition | **unit** `test_claim_refuses_missing_or_foreign_document`; `test_claim_rechecks_document_association` | Inaccessible/remapped document accepted; 4 pass |
| G19 cancel:1088 Document lock | Remove `with_for_update()` | `test_cancellation_does_not_lock_job_while_waiting_for_document[task]` | Required document-lock attempt never observed within bounded wait; 1 pass |
| G20 cancel:1079 lock order | Lock job before document | `test_cancellation_does_not_lock_job_while_waiting_for_document[task]` | Competing job lock gets 55P03; 1 pass |
| G21 cancel:1104 job lock | Remove `with_for_update()` | `test_cancellation_locks_job_before_checking_its_state` | Independent publisher can acquire job lock; 1 pass |
| G22 cancel:1089 Document refresh | Disable `populate_existing` | `test_task_id_cancellation_refreshes_document` | Deleted document accepted / completed document changed to FAILED; 2 pass |
| G23 cancel:1105 job refresh | Disable `populate_existing` | `test_task_id_cancellation_preserves_concurrent_completion[task]` | Completed job overwritten by stale cancellation; 1 pass |
| G24 cancel:1115 terminal-state denial | Disable condition | `test_task_id_cancellation_preserves_concurrent_completion`; `test_task_id_cancellation_fails_retained_document` | Completed job overwritten; 4 pass |
| G25 cancel:1101 dispatch identity | Remove locked task-ID predicate | `test_task_id_cancellation_rechecks_dispatch_identity` | Reassigned task accepted; 3 pass |
| G26 cancel:1147 other active attempt | Always fail document | `test_cancelled_old_job_preserves_another_active_ingestion` | Another active attempt's document changed to FAILED; 1 pass |
| G27 cancel:1152 commit-before-revoke | Revoke before commit | `test_task_id_cancellation_fails_retained_document` | Broker observer sees QUEUED/PENDING; 2 pass |
| G28 delete:1212 Document refresh | Disable `populate_existing` | `test_delete_rechecks_owner_under_lock` | Changed owner accepted; 1 pass |
| G29 delete:1211 Document lock | Remove `with_for_update()` | `test_concurrent_deletes_release_quota_once` | Both concurrent deletions succeed; 1 pass |
| G30 delete:1248 job cancellation | Disable condition | `test_deleted_ingestion_is_not_claimed` | Job remains QUEUED; 1 pass |
| G31 claim:107 autoflush suppression | Replace `no_autoflush` with a no-op context | `test_claim_does_not_lock_job_while_waiting_for_document` | Dirty snapshot locks job before document; 1 pass |
| G32 cancel:1065 autoflush suppression | Replace `no_autoflush` with a no-op context | `test_cancellation_does_not_lock_job_while_waiting_for_document[task]` | Dirty snapshot locks job before document; 1 pass |
| G33 delete:1212 Document-select autoflush | Enable autoflush | `test_cancellation_does_not_lock_job_while_waiting_for_document[document]` | Dirty snapshot locks job before document; 1 pass |
| G34 delete:1240 job-select autoflush | Enable autoflush | `test_task_id_cancellation_preserves_concurrent_completion[document]` | Dirty stale job changes COMPLETED to CANCELLED; 1 pass |
| G35 delete:1240 job refresh | Disable `populate_existing` | `test_task_id_cancellation_preserves_concurrent_completion[document]` | Cached job changes COMPLETED to CANCELLED; 1 pass |
| G36 cancel:1122 document status | Fail every document status | `test_task_id_cancellation_refreshes_document[completed]` | Completed document changed to FAILED; 1 pass |
| G37 cancel:1092 document deletion | Remove deletion check | `test_task_id_cancellation_refreshes_document[deleted]` | Deleted document accepted; 1 pass |
| G38 cancel:1108 document association | Remove association recheck | `test_task_id_cancellation_rechecks_dispatch_identity[document]` | Changed document link accepted; 1 pass |
| G39 cancel:1102 job deletion | Remove locked live-job predicate | `test_task_id_cancellation_rechecks_dispatch_identity[deleted]` | Deleted job accepted; 1 pass |

The lock-order cases hold the Document lock in one connection while the
claim/cancellation waits in another, then acquire the job with `NOWAIT` in the
first connection. The commit-order case collects state from an independent
observer at broker invocation and asserts outside the best-effort callback,
so an assertion cannot be swallowed as a revocation error.

Additional PostgreSQL regressions verify both cancellation ID forms, broker
failure, duplicate cancellation, foreign tenants, cascade semantics, a commit
failure that rolls back once without revoking, and quota release exactly once.

## GOO-358 follow-up verification — 2026-10-03
Source repair: `abc4a2682`; merged develop baseline: `2a45aa50ce0db947ed91bdbb03534704107e5553`.
Real PostgreSQL fixtures, with remote provider transports simulated. Focused suite: **90 passed**. Covers late writes, ordinary deletion, retry intent, and concurrent changes to cleanup scope. No deployed-provider health claim.
Each guard below was removed in a runtime-only recompilation of its original function. All 15 named checks failed at the expected assertion, then passed with the original function restored. Tracked source was unchanged. A first harness attempt copied function globals and lost dependency patches; those results were discarded and all 15 checks rerun with live module globals.
Run each selector with `ORCHESTRATION_TEST_DATABASE_URL` pointing to disposable PostgreSQL and the backend dependencies available: `PYTHONPATH=backend pytest -q -o addopts= <selector>`. Source line is the function entry; the guard label identifies the exact condition mutated.
| Check | Guard | Source function line | Named selector | Result |
|---|---|---|---|---|
| T4-1 | Persist cascade intent | `backend/src/services/documents/file_service.py:1175` | `backend/tests/unit/tasks/test_ingestion_stage_guard_postgres.py::test_non_cascade_late_graph_write_is_retained` | FAIL → PASS |
| T4-2 | Persist pending cleanup before provider | `backend/src/services/documents/file_service.py:1175` | `backend/tests/integration/test_document_deletion_postgres.py::test_normal_delete_graph_cleanup_is_durable` | FAIL → PASS |
| T4-3 | Late graph cleanup respects cascade | `backend/src/tasks/reconcile_tasks.py:232` | `backend/tests/unit/tasks/test_ingestion_stage_guard_postgres.py::test_non_cascade_late_graph_write_is_retained` | FAIL → PASS |
| T4-4 | Reconciler selection respects cascade | `backend/src/tasks/reconcile_tasks.py:57` | `backend/tests/unit/tasks/test_reconcile_tasks.py::test_non_cascade_deleted_graph_is_not_selected` | FAIL → PASS |
| T4-5 | Refresh cascade intent after re-drive | `backend/src/tasks/reconcile_tasks.py:139` | `backend/tests/unit/tasks/test_reconcile_tasks.py::test_redrive_preserves_non_cascade_graph_after_concurrent_delete` | FAIL → PASS |
| T4-6 | Pre-cleanup tenant scope | `backend/src/services/documents/file_service.py:1385` | `backend/tests/integration/test_document_deletion_postgres.py::test_graph_cleanup_rechecks_scope_and_delete_intent[foreign]` | FAIL → PASS |
| T4-7 | Pre-cleanup deletion scope | `backend/src/services/documents/file_service.py:1385` | `backend/tests/integration/test_document_deletion_postgres.py::test_graph_cleanup_rechecks_scope_and_delete_intent[live]` | FAIL → PASS |
| T4-8 | Pre-cleanup cascade scope | `backend/src/services/documents/file_service.py:1385` | `backend/tests/integration/test_document_deletion_postgres.py::test_graph_cleanup_rechecks_scope_and_delete_intent[non-cascade]` | FAIL → PASS |
| T4-9 | Cleanup-outcome tenant scope | `backend/src/services/documents/file_service.py:1385` | `backend/tests/integration/test_document_deletion_postgres.py::test_graph_cleanup_outcome_does_not_overwrite_changed_row[foreign]` | FAIL → PASS |
| T4-10 | Cleanup-outcome deletion scope | `backend/src/services/documents/file_service.py:1385` | `backend/tests/integration/test_document_deletion_postgres.py::test_graph_cleanup_outcome_does_not_overwrite_changed_row[live]` | FAIL → PASS |
| T4-11 | Cleanup-outcome cascade scope | `backend/src/services/documents/file_service.py:1385` | `backend/tests/integration/test_document_deletion_postgres.py::test_graph_cleanup_outcome_does_not_overwrite_changed_row[non-cascade]` | FAIL → PASS |
| T4-12 | Reload DO KB document rather than merge stale snapshot | `backend/src/tasks/processing_tasks.py:66` | `backend/tests/unit/tasks/test_ingestion_stage_guard_postgres.py::test_stale_snapshot_does_not_revert_state_through_do_kb_sync` | FAIL → PASS |
| T4-13 | Do not report non-cascade graph cleanup | `backend/src/tasks/reconcile_tasks.py:264` | `backend/tests/unit/tasks/test_reconcile_tasks.py::test_non_cascade_do_kb_cleanup_does_not_report_graph_cleanup` | FAIL → PASS |
| T4-14 | Compensate late graph write | `backend/src/tasks/processing_tasks.py:194` | `backend/tests/unit/tasks/test_ingestion_stage_guard_postgres.py::test_late_graph_write_is_compensated` | FAIL → PASS |
| T4-15 | Compensate late DO KB write | `backend/src/tasks/processing_tasks.py:194` | `backend/tests/unit/tasks/test_ingestion_stage_guard_postgres.py::test_late_do_kb_success_is_compensated` | FAIL → PASS |

## GOO-358 cleanup session isolation — 2026-10-03

The real worker/API check reproduced a committed cancellation followed by an
HTTP 500 (`MissingGreenlet`). Three PostgreSQL regressions also reproduced
expired caller objects before the repair. Cleanup now owns a separate
`AsyncSession`; its rollback and commit do not expire caller objects, discard
pending caller changes or commit them. The source repair is `e7c09db01`
(original tested commit `27cbae1bf`; identical source).

All T4-1 through T4-15 checks above were rerun against the isolated-session
implementation: every mutant failed at the intended assertion and every
restored selector passed. The added T4-16 check borrows the caller session
instead of creating its own: `test_graph_cleanup_preserves_caller_objects_and_pending_writes[success]`
fails with the expected `MissingGreenlet`, then passes after restoration.
The initial harness expected only `AssertionError`; its classification was
corrected for this exception reproduction and T4-16 was rerun. No mutant
changed tracked source. The focused cleanup/worker suite passed **53 tests**.

```bash
ORCHESTRATION_TEST_DATABASE_URL='<disposable-postgres-url>' PYTHONPATH=backend \
  pytest -q -o addopts= \
  'backend/tests/integration/test_document_deletion_postgres.py::test_graph_cleanup_preserves_caller_objects_and_pending_writes'
```

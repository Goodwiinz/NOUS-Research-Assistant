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

Verified on `741b3b637` against local PostgreSQL 2026-10-01.

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

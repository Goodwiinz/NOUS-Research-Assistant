# Ingestion lifecycle verification

Verified locally on 2026-10-03 against the [Linear implementation plan](https://linear.app/goodwiinz/document/70ee02e5-0854-4642-ad3e-4aa0f24c89eb).
This dated report describes development checks, not a deployment or a
production provider audit. All remaining issues belong to abdel el bikha.

## Source provenance

- Merged baseline: `2a45aa50ce0db947ed91bdbb03534704107e5553` (GOO-356, PR #1850).
- GOO-355 is already merged in PR #1836; GOO-357 is merged in PR #1800
  (`1b0f821e57773ff266bb4e562354791e2a1e6155`). Their code was not reimplemented.
- GOO-358 repair: `abc4a2682`; isolated cleanup session: `e7c09db01a2300fef6fa93644f7ecf4311687e81`.
  PR #1845 retains its original history through a forward merge.
- GOO-359 repair: `fcf7792ef` (same source as tested `19d362d897e9e725bf43fd92a79af39ba5113f6d`);
  typed lock probe: `00cf5bcb70a3749e84ee646578d886eaa2c236ad`.
- GOO-360 worker tests: `d3284d40a` (source tree identical to tested
  `64fbf27539c3abe8dcd9ceb5088ea784d4681b21`).
- Combined snapshot before this report: `38d0aec91e4ad9216e89f2d58d8853a40d9eea12`.
  Later documentation commits do not change the verified implementation.

## Reproduction and guard verification

GOO-358 had four intended failures before cascade/durable-cleanup fixes,
plus a stale-intent reconciler reproduction. The real worker then exposed
HTTP 500 after a cancellation commit: rollback in graph cleanup expired
its caller's response objects. Three PostgreSQL caller-state regressions
failed with `MissingGreenlet` before the separate-session repair. Afterwards
the API/cleanup/worker checks passed **53 tests**.

GOO-359 began with **12 intended failures and 7 preservation cases passing**.
After ordered locking and revalidation, recovery, existing sweepers and
publication checks passed **65 tests**. The two NOWAIT probes establish real
PostgreSQL document and job locks; rollback restores both state transitions.

All **16 GOO-358** and **15 GOO-359** logical guard-removal checks failed their
named regression and passed after restoration. These use runtime-only
recompilation with live module globals; no mutant was committed. The first
copied-globals harness attempt, the T4-16 assertion-only classification and
an interrupted run without the disposable broker are not counted as proof.
Exact guards, selectors and failure observations are in
[the mutation receipt](ingestion-lifecycle-mutation-checks.md).

## Real worker coverage

The new integration file executes the canonical Celery task against real
PostgreSQL and Redis, with deterministic barriers and a Linux fork/solo worker.
It uses actual upload, cancellation, deletion, reprocessing, status and search
routers and real JWT verification. Authentication/database dependencies resolve
synthetic users in the disposable database; only external provider transports
(Neo4j, DO Knowledge Base and Spaces) are simulated. Parsing, spaCy extraction,
lifecycle checks, database writes and full-text search run normally.

The nine worker cases cover:

1. Text upload to completed status and authenticated search; foreign-account
   status is denied, foreign search excludes the document, anonymous access is
   denied, and identical bytes in another organization create its own document.
2. Duplicate delivery after completion: no extra live entities or satellite data.
3. Document cancellation before first delivery: no processing, quota released once.
4. Cancellation while a graph write is held: late data is compensated.
5. Cancellation while a DO KB write is held: late data is compensated.
6. Producer resumes only after an accepted delivery finishes: completed state survives.
7. The same interleaving with lost publication acknowledgement: completed state survives.
8. Graph cleanup outage during ordinary deletion, followed by actual reconciler recovery.
9. Worker process loss after entity persistence: stale sweep fails the job/document
   together; explicit old-task redelivery is a terminal no-op; user-requested
   reprocessing creates a new successful attempt without duplicate live entities.

## Commands and outcomes

Use installed backend test dependencies and the repository-pinned
`en_core_web_sm` 3.8.0 model. Missing PostgreSQL, Redis, Linux fork support or
the model makes service-dependent checks **NOT RUN**; a skip is not runtime proof.
Each database test owns a unique schema. Each worker test owns a queue and
Redis key prefix; teardown stops only its workers and removes only that prefix.

```bash
export ORCHESTRATION_TEST_DATABASE_URL='<disposable-postgres-url>'
export INGESTION_TEST_DATABASE_URL="$ORCHESTRATION_TEST_DATABASE_URL"
export INGESTION_TEST_REDIS_URL='<disposable-redis-url>'
export REDIS_URL="$INGESTION_TEST_REDIS_URL"
export PYTHONPATH=backend
pytest -q -o addopts= -o log_cli=false \
  backend/tests/unit/tasks/test_ingestion_stage_guard_postgres.py \
  backend/tests/integration/test_document_deletion_postgres.py \
  backend/tests/tasks/test_processing_tasks_do_kb.py \
  backend/tests/unit/tasks/test_reconcile_tasks.py \
  backend/tests/api/documents/test_delete_document_neo4j_cleanup.py \
  backend/tests/unit/services/test_delete_file_ordering.py \
  backend/tests/unit/tasks/test_stuck_processing_recovery.py \
  backend/tests/unit/tasks/test_sweepers.py \
  backend/tests/integration/test_processing_publication_postgres.py \
  backend/tests/integration/test_ingestion_lifecycle_postgres.py
```

Result: **167 passed, 5 warnings, 103.41 seconds**; no skips in this service-dependent
suite. Local runtime: Python 3.12.3, PostgreSQL 16.15, Redis 7, Celery 5.6.3.
The plan's focused pair is the last two files above.

In the ordinary unit-test environment (do not reuse the worker broker for
unit-test rate-limit buckets):

```bash
ruff check backend/src
pytest -q -o addopts= -o log_cli=false \
  backend/tests/unit/architecture backend/tests/unit/api \
  backend/tests/unit/services/threads backend/tests/api/threads
python scripts/ci/generate_openapi.py --check
bash scripts/ci/run_local_ci.sh --skip-tests \
  --base 2a45aa50ce0db947ed91bdbb03534704107e5553
```

- Required pytest matrix: **1319 passed, 72 skipped, 1 inherited failure**,
  36 warnings, 76.16 seconds. Failure:
  `test_rerun_boundary.py::test_execute_code_tool_unchanged`, which pins the
  Python 3.11 `ast.dump` hash while the local runtime is 3.12.
  `tools_impl.py` is byte-identical to baseline (SHA256
  `3f231d46c137c0e6e9bebbd668536ac414e0032870fcd7b30e5ea0745d5b3091`).
  Removing only Python 3.12's added empty `type_params` fields from the dump
  yields the pinned hash `d0885e64d108349f7bd81e5009c4b8efa7d26c0c37ac1b67888cbd551c6f766e`.
  No source or test pin was changed. CI uses Python 3.11 and must confirm this gate.
- Branch validation: **PASS for every gate run**: full-source Ruff; changed-file
  Ruff/Black/isort; mypy on both new test files; directory docs; OpenAPI drift;
  Alembic single-head/revision checks (111 revisions).
- Conditional generated TypeScript and migration delta/empty-database checks
  were **SKIPPED unchanged**, not claimed as tested. Focused pytest ran separately
  because branch validation was invoked with `--skip-tests`.

## Limits and decisions

No production fault injection, real provider-server semantics, deployed prefork
worker behavior, application-wide middleware or HTTP-provider authentication was
verified. Process-loss recovery uses explicit canonical redelivery after the
terminal sweep; Redis visibility-timeout redelivery timing is not tested.
The exercised file is text; PDF, audio, video and figure paths are outside this
verification. No chat/export isolation claim is made. No public API schema or
migration changed, and no automatic restart of a terminal failure was introduced.

The existing DO KB bridge commit-loss case and reconciler starvation under
repeated provider failures are retained for final review to judge against
this plan; they are not claimed repaired by this evidence.

Ruling: Preserve the existing GOO-358 PR via a forward merge of current develop, then add fixes and independent commits for Tasks 5/6 — avoids overwriting shared history or redoing merged Tasks 1–3 — cost if wrong: stacked PRs need coordinated base changes.
Ruling: Treat the three unresolved PR #1845 review comments as part of Task 4 validation — cascade=false and durable cleanup are required by the spec — cost if wrong: Task 4 scope grows to ordinary deletion and reconciliation.
Ruling: Store cascade intent in existing document_metadata, with pending graph cleanup committed before remote calls and legacy missing intent retaining previous cleanup semantics — avoids a schema migration and preserves cascade=false — cost if wrong: old non-cascade rows without recorded intent cannot be distinguished.
Ruling: Limit stage-guard fixtures to the five required tables — previous full metadata allocation exhausted disposable host storage, and unrelated tables are not part of these proofs — cost if wrong: cross-table fixture requirements would surface as test failures.
Ruling: Lock all candidate documents in sorted order before locking the full candidate/competing job batch — preserves the existing bulk-delete lock order and one transaction — cost if wrong: a sweep can hold up to 200 document locks until its commit.
Ruling: Put type-check caches in disposable RAM — the installed third-party type graph filled the nearly-full host disk; only this worktrees own generated cache was removed — cost if wrong: the RAM cache must be regenerated later.
Ruling: Load the repository-pinned spaCy model from a disposable RAM directory — the existing backend dependencies support it without installing into the nearly-full host environment — cost if wrong: other environments must provide the same model prerequisite.
Ruling: Exercise the actual document/file/search routers with real authentication in a minimal FastAPI app and a Linux fork/solo canonical worker — isolates lifecycle behavior from unrelated startup services and makes process-loss barriers deterministic — cost if wrong: application-wide middleware, deployed prefork behavior and real provider-server semantics remain outside this proof.
Ruling: Give post-delete graph cleanup its own AsyncSession bound to the caller database — releasing its read transaction otherwise expires response objects and discards caller pending writes — cost if wrong: each cleanup uses an additional short database session.
Ruling: Use a pipe for the killed-worker release barrier — killing a process inside multiprocessing.Event.wait corrupts its notification semaphore and hangs teardown — cost if wrong: the fixture supports one bounded release per blocked provider write.
Ruling: Keep the inherited Python 3.11 AST pin unchanged and report the required matrix failure under local Python 3.12 — tool source is byte-identical to merged develop and removing only 3.12 type_params fields reproduces the pinned 3.11 hash — cost if wrong: CI on its pinned 3.11 runtime must still establish the required green gate before merging.

# Ingestion lifecycle runner verification — 2026-10-04

Status: supplemental validation and local follow-up repairs; not a merge or
deployment claim. This record supplements, without rewriting, the
[2026-10-01 verification record](2026-10-01-ingestion-lifecycle-verification.md).

## Source and environment

- Existing GOO-359 PR: [#1851](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1851),
  stacked on GOO-358 [#1845](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1845).
- Existing GOO-360 PR: [#1852](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1852).
  Exact validation base: `8380b0d0478b773e399e97675a93096c0ee5b0b5`.
- Local terminal-sibling follow-up: `bb2500cf146d57f79ff532e7eca43e304d96967a`.
- Cancellation test preparation fix: blob
  `0fb4ecfc0f6fdf0fe96b0817dacbe5b4607cde5d` for
  `backend/tests/integration/test_document_deletion_postgres.py`.
- User authorized a disposable EKS dev test pod with scratch PostgreSQL/Redis,
  source checkout in a scratch directory, process-loss tests and exact-pod cleanup.
  No application secrets or live databases were used. Live worker inspection was
  limited to package/model availability.
- Reused backend image digest:
  `daf546b41a64abb9a7f58542e33e546f84f4dbadbb990b37a377d685773ce1cb`.
  Runtime: Linux, Python 3.11.16, spaCy 3.8.16, `en_core_web_sm` 3.8.0,
  SQLAlchemy 2.1.1, psycopg2 2.9.13, asyncpg 0.31.0; PostgreSQL 16 and Redis 7
  sidecars. No Python dependencies were installed.

## Results

| Check | Result |
|---|---|
| Exact PR-head publication and real-worker pair | **36 passed**, no skips, 81.15 seconds |
| Exact PR-head combined repair suite | **205 passed, 2 failed**, 139.36 seconds |
| New terminal-sibling regression before repair | **Failed**: sibling retry acquired its row while recovery owned the document |
| Two cancellation cases and sibling regression after repairs | **3 passed** |
| Combined repair suite plus sibling regression after repairs | **208 passed**, no skips, 139.71 seconds |
| Three isolated logical guard-removal checks | All named tests failed for their intended lock/ownership defects |
| Required backend matrix on local Python 3.11.13 | **1320 passed, 72 skipped**, 35.65 seconds |

Final `scripts/ci/run_local_ci.sh --base 8380b0d0478b773e399e97675a93096c0ee5b0b5 --skip-tests`
passed every executed gate: full/changed Ruff, changed Black/isort, new-file
MyPy, directory-doc lint, OpenAPI freshness and the 111-revision Alembic graph.
Generated API types and migration execution probes were skipped because their
inputs did not change. Required pytest checks ran separately.

Independent code review approved the three-file repair delta. CodeRabbit CLI
completed the four-file repair/evidence review with **zero findings**. After
all three isolated mutation processes exited and restored their methods, the
unmodified focused selectors passed again: **3 passed**, 3.93 seconds.

The worker suite contains **13 canonical Celery fork/solo cases**. Actual text
parsing, spaCy extraction, JWT authentication, PostgreSQL locking/commits,
Redis delivery and full-text search ran. Only provider transports were simulated.
Coverage includes upload/status/search, tenant denial, duplicate delivery,
cancellation, publication acknowledgement loss, cleanup outage/reconciliation,
worker SIGKILL followed by terminal recovery and explicit retry, provider
acceptance before SIGKILL, and overlapping reprocessing denial.

## Follow-up repairs

### Terminal sibling retry ownership

The published sweeper locked only active sibling jobs. An existing FAILED
sibling can retry through job-only update paths, so it could become active after
the sweeper's ownership decision. The new PostgreSQL NOWAIT regression
demonstrated this interleaving. Removing the active-status predicate from sibling
lock membership includes terminal rows without changing candidate eligibility
or the active-only ownership decision. Document-first/global-job-ID lock order
remains unchanged.

### SQLAlchemy 2.1 cancellation fixture setup

SQLAlchemy 2.1 autoflushes textual SQL, including `SET LOCAL`, as documented in
the [2.1 migration guide](https://docs.sqlalchemy.org/en/21/changelog/migration_21.html#session-autoflush-behavior-simplified-to-be-unconditional).
The two failing cases dirtied a cached job before configuring `lock_timeout`,
so test setup acquired the job lock before the service was invoked. Moving the
timeout statement before the dirty assignment preserves the test's original
purpose. No assertion or service guard was weakened.

Guard-removal verification restored the old sibling-status predicate and
separately removed cancellation `no_autoflush`/document-select autoflush
suppression. All three selectors failed at the intended ownership or NOWAIT
assertion. Methods were restored in each isolated process; source files were
unchanged by mutations.

```sh
PYTHONPATH=backend pytest -q \
  backend/tests/unit/tasks/test_stuck_recovery_terminal_retry.py \
  backend/tests/integration/test_document_deletion_postgres.py::test_cancellation_does_not_lock_job_while_waiting_for_document
```

## Evidence artifacts and limits

JUnit artifact SHA-256 values:

- Exact-head focused run: `69c3d58fd169decf126ca7169fa774b81158397b3c848f513bdafa5166a5819a`.
- Exact-head combined run: `a317e41c384d0c7a5eaac5d6cea4d711664ad307372c88fa6c362e2e08cc9161`.
- Follow-up combined run: `0d6089fc4611795bc0ad5ea4c9996d6027997e5439aab11eb626510ebc33a296`.

The local matrix's 72 skips require its separate PostgreSQL fixture; they are
not passing runtime evidence. The Linux runner lacked pytest-timeout, reported
as an unknown configuration-option warning; explicit worker/provider barriers
and bounded worker shutdown still ran. No plugin was installed to hide it.

Production provider servers, deployed prefork operation, application-wide
middleware, timed broker visibility redelivery, non-text formats and chat/export
isolation remain unverified. Existing stack merge order is #1845 → #1851 → #1852;
retarget and require fresh CI after each parent merges. Local follow-up commits
must be incorporated before claiming the additional regressions repaired in
those PRs.

Cleanup completed on 2026-10-04: the named runner's ownership annotation was
verified before exact-pod deletion, and a subsequent lookup found no pod.
Scratch PostgreSQL/Redis used only ephemeral pod storage. JUnit artifacts were
copied out before cleanup. No application pod or live database was modified.

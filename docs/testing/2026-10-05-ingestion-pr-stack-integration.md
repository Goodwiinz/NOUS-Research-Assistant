# Ingestion PR stack integration — 2026-10-05

Status: existing PR heads updated locally for publication and fresh CI. No PR
merge or deployed repair is claimed by this record. This supplements the
[October 4 runner verification](2026-10-04-ingestion-lifecycle-runner-verification.md).

## Integrated source

- `develop` base: `2ba9b0e973df21772d7883390f409d30bfa13914`.
- GOO-358 [#1845](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1845)
  is merged; preserve its later lease, cleanup and deduplication repairs.
- GOO-359 [#1851](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1851):
  terminal-sibling follow-up cherry-picked as `b11df513a`; forward `develop`
  merge `6f6b44f98`; import alignment `f5a9c5be5`.
- GOO-360 [#1852](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1852):
  updated parent merged as `9bf315e70`; October 4 fixture/evidence follow-up
  cherry-picked as `8f6f4f742`; public error contract repair `6a583637d`;
  import alignment `fff6e675f` and parent alignment `9df962ad1`.
- Runtime validation source: `6a583637d44d00fcd08f115e6e4c664fc50a981b`.
  Subsequent import-group alignment and this evidence document do not change
  lifecycle logic or test assertions.

Conflict resolution retained current `develop` versions of DO KB ingestion,
file cleanup, satellite state, reconciliation and their existing tests. Only
the recovery changes remain in the processing task diff against `develop`.
Both historical mutation receipts were retained, not rewritten.

## Public error contract reproduction

Four new rejection cases exercise the actual file route and document-helper
defaults with an async database double. Before repair, both file cases failed:
`Document not found` versus `File not found`, and `documents` versus `files`
in the authorization message. Both document cases already passed.

The helper now accepts internal keyword-only message defaults; the file route
passes its established 404/403 details. Organization/deletion predicates,
ownership/admin checks, the document lock and the active-ingestion guard are
unchanged. Rejections perform no job insertion, flush or commit.

```sh
PYTHONPATH=backend pytest -q \
  backend/tests/api/documents/test_files_endpoint_bugs.py \
  backend/tests/test_reprocess_document_no_orphan.py
```

Result after repair: **12 passed**.

## Fresh validation

User explicitly authorized renewed AWS login and reuse of the named disposable
test pod with scratch PostgreSQL/Redis, synthetic data, no application secrets
and exact-pod cleanup. The existing backend image digest was
`daf546b41a64abb9a7f58542e33e546f84f4dbadbb990b37a377d685773ce1cb`.
Linux Python 3.11.16, spaCy 3.8.16/model 3.8.0 and SQLAlchemy 2.1.1 were reused;
PostgreSQL 16 and Redis 7 ran as ephemeral sidecars. No dependency installation.

The combined suite used the 13-file command in the prior verification record,
plus `backend/tests/unit/tasks/test_stuck_recovery_terminal_retry.py`:

- **217 passed, no skips**, two warnings, **148.90 seconds**.
- Includes all **13 real canonical Celery fork/solo lifecycle cases**.
- Includes current merged GOO-358 lease/cleanup tests and terminal-sibling
  recovery protection, plus the restored file/document error contracts.
- JUnit SHA-256:
  `820ae4541e8790c228ff86a129a6e41becce9b44862b94f1bdafee1a18cdd3b7`.

Required local backend matrix on Python 3.11.13:

```sh
PYTHONPATH=backend pytest -q \
  backend/tests/unit/architecture backend/tests/unit/api \
  backend/tests/unit/services/threads backend/tests/api/threads
```

Result: **1339 passed, 72 skipped**, 25 warnings, **28.61 seconds**. Skips
require the matrix's separately configured PostgreSQL fixture; they are not
passing runtime evidence. Local PostgreSQL/Docker were unavailable, so the
service-dependent repair suite ran only in the authorized disposable runner.

## Review and remaining gates

Independent review approved merge resolution, follow-ups and error-contract
coverage. CodeRabbit completed the stack-only comparison against `develop`
with **zero findings**. Its first comparison against the old PR head exceeded
the file limit because it included unrelated merged `develop` changes; that
failed invocation is not review evidence.

Initial static checks exposed import grouping in the existing recovery and
worker test files. Both were aligned to the repository's isort configuration;
no assertions or implementation behavior were changed. Historical font-license
trailing spaces arrived from `develop` and were preserved; the actual PR diffs
pass whitespace checks.

Final static validation passed for both branches using
`scripts/ci/run_local_ci.sh --skip-tests`: #1851 against the pinned `develop`
base above, #1852 against its updated parent. Executed gates include full/changed
Ruff, changed Black/isort, added-file MyPy, directory-doc lint, OpenAPI freshness
and the 111-revision Alembic graph. Unchanged generated-type and migration
execution checks were skipped. Required pytest checks ran separately.

Runner ownership annotation was verified before exact-pod deletion. JUnit was
copied out first; a subsequent lookup confirmed absence. No application pod or
live database was modified.

#1851 should target `develop`. #1852 retains #1851 as its stacked base until
the parent merges; the manually dispatchable Test Pipeline can provide fresh
head checks in the meantime. After parent merge, retarget #1852 to `develop`,
refresh the branch and require another PR-base CI run before merge. Real provider
servers, deployed prefork operation, full middleware, timed broker redelivery
and non-text ingestion retain the prior record's verification limits.

# Backend backlog repair evidence — 2026-10-09

Status: local verification, no deployment or external issue updates. Base source:
`331ef5e84e9bba33234990570eacb9ddb3056150`. Runtime: Python 3.13.12 in the
existing `/tmp/nous-qa-fix-venv` environment. Follow the current
[backend](../engineering/backend.md) and [testing](../engineering/testing.md)
contracts when interpreting this dated record.

## Changes and regression evidence

- **GOO-412:** reject real DATA rotation with HTTP 409 after authorization,
  retaining dry-run inspection. The key manager independently rejects DATA
  rotation before replacing/deactivating a key. The regression attempts rotation,
  encrypts after rejection, initializes a fresh encryption manager, and decrypts
  the stored field. It failed before the fix because rotation was accepted.
  Durable versioned DATA key rotation remains unsupported.
- **GOO-413:** queue progress excludes observations consumed by prior resolutions
  unless they belong to the current resolution. The SQLite service regression
  resolves a report, reopens it, checks zero screened before fresh votes, checks
  each reviewer's progress after one fresh vote, and checks progress after the
  new cycle resolves. It failed before the fix on the stale own observation.
- **GOO-414:** graph cleanup catches session construction, context entry, service
  cleanup, and context exit failures after deletion has committed. Tests confirm
  the single-delete response remains successful, failures are logged, and the
  next bulk document is cleaned. Each failure injection failed before the fix.
  The existing committed retry marker remains available to the reconciler.
- **GOO-417 cleanup:** child shutdown, summary, excerpts, evidence flush, and
  engine disposal are independently attempted. Cleanup failures are logged;
  a primary scenario error is retained, and a cleanup error on an otherwise
  successful run still fails the run. Six injected cleanup failures previously
  replaced the primary error. Additional checks verify actual evidence files
  and disposal on the successful path and cleanup-only failure path.

## Executed verification

From `backend/`, using the environment's Python:

```sh
python -m pytest -q tests/unit/core/test_encryption_initialization.py \
  tests/security/test_encryption_org_scoping.py \
  tests/security/test_encryption_endpoint_guards.py \
  tests/unit/services/test_screening_service.py \
  tests/api/documents/test_document_delete_path.py \
  tests/api/documents/test_documents_bulk_delete_dedup.py \
  tests/unit/scripts/test_r8_live_proof_cleanup.py --tb=short
python -m pytest -q tests/unit/architecture tests/unit/api --tb=short
python -m pytest -q tests/unit/services/threads tests/api/threads --tb=short
```

- Focused regression and adjacent suites: **78 passed**.
- Architecture/API matrix: **1532 passed, 1 failed, 3 setup errors**. The failure
  was `test_rerun_boundary.py::test_execute_code_tool_unchanged`; its expected
  AST hash is documented as Python 3.11-specific. The untouched pinned source
  produces the same failing actual hash under Python 3.13. All three setup
  errors were in `test_cancel_upload_revokes_celery.py`, whose SQLite fixture
  creates encrypted user fields without initializing encryption. Rerunning that
  file after explicit test-key initialization produced **3 passed**.
- Threads matrix: **121 passed, 72 skipped**; skipped cases require the
  unavailable local PostgreSQL service. These are not successful DB proofs.

From the repository root:

```sh
scripts/ci/run_local_ci.sh --base 331ef5e84e9bba33234990570eacb9ddb3056150 --skip-tests
python -m mypy --python-version 3.12 --ignore-missing-imports \
  --follow-imports=silent backend/tests/unit/scripts/test_r8_live_proof_cleanup.py
```

All executed local CI gates passed: full-source Ruff, changed-file
Ruff/Black/isort, directory docs, feature map, OpenAPI snapshot, and Alembic
head/revision checks. The new proof test also passed explicit Ruff/Black/isort
checks while untracked. Generated frontend API types and migration replay were
conditional skips because those inputs did not change. Added-test MyPy passes
with the Python 3.12 target override; the repository's default Python 3.11 target
cannot parse the installed NumPy stub's newer syntax. No repository type-checking
configuration was changed to hide that environment incompatibility.

No live proof, external provider journey, or PostgreSQL screening concurrency
proof was executed. Frontend coverage was not measured in this backend slice;
GOO-417's separate coverage-floor work remains subject to a fresh measuring run.

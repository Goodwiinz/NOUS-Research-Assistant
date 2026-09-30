# Task 3 implementation report

## Status

DONE

## What changed

- Added strict, extra-forbid review schemas for screening, extraction, and final gates. Requests bind review kind, step, contract, and the canonical hash of the complete persisted stage envelope; item decisions are bounded and cannot carry client-supplied research content.
- Added an owner-scoped review service that resolves ownership through `ResearchRun -> ResearchBlueprint -> ResearchProject`, locks the owned run and step in one transaction, recomputes the canonical persisted-output hash, validates the current paused gate and exact item set, and appends an immutable review ledger row.
- Added idempotent canonical replay and conflict handling, including the `IntegrityError` race path. Approval marks only the matching pending descriptor approved, while decline remains durable and leaves the run paused.
- Added final approval checks that bind a passing persisted verification envelope and the exact verified report/artifact hash.
- Added deterministic downstream review overlays without modifying `ResearchStep.output`.
- Added owner-only pending and submission endpoints, content-free stale descriptors, bounded pending output, and stable review conflict codes. Registered the reviews router through the existing research-engine export and application include pattern.

## TDD evidence

### RED

Exact command run before production implementation:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_research_review_service.py backend/tests/unit/api/test_research_engine_reviews.py
```

Result:

```text
29 failed, 1 warning in 10.77s
```

Representative expected failures:

- `src.services.research_engine.review_service` did not exist.
- `src.api.research_engine.reviews` did not exist.
- `StageReviewRequest` and the review response schemas did not exist.
- Exact-set validation, owner scoping, persisted-output hash binding, replay/conflict behavior, verified-final checks, durable decline, and immutable overlays were therefore unavailable.

### GREEN

The same exact command after implementation:

```text
35 passed, 2 warnings in 10.63s
```

The warnings are pre-existing repository/framework warnings: the legacy Pydantic `schema_extra` key and Starlette's legacy HTTP 422 constant.

Adjacent research-engine regression command:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/schemas/test_research_engine_schemas.py backend/tests/unit/api/test_research_engine_endpoints.py backend/tests/unit/services/test_research_template_contracts.py
```

Result:

```text
128 passed, 2 warnings in 10.47s
```

## Mutation proof

- Temporarily changed the identical canonical decision guard in `_resolve_existing` from `if (` to `if False and (`.
- Ran:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_research_review_service.py::test_integrity_error_race_reloads_and_replays_the_original_row
```

- The mutation was caught as expected: `1 failed, 1 warning in 3.55s`, raising `ResearchReviewError: A different review decision already exists` instead of replaying the original row.
- Restored the file exactly. Its SHA-256 returned to `3fba48e9e669835ff6661b1ee4dc20ecf4a084dca6799eba8b8496c6fde5d266`.
- Re-ran the same test after restoration: `1 passed, 1 warning in 3.13s`.

## Quality checks

- Ruff on all seven Task 3 Python paths: `All checks passed!`
- Black check: `7 files would be left unchanged.`
- isort check: passed with no output.
- Added production/test MyPy gate: `Success: no issues found in 4 source files`.
- `git diff --check`: passed.

## Files changed

- `backend/src/services/research_engine/review_service.py`
- `backend/src/api/research_engine/reviews.py`
- `backend/src/schemas/research_engine.py`
- `backend/src/api/research_engine/__init__.py`
- `backend/src/main.py`
- `backend/tests/unit/services/test_research_review_service.py`
- `backend/tests/unit/api/test_research_engine_reviews.py`

## Self-review

- Confirmed authorization derives only from the project owner; the request organization is stored only as an audit snapshot. Missing resources and same-organization resources owned by another user both return 404.
- Confirmed hashing uses the shared `canonical_stage_output_hash` over the complete persisted envelope, including `contract_version`, and neither review submission nor overlays mutate persisted stage output.
- Confirmed descriptor, kind, index, stage, contract version, hash, and exact item identities are validated while the rows are locked in the owned transaction.
- Confirmed duplicate, omitted, unknown, injected, unresolved-on-approval, missing-reason, and overlong-reason inputs are rejected.
- Confirmed identical repeated or concurrent canonical decisions replay the original ledger row, while a different decision returns `review_decision_conflict` and history is never overwritten.
- Confirmed stale errors expose only the current bounded descriptor and no research content.
- Confirmed final approval requires the persisted verification and report bindings to prove a verified artifact.
- Confirmed route registration follows the existing research-engine import/include pattern.

## Concerns

None. The SDD report is retained at this path and ignored by the repository's report convention. Generated OpenAPI/client artifacts remain assigned to Task 6.

## Independent review fix round

### Findings addressed

- Validation failures now build one sanitized error list for both the 422 response and the warning log. The list retains stable `type`, generic `msg`, and a bounded `loc`; it drops raw `input`/`ctx`, replaces arbitrary location strings with `<field>`, and never reflects nested values or client-controlled field names.
- `review_kind` now selects the screening, extraction, or final decision model before nested validation. An extraction decline containing an exact all-`unresolved` item set is accepted and remains durably pending.
- Oversized valid pending outputs now return an explicit review projection that preserves the complete ordered source/part identity set and bounded safe fields. Its public byte ceiling is derived from all 200 supported candidates with two 512-character identities, JSON escaping, safe-field, item, and envelope allowances. Research text, evidence, reasons, and coverage remain excluded, and persisted output remains unchanged.
- Decline submissions use shared PostgreSQL row locks because they append an immutable ledger row and do not update the gate. Approval retains exclusive locking. This permits a real uniqueness race while preserving serialization against an approving writer; the existing `IntegrityError` rollback/reload/compare path resolves identical races as replay and different races as `review_decision_conflict`.

### TDD RED evidence

Initial boundary regressions were run before implementation:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/api/test_research_engine_reviews.py::test_review_validation_redacts_nested_input_from_response_and_logs backend/tests/unit/services/test_research_review_service.py::test_extraction_decline_accepts_an_all_unresolved_exact_set backend/tests/unit/services/test_research_review_service.py::test_pending_large_output_projects_every_ordered_identity_within_bound
```

Result: `3 failed, 2 warnings in 11.00s`.

Representative failures showed the injected secret in the response/log, routed an all-unresolved extraction payload through the screening model, and raised response validation for a valid persisted output above 32 KiB.

The strengthened client-controlled-location and maximum-cardinality projection checks were then run:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/api/test_research_engine_reviews.py::test_review_validation_redacts_nested_input_from_response_and_logs backend/tests/unit/services/test_research_review_service.py::test_pending_projection_covers_200_near_worst_case_identities
```

Result: `2 failed, 2 warnings in 10.11s`. The secret field name remained in `loc`, and the 200-record projection exceeded the old shared 32 KiB limit.

The real PostgreSQL race test was also red before the lock correction:

```text
ORCHESTRATION_TEST_DATABASE_URL=<postgresql-test-url> PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/integration/test_research_review_concurrency_postgres.py
```

Result: `2 failed, 1 warning in 9.32s`. Exclusive `FOR UPDATE` locking serialized the service calls, so only one session could reach the pre-insert barrier and no actual uniqueness race occurred.

### GREEN evidence

Final focused unit/API command:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_research_review_service.py backend/tests/unit/api/test_research_engine_reviews.py
```

Result: `39 passed, 3 warnings in 11.16s`.

Final real PostgreSQL command, using two independent sessions against the supplied ephemeral PostgreSQL target:

```text
ORCHESTRATION_TEST_DATABASE_URL=<postgresql-test-url> PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/integration/test_research_review_concurrency_postgres.py
```

Result: `2 passed, 1 warning in 2.73s`. Both sessions reached the pre-insert barrier. Identical decisions produced one fresh response plus one replay with the same row ID; differing decisions produced one fresh response plus stable `review_decision_conflict`. Each case left exactly one durable ledger row.

Adjacent schema/API/logging/contract command:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/schemas/test_research_engine_schemas.py backend/tests/unit/api/test_research_engine_endpoints.py backend/tests/unit/api/test_http_exception_log_level.py backend/tests/unit/services/test_research_template_contracts.py
```

Result: `140 passed, 3 warnings in 10.26s`.

Warnings are the existing Pydantic `schema_extra` warning and Starlette's legacy HTTP 422 constant warning.

### Fix-round quality checks

- Ruff over the six changed/new Python paths: `All checks passed!`
- Black check: `6 files would be left unchanged.`
- isort check: passed with no output.
- MyPy over the schema, service, unit/API tests, and PostgreSQL integration test: `Success: no issues found in 5 source files`.
- A diagnostic MyPy run including the repository's `backend/src/main.py` reported 19 existing errors in unchanged lines (fallback stubs, middleware functions, and exception-handler registration); it reported no issue in the new validation sanitizer.
- `git diff --check`: passed.

### Fix-round files changed

- `backend/src/main.py`
- `backend/src/schemas/research_engine.py`
- `backend/src/services/research_engine/review_service.py`
- `backend/tests/unit/api/test_research_engine_reviews.py`
- `backend/tests/unit/services/test_research_review_service.py`
- `backend/tests/integration/test_research_review_concurrency_postgres.py`

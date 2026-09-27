# Task 5 implementation report

## Status

DONE

## What changed

- Added deterministic Markdown, canonical JSON, and CSV artifacts through the existing renderer and exporter. The export stage makes no model calls, persists the exact report before the final review pause, and binds both the immediate verification-envelope hash and report hash.
- Added the owner-scoped `GET /runs/{run_id}/export?format=markdown|json|csv` route. Only completed runs export; inaccessible runs return 404, nonterminal runs return 409, filenames contain only the run UUID, and legacy/custom runs remain explicitly unverified.
- Added versioned JSON provenance for run, blueprint, template, confirmed scope, provider manifests, deduplication, original stage envelopes and hashes, approved review overlays, claims, evidence, verification, models, timestamps, limitations, and `verified|unverified|no_evidence` status.
- Preserved failed verification checks and the unverified banner in every downloadable format. No-evidence runs expose JSON and CSV audit artifacts while Markdown returns `brief_not_available_no_evidence`.
- Added a stable bounded CSV projection with source, bibliography, extracted data, evidence, evidence level, human decision, and reason fields. It keeps accepted and rejected extraction audit rows and neutralizes cells beginning with `=`, `+`, `-`, tab, or `@`.
- Added content-safe research observability with validated identifiers, categories, counts, durations, counters, and histograms for stage duration, provider outcomes, reviews, pauses, overrides, final status, and validation failures. Unsupported content fields fail before logging.
- Removed exception bodies from research stream logs while retaining the existing client event contract.

## Approved contract seam

The controller approved a minimal `contracts.py` change because Task 3 final-approval validation already requires `verification_output_hash` and `report_hash`, but the strict export-envelope allowlist rejected them. Task 5 now owns only those two additional export keys, rehydrates them, and tests that an undeclared content key remains rejected. No other stage ownership changed.

## TDD evidence

### Initial RED

The three required Task 5 test files were written first and run with:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_export_service.py backend/tests/unit/services/test_research_observability.py backend/tests/unit/api/test_research_engine_exports.py
```

Result: collection stopped with `3 errors` and no tests collected because `ExportFormat`, `ExportArtifact`, and `observability.py` did not exist.

### Review-gap RED/GREEN

- A focused four-test gap batch initially failed `2` cases for the missing explicit evidence CSV column and missing pause/override metrics; after implementation it passed `4/4`.
- Self-review added an end-to-end CSV audit test. RED was `1 failed` because a rejected extraction disappeared after overlay projection. CSV reconstruction was moved to the immutable pre-projection context; GREEN was `1 passed`.

### Final Task 5 GREEN

The exact required command passed after the final audit-row repair:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_export_service.py backend/tests/unit/services/test_research_observability.py backend/tests/unit/api/test_research_engine_exports.py
```

Result: `27 passed, 2 warnings in 9.45s`.

Report-rendering/template contracts:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_research_template_contracts.py
```

Result: `35 passed, 1 warning in 4.36s`.

## Adjacent regression evidence

Task 3/4 review, overlay, lifecycle, endpoint, template, and repair command:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_research_template_contracts.py backend/tests/unit/services/test_research_review_overlays.py backend/tests/unit/services/test_research_review_service.py backend/tests/unit/services/test_research_run_lifecycle.py backend/tests/unit/api/test_research_engine_reviews.py backend/tests/unit/services/test_task4_astra_repairs.py
```

Result: `93 passed, 3 warnings in 12.08s`.

Full stream regression after the compatibility repairs:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/api/test_research_engine_stream.py
```

Result: `31 passed, 1 warning in 5.24s`.

No Task 5 schema, migration, locking, or transaction behavior changed, so a new PostgreSQL run was not required; the adjacent Task 3/4 PostgreSQL evidence remains unchanged. Warnings are the repository's existing Pydantic `schema_extra` and Starlette HTTP 422 deprecation warnings.

## Quality checks

- Ruff on all 10 changed Python paths: `All checks passed!`.
- Repo-wide blocking Ruff check on `backend/src`: `All checks passed!`.
- Black: `10 files would be left unchanged.`
- isort: passed with no output.
- Added-file MyPy with repository gate flags: `Success: no issues found in 3 source files`.
- Modified legacy MyPy baseline comparison is unchanged: `runs.py` 42/42, `schemas/research_engine.py` 0/0, `contracts.py` 0/0, `export_service.py` 4/4, `report_rendering.py` 0/0, `step_executor.py` 2/2, and `test_export_service.py` 14/14. Zero Task 5 diagnostics were introduced.
- `git diff --check`: passed.

## Files changed

- `backend/src/services/research_engine/observability.py`
- `backend/src/services/research_engine/export_service.py`
- `backend/src/services/research_engine/report_rendering.py`
- `backend/src/services/research_engine/step_executor.py`
- `backend/src/services/research_engine/contracts.py` (controller-approved export hash seam)
- `backend/src/api/research_engine/runs.py`
- `backend/src/schemas/research_engine.py`
- `backend/tests/unit/services/test_export_service.py`
- `backend/tests/unit/services/test_research_observability.py`
- `backend/tests/unit/api/test_research_engine_exports.py`

## Self-review

- Confirmed the final-review target is the persisted export envelope; approval binds its verification output hash and canonical report hash without regenerating content.
- Confirmed downloads use only persisted typed envelopes, immutable original stage output, approved server-side overlays, review identities, and the run manifest.
- Confirmed overlay projection never mutates original step output and CSV retains rejected records for audit.
- Confirmed every unverified path keeps its banner and failed checks, while no-evidence never presents a reader-facing brief.
- Confirmed the ownership join is user-owner scoped rather than organization scoped and every non-owner result is indistinguishable from a missing run.
- Confirmed filenames contain no question text, CSV cells are bounded and formula-neutralized, and observability rejects content-bearing fields before emitting a log or metric.
- Confirmed legacy `export_json` behavior and the complete Task 3/4 review/stream surfaces remain green.

## Concerns

None for Task 5. Generated OpenAPI and client artifacts remain assigned to Task 6.

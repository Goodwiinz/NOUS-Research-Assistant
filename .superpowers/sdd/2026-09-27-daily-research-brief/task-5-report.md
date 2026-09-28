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

## Important-review repair — 2026-09-28

The first Task 5 review found seven Important gaps. The repair stayed within
Task 5 and the controller-approved adjacent lifecycle, engine, discovery,
review, route, and test surfaces.

1. Verified Daily Brief Markdown now returns the exact persisted
   `markdown`/`content` bytes bound by the export envelope and final approval.
   The regression covers both `# EXACT APPROVED ARTIFACT\n` and an empty
   approved artifact. Only legacy output that cannot claim approved Daily Brief
   status may use the renderer fallback.
2. A noncontiguous persisted step history now fails closed with the stable,
   content-free `export_reconstruction_failed` error. The modern and legacy
   JSON exporters keep the completed run unchanged and emit only identifiers
   and an `invalid_history` category.
3. A Daily Brief is verified only when its persisted manifest has
   `final_status=verified`, its confirmed scope has a matching trusted
   configuration hash, its v1 verification envelope passed, the export binds
   that verification hash and report hash, and an exact final-approval row
   binds the persisted export envelope. Missing any durable evidence produces
   an unverified artifact.
4. Review audit input is sorted deterministically. CSV screening and extraction
   decisions no longer share a last-write-wins map; extraction rows use only
   extraction-review decisions and reasons. Reversing review-row order produces
   identical bytes and preserves rejection reasons.
5. Provenance now hashes every actual pre-export typed envelope instead of a
   stale route snapshot, excludes the self-referential export hash, includes
   configured blueprint models, before/after deduplication counts, generation
   and review timestamps, and keeps `coverage.exhaustive=false`. Generated
   Markdown contains stable Limitations and Provenance appendices while the
   persisted approved reader report remains the final-review target.
6. Content-safe observability now has production call sites for run start and
   terminal outcomes, stage durations and validation failures, provider counts,
   deduplication, pauses, review outcomes and waits, extraction decisions,
   verification and override outcomes, exports, rehydration failures, and SSE
   failures. Replay and duplicate lifecycle transitions do not increment review,
   pause, override, or run-start counters twice. Emission failures are isolated
   from persisted lifecycle state.
7. Generic engine and route failures no longer serialize or log exception text.
   Engine and route regressions raise
   `RuntimeError('PRIVATE_RESEARCH_QUESTION')` and prove that value appears in
   neither SSE events nor logs. Stable error messages and categories retain only
   the structural fields needed by callers.

The previously approved `contracts.py` seam remains limited to the Task 5
export-owned `verification_output_hash` and `report_hash` keys. This repair did
not broaden it. Deduplication metadata is added to the persisted search manifest
through the existing search-output contract shape.

### Repair TDD evidence

- The first focused review batch selected one direct regression for each of the
  seven findings and failed `7/7` before implementation.
- Production observability and exception-redaction engine tests then failed
  `2/2`; production call-site capture initially reported `3 failed, 2 passed`.
- The post-resume provenance batch initially reported `1 failed, 1 passed`.
- Self-review RED cases separately caught empty approved Markdown fallback,
  legacy exporter reconstruction fallback, pre-persistence observability
  failure, malformed telemetry state corruption, ISO review timestamp handling,
  duplicate pause/review/override telemetry, and use of stale `outputs_hash`
  provenance. Each focused case passed after its narrow repair.

The exact final Task 5 command was:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_export_service.py backend/tests/unit/services/test_research_observability.py backend/tests/unit/api/test_research_engine_exports.py
```

Result: `36 passed, 2 warnings in 10.80s`.

The final Task 3/4/template adjacency command was:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q -o log_cli=false backend/tests/unit/services/test_research_template_contracts.py backend/tests/unit/services/test_research_review_overlays.py backend/tests/unit/services/test_research_review_service.py backend/tests/unit/services/test_research_run_lifecycle.py backend/tests/unit/api/test_research_engine_reviews.py backend/tests/unit/services/test_task4_astra_repairs.py
```

Result: `96 passed, 3 warnings in 12.64s`.

The final workflow and stream/security commands were:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_workflow_engine.py
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q -o log_cli=false backend/tests/unit/api/test_research_engine_stream.py
```

Results: `26 passed, 1 warning in 4.82s` and
`33 passed, 1 warning in 6.36s`.

The changed discovery and deterministic-rendering adjacency commands were:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q -o log_cli=false backend/tests/unit/services/test_paper_discovery.py
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q -o log_cli=false backend/tests/unit/services/test_determinism_golden.py
```

Results: `15 passed, 1 warning in 4.95s` and
`9 passed, 1 warning in 5.17s`.

### Repair quality gates

- `.venv/bin/ruff check` on all 13 changed Python paths: `All checks passed!`.
- `.venv/bin/ruff check backend/src`: `All checks passed!`; this also confirms
  the duplicate literal keys identified during review are gone.
- `.venv/bin/black --check` on all 13 changed Python paths:
  `13 files would be left unchanged`.
- `.venv/bin/isort --check-only` on all 13 changed Python paths: exit `0`.
- The exact MyPy gate was run on the same 13 paths with
  `.venv/bin/mypy --ignore-missing-imports --follow-imports=silent`. Baseline
  `43fdac2b5` and the final repair each report `144 errors in 5 files`; zero
  diagnostics were introduced.
- `git diff --check`: passed after the report and ledger update.

### Repair self-review

- Re-read every production observability call site and confirmed that its
  fields are identifiers, enum-like categories, counts, or durations. No
  question, criterion, abstract, quote, extraction, prompt, review note, or
  report body is accepted by the event schema.
- Inspected final lifecycle and review call sites for duplicate emissions.
  Replays and duplicate persistence transitions are guarded, and resumed SSE
  connections do not emit another run-start metric.
- Searched all changed production paths for raw exception serialization and
  found no remaining `str(exc)` or `repr(exc)` path.
- Inspected the final test diff for weakened assertions. Compatibility
  assertions changed only where the new content-free security contract
  intentionally replaces raw exception text.
- No Task 6 source, generated contract, client, UI, deployment, or remote state
  was changed.

## Remaining Important repair — 2026-09-28

The final scoped review identified two persisted-artifact trust gaps. Both now
fail closed without changing legacy readable-export behavior:

1. Reconstruction rejects every contiguous persisted step whose `output` is
   not a JSON object. Both the v1 and legacy exporters return the stable,
   content-free `export_reconstruction_failed` error, leave the run unchanged,
   and emit the existing aggregate reconstruction metrics. An explicit empty
   object remains a deliberately supported legacy no-op and has a direct
   contract regression.
2. Final approval and verified-download trust share one canonical Markdown
   predicate: the export must declare `format=markdown`, `markdown` and
   `content` must both be strings, and their values must be identical. The
   exact final-review hash still binds the whole export envelope. Missing,
   non-string, mismatched, or JSON-formatted content cannot be labeled trusted
   or silently served as alternate verified bytes. A legitimately persisted
   empty string remains byte-exact and trusted.

### Final repair TDD and verification

The focused regression command selected 18 direct cases. Before production
changes it reported `18 failed in 7.29s`: six malformed-history cases, six
download-trust cases, and six final-approval cases. After the shared contract
repair, the same batch reported `18 passed in 5.57s`. The explicit empty-object
legacy contract test then passed `1/1`.

The exact Task 5 command was rerun after all test changes:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_export_service.py backend/tests/unit/services/test_research_observability.py backend/tests/unit/api/test_research_engine_exports.py
```

Result: `49 passed, 2 warnings in 10.94s`.

Adjacent gates:

- Task 3/4/template review command: `102 passed, 3 warnings in 12.99s`.
- Workflow engine: `26 passed, 1 warning in 5.36s`.
- Stream/security: `33 passed, 1 warning in 5.88s`.
- Paper discovery: `15 passed, 1 warning in 4.99s`.
- Deterministic rendering: `9 passed, 1 warning in 4.77s`.

Static and self-review evidence:

- Ruff on all five changed Python paths and repo-wide `backend/src`: passed.
- Black: `5 files would be left unchanged`.
- isort: passed after normalizing the two new imports.
- MyPy on the five changed Python paths reports the same `18 errors in 2
  files` at base `9c120f85e` and in the repaired tree; zero diagnostics were
  introduced.
- `git diff --check`: passed.
- The test diff adds assertions only; the review fixture changed from a JSON
  export to the canonical Markdown envelope now required for successful final
  approval. Existing approval, exact-empty-byte, workflow, stream, review,
  template, discovery, and determinism behavior remains green.
- No Task 6 source or generated artifacts were changed.

## Parser-boundary repair — 2026-09-28

Canonical-looking persisted objects can no longer fall through the legacy
merge path. An object containing either `contract_version` or `stage_type` now
requires a string, known stage type and must pass the existing canonical
envelope validator. Missing markers, unknown or unhashable stage types, and
malformed envelope metadata therefore reach both exporters as the stable
`export_reconstruction_failed` response. Marker-free legacy objects retain
their existing behavior, while `{}` is an explicit no-op. The remaining
rehydration set-membership check was changed to equality-based tuple membership
so unhashable persisted values cannot leak a raw `TypeError` there.

The new 14-case v1/legacy adversarial matrix first reported `10 failed, 4
passed in 5.73s`; after the repair it reported `14 passed in 4.95s`. Final
verification was Task 5 `63 passed, 2 warnings in 10.45s`, Task 3/4/template
`102 passed, 3 warnings in 11.77s`, and stream/security `33 passed, 1 warning
in 5.50s`. Changed-path and repo-wide Ruff, Black, isort, and diff checks pass.
MyPy reports the same `14 errors in 1 file` for the two changed Python paths at
base `cacd70946` and in the repaired tree, with zero introduced diagnostics.
No Task 6 work was performed.

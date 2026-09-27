# Task 4 implementation report

## Status

DONE

## What changed

- Added `ResearchRunLifecycleService` as the transaction boundary for Task 4 step persistence, durable pause descriptors, exact resume authorization, stream claims, and admission-denial claim release. A completed step, canonical envelope hash, source rows, token accounting, stage hashes, gate state, and run status now commit together.
- Made step completion idempotent. Re-delivery of the same persisted envelope returns the original step without duplicating rows or token charges; a different envelope for the same step conflicts. Transaction failure rolls back every related write.
- Added content-free durable pause descriptors for review gates and failed verification. `RunResponse` reloads only pause reason, review kind, step index, and canonical output hash.
- Required the exact approved review row and current persisted step hash before review continuation. Screening or extraction approval with no accepted evidence completes as `no_evidence`; approved final review completes the run without regenerating export content.
- Required explicit `{continue_unverified:true, output_hash:<exact>}` consent for a failed verification envelope. The actor, time, step, and exact hash are persisted; the one-use authorization is consumed under lock, the failed-verification state survives downstream reconstruction, and final approval is skipped for the unverified path.
- Added atomic claim release after paid-work admission denial. A paused run regains its unconsumed authorization for retry, while a new run returns to a pristine pending state.
- Closed every pre-execution claim gap: organization and connector setup now happen before a claim, and admission or post-admission setup failures restore the exact pending or paused state, including the one-use exact-hash authorization.
- Kept `WorkflowEngine` as the sole stage orchestrator. It emits canonical version-1 output hashes and review pauses only after step completion; the stream persists the transition before serializing the SSE frame.
- Added immutable review overlays. Approved screening and extraction decisions project only downstream inputs, while original step output and hashes remain unchanged; cold rehydration produces the same context as uninterrupted execution.
- Added a versioned Daily Brief prompt projection containing only confirmed scope, selected safe capabilities, the immediate typed envelope, stable evidence IDs, target schema, and remaining budget. Daily Brief persistence retains only prompt-context version/hash; legacy prompt behavior remains compatible.
- Kept content-bearing source records in prompt batches only once and passed each stage's exact derived response schema into its prompt context, including `review_fields` extraction schemas.
- Added a server-owned zero-evidence terminal transition for empty screening or extraction. It records bounded audit state, creates no fabricated review identity, skips later model calls and final review, and is reconstructible after reload.
- Persisted bounded manual-pause descriptors before the first step, after a committed step, and on disconnect. Strong review or verification gates win simultaneous user pauses, and every pause SSE frame is content-free.

## TDD evidence

### Initial RED

The five required Task 4 test surfaces were created before production implementation and run with:

```text
ORCHESTRATION_TEST_DATABASE_URL=postgresql+asyncpg://codex_test:codex_test@127.0.0.1:32777/orchestration_test PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_research_run_lifecycle.py backend/tests/unit/services/test_research_review_overlays.py backend/tests/unit/services/test_research_prompt_context.py backend/tests/unit/api/test_research_engine_stream.py backend/tests/integration/test_research_engine_resume_postgres.py
```

Result: collection stopped with `2 errors` after discovering 31 tests because `run_lifecycle.py` and `prompt_context.py` did not exist.

### Admission-retry RED/GREEN

The late retry regression was added before its implementation:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_research_run_lifecycle.py -k release
```

RED result: `2 failed, 5 deselected, 1 warning in 2.87s`; both failures were the missing `release_stream_claim` transition.

GREEN result after implementation: `2 passed, 5 deselected, 1 warning in 2.34s`.

### Independent-review gap RED/GREEN

Focused tests were added for all six Important review findings before the fix. The first review-gap selection produced `6 failed, 27 passed`; the expanded selection produced nine expected failures covering pre-execution claim restoration, prompt-size bounds, exact stage schemas, content-free committed gate events, zero-evidence terminal behavior, and durable manual-pause reloads.

After implementation, the focused review-gap surface passed: `70 passed, 1 warning in 5.36s`.

### Final Task 4 GREEN

Final required command, including workflow and determinism regressions:

```text
ORCHESTRATION_TEST_DATABASE_URL=postgresql+asyncpg://codex_test:codex_test@127.0.0.1:32777/orchestration_test PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_research_run_lifecycle.py backend/tests/unit/services/test_research_review_overlays.py backend/tests/unit/services/test_research_prompt_context.py backend/tests/unit/api/test_research_engine_stream.py backend/tests/integration/test_research_engine_resume_postgres.py backend/tests/unit/services/test_workflow_engine.py backend/tests/unit/services/test_determinism_golden.py
```

Original Task 4 gate after the admission-retry regression: `70 passed, 1 warning in 6.30s`.

Final gate after closing the six independent-review findings: `88 passed, 1 warning in 6.02s`.

The PostgreSQL cases prove one winning stream claim, no paid execution by the losing stream, exact failed-verification continuation, durable paid parse/timeout accounting, and idempotent resume behavior.

## Adjacent regression evidence

Review, schema, endpoint, template-security, template-contract, and capability command:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_research_review_service.py backend/tests/unit/api/test_research_engine_reviews.py backend/tests/unit/schemas/test_research_engine_schemas.py backend/tests/unit/api/test_research_engine_endpoints.py backend/tests/unit/api/test_research_template_security.py backend/tests/unit/services/test_research_template_contracts.py backend/tests/unit/api/test_research_engine_capabilities.py
```

Result: `180 passed, 4 warnings in 12.30s`.

Real PostgreSQL review concurrency command:

```text
ORCHESTRATION_TEST_DATABASE_URL=postgresql+asyncpg://codex_test:codex_test@127.0.0.1:32777/orchestration_test PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/integration/test_research_review_concurrency_postgres.py
```

Result: `2 passed, 1 warning in 2.58s`.

Warnings are the repository's existing Pydantic `schema_extra` warning and Starlette's legacy HTTP 422 constant warning.

## Quality checks

- Ruff across all 11 review-fix Python paths: `All checks passed!`
- Black check: `11 files would be left unchanged.`
- isort check: passed with no output.
- Direct MyPy comparison across the six affected production modules reports the same 44 legacy SQLAlchemy/annotation errors at base `3f4647da` and at the review fix: zero new errors. The three errors initially introduced by the review fix were corrected before the final comparison.
- `git diff --check`: passed.

## Files changed

- `backend/src/services/research_engine/run_lifecycle.py`
- `backend/src/services/research_engine/prompt_context.py`
- `backend/src/services/research_engine/contracts.py`
- `backend/src/services/research_engine/review_service.py`
- `backend/src/services/research_engine/engine.py`
- `backend/src/services/research_engine/step_executor.py`
- `backend/src/api/research_engine/runs.py`
- `backend/src/schemas/research_engine.py`
- `backend/tests/unit/services/test_research_run_lifecycle.py`
- `backend/tests/unit/services/test_research_review_overlays.py`
- `backend/tests/unit/services/test_research_prompt_context.py`
- `backend/tests/unit/api/test_research_engine_stream.py`
- `backend/tests/integration/test_research_engine_resume_postgres.py`

## Self-review

- Confirmed the canonical hash is recomputed from the complete persisted version-1 envelope at step persistence and again before an approved-review resume.
- Confirmed all gate conflict responses and reload descriptors are content-free.
- Confirmed step/source/token/manifest/status writes share one transaction and duplicate delivery cannot double-charge.
- Confirmed SSE frames follow their database commits, including review and verification pauses.
- Confirmed a stream cannot consume an authorization twice and a 429 admission denial restores the exact claim state atomically.
- Confirmed missing-organization, connector-construction, and admission exceptions cannot strand a pending or exact-hash paused run in `running` or trigger paid execution.
- Confirmed no-evidence and approved-final outcomes complete directly without later model calls or export regeneration.
- Confirmed the unverified path retains failed checks, records exact consent, skips final review, and completes with `final_status=unverified`.
- Confirmed prompt payloads exclude credentials, internal URLs, actor metadata, unrelated prior outputs, and review notes; persisted prompt metadata contains version/hash only.
- Confirmed the maximum normal 25-source Daily Brief input remains batchable under `MAX_PROMPT_BYTES`, with source content carried once per batch rather than duplicated in fixed context.
- Confirmed manual pauses survive reload with a bounded descriptor, ordinary resume clears stale user-pause state, and a committed review or verification gate takes precedence.
- Confirmed legacy custom workflow prompts and deterministic golden behavior remain green.

## Concerns

None for Task 4. Generated OpenAPI/client artifacts remain assigned to Task 6 by the implementation plan.

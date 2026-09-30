# Task 6 implementation report

## Status

DONE

## What changed

- Regenerated the checked-in OpenAPI document and TypeScript declarations from the backend application. Neither generated artifact was edited by hand.
- Replaced handwritten research-engine wire models with aliases to generated `components["schemas"]` types. The service now exposes typed template-detail, capability, run-start, pending-review, review-submit, run-resume, and export-URL operations.
- Added the Daily Research Brief setup form with the exact bounded-review warning, structured scope fields, a required confirmation, and server-contract bounds of one to four sources and one to fifty results per source.
- Made source choices come from the shared capability query, retaining only the first occurrence of each canonical available Daily Brief provider.
- Made template selection fetch and cache full template detail before applying it, persist `template_source`, and clear that source plus scope confirmation when the user changes the step topology.
- Restricted the step editor to the six backend step types: `search`, `screen`, `extract`, `synthesize`, `verify`, and `export`.
- Kept TanStack Query as the single client cache owner for template and capability data, with stable accessible loading, error, and retry states.

## TDD evidence

### Initial RED

The OpenAPI contract test was added first and run with:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/api/test_research_engine_openapi.py
```

Result: `2 failed`. The checked-in contract did not contain the capabilities path or the qualified research export enum.

The three frontend Task 6 test files were then run before implementation. The plan's literal `pnpm --dir frontend vitest run ...` form is not accepted by pnpm 10 (`ERR_PNPM_RECURSIVE_EXEC_FIRST_FAIL`, command `frontend` not found), so the same Vitest invocation was executed through `pnpm --dir frontend exec vitest run ...`.

Result: `10 failed, 14 passed` across three files. The missing behaviors included typed service methods, detail-before-apply, persisted template source, the scope gate, capability filtering and bounds, and the six-type vocabulary.

### Generated contracts

```text
PYTHONPATH=backend .venv/bin/python scripts/ci/generate_openapi.py
corepack pnpm@10.18.2 --dir frontend generate:api-types
```

Both generators completed successfully. After staging the generated outputs, `check:api-types` regenerated them again and found no drift.

### Final Task 6 GREEN

Backend OpenAPI contract:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/api/test_research_engine_openapi.py
```

Result: `2 passed in 2.46s`.

Frontend service and setup/editor contracts:

```text
corepack pnpm@10.18.2 --dir frontend exec vitest run src/services/__tests__/researchEngineService.test.ts src/components/research-engine/__tests__/DailyResearchBriefSetup.test.tsx src/components/research-engine/__tests__/BlueprintEditor.test.tsx
```

Result after the review fixes: `3 passed` files, `29 passed` tests.

The final setup case initially exposed a real duplicate-capability ordering bug: a `Map` retained the last label for a canonical provider ID. The implementation now retains the first canonical capability; the original assertions remain unchanged and `DailyResearchBriefSetup.test.tsx` passes `3/3`.

## Adjacent regression evidence

- Adjacent research-engine component and service tests after the review fixes: `4 passed` files, `32 passed` tests.
- Adjacent backend endpoint, template, schema, review, and export tests: `121 passed, 5 warnings in 12.00s`.
- Full frontend suite: `311 passed` files, `2325 passed` tests in `125.49s`.

## Quality checks

- Frontend `type-check`: passed.
- Frontend `check:api-types`: passed; generated declarations are reproducible from the generated OpenAPI file.
- ESLint on every changed TypeScript/TSX path: passed with no diagnostics.
- Prettier on every changed TypeScript/TSX path: passed.
- Ruff, Black, and isort on the added backend contract test: passed.
- The local CI wrapper passed full-tree Ruff, directory docs, changed-file
  Ruff/Black/isort/MyPy, OpenAPI drift, generated API types, Alembic, 161 NOUS
  contract tests, frontend type-check, the frontend lint-debt ratchet, and the
  tsconfig exclusion ratchet. Its repo-wide backend pytest phase reported the
  established unrelated baseline surface: `30 failed, 6264 passed, 80 skipped,
  518 deselected, 3 xpassed, 122 warnings in 218.59s`. None of the failures is
  in a Task 6 changed path; the output includes local PostgreSQL authentication
  failures and refused Redis connections. The standalone full frontend suite
  remains green at `2325/2325`.
- `git diff --check`: passed.
- The frontend commands emit the repository engine warning because this worker has Node `22.22.0` while `package.json` requests Node `24.x`; all commands completed successfully under the available runtime.

## Review fix: stale persisted run target

Review found that a persisted Daily Brief kept its old blueprint ID after a
topology edit. Clearing `template_source` also removed the scope-confirmation
guard, so the edited draft could start the old persisted blueprint before the
custom topology was saved.

The combined editor regression loads a persisted Daily Brief, confirms its
scope, edits the topology, exercises a rejected save, retries successfully,
and then starts the newly persisted custom blueprint. Before the fix the
focused file was RED at `1 failed, 6 passed`: Start remained enabled directly
after `Add step`. After the fix it passes `7/7`.

`BlueprintEditor` now tracks unsaved topology independently from
`template_source`. Every step/topology change marks the draft dirty, clears the
Daily Brief semantics, disables Start, and is also rejected by the start
handler. A failed save preserves that state. Only a successful save binds the
returned blueprint and clears the dirty guard, after which the new custom
blueprint starts without a Daily Brief scope payload.

## Review fix: save-response ordering

A second review found that an in-flight successful save could still clear the
dirty guard after the user made a newer topology edit. The deferred-promise
regression saves a seven-step custom draft, adds an eighth step while that
request is pending, and then resolves the older response. Before the fix the
editor file was RED at `1 failed, 7 passed`: Start became enabled for the stale
saved response. After the fix it passes `8/8`.

Topology changes now advance a monotonic draft revision. Save captures that
revision before sending its request and accepts the returned blueprint as the
displayed and runnable target only when the revision still matches. A response
for an older topology is discarded for current-draft purposes, leaving the
newer draft dirty and Start disabled. The existing rejected-save case and the
unchanged successful-save case remain green.

## Files changed

- `backend/openapi.json` (generated)
- `backend/tests/unit/api/test_research_engine_openapi.py`
- `frontend/src/types/generated/api.d.ts` (generated)
- `frontend/src/services/researchEngineService.ts`
- `frontend/src/services/__tests__/researchEngineService.test.ts`
- `frontend/src/components/research-engine/TemplateSelector.tsx`
- `frontend/src/components/research-engine/SourceSelector.tsx`
- `frontend/src/components/research-engine/DailyResearchBriefSetup.tsx`
- `frontend/src/components/research-engine/StepCard.tsx`
- `frontend/src/components/research-engine/BlueprintEditor.tsx`
- `frontend/src/components/research-engine/__tests__/DailyResearchBriefSetup.test.tsx`
- `frontend/src/components/research-engine/__tests__/BlueprintEditor.test.tsx`

## Self-review

- Confirmed every public request and response type in the research-engine service is either a generated schema alias or a local presentation adapter for the legacy untyped template-summary endpoint.
- Confirmed selecting a template cannot apply summary data: the editor opens only after the detail request resolves.
- Confirmed every topology edit clears both `template_source` and the Daily Brief scope confirmation, advances the draft revision, and blocks old persisted or stale saved run targets until the current revision is successfully saved.
- Confirmed source options exclude ineligible and unavailable capabilities, de-duplicate canonical IDs deterministically, and prevent zero or more than four selections.
- Confirmed any scope edit clears confirmation and the Daily Brief run action stays disabled until the exact current scope is confirmed.
- Confirmed error messages do not disclose thrown exception text and every changed interactive control has a stable accessible name or status role.

## Concerns

None for Task 6. Task 7 run hydration, reviews, and result rendering were not changed.

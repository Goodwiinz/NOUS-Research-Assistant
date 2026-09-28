# Daily Research Brief verification record

Date: 2026-09-28

Branch: `codex/daily-research-brief-20260927`

Task 8 base: `68cc641938f43b99fa8d00e18822ccf2c62106e8`

Disposition: **CERTIFICATION COMPLETE — NOT READY TO ENABLE**

This record covers the local Daily Research Brief certification work. The
candidate commit contains this file, so embedding that commit's own SHA here
would create a self-referential hash. The exact committed candidate SHA is
captured after the commit by `git rev-parse HEAD` in the local
`task-8-candidate-sha.log` evidence and in the handoff.

The feature remains disabled. The live-model quality gate and authenticated
Safety scan are blocked, dependency audits failed, and required remote and
release actions were not run. A local passing test does not override any of
those release gates.

## Gate ledger

`PASS` means the named local gate ran and met its assertions. `FAILED` means it
ran and returned a failing result. `BLOCKED` means a required credential or
authenticated service was unavailable. `NOT RUN` means the action was outside
the authorized local scope.

| Gate | Status | Evidence |
| --- | --- | --- |
| PostgreSQL lifecycle integration | PASS | 5 tests passed against disposable PostgreSQL; persistence, exact hashes, review rows, reload/resume, concurrency, outcomes, and ownership paths were exercised. |
| Browser/API/PostgreSQL lifecycle | PASS | 10 Playwright scenarios passed through the real browser-to-API-to-database boundary. |
| Browser 200-row review rendering | PASS | Frozen p95 threshold `999.24 ms`; final candidate run observed p50 `565.1 ms`, p95 `647.4 ms` over 11 cold reloads. |
| Frozen backend performance | PASS | Three consecutive full invocations passed every unchanged numeric threshold after the measured regressions were repaired. |
| Focused backend regression suite | PASS | 81 tests passed. |
| Focused frontend regression suite | PASS | 5 files and 61 tests passed. |
| Frontend type-check and full Vitest | PASS | Node `24.21.0`, pnpm `10.18.2`; 315 files and 2,368 tests passed. |
| Full `frontend validate` command | FAILED | Full-tree lint stopped the command with 2,074 existing problems: 113 errors and 1,961 warnings. The command did not reach its later type/test steps. |
| OpenAPI snapshot and generated TypeScript | PASS | OpenAPI check passed; API types regenerated with `openapi-typescript 7.13.0` and produced no diff. |
| Alembic upgrade/downgrade/upgrade | PASS | `agent_ops_20260925 -> daily_brief_reviews_20260927 -> agent_ops_20260925 -> daily_brief_reviews_20260927` passed on disposable PostgreSQL and cleaned up. |
| Bandit source scan | PASS | Bandit `1.9.4` returned no in-scope issues under the repository baseline and configured severity/confidence filters. |
| Candidate secret scan | PASS | Gitleaks `8.30.1` found no leaks in the candidate-only snapshot. |
| JavaScript dependency audit | FAILED | pnpm audit returned 21 advisories: 2 critical, 9 high, 8 moderate, and 2 low. |
| Python dependency audit | FAILED | pip-audit returned 5 vulnerabilities in 4 resolved packages. |
| Authenticated Safety scan | BLOCKED | Safety `3.8.1`; `SAFETY_API_KEY` was absent, so the authenticated database scan could not run. |
| Anonymous legacy Safety check | FAILED | 1 active `gunicorn 22.0.0` finding; 129 findings were ignored by Safety's automatic unpinned-requirement policy. |
| Current configured-model evaluation | BLOCKED | `ANTHROPIC_API_KEY` was absent; 9 prerequisite regressions passed and the live eval skipped with the explicit blocked reason. No citation baseline or candidate score was frozen. |
| Final local CI script | FAILED | The final-tree rerun retained 29 repository-baseline backend failures and failed broad changed-file/debt gates inherited from the branch window. All 9 eval prerequisites and the Task 8 pause regressions passed in the full collection; no remaining failure names a Task 8 path. |
| Candidate-SHA remote branch-rule checks | NOT RUN | Awaiting explicit repository-write authorization; no push was made solely to create checks. |
| Production configuration and rollback drill | NOT RUN | No production environment or release controls were changed or inspected. |
| Deploy and template/feature enablement | NOT RUN | Awaiting release authorization. |

## Test environment

The local evidence was collected on Linux `6.8.0-124-generic` x86_64 with
Python `3.12.3`, pytest `9.1.1`, Node `24.21.0`, pnpm `10.18.2`, and Docker
`29.1.3`. Database commands used a disposable PostgreSQL instance through
`$ORCHESTRATION_TEST_DATABASE_URL`; this document and the committed tests do
not contain its password.

## PostgreSQL lifecycle evidence

Command:

```bash
PYTHONPATH=backend \
ORCHESTRATION_TEST_DATABASE_URL="$ORCHESTRATION_TEST_DATABASE_URL" \
.venv/bin/python -m pytest -c backend/pytest.ini -q \
  backend/tests/integration/test_daily_research_brief_postgres.py
```

Result: `5 passed, 1 warning in 4.93s`.

The suite proves these server-owned behaviors:

- the six-stage happy path pauses at screening, extraction, and final review,
  survives reloads at every gate, resumes exactly once, and reconstructs exact
  persisted hashes, evidence links, review identifiers, overlays, and export
  artifacts;
- a partial provider failure retains successful evidence and complete coverage,
  while all-provider failure and no-evidence produce their distinct durable
  terminal outcomes;
- duplicate sources, missing abstracts, and metadata-only sources retain their
  canonical evidence-level semantics;
- verification failure cannot reach final approval, and an unverified
  continuation requires the exact failed verification-output hash;
- reconnect, idempotent replay, stale and conflicting review attempts,
  simultaneous resumes, and duplicate submissions do not create duplicate
  stage or review rows; and
- same-organization and cross-tenant non-owners receive the same
  non-enumerating denial.

## Browser boundary evidence

The browser suite uses a real Next application, the Uvicorn test API, and the
disposable PostgreSQL database. Controlled connector/model fixtures stop at the
external provider seam; no mock replaces the browser, HTTP API, lifecycle
service, or database boundary.

Server commands:

```bash
cd backend
PYTHONPATH=. \
ORCHESTRATION_TEST_DATABASE_URL="$ORCHESTRATION_TEST_DATABASE_URL" \
../.venv/bin/python -m uvicorn \
  tests.integration.test_daily_research_brief_postgres:create_e2e_app \
  --factory --host 127.0.0.1 --port 8765
```

```bash
BACKEND_URL=http://127.0.0.1:8765 \
NEXT_PUBLIC_API_BASE_URL=/api/v1 \
NEXT_PUBLIC_API_URL= \
NEXT_PUBLIC_AUTH_COOKIE_NAME=daily-brief-e2e-auth \
NEXT_PUBLIC_SUPABASE_URL=http://localhost:3000/api/v1/auth-fixture \
SUPABASE_SERVER_URL=http://127.0.0.1:8765/api/v1/auth-fixture \
NEXT_PUBLIC_SUPABASE_ANON_KEY=e2e-anon-key \
corepack pnpm@10.18.2 --dir frontend run dev:offline
```

```bash
ORCHESTRATION_TEST_DATABASE_URL="$ORCHESTRATION_TEST_DATABASE_URL" \
corepack pnpm@10.18.2 --dir frontend exec playwright test \
  e2e/research-engine/daily-research-brief.spec.ts --project=chromium
```

Result: `10 passed (2.7m)`. The scenarios cover all three reloadable review
gates and exact downloads, partial provider failure, no-evidence output,
verification failure, exact-hash override and unverified labels, reconnect
without duplicate screening, the 200-row review bound, stale review refresh,
immutable duplicate review replay, and owner-only denials.

The review-page baseline was p50 `656.1 ms` and p95 `832.7 ms` from 11 cold
reloads. The frozen ceiling is baseline p95 plus 20%, or `999.24 ms`. A focused
candidate rerun observed p50 `648.4 ms` and p95 `793.7 ms`; an earlier valid
combined run observed p50 `649.9 ms` and p95 `832.5 ms`. After the final
frontend changes, the final combined ten-scenario run observed p50 `565.1 ms`
and p95 `647.4 ms`. A preceding attempt redirected to login because the local
Next server lacked the complete offline-auth environment; that run had one
failure and nine tests not run, is preserved as environment-setup evidence,
and is excluded from feature results. Earlier runs that lost their host channel
are likewise excluded and are not reported as passes.

## Current-model evaluation

Command:

```bash
PYTHONPATH=backend \
DAILY_BRIEF_EVAL_MODE=candidate \
DAILY_BRIEF_CITATION_THRESHOLD="$DAILY_BRIEF_CITATION_THRESHOLD" \
.venv/bin/python -m pytest -c backend/pytest.ini -q \
  backend/tests/eval/test_daily_research_brief_eval.py
```

Result: `BLOCKED`; the focused prerequisite suite returned
`9 passed, 1 skipped, 1 warning in 3.35s`. The live test skipped because
`ANTHROPIC_API_KEY` was absent. Mixed integration collection independently
selected that test and returned `1 skipped, 14 deselected, 1 warning`.

The prerequisite matrix proves that an absent key, the integration fixture
key, and candidate mode without a frozen citation threshold all stop before a
model call. Baseline mode without a threshold and candidate mode with a valid
threshold proceed to the call boundary. Supplied malformed, negative, and
greater-than-one thresholds fail before a call. Missing prerequisites are
therefore reported as `BLOCKED`; invalid supplied configuration remains a test
failure.

The eval calls the configured `claude-sonnet-4-6` model through all six stages
over fixed external evidence. It requires at least one claim and citation, zero
unsupported material claims, exact coverage labels, grounded quotes, exact
screen/extract coverage, the complete six-stage output sequence, verified
claim-set equality, and exact verification/report hash reconstruction. These
checks form `live_gate_output_readiness`; they do not claim persistence or
review-gate compliance from YAML strings. PostgreSQL and browser tests prove
the separate lifecycle gates.

Citation correctness must first be measured with
`DAILY_BRIEF_EVAL_MODE=baseline`; the resulting value then becomes the frozen
candidate threshold. Because no live call ran, neither a citation baseline nor
a candidate result exists. Template-only output cannot satisfy this gate.

## Frozen performance evidence

Command used for each full invocation:

```bash
PYTHONPATH=backend \
ORCHESTRATION_TEST_DATABASE_URL="$ORCHESTRATION_TEST_DATABASE_URL" \
DAILY_BRIEF_PERF_CAPTURE=1 \
.venv/bin/python -m pytest -c backend/pytest.ini -q -s \
  backend/tests/performance/test_daily_research_brief_performance.py
```

The numeric thresholds were frozen from the pre-optimization baseline at 20%
headroom and were never loosened. The nearest-rank percentile function also
stayed unchanged. The max-stage sampling policy was corrected from 11 samples,
where nearest-rank p95 accidentally selected the maximum, to 40 measured
samples, where p95 is rank 38. A deterministic regression locks that policy.
Each max-stage test has one complete untimed warmup, leaves GC enabled, and
clears the controlled provider's request recording after each timed stage.

| Metric | Measured baseline | Frozen ceiling |
| --- | ---: | ---: |
| Six-stage p95 | `29.454 ms` | `35.345 ms` |
| Export p95 | `0.688 ms` | `0.826 ms` |
| Export payload | `15,294 B` | `18,353 B` |
| 1-provider search p95 | `6.577 ms` | `7.893 ms` |
| 2-provider search p95 | `11.284 ms` | `13.541 ms` |
| 4-provider search p95 | `31.620 ms` | `37.944 ms` |
| Max search p95 | `32.143 ms` | `38.572 ms` |
| Max screening p95 | `64.343 ms` | `77.212 ms` |
| Max extraction p95 | `708.040 ms` | `849.648 ms` |
| Max-stage payload | `1,152,543 B` | `1,383,052 B` |
| Max-stage traced peak | `3,624,356 B` | `4,349,228 B` |
| Review projection p95 | `7.735 ms` | `9.282 ms` |
| Review payload | `24,094 B` | `28,913 B` |
| Cold hydration p95 | `3.319 ms` | `3.983 ms` |
| Ten-run lifecycle soak p95 | `1,723.942 ms` | `2,068.731 ms` |

Three consecutive idle full-file invocations passed:

| Metric | Run 1 | Run 2 | Run 3 | Ceiling |
| --- | ---: | ---: | ---: | ---: |
| Six-stage p95 (ms) | 23.138 | 23.227 | 22.343 | 35.345 |
| Export p95 (ms) | 0.712 | 0.649 | 0.702 | 0.826 |
| 1/2/4-provider p95 (ms) | 3.697 / 7.210 / 16.581 | 4.432 / 7.618 / 17.713 | 3.970 / 6.983 / 14.791 | 7.893 / 13.541 / 37.944 |
| Max search/screen/extract p95 (ms) | 17.674 / 36.485 / 110.986 | 18.365 / 40.021 / 112.123 | 18.465 / 38.524 / 112.266 | 38.572 / 77.212 / 849.648 |
| Peak traced bytes | 3,115,916 | 3,115,149 | 3,112,398 | 4,349,228 |
| Review projection p95 (ms) | 4.760 | 6.729 | 5.744 | 9.282 |
| Cold hydration p95 (ms) | 2.906 | 2.841 | 3.631 | 3.983 |
| Soak lifecycle p95 (ms) | 1,187.009 | 1,364.823 | 1,299.151 | 2,068.731 |

Every run produced 200 candidate rows, 25 screen batches, 25 extraction
batches, the unchanged `1,152,543 B` max-stage payload, ten completed soak
lifecycles, zero duplicate stage rows, and 30 unique review rows.

Earlier failures are retained because a single favorable rerun did not prove
stability. The initial candidate failed max-search p95 at `46.135 ms` against
`38.572 ms`; its immediate rerun barely passed at `37.816 ms`. Later full runs
failed search at `48.231 ms`, hydration at `4.016 ms`, screening at
`191.853/190.960 ms`, and screening at `195.187 ms`. Profiling found fixture
request/executor reference cycles, a production bound-method handler cycle,
quadratic source/coverage scans, repeated schema compilation, quadratic usage
snapshot copying, discovery candidate scans for provably unique identifiers,
and a deep copy of the roughly 1 MB stage envelope used only as a prompt view.

The repairs keep public isolation and validation semantics intact: dispatch
uses class-level handler names; screen/extract offsets use indexed maps; extract
reuses one `Draft202012Validator`; contract injection makes a shallow top-level
copy; discovery skips scans only when no exact identity pair can merge and
otherwise runs the original full algorithm; accounting snapshots copy only the
list while retaining immutable metadata entries; and the private prompt path
uses a shallow top-level snapshot while the public envelope API still deep
copies. Regression tests cover input immutability, all six legacy routes,
weak-reference reclamation without forced GC, 200 unique and bridging/conflict
identity cases, multipart offsets/order, schema compile count and later invalid
records, stable accounting snapshots, and prompt-hash/isolation equivalence.

## Frontend and contract checks

Focused command:

```bash
corepack pnpm@10.18.2 --dir frontend exec vitest run --project unit \
  src/components/research-engine/__tests__/BlueprintEditor.test.tsx \
  src/components/research-engine/__tests__/ReviewPanel.test.tsx \
  src/components/research-engine/__tests__/RunResults.test.tsx \
  src/components/research-engine/__tests__/RunView.test.tsx \
  src/services/__tests__/researchEngineService.test.ts
```

Result: 5 files and 61 tests passed in `4.53s`.

The exact Node 24 full checks passed:

```bash
corepack pnpm@10.18.2 --dir frontend run type-check
corepack pnpm@10.18.2 --dir frontend test
```

Result: type-check passed; 315 files and 2,368 tests passed in `84.99s`.

The requested aggregate command failed during its first full-tree lint phase:

```bash
corepack pnpm@10.18.2 --dir frontend validate
```

Result: 113 errors and 1,961 warnings. That result is recorded as `FAILED`,
even though the exact type-check and test phases pass separately. The final
local CI run separately evaluates the repository's changed-file frontend debt
ratchet.

Contract commands:

```bash
.venv/bin/python scripts/ci/generate_openapi.py --check
corepack pnpm@10.18.2 --dir frontend run generate:api-types
git diff --exit-code -- backend/openapi.json \
  frontend/src/types/generated/api.d.ts
```

Result: OpenAPI was current and regenerated TypeScript had no drift.

## Final local CI

`$NODE24_BIN_DIR` was the directory containing the Node `24.21.0` executable.

Command:

```bash
PATH="$PWD/.venv/bin:$NODE24_BIN_DIR:$PATH" \
ORCHESTRATION_TEST_DATABASE_URL="$ORCHESTRATION_TEST_DATABASE_URL" \
PYTHON=.venv/bin/python \
bash scripts/ci/run_local_ci.sh --base origin/main --frontend
```

Result: `FAILED`. The final-tree backend aggregate returned `29 failed, 6,293
passed, 81 skipped, 523 deselected, 3 xpassed, 122 warnings in 198.66s`. The
script also
reported failures for `ruff (changed)`, `black (changed)`, `isort (changed)`,
`mypy (added)`, `pytest`, `frontend quality ratchet`, and `tsconfig exclusion
ratchet`. Its `origin/main` branch window includes the inherited Tasks 1–7
history (about 1,455 Python files), so those broad changed-file/debt gates do
not isolate Task 8; repo-wide Black would reformat 192 files. The advisory full
frontend lint in this run reproduced the 2,074-problem result above.

Two failures initially found in Task 8 paths were treated as candidate
failures and repaired: unavailable live-eval prerequisites now skip with an
explicit `BLOCKED` reason before any model call, and the Task 5 pause regression
now asserts the deliberately content-free pause descriptor rather than stale
context. The final full collection passed all 9 eval prerequisite cases,
skipped only the unavailable live call, and passed every Task 8 pause
regression. Separate focused evidence is `9 passed, 1 skipped` for the eval
file and `48 passed` for the ordered workflow/regression batch. None of the 29
remaining backend failures names a Task 8 file. Frontend type-check, OpenAPI,
generated types, and the Task 8 migration checks passed inside or alongside
the run. The overall local-CI gate remains `FAILED`; the passing Task 8 paths
do not change that classification.

## Migration evidence

The disposable PostgreSQL cycle started at the actual parent revision,
`agent_ops_20260925`, and ran:

```text
upgrade daily_brief_reviews_20260927
downgrade agent_ops_20260925
upgrade daily_brief_reviews_20260927
```

The cycle passed and cleaned its isolated schema. The migration is additive;
its downgrade drops the review indexes and table. Release ordering remains API
and model first, then frontend exposure. During an application rollback,
template selection must be disabled first and the additive review table should
remain until the normal migration rollback window.

## Security and dependency scans

Bandit and dependency outcomes are intentionally separate. A passing source
scan does not make a dependency audit green.

### Bandit — PASS

```bash
bandit -r backend/src -ll -ii -x tests \
  -b backend/.bandit-baseline.json
```

Bandit `1.9.4` scanned 184,380 lines and returned no in-scope issues. The
repository baseline was generated at `2026-07-12T06:23:59Z` and contains ten
medium findings (`B108` x3, `B104` x4, `B615` x2, and `B314` x1). Four checks
are specifically disabled in source. The run used no additional Task 8
suppression.

### Gitleaks — PASS

Gitleaks `8.30.1` scanned a candidate-only snapshot with its default rules plus
`.gitleaks.toml` and found no leaks. The configuration excludes repository
metadata/worktrees, installed dependencies and virtual environments,
coverage/result artifacts, documented fixture paths, the example tfvars file,
and deliberate eval secret-shaped fixtures. The final staged candidate was
rescanned after this report was added.

### pnpm audit — FAILED

```bash
corepack pnpm@10.18.2 audit --json
```

The npm advisory service evaluated 1,900 dependencies from the committed
`pnpm-lock.yaml`. No advisory was muted. It returned 21 advisories: 2 critical,
9 high, 8 moderate, and 2 low. Affected modules include `next`,
`@tiptap/core`, `extract-zip`, `fast-uri`, `js-yaml`, `sharp`, `hono`, `qs`,
`@vitest/mocker`/`vitest`, and `joi`. This blocks the dependency gate.

### pip-audit — FAILED

```bash
pip-audit -r backend/requirements.txt --format json
```

pip-audit `2.9.0` used its default PyPI vulnerability service and resolved a
fresh compatible environment from the partially unpinned requirements. No
vulnerability was ignored. It evaluated 301 dependencies and found:

| Package | Resolved version | Advisory | Fix |
| --- | --- | --- | --- |
| `torch` | 2.12.1 | `GHSA-rrmf-rvhw-rf47` | 2.13.0 |
| `setuptools` | 71.1.0 | `PYSEC-2025-49` | 78.1.1 |
| `setuptools` | 71.1.0 | `PYSEC-2026-3447` | 83.0.0 |
| `lxml` | 5.4.0 | `PYSEC-2026-87` | 6.1.0 |
| `ecdsa` | 0.19.2 | `PYSEC-2026-1325` | no published fix |

This also blocks the dependency gate.

### Safety — BLOCKED and FAILED

The authenticated Safety `3.8.1` scan is `BLOCKED`: `SAFETY_API_KEY` was not
available, and the noninteractive command terminated at its login prompt. It
must be rerun with authorized credentials before release.

The deprecated anonymous fallback check is recorded separately as `FAILED`.
It inspected 88 packages in `backend/requirements.txt`, found one active
`gunicorn 22.0.0` advisory (Safety ID `72809`), and automatically ignored 129
findings because Safety's default policy ignores unresolved unpinned
requirements. No explicit ignore flag or local policy file was supplied. This
fallback does not substitute for the blocked authenticated scan.

## Production controls, monitoring, and rollback

No dedicated Daily Research Brief runtime flag was found in this local source,
and the local template loader exposes the template. The design permits a
feature-control mechanism if one applies, but production configuration was not
inspected. Because this candidate was not deployed or enabled, the feature
remains disabled for this release decision. Production template-selection
control and its rollback behavior remain unproven and are release blockers.

Version 1 remains owner-only. The stored organization ID is audit context; it
does not authorize another user. Local PostgreSQL and browser evidence proves
same-organization and cross-tenant non-owner denials, but production policy
configuration was not inspected.

The application emits content-safe dimensions such as run ID, organization ID,
step index/type, duration, provider and outcome, returned/deduplicated counts,
pause/review kind, review wait, extraction decision/count, verification and
override outcome, export format/outcome/error kind, and terminal status. It
does not emit research questions, abstracts, quotes, decisions' free text, or
report bodies. Candidate release monitoring should implement alerts for:

- run and provider failure rate by outcome;
- p50/p95 stage duration by stage type, including a 200-row max-bound view;
- persisted rehydration and SSE reconnect/error rate;
- review wait, stale/conflict/duplicate review outcomes, and simultaneous
  resume conflicts;
- verification failure and exact-hash override rate; and
- export failure by format/error kind and terminal no-evidence/unverified rate.

These are proposed production queries over content-safe fields. No production
telemetry query or alert was run or changed during Task 8.

Local database rollback evidence is `PASS`. Application rollback, production
template disablement, branch-rule checks, deployment, enablement, and a live
rollback drill are `NOT RUN`. Required release sequence:

1. resolve dependency findings and run authenticated Safety;
2. run the live-model baseline and candidate evaluation with the same model;
3. obtain required remote checks for the exact candidate SHA;
4. verify production owner-only and template-selection controls;
5. deploy the backend/migration before any frontend exposure; and
6. enable only after an authorized release owner accepts every gate, with
   template disablement as the first rollback action.

## Local evidence artifacts

Detailed command output is retained locally under
`.superpowers/sdd/2026-09-27-daily-research-brief/`. That directory ignores
generated logs, so this committed record is the reviewable summary. Principal
artifacts are:

- `task-8-postgres-integration.log`
- `task-8-browser-max-bound-focused.log`
- `task-8-browser-full.log`, `task-8-browser-full-final-candidate.log`, and the
  excluded `task-8-browser-invalid-auth-environment.log`
- `task-8-current-model-eval.log`, `task-8-eval-focused-final.log`, and
  `task-8-eval-mixed-collection-final.log`
- `task-8-performance-candidate.log` and later failed stability logs
- `task-8-performance-prompt-view-stability-{1,2,3}.log`
- `task-8-all-optimizations-focused.log`
- `task-8-frontend-focused-final.log`, `task-8-frontend-vitest-final.log`, and
  `task-8-frontend-validate-final.log`
- `task-8-openapi-drift.log`, `task-8-types-generate.log`, and
  `task-8-alembic-roundtrip.log`
- `task-8-bandit.log`, `task-8-gitleaks.log`,
  `task-8-gitleaks-final.log`, `task-8-pnpm-audit.json`,
  `task-8-pip-audit.json`, `task-8-safety-authenticated.log`, and
  `task-8-safety.log`
- `task-8-local-ci-final-candidate-rerun.log`
- `task-8-candidate-sha.log`

The ship gate is closed until every `FAILED`, `BLOCKED`, and release-critical
`NOT RUN` item is cleared on one exact candidate SHA. No push, pull request,
deployment, production flag change, or template enablement was performed.

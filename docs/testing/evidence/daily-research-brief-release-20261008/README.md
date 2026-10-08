# Daily Research Brief release-evidence refresh, 2026-10-08

Date: 2026-10-08. First-round commands ran 05:40Z–06:10Z. Their tests and
source are byte-identical to commit `f0b7d4575`, the first commit on this
branch (the commit they ran on was amended for documentation wording only).
The review follow-up ran 06:25Z–06:40Z on the same base. It is identified by
the blob hashes listed in [Review follow-up](#review-follow-up), because the
commit that records it cannot cite its own hash.

Tickets: GOO-336 (setup, review, run and export surfaces) and GOO-337
(verification ledger and release evidence).

Source under test: `9c90ed8d3249f15931ba1a5d372dbd30b811f436`, the tip of
`origin/develop` at the time of the run (`git ls-remote origin
refs/heads/develop`). That commit is the gitops proposal for backend image
`ed6809a5fa360d1594794e166679efc1d4127a94`. The branch
`docs/daily-brief-release-evidence-20261008` adds two tests, a CI contract
test, one CI enrollment variable and these documents on top of it. No
production source changed.

Authorization scope: local runs, plus unauthenticated `GET` requests to the
public dev endpoints. Nothing was pushed or deployed and no flag was changed.
No credential was used. No `kubectl` command reached the cluster (see
BLOCKED below), and nothing in Argo CD, Helm releases or the database was
mutated.

Ledger: [daily-research-brief-verification.md](../../daily-research-brief-verification.md),
amendment "2026-10-08 release-evidence refresh". Vocabulary as in the ledger:
`PASS` ran and met its assertions; `FAILED` ran and failed; `BLOCKED` a
required credential or service was unavailable; `NOT RUN` outside authorized
scope or not feasible here.

## Environment

macOS (Darwin 27.2.0, arm64). Python 3.11.13 from the shared
`backend/.venv`, which is the same minor version as CI's `PYTHON_VERSION`.
Tooling: pytest 9.1.1, ruff 0.15.15, black 26.5.1, isort 5.13.2 and mypy
1.7.1, which match the CI pins. PostgreSQL 14.23 (Homebrew, `/tmp` socket,
disposable database `goo336_daily_brief_20261008`, dropped afterwards); CI
uses `postgres:15`. Node 24.21.0, pnpm 10.18.2, helm v4.1.0.
`pnpm install --frozen-lockfile` passed in 7.7 s.

## Summary

| Gate | Status | Evidence |
| --- | --- | --- |
| Full `pnpm --dir frontend validate` | FAILED | Exit 1 in the lint phase: `1912 problems (89 errors, 1823 warnings)`. The prior baseline was 113 errors / 1,961 warnings. None of the errors is in a Daily Brief path. |
| `pnpm --dir frontend lint` alone | FAILED | Same totals over 1,037 linted files, 51 of them with errors. #1716 changed 22 frontend files. ESLint covers 20 of them; the e2e spec and the generated `api.d.ts` are outside its scope. Those 20 have 0 errors and 4 warnings, all `explicit-function-return-type` in `StepProgress.tsx`. The only research-engine error is in `EvidenceMap.tsx:191` (`react-hooks/set-state-in-effect`), which #1716 did not touch. |
| Frontend quality ratchet (`check_frontend_quality.mjs`) | PASS | `OK: frontend lint debt within baseline (errors 98, warnings 1878)` with the changed test file. |
| `pnpm --dir frontend type-check` | PASS | `tsc --noEmit` exit 0 on the base and on the branch. |
| Full Vitest (`pnpm --dir frontend test`) | PASS | Base: 374 files, 2,845 tests. Branch: 374 files, 2,846 tests. |
| Research-engine Vitest subset | PASS | `src/components/research-engine`, `researchEngineService.test.ts` and `research-engine-store.test.ts`: 26 files, 186 tests. |
| Disabled-state backend unit tests | PASS | 4 named tests (below). The 4-file command passed 63 tests. |
| New disabled-flag PostgreSQL test | PASS (local PostgreSQL 14.23) | `test_disabled_flag_keeps_existing_run_readable_and_exportable`: 1 passed. After review it also covers step history, review state and a flag-on control. Seven source mutations each turned it RED, and each restore turned it GREEN. CI now enrolls it through `DAILY_BRIEF_ROLLBACK_TEST_DATABASE_URL`; no CI run has been observed yet. |
| Review-overlay frontend tests | PASS | `ReviewPanel.test.tsx` 8/8, including the new final-review claim-coverage test, which asserts visibility after review (two mutations RED/GREEN). |
| PostgreSQL lifecycle integration | FAILED | `test_daily_research_brief_postgres.py` on `9c90ed8d3`: 4 failed, 1 passed. Bisected to #1720 (below). |
| Browser → Next → FastAPI → PostgreSQL E2E | NOT RUN | See the reasons below. |
| OpenAPI snapshot | PASS | `scripts/ci/generate_openapi.py --check`: up to date. |
| Configured-model eval prerequisites | PASS / BLOCKED | `test_daily_research_brief_eval.py`: 13 passed, 1 skipped. The skip is `BLOCKED: ANTHROPIC_API_KEY is absent`. |
| Authenticated Safety | BLOCKED | `SAFETY_API_KEY` absent from the environment; not run. |
| Declared dev revision and configuration | PASS (declared) | Argo `nous-dev-aws` tracks `develop`, and `values-aws.yaml` pins image `ed6809a5f` (sha256 `833d07f4…3a89`). That image contains #1716, GOO-338 and both Daily Brief Alembic revisions. |
| Live API schema matches `ed6809a5f` | PASS | The live `/openapi.json` canonical SHA-256 equals `backend/openapi.json` at `ed6809a5f`. All 17 Daily Brief run, review and template routes are present. |
| `GET /health` | PASS | `200 {"status":"healthy",…}` |
| Unauthenticated Daily Brief routes | PASS (401), not a mount proof | Every probe answered `401 Not authenticated`, including two control paths that do not exist. The auth middleware answers before routing, so the mount is proven by the OpenAPI match above instead. |
| `GET https://goodwiinz.tech/` | PASS | 200. |
| Live deployment env and migration Job (`kubectl`) | BLOCKED | `aws: [ERROR]: Your session has expired. Please reauthenticate using 'aws login'.` |
| Authenticated live acceptance, rollback rehearsal, owner-only access inspection | NOT RUN | Owner actions; see the ledger amendment. |

## GOO-336: frontend validation

```text
$ pnpm --dir frontend validate          # = lint && type-check && test
✖ 1912 problems (89 errors, 1823 warnings)
  0 errors and 6 warnings potentially fixable with the `--fix` option.
ELIFECYCLE  Command failed with exit code 1.        (17 s; later phases not reached)
$ pnpm --dir frontend type-check        # exit 0 (11 s)
$ pnpm --dir frontend test              # exit 0 (46 s)
 Test Files  374 passed (374)
      Tests  2845 passed (2845)
```

Against the ledger's earlier figure (113 errors / 1,961 warnings), current
develop has 24 fewer errors and 138 fewer warnings. The leading error rules are
`react-hooks/set-state-in-effect` (37), `react-hooks/purity` (29) and
`react-hooks/immutability` (10). The aggregate gate still fails on inherited
debt, so this row stays FAILED. The type-check and full Vitest phases, which
the aggregate never reaches, were run separately and passed.

## GOO-336: disabled-state behaviour

The flag is checked only during template discovery
(`BlueprintLoader.list_templates` and `load_template`) and in `start_run`
(`backend/src/api/research_engine/runs.py:360-367`). Run detail, manifest,
review, stream and export routes do not consult it.

| Behaviour with `DAILY_RESEARCH_BRIEF_ENABLED=false` | Test | Result |
| --- | --- | --- |
| List hides the template; detail is not found | `test_blueprint_loader.py::TestListTemplates::test_daily_brief_is_visible_by_default_and_hidden_when_disabled`, `test_research_template_security.py::test_disabled_daily_template_is_hidden_and_cannot_be_instantiated` | PASS |
| Create refuses a Daily blueprint (custom still allowed) | `test_research_template_security.py::test_disabled_daily_template_is_hidden_and_cannot_be_instantiated` | PASS |
| Start refuses a new run from a persisted Daily blueprint | `test_research_engine_endpoints.py::TestStartRun::test_disabled_daily_brief_cannot_start_from_existing_blueprint` (mocked), and the new PostgreSQL test (real rows) | PASS |
| Code default is false; the environment can opt in | `test_blueprint_loader.py::TestListTemplates::test_daily_brief_release_setting_defaults_false_and_env_can_enable` | PASS |
| Existing run stays readable and exportable | No test before this branch. Added `backend/tests/integration/test_daily_research_brief_postgres.py::test_disabled_flag_keeps_existing_run_readable_and_exportable` | PASS |

The new test seeds a completed Daily Brief run, with one persisted step, in an
isolated PostgreSQL schema and forces the flag false. Run detail, manifest,
`/steps` (the persisted step with its output) and `/reviews/pending`
(`pending: false`) return 200. Markdown, JSON and CSV exports return 200 with
`daily-research-brief-<run>.{md,json,csv}` filenames, and the JSON names the
run with `final_status` `unverified`. A start from the same persisted
blueprint returns 404 `Blueprint not found`. As a positive control the same
caller then sends the same start with the flag true and gets 422 `Daily Brief
scope confirmation is required`. The 404 therefore comes from the kill switch,
not from an access failure, which has the same 404 body. The run count for
that blueprint stays 1. The step history, review state and control were added
after review; the first-round version is the command below.

```text
$ ORCHESTRATION_TEST_DATABASE_URL="postgresql://goodwiinz@/goo336_daily_brief_20261008?host=/tmp" \
  PYTHONPATH=backend backend/.venv/bin/python -m pytest -c backend/pytest.ini -q \
  "backend/tests/integration/test_daily_research_brief_postgres.py::test_disabled_flag_keeps_existing_run_readable_and_exportable"
1 passed, 1 warning in 0.81s
```

The test pins behaviour that already existed, so it passed on its first run.
Its RED side comes from mutation. Each mutation was applied to the source,
run with the focused command above, restored from a byte copy, confirmed by
an empty `git diff`, and rerun:

| ID | Mutation (the defect the test prevents) | RED | Restored |
| --- | --- | --- | --- |
| M1 | Kill switch added to `export_run` (`runs.py:540`) for Daily blueprints | `1 failed`: `('markdown', '{"detail":"Run not found"}')`, `assert 404 == 200` | `1 passed` |
| M2 | Kill switch added inside `ExportService.export` (`export_service.py:82`), a layer that route-mocked unit tests cannot see | `1 failed`: `{"code":"run_not_found",…}`, `assert 404 == 200` | `1 passed` |
| M3 | `start_run` guard removed (`runs.py:360-367`) | `1 failed`: `{"detail":"Daily Brief scope confirmation is required"}`, `assert 422 == 404` | `1 passed` |
| M4 | Kill switch added to `get_run` (`runs.py:417`) for Daily blueprints | `1 failed`: `{"detail":"Run not found"}`, `assert 404 == 200` | `1 passed` |

After review, M1–M4 were rerun against the extended test and M5–M7 were
added. Each one is RED, then byte-identical restore, then an empty `git diff`
and GREEN, as listed in [Review follow-up](#review-follow-up).

Observation: a run that was created before the flag flips can still be
streamed and resumed, because the stream route does not consult the flag. That
matches the ledger's wording ("refuses starting a new Daily run"), but a
rollback does not halt in-flight runs. The owner should decide whether that is
intended.

## GOO-336: review overlays and export metadata

Existing coverage, all passing in the full Vitest run:

- Screening: `ReviewPanel.test.tsx` "shows original screening evidence…" and
  "blocks unresolved approval and requires a reason for exclusions".
- Extraction: "renders original extraction data and quoted evidence". In
  `RunView.test.tsx`: "fills a projected extraction review…" and the
  projected-mismatch matrix.
- Final release: "does not offer final approval for a {unverified, failed}
  artifact". In `RunView.test.tsx`: "uses the matching full export step for a
  projected final review".
- Export actions and metadata after completion: `RunResults.test.tsx` checks
  the verified label, provider, source, extraction and claim counts, and
  authenticated JSON download. It also checks that an overridden result is
  labelled unverified and that a no-evidence outcome offers audit downloads
  only.

Gap: no test asserted that claim coverage is available at the final decision
point. Added `ReviewPanel.test.tsx` "shows claim coverage beside the persisted
brief at the final decision". For a verified final review it checks that the
persisted brief is visible and that the "Claim coverage and checks"
disclosure is visible. The disclosure is a `<details>` element without
`open`, so the claim id, status and evidence id start collapsed. The test
asserts they are not visible, clicks the summary, and then asserts the
disclosure is open and they are visible, beside an enabled "Approve final
review" button. The first-round version asserted DOM presence only; review
pointed out that jsdom accepts collapsed content as present.

| ID | Mutation | RED | Restored |
| --- | --- | --- | --- |
| F1 | `ReviewPanel.tsx:382` `{finalChecks !== undefined && (` changed so the block never renders | `Tests 1 failed`: `Unable to find an element with the text: Claim coverage and checks` | `Tests 1 passed` |
| F2 | `ReviewPanel.tsx:384-386` `<summary>` replaced with `<p>`, so the reviewer cannot open the disclosure (review follow-up) | `Tests 1 failed`: `Received element is not visible` | `Tests 1 passed` |

## GOO-336: PostgreSQL lifecycle (browser-to-worker substrate)

```text
$ ORCHESTRATION_TEST_DATABASE_URL=… PYTHONPATH=backend backend/.venv/bin/python -m pytest \
  -c backend/pytest.ini -q backend/tests/integration/test_daily_research_brief_postgres.py
FAILED test_controlled_lifecycle_persists_each_stage_exactly_once        - assert 'event: run_paused' in …
FAILED test_partial_provider_failure_deduplicates_and_persists_metadata_only - assert {'execution_i…} == {'status': 'f…TimeoutError'}
FAILED test_terminal_search_scenarios_are_durable[no_evidence]           - assert '"final_status": "no_evidence"' in …
FAILED test_failed_verification_requires_hash_bound_override             - TypeError: 'NoneType' object is not subscriptable
4 failed, 1 passed, 1 warning in 1.46s                                   (base 9c90ed8d3)
```

The stream log for each failure names
`ResearchRunLifecycleError: Completed step hash does not match its persisted envelope`
at the search stage. On the branch, the same file plus
`test_research_review_concurrency_postgres.py` gives
`4 failed, 4 passed` (the passes are `all_provider_failure`, the new test and
both review-concurrency tests).

Bisect without checkout: `git archive <sha> backend/src backend/tests
backend/pytest.ini` into scratch, then the same command, interpreter and
database.

| Commit | Role | Result |
| --- | --- | --- |
| `1b3ee4d50` | #1716 merge | `5 passed` |
| `e4c123500` | parent of #1720 | `5 passed` |
| `ad6f47b16` | #1720 (search strategy versions and receipts) | `4 failed, 1 passed`: `completed search receipt does not match its checkpoint` |
| `b81da08a2` | parent of #1899 | same as `ad6f47b16` |
| `61e9f016d` | #1899 (step id for search receipts) | `4 failed, 1 passed`: `Completed step hash does not match its persisted envelope` |
| `9c90ed8d3` | develop tip, deployed image `ed6809a5f` | same as `61e9f016d` |

Mechanism: `WorkflowEngine` hashes the contract-v1 search output
(`engine.py:364`). The stream route then calls
`SearchReceiptJournal.finalize_search_step` (`runs.py:983`), which mutates
that output in place (`search_receipts.py:170`, `:180`, `:184`).
`persist_step_completion` then rejects the changed hash
(`run_lifecycle.py:206`). A throwaway diagnostic made `finalize_search_step`
work on a copy. The `no_evidence` case then passed, and the controlled
lifecycle failed later on `relation "research_decision_streams" does not
exist`, a table the test harness does not create (added in #1710). The
partial-provider test also still expects the pre-#1720 receipt shape. The
diagnostic was reverted (`backend/src` clean) and is not part of this branch.

CI never saw this. The `Run integration tests` step in
`.github/workflows/test-pipeline.yml` does not set
`ORCHESTRATION_TEST_DATABASE_URL`, and it did not at `1b3ee4d50` either, so
these tests skip in CI. After review, that step sets only
`DAILY_BRIEF_ROLLBACK_TEST_DATABASE_URL`, which enrolls the disabled-flag test
alone; the four lifecycle tests keep skipping until the regression is fixed. `frontend/e2e/research-engine/daily-research-brief.spec.ts`
is not run by any CI job either; the CI E2E job runs `tests/e2e`. The
"integration 308" and "E2E 6" figures in the #1716 final-head evidence do not
include either suite.

Browser E2E is NOT RUN. It needs a Next dev server, a Uvicorn fixture and
Playwright Chromium. The ports are configurable (`BASE_URL`, default
`http://localhost:3000`, and `DAILY_BRIEF_E2E_PORT`, default 8765). Outside
CI, however, `frontend/playwright.config.ts` sets `reuseExistingServer`, so
on this machine, which other agents share, the run could attach to another
agent's dev server. More decisively, the spec drives the same
`create_e2e_app` lifecycle that already fails at the search stage above, so
a pass is not possible on this head until that regression is fixed.

## GOO-337: deployed revision and configuration (read-only)

```text
$ git ls-remote origin refs/heads/develop
9c90ed8d3249f15931ba1a5d372dbd30b811f436  refs/heads/develop
$ git log -1 9c90ed8d3      chore(gitops): propose dev image ed6809a (#1933)   2026-10-08T04:40:00Z
values-aws.yaml backend.image: tag/sourceSha ed6809a5fa360d1594794e166679efc1d4127a94
                               digest sha256:833d07f47288cc103ef14ee955b0d279cbd19d417ac05330c1edf7ab4b9f3a89
$ git merge-base --is-ancestor <c> ed6809a5f
1b3ee4d50 (#1716) in   22f3f6ebc (GOO-338) in   ad6f47b16 (#1720) in   61e9f016d (#1899) in
$ git ls-tree ed6809a5f backend/alembic/versions/ → 20260927_daily_research_brief_reviews.py,
  merge_research_heads_20260928_merge_daily_brief_and_academic_workflow_.py
$ alembic heads → hb03_workspace_grants (head); daily_brief_reviews_20260927 is its ancestor
$ helm template nous-dev-aws … -f values.yaml -f values-aws.yaml   (render only)
Deployment backend, celery-worker, celery-beat; Job migrate-5e56e6fa… (sync-wave 1); CronJob synthetic-traffic:
  image …/nous/backend@sha256:833d07f4…3a89, env DAILY_RESEARCH_BRIEF_ENABLED=true
```

`env` entries take precedence over the `envFrom` Secrets, so the rendered
value is `true` even if a synced Secret carried the key. This is the declared
state. Without `kubectl` the live pod env and the migration Job's completion
remain unverified.

## GOO-337: live probes (GET only, unauthenticated)

```text
run_at=2026-10-08T05:55:12Z
200 GET https://dev-api.goodwiinz.tech/health   {"status":"healthy",…}
401 GET …/api/v1/research-engine/blueprints/templates
401 GET …/api/v1/research-engine/blueprints/templates/daily_research_brief
401 GET …/api/v1/research-engine/runs/00000000-0000-0000-0000-000000000000
401 GET …/api/v1/research-engine/runs/00000000-0000-0000-0000-000000000000/export?format=json
401 GET …/api/v1/research-engine/no-such-route-goo337-control      (control)
401 GET …/api/v1/no-such-router-goo337-control                      (control)
200 GET https://goodwiinz.tech/
200 GET https://dev-api.goodwiinz.tech/openapi.json   917,022 bytes, 516 paths
```

The live OpenAPI document's canonical JSON SHA-256 is
`bfe5b396da97ca67e95c4c7f619a063d7bced19206e22a324f6f2b0eee32a9d9`, the same
value as `backend/openapi.json` at `ed6809a5f`. The pre-#1930 schema at
`b70c65f05` differs (`a96079ee…`), so the running API is at least
`ed6809a5f`. No later develop commit changes backend code. The document lists
the template list and detail, `POST /blueprints/{blueprint_id}/runs`, run
detail, manifest, stream, pause and resume, `reviews/pending`,
`reviews/{step_index}` and `export` routes. Because the two control paths also
answer 401, the 401s alone do not prove anything.

```text
$ kubectl config current-context
arn:aws:eks:us-east-1:267685730035:cluster/nous-dev-cluster
$ kubectl -n multimodal-rag-system get deploy --request-timeout=20s
aws: [ERROR]: Your session has expired. Please reauthenticate using 'aws login'.
Unable to connect to the server: getting credentials: exec: executable aws failed with exit code 255
```

`aws login` is interactive, so the live deployment env, pod image digest and
migration Job status are BLOCKED. No other `kubectl` command was attempted.

## Lint gates for the touched files

| File | Command | Result |
| --- | --- | --- |
| `backend/tests/integration/test_daily_research_brief_postgres.py` | `ruff check`, `black --check`, `isort --check-only` | PASS (all three) |
| same | `mypy --ignore-missing-imports --follow-imports=silent --disallow-untyped-defs` | 10 pre-existing errors at lines 23, 309-322, 943, 957 and none in the test's lines 1439-1549 (after review). The file is modified, not added, so the CI mypy gate does not apply. |
| `backend/tests/unit/ci/test_shard_tests.py` | `ruff check`, `black --check`, `isort --check-only`; `mypy --disallow-untyped-defs` | PASS; mypy `Success: no issues found` (modified, not added) |
| `.github/workflows/test-pipeline.yml` | `actionlint` 1.7.12 (local; CI pins 1.7.7) | One shellcheck `SC2086:info` at line 1282, which is the same finding as line 1279 on the base file. Nothing new from the added env lines. |
| `frontend/src/components/research-engine/__tests__/ReviewPanel.test.tsx` | `eslint`, `prettier --check`, quality ratchet | PASS |

## Review follow-up

An independent review found that CI never ran the disabled-flag test. The test
read only `ORCHESTRATION_TEST_DATABASE_URL`, and enrolling that variable would
also turn on the four failing lifecycle tests. The review also raised five minor
points. The follow-up ran on the same base, `9c90ed8d3`, with the same
interpreter and PostgreSQL 14.23, in disposable database
`goo336_rollback_review_20261008` (dropped afterwards). The files under test are
identified by git blob hash:

| File | Blob |
| --- | --- |
| `backend/tests/integration/test_daily_research_brief_postgres.py` | `e178b026353e5caf40c6997ad07144d6e07ffd9e` |
| `backend/tests/unit/ci/test_shard_tests.py` | `664351cccfabd8a04f726963ef456a5999110c5e` |
| `frontend/src/components/research-engine/__tests__/ReviewPanel.test.tsx` | `6fafe67254f0eb93cf079c828b624d465cbb4c5e` |
| `.github/workflows/test-pipeline.yml` | `202c5b863fa2c299941ca768f9810ac113db8c6a` |

Changes:

- The disabled-flag test reads `DAILY_BRIEF_ROLLBACK_TEST_DATABASE_URL`, falling
  back to `ORCHESTRATION_TEST_DATABASE_URL`, which is the
  `test_artifact_publish_postgres.py` pattern. The `Run integration tests` step
  sets the new variable to the job's `postgres:15` service.
- A CI contract test,
  `test_shard_tests.py::test_integration_job_enrolls_daily_brief_rollback_regression`,
  fails if that variable is dropped from the step.
- The test also reads `/steps` and `/reviews/pending`, and adds the flag-on
  positive control for the start refusal.
- The claim-coverage test asserts visibility of the collapsed disclosure.

```text
# Before the fix: the dedicated variable alone still skipped the test.
$ env -u ORCHESTRATION_TEST_DATABASE_URL DAILY_BRIEF_ROLLBACK_TEST_DATABASE_URL=… pytest …::test_disabled_flag_keeps_existing_run_readable_and_exportable -rs
SKIPPED [1] …:1451: ORCHESTRATION_TEST_DATABASE_URL is not configured

# After: CI-shaped run of the whole module (dedicated variable only, -m integration)
$ env -u ORCHESTRATION_TEST_DATABASE_URL ENVIRONMENT=testing DAILY_BRIEF_ROLLBACK_TEST_DATABASE_URL=… \
  pytest backend/tests/integration/test_daily_research_brief_postgres.py -c backend/pytest.ini -m integration -rs
1 passed, 5 skipped           (the 5 skips: "ORCHESTRATION_TEST_DATABASE_URL is not configured")
# Fallback: ORCHESTRATION_TEST_DATABASE_URL only            -> 1 passed
# Neither variable                                          -> 1 skipped ("Daily Brief rollback PostgreSQL test database is not configured")
# Whole module with ORCHESTRATION_TEST_DATABASE_URL         -> 4 failed, 2 passed (lifecycle regression unchanged)

$ pytest -c backend/pytest.ini backend/tests/unit/ci/test_shard_tests.py   -> 22 passed
$ pytest -c backend/pytest.ini backend/tests/unit/ci                       -> 450 passed
$ pytest -c backend/pytest.ini <4 disabled-state unit files>               -> 63 passed
$ pnpm --dir frontend exec vitest run …/ReviewPanel.test.tsx               -> 8 passed
$ pnpm --dir frontend test                                                 -> 374 files, 2,846 tests passed
$ pnpm --dir frontend type-check                                           -> exit 0
$ eslint app src (JSON) + check_frontend_quality.mjs --base origin/develop -> 89 errors / 1,823 warnings over 1,037 files (unchanged);
                                                                              OK: within baseline (errors 98, warnings 1878)
```

Mutations (line numbers are the injection points on `9c90ed8d3`; the
first-round table above cites the enclosing functions): each was injected into
source, run with the focused test, restored
from a byte copy, confirmed by an empty `git diff` for source files (and
byte-identical restore for the workflow, which carries this change), and rerun
GREEN.

| ID | Mutation | Focused test | RED | Restored |
| --- | --- | --- | --- | --- |
| M1 | Kill switch on `export_run` (`runs.py:547`) | rollback test | `('markdown', '{"detail":"Run not found"}')` | 1 passed |
| M2 | Kill switch inside `ExportService.export` (`export_service.py:96`) | rollback test | `('markdown', '{"detail":{"code":"run_not_found",…}}')` | 1 passed |
| M3 | `start_run` guard neutralized (`runs.py:362`) | rollback test | `{"detail":"Daily Brief scope confirmation is required"}` (start answered 422, not 404) | 1 passed |
| M4 | Kill switch on `get_run` (`runs.py:423`) | rollback test | `{"detail":"Run not found"}` | 1 passed |
| M5 | Kill switch on `list_steps` (`steps.py:48`) | rollback test | `{"detail":"Run not found"}` | 1 passed |
| M6 | Kill switch on `get_pending_review` (`reviews.py:58`) | rollback test | `{"detail":"Run not found"}` | 1 passed |
| M7 | `start_run` denies access before the guard (`runs.py:357`) | rollback test | control: `{"detail":"Blueprint not found"}` (the 404-only assertion alone would have passed) | 1 passed |
| C1 | `DAILY_BRIEF_ROLLBACK_TEST_DATABASE_URL` removed from the CI step | CI contract test | `assert None == "postgresql://test:test@localhost:${{ … }}/test_db"` | 1 passed |
| F2 | `<summary>` replaced with `<p>` | claim-coverage test | `Received element is not visible` | 1 passed |

F1 was also rerun against the strengthened test: `Unable to find an element with
the text: Claim coverage and checks`, then 1 passed after restore.

Minor points resolved in the documents rather than in code:

- The first-round re-checks cited commit `7a11b8f5f`, which was amended away and
  cannot be resolved after push. They now cite `f0b7d4575`, whose tests and
  source are byte-identical (`git diff --stat 7a11b8f5f f0b7d4575` lists only
  the two documents).
- The browser E2E reason no longer says the ports are fixed.
- The ledger now calls "production means the dev lane" a proposed
  interpretation that the release owner must confirm.

The CI result for the enrolled test is not yet observed. It counts only once
the Integration Tests job passes on the exact pushed head.

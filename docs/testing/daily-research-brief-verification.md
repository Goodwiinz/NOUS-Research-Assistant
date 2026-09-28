# Daily Research Brief verification record

Date: 2026-09-28

Branch: `codex/daily-research-brief-20260927`

Task 8 base: `68cc641938f43b99fa8d00e18822ccf2c62106e8`

Final executable source: `b334ec79861b438e8646ff75325cf4f5bdd75a30`

Prior final re-review source: `ac17ca15a3a91af936851647bbde89d9860709ba`

Prior whole-feature source: `9ca9d6a2995ba8dd52328efd0e5cd93856138b4a`

Independent-review source: `a92fcfa770f4fbe89d7154f1bb88b37f8cf5f408`

Original certification source: `efd76079be75fdbba9eef4215c1c831d829cacb4`

Disposition: **CERTIFICATION COMPLETE — NOT READY TO ENABLE**

## 2026-09-28 identifier-safety amendment

This amendment supersedes the prior final-source designation. Source
`ac17ca15` added the missing multi-provider bibliography fallback and exact
hash-relationship regression. Final source `b334ec798` constrains reader-facing
identifiers to the seven discovery identifier kinds, makes populated canonical
and direct identifiers authoritative over retained provenance, and rejects
opaque provider keys at the final projection. Earlier results remain attributed
to the source on which they ran.

The final executable source is followed by a documentation/evidence-only child
commit. That scheme avoids a self-referential hash: committed transcripts name
immutable source `b334ec798`, while the handoff names both commits. Earlier
performance/eval transcripts stay bound to `a92fcfa` or `9ca9d6a` where they
actually ran.

`DAILY_RESEARCH_BRIEF_ENABLED` is a server-owned setting and defaults to
`false`. It was enabled only in isolated local tests. Production configuration
was not inspected or changed. The live-model quality gate and authenticated
Safety scan are BLOCKED; dependency audits, anonymous Safety, full frontend
validation, and full local CI are FAILED; remote checks and release actions are
NOT RUN. Local passing gates do not override those release blocks.

## Gate ledger

`PASS` means the named local gate ran and met its assertions. `FAILED` means it
ran and returned a failing result. `BLOCKED` means a required credential or
service was unavailable. `NOT RUN` means the action was outside authorized
local scope or lacked the required release state.

| Gate                                        | Status  | Evidence                                                                                                                                 |
| ------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| PostgreSQL lifecycle integration            | PASS    | Prior source `9ca9d6a`: 5 tests passed; exact-source browser on `b334ec798` also exercised real PostgreSQL.                              |
| Browser → Next → FastAPI → PostgreSQL       | PASS    | Final source `b334ec798`: all 10 serial scenarios passed with the flag explicitly enabled locally.                                      |
| Browser 200-row review rendering            | PASS    | Final source: 11 cold reloads, p50 `523.3 ms`, p95 `604.3 ms`, frozen ceiling `999.24 ms`.                                               |
| Frozen backend performance                  | PASS    | Three runs on `a92fcfa` and one exact run on prior source `9ca9d6a` passed unchanged ceilings; not relabeled for later rendering fixes. |
| Affected/broader backend                    | PASS    | Final source: `162 passed`; prior broader focused set: `277 passed`.                                                                      |
| Focused frontend                            | PASS    | 5 files and 61 tests passed; manual-resume boundary included.                                                                            |
| Frontend type-check and earlier full Vitest | PASS    | Final source: Node `24.21.0`, pnpm `10.18.2`; type-check passed. Earlier full suite: 315 files, 2,368 tests.                             |
| Full `frontend validate`                    | FAILED  | Existing full-tree lint debt stopped the command: 113 errors and 1,961 warnings.                                                         |
| OpenAPI and generated TypeScript            | PASS    | Snapshot check passed; regenerated types produced no diff.                                                                               |
| Alembic upgrade/downgrade/upgrade           | PASS    | `agent_ops_20260925 → daily_brief_reviews_20260927 → agent_ops_20260925 → daily_brief_reviews_20260927` passed on disposable PostgreSQL. |
| Bandit source scan                          | PASS    | Bandit `1.9.4` returned no in-scope finding under the repository baseline and configured filters.                                        |
| Staged candidate secret scan                | PASS    | Gitleaks `8.30.1` found no staged-source secret.                                                                                         |
| JavaScript dependency audit                 | FAILED  | pnpm audit returned 21 advisories: 2 critical, 9 high, 8 moderate, 2 low.                                                                |
| Python dependency audit                     | FAILED  | pip-audit returned 5 vulnerabilities in 4 resolved packages.                                                                             |
| Authenticated Safety                        | BLOCKED | Safety `3.8.1`; `SAFETY_API_KEY` absent.                                                                                                 |
| Anonymous legacy Safety                     | FAILED  | One active `gunicorn 22.0.0` finding; 129 unpinned findings automatically ignored.                                                       |
| Fixed-corpus semantic eval                  | PASS    | Independent deterministic labels distinguish supported, contradictory, and unsupported evidence and reject a false self-supported claim. |
| Current configured-model eval               | BLOCKED | `ANTHROPIC_API_KEY` absent. No live score or citation threshold is claimed.                                                              |
| Full local CI                               | FAILED  | 29 repository-baseline backend failures plus inherited changed-file/debt gates; no remaining failure names a Task 8 path.                |
| Remote candidate-SHA checks                 | NOT RUN | No push was authorized solely to obtain checks.                                                                                          |
| Production configuration/rollback           | NOT RUN | No production flag, owner policy, telemetry, or rollback drill was inspected or changed.                                                 |
| Deploy/flag enablement                      | NOT RUN | No deployment or production enablement was authorized.                                                                                   |

Bandit is a source scan. It does not offset either failed dependency audit.

## Test environment

Final local evidence used Linux `6.8.0-124-generic` x86_64, Python `3.12.3`,
pytest `9.1.1`, Node `24.21.0`, pnpm `10.18.2`, Docker `29.1.3`, Bandit
`1.9.4`, and Gitleaks `8.30.1`. Database commands used disposable PostgreSQL
through `$ORCHESTRATION_TEST_DATABASE_URL`; no credential is committed.

## Final behavior proved

### Canonical provenance rendering

Extraction evidence is joined to canonical source records by `source_id`.
Rendering obtains `evidence_level`, DOI, publication year/date, URL, journal,
and other bibliography fields from the canonical/nested connector shape rather
than from extraction records that cannot carry those fields. When deduplication
retains a secondary connector under `metadata.provenance[]`, rendering fills
only empty canonical bibliography fields from those immutable origin snapshots;
it never overrides a non-empty canonical value. Reader identifier maps are
restricted to `doi`, `pmid`, `pmcid`, `arxiv`, `openalex`,
`semantic_scholar`, and `rag_store`. Populated canonical/direct identifiers are
seeded before provenance fallback, and the final projection filters the map
again. Real-shaped OpenAlex, Crossref, and PubMed regressions, including both
provider orders, a three-provider DOI/PMID bridge, and a conflicting provenance
DOI with opaque `provider_trace`/`patient_id` values, prove an abstract source
remains `abstract`, canonical DOI/year/journal survive into JSON, Markdown, and
CSV, and opaque identifier data cannot reach readers. The provenance blob also
remains internal.

### Final approval attestation

The final review does not mutate the artifact it reviewed. After approval, an
append-only attestation binds:

- review ID, reviewer ID, timestamp, decision, kind, and review index;
- the reviewed output hash and report hash; and
- the verification output hash and canonical attestation hash.

Verified downloads expose the complete review history and final attestation in
JSON, Markdown, and CSV. `report_hash` is the canonical JSON hash of the report;
it is not the Markdown byte hash. The post-approval audit records a separate
SHA-256 for the exact persisted Markdown prefix. The export-stage `output_hash`
is the canonical hash of the full persisted export envelope, which includes
`report_hash`, `verification_output_hash`, and the exact Markdown content. Tests
prove that changing that content changes `output_hash`. Missing or mismatched
review identity, output/report/verification hash, or attestation hash fails
closed with `verified_artifact_attestation_invalid`.

### Default-off runtime control

`DAILY_RESEARCH_BRIEF_ENABLED=false` is the default. While false, the server:

- omits the Daily template from enumeration;
- returns the same not-found behavior for template detail;
- refuses Daily blueprint creation; and
- refuses starting a new Daily run from an already persisted Daily blueprint.

Existing run read/audit/export paths remain available for recovery and legal or
operational audit. Legacy and custom workflows remain available. The frontend
uses the server template list and has no hard-coded bypass. Unit, integration,
and browser fixtures that exercise Daily Brief opt in explicitly.

### Durable terminal cancellation

`recover_stream_cancellation` locks the run in the transaction and checks its
current durable status before writing a pause. If terminal completion/failure
won the race before terminal frame delivery, recovery returns without changing
status or creating a user-pause/resume path.

This guard is mutation-proven. Removing only the completed/failed early return
made the focused route-boundary regression fail because `completed` became
`paused` (`1 failed` in 4.42s). Restoring the guard passed (`1 passed` in
4.13s). Bounded RED/GREEN transcripts and raw-capture digests are committed.
Existing manual-pause and reconnect tests pass in the affected and broader
focused suites.

## PostgreSQL lifecycle evidence

Exact command:

```bash
PYTHONPATH=backend \
ORCHESTRATION_TEST_DATABASE_URL="$ORCHESTRATION_TEST_DATABASE_URL" \
.venv/bin/python -m pytest -c backend/pytest.ini -q \
  backend/tests/integration/test_daily_research_brief_postgres.py
```

Prior whole-feature source result: `5 passed, 1 warning in 4.88s`. The two later
source changes affected only report rendering/tests; the final exact-source
browser run exercised the real PostgreSQL lifecycle, but this dedicated command
was not rerun.

The suite proves the six-stage happy path and all three reloadable review gates;
partial/all provider failure; evidence levels and no-evidence output;
verification failure and exact-hash override; reconnect; stale/conflicting and
idempotent duplicate reviews; owner denials; exact persisted outputs; final
approval attestation; and default-off behavior where applicable.

After duplicate resume POSTs, two actual SSE requests synchronize immediately
before `claim_stream`. The PostgreSQL row lock admits one `200` and rejects one
`409`. Assertions require exactly one stage execution, one review row, one
durable transition, and no remaining one-use authorization. Removing only the
claim row lock produced mutation RED `(200, 200)` plus duplicate extraction;
restoring the exact production file produced GREEN `(200, 409)`.

## Exact-source browser evidence

The test uses a real Chromium browser, Next application, Uvicorn FastAPI test
application, and disposable PostgreSQL. Controlled provider/model fixtures stop
at the external-provider seam. The browser, HTTP API, lifecycle, and database
are not mocked.

The offline Next server ran with this environment:

```bash
BACKEND_URL=http://127.0.0.1:8765 \
NEXT_PUBLIC_API_BASE_URL=/api/v1 \
NEXT_PUBLIC_API_URL= \
NEXT_PUBLIC_AUTH_COOKIE_NAME=daily-brief-e2e-auth \
NEXT_PUBLIC_SUPABASE_URL=http://localhost:3000/api/v1/auth-fixture \
SUPABASE_SERVER_URL=http://127.0.0.1:8765/api/v1/auth-fixture \
NEXT_PUBLIC_SUPABASE_ANON_KEY=e2e-anon-key \
DAILY_RESEARCH_BRIEF_ENABLED=true \
"$NODE24_BIN" /usr/bin/corepack pnpm@10.18.2 \
  --dir frontend run dev:offline
```

The exact test command was:

```bash
DAILY_RESEARCH_BRIEF_ENABLED=true \
ORCHESTRATION_TEST_DATABASE_URL="$ORCHESTRATION_TEST_DATABASE_URL" \
DAILY_BRIEF_E2E_PORT=8765 \
"$NODE24_BIN" /usr/bin/corepack pnpm@10.18.2 \
  --dir frontend exec playwright test \
  e2e/research-engine/daily-research-brief.spec.ts \
  --project=chromium --reporter=line
```

The Playwright fixture starts Uvicorn with the same flag and database URL. The
precondition asserted a clean worktree at exact source
`b334ec79861b438e8646ff75325cf4f5bdd75a30`.

Result: `10 passed (2.9m)`. The scenarios cover all three reloadable review
gates and exact downloads; provenance/attestation; partial provider failure;
no-evidence; verification failure and exact-hash override; manual resume after
an intermediate paused GET; 200-row rendering; stale refresh; immutable
replay; and same-organization/cross-tenant owner denials. Eleven cold 200-row
reloads observed p50 `523.3 ms` and p95 `604.3 ms` below `999.24 ms`.

The manual-resume test performs the resume POST, lets the immediate refresh
return paused, opens the SSE request, and verifies durable `running` through the
real audit route. The deliberately held connector makes the local Next dev
proxy buffer the body, so the test then sends the same authenticated pause
through browser → Next → FastAPI and resumes to the next lifecycle boundary.
It counts two resumed stream attempts: the controlled transient failure and the
winning claim. This proves the authorization survives the intermediate paused
response and is consumed by a real stream, rather than only proving a POST was
issued.

Earlier attempts remain committed as failed/excluded evidence. One source had
a consumed-download-response assertion (`1 failed, 9 not run`); another had an
unstable reconnect boundary (`5 passed, 1 failed, 4 not run`); three focused
attempts exposed Next response buffering. A login redirect caused by incomplete
offline auth and host/channel interruptions are environment evidence. None is
reported as a pass. On final source, two additional attempts stopped before the
feature lifecycle because stale local Next route manifests omitted `/login` and
then the dynamic project/blueprint route (`1 failed, 9 not run` each). Their raw
captures are retained as failed/excluded setup evidence. Regenerating the local
route cache changed no source bytes; the subsequent full run passed.

## Configured-model evaluation

Exact command:

```bash
PYTHONPATH=backend \
.venv/bin/python -m pytest -c backend/pytest.ini -q \
  backend/tests/eval/test_daily_research_brief_eval.py
```

Result on independent-review source: `13 passed, 1 skipped` in 3.84s. Local
prerequisite and fixed-corpus semantic regressions PASS. The configured-model
call is separately BLOCKED because `ANTHROPIC_API_KEY` is absent. A skip is not
a live-model pass.

The prerequisite matrix stops before a model call for a missing key, fixture
key, and candidate mode without a frozen citation threshold. Baseline mode
without a threshold and candidate mode with a valid threshold proceed to the
call boundary. Supplied malformed or out-of-range thresholds fail.

The deterministic scorer does not read the pipeline/model's own `supported`
label. Fixed evidence includes supported, contradictory, and unsupported
relations. An adversarial false increase claim quotes real evidence showing a
decrease and self-labels supported; the scorer rejects it. This proves only the
encoded fixed-corpus relations. It is not a general entailment benchmark and
does not replace the unavailable live-provider lifecycle evaluation. No live
citation baseline or candidate score was frozen.

## Frozen performance evidence

Exact command for every full invocation:

```bash
PYTHONPATH=backend \
ORCHESTRATION_TEST_DATABASE_URL="$ORCHESTRATION_TEST_DATABASE_URL" \
DAILY_BRIEF_PERF_CAPTURE=1 \
.venv/bin/python -m pytest -c backend/pytest.ini -q -s \
  backend/tests/performance/test_daily_research_brief_performance.py
```

The numeric thresholds are the frozen pre-optimization baseline plus 20% and
were never loosened. The nearest-rank function did not change. Final method
matches the baseline: one complete untimed warmup, then 11 measured max-stage
observations with GC enabled. Nearest-rank p95 is therefore rank 11, the
maximum. A deterministic regression locks this policy. The intervening
40-sample/rank-38 method was rejected as non-comparable.

Three consecutive invocations on `a92fcfa` passed all six tests. Their max
search/screen/extract p95 values were `14.170/29.408/88.468`,
`13.709/31.652/89.163`, and `13.839/28.162/84.095 ms`.

After the rendering/attestation/lifecycle changes, the exact documented command
ran again on prior whole-feature source `9ca9d6a` and returned `6 passed` in
18.93s. Sources `ac17ca15` and `b334ec798` only project and filter
already-retained bibliography/identifier maps during report construction;
performance was not rerun or relabeled for either source.

| Metric                        |                  Source 9ca9d6a |                 Frozen ceiling |
| ----------------------------- | ----------------------------: | -----------------------------: |
| Six-stage p95                 |                   `19.962 ms` |                    `35.345 ms` |
| Export p95                    |                    `0.595 ms` |                     `0.826 ms` |
| Export payload                |                    `15,498 B` |                     `18,353 B` |
| 1/2/4-provider p95            |   `3.524 / 6.428 / 13.190 ms` |   `7.893 / 13.541 / 37.944 ms` |
| Max search/screen/extract p95 | `15.974 / 31.788 / 96.698 ms` | `38.572 / 77.212 / 849.648 ms` |
| Max-stage payload             |                 `1,152,543 B` |                  `1,383,052 B` |
| Peak traced bytes             |                 `3,034,368 B` |                  `4,349,228 B` |
| Review projection p95         |                    `6.145 ms` |                     `9.282 ms` |
| Review payload                |                    `24,094 B` |                     `28,913 B` |
| Cold hydration p95            |                    `3.096 ms` |                     `3.983 ms` |
| Ten-run lifecycle p95         |                `1,149.907 ms` |                 `2,068.731 ms` |

The 204-byte export increase reflects correct canonical bibliography and final
attestation data and remains below the unchanged ceiling. Every run produced
200 candidates, 25 screen batches, 25 extract batches, ten completed soak
lifecycles, zero duplicate stage rows, and 30 unique review rows.

Earlier failures remain visible: search `46.135` and `48.231 ms`, hydration
`4.016 ms`, and screen `191.853/190.960/195.187 ms`. Profiling connected them
to retained fixture/executor cycles, a production bound-method cycle,
quadratic source/coverage and usage-snapshot work, repeated schema compilation,
unnecessary merge scans for provably unique identifiers, and a private prompt
view deep copy. The fixes keep exact behavior, public envelope isolation,
validation, thresholds, and the final baseline-equivalent method intact.

## Frontend, contracts, and migration

Prior focused frontend result: 5 files and 61 tests passed. Node `24.20.0`
type-check passed there; final re-review type-check passed with Node `24.21.0`.
The earlier full Vitest suite passed 315 files and 2,368 tests. Full
`frontend validate` remains FAILED at its lint phase with 113 errors and 1,961
warnings; later steps were not reached by that aggregate command.

Contract checks:

```bash
.venv/bin/python scripts/ci/generate_openapi.py --check
"$NODE24_BIN" /usr/bin/corepack pnpm@10.18.2 \
  --dir frontend run generate:api-types
git diff --exit-code -- backend/openapi.json \
  frontend/src/types/generated/api.d.ts
```

OpenAPI was current and regenerated TypeScript produced no diff.

The disposable PostgreSQL migration cycle started at actual parent
`agent_ops_20260925`, upgraded to `daily_brief_reviews_20260927`, downgraded to
`agent_ops_20260925`, upgraded again, and cleaned up. The migration is additive;
its downgrade drops the review indexes/table.

## Broad local CI and scans

The full local CI command remains FAILED. Its aggregate was `29 failed, 6,293
passed, 81 skipped, 523 deselected, 3 xpassed`; broad `ruff (changed)`, `black
(changed)`, `isort (changed)`, `mypy (added)`, frontend quality, and tsconfig
ratchets also failed across the inherited Tasks 1–7 branch window. Two Task
8-owned failures originally exposed by CI were fixed and rerun. None of the 29
remaining failures names a Task 8 path. Focused passes do not relabel this gate.

Bandit `1.9.4` scanned `backend/src` under the repository baseline and
configured severity/confidence filters and found no in-scope issue. Staged
Gitleaks `8.30.1` found no source or docs/evidence secret. These PASS results are
separate from dependency outcomes.

`pnpm audit --json` evaluated 1,900 dependencies and FAILED with 21 advisories:
2 critical, 9 high, 8 moderate, 2 low. No advisory was muted. `pip-audit
-r backend/requirements.txt --format json` FAILED with 5 vulnerabilities in 4
resolved packages: `torch 2.12.1`, `setuptools 71.1.0` (two advisories),
`lxml 5.4.0`, and `ecdsa 0.19.2`. No vulnerability was ignored.

Authenticated Safety `3.8.1` is BLOCKED because `SAFETY_API_KEY` is absent. The
anonymous legacy fallback is separately FAILED: one active `gunicorn 22.0.0`
finding and 129 findings automatically ignored by its unpinned-requirement
policy. The fallback does not substitute for authenticated Safety.

## Release control and rollback

Production configuration inspection remains NOT RUN. The exact configuration
name is `DAILY_RESEARCH_BRIEF_ENABLED`; source default is `false`.

Authorized enable sequence:

1. Keep the flag false while deploying the backend and additive migration.
2. Resolve dependency findings and run authenticated Safety.
3. Run live-model baseline/candidate evaluation with the same configured model.
4. Obtain required remote checks for the exact candidate SHA.
5. Inspect production owner-only access and confirm existing-run audit/export.
6. Set the flag true, apply the backend configuration, then smoke
   list/detail/create/start and read/export.

Disable-first rollback:

1. Set `DAILY_RESEARCH_BRIEF_ENABLED=false` and apply that configuration before
   application or database rollback.
2. Confirm list/detail/create/start hide or refuse Daily Brief.
3. Confirm existing persisted runs remain readable/exportable.
4. Revert application code if required; retain the additive review table until
   the normal migration rollback window.

No production flag value, deployment, telemetry query, alert, or rollback drill
was inspected or changed. No push or pull request was made. The feature stays
disabled for this release decision.

## Evidence artifacts

The committed evidence directory is
`docs/testing/evidence/daily-research-brief-task8-followup-20260928/`. Its index
explains source provenance. Bounded transcripts include exact source/candidate
state, command, exit status, and salient output. Retained raw captures also have
SHA-256 digests; the identifier-safety expected RED is explicitly marked as the
one bounded terminal capture without a separate raw file. Evidence covers:

- three baseline-equivalent performance runs on `a92fcfa` and the prior
  whole-feature performance run on `9ca9d6a`;
- simultaneous stream-claim mutation RED/restored GREEN;
- terminal-cancellation mutation RED/restored GREEN;
- exact-source 10-scenario browser passes on `9ca9d6a`, `ac17ca15`, and final
  `b334ec798`,
  plus prior failed/excluded attempts;
- provenance-fallback and identifier-safety RED/restored GREEN; and
- affected/backend/PostgreSQL/frontend/type/OpenAPI/lint/Bandit/Gitleaks checks,
  with final identifier-safety checks bound to `b334ec798`.

Older ignored logs under the SDD task directory are supplemental mutable local
records. They are not the sole basis of any final-source claim.

The ship gate remains closed until every FAILED, BLOCKED, and release-critical
NOT RUN item is cleared on one exact release candidate.

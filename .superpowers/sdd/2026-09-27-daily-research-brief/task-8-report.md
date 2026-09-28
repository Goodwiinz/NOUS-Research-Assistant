# Task 8 report — Daily Research Brief certification

Date: 2026-09-28

Branch: `codex/daily-research-brief-20260927`

Base: `68cc641938f43b99fa8d00e18822ccf2c62106e8`

Disposition: **complete, with a negative ship decision**

The exact candidate SHA is captured after the local commit rather than placed
inside a file that contributes to that SHA. See `task-8-candidate-sha.log` and
the final handoff.

## Delivered evidence

- Added a real PostgreSQL lifecycle suite covering happy path and all three
  reloadable gates, partial/all provider failures, deduplication, evidence
  levels, no evidence, verification override, reconnect, stale/duplicate/
  conflicting reviews, simultaneous resumes, and owner denials. Result:
  `5 passed`.
- Added a browser-to-API-to-PostgreSQL Playwright suite with ten distinct
  lifecycle scenarios. Result: `10 passed`; 200-row review rendering observed
  p50 `565.1 ms` and p95 `647.4 ms` against the frozen `999.24 ms` ceiling.
- Added a live configured-model eval over controlled external evidence. It
  measures real output readiness and evidence quality rather than inferring
  gate compliance from a template. Result: `BLOCKED` because
  `ANTHROPIC_API_KEY` is absent; no citation threshold or candidate pass is
  claimed.
- Added measured performance gates for all six stages, provider fanout, the
  200-row screen/extract bound, payload and memory, review projection, cold
  hydration, and ten full PostgreSQL lifecycles. Numeric baselines and 20%
  ceilings were frozen before optimization and were not loosened.
- Added the durable verification record at
  `docs/testing/daily-research-brief-verification.md`, including commands,
  exact classifications, performance history, security/dependency outcomes,
  monitoring fields, migration/rollback evidence, and release actions.

## Performance investigation and repair

Initial candidate evidence was unstable: max search failed at `46.135 ms`
against `38.572 ms`, while an immediate rerun barely passed at `37.816 ms`.
Subsequent independent full runs exposed search, hydration, and screen
regressions. Those failures remain in the local evidence; none is relabeled as
a pass.

The max-stage method now uses one complete warmup and 40 measured samples with
GC enabled. Nearest-rank p95 remains unchanged and selects rank 38; a small
deterministic test prevents a future sample-count change from silently making
p95 the maximum again. The numeric thresholds stayed fixed.

The repairs are narrowly scoped and behavior-preserving:

- fixture request logs are cleared after each timed stage so the harness does
  not retain executor/request cycles;
- `StepExecutor` legacy dispatch uses a class-level step-to-method-name map,
  removing the executor's bound-method reference cycle;
- screen/extract build source and end-offset maps instead of repeatedly
  scanning source and coverage lists;
- extract compiles its JSON Schema validator once per stage and reuses it for
  sequential records;
- contract-stage prompt injection and the private prompt-context path copy only
  the top-level mapping while public envelope APIs retain deep-copy isolation;
- discovery skips the quadratic merge scan only for sources with no previously
  seen exact identifier pair in the same public/local partition, and uses the
  original full merge loop for every potentially mergeable source; and
- batch accounting stores stable list snapshots while reusing immutable usage
  metadata entries.

Focused regressions cover original-context immutability, every legacy handler
route, immediate weak-reference reclamation, multipart offsets/order, 200
unique sources, provider bridges/conflicts/arXiv versions/public-local
isolation, reference-algorithm equivalence, one schema compile per stage,
malformed later records, accounting snapshot stability, and exact prompt
hash/isolation. The combined focused backend suite passed `81` tests.

Three consecutive final full performance invocations passed all frozen
ceilings. Max search/screen/extract p95 results were:

1. `17.674 / 36.485 / 110.986 ms`
2. `18.365 / 40.021 / 112.123 ms`
3. `18.465 / 38.524 / 112.266 ms`

Every invocation produced 200 candidate rows, 25 screen batches, 25 extract
batches, ten completed lifecycles, zero duplicate stage rows, and 30 unique
review rows.

## Other local gates

PASS:

- focused frontend: 5 files, 61 tests;
- frontend Node 24 type-check and full Vitest: 315 files, 2,368 tests;
- OpenAPI snapshot and regenerated TypeScript types with no drift;
- disposable PostgreSQL upgrade/downgrade/upgrade;
- Bandit `1.9.4` under the repository baseline; and
- candidate-only Gitleaks `8.30.1` scan.

FAILED:

- full `frontend validate`: repository-wide lint returned 113 errors and 1,961
  warnings before its later phases;
- pnpm audit: 21 advisories across 1,900 dependencies, including 2 critical
  and 9 high; and
- pip-audit: 5 vulnerabilities in 4 resolved packages.

Bandit is a separate source scan and does not offset the failed dependency
audits. The dependency gate remains failed.

BLOCKED:

- live configured-model evaluation because `ANTHROPIC_API_KEY` is absent; and
- authenticated Safety because `SAFETY_API_KEY` is absent.

The anonymous deprecated Safety fallback separately failed with one active
`gunicorn 22.0.0` finding and 129 automatically ignored unpinned findings.

NOT RUN:

- remote branch-rule checks for the candidate SHA, awaiting repository-write
  authorization;
- production configuration inspection and live rollback drill; and
- deploy, template/feature enablement, or any release action, awaiting release
  authorization.

The source has no dedicated Daily Research Brief runtime toggle and the local
template loader exposes the template. Production exposure control is therefore
unproven. No candidate deployment occurred, and the release disposition keeps
the feature disabled.

## Final candidate verification

The final local CI gate is `FAILED`. Its final-tree backend aggregate returned
`29 failed, 6,293 passed, 81 skipped, 523 deselected, 3 xpassed, and 122
warnings`; no
remaining failure names a Task 8 path. The script also failed broad changed-
file/debt gates because its `origin/main` window includes the inherited Tasks
1–7 history, including about 1,455 Python files and 192 files that repo-wide
Black would reformat. Those results remain failures rather than being hidden by
focused passes.

The two Task 8-owned failures exposed by the run were repaired and rerun:

- live-eval prerequisite handling passed nine focused cases and skipped the
  unavailable live call with an explicit `BLOCKED` reason; and
- the ordered workflow/pause regression batch passed 48 tests after aligning
  the assertion with the deliberately content-free pause event contract.

The final full collection also passed all nine eval prerequisite cases,
skipped only the unavailable live-model call, and passed every Task 8 pause
regression. None of its 29 failures names a Task 8 path.

The eval matrix proves missing/fixture credentials and a missing candidate
threshold block before a model call, valid baseline/candidate prerequisites
reach the call boundary, and malformed or out-of-range supplied thresholds
fail. The live-model gate remains `BLOCKED`; no score is claimed.

One browser attempt after the final frontend changes was invalid because the
local Next server lacked the complete offline-auth environment. It is retained
as excluded setup evidence. With the required API base, empty public API URL,
auth-cookie name, backend, Supabase URLs, and anon key verified, the final run
passed all ten scenarios in `2.7m`.

Final review includes `git diff --check`, a candidate-only staged secret scan,
and explicit staging inspection. The post-commit SHA is written to the ignored
evidence log so the committed report does not create a self-referential hash.

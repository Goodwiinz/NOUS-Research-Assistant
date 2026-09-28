# SDD ledger — plan: docs/superpowers/plans/2026-09-27-daily-research-brief.md

## Authority and workspace

- Spec: `docs/superpowers/specs/2026-09-27-daily-research-brief-design.md` (approved by the user's “Code now” instruction).
- Plan: `docs/superpowers/plans/2026-09-27-daily-research-brief.md`.
- Worktree: `/home/clawdbot/rag-clean/.worktrees/daily-research-brief-20260927`.
- Branch: `codex/daily-research-brief-20260927`.
- Starting commit: `3f154af1c25a46e643f82378958be1a2ceeac762`.
- The source worktree `/home/clawdbot/rag-clean/.worktrees/agent-orchestration-repairs-20260925` has unrelated uncommitted repairs. A clean feature worktree was created from its reviewed HEAD so those repairs and the feature remain byte-for-byte isolated and reviewable.
- User-selected implementation/review model: `gpt-5.6-sol` at `max` reasoning for every dispatched task and review.

## Preflight rulings

1. The plan originally named `backend/src/services/research_engine/connectors/discovery.py`; the live module is `backend/src/services/research_engine/discovery.py`. The committed plan was corrected and Task 1 must use the live module.
2. `backend/src/services/research_engine/connectors/registry.py` already exists. Task 1 modifies it; it must not create a parallel registry.
3. The existing exporter is `backend/src/services/research_engine/export_service.py`. The committed plan was corrected; Task 5 extends it and must not create `export.py`.
4. The proposed migration parent `agent_ops_20260925` is the actual revision ID in `20260925_agent_tool_operation_results.py`; Task 2 may use it without editing that parent migration.
5. Research-engine routers are individually exported from `backend/src/api/research_engine/__init__.py` and individually included in `backend/src/main.py`. Tasks 1 and 3 must preserve that pattern for capabilities and reviews.
6. Evaluation thresholds must be frozen from measured baselines. The unmeasured 95% citation threshold was removed from the plan; correctness invariants remain exact.
7. Repository push, remote-check creation, publishing, deployment, and flag enablement are outside the current coding authorization. Task 8 records existing remote evidence if any and marks missing external actions `NOT RUN` or `BLOCKED`; it must not push or deploy.
8. No task text mandates an assertion-free test, duplicated implementation block, or another defect identified by the review rubric.
9. Task 1's broader compatibility run found `backend/tests/unit/services/test_paper_discovery.py` asserting the retired emitted values `excerpt`/`metadata`. Because the Task 1 contract requires new outputs to emit `workspace_document`/`metadata_only` while accepting legacy values only on read, that directly affected test is controller-approved Task 1 scope and must be updated and committed with the change.

## Cross-task conflict scan

| Tasks | Producer → consumer / shared surface | Finding and ruling |
|---|---|---|
| 1 → 2 | `research_engine.py`; template, connector normalization, canonical hashing feed trusted template expansion and scope | Compatible. Task 2 extends schemas and consumes Task 1 helpers without changing the v1 template contract. |
| 1 → 3 | `research_engine.py`, router exports/main includes; canonical stage hash feeds review authorization | Compatible. Task 3 adds a separate router and uses the one canonical hash. |
| 1 → 4 | `contracts.py`, `research_engine.py`; hashes/capabilities feed lifecycle and prompt context | Compatible. Task 4 may extend contracts but cannot introduce another serializer/hash. |
| 1 → 5 | `report_rendering.py`, `research_engine.py`; evidence normalization/hashes feed artifact reconstruction | Compatible. Task 5 extends the existing renderer/exporter and preserves Task 1 evidence aliases. |
| 1 → 6 | Template-detail/capabilities schema and routes feed generated contracts and setup UI | Compatible. Task 6 consumes regenerated types; no handwritten duplicate wire contract. |
| 1 → 7 | Canonical step/evidence vocabulary feeds persisted run rendering | Compatible. Task 7 renders the same six step types and evidence labels. |
| 1 → 8 | Template/hash/provider bounds are exercised by integration/eval/performance evidence | Compatible. Task 8 validates rather than changes the contract except for demonstrated regressions. |
| 2 → 3 | `research_engine.py`; append-only review model and owner fields feed review service | Compatible. Task 3 owns decision semantics; Task 2 owns persistence/schema foundations. |
| 2 → 4 | `runs.py`, `research_engine.py`; confirmed scope/model feed lifecycle | Compatible. Task 4 moves state mutation behind the lifecycle service while preserving trusted expansion. |
| 2 → 5 | `runs.py`, `research_engine.py`; template/scope/review persistence feed exports | Compatible. Task 5 reconstructs only from server-persisted state. |
| 2 → 6 | Template detail, start request, scope confirmation feed generated types/UI | Compatible. Task 6 must use generated aliases. |
| 2 → 7 | Persisted template/scope/run state feeds hydration/results | Compatible. Task 7 does not create a second server-state cache. |
| 2 → 8 | Migration/ownership/scope behaviors feed PostgreSQL and browser coverage | Compatible. Task 8 proves reversible migration and owner-only access. |
| 3 → 4 | `research_engine.py`, `review_service.py`; approved overlays feed resume/lifecycle | Compatible. Task 4 extends the service and keeps original step output immutable. |
| 3 → 5 | Review ledger/overlays feed deterministic artifact reconstruction | Compatible. Artifact contains review IDs and decisions; no client content is trusted. |
| 3 → 6 | Review endpoints/schemas feed OpenAPI/client types | Compatible. Generated types remain the single wire contract. |
| 3 → 7 | Pending/submit review API feeds `ReviewPanel` | Compatible. Server exact-set validation remains authoritative over local drafts. |
| 3 → 8 | Stale/replay/conflict/ownership behavior feeds integration/browser tests | Compatible. Task 8 adds race evidence without rewriting review semantics. |
| 4 → 5 | `runs.py`, `research_engine.py`, `step_executor.py`; lifecycle outcomes feed export/final state | Compatible. Export is model-free and final approval binds the exact persisted artifact. |
| 4 → 6 | Typed resume and pause descriptor feed generated contracts/UI service | Compatible. Direct SSE may not authorize continuation. |
| 4 → 7 | Run pause state/resume rules feed hydration and outcome UI | Compatible. Persisted state is primary and SSE is notification only. |
| 4 → 8 | Atomic transitions/concurrency feed PostgreSQL/browser/soak tests | Compatible. PostgreSQL absence is reported as blocked, never replaced with SQLite evidence. |
| 5 → 6 | Export route/enums feed OpenAPI and client service | Compatible. Task 6 regenerates both contract artifacts together. |
| 5 → 7 | Outcome-safe export endpoint feeds `RunResults` downloads | Compatible. Browser never generates report content itself. |
| 5 → 8 | Artifact hashes, CSV safety, and observability feed deterministic/eval/security evidence | Compatible. Task 8 validates exact reconstruction and content-safe logs. |
| 6 → 7 | Shared `StepCard.tsx` and generated service contract feed run/review UI | Compatible. Task 7 builds on Task 6's six-type rendering and must preserve setup behavior. |
| 6 → 8 | Setup UI and public contracts feed browser lifecycle coverage | Compatible. Task 8 tests the real browser/API/database boundary. |
| 7 → 8 | Hydration/review/results UI feed reload/reconnect/outcome browser cases | Compatible. Task 8 may repair only a demonstrated regression. |

## Per-task self-consistency scan

| Task | Tests vs code, create/modify sequencing, internal consistency | Ruling |
|---|---|---|
| 1 | Tests cover template, helpers, registry, and route. Two stale file declarations were corrected before commit. | Execute with existing registry and live discovery module. |
| 2 | Model/migration/schema/service/routes align; migration parent exists; review service intentionally follows in Task 3. | Execute. Do not edit the parent migration. |
| 3 | Service/router/schema tests match exact-set, owner-only, replay/conflict behavior. | Execute after Task 2. |
| 4 | Unit plus PostgreSQL coverage matches lifecycle, overlay, prompt, and stream changes. It consumes Task 3 review state and is the only mutation boundary. | Execute. A clean worktree resolves the prior dirty integration-test collision. |
| 5 | Tests match exporter/renderer/observability/routes. Stale exporter filename was corrected. | Execute by extending `export_service.py`; no parallel report model. |
| 6 | OpenAPI/type generation and UI/service tests align. Generated files are clean in this isolated worktree. | Execute after all backend routes/schemas stabilize. |
| 7 | Store/component tests match persisted-first hydration, review UI, and server downloads. `StepCard.tsx` is intentionally extended after Task 6. | Execute while preserving Task 6 setup tests. |
| 8 | Evidence tests cover lifecycle and release gates. Original unmeasured threshold and unauthorized push language were corrected. | Execute local evidence fully; mark credentialed/external gates truthfully when unavailable or unauthorized. |

## Task progress

- Task 1: complete
- Task 2: complete
- Task 3: complete
- Task 4: complete
- Task 5: complete
- Task 6: complete
- Task 7: pending
- Task 8: pending

## Task 1 review loop

- Base: `3f154af1c25a46e643f82378958be1a2ceeac762`
- Implementation commit: `d340f4fa1123e48ceb2cc41fccdd20c41446a380`
- Review round 1: spec compliant; quality needs fixes.
- Important finding: `build_connectors` weakened the `rag_search` contract to `Callable[..., object]` even though `RagStoreConnector` awaits an async mapping result. Restore the awaitable mapping type and its test type; add a focused type/behavior check if needed.
- Minor finding: focused test output carries a pre-existing Pydantic warning. Track it as baseline evidence unless the changed code owns the warning.
- Reviewer ⚠️: broad-suite failures were claimed unrelated without base evidence. Controller will run the same broad command at the base commit in an isolated detached worktree and compare counts/failure surfaces.
- Reviewer ⚠️ resolved: the identical broad command at base `3f154af1c` produced `32 failed, 5964 passed, 80 skipped, 3 xpassed, 149 warnings, 4 errors`; Task 1 produced `32 failed, 5994 passed, 80 skipped, 3 xpassed, 145 warnings, 4 errors`. The same failure/error count and listed failure surfaces were present at base, while Task 1 adds 30 passes. Evidence: `task-1-base-suite.log` and `task-1-report.md`.
- Fix round 1 commit: `5374a11cbcf89b4e817f264a3f05f2712f95e46c` (`fix(research): restore rag search callback contract`).
- Scoped re-review: async callback finding ADDRESSED; no new Critical/Important breakage; no out-of-scope observations.
- Minor warning resolved as pre-existing baseline: base emitted 149 warnings and Task 1 emitted 145 under the same broad command, so Task 1 did not introduce the reported warning class.
- Task 1: complete

## Task 2 review loop

- Base: `5374a11cbcf89b4e817f264a3f05f2712f95e46c`
- Implementation commit: `e2bb58ab6` (`feat(research): persist daily brief scope and review schema`).
- Focused GREEN: 134 passed; Task 1 adjacency: 58 passed; Alembic guard: one head `daily_brief_reviews_20260927`, 72 revisions, all IDs <=32 chars.
- Review round 1: no Critical findings; two Important validation findings and two Minor hardening findings.
- Important: coercive Pydantic values allowed `confirmed: 1` and non-integer encodings for `limit_per_provider`.
- Important: non-string provider elements could raise uncaught `TypeError` and return 500.
- Minor: timezone-naive audit default and migration test collapsing both user foreign keys.
- Fix round 1 commit: `9efd2e0cc456910fd94baba3c1314505f673d41b` (`fix(research): harden daily brief request validation`).
- Fix verification: 145 passed; Ruff, Black, isort, MyPy, and diff checks passed.
- Scoped re-review: all four findings ADDRESSED; no new Critical/Important breakage; no out-of-scope observations.
- Task 2: complete

## Task 3 review loop

- Base: `9efd2e0cc456910fd94baba3c1314505f673d41b`
- Implementation commit: `6f179f3f7fb02872ece369a61d99ed06f4dd3cd3` (`feat(research): add exact-hash stage reviews`).
- Review round 1 found one Critical validation-content leak and three Important gaps: ambiguous all-unresolved extraction payloads, oversized pending output failures, and mocked rather than real PostgreSQL race evidence.
- Fix commit: `279918289edf9fb31089978e12d6ee6a4d210aae` (`fix(research): harden stage review boundaries`).
- Fix verification: 39 focused unit/API tests, 2 real PostgreSQL race tests, and 140 adjacent tests passed; Ruff, Black, isort, targeted MyPy, and diff checks passed.
- Scoped re-review: every prior finding ADDRESSED; shared-lock decline races preserve correctness; no new Critical or Important regressions.
- Task 3: complete

## Task 4 review loop

- Base: `279918289edf9fb31089978e12d6ee6a4d210aae`.
- Implementation commit: `3f4647da25fe1a7388f82df68d4d7e7003d90549` (`feat(research): enforce durable review lifecycle`).
- Review round 1 found six Important gaps: pre-execution claim restoration, duplicated prompt source content, placeholder rather than exact stage schemas, content-bearing/wrong-priority pause events, unreachable empty-stage `no_evidence`, and missing durable manual-pause descriptors.
- Fix commit: `fix(research): close lifecycle review gaps` (this commit).
- Fix verification: 70 focused tests and the final 88-test Task 4/PostgreSQL gate passed; 180 adjacent tests and 2 real PostgreSQL race tests passed. Ruff, Black, isort, and diff checks passed. Direct MyPy comparison is unchanged at 44 legacy errors on both base and fix, with zero patch-introduced errors.
- Task 4: complete; Task 5 has not started.

## Task 5 review loop

- Base: `c1608645219634ac7da8519bd82e669d21c0557d`.
- Controller-approved seam: `contracts.py` may add only `verification_output_hash` and `report_hash` to the export-stage owned/rehydrated fields because Task 3 final approval already validates both bindings; undeclared export keys remain rejected.
- Initial RED: the exact three-file Task 5 command stopped with 3 collection errors for the missing enum, artifact type, and observability module.
- Review-gap RED/GREEN: explicit evidence CSV and pause/override metrics failed 2 focused tests before implementation and passed 4 afterward; self-review then proved rejected CSV audit rows were lost (1 RED) before the immutable audit-context repair (1 GREEN).
- Final focused GREEN: 27 passed; report-rendering contracts: 35 passed; adjacent Task 3/4 tests: 93 passed; full stream regression: 31 passed.
- Quality gates: changed-file and repo-wide Ruff passed; Black and isort passed; three added files are MyPy-clean; all seven modified legacy files match their base diagnostic counts; diff check passed.
- Task 5: complete; Task 6 has not started.
- Review round 2 found seven Important gaps: exact persisted Markdown bytes,
  fail-closed reconstruction, durable verified trust evidence, deterministic
  extraction-owned CSV review projection, complete post-resume provenance,
  production observability wiring, and content-free generic SSE/errors.
- Fix round 2 used direct RED regressions for all seven findings plus focused
  self-review cases for empty artifacts, legacy reconstruction, malformed
  telemetry, ISO timestamps, replay/duplicate metrics, and actual-envelope
  hashes. The controller authorized the narrowly required engine, discovery,
  review, lifecycle, route, and related test surfaces; the existing
  `contracts.py` export-hash seam was not broadened.
- Fix round 2 commit: `fix(research): harden audited brief exports` (this
  commit).
- Fix verification: Task 5 `36 passed`; Task 3/4/template adjacency `96
  passed`; stream/security `33 passed`; workflow `26 passed`; discovery `15
  passed`; deterministic rendering `9 passed`. Changed-file and repo-wide Ruff,
  Black, isort, and diff checks passed. The 13-file MyPy comparison is unchanged
  at `144` legacy diagnostics on both base `43fdac2b5` and the repair, with zero
  introduced diagnostics.
- Scoped self-review: all seven findings ADDRESSED; no unresolved Critical or
  Important issue; Task 6 has not started.
- Review round 3 found two remaining Important gaps: contiguous non-object
  persisted outputs were skipped during reconstruction, and final approval did
  not require a canonical persisted Markdown payload even though verified
  downloads must serve its exact bytes.
- Fix round 3 started from `9c120f85eaf71c4f28df1324b6b908e1af20f91a`.
  Its focused RED was `18 failed`; focused GREEN was `18 passed`. The final
  Task 5 gate passed `49`, Task 3/4/template adjacency passed `102`, workflow
  passed `26`, stream/security passed `33`, discovery passed `15`, and
  deterministic rendering passed `9`.
- Fix round 3 keeps explicit `{}` output as a tested legacy no-op, rejects all
  non-object output with `export_reconstruction_failed`, and makes final review
  plus verified download trust require matching string `markdown`/`content`
  fields under `format=markdown`. Exact empty-string bytes remain supported.
- Fix round 3 quality gates: changed-file and repo-wide Ruff passed; Black and
  isort passed; MyPy is unchanged at `18` diagnostics on both base and repair;
  diff check passed. Commit: `fix(research): fail closed on audited export
  corruption` (this commit). Task 6 has not started.
- Review round 4 found one remaining Important parser-boundary gap: malformed
  canonical markers could be treated as legacy success or leak an unhashable
  `stage_type` as raw `TypeError`.
- Fix round 4 starts from `cacd70946f65325e6897220abd8bc299b673f707`.
  Its 14-case matrix moved from `10 failed, 4 passed` to `14 passed`; final
  gates are Task 5 `63`, Task 3/4/template `102`, and stream/security `33`, all
  passing. Ruff, Black, isort, diff, and unchanged `14`-diagnostic MyPy delta
  checks pass. Commit: `fix(research): validate persisted stage markers` (this
  commit). Task 6 has not started.

## Task 6 implementation

- Base: `0e5b0665a26e64509b93f29cfdb6701a4cc849c9`.
- Initial RED: the new OpenAPI contract tests failed `2/2`; the new frontend
  Task 6 batch failed `10` tests and passed `14` before implementation.
- Generated `backend/openapi.json` and
  `frontend/src/types/generated/api.d.ts` with the repository generators; the
  final API drift check passes.
- Added generated schema aliases and typed research-engine service methods,
  full-detail template application, capability-backed source selection, the
  bounded Daily Brief scope/confirmation form, template-source invalidation,
  and the exact six-step backend vocabulary.
- Focused GREEN: backend OpenAPI `2 passed`; frontend Task 6 `27 passed` across
  three files. Adjacent GREEN: frontend research-engine/service `30 passed` and
  backend endpoint/template/schema/review/export `121 passed`.
- Full frontend regression: `311 passed` files and `2325 passed` tests.
- TypeScript type-check, API type drift, ESLint, Prettier, Ruff, Black, isort,
  and diff checks pass. Task 7 has not started.
- The local CI changed-file, contract, migration, NOUS, and frontend ratchet
  gates pass. Its repo-wide backend phase retains `30` unrelated failures with
  `6264` passes, including unavailable/misconfigured PostgreSQL and Redis
  surfaces; no failure is in a Task 6 changed path.
- Review round 1 found one Important stale-target gap: after a confirmed
  persisted Daily Brief topology was edited, clearing its template marker also
  removed the scope guard while the old persisted blueprint ID remained
  runnable.
- Fix round 1 started from `02cbdf54af3fe9cbd67e13ff09bbc9a863973f90`.
  Its combined editor regression was RED at `1 failed, 6 passed` and GREEN at
  `7 passed`. Unsaved topology now blocks both the Start control and handler;
  rejected saves preserve the block, and only a successful custom-blueprint
  save rebinds the runnable blueprint.
- Fix verification: Task 6 frontend `28 passed`; adjacent research-engine
  frontend `31 passed`; TypeScript, API type drift, changed-file ESLint,
  Prettier, and diff checks pass. Commit: `fix(research-ui): block stale
  blueprint runs` (this commit). Task 7 remains untouched.
- Task 6: complete.

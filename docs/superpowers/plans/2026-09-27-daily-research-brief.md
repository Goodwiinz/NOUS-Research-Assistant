# Daily Research Brief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded, reload-safe Daily Research Brief workflow with confirmed scope, durable human review gates, exact-hash continuation, honest verification status, and provenance-complete Markdown/JSON/CSV exports.

**Architecture:** Keep the existing `WorkflowEngine`, `StepExecutor`, typed stage envelopes, and persisted `ResearchStep` rows as the sole workflow/evidence model. Add server-owned template expansion, one canonical output hash, an append-only review ledger, and a lifecycle service that makes persistence/pause/review/resume transitions atomic; the frontend hydrates persisted state first and treats SSE as notification through the existing research-engine store.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy async, Alembic/PostgreSQL JSONB, pytest; Next.js 16, React 18, TypeScript, Zustand, Vitest/Testing Library, Playwright; Node 24.x, pnpm 10.18.2.

**Spec:** `docs/superpowers/specs/2026-09-27-daily-research-brief-design.md`

## Global Constraints

- Treat the supplied design as approved for this work, despite the stale status line in the file.
- Work only in `/home/clawdbot/rag-clean/.worktrees/daily-research-brief-20260927` on `codex/daily-research-brief-20260927`, created from the reviewed `codex/agent-orchestration-repairs-20260925` HEAD after this plan is committed. The source worktree's unrelated dirty repairs must remain untouched. In the feature worktree, inspect `git status --short`, stage only named task paths, and never use broad add/reset/checkout commands.
- Valid step types remain exactly `search`, `screen`, `extract`, `synthesize`, `verify`, and `export`. Do not add a second engine, evidence model, renderer, or overlapping frontend server-state cache.
- Daily Brief contract version is `1`; topology is `search → screen → extract → synthesize → verify → export`; defaults are `openalex,crossref` and `25`; provider bounds are 1–4 and 1–50/provider; gates are screening/extraction/final; formats are markdown/json/csv; `coverage.exhaustive` is false.
- Original `ResearchStep.output` is immutable. Review decisions are append-only overlays authorized by a canonical hash of the complete persisted envelope, including `contract_version`.
- Version 1 is owner-only through `ResearchProject.owner_id`; organization membership never widens access. Absent and inaccessible resources use the same 404.
- Routes are transport adapters; the lifecycle/review/export services own their transactions. SSE is emitted only after durable commit.
- Logs, errors, and metrics may contain IDs, kinds, counts, durations, and status, but never questions, criteria, abstracts, quotations, extraction content, prompts/responses, review notes, or report bodies.
- `backend/openapi.json` and `frontend/src/types/generated/api.d.ts` are regenerated together and never hand-edited.
- Legacy blueprints/runs keep their current behavior. Missing scope/reviews cannot be presented as an approved Daily Brief. Rollback disables template selection first and leaves the additive table until the normal migration rollback window.

## Review Focus

- Provider aliases, duplicates, ineligible IDs, over-four selections, and limits over 50 must fail before a paid call (Task 1).
- Stale/concurrent review tabs must produce stale/conflict/replay outcomes without overwriting history (Tasks 3 and 8).
- Reloads, disconnects, and direct stream requests must never bypass a durable gate or verification failure (Tasks 4 and 8).
- Zero screened inclusions or all rejected extractions must end `no_evidence` without later model calls/final approval (Tasks 4, 5, and 8).
- CSV formula prefixes/oversized cells and legacy evidence labels must remain safe/readable (Tasks 1 and 5).

## Shared Interfaces

Use these names consistently:

```python
# backend/src/services/research_engine/contracts.py
def canonical_json_bytes(value: object) -> bytes: ...
def canonical_json_sha256(value: object) -> str: ...
def canonical_stage_output_hash(output: dict[str, object]) -> str: ...
def normalize_evidence_level(value: str | None) -> str: ...

# backend/src/services/research_engine/connectors/registry.py
@dataclass(frozen=True)
class ConnectorCapability:
    connector_id: str
    label: str
    daily_brief_eligible: bool
    full_text: bool
    date_filter: bool
    cursor: bool
    aliases: tuple[str, ...] = ()

def safe_capability_projection() -> list[dict[str, object]]: ...
def normalize_connector_selection(ids: Sequence[str], *, daily_brief_only: bool) -> tuple[str, ...]: ...
def build_connectors(rag_search: Callable[..., object], connector_ids: Sequence[str] | None = None) -> dict[str, SourceConnector]: ...

# backend/src/services/research_engine/scope.py
def resolve_effective_daily_brief_parameters(defaults: Mapping[str, object], overrides: Mapping[str, object]) -> dict[str, object]: ...
def canonicalize_scope_confirmation(*, effective: Mapping[str, object], submitted: DailyBriefScopeConfirmation, actor_id: UUID, confirmed_at: datetime) -> dict[str, object]: ...

# backend/src/services/research_engine/run_lifecycle.py
@dataclass(frozen=True)
class PauseDescriptor:
    pause_reason: Literal["user_paused", "review_required", "verification_failed"]
    review_kind: ReviewKind | None
    step_index: int
    output_hash: str

class ResearchRunLifecycleService:
    async def persist_step_completion(...) -> PersistedStepTransition: ...
    async def authorize_resume(...) -> ResumeTransition: ...
    async def claim_stream(...) -> StreamClaim: ...
```

Canonical JSON is UTF-8 with sorted keys, compact separators, `ensure_ascii=False`, and `allow_nan=False`. The manifest uses bounded keys `scope_confirmation`, `provider_manifest`, `stage_hashes`, `pending_review`, `review_history`, `resume_authorization`, `verification_override`, `final_status`, and `artifact`. Stable conflict codes are `review_required`, `review_output_stale`, `review_decision_conflict`, `review_payload_incomplete`, `verification_override_required`, `verification_output_stale`, `run_not_completed`, and `brief_not_available_no_evidence`.

---

### Task 1: Template Contract, Canonical Hash, and Safe Connector Capabilities

**Files:**
- Create: `backend/src/services/research_engine/blueprints/templates/daily_research_brief.yaml`
- Modify: `backend/src/services/research_engine/connectors/registry.py`
- Create: `backend/src/api/research_engine/capabilities.py`
- Modify: `backend/src/services/research_engine/blueprints/loader.py`
- Modify: `backend/src/services/research_engine/contracts.py`
- Modify: `backend/src/services/research_engine/discovery.py`
- Modify: `backend/src/services/research_engine/connectors/__init__.py`
- Modify: `backend/src/services/research_engine/report_rendering.py`
- Modify: `backend/src/api/research_engine/__init__.py`
- Modify: `backend/src/main.py`
- Modify: `backend/src/schemas/research_engine.py`
- Test: `backend/tests/unit/services/test_blueprint_loader.py`
- Test: `backend/tests/unit/services/test_research_template_contracts.py`
- Test: `backend/tests/unit/services/test_research_connector_registry.py`
- Test: `backend/tests/unit/api/test_research_engine_capabilities.py`

**Interfaces:** Produces the template, the hash/evidence helpers, `ConnectorCapability`, safe projection/normalization/build functions, and `GET /api/v1/research-engine/capabilities` with `id,label,daily_brief_eligible,available,features.{full_text,date_filter,cursor}` only.

- [ ] Write failing tests for the exact v1 YAML topology/defaults/gates/formats; all bundled templates validating before list/load; key-order-independent and contract-sensitive hashes; NaN rejection; new evidence vocabulary `full_text|abstract|metadata_only|workspace_document` with legacy `metadata|excerpt` read aliases; and connector alias/duplicate/eligibility/cardinality safety. Assert `web`, aliases, URLs, secrets, and config never appear in capability JSON.
- [ ] Run RED: `PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_blueprint_loader.py backend/tests/unit/services/test_research_template_contracts.py backend/tests/unit/services/test_research_connector_registry.py backend/tests/unit/api/test_research_engine_capabilities.py` (expected failures: missing template/registry/route/helpers).
- [ ] Implement the minimal contract. Make registry feature flags reflect behavior implemented today; keep `web` internal for legacy normalization only. Invalid bundled YAML fails the focused contract test/startup path rather than reaching the editor.
- [ ] Run GREEN with the RED command plus existing contract/determinism tests: `PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_determinism_golden.py backend/tests/unit/services/test_workflow_engine.py`.
- [ ] Commit only listed paths: `git commit -m "feat(research): add daily brief template capabilities"`.

### Task 2: Review Migration, API Schemas, Confirmed Scope, and Trusted Template Expansion

**Files:**
- Create: `backend/src/models/research_stage_review.py`
- Create: `backend/alembic/versions/20260927_daily_research_brief_reviews.py`
- Create: `backend/src/services/research_engine/scope.py`
- Modify: `backend/src/models/__init__.py`
- Modify: `backend/src/models/research_run.py`
- Modify: `backend/src/schemas/research_engine.py`
- Modify: `backend/src/api/research_engine/blueprints.py`
- Modify: `backend/src/api/research_engine/runs.py`
- Test: `backend/tests/unit/models/test_research_models.py`
- Test: `backend/tests/unit/ci/test_daily_research_brief_migration.py`
- Test: `backend/tests/unit/schemas/test_research_engine_schemas.py`
- Test: `backend/tests/unit/services/test_daily_brief_scope.py`
- Test: `backend/tests/unit/api/test_research_template_security.py`
- Test: `backend/tests/unit/api/test_research_engine_endpoints.py`

**Interfaces:** Produces append-only `ResearchStageReview`; `DailyBriefScopeConfirmation`; optional `RunCreate.scope_confirmation`; `RunResumeRequest`; `BlueprintTemplateDetailResponse`; full `GET /blueprints/templates/{slug}`; and server-owned template creation.

- [ ] Write failing migration/model tests for fields `id,owner_id,organization_id,run_id,step_index,stage_type,review_kind,reviewer_id,output_hash,decision,decision_payload,note,created_at`; indexes on `(run_id,step_index)`, owner, reviewer, org; unique `(run_id,step_index,output_hash,review_kind)`; FK/reversible upgrade; one Alembic head with `down_revision="agent_ops_20260925"`. The append-only model inherits `Base`, not soft-delete `BaseModel`.
- [ ] Write failing schema/start/template tests: question 1–2000; 1–25 inclusion and 0–25 exclusion entries max 500; 1–4 canonical providers; limit 1–50; notes max 2000; `confirmed=True`; client hash/extra fields forbidden. Daily Brief start merges only declared overrides, verifies submitted values equal effective values, and stores server hash/actor/time; legacy start remains unchanged. Template detail returns validated full content; known template source rejects client reorder/gate removal/extra stages; custom topology clears `template_source`; saved concrete steps do not drift with YAML edits.
- [ ] Run RED: `PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/models/test_research_models.py backend/tests/unit/ci/test_daily_research_brief_migration.py backend/tests/unit/schemas/test_research_engine_schemas.py backend/tests/unit/services/test_daily_brief_scope.py backend/tests/unit/api/test_research_template_security.py backend/tests/unit/api/test_research_engine_endpoints.py`.
- [ ] Implement the additive model/revision, canonical scope service, detail route, and trusted server expansion. Do not edit the existing dirty `20260925_agent_tool_operation_results.py` migration.
- [ ] Run GREEN with the RED command and the repository's Alembic single-head test. Commit only listed paths with `git commit -m "feat(research): persist daily brief scope and review schema"`.

### Task 3: Exact-Set Review Service and Owner-Scoped Review APIs

**Files:**
- Create: `backend/src/services/research_engine/review_service.py`
- Create: `backend/src/api/research_engine/reviews.py`
- Modify: `backend/src/schemas/research_engine.py`
- Modify: `backend/src/api/research_engine/__init__.py`
- Modify: `backend/src/main.py`
- Test: `backend/tests/unit/services/test_research_review_service.py`
- Test: `backend/tests/unit/api/test_research_engine_reviews.py`

**Interfaces:** Produces `ReviewKind`, `ReviewDecision`, typed screening/extraction item decisions, `StageReviewRequest/Response`, `PendingReviewResponse`, and `ResearchReviewService.get_pending_review`, `.submit_review`, `.apply_approved_overlays` for `GET /runs/{run_id}/reviews/pending` and `POST /runs/{run_id}/reviews/{step_index}`.

- [ ] Write failing tests for screening `include|exclude|unresolved` and extraction `accept|reject|unresolved`; exact persisted item set once each; exclusion/rejection reason required/max 500; no injected source/citation/text; matching kind/index/contract/hash; final approval only for a verified export envelope binding verification/report hashes. Cover owner 404, same-org-other-owner 404, stale hash 409/current content-free descriptor, identical replay returning original row, different replay conflict, decline recorded while paused, and unchanged `ResearchStep.output`.
- [ ] Run RED: `PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_research_review_service.py backend/tests/unit/api/test_research_engine_reviews.py`.
- [ ] Implement one locked transaction: load through project ownership, recompute persisted hash, validate descriptor/item set, canonicalize payload, insert the ledger row, and mark pending state approved only for approval. On unique-race reload, compare canonical decisions for replay vs conflict. Pending response may contain bounded owned stage output; error/descriptor responses contain no research content.
- [ ] Run GREEN with the RED command. Commit listed paths with `git commit -m "feat(research): add exact-hash stage reviews"`.

### Task 4: Atomic Lifecycle, Review Overlays, Resume Authorization, and Engine Context

**Files:**
- Create: `backend/src/services/research_engine/run_lifecycle.py`
- Create: `backend/src/services/research_engine/prompt_context.py`
- Modify: `backend/src/services/research_engine/contracts.py`
- Modify: `backend/src/services/research_engine/review_service.py`
- Modify: `backend/src/services/research_engine/engine.py`
- Modify: `backend/src/services/research_engine/step_executor.py`
- Modify: `backend/src/api/research_engine/runs.py`
- Modify: `backend/src/schemas/research_engine.py`
- Test: `backend/tests/unit/services/test_research_run_lifecycle.py`
- Test: `backend/tests/unit/services/test_research_review_overlays.py`
- Test: `backend/tests/unit/services/test_research_prompt_context.py`
- Test: `backend/tests/unit/api/test_research_engine_stream.py`
- Test: `backend/tests/integration/test_research_engine_resume_postgres.py`

**Interfaces:** Produces `PauseDescriptor`, `PersistedStepTransition`, `ResumeTransition`, `StreamClaim`, `ResearchRunLifecycleService` methods from Shared Interfaces, `PromptContextBuilder.build(...)`, and optional content-free `RunResponse.pause_reason,review_kind,step_index,output_hash`.

- [ ] Write failing tests proving step envelope/hash/source/token/pending review/status commit atomically; rollback leaves none; duplicate event does not double persist; pause SSE follows commit; reload derives descriptor. Approved overlays affect only downstream inputs and cold rehydrate equals uninterrupted resume; original output/hash stays unchanged. Empty screen inclusion/all extraction rejection complete `no_evidence`, skip later calls/final review, and retain audit.
- [ ] Add failing authorization tests: review resume requires matching approval; decline stays paused; failed verify stores `verification_failed`; only `{continue_unverified:true,output_hash:<exact>}` records actor/time/hash and authorizes unverified export; stale/missing override fails; direct stream/ordinary resume cannot bypass; authorization is one-use; approved final export completes without regeneration. Concurrent claims execute once in PostgreSQL.
- [ ] Add failing prompt tests proving model stages see only confirmed scope, safe selected capabilities, immediate typed envelope/evidence IDs, target schema, and budget. Persist/log prompt version/hash only for Daily Brief; exclude secrets, unrelated prior output, review notes, and prompt/content. Preserve legacy prompt behavior.
- [ ] Run RED: `PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_research_run_lifecycle.py backend/tests/unit/services/test_research_review_overlays.py backend/tests/unit/services/test_research_prompt_context.py backend/tests/unit/api/test_research_engine_stream.py backend/tests/integration/test_research_engine_resume_postgres.py`.
- [ ] Implement the lifecycle service as the only mutation boundary; keep `WorkflowEngine` as the sole orchestrator and existing typed envelopes as handoffs. Stream claims consume a POST-created authorization under lock; SSE only serializes committed state.
- [ ] Run GREEN with RED command plus `backend/tests/unit/services/test_workflow_engine.py` and `backend/tests/unit/services/test_determinism_golden.py`. If PostgreSQL is unavailable, record BLOCKED rather than substituting SQLite. Commit listed paths with `git commit -m "feat(research): enforce durable review lifecycle"`.

### Task 5: Deterministic Artifacts, Downloads, and Content-Safe Observability

**Files:**
- Create: `backend/src/services/research_engine/observability.py`
- Modify: `backend/src/services/research_engine/export_service.py`
- Modify: `backend/src/services/research_engine/report_rendering.py`
- Modify: `backend/src/services/research_engine/step_executor.py`
- Modify: `backend/src/api/research_engine/runs.py`
- Modify: `backend/src/schemas/research_engine.py`
- Test: `backend/tests/unit/services/test_export_service.py`
- Test: `backend/tests/unit/services/test_research_observability.py`
- Test: `backend/tests/unit/api/test_research_engine_exports.py`

**Interfaces:** Produces `ExportFormat(markdown|json|csv)`, `ExportArtifact(content: bytes, media_type: str, filename: str)`, `ExportService.export(run_id, owner_id, format, db)`, and `GET /runs/{run_id}/export?format=...`.

- [ ] Write failing tests that export is model-free and persists the exact reader artifact before final pause; export envelope explicitly binds verification output hash and report hash. JSON contains version/run/blueprint/template/scope/provider/dedupe/stage/review/claim/evidence/verification/model/timestamp/limitations provenance and `verified|unverified|no_evidence`. Markdown preserves unverified failed checks. CSV includes stable source/bibliography/extracted/evidence/decision/reason fields, bounds cells, and prefixes cells beginning `=`, `+`, `-`, tab, or `@` with `'`.
- [ ] Add route tests: owned terminal completed only; legacy completed exports remain readable but never labeled approved Daily Brief; unverified banner survives every format; no-evidence offers audit JSON/CSV and Markdown returns `brief_not_available_no_evidence`; deterministic safe filename never contains question text; cross-owner is 404. Add log/metric capture tests for allowed aggregate fields and forbidden content.
- [ ] Run RED: `PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/services/test_export_service.py backend/tests/unit/services/test_research_observability.py backend/tests/unit/api/test_research_engine_exports.py`.
- [ ] Extend the existing exporter/renderer; do not build a parallel report model. Reconstruct from persisted envelopes, approved overlays, review IDs, and manifest. Add bounded counters/histograms for stage duration, provider outcomes, pause/review/override/final status, and validation errors.
- [ ] Run GREEN with RED command and report-rendering contract tests. Commit listed paths with `git commit -m "feat(research): export audited daily briefs"`.

### Task 6: Regenerate Public Contracts and Build the Daily-Brief Setup UI

**Files:**
- Modify (generated): `backend/openapi.json`
- Modify (generated): `frontend/src/types/generated/api.d.ts`
- Modify: `frontend/src/services/researchEngineService.ts`
- Modify: `frontend/src/components/research-engine/TemplateSelector.tsx`
- Modify: `frontend/src/components/research-engine/BlueprintEditor.tsx`
- Modify: `frontend/src/components/research-engine/SourceSelector.tsx`
- Modify: `frontend/src/components/research-engine/StepCard.tsx`
- Create: `frontend/src/components/research-engine/DailyResearchBriefSetup.tsx`
- Test: `backend/tests/unit/api/test_research_engine_openapi.py`
- Test: `frontend/src/services/__tests__/researchEngineService.test.ts`
- Test: `frontend/src/components/research-engine/__tests__/DailyResearchBriefSetup.test.tsx`
- Test: `frontend/src/components/research-engine/__tests__/BlueprintEditor.test.tsx`

**Interfaces:** `researchEngineService.ts` aliases `components["schemas"][...]` from generated types and exposes `getTemplateDetail`, `getCapabilities`, typed `startRun`, `getPendingReview`, `submitReview`, typed `resumeRun`, and `getRunExportUrl`; no duplicate handwritten wire interfaces.

- [ ] Write failing OpenAPI test for capabilities, template detail, pending review, submit, typed resume, and export; response pause fields; schemas/enums. Write failing frontend tests that template selection fetches full detail before apply, persists `template_source`, capability options are canonical/eligible/available, 1–4 and 1–50 are enforced, custom topology clears source, only six backend step types render, scope confirmation is required, and exact bounded-warning copy is shown.
- [ ] Run RED backend/frontend: `PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/unit/api/test_research_engine_openapi.py` and `corepack pnpm@10.18.2 --dir frontend vitest run src/services/__tests__/researchEngineService.test.ts src/components/research-engine/__tests__/DailyResearchBriefSetup.test.tsx src/components/research-engine/__tests__/BlueprintEditor.test.tsx`.
- [ ] Generate, never edit, contracts: `PYTHONPATH=backend .venv/bin/python scripts/ci/generate_openapi.py` then `corepack pnpm@10.18.2 --dir frontend generate:api-types`. Add small domain adapters only where components need normalized values.
- [ ] Implement setup/editor using the generated service. Display exactly: “This is a bounded brief from the selected sources. It is not an exhaustive or systematic review.”
- [ ] Run GREEN with RED commands, `corepack pnpm@10.18.2 --dir frontend type-check`, and `corepack pnpm@10.18.2 --dir frontend check:api-types`. Commit all listed/generated files together with `git commit -m "feat(research-ui): configure daily research briefs"`.

### Task 7: Hydrate Runs, Render Durable Reviews, and Expose Outcome-Safe Downloads

**Files:**
- Modify: `frontend/src/store/research-engine-store.ts`
- Modify: `frontend/src/components/research-engine/RunView.tsx`
- Modify: `frontend/src/components/research-engine/StepCard.tsx`
- Create: `frontend/src/components/research-engine/ReviewPanel.tsx`
- Create: `frontend/src/components/research-engine/RunResults.tsx`
- Test: `frontend/src/store/__tests__/research-engine-store.test.ts`
- Test: `frontend/src/components/research-engine/__tests__/RunView.test.tsx`
- Test: `frontend/src/components/research-engine/__tests__/ReviewPanel.test.tsx`
- Test: `frontend/src/components/research-engine/__tests__/RunResults.test.tsx`

**Interfaces:** Existing store gains `hydrateRun(run, steps)`, `mergeRunEvent(event)`, `setPendingReview(review)`, and `resetRun(runId)`; persisted identity is step `id`, with `(run_id,step_index)` fallback until an event is reconciled. The store remains the single owner for this run state.

- [ ] Write failing store/RunView tests: fetch `getRun` and `listSteps` before opening SSE; render persisted steps immediately; merge reconnect/duplicate/out-of-order events idempotently; run switch clears prior state; missed pause reconstructs user/review/verification reason from RunResponse; paused review fetches pending data; stale 409 refreshes; unsaved local choices are never claimed durable.
- [ ] Write failing review/result tests: screening/extraction show original records/evidence level, unresolved blocks approval, exclusions/rejections require reasons, final panel is impossible after override/failure, decline remains paused, explicit exact-hash unverified action is separate from ordinary resume, and verified/unverified/no-evidence states/download choices are distinct and accessible.
- [ ] Run RED: `corepack pnpm@10.18.2 --dir frontend vitest run src/store/__tests__/research-engine-store.test.ts src/components/research-engine/__tests__/RunView.test.tsx src/components/research-engine/__tests__/ReviewPanel.test.tsx src/components/research-engine/__tests__/RunResults.test.tsx`.
- [ ] Implement persisted-first hydration and review/result components. Keep draft decisions component-local; server validation is authoritative. Download through the API endpoint, not client-generated content.
- [ ] Run GREEN with RED command plus `corepack pnpm@10.18.2 --dir frontend type-check` and `corepack pnpm@10.18.2 --dir frontend lint`. Commit listed paths with `git commit -m "feat(research-ui): review and download daily briefs"`.

### Task 8: Prove the Lifecycle, Freeze Measured Gates, and Prepare Ship Evidence

**Files:**
- Create: `backend/tests/integration/test_daily_research_brief_postgres.py`
- Create: `frontend/e2e/research-engine/daily-research-brief.spec.ts`
- Create: `backend/tests/eval/test_daily_research_brief_eval.py`
- Create: `backend/tests/performance/test_daily_research_brief_performance.py`
- Create: `docs/testing/daily-research-brief-verification.md`
- Modify only if required by a discovered regression: files from Tasks 1–7 and their focused tests.

**Interfaces:** Produces immutable evidence for the exact commit SHA: test commands/results, migration round trip, generated drift, browser trace, current-model eval, measured baseline/thresholds, max-bound soak, dependency/security scans, remote checks, deploy/feature-flag/rollback status.

- [ ] Before optimizing, measure the existing six-stage controlled-fixture baseline and write numeric p50/p95 plus frozen pass thresholds to the verification doc: no more than 20% p95 regression for equivalent orchestration, maximum 200 candidate rows (4×50), review-page p95 threshold derived from its measured baseline, zero duplicate stage/review rows under concurrency, and bounded memory/payload values. Do not invent or retroactively loosen thresholds.
- [ ] Write PostgreSQL integration cases for happy path with reload at all three gates, partial/all provider failure, dedupe, missing abstract/metadata-only, no evidence, verification override, reconnect, stale/duplicate/conflicting reviews, simultaneous resumes, and cross-owner/same-org denials. Assert exact persisted hashes, review IDs/overlays, evidence links, and artifacts.
- [ ] Write Playwright browser-to-worker lifecycle covering scope, search, screening approval, extraction mixed decisions/reload, final approval, downloads, and separate no-evidence/unverified/stale/reconnect paths. Use controlled external fixtures; no mocks may replace the browser↔API↔database boundary.
- [ ] Add current configured-model eval cases, measure their baseline, and freeze explicit evidence-quality gates before release. Treat zero unsupported material claims, 100% gate compliance, 100% coverage-label correctness, and 100% export hash reconstruction as correctness requirements; set the citation-correctness threshold from the measured baseline before evaluating the candidate. If credentials are absent, mark BLOCKED; do not claim pass from mocked results.
- [ ] Run focused verification: `PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q backend/tests/integration/test_daily_research_brief_postgres.py`; `corepack pnpm@10.18.2 --dir frontend playwright test e2e/research-engine/daily-research-brief.spec.ts --project=chromium`; eval/performance commands with required credentials/flags documented in the evidence file.
- [ ] Run full local gates: `PYTHON=.venv/bin/python bash scripts/ci/run_local_ci.sh origin/main`; `corepack pnpm@10.18.2 --dir frontend validate`; regenerate OpenAPI/types and require clean diffs on the two generated files; run Alembic upgrade/downgrade/upgrade on disposable PostgreSQL.
- [ ] Run fresh dependency/security scans using the repository's pinned requirements/lockfile and document tool versions, databases, suppressions, and results. Record the exact candidate SHA and any remote branch-rule checks already available for that SHA; pushing or publishing solely to obtain remote checks remains `NOT RUN — awaiting explicit repository-write authorization`. Record production configuration, template flag default-off, migration ordering, owner-only policy, monitoring/alert queries, and rollback drill. Deployment/flag enablement remains `NOT RUN — awaiting release authorization` unless separately authorized.
- [ ] Self-review the spec line-by-line, verify `git diff --check`, `git status --short`, no unrelated dirty file was staged, and every claimed pass has an artifact. Commit test/evidence paths with `git commit -m "test(research): certify daily brief lifecycle"`.

## Final Ship Gate

The feature is ready to enable only when Tasks 1–8 are green on one exact SHA; the additive migration is proven both directions on disposable PostgreSQL; OpenAPI/generated types are clean; browser-worker, current-model, concurrency, max-bound performance, dependency, and security evidence are attached; branch rules pass; the template flag is still reversible; and an authorized release owner has approved deploy/enablement. Any `BLOCKED`, `NOT RUN`, stale-SHA, mocked-boundary, or threshold-regression item keeps the feature disabled.

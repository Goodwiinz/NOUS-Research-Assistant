# Daily Research Brief: audited workflow ship plan

Date: 2026-10-01
Status: proposed; awaiting review
Base: `origin/develop` at `a09958699`
Linear milestone: "Daily Research Brief — audited workflow ship" (project RAG): GOO-331, GOO-334, GOO-335, GOO-336, GOO-337, GOO-338.

## Read this first: the feature is already merged

The milestone issues are **review and release-evidence trackers**, not build tickets. The workflow itself shipped in PR #1716 ("Enable audited Daily Research Brief workflow", merged 2026-09-28 as `1b3ee4d50`). Each issue's description opens with "Delivered in PR #1716" and lists acceptance criteria that are reviews, verifications, or live checks.

So this plan does not rebuild anything. Each task:

1. reviews one slice of the shipped code against its acceptance bullets;
2. closes a gap only where the review finds one, with a regression test that fails when the guard is removed;
3. records anything larger as a follow-up issue, as the milestone asks ("record any changes as follow-up issues rather than weakening the contract").

### What already exists on `origin/develop`

| Area | Shipped unit | Notes |
|---|---|---|
| Design and plan records | `docs/superpowers/specs/2026-09-27-daily-research-brief-design.md`, `docs/superpowers/plans/2026-09-27-daily-research-brief.md` | Historical records. Don't edit them; amend by dated record. |
| Verification ledger | `docs/testing/daily-research-brief-verification.md`, `docs/testing/evidence/daily-research-brief-*/` | Disposition "CERTIFICATION COMPLETE — NOT READY TO ENABLE", then a 2026-09-28 amendment that turned the source default on. Dependency audits, anonymous Safety, full frontend validation and full local CI are recorded `FAILED`. Model eval and authenticated Safety are `BLOCKED`. Remote/production checks are `NOT RUN`. |
| Template | `backend/src/services/research_engine/blueprints/templates/daily_research_brief.yaml` | search → screen → extract → synthesize → verify → export, with gates `screening`, `extraction` and `final`. Topology and bounds are enforced by `BlueprintLoader._validate_daily_research_brief` (`blueprints/loader.py:142`). |
| Runtime flag | `DAILY_RESEARCH_BRIEF_ENABLED: bool = True` (`backend/src/core/config.py:555`) | Gates template list/detail (`loader.py:44,65`) and create/start (`api/research_engine/runs.py:361`). It is not set in any Helm values file, so dev runs the source default (on). |
| Scope confirmation | `services/research_engine/scope.py` (`resolve_effective_daily_brief_parameters`, `canonicalize_scope_confirmation`); called by `start_run` (`runs.py:369-397`) | The server computes `configuration_hash`. Export re-verifies it in `ExportService._trusted_scope` (`export_service.py:502`). |
| Connector registry | `services/research_engine/connectors/registry.py` (`CONNECTOR_CAPABILITIES`, `normalize_connector_selection(daily_brief_only=True)`, `safe_capability_projection`, `build_connectors`) | Daily Brief allows 1 to 4 eligible providers. `rag_store` and the `web` alias are refused. |
| Exact-plan binding | `services/research_engine/run_conformance.py` (`create_approved_run`, `require_run_conformance`) | Every run binds to an approved protocol version. `parameters_override` is refused, and blueprints are immutable once run. There is no blueprint update route. |
| Typed stages | `contracts.py` (envelopes, `resolve_parameters`, `merge_stage_output`), `step_executor.py`, `prompt_context.py` (`PromptContextBuilder`), `prompt_batches.py` | Each Daily Brief stage reads only its immediate upstream envelope (`_daily_brief_prompt_context`, `step_executor.py:833`). |
| Search and dedupe | `discovery.py` (`search_sources` raises on unknown names; `prepare_sources`), `search_receipts.py`, `source_persistence.py` | Execution ids are uuid5 over run, step and strategy, so replays are deterministic. |
| Reviews | `models/research_stage_review.py`, `review_service.py`, `api/research_engine/reviews.py`, migration `20260927_daily_research_brief_reviews.py` | Append-only and hash-bound to the output the reviewer saw. |
| Verification and export | `verification.py`, `report_rendering.py`, `export_service.py`, `audit_bundle.py` | Markdown, JSON and CSV, with attestation hashes. |
| Academic R0–R8 machinery (GOO-297..320) | `identity_service.py`/`report_identity.py` (GOO-299), `manifest_rules.py`/`experiment_service.py` (GOO-312), `evidence_*`, `screening_*`, `synthesis_*`, `rerun_*`, `prisma*`, the decision ledger | Composes with Daily Brief through the same `ResearchRun`. Don't add a parallel provenance or manifest store. |
| Frontend | `frontend/src/components/research-engine/{DailyResearchBriefSetup,SourceSelector,BlueprintEditor}.tsx`, `services/researchEngineService.ts`, `frontend/e2e/research-engine/daily-research-brief.spec.ts` | Generated types already cover the routes. |
| Tests | `backend/tests/unit/services/test_daily_brief_scope.py`, `test_research_connector_registry.py`, `test_research_template_contracts.py`, `test_research_prompt_context.py`, `test_research_review_*`, `unit/api/test_research_template_security.py`, `integration/test_daily_research_brief_postgres.py`, `eval/test_daily_research_brief_eval.py`, `performance/test_daily_research_brief_performance.py`, `unit/ci/test_daily_research_brief_migration.py` | |

## Feature flag (GOO-338)

The enablement lever is the existing `DAILY_RESEARCH_BRIEF_ENABLED`. Don't add a second flag.

- **Required end state:** the source default is **off** (`False`), and enablement is an explicit per-environment value in `infrastructure/helm/knowledge-graph-analytics/values-aws.yaml`, next to `DO_KB_ENABLED` at line 141.
- **Conflict to resolve before GOO-338 lands:** the 2026-09-28 amendment in the verification ledger records that the user explicitly authorized a source default of `true`. Flipping it to `false` without adding the Helm value hides Daily Brief on dev. GOO-338 therefore flips the default and adds `DAILY_RESEARCH_BRIEF_ENABLED: "true"` to `values-aws.yaml` in the same PR, so dev behavior is unchanged and the decision becomes explicit and reversible per environment. The release owner signs off in the PR. Rollback is a values-only change to `"false"`.

## Dependency order

```
GOO-331 (contract review + scope guard)
  ├─> GOO-334 (provenance / review gates / attested exports)
  └─> GOO-335 (academic integration / migrations / authz)
          └─> GOO-336 (setup / review / run / export surfaces)
                  └─> GOO-337 (verification ledger amendment)
                          └─> GOO-338 (flag default off + explicit enablement + live rollout)
```

GOO-334 and GOO-335 can run in parallel after GOO-331 review. GOO-336 needs both, because the UI exposes the gates and the authorization model. GOO-337 records evidence from all earlier tasks. GOO-338 is the only task that touches deployment.

## Task 1 — GOO-331: bounded orchestrator and capability contracts

**Acceptance (from the issue):** review the workflow graph and stage contracts against the design spec; confirm no stage can silently widen scope or invoke an undeclared connector; keep provider search, dedupe, screening, extraction, synthesis, verification and export deterministic and replayable; record changes as follow-up issues.

**Review findings (done while writing this plan):**

| Contract | Enforced by | Status |
|---|---|---|
| ResearchEngine is the sole orchestrator; stages exchange typed envelopes | `engine.py` → `StepExecutor.execute`; `merge_stage_output` merges only declared v1 outputs; `_daily_brief_prompt_context` reads only the immediate upstream envelope | Holds |
| Scope confirmation precedes provider search | `start_run` returns 422 without `scope_confirmation`; the server computes the hash; resume injects `scope_confirmation` and `provider_manifest` into the engine context (`runs.py:658-663`) | Holds |
| Template topology cannot be altered | `create_blueprint` replaces steps with the canonical template steps (`blueprints.py:185`); there is no update route; `_verify_plan` requires the blueprint plan to equal the approved protocol plan | Holds |
| Parameters cannot be overridden after confirmation | `create_approved_run` refuses `parameters_override`; `require_run_conformance` re-checks it | Holds |
| **Search step stays inside the confirmed providers and limit** | Holds **only by construction across three modules**: the blueprint parameters were confirmed, the search resolves `{providers}`/`{limit_per_provider}` from the same parameters, and the stream builds the **full** legacy registry (`_build_connectors` → `build_connectors(rag_search)` includes `rag_store`, `pubmed` and `web`). Nothing at the execution boundary compares the executed search with `scope_confirmation`. | **Gap: no fail-closed check at the chokepoint** |
| Unknown connector is a typed failure | `search_sources` raises `Unknown research source` (`discovery.py:171`) | Holds |
| Deterministic replay | uuid5 execution ids over run, step and `strategy_version`; `prepare_sources` is deterministic (an oracle test exists in `test_paper_discovery.py`) | Holds |

**Change (one PR, stacked on this plan):**
- `backend/src/services/research_engine/step_executor.py` `_execute_search`: when `_is_daily_brief_context(context)` is true, fail closed if the canonical providers are not a subset of `scope_confirmation.providers`, or if the effective per-provider limit exceeds `scope_confirmation.limit_per_provider`. That makes the search step itself enforce the confirmed scope, whatever future context or template drift happens upstream.
- Tests: `backend/tests/unit/services/test_daily_brief_search_scope.py` covers an undeclared provider, a widened limit, and the in-scope case still searching only the confirmed providers. Mutation check: remove each guard, confirm its test fails with the named defect, restore it.

**Follow-ups to file (not fixed in GOO-331):**
- F1: `_execute_search` runs `_safe_render` on a query that `resolve_parameters` already resolved. A research question containing `$name` text (for example `$providers`) is substituted with context values before it reaches providers. The fix is to skip the second render for v1 contracts. That is a behavior change for legacy templates, so it needs its own review.
- F2: optional hardening. Instantiate only the confirmed connectors for Daily Brief runs (`build_connectors(rag_search, connector_ids=scope.providers)` already supports subsets). It is redundant once the Task 1 guard lands, so file it only if review wants defense in depth.

**Checks:** ruff, black and isort on changed files; mypy on the added test file; `pytest -q backend/tests/unit/architecture backend/tests/unit/api`; the focused `pytest -q backend/tests/unit/services/test_daily_brief_search_scope.py backend/tests/unit/services/test_paper_discovery.py backend/tests/unit/services/test_step_executor.py`. No schema, OpenAPI or migration change.

## Task 2 — GOO-334: provenance, review gates, attested exports

**Review against:** `export_service.py` (`_trusted_scope:502`, attestation at `:498`, `_provenance`), `report_rendering.py`, `review_service.py` (`apply_approved_overlays`), `models/research_stage_review.py`, and the identity tables from GOO-299.

**Work:**
- Prove that every exported claim resolves to a source span or an explicit unresolved state: a property test over the eval fixture in `backend/tests/eval/test_daily_research_brief_eval.py` asserting that no claim in the JSON or CSV export lacks `evidence_ids` unless its status is `unresolved`/`unsupported`.
- Prove the stability of hashes across reruns: export the same persisted run twice and assert byte-identical Markdown, JSON and CSV plus equal attestation hashes. Extend `backend/tests/unit/services/test_export_service.py`.
- Prove that no internal identifiers leak: assert that exports contain no DB primary keys beyond the documented `run_id`/`blueprint_id`, no `owner_id`, and no `organization_id`. Extend `test_export_service.py`.
- Prove that the final gate is mandatory: no `export` output is releasable without an `approve` row for `review_kind=final` on the current `outputs_hash`. There is existing coverage in `test_research_review_service.py`; add a mutation check if it is missing.

**Touch list:** tests only, unless a check fails. A failure becomes a scoped fix in `export_service.py` or `review_service.py`.

## Task 3 — GOO-335: academic workflow integration and migrations

**Review against:** `project_access.py` (`resolve_engine_project_context`, `require_blueprint`, `ResearchAction`), `run_conformance.py`, and `backend/alembic/versions/{20260927_daily_research_brief_reviews,merge_daily_brief_harness_heads,merge_research_heads_20260928_*}.py`.

**Work:**
- Assert that Daily Brief create, start, review, export and resume all route through `resolve_engine_project_context`/`require_*` with the same `ResearchAction`, with negative tests for a foreign owner and a soft-deleted project. Extend `backend/tests/unit/api/test_research_template_security.py`.
- Verify migrations with a fresh upgrade to head, a branch-specific downgrade of `20260927_daily_research_brief_reviews`, and a re-upgrade, using the Postgres integration fixture (`backend/tests/integration/conftest.py` `postgres_container`). Add it to `backend/tests/unit/ci/test_daily_research_brief_migration.py` if it is not already covered. Run `(cd backend && python ../scripts/ci/check_alembic.py)`.
- Confirm that `backend/openapi.json` and `frontend/src/types/generated/api.d.ts` have no drift (`python scripts/ci/generate_openapi.py --check`).

**Touch list:** tests only, unless a check fails.

## Task 4 — GOO-336: setup, review, run and export surfaces

**Work:**
- Run full frontend validation (`pnpm --dir frontend lint`, `type-check` and `test`). The ledger records it as `FAILED`, so fix or record each failure, separating pre-existing failures from Daily Brief ones.
- Disabled state: with `DAILY_RESEARCH_BRIEF_ENABLED=false`, the template list hides Daily Brief, create/start return 404, and existing run detail and export still render. The backend half is covered in `test_research_engine_endpoints.py:465`. Add a Vitest case in `DailyResearchBriefSetup.test.tsx`/`RunResults.test.tsx` for a 404 on start that shows a clear message, not a generic error.
- Review overlays and export metadata are visible at the screening, extraction and final pause points: extend `RunResults.test.tsx`.
- Browser-to-worker lifecycle: run `frontend/e2e/research-engine/daily-research-brief.spec.ts` against a live stack. If no stack is available, report it as `NOT RUN`.

**Touch list:** `frontend/src/components/research-engine/*` and their `__tests__`, plus the e2e spec only if it is stale.

## Task 5 — GOO-337: verification ledger and release evidence

**Work:** add a **dated amendment** to `docs/testing/daily-research-brief-verification.md` (never rewrite earlier sections) that records the Task 1–4 results with exact SHAs. It also lists each previously `FAILED`, `BLOCKED` or `NOT RUN` item with its current state:
- dependency audits;
- anonymous and authenticated Safety;
- full frontend validation;
- full local CI;
- configured-model eval;
- remote candidate-SHA checks;
- production configuration inspection.

Each item gets current evidence or an explicit owner waiver.

**Touch list:** `docs/testing/daily-research-brief-verification.md` and a new `docs/testing/evidence/daily-research-brief-<date>/README.md`.

## Task 6 — GOO-338: controlled enablement and production rollout

**Code change (one small PR):**
- `backend/src/core/config.py:555`: set `DAILY_RESEARCH_BRIEF_ENABLED: bool = False` and update the comment.
- `backend/tests/unit/services/test_blueprint_loader.py:58-64`: assert the default is `False` and that an environment value of `true` enables it.
- `infrastructure/helm/knowledge-graph-analytics/values-aws.yaml`: add `- name: DAILY_RESEARCH_BRIEF_ENABLED` / `value: "true"` next to `DO_KB_ENABLED`, so dev keeps current behavior explicitly.
- `frontend/e2e/research-engine/daily-research-brief.spec.ts:256` already sets `'true'` explicitly, so no change is needed there.

**Live steps (user-side, after the PR merges and the GitOps PR deploys):**
1. Inspect the deployed environment (`kubectl -n multimodal-rag-system get deploy … -o yaml`) and confirm the flag value.
2. Run authenticated acceptance for list, detail, create, start, review and export.
3. Rehearse the rollback: set the values entry to `"false"`, confirm that new create/start return 404 and that historical runs and exports still read, then restore it.
4. Record the evidence in the GOO-337 ledger amendment.

Production (non-dev) values are dead files (see `docs/engineering/gotchas.md`), so "production" here means the live dev environment unless the release owner says otherwise.

## Verification commands (all tasks)

```sh
backend/.venv/bin/ruff check backend/src
pytest -q backend/tests/unit/architecture backend/tests/unit/api
python scripts/ci/generate_openapi.py --check
(cd backend && python ../scripts/ci/check_alembic.py)   # Task 3 only
make docs-lint
scripts/ci/run_local_ci.sh --base origin/develop [--frontend]
```

Checks that need a DB, browser, credentials or cluster are reported as `NOT RUN`, never as passing.

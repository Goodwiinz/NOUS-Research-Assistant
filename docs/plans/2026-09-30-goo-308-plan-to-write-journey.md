# GOO-308 Plan-to-Write Journey + Evaluation Plan (Academic R4)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Connect the project surfaces that GOO-299 to GOO-307 build into one Plan → Discover → Select → Extract → Write journey on the project Workflow tab, and then evaluate that journey. The journey is driven by persisted state. An independent researcher can reconstruct methods, exclusions, facts and claim support offline from one downloaded audit bundle. This ticket adds no project model, no new write path and no migration.

**Architecture:**
- **Two read endpoints** on one new router, `backend/src/api/research_engine/journey.py`, both `resolve_project(VIEW)`:
  - `GET /journey` returns stage facts plus the derived status of each stage.
  - `GET /audit-bundle` returns one zip that holds the existing exports, a `manifest.json` and a `SHA256SUMS` file.
- **Two service modules:**
  - `journey.py`: count queries plus a pure `derive_stages`.
  - `audit_bundle.py`: gathers the parts, and has a pure `write_zip` and `verify_bundle`.
- **The bundle has no reader of its own.** Each part comes from the existing export or read function, called with the same `ProjectContext`. The bundle can never expose something its parts withhold, so GOO-300's restriction policy, GOO-302's reveal predicate and GOO-306's no-document-text rule are all inherited unchanged.
- **Frontend:**
  - A `JourneyRail` sits at the top of `ProjectWorkflow`.
  - The existing panels are grouped under five stage anchors.
  - GOO-306's `DraftClaimsPanel` gains the claim authoring and assessment controls that GOO-306 and GOO-307 deferred.
- **Evaluation:** `evals/academic-journey-v1/` reuses GOO-293's collector helpers through `importlib`, the same way `evals/academic-writing-baseline-v1/tests/test_collect.py:13-19` loads them.

**Tech stack:** FastAPI, SQLAlchemy async, stdlib `zipfile` and `hashlib`, the `postgres_container` fixture (`backend/tests/integration/conftest.py:208`), openapi-typescript, Next.js with TanStack Query and Vitest, and Playwright.

**Stacking (read first):**
- **Upstream code.** This ticket is the top of the academic stack on `feat/goo-304-extraction-forms`: 299 → 300 → 301 → 302 → 303 → 304 → 305 → 306 → 307. GOO-307 also needs GOO-297 (#1753).
- **What is on the branch today.** It holds only GOO-299, GOO-301 and **part of GOO-300**: the parsers, receipts and `corpus_service.py`. GOO-300 Tasks 6-9 are **not on the branch**. There is no `corpus_export.py`, no `api/research_engine/corpus.py` and no `CorpusPanel.tsx`; compare `backend/src/api/research_engine/__init__.py:3-11` with the GOO-300 plan :217-344.
- **What the bundle's parts need.** Its corpus part needs GOO-300 Task 6, and its other parts need the 303, 304, 306 and 307 service functions named below. This PR opens only after all of these are on `develop`.
- **Live principals.** R0 could not run any second principal: every non-owner token got 500 `DATABASE_ERROR` from JIT provisioning (`docs/audits/2026-09-29-live-evidence/journey.md:79-80`). The fix is **PR #1747**, which is not in this worktree's refs (`git log --all --grep=1747` is empty). Confirm that it is merged and deployed before any live step.

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| Where the rail's state comes from | `GET /research-engine/projects/{project_id}/journey` returns `JourneyResponse{stages: [{key, status, facts, blockers}], current}`. `status` is one of `not_started`, `in_progress`, `attention` or `complete`. The facts are count queries over the rows that each ticket persists. `derive_stages(facts)` is pure. | Runs cannot be listed per project from the client (`backend/src/api/research_engine/runs.py:411-535` has only per-run GETs), so a client-side rail would need about nine requests and still miss Discover. One read, with the rule in one pure function, is smaller and testable. |
| Stage rules (v1) | **Plan:** `complete` if a question version exists, `current_approved_version_id` is set and `run_conformance._verify_plan` (`backend/src/services/research_engine/run_conformance.py:33-58`) passes for the bound blueprint. `attention` carries that function's 409 detail, such as drift ("Blueprint changes require a protocol amendment", `:47-50`). **Discover:** `complete` if `reports ≥ 1` and there is either at least one completed run with `conformance_status ∈ {plan_verified, conformant}` or at least one import receipt. `attention` if a run failed. **Select:** `complete` if every queued report has a GOO-302 resolution tip, no conflict is open, no GOO-303 request lacks a terminal attempt, and `derive_prisma_flow` raises no `PrismaInconsistency`. **Extract:** `complete` if accepted-tip cells equal document × field cells of the current form version (with more than zero cells) and none is stale (GOO-304 `is_stale`, GOO-305 `source_changed`). `attention` for stale cells or open disagreements. **Write:** `complete` if GOO-307 `statuses()` says the current draft is `verified`. `attention` if it is `stale` or blocked. `current` is the first stage that is not complete. | Stages are derived independently, not gated in sequence, because real reviews loop back (an amendment, a new import). The rail reports state and never enforces order, and the existing server gates stay authoritative. |
| Blind review in the rail | Select counts use only **resolutions** (GOO-302 tips) and queue `report_ids`. They never use per-reviewer observation counts. | A resolution exists only after the required observations exist, which implies reveal (GOO-302 plan :5). An observation count would tell reviewer A that reviewer B has submitted. |
| Bundle endpoint | `GET /research-engine/projects/{project_id}/audit-bundle`, VIEW. It returns `Response(zip, "application/zip", Content-Disposition: attachment; filename="audit-{project_id}-{manifest_sha[:12]}.zip")`, the shape used by `runs.py:537-561`. Archived projects get 200. Foreign, public-only and deleted projects get 404 (`project_access.py:192-193,236-238`). A part that exceeds its own cap (GOO-300's 413 `export_too_large`) fails the whole bundle, because a partial bundle is never sent. | A download is not a decision, so it needs no ledger event and no idempotency key. |
| Bundle members | `corpus.json` is GOO-300 `corpus_export.build_package`. `prisma-flow.json` is GOO-303 `prisma.package(derive_prisma_flow(load_inputs))`. `claims.json` is GOO-306 `claims_service.export_package(draft_id=None)`. `drafts/{draft_id}-v{n}.md` covers the current draft **and** every version with a `draft_releases` row, produced by `export_draft` (`backend/src/services/research/draft_generation_service.py:2031`) with GOO-307's candidate/verified/stale labels. `drafts/release-checks.json` is GOO-307 `check()` for each of those versions. `methods.json` holds approved and superseded protocol versions `{id, status, content_hash, question_version_id, blueprint_id, snapshot, execution_plan}` plus every run `{id, status, protocol_version_id, effective_plan_hash, conformance_status, blueprint_version, reproducibility_manifest}`, with `_PAUSE_REQUESTED_KEY` removed as in `runs.py:530`. `extraction.json` holds every matrix's GOO-304 `list_versions` and `cell_view` accepted tips with their `observation_ids`. Last come `manifest.json` and `SHA256SUMS`. | These are the ticket's five exports, plus two gaps. **Methods:** the corpus package carries only the protocol's id and hash (GOO-300 plan :233), so a reader cannot recompute `content_hash` or `effective_plan_hash` (`run_conformance.py:23-30`) without the snapshot. **Facts:** the claims package carries only accepted values reachable from a claim link (GOO-306 plan :226), so unlinked facts would be missing. Both gaps reuse existing readers and add no query logic. |
| Manifest | `{"schema": "nous.academic.audit-bundle.v1", project_id, generated_at, deployment_sha: os.getenv("GIT_SHA"), protocol_version_id, stream_heads, parts: [{path, schema, sha256, bytes, body_sha256, status: "ok" \| "empty"}]}`. `SHA256SUMS` uses `sha256sum` format and covers every member, including `manifest.json`. | `sha256sum -c SHA256SUMS` checks the bundle offline with standard tools. `body_sha256` is the part's own content hash, stable across downloads, while `sha256` covers the file bytes, which include `exported_at`. An empty part is listed with `status: "empty"` and never left out silently. |
| Snapshot consistency | The route runs `await db.rollback()`, then (on PostgreSQL only) `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`, and only then `resolve_project(VIEW)` and the part builders. | Without this, a commit between two parts gives PRISMA counts that disagree with `corpus.json`. It uses one native DB feature instead of app-level locking, and VIEW takes no locks (`project_access.py:195`). |
| Offline verification | `verify_bundle(data: bytes) -> dict` is pure. It checks `SHA256SUMS`, recomputes each part's `body_sha256` with `contracts.canonical_json_sha256` (`backend/src/services/research_engine/contracts.py:38-40`), checks that every draft file's content hash matches `claims.json.drafts[]`, and runs GOO-300 `verify_package` on `corpus.json`. The eval collector re-implements only the checksum half with stdlib and **does not import `src`**. | The product verifier catches regressions. The collector's independent check is what makes it an audit rather than a self-report. |
| Claim authoring UI | Extend GOO-306's `DraftClaimsPanel` with optional props `{roles, canEdit}`. Without them it stays read-only, as in `DraftStep`. Mount it in the Write section of `ProjectWorkflow` for the current draft. **Create:** the draft is shown in a `<textarea readOnly>`. The native `selectionStart`/`selectionEnd` are UTF-16 offsets, converted to code points with `Array.from(content.slice(0, i)).length`, then `POST /claims`. **Link:** a select of accepted-value tips from GOO-304 `GET /matrices/{id}`, then `POST /claims/{id}/links` with `kind: "extraction"`. **Observe:** a button per link, `POST …/links/{link_id}/observations`. **Assess:** only when `roles` include `adjudicator`, with a stance select, checkboxes for live non-legacy links, a rationale, and `POST /claims/{id}/assessments`. Every POST sends `idempotency_key: crypto.randomUUID()` per form open. On 409 the claims query is refetched and the server detail is shown. | GOO-306 :320 and GOO-307 :355 both left this UI out. GOO-306 defines offsets in code points (plan :35), and an emoji in a draft would misalign a naive JS offset. `source_span` links stay API-only (`# ponytail:`), because the journey's Extract→Write path runs through accepted values. |
| Stage navigation | `ProjectWorkflow` wraps its panels in `<section id="journey-plan|discover|select|extract|write">`. The rail links to those anchors. Extract and Write also get "Open matrix" and "Open drafts" buttons that call a new optional `onOpenTab` prop, which `page.tsx` passes as `handleTabChange` (`frontend/app/(dashboard)/projects/[id]/page.tsx:450-458,1344-1345`). | This reuses the existing tabs (`page.tsx:722-724`) and replaces nothing. |
| Rail freshness | `useQuery(['project', id, 'research-engine', 'journey'])`. `useIsMutating()` dropping to 0 invalidates it. `# ponytail: coarse; add per-panel invalidation if the refetch is noisy.` | Each panel owns its own keys (for example `ScreeningQueuePanel.tsx:68`, `ReportIdentityPanel.tsx:49`). This way no panel needs editing. |
| Migration | **None.** Both endpoints only read tables created by GOO-299 to GOO-307. | `(cd backend && python ../scripts/ci/check_alembic.py)` must report the GOO-307 head `d7f9b1c3e5a8` unchanged. |
| OpenAPI | Two new GET operations and `JourneyResponse`/`JourneyStage`. The bundle is declared with `responses={200: {"content": {"application/zip": {}}}}`. **Expected oasdiff result: additive, no ERR.** Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts` in the same commit. | `docs/engineering/api-contracts.md` pipeline. |

---

### Task 1: Pure stage derivation

**Files:**
- Create `backend/src/services/research_engine/journey.py`, containing the pure part: `StageFacts` (a frozen dataclass per stage), `derive_stages(facts) -> list[Stage]` and `STAGES = ("plan", "discover", "select", "extract", "write")`.
- Create `backend/tests/unit/services/test_journey_stages.py`.

**Tests (write first):**
- `test_empty_project_all_not_started_current_is_plan`
- `test_plan_drift_is_attention_with_server_detail`: the facts carry `plan_error="Blueprint changes require a protocol amendment"`.
- `test_discover_requires_reports_and_a_conformant_run_or_import`
- `test_failed_run_marks_discover_attention_not_complete`
- `test_select_open_conflict_or_pending_fulltext_blocks_complete`
- `test_unavailable_fulltext_is_terminal_not_exclusion`: an `unavailable` attempt counts as terminal and has no effect on the exclusion counts.
- `test_extract_stale_cell_is_attention`
- `test_write_complete_only_when_current_draft_verified`: `candidate` gives in_progress, `stale` gives attention, `verified` gives complete.
- `test_stages_independent_current_is_first_incomplete`: Write can be `complete` while Select is `attention`.

**Run:** `pytest -q backend/tests/unit/services/test_journey_stages.py`
**Commit:** `feat(research): pure journey stage derivation (GOO-308)`

---

### Task 2: Journey facts + route

**Files:**
- Modify `journey.py`: add `async def facts(db, context) -> StageFacts`. It runs one count query per table, each filtered by `collection_id`, or by `matrix.project_id` for extraction rows, or by `blueprint.project_id == context.engine.id` for runs. It also calls `_verify_plan` for the latest approved version and its blueprint, inside a try/except around `HTTPException`, which becomes `plan_error`.
- Create `backend/src/api/research_engine/journey.py` (`APIRouter(prefix="/research-engine", tags=["research-engine-journey"])`, transport only, no commit).
- Modify `backend/src/api/research_engine/__init__.py:3-22` and `backend/src/main.py:80-89,674-675` to register the router next to screening with `prefix="/api/v1"`.
- Add `JourneyStage` and `JourneyResponse` to `backend/src/schemas/research_engine.py`.
- Create `backend/tests/unit/api/test_research_journey_routes.py`, following `test_research_identity_routes.py`, dependency-overridden.

**Tests:**
- `test_journey_foreign_project_404`
- `test_journey_archived_200`
- `test_journey_reviewer_sees_no_observation_counts` (the response keys are a closed set)

**Commit:** `feat(research): project journey facts endpoint (GOO-308)`

---

### Task 3: Audit bundle

**Files:**
- Create `backend/src/services/research_engine/audit_bundle.py`:

```python
SCHEMA = "nous.academic.audit-bundle.v1"
@dataclass(frozen=True) class Part: path: str; schema: str | None; data: bytes; body_sha256: str | None
async def gather_parts(db, context) -> list[Part]        # calls the existing builders in a fixed order
def write_zip(parts, *, project_id, generated_at, deployment_sha, protocol_version_id, stream_heads) -> tuple[bytes, str]  # (zip, manifest sha)
def verify_bundle(data: bytes) -> dict                   # pure; raises BundleError on any mismatch
```

  `write_zip` uses a fixed `ZipInfo.date_time` and sorted member order, so identical parts give identical bytes, apart from `generated_at` in the manifest.
- Add the route to `backend/src/api/research_engine/journey.py`. It follows the snapshot order in Decisions.
- Create `backend/tests/unit/services/test_audit_bundle.py`.

**Tests (unit; parts are fed as bytes, with no DB):**
- `test_manifest_lists_every_part_with_sha_bytes_and_body_hash`
- `test_sha256sums_is_sha256sum_compatible_and_covers_manifest`: parse it and recompute.
- `test_empty_part_listed_with_status_empty_never_omitted`
- `test_verify_bundle_rejects_tampered_member`: flip one byte in `claims.json`.
- `test_verify_bundle_rejects_body_hash_mismatch`: re-zip with a matching SHA256SUMS but an edited body.
- `test_verify_bundle_rejects_draft_not_matching_claims_package_hash`
- `test_same_parts_same_member_bytes`
- `test_route_404_foreign_200_archived_attachment_header` (API unit, dependency-overridden)

**Then:** `python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types && python scripts/ci/generate_openapi.py --check`. Commit both generated files with the router.
**Commit:** `feat(research): offline-verifiable project audit bundle (GOO-308)`

---

### Task 4: PostgreSQL proof (one test)

**Files:**
- Create `backend/tests/integration/test_audit_bundle_postgres.py`, `pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]`. It seeds with `seed_canonical_project_scope` and `seed_approved_protocol_binding` (`backend/tests/integration/research_engine_postgres_support.py:84,143`) and the schema-rebuild pattern of `test_screening_queue_postgres.py:77-135`.

**`test_audit_bundle_matches_rows_scope_and_snapshot`** runs these steps in order:
1. **Seed** (org A project P, org B user F):
   - one approved protocol and one conformant completed run with a `_search_receipts_v1` manifest;
   - one `restricted` import whose record `raw` holds the sentinel `RESTRICTED-7f3a`;
   - a `Document.content_text` that holds `FULLTEXT-91c2`;
   - a dual queue with one agreement and one adjudicated conflict, and one `unavailable` full-text attempt;
   - one matrix with one accepted value;
   - one claim with a supporting assessment on that value;
   - a draft v1 promoted by GOO-307.

   Principals are owner O, reviewers R1 and R2, adjudicator J and supervisor S.
2. **Download as O:** the zip opens, `verify_bundle` passes, and every `SHA256SUMS` line recomputes.
3. **Hashes match rows:**
   - `sha256(draft.content)` equals the release `content_hash` and the `content_hash` of `claims.json.drafts[0]`;
   - `claims.json.body.stream_head` equals `research_decision_streams.next_seq - 1`;
   - every `prisma-flow.json` count equals raw SQL written in the test (the GOO-303 plan :311 approach);
   - `methods.json` recomputes `canonical_hash(protocol_content(...)) == content_hash` and `effective_plan_hash(...) == run.effective_plan_hash`;
   - `extraction.json`'s accepted tip id equals the DB tip.
4. **Restricted content absent:** neither sentinel appears in any member's bytes.
5. **Scope:** F gets 404, a public-only workspace viewer gets 404, a soft-deleted project gets 404, and the archived project gets 200.
6. **Zero writes:** table counts and every `next_seq` are unchanged. A second download has an identical `body_sha256` for every part.
7. **Snapshot:** monkeypatch the second part builder so that it first commits a new adjudication in a separate session. The bundle's `corpus.json` and `prisma-flow.json` must still agree on `stream_heads`, and the new resolution must be absent from both.
8. **Journey rides along:** `GET /journey` as O shows Plan through Write `complete`. As R1, before R2 submits on a new report, the response is byte-identical to the response after R2 submits and before the resolution (the blind-review guard).

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_audit_bundle_postgres.py`. Without a database, report it as **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for audit bundle rows, scope and snapshot (GOO-308)`

---

### Task 5: Frontend rail, stage sections, claim authoring

**Files:**
- Create `frontend/src/types/api/research-journey-contract.ts`, aliasing `JourneyResponse` and `JourneyStage` (the pattern is `research-screening-contract.ts`).
- Modify `frontend/src/services/researchEngineService.ts`: add `getJourney(projectId)` and `downloadAuditBundle(projectId)` through `api.download` (`frontend/src/services/api-client.ts:585`), as `downloadRunExport` does (`researchEngineService.ts:190-196`).
- Create `frontend/src/components/research-engine/JourneyRail.tsx`, an `<ol aria-label="Research journey">` of five `<li>`:
  - each shows its status as text plus an icon, its facts as short counts and its blockers;
  - the current stage has `aria-current="step"`;
  - colors come from theme tokens;
  - a footer holds a "Download audit bundle" button.
- Modify `frontend/src/components/research-engine/ProjectWorkflow.tsx`:
  - render `<JourneyRail>` above `BlueprintEditor` (`:137`);
  - wrap the panels at `:137-162` and the GOO-300/302/303 panels in the five `<section id="journey-…">` groups;
  - add an optional `onOpenTab` prop;
  - mount `DraftClaimsPanel` in `journey-write` with `roles={roles.data ?? []}` and `canEdit`.
- Modify `frontend/app/(dashboard)/projects/[id]/page.tsx:1345` to pass `onOpenTab={handleTabChange}`.
- Modify GOO-306's `frontend/src/components/research/DraftClaimsPanel.tsx` and `projectService.ts` (add `createClaim`, `linkClaimEvidence`, `observeClaimLink`, `assessClaim`), as described in Decisions.
- Create `frontend/src/components/research-engine/__tests__/JourneyRail.test.tsx`. Modify `__tests__/ProjectWorkflow.test.tsx:10-60` to add the mocks.
- Extend `frontend/src/components/research/__tests__/DraftClaimsPanel.test.tsx`.

**Tests:**
- `renders five stages with status text and marks current step`
- `download button calls downloadAuditBundle`
- `refetches journey after a mutation settles`
- `selection offsets are code points` (a passage after "📊" posts the code-point `start_char`)
- `assess form hidden without adjudicator role`
- `stale 409 refetches claims and shows server detail`
- `read-only without props`

**Run:** `pnpm --dir frontend exec vitest run src/components/research-engine/__tests__/JourneyRail.test.tsx src/components/research-engine/__tests__/ProjectWorkflow.test.tsx src/components/research/__tests__/DraftClaimsPanel.test.tsx`
**Commit:** `feat(frontend): journey rail, stage sections and claim authoring (GOO-308)`

---

### Task 6: Playwright

**Files:**
- Modify `frontend/e2e/nous-flows/research-project-identity.spec.ts`. Its catch-all (`:365-367`) answers `{items: [], total: 0}`. Add one branch for `/research-engine/projects/${COLLECTION_ID}/journey` that returns five `not_started` stages, and assert that the rail renders and that the path uses the canonical id (`seenProjectScopedPaths`, `:20,52`). The mocked check is otherwise unchanged and still opt-in (`MOCK_PROJECT_IDENTITY_E2E=1`, `:22-25`).
- Create `frontend/e2e/nous-flows/research-journey.live.spec.ts`. It is skipped unless `RESEARCH_JOURNEY_LIVE=1`, using the same gate shape as `frontend/e2e/agent-qa/live-agent-qa.spec.ts:25-28`, and makes **no `page.route` calls**.
  - One browser context per principal, from saved states in `RESEARCH_JOURNEY_AUTH_DIR/{author,supervisor,reviewer-a,reviewer-b,adjudicator,foreign}.json`. Each state is made with `NOUS_AUTH_STATE=… pnpm --dir tools/nous-playwright auth` (`tools/nous-playwright/auth.mjs:101`).
  - It drives the known-answer project (Task 7) through the UI, from Plan to Write.
  - After every step it reads `GET /journey` and the relevant list through `context.request`. It appends `{step, principal, method, path, status, ids, versions, hashes}` to `$RESEARCH_JOURNEY_OUT/transitions.jsonl` and never writes headers or cookies.
  - It downloads the bundle through the button, saves it as `audit-bundle.zip`, and checks `SHA256SUMS` in-test with `node:crypto`.
  - It records the deny probes: foreign 404 on journey, bundle and claims; a reviewer's promote 403.
  - It reloads the page at each stage and asserts that the rail state is unchanged.
  - Run with `--trace on` and keep `trace.zip`.

**Run (live, authorized only):** `RESEARCH_JOURNEY_LIVE=1 BASE_URL=https://goodwiinz.tech RESEARCH_JOURNEY_AUTH_DIR=… RESEARCH_JOURNEY_OUT=… pnpm --dir frontend exec playwright test e2e/nous-flows/research-journey.live.spec.ts --config=playwright.nous.config.ts --trace on`
**Commit:** `test(e2e): live authenticated plan-to-write journey behind a flag (GOO-308)`

---

### Task 7: Evaluation harness `evals/academic-journey-v1/`

**Files:**
- `protocol.json` (frozen)
- `corpora/known-answer.json` (documents and screening criteria, which the runner may read)
- `corpora/known-answer.gold.json` (read by the collector only)
- `collect.py`
- `tests/test_collect.py`
- `README.md`

**Protocol:** `baseline_status: not_established`. The projects are:
- `real-1` and `real-2`, `kind: consenting_real`. Each trial retains a `consent.json` (the signed consent record id plus the scope digest, never the content).
- `known-answer`, `kind: held_out_known_answer`, with 10 frozen documents:
  - 2 duplicate reports of one study;
  - 1 with no retrievable full text;
  - 2 full-text exclusions with protocol reasons;
  - 3 fields with gold values;
  - 6 manuscript claims, one of them a seeded unsupported number and one contradictory.

  The gold file is never shown to principals, and no product change may be tuned against it.

**Principals (required, distinct user ids, checked against a retained `GET /roles`):**
- the author (the workspace owner, with no decision role);
- an **explicitly assigned** supervisor;
- reviewers A and B;
- an adjudicator;
- the probe principals: a foreign-org user and a role-less workspace viewer.

**Measurements** (each has a fixed definition, is computed by the collector **from the bundle and transitions alone**, and is always published with its denominator):

| Measurement | Definition |
|---|---|
| Participant count | Distinct human `actor_user_id` across bundle decisions, assessments and accepted values with `actor_role ≠ machine`, reported per role and compared with the declared principals. |
| Completion denominator | 3 projects × 5 stages = 15 (project, stage) pairs. A pair counts only if the final `/journey` status is `complete` **and** that stage's bundle evidence verifies. Published as `k/15` plus per-stage `k/3`. |
| Manual correction burden | The count of human rows that override or amend machine or earlier output: identity merges and splits (`corpus.json` decisions), adjudicated screening resolutions, accepted values whose value equals no cited machine observation, superseding claim versions, and superseding assessments. Reported raw and per included report. |
| Disputed-claim resolution time | For each claim version whose first assessment stance is not `supporting`, or whose stance observation disagrees with its final tip, the time from the first such row's `created_at` to the final tip's `created_at`. Reported as n, median and max. The claims package carries every `created_at`. |
| Export completeness | `parts_ok/7` (present, checksum-verified, body hash recomputed), plus reconstruction checks. **Known-answer:** PRISMA counts, the exclusion reasons, the accepted values and the claim support mapping all equal the gold. **Real projects:** every excluded report has a reason, every link resolves by slicing, and every run's hashes recompute from `methods.json`. |

**Outcome dimensions** (separate verdicts that are never combined into one score):
- `reporting_completeness`: `passed`, `failed` or `not_run`.
- `protocol_adherence`: `passed`, `failed` or `not_run`. Every run is bound and conformant, and the drift, override and legacy scenarios were rejected before paid execution.
- `claim_correctness`: `passed`, `failed`, `not_run` or `judge_unavailable`. On the known-answer project it is checked against gold. On real projects it comes from the independent human adjudication record, optionally with GOO-293's calibrated judge (`semantic-judge-calibration.json`).
- `infrastructure`: `passed` or `failed`, and unscored.
- `judge_unavailable`: unscored, and kept in the sample-size table, as the GOO-293 README :45-55 does.

**Scenarios** (each is `required` and has a declared induction method). A scenario with no safe induction on shared dev is recorded as `not_run`. It is never simulated and never counted as a pass.

| Id | Scenario | Expected persisted evidence |
|---|---|---|
| S1 | Full journey on each project | All 15 pairs, the bundle, and the transitions |
| S2 | Reload at each stage, plus a new browser context and re-login | The rail and ids are identical before and after. A worker restart during extraction requires explicit authorization (a cluster action). |
| S3 | Provider outage | The run records a non-`ok` execution receipt, Discover is `attention`, and nothing is fabricated. It is induced by a blueprint that uses a connector unavailable in dev; otherwise it is `not_run`. |
| S4 | Missing full text | An `unavailable` attempt appears in PRISMA "not retrieved" and is not counted as an exclusion. |
| S5 | Disputed evidence | A dual-review conflict is adjudicated, an `opposing` assessment blocks promotion (409 `release_blocked`), and the later resolution is timed. |
| S6 | Unauthorized, foreign or deleted access | Foreign users get 404 on journey, bundle, claims and prisma. A role-less viewer gets 404, and a reviewer's assess and promote get 403. A deleted project gets 404 and an archived one gets 200 for the bundle and 409 for writes. |
| S7 | Approved-plan drift, override or legacy-unbound, before paid execution | A blueprint edit after approval gives 409 "Blueprint changes require a protocol amendment" (`run_conformance.py:47-50`). `parameters_override` gives 409 (`:116-119`). A legacy unbound run's stream or resume gives 409 "Run is not bound to an approved protocol" (`:157-160`). In each case, `GET /runs/{id}/steps` is empty and the manifest has no search receipts, which shows that no paid call ran. |
| S8 | Source change after release | GOO-307 `release.staled`, the rail shows Write `attention`, and a sibling release stays `verified`. |

**Collector** (`collect.py --trials-dir --output --source-sha`). It loads GOO-293's module through `importlib` and reuses `EvidenceError`, `_file_digest`, `_canonical_digest`, `_reject_sensitive_configuration`, `_verify_source_checkout` and `_verify_source_attestation` (`evals/academic-writing-baseline-v1/collect.py:99-180,271-350`). It adds:
1. **Transitions:** every id is created before it is referenced, every stream's `next_seq` never decreases, every version or hash that a later step names equals the value recorded when it was created, and the final ids are a subset of the bundle's ids.
2. **Bundle:** stdlib `zipfile` and `hashlib` recompute `SHA256SUMS` and each `body_sha256`. The release `content_hash` must equal `sha256(draft file content)`. It does **not** import `backend/src`.
3. **Redaction:** the runner never writes headers or cookies. The collector runs `_reject_sensitive_configuration` over every retained JSON and rejects the trial on a hit, which is stricter than redacting after the fact. The published report copies only declared fields.
4. **Refusal:** there is no output file and the exit code is non-zero when any declared trial lacks `transitions.jsonl`, `audit-bundle.zip`, `trace.zip`, `roles.json`, `consent.json` (real projects) or `independent-review.json` bound to the bundle's manifest sha, or when a required scenario has neither evidence nor a `not_run` reason.

**Tests** (`tests/test_collect.py`, using the fixtures pattern of GOO-293 :22-30):
- `test_protocol_declares_three_projects_five_principals_and_measurements`
- `test_refuses_trial_without_bundle_or_trace`
- `test_tampered_bundle_member_rejected`
- `test_transition_hash_drift_rejected`
- `test_secret_shaped_value_rejected`
- `test_completion_reports_k_of_15_and_per_stage`
- `test_correction_burden_and_dispute_time_from_bundle_only`
- `test_infrastructure_and_judge_unavailable_unscored`
- `test_known_answer_reconstruction_against_gold`
- `test_not_run_scenario_requires_reason`

**Run:** `pytest -q evals/academic-journey-v1/tests`
**Commit:** `feat(evals): academic plan-to-write journey protocol and collector (GOO-308)`

---

## Mutation verification

Follow `docs/engineering/testing.md` (Mutation verification). Record each check in the test docstring and in `docs/testing/agent-orchestration-mutation-checks.md` under a GOO-308 section.

| Guard (file:line set during implementation) | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| The `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` line in the bundle route | delete it | `pytest -q backend/tests/integration/test_audit_bundle_postgres.py` | step 7: `corpus.json` and `prisma-flow.json` `stream_heads` differ |
| The resolutions-only Select count in `journey.facts` | count `screening_observations` | same | step 8: R1's journey changes after R2 submits |
| GOO-300's `full_text`/restricted strip, exercised through the bundle | skip the strip | same | step 4: a sentinel appears in `corpus.json` |
| The `SHA256SUMS` check in `verify_bundle` | `return` early | `pytest -q backend/tests/unit/services/test_audit_bundle.py -k tampered` | a tampered member verifies |
| The collector's bundle checksum recomputation | skip it | `pytest -q evals/academic-journey-v1/tests -k tampered` | a tampered bundle is accepted |
| The collector's refusal on missing evidence | skip it | `pytest -q evals/academic-journey-v1/tests -k refuses` | the report is written |
| The UTF-16 to code-point conversion in `DraftClaimsPanel` | use `selectionStart` directly | `pnpm --dir frontend exec vitest run src/components/research/__tests__/DraftClaimsPanel.test.tsx -t "code points"` | the posted `start_char` is off by one |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent backend/src/services/research_engine/journey.py backend/src/services/research_engine/audit_bundle.py backend/src/api/research_engine/journey.py
pytest -q backend/tests/unit/services/test_journey_stages.py backend/tests/unit/services/test_audit_bundle.py backend/tests/unit/api/test_research_journey_routes.py
pytest -q backend/tests/unit/architecture backend/tests/unit/api
pytest -q evals/academic-journey-v1/tests evals/academic-writing-baseline-v1/tests
(cd backend && python ../scripts/ci/check_alembic.py)          # head unchanged (d7f9b1c3e5a8): no migration
python scripts/ci/generate_openapi.py --check && pnpm --dir frontend check:api-types
pnpm --dir frontend exec vitest run src/components/research-engine src/components/research
MOCK_PROJECT_IDENTITY_E2E=1 BASE_URL=http://localhost:3040 pnpm --dir frontend exec playwright test e2e/nous-flows/research-project-identity.spec.ts --project=chromium --config=playwright.nous.config.ts
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists.** Report each of these as NOT RUN, never as passing:
- **PostgreSQL proof:** needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **Mocked Playwright:** needs a browser and the mock harness (`frontend/e2e/nous-flows/README-project-identity.md`).
- **Live Playwright:** needs `RESEARCH_JOURNEY_LIVE=1`, the six saved principal states, a deployed stack that includes GOO-299 to GOO-308 and #1747, and authorization for real-provider spend.
- **The two consenting real projects:** need signed consent and real researchers acting as principals. Neither exists today, so both `real-*` trials are NOT RUN until they do.
- **S2 worker restart and S3 provider outage:** need explicit cluster authorization or a safe induction.
- **`claim_correctness` via judge:** needs judge credentials; without them it is `judge_unavailable` and unscored.

## Closure evidence list (Linear)

1. **Source:** the merge SHA on `develop`, the green Test Pipeline link, and the `openapi-contract` and oasdiff job links (additive).
2. **Deployed:** `GIT_SHA` from the backend (it equals `manifest.json.deployment_sha` in the downloaded bundle), `kubectl -n rag-dev exec deploy/backend -- alembic current` = `d7f9b1c3e5a8 (head)`, and the Vercel deployment id for the frontend.
3. **Model and harness identity:** provider and model per run (the runs' manifests in `methods.json`), the digest-pinned runner image, the `collect.py`, `protocol.json` and corpus digests (the collector prints them), and the Playwright and tool versions.
4. **Browser traces:** `trace.zip` for the live spec and the mocked spec's junit (`playwright-nous-junit.xml`, `frontend/playwright.nous.config.ts:32-37`).
5. **PostgreSQL results:** junit for `test_audit_bundle_postgres.py` and the GOO-300 to GOO-307 PostgreSQL tests on the same SHA.
6. **Checksums:** `SHA256SUMS`, `sha256sum -c` output, the manifest sha, and each part's `body_sha256` from a second download (it must be equal).
7. **Independent review records:** `independent-review.json` per trial, from someone who is not the author, bound to the manifest sha, confirming offline reconstruction of methods, exclusions, facts and claim support.
8. **Mutation transcripts** for the seven guards above.
9. **Collector output** `result.json` with every denominator, plus the explicit NOT RUN list.

## Out of scope

Each item below has a `ponytail:` marker at its seam:
- a new project model;
- write paths or ledger events;
- `source_span` link authoring UI;
- per-panel rail invalidation;
- RIS/CSV renderings in the bundle;
- persisting journey state;
- establishing the baseline (this ticket freezes the protocol; the trials establish it);
- running the disruptive `tests/load/run-shared-dev-max.sh --full`.

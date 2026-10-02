# GOO-320 Superseding Review Versions + Reconciled Update Accounting Plan (Academic R8)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A supervisor can create an immutable **review version** that names its parent version and exactly one accepted GOO-319 delta. The version freezes its corpus (live report ids), the protocol, strategy and input versions it rests on, and the decisions it carries forward. An unchanged work keeps its parent screening decision **by reference** to the original resolution id, so attribution stays with the original reviewers. A new, changed or corrected/retracted work becomes **new assigned review work** in a targeted GOO-301/302 queue, and its earlier decision is kept as the predecessor, never overwritten. Update accounting (previous studies, new records, duplicates, amended inclusion, full-text availability, changed sources) is derived from retained record, report and decision events and must reconcile arithmetically with the parent's GOO-303 PRISMA flow; a retried import cannot double-count. Only claims, assessments and artifacts that depend on a changed or corrected source go stale, through GOO-307's walk. A successor release (GOO-315) is linked as superseding the parent's release without touching it, and each version's export independently reconstructs its corpus, decisions, evidence and release bytes.

**Architecture:**
- **Two insert-only tables**, each with GOO-309's `prevent_research_insert_only_mutation()` trigger:
  - `research_review_versions`: the version chain (root + successors).
  - `research_review_release_links`: one row linking a version to a GOO-315 release, with the release it supersedes.
- **One pure module**, `review_update_rules.py`: carry-forward eligibility, required-work selection and accounting reconciliation.
- **One ledger family**, `research_review_update`; **one service**, `review_update_service.py`; **one router**, `api/research_engine/review_versions.py`.
- **Reuse:** new review work = `screening_service.create_queue` + `assign` (GOO-301/302); accounting = `prisma.derive_prisma_flow` over `prisma_service.load_inputs` (GOO-303); staleness = a `graph_part` in GOO-307's `_graph`, like GOO-309/310; release lineage reads GOO-315 rows.
- **Derived only:** work status, accounting and staleness are computed on read. Nothing is stamped. `ResearchRun` and `GeneratedDraft.version` are never reused as the review version.

**Tech stack:** FastAPI, SQLAlchemy async, Alembic, the PostgreSQL integration fixture (`postgres_container`, `backend/tests/integration/conftest.py:208`), openapi-typescript, Next.js with TanStack Query and Vitest.

**Stacking (read first):**
- This PR stacks on **GOO-319** (`feat/goo-319-scheduled-updates`), which stacks on GOO-318 → GOO-317 … → GOO-310. It needs GOO-315 merged for release links.
- Line numbers below are for `c935dde6e`; re-locate them after each rebase. GOO-315 names are *planned by GOO-315* and must be re-checked first.

### Consumed names

| Consumed | Name (path:line) | Property relied on |
|---|---|---|
| Accepted delta (GOO-319, *planned in this train*) | `search_update_service.accepted_delta(db, collection_id, execution_id)`; `research_search_execution_results` (`execution_id`, `baseline_execution_id`, `import_receipt_id`, `corpus_snapshot`, `delta`, `delta_hash`) | Classes `new/changed/corrected_retracted/unchanged/unknown` per report, with evidence. |
| Screening queue (GOO-301) | `ScreeningQueue` (`backend/src/models/screening.py:47`): `protocol_version_id, criteria_hash, stage, report_ids, supersedes_queue_id` | Immutable corpus snapshot per queue. |
| Create/assign (GOO-301/302) | `screening_service.create_queue(db, context, actor_user_id, data)` (`backend/src/services/research_engine/screening_service.py:562`), `ScreeningQueueCreate{protocol_version_id, stage, report_ids, supersedes_queue_id, suggestion_step_id, idempotency_key}` (`backend/src/schemas/research_engine.py:467`), `assign` (`:710`) | SUPERVISE; idempotent per Collection key; explicit `report_ids`. |
| Resolutions (GOO-302) | `ScreeningResolution` (`backend/src/models/screening.py:162`): `queue_id, report_id, basis, outcome, exclusion_reason, input_observation_ids, criteria_hash, supersedes_resolution_id, event_id`; resolved bases `("single","agreement","adjudicated")` (`screening_service._RESOLVED_BASES`, `:112`); `_resolution_tips` (`:274`) | The tip resolution is the current decision; `event_id` → actor attribution. |
| Criteria hash | `screening_rules.criteria_hash(snapshot)` (`backend/src/services/research_engine/screening_rules.py:57`) | Equal hash = same eligibility criteria for a stage. |
| PRISMA (GOO-303) | `prisma_service.load_inputs(db, context)` (`backend/src/services/research_engine/prisma_service.py:93`), `prisma.PrismaInputs`/`Record`/`Report`/`Outcome`/`Attempt`/`Merge` (`backend/src/services/research_engine/prisma.py:27-95`), `derive_prisma_flow` (`:134`), `PrismaInconsistency` (`:98`), `package` (`:342`) | `Record.key` unique; a flow is derived from retained rows only. |
| Identity (GOO-299) | `ResearchReport.merged_into_report_id`, `identity_service.live_reports` (`:174`), `analysis_unit` (`:121`) | Merges followed; study units. |
| Document ↔ report | `acquisition_service.document_reports(db, collection_id)` (`backend/src/services/research_engine/acquisition_service.py:432`) | `{document_id: report_id}`. |
| Evidence graph (GOO-307) | `release_rules.node`, `source_node`, `dependents` (`backend/src/services/research/release_rules.py:56-63,146`); `draft_release_service.Graph`, `_graph` and its `graph_part` loop (`backend/src/services/research/draft_release_service.py:111,129,194`) | `stale_nodes()` derived on read; parts return `(edges, changed)`. |
| Release (*planned by GOO-315*) | immutable release row (id, `collection_id`, state, package hash) and its export | Never mutated; a successor is a new release. |
| Corpus export | `corpus_export.build_package` (`:616`), `seal` (`:99`) | Deterministic sealed package. |
| Current protocol | `identity_service.current_protocol_version_id` (`:103`) | Approved version. |
| Ledger | `lock_aggregate_stream` (`ledger.py:1318`), `append_decision` (`:1321`), `replay_decisions` (`:1420`), `_RATIONALE_EVENTS` (`:424`), `_FAMILIES` (`:2235`), `_replayed_event` (`identity_service.py:76`) | Caller-owned append. |
| Roles | `ResearchAction.SUPERVISE`/`VIEW`, `resolve_project` (`project_access.py:28,186`) | Ownership grants no decision role. |
| Bundle | `audit_bundle._sealed_part` (`:86`), `gather_parts` (`:315`) | One reader per part. |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| **Review-version identity** | A new table `research_review_versions` with its own id and `version_number`. The **root** version (number 1, no parent, no delta) freezes the project's state at creation: live report ids, every current resolution tip id per stage, the governing protocol version, and the parent PRISMA body hash. A **successor** names `parent_review_version_id` (must be the chain tip, `UNIQUE(parent_review_version_id)`, 409 `Review version is stale; reload`) and `(accepted_execution_id, delta_hash)`. `content_hash = canonical_json_sha256(body)`. Never `ResearchRun` (a run is one engine execution) and never `GeneratedDraft.version` (a draft counter). | An explicit, linear, content-addressed chain is the review version. Reusing a run or draft counter would conflate execution and manuscript history with review history. |
| Accepting a delta | The successor request carries `execution_id` and `delta_hash`; the service re-reads `accepted_delta` and requires the hash to match (409 `Delta changed; reload`) and the delta's `baseline_execution_id` to be the execution the parent accepted (or, for the first successor, the schedule's baseline). One delta can be accepted by at most one version (`UNIQUE(accepted_execution_id)`). | Prevents skipping a delta or applying one twice. |
| **Carry-forward rule** | For each stage, a parent decision is carried **by reference** (`{report_id, stage, resolution_id, event_id, outcome, basis}`, no copied actor fields; attribution resolves through `event_id`) when **all** hold: (1) the report's delta class is `unchanged`; (2) the parent resolution is a tip with a resolved basis; (3) the successor's protocol `criteria_hash` for that stage equals the resolution's `criteria_hash`; (4) the report was not merged or split since the parent. Anything else is **not** carried. | "Unchanged decisions carried forward by reference (attribution kept)." Carry-forward is defined against changed protocols by the criteria hash: same criteria, same decision; different criteria, new work. |
| **Required new work** | `required_work[stage]` = reports whose class is `new`, `changed` or `corrected_retracted`, plus `unchanged` reports that fail (3) or (4). `unknown` reports are listed under `needs_attention` with their reason and are neither carried nor queued automatically; a supervisor resolves them by including them in a later version or marking them `carried_with_uncertainty` (explicit flag, rationale required). Full-text stage work is required only for reports whose title/abstract outcome is (or becomes) `include`. | "Changed evidence/eligibility creates new assigned review work." Unchanged title/DOI with a changed record is `changed`, so it is re-reviewed. `unknown` never silently passes as unchanged. |
| Creating the queue | After the version commit, the service calls `screening_service.create_queue` with `report_ids = required_work[stage]`, the successor protocol version, and `idempotency_key = f"review-version:{version_id}:{stage}"`, then `assign` for the reviewers named in the request. Derived `work_status[stage]` = `queued` when a queue exists whose `report_ids` equal the required set, else `queue_missing` (the UI retries with the same key). | Reuses GOO-301/302 unchanged. The deterministic key makes the second commit idempotent, and the version row never needs updating. |
| Predecessor decisions | A re-reviewed report's parent resolution stays in its parent queue; the successor's export lists it as `predecessor` next to the new resolution. Nothing in the parent queue is superseded or edited. | "Revised ones preserve predecessors and rationale"; "never silently overwrite human decisions." |
| Missing historical data | A root-version report with no resolution tip in a stage is recorded as `decision_missing` (stage, report); one whose resolution's `event_id` no longer replays is `attribution_missing`. Both are shown and exported, never defaulted to `exclude`. | "Label missing historical decision data explicitly." |
| **Update accounting** | `review_update_rules.accounting(parent_flow, successor_flow, delta, carried, receipt_record_keys)` returns PRISMA-2020-for-updated-reviews boxes: `studies_in_previous_version`, `reports_in_previous_version`, `new_records_identified` (by source), `duplicates_removed`, `records_screened`, `excluded` (by reason), `reports_sought`/`not_retrieved` (full-text availability), `amended_inclusion` (carried include → new exclude or vice versa), `changed_sources`, `corrected_retracted`, `new_studies_included`, `total_studies_included`. Both flows come from `derive_prisma_flow` over `load_inputs` filtered to the version's frozen records. **Reconciliation:** `total_studies_included == studies_in_previous_version − amended_out + new_studies_included + amended_in`, and `new_records_identified == |successor record keys − parent record keys|`; any failure raises `PrismaInconsistency` (409 `Update accounting does not reconcile`). | Counts derive from retained events; `Record.key` uniqueness and the GOO-319 `dedup_key` mean a retried import adds no keys, so it cannot double-count. |
| **Selective invalidation (GOO-307 walk)** | `review_update_service.graph_part` adds, for the current tip version's accepted delta: edges `("report", r) → source_node(doc, hash, text_sha)` for each document of a `changed` or `corrected_retracted` report (via `document_reports` and the source hashes already used by `_graph`), and puts `("report", r)` in `changed`. Everything downstream (accepted values → links → assessments → releases, appraisals, evidence tables, synthesis) goes stale by the existing walk; unrelated nodes do not. | "Invalidate only affected claims/assessments/artifacts via the GOO-307 walk." No stamping, no upstream writer changes. |
| **Release relationship** | `research_review_release_links {review_version_id, release_id, supersedes_release_id}`: `release_id` must be a GOO-315 release in the same Collection created after the version; `supersedes_release_id` must be the release linked to the parent version (409 otherwise). `UNIQUE(review_version_id)`, `UNIQUE(supersedes_release_id)`. The parent release row is never touched. | "Attach superseding release relationships without mutating parent releases." |
| Who | Create versions, accept deltas, mark `carried_with_uncertainty`, link releases: SUPERVISE. Read and export: VIEW. Screening the new work: the GOO-301/302 roles, unchanged. | Same explicit-role split as GOO-301. |
| Exports | `GET .../review-versions/{id}/export` → sealed `nous.academic.review-version.v1` with corpus (report ids + identifiers), carried decisions resolved to their resolution rows and actors, new-work queues and their resolutions, predecessors, `needs_attention`, accounting with both PRISMA bodies, the accepted delta, and the linked release id + package hash. Built only from rows referenced by that version, so parent and successor exports are independent. | "Parent and successor exports independently reconstruct corpus, decisions, evidence and release bytes." Release bytes come from GOO-315's own export by id. |

**Migration head:** new revision `d4a6c8e0f2b3_create_review_versions.py`, `down_revision = "c2f4b6d8e0a1"` (GOO-319). Two tables with insert-only triggers; copies `_deny_data_api`; imports nothing from `src`; `downgrade()` drops triggers and tables only.

---

### Task 1: Pure rules (`review_update_rules.py`)

**Files:**
- Create `backend/src/services/research_engine/review_update_rules.py`.
- Create `backend/tests/unit/services/test_review_update_rules.py`.

```python
@dataclass(frozen=True) class ParentDecision: report_id; stage; resolution_id; event_id; outcome; basis; criteria_hash
def carry_forward(delta: Mapping[str, Mapping], parents: Sequence[ParentDecision], criteria: Mapping[str, str], identity_changed: set[UUID]) -> tuple[list[dict], dict[str, list[UUID]], list[dict]]   # carried, required_work, needs_attention
def missing_history(reports: Sequence[UUID], parents: Sequence[ParentDecision], stages: Sequence[str]) -> list[dict]
def accounting(parent_flow: Mapping, successor_flow: Mapping, delta: Mapping, carried: Sequence[Mapping], parent_keys: set[str], successor_keys: set[str]) -> dict   # raises PrismaInconsistency
def version_hash(body: Mapping) -> str
```

**Tests (write first):**
- `test_unchanged_carried_by_reference_without_actor_copy`
- `test_changed_and_corrected_go_to_required_work`
- `test_changed_criteria_hash_blocks_carry_forward`
- `test_unknown_goes_to_needs_attention_not_carried`
- `test_missing_resolution_labelled_decision_missing`
- `test_accounting_reconciles_and_rejects_mismatch`
- `test_retried_import_keys_do_not_double_count`

**Commit:** `feat(research): pure review carry-forward and update accounting rules (GOO-320)`

---

### Task 2: Models + migration

**Files:**
- Create `backend/src/models/research_review_version.py` (`ResearchReviewVersion`, `ResearchReviewReleaseLink`); export them.
- Create `backend/alembic/versions/d4a6c8e0f2b3_create_review_versions.py`.
- Modify `backend/tests/integration/test_screening_queue_postgres.py` (`_REBUILT_TABLES`, `_upgrade`).

| Table | Columns | Constraints |
|---|---|---|
| `research_review_versions` | `id, collection_id FK RESTRICT, version_number INT, parent_review_version_id FK self NULL, accepted_execution_id FK research_search_executions NULL, delta_hash CHAR(64) NULL, protocol_version_id FK, strategy_version VARCHAR(80) NULL, input_versions JSONB, report_ids JSONB, carried JSONB, required_work JSONB, needs_attention JSONB, missing_history JSONB, prisma_body_hash CHAR(64), content_hash CHAR(64), rationale TEXT NOT NULL, created_by_id FK users, actor_role VARCHAR(16) CHECK = 'supervisor', created_at` | `UNIQUE(parent_review_version_id)`; partial unique `uq_review_version_root (collection_id) WHERE parent_review_version_id IS NULL`; `UNIQUE(accepted_execution_id)`; `(parent_review_version_id IS NULL) = (accepted_execution_id IS NULL)`; `(parent_review_version_id IS NULL) = (version_number = 1)` |
| `research_review_release_links` | `id, collection_id, review_version_id FK UNIQUE, release_id FK (GOO-315), supersedes_release_id FK NULL UNIQUE, linked_by_id FK users, created_at` | — |

Partial indexes declare both `postgresql_where=` and `sqlite_where=`.

**Check:** single head `d4a6c8e0f2b3`; offline `--sql` contains `research_review_versions`.
**Commit:** `feat(research): insert-only review versions and release links (GOO-320)`

---

### Task 3: Ledger family `research_review_update`

`ledger.py` vocabulary, validators and `_FAMILIES` entry (subject `review_version`, `requires_subject_version=False`); `review_update.versioned` in `_RATIONALE_EVENTS`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `review_update.versioned` | `collection_id, review_version_id, parent_review_version_id, version_number, accepted_execution_id, delta_hash, protocol_version_id, carried_count, required_work_counts, needs_attention_count, content_hash` | `supervisor` |
| `review_update.release_linked` | `collection_id, review_version_id, release_id, supersedes_release_id` | `supervisor` |

**Replay rules:** linear chain with contiguous `version_number`; each accepted execution appears once; a `release_linked` names a version already versioned in this stream and supersedes the release linked to its parent.

**Tests:** `test_review_replay_rejects_fork`, `test_review_replay_rejects_reused_delta`, `test_review_replay_rejects_wrong_superseded_release`.
**Commit:** `feat(research): research_review_update decision family and replay rules (GOO-320)`

---

### Task 4: Service + graph part

**Files:**
- Create `backend/src/services/research_engine/review_update_service.py`.
- Modify `backend/src/services/research/draft_release_service.py`: add `review_update_service.graph_part` to the `_graph` part loop (`:194`), after the GOO-310/311 parts.

```python
AGGREGATE_TYPE = "research_review_update"; SUBJECT_TYPE = "review_version"
async def create_root(db, context, actor_id, data) -> tuple[ReviewVersionResponse, bool]       # SUPERVISE
async def create_successor(db, context, actor_id, data) -> tuple[ReviewVersionResponse, bool]  # SUPERVISE; commit, then idempotent create_queue/assign per stage
async def ensure_work(db, context, actor_id, version_id) -> ReviewVersionResponse              # retries queue creation with the same keys
async def link_release(db, context, actor_id, version_id, data) -> ReleaseLinkResponse
async def list_versions(db, context) -> ReviewVersionListResponse     # chain, derived work_status, accounting, stale counts
async def accounting(db, context, version_id) -> UpdateAccountingResponse
async def graph_part(db, collection_id) -> tuple[list[Edge], set[Node]]
async def export_version(db, context, version_id) -> dict             # sealed nous.academic.review-version.v1
```

- **Write order:** `resolve_project(SUPERVISE)` → `lock_aggregate_stream(research_review_update, collection_id)` → `_replayed_event` → read tip, delta (`accepted_delta`), resolution tips, criteria hashes, PRISMA inputs → rules → insert + append + **one commit** → queue creation (its own idempotent commits).
- `create_successor` rejects when the parent's required work still has unresolved reports in its queues (409 `Parent review work is unresolved`), so accounting is always over settled decisions.

**Commit:** `feat(research): superseding review versions with carry-forward and targeted work (GOO-320)`

---

### Task 5: API + contracts + bundle part

**Files:** `backend/src/api/research_engine/review_versions.py` (`REVIEW_VERSIONS = "/projects/{project_id}/review-versions"`), registration, schemas, `audit_bundle._review_versions` part (`review-versions.json`), regenerated OpenAPI and frontend types.

| Route | Action | Notes |
|---|---|---|
| `GET {REVIEW_VERSIONS}` | VIEW | Chain with work status, accounting summary, needs-attention, stale counts. |
| `POST {REVIEW_VERSIONS}` | SUPERVISE | Root when no `parent_review_version_id`; successor otherwise; 201/200 replay; 409 stale tip, delta changed, unresolved parent work, accounting mismatch. |
| `POST {REVIEW_VERSIONS}/{id}/work` | SUPERVISE | `ensure_work`. |
| `POST {REVIEW_VERSIONS}/{id}/release` | SUPERVISE | Link a GOO-315 release. |
| `GET {REVIEW_VERSIONS}/{id}/accounting` | VIEW | Both PRISMA bodies plus update boxes. |
| `GET {REVIEW_VERSIONS}/{id}/export` | VIEW | Sealed JSON attachment. |

**oasdiff:** additive. **Expected: no ERR.**
**Tests** (`backend/tests/unit/api/test_review_version_routes.py`): `test_reviewer_cannot_create_version_403`, `test_owner_without_supervisor_403`, `test_export_read_only_no_commit`.
**Commit:** `feat(research): review version endpoints and audit bundle part (GOO-320)`

---

### Task 6: Structural guard

`backend/tests/unit/architecture/test_review_update_boundary.py`:
- **(a)** `review_update_service` never writes `ScreeningResolution`, `ScreeningObservation` or `ScreeningQueue` directly (only through `create_queue`/`assign`);
- **(b)** no module under `services/research_engine/` references `ResearchRun` or `GeneratedDraft.version` in `review_update_*`;
- **(c)** no `update(`/`delete(`/`merge` on the two new models or on GOO-315 release rows.

**Commit:** `test(research): guard review-version boundaries (GOO-320)`

---

### Task 7: PostgreSQL proof (one test)

`backend/tests/integration/test_review_versions_postgres.py` (`integration`, `requires_postgres`), reusing `screening_factory`, `_upgrade` (through `d4a6c8e0f2b3`), `seed_approved_protocol_binding`, GOO-319's seed for a succeeded execution with results, and GOO-315's release seed.

**`test_superseding_review_reconciles_carries_forward_and_targets_staleness`** runs these steps in order:
1. **Seed parent:** reports R1–R6, where R2 and R3 are duplicate reports of study S; title/abstract and full-text resolutions by reviewers A and B (dual) and adjudicator J; R6 has no resolution (historical gap); claims/assessments citing R1's and R4's documents; a verified release P.
2. **Root version:** created by supervisor; `missing_history` lists R6 `decision_missing`; its PRISMA body hash equals `prisma.package(derive_prisma_flow(load_inputs))`. Reviewer → 403, foreign → 404.
3. **Delta:** a GOO-319 execution with R1 `unchanged`, R4 `changed`, R5 `corrected_retracted` (Crossref notice), new N1/N2 (N2 duplicate report of S), R3 `unknown/not_returned`.
4. **Successor:** carried = R1 (and R2) by `resolution_id`, attribution resolves to A/B/J through `event_id`; required work = R4, R5, N1, N2; needs-attention = R3 with reason. A targeted title/abstract queue exists with exactly those report ids; R4's parent resolution is still the tip of the parent queue and is listed as `predecessor`.
5. **Idempotency/concurrency:** two concurrent successor requests on the same parent → one version (`UNIQUE(parent_review_version_id)`), the other 409 stale; same idempotency key → 200 replay; queue creation retried via `/work` → no second queue.
6. **Accounting:** after screening the new work (N1 include, N2 merged into S as duplicate, R5 exclude), `studies_in_previous_version`, `new_studies_included`, `duplicates_removed`, `amended_inclusion` (R5 include → exclude) reconcile; a re-run of the GOO-319 execution adds no record keys and the totals do not change.
7. **Targeted staleness:** claims/assessments citing R4's and R5's documents are `stale`; those citing R1 are not; no row changed (`xmin` unchanged).
8. **Changed protocol:** approve an amendment that changes the title/abstract criteria; a further successor carries **nothing** at that stage and requires every report.
9. **Release link:** link successor release P2 superseding P; linking a release that does not supersede the parent's → 409; P's row is byte-identical.
10. **Exports:** parent and successor exports each rebuild corpus, decisions (with actors), accounting and release id/hash without reading the other; seals verify.
11. **Archived/deleted, insert-only, replay, downgrade:** writes 409/reads 200/404 after deletion; UPDATE/DELETE → `55000`; replay passes; downgrade drops only the two tables.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_review_versions_postgres.py`; without a database **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for superseding review versions (GOO-320)`

---

### Task 8: Frontend

**Files:**
- Create `frontend/src/types/api/research-review-version-contract.ts`.
- Modify `frontend/src/services/researchEngineService.ts`: `listReviewVersions`, `createReviewVersion`, `ensureReviewWork`, `linkReviewRelease`, `getReviewAccounting`, `exportReviewVersion`.
- Create `frontend/src/components/research-engine/ReviewVersionsPanel.tsx`, mounted in `ProjectWorkflow.tsx` under the GOO-319 schedules: version timeline, "Create update from delta" (pick an accepted execution), carried vs new-work counts, needs-attention list with reasons, a link to each targeted queue, update accounting as previous/new/total columns, stale badge counts and the release lineage.
- Tests in `__tests__/ReviewVersionsPanel.test.tsx`: `unknown never counted as carried`, `missing history labelled`, `create hidden without supervisor`.

**Commit:** `feat(frontend): superseding review versions panel (GOO-320)`

---

## Mutation verification

Record in the test docstring and `docs/testing/agent-orchestration-mutation-checks.md` (GOO-320 section).

| Guard | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| Carry only `unchanged` | carry `changed` too | `pytest -q backend/tests/integration/test_review_versions_postgres.py` | step 4: R4 carried |
| Criteria-hash check | skip it | same | step 8: decisions carried across changed criteria |
| `unknown` → needs-attention | carry it | unit `-k unknown` + step 4 | R3 carried |
| Accounting reconciliation | return without checking | unit `-k reconciles` | mismatch accepted |
| `graph_part` edges per changed report | add edges for every report | step 7 | R1's claims stale |
| `UNIQUE(parent_review_version_id)` | drop in test migration | step 5 | two successors |
| Superseded-release check | skip | step 9 | wrong supersession accepted |

Restore, confirm empty `git diff`, rerun green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_review_update_rules.py backend/tests/unit/services/test_research_decision_ledger.py
pytest -q backend/tests/unit/services/test_release_rules.py backend/tests/unit/services/test_audit_bundle.py backend/tests/unit/api backend/tests/unit/architecture
(cd backend && python ../scripts/ci/check_alembic.py)      # single head d4a6c8e0f2b3
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research-engine
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists:**
- **PostgreSQL proof** (this test plus GOO-318/319's on the same SHA): needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **Methods-expert review** of the update-accounting boxes against PRISMA 2020 for updated reviews: none available today; record the verdict in the Linear closure.
- **Live journey:** needs a deployed stack with `d4a6c8e0f2b3`, a real GOO-319 delta and the saved principals.

## Authenticated journey list for Linear closure

On dev after deploy (`alembic current` shows `d4a6c8e0f2b3 (head)`).

1. **Root:** a supervisor freezes the root version; missing historical decisions are labelled.
2. **Update:** create a successor from a real GOO-319 delta; carried and new-work counts match the delta classes; the targeted queue opens for assigned reviewers.
3. **Screen:** reviewers resolve the new work; accounting shows previous/new/total and reconciles.
4. **Stale:** only claims citing changed/corrected sources show `Stale`.
5. **Release:** link the successor release; the parent release downloads byte-identical.
6. **Export/bundle:** both version exports and `review-versions.json` verify.
7. **Deny:** reviewer create → 403; foreign → 404.

Record SHA/PR, CI and oasdiff links, both exports, junit, mutation transcripts and the NOT RUN list.

## Seams

- **Upstream (GOO-319):** accepts one delta by `(execution_id, delta_hash)`; never re-classifies.
- **Upstream (GOO-301/302/303):** creates queues and assignments only through their services; reads resolutions and PRISMA inputs.
- **Upstream (GOO-315):** links releases; never changes them. Depositing a superseding release as a Zenodo new version is GOO-318's ponytail seam.

## Out of scope

`ponytail:` markers at each seam: automatic acceptance of deltas; carrying forward extraction/appraisal values (they are re-staled and re-accepted through GOO-304/309, not carried); branching review versions; a persisted stale flag; living-review publication cadence rules.

---

## Amendment 2026-10-01: as implemented

The plan above is kept as written. Where the implementation differs:

- **Root decisions:** the root's `carried` holds its own resolved tips on live reports (by reference). `input_versions.decision_tips` freezes every current tip the PRISMA loader would choose (latest non-superseded queue per stage and report), so the root's `prisma_body_hash` equals the live flow. Stream heads in every version's flow leave out the `research_review_update` stream itself (it moves on append).
- **Successor records:** a successor's PRISMA records are the parent's plus the accepted delta's own receipts (the scheduled search and its citation chases). A report imported any other way is `needs_attention` with reason `not_in_delta`. Another schedule's import never joins the version.
- **Missing history:** following the carry rule, an unchanged report with no resolved parent decision is neither carried nor queued; it stays `decision_missing` in every successor, a changed protocol included. `attribution_missing` is checked as "the tip's event is not an event of this Collection"; the event FK makes it practically unreachable.
- **Uncertainty:** `carry_with_uncertainty` is a list on the successor request (only `unknown` reports, 422 otherwise); those references carry `uncertain: true` and the version's rationale.
- **Full text:** `required_work.full_text` is the frozen full-text set (carried include, full text not carryable). The full-text queue is created only after the title/abstract work settles, over that set plus the new title/abstract includes (`waiting_on_title_abstract` until then). Derived statuses add `queue_mismatch`.
- **Reviewers:** named on the request, checked up front (422) and stored in `input_versions.reviewer_user_ids`; `ensure_work` re-resolves SUPERVISE per stage, finds each queue by its key before creating it, and assigns only reviewers not already active. Right after a version commit a refusal is logged and left `queue_missing`; an explicit `/work` retry raises it.
- **Fork guard:** there is no separate tip check; `UNIQUE(parent_review_version_id)` alone answers 409 `Review version is stale; reload`, and `UNIQUE(accepted_execution_id)` answers `Delta already accepted by a review version`.
- **Accounting:** both flows are re-derived from retained rows. A version with a successor uses the decisions frozen at the successor's creation (`parent_decision_tips`) and full-text attempts and merges up to the successor's stream heads. Each flow is cross-checked against the study units of its decisions; a parent include left in `needs_attention` is `withheld_reports`, not an amendment. While the new work is unresolved the response carries `error` (200); a non-reconciling result is 409. `flow_matches_frozen_hash` re-derives the creation-time body.
- **Staleness:** `graph_part` adds `report -> document` edges for the tip version's `changed`/`corrected_retracted` reports, and `_graph` bridges each such document to the source revisions pinned from it. Ceiling: only the tip's delta stales, so a claim re-assessed on a still-changed report stays stale until a later version accepts a delta where it is unchanged.
- **Release links:** only a verified GOO-315 release can be linked. A successor's release must be created after the version; the root may link an earlier release.
- **Bundle:** `review-versions.json` uses schema `nous.academic.review-versions.v1`.
- **Proof step 5:** the two concurrent successors accept the first-fire deltas of two schedules, so only `UNIQUE(parent_review_version_id)` can refuse the fork.
- **Current limits (ponytail):** a version's flow reads report merge and study-link state as it is now. The tip's stale counts walk the whole graph on every list.

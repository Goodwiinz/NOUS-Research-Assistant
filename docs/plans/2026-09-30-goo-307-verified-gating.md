# GOO-307 Verified Draft Gating + Selective Invalidation Plan (Academic R4)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Every draft version is a `candidate` until an ADJUDICATOR or SUPERVISOR promotes that exact version, and promotion is what makes it `verified`. The promotion binds the version's content hash to the accepted, current GOO-306 assessments of every factual assertion in it. An unsupported or unresolved claim blocks promotion, and the response lists each one. An upstream change (source, form, extraction or claim) marks only the releases that depend on it `stale`. The stale release gets a `release.staled` event and is never deleted or reverted. `is_current`, a `passed` review and LLM verdicts never count as verification.

**Architecture:** This adds one table, `draft_releases` (insert-only apart from one guarded `stale_at` stamp), one ledger family `research_release`, one pure module `release_rules.py` (the gate and the dependency walk) and one service `draft_release_service.py`. Status is **derived**: no release row means `candidate`, a live row means `verified`, and a row with `stale_at` set and no live successor means `stale`. `generated_drafts` gets no status column, so there is no pointer to drift. The existing review gate (`_require_passing_citation_review`, `backend/src/services/research/draft_generation_service.py:1405-1464`) and the revision stale-base guard (`:1260-1337`) stay as they are, as candidate hygiene. The brief's `~L1029-1112`/`~L919-970` are the audit SHA's numbers; on this branch the code sits at the lines above.

**Tech stack:** FastAPI, SQLAlchemy async, Alembic, the PostgreSQL integration fixture (`postgres_container`, `backend/tests/integration/conftest.py:208`), openapi-typescript, Next.js with TanStack Query and Vitest.

**Stacking (read first):**
- The PR stacks on **GOO-306** (claims, evidence links, assessments), which stacks on GOO-305 → GOO-304 → GOO-303 → … on `feat/goo-304-extraction-forms`.
- It also needs **GOO-297**: `DraftTaskResult`, `finish_task` and `reconcile_task`, which exist only on `feat/goo-297-task-terminal-results` (PR **#1753**, `backend/src/models/draft_task_result.py`, `services/research/draft_generation_service.py:125-223` on that branch).
- This PR opens **after #1753 merges to `develop`**, and the academic stack is rebased onto that `develop` first. Line numbers in `draft_generation_service.py` below are for this branch, so re-locate them after the rebase, because #1753 shifts that file by about 115 lines.

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| Where status lives | It is derived from `draft_releases` rows and never stored on `generated_drafts`. `release_status(draft)` is `verified` if a row has `stale_at IS NULL`, `stale` if every row is stale, and `candidate` if there are no rows. | Every generated or revised version is a candidate by default without any writer having to set it. Revision (`:1315`, `:1336`) and generation (`:584-620`) stay untouched. |
| Release row | `draft_releases`: `id, collection_id FK RESTRICT, draft_id FK generated_drafts RESTRICT, draft_version INT, content_hash CHAR(64), claim_version_ids JSONB, assessment_ids JSONB, interpretation_claim_version_ids JSONB, protocol_version_id NULL, policy_version SMALLINT, promoted_by_id FK users, actor_role VARCHAR(16) CHECK IN ('adjudicator','supervisor'), rationale TEXT NULL, created_at, stale_at NULL, stale_event_id NULL`. `UNIQUE(draft_id) WHERE stale_at IS NULL` (`uq_draft_releases_live`). `CHECK ((stale_at IS NULL) = (stale_event_id IS NULL))`. | The row snapshots exactly what authorized the release, so a prior version can be rebuilt from its ids. The partial unique index is the backstop against a second live release. Id lists are JSONB because they are only read as sets, as with GOO-304's `observation_ids`. |
| Draft deletion | GOO-306 already adds a `DraftRetainedError` pre-check to `delete_draft` (`draft_generation_service.py:1904-1951`), which the route maps to 409. This plan adds "has a `draft_releases` row" to **that same check**. The FK `RESTRICT` is the backstop. | The ticket says verified versions are "never silently deleted". GOO-297 made its FK-less choice because task rows must outlive drafts. A release must not outlive the content it certifies. |
| Content binding | The promote body carries `content_hash`. The server recomputes `sha256(draft.content.encode())`, the same recipe as `DraftReview` (`:1394`) and GOO-297 `finish_task`. Any mismatch, or `{draft_id}` and `{version}` naming different rows, returns 409 `Draft content changed; reload`. If a `draft_task_results` row names this `artifact_id`, its `artifact_version` and `artifact_hash` must equal the draft's, or the response is 409 `Draft does not match its task artifact`. | This is "a promotion may not bind another task's artifact": the promoted bytes are the exact bytes a completed task (or a synchronous revision, which has no task row) produced. |
| Who promotes | A new `ResearchAction.RELEASE` that requires **ADJUDICATOR or SUPERVISOR**. `_DECISION_ROLE` (`backend/src/services/research_engine/project_access.py:37-41`) becomes `dict[action, frozenset[role]]`. The 403 detail stays `f"{role} role required"` for the single-role actions and is `adjudicator or supervisor role required` for RELEASE. `RELEASE` joins `_MUTATING_ACTIONS` (`:42`). `actor_role` is `adjudicator` when the user holds it, otherwise `supervisor`. | An ADJUDICATOR accepts claim assessments, and a SUPERVISOR holds methods-release authority, which is how protocol approval works (#1710). Both are explicit assignments that are independent of workspace edit rights, as GOO-294 requires. `# ponytail: no promoter≠assessor rule; add a separation-of-duties check if methods review asks.` |
| Lock order | `resolve_project(RELEASE)` takes Workspace SHARE, then Collection UPDATE, then **reloads roles after the lock** (`project_access.py:196-289`). Next comes `lock_aggregate_stream(research_release, collection_id)` (`backend/src/services/research_decisions/ledger.py:445`), then the gate reads, then insert, append and **one commit**. Role revocation (`backend/src/api/research_engine/projects.py:442-469`) goes through `resolve_project(MANAGE)`, so it queues on the same Collection lock. GOO-304/305/306 writers (including the extraction worker's `lock_active_project`) also hold Collection UPDATE, so no assessment can change between the gate and the commit. | This reuses the proven order. No new locks are added, and the race is closed by the existing role reload. |
| Stream | There is one `research_release` stream per project (`aggregate_id = collection_id`, `subject_type="draft_release"`, `requires_subject_version=False`). | Invalidation crosses drafts inside a project, so one stream keeps `release.staled` and `release.promoted` in a single total order. The Collection lock already serializes them. |
| Duplicate/concurrent promote | This works in three layers. (1) The idempotency key is replayed through `identity_service._replayed_event` (`backend/src/services/research_engine/identity_service.py:75`) and returns the original release. (2) Under the lock, if a live release for `draft_id` exists with the same `content_hash`, the service returns it (`200`, `replayed=true`) and writes nothing. (3) As a SQLSTATE backstop, an `IntegrityError` on `uq_draft_releases_live` rolls back and re-reads the live row, the same shape as GOO-301 `a51622bc2`. | Any number of promotes produces exactly one live release, and every caller sees the same terminal result. It is the GOO-297 "exactly-once terminal" idea with the partial unique index as the guard. |
| What must be assessed | The assertion list is **GOO-306's `research_claim_versions` tips bound to this exact `(draft_id, draft_content_hash)`**. Coverage is checked against GOO-292's segmentation (`CitationVerificationService._claim_observations`, `backend/src/services/research/citation_verification_service.py:330-380`), which gets a pure `assertion_spans(content) -> [(start, end, text)]` extracted from its boundary regex (`:336`) so that both share one splitter. Every non-heading span must overlap at least one claim version (factual or interpretation). Otherwise the blocker is `unclaimed_assertion`. | The heuristic classifier admits it is incomplete (`factual_classification_complete: False`, `:178`), so a person must classify every sentence and nothing passes by default. |
| Factual claim passes iff | It has a `research_claim_assessments` tip (GOO-306, one per claim version, and the DB enforces `actor_role='adjudicator'`) that meets four conditions: (a) its stance is in `SUPPORTING_STANCES = {"supporting"}`, which covers a supported *statement of uncertainty* such as "evidence is inconclusive"; (b) every `link_ids` entry is a live link tip (`status='linked'`, not superseded) of kind `extraction` or `source_span`; (c) none of those links is stale under the dependency walk; and (d) the assessment is the tip bound to the snapshot being promoted. | The blocker codes are `unassessed` (no links and no tip), `model_only` (a live link has `research_claim_stance_observations` rows but there is no assessment tip, which is GOO-306's definition), `opposed` (`opposing`), `unresolved` (`neutral`, `not_addressed` or `unresolved`), `legacy_only` (the cited links are all `legacy_unanchored`; GOO-306 says these never promote), `superseded_assessment` and `stale_evidence`. Each blocker carries `{claim_version_id, start, end, text, code, detail}`. |
| Interpretations | A claim version with `kind='interpretation'` is exempt from assessment. It must carry `attributed_to_user_id` (GOO-306), otherwise the blocker is `unattributed_interpretation`. Export and UI label it `Interpretation — {name}`. | "Labelled and attributed", exactly as the ticket says. |
| Explicit dimension policy (`RELEASE_POLICY`, v1) | **support**: required as above. **identity**: from the draft's persisted review verdicts (`generation_params.citation_review`), where `mismatch` blocks (`identity_mismatch`), `match` passes, and `unresolved`/`no_identifiers` pass but are reported. **publication**: `observation_status == 'retracted'` blocks (`retracted_source`), and `unknown` or `corrected` is reported as `publication: unavailable` and never blocks. `fully_verified` (`citation_verification_service.py:149-158`) is **not read**. | The publication check does not exist (`:427-433` always returns `status: unknown`), and reporting it truthfully is the ticket's rule. `policy_version` is stored on the row so that a later policy change is visible. |
| Protocol lineage | `protocol_version_id = Collection.current_approved_version_id` (`backend/src/models/research_protocol.py:85`) at promotion time, or `NULL`. It is written to the row and the event. | "Preserve exact protocol/run lineage in the release assessment" without duplicating the protocol ledger. |
| Selective invalidation | A pure `release_rules.dependents(edges, changed) -> set[node]` walks the edge list `source(document_id, source_hash, text_sha256) → extraction_observations → extraction_accepted_values → research_claim_evidence_links → research_claim_assessments (via link_ids) and research_claim_versions → draft_releases (via the snapshotted ids)`. A `source_span` link hangs directly off `source`. `draft_release_service.load_edges(db, collection_id)` builds the edges with one query per table, all filtered by the project (links and assessments by `collection_id`, extraction rows via `matrix.project_id`). | The walk is pure and unit-tested. Unrelated claims are unreachable by construction, so no shortcut can stale every draft. |
| When staleness is persisted | One function, `invalidate_dependents(db, *, collection_id, changed, actor_id, actor_role, cause)`, runs **inside the caller's transaction and never commits**. It runs a guarded `UPDATE draft_releases SET stale_at, stale_event_id WHERE id IN (...) AND stale_at IS NULL` and appends one `release.staled` event only when rows were stamped. Callers, each after its own event append: GOO-304 `create_version` (after `extraction.staled`), GOO-304 `accept_value` (a new tip supersedes a linked one), GOO-305's two `source_changed` paths (accept guard and worker rerun), and GOO-306's `create_version`, `link` (a supersede or withdrawal) and `assess` (a supersede) at its write-order step 8, the seam its plan reserves (GOO-306 plan "Write order" step 8). | The same GOO-297 guarded-update pattern, so a release is staled exactly once. The callers already hold Collection UPDATE and their own stream (`research_extraction` or `research_claims`). `research_release` is always the **last** lock taken, and `promote` never takes the other two streams, so there is no lock cycle. |
| Unwritten changes | Source text can change without any writer calling in (`content_text` has many writers, GOO-305 "Source change → stale"). Such a change is **derived on read**: `release_status` shows `stale` when the walk over current hashes reaches the release, and it is persisted by the next write that touches that source. The gate always re-derives, so a stale input can never promote. | This is GOO-304/305's rule, with no hook on each writer. |
| Assessments stale? | Assessment staleness is derived by the same walk (reported, not written). Only releases are stamped. `release.staled` lists the affected `assessment_ids` and the path for audit. | GOO-306 owns assessment rows, and GOO-307 only reads them. `# ponytail: persist assessment staleness if a reader other than the gate needs it.` |
| Re-promotion | A stale release stays stale. Once its inputs are re-accepted, promoting the same version again inserts a **new** row (the partial unique allows it). The old row keeps its snapshot. | Prior versions and prior releases stay reconstructable. |
| Candidate export | `export_draft` (`:2031-2090`) keeps the stored content byte-for-byte and wraps the output. Markdown gets a header block `> Status: CANDIDATE — not verified. N unresolved item(s).` (or `VERIFIED release {id} · sha256 {hash[:12]}`, or `STALE — invalidated by {cause}`), plus inline `**[UNRESOLVED: {code}]**` after each blocking span, `*[Interpretation — {name}]*` after interpretations, and a trailing `## Unresolved items` list. LaTeX uses the same labels through `_latex_escape` (`:2100`). | The ticket says candidates are exportable with unresolved items visibly labelled, and export must never imply verification. The labels are computed from the same gate result (`check_release`). |
| Revisions | `revise_draft` is unchanged. Its new version has no release row, so it is `candidate`. A blocked or conflicting revision leaves both the current draft and its release status unchanged. | This adds the status dimension without replacing the guard. The test covers it. |

**Migration head:** new revision `d7f9b1c3e5a8_create_draft_releases.py` (12 characters, within the 32 allowed by `scripts/ci/check_alembic.py:40`). `down_revision = "c4e6a8b0d2f5"` (GOO-306's `c4e6a8b0d2f5_create_research_claims.py`). The chain is 304 `a3c5e7f9b1d4` → 305 `b8d0f2a4c6e9` → 306 `c4e6a8b0d2f5` → 307 `d7f9b1c3e5a8`. **Re-pointing rule:** `down_revision` always names the direct stack parent. After any rebase, set it to the single head that `(cd backend && python ../scripts/ci/check_alembic.py)` reports for the rebased parent. Never add a merge revision. **#1753 note:** GOO-297's `c9d1e2f3a4b5` and this stack's root `c9d2e4f6a8b1` (GOO-299) both have `down_revision = "merge_daily_harness_20260928"`. When #1753 lands first, the stack rebase re-points **`c9d2e4f6a8b1` → `c9d1e2f3a4b5`**, and that is the GOO-299 root's fix, not this file's. Two heads with `d7f9b1c3e5a8` as one of them means a parent has not been re-pointed. The migration imports nothing from `src` and copies `_deny_data_api` (`backend/alembic/versions/e1f3a5c7d9b2_create_screening_queues.py:20-30`).

---

### Task 1: Pure rules (`release_rules.py`)

**Files:**
- Create `backend/src/services/research/release_rules.py`.
- Modify `citation_verification_service.py:330-340` to extract `assertion_spans` (a pure module-level function that `_claim_observations` then calls, with no behavior change).
- Create `backend/tests/unit/services/test_release_rules.py`.

```python
POLICY_VERSION = 1
SUPPORTING_STANCES = frozenset({"supporting"})   # GOO-306 StanceEnum values + "unresolved"
BLOCKER_CODES = ("unclaimed_assertion","unassessed","model_only","opposed","unresolved","legacy_only",
                 "superseded_assessment","stale_evidence","unattributed_interpretation",
                 "identity_mismatch","retracted_source")
@dataclass(frozen=True) class Blocker: code; claim_version_id: UUID|None; start: int; end: int; text: str; detail: str
@dataclass(frozen=True) class GateResult: blockers: tuple[Blocker,...]; dimensions: dict; claim_version_ids; assessment_ids; interpretation_ids
def release_status(rows: Sequence[ReleaseRow], derived_stale: bool) -> Literal["candidate","verified","stale"]
def dependents(edges: Iterable[tuple[Node, Node]], changed: Iterable[Node]) -> set[Node]   # BFS, pure
def check_release(content, claims: Seq[ClaimIn], review: dict, stale_nodes: set[Node]) -> GateResult
def label_export(content, gate: GateResult, fmt: Literal["markdown","latex"]) -> str
```

**Tests (write first; they fail on the import):**
- `test_every_assertion_span_must_be_claimed`
- `test_model_only_stance_observation_blocks_assessed_supporting_passes`
- `test_supported_statement_of_uncertainty_passes`: "The effect remains uncertain [Doc 1]." with a `supporting` assessment tip passes.
- `test_legacy_unanchored_links_only_block_as_legacy_only`
- `test_withdrawn_or_superseded_link_in_assessment_blocks`
- `test_unresolved_and_opposed_block_with_offsets`
- `test_interpretation_exempt_but_must_be_attributed`
- `test_identity_mismatch_blocks_publication_unknown_reported_not_blocking`
- `test_fully_verified_flag_is_ignored`: a review with `fully_verified=True` plus an unassessed claim is still blocked.
- `test_dependents_walk_is_selective`: two claims on different accepted values, one source change, and only one release is reached.
- `test_dependents_form_change_reaches_only_changed_field`
- `test_status_candidate_verified_stale_and_repromoted`
- `test_label_export_keeps_content_bytes_and_marks_unresolved`

**Run:** `pytest -q backend/tests/unit/services/test_release_rules.py backend/tests/unit/services/test_citation_verification_service.py` (the second file proves `assertion_spans` changed no behavior).
**Commit:** `feat(research): pure release gate and dependency walk (GOO-307)`

---

### Task 2: Model + migration

**Files:**
- Create `backend/src/models/draft_release.py` (`DraftRelease`, the columns in Decisions) and export it from `backend/src/models/__init__.py`.
- Create `backend/alembic/versions/d7f9b1c3e5a8_create_draft_releases.py`: create the table with `_deny_data_api`, the partial unique `uq_draft_releases_live` and the two CHECKs. `downgrade()` drops the table.
- Modify the `_REBUILT_TABLES` drop order in `backend/tests/integration/test_screening_queue_postgres.py:77-88` to add `draft_releases` first.

**Check:** `(cd backend && python ../scripts/ci/check_alembic.py)` reports single head `d7f9b1c3e5a8`, and `alembic upgrade head --sql | grep -c draft_releases` is > 0 (offline, no Docker).
**Commit:** `feat(drafts): draft_releases table (GOO-307)`

---

### Task 3: Ledger family `research_release`

**Files:**
- Modify `backend/src/services/research_decisions/ledger.py`: add the vocabulary next to the screening keys (`:82-114`), `_validate_release_payload` next to `_validate_screening_payload` (`:345`), `_validate_release_transitions` after `:785`, and the entry in `_FAMILIES` (`:788-810`). Update the docstring family list at `:3-6`.
- Modify `backend/tests/unit/services/test_research_decision_ledger.py`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `release.promoted` | `collection_id, release_id, draft_id, draft_version, content_hash, claim_version_ids, assessment_ids, interpretation_claim_version_ids, protocol_version_id, policy_version, dimensions` | `adjudicator` or `supervisor` |
| `release.staled` | `collection_id, release_ids, cause{family, event_id, kind}, changed_nodes, assessment_ids` | the causing writer's role (`editor`, `adjudicator`, `machine`, `reviewer`) |

**Replay rules:**
1. Every `collection_id` equals `aggregate_id`.
2. A `promoted` event has `actor_role` in {adjudicator, supervisor}, and no earlier live `promoted` exists for the same `draft_id`.
3. A `staled` event names only releases that were promoted earlier in this stream and are not yet staled.

**Tests:**
- `test_release_replay_rejects_reviewer_promotion`
- `test_release_replay_rejects_second_live_release`
- `test_release_replay_rejects_double_stale`
- `test_release_payload_keys_exact`

**Commit:** `feat(research): research_release decision family (GOO-307)`

---

### Task 4: Authorization action

**Files:**
- Modify `project_access.py:28-42` (add `RELEASE` and the frozenset role map) and `:286-289` (`required.isdisjoint(roles)` gives 403).
- Modify `backend/tests/unit/api` or `services` access tests. Add `test_release_requires_adjudicator_or_supervisor_owner_gets_403`, and keep the existing single-role messages byte-identical.

**Commit:** `feat(research): RELEASE action for adjudicator or supervisor (GOO-307)`

---

### Task 5: Service (`draft_release_service.py`) + invalidation hooks

**Files:**
- Create `backend/src/services/research/draft_release_service.py`.
- Modify GOO-306's `DraftRetainedError` pre-check in `delete_draft` (`draft_generation_service.py:1904-1951`) to also match `draft_releases` and `:2031-2090` (`export_draft` calls `label_export`).
- Modify GOO-304 `extraction_forms_service.{create_version, accept_value}`, GOO-305's source-change paths and GOO-306's `claims_service.{create_version, link, assess}` (at their step-8 seam) to add one `invalidate_dependents(...)` call each.

```python
async def load_edges(db, collection_id) -> list[tuple[Node, Node]]
async def gate_inputs(db, context, draft) -> tuple[list[ClaimIn], set[Node]]    # claims for (draft_id, content_hash) + derived stale nodes
async def check(db, context, draft_id, version) -> ReleaseCheckResponse        # VIEW; status + blockers + latest release + stale cause
async def promote(db, context, actor_id, draft_id, version, body) -> tuple[DraftReleaseResponse, bool]   # RELEASE; commits once
async def invalidate_dependents(db, *, collection_id, changed, actor_id, actor_role, cause) -> list[UUID]  # never commits
async def statuses(db, collection_id, draft_ids) -> dict[UUID, str]             # one query for list/get/current responses
```

**`promote` write order:**
1. The route calls `resolve_project(RELEASE)` (locks and post-lock role reload).
2. Load the draft by `(id, project_id, version)`, else 404 `Draft not found`.
3. `lock_aggregate_stream(research_release, collection_id)`.
4. Idempotency replay.
5. Compare the content hash with the body and with any GOO-297 task row, 409 on mismatch.
6. If a live release with the same hash exists, return it with `replayed=True`.
7. `gate_inputs`, then `check_release`. Blockers give `ReleaseBlocked`, which the route turns into 409 `{"detail": {"code": "release_blocked", "blockers": [...]}}`. Stale or superseded inputs are the `stale_evidence`/`superseded_assessment` blockers, so the ticket's "stale approvals → 409" is this path.
8. Insert the `DraftRelease`, `append_decision(release.promoted)`, and `commit()` once.
9. On an `IntegrityError` on `uq_draft_releases_live`, roll back and return the live row.

**Commit:** `feat(drafts): verified promotion and selective invalidation service (GOO-307)`

---

### Task 6: API + contracts

**Files:**
- Modify `backend/src/api/research/drafts.py` (router prefix `/api/v1/projects/{project_id}/drafts`, `:32`).
- Add schemas to `backend/src/shared/research_schemas.py` next to `DraftResponse` (`:700`).
- Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

| Route | Action | Notes |
|---|---|---|
| `GET ""`, `GET /current`, `GET /{draft_id}` (existing, `drafts.py:181-297`) | VIEW | Adds `release_status` and `content_hash` keys. These routes return untyped dicts, so the spec does not change. |
| `GET /{draft_id}/versions/{version}/release` | VIEW | `ReleaseCheckResponse{release_status, content_hash, blockers[], dimensions, release: DraftReleaseResponse\|null, invalidation: {stale_at, cause, changed_nodes, assessment_ids}\|null}`. This is both the blocking list and the invalidation report. |
| `POST /{draft_id}/versions/{version}/promote` | RELEASE | Body `DraftPromoteRequest{content_hash: str(64), idempotency_key: str(1..255), rationale: str(≤2000)\|None}`. Returns 201 new, 200 replayed, 409 blocked/stale/content-changed, 403 without the role, 404 cross-project. |
| `DELETE /{draft_id}` (existing) | EDIT | New 409 for a released version. |
| `POST /{draft_id}/export` (existing) | VIEW | The body is labelled as described in Decisions. |

**oasdiff:** two new operations and new schemas are additive. A 409 on DELETE is a new response code, which is not ERR. **Expected: no ERR-level change.**

**Tests** (`backend/tests/unit/api/test_draft_release_routes.py`, dependency-overridden like the existing draft route tests):
- `test_promote_reviewer_403_owner_without_role_403`
- `test_promote_blocked_returns_precise_list`
- `test_list_drafts_includes_release_status_default_candidate`
- `test_export_candidate_is_labelled`

**Commit:** `feat(drafts): promote and release-check endpoints (GOO-307)`

---

### Task 7: Structural guard

**Files:**
- Create `backend/tests/unit/architecture/test_release_boundary.py`. It is an AST scan that fails if `src/tasks/`, `src/services/agent/` or `draft_generation_service.py` names `promote`, `DraftRelease(` or `release.promoted`. It also asserts that the `promote` body references `ResearchAction.RELEASE` via its caller signature (`context: ProjectContext`).

The rule is that automation cannot verify. The agent's `revise_draft` or `create_draft` must never reach promotion.

**Commit:** `test(drafts): guard that workers and agents cannot promote (GOO-307)`

---

### Task 8: PostgreSQL proof (one test)

**Files:**
- Create `backend/tests/integration/test_draft_release_postgres.py` with `pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]`. It reuses the schema-per-test fixture (`test_draft_review_persistence.py:44-68`) and `_wait_until_blocked` plus the revocation shape from `test_research_authorization_concurrency.py:124-175`.

**`test_release_gate_invalidation_graph_and_races`** runs these steps in order:
1. **Seed** (org A project, org B user F):
   - Two documents D1 and D2 with checksums.
   - A GOO-304 matrix with fields `Sample size` (on D1) and `Design` (on D2), each with an accepted value.
   - Draft v1 (current) content, with a GOO-306 `research_claim_versions` row per sentence:
     - C1 is factual, has an `extraction` link to the D1 accepted value, and has a `supporting` assessment tip.
     - C2 is factual, has an `extraction` link to the D2 accepted value, and has a `supporting` tip ("the effect remains uncertain").
     - C3 is an interpretation attributed to J.
     - C4 is the **seeded unsupported number** from `dev-unsupported-number`: it has a `source_span` link and a `research_claim_stance_observations` snapshot, but no assessment.
     - C5 has only a `legacy_unanchored` link. Before step 3 it is re-linked with a `source_span` link and assessed, so that step 3 has exactly one blocker. Its `legacy_only` blocker is asserted in step 2's export.
   - Users: owner O (no roles), reviewer R, adjudicator J, supervisor S.
   - A `draft_task_results` row that is completed with v1's hash.
2. **Candidate:** `release_status == 'candidate'`, and the export contains `CANDIDATE` and `[UNRESOLVED: model_only]` next to C4's text. The stored `content` is byte-identical.
3. **Blocked:** J promotes and gets 409 with exactly one blocker `{code: model_only, claim_version_id: C4}`. No `draft_releases` row and no event.
4. **Role checks:** O and R each get 403 on promote. F gets 404.
5. **Fix C4:** J records a `supporting` assessment for C4 through GOO-306 `assess`. S promotes with a wrong `content_hash` and gets 409.
6. **Concurrent promotion:** J and S promote concurrently (`asyncio.gather`, separate sessions, different idempotency keys). Assert:
   - both return the **same** release id;
   - `count(*) FROM draft_releases WHERE stale_at IS NULL AND draft_id=v1` is 1;
   - exactly one `release.promoted` event;
   - `SELECT 1` works afterwards.
   - A retry with J's key replays the original.
7. **Commit/reopen:** in a new session, the release row's `claim_version_ids` is {C1, C2, C4}, `interpretation_claim_version_ids` is {C3}, and `content_hash` equals `sha256(v1.content)`. `release_status` is `verified`, and the export contains `VERIFIED`.
8. **Stale assessment:** J supersedes C2's assessment through GOO-306's writer. This stales v1's release (`release.staled` names it, `stale_at` is set) and the row is otherwise column-for-column unchanged. Promoting again against the superseded snapshot returns 409 `superseded_assessment` until C2 is re-accepted. After re-acceptance, promoting again inserts a **second** row and the first stays stale.
9. **Selective invalidation:**
   - Seed draft v2, a revision whose claims cite only D2, and promote it.
   - Change D1's source hash and call the GOO-305 source-changed path.
   - Assert that v1's live release goes stale and **v2's stays verified**.
   - The graph assertions: `dependents(edges, {source D1})` contains C1's link, C1's assessment and v1's release, and contains no node of C2 or of v2.
10. **Form change:** an editor amends `Design.unit` (GOO-304 `create_version`, which emits `extraction.staled`). Only releases citing `Design` are stamped, and `release.staled.cause.family == 'research_extraction'`.
11. **Role-revocation race**, parametrized `revoke ∈ {membership, adjudicator}` with a fresh draft that passes the gate:
    - (a) J holds the RELEASE locks and the revoker blocks (`_wait_until_blocked`). J commits and the revocation finishes after, giving one release.
    - (b) The revocation commits while J waits on the lock. J's post-lock reload gives 403, with no row and no event.
12. **Failed revision:** a blocked `revise_draft` (the existing scenario in `test_draft_review_persistence.py:72-160`) leaves the current draft and its `release_status` unchanged.
13. **Deletion:** `delete_draft(v1)` returns 409, and every row is intact.
14. **Replay:** `replay_decisions(research_release, collection_id)` succeeds and its sequence is contiguous.
15. **Downgrade:** `d7f9b1c3e5a8.downgrade()` drops only `draft_releases`.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_draft_release_postgres.py`. Without a database, report it as **NOT RUN**.
**Commit:** `test(drafts): PostgreSQL proof for release gate, invalidation graph and races (GOO-307)`

---

### Task 9: Held-out seeded-failure evaluation hook

**Files:**
- Create `evals/academic-writing-baseline-v1/tests/test_release_gate_seeded.py`. It is a pure test with no DB.
- Modify `evals/academic-writing-baseline-v1/README.md` (one paragraph).

The test loads `corpora/development.json` (`dev-unsupported-number`, `dev-invalid-revision`) and `corpora/held-out.json` (`held-contradictory-sources`, `held-hijacked-id`). For each task it builds a draft string with one supported sentence and one seeded failure:
- an invented number for `dev-unsupported-number`;
- an opposed claim for `held-contradictory-sources`;
- a claim whose assessment cites a link id missing from the project's edge set (GOO-306 refuses such links with 404 at write time, so this is defense in depth), which the gate reports as `stale_evidence`, for `held-hijacked-id`;
- an interpretation with no attribution for `dev-invalid-revision`, which gives `unattributed_interpretation`.

It feeds the draft to `release_rules.check_release` and asserts that the failure blocks with the expected code while the supported sentence passes.

The held-out cases use **only the task conditions** (the failure shapes) and never the gold judgments. The corpus files are not edited, so the README's frozen-corpus rule holds. This is a hook: the live 14-trial run stays GOO-293's job.

**Run:** `pytest -q evals/academic-writing-baseline-v1/tests/test_release_gate_seeded.py`
**Commit:** `test(evals): seeded unsupported/unresolved claims block promotion (GOO-307)`

---

### Task 10: Minimal frontend

**Files:**
- Create `frontend/src/types/api/research-release-contract.ts`, which aliases `ReleaseCheckResponse`, `DraftReleaseResponse` and `DraftPromoteRequest` (the pattern is `frontend/src/types/api/research-screening-contract.ts`).
- Modify `frontend/src/services/projectService.ts` (next to the draft calls at `:455-575`) to add `getDraftRelease` and `promoteDraft`. The `Draft` type gains optional `release_status` and `content_hash`, which stay hand-written because the wire shape is untyped `{}`.
- Modify `frontend/src/components/research/DraftViewer.tsx`:
  - Add a badge next to `Version {n}` (`:159`): `Candidate` / `Verified` / `Stale`, using theme tokens with `aria-label`.
  - Add a "Promote to verified" button, shown only when the viewer's project roles include adjudicator or supervisor. It calls `useQuery(['draft-release', projectId, draftId, version])`, which lists the blockers (code label, sentence text) and disables the button while any remain. `useMutation` posts `{content_hash, idempotency_key: crypto.randomUUID()}`, and on 409 it renders the returned blockers.
  - Add a stale banner that shows the invalidation cause.
- Tests go in `frontend/src/components/research/__tests__/DraftViewer.release.test.tsx`:
  - `renders candidate badge by default`
  - `lists blockers and disables promote`
  - `hides promote without role`
  - `shows stale cause`

The unresolved labels in export come from the backend and need no frontend change. There is no claim create/link/assess UI in this ticket (see the GOO-306 seam).

**Commit:** `feat(frontend): draft release badge, blocker list and promote action (GOO-307)`

---

## Mutation verification

Follow `docs/engineering/testing.md`. Record each result in the test docstring and in `docs/testing/agent-orchestration-mutation-checks.md` under a GOO-307 section.

| Guard (file:line set during implementation) | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| The live-release short-circuit in `promote` (step 6) | `if False:` | `pytest -q backend/tests/integration/test_draft_release_postgres.py` | step 6 hits `IntegrityError` on `uq_draft_releases_live` (the backstop still returns one id, so the test also asserts `replayed` on exactly one caller) |
| The `uq_draft_releases_live` backstop handler | re-raise | same | step 6 raises an unhandled `IntegrityError` when both callers pass step 6 |
| The post-lock role reload (`project_access.py:262-289`), exercised through RELEASE | read roles before the locks | same `-k race` | step 11b promotes after revocation |
| The content-hash binding (step 5) | skip | same | step 5 wrong-hash promote returns 201 |
| The `stale_at IS NULL` guard in `invalidate_dependents` | drop the predicate | same | step 8/9 produces a second `release.staled` for the same release, and replay raises |
| The `dependents` selectivity (the edge filter by changed node) | return all releases | `pytest -q backend/tests/unit/services/test_release_rules.py -k selective` plus integration step 9 | v2 goes stale |
| The `model_only` rule | treat a stance as accepted | `pytest -q evals/academic-writing-baseline-v1/tests/test_release_gate_seeded.py` | the seeded unsupported number promotes |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_release_rules.py backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/services/test_citation_verification_service.py
pytest -q backend/tests/unit/api backend/tests/unit/architecture
pytest -q evals/academic-writing-baseline-v1/tests
(cd backend && python ../scripts/ci/check_alembic.py)      # single head d7f9b1c3e5a8
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

Report the PostgreSQL tests (this one and `test_draft_review_persistence.py`) as **NOT RUN** without `postgres_container`/`RESEARCH_DECISION_DATABASE_URL`.

## Authenticated journey list for Linear closure

Run this on dev (goodwiinz.tech → the dev API) after the deploy. `kubectl -n rag-dev exec deploy/backend -- alembic current` should show `d7f9b1c3e5a8 (head)`.

1. **Candidate:** generate a draft. The badge reads `Candidate`. Download the Markdown and the LaTeX ZIP: both carry the CANDIDATE header and `[UNRESOLVED: …]` labels. Keep the files.
2. **Blocked:** as the ADJUDICATOR, open the promote panel. It lists every unassessed or model-only sentence, and Promote is disabled. A curl POST returns 409 `release_blocked` with the same list.
3. **Roles:** the owner (no roles) and a REVIEWER get 403 from promote, and the button is hidden. A user from another org gets 404.
4. **Promote:** accept the remaining assessments (GOO-306 API via curl; see Task 10), then promote. The badge reads `Verified` and the export header shows the release id and hash. A second click returns the same release id.
5. **Stale:** reprocess one cited source, or amend a cited field. The verified draft whose claims cite it shows `Stale` with the cause. A second verified draft that cites other sources stays `Verified`. `GET …/release` shows the invalidation path.
6. **Revision:** a blocked revision leaves the current draft and its badge unchanged. A successful revision creates a new `Candidate` version, and the earlier version stays `Verified`.
7. **Task binding:** `GET /drafts/status/{task_id}` (GOO-297) `artifact_hash` equals the promoted `content_hash`.

## GOO-306 seam (aligned with `2026-09-30-goo-306-versioned-claims.md`)

This plan was first drafted before the GOO-306 plan existed. It has since been re-aligned to that plan's names, which are authoritative (GOO-306 "GOO-307 seam" section). What this plan reads:

| GOO-307 reads | GOO-306 name | Property relied on |
|---|---|---|
| Claim version bound to the exact draft | `research_claim_versions(draft_id, draft_version, draft_content_hash, start_char, end_char, text, kind, attributed_to_user_id, supersedes_claim_version_id)`, tip = not superseded | `text == content[start:end]` in code points. `kind` and attribution are enforced by a CHECK. |
| Evidence link | `research_claim_evidence_links(kind ∈ {extraction, source_span, legacy_unanchored}, status ∈ {linked, withdrawn}, accepted_value_id, document_id, source_hash, text_sha256, supersedes_link_id)` | A live link is a `linked` tip. `legacy_unanchored` never supports a release (`legacy_only`). |
| Model evidence | `research_claim_stance_observations` (immutable snapshots) | Only used to tell `model_only` apart from `unassessed`. It never authorizes anything. |
| Human assessment | `research_claim_assessments(stance, link_ids, stance_observation_ids, assessed_by_id, actor_role='adjudicator', supersedes_assessment_id)`, one tip per claim version | Stances are `supporting \| opposing \| neutral \| not_addressed \| unresolved`. |
| Ledger | family `research_claims` (per-Collection stream), events `claim.versioned`, `claim.linked`, `claim.observed`, `claim.assessed` | Supersession is a `supersedes_*` key; there is no `claim.superseded`. |
| Hook point | `claims_service` write order step 8 (after the append, before the commit) | `invalidate_dependents` goes there. |
| Draft retention | `DraftRetainedError` pre-check in `delete_draft` | This plan extends the same check to releases. |
| Migration | `c4e6a8b0d2f5`, `down_revision = "b8d0f2a4c6e9"` | It is this plan's `down_revision`. |

**Disagreement with GOO-306:** GOO-306 :320 assigns the claim create/link/assess **UI** to GOO-307. This ticket's frontend scope is a badge, a promote button, the blocker list and export labels. This plan keeps claim authoring and assessment **API-only** (the journey uses curl) and leaves the UI for a follow-up ticket. File that ticket if reviewers need the UI before the pilot.

## Out of scope

- A generic approval framework.
- A promoter≠assessor rule.
- Persisted assessment staleness.
- Hooking every `content_text` writer (staleness is derived on read and persisted on the next write).
- A live publication/retraction check (reported as `unavailable`).
- Establishing the 14-trial GOO-293 baseline.

Each has a `ponytail:` marker at its seam.

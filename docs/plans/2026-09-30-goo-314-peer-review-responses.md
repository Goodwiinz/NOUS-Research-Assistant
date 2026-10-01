# GOO-314 Peer-Review Responses Against Manuscript Revisions Plan (Academic R7)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A peer-review round records versioned comments from **external reviewers**. An external reviewer is a labelled identity that is never a project user or role. Each comment is anchored to a passage of one saved draft version, using GOO-306 offsets plus a quote hash. Each comment has an assignee, versioned author responses and resolution decisions. Every response either links a retained revision (a saved later draft version plus an anchored diff) or carries an attributed no-change rationale, and it may cite GOO-306 claim versions as evidence. On a later revision a comment re-anchors only when its quote is found exactly once; otherwise it shows an explicit `unresolved_anchor` state that survives into the response export. Comments, assignments, responses and resolutions are insert-only with actors, timestamps and retained predecessors. Concurrent edits cannot overwrite a response. Storage is independent of `DraftReview` (machine citation review).

**Architecture:**
- **Five insert-only tables:** `peer_review_rounds`, `peer_review_reviewers`, `peer_review_comments` (versioned), `peer_review_responses` (versioned) and `peer_review_decisions`. The last holds both assignment and resolution, which are distinguished by `kind` and have different roles.
- **One pure module**, `peer_review_rules.py`: anchor state, the sentence-level anchored diff (stdlib `difflib`) and response-shape checks.
- **One service**, `peer_review_service.py`, **one router**, `api/research/peer_review.py`, and **one ledger family**, `research_peer_review`.
- **One new read endpoint**, `GET …/drafts/diff`, gives the anchored diff between two saved versions. The existing `compare` (counts and word-set similarity) is unchanged.
- **Status is derived** (open, responded, resolved), and so is anchor state. Neither is stored.

**Tech stack:** FastAPI, SQLAlchemy async, Alembic, Python `difflib`, the PostgreSQL integration fixture, openapi-typescript, Next.js with TanStack Query and Vitest.

**Stacking (read first):** This PR stacks on **GOO-313** (`c0f2a4b6d8e9`) → GOO-312 → GOO-311 `a6c8e0b2d4f5` → … It has no logic dependency on R6. It is chained so that the migration history stays linear, and it opens after GOO-313 merges.

### Consumed names

| Consumed | Name (path:line) | Property relied on |
|---|---|---|
| Saved versions | `GeneratedDraft(project_id, version, title, content, is_current)` (`backend/src/models/generated_draft.py:26-58`); the GOO-307 content-hash recipe `sha256(draft.content.encode())` | A saved version's content never changes. A revision is a new row. |
| Offsets (GOO-306) | `ResearchClaimVersion(draft_id, draft_content_hash, start_char, end_char)` (`backend/src/models/research_claim.py:106-160`), `SPAN_CHECK` (`:34`) | Anchors use the same `(draft_id, draft_content_hash, start_char, end_char)` shape. |
| Evidence (GOO-306) | `research_claim_versions` and `research_claims` in the same Collection; `claims_service._version` (`backend/src/services/research/claims_service.py:244`), `_claim` (`:230`) | Evidence links are claim-version ids, checked by Collection. |
| Sentence splitter (GOO-307) | `release_rules.assertion_spans(content)` (`backend/src/services/research/release_rules.py:70`) | The diff runs over the same spans that the release gate checks. |
| Draft retention | `delete_draft`'s pinned-version pre-check (`backend/src/services/research/draft_generation_service.py:2210-2225`), `DraftRetainedError` (`:126`), mapped to 409 in `backend/src/api/research/drafts.py:362` | Drafts referenced by comments or responses join that one union. |
| Existing compare | `compare_drafts` (`draft_generation_service.py:2287`, route `drafts.py:475-500`) | Untouched. |
| Machine review | `DraftReview` (`backend/src/models/draft_review.py:9-22`) | Not read or written. |
| Roles | `ResearchAction.VIEW/EDIT/ADJUDICATE`, `resolve_project` (`backend/src/services/research_engine/project_access.py:28-48,186`) | ADJUDICATE needs an explicit adjudicator assignment. Ownership and edit rights never resolve a comment. |
| Ledger | `lock_aggregate_stream` (`backend/src/services/research_decisions/ledger.py:1318`), `append_decision` (`:1321`), `replay_decisions` (`:1420`), `_FAMILIES` (`:2235`), `identity_service._replayed_event` (`backend/src/services/research_engine/identity_service.py:76`) | The append is caller-owned. |
| Router prefix | `/api/v1/projects/{project_id}/drafts` (`drafts.py:41`), `/api/v1/projects/{project_id}/claims` (`backend/src/api/research/claims.py:33`) | The new router uses `/api/v1/projects/{project_id}/peer-review`. |
| Bundle | `audit_bundle._sealed_part`, `gather_parts` (`backend/src/services/research_engine/audit_bundle.py:86,315`) | One reader per part. |
| Insert-only trigger | `prevent_research_insert_only_mutation()` | SQLSTATE `55000`. |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| External reviewer identity | `peer_review_reviewers(id, round_id, label VARCHAR(64), display_name VARCHAR(255) NULL, created_by_id, created_at)` with `UNIQUE(round_id, label)`. There is **no** `user_id` column, and no code path maps a reviewer to a user or a role. | "External reviewer identity distinct from project roles." Journals usually anonymize reviewers, so `label` ("Reviewer 2") is the identity and `display_name` is optional. |
| Round | `peer_review_rounds(id, collection_id, draft_id FK generated_drafts RESTRICT, draft_content_hash, label, received_at DATE NULL, created_by_id, created_at)`. The reviewed version is bound by content hash. | A round reviews one exact saved version. |
| Comment versions | `peer_review_comments(id, round_id, reviewer_id, number SMALLINT, body TEXT, draft_id, draft_content_hash, start_char NULL, end_char NULL, quote TEXT NULL, quote_sha256 NULL, supersedes_comment_id UNIQUE NULL, author_id, created_at)`. Partial unique initial `(round_id, number) WHERE supersedes IS NULL`. `CHECK ((start_char IS NULL) = (quote IS NULL))`, and the span must be within the content with `quote == content[start:end]` (service-checked, then the hash is stored). A **general comment** has no anchor and is not an error. | Comments are versioned with retained predecessors. Anchors use the GOO-306 shape. |
| Anchor state (derived) | `peer_review_rules.anchor_state(comment, target_content, target_hash) -> (state, start, end)`:<br>`exact` when `target_hash == comment.draft_content_hash`;<br>`carried` when `quote` occurs **exactly once** in the target, at the new offsets;<br>`unresolved_anchor` when it occurs zero times or more than once;<br>`general` when there is no anchor.<br>The state is never stored. Export and UI show `unresolved_anchor` with the original quote and version. | "Unresolved/misanchored comments survive later revisions and export (explicit unresolved-anchor state)." Exact-once matching never guesses. `# ponytail: no fuzzy re-anchoring; add a reviewed manual re-anchor (a new comment version) if pilots hit it.` |
| Assignment and resolution | `peer_review_decisions(id, comment_root_id, kind CHECK IN ('assigned','resolved','reopened'), assignee_id NULL, response_id NULL, rationale TEXT NULL, actor_id, actor_role CHECK IN ('editor','adjudicator'), supersedes_decision_id UNIQUE NULL, created_at)`. `assigned` needs EDIT, actor `editor`, and an assignee who is a Collection member. `resolved` and `reopened` need **ADJUDICATE** (`adjudicator`). `resolved` must name the response tip being accepted. Tips are per `(comment_root_id, kind family)`: one chain for assignment, one for resolution. | "Unauthorized resolution fails." One table keeps the decisions in a single ordered stream. The role split stops the author who answers a comment from closing it alone. `# ponytail: no responder≠resolver rule; add one if editors ask.` |
| Response versions | `peer_review_responses(id, comment_root_id, kind CHECK IN ('change','no_change'), body TEXT, revised_draft_id FK RESTRICT NULL, revised_content_hash NULL, base_draft_id FK RESTRICT NULL, diff_sha256 NULL, rationale TEXT NULL, evidence_claim_version_ids JSONB, supersedes_response_id UNIQUE NULL, author_id, created_at)`. `CHECK ((kind = 'change') = (revised_draft_id IS NOT NULL AND diff_sha256 IS NOT NULL AND rationale IS NULL))` and `CHECK (kind = 'change' OR (rationale IS NOT NULL AND length(rationale) > 0))`. Partial unique initial `(comment_root_id) WHERE supersedes IS NULL`. | "A response must link a retained revision/diff or an attributed no-change rationale." The DB enforces the either-or. |
| What a `change` response proves | `revised_draft_id` is a saved version of the same project with `version` greater than the round's reviewed version. `base_draft_id` defaults to the round's draft. The service computes `anchored_diff(base, revised)`. It must be non-empty, and if the comment is anchored, at least one hunk must overlap the comment's anchor span in the base (otherwise 422 `Revision does not touch the commented passage`). `diff_sha256 = canonical_json_sha256(hunks)` is stored, and the diff is recomputed and compared on read (`diff_verified`). | It is a real change to the passage in question, reproducible from retained versions. |
| Anchored diff | `peer_review_rules.anchored_diff(old, new) -> [{op: "equal"\|"replace"\|"insert"\|"delete", old: [start, end], new: [start, end], old_text, new_text}]`, using `difflib.SequenceMatcher(None, old_spans, new_spans, autojunk=False)` over `assertion_spans` texts and mapping back to character offsets. Only non-`equal` hunks are returned. A new `GET …/drafts/diff?from_draft_id&to_draft_id` (VIEW) returns it with both content hashes. | "Anchored diff between saved draft versions (current compare returns counts/word-set similarity only)." Stdlib, deterministic and sentence-aligned with the release gate. |
| Evidence links | `evidence_claim_version_ids` must be GOO-306 claim versions in this Collection (a foreign one gives 404 `Claim version not found`). They are stored as JSONB ids and rendered with the claim text and assessment status. | "Evidence links (GOO-306 claims/links)"; "cross-project links fail". |
| Concurrency | Every write runs `resolve_project` (Collection lock), then `lock_aggregate_stream(research_peer_review, collection_id)`, then an expected-tip check: the body carries `supersedes_*_id`, and a different current tip gives 409 `Response changed; reload` with the current tip in the detail. `UNIQUE(supersedes_*)` and the partial unique initial are the backstop, mapping `IntegrityError` to the same 409. | "Concurrent edits cannot lose a response": the loser is told and nothing is overwritten. |
| Derived status | `open` (no response tip), `responded` (a response tip with no `resolved` decision tip, or a resolution naming an older response), `resolved` (the resolution tip names the current response tip). A new response after a resolution makes the comment `responded` again. | Derived, never stamped, so it is always consistent with the retained rows. |
| Response export | `GET …/rounds/{id}/export?format=markdown\|json`. Comments are listed by reviewer, then number. Each shows the comment, its anchor state and quote, the response (kind, text, revised version, diff hunks or rationale, evidence claims), and the resolution with actors and timestamps. A trailing `## Unresolved items` section lists every comment not `resolved` and every `unresolved_anchor`. JSON is `nous.peer-review-response.v1` with `body_sha256`. | "Unresolved items visible after revision and in the response export." |
| Draft retention | `delete_draft`'s union adds `peer_review_rounds.draft_id`, `peer_review_comments.draft_id`, `peer_review_responses.revised_draft_id` and `base_draft_id`. | Reviewed and revised versions must stay resolvable, and the RESTRICT FKs are the backstop. |
| Independence from `DraftReview` | Nothing reads or writes `draft_reviews`. An AST guard (Task 6) checks this. | "Independent storage from DraftReview." |
| GOO-315 seam | `peer_review_service.open_obligations(db, collection_id, draft_id) -> list[{comment_root_id, state, anchor_state}]` returns every comment in rounds on this project whose reviewed version is ≤ the draft's version and whose status is not `resolved`. | Peer-review obligations become one input to verified promotion in GOO-315, with no gate logic here. |

**Migration head:** new revision `d2a4c6e8f0b1_create_peer_review.py`, with `down_revision = "c0f2a4b6d8e9"` (GOO-313). Same re-pointing rule. It creates the five tables with `_deny_data_api` and insert-only triggers. `downgrade()` refuses when rows exist.

---

### Task 1: Pure rules (`peer_review_rules.py`)

**Files:**
- Create `backend/src/services/research/peer_review_rules.py` (stdlib, plus `release_rules.assertion_spans`).
- Create `backend/tests/unit/services/test_peer_review_rules.py`.

```python
AnchorState = Literal["exact", "carried", "unresolved_anchor", "general"]
def check_anchor(content: str, start: int, end: int, quote: str) -> str          # quote_sha256; ValueError
def anchor_state(quote, quote_sha256, origin_hash, start, end, target: str, target_hash: str) -> tuple[AnchorState, int | None, int | None]
def anchored_diff(old: str, new: str) -> list[dict]
def touches(hunks: Sequence[dict], start: int, end: int) -> bool
def comment_status(response_tip_id, resolution_tip) -> Literal["open", "responded", "resolved"]
```

**Tests (they fail on the import):**
- `test_anchor_exact_on_same_version`
- `test_anchor_carried_when_quote_found_once_with_new_offsets`
- `test_anchor_unresolved_when_quote_missing_or_ambiguous`
- `test_diff_reports_offsets_on_both_sides_for_replace_insert_delete`
- `test_diff_empty_for_identical_content`
- `test_touches_requires_overlap_with_anchor`
- `test_status_resolution_of_older_response_is_responded`
- `test_unicode_offsets_are_python_str_indices`: the same convention as GOO-306.

**Commit:** `feat(research): pure peer-review anchors and anchored draft diff (GOO-314)`

---

### Task 2: Models + migration

**Files:**
- Create `backend/src/models/peer_review.py` (`PeerReviewRound`, `PeerReviewReviewer`, `PeerReviewComment`, `PeerReviewResponse`, `PeerReviewDecision`) and export them.
- Create `backend/alembic/versions/d2a4c6e8f0b1_create_peer_review.py`.
- Modify `test_screening_queue_postgres.py` (`_REBUILT_TABLES`).

**Check:** `check_alembic.py` reports single head `d2a4c6e8f0b1`, and `alembic upgrade head --sql | grep -c peer_review_responses` is > 0.
**Commit:** `feat(research): peer-review round, reviewer, comment, response and decision tables (GOO-314)`

---

### Task 3: Ledger

**Files:**
- Modify `ledger.py` to add the `research_peer_review` family (subject type `peer_review_comment`).
- Modify `test_research_decision_ledger.py`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `review_round.recorded` | `collection_id, round_id, draft_id, draft_content_hash, reviewer_ids` | `editor` |
| `review_comment.versioned` | `collection_id, round_id, comment_id, comment_root_id, supersedes_comment_id, reviewer_id, draft_content_hash, anchored, quote_sha256` | `editor` |
| `review_response.versioned` | `collection_id, comment_root_id, response_id, supersedes_response_id, kind, revised_draft_id, diff_sha256, evidence_claim_version_ids` | `editor` |
| `review_decision.recorded` | `collection_id, comment_root_id, decision_id, kind, assignee_id, response_id, supersedes_decision_id` | `editor` (assigned) or `adjudicator` (resolved, reopened) |

**Replay rules:**
1. Every `collection_id` equals the `aggregate_id`.
2. Each comment root and each response chain has one tip.
3. `resolved` names a response recorded earlier for the same root, and its actor is `adjudicator`.
4. A `change` response has a `diff_sha256`.

**Tests:**
- `test_peer_review_replay_rejects_editor_resolution`
- `test_peer_review_replay_rejects_resolution_of_unknown_response`
- `test_peer_review_replay_rejects_forked_response_chain`

**Commit:** `feat(research): research_peer_review decision family (GOO-314)`

---

### Task 4: Service

**Files:**
- Create `backend/src/services/research/peer_review_service.py`.
- Modify `draft_generation_service.delete_draft`'s union (`:2214-2223`).
- Add `diff_drafts(project_id, from_id, to_id)` next to `compare_drafts`. It calls `peer_review_rules.anchored_diff`, and `compare_drafts` is unchanged.

```python
AGGREGATE_TYPE = "research_peer_review"
async def create_round(db, context, actor_id, data) -> RoundResponse                         # EDIT
async def add_comment(db, context, actor_id, round_id, data) -> CommentResponse              # EDIT; new or successor version
async def respond(db, context, actor_id, comment_root_id, data) -> ResponseResponse          # EDIT
async def decide(db, context, actor_id, comment_root_id, data) -> DecisionResponse           # EDIT (assigned) / ADJUDICATE (resolved, reopened)
async def get_round(db, context, round_id, target_draft_id: UUID | None) -> RoundDetail      # VIEW; anchor states against target
async def export_round(db, context, round_id, fmt) -> tuple[bytes, str]                      # VIEW
async def open_obligations(db, collection_id, draft_id) -> list[dict]
```

**Write order (each mutation):**
1. `resolve_project(action)`.
2. `lock_aggregate_stream`.
3. `_replayed_event` (idempotency key).
4. Load and validate the targets, Collection-scoped. A foreign draft or claim version gives 404.
5. Run the tip check.
6. Insert and append.
7. **One commit.**

**Commit:** `feat(research): peer-review rounds, anchored comments, responses and decisions (GOO-314)`

---

### Task 5: API + contracts + bundle part

**Files:**
- Create `backend/src/api/research/peer_review.py` (prefix `/api/v1/projects/{project_id}/peer-review`) and register it in `main.py`.
- Add the diff route to `drafts.py`.
- Create `backend/src/shared/peer_review_schemas.py`.
- Modify `audit_bundle.py` to add a `peer_review.json` part before `_prisma`.
- Regenerate the OpenAPI spec and the types.

| Route | Action | Notes |
|---|---|---|
| `POST …/peer-review/rounds` | EDIT | `RoundCreate{draft_id, draft_content_hash, label, received_at?, reviewers: [{label, display_name?}], idempotency_key}` |
| `GET …/peer-review/rounds` and `GET …/rounds/{id}?target_draft_id=` | VIEW | Comments with derived status and anchor state against the target (default: the current draft). |
| `POST …/rounds/{id}/comments` | EDIT | `CommentCreate{reviewer_id, number, body, anchor?: {start, end, quote}, supersedes_comment_id?, idempotency_key}` |
| `POST …/comments/{root_id}/responses` | EDIT | `ResponseCreate{kind, body, revised_draft_id?, base_draft_id?, rationale?, evidence_claim_version_ids, supersedes_response_id?, idempotency_key}`. Returns 409 on a stale tip and 422 for an untouched passage or a bad shape. |
| `POST …/comments/{root_id}/decisions` | EDIT or ADJUDICATE by kind | `DecisionCreate{kind, assignee_id?, response_id?, rationale?, supersedes_decision_id?, idempotency_key}` |
| `GET …/rounds/{id}/export?format=markdown\|json` | VIEW | An attachment. |
| `GET /api/v1/projects/{project_id}/drafts/diff?from_draft_id&to_draft_id` | VIEW (the drafts router's existing project check) | `{from: {id, version, content_hash}, to: {…}, hunks}` |

**oasdiff:** new operations only. Expected: no ERR.

**Tests** (`backend/tests/unit/api/test_peer_review_routes.py`):
- `test_resolution_by_owner_without_adjudicator_403`
- `test_cross_project_revised_draft_404`
- `test_export_lists_unresolved_section`
- `test_compare_route_unchanged`

**Commit:** `feat(research): peer-review endpoints, response export, draft diff and bundle part (GOO-314)`

---

### Task 6: PostgreSQL proof (one test) + structural guard

**Files:**
- Create `backend/tests/integration/test_peer_review_postgres.py` with the integration and `requires_postgres` markers. It reuses `screening_factory` and `_upgrade` (through `d2a4c6e8f0b1`), seeds drafts v1 and v2 (v2 rewrites one sentence), and seeds GOO-306 claim versions.
- Create `backend/tests/unit/architecture/test_peer_review_boundary.py`. It fails if `peer_review_service` imports `DraftReview`, or if any `peer_review_*` model has a `user_id` column on reviewers.

**`test_peer_review_round_change_no_change_unresolved_and_concurrency`** runs these steps in order:
1. **Round** on v1 with Reviewer 1 and Reviewer 2. There are three comments:
   - C1 is anchored on the sentence that v2 rewrites.
   - C2 is anchored on an unchanged sentence.
   - C3 is anchored on a sentence that v2 deletes.
2. **Assign** C1 to an editor (EDIT). The owner with no roles can assign, because assignment is EDIT.
3. **Change response** to C1 with `revised_draft_id = v2`. The diff overlaps the anchor, `diff_sha256` is stored, and the evidence is one claim version. A revision that does not touch C1's passage gives 422.
4. **No-change response** to C2 with a rationale, by an attributed author.
5. **Resolve** C1 as the adjudicator. The owner with no roles gets 403. C2 is left `responded`, and C3 has no response.
6. **Reload in a fresh session** with target v2:
   - C1 is `resolved`/`unresolved_anchor`: v2 rewrote its sentence, so the original quote and v1 offsets are shown, and the response links to the new text.
   - C2 is `responded`/`carried` with new offsets.
   - C3 is `open`/`unresolved_anchor`.
7. **Export:** the Markdown and JSON include all three comments and their anchor states. `## Unresolved items` lists C2 and C3. The JSON `body_sha256` re-hashes.
8. **Concurrency:** two `respond` calls superseding the same C2 tip, run concurrently (two sessions, `asyncio.gather`). Exactly one succeeds and the other gets 409 with the winner's id. Both bodies stay retrievable (the winner as the tip, the loser returned to its caller, nothing overwritten). The chain has exactly one tip.
9. **Cross-project:** a claim version from another project as evidence gives 404, and a revised draft from another project gives 404.
10. **Retention:** deleting v1 or v2 gives 409 (`DraftRetainedError`).
11. **Insert-only and replay:** UPDATE or DELETE on each table raises `55000`, and `research_peer_review` replays.
12. **Independence:** the `draft_reviews` row count is unchanged throughout.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_peer_review_postgres.py`. Without a database, report it as **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for peer-review responses, anchors and concurrency (GOO-314)`

---

### Task 7: Frontend

**Files:**
- Create `frontend/src/types/api/peer-review-contract.ts`.
- Modify `frontend/src/services/projectService.ts` to add the peer-review calls and `diffDrafts`.
- Create `frontend/src/components/research/PeerReviewPanel.tsx` and mount it next to `DraftReleasePanel` in the draft view.
  - The panel lists rounds, then reviewers, then comments, with status and anchor-state badges. `unresolved_anchor` gets a visible warning and the original quote.
  - The response form chooses Change (pick a saved later version, which shows the anchored diff inline) or No change (a rationale is required), plus an evidence claim picker.
  - Resolve is shown only to adjudicators.
  - "Old text / New text" links open `DraftViewer` at the base and revised offsets.
- Modify `DraftComparison.tsx` to add an "Anchored diff" tab using `diffDrafts`. The existing counts view stays.
- Tests:
  - `unresolved anchor badge and quote shown`
  - `no-change requires rationale`
  - `resolve hidden without adjudicator role`
  - `response links open old and new text`

**Commit:** `feat(frontend): peer-review panel, anchored diff and response navigation (GOO-314)`

---

## Mutation verification

Follow `docs/engineering/testing.md`. Record each check under a GOO-314 section in `docs/testing/agent-orchestration-mutation-checks.md`.

| Guard | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| Exact-once re-anchoring | accept the first match | `pytest -q backend/tests/unit/services/test_peer_review_rules.py -k ambiguous` | `carried` on an ambiguous quote |
| Change-or-rationale CHECK | drop it in a scratch migration | `pytest -q backend/tests/integration/test_peer_review_postgres.py` | step 4: an empty no-change accepted |
| Response tip check plus `UNIQUE(supersedes_response_id)` | skip the tip check **and** drop the unique | same | step 8: two tips |
| ADJUDICATE on resolution | use EDIT | same | step 5: the owner resolves |
| `touches` overlap | return `True` | same | step 3: an unrelated revision accepted |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_peer_review_rules.py backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/services/test_audit_bundle.py
pytest -q backend/tests/unit/api backend/tests/unit/architecture
(cd backend && python ../scripts/ci/check_alembic.py)      # single head d2a4c6e8f0b1
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists.** Report each of these as NOT RUN, never as passing:
- **PostgreSQL proof:** needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **Real journal review import:** no pilot round exists yet, so the journey uses a synthetic round.
- **Live journey:** needs a deployed stack with `d2a4c6e8f0b1`.

## Authenticated journey list for Linear closure

1. **Round:** record a two-reviewer round against a saved draft version with three anchored comments.
2. **Revise:** save a revision that rewrites one commented sentence and deletes another.
3. **Respond:** give C1 a change response linked to the revision (check the inline diff) plus a claim as evidence, and give C2 a no-change response with a rationale.
4. **Resolve:** as the adjudicator, resolve C1. The owner without the role sees no Resolve control and gets 403 on the API.
5. **Reload:** C2 is carried with new offsets, and C3 shows `unresolved anchor` with its original quote.
6. **Export:** download the Markdown and JSON. Unresolved items lists C2 and C3.
7. **Navigate:** from C1's response, open the old and new text and the evidence claim.

Record the SHA/PR, CI links, exports, the PostgreSQL junit, the mutation transcripts and the NOT RUN list.

## Seams

- **Upstream (GOO-306/307):** reads claim versions and `assertion_spans` only, and writes nothing to claims or releases.
- **Next (GOO-315):** `open_obligations(draft_id)` is the peer-review obligation input to verified promotion, when any round exists for the project.
- **GOO-316:** anonymized packages exclude reviewer `display_name` and every peer-review actor name.

## Out of scope

Each item below has a `ponytail:` marker at its seam:
- importing reviews from journal systems (Editorial Manager, ScholarOne);
- emailing reviewers;
- fuzzy re-anchoring;
- character-level diffs;
- reviewer accounts;
- a rebuttal-letter template engine (the export is one fixed layout).

# GOO-306 Versioned Claims + Evidence Links to Manuscript Passages Plan (Academic R4)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A manuscript claim gets a stable, project-scoped identity and immutable versions. Each version pins the exact draft (`id`, `version`, content hash) and the code-point offsets of its passage. Evidence links (append-only) tie a claim version to an accepted GOO-304/305 extraction value or to a verified span of an exact retained source revision. Old citations become explicit `legacy_unanchored` links. Model stance classifications are snapshotted as immutable observations, and a human ADJUDICATOR's assessment is a separate, rationale-bearing chain. A project-level export lets a reader resolve source → observation → claim → passage offline. Nothing existing is replaced: `stance_classifications`, `draft_reviews` and GOO-304/305 rows are only read.

**Architecture:** Five insert-only tables, with no updates and no deletes:
- `research_claims`
- `research_claim_versions`
- `research_claim_evidence_links`
- `research_claim_stance_observations`
- `research_claim_assessments`

There is one pure module, `claim_rules.py`, and one service, `claims_service.py`. There is one router, `api/research/claims.py`, mounted beside drafts at `/api/v1/projects/{project_id}/claims`. Events go to the existing ledger (`backend/src/services/research_decisions/ledger.py`) under a new family, `research_claims`, with **one stream per Collection** (`aggregate_id = collection_id`, the identity-service shape at `backend/src/services/research_engine/identity_service.py:66-72`). The stream is per Collection so that claim *creation* is idempotent too: a per-claim stream would not exist before the claim does. Every writer uses the existing lock order: `resolve_project` takes Workspace SHARE, then Collection UPDATE, then reloads roles (`backend/src/services/research_engine/project_access.py:196-205,260-289`). The `research_claims` stream `FOR UPDATE` comes next (`ledger.py:405-447`). GOO-304's `accept_value` and its worker also hold Collection UPDATE, so the extraction rows that a link reads can't move underneath it.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, the PostgreSQL integration fixture (`postgres_container`/`RESEARCH_DECISION_DATABASE_URL`), openapi-typescript, Next.js with TanStack Query and Vitest.

**Dependencies:** This PR stacks on GOO-305 (`docs/plans/2026-09-30-goo-305-source-anchors.md`), which stacks on GOO-304 (`…-goo-304-extraction-forms.md`), on `feat/goo-304-extraction-forms` @ `b8110e3f1`. The names it consumes from them:

| Consumed | Where |
|---|---|
| `extraction_accepted_values` (tip via `supersedes_accepted_value_id`, `source_hash`, `observation_ids`) and `extraction_rules.is_stale` | GOO-304 :121-128, :41, :72 |
| `extraction_observations.{text_sha256, anchor_status, anchor_start_char, anchor_end_char, citation}` and `extraction_accepted_values.{anchor_observation_id, anchor_resolution}` | GOO-305 :126-146 |
| `source_anchors.verify_anchor`, `text_sha256`, and "offsets are Python code points into `Document.content_text` as stored" | GOO-305 :30, :58-66 |
| `extraction_forms_service.document_source_hash` | GOO-304 :35, :191 |

---

## Decisions (read before Task 1)

| Question | Decision | Why |
|---|---|---|
| Claim identity | `research_claims(id, collection_id FK collections RESTRICT, created_by_id FK users, created_at)` with `UNIQUE(id, collection_id)`, the target of the composite FKs. It has no text, no status and no draft. | The ticket asks for a minimal stable id. Everything that can change lives in versions. |
| Version row | `research_claim_versions`: `id, collection_id, claim_id, version_no, kind, attributed_to_user_id, text, text_sha256, normalized_hash, draft_id, draft_version, draft_content_hash, start_char, end_char, draft_review_id, created_by_id, created_at, supersedes_claim_version_id`. Constraints: `UNIQUE(claim_id, version_no)`, `supersedes_claim_version_id UNIQUE`, a partial unique `(claim_id) WHERE supersedes_claim_version_id IS NULL` (one v1), `CHECK ((supersedes_claim_version_id IS NULL) = (version_no = 1))`, `CHECK (0 <= start_char AND start_char < end_char)`, and `FK (claim_id, collection_id) → research_claims(id, collection_id)`. | The tip is the row that nothing supersedes, which is the same pattern as the GOO-301/302/304 chains. The composite FK makes a cross-project version impossible in the database. The column names follow the GOO-307 plan's assumptions (`draft_content_hash`, `start_char`, `end_char`, `supersedes_claim_version_id`, `kind`, `attributed_to_user_id`, GOO-307 :341), so GOO-307 renames nothing. |
| Passage anchor | `text` must equal `draft.content[start_char:end_char]` **exactly**, sliced by the server. `draft_content_hash = sha256(draft.content.encode("utf-8"))`, the `DraftReview.candidate_content_hash` recipe (`backend/src/services/research/draft_generation_service.py:1394`). Offsets are code points, as in GOO-305 :30. A mismatch gives 422 `Text does not match the draft passage`. | "Offsets into that draft content." `generated_drafts` has no hash column (`backend/src/models/generated_draft.py:26-60`) and its content is never updated in place (the only writers are inserts at `draft_generation_service.py:594,1296`), so the hash is computed, not stored there. A reader can check the passage offline by slicing. |
| Wording change | A new version needs a new `(draft_id, start_char, end_char)` whose slice is the new text. A body identical to the tip gives 409 `No change`. The claim's `kind` can change between versions. | "Wording changes create a new version." The wording *is* the manuscript text, so a rewording shows up as a revised draft (`revise_draft`, `:1169-1356`). `# ponytail: allow a free-text claim decoupled from the passage if authors need paraphrased claims.` |
| Normalized hash | `normalized_hash = consensus_calculator._generate_claim_hash(text)` (`backend/src/services/evidence/consensus_calculator.py:20-29`). This is **the same key** as `stance_classifications.claim_hash` (`backend/src/models/evidence.py:41-46`). A unit test pins that the two are equal. `text_sha256` is the exact-bytes hash. | It reuses the stance join key, with no second normalizer. |
| `kind` | `factual \| interpretation` (default `factual`). `attributed_to_user_id` is required iff `interpretation` (`CHECK`), and defaults to the author. | GOO-307 exempts attributed interpretations from assessment (GOO-307 :32). Adding two columns now saves GOO-307 a migration on this table. |
| Citation-review reuse | `draft_review_id` FK `draft_reviews` NULL is set when `generation_params.citation_review_id` names a `DraftReview` of this project whose `candidate_content_hash == draft_content_hash` (`draft_generation_service.py:617-618,1312-1313`), and `NULL` for drafts older than GOO-292. The read path projects `citation_review_status` = the `support_status` of that review's `claims[]` entry whose `text == version.text.strip()` (`citation_verification_service.py:330-380`), else `null`. | The review is reused by reference and never copied or re-run. |
| Draft deletion | `draft_id` FK `generated_drafts` **RESTRICT**. `DraftGenerationService.delete_draft` (`:1904`) checks first and raises `DraftRetainedError`, which the route (`backend/src/api/research/drafts.py:300-318`) maps to 409 `Draft has claims; it is retained as evidence`. The FK is the backstop. | Drafts are hard-deleted today (`:1917`). A pinned passage must stay resolvable. The only behavior change is a new 409 on a route whose spec declares only 200/422, so oasdiff sees no ERR. |
| Link row | `research_claim_evidence_links`: `id, collection_id, claim_version_id, kind, accepted_value_id, draft_citation_id, document_id, source_hash, text_sha256, start_char, end_char, quote, status ∈ {linked, withdrawn}, supersedes_link_id UNIQUE, created_by_id, created_at`. It has `FK (claim_version_id, collection_id) → research_claim_versions(id, collection_id)` (the versions table gets `UNIQUE(id, collection_id)`). `CHECK`s per kind: **`extraction`** needs `accepted_value_id` set and no span columns (the span lives on GOO-305's anchor observation). **`source_span`** needs `document_id`, `source_hash`, `text_sha256`, `start_char < end_char` and `quote`. **`legacy_unanchored`** needs `draft_citation_id`, with `source_hash`, `text_sha256` and the span all `NULL`. A withdrawal requires `supersedes_link_id` to be set. | Append-only. Removing a link appends a `withdrawn` row. Re-pointing it appends a new row that supersedes the old one. The DB enforces the claim-side tenancy. The target side (a GOO-304 row or an org-scoped document) has no `collection_id`, so the service checks it (next row). |
| Target checks (service) | The claim version must be the tip, else 409 `Claim version is stale; reload`. **`extraction`:** the accepted value's `form_version → matrix.project_id` must equal this project, the matrix must not be deleted, and the document must be visible through `project_documents_query` (`project_access.py:94-112`). Otherwise 404 `Evidence not found`. It must be the chain tip and not stale (GOO-304 `is_stale`, GOO-305 `source_changed`), else 409 `Evidence is stale`. The server copies `document_id`, `source_hash`, and the anchor observation's `text_sha256` (or the first cited observation's). **`source_span`:** the document must be visible (else 404 `Evidence not found`). `source_hash = document_source_hash(doc)` and `text_sha256 = source_anchors.text_sha256(doc.content_text)` are taken from the **current** row. `verify_anchor(text, quote, start_hint=start_char).status` must be `verified`, else 422 `Span does not match the source`. **`legacy_unanchored`:** the `DraftCitation` must have `draft_id == version.draft_id`, else 422 `Citation belongs to another draft version`. `document_id` is copied from it and may be `NULL`, because `draft_citations.document_id` is `SET NULL` (`backend/src/models/draft_citation.py:36-41`). | A cross-project link or a link to another project's document returns 404 and does not leak existence. Cross-source links fail verification. Legacy `[Doc N]` citations have no offsets, so they are labelled `legacy_unanchored` and never promoted. |
| Stance observations: extend `stance_classifications` or add a table? | **New table `research_claim_stance_observations`**, insert-only: `id, collection_id, link_id, stance, classifier_confidence, justification_excerpt, classifier_version, inference_model_version, source_content_hash, stance_classification_id (FK SET NULL, provenance only), classified_at, observed_by_id, created_at`. There is no unique key, so every snapshot is a row. The `evidence` router is not touched. | This is the smaller diff. Making the existing rows append-only would mean changing the raw-SQL-guarded unique index (`backend/alembic/versions/add_org_id_to_stance_classifications.py:59-75`), both upsert branches (`backend/src/api/evidence/router.py:239-281`), and the `/breakdown` read, which assumes one row per key (`:466-500`). Its cache and tests would also change. Those rows are keyed by `(org, claim_hash, source)`, not by project or link, so they could not carry the link anyway. The meter keeps upserting its cache, and the claim record is the immutable snapshot. `classifier_version` is already a fingerprint of the classifier's full configuration (`backend/src/services/evidence/stance_classifier.py:86-92`), and it is stored beside `inference_model_version`. |
| How an observation is produced | `POST …/links/{link_id}/observations` (EDIT) looks up the existing `stance_classifications` row where `organization_id = context.organization_id`, `claim_hash = version.normalized_hash`, `source_id = link.document_id`, `model_version = _classifier_version()` (`router.py:128-133`) and `source_content_hash = link.text_sha256`. It snapshots that row. No match gives 409 `No stance classification for this claim and source revision; run the evidence meter`. A `legacy_unanchored` link gives 409 `Legacy links cannot be assessed`. | No LLM call runs inside a claims write. It reuses the meter's classifier output, and its `source_content_hash` is `sha256(content_text)` (`backend/src/services/evidence/source_loader.py:20-24`), which is GOO-305's `text_sha256`, so the revision match is exact. The stance claim text is limited to 10-1000 characters by the meter's query (`router.py:297-299`). `# ponytail: classify inline if users won't open the meter first.` |
| Human assessment | `research_claim_assessments`: one insert-only chain **per claim version**. Columns: `id, collection_id, claim_version_id, stance ∈ {supporting, opposing, neutral, not_addressed, unresolved}, link_ids JSONB (0..20), stance_observation_ids JSONB (0..20), rationale (1..2000), assessed_by_id, actor_role, created_at, supersedes_assessment_id UNIQUE`, a partial unique `(claim_version_id) WHERE supersedes_assessment_id IS NULL`, `CHECK (actor_role = 'adjudicator')`, and a composite FK to the versions table. The service requires every `link_id` to be a live (tip, `linked`, not `legacy_unanchored`) link of this version, and every observation to belong to a cited link. `link_ids` must be non-empty unless the stance is `unresolved` or `not_addressed`. The stance values reuse `StanceEnum` (`evidence.py:20-26`) plus `unresolved`. | One tip per claim version is what GOO-307's gate reads (GOO-307 :31, :343). An assessment judges the claim version on its cited links, so a per-link chain would force GOO-307 to aggregate. "Decision version" is the chain position. The JSONB id sets follow GOO-304's `observation_ids` (:130). |
| Who assesses | `ResearchAction.ADJUDICATE` only (403 `adjudicator role required`, `project_access.py:287-289`). Owners and editors get no implicit power. | This matches GOO-304's acceptance rule (:38). An explicit reviewer-assignment table would be new infrastructure with nothing to enforce yet (blinding is GOO-302's concern). `# ponytail: reuse GOO-301 assignments if per-claim workload is needed.` |
| Who authors | Claims, versions, links and observation snapshots need `ResearchAction.EDIT` (workspace editor or higher). Observation events carry `actor_role='machine'`, with `actor_user_id` set to the requester (GOO-304 :32). | Authoring the manuscript is editing. The snapshot's content is machine output, and the person who asked for it is recorded. |
| Idempotency | Every POST carries `idempotency_key` (1..255). Under the stream lock, `identity_service._replayed_event` (`:75-99`) runs **before** any validation. The same key with the same body returns 200 and the original rows, looked up by the ids in the payload. The same key with a different body gives 409 `Idempotency conflict`. `request_fingerprint = decision_request_fingerprint(body without the key)` (`ledger.py:139`). | This is the GOO-301/304 pattern. The first write returns 201 and a replay returns 200. |
| Concurrency backstop | An `IntegrityError` on either partial-unique or supersedes-unique index rolls back and returns 409 `… is stale; reload`, keyed on SQLSTATE `23505` plus the constraint name (GOO-301 `a51622bc2`). | The tip checks run under the lock, and the index is the second guard. |
| Derived staleness (read only) | Each link response carries `source_changed`: the link's `text_sha256` differs from `sha256` of the document's current `content_text`, or its accepted value is no longer the tip or `is_stale`. Nothing is written. | This is GOO-305's derive-on-read rule. GOO-307 persists release staleness; this ticket doesn't. |
| Export | New `GET /api/v1/projects/{project_id}/claims/export?draft_id=` (VIEW). It returns `{"schema": "nous.academic.claims-export.v1", "exported_at", "body_sha256", "body"}`, where `body_sha256 = contracts.canonical_json_sha256(body)` (`backend/src/services/research_engine/contracts.py:27-40`). The response uses `Content-Disposition: attachment; filename="claims-{project_id}-{body_sha256[:12]}.json"`, as `runs.py:537-561` does. See Task 5 for the body. The export performs zero writes. | This gives a reader everything needed to resolve source → observation → claim → passage without the app. The GOO-300/303 package shape is reused. |
| "Replace the empty evidence section in `research_engine/export_service.py` ~L53-76" | **Not done. The premise doesn't hold at `b8110e3f1`.** `:53-76` is `ExportArtifact`/`ResearchExportError`. Run evidence is projected from typed step envelopes (`export_service.py:730-781`, `report_rendering.py:211-273`) and is empty only for legacy runs (`:885-892`). That export is owner-scoped and run-scoped over a `ResearchProject` (`:88-104`), not over a Collection's drafts. The GOO-300 corpus export (`corpus_export.py`) isn't on this branch. The draft Markdown/LaTeX export (`draft_generation_service.py:2031-2089`) is left to GOO-307, which rewrites it with status labels (GOO-307 :40). | Splicing manuscript claims into a run brief would attach evidence to the wrong subject. The claims package is the project-level evidence export. **Say so if the run export must embed it anyway** (it would be one `claims_package_sha256` field). |

**Migration head:** new revision `c4e6a8b0d2f5_create_research_claims.py` (12 characters, within the limit of 32 in `scripts/ci/check_alembic.py:40`), with `down_revision = "b8d0f2a4c6e9"` (GOO-305 :48). The chain is 304 `a3c5e7f9b1d4` → 305 `b8d0f2a4c6e9` → 306 `c4e6a8b0d2f5` → 307 (`d7f9b1c3e5a8`, GOO-307 :43).

**Re-point rule:** `down_revision` always names the *direct stack parent*. After every rebase, run `(cd backend && python ../scripts/ci/check_alembic.py)` and set it to the parent's single head. On this branch today that head is `e1f3a5c7d9b2` (measured 2026-09-29: "single head 'e1f3a5c7d9b2', 87 revisions"). Two heads mean a parent has not been re-pointed, so fix it there. Never add a merge revision.

The migration imports nothing from `src`. It copies `_deny_data_api` (`backend/alembic/versions/e1f3a5c7d9b2_create_screening_queues.py:20-31`), applies it to all five tables, and has no backfill. Downgrade drops the tables in reverse FK order.

---

### Task 1: Pure rules (`claim_rules.py`)

**Files:**
- Create `backend/src/services/research/claim_rules.py`.
- Create `backend/tests/unit/services/test_claim_rules.py`.

```python
KINDS = ("factual", "interpretation"); LINK_KINDS = ("extraction", "source_span", "legacy_unanchored")
STANCES = tuple(s.value for s in StanceEnum) + ("unresolved",)
def content_hash(content: str) -> str                                   # sha256 utf-8, DraftReview recipe
def normalized_hash(text: str) -> str                                   # delegates to consensus_calculator
def check_passage(content: str, start: int, end: int, text: str) -> None   # ValueError on mismatch/out-of-range/blank
def check_link_shape(kind, **cols) -> None                              # mirrors the DB CHECKs, for 422 before insert
def check_assessment(stance, link_ids, observation_link_ids, live_link_ids) -> None
def link_source_changed(link_text_sha, current_text_sha, accepted_is_tip, accepted_stale) -> bool
```

**Tests:**
- `test_normalized_hash_equals_stance_claim_hash` (this pins the join key)
- `test_passage_must_equal_exact_slice_code_points` (`"naïve 🧪 trial"`)
- `test_passage_out_of_range_or_blank_rejected`
- `test_link_shape_per_kind` (a legacy link with a hash is rejected; `source_span` without a quote is rejected)
- `test_assessment_requires_live_links_unless_unresolved`
- `test_observation_must_belong_to_cited_link`

**Run:** `pytest -q backend/tests/unit/services/test_claim_rules.py`. It fails on the import first, then passes.
**Commit:** `feat(research): pure claim passage and link rules (GOO-306)`

---

### Task 2: Models + migration

**Files:**
- Create `backend/src/models/research_claim.py`, with the five classes described in Decisions. It uses the `_fk`/`_created_at` helper pattern from `backend/src/models/screening.py:32-40`.
- Export them from `backend/src/models/__init__.py`.
- Create `backend/alembic/versions/c4e6a8b0d2f5_create_research_claims.py`.

FK `ondelete`:
- `RESTRICT` everywhere (collections, users, `generated_drafts`, `draft_reviews`, `draft_citations`, `documents`, `extraction_accepted_values`, and the self-references);
- except `stance_classification_id`, which is `SET NULL` because that row is a mutable cache and the snapshot carries its values.

**Verify:**
- `(cd backend && python ../scripts/ci/check_alembic.py)` reports a single head `c4e6a8b0d2f5`.
- `cd backend && alembic upgrade head --sql | grep -c research_claim_evidence_links` is greater than 0 (offline, no Docker).

**Commit:** `feat(research): research claim, version, link, observation and assessment tables (GOO-306)`

---

### Task 3: Ledger family `research_claims`

**Files:**
- Modify `backend/src/services/research_decisions/ledger.py`:
  - add the vocabulary beside `:82-114`;
  - add `_validate_claims_payload` beside `_validate_screening_payload` (`:345`);
  - add `_validate_claims_transitions` after `:785`;
  - add the entry to `_FAMILIES` (`:788-810`);
  - update the docstring's family list (`:3-6`).
- Modify `backend/tests/unit/services/test_research_decision_ledger.py`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `claim.versioned` | `collection_id, claim_id, claim_version_id, version_no, supersedes_claim_version_id, kind, attributed_to_user_id, text_sha256, normalized_hash, draft_id, draft_version, draft_content_hash, start_char, end_char, draft_review_id` | `editor` |
| `claim.linked` | `collection_id, claim_id, claim_version_id, link_id, supersedes_link_id, status, kind, accepted_value_id, draft_citation_id, document_id, source_hash, text_sha256, start_char, end_char, quote_sha256` | `editor` |
| `claim.observed` | `collection_id, claim_id, link_id, observation_id, stance, stance_classification_id, classifier_version, inference_model_version, source_content_hash, classified_at` | `machine` |
| `claim.assessed` | `collection_id, claim_id, claim_version_id, assessment_id, supersedes_assessment_id, stance, link_ids, stance_observation_ids` | `adjudicator` (the rationale goes in `reason`) |

`claim.observed` is a fourth event beyond the ticket's three. Keeping machine snapshots out of `claim.assessed` means the rule "assessed ⇒ adjudicator" stays a single line. Family settings: `subject_type="research_claim"`, `subject_id=claim_id`, `requires_subject_version=False`.

**Replay rules:**
1. Every event has the same `collection_id`.
2. **`versioned`:** v1 has `supersedes` null. Otherwise `version_no = prev + 1` and `supersedes` = the claim's current tip. Ids are never reused.
3. **`linked`:** the version was versioned earlier and is that claim's tip at this point. `supersedes_link_id` is null for a new chain, or else the current tip of that chain, on the same version. `withdrawn` requires `supersedes`. The kind-specific keys are non-null exactly as the CHECKs require.
4. **`observed`:** `actor_role == 'machine'`. The link is live and its kind is not `legacy_unanchored`. `source_content_hash == link.text_sha256`.
5. **`assessed`:** `actor_role == 'adjudicator'`. The version is the tip. Every `link_id` is live on that version. Every observation belongs to a cited link. `supersedes` equals the version's assessment tip.

**Tests:**
- `test_claims_replay_rejects_machine_assessment`
- `test_claims_replay_rejects_forked_version_chain`
- `test_claims_replay_rejects_link_to_non_tip_version`
- `test_claims_replay_rejects_assessment_citing_withdrawn_link`
- `test_claims_replay_rejects_observation_on_legacy_link`
- `test_claims_payload_keys_exact`

**Commit:** `feat(research): research_claims decision family and replay rules (GOO-306)`

---

### Task 4: Service + routes + contracts

**Files:**
- Create `backend/src/services/research/claims_service.py`.
- Create `backend/src/api/research/claims.py`, with `router = APIRouter(prefix="/api/v1/projects/{project_id}/claims", tags=["claims"])`. Export it from `backend/src/api/research/__init__.py` and include it in `backend/src/main.py` next to `drafts_router` (`:642`).
- Modify `backend/src/services/research/draft_generation_service.py:1904` (add `DraftRetainedError`) and `backend/src/api/research/drafts.py:300-318` (map it to 409).
- Create `backend/src/shared/claim_schemas.py` with these models:
  - `ClaimCreate{draft_id, start_char, end_char, text(1..4000), kind, idempotency_key}`
  - `ClaimVersionCreate{…, supersedes_claim_version_id}`
  - `ClaimLinkCreate{claim_version_id, kind, accepted_value_id?, document_id?, start_char?, end_char?, quote(1..2000)?, draft_citation_id?, status="linked", supersedes_link_id?, idempotency_key}`
  - `StanceObservationCreate{idempotency_key}`
  - `ClaimAssessmentCreate{claim_version_id, stance, link_ids(0..20), stance_observation_ids(0..20), rationale(1..2000), supersedes_assessment_id?, idempotency_key}`
  - the matching `*Response` models, `ClaimSummary` and `ClaimListResponse`.
- Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

```python
async def create_claim(db, context, actor_id, data) -> tuple[ClaimResponse, bool]        # (resp, replayed)
async def create_version(db, context, claim_id, actor_id, data) -> tuple[ClaimVersionResponse, bool]
async def link(db, context, claim_id, actor_id, data) -> tuple[ClaimLinkResponse, bool]
async def observe_stance(db, context, claim_id, link_id, actor_id, key) -> tuple[StanceObservationResponse, bool]
async def assess(db, context, claim_id, actor_id, data) -> tuple[ClaimAssessmentResponse, bool]  # ADJUDICATOR in effective_roles
async def list_claims(db, context, draft_id: UUID | None) -> ClaimListResponse            # VIEW
async def get_claim(db, context, claim_id) -> ClaimDetailResponse                          # VIEW, full history
async def export_package(db, context, draft_id: UUID | None) -> tuple[bytes, str]         # VIEW, zero writes
```

**Write order** (every writer; the same shape as GOO-304 :194-203):
1. The route resolves the context with `resolve_project(EDIT | ADJUDICATE)`.
2. `lock_aggregate_stream(research_claims, collection_id)`.
3. `_replayed_event`.
4. Load the claim, version and link scoped to `context.collection.id`, else 404 `Claim not found`.
5. Load the draft with `GeneratedDraft.project_id == context.collection.id`, else 404 `Draft not found`.
6. Target and tip checks, plus `claim_rules`.
7. Insert the row, then `append_decision`.
8. **Seam for GOO-307**, after the append and before the commit: this is where `invalidate_dependents(...)` goes (GOO-307 :36). Keep steps 7-9 in one ordered block.
9. `commit()` once. On an `IntegrityError` backstop, roll back and return 409.

**Routes:**

| Route | Action | Returns |
|---|---|---|
| `GET /claims?draft_id=` | VIEW | For each claim: its tip version, or when `draft_id` is given, the versions pinned to that draft. Also live links with `kind`, `source_changed` and the latest observation per link, the assessment tip, and `citation_review_status`. Counts are `{claims, links_by_kind, legacy_unanchored, assessed, unassessed}`, and they apply the same filter helper as the list (`gotchas.md` "Search"). |
| `GET /claims/{claim_id:uuid}` | VIEW | Every version, link, observation and assessment, in `created_at, id` order. |
| `GET /claims/export?draft_id=` | VIEW | The package download. Declared before `{claim_id:uuid}`. |
| `POST /claims`, `POST /claims/{id}/versions` | EDIT | 201, or 200 on a replay. |
| `POST /claims/{id}/links` | EDIT | 201 / 200 |
| `POST /claims/{id}/links/{link_id}/observations` | EDIT | 201 / 200 |
| `POST /claims/{id}/assessments` | ADJUDICATE | 201 / 200 |

Errors use the stable detail strings from Decisions and never exception text (`AGENTS.md` → `backend.md`). Routes don't commit (`backend/tests/unit/architecture/test_workspace_boundaries.py`).

**oasdiff:** the change adds new paths and schemas only. The drafts `DELETE` gains a runtime 409 that its spec doesn't declare (`responses: 200, 422` in `backend/openapi.json`). **Expected result: no ERR-level change.**

**Unit tests** go in `backend/tests/unit/api/test_claims_routes.py`, dependency-overridden like GOO-304's `test_extraction_forms_routes.py`:
- `test_create_requires_edit_viewer_404`
- `test_assess_requires_adjudicator_owner_403`
- `test_text_mismatch_422_stable_detail`
- `test_legacy_link_has_no_hash_or_span`
- `test_delete_draft_with_claims_409`
- `test_replay_returns_200_same_ids`

**Commit:** `feat(research): versioned claims, evidence links and assessments API (GOO-306)`

---

### Task 5: Export package + automation guard

**Files:**
- Create `backend/src/services/research/claims_export.py` (pure: `build_body(rows) -> dict`, `package(body, exported_at) -> dict`).
- Create `backend/tests/unit/services/test_claims_export.py`.
- Modify GOO-304's `backend/tests/unit/architecture/test_extraction_acceptance_boundary.py`. Extend its forbidden-name set with `assess` from `claims_service` and `ResearchClaimAssessment`, so that `src/tasks/` and `src/services/agent/` cannot assess claims. Also assert that `assess`'s body names `ResearchProjectRole.ADJUDICATOR`.

**Package body** (all lists `ORDER BY created_at, id`; canonical JSON):
- `collection_id`, `draft_filter`, and `stream_head` (the `research_claims` stream's `next_seq - 1`).
- `drafts[]`: `{id, version, content_hash, content, content_hash_matches}`. The manuscript is the project's own text, and it is included so that passages resolve offline.
- `documents[]`: `{id, title, source_hash, current_text_sha256}`. There is **no `content_text`**, following the GOO-300 restricted-content rule (GOO-300 :23).
- `extraction.accepted_values[]` and `extraction.observations[]`: only the rows reachable from `extraction` links (accepted value → `observation_ids` and `anchor_observation_id`), each with its anchor columns and `citation`.
- `claims[]`, `claim_versions[]`, `links[]` (with `quote`), `stance_observations[]`, `assessments[]` (with `rationale`, `assessed_by_id`, `actor_role`).
- `counts`: `{claims, claim_versions, links_by_kind{extraction, source_span, legacy_unanchored}, withdrawn_links, stance_observations, assessments_by_stance, unassessed_tip_versions}`.
- `resolution[]`: one row per live link, `{claim_version_id, passage: [draft_id, start_char, end_char], link_id, kind, observation_id | null, document_id, source_hash, text_sha256, span | null}`, so that a reader can walk the chain without joining anything themselves.

**Tests:**
- `test_body_hash_stable_under_row_order` (shuffled input gives the same `body_sha256`)
- `test_every_passage_resolves_by_slicing`
- `test_counts_match_rows`
- `test_legacy_links_counted_and_unresolved`
- `test_no_document_text_in_package`

**Commit:** `feat(research): reconstructable claims evidence export (GOO-306)`

---

### Task 6: PostgreSQL proof (one test)

**Files:**
- Create `backend/tests/integration/test_research_claims_postgres.py`, with `pytestmark = pytest.mark.integration`. The fixture copies the `screening_factory` shape (`backend/tests/integration/test_screening_queue_postgres.py:97-140`): `Base.metadata.create_all`, then drop **this ticket's five tables**, then run only `c4e6a8b0d2f5.upgrade()` (the models already create the GOO-304/305 tables). Seeding reuses `seed_approved_protocol_binding` (`research_engine_postgres_support.py`) for users and the project.

**`test_claims_versions_links_assessments_tenancy_and_export`**, in order:

1. **Seed.**
   - Org A: editor E (the workspace owner), adjudicator J, reviewer R, and projects P1 and P2.
   - Org B: user F.
   - Documents: D1 in P1 (text with a unique sentence at a known offset, plus `checksum_sha256`), and D2 in P2 only.
   - P1 draft v1: content containing the claim sentence, a `DraftReview` with the matching `candidate_content_hash`, and a `DraftCitation` `[Doc 1]` → D1.
   - GOO-304/305 rows: a P1 matrix, form version, verified observation and accepted tip on D1, and an accepted tip in P2's matrix.
   - One `stance_classifications` row for `(org A, normalized_hash, D1, classifier_version, source_content_hash = sha256(D1.content_text))`.
2. **Create + idempotency.**
   - E creates a claim on v1's passage: 201.
   - The same key and body again: 200 with the same ids, and exactly one `claim.versioned` event.
   - The same key with a different body: 409.
3. **Commit/reopen.** In a new session, raw SQL checks `substring(d.content from v.start_char+1 for v.end_char-v.start_char) = v.text`. `encode(sha256(convert_to(d.content,'UTF8')),'hex') = v.draft_content_hash`, and `draft_review_id` is set.
4. **Version history.**
   - Seed draft v2 with the reworded sentence. E posts v2 (`supersedes` = v1): 201.
   - A stale `supersedes` gives 409. A body identical to the tip gives 409 `No change`.
   - Two concurrent posts with the same `supersedes` (`asyncio.gather`, separate sessions): exactly one 201, and `SELECT 1` still works afterwards.
   - v1's row is column-for-column unchanged.
5. **Links on v2.**
   - Linking P1's accepted value: 201.
   - Linking P2's accepted value: 404.
   - `source_span` on D1 with correct offsets: 201. With off-by-one offsets: 422. On D2: 404.
   - Linking v1's `DraftCitation`: 422. Linking v2's own citation: 201, stored as `legacy_unanchored` with null hashes.
   - A link to v1 (not the tip): 409.
   - **Constraint:** a raw `INSERT` of a link row with `collection_id = P2` and v2's id raises `IntegrityError` from the composite FK.
6. **Observations.**
   - E snapshots the span link: 201, with the stance and `classifier_version` copied.
   - `UPDATE stance_classifications SET stance='opposing'` (the meter re-upserting). The observation row is unchanged. A second snapshot is a **new** row, so there are 2 rows.
   - A snapshot on the legacy link: 409.
7. **Assessments.**
   - R assesses: 403.
   - J assesses v2 as `supporting`, citing the two live non-legacy links and the second observation: 201.
   - J again with `supersedes=None`: 409.
   - Two concurrent supersedes of the same tip: one 201.
   - Withdrawing the span link, then assessing with it cited: 422.
8. **Tenancy and lifecycle.**
   - F gets 404 on list, detail, create, link and export.
   - Archive P1: every POST gets 409 `Archived projects are read-only`, while GET and export get 200.
   - Deleting draft v1 gives 409, and the row still exists.
   - Detach D1 (`collection_documents.is_deleted`): a new `source_span` on D1 gets 404. The existing link reads with `source_changed=false`; `UPDATE documents SET content_text = content_text || 'x'` makes it `true`.
9. **Replay.** `replay_decisions(collection_id=P1, aggregate_type="research_claims", aggregate_id=P1)` succeeds, and the event counts per type equal the row counts.
10. **Export hash reconciliation.**
    - `GET /claims/export` bytes: `sha256(canonical_json_bytes(body)) == body_sha256`, the filename contains `body_sha256[:12]`, and every `resolution[]` passage slices to its version text.
    - Every `extraction` link's `observation_id` appears in `extraction.observations`.
    - The counts equal independent raw-SQL counts.
    - A second export has the same `body_sha256`. Row counts and `research_decision_streams.next_seq` are unchanged, so the export wrote nothing.
11. **Downgrade.** After `downgrade()` the five tables are gone, and `stance_classifications`, `generated_drafts` and the GOO-304/305 rows are intact.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_research_claims_postgres.py`. Without a database, report it as **NOT RUN**; the CI Integration job runs it.
**Commit:** `test(research): PostgreSQL proof for versioned claims and evidence links (GOO-306)`

---

### Task 7: Minimal frontend (read-only claims list)

**Files:**
- Create `frontend/src/types/api/research-claims-contract.ts`, aliasing the generated `ClaimListResponse`/`ClaimSummary` (the pattern is `research-screening-contract.ts`).
- Modify `frontend/src/services/projectService.ts` to add `listClaims(projectId, draftId)` and `downloadClaimsExport(projectId, draftId)`, placed after `listDraftReviews` (`:486-490`).
- Create `frontend/src/hooks/useDraftClaims.ts`, shaped like `useDraftReviews.ts` with key `['project', projectId, 'claims', draftId]`.
- Create `frontend/src/components/research/DraftClaimsPanel.tsx`.
- Modify `frontend/src/components/research/steps/DraftStep.tsx:157`: render `<DraftClaimsPanel projectId draftId={currentDraft.id} />` under `DraftReviewSummary` when there is a draft.
- Create the test `frontend/src/components/research/__tests__/DraftClaimsPanel.test.tsx`.

**Behavior:** a list, one row per claim, containing:
- the passage text, truncated;
- `v{version_no}`;
- `Interpretation — {name}` when applicable;
- link counts per kind, with a text badge "Legacy (unanchored)";
- a "Source changed" badge;
- the latest model stance as a word, never a percentage;
- the assessment tip ("Accepted: supporting by {name}") or "Unassessed".

It also has an "Export evidence" button. There is no create, link or assess UI; that belongs to GOO-307. Colors use theme tokens, and badges carry an `aria-label`.

**Tests:**
- `renders unassessed and legacy badges`
- `never renders a percentage`
- `export button calls downloadClaimsExport`

**Commit:** `feat(frontend): read-only claims list per draft version (GOO-306)`

---

## Mutation verification

Follow the procedure in `docs/engineering/testing.md:42-63`. Record each guard's file:line and the focused command in the test docstring, and under a GOO-306 section in `docs/testing/agent-orchestration-mutation-checks.md`.

| Guard | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| `create_version` tip check | `if False:` | the Postgres test | step 4: the concurrent or stale 409 becomes an unhandled `IntegrityError` on `uq_research_claim_versions_initial`/supersedes |
| The `assess` tip check plus the SQLSTATE backstop | Re-raise the error | same | step 7: an unhandled `IntegrityError` in the concurrent step |
| The `_replayed_event` call | Delete it | `pytest -q backend/tests/unit/api/test_claims_routes.py -k replay` and step 2 | a second `claim.versioned`, or `DecisionIdempotencyConflict` |
| The accepted-value project check | Drop `matrix.project_id == collection.id` | step 5 | P2's accepted value links with 201 |
| Composite FK | Remove it from the migration | step 5 | the raw cross-project `INSERT` succeeds |
| `ADJUDICATOR` in `assess` | Drop the role check | `-k adjudicator` and the boundary guard | the reviewer's assessment returns 201 |
| Replay `assessed ⇒ adjudicator` | Delete the check | `pytest -q backend/tests/unit/services/test_research_decision_ledger.py -k machine_assessment` | no `DecisionReplayError` |
| Delete-draft retention | Remove the pre-check | `-k delete_draft_with_claims` | 500 (a raw FK error) instead of 409 |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_claim_rules.py backend/tests/unit/services/test_claims_export.py \
  backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/api/test_claims_routes.py \
  backend/tests/unit/architecture backend/tests/unit/services/test_draft_citation_review_pass.py
(cd backend && python ../scripts/ci/check_alembic.py)          # single head c4e6a8b0d2f5 (after re-point)
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts   # after committing both
pnpm --dir frontend exec vitest run src/components/research
scripts/ci/run_local_ci.sh --base origin/feat/goo-305-source-anchors --frontend
```

The existing evidence suites (`backend/tests/evidence/`, `backend/tests/test_evidence_getdb_sync.py`) must pass **unchanged**. That proves the stance path was not touched.

## Authenticated journey list (Linear closure)

Run this on `rag-dev` after the deploy. It is blocked until a backend origin is reachable (`docs/plans/2026-09-29-academic-r0-r1-closure.md`). Accounts: `allocs16@gmail.com` (editor), R (REVIEWER), J (ADJUDICATOR), and one account in a second org.

1. **Migration:** `kubectl -n rag-dev exec deploy/backend -- alembic current` shows `c4e6a8b0d2f5 (head)`, or GOO-307's revision if that has also landed.
2. **Claim:** as the editor, generate a draft, then `POST /claims` over one sentence (curl, offsets from `content`). The Draft step's claims panel lists it as `v1` with "Unassessed". Replaying the same curl returns 200 with the same id.
3. **Version:** revise the draft, then `POST /claims/{id}/versions` against v2's reworded sentence. The panel for v2 shows `v2`, and the panel for v1 still shows `v1`.
4. **Links:** link an accepted matrix value and a verified span. Linking a `[Doc N]` citation shows "Legacy (unanchored)". Linking another project's accepted value returns 404.
5. **Stance:** run the evidence meter for the claim text on the source, then `POST …/observations`. The panel shows the stance as a word. Re-running the meter doesn't change the snapshot.
6. **Assess:** R's assessment returns 403. J's assessment with a rationale shows "Accepted: supporting by J". A second assessment with the old `supersedes` returns 409.
7. **Denials:** the second-org user gets 404 on the list and the export. After archiving, POSTs return 409 and the export still downloads. Deleting v1 returns 409 `Draft has claims`.
8. **Export:** download it, then `python -c` recompute `sha256` of the canonical body. It equals `body_sha256` and the filename prefix. Slice one passage from `drafts[].content`.
9. **Browser:** take screenshots of the claims panel (legacy badge, unassessed, accepted, source changed).

## GOO-307 seam: misalignments with `2026-09-30-goo-307-verified-gating.md`

That plan was written before this one existed (GOO-307 :337) and assumed some names. Line by line:

| GOO-307 assumed (:341-345) | This plan (authoritative) | Action for GOO-307 |
|---|---|---|
| tables `claim_versions`, `claim_evidence_links`, `claim_assessments` | `research_claim_versions`, `research_claim_evidence_links`, `research_claim_assessments` (the ticket's names) | Rename its references. |
| columns `draft_content_hash, start_char, end_char, text, kind, attributed_to_user_id, supersedes_claim_version_id` | the same names, adopted | none |
| link `accepted_value_id \| (document_id, source_hash, text_sha256, start_char, end_char)`, exactly one target | the same, plus a third kind, `legacy_unanchored` (`draft_citation_id`, no hash), and a `status` column (`linked`/`withdrawn`) with a supersedes chain | The gate must treat a legacy link as non-supporting. A new blocker such as `legacy_unanchored`, or folding it into `stale_evidence`, is GOO-307's call. It reads only live link tips. |
| assessment `stance, accepted_by_id, actor_role, rationale, stance_classification_id NULL, supersedes_assessment_id`, one tip per claim version | one tip per claim version ✔, with `assessed_by_id` (not `accepted_by_id`). The model evidence is `stance_observation_ids` (immutable snapshots in `research_claim_stance_observations`), **not** a `stance_classification_id` pointing at the mutable meter row. The assessment also carries `link_ids`. | Rename the column. "`model_only`" = a live link that has observations but the version has no assessment tip. |
| stance `supports`, `unresolved` | `supporting`, `opposing`, `neutral`, `not_addressed`, `unresolved` (the `StanceEnum` values) | Set `release_rules.SUPPORTING_STANCES = {"supporting"}` (GOO-307 :347 already anticipates this). |
| family `research_claim`, events `claim.versioned`, `claim.assessed`, `claim.superseded` | family **`research_claims`**, events `claim.versioned`, `claim.linked`, `claim.observed`, `claim.assessed`. There is **no `claim.superseded`**: supersession is the `supersedes_*` key in versioned/linked/assessed. | Hook `invalidate_dependents` into this plan's `create_version`, `link` (a supersede or withdrawal) and `assess` (a supersede), at the step-8 seam. |
| migration `down_revision = "b8d0f2a4c6e9"` | this plan's id is `c4e6a8b0d2f5` | GOO-307's `d7f9b1c3e5a8.down_revision = "c4e6a8b0d2f5"`. |
| Edge walk `accepted_value → evidence_link → claim_version/assessment` | Satisfied: the link rows carry `accepted_value_id`, `document_id`, `source_hash` and `text_sha256`, and assessments cite `link_ids`. | none |
| `draft_releases.draft_id FK generated_drafts RESTRICT` | Compatible with this plan's `DraftRetainedError`. | GOO-307 adds "has releases" to the same pre-check. |

## Out of scope (owned elsewhere)

Each has a `ponytail:` marker at its seam:
- Promotion, gating, persisted staleness and the claim create/link/assess UI (GOO-307).
- Inline stance classification.
- Paraphrased claims decoupled from the passage.
- Carrying links over to a new claim version (the client re-links).
- Claims inside the run-brief export and the GOO-300 corpus export (neither is a Collection-draft subject on this branch).
- Per-claim reviewer assignments.

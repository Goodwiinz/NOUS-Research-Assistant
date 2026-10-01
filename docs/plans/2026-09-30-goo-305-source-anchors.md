# GOO-305 Source Anchors + Matrix Reconciliation Plan (Academic R4)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Every machine and human extraction observation carries a verifiable source anchor: the retained document id, pinned hashes, a verbatim quote, character offsets into the retained text, and a page when one is known. Extraction reads the whole retained text in bounded chunks instead of the first 12,000 characters. Accepting a value through GOO-304's path is refused when the anchor is ambiguous, unverified or points at a changed source, unless the adjudicator explicitly resolves it. The matrix gains an evidence drawer, a side-by-side disagreement view and an accept form with a rationale. No confidence percentage is shown anywhere.

**Architecture:** One new pure module, `source_anchors.py` (no DB), covers anchor verification, chunk windows and per-field aggregation. Anchors are **columns on GOO-304's `extraction_observations`**, and the quote is GOO-304's existing `citation` column. An anchor describes exactly one observation, so a separate table would add nothing. `extraction_accepted_values` gains an anchor-resolution snapshot. **This ticket adds no new routes.** GOO-304's `GET /matrices/{id}/observations` (`list_observations`) is extended with anchor, context and coverage fields, and GOO-304's `POST /observations` and `POST /accepted-values` bodies gain optional anchor fields. Every read and decision goes through `project_documents_query` (`backend/src/services/research_engine/project_access.py:94-112`). Events stay in GOO-304's `research_extraction` family as schema-v2 payloads, with no new event types.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, the `postgres_container`/`RESEARCH_DECISION_DATABASE_URL` integration fixture (`backend/tests/integration/conftest.py:208`), openapi-typescript, Next.js with TanStack Query and Vitest.

**Dependencies:** This plan stacks on GOO-304 (`docs/plans/2026-09-30-goo-304-extraction-forms.md`, branch `feat/goo-304-extraction-forms`, planned revision `a3c5e7f9b1d4`). GOO-304 stacks on GOO-303 (`f2a4c6e8b0d3`), and GOO-303 supplies the "retrieved document id + pinned hash" convention (GOO-303 plan :56). The names below were **checked against the GOO-304 plan**: its Decisions table (:23-49) and its "GOO-305 seam" section (:383-397). Every name comes from that plan; none is invented here.

| This plan uses (from GOO-304) | Where |
|---|---|
| `extraction_observations` (`kind ∈ {machine,human}`, `actor_user_id`, `extractor_run_id`, `extractor_model`, `value JSONB` xor `missingness`, `validation_state`, `citation`, `source_hash`) | GOO-304 :113-120 |
| `extraction_accepted_values` (`observation_ids` 1–20, `supersedes_accepted_value_id`, `rationale`, `source_hash`) | :122-128 |
| Missingness `not_reported \| not_applicable \| unavailable_text \| extraction_error \| unresolved_disagreement`, with its per-writer sets | :34 |
| `extraction_forms_service.{observe, accept_value, list_observations, append_machine_observations, cell_view, document_source_hash}` | :181-192 |
| `accept_value` write order. The anchor guard goes in **step 7**: after the lock and the idempotency replay, before the insert | :195-204, :397 |
| Ledger `research_extraction`: `extraction.observed` (one event per document, `observations{obs_id: field_id}`), `extraction.accepted`, `extraction.staled`, all schema 1 | :148-160 |
| Worker idempotency per `(task_id, document_id)` with a pre-LLM skip. Pinned `source_hashes`, with a mismatch meaning skip | :45, :206-214 |
| Legacy values are `extraction_cells` rows shown by `cell_view` as `source='legacy'`. They are **not** observations | :30-31, :390 |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| Which text do offsets index? | **`Document.content_text` exactly as stored**, with no normalization. Offsets are Python `str` indices (code points), `[start_char, end_char)`. The server slices every quote and context itself, so the client never indexes (JS strings use UTF-16). | Any normalization breaks `text[start:end] == quote`. The existing chunkers strip text and discard offsets (`backend/src/services/processing/llm_entity_extraction.py:34-60`, `backend/src/services/research/evidence_selection.py:54-75`), so neither is reused. |
| Which hashes are pinned? | GOO-304's `source_hash` (`checksum_sha256`, or `sha256(content_text)` when NULL, GOO-304 :36) **plus** a new `text_sha256 = sha256(content_text.encode("utf-8"))`. | `checksum_sha256` hashes the uploaded file bytes (`backend/src/services/documents/file_service.py:567`, `backend/src/services/arxiv/storage.py:80`). `content_text` is rewritten without a checksum change on reprocessing and caption merge (`backend/src/services/processing/multimodal_processing_service.py:1313,1324`). Offsets are only valid against the exact text. `document_versions` has no writer (`DocumentVersion`, `backend/src/models/document_processing.py:121`; GOO-303 plan :56). |
| Page | The page is the last `[Page N]` marker at or before `start_char`, via `_PAGE_MARKER_RE` (`evidence_selection.py:9`, imported and not copied). With no markers, `anchor_page = NULL`, and the UI says "page unavailable". | Some ingest paths write these markers (`backend/src/services/processing/processing_service.py:261`, `file_service.py:720`, `backend/src/services/arxiv/arxiv_service.py:763`). The PyMuPDF path joins pages with no marker (`multimodal_processing_service.py:311-316`). `# ponytail: marker-derived page only; persist a page→offset map at text extraction if reviewers need pages for marker-less PDFs.` |
| Anchor states | `verified`: the quote occurs exactly once in the window the model read, or a given `start` matches exactly. `ambiguous`: the quote occurs more than once in that window with no start; the ≤20 starts are stored. `unverified`: no quote, quote not found (OCR noise or paraphrase), or a mismatching start. `location_unavailable`: `content_text` is NULL or empty. `legacy_unanchored`: legacy `extraction_cells`, **computed in `cell_view` and never stored** (GOO-304 keeps legacy values out of observations). The anchor is `NULL` (not applicable) on missingness rows. | These are the cases in the ticket. `verify_anchor` is pure. |
| Ambiguity scope | Occurrences are counted within the model's window, and the offsets stored are global. `occurrences_in_text` (the count across the whole text) is stored for display. Human observations are verified against the whole text. | The model can only quote what it saw. The drawer still shows "appears N× in document". A human sees the whole document, so their quote must be unique in it or carry an explicit start. |
| Coverage | `CHUNK_CHARS = 12_000`, `OVERLAP = 500`, `MAX_CHUNKS = 8`. When the text needs more than 8 windows, the starts are spread evenly: `starts = [round(i*(n-W)/(k-1)) for i in range(k)]`. `inspected_coverage` holds the merged `[start, end)` ranges. `coverage_complete = merged == [[0, n]]`. | The end of any document is reachable, and the cost is ≤8 LLM calls per document (today it is 1, `backend/src/services/research/extraction_matrix_service.py:206-214`). `# ponytail: fixed 8×12k cap; make it a setting only if real matrices hit it.` |
| Model output | GOO-304's prompt shape per field is `{"value", "missing", "citation"}` (GOO-304 :47). `citation` is now asked for **verbatim**. Model offsets are neither requested nor trusted. Offsets come only from the server-side search. | This reuses the GOO-304 column and prompt. |
| Typed output errors | GOO-304 already maps unparseable JSON, a missing key or a malformed entry to `extraction_error` and keeps coercion failures as `invalid` (GOO-304 :35, :47). This ticket applies that **per chunk**. It also changes the current parser tests that expect `None`, `tests/unit/test_extraction_matrix_service.py:28-45`. That file lives in the **repo-root** `tests/`, which is why GOO-304 :395 did not find it under `backend/tests`. Whichever PR changes `_parse_extraction_result` first updates those two tests. | This meets the ticket's "never `not_reported`" rule without duplicating GOO-304's mapping. |
| Aggregating fields across chunks | Non-null candidates are grouped by `str(value).strip().casefold()`, giving **one observation per distinct value**. Each keeps the anchor of its first `verified` candidate, otherwise its first candidate. When no chunk found a value, the field gets `extraction_error` if any chunk returned an error for it, then `not_applicable` if any chunk said so, then `not_reported` only if `coverage_complete`, and otherwise `unavailable_text`. All rows go into the one per-document `extraction.observed` event (GOO-304 :394). | Distinct machine values stay separate observations, which the disagreement view shows. Automation never picks a winner. A partial read never claims `not_reported`. |
| Chunk transport failure | Unchanged from GOO-304: an exception writes nothing for that document, and Celery retries it (keyed idempotently per `(task, doc)`). | This keeps observations all-or-nothing per document. |
| Confidence | GOO-304 already stops writing it (GOO-304 :43). This ticket makes `CellCitation` **never render confidence**, and `cell_view` adds `confidence_calibration: "uncalibrated"` beside a non-null legacy `confidence`. Stored legacy values are not rewritten. | Ticket: "never a percentage". The constant 0.8 in legacy rows (`extraction_matrix_service.py:237,246`) was never measured. |
| Which observation's anchor governs an accept | The **anchor observation** is the cited, valid observation whose value equals the accepted value (GOO-304's "equals one cited" rule, :40). If several qualify, the one with the best status wins (`verified > ambiguous > unverified > location_unavailable`), and ties go to the earliest. It is stored as `anchor_observation_id`. Missingness and `unresolved_disagreement` accepts get `anchor_resolution='not_applicable'`. | This adds no new client field in the common case, and it is deterministic. |
| Resolving a bad anchor | `ambiguous` gives 409 `Anchor ambiguous; choose an occurrence`, unless the body's `anchor_start` is one of the stored occurrences and re-verifies (the result is `disambiguated`). `unverified` or `location_unavailable` gives 409 `Anchor unverified; confirm to accept`, unless `accept_unverified=true` (GOO-304 already requires the rationale; the result is `accepted_unverified`). Otherwise the result is `verified`. **An override** is GOO-304's path: a REVIEWER records a human observation with its own `citation` (+ optional `anchor_start`), verified the same way, and the adjudicator accepts it with a rationale. | Nothing is accepted silently. The snapshot keeps history readable after the source changes. |
| Re-check at decision time | `assert_anchor_acceptable` runs at GOO-304 `accept_value` step 7, which is after `resolve_project(ADJUDICATE)` has taken the locks and reloaded the roles (`project_access.py:196-235,261-289`). It re-fetches the document through `project_documents_query`, compares `source_hash` and `text_sha256` with the current document, and re-runs `verify_anchor` on the **current** text. | A document can be deleted, detached, moved out of the org or reprocessed between load and decision. |
| Source change → stale | This is **derived on read** in `cell_view` and `list_observations`: `source_changed = pinned (source_hash, text_sha256) != current`. It is **persisted** on the next write that touches the source. (a) The accept guard appends `extraction.staled` v2 (`reason='source_changed'`) for that document's accepted tips in the matrix and returns `SourceChanged`, the service **commits**, then the route raises 409 `Source changed; re-extract`. (b) The worker, before appending new observations for a rerun, appends the same event for tips pinned to an older hash. Rows are never updated. | GOO-304 leaves source-change `staled` events to this ticket (GOO-304 :42, :404). `content_text` has many writers, so hooking each one is out of scope, while the read flag is always correct. |
| New event types? | **None.** `extraction.observed` v2 changes each `observations{}` value to `{field_id, anchor_status, citation_sha256, start, end, page}` and adds a top-level `text_sha256, inspected_coverage`. `extraction.accepted` v2 adds `anchor_observation_id, anchor_resolution, anchor_start_char`. `extraction.staled` v2 adds `reason ∈ {form_changed, source_changed}, document_id, new_source_hash, new_text_sha256` with `new_form_version_id` nullable. | `_validate_event` requires exact key sets per `(event_type, version)` (`backend/src/services/research_decisions/ledger.py:171-190`), so a new version is the additive path, and v1 history still replays. Verification is deterministic and fully captured in `observed`. Reconciliation *is* `accepted`, so separate `anchor_verified`/`reconciled` events would duplicate them. |
| Evidence authorization | `list_observations` (VIEW) gains the document check: `project_documents_query(context.collection.id).where(Document.id == document_id)`. A miss gives 404 `Document not found`, with no citation or context in the body. An archived project gives 200, because reads are allowed; writes get 409 at `project_access.py:232-235`. A revoked member gets 404 (`:237-239`). | "Checks on evidence fetch and on final decision, not only initial load." GOO-304 checks document visibility only on writes (:200). |

**Migration head:** new revision `b8d0f2a4c6e9_add_extraction_source_anchors.py` with `down_revision = "a3c5e7f9b1d4"` (GOO-304 :51, which already reserves this id for GOO-305). The rule is the same across the stack: always name the direct stack parent and re-point after every rebase. Run `(cd backend && python ../scripts/ci/check_alembic.py)` and expect `single head 'b8d0f2a4c6e9'`. Never add a merge revision. If two heads appear, a parent has not been re-pointed, so fix it there. The measured head today on `a51622bc2` is `e1f3a5c7d9b2`.

---

### Task 1: Pure anchor module

**Files:**
- Create `backend/src/services/research/source_anchors.py` (stdlib plus the `_PAGE_MARKER_RE` import).
- Create `backend/tests/unit/services/test_source_anchors.py`.

```python
AnchorStatus = Literal["verified","ambiguous","unverified","location_unavailable"]
@dataclass(frozen=True) class Anchor: status; start_char: int|None; end_char: int|None; page: int|None
    occurrences: tuple[int, ...]; occurrences_in_text: int
def text_sha256(text: str | None) -> str | None
def verify_anchor(text: str|None, quote: str|None, *, window: tuple[int,int]|None=None, start_hint: int|None=None) -> Anchor
def page_at(text: str, offset: int) -> int | None
def context(text: str, start: int, end: int, radius: int = 300) -> tuple[str, str]
```

Rules:
- A `start_hint` wins only when `text[s:s+len(quote)] == quote`. A mismatching hint gives `unverified` and is never silently re-searched.
- Occurrences come from overlapping `str.find` in a loop, capped at 20.

**Tests (the ticket's fixtures):**
- `test_unique_quote_verified_with_global_offsets`
- `test_repeated_quote_is_ambiguous_and_lists_occurrences`
- `test_start_hint_disambiguates`
- `test_wrong_start_hint_is_unverified_not_researched`
- `test_ocr_noise_quote_not_found_is_unverified` (`"random1zed"` vs `"randomized"`)
- `test_no_text_is_location_unavailable`
- `test_page_from_markers_and_none_without_markers`
- `test_offsets_are_code_points` (`"naïve 🧪 trial"`)
- `test_unique_in_window_repeated_in_text_is_verified_with_count`
- `test_changed_source_changes_text_sha256` (an appended caption block leaves the checksum the same but changes `text_sha256`)

**Run:** `pytest -q backend/tests/unit/services/test_source_anchors.py`. It fails on the import first, then passes.
**Commit:** `feat(research): pure source-anchor verification (GOO-305)`

---

### Task 2: Chunk windows + per-field aggregation (pure)

**Files:**
- Modify `backend/src/services/research/source_anchors.py` by appending the functions below.
- Create `backend/tests/unit/services/test_extraction_coverage.py`.
- Modify `tests/unit/test_extraction_matrix_service.py:28-45` only if GOO-304 left the `None` asserts. They become `missingness == "extraction_error"`, and the tests are kept, not deleted.

```python
CHUNK_CHARS, OVERLAP, MAX_CHUNKS = 12_000, 500, 8
def plan_windows(n: int) -> list[tuple[int, int]]
def merge_ranges(ranges) -> list[tuple[int, int]]
@dataclass(frozen=True) class Candidate: field_id; value; missingness; validation_state; citation; anchor: Anchor|None
def anchor_candidates(parsed_chunk, window, text) -> list[Candidate]   # wraps GOO-304's parsed/coerced output, verifies citations in-window
def aggregate(field_id, candidates, coverage_complete: bool) -> list[Candidate]
```

**Tests:**
- `test_windows_cover_whole_text_under_cap`: the union is `[0, n]`, and each overlap is ≤ `OVERLAP`.
- `test_late_document_evidence_reachable`: 60k chars, with the only matching sentence at 55,000. A fake parse returns it only for the window that contains it. The result is `verified` with `start_char == 55_000`. **This fails against today's 12k prefix.**
- `test_over_cap_spreads_windows_and_reports_partial_coverage`: 200k chars gives 8 windows, the last ending at `n`, with `coverage_complete=False` and the gaps listed.
- `test_partial_coverage_without_value_is_unavailable_text_not_not_reported`
- `test_chunk_parse_error_without_value_elsewhere_is_extraction_error`
- `test_explicit_not_reported_only_when_coverage_complete`
- `test_distinct_values_across_chunks_become_separate_observations`
- `test_same_value_in_overlap_dedups_to_one`

**Run:** `pytest -q backend/tests/unit/services/test_extraction_coverage.py tests/unit/test_extraction_matrix_service.py`
**Commit:** `feat(research): chunked whole-text extraction coverage (GOO-305)`

---

### Task 3: Migration + model columns

**Files:**
- Modify `backend/src/models/extraction_matrix.py`: the GOO-304 classes `ExtractionObservation` and `ExtractionAcceptedValue`.
- Create `backend/alembic/versions/b8d0f2a4c6e9_add_extraction_source_anchors.py`.

`extraction_observations` gains these columns. All are nullable, so the change is additive:

```
anchor_status        VARCHAR(24)  CHECK IN ('verified','ambiguous','unverified','location_unavailable')
anchor_start_char    INT          anchor_end_char INT      anchor_page INT
anchor_occurrences   JSONB        (≤20 ints)               occurrences_in_text INT
text_sha256          VARCHAR(64)  inspected_coverage JSONB  text_length INT
CHECK ((anchor_start_char IS NULL) = (anchor_end_char IS NULL))
CHECK (anchor_start_char IS NULL OR anchor_status IN ('verified','ambiguous'))
CHECK (anchor_status <> 'verified' OR (anchor_start_char IS NOT NULL AND text_sha256 IS NOT NULL))
CHECK ((missingness IS NULL) OR (anchor_status IS NULL))
```

`extraction_accepted_values` gains:

```
anchor_observation_id UUID NULL (FK extraction_observations RESTRICT)
anchor_resolution     VARCHAR(24) NULL CHECK IN ('verified','disambiguated','accepted_unverified','not_applicable')
anchor_start_char INT NULL, anchor_end_char INT NULL
CHECK (anchor_resolution <> 'disambiguated' OR anchor_start_char IS NOT NULL)
```

**No backfill.** Observations and accepted values written by GOO-304 before this revision keep `NULL`, which means "pre-anchor". The API presents them as `unverified` and `legacy` respectively. Legacy cells are marked `legacy_unanchored` in `cell_view` only. Searching old citations for offsets would invent provenance, which the GOO-304 boundary forbids. Every value-bearing write after the migration must set `anchor_status`; the service enforces this and Task 5 tests it. Downgrade drops the columns.

**Verify:**
- `check_alembic.py` reports a single head.
- `cd backend && alembic upgrade head --sql | grep -c anchor_status` renders offline (no Docker).
- `pytest -q backend/tests/unit/architecture`

**Commit:** `feat(research): source-anchor columns on extraction observations (GOO-305)`

---

### Task 4: Ledger v2 payloads

**Files:**
- Modify `backend/src/services/research_decisions/ledger.py`: in GOO-304's `research_extraction` payload map, add `("extraction.observed", 2)`, `("extraction.accepted", 2)` and `("extraction.staled", 2)` with the key sets from Decisions.
- Modify `backend/tests/unit/services/test_research_decision_ledger.py`.

`_validate_extraction_payload` checks:
- the `anchor_status` enum;
- offsets are both null or ints with `0 ≤ start < end`;
- the hashes match `_SHA256_RE`;
- coverage is sorted, non-overlapping int pairs;
- `reason` is in the enum, and `source_changed` ⇒ `document_id` and `new_*_hash` are set.

The quote is on the row, and the event binds it by `citation_sha256`.

Replay additions to `_validate_extraction_transitions`:
- `accepted` v2 with `verified` must name an `anchor_observation_id` that was observed `verified` and is in `observation_ids`.
- `disambiguated` needs an `ambiguous` observation whose recorded occurrences contain `anchor_start_char`.
- `staled` v2 `source_changed` needs each tip's `source_hash` or `text_sha256` to differ from `new_*`.
- Violations raise `DecisionReplayError("accepted anchor contradicts observation")` or `("stale without source change")`.

**Tests:**
- `test_observed_v2_requires_anchor_keys`
- `test_v1_extraction_history_still_replays`
- `test_accepted_verified_on_unverified_observation_fails_replay`
- `test_disambiguated_start_must_be_a_recorded_occurrence`
- `test_source_stale_requires_hash_change`

**Run:** `pytest -q backend/tests/unit/services/test_research_decision_ledger.py`
**Commit:** `feat(research): anchor fields in extraction decision events (GOO-305)`

---

### Task 5: Worker, evidence read, observe/accept guard

**Files:**
- Modify `backend/src/services/research/extraction_matrix_service.py`: GOO-304's `run_background_extraction`, where today's prefix is at `:206`.
- Modify `_build_extraction_prompt` (`:276-310`) to say that `citation` must be verbatim.
- Modify `backend/src/services/research/extraction_forms_service.py` (GOO-304): `append_machine_observations`, `observe`, `accept_value`, `list_observations` and `cell_view`.
- Create `backend/tests/unit/services/test_extraction_anchors_service.py`.

**Worker, per document** (after GOO-304's idempotency skip and source-hash check):
1. Compute `text_sha256` of the text it read.
2. For each `plan_windows(len(text))` window, call the LLM once, run GOO-304's parse/coerce, then `anchor_candidates`.
3. Run `aggregate` per field.
4. Take `lock_active_project`.
5. Append `extraction.staled` v2 for accepted tips of this document pinned to an older hash.
6. `append_machine_observations(...)` writes rows with anchors, coverage and `text_sha256`, all in the one `extraction.observed` v2 event.
7. Commit.

**`observe` (human):** optional `anchor_start` in `ExtractionObservationCreate`. When a `citation` is given, run `verify_anchor` against the current text, scoped by `project_documents_query` (GOO-304 step 5), and store the result. `ambiguous` is **stored**, not rejected. The block happens at accept, so a reviewer can record evidence without choosing an occurrence.

**`list_observations` → evidence:**
- Add the document-visibility check (Decisions).
- Each observation gains `anchor{status, start_char, end_char, page, occurrences, occurrences_in_text}`, `context_before` and `context_after` (±300 chars from the **current** text, only when `source_changed` is false), `inspected_coverage`, `coverage_complete`, `text_length` and `source_changed`.
- Accepted-chain entries gain `anchor_resolution`, `anchor_observation_id` and `source_changed`.
- The document text is read once per call; the call covers one cell.

**`accept_value` step 7 → `assert_anchor_acceptable(db, context, cited, body) -> AnchorResolution | SourceChanged`:** runs in this order:
1. document visibility (404);
2. hash comparison (`SourceChanged` after appending the stale event);
3. anchor-observation choice;
4. `verify_anchor(current_text, citation, start_hint=body.anchor_start or stored start)`;
5. the resolution rules (409s).

On `SourceChanged`, `accept_value` commits the staled event and then raises 409. This is the only commit-then-raise path, and it is documented in the docstring.

**Unit tests:**
- `test_worker_writes_anchor_coverage_text_hash_and_no_confidence`
- `test_worker_rerun_after_text_change_appends_staled_then_observations`
- `test_list_observations_foreign_org_document_404_no_citation`
- `test_list_observations_detached_document_404`
- `test_list_observations_source_changed_hides_context`
- `test_accept_ambiguous_without_start_409`
- `test_accept_ambiguous_with_recorded_start_disambiguated`
- `test_accept_unverified_requires_flag`
- `test_accept_after_source_change_commits_staled_then_409`
- `test_human_override_observation_verified_then_accepted`, where earlier rows stay byte-identical
- `test_legacy_cell_view_is_legacy_unanchored_with_uncalibrated_confidence`

**Gates:** `ruff check backend/src`, plus mypy on the added `source_anchors.py`.
**Commit:** `feat(research): anchored chunked extraction and reconcile guard (GOO-305)`

---

### Task 6: Contracts

**Files:**
- Modify `backend/src/shared/scispace_schemas.py`:
  - GOO-304's `ExtractionObservationResponse` and `ExtractionAcceptedValueResponse` gain the fields listed above. Add a nested `ExtractionAnchor` model.
  - `ExtractionObservationCreate` gains `anchor_start: int ≥ 0 | None`.
  - `ExtractionAcceptCreate` gains `anchor_start: int ≥ 0 | None` and `accept_unverified: bool = False`.
- Create `backend/tests/unit/api/test_extraction_anchor_routes.py`, dependency-overridden like GOO-304's `test_extraction_forms_routes.py`.
- Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

Everything is additive: new optional request fields and new response fields on schemas GOO-304 introduces, so no `api-breaking-approved` label is needed. The `GET /matrices/{id}` dict stays untyped (GOO-304 :249), so `confidence_calibration` and `anchor_status` on legacy cells do not appear in the spec.

**Route tests:**
- 409 details are the stable strings from Decisions, never exception text;
- the `SourceChanged` path commits exactly once;
- on an archived project, `GET /observations` returns 200 and accept returns 409.

Then run `python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types`, then `--check`.

**Commit:** `feat(research): anchor fields in extraction API contracts (GOO-305)`

---

### Task 7: One real PostgreSQL integration test file

**Files:**
- Create `backend/tests/integration/test_extraction_anchor_postgres.py` (`pytestmark = pytest.mark.integration`).

It reuses GOO-304's fixture shape (`screening_factory`-style schema-per-test plus `_upgrade` through `a3c5e7f9b1d4`, then `b8d0f2a4c6e9`), which proves the migration on real Postgres. The seed:
- users: editor E, REVIEWERs R1/R2, ADJUDICATOR J, and org-B user F with document Df;
- D1: `[Page N]` markers, one sentence repeated twice inside one window, an OCR-garbled sentence, and the only sample-size sentence at char 55,000 of 60,000;
- D2: empty `content_text`.

The LLM is stubbed at `ExtractionMatrixService._get_openai_client` (`extraction_matrix_service.py:115-142`).

1. **`test_anchors_and_reconciliations_survive_commit_and_reopen`**
   1. Run the worker. On D1, the sample size is `verified` with its page from the marker, one field is `ambiguous` and one is `unverified`. D2 is `unavailable_text` with coverage `[]`.
   2. R1 and R2 record different human values, each with a citation. `list_observations` returns both.
   3. J's accept of the ambiguous value without a start returns 409; with a start it is `disambiguated`. The unverified value is accepted with `accept_unverified` plus a rationale. An override is R1's human observation, then J's accept of it.
   4. **In a new session**, raw SQL asserts `substring(d.content_text from o.anchor_start_char+1 for o.anchor_end_char-o.anchor_start_char) = o.citation` for every `verified` row, independent of the Python verifier. It also checks `text_sha256 = encode(sha256(convert_to(content_text,'UTF8')),'hex')`, the coverage ranges and the `anchor_resolution` values.
   5. `replay_decisions(..., "research_extraction", matrix_id)` succeeds.
2. **`test_source_change_stales_and_blocks_decision`**
   1. `UPDATE documents SET content_text = content_text || '\nFIGURES…'` for D1 (the checksum is unchanged).
   2. `list_observations` returns `source_changed=true` with no context.
   3. J's accept returns 409. **In a new session**, `extraction.staled` v2 (`source_changed`) names D1's accepted tips, and those `extraction_accepted_values` rows are column-for-column unchanged.
   4. A rerun with a new task id appends new observations pinned to the new `text_sha256`. The old observations remain.
3. **`test_authorization_at_fetch_and_decision`**
   1. F gets 404 on `GET /observations` and on accept.
   2. D1 soft-deleted (`documents.is_deleted`), then restored and detached (`collection_documents.is_deleted`): `GET /observations` returns 404 with no citation in the body, and accept returns 404.
   3. J's ADJUDICATOR role is revoked between the evidence read and the accept, so the accept returns 403 (the role is reloaded after the lock, `project_access.py:261-289`).
   4. A concurrent session holds the Collection `FOR UPDATE` while archiving. The accept blocks, then returns 409 `Archived projects are read-only`; `GET /observations` returns 200.
   5. Two concurrent accepts on a disambiguated anchor with the same `supersedes`: one 201 and one 409 (GOO-304's chain guard still holds with the anchor guard in front of it).

**Mutation verification** (procedure in `docs/engineering/testing.md:42-63`). Record each guard's file:line and command in the test docstring, and in `docs/testing/agent-orchestration-mutation-checks.md` under GOO-305. For each one:

- **Guard: the `ambiguous` branch in `assert_anchor_acceptable`.** Replace it with `if False:`. Test 1 step 3's 409 becomes 201, and `replay_decisions` then raises "accepted anchor contradicts observation".
- **Guard: the hash comparison in `assert_anchor_acceptable`.** Remove it. In test 2 step 3, the accept returns 201 and no `staled` event is written.
- **Guard: the `project_documents_query` check in `list_observations` and the guard.** Replace it with a bare `select(Document).where(Document.id == …)`. Test 3 step 2 returns 200 with the foreign or deleted citation.
- **Guard: the `coverage_complete` condition in `aggregate`.** Force it to `True`. `test_partial_coverage_without_value_is_unavailable_text_not_not_reported` fails with `not_reported`.
- **Guard: the window loop.** Revert to `text[:CHUNK_CHARS]`. `test_late_document_evidence_reachable` fails because no observation is `verified`.

After each check, restore the guard, confirm that `git diff` on the source file is empty, and rerun until green.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_extraction_anchor_postgres.py`. Without a database, report it as **NOT RUN**; the CI Integration job runs it.

**Commit:** `test(research): PostgreSQL proof for source anchors and reconciliation (GOO-305)`

---

### Task 8: Frontend (evidence drawer, disagreement, accept/override)

**Files:**
- Modify `frontend/src/types/api/research-extraction-contract.ts` (GOO-304) to alias the new anchor fields from the generated schemas.
- Modify `frontend/src/components/research/CellCitation.tsx`. Delete `getConfidenceColor`/`getConfidenceLabel` and the confidence block (today `:16-26,54-83`), plus the `confidence` prop. The button opens the drawer, and its label includes the anchor status as text (e.g. `aria-label="Evidence: verified"`).
- Modify `frontend/src/components/research/CellObservations.tsx` (GOO-304's read-only popover) into a `Sheet` drawer (`@/components/ui/sheet`). It keeps GOO-304's query key `['extraction-observations', matrixId, documentId, fieldId]`. This replaces GOO-304's popover rather than adding a second component.
- Modify `frontend/src/components/research/ExtractionMatrix.tsx` (the cell render, today `:503-515`): stop passing `confidence`, and add a "2 values" text badge when the observations for a cell disagree.
- Modify `frontend/src/services/scispaceService.ts`: GOO-304's observe and accept calls take `anchor_start` and `accept_unverified`.
- Add or modify tests: create `__tests__/CellCitation.test.tsx` and extend GOO-304's `__tests__/CellObservations.test.tsx`.

**Drawer sections:**
1. **Evidence** for the selected observation: the citation in `<mark>` between `context_before` and `context_after`; `p. N` or "page unavailable"; `chars start–end of text_length`; and a status text badge. For `ambiguous`, the occurrences are a radio list showing each one's context, plus "appears N× in document".
2. **Coverage:** "Inspected X% of text" with the ranges. When `coverage_complete=false`, a visible "Partial coverage" warning.
3. **Disagreement:** with two or more observations, they are shown side by side in two columns (value or missingness label, kind, actor/model, anchor status, created_at). Each has a "Cite" checkbox that feeds `observation_ids`.
4. **Decide**, rendered only for the ADJUDICATOR role when not read-only:
   - a value picker limited to the cited values or `unresolved_disagreement`;
   - a required rationale `<Textarea>`;
   - the chosen occurrence becomes `anchor_start`;
   - for non-verified anchors, a required checkbox "Accept without a verified source location";
   - `idempotency_key = crypto.randomUUID()`, generated once per click;
   - `supersedes_accepted_value_id` = the chain tip.
   - When `source_changed`, show the banner "Source changed since extraction; re-run extraction" and disable Decide.
   - Errors render in `role="alert"`.
   - On success, invalidate the matrix query and the observations query.
5. **Add evidence**, for the REVIEWER role: a value input, a citation input and an optional occurrence field. This is how an override is made.

**Tests:**
- `CellCitation` shows no `%` and no "Confidence".
- Verified evidence renders the citation, page and offsets.
- Ambiguous blocks Accept until an occurrence is chosen.
- Unverified requires the checkbox.
- Partial coverage shows the warning.
- `source_changed` disables Decide.
- Disagreement renders two columns.
- Rationale is required.
- A reviewer sees Add evidence but not Decide.

**Run:**
- `pnpm --dir frontend exec vitest run src/components/research`
- `pnpm --dir frontend type-check`
- `scripts/ci/run_local_ci.sh --frontend`

**Commit:** `feat(frontend): matrix evidence drawer and anchored reconciliation (GOO-305)`

---

### Task 9: Gates + PR

1. `scripts/ci/run_local_ci.sh --base origin/feat/goo-304-extraction-forms --frontend` must be all green: ruff, black/isort on changed files, mypy on added files, `check_alembic.py`, OpenAPI drift and the unit suites.
2. `pytest -q backend/tests/unit/services/test_source_anchors.py backend/tests/unit/services/test_extraction_coverage.py backend/tests/unit/services/test_extraction_anchors_service.py backend/tests/unit/services/test_extraction_rules.py backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/api/test_extraction_anchor_routes.py backend/tests/unit/services/test_bg_extraction_docid_scope.py backend/tests/unit/api/test_extraction_matrix_docid_scope.py tests/unit/test_extraction_matrix_service.py backend/tests/unit/architecture` must pass. The architecture suite includes GOO-304's acceptance-boundary guard. The offload guards `backend/tests/unit/api/test_audit_pr8_10_guards.py:133` and `test_audit_pr7_agent_runtime_guards.py:131` must stay green.
3. Open the PR against GOO-304's branch. The description includes:
   - the five mutation transcripts;
   - the `oasdiff` changelog;
   - the re-point rule;
   - the PostgreSQL version from the Integration job;
   - the LLM call count per document (≤8, previously 1).

---

## Authenticated two-reviewer journey (Linear closure)

This runs on `rag-dev` after deploy. It is blocked until a backend origin is reachable (`docs/plans/2026-09-29-academic-r0-r1-closure.md`). Accounts:
- `allocs16@gmail.com`: editor + SUPERVISOR;
- R1 and R2: REVIEWER;
- J: ADJUDICATOR;
- one account in a second org.

1. **Migration:** `kubectl -n rag-dev exec deploy/backend -- alembic current` shows `b8d0f2a4c6e9 (head)`.
2. **Late evidence:** upload a PDF of more than 40 pages whose outcome only appears in the discussion, then run the matrix. The drawer shows the late citation, `p. N` or "page unavailable", the offsets, and coverage at 100% or flagged partial. On the pod, `SELECT substring(content_text …)` equals the citation.
3. **Invalid output** (a malformed-column fixture on dev): the cell shows `extraction_error`, never a blank and never "Not reported".
4. **Two reviewers:** R1 and R2 each add a different value with a citation in their own sessions. After a reload, the disagreement view shows both side by side.
5. **Adjudicate (J):**
   - Accepting the ambiguous anchor without an occurrence returns 409; after choosing one and giving a rationale, it is `disambiguated`.
   - Accepting the unverified anchor requires the checkbox.
   - Accepting R1's override observation works.
   - The prior observations are unchanged after reload.
6. **Replay:** send the same accept body and key again with curl. It returns 200 with the same id, and the row count is unchanged.
7. **Source change:** reprocess the document. The drawer shows "Source changed" and Decide is disabled. The next accept returns 409, and `research_decision_events` gains `extraction.staled` (`source_changed`). Re-running extraction appends new anchored observations, and the old ones are retained.
8. **Denials:**
   - the second-org user gets 404 on `GET /observations` and on accept;
   - after the document is removed from the project, evidence returns 404;
   - after J's role is revoked, accept returns 403;
   - on an archived project, evidence returns 200 and accept returns 409.
9. **Browser:** capture screenshots of the drawer (verified, ambiguous, partial coverage, source changed), the disagreement view after reload, and a `CellCitation` with no percentage.
10. **CI:** record the Integration job URL where `test_extraction_anchor_postgres.py` passed at the merge SHA, and paste the mutation transcripts into the PR.

---

**Skipped on purpose:**
- an anchors table (one anchor per observation);
- a new evidence route (GOO-304's `list_observations` carries the evidence);
- `anchor_verified` and `reconciled` events (both are captured in v2 payloads);
- a page→offset map for marker-less PDFs (see the `ponytail` note above);
- hooking every `content_text` writer (staleness is derived on read and persisted on the next write);
- a calibrated confidence (there is no measurement to calibrate against).

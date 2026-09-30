# GOO-303 Full-Text Acquisition + Derived PRISMA Flow Plan (Academic R3)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Record per-report full-text acquisition (request, then attempts ending `requested | retrieved | unavailable`) separately from eligibility, and derive the PRISMA 2020 flow purely from persisted rows: GOO-298/300 records, GOO-299 identity, GOO-301 observations resolved by GOO-302, and the new acquisition rows. Totals are never stored or editable. Missing full text is never an exclusion. A `retrieved` outcome points at an existing, org- and project-scoped `Document` whose content hash is pinned at link time.

**Architecture:** Two new tables and one new ledger family, with no second ledger. `research_fulltext_requests` holds one row per (Collection, report). `research_fulltext_attempts` is an insert-only chain per request, made linear by `previous_attempt_id UNIQUE` plus a partial unique index on the chain head (the same pattern as GOO-301 `supersedes_observation_id`). Each write appends to the existing decision ledger (`backend/src/services/research_decisions/ledger.py`) under a new family, `research_acquisition`, with one stream per Collection. Writers serialize on the existing lock order: `resolve_project` takes Workspace SHARE, then Collection UPDATE, and reloads roles after the lock (`backend/src/services/research_engine/project_access.py:196-205,260-289`). The acquisition stream `FOR UPDATE` comes next. PRISMA has two parts. A loader (`prisma_service.load_inputs`) makes a single read-only pass over raw rows. A pure `derive_prisma_flow(inputs)` does all the counting. Export returns that function's JSON, plus an optional tiny Markdown/Mermaid rendering, served through the same download path as `/runs/{id}/export` (`backend/src/api/research_engine/runs.py:537-561`).

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, a PostgreSQL integration test via `postgres_container`/`RESEARCH_DECISION_DATABASE_URL`, SQLite unit tests, openapi-typescript, Next.js with TanStack Query and Vitest.

**Dependencies:** Stacks on GOO-302, which stacks on GOO-301 (`feat/goo-301-screening-queues`, `593b0083b`), which stacks on GOO-299 #1752. It consumes the following:
- **GOO-299:** `research_reports.merged_into_report_id`, `study_id` and `study_link_status` (`backend/src/models/research_report.py:54-85`), and `research_report_observations` (`:117-138`).
- **GOO-300** (when stacked): accepted `research_import_records` and their receipts' `declared.database`.
- **GOO-301:** `screening_queues`/`screening_observations` and `screening_rules.exclusion_reasons`.
- **GOO-302:** the `screening_resolutions` chain and its `screening.adjudicated|reopened|revealed` events. See "GOO-302 seam".

---

## Decisions (read before Task 1)

| Question | Decision | Why |
|---|---|---|
| New ledger family or extend `research_screening`? | **New family `research_acquisition`**, aggregate = Collection (`aggregate_id = collection_id`, like `research_identity`, `ledger.py:50-51`). `subject_type="research_report"`, `subject_id=report_id`, `requires_subject_version=False`, `subject_hash = decision_request_fingerprint(payload)` (`ledger.py:166-171`). | A `research_screening` stream is keyed per queue, and its replay requires event 1 to be `screening.queue_created` carrying the corpus (GOO-301 plan, Task 3). Acquisition happens *before* any full-text queue exists: the reports sought are exactly the ones that queue will later be built from. Folding it in would force a fake queue or weaken that replay rule. A separate family keeps "acquisition ≠ eligibility" structural. `_validate_event`/`replay_decisions` are already family-generic (`ledger.py:117-171,439-522`). |
| Who requests and records outcomes? | `ResearchAction.EDIT` (workspace owner/admin/editor, `project_access.py:276-280`). `actor_role="editor"`. | Acquisition is clerical search work, not an eligibility decision. The same role is needed to attach the PDF to the project (`backend/src/api/research/projects.py:390-411`), and GOO-300 imports use EDIT too. Reviewers who aren't editors can read the state (VIEW). |
| Request cardinality | `UNIQUE(collection_id, report_id)`. The report must be live (`identity_service._live_reports`, `backend/src/services/research_engine/identity_service.py:156-193`): a foreign report gives 404 and a merged one gives 409. The same key replays (200). A different key for a report that already has a request gets 409 `Full text already requested`. | One request per report means "reports sought" cannot inflate. A retry after `unavailable` is a new *attempt*, not a new request. |
| Attempt chain | Insert-only. The body carries `previous_attempt_id`, which must equal the current head (the row nobody points at via `NOT EXISTS`, or `null` for the first attempt). A mismatch gives 409 `Attempt is stale; reload acquisition state`. The DB also enforces it: `UNIQUE(previous_attempt_id)` plus partial `UNIQUE(request_id) WHERE previous_attempt_id IS NULL`. After a `retrieved` head, any further attempt gets 409 `Full text already retrieved`. | This is the same optimistic-concurrency pattern as GOO-301 observations, so concurrent writers cannot fork the chain. `# ponytail: retrieved is terminal; add a "replace document" attempt if a wrong PDF is ever linked.` |
| Outcome semantics | `requested` means an attempt was made and is awaiting a response (an ILL or author email). `unavailable` requires `reason` (1–2000 chars, free text) and never touches eligibility. `retrieved` requires `document_id`. Every attempt carries `attempted_on: date` (≤ today, as reported by the actor), `actor_id` and `created_at`. | Ticket: "reason, date, actor", and unavailable is not an exclusion. A controlled vocabulary for unavailability is YAGNI: PRISMA reports it as one count. |
| What counts as "retrieved"? | Only a `retrieved` attempt linked to a `Document` that is visible through `project_documents_query(collection_id).where(Document.id == document_id)` (`project_access.py:94-112`), meaning org-scoped, attached to this Collection and not deleted. If not visible: 404 `Document not found`. If `checksum_sha256` is NULL: 422 `Document has no content hash yet`. The attempt pins `document_content_hash = checksum_sha256`. An OA `url`, `evidence_level="full_text"` or provenance `full_text` on a `ResearchSource` is **never** read by this code. | Ticket boundary: reuse the existing upload (`POST /api/v1/documents/files/upload`, `backend/src/api/documents/files.py:141`) plus the existing project attach (`projects.py:390`). There is no crawler. "Version" is the pinned hash, because `document_versions` has **no writer** anywhere in `backend/src` (`grep -rn "DocumentVersion(" backend/src/services backend/src/api` → none), so pinning a `document_versions.id` would pin nothing. Document delete is soft (`backend/src/api/documents/documents.py:647`). The attempt keeps id+hash, and the list shows `document_available=false`. |
| Full-text eligibility gate | GOO-301 `screening_service.submit` on a `full_text` queue (after GOO-302's step 4a `Report resolved; reopen to change`, GOO-302 plan :175) returns 409 `Full text not retrieved` unless the report (or a report merged into it) has a `retrieved` head. The check is one query under the same Collection lock. | This makes "assessed ⊆ retrieved" true by construction, so an unavailable report can never be excluded at full text. It needs only ~10 lines in one function. Exclusion still requires the protocol reason (`screening_rules.validate_observation`) plus GOO-302 resolution. |
| PRISMA inputs | See "Derivation rules". Only **resolved** GOO-302 outcomes feed screening/eligibility counts. Raw observations are never read. | Counting unrevealed observations would let a reviewer infer another reviewer's decision from aggregate deltas, which is GOO-302's blinding boundary. Unresolved reports count as `awaiting`. |
| Stored totals? | None. No table holds counts. `GET .../prisma` recomputes on every call. | Ticket: "no stored/editable totals". `# ponytail: O(records) per call; cache by stream heads if a project exceeds ~50k records (same cap as GOO-300 export).` |
| Export binding | The body carries `versions = {corpus_hash, protocol_version_ids, stream_heads}`. `corpus_hash` = sha256 over sorted `(record_key, final_report_id)`. `stream_heads` = `{f"{aggregate_type}:{aggregate_id}": next_seq-1}` for the identity stream, the acquisition stream, and every screening queue stream (GOO-302's events live in those). The package is `{"schema": "nous.academic.prisma-flow.v1", "generated_at", "body_sha256", "body"}`, with the body hashed by `contracts.canonical_json_bytes` (`backend/src/services/research_engine/contracts.py:27-35`). | Two exports with no writes in between have the same `body_sha256`. Any later event moves a stream head. That makes the export bound to the corpus, protocol and review versions without storing anything. |
| Formats | `format=json` (default) or `md`. The `md` output is a ~30-line renderer: a counts table plus a Mermaid `flowchart TD` of the PRISMA boxes. | "Optional Markdown/Mermaid if tiny". No PDF/SVG. |

### Derivation rules (`derive_prisma_flow`)

`final(r)` follows `merged_into_report_id` to the survivor. Every count below is over rows the loader returns, and nothing else.

| PRISMA box | Rule |
|---|---|
| Records identified, per source | Each `ResearchReportObservation ⋈ ResearchSource` contributes **one record per `metadata_["provenance"][i]`**, keyed by that entry's `connector_type`. In-run DOI merges keep every provider snapshot in `provenance` (`backend/src/services/research_engine/discovery.py:79-81,106`, persisted by `source_persistence.py:22-25`), so records merged inside a run still count once per provider. `rag_store` sources have no observation (GOO-299 decision) and are reported only as `excluded_from_flow.workspace_documents`. |
| Records identified, per import | Each accepted `research_import_records` row counts one record, grouped by `receipt_id` → `declared.database`. This applies only when the GOO-300 table exists; the loader checks the model import. |
| Removed before screening | `duplicates = total_records − |{final(report_id) over records}|`. `import_rejected` = rejected import rows (reported, not subtracted from `total_records`). |
| Records screened / excluded | Over unique final reports: `screened` = count with a resolved `title_abstract` outcome of `include` or `exclude`; `excluded` = count with `exclude`; `awaiting` = unique − screened. |
| Reports sought / not retrieved | `sought` = distinct `final(request.report_id)`. Per final report the state is `retrieved` if any of its requests' heads is `retrieved`; otherwise the head of the latest request (`unavailable`, `requested`, or `pending` when there are no attempts). `not_retrieved` = count of `unavailable`. `awaiting_retrieval` = `requested` + `pending`. |
| Reports assessed / excluded with reasons | `assessed` = count with a resolved `full_text` outcome of `include` or `exclude`. `excluded_by_reason` = `{reason: n}` over `exclude`. |
| Studies included | `included_reports` = `full_text` `include`. `included_studies` = distinct `study_id` over included reports whose `study_link_status == "confirmed"`, plus one per included report without a confirmed link. `unconfirmed_study_links` = included reports with `proposed`/`disputed` (each counted as its own study, which is the conservative choice). |
| Amendments | Every GOO-302 adjudication, reopen or re-resolution event after a report's first resolution, every acquisition head that replaced an `unavailable`, and every `identity.report_merged` whose losers already had an outcome. Each entry is `{event_id, aggregate_type, seq, kind, report_id, from, to}`, ordered by `(aggregate_type, seq)`. Counts always reflect current state, and `amendments[]` shows the path. |
| Outcome collision after a merge | If several reports mapping to one final report carry different outcomes, the survivor's own outcome wins; otherwise the one with the highest `seq` wins. The collision is recorded in `warnings[]`. |
| Reconciliation checks (asserted in the function, returned as `checks{}`) | `screened + awaiting == unique_reports`; `assessed ≤ sought − not_retrieved` (guaranteed by the gate); `included_reports + Σexcluded_by_reason == assessed`. A violated check raises `PrismaInconsistency`, never returns a wrong flow. |

### GOO-302 seam (aligned with `docs/plans/2026-09-29-goo-302-blind-dual-review.md`)

GOO-302 adds no read function for this. It adds the insert-only `screening_resolutions` chain per `(queue, report)` (GOO-302 plan :82-96). The chain has a tip (the row not named by any `supersedes_resolution_id`), `basis in single|agreement|conflict|adjudicated|reopened`, `outcome`, `exclusion_reason` and `event_id`. It also adds the `screening.adjudicated|reopened|revealed` events in the `research_screening` family (plan :118-136). The loader reads the rows directly:

- **Resolved outcome** for `(stage, report)`: the tip, in the latest non-superseded queue of that stage that contains the report (a queue no other queue names in `supersedes_queue_id`), with `basis in {single, agreement, adjudicated}` and `outcome in {include, exclude}`. A tip that is `conflict` or `reopened`, missing, or has an `uncertain` outcome counts as `awaiting`.
- **Amendments:** every resolution row with `supersedes_resolution_id IS NOT NULL`, meaning adjudicated or reopened, with `seq` taken from its `event_id`.
- **Blinding:** a resolution row exists only after reveal (GOO-302 plan :23), so these reads cannot expose an unrevealed observation. The loader never touches `screening_observations`.

The pure function takes plain tuples (`Outcome(stage, report_id, decision, reason, event_id, seq, supersedes: bool)`), so a rename in GOO-302 only touches the loader.

**Migration head:** new revision `f2a4c6e8b0d3` (≤ 32 characters, `scripts/ci/check_alembic.py:40`). Its `down_revision` is GOO-302's `f3b5d7e9a1c4` (GOO-302 plan :34; that file's parent is `e1f3a5c7d9b2`). If GOO-302's revision id changes in review, re-point this file in the same rebase. The rule is the one GOO-301's plan uses (`2026-09-29-goo-301-screening-queues.md:32-36`): always name this PR's direct stack parent, re-point it after every rebase, run `(cd backend && python ../scripts/ci/check_alembic.py)` and expect `single head 'f2a4c6e8b0d3'`. Never add a merge revision here. If two heads appear, the parent PR has not been re-pointed yet, so fix it there.

---

### Task 1: Pure PRISMA module

**Files:**
- Create `backend/src/services/research_engine/prisma.py`.
- Create `backend/tests/unit/services/test_prisma_flow.py`.

```python
SCHEMA = "nous.academic.prisma-flow.v1"
@dataclass(frozen=True) class Record: key: str; origin_kind: str; origin: str; report_id: UUID
@dataclass(frozen=True) class Report: id: UUID; merged_into: UUID|None; study_id: UUID|None; study_status: str|None
@dataclass(frozen=True) class Attempt: request_id: UUID; report_id: UUID; attempt_id: UUID|None; outcome: str|None; seq: int; previous_attempt_id: UUID|None
@dataclass(frozen=True) class PrismaInputs: records, rejected_imports: int, workspace_documents: int, reports, outcomes (Outcome), attempts, merges, versions
class PrismaInconsistency(ValueError)
def derive_prisma_flow(inputs: PrismaInputs) -> dict[str, Any]   # {"counts", "amendments", "warnings", "checks", "versions"}
def package(body: dict) -> dict                                   # schema, generated_at, body_sha256
def render_markdown(body: dict) -> str                            # table + mermaid flowchart
```

**Step 1: write the failing tests.** Each builds `PrismaInputs` by hand.
- `test_in_run_merged_provenance_counts_each_provider_record`
- `test_duplicates_are_records_minus_unique_final_reports` (including a report merged by GOO-299)
- `test_unavailable_is_not_retrieved_and_never_excluded`: the `not_retrieved` count increments, and `excluded_by_reason` and `excluded` do not.
- `test_retry_after_unavailable_counts_retrieved_and_records_amendment`
- `test_two_reports_one_confirmed_study_counts_one_study`, and `test_proposed_link_counts_separately`
- `test_reopened_resolution_uses_current_outcome_and_lists_amendment`
- `test_duplicate_input_rows_raise` (the same `attempt_id`/`event_id` twice raises `PrismaInconsistency`; the loader must never double-read)
- `test_body_hash_stable_and_moves_with_stream_head`
- `test_markdown_has_mermaid_and_every_count`

**Step 2: run them.** `pytest -q backend/tests/unit/services/test_prisma_flow.py` should fail with `ModuleNotFoundError`.
**Step 3: implement** the module with stdlib only and no DB access.
**Step 4: rerun** until green.
**Step 5: commit.** `feat(research): pure PRISMA flow derivation (GOO-303)`

---

### Task 2: Models + migration

**Files:**
- Create `backend/src/models/research_fulltext.py`.
- Modify `backend/src/models/__init__.py` to export it.
- Create `backend/alembic/versions/f2a4c6e8b0d3_create_fulltext_acquisition.py`.
- Modify the drop loops in `backend/tests/integration/test_report_identity_postgres.py:84-110` (the `identity_factory` fixture) and `backend/tests/integration/test_academic_wave_migrations.py` (`_IDENTITY_TABLES`, the same place GOO-301 Task 2 edits). Drop the attempts table, then the requests table, **before** `screening_resolutions` (GOO-302 plan :79), the GOO-301 screening tables and the identity tables. Their FKs to `research_reports` would otherwise block the drop.

The style follows `research_report.py` (plain `Base`, `_collection_fk`/`_created_at`, `research_report.py:31-38`):

```python
class ResearchFulltextRequest(Base):     # research_fulltext_requests
    id, collection_id FK collections RESTRICT
    report_id FK research_reports RESTRICT
    requested_by_id FK users RESTRICT, requested_at (server now())
    protocol_version_id FK research_protocol_versions RESTRICT NULL   # governing version at request time
    UNIQUE(collection_id, report_id)                                  # "sought" cannot inflate
    INDEX(collection_id)

class ResearchFulltextAttempt(Base):     # research_fulltext_attempts — insert-only
    id, request_id FK research_fulltext_requests RESTRICT
    outcome String(16) CHECK IN ('requested','retrieved','unavailable')
    reason Text NULL; attempted_on Date NOT NULL; actor_id FK users RESTRICT; created_at
    document_id FK documents RESTRICT NULL; document_content_hash String(64) NULL
    previous_attempt_id FK research_fulltext_attempts RESTRICT NULL UNIQUE
    CHECK ((outcome = 'retrieved') = (document_id IS NOT NULL))
    CHECK ((document_id IS NULL) = (document_content_hash IS NULL))
    CHECK (outcome <> 'unavailable' OR reason IS NOT NULL)
    UNIQUE INDEX (request_id) WHERE previous_attempt_id IS NULL      # one chain head per request
    INDEX(request_id)
```

Left out on purpose: `collection_id` on attempts (it is reached via the request), a `status` column on requests (it is derived from the head), and `document_version_id` (that table has no writers; see Decisions).

The partial index declares both `postgresql_where=` and `sqlite_where=` (the GOO-301 Task 2 note). The migration creates both tables and calls `_deny_data_api` on each (copied from `backend/alembic/versions/c9d2e4f6a8b1_create_report_identities.py:20-32`). The downgrade drops them in reverse order. No backfill: there is no prior acquisition data to invent.

**Verify:**
- `check_alembic.py` reports a single head.
- `alembic upgrade head --sql` renders both tables offline (no Docker).
- `pytest -q backend/tests/unit/architecture` passes.

**Commit:** `feat(research): full-text acquisition tables (GOO-303)`

---

### Task 3: Ledger family `research_acquisition`

**Files:**
- Modify `backend/src/services/research_decisions/ledger.py`: add the vocabulary next to `_IDENTITY_PAYLOAD_KEYS` (`:50-73`), `_validate_acquisition_payload` and `_validate_acquisition_transitions`, add an entry in `_FAMILIES` (`:620-635`), and update the family list in the module docstring (`:3-5`).
- Modify `backend/tests/unit/services/test_research_decision_ledger.py`.

| event_type (schema 1) | payload keys | actor_role |
|---|---|---|
| `acquisition.requested` | `collection_id, request_id, report_id, protocol_version_id` | editor |
| `acquisition.attempted` (outcome `requested`) | `collection_id, request_id, report_id, attempt_id, previous_attempt_id, attempted_on, reason` | editor |
| `acquisition.unavailable` | same keys as `attempted` | editor |
| `acquisition.retrieved` | `attempted` keys plus `document_id, document_content_hash` | editor |

`_validate_acquisition_payload` checks the following:
- the ids are UUID strings;
- `collection_id == aggregate_id`;
- `report_id == subject_id`;
- `attempted_on` is an ISO date;
- `unavailable` has a non-empty `reason`;
- `document_content_hash` matches `_SHA256_RE` (`ledger.py:29`).

`_validate_acquisition_transitions` is the replay rule:
1. There is at most one `requested` per `report_id`.
2. Every attempt names a known request.
3. `previous_attempt_id` equals that request's current head (`None` first).
4. There are no attempts after `retrieved`.

Violations raise `DecisionReplayError("contradictory acquisition chain")` / `"acquisition after retrieval"`.

**Tests:**
- `test_acquisition_payload_rejects_foreign_collection`
- `test_unavailable_requires_reason`
- `test_acquisition_replay_rejects_forked_chain`
- `test_acquisition_replay_rejects_attempt_after_retrieved`
- `test_acquisition_replay_accepts_request_unavailable_retry_retrieved`

The protocol and identity (and screening) families must still pass unchanged: `pytest -q backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/services/test_research_protocol_service.py backend/tests/unit/services/test_research_identity_service.py`.

**Commit:** `feat(research): acquisition events in the decision ledger (GOO-303)`

---

### Task 4: Acquisition service + PRISMA loader + full-text gate

**Files:**
- Create `backend/src/services/research_engine/acquisition_service.py`.
- Create `backend/src/services/research_engine/prisma_service.py` (the loader only).
- Modify `backend/src/services/research_engine/screening_service.py` (from GOO-301): add the full-text gate in `submit` after the stale checks.
- Modify `backend/src/schemas/research_engine.py`: append the schemas after the GOO-301/302 blocks (currently `IdentityEventResponse` is at `:433-440`).
- Create `backend/tests/unit/services/test_acquisition_service.py` (SQLite, following `backend/tests/unit/services/test_research_identity_service.py`).

The service never commits, following `identities.py:1-6`.

```python
async def request_fulltext(db, context, actor_user_id, data: FulltextRequestCreate) -> FulltextStateResponse
async def record_attempt(db, context, request_id, actor_user_id, data: FulltextAttemptCreate) -> FulltextStateResponse
async def list_fulltext(db, context) -> list[FulltextStateResponse]          # one per request, head + chain
async def retrieved_report_ids(db, collection_id, report_ids) -> set[UUID]    # used by the screening gate
```

Mutation order, identical to `identity_service.merge_reports` (`identity_service.py:529-607`):
1. `stream = lock_aggregate_stream(..., "research_acquisition", collection_id)` (`ledger.py:341-342`).
2. Fingerprint `{operation, actor, request_id?, body}`, then `_replayed_event` (`identity_service.py:72-96`). On a replay, reload the row named in the payload and return it.
3. Validations: `_live_reports`; request lookup `WHERE id=:rid AND collection_id=:cid` (a foreign one gives 404 `Full text request not found`); head check; document visibility and hash.
4. Insert and flush.
5. `append_decision` (`ledger.py:345-436`), with `DecisionIdempotencyConflict` mapped to 409 `Idempotency conflict`.

`protocol_version_id` comes from `identity_service._protocol_version_id` (`:99-112`), or from its public name `current_protocol_version_id` if GOO-300 is stacked.

`prisma_service.load_inputs(db, context) -> PrismaInputs` runs one `SELECT` per input kind, with no locks and every query filtered by `collection_id`:
- observations ⋈ sources (reading `metadata_["provenance"]`);
- import records (if present);
- `research_reports` for the Collection;
- `screening_resolutions` tips/chains joined to `screening_queues` (stage, supersession) and events (for `seq`), as described in the GOO-302 seam;
- requests ⋈ attempts ⋈ the acquisition events (for `seq`);
- `identity.report_merged` events via `replay_decisions(..., "research_identity", collection_id)`;
- stream heads from `research_decision_streams WHERE collection_id=:cid`.

It then calls `prisma.derive_prisma_flow`. It reads inside one `REPEATABLE READ` transaction (`await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})`) so that a concurrent writer cannot produce a torn snapshot. This is a no-op on SQLite.

Request schemas:
- `FulltextRequestCreate{report_id, idempotency_key: str(1..240)}`
- `FulltextAttemptCreate{outcome: Literal["requested","retrieved","unavailable"], attempted_on: date, reason: str(1..2000)|None, document_id: UUID|None, previous_attempt_id: UUID|None, idempotency_key}` with a `model_validator`: retrieved ⇔ `document_id`, and unavailable ⇒ `reason`.

Response schemas:
- `FulltextAttemptResponse{id, outcome, reason, attempted_on, actor_id, document_id, document_content_hash, document_available: bool, previous_attempt_id, created_at}`
- `FulltextStateResponse{request_id, report_id, protocol_version_id, requested_by_id, requested_at, state: Literal["pending","requested","retrieved","unavailable"], head_attempt_id, attempts: list[...]}`
- `PrismaFlowResponse{schema_, generated_at, body_sha256, body: PrismaFlowBody}`, where `PrismaFlowBody` types `counts` (with `records_by_source: dict[str,int]`, `excluded_by_reason: dict[str,int]`), `amendments`, `warnings`, `checks` and `versions`. Typed rather than a `dict[str, Any]` passthrough, so the frontend aliases real fields (`docs/engineering/api-contracts.md`, Adopt-on-touch).

**Unit tests (SQLite):**
- `test_request_is_idempotent_and_unique_per_report`: same key → same id; a new key → 409; one row.
- `test_unavailable_does_not_create_observation_or_exclusion`: zero `screening_observations` rows, and the report stays eligible for full-text screening after a later `retrieved`.
- `test_retrieved_requires_project_scoped_document`: another org's doc gives 404; a doc in the org but not attached gives 404; a NULL checksum gives 422; the hash is pinned.
- `test_stale_previous_attempt_is_409`
- `test_full_text_submit_requires_retrieved` (gate): unavailable or pending → 409 `Full text not retrieved`; after retrieved → 200.
- `test_metadata_url_and_evidence_level_never_imply_retrieved`: a source whose metadata has `evidence_level="full_text"` and an OA url gives state `pending` and PRISMA `sought=0`.

**Gates:** `ruff check backend/src`, plus mypy on the four added files (`docs/engineering/backend.md` Ratchets).

**Commit:** `feat(research): full-text acquisition service and PRISMA loader (GOO-303)`

---

### Task 5: Routes + OpenAPI/TypeScript

**Files:**
- Create `backend/src/api/research_engine/acquisition.py` (`APIRouter(prefix="/research-engine", tags=["research-engine-acquisition"])`, transport only).
- Modify `backend/src/api/research_engine/__init__.py:5,19` and `backend/src/main.py:83,671-672` to register it with `prefix="/api/v1"`, next to identities.
- Create `backend/tests/unit/api/test_research_acquisition_routes.py`, following `test_research_identity_routes.py`.
- Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

| Method + path | `resolve_project` action | Service | Commit |
|---|---|---|---|
| `POST /projects/{project_id}/fulltext/requests` | EDIT | `request_fulltext` (201, or 200 on replay) | yes |
| `POST /projects/{project_id}/fulltext/requests/{request_id}/attempts` | EDIT | `record_attempt` | yes |
| `GET /projects/{project_id}/fulltext` | VIEW | `list_fulltext` | no |
| `GET /projects/{project_id}/prisma` | VIEW | `load_inputs` → derive → package | no |
| `GET /projects/{project_id}/prisma/export?format=json\|md` | VIEW | same, as `Response(content, media_type, headers={"Content-Disposition": 'attachment; filename="prisma-flow-{project_id}-{body_sha256[:12]}.{ext}"'})`, as in `runs.py:557-561` | no |

`format` is a `Literal["json","md"]` query param, a validated enum per the gotchas "never raw strings in sort/filter" rule. `PrismaInconsistency` maps to 500 with the stable detail `PRISMA flow inconsistent` and a logged event id, never the exception text. Archived projects: reads return 200, and writes return 409 through `resolve_project` (`project_access.py:232-235`).

**Route tests:**
- action mapping per route;
- each mutation commits exactly once;
- reads never commit;
- the export sets an `attachment` Content-Disposition and its body equals the `GET /prisma` body;
- a foreign request id gets a stable 404.

Then run `python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types`, then `--check`. The changes are additive only, so no `api-breaking-approved` label is needed.

**Commit:** `feat(research): full-text acquisition and PRISMA API (GOO-303)`

---

### Task 6: One real PostgreSQL integration test file

**Files:**
- Create `backend/tests/integration/test_acquisition_prisma_postgres.py` (marker `integration`).

The fixture `prisma_factory` copies `identity_factory` (`test_report_identity_postgres.py:84-110`):
1. Run `create_all`.
2. Drop acquisition → `screening_resolutions` → GOO-301 screening → identity tables.
3. Run each upgrade in chain order: `c9d2e4f6a8b1`, `d4e6f8a0b2c3` if present, `e1f3a5c7d9b2`, `f3b5d7e9a1c4`, then `f2a4c6e8b0d3`.

This proves the migration on real Postgres.

The seed reuses `_seed` (`test_report_identity_postgres.py:113-185`) plus GOO-301's screening seed (supervisor O, reviewers R and R2, adjudicator A, foreign F, and a protocol with `full_text_exclusion_reasons=["wrong population","wrong design"]`, in dual mode). `_wait_until_blocked` is copied from `:463-472`.

Known corpus:
- Run 1 has openalex+pubmed returning the same DOI (one `ResearchSource` with 2 provenance entries). Run 2 finds that DOI again. There are 5 more distinct DOIs.
- GOO-299 merges one pair of reports.
- r4 and r5 get a `confirmed` link to study S.
- With GOO-300 stacked: one import receipt with 1 duplicate and 1 rejected record.

1. **`test_flow_recomputes_from_raw_rows_after_commit_and_reopen`**
   1. Title/abstract: dual review on all reports. One conflict is adjudicated by A. One resolved report is **reopened** and re-resolved (GOO-302), which gives the expected amendment.
   2. Acquisition:
      - r2: `unavailable` (reason "not held by library").
      - r3: `unavailable`, then `retrieved` on a document uploaded and attached through `projects.py:390`.
      - r4, r5: `retrieved`.
      - r6: requested, with no attempts.
   3. Full text: r3 excluded ("wrong design"), r4 and r5 included. The gate proves that r2 gets 409 `Full text not retrieved`.
   4. **Replays:** resubmit every request, attempt and observation with the same keys. Everything returns 200, and the row and event counts are unchanged.
   5. In a **new session**, call `GET /prisma` via the service and route function.
   6. The test computes every count **independently** with raw SQL written in the test, without importing `prisma.py`: `jsonb_array_length(metadata->'provenance')` grouped by provider, `count(DISTINCT final report)` through a recursive CTE on `merged_into_report_id`, resolved tips read from `screening_resolutions` with their own `NOT EXISTS` tip query, request/attempt heads via `NOT EXISTS`, and `DISTINCT study_id` for confirmed links. Every exported count must equal its raw-SQL value, `amendments` must contain the reopen and the r3 unavailable→retrieved event, and `checks` must all be true.
   7. `replay_decisions` over the identity, acquisition and every screening stream succeeds.
   8. The export's `body_sha256` is identical across two calls, and it changes after one more attempt.
2. **`test_concurrent_acquisition_and_eligibility_stay_reconstructable`**
   1. Two sessions record attempts on the same request with the same `previous_attempt_id` and **different** keys. Session 2 blocks on the Collection lock, which `_wait_until_blocked` proves. The result is one 200, one 409 `Attempt is stale…`, and exactly one head.
   2. The same race with the **same** key and body: both return the same attempt id, with one row and one event.
   3. Session A records `unavailable` on r7 while session B submits a full-text `exclude` on r7. Whatever order the lock serializes them in, the outcome is `unavailable` with B at 409, and r7 never appears in `excluded_by_reason`.
   4. A direct `INSERT` of a second chain head raises `IntegrityError` naming the partial unique index.
   5. The flow is recomputed and compared to raw SQL as in test 1.
3. **`test_tenancy_and_lifecycle`**
   1. F gets 404 `Project not found` on the GET prisma, fulltext and attempts routes.
   2. A request id from another Collection gets 404.
   3. A `document_id` from F's org gets 404 `Document not found`.
   4. An archiver session holds `collections.research_status='archived'` `FOR UPDATE`. The request call blocks, then returns 409 `Archived projects are read-only` (`project_access.py:232-235`). `GET /prisma` on the archived project returns 200 with unchanged counts.
   5. A role-less workspace viewer gets 404 on POST, from EDIT at `project_access.py:276-280`.

**Mutation verification** (procedure in `docs/engineering/testing.md`; record the guard file:line and the command in the test docstring):
- **Guard: the head check in `record_attempt`** (`previous_attempt_id == head`). Comment it out. `-k concurrent` should then fail with `IntegrityError` on `uq_research_fulltext_attempt_head` instead of the expected 409. Restore it, confirm `git diff --exit-code backend/src/services/research_engine/acquisition_service.py`, and rerun until green.
- **Guard: the idempotency replay in `record_attempt`/`request_fulltext`.** Comment it out. The replay step in test 1 should then fail with 409 `Attempt is stale…` or `Full text already requested`, where the test expects the replayed id and unchanged counts.
- **Guard: the full-text retrieved gate in `screening_service.submit`.** Comment it out. Test 2 step 3 should then fail with r7 counted in `excluded_by_reason`.
- **Guard: the duplicate-row check in `derive_prisma_flow`.** Make the loader join attempts without `DISTINCT`/head filtering. `test_duplicate_input_rows_raise` and test 1 should then fail with inflated `sought`.

**Run:** `pytest -q backend/tests/integration/test_acquisition_prisma_postgres.py`. This needs testcontainers or `RESEARCH_DECISION_DATABASE_URL`. Report it as `NOT RUN` locally if neither is available; the CI Integration job runs it.

**Commit:** `test(research): PostgreSQL proof for acquisition and derived PRISMA flow (GOO-303)`

---

### Task 7: Regression preservation

Leave the following untouched and keep them green:
- `backend/tests/unit/services/test_paper_discovery.py:148-203` (merged provider evidence, `evidence_level == "abstract"` at `:189`);
- `:422-434` (`rag_store` snippets are `workspace_document`, never `full_text`);
- `:530` (OpenAlex `full_text is None`).

Neither `discovery.py` nor `contracts.py:18-24` (`EVIDENCE_LEVELS`/legacy `excerpt → workspace_document`) is edited. The new unit test `test_metadata_url_and_evidence_level_never_imply_retrieved` (Task 4) is the persisted-workflow counterpart.

**Run:** `pytest -q backend/tests/unit/services/test_paper_discovery.py`.

---

### Task 8: Frontend (status column + PRISMA card)

**Files:**
- Create `frontend/src/types/api/research-acquisition-contract.ts`, aliasing `components['schemas']['FulltextStateResponse' | 'FulltextAttemptCreate' | 'PrismaFlowResponse' …]`, as `research-identity-contract.ts` does.
- Modify `frontend/src/services/researchEngineService.ts`: add `listFulltext`, `requestFulltext`, `recordFulltextAttempt`, `getPrismaFlow` and `downloadPrismaFlow(projectId, format)` after the screening block. The download uses `api.download` like `downloadRunExport` (`:180-186`).
- Modify `frontend/src/components/research-engine/ScreeningQueuePanel.tsx` (from GOO-301): add a **Full text** column.
- Create `frontend/src/components/research-engine/PrismaFlowCard.tsx`.
- Modify `frontend/src/components/research-engine/ProjectWorkflow.tsx`: render `<PrismaFlowCard projectId={project.id} />` after the screening panel. `ReportIdentityPanel` is at `:149`.
- Add or modify tests: create `__tests__/PrismaFlowCard.test.tsx`, extend `__tests__/ScreeningQueuePanel.test.tsx`, and add the mock to `__tests__/ProjectWorkflow.test.tsx`.

**Full text column** (`useQuery(['fulltext', projectId])`): a status badge showing `pending | requested | retrieved | unavailable`, where unavailable shows its reason as a tooltip. When `!readOnly`, it shows these actions:
- a **Request** button;
- a **Mark unavailable** action with a required reason `<textarea>` and `<input type="date">`;
- a **Mark retrieved** action with a `<select>` of project documents from the existing `projectService.listProjectDocuments` (`frontend/src/services/projectService.ts:239-256`).

Every action sends `previous_attempt_id = head_attempt_id` and `idempotency_key = crypto.randomUUID()`, generated once per click. On a full-text queue, the Exclude/Include buttons are disabled with the text "Full text not retrieved" unless the status is `retrieved`. This mirrors the server gate; the server stays authoritative. Errors are shown with `role="alert"`.

**PrismaFlowCard** (`useQuery(['prisma', projectId])`): one `<section>` with a `<dl>` of the PRISMA boxes (identified per source/import, duplicates, screened/excluded, sought/not retrieved, assessed/excluded by reason, included reports/studies), an amendments count, the truncated `body_sha256`, and **Download JSON** / **Download Markdown** buttons. It has no editable field. All acquisition and screening mutations invalidate `['prisma', projectId]` and `['fulltext', projectId]`.

**Tests:**
- the status badge renders per state;
- unavailable requires a reason;
- the retrieved select lists only project documents;
- the full-text exclude is disabled until retrieved;
- the card renders the mocked counts and download calls `downloadPrismaFlow('p1','md')`;
- the card has no inputs.

**Run:**
- `pnpm --dir frontend exec vitest run src/components/research-engine/__tests__/PrismaFlowCard.test.tsx src/components/research-engine/__tests__/ScreeningQueuePanel.test.tsx src/components/research-engine/__tests__/ProjectWorkflow.test.tsx`
- `pnpm --dir frontend type-check`
- `scripts/ci/run_local_ci.sh --frontend`

**Commit:** `feat(frontend): full-text status and PRISMA flow card (GOO-303)`

---

### Task 9: Gates + PR

1. `scripts/ci/run_local_ci.sh --base origin/<GOO-302 branch> --frontend` must be all green: ruff, black/isort on changed files, mypy on added files, `check_alembic.py`, OpenAPI drift and the unit suites.
2. `pytest -q backend/tests/unit/services/test_prisma_flow.py backend/tests/unit/services/test_acquisition_service.py backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/api/test_research_acquisition_routes.py backend/tests/unit/services/test_paper_discovery.py backend/tests/unit/architecture` must pass.
3. Open the PR against GOO-302's branch and retarget it as the stack merges. Its description includes:
   - the mutation-verification transcript for all four guards;
   - the `oasdiff` changelog (added paths/schemas only);
   - the migration re-point rule;
   - the PostgreSQL version from the Integration job.

---

## Authenticated journey list for Linear closure

These steps run against `rag-dev` after deploy. They are blocked until a backend origin is reachable (the hard blocker in `docs/plans/2026-09-29-academic-r0-r1-closure.md`). Log in as `allocs16@gmail.com` (workspace editor and SUPERVISOR) on a project that already has GOO-301/302 title/abstract resolutions and an approved protocol with `full_text_exclusion_reasons`. A second account holds REVIEWER and a third holds ADJUDICATOR.

1. **Migration:** `kubectl -n rag-dev exec deploy/backend -- alembic current` shows `f2a4c6e8b0d3 (head)`.
2. **Request:** in the Workflow tab, click Request on 3 included reports. Each returns 201, and the column reads `pending`. Replaying one request (same key, via curl) returns 200 with the same `request_id`. A new key returns 409 `Full text already requested`.
3. **Unavailable:** mark report A `unavailable` with a reason and date. The column shows the reason. On a full-text queue, Exclude on A is disabled, and a curl `POST .../observations` for A returns 409 `Full text not retrieved`.
4. **Retrieved:** upload a PDF (`/api/v1/documents/files/upload`), attach it to the project, and mark report B `retrieved` with it. The response pins `document_content_hash`, which must equal `SELECT checksum_sha256 FROM documents WHERE id=…`. A document id from another org returns 404. A stale `previous_attempt_id` returns 409.
5. **Retry:** mark A `retrieved` after a new upload. `GET .../fulltext` shows the chain `unavailable → retrieved`.
6. **Screen:** both reviewers screen B at full text (exclude "wrong design" vs include). The adjudicator resolves it as exclude, then reopens and re-resolves (GOO-302).
7. **PRISMA:** the card shows the counts. Download JSON and Markdown. For every count in the JSON, run the matching raw SQL from Task 6 on the dev DB (read-only, from the pod) and confirm they are equal. `amendments` contains the reopen and A's unavailable→retrieved. `body_sha256` is stable across two downloads and changes after one more attempt.
8. **Replay:** resubmit step 4's body with the same key. The response is 200, and the PRISMA JSON `body_sha256` is unchanged.
9. **Denials:** a user from another org gets 404 on `/prisma`. A role-less workspace viewer gets 404 on POST request. On an archived project, POST returns 409 and `GET /prisma` returns 200.
10. **Browser:** capture a screenshot of the Full text column (all four states) and the PRISMA card after a reload.
11. **CI:** record the Integration job URL where `test_acquisition_prisma_postgres.py` passed at the merge SHA, and paste the mutation transcript into the PR.

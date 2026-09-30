# GOO-304 Versioned Extraction Forms + Independent Observations Plan (Academic R4)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Put immutable, typed form versions and append-only observations under the existing extraction matrix without breaking the current matrix UI or storage. A column edit creates a new form version. A machine rerun appends observations. A human with `ADJUDICATE` records an accepted value that cites exact observation ids and an exact form version. Automation can never accept anything. Legacy columns and cells become an explicit `legacy_unversioned` form version that is attributed to nobody. Legacy cells stay readable and are never promoted.

**Architecture:** Three new tables, no new aggregate table and no second ledger.
- **`extraction_form_versions`**: immutable field lists for one matrix. The matrix row stays the form identity.
- **`extraction_observations`**: insert-only.
- **`extraction_accepted_values`**: an insert-only chain per `(document, field)`, the same tip/`supersedes` pattern as GOO-301 observations and the GOO-302 resolutions.

`extraction_cells` is frozen: nothing writes to it after the migration, and it stays as the last fallback on the read path. Every write goes to the existing decision ledger (`backend/src/services/research_decisions/ledger.py`) under a new family, `research_extraction`, with one stream per matrix. Writers keep the existing lock order: `resolve_project` takes Workspace SHARE, then Collection UPDATE, and reloads roles after the lock (`backend/src/services/research_engine/project_access.py:196-205,262-289`). The matrix stream `FOR UPDATE` comes next (`ledger.py:405-442`). The worker keeps its existing order, `lock_active_project` (Collection UPDATE, `project_access.py:160-177`) and then the stream, which is the same order minus the Workspace SHARE it never holds. All validation (types, missingness, precedence, staleness) is in one pure module, `extraction_rules.py`.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, PostgreSQL integration test (`postgres_container`/`RESEARCH_DECISION_DATABASE_URL`), SQLite-free pure unit tests, openapi-typescript, Next.js with TanStack Query and Vitest.

**Dependencies:** This branch (`feat/goo-304-extraction-forms` @ `a51622bc2`) holds GOO-299/300/301 code. GOO-302 (`f3b5d7e9a1c4`) and GOO-303 (`f2a4c6e8b0d3`) are plans only (`docs/plans/2026-09-29-goo-30{2,3}-*.md`). This ticket shares no tables with them. The only coupling is the Alembic chain and `ledger.py`'s `_FAMILIES` dict (`ledger.py:788-810`), so a rebase conflict there is additive. GOO-305 (anchors and reconciliation UX) consumes `extraction_observations.id`, `citation` and the accept endpoint, all defined here.

---

## Decisions (read before Task 1)

| Question | Decision | Why |
|---|---|---|
| `extraction_forms` table? | **No.** `extraction_matrices` *is* the form identity (it has `project_id`, `name`, soft-delete and lifecycle; `backend/src/models/extraction_matrix.py:18-38`). Versions hang off `matrix_id`. | A forms table would be a 1:1 copy of the matrix. `# ponytail: add extraction_forms when one form must be shared by several matrices.` |
| Version identity | `UNIQUE(matrix_id, version_no)`. "Current" is `max(version_no)`, which is derived, not a pointer. `content_hash = sha256(canonical_json({provenance, protocol_version_id, fields}))` uses `contracts.canonical_json_bytes` (`backend/src/services/research_engine/contracts.py:27-35`). A PATCH whose fields hash to the current version's hash creates nothing, so it is idempotent. | With no pointer column there is no pointer drift. Writers are serialized by the Collection lock and the stream lock, and the unique constraint is the backstop. |
| Stable field ids | `field_id = uuid5(FIELD_NAMESPACE, f"{matrix_id}:{name}")`, a constant in `extraction_rules.py` that the migration copies (frozen) into its own file. The same name gets the same id in every version and every row, and ids never collide across matrices. | This needs no lookup table and no request change. A rename is a new field, and old values stay readable under the old version. `# ponytail: accept an explicit field_id on ExtractionColumn when rename-with-history is needed.` |
| Field types | `text \| number \| boolean \| categorical`, plus optional `unit` (≤50), `timepoint` (≤100) and `categories` (required iff `categorical`, 1–50 strings). These are added to `ExtractionColumn` (`backend/src/shared/scispace_schemas.py:17-32`) as optional fields with defaults (`type="text"`), so the request change is additive. | Exactly the ticket's list. Date and range types are YAGNI until a protocol asks for them. |
| Governing protocol version | `protocol_version_id` FK is **nullable**. At version creation it pins the Collection's approved protocol `current_approved_version_id` (`backend/src/models/research_protocol.py:85`) when one exists, and otherwise `NULL`. | Most matrices live in projects that have no protocol. A `NOT NULL` column would break today's `POST /matrices`. Showing a form as stale when the protocol changes is GOO-305 UX. |
| Legacy migration | Every matrix row, soft-deleted ones included, gets one version with `version_no=1` and `provenance='legacy_unversioned'`. It has `created_by_id NULL`, `protocol_version_id NULL`, `created_at = matrix.created_at`, and `fields` = `matrix.columns` in order, then the sorted distinct `extraction_cells.column_name` values missing from `columns` (orphans of past edits), each with `type='text'`. `CHECK ((provenance='legacy_unversioned') = (created_by_id IS NULL))` and `CHECK (provenance <> 'legacy_unversioned' OR version_no = 1)`. Cells are **not** copied into observations. | "Don't invent provenance": copying cells into observations would invent an extractor and a source hash. A cell stays traceable because `(matrix_id, column_name)` → `uuid5` → the legacy field. The orphan union keeps every existing cell addressable. |
| `extraction_cells` after migration | Frozen. The worker upsert (`backend/src/services/research/extraction_matrix_service.py:221-249`) is deleted. `clear_stale_cells` (`backend/src/api/research/extraction_matrix.py:311-321`) stops soft-deleting and only computes `stale_document_ids` (documents with values on removed fields). The Field description says it is deprecated. | "Prior values stay readable." Deleting cells would lose history. Keeping the flag means the request contract does not break. |
| `extraction_matrices.columns` | It stays, and the one version writer mirrors it (`[{name, description}]` of the current version) in the same transaction. Every read goes through the current version. | Downgrade stays safe, and any unseen raw reader keeps working (for example `backend/tests/integration/test_research_project_mapping.py:430-434`). `# ponytail: drop the mirror in the release after downgrade support ends.` |
| Observation kinds | `kind ∈ {machine, human}`. `actor_user_id NOT NULL`: for `machine` it is the **initiating** user carried in the task payload, and for `human` it is the author. `extractor_run_id` (Celery task id) and `extractor_model` are required iff `machine` (`CHECK`). | Each row stays attributable to a person, which the ledger needs anyway (`backend/src/models/research_decision.py:67-69`). |
| Typed value vs missingness | One `value JSONB` (string, number, bool, or a category string) **xor** one `missingness`, enforced by `CHECK ((value IS NULL) <> (missingness IS NULL))`. Taxonomy: `not_reported \| not_applicable \| unavailable_text \| extraction_error \| unresolved_disagreement`. Allowed per writer: machine can write `not_reported, not_applicable, unavailable_text, extraction_error`. Human can write `not_reported, not_applicable, unavailable_text`. Accepted can use any except `extraction_error`. | One JSONB column replaces four typed nullable columns. The per-writer sets live in `extraction_rules.MISSINGNESS` and are unit-tested. Only an adjudicator can declare `unresolved_disagreement`. |
| Validation state | `validation_state ∈ {valid, invalid}`. `extraction_rules.coerce(field, raw)` does the conversion. A machine value that fails coercion (e.g. `"12 participants"` for `number`) is **kept** as its raw JSON string with `invalid`. A human value that fails gets 422. | Machine output is evidence and is never dropped silently. An invalid observation can't be the value source for an acceptance. |
| Source version | `source_hash String(64) NOT NULL` = `document_source_hash(doc)` = `checksum_sha256` (`backend/src/models/document.py:66-68`), or `sha256(content_text or "")` when that is NULL. It is pinned at enqueue and re-checked by the worker. On mismatch the document is skipped (`skipped += 1`) and nothing is written. | `document_versions` has no writer (GOO-303 plan, "What counts as retrieved"), so a hash is the only real version. The fallback covers documents that have text but no checksum (only uploads and arXiv set it: `file_service.py:567`, `arxiv_change_tracker.py:763`). A later change from NULL to a checksum looks stale, which is the conservative outcome. |
| GOO-299 report link | Not added. Observations key on `document_id`, like the matrix grid. | The report is reachable through GOO-303's `retrieved` attempt (document → report). `# ponytail: add report_id when extraction runs on reports without a Document.` |
| Who observes (human) | `ResearchAction.REVIEW` (the REVIEWER role, `project_access.py:37-41,287-289`), `actor_role='reviewer'`. | GOO-301 needs assignments because a queue has a frozen corpus and blinding. Extraction has neither in this ticket, so the role alone is enough. `# ponytail: reuse GOO-301 assignment rows if per-document workload or blinding is required.` |
| Who accepts | `ResearchAction.ADJUDICATE` only (403 `adjudicator role required`), `actor_role='adjudicator'`. Owners and editors hold no implicit power. | The ticket names ADJUDICATE. A dedicated extraction-reviewer assignment would be a table with nothing to enforce yet. |
| What an acceptance may say | It cites 1–20 observations with the same `document_id`, `field_id` and `form_version_id`, all with `source_hash == current source hash` (otherwise 409 `Source changed; re-extract`), and that form version must be current (otherwise 409 `Form version is stale`). The accepted value/missingness must **equal one cited valid observation's**. The one exception is `unresolved_disagreement`, which needs ≥2 cited observations whose values differ. The request also needs a rationale (1–2000 chars) and `supersedes_accepted_value_id` equal to the current tip (otherwise 409 `Accepted value is stale; reload`). | An adjudicator reconciles evidence and does not author values. To enter a new value they record a human observation first, which also needs REVIEWER. The tip check plus the DB partial unique index stops two adjudicators from forking the chain. |
| Automation cannot accept | The accept path is one function, `accept_value(db, context: ProjectContext, ...)`, and it requires `ADJUDICATOR in context.effective_roles`. The worker has no `ProjectContext`. An AST guard (Task 6) fails if `extraction_matrix_service.py`, `src/tasks/` or `src/services/agent/` references `accept_value` or `ExtractionAcceptedValue`. Ledger replay rejects `extraction.accepted` unless `actor_role == 'adjudicator'`. | The rule is enforced three ways, with no runtime flag. |
| Staleness | This is **derived** at read time, never written. An accepted value is stale iff (a) its field's definition (`type, unit, timepoint, categories`) in the current version differs from the definition in its own version, or the field was removed, or (b) its `source_hash` differs from the document's current source hash. A form amendment appends one `extraction.staled` event listing the accepted tips made stale by (a). Nothing is rewritten. | "Mark stale, not rewritten." The check is per field, so adding a column does not stale every verified value. `# ponytail: (b) is detected at read only; emit staled from the document reprocess path once document content revisions exist.` |
| Compatibility read (`GET /matrices/{id}`) | For each allowed document and current field, `extraction_rules.pick_cell` picks the first match in this order: the accepted tip (non-stale preferred), the latest machine observation of that `field_id` in any version, then the legacy cell by `column_name`. The old keys (`document_id, column_name, value, citation_snippet, confidence`) stay. New keys: `field_id, form_version_id, source ∈ {accepted, machine, legacy}, missingness, validation_state, stale`. `value` is the display string (`12`, `true`, category) or `null` when there is missingness. `confidence` is the stored value for legacy cells and `null` otherwise. | This follows the ticket's precedence. Following a field across versions means one column edit no longer blanks the grid, which is today's behavior (`extraction_matrix.py:304-321` keeps cells of unchanged columns). The fabricated constant `0.8` (`extraction_matrix_service.py:237,246`) is not carried forward. |
| Matrix with no version row | Read works: `form_version: null`, columns come from `matrix.columns`, and cells come from legacy only. Writes get 409 `Matrix has no form version`. | Only raw-SQL fixtures can produce such a row once the migration and every create path write v1. |
| Worker idempotency | Per document, the idempotency key is `f"{task_id}:{document_id}"`. Before calling the LLM, the worker looks the key up in the matrix stream. If it is already there, it counts the document as `completed` and skips it. | Celery `autoretry_for=(Exception,)` (`backend/src/tasks/research_tasks.py:199-205`) replays the whole task after per-document commits. Without this skip a retry double-appends and re-pays the LLM cost. A user-triggered rerun has a new task id and appends, as the ticket requires. |
| Missing new kwargs (in-flight tasks at deploy) | If `form_version_id` or `initiated_by_user_id` is absent, the task status becomes `failed` with `error="Extraction task predates form versions; re-run"`, and nothing is written. | Filling in an actor would invent provenance. |
| Machine missingness mapping | The prompt asks for `{"value", "missing": "not_reported"\|"not_applicable"\|null, "citation"}`. A key that is missing or malformed, or a whole-document parse failure (`extraction_matrix_service.py:338-355`), gives `extraction_error`. Empty `content_text` on a document in the project gives `unavailable_text` for every field (today it is only skipped, `:196-204`). An LLM transport exception writes no rows, as today (`:254-263`). | Every outcome is recorded with its reason. A `null` value is never recorded as a silent blank. |
| Blinding | None. `GET observations` is VIEW. | The ticket asks that observations be *retained*, not blinded. If blinding is required, reuse GOO-302's single reveal predicate. |
| Transport | The existing routes still commit in the router today (`extraction_matrix.py:141,323,506`). The new writes go through service functions that each commit exactly once. `create_matrix` and `update_matrix` start calling `extraction_forms_service.create_version` in place of their inline writes. | This follows the house rule (`docs/engineering/backend.md` "Router → service → transaction") for every line this ticket touches. |

**Migration head:** new revision `a3c5e7f9b1d4_version_extraction_forms.py` (12 characters, within the 32 allowed by `scripts/ci/check_alembic.py:40`), with `down_revision = "f2a4c6e8b0d3"` (GOO-303 plan :62). The chain is 301 `e1f3a5c7d9b2` → 302 `f3b5d7e9a1c4` → 303 `f2a4c6e8b0d3` → 304 `a3c5e7f9b1d4`. **Re-pointing:** `down_revision` always names this PR's *direct stack parent*. If GOO-302 or GOO-303 land after this ticket, or their ids change in review, set `down_revision` to whatever `(cd backend && python ../scripts/ci/check_alembic.py)` reports as the single head of the rebased parent (today on this branch that is `e1f3a5c7d9b2`), in the same rebase. Two heads with `a3c5e7f9b1d4` as one of them means a parent has not been re-pointed, so fix it there. Never add a merge revision. **Collision note:** the GOO-305 plan (`2026-09-30-goo-305-source-anchors.md:53,130`) also picked `b8d0f2a4c6e9` for its own revision. That id belongs to GOO-305, and GOO-305's `down_revision` must be `a3c5e7f9b1d4`. The migration imports nothing from `src`: it copies `_deny_data_api` (`backend/alembic/versions/e1f3a5c7d9b2_create_screening_queues.py:20-30`), `FIELD_NAMESPACE`, and the canonical-JSON hash.

---

### Task 1: Pure rules (`extraction_rules.py`)

**Files:**
- Create `backend/src/services/research/extraction_rules.py`.
- Create `backend/tests/unit/services/test_extraction_rules.py`.

```python
FIELD_NAMESPACE: UUID                      # frozen; copied into the migration
TYPES = ("text", "number", "boolean", "categorical")
MISSINGNESS = {"machine": {...4}, "human": {...3}, "accepted": {...4}}
def field_id(matrix_id: UUID, name: str) -> UUID
def build_fields(matrix_id, columns: Sequence[ExtractionColumn]) -> list[dict]   # adds field_id; 422 on bad categorical
def form_hash(provenance: str, protocol_version_id: UUID | None, fields: list[dict]) -> str
def field_def(fields, field_id) -> dict | None        # type/unit/timepoint/categories only
def coerce(field: dict, raw: Any) -> tuple[Any, bool]  # (typed or raw, valid)
def check_missingness(writer: str, missingness: str) -> None       # ValueError
def display(value: Any) -> str                          # 12.0 -> "12", True -> "true"
def is_stale(accepted_def, current_def, accepted_hash, current_hash) -> bool
def pick_cell(accepted: Seq[Acc], machine: Seq[Obs], legacy: Cell | None, current_def, current_hash) -> CellView | None
def check_acceptance(value, missingness, cited: Seq[Obs]) -> None # "equals one valid cited" / disagreement rule
```

**Step 1: write the failing tests.**
- `test_field_id_stable_across_versions_and_distinct_across_matrices`
- `test_form_hash_ignores_nothing_but_order_matters`: reordering columns makes a new version, and identical input gives the same hash.
- `test_coerce_number_boolean_categorical_text`: `"12"`→12, `"12 participants"`→invalid, `"Yes"`→True, and a category match is exact after case-fold.
- `test_missingness_taxonomy_per_writer`: machine cannot write `unresolved_disagreement`, human cannot write `extraction_error`, and accepted cannot write `extraction_error`.
- `test_value_xor_missingness`
- `test_stale_on_field_def_change_not_on_unrelated_column`
- `test_stale_on_source_hash_change`
- `test_pick_cell_precedence_accepted_then_machine_then_legacy`
- `test_pick_cell_prefers_non_stale_accepted`
- `test_acceptance_must_equal_a_valid_cited_observation`
- `test_unresolved_disagreement_needs_two_differing`

**Step 2: run them.** `pytest -q backend/tests/unit/services/test_extraction_rules.py` fails on the import.
**Step 3: implement.** Pure code: no DB and no FastAPI. The routes turn `ValueError` into 422.
**Step 4: rerun** until green.
**Step 5: commit.** `feat(research): typed extraction field rules and missingness taxonomy (GOO-304)`

---

### Task 2: Models + migration

**Files:**
- Modify `backend/src/models/extraction_matrix.py` to add three classes, reusing the `_fk`/`_created_at` helper pattern from `backend/src/models/screening.py:32-40`.
- Export the three classes from `backend/src/models/__init__.py`.
- Create `backend/alembic/versions/a3c5e7f9b1d4_version_extraction_forms.py`.

```python
class ExtractionFormVersion(Base):        # extraction_form_versions — immutable
    id; matrix_id FK extraction_matrices RESTRICT; version_no INT NOT NULL
    provenance VARCHAR(24) CHECK IN ('legacy_unversioned','authored')
    fields JSONB NOT NULL                  # [{field_id,name,description,type,unit,timepoint,categories}]
    protocol_version_id FK research_protocol_versions NULL
    content_hash VARCHAR(64) NOT NULL; created_by_id FK users NULL; created_at
    UNIQUE(matrix_id, version_no); CHECK legacy<->created_by NULL; CHECK legacy->version_no=1

class ExtractionObservation(Base):        # extraction_observations — insert-only
    id; form_version_id FK RESTRICT; field_id UUID NOT NULL; document_id FK documents RESTRICT
    kind CHECK IN ('machine','human'); actor_user_id FK users NOT NULL
    extractor_run_id VARCHAR(64) NULL; extractor_model VARCHAR(100) NULL   # CHECK required iff machine
    value JSONB NULL; missingness VARCHAR(32) NULL CHECK IN (taxonomy)     # CHECK xor
    validation_state CHECK IN ('valid','invalid'); citation TEXT NULL
    source_hash VARCHAR(64) NOT NULL; created_at
    INDEX(document_id, field_id, created_at)

class ExtractionAcceptedValue(Base):      # extraction_accepted_values — insert-only chain
    id; form_version_id FK RESTRICT; field_id UUID; document_id FK RESTRICT
    value JSONB NULL; missingness VARCHAR(32) NULL (CHECK xor, CHECK <> 'extraction_error')
    observation_ids JSONB NOT NULL (1..20); accepted_by_id FK users NOT NULL
    rationale TEXT NOT NULL; source_hash VARCHAR(64) NOT NULL
    supersedes_accepted_value_id FK self NULL UNIQUE; created_at
    UNIQUE(document_id, field_id) WHERE supersedes_accepted_value_id IS NULL
```

Column justification: `observation_ids` is JSONB because it is only read as a set (the same choice as GOO-302's `input_observation_ids`). `accepted.source_hash` exists because staleness (b) needs it and the cited observations' hash could differ across rows. `field_id` is not an FK because fields are rows *inside* the version JSONB. Membership is validated in the service and in replay.

**Migration.** `upgrade()` creates the three tables with `_deny_data_api` on each, then runs the backfill in one pass per matrix: `SELECT id, columns, created_at FROM extraction_matrices` (soft-deleted rows included), plus the orphan cell names from `SELECT DISTINCT matrix_id, column_name FROM extraction_cells`. It inserts the legacy version using the frozen namespace and hash. `downgrade()` drops the three tables in reverse FK order. Legacy tables are never touched, and data written after the upgrade is lost (stated in the docstring).
- Modify the `_REBUILT_TABLES` drop order in `backend/tests/integration/test_screening_queue_postgres.py:77-88`: add the three new tables **first**, because their FKs point at `documents`/`users` only, and at `research_protocol_versions`, which stays.

**Step 1:** `python scripts/ci/check_alembic.py` (from `backend/`) reports a single head `a3c5e7f9b1d4`.
**Step 2:** `alembic upgrade head --sql | grep -c extraction_form_versions` > 0 (offline, no Docker).
**Step 3: commit.** `feat(research): extraction form versions, observations and accepted values tables (GOO-304)`

---

### Task 3: Ledger family `research_extraction`

**Files:**
- Modify `backend/src/services/research_decisions/ledger.py`: the vocabulary goes next to `:82-114`, `_validate_extraction_payload` next to `_validate_screening_payload`, `_validate_extraction_transitions` after `:785`, and the entry in `_FAMILIES :788-810`. Update the module docstring's family list at `:3-6`.
- Modify `backend/tests/unit/services/test_research_decision_ledger.py`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `extraction.observed` | `collection_id, matrix_id, form_version_id, form_content_hash, document_id, source_hash, kind, observations{obs_id: field_id}, extractor_run_id, extractor_model` | `machine` or `reviewer` |
| `extraction.accepted` | `collection_id, matrix_id, accepted_value_id, form_version_id, document_id, field_id, observation_ids, value, missingness, supersedes_accepted_value_id, source_hash` | `adjudicator` |
| `extraction.staled` | `collection_id, matrix_id, new_form_version_id, accepted_value_ids` | `editor` |

`aggregate_id = matrix_id` and `collection_id = project_id`. `subject_type="extraction_matrix"`, `subject_id=matrix_id`, `requires_subject_version=False` (so `subject_hash` = payload fingerprint, `ledger.py:612-615`). Payload validation: `matrix_id == aggregate_id`, `kind ∈ {machine, human}`, missingness is in the taxonomy, and every id parses as a UUID.

**Replay rules** (`_validate_extraction_transitions`):
1. Every `collection_id` is the same.
2. `observed`: `kind == 'machine'` ⇔ `actor_role == 'machine'` ⇔ the extractor fields are non-null. `human` ⇔ `reviewer`. Observation ids are never seen twice.
3. `accepted`: `actor_role == 'adjudicator'`. Every cited id was observed earlier in this stream with the same `document_id`, `form_version_id` and `field_id`. `supersedes_accepted_value_id` equals the current tip for `(document_id, field_id)`.
4. `staled`: every id is a current tip that is not already staled, and `new_form_version_id` differs from each tip's `form_version_id`.

**Tests:**
- `test_extraction_replay_rejects_worker_acceptance`: actor_role `machine` on `accepted` raises `DecisionReplayError`.
- `test_extraction_replay_rejects_accept_citing_unobserved_or_other_field`
- `test_extraction_replay_rejects_forked_accept_chain`
- `test_extraction_replay_rejects_machine_without_extractor`
- `test_extraction_payload_keys_exact`

**Commit:** `feat(research): research_extraction decision family and replay rules (GOO-304)`

---

### Task 4: Service (`extraction_forms_service.py`) + worker

**Files:**
- Create `backend/src/services/research/extraction_forms_service.py`.
- Modify `backend/src/services/research/extraction_matrix_service.py:144-310`.
- Modify `backend/src/tasks/research_tasks.py:206-222`.
- Modify `backend/tests/unit/services/test_bg_extraction_docid_scope.py` (only if it asserts the upsert; its scope assertion stays).

```python
async def current_version(db, matrix_id) -> ExtractionFormVersion | None
async def create_version(db, context, matrix, columns, actor_id) -> ExtractionFormVersion   # no-op on same hash; mirrors matrix.columns;
                                                                                           # appends extraction.staled for (a); commits once
async def extraction_task_kwargs(db, matrix, document_ids, actor_id, task_id) -> dict      # pins form_version_id, source_hashes, actor
async def append_machine_observations(db, *, matrix, version, document, parsed, actor_id, run_id, model) -> None  # worker-only; no commit
async def observe(db, context, matrix_id, actor_id, data: ExtractionObservationCreate) -> ExtractionObservationResponse   # REVIEW
async def accept_value(db, context, matrix_id, actor_id, data: ExtractionAcceptCreate) -> ExtractionAcceptedValueResponse  # ADJUDICATE
async def list_observations(db, context, matrix_id, document_id, field_id) -> list[...]       # VIEW; plus accepted chain
async def list_versions(db, context, matrix_id) -> list[...]                                  # VIEW
async def cell_view(db, matrix, allowed_document_ids) -> tuple[FormVersionSummary | None, list[dict]]
def document_source_hash(document) -> str
```

**Write order for `observe` and `accept_value`** (the same shape as `screening_service.submit`, `backend/src/services/research_engine/screening_service.py:820-835`):
1. Routes resolve the context first: `resolve_project(REVIEW | ADJUDICATE)`.
2. Load the matrix scoped to `context.collection.id` and not deleted, else 404 `Matrix not found`.
3. `lock_aggregate_stream(research_extraction, matrix_id)` (`ledger.py:448`).
4. Idempotency replay via `identity_service._replayed_event` (`backend/src/services/research_engine/identity_service.py:75`). A replay returns the original rows, looked up by the ids in its payload.
5. The document must be visible through `project_documents_query(collection).where(Document.id == ...)` (`project_access.py:94-112`), else 404 `Document not found`.
6. The form version must be current, else 409 `Form version is stale`. The field must be in it, else 422.
7. Run `extraction_rules` checks: coercion and missingness, plus for accept the tip, source, and "equals a cited" checks.
8. Insert the row, `append_decision`, and `commit()` once.
9. Catch an `IntegrityError` on `uq_extraction_accepted_initial` or the supersedes-unique constraint and return 409 `Accepted value is stale; reload` (the SQLSTATE backstop, as in GOO-301 commit `a51622bc2`).

**Worker** (`run_background_extraction`), new keyword-only params `form_version_id`, `initiated_by_user_id`, `source_hashes`; the `columns` param stays and is ignored. For each document:
1. Skip if the idempotency key already exists.
2. Scoped fetch (unchanged, `:192-195`).
3. Hash mismatch → skip.
4. Empty text → `unavailable_text` rows.
5. Otherwise prompt, then `coerce`.
6. `lock_active_project` (`:220`).
7. `append_machine_observations` (rows + one `extraction.observed`, `actor_role='machine'`, `actor_user_id=initiated_by_user_id`).
8. `commit()`.

The upsert at `:221-249` and the `0.8` are deleted. Of the extraction write functions, the worker may call only `append_machine_observations`.

**Enqueue sites:** all three keep their `run_extraction_matrix.apply_async(...)` call, so the source guards in `backend/tests/unit/api/test_audit_pr8_10_guards.py:134-141` and `test_audit_pr7_agent_runtime_guards.py:132-137` stay green. Each builds `kwargs` with `extraction_task_kwargs(...)`, using `actor_id=current_user.id`:
- `extraction_matrix.py:159-168` (create);
- `extraction_matrix.py:421-430` (trigger);
- `backend/src/api/research/projects.py:512-523` (document added).

**Commit:** `feat(research): append-only machine and human extraction observations (GOO-304)`

---

### Task 5: API + contracts

**Files:**
- Modify `backend/src/shared/scispace_schemas.py`:
  - `ExtractionColumn` gains `type, unit, timepoint, categories`, all optional with defaults.
  - New `ExtractionObservationCreate{document_id, field_id, form_version_id, value: Any|None, missingness: Literal[3]|None, citation: str(≤2000)|None, idempotency_key: str(1..255)}`.
  - New `ExtractionAcceptCreate{document_id, field_id, form_version_id, observation_ids: list[UUID](1..20), value, missingness: Literal[4]|None, rationale: str(1..2000), supersedes_accepted_value_id: UUID|None, idempotency_key}`.
  - New response models `ExtractionFormVersionResponse`, `ExtractionObservationResponse`, `ExtractionAcceptedValueResponse` and `ExtractionCellObservationsResponse{observations, accepted_chain}`.
- Modify `backend/src/api/research/extraction_matrix.py`.
- Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

| Route | Action | Service | Commits |
|---|---|---|---|
| `GET /matrices/{id}` (existing) | VIEW | `cell_view`, which adds `form_version` and new cell keys | no |
| `POST /projects/{pid}/matrices`, `PATCH /matrices/{id}` (existing) | EDIT | `create_version` | yes (in service) |
| `GET /matrices/{id}/form-versions` | VIEW | `list_versions` | no |
| `GET /matrices/{id}/observations?document_id=&field_id=` | VIEW | `list_observations` | no |
| `POST /matrices/{id}/observations` | REVIEW | `observe` | yes |
| `POST /matrices/{id}/accepted-values` | ADJUDICATE | `accept_value` | yes |

The new routes call `resolve_project(db, matrix.project_id, current_user.id, action)` directly (`project_access.py:180`) so that they hold a `ProjectContext`. `_validate_project_ownership` (`extraction_matrix.py:65-72`) returns only the collection.

**oasdiff:** every existing matrix route returns an untyped dict (`"schema": {}` in `backend/openapi.json` for all seven matrix and extraction-task operations), so their new keys don't appear in the spec at all. They stay untyped on purpose: giving `{}` a typed `response_model` could be reported as a response type change. The new optional request fields on `ExtractionColumn` are additive. The four new operations are additions. **Expected result: no ERR-level change.** If oasdiff flags `ExtractionColumn` because of `defaultNonNullable`, that is WARN only.

**Tests** (`backend/tests/unit/api/test_extraction_forms_routes.py`, dependency-overridden like `test_extraction_matrix_docid_scope.py`):
- `test_observe_requires_reviewer_role_owner_gets_403`
- `test_accept_requires_adjudicator`
- `test_accept_rejects_value_not_in_citations_422`
- `test_patch_same_columns_creates_no_version`
- `test_get_matrix_keeps_legacy_keys`

**Commit:** `feat(research): extraction form, observation and acceptance endpoints (GOO-304)`

---

### Task 6: Structural guard (automation cannot accept)

**Files:**
- Create `backend/tests/unit/architecture/test_extraction_acceptance_boundary.py`.

This is an AST scan. It fails if any module under `src/tasks/`, `src/services/agent/` or `src/services/research/extraction_matrix_service.py` names `accept_value` or `ExtractionAcceptedValue`, or imports `extraction_forms_service` other than `append_machine_observations`/`extraction_task_kwargs`/`document_source_hash`. It also asserts that `accept_value`'s body contains `ResearchProjectRole.ADJUDICATOR`.

**Commit:** `test(research): guard that workers and agents cannot accept extraction values (GOO-304)`

---

### Task 7: PostgreSQL proof (one test)

**Files:**
- Create `backend/tests/integration/test_extraction_forms_postgres.py`, with `pytestmark = pytest.mark.integration`. It reuses the `screening_factory` schema-per-test fixture shape (`test_screening_queue_postgres.py:113-140`) and `seed_approved_protocol_binding` (`research_engine_postgres_support.py`).

**`test_extraction_forms_legacy_migration_and_observation_lifecycle`** runs these steps in order:
1. **Build the pre-migration state:** `Base.metadata.create_all`, drop the three new tables, then run `_upgrade` for the chain up to the parent. Seed an org A project with two documents (checksums set), one matrix with `columns=[Sample size, Design]`, three `extraction_cells` (one on the orphan column `Old col`, `confidence=0.8`), and one soft-deleted matrix. Seed users: an editor E, reviewers R1 and R2, an adjudicator J, and an org B user F.
2. **Migrate in place:** run `a3c5e7f9b1d4.upgrade()`. Assert:
   - one `legacy_unversioned` v1 per matrix, the soft-deleted one included;
   - `created_by_id IS NULL`;
   - fields = `[Sample size, Design, Old col]`;
   - `extraction_cells` is byte-identical (row count, values, `confidence` 0.8);
   - `GET` view: every cell has `source='legacy'`, `stale=false` and no accepted row.
3. **Commit/reopen:** in a new session, `cell_view` returns the same three cells.
4. **Amend:** E PATCHes to change `Sample size` to `number`. This creates v2 (`authored`, `created_by=E`, `protocol_version_id` = approved). v1 stays readable through `list_versions`. PATCHing again with the same payload creates no v3.
5. **Machine run, then rerun:** call `run_background_extraction` directly twice with two task ids (the LLM client is stubbed to `{"Sample size": {"value": "12 participants"}, "Design": {"missing": "not_reported"}}`). Assert that 2×2 observations were appended with `kind='machine'`, `actor_user_id=E` and `extractor_run_id` = each task id. `Sample size` must be `validation_state='invalid'` with its raw value kept, and `Design` must have `missingness='not_reported'`. The ledger holds two `extraction.observed` events with `actor_role='machine'`. Calling the first task id again appends nothing, which covers the retry skip.
6. **Worker cannot accept:** after step 5, `SELECT count(*) FROM extraction_accepted_values` is 0.
7. **Concurrent reviewers:** R1 and R2 submit different valid human values for `(doc1, Sample size)` concurrently (`asyncio.gather`, separate sessions). Both rows are retained and `list_observations` shows both.
8. **Accept:**
   - J accepts R1's value citing `[R1, R2]`: 201. The row holds v2's id, both observation ids and J.
   - A second accept by J with `supersedes=None` while a tip exists: 409.
   - Two concurrent accepts with the same `supersedes` value: exactly one succeeds, then `SELECT 1` still works.
   - R1 (reviewer, no ADJUDICATOR) calls accept: 403.
9. **Stale:** E PATCHes `Sample size.unit` to `participants`, creating v3. The accepted row is **unchanged** (column-by-column compare). `cell_view` shows it with `source='accepted'` and `stale=true`, and the ledger has one `extraction.staled` naming it.
10. **Tenancy:**
    - F gets 404 on `GET /observations`, observe and accept.
    - Archive the project: observe and accept get 409, and the worker writes nothing for a new task.
    - Soft-delete the matrix: all three routes get 404, and the worker skips.
    - Soft-delete doc2 through `CollectionDocument`: it disappears from `cell_view`, and observe on it gets 404.
11. **Replay:** `replay_decisions(research_extraction, matrix_id)` succeeds, and the event sequence is contiguous.
12. **Downgrade:** after `downgrade()` the three tables are gone, and `extraction_cells` and `extraction_matrices.columns` (the v3 mirror) are intact.

Run it with: `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_extraction_forms_postgres.py`. Without a database, report it as **NOT RUN**.

**Commit:** `test(research): PostgreSQL proof for extraction form migration and observations (GOO-304)`

---

### Task 8: Minimal frontend

**Files:**
- Create `frontend/src/types/api/research-extraction-contract.ts`, which aliases the new generated schemas (the pattern is `frontend/src/types/api/research-screening-contract.ts`).
- Modify `frontend/src/types/scispace.ts:15-30`: `ExtractionCell` and `ExtractionMatrix` gain the new keys as optional fields. They stay hand-written because the wire shape is untyped `{}` (see `docs/engineering/api-contracts.md` "Adopt-on-touch").
- Modify `frontend/src/services/scispaceService.ts` to add `getCellObservations` and `listFormVersions`.
- Modify `frontend/src/components/research/ExtractionMatrix.tsx:503-515` and the header.
- Modify `frontend/src/components/research/CellCitation.tsx:11-34`.
- Create `frontend/src/components/research/CellObservations.tsx`.
- Tests go in `frontend/src/components/research/__tests__/`.

**Behavior:**
- **Header badge:** `Form v{n}`, or `Legacy (unversioned)`. Its `title` shows the truncated `content_hash` and, when there is one, the protocol version.
- **Cell:**
  - Missingness renders as muted italic text with a label map, for example `not_reported` → "Not reported". Colors use theme tokens only.
  - `stale` adds a small "Stale" text badge with `aria-label`.
  - `source='legacy'` adds a "Legacy" badge.
  - `validation_state='invalid'` adds an "Unvalidated" badge.
- **`CellCitation`:** returns the confidence row only when `confidence !== null`, so there is no fake "Low".
- **`CellObservations`:** a popover button (`aria-label="Observations for {column}"`) that runs `useQuery(['extraction-observations', matrixId, documentId, fieldId], { enabled: open })`. It lists kind, actor/model, form version, value or missingness, `created_at` and the accepted chain. It is read-only; reconciliation belongs to GOO-305.

**Tests:**
- `test renders missingness label and stale badge`
- `test hides confidence when null`
- `test observations popover lists both reviewers`

**Commit:** `feat(frontend): form version badge, missingness and per-cell observations (GOO-304)`

---

## Mutation verification

Follow the procedure in `docs/engineering/testing.md`. Record each result in the test docstring and in `docs/testing/agent-orchestration-mutation-checks.md` under a GOO-304 section.

| Guard (file:line set during implementation) | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| The `accept_value` tip check (`supersedes != current`) | `if False:` | `pytest -q backend/tests/integration/test_extraction_forms_postgres.py` | step 8's 409 becomes an `IntegrityError` from the partial unique index, and `SELECT 1` then fails in the aborted transaction |
| The accept SQLSTATE backstop (`IntegrityError` → 409) | re-raise | same | an unhandled `IntegrityError` in the concurrent-accept step |
| The worker idempotency skip | remove the pre-LLM lookup | same | step 5 retry: 2 extra machine rows, or `DecisionIdempotencyConflict` |
| The ADJUDICATOR requirement in `accept_value` | drop the role check | `pytest -q backend/tests/unit/api/test_extraction_forms_routes.py -k adjudicator` and `backend/tests/unit/architecture/test_extraction_acceptance_boundary.py` | the reviewer accept returns 201, and the guard test fails |
| Replay `accepted` actor_role rule | delete the check | `pytest -q backend/tests/unit/services/test_research_decision_ledger.py -k worker_acceptance` | `DecisionReplayError` is not raised |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_extraction_rules.py backend/tests/unit/services/test_research_decision_ledger.py
pytest -q backend/tests/unit/api backend/tests/unit/architecture backend/tests/unit/services/test_bg_extraction_docid_scope.py
(cd backend && python ../scripts/ci/check_alembic.py)     # single head a3c5e7f9b1d4 (or the re-pointed chain)
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts   # after committing both
pnpm --dir frontend exec vitest run src/components/research
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

Report the PostgreSQL test as **NOT RUN** if there is no `postgres_container`/`RESEARCH_DECISION_DATABASE_URL`.

## Authenticated journey list for Linear closure

Run this on dev (goodwiinz.tech → the dev API) after the deploy. `kubectl -n rag-dev exec deploy/backend -- alembic current` should show `a3c5e7f9b1d4 (head)`.

1. **Legacy preserved:** open a project whose matrix existed before the deploy. The header reads `Legacy (unversioned)`, and every old value is still shown with a "Legacy" badge. Take a screenshot.
2. **Amend:** as an editor, change a column to `number` with a unit. The badge now reads `Form v2`. Legacy values of unchanged columns are still visible. `GET /form-versions` lists v1 and v2.
3. **Machine run ×2:** run extraction twice. The cell's Observations popover lists two machine rows, with the model, your name as initiator and v2. An unparseable number shows "Unvalidated", and a missing value shows "Not reported" rather than a blank.
4. **Two reviewers:** two REVIEWER accounts each POST a human observation to the same cell at the same time (curl). Both appear in the popover.
5. **Accept:** an ADJUDICATOR POSTs an accepted value citing both. The cell shows `source=accepted`. The same request from a reviewer returns 403, and a second accept with the old `supersedes` returns 409.
6. **Stale:** change that column's unit. The accepted cell shows "Stale", and `GET /observations` shows the accepted row unchanged.
7. **Tenancy:** a user from another org gets 404 on `GET /matrices/{id}/observations`. Archive the project, and a new observation POST returns 409.

## GOO-305 seam (aligned with `2026-09-30-goo-305-source-anchors.md`)

GOO-305 builds on this plan's names without renaming anything, and adds its migration `b8d0f2a4c6e9` with `down_revision = "a3c5e7f9b1d4"`. What this plan has to keep stable for it:

- **Hook point.** `accept_value` step 7 (after the lock and role reload, before the insert) is where GOO-305 calls `assert_anchor_acceptable`. Keep the checks there in one ordered block so the call has one place to go.
- **Source pinning.** GOO-305 pins its own `text_sha256` alongside this plan's `source_hash`. It doesn't replace `source_hash` or duplicate it, so `document_source_hash` keeps its meaning.
- **Ledger.** GOO-305 adds no event types. It extends `extraction.observed`, `extraction.accepted` and `extraction.staled` as schema-2 payloads in the `research_extraction` family, so the schema-1 vocabulary and replay rules in Task 3 must stay valid for existing events.
- **Evidence reads.** GOO-305 gives `list_observations` a document-visibility check (`project_documents_query(context.collection.id).where(Document.id == document_id)`, 404 on a miss). That check doesn't exist in this plan.
- **Parser tests.** `tests/unit/test_extraction_matrix_service.py:28-45` (repo root, not `backend/tests`) asserts `None` for a missing column and for invalid JSON. Whichever of GOO-304 (Task 4 mapping to `extraction_error`) or GOO-305 changes `_parse_extraction_result` first updates those two tests in the same PR.

## Out of scope (owned elsewhere)

- Reconciliation UI, source anchors and page/offset citations belong to GOO-305, which builds on `extraction_observations.id`/`citation` and `POST /accepted-values`.
- Blinding.
- Protocol-amendment staleness of forms.
- Source-change `staled` events.
- `report_id` on observations.
- A shared `extraction_forms` table.

Each has a `ponytail:` marker at its seam.

## Amendment — 2026-09-30 (implementation of Task 7)

**Mutation rows 1 and 2 were wrong as written.**
- Disabling the `accept_value` tip check does not produce an `IntegrityError` for a same-cell stale accept. The partial unique index `uq_extraction_accepted_initial` and the `supersedes` unique constraint still reject it, and `_flush_or_conflict` turns that into the same 409 `Accepted value is stale; reload`.
- The only case where the tip check alone matters is a `supersedes_accepted_value_id` naming **another cell's** tip. Such a row passes both indexes and forks the chain, which ledger replay would later reject as a 500. Task 7 therefore adds step 8b: a doc2 accept whose `supersedes` names doc1's tip must get 409. With the tip check removed, that accept is inserted (`DID NOT RAISE`).
- The backstop re-raise fails only together with the tip check off (`UniqueViolationError` on `uq_extraction_accepted_initial`). On its own it survives, because the stream lock serializes accepts, so no writer reaches the index while the tip check is on.

**Review follow-up.** Replay also rejects `extraction.staled` unless `actor_role == 'editor'` (`test_extraction_replay_rejects_staled_by_non_editor`).

Recorded results: `docs/testing/agent-orchestration-mutation-checks.md`, GOO-304 section.

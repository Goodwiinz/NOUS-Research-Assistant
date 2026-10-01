# GOO-310 Outcome Certainty, Evidence Tables + Contradiction Review Plan (Academic R5)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** For each protocol-declared outcome and timepoint, a reviewer can freeze an **evidence table version**. That is one row per analysis unit (study, or report without a study), and every cell resolves to a GOO-304 accepted value, its pinned source revision and the underlying GOO-299 study. A missing value is a visible `missing` cell and never a blank that looks like agreement. Contradicting values are recorded as inspectable **contradiction groups** with an explicit `unresolved`, `resolved` or `acknowledged` state, an attributed explanation, and dissent that stays visible after resolution. An **outcome-certainty assessment** (GRADE structure) cites one exact table version, the GOO-309 appraisals behind its risk-of-bias domain and the contradictions behind its inconsistency domain. Certainty, evidence agreement and model confidence are three separate things, stored in three places. An upstream change stales only the tables and assessments that depend on it, and every earlier version stays exportable.

**Architecture:**
- **Three insert-only tables**, each with the GOO-309 trigger function `prevent_research_insert_only_mutation()`:
  - `evidence_table_versions`: rows as JSONB, read only as a whole.
  - `evidence_contradictions`: a chain per group.
  - `outcome_certainty_assessments`: a chain per outcome and timepoint.
- **One pure module**, `evidence_rules.py`, which builds rows, derives cell states, derives the certainty level and derives the contradiction status and dissent.
- **One ledger family**, `research_evidence`, with one stream per Collection. **One service**, `evidence_service.py`, and **one router**, `api/research_engine/evidence.py`.
- **Derived status and staleness only.** Staleness extends GOO-307's `_graph` through `evidence_service.graph_part`, the same hook GOO-309 added.
- **Model stance data** from `stance_classifications` is shown as `unreviewed_model_suggestion` groups on the table read. It is snapshotted into a contradiction only when a human opens one, and it never sets any state.

**Tech stack:** FastAPI, SQLAlchemy async, Alembic, the PostgreSQL integration fixture (`postgres_container`, `backend/tests/integration/conftest.py:208`), openapi-typescript, Next.js with TanStack Query and Vitest.

**Stacking (read first):**
- This PR stacks on **GOO-309** (`feat/goo-309-appraisal-instrument`), which stacks on GOO-308 → 307 → … → 299.
- It opens after GOO-309 merges. Line numbers below are for `e5b909b56` plus the GOO-309 plan, so re-locate them after each rebase.
- GOO-311 consumes `evidence_table_versions` (id, `rows`, `field_ids`, `content_hash`, `form_version_id`, `excluded`) and `evidence_service.table_version`, both defined here.

### Consumed names

| Consumed | Name (path:line) | Property relied on |
|---|---|---|
| Unit rule (GOO-309) | `identity_service.analysis_unit(report)` | `study:<id>` for a confirmed study, `report:<id>` for no link, `None` for an unresolved link. |
| Document → report (GOO-309) | `acquisition_service.document_reports(db, collection_id)` | Built from `retrieved` head attempts plus the merge chain. |
| Declared outcomes (GOO-309) | `protocol_methods.declared_outcomes(snapshot)` | `key → timepoints`. |
| Appraisals (GOO-309) | `appraisal_service.current_appraisals(db, collection_id)` → `{(target_key, outcome_key, timepoint): Status(value, unresolved_domains, current_ids)}`, and the graph node `("appraisal", id)` | Revealed keys only; `value ∈ {awaiting_independent, agreed, conflict, adjudicated}`. |
| Insert-only trigger (GOO-309) | `prevent_research_insert_only_mutation()` (created in `e2a4c6b8d0f1`) | SQLSTATE `55000` on UPDATE or DELETE. |
| Accepted values (GOO-304) | `ExtractionAcceptedValue` (`backend/src/models/extraction_matrix.py:234`), tip = unsuperseded (`extraction_forms_service._is_tip`, `backend/src/services/research/extraction_forms_service.py:293`), `current_version` (`:237`) | Keyed `(document_id, field_id)`, with `value` xor `missingness`, and `source_hash` plus `text_sha256` pinned. |
| Field definition (GOO-304) | `extraction_rules.find_field` / `field_def` (`backend/src/services/research/extraction_rules.py:164-177`) | A field carries `type, unit, timepoint, categories`. |
| Legacy/machine cells (GOO-304) | `extraction_forms_service.cell_view(db, matrix, allowed_document_ids)` (`:1195`), each cell's `source ∈ {accepted, machine, legacy}` | Only `accepted` is reviewed. |
| Project documents | `project_access.project_documents_query(project_id)` (`backend/src/services/research_engine/project_access.py:100`) | Organization- and deletion-scoped. |
| Stance rows | `StanceClassificationModel` (`backend/src/models/evidence.py:29`): `claim_hash, claim_text, source_id, source_content_hash, organization_id, stance, confidence, model_version, inference_model_version` | This table is **mutable**: the unique `(claim_hash, source_id, model_version, organization_id)` gets overwritten. It is read only, and snapshotted when cited. |
| Evidence graph (GOO-307) | `release_rules.node`, `source_node`, `dependents` (`backend/src/services/research/release_rules.py:56-63,146`), `draft_release_service.Graph`/`_graph` (`backend/src/services/research/draft_release_service.py:111,129`), with GOO-309's `graph_part` hook in `_graph` | `stale_nodes()` is derived on read. |
| Ledger | `lock_aggregate_stream` (`backend/src/services/research_decisions/ledger.py:1089`), `append_decision` (`:1092`), `replay_decisions` (`:1191`), `_RATIONALE_EVENTS` (`:339`), `_FAMILIES` (`:1895`), `identity_service._replayed_event` (`backend/src/services/research_engine/identity_service.py:76`) | The append is caller-owned. |
| Roles | `ResearchAction.REVIEW`/`ADJUDICATE`, `resolve_project` (`project_access.py:28-47,186`) | Roles are reloaded after the lock; ownership grants no decision role. |
| Current protocol | `identity_service.current_protocol_version_id` (`:103`) | The approved version, or `None`. |
| Canonical hash | `contracts.canonical_json_sha256` (`backend/src/services/research_engine/contracts.py:38`) | Stable body hashes. |
| Bundle | `audit_bundle._sealed_part` (`backend/src/services/research_engine/audit_bundle.py:80`), `gather_parts` (`:290`) | One reader per part. |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| **How rows key on (study, outcome, timepoint)** | A table version is created for one `(outcome_key, timepoint)` (declared in the protocol) over an explicit, ordered `field_ids` list from **one matrix's current form version**. Every field must have `field_def(...).timepoint == timepoint` (otherwise 422 `Field {name} is not at {timepoint}`). The table holds **exactly one row per `analysis_unit`**, with `row_key = f"{unit}\|{outcome_key}\|{timepoint}"`. Units are the distinct `analysis_unit` values of the project's documents in `document_reports`. Documents whose report link is unresolved go to `excluded: [{document_id, report_id, reason: "study_link_unresolved"}]`, and documents with no report go to `excluded` with `reason: "no_report_identity"`. | The row is the unit of evidence, so two reports of one study share one row, and GOO-311 can never double-weight a study by construction. GOO-304 fields already carry `timepoint`, so no new outcome model is needed. The protocol's declared outcome is the governing key. |
| Cells | For each row and field, the cell holds every non-stale accepted tip on any document of that unit. The state is `value` (≥1 tip, all with equal `value`), `missingness` (all tips carry the same `missingness`, e.g. `not_reported`), `missing` (no tip) or `conflict` (tips disagree). Each tip is stored as `{accepted_value_id, document_id, report_id, source_hash, text_sha256, value, missingness}`. Accepted values are immutable, so copying `value` into the snapshot is exact. | "Every table value resolves to accepted extraction, source revision and underlying study." "Missing evidence never implies agreement" is enforced by giving `missing` its own state, and a within-study `conflict` cell is visible, not collapsed. |
| Unreviewed data | The table **read** (`GET .../tables/preview`) also returns `unreviewed_cells`: the `cell_view` cells with `source ∈ {machine, legacy}` for the same fields. They are labelled `review_state: "unreviewed"` and are never written into a version. | "Legacy matrix cells stay usable but explicitly unreviewed until reconciled." Reconciliation is GOO-304 acceptance. |
| Versioning | `POST .../tables` takes `{outcome_key, timepoint, matrix_id, field_ids, supersedes_table_id?, idempotency_key}`. The server builds rows from the current tips. `content_hash = canonical_json_sha256({protocol_version_id, form_version_id, outcome_key, timepoint, field_ids, rows, excluded})`. If it equals the tip's hash, the tip is returned (`200`, `replayed=true`) and nothing is written. Otherwise a successor is inserted, and `supersedes_table_id` must be the current tip (409 `Evidence table is stale; reload`). Partial unique initial per `(collection_id, outcome_key, timepoint)` and `UNIQUE(supersedes_table_id)`. | An unchanged rebuild is stable and a changed one is a new version. Old versions are never touched, so prior exported states are retained. |
| Contradiction groups | `evidence_contradictions` is one insert-only chain per group. `contradiction_id` is the id of the `opened` row. Each row has a `kind`: `opened`, `resolved`, `acknowledged` or `dissent`. The **opened** row cites `table_version_id`, `field_id` and ≥2 `accepted_value_ids`, all present in that version's cells for that field (otherwise 422 `Contradiction members must be table cells`). Members may come from different units or from a within-unit `conflict` cell. The derived status is the last non-`dissent` kind (`opened` → `unresolved`). `previous_id` must be the chain tip (409 `Contradiction is stale; reload`, `UNIQUE(previous_id)`). Every row needs `explanation` (1–4000). | This is an inspectable group with an attributed explanation. Because rows are insert-only, "dissent remains visible even when resolved": a `dissent` row never changes the status and is always listed, and the rows before a resolution are kept. |
| Who records what | `opened` and `dissent`: REVIEW or ADJUDICATE (actor_role is the role used). `resolved` and `acknowledged`: ADJUDICATE only (`CHECK (kind NOT IN ('resolved','acknowledged') OR actor_role = 'adjudicator')`). `acknowledged` means "a real disagreement, kept as such". | A reviewer can raise a disagreement but not close it. That follows the same explicit-role split as GOO-302 and GOO-304. |
| Stance data as suggestions | The table read returns `stance_suggestions`. These are `StanceClassificationModel` rows with `organization_id == context.organization_id` and `source_id IN` the table's document ids, grouped by `claim_hash`, keeping groups that contain both `supporting` and `opposing`. Each carries `{id, source_id, stance, confidence, model_version, inference_model_version}` and `review_state: "unreviewed_model_suggestion"`. When opening a contradiction, a user may pass `stance_classification_ids`. The server snapshots those rows (org-filtered, 404 if foreign) into the opened row's `suggestion` JSONB with `origin: "model_suggestion"`. A suggestion never creates, resolves or blocks anything. | The stance meter's upsert semantics (`uq_stance_classifications_org_claim_src_model`) cannot be a decision ledger. A snapshot keeps the attribution after the row is overwritten, and the human row is the decision. |
| Certainty method | The protocol declares `appraisal_synthesis.certainty = {"method": "grade", "version": "handbook-2013"}`, parsed by `protocol_methods.certainty_method(snapshot)` (409 `Protocol declares no certainty method`). Only the **structure** is encoded: the starting level `high \| low`, the five downgrade domains `risk_of_bias, inconsistency, indirectness, imprecision, publication_bias`, each with a rating of `0, -1, -2` or `null`, and the levels `high, moderate, low, very_low`. No handbook text. Upgrading domains are not encoded. | GRADE is the standard certainty framework, and structure-only encoding matches GOO-309's licence stance. The pilot is randomized trials, which start `high` and never upgrade. `# ponytail: add large-effect/dose-response upgrades when observational outcomes are graded.` |
| Level (derived) | `evidence_rules.certainty_level(start, ratings)` returns `null` if any rating is `null`, and otherwise the start minus Σ|rating|, floored at `very_low`. The row stores `level`, and the service rejects a request whose `level` differs (422), so the stored level is always the derived one. | Missing ratings stay unknown, and there is no scoring beyond GRADE's own arithmetic. |
| What certainty cites | `table_version_id` must be the current, non-stale tip for its outcome and timepoint (409 `Evidence table is stale; reload`). A non-null `risk_of_bias` rating requires `appraisal_assessment_ids` to be exactly the union of `current_ids` from `current_appraisals` for every row's `(unit, outcome_key, timepoint)`, each with status `agreed` or `adjudicated`. Otherwise 422 `Risk of bias unresolved for {unit}`, and the rating must stay `null`. A non-null `inconsistency` rating requires `contradiction_ids` to list every contradiction group on this table version. The response **always** lists `unresolved_contradictions` and `dissent`, whatever the rating. | Certainty rests on reviewed appraisal, never on a missing one. Conclusions keep their uncertainty and dissent in the same object that states the level. |
| Who assesses certainty | REVIEW (`actor_role = 'reviewer'`). One chain per `(collection_id, outcome_key, timepoint)` (partial unique initial plus `UNIQUE(supersedes_certainty_id)`). `rationale` is required (1–4000). | The ticket asks for "reviewer and rationale". A successor by a different reviewer keeps the earlier row, attributed, in the history. |
| Separation | Certainty (`outcome_certainty_assessments.level`), agreement (contradiction status) and model confidence (`stance_classifications.confidence`, only inside `suggestion` snapshots and suggestion reads) never share a column or a computation. An AST guard (Task 6) fails if `evidence_rules.certainty_level`'s module names `confidence` or `stance`. | "Keep certainty, evidence agreement and model confidence separate." |
| Staleness (reusing GOO-307's walk) | `evidence_service.graph_part` adds these edges:<br>`("accepted", id) → ("evidence_table", t)` for every cell tip;<br>`("protocol", v) → ("evidence_table", t)`;<br>`("evidence_table", t) → ("certainty", c)`;<br>`("appraisal", a) → ("certainty", c)` for each cited appraisal;<br>`("accepted", id) → ("contradiction", group)` for its members.<br>`changed` adds superseded table versions and superseded certainty rows. `stale` on every read is `node in Graph.stale_nodes()`. Nothing is stamped, and there are no new hooks in upstream writers. | A changed source, form or accepted value reaches only the tables whose cells cite it, and through them only their certainty rows. An unrelated outcome's table stays current. This is the same walk and the same derive-on-read rule as GOO-307. |
| New data vs a version change | A study added after a table was frozen is not a version change of anything the table cites, so the walk does not stale it. The preview returns `differs_from_tip: bool` (a hash compare) so the UI can offer "Rebuild table". | That is honest: the frozen table is still exactly what it claims to be. `# ponytail: stale tables on unit-set change if reviewers ask for it.` |
| Write order | 1. `resolve_project(REVIEW\|ADJUDICATE)`. 2. `lock_aggregate_stream(research_evidence, collection_id)`. 3. `_replayed_event`. 4. Protocol checks. 5. Validation and reads (including `_graph` for staleness). 6. Insert, append, **one commit**. A unique violation on the initial or supersedes indexes gives 409 stale. | This is the R4 order. `_graph` is read under the Collection lock, so the gate cannot race an upstream writer. |

**Migration head:** new revision `f4b6d8a0c2e3_create_evidence_certainty.py`, with `down_revision = "e2a4c6b8d0f1"` (GOO-309). The re-pointing rule is the same as GOO-309's. The migration imports nothing from `src`. It copies `_deny_data_api`, creates the three tables, and attaches `CREATE TRIGGER trg_<table>_insert_only BEFORE UPDATE OR DELETE ... EXECUTE FUNCTION prevent_research_insert_only_mutation()` to each. `downgrade()` drops the triggers and tables in reverse FK order and **not** the function, which GOO-309 owns.

---

### Task 1: Pure rules (`evidence_rules.py`)

**Files:**
- Create `backend/src/services/research_engine/evidence_rules.py`.
- Modify `backend/src/services/research_engine/protocol_methods.py` to add `certainty_method(snapshot) -> tuple[str, str]`.
- Create `backend/tests/unit/services/test_evidence_rules.py`.

```python
CELL_STATES = ("value", "missingness", "missing", "conflict")
CONTRADICTION_KINDS = ("opened", "resolved", "acknowledged", "dissent")
GRADE_DOMAINS = ("risk_of_bias", "inconsistency", "indirectness", "imprecision", "publication_bias")
LEVELS = ("very_low", "low", "moderate", "high")      # ordered
@dataclass(frozen=True) class Tip: accepted_value_id; document_id; report_id; unit; field_id; source_hash; text_sha256; value; missingness
def build_rows(units: Sequence[str], tips: Sequence[Tip], field_ids: Sequence[UUID], outcome_key, timepoint) -> list[dict]
def cell_state(tips: Sequence[Tip]) -> str
def table_hash(protocol_version_id, form_version_id, outcome_key, timepoint, field_ids, rows, excluded) -> str
def contradiction_status(chain: Sequence[Row]) -> Literal["unresolved", "resolved", "acknowledged"]
def dissent(chain: Sequence[Row]) -> list[dict]                 # every dissent row + superseded opinions
def certainty_level(start: str, ratings: Mapping[str, int | None]) -> str | None
def check_certainty(start, ratings, level, rob_cited, rob_required, rob_statuses, contradiction_cited, contradiction_all) -> None  # ValueError
def stance_groups(rows: Sequence[Mapping]) -> list[dict]        # supporting+opposing per claim_hash
```

**Tests (write first; they fail on the import):**
- `test_two_reports_of_one_study_make_one_row`
- `test_missing_cell_is_missing_not_blank_and_not_agreement`
- `test_disagreeing_tips_in_one_unit_is_conflict_cell`
- `test_table_hash_stable_and_order_of_field_ids_matters`
- `test_contradiction_status_ignores_dissent_and_keeps_it_listed`
- `test_certainty_level_derivation_and_null_propagation`: `high` with −1, −1 gives `low`; `high` with −2, −2 floors at `very_low`; any `null` gives `null`.
- `test_rob_rating_requires_every_unit_resolved`
- `test_stance_groups_need_both_supporting_and_opposing`
- `test_no_confidence_in_certainty`: `certainty_level` ignores any extra key named `confidence`.

**Run:** `pytest -q backend/tests/unit/services/test_evidence_rules.py`
**Commit:** `feat(research): pure evidence-table, contradiction and certainty rules (GOO-310)`

---

### Task 2: Models + migration

**Files:**
- Create `backend/src/models/research_evidence_table.py` (`EvidenceTableVersion`, `EvidenceContradiction`, `OutcomeCertaintyAssessment`) and export them from `backend/src/models/__init__.py`.
- Create `backend/alembic/versions/f4b6d8a0c2e3_create_evidence_certainty.py`.
- Modify `backend/tests/integration/test_screening_queue_postgres.py`: prepend the three tables to `_REBUILT_TABLES` (dependents first), and append the filename to `_upgrade`'s list.

| Table | Columns | Constraints |
|---|---|---|
| `evidence_table_versions` | `id, collection_id FK RESTRICT, protocol_version_id FK NOT NULL, outcome_key VARCHAR(100), timepoint VARCHAR(100), matrix_id FK extraction_matrices, form_version_id FK extraction_form_versions, field_ids JSONB, rows JSONB, excluded JSONB, content_hash CHAR(64), created_by_id FK users, supersedes_table_id FK self NULL, created_at` | `UNIQUE(supersedes_table_id)`; partial unique `uq_evidence_table_initial (collection_id, outcome_key, timepoint) WHERE supersedes_table_id IS NULL`; PostgreSQL-only `jsonb_typeof(field_ids)='array' AND jsonb_array_length(field_ids) BETWEEN 1 AND 50` |
| `evidence_contradictions` | `id, collection_id, contradiction_id UUID NOT NULL, table_version_id FK, field_id UUID, accepted_value_ids JSONB NULL, kind VARCHAR(16), explanation TEXT NOT NULL, actor_id FK users, actor_role VARCHAR(16), suggestion JSONB NULL, previous_id FK self NULL, created_at` | `kind IN (...)`; `actor_role IN ('reviewer','adjudicator')`; `(kind = 'opened') = (previous_id IS NULL)`; `kind <> 'opened' OR contradiction_id = id`; `(kind = 'opened') = (accepted_value_ids IS NOT NULL)`; `suggestion IS NULL OR kind = 'opened'`; `kind NOT IN ('resolved','acknowledged') OR actor_role = 'adjudicator'`; `UNIQUE(previous_id)` |
| `outcome_certainty_assessments` | `id, collection_id, table_version_id FK, outcome_key, timepoint, method_key VARCHAR(16), method_version VARCHAR(32), starting_level VARCHAR(8), domains JSONB, level VARCHAR(16) NULL, appraisal_assessment_ids JSONB, contradiction_ids JSONB, rationale TEXT NOT NULL, assessed_by_id FK users, actor_role VARCHAR(16) CHECK = 'reviewer', input_hash CHAR(64), supersedes_certainty_id FK self NULL, created_at` | `starting_level IN ('high','low')`; `level IS NULL OR level IN (...)`; `UNIQUE(supersedes_certainty_id)`; partial unique `uq_certainty_initial (collection_id, outcome_key, timepoint) WHERE supersedes_certainty_id IS NULL` |

Every partial index declares both `postgresql_where=` and `sqlite_where=`.

**Check:**
- `(cd backend && python ../scripts/ci/check_alembic.py)` reports single head `f4b6d8a0c2e3`.
- `alembic upgrade head --sql | grep -c evidence_table_versions` is > 0 (offline).

**Commit:** `feat(research): insert-only evidence table, contradiction and certainty tables (GOO-310)`

---

### Task 3: Ledger family `research_evidence`

**Files:**
- Modify `backend/src/services/research_decisions/ledger.py`: add the vocabulary after GOO-309's, add `evidence.contradiction_recorded` and `evidence.certainty_assessed` to `_RATIONALE_EVENTS`, add the payload and transition validators, and add the `_FAMILIES` entry (subject type `evidence_outcome`, `requires_subject_version=False`). Update the docstring list.
- Modify `backend/tests/unit/services/test_research_decision_ledger.py`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `evidence.table_versioned` | `collection_id, table_version_id, supersedes_table_id, outcome_key, timepoint, protocol_version_id, matrix_id, form_version_id, field_ids, content_hash, row_count, excluded_count` | `reviewer` or `adjudicator` |
| `evidence.contradiction_recorded` | `collection_id, contradiction_id, row_id, previous_id, kind, table_version_id, field_id, accepted_value_ids, suggestion_ids` | `kind`-dependent (see the model CHECK) |
| `evidence.certainty_assessed` | `collection_id, certainty_id, supersedes_certainty_id, table_version_id, outcome_key, timepoint, method_key, method_version, starting_level, ratings, level, appraisal_assessment_ids, contradiction_ids, input_hash` | `reviewer` |

**Replay rules** (the ledger imports only the pure `evidence_rules`):
1. Every `collection_id` equals the `aggregate_id`.
2. Each chain has at most one tip, and every `supersedes_*` or `previous_id` names the tip at that point.
3. `resolved` and `acknowledged` are by an `adjudicator`.
4. `level == certainty_level(starting_level, ratings)`.
5. A `certainty_assessed` event names a `table_version_id` that was versioned earlier in this stream for the same `(outcome_key, timepoint)`.

**Tests:**
- `test_evidence_replay_rejects_reviewer_resolution`
- `test_evidence_replay_rejects_forked_table_chain`
- `test_evidence_replay_rejects_level_not_derived`
- `test_evidence_payload_keys_exact`

**Commit:** `feat(research): research_evidence decision family and replay rules (GOO-310)`

---

### Task 4: Service (`evidence_service.py`) + graph part

**Files:**
- Create `backend/src/services/research_engine/evidence_service.py`.
- Modify `backend/src/services/research/draft_release_service.py` in GOO-309's `_graph` hook: add a second local import and call, `evidence_service.graph_part`, after `appraisal_service.graph_part`.

```python
AGGREGATE_TYPE = "research_evidence"; SUBJECT_TYPE = "evidence_outcome"
async def preview(db, context, outcome_key, timepoint, matrix_id, field_ids) -> EvidenceTablePreview  # VIEW; no write
async def create_table(db, context, actor_id, actor_role, data) -> tuple[EvidenceTableResponse, bool]  # REVIEW|ADJUDICATE; commits once
async def table_version(db, collection_id, table_version_id) -> Any   # scoped load; foreign -> 404 (GOO-311 reads this)
async def record_contradiction(db, context, actor_id, actor_role, data) -> tuple[ContradictionResponse, bool]
async def assess_certainty(db, context, actor_id, data) -> tuple[CertaintyResponse, bool]
async def list_outcomes(db, context) -> EvidenceOutcomeListResponse   # every version, contradiction chain, certainty chain, stale flags
async def graph_part(db, collection_id) -> tuple[list[Edge], set[Node]]
async def export_package(db, context) -> dict                         # schema nous.academic.evidence.v1
```

- **`preview` and `create_table` build tips** from `ExtractionAcceptedValue` tips on the given `field_ids`, restricted to `project_documents_query(collection_id)` and to documents present in `document_reports`. Tips that are `stale` under `_graph(...).stale_nodes()` are dropped, so a superseded or source-changed value never enters a new version. The tips go through `evidence_rules.build_rows`.
- **`matrix_id`** must belong to the project (404 otherwise), and `field_ids` must be in `current_version(matrix_id)`.
- **The stance query** is one `select(StanceClassificationModel).where(organization_id == context.organization_id, source_id.in_(document_ids))`, with no org fallback (the tenant rule).

**Commit:** `feat(research): evidence tables, contradiction review and outcome certainty (GOO-310)`

---

### Task 5: API + contracts + bundle part

**Files:**
- Create `backend/src/api/research_engine/evidence.py` (`APIRouter(prefix="/research-engine")`, `EVIDENCE = "/projects/{project_id}/evidence"`). Register it in `backend/src/api/research_engine/__init__.py` and `backend/src/main.py`.
- Add the schemas to `backend/src/schemas/research_engine.py` after GOO-309's.
- Modify `audit_bundle.py`: add an `_evidence` builder that returns `_sealed_part("evidence.json", "nous.academic.evidence.v1", ...)` and add it to `gather_parts` before `_prisma`.
- Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

| Route | Action | Notes |
|---|---|---|
| `GET {EVIDENCE}` | VIEW | `EvidenceOutcomeListResponse{outcomes: [{outcome_key, timepoint, tables: [EvidenceTableResponse + stale], contradictions: [{contradiction_id, status, rows, dissent, stale}], certainty: [CertaintyResponse + stale + unresolved_contradictions + dissent]}]}` |
| `GET {EVIDENCE}/tables/preview?outcome_key&timepoint&matrix_id&field_ids` | VIEW | The rows, `excluded`, `unreviewed_cells`, `stance_suggestions` and `differs_from_tip`. |
| `POST {EVIDENCE}/tables` | REVIEW or ADJUDICATE | Returns 201, or 200 when the hash equals the tip. Also 409 stale, 422 timepoint, 404 foreign matrix. |
| `POST {EVIDENCE}/contradictions` | opened/dissent: REVIEW or ADJUDICATE; resolved/acknowledged: ADJUDICATE | The route resolves with the action the body's `kind` needs. A reviewer resolving gets 403. |
| `POST {EVIDENCE}/certainty` | REVIEW | Returns 422 for a non-derived level or unresolved RoB, and 409 for a stale table or tip. |
| `GET {EVIDENCE}/export` | VIEW | A JSON attachment containing every version, including stale ones. |

**oasdiff:** six new operations, so the change is additive. **Expected: no ERR.**

**Tests** (`backend/tests/unit/api/test_evidence_routes.py`):
- `test_resolve_contradiction_reviewer_403`
- `test_certainty_owner_without_role_403`
- `test_preview_is_read_only_no_commit`
- `test_export_includes_stale_versions`

**Commit:** `feat(research): evidence, contradiction and certainty endpoints (GOO-310)`

---

### Task 6: Structural guard

**Files:**
- Create `backend/tests/unit/architecture/test_evidence_boundary.py`, an AST scan with three checks:
  - **(a)** `evidence_rules.py` does not name `confidence` or `stance`, except inside `stance_groups`.
  - **(b)** No module under `src/services/evidence/` (the stance meter) or `src/tasks/` references `evidence_service`, `EvidenceContradiction`, `OutcomeCertaintyAssessment` or the three event types. The classifier cannot write a decision.
  - **(c)** `evidence_service` never calls `db.merge`, `update(` or `delete(` on the three models.

**Commit:** `test(research): guard certainty, agreement and model confidence separation (GOO-310)`

---

### Task 7: PostgreSQL proof (one test)

**Files:**
- Create `backend/tests/integration/test_evidence_certainty_postgres.py` with `pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]`. It reuses `screening_factory` and `_upgrade` (now through `f4b6d8a0c2e3`), `seed_approved_protocol_binding` with `outcomes.declared: [{key: depressive_symptoms, timepoints: ["12 weeks"]}, {key: mean_age, timepoints: ["baseline"]}]` and `certainty: {method: grade, version: handbook-2013}`, and GOO-309's seed helpers from `test_appraisal_postgres.py`.

**`test_evidence_table_contradiction_certainty_and_selective_staleness`** runs these steps in order:
1. **Seed:**
   - Study S has reports R1 and R2 (conflicting reports of one trial), with documents D1 and D2.
   - Report R3 (no study) has document D3, and reports no outcome.
   - Report R5 has a `disputed` link and document D5.
   - A form with fields `GDS mean (intervention)` and `GDS mean (control)` at `12 weeks`, and `Mean age` at `baseline`.
   - Accepted tips: D1 and D2 disagree on `GDS mean (intervention)` (4.1 vs 4.4). D3 has none (missing). Every document has `Mean age`.
   - GOO-309 appraisals: S is adjudicated, R3 is still awaiting.
   - One `stance_classifications` pair (supporting and opposing) on D1/D3, plus one row in org B.
2. **Preview:**
   - S is one row, and its intervention cell is `conflict` with both tips.
   - R3's cells are `missing`, and D5 is in `excluded` with `study_link_unresolved`.
   - `unreviewed_cells` holds the matrix's machine cells.
   - `stance_suggestions` has exactly one group, and the org B row is absent.
3. **Create the table** (reviewer A): rows round-trip. Posting the same request again gives `200`, `replayed`, and no new row. Every cell's `accepted_value_id`, `source_hash` and `report_id` resolve by joining back to `extraction_accepted_values`, `documents` and `research_reports`.
4. **Contradiction:**
   - A opens a group on S's two tips and cites the suggestion ids, so `suggestion.origin == "model_suggestion"`.
   - A resolving gets 403.
   - Adjudicator J resolves it, then reviewer B appends `dissent`.
   - The status is `resolved`, B's dissent is listed, and the `opened` row is still there.
5. **Certainty:**
   - A non-null `risk_of_bias` rating gives 422 (R3 unresolved).
   - A request with `risk_of_bias: null` and the other ratings −1/0/0/0 succeeds with `level: null` (missing stays unknown).
   - A `level` not derived from the ratings gives 422.
   - The owner O gets 403, and foreign user F gets 404.
6. **Separate outcome:** create a `mean_age@baseline` table (T2) and a certainty assessment on it.
7. **Selective staleness:** supersede D1's accepted `GDS mean (intervention)` (GOO-304 `accept_value`).
   - The `depressive_symptoms` table T1, its certainty and the contradiction group are `stale`.
   - T2 and its certainty are **not** stale.
   - No row changed (`xmin` is unchanged). The export still contains T1's rows byte-for-byte, with `stale: true`.
8. **Changed source:** change D3's `content_text` without any writer call. T2 (which cites D3's `Mean age`) is `stale` on read, which is the derive-on-read rule.
9. **Successor:** rebuilding T1 creates a successor, and the old version stays listed as superseded.
10. **Insert-only:** UPDATE or DELETE on each table raises `55000`.
11. **Archived/deleted:** 409 on writes, 200 on reads, 404 after workspace deletion.
12. **Replay:** `replay_decisions(research_evidence, collection_id)` succeeds with a contiguous sequence.
13. **Downgrade:** only the three tables are dropped, and the GOO-309 function remains.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_evidence_certainty_postgres.py`. Without a database, report it as **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for evidence tables, contradictions and certainty (GOO-310)`

---

### Task 8: Frontend

**Files:**
- Create `frontend/src/types/api/research-evidence-contract.ts`, which aliases the new schemas.
- Modify `frontend/src/services/researchEngineService.ts` to add `listEvidence`, `previewEvidenceTable`, `createEvidenceTable`, `recordContradiction`, `assessCertainty` and `exportEvidence`.
- Create `frontend/src/components/research-engine/EvidenceTablePanel.tsx` and mount it in the `Extract` stage after `AppraisalPanel`.
  - Pick an outcome and timepoint (from the protocol's declared outcomes), a matrix and fields.
  - The preview table has one row per unit, and each cell shows its value and state. A `missing` cell reads "Not reported / missing" and a `conflict` cell reads "Reports disagree". Each cell is linked to its report, document and source revision. `unreviewed_cells` sit in a separate, collapsed "Unreviewed (machine/legacy)" section.
  - "Freeze table version" is shown to reviewers and adjudicators.
  - Contradictions list each group's members, status, explanation chain and dissent. Stance suggestions are labelled "Model suggestion — unreviewed", and "Open contradiction" is available from a suggestion.
  - The certainty form has the starting level, five rating `<select>`s (with "Unknown") and a rationale, and shows the derived level live. It also shows the unresolved contradictions and dissent next to the level.
  - Stale badges are shown on stale versions.
- Tests go in `__tests__/EvidenceTablePanel.test.tsx`:
  - `missing cell never shows agreement`
  - `suggestion labelled unreviewed`
  - `resolve hidden without adjudicator`
  - `level derived and null when unknown`

**Commit:** `feat(frontend): evidence table, contradiction review and certainty panel (GOO-310)`

---

## Mutation verification

Follow `docs/engineering/testing.md`. Record each check in the test docstring and in `docs/testing/agent-orchestration-mutation-checks.md` under a GOO-310 section.

| Guard | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| One row per `analysis_unit` in `build_rows` | key rows by `report_id` | `pytest -q backend/tests/unit/services/test_evidence_rules.py -k one_study` plus integration step 2 | S appears twice |
| The `missing` cell state | treat no tip as `value: null` | `pytest -q backend/tests/unit/services/test_evidence_rules.py -k missing` | a missing cell reports agreement |
| The adjudicator-only resolution | allow a reviewer | `pytest -q backend/tests/integration/test_evidence_certainty_postgres.py` | step 4: A resolves |
| Stale-tip exclusion in `build_rows` inputs | skip the `stale_nodes` filter | same | step 9: the rebuilt T1 includes the superseded value |
| The RoB-resolved check in `check_certainty` | skip | same | step 5: a non-null rating is accepted with R3 awaiting |
| `graph_part` edge selection | connect every table to every accepted value | same | step 7: T2 goes stale |
| The org filter on the stance query | drop it | same | step 2: the org B row appears |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_evidence_rules.py backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/services/test_appraisal_rules.py
pytest -q backend/tests/unit/services/test_release_rules.py backend/tests/unit/services/test_audit_bundle.py
pytest -q backend/tests/unit/api backend/tests/unit/architecture
(cd backend && python ../scripts/ci/check_alembic.py)      # single head f4b6d8a0c2e3
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research-engine
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists.** Report each of these as NOT RUN, never as passing:
- **PostgreSQL proof** (this test, plus GOO-309's and the R4 tests on the same SHA): needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **Methods-expert review of the certainty example:** a named expert reviews the step 5/6 certainty rows and the GRADE structure-only encoding (domains, arithmetic, starting levels) and records the verdict in the Linear closure. None is available today.
- **Live journey:** needs a deployed stack with `f4b6d8a0c2e3`, PR #1747 and the saved principals.

## Authenticated journey list for Linear closure

Run this on dev after the deploy. `alembic current` should show `f4b6d8a0c2e3 (head)`.

1. **Preview:** for the pilot outcome, two reports of one trial share one row, a study that does not report the outcome shows `missing`, and machine or legacy cells appear only under "Unreviewed". Keep the preview JSON.
2. **Freeze:** a reviewer freezes the table. Freezing again returns the same version id.
3. **Contradiction:** open a group from the model suggestion (labelled unreviewed), resolve it as the adjudicator, and add dissent as the other reviewer. All three rows are visible.
4. **Certainty:** a reviewer records GRADE ratings with one domain unknown, and the level shows "Unknown". Resolve it and record again, and the derived level appears next to the unresolved contradictions and dissent.
5. **Deny:** a reviewer resolving gets 403, the owner assessing gets 403, and a foreign user gets 404.
6. **Stale:** supersede one accepted value. Only that outcome's table and certainty show `Stale`, and the other outcome stays current. The export keeps the stale version.
7. **Bundle:** `evidence.json` is in the GOO-308 audit bundle, and `sha256sum -c SHA256SUMS` passes.

Record the SHA/PR, the CI and oasdiff links, the retained exports, the PostgreSQL junit, the mutation transcripts and the NOT RUN list.

## Seams

- **Upstream (GOO-309):** this ticket reads `current_appraisals` and cites `current_ids`. It never writes appraisals.
- **Downstream (GOO-311):** synthesis takes an `evidence_table_versions` id as its **only** input set. It reads `rows[*].cells` for the fields its config maps to arms and statistics, `excluded` for pre-execution exclusions, `form_version_id` for field units and timepoints, and `content_hash` for its input hash. GOO-311 adds `("evidence_table", t) → ("synthesis", s)` in its own `graph_part`. A certainty assessment does **not** cite a synthesis result in this ticket. `# ponytail: add certainty → synthesis citation when imprecision ratings must bind a pooled CI.`

## Out of scope

Each item below has a `ponytail:` marker at its seam:
- GRADE upgrading domains;
- summary-of-findings rendering;
- dual independent certainty assessment;
- a persisted stale flag;
- staling a table when new units appear;
- promoting stance rows into decisions;
- claim links to certainty rows;
- reconciling legacy cells (GOO-304 acceptance already does it).

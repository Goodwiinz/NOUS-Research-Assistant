# GOO-309 Versioned Study-Design Appraisal Instrument Plan (Academic R5)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A project can appraise one result (an analysis unit, an outcome and a timepoint) with exactly one instrument, the one its approved protocol names: **RoB 2, individually randomized parallel-group variant, version 2019-08-22**. Each reviewer submits an insert-only assessment that pins the instrument version, the protocol version, the study or report, its answers, its rationale and its exact evidence (GOO-304 accepted values or GOO-305 anchored observations). When the protocol mode is `dual_independent`, the API hides a peer's assessment until the required number of independent reviewers have submitted. An ADJUDICATOR who did not assess the result is the only person who can resolve a conflict. An answer that is not given stays `unknown`, and nothing derives it. A design the instrument does not cover is recorded as `not_applicable` and is never scored. Model confidence, source popularity and the legacy `QualityMark` checks never feed an appraisal.

**Architecture:**
- **One table**, `appraisal_assessments`. It is insert-only and enforced by a `BEFORE UPDATE OR DELETE` trigger. Edits and adjudications are new rows that supersede the old ones.
- **Two pure modules**:
  - `protocol_methods.py` parses the protocol snapshot's methods sections. GOO-310 and GOO-311 extend it.
  - `appraisal_rules.py` holds the frozen instrument structure, validation, the overall-judgement rule and the derived per-result status.
- **One ledger family**, `research_appraisal`, with one stream per Collection.
- **One service**, `appraisal_service.py`, and **one router**, `api/research_engine/appraisals.py`.
- **Derived status.** Whether a result is `awaiting_independent`, `agreed`, `conflict` or `adjudicated`, and whether it is `stale`, is derived on read. Nothing is stamped.
- **Staleness** reuses GOO-307's evidence graph. `draft_release_service._graph` gains the appraisal edges, and `Graph.stale_nodes()` answers "is this appraisal stale" with the same walk that stales releases.
- **Blinding** reuses GOO-302's pure predicate, `screening_rules.visible`, over a revealed-id set, and every read path goes through it.

**Tech stack:** FastAPI, SQLAlchemy async, Alembic, the PostgreSQL integration fixture (`postgres_container`, `backend/tests/integration/conftest.py:208`), openapi-typescript, Next.js with TanStack Query and Vitest.

**Stacking (read first):**
- This PR stacks on **GOO-308** (`feat/goo-308-plan-to-write-journey` @ `e5b909b56`), which carries GOO-299 → 300 → 301 → 302 → 303 → 304 → 305 → 306 → 307. It needs GOO-307 only for `draft_release_service._graph`. GOO-308 is needed only for the audit-bundle part in Task 5.
- The branch is `feat/goo-309-appraisal-instrument`, in the worktree `RAG_system-wt-goo309`.
- GOO-310 consumes `appraisal_rules.status`, `appraisal_service.current_appraisals` and `protocol_methods.declared_outcomes`. GOO-311 consumes `acquisition_service.document_reports` and `identity_service.analysis_unit`. All of these are defined here.

### Consumed names (each verified on `e5b909b56`)

| Consumed | Name (path:line) | Property relied on |
|---|---|---|
| Study and report identity (GOO-299) | `ResearchStudy` (`backend/src/models/research_report.py:41`), `ResearchReport.study_id`, `.study_link_status`, `.merged_into_report_id` (`:54-72`) | `study_link_status ∈ {proposed, confirmed, disputed}`, and it is NULL iff `study_id` is NULL (`ck_research_report_study_link_pair`). |
| Scoped report load | `identity_service.live_reports` (`backend/src/services/research_engine/identity_service.py:163`) | A foreign id gets 404, and a merged report gets 409. |
| Current protocol | `identity_service.current_protocol_version_id` (`:103`), `ResearchProtocol.current_approved_version_id` (`backend/src/models/research_protocol.py:85`) | The approved version, or `None`. |
| Idempotent replay | `identity_service._replayed_event` (`:76`) | An identical retry returns the prior event, and a reused key with a different body gives 409. |
| Methods sections | `ProtocolSnapshot.appraisal_synthesis`, `.outcomes`, `.reviewer_mode` (`backend/src/schemas/research_engine.py:285-305`) | These are free-form bounded dicts, so the keys this plan adds need no schema change. |
| Reveal predicate (GOO-302) | `screening_rules.visible` (`backend/src/services/research_engine/screening_rules.py:140`), `MODES`/`REQUIRED` (`:12-14`), and the pattern in `screening_service.visible_observation_ids` (`backend/src/services/research_engine/screening_service.py:317`) | A viewer sees their own rows plus the revealed set, and nothing else. |
| Accepted values (GOO-304) | `ExtractionAcceptedValue` (`backend/src/models/extraction_matrix.py:234`); a tip is a row nobody supersedes (`extraction_forms_service._is_tip`, `backend/src/services/research/extraction_forms_service.py:293`) | They key on `(document_id, field_id)` and pin `source_hash` and `text_sha256`. |
| Anchors (GOO-305) | `ExtractionObservation.anchor_status`, `.text_sha256` (`extraction_matrix.py:182-188`) | `verified` and `ambiguous` carry offsets into a pinned text. |
| Full text to report (GOO-303) | `ResearchFulltextRequest.report_id`, `ResearchFulltextAttempt.document_id`, `.outcome` (`backend/src/models/research_fulltext.py:40-80`), `acquisition_service._is_head` and the merge walk in `retrieved_report_ids` (`backend/src/services/research_engine/acquisition_service.py:113,378`) | A `retrieved` head attempt is how a document becomes a report. |
| Evidence graph (GOO-307) | `release_rules.node`, `source_node`, `dependents` (`backend/src/services/research/release_rules.py:56-63,146`), `draft_release_service.Graph`, `_graph` (`backend/src/services/research/draft_release_service.py:111,129`) | The walk is pure. `stale_nodes() = changed ∪ dependents(edges, changed)`. |
| Ledger | `lock_aggregate_stream` (`backend/src/services/research_decisions/ledger.py:1089`), `append_decision` (`:1092`), `replay_decisions` (`:1191`), `_Family` (`:376`), `_RATIONALE_EVENTS` (`:339`), `_FAMILIES` (`:1895`) | The append is caller-owned and never commits. |
| Roles | `ResearchAction.REVIEW`, `.ADJUDICATE` (`backend/src/services/research_engine/project_access.py:28-47`), `resolve_project` (`:186`) | It takes Workspace SHARE, then Collection UPDATE, then reloads roles after the lock. Archived projects give 409 on writes. Owners and editors get no decision role. |
| Bundle | `audit_bundle._sealed_part` (`backend/src/services/research_engine/audit_bundle.py:80`), `gather_parts` (`:290`) | Each part is one existing reader. |
| Legacy marks | `QualityMark` (`backend/src/services/research_engine/verification.py:9`, `schemas/research_engine.py:1389`) | They are never read here and never backfilled. |
| Migration helpers | `_deny_data_api` (`backend/alembic/versions/e1f3a5c7d9b2_create_screening_queues.py:20-32`), the trigger pattern (`backend/alembic/versions/a3c5e7f901b2_create_research_decision_ledger.py:129-144`) | Both are copied and never imported. |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| **Which instrument** | **RoB 2 (Cochrane risk-of-bias tool for randomized trials), individually randomized parallel-group trials, "effect of assignment" variant, version `2019-08-22`.** Only its **structure** is encoded: domain ids and short names, signalling-question **ids** (1.1–1.3, 2.1–2.7, 3.1–3.4, 4.1–4.5, 5.1–5.3), the response vocabulary `Y, PY, PN, N, NI, NA`, the judgement vocabulary `low, some_concerns, high`, and the rule that the overall judgement is at least the worst domain judgement. No signalling-question wording, guidance text or domain algorithm ships. The spec records `licence: "CC BY-NC-ND 4.0 (riskofbias.info); structure only, no text or algorithms"` and the source URL. | The pilot corpus is randomized trials of supervised exercise in older adults (`evals/academic-journey-v1/corpora/known-answer.json:6,15-24`), and RoB 2 is the standard instrument for that design. Its licence forbids derivatives, so the plan encodes names and ids only, as the ticket requires. No permissively licensed instrument fits randomized trials as well. |
| Other designs | `DESIGNS = (randomized_parallel_group, randomized_cluster, randomized_crossover, non_randomized_intervention, cohort, case_control, cross_sectional, other)`. RoB 2 `applies_to = {randomized_parallel_group}`. Any other design must be submitted as `applicability="not_applicable"` with empty domains and a NULL overall, and anything else gets 422 `RoB 2 (parallel-group) does not apply to {design}`. The cluster and crossover RoB 2 variants are not encoded. | An incompatible instrument can never score a study silently. The not-applicable row is explicit and attributed. `# ponytail: encode the cluster/crossover variants or ROBINS-I when a protocol needs them.` |
| Unit of assessment | RoB 2 assesses a **result**, so the key is `(target_key, outcome_key, timepoint, instrument_key, instrument_version)`. `target_key` is `study:<id>` or `report:<id>` from `identity_service.analysis_unit(report)`. A report with a `confirmed` study appraises the study. A report with no study link is its own unit. A `proposed` or `disputed` link gives 409 `Study link unresolved`. A report with a confirmed study submitted as `report_id` gives 422 `Report belongs to a study; appraise the study`. | Several reports of one study get one appraisal. GOO-311 needs the same unit rule, so it lives in one pure function. |
| Where the instrument and mode come from | The approved protocol version's snapshot declares `appraisal_synthesis.appraisal = {"instrument": "rob2", "version": "2019-08-22", "mode": "single" \| "dual_independent"}` and `outcomes.declared = [{"key", "label", "timepoints": [..]}]`. They are parsed by the pure `protocol_methods.appraisal_method(snapshot)` and `declared_outcomes(snapshot)`, which raise `ValueError`. The service turns that into 409 `Protocol declares no appraisal instrument` or `... no outcomes`. `mode` must be in `screening_rules.MODES`. | These sections are already free-form, required dicts (`schemas/research_engine.py:290-292`), so the change is additive with no OpenAPI change. Existing `{"method": "narrative"}` protocols keep working and simply cannot appraise. |
| Instrument pinning | The request names `instrument_key` and `instrument_version`, and they must equal the protocol's, otherwise 422 `Instrument is not the protocol's`. The row stores both plus `instrument_spec_hash = canonical_json_sha256(SPEC)` (`contracts.py:38`), and `protocol_version_id` must be the current approved version (409 `Protocol version is stale; reload`). | An export can prove which structure the answers were validated against. |
| Answers and missingness | `domains` is `{D1..D5: {judgment: low\|some_concerns\|high\|null, signals: {"1.1": Y\|PY\|PN\|N\|NI\|NA\|null, ...}, rationale: str\|null, evidence: [{kind, id}]}}`. Missing domain or signal keys are filled in as `null` by `appraisal_rules.normalize`, and `null` means `unknown`. A non-null judgement needs a rationale (1–4000 characters). **`overall` must be `null` if any domain is `null`.** Otherwise it must be ≥ the worst domain (`low < some_concerns < high`), so a reviewer may escalate (RoB 2 allows "high" for several some-concerns domains) but may never lower it. | Missing answers stay unknown, and there is no scoring engine. The only rule encoded is the ordering, which is the instrument's published structure. |
| Evidence references | Each item is `{"kind": "accepted_value" \| "observation", "id"}`, with at most 20 per domain. An `accepted_value` must be a GOO-304 tip in a matrix of this project, and an `observation` must have `anchor_status ∈ {verified, ambiguous}` (GOO-305). In both cases the document must map to **this** `target_key` through `acquisition_service.document_reports`, otherwise 422 `Evidence is not from this study`. `input_hash = canonical_json_sha256({spec_hash, protocol_version_id, evidence pins})`, where the pins are `(id, document_id, source_hash, text_sha256)`. | Every judgement points at exact, versioned text. Evidence is optional, because "no information" is a legitimate RoB 2 basis. |
| Document → report | This adds `acquisition_service.document_reports(db, collection_id) -> dict[UUID, UUID]`: `retrieved` head attempts (`_is_head`) give `document_id → request.report_id`, and the report follows the merge chain the same way `retrieved_report_ids` does (`:378-440`). It is one query plus the existing merge walk. | Only GOO-303's attempt records a document as a report's full text. Any other document has no study, so it cannot be appraisal evidence. |
| Independence (reveal) | Revealed keys are those where the number of distinct `independent` tip assessors is ≥ `screening_rules.REQUIRED[mode]`. `revealed_ids` is every row (any kind, including superseded ones) of a revealed key. The one read predicate is `visible_appraisal_ids(db, collection_id, viewer_id)`, which returns the viewer's own rows ∪ `revealed_ids`, applied through `screening_rules.visible(row.assessor_id, row.id, viewer, revealed)`. The list, the export and the audit bundle all filter through it. The bundle has no viewer, so it passes `viewer_id=None` and gets only `revealed_ids`. | This is GOO-302's reveal pattern, enforced at the API. A peer's submission, its existence and its answers stay hidden until the viewer's own answer is in. Role assignment alone does not give that. |
| Edits after reveal | Before reveal, an assessor may supersede their own tip (successor row, 409 `Appraisal is stale; reload` if `supersedes_assessment_id` is not their tip). After reveal in `dual_independent` mode, independent successors get 409 `Revealed; changes go through adjudication`. In `single` mode the assessor may keep superseding. | This prevents "saw the peer, changed my answer to agree". The first revealed answers stay the independent record. |
| Status (derived) | `appraisal_rules.status(mode, independent_tips, adjudicated_tip)` returns `awaiting_independent` when fewer tips exist than required, `adjudicated` when the adjudicated tip's `resolves_assessment_ids` equals the current independent tip ids, `agreed` when every tip has equal `applicability`, `study_design`, domain judgements and `overall` (single mode is always `agreed`), and otherwise `conflict`. `unresolved_domains` lists the domains that are `null` in the governing row, or that differ between tips in a conflict. `current_ids` is the governing row ids. | No status column means no pointer that can drift. Signal answers may differ without a conflict, because the judgement is what the ticket reconciles. |
| Adjudication | `ResearchAction.ADJUDICATE` (the ADJUDICATOR role, an explicit assignment). It is allowed only when the status is `conflict`, or `adjudicated` with `supersedes_assessment_id` equal to the adjudicated tip. `resolves_assessment_ids` must equal the current independent tip ids (409 `Appraisal is stale; reload`). **The adjudicator must not be one of the assessors of those tips** (403 `Adjudicator assessed this result`). A rationale is required. The adjudicator submits full answers that are validated like any submission. | "Only assigned adjudicators resolve conflicts." The history keeps the disagreement: both independent rows and the adjudication stay, each attributed. |
| Machine suggestions | None in this ticket. No producer is built and no suggestion table is added. An AST guard (Task 6) fails if `src/tasks/`, `src/services/agent/` or any extraction worker references `appraisal_service` or `AppraisalAssessment`, or if `appraisal_rules`/`appraisal_service` read `confidence`, `citation_count`, `StanceClassificationModel` or `QualityMark`. | "Keep machine suggestions separate" holds when none exist. `# ponytail: add an appraisal_suggestions table beside, never inside, appraisal_assessments when a model proposes answers.` |
| Legacy quality marks | No backfill. The migration inserts nothing, and `verification.py` stays untouched. | The ticket forbids it. The PostgreSQL proof asserts zero rows after upgrade on a database that holds `research_steps` rows with `quality_marks`. |
| Staleness | This extends GOO-307's `_graph` through `appraisal_service.graph_part(db, collection_id) -> (edges, changed)`, called from `_graph` with a local import to avoid a cycle. The edges are `("accepted", id) → ("appraisal", row)`, `source_node(obs.document_id, obs.source_hash, obs.text_sha256) → ("appraisal", row)` for observation evidence, and `("protocol", protocol_version_id) → ("appraisal", row)`. `changed` adds every superseded appraisal id and `("protocol", v)` for each approved version that is no longer current. Staleness is shown on read as `stale: true` and is never written. | This is the same walk and the same derive-on-read rule as GOO-305 and GOO-307. No new mechanism, and a changed source or form reaches only the appraisals that cite it. |
| Insert-only | The table has no `UPDATE` or `DELETE` path in code, and a trigger `trg_appraisal_assessments_insert_only` calls a new generic `prevent_research_insert_only_mutation()` (SQLSTATE `55000`). GOO-310 and GOO-311 reuse that function. FKs are `RESTRICT`. | "Submitted assessments retain instrument/input versions and actor; edits create successors" is enforced by the database. |
| Write order | 1. The route calls `resolve_project(REVIEW\|ADJUDICATE)` (post-lock role reload). 2. `lock_aggregate_stream(research_appraisal, collection_id)`. 3. `_replayed_event`. 4. Protocol and instrument checks. 5. Target and unit. 6. `appraisal_rules.validate`. 7. Evidence checks. 8. Tip and reveal checks. 9. Insert, then `append_decision`, then **one commit**. An `IntegrityError` on the initial-row partial uniques rolls back and gives 409 `Appraisal is stale; reload` (the GOO-301 `_is_unique_violation` shape). | This is the proven lock order of every R4 writer. Role revocation queues on the same Collection lock. |

**Migration head:** new revision `e2a4c6b8d0f1_create_appraisal_assessments.py` (12 characters, within the 32 allowed by `scripts/ci/check_alembic.py`). `down_revision = "d7f9b1c3e5a8"` (GOO-307). The chain is 307 `d7f9b1c3e5a8` → 309 `e2a4c6b8d0f1` → 310 `f4b6d8a0c2e3` → 311 `a6c8e0b2d4f5`. **Re-pointing rule:** `down_revision` always names the direct stack parent. After a rebase, set it to the single head that `(cd backend && python ../scripts/ci/check_alembic.py)` reports for the parent. Never add a merge revision. GOO-308 has no migration. The migration imports nothing from `src` and copies `_deny_data_api`.

---

### Task 1: Pure rules (`protocol_methods.py`, `appraisal_rules.py`)

**Files:**
- Create `backend/src/services/research_engine/protocol_methods.py`.
- Create `backend/src/services/research_engine/appraisal_rules.py`.
- Create `backend/tests/unit/services/test_appraisal_rules.py`.

```python
# protocol_methods.py: stdlib only; GOO-310/311 add certainty_method / synthesis_selection here.
def appraisal_method(snapshot: Mapping[str, Any]) -> tuple[str, str, str]   # (instrument, version, mode)
def declared_outcomes(snapshot: Mapping[str, Any]) -> dict[str, tuple[str, ...]]  # key -> timepoints

# appraisal_rules.py: stdlib + screening_rules only (the ledger imports it)
RESPONSES = ("Y", "PY", "PN", "N", "NI", "NA")
JUDGMENTS = ("low", "some_concerns", "high")          # ordered
DESIGNS = (...8 values...)
SPEC = {"key": "rob2", "version": "2019-08-22", "variant": "individually_randomized_parallel_group/assignment",
        "applies_to": ["randomized_parallel_group"], "licence": "...", "source": "https://www.riskofbias.info",
        "domains": {"D1": {"name": "Randomization process", "signals": ["1.1", "1.2", "1.3"]}, ...D5}}
SPEC_HASH: str                                         # canonical_json_sha256(SPEC), computed once
def spec(key: str, version: str) -> Mapping[str, Any]   # ValueError: unknown instrument
def normalize(domains: Mapping[str, Any]) -> dict       # fill missing keys with null
def validate(spec, design, applicability, domains, overall) -> dict   # ValueError -> 422
def overall_floor(domains) -> str | None                # None if any judgement is null
def status(mode, independent_tips: Sequence[Row], adjudicated_tip: Row | None) -> Status
    # Status(value, unresolved_domains, current_ids)
def revealed_keys(mode, tips_by_key) -> set[Key]
```

**Tests (write first; they fail on the import):**
- `test_protocol_without_appraisal_or_outcomes_raises`, and `test_mode_must_be_screening_mode`
- `test_spec_hash_is_stable_and_spec_has_no_question_text`: no value in `SPEC` is longer than 60 characters, and no key named `text`, `question` or `guidance` exists (the licence guard).
- `test_non_randomized_design_must_be_not_applicable_with_empty_domains`
- `test_unknown_signal_or_domain_or_response_rejected`
- `test_missing_answers_stay_null_and_overall_must_be_null`
- `test_overall_may_escalate_but_not_go_below_worst_domain`
- `test_judgment_without_rationale_rejected`
- `test_status_awaiting_agreed_conflict_adjudicated`: signals differ but judgements agree gives `agreed`, one domain differing gives `conflict` with `unresolved_domains == ["D3"]`, and an adjudication resolving stale tips does not count.
- `test_single_mode_reveals_on_first_submission`

**Run:** `pytest -q backend/tests/unit/services/test_appraisal_rules.py`
**Commit:** `feat(research): pure RoB 2 structure, validation and appraisal status (GOO-309)`

---

### Task 2: Model + migration

**Files:**
- Create `backend/src/models/research_appraisal.py` (`AppraisalAssessment`) and export it from `backend/src/models/__init__.py`.
- Create `backend/alembic/versions/e2a4c6b8d0f1_create_appraisal_assessments.py`.
- Modify `backend/tests/integration/test_screening_queue_postgres.py`: add `appraisal_assessments` first in `_REBUILT_TABLES` (`:81`), and add the filename last in `_upgrade`'s list (`:120-128`).

**`appraisal_assessments`:**
- `id` UUID PK.
- `collection_id` FK `collections` RESTRICT.
- `protocol_version_id` FK `research_protocol_versions` RESTRICT, NOT NULL.
- `instrument_key` VARCHAR(32), `instrument_version` VARCHAR(32), `instrument_spec_hash` CHAR(64).
- `study_id` FK `research_studies` NULL and `report_id` FK `research_reports` NULL, with `CHECK ((study_id IS NULL) <> (report_id IS NULL))`.
- `target_key` VARCHAR(80), plus a PostgreSQL-only `CHECK (target_key = COALESCE('study:' \|\| study_id::text, 'report:' \|\| report_id::text))` (`.ddl_if(dialect="postgresql")`, the GOO-304 pattern at `extraction_matrix.py:278-282`).
- `outcome_key` VARCHAR(100), `timepoint` VARCHAR(100).
- `study_design` VARCHAR(40).
- `applicability` VARCHAR(16), `CHECK IN ('applicable','not_applicable')`.
- `domains` JSONB NOT NULL.
- `overall` VARCHAR(16) NULL, `CHECK (overall IS NULL OR overall IN ('low','some_concerns','high'))` and `CHECK (applicability = 'applicable' OR overall IS NULL)`.
- `kind` VARCHAR(16), `CHECK IN ('independent','adjudicated')`.
- `actor_role` VARCHAR(16), `CHECK (actor_role IN ('reviewer','adjudicator'))` and `CHECK ((kind = 'adjudicated') = (actor_role = 'adjudicator'))`.
- `assessor_id` FK `users` RESTRICT.
- `resolves_assessment_ids` JSONB NULL, `CHECK ((kind = 'adjudicated') = (resolves_assessment_ids IS NOT NULL))`.
- `rationale` TEXT NULL, `CHECK (kind <> 'adjudicated' OR rationale IS NOT NULL)`.
- `input_hash` CHAR(64).
- `supersedes_assessment_id` FK self RESTRICT NULL, `UNIQUE`.
- `created_at`.

**Indexes:**
- `uq_appraisal_initial_independent (collection_id, assessor_id, target_key, outcome_key, timepoint, instrument_key, instrument_version) WHERE supersedes_assessment_id IS NULL AND kind = 'independent'`
- `uq_appraisal_initial_adjudicated (collection_id, target_key, outcome_key, timepoint, instrument_key, instrument_version) WHERE supersedes_assessment_id IS NULL AND kind = 'adjudicated'`

Both declare `postgresql_where=` and `sqlite_where=`. Add `idx_appraisal_collection_key (collection_id, target_key, outcome_key, timepoint)`.

**Migration:**
- Create the table, call `_deny_data_api`, `CREATE FUNCTION prevent_research_insert_only_mutation()` (raising `'research rows are insert-only'` with ERRCODE `55000`), and `CREATE TRIGGER trg_appraisal_assessments_insert_only BEFORE UPDATE OR DELETE`.
- **No data statements.**
- `downgrade()` drops the trigger, the table and the function.

**Check:**
- `(cd backend && python ../scripts/ci/check_alembic.py)` reports single head `e2a4c6b8d0f1`.
- `alembic upgrade head --sql | grep -c appraisal_assessments` is > 0 (offline, no Docker).
- `alembic upgrade head --sql | grep -ci "insert into"` for this revision's section is 0.

**Commit:** `feat(research): insert-only appraisal_assessments table (GOO-309)`

---

### Task 3: Ledger family `research_appraisal`

**Files:**
- Modify `backend/src/services/research_decisions/ledger.py`:
  - Add the vocabulary next to `_RELEASE_*` (`:313-338`).
  - Add `appraisal.adjudicated` to `_RATIONALE_EVENTS` (`:339`).
  - Add `_validate_appraisal_payload` after `_validate_release_payload` (`:993`) and `_validate_appraisal_transitions` after `_validate_release_transitions` (`:1841`).
  - Add the `_FAMILIES` entry (`:1895`, `requires_subject_version=False`, subject type `appraisal_assessment`).
  - Update the docstring family list (`:3-6`).
- Modify `backend/tests/unit/services/test_research_decision_ledger.py`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `appraisal.submitted` | `collection_id, assessment_id, supersedes_assessment_id, target_key, outcome_key, timepoint, instrument_key, instrument_version, instrument_spec_hash, protocol_version_id, mode, study_design, applicability, overall, unresolved_domains, input_hash` | `reviewer` |
| `appraisal.adjudicated` | the same keys plus `resolves_assessment_ids` | `adjudicator` |

**Replay rules** (they import only the pure `appraisal_rules`, as the ledger does with `screening_rules`):
1. Every `collection_id` equals the `aggregate_id`.
2. `submitted` is by a `reviewer`, and its `supersedes_assessment_id` is that assessor's previous tip for the key, or `None` if the assessor has no tip.
3. In `dual_independent` mode, no `submitted` successor follows the event that reached the required count for its key.
4. `adjudicated` is by an `adjudicator` who is not an `actor_user_id` of any event named in `resolves_assessment_ids`, and those ids equal the independent tips at that point in the stream.

**Tests:**
- `test_appraisal_replay_rejects_post_reveal_independent_edit`
- `test_appraisal_replay_rejects_self_adjudication`
- `test_appraisal_replay_rejects_resolving_stale_tips`
- `test_appraisal_payload_keys_exact`

**Commit:** `feat(research): research_appraisal decision family and replay rules (GOO-309)`

---

### Task 4: Service (`appraisal_service.py`) + unit helpers + graph part

**Files:**
- Create `backend/src/services/research_engine/appraisal_service.py`.
- Modify `backend/src/services/research_engine/identity_service.py`: add the pure `analysis_unit(report) -> str | None`. It returns `study:<id>` for a `confirmed` link, `report:<id>` for no link, and `None` for `proposed`/`disputed`. A merged report never reaches it, because `live_reports` refuses merged reports.
- Modify `backend/src/services/research_engine/acquisition_service.py`: add `document_reports`, using the merge walk that `retrieved_report_ids` already has. Extract that walk into a private `_final_report(merged, report_id)` that both call, with no behavior change.
- Modify `backend/src/services/research/draft_release_service.py:129-196`: after the release edges, `from src.services.research_engine import appraisal_service` (local import), then `part_edges, part_changed = await appraisal_service.graph_part(db, collection_id)`, `edges += part_edges`, `changed |= part_changed`, all **before** `_changed_sources`.

```python
AGGREGATE_TYPE = "research_appraisal"; SUBJECT_TYPE = "appraisal_assessment"
async def document_units(db, collection_id) -> dict[UUID, str]          # document_id -> target_key (unresolved omitted)
async def visible_appraisal_ids(db, collection_id, viewer_id: UUID | None) -> set[UUID]   # THE predicate
async def submit(db, context, actor_id, data: AppraisalSubmit) -> tuple[AppraisalResponse, bool]      # REVIEW; commits once
async def adjudicate(db, context, actor_id, data: AppraisalAdjudicate) -> tuple[AppraisalResponse, bool]  # ADJUDICATE
async def list_appraisals(db, context, viewer_id) -> AppraisalListResponse  # VIEW; per key status + visible rows + stale
async def current_appraisals(db, collection_id) -> dict[Key, Status]        # revealed keys only (GOO-310 reads this)
async def graph_part(db, collection_id) -> tuple[list[Edge], set[Node]]
async def export_package(db, context, viewer_id: UUID | None) -> dict       # schema nous.academic.appraisal.v1
```

- **`list_appraisals` for an unrevealed key a viewer has not submitted to** shows only `{key, status: "awaiting_independent", mine: false}`, with no count of submitted peers. Revealing the count would tell reviewer A that B has submitted (the same reason GOO-308's journey counts resolutions only).
- **`stale`** is `("appraisal", id) in (await _graph(...)).stale_nodes()`. `_graph` is reused and not re-implemented.

**Tests** (unit, service-level with a fake session where the R4 service tests do the same, otherwise covered by Task 8):
- `test_document_reports_follows_merge_chain`
- `test_analysis_unit_confirmed_none_proposed`

**Commit:** `feat(research): appraisal submit, adjudicate, reveal predicate and graph edges (GOO-309)`

---

### Task 5: API + contracts + bundle part

**Files:**
- Create `backend/src/api/research_engine/appraisals.py` (`APIRouter(prefix="/research-engine")`, `APPRAISALS = "/projects/{project_id}/appraisals"`). Register it in `backend/src/api/research_engine/__init__.py` and `backend/src/main.py` next to `research_engine_journey_router` (`main.py:88,691`).
- Add the schemas to `backend/src/schemas/research_engine.py` after `JourneyResponse` (`:779`).
- Modify `backend/src/services/research_engine/audit_bundle.py`: add an `_appraisal` builder that returns `_sealed_part("appraisal.json", "nous.academic.appraisal.v1", body, empty)` from `export_package(db, context, viewer_id=None)`, placed before `_prisma` in `gather_parts` (`:295`). Also modify `verify_bundle` so the part joins the recomputed `body_sha256` set.
- Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

| Route | Action | Notes |
|---|---|---|
| `GET {APPRAISALS}` | VIEW | `AppraisalListResponse{instrument: {key, version, spec_hash, mode, domains: [{id, name, signals}], responses, judgments, applies_to, licence}, results: [{target_key, study_id, report_id, outcome_key, timepoint, status, unresolved_domains, stale, rows: [AppraisalResponse]}]}`. `rows` are filtered by the predicate. |
| `POST {APPRAISALS}` | REVIEW | `AppraisalSubmit{protocol_version_id, instrument_key, instrument_version, study_id?, report_id?, outcome_key, timepoint, study_design, applicability, domains, overall?, supersedes_assessment_id?, idempotency_key(1..240)}`. Returns 201, or 200 on replay. Errors: 403 without the REVIEWER role, 404 for a foreign target, 409 for stale, revealed, unresolved link or archived, 422 for validation. |
| `POST {APPRAISALS}/adjudications` | ADJUDICATE | `AppraisalAdjudicate{...submit fields..., resolves_assessment_ids, rationale(1..4000)}`. Returns 403 for a role-less user or a self-adjudicator, and 409 for no conflict or stale tips. |
| `GET {APPRAISALS}/export` | VIEW | `Response(application/json, attachment; filename="appraisal-{project_id}.json")`, the claims export shape (`backend/src/api/research/claims.py:56-74`). |

**oasdiff:** four new operations and new schemas, so the change is additive. **Expected: no ERR.**

**Tests** (`backend/tests/unit/api/test_appraisal_routes.py`, dependency-overridden like `test_draft_release_routes.py`):
- `test_submit_owner_without_role_403_viewer_404`
- `test_adjudicate_reviewer_403`
- `test_list_hides_unrevealed_peer_rows_and_counts`
- `test_export_content_disposition`

**Commit:** `feat(research): appraisal endpoints, export and audit-bundle part (GOO-309)`

---

### Task 6: Structural guard

**Files:**
- Create `backend/tests/unit/architecture/test_appraisal_boundary.py`. It is an AST scan (the pattern of `test_release_boundary.py`) that checks three things:
  - **(a)** `src/tasks/`, `src/services/agent/`, `extraction_matrix_service.py` and `draft_generation_service.py` never reference `appraisal_service`, `AppraisalAssessment` or `appraisal.submitted`.
  - **(b)** `appraisal_rules.py` and `appraisal_service.py` never name `confidence`, `citation_count`, `cited_by`, `StanceClassificationModel`, `QualityMark` or `verification`.
  - **(c)** `submit` and `adjudicate` take `context: ProjectContext`.

**Commit:** `test(research): guard that automation and model signals cannot appraise (GOO-309)`

---

### Task 7: Gold examples (pure)

**Files:**
- Create `backend/tests/fixtures/appraisal/rob2_gold_v1.json`. Its header is `{"instrument": "rob2", "version": "2019-08-22", "spec_hash": <SPEC_HASH>, "expert_reviewed": false, "reviewer": null}` and it holds five cases:
  1. all low;
  2. one high domain with the rationale "outcome assessors aware of allocation", which gives overall high;
  3. two some-concerns domains escalated to overall high by the reviewer;
  4. D3 left unanswered, so the overall is NULL and `unresolved_domains = ["D3"]`;
  5. a cohort design recorded as `not_applicable`.

  Each case holds its answers, the expected `overall`, `unresolved_domains` and the explanation strings.
- Create `backend/tests/unit/services/test_appraisal_gold.py`. It checks that each case passes `validate`, reproduces `overall_floor` and `unresolved_domains`, and keeps explanations byte-identical after a `normalize` round trip. A case with `overall` below the floor must be rejected (a negative control).

`expert_reviewed` stays `false` until a methods expert signs the file (the NOT RUN list). The test asserts the header, so a signed file must also set `reviewer`.

**Run:** `pytest -q backend/tests/unit/services/test_appraisal_gold.py`
**Commit:** `test(research): RoB 2 gold examples with unresolved states (GOO-309)`

---

### Task 8: PostgreSQL proof (one test)

**Files:**
- Create `backend/tests/integration/test_appraisal_postgres.py` with `pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]`. It reuses `screening_factory` and `_upgrade` from `test_screening_queue_postgres.py` (now including `e2a4c6b8d0f1`) and `seed_approved_protocol_binding` (`backend/tests/integration/research_engine_postgres_support.py:143`), with a snapshot that declares `appraisal: {instrument: rob2, version: 2019-08-22, mode: dual_independent}` and `outcomes.declared: [{key: depressive_symptoms, timepoints: ["12 weeks"]}]`.

**`test_appraisal_independence_adjudication_and_staleness`** runs these steps in order:
1. **Seed** (org A project, org B user F):
   - Reports R1 and R2 confirmed to study S, and R3 with no study link. R4 is `proposed`.
   - D1 is retrieved for R1 and D3 for R3, through GOO-303 attempts.
   - A GOO-304 matrix with an accepted `Blinding` value on D1, and a GOO-305 `verified` observation on D1.
   - Users: owner O (no roles), reviewers A and B, adjudicator J, and J2 (who holds both ADJUDICATOR and REVIEWER), plus a role-less viewer V.
   - One `research_steps` row with non-empty `quality_marks`.
2. **Legacy:** after `upgrade()`, `count(*) FROM appraisal_assessments` is 0.
3. **Rejections:**
   - O and V get 403 on submit, and F gets 404.
   - `instrument_key="robins-i"` gives 422, and `report_id=R1` gives 422 (belongs to S).
   - R4 gives 409, and a stale `protocol_version_id` gives 409.
   - Evidence from D3 on target S gives 422, and a cohort design scored `applicable` gives 422.
4. **Blind:** A submits for S (D3 null, overall null).
   - B's `GET` shows S as `awaiting_independent` with no A row and no count.
   - A's `GET` shows only A's row.
   - The export as B and the audit bundle `appraisal.json` both lack A's row.
5. **Reveal and conflict:** A supersedes their own row once (a successor with the same key), then B submits with D4 different.
   - Status is `conflict`, and `unresolved_domains ⊇ {D3, D4}`.
   - Both rows are visible to V, and so is A's superseded row.
   - A's further successor gets 409 `Revealed; changes go through adjudication`.
6. **Adjudication:**
   - B has no ADJUDICATOR role, so B gets 403.
   - J adjudicating with the wrong `resolves_assessment_ids` gets 409.
   - J's adjudication succeeds and the status is `adjudicated`.
   - Both independent rows remain, each attributed.
7. **Self-adjudication and not applicable:** on R3, A submits `cohort` / `not_applicable` and J2 submits `randomized_parallel_group` / `applicable`, which is a conflict on applicability.
   - J2 adjudicating R3 gets 403 `Adjudicator assessed this result`.
   - J adjudicates it as `not_applicable`, and the row is stored with `domains = {}` and `overall IS NULL`.
8. **Round trip:** in a new session, `list_appraisals` and `export_package` return the same `instrument_spec_hash`, `protocol_version_id`, `input_hash` and `assessor_id` values as written. The export's `body_sha256` is stable across two calls.
9. **Insert-only:** raw `UPDATE appraisal_assessments SET overall='low'` and `DELETE` both raise SQLSTATE `55000`.
10. **Staleness is selective:** supersede the `Blinding` accepted value on D1 (GOO-304 `accept_value`). J's adjudication and B's row (which cites it) are `stale`, and A's R3 row (which cites nothing on D1) is not. No row changed (`xmin` is unchanged).
11. **Archived and deleted:** archiving the Collection gives 409 on submit and 200 on `GET`. Soft-deleting the Workspace gives 404.
12. **Revocation race:** A holds the REVIEW locks while the owner revokes A's REVIEWER role. The revoker blocks (`_wait_until_blocked`, `test_research_authorization_concurrency.py`), and after A commits the revocation completes. With the order reversed, A gets 403, and there is no row and no event.
13. **Replay:** `replay_decisions(research_appraisal, collection_id)` succeeds and its sequence is contiguous.
14. **Downgrade:** `e2a4c6b8d0f1.downgrade()` drops only the table and the function.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_appraisal_postgres.py`. Without a database, report it as **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for blind appraisal, adjudication and staleness (GOO-309)`

---

### Task 9: Frontend

**Files:**
- Create `frontend/src/types/api/research-appraisal-contract.ts`, which aliases `AppraisalListResponse`, `AppraisalResponse`, `AppraisalSubmit` and `AppraisalAdjudicate` (the pattern of `research-screening-contract.ts`).
- Modify `frontend/src/services/researchEngineService.ts` to add `listAppraisals`, `submitAppraisal`, `adjudicateAppraisal` and `exportAppraisals`.
- Create `frontend/src/components/research-engine/AppraisalPanel.tsx` and mount it in `ProjectWorkflow.tsx`'s `Extract` stage (`:239-243`) with `roles` and `readOnly={archived}`. No journey stage is added.
  - One card per result shows the unit label, outcome and timepoint, a status badge (theme tokens with `aria-label`) and the unresolved domains listed by name.
  - Rows show the assessor, `instrument key@version`, each domain's judgement and rationale, and the evidence as the accepted-value display plus the anchor quote from GOO-305's evidence read.
  - **Reviewer form:** a design `<select>`, an applicability radio, and per domain a judgement `<select>` whose empty option reads "Unknown". Signalling answers are a `<select>` per id (with "Unanswered"), with no question text. Rationale is required when a judgement is set. Evidence is chosen from the unit's accepted values.
  - **Adjudicator form:** shown only on `conflict` and only when the roles include adjudicator.
  - Every POST sends `idempotency_key: crypto.randomUUID()`, and a 409 refetches and shows the detail.
- Tests go in `frontend/src/components/research-engine/__tests__/AppraisalPanel.test.tsx`:
  - `shows awaiting without peer details`
  - `unknown judgement posts null`
  - `adjudicate hidden without role`
  - `lists unresolved domains`

**Run:** `pnpm --dir frontend exec vitest run src/components/research-engine/__tests__/AppraisalPanel.test.tsx`
**Commit:** `feat(frontend): appraisal panel with unresolved domains and evidence (GOO-309)`

---

## Mutation verification

Follow `docs/engineering/testing.md` (Mutation verification). Record each check in the test docstring and in `docs/testing/agent-orchestration-mutation-checks.md` under a GOO-309 section.

| Guard (file:line set during implementation) | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| `visible_appraisal_ids` filtering in `list_appraisals` | return all ids | `pytest -q backend/tests/integration/test_appraisal_postgres.py` | step 4: B sees A's row |
| The revealed-edit refusal in `submit` | skip | same | step 5: A's post-reveal successor is 201 |
| The self-adjudication check | skip | same | step 7: J2 adjudicates R3 |
| The `resolves_assessment_ids == tips` check | skip | same | step 6: a stale adjudication is 201 |
| The insert-only trigger | `DROP TRIGGER` in the fixture | same `-k appraisal` | step 9: UPDATE succeeds |
| The `overall` null-if-unknown rule | allow a non-null overall | `pytest -q backend/tests/unit/services/test_appraisal_gold.py` | case 4 validates with an overall |
| `graph_part` edge selection | add an edge from every accepted value | `pytest -q backend/tests/integration/test_appraisal_postgres.py` | step 10: A's R3 row is stale |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_appraisal_rules.py backend/tests/unit/services/test_appraisal_gold.py backend/tests/unit/services/test_research_decision_ledger.py
pytest -q backend/tests/unit/services/test_release_rules.py backend/tests/unit/services/test_audit_bundle.py   # _graph and bundle unchanged for old parts
pytest -q backend/tests/unit/api backend/tests/unit/architecture
(cd backend && python ../scripts/ci/check_alembic.py)      # single head e2a4c6b8d0f1
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research-engine
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists.** Report each of these as NOT RUN, never as passing:
- **PostgreSQL proof** (`test_appraisal_postgres.py`, plus the R4 PostgreSQL tests on the same SHA): needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **Methods-expert acceptance:**
  - confirmation that RoB 2 (parallel-group, assignment) fits the pilot's designs;
  - the licence reading (CC BY-NC-ND, structure-only encoding);
  - signing `rob2_gold_v1.json` (`expert_reviewed: true`, `reviewer`).

  This needs a named methods expert. None is available today.
- **Live journey:** needs a deployed stack with `e2a4c6b8d0f1`, PR #1747 (second-principal JIT provisioning, GOO-308 plan "Live principals") and five saved principals.

## Authenticated journey list for Linear closure

Run this on dev (goodwiinz.tech → the dev API) after the deploy. `kubectl -n rag-dev exec deploy/backend -- alembic current` should show `e2a4c6b8d0f1 (head)`.

1. **Protocol:** amend and approve a protocol whose `appraisal_synthesis.appraisal` and `outcomes.declared` are set. `GET /appraisals` shows RoB 2 `2019-08-22` with its licence note.
2. **Blind:** reviewer A submits for a study, leaving one domain unknown. Reviewer B's panel shows `Awaiting` with no count. Keep both `GET` responses.
3. **Conflict:** B submits with one differing domain. Both panels show `Conflict`, list the unresolved domains, and show each row's evidence quote. A's edit returns 409.
4. **Adjudicate:** an adjudicator who did not assess resolves it. Status is `Adjudicated`, and both independent rows remain attributed.
5. **Deny:** the owner with no roles gets 403 on submit, a reviewer gets 403 on adjudicate, a foreign user gets 404, and an archived project gives 409 on writes and 200 on reads.
6. **Not applicable:** a cohort study is recorded as `Not applicable`, and scoring it gives 422.
7. **Reopen/export:** download `/appraisals/export` and the GOO-308 audit bundle. `appraisal.json` has the same instrument, protocol and input hashes, and `sha256sum -c SHA256SUMS` passes.
8. **Stale:** supersede a cited accepted value. Only the appraisals that cite it show `Stale`.

Record the SHA/PR, the Test Pipeline, `openapi-contract` and oasdiff job links, the retained responses and exports, the PostgreSQL junit, the mutation transcripts, and the NOT RUN list.

## GOO-310 seam

GOO-310 reads, and never writes:
- `appraisal_service.current_appraisals(db, collection_id)`, which returns `{(target_key, outcome_key, timepoint): Status(value, unresolved_domains, current_ids)}` for revealed keys only. Its risk-of-bias certainty domain may cite only `current_ids` whose status is `agreed` or `adjudicated`, or whose rows are `not_applicable`.
- `protocol_methods.declared_outcomes`, `acquisition_service.document_reports` and `identity_service.analysis_unit`, for its evidence-table rows.
- The graph node `("appraisal", id)`. GOO-310 adds `appraisal → certainty` edges in its own `graph_part`.

## Out of scope

Each item below has a `ponytail:` marker at its seam:
- a second instrument or RoB 2 variant;
- encoding signalling-question text or RoB 2 algorithms (licence);
- machine appraisal suggestions;
- per-result reviewer assignments (the role is the assignment, as for GOO-304 extraction);
- a persisted status or staleness column;
- a new journey stage;
- backfilling legacy `QualityMark` data.

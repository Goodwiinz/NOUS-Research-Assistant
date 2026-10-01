# GOO-311 One Bounded Quantitative Synthesis Method Plan (Academic R5)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** This adds exactly one methods-approved, protocol-selected calculation: **standardized mean difference (Hedges' g) pooled by inverse-variance random effects with the DerSimonian–Laird τ² estimator**, over a frozen GOO-310 evidence table version.
- Before anything runs, the input set and every exclusion are inspectable, each with a structured reason.
- Execution is deterministic stdlib code. It persists the per-study effects and weights, the pooled estimate, SE, 95% CI, Q, df, I² and τ², plus the protocol, table version, configuration hash and estimator version.
- Re-running an unchanged input returns the same result. A changed input creates a successor result.
- Manuscript claims that cite the old result through a new `synthesis_result` evidence-link kind make their verified releases go stale.
- Results match a gold fixture computed by a second, independent implementation within a predeclared tolerance.

**Architecture:**
- **One insert-only table**, `synthesis_results`, with GOO-309's trigger function.
- **One additive column**, `research_claim_evidence_links.synthesis_result_id`, plus the claim-link CHECKs rewritten to admit a fourth kind.
- **One pure module**, `synthesis_rules.py`, which uses only `math`: validation, the estimator and the hashes.
- **One ledger family**, `research_synthesis`, plus `claim.linked` schema v2.
- **One service**, `synthesis_service.py`, and **one router**, `api/research_engine/synthesis.py`.
- **Inputs** come only from a GOO-310 table version. Rows there are one per analysis unit, so several reports of one study can never become independent weights.
- **Staleness** extends GOO-307's `_graph` through `synthesis_service.graph_part`. A successor result stales the dependent releases through the existing `draft_release_service.invalidate_dependents`.
- The existing `synthesize` blueprint step (`StepType.SYNTHESIZE`, `backend/src/schemas/research_engine.py:97`), narrative drafts and every export stay untouched.

**Tech stack:** FastAPI, SQLAlchemy async, Alembic, Python `math` (production), numpy 2 (test-only independent implementation, already in `backend/requirements.txt`), the PostgreSQL integration fixture (`postgres_container`, `backend/tests/integration/conftest.py:208`), openapi-typescript, Next.js with TanStack Query and Vitest.

**Stacking (read first):** This PR stacks on **GOO-310** (`feat/goo-310-outcome-certainty`) → GOO-309 → GOO-308 → … → GOO-299. It opens after GOO-310 merges. Line numbers are for `e5b909b56` plus the GOO-309/310 plans, so re-locate them after each rebase.

### Consumed names

| Consumed | Name (path:line) | Property relied on |
|---|---|---|
| Input set (GOO-310) | `evidence_table_versions(id, protocol_version_id, outcome_key, timepoint, matrix_id, form_version_id, field_ids, rows, excluded, content_hash)`, `evidence_service.table_version(db, collection_id, id)` | One row per `analysis_unit`. Cells have a state in `value \| missingness \| missing \| conflict` and carry `{accepted_value_id, document_id, report_id, source_hash, text_sha256, value, missingness}`. |
| Unit rule (GOO-309) | `identity_service.analysis_unit`, via the GOO-310 rows | `study:<id>` or `report:<id>`. |
| Methods sections (GOO-309) | `protocol_methods.py`, `declared_outcomes`; `ProtocolSnapshot.appraisal_synthesis` (`backend/src/schemas/research_engine.py:290`) | Free-form bounded dicts, so no OpenAPI change. |
| Field definitions (GOO-304) | `extraction_rules.field_def` (`backend/src/services/research/extraction_rules.py:170`), `ExtractionFormVersion.fields` (`backend/src/models/extraction_matrix.py:121-135`) | Each field has `type, unit, timepoint`. |
| Claim links (GOO-306) | `ResearchClaimEvidenceLink` (`backend/src/models/research_claim.py:163`), `LINK_KIND_CHECK`/`LINK_SHAPE_CHECK` (`:36-51`), `claim_rules.LINK_KINDS`/`check_link_shape` (`backend/src/services/research/claim_rules.py:19,49`), `claims_service.link` (`backend/src/services/research/claims_service.py:656`), `_check_request_shape` (`:553`), `observe_stance` (`:803`) | Link shapes are mirrored in the pure rules. A link is a `linked` tip. |
| Release gate (GOO-307) | `release_rules.ANCHORED_LINK_KINDS` (`backend/src/services/research/release_rules.py:34`), `_factual_code` (`:163-189`), `draft_release_service._graph` (`backend/src/services/research/draft_release_service.py:129`), `invalidate_dependents` (`:657`), `ledger._RELEASE_CAUSE_FAMILIES` (`backend/src/services/research_decisions/ledger.py:316`) | A factual claim passes only on live anchored links. Invalidation runs in the caller's transaction. |
| Ledger | `lock_aggregate_stream` (`ledger.py:1089`), `append_decision` (`:1092`), `replay_decisions` (`:1191`), `_CLAIMS_PAYLOAD_KEYS` (`:244`), `_FAMILIES` (`:1895`), `identity_service._replayed_event` (`backend/src/services/research_engine/identity_service.py:76`) | The append is caller-owned. |
| Roles | `ResearchAction.REVIEW`, `resolve_project` (`backend/src/services/research_engine/project_access.py:28-47,186`) | Roles are reloaded after the lock. |
| Current protocol | `identity_service.current_protocol_version_id` (`:103`) | The approved version, or `None`. |
| Canonical hash | `contracts.canonical_json_sha256` (`backend/src/services/research_engine/contracts.py:38`) | Stable across runs. |
| Insert-only trigger (GOO-309) | `prevent_research_insert_only_mutation()` | SQLSTATE `55000` on UPDATE or DELETE. |
| Bundle | `audit_bundle._sealed_part` / `gather_parts` (`backend/src/services/research_engine/audit_bundle.py:80,290`) | One reader per part. |
| Not touched | `models/ab_testing*.py`, `StepType.SYNTHESIZE` step executors, `draft_generation_service` narrative synthesis | They are guarded in Task 7. |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| **The one effect measure and model** | **SMD as Hedges' g** with the small-sample factor `J = 1 − 3/(4·df − 1)`, where `df = n₁ + n₂ − 2`. The pooled SD is `s_p = √(((n₁−1)s₁² + (n₂−1)s₂²)/df)`, `d = (m₁ − m₂)/s_p`, `g = J·d`, and `Var(g) = J²·((n₁+n₂)/(n₁n₂) + d²/(2(n₁+n₂)))` (Borenstein et al., *Introduction to Meta-Analysis*, 2009, ch. 4). Pooling is **inverse-variance random effects with DerSimonian–Laird**: `w = 1/v`, fixed-effect `μ_F = Σwg/Σw`, `Q = Σw(g−μ_F)²`, `df = k−1`, `C = Σw − Σw²/Σw`, `τ² = max(0, (Q−df)/C)`, `I² = max(0, (Q−df)/Q)` (0 when `Q = 0`), `w* = 1/(v+τ²)`, `μ = Σw*g/Σw*`, `SE = √(1/Σw*)`, and 95% CI `μ ± 1.959963984540054·SE`. The sign is intervention − control, stored as `direction: "intervention_minus_control"` and never flipped. | The pilot outcome is continuous depressive-symptom scores (GDS-15 and similar scales; `evals/academic-journey-v1/corpora/known-answer.json:6,15`), and the included trials may use different scales. That needs SMD rather than MD, and SMD is not a ratio measure like log RR. DL is closed-form, deterministic and the most widely reproduced estimator. `# ponytail: DL under-covers with few studies; add HKSJ or REML as a new estimator version, never by editing this one.` |
| **Gold fixture tolerance** | **Absolute 1e-8** on every persisted number (g and v per study, μ, SE, CI bounds, Q, τ², I² as a proportion). It is declared in the fixture header `tolerance: {"abs": 1e-8}` before any comparison runs. The two independent implementations agree to within about 1e-16. | The recorded gold values have 10 decimals, so 1e-8 leaves room for rounding in the record and nothing else. Any real formula difference (for example exact J via gamma, as metafor uses) shows up at about 1e-4 and fails. |
| Gold values (recorded now; Task 1 re-derives them) | **Fixture `smd_dl_gold_v1`** is post-intervention GDS-15, 12 weeks, with the arms given as (m₁, s₁, n₁ \| m₂, s₂, n₂): A (4.1, 2.6, 60 \| 5.3, 2.9, 60), B (6.0, 3.1, 45 \| 6.4, 3.0, 47), C (3.2, 2.2, 30 \| 5.1, 2.5, 28), D (5.5, 2.8, 80 \| 5.6, 2.7, 82). This gives g = −0.4329406845, −0.1300815131, −0.7978273216, −0.0361951586; Q = 7.3067910677 (df 3); τ² = 0.0558459463; I² = 0.5894230487; μ = −0.3084711364; SE = 0.1551738785; 95% CI [−0.6126063496, −0.0043359232]. **Homogeneous fixture** E (5.0, 3.0, 50 \| 5.9, 3.0, 50) and F (5.1, 3.0, 50 \| 6.0, 3.0, 50): Q = 0, τ² = 0 (clamped), I² = 0, μ = −0.2976982097, SE = 0.1411234657. Both were computed on 2026-09-30 by a pure-Python and a numpy/scipy (`norm.ppf(0.975)`) implementation, and the largest disagreement was 1.1e-16. | The ticket asks for gold values from a second, independent implementation, with both outputs recorded. The homogeneous case exercises the τ² and I² clamps. |
| Protocol selection | `appraisal_synthesis.synthesis = {"measure": "smd_hedges_g", "model": "random_effects_dl", "outcome": "<declared key>", "timepoint": "<declared timepoint>"}`, parsed by `protocol_methods.synthesis_selection(snapshot)`. A missing section gives 409 `Protocol selects no quantitative synthesis`, and any other measure or model gives 409 `Protocol selects an unsupported synthesis method`. The table version's `outcome_key`/`timepoint` must equal the selection's, and its `protocol_version_id` must be the current approved version (otherwise 409 `Evidence table is stale; reload`). | "Protocol-selected." A narrative-only protocol (`{"method": "narrative"}`) is unaffected. |
| Configuration | The request maps six **roles** to the table's `field_ids`: `mean_i, sd_i, n_i, mean_c, sd_c, n_c`. The stored `config` is `{measure, model, direction, ci_level: 0.95, z: 1.959963984540054, roles, j: "1-3/(4*df-1)", tau2_floor: 0.0, i2_when_q_zero: 0.0}`, and `config_hash = canonical_json_sha256(config)`. `estimator_version = "nous.smd-hedges-g.dl/1"` and `software = {"python": platform.python_version(), "estimator": estimator_version}`. | The numerical configuration is explicit and retained, so a future estimator is a new version string. The Python version is recorded but does not enter `input_hash`, because stdlib float arithmetic is IEEE-754. |
| Validation (structured, never prose) | **Run-level:**<br>`unit_mismatch` (the `mean_*` and `sd_*` role fields do not all have the same non-null `unit`);<br>`timepoint_mismatch` (a role field's `timepoint` differs from the selection's);<br>`role_not_in_table`;<br>`wrong_field_type` (non-`number`);<br>`insufficient_studies` (fewer than 2 included units).<br>**Unit-level**, stored in `excluded: [{unit, report_ids, reason, detail}]`:<br>`missing_input:{role}` (the cell is `missing` or `missingness`);<br>`conflicting_reports:{role}` (a `conflict` cell: two reports of one study disagree, so the unit is excluded and never double-counted);<br>`invalid_sample_size:{arm}` (n is not an integer ≥ 2);<br>`invalid_variance:{arm}` (sd ≤ 0 or not finite);<br>`non_numeric:{role}`;<br>the table's own `excluded` entries, carried through with their GOO-310 reasons. | "Missing/incompatible variance, units or outcome/timepoint data produce explicit validation results." A run with a run-level failure persists `status = "validation_failed"`, NULL numbers and its reasons, so a failed attempt is retained too. |
| Duplicate reports | This is structural. Inputs are GOO-310 rows, one per unit, and a unit with two agreeing reports contributes one value. A unit whose reports disagree is excluded with `conflicting_reports`. `synthesis_rules.compute` also asserts that the included `unit` keys are unique (`ValueError`). | "Multiple reports of one Study cannot get independent weights", guarded twice. |
| Inspect before execute | `GET .../synthesis/preview?table_version_id&roles` (VIEW, zero writes) returns the exact `included` (unit, report_ids, accepted_value_ids, six values) and `excluded` lists, plus `input_hash`, that `POST .../synthesis` would use. `POST` recomputes the same lists and refuses with 409 `Inputs changed since preview; reload` if the body's `expected_input_hash` differs. | The input set and its exclusions are inspectable before execution, and what was inspected is what runs. |
| Idempotency and successors | `input_hash = canonical_json_sha256({table_content_hash, config_hash, estimator_version, protocol_version_id})`. If the tip for `(collection_id, outcome_key, timepoint)` has the same `input_hash`, it is returned (`200`, `replayed=true`) and nothing is written. Otherwise a successor is inserted with `supersedes_result_id = tip` (409 `Synthesis result is stale; reload` if the body names another tip). There is a partial unique initial index plus `UNIQUE(supersedes_result_id)`. | "Re-execution of an unchanged fixture is stable, changed inputs create a successor result." |
| Numbers storage | `estimate, se, ci_low, ci_high, q, df, i2, tau2` are `Float` (double precision) NULL columns, with `CHECK ((status = 'computed') = (estimate IS NOT NULL))`. `included` is JSONB with per-unit `g, v, w_fixed, w_random`. `result_hash = canonical_json_sha256({included, excluded, numbers})`. Summation goes in sorted `unit` order. | The result is deterministic and byte-stable across runs. Per-study rows let a reviewer recompute by hand. |
| Who executes | `ResearchAction.REVIEW` (`actor_role = 'reviewer'`). | Execution is a recorded analytic decision by an explicit role, not by ownership. The arithmetic itself is machine and deterministic. |
| Dependent conclusions | A new GOO-306 link kind, **`synthesis_result`**: `research_claim_evidence_links.synthesis_result_id` FK RESTRICT NULL. `LINK_KIND_CHECK` becomes `kind IN ('extraction','source_span','legacy_unanchored','synthesis_result')`, and `LINK_SHAPE_CHECK` gains `OR (kind = 'synthesis_result' AND synthesis_result_id IS NOT NULL AND accepted_value_id IS NULL AND document_id IS NULL AND source_hash IS NULL AND text_sha256 IS NULL AND draft_citation_id IS NULL AND start_char IS NULL AND end_char IS NULL AND quote IS NULL)`, while the three existing branches each add `AND synthesis_result_id IS NULL`. Only a `computed`, non-stale tip can be linked (otherwise 409 `Synthesis result is not current`). `observe_stance` on such a link gives 422 `Synthesis links carry no model stance`. `release_rules.ANCHORED_LINK_KINDS` adds `synthesis_result`. | A pooled estimate in a manuscript appears in no source span, so without this kind the GOO-307 gate would block it forever. With it, "changed inputs … stale dependent conclusions" means the verified releases citing the old result go stale through the existing path. |
| Staleness (reusing GOO-307's walk) | `synthesis_service.graph_part` adds `("evidence_table", t) → ("synthesis", s)`, `("protocol", v) → ("synthesis", s)` and `("synthesis", s) → ("link", l)` for every `synthesis_result` link. `changed` adds superseded results. On a successor insert, `execute` calls `draft_release_service.invalidate_dependents(changed={("synthesis", old_tip)}, cause={"family": "research_synthesis", ...})` in its own transaction before the commit, and `_RELEASE_CAUSE_FAMILIES` adds `research_synthesis`. A result whose table goes stale is `stale` on read. | This is one walk with no new mechanism. Upstream extraction changes reach a result through GOO-310's `accepted → evidence_table` edges. Only the releases that cite this result are stamped. |
| Existing surfaces | Nothing in `StepType.SYNTHESIZE`, the narrative synthesis prompts, `ab_testing*` models or existing exports changes. The AST guard (Task 7) fails if `synthesis_rules`/`synthesis_service` import an LLM client (`openai`, `langchain`, `src.services.llm`) or `ab_testing`, or if any `src/services/agent/` or `src/tasks/` module imports them. | "Deterministic code, not prose completion"; "do not retrofit A/B experiment tables". |

**Migration head:** new revision `a6c8e0b2d4f5_create_synthesis_results.py`, with `down_revision = "f4b6d8a0c2e3"` (GOO-310). The re-pointing rule is the same as GOO-309's. The migration imports nothing from `src`.

**`upgrade()`** does the following:
1. Creates `synthesis_results`, with `_deny_data_api` and `trg_synthesis_results_insert_only` on GOO-309's function.
2. `op.add_column('research_claim_evidence_links', synthesis_result_id UUID NULL FK synthesis_results RESTRICT)`.
3. `op.drop_constraint` and recreates `ck_research_claim_links_kind` and `ck_research_claim_links_shape` with the frozen strings above. Existing rows satisfy the new CHECKs, because the column is NULL.

**`downgrade()`** refuses with `RuntimeError` if any link has `kind = 'synthesis_result'`, which is the "never silently drop evidence" rule. Otherwise it restores the old CHECKs and drops the column and the table.

---

### Task 1: Gold fixtures and the independent implementation (write first)

**Files:**
- Create `backend/tests/fixtures/synthesis/independent_numpy.py`: numpy only, vectorized, with `scipy.stats.norm.ppf(0.975)` if scipy imports, otherwise the literal constant. It must not import `src`.
- Create `backend/tests/fixtures/synthesis/smd_dl_gold_v1.json` with `{"schema": "nous.synthesis-gold.v1", "measure": "smd_hedges_g", "model": "random_effects_dl", "tolerance": {"abs": 1e-8}, "expert_reviewed": false, "reviewer": null, "cases": [...]}`. The cases are:
  - `heterogeneous` (A–D) and `homogeneous` (E–F), each with inputs, `expected` (the 10-decimal values in Decisions) and `independent` (numpy's output to 12 decimals);
  - `duplicate_report` (study S reported twice with equal values, which must give k and weights equal to the single-report case);
  - `missing_variance` (sd = 0);
  - `unit_mismatch` and `timepoint_mismatch` (config-level);
  - `single_study` (`insufficient_studies`).
- Create `backend/tests/unit/services/test_synthesis_gold_independent.py`: `test_numpy_reproduces_recorded_gold` checks that `independent_numpy` equals `expected` within the tolerance, which proves the recorded values are not self-referential.

**Run:** `pytest -q backend/tests/unit/services/test_synthesis_gold_independent.py`
**Commit:** `test(research): SMD/DL gold fixtures from an independent numpy implementation (GOO-311)`

---

### Task 2: Pure rules (`synthesis_rules.py`)

**Files:**
- Create `backend/src/services/research_engine/synthesis_rules.py` (stdlib `math` and `dataclasses` only).
- Modify `backend/src/services/research_engine/protocol_methods.py` to add `synthesis_selection(snapshot) -> tuple[str, str, str, str]`.
- Create `backend/tests/unit/services/test_synthesis_rules.py`.

```python
MEASURE = "smd_hedges_g"; MODEL = "random_effects_dl"; ESTIMATOR_VERSION = "nous.smd-hedges-g.dl/1"
Z_975 = 1.959963984540054
ROLES = ("mean_i", "sd_i", "n_i", "mean_c", "sd_c", "n_c")
@dataclass(frozen=True) class Arm: mean: float; sd: float; n: int
@dataclass(frozen=True) class UnitInput: unit: str; report_ids: tuple[str, ...]; accepted_value_ids: tuple[str, ...]; i: Arm; c: Arm
@dataclass(frozen=True) class Exclusion: unit: str | None; reason: str; detail: str
def config(roles: Mapping[str, str]) -> dict
def check_config(fields_by_id: Mapping[str, Mapping], roles, timepoint) -> list[Exclusion]       # run-level
def select_inputs(rows: Sequence[Mapping], roles, table_excluded) -> tuple[list[UnitInput], list[Exclusion]]
def hedges_g(i: Arm, c: Arm) -> tuple[float, float]                  # (g, v)
def pool_dl(effects: Sequence[tuple[str, float, float]]) -> dict     # sorted by unit; asserts unique units
def input_hash(table_hash, config_hash, protocol_version_id) -> str
def result_hash(included, excluded, numbers) -> str
```

**Tests (they fail on the import):**
- `test_matches_gold_heterogeneous_within_declared_tolerance`
- `test_matches_gold_homogeneous_tau2_and_i2_clamped`
- `test_duplicate_reports_one_weight`
- `test_conflicting_reports_excluded_with_reason`
- `test_missing_variance_and_bad_n_are_structured_exclusions`
- `test_unit_and_timepoint_mismatch_fail_run_level`
- `test_insufficient_studies`
- `test_pool_rejects_duplicate_unit_keys`
- `test_input_hash_stable_and_sensitive`: the same inputs give the same hash, and one changed value gives a different hash.
- `test_order_independent_result`: shuffling the rows gives an identical `result_hash`.

**Run:** `pytest -q backend/tests/unit/services/test_synthesis_rules.py`
**Commit:** `feat(research): deterministic SMD Hedges' g with DerSimonian-Laird pooling (GOO-311)`

---

### Task 3: Model + migration

**Files:**
- Create `backend/src/models/research_synthesis.py` (`SynthesisResult`) and export it from `backend/src/models/__init__.py`.
- Modify `backend/src/models/research_claim.py:36-51,163-203`: `LINK_KIND_CHECK`, `LINK_SHAPE_CHECK` and the `synthesis_result_id` column.
- Modify `claim_rules.py:19,49-100`: `LINK_KINDS` and a `check_link_shape` branch that takes `synthesis_result_id` as a new keyword-only argument (default `None`).
- Create `backend/alembic/versions/a6c8e0b2d4f5_create_synthesis_results.py`.
- Modify `backend/tests/integration/test_screening_queue_postgres.py` (`_REBUILT_TABLES` and `_upgrade`). `research_claim_evidence_links` is already rebuilt there, so the new FK needs `synthesis_results` dropped **after** it.

**`synthesis_results`:**
- `id, collection_id FK RESTRICT, protocol_version_id FK NOT NULL, table_version_id FK evidence_table_versions RESTRICT, outcome_key, timepoint`
- `measure VARCHAR(32), model VARCHAR(32), config JSONB, config_hash CHAR(64), estimator_version VARCHAR(64), software JSONB`
- `status VARCHAR(20) CHECK IN ('computed','validation_failed')`
- `included JSONB, excluded JSONB`
- `estimate, se, ci_low, ci_high, q, tau2, i2 FLOAT NULL, df INTEGER NULL`
- `input_hash CHAR(64), result_hash CHAR(64)`
- `executed_by_id FK users, actor_role VARCHAR(16) CHECK = 'reviewer'`
- `supersedes_result_id FK self NULL UNIQUE`, `created_at`

**Constraints:** `CHECK ((status = 'computed') = (estimate IS NOT NULL))` and `CHECK (i2 IS NULL OR (i2 >= 0 AND i2 <= 1))`, plus a partial unique `uq_synthesis_initial (collection_id, outcome_key, timepoint) WHERE supersedes_result_id IS NULL`.

**Check:**
- `check_alembic.py` reports single head `a6c8e0b2d4f5`.
- `alembic upgrade head --sql | grep -c synthesis_results` is > 0.
- `pytest -q backend/tests/unit/services/test_claim_rules.py`: existing shapes are unchanged, and `test_synthesis_link_shape` is added.

**Commit:** `feat(research): synthesis_results table and synthesis claim-link kind (GOO-311)`

---

### Task 4: Ledger

**Files:**
- Modify `ledger.py`:
  - Add the `research_synthesis` family (subject type `synthesis_result`).
  - Add `("claim.linked", 2)` = the v1 keys ∪ `{synthesis_result_id}`.
  - Add `research_synthesis` to `_RELEASE_CAUSE_FAMILIES` (`:316`).
  - Update the claims transition validator so a v2 `synthesis_result` link requires `synthesis_result_id` and nulls the source fields.
- Modify `claims_service.link` so it always writes schema 2. v1 events keep replaying.
- Modify `test_research_decision_ledger.py`.

| Event | Payload keys | actor_role |
|---|---|---|
| `synthesis.executed` (1) | `collection_id, result_id, supersedes_result_id, table_version_id, protocol_version_id, outcome_key, timepoint, measure, model, config_hash, estimator_version, status, input_hash, result_hash, included_units, excluded` | `reviewer` |
| `claim.linked` (2) | the v1 keys plus `synthesis_result_id` | as v1 |

**Replay rules:**
1. Every `collection_id` equals the `aggregate_id`.
2. Each `(outcome_key, timepoint)` has one tip.
3. A successor's `input_hash` differs from its predecessor's (an unchanged input never makes a successor).
4. `status == "computed"` iff `included_units` has at least 2 entries.

**Tests:**
- `test_synthesis_replay_rejects_successor_with_same_input_hash`
- `test_claim_linked_v1_still_replays_v2_requires_result_id`
- `test_release_staled_accepts_synthesis_cause`

**Commit:** `feat(research): research_synthesis decision family and claim.linked v2 (GOO-311)`

---

### Task 5: Service + graph part + link kind

**Files:**
- Create `backend/src/services/research_engine/synthesis_service.py`.
- Modify `draft_release_service._graph`: a third local import and call to `synthesis_service.graph_part`. In the link loop (`:163-172`), add `elif row.kind == "synthesis_result": parent = rules.node("synthesis", row.synthesis_result_id)`.
- Modify `release_rules.py:34`: `ANCHORED_LINK_KINDS` adds `"synthesis_result"`.
- Modify `claims_service`:
  - `_check_request_shape` (`:553`) and `link` (`:656`) accept `synthesis_result_id`, with a new `_synthesis_target` next to `_extraction_target` (`:577`). It is Collection-scoped, gives 404 for a foreign result, and gives 409 `Synthesis result is not current` unless the result is `computed`, a tip and not stale.
  - `observe_stance` (`:803`) gives 422 for this kind.
- Modify `backend/src/shared/claim_schemas.py`: `ClaimLinkCreate` and the link response gain optional `synthesis_result_id`.

```python
AGGREGATE_TYPE = "research_synthesis"; SUBJECT_TYPE = "synthesis_result"
async def preview(db, context, table_version_id, roles) -> SynthesisPreview                 # VIEW; zero writes
async def execute(db, context, actor_id, data: SynthesisExecute) -> tuple[SynthesisResultResponse, bool]  # REVIEW; commits once
async def list_results(db, context) -> list[SynthesisResultResponse]                       # every row + stale
async def graph_part(db, collection_id) -> tuple[list[Edge], set[Node]]
async def export_package(db, context) -> dict                                              # nous.academic.synthesis.v1
```

**`execute` write order:**
1. `resolve_project(REVIEW)`.
2. `lock_aggregate_stream(research_synthesis, collection_id)`.
3. `_replayed_event`.
4. `synthesis_selection` plus the current-protocol check.
5. `evidence_service.table_version`: the tip must not be stale under `_graph`, otherwise 409.
6. `check_config`, then `select_inputs`, then `expected_input_hash` equality.
7. If the tip has the same `input_hash`, return it.
8. `pool_dl`, or `validation_failed`.
9. Insert and append `synthesis.executed`.
10. If a predecessor exists, `invalidate_dependents(changed={("synthesis", predecessor)})`.
11. **One commit.**

A unique violation gives 409 stale. `research_release` is locked last, inside `invalidate_dependents`, which is the GOO-307 ordering rule, so there is no cycle.

**Commit:** `feat(research): synthesis preview, execution, successor staling and claim links (GOO-311)`

---

### Task 6: API + contracts + bundle part

**Files:**
- Create `backend/src/api/research_engine/synthesis.py` (`SYNTHESIS = "/projects/{project_id}/synthesis"`). Register it in `__init__.py` and `main.py`.
- Add the schemas to `backend/src/schemas/research_engine.py`.
- Modify `audit_bundle.py` to add a `synthesis.json` part before `_prisma`.
- Regenerate the OpenAPI spec and the types.

| Route | Action | Notes |
|---|---|---|
| `GET {SYNTHESIS}/preview?table_version_id&mean_i&sd_i&n_i&mean_c&sd_c&n_c` | VIEW | `SynthesisPreview{config, config_hash, included, excluded, run_failures, input_hash}` |
| `POST {SYNTHESIS}` | REVIEW | `SynthesisExecute{table_version_id, roles, expected_input_hash, supersedes_result_id?, idempotency_key}`. Returns 201 new, 200 unchanged, 409 stale/changed/unsupported, 403 without the role, 404 foreign. |
| `GET {SYNTHESIS}` | VIEW | Every result with `stale`, `included`, `excluded`, numbers and provenance. |
| `GET {SYNTHESIS}/export` | VIEW | A JSON attachment. The numbers are emitted with `repr` so they round-trip exactly. |
| `POST /api/v1/projects/{project_id}/claims/{claim_id}/links` (existing) | EDIT (unchanged) | Accepts `kind: "synthesis_result"`. |

**oasdiff:** new operations; a new optional request field; the `kind` enum widened on a request (WARN at most, not ERR); a new optional response field. **Expected: no ERR.** If oasdiff flags the enum widening as ERR, apply the `api-breaking-approved` label with the justification "additive enum value on request" (`docs/engineering/api-contracts.md`, Escape hatch).

**Tests** (`backend/tests/unit/api/test_synthesis_routes.py`):
- `test_execute_owner_without_role_403`
- `test_preview_zero_writes`
- `test_execute_rejects_changed_expected_hash`
- `test_link_to_validation_failed_result_409`

**Commit:** `feat(research): synthesis endpoints, export and bundle part (GOO-311)`

---

### Task 7: Structural guard

**Files:**
- Create `backend/tests/unit/architecture/test_synthesis_boundary.py`, an AST scan with three checks:
  - **(a)** `synthesis_rules.py` imports only stdlib.
  - **(b)** `synthesis_rules.py` and `synthesis_service.py` never import `openai`, `anthropic`, `langchain*`, `src.services.llm*`, `src.services.agent*` or `src.models.ab_testing*`.
  - **(c)** Nothing in `src/services/agent/`, `src/tasks/` or the blueprint step executors imports `synthesis_service` or constructs `SynthesisResult(`.
- Assert that `StepType.SYNTHESIZE == "synthesize"` still holds, and that `backend/src/services/research_engine/step_executor.py` does not import `synthesis_service` or `synthesis_rules` (the narrative step stays separate).

**Commit:** `test(research): guard deterministic synthesis and untouched narrative steps (GOO-311)`

---

### Task 8: PostgreSQL proof (one test)

**Files:**
- Create `backend/tests/integration/test_synthesis_postgres.py` with `pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]`. It reuses `screening_factory`/`_upgrade` (through `a6c8e0b2d4f5`), `seed_approved_protocol_binding` with a `synthesis` selection for `depressive_symptoms@12 weeks`, and the GOO-310 seed helpers.

**`test_synthesis_gold_duplicates_validation_successor_and_stale_release`** runs these steps in order:
1. **Seed:**
   - Units A–D from the gold fixture as accepted values on six fields at `12 weeks`.
   - Study A is reported twice (R1 and R2) with identical values.
   - Unit E has `sd_c` = 0.
   - Unit F lacks `n_i` (a `missing` cell).
   - Freeze a GOO-310 table.
2. **Preview:**
   - `included` is exactly A, B, C and D, with A appearing once and carrying both report ids.
   - `excluded` holds E `invalid_variance:c` and F `missing_input:n_i`.
   - There are no writes (row and event counts are unchanged).
3. **Execute** as reviewer:
   - The persisted `included[*].g`/`v`, the estimate, SE, CI, Q, df, τ² and I² match `smd_dl_gold_v1.heterogeneous` within 1e-8.
   - The row references the protocol version, table version, `config_hash` and `estimator_version`, and its exclusions are retained.
   - The owner with no roles gets 403, and a foreign user gets 404.
4. **Stable:** executing again with a new idempotency key returns 200 with the same id and `result_hash`. There is still one row and one event.
5. **Validation failure:** a table whose `mean_c` role field has unit `%` gives a persisted `validation_failed` row with `unit_mismatch` and NULL numbers. It is a separate outcome key, so it does not touch the tip chain above.
6. **Claim link:** create a claim on a draft sentence citing the pooled SMD, link it with `kind="synthesis_result"`, assess it `supporting`, and promote the draft (GOO-307). It is `verified`. Linking the `validation_failed` result gives 409.
7. **Changed input:** supersede unit B's accepted `mean_i` and rebuild the table.
   - The old result is `stale` on read.
   - Execute creates a **successor** whose `supersedes_result_id` is the old id.
   - Step 6's release is stamped stale with `release.staled.cause.family == "research_synthesis"`.
   - A second verified draft that does not cite synthesis stays `verified`.
8. **Insert-only:** UPDATE or DELETE on `synthesis_results` raises `55000`.
9. **Archived:** 409 on execute, 200 on list.
10. **Replay:** the `research_synthesis` and `research_claims` streams replay, and `research_release` replays with the new cause family.
11. **Downgrade:** with a `synthesis_result` link present, downgrade raises. After the link's test schema is dropped, it restores the old CHECKs.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_synthesis_postgres.py`. Without a database, report it as **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for gold SMD/DL, duplicates, validation and staling (GOO-311)`

---

### Task 9: Frontend

**Files:**
- Create `frontend/src/types/api/research-synthesis-contract.ts`.
- Modify `frontend/src/services/researchEngineService.ts` to add `previewSynthesis`, `executeSynthesis`, `listSynthesis` and `exportSynthesis`.
- Create `frontend/src/components/research-engine/SynthesisPanel.tsx` and mount it in the `Extract` stage after `EvidenceTablePanel`.
  - The panel shows the protocol's selected method (read-only text), a table-version select and six role `<select>`s over the table's fields.
  - **Preview** shows the included units (with report counts) and the excluded units with reason codes as human labels. Any run-level failure disables Execute.
  - **Execute** is available to reviewers.
  - The result table lists per-unit g, 95% CI and weight, plus the pooled SMD with its CI, Q (df), I² as a percentage and τ². The estimator version and hashes sit in a `<details>`. No chart.
  - A stale result shows a banner with its cause.
- Modify `frontend/src/components/research/DraftClaimsPanel.tsx`: the link form gains a "Synthesis result" option listing current computed results.
- Tests go in `__tests__/SynthesisPanel.test.tsx`:
  - `excluded reasons shown before execute`
  - `execute disabled on run failure`
  - `stale banner`
  - `I² shown as percent`

**Commit:** `feat(frontend): synthesis preview, execution and result panel (GOO-311)`

---

## Mutation verification

Follow `docs/engineering/testing.md`. Record each check in the test docstring and in `docs/testing/agent-orchestration-mutation-checks.md` under a GOO-311 section.

| Guard | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| The small-sample factor J | `J = 1` | `pytest -q backend/tests/unit/services/test_synthesis_rules.py -k gold` | a gold mismatch of about 1e-3 |
| The τ² clamp `max(0, …)` | remove it | same `-k homogeneous` | τ² < 0 |
| One row per unit in `select_inputs` | expand rows per `report_ids` | `pytest -q backend/tests/unit/services/test_synthesis_rules.py -k duplicate` plus integration step 2 | A is weighted twice |
| The `invalid_variance` check | skip it | same `-k missing_variance` | ZeroDivisionError, or E included |
| The unchanged-input short-circuit | skip it | `pytest -q backend/tests/integration/test_synthesis_postgres.py` | step 4: a second row |
| `invalidate_dependents` on a successor | skip the call | same | step 7: the release stays verified |
| `graph_part` selection | link every result to every link | same | step 7: the unrelated draft goes stale |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_synthesis_gold_independent.py backend/tests/unit/services/test_synthesis_rules.py
pytest -q backend/tests/unit/services/test_claim_rules.py backend/tests/unit/services/test_release_rules.py backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/services/test_audit_bundle.py
pytest -q backend/tests/unit/api backend/tests/unit/architecture
(cd backend && python ../scripts/ci/check_alembic.py)      # single head a6c8e0b2d4f5
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research-engine src/components/research
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists.** Report each of these as NOT RUN, never as passing:
- **PostgreSQL proof** (this test, plus GOO-309/310 and the R4 PostgreSQL tests on the same SHA): needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **Methods-expert signoff:**
  - that SMD/Hedges' g with DL fits the pilot's outcome;
  - the variance formula and the 1e-8 tolerance;
  - the gold fixture (`expert_reviewed: true`, `reviewer`).

  None is available today.
- **External cross-check** against R `metafor` or Stata `meta`: needs R or Stata. When it runs, metafor's exact-J `escalc("SMD")` differs by about 1e-4 by design, so compare with `vtype="LS"` and the approximate J, or record the expected difference. It is never a substitute for the recorded independent numpy values.
- **Live journey:** needs a deployed stack with `a6c8e0b2d4f5`, PR #1747 and the saved principals.

## Authenticated journey list for Linear closure

Run this on dev after the deploy. `alembic current` should show `a6c8e0b2d4f5 (head)`.

1. **Protocol:** approve a protocol amendment that selects `smd_hedges_g` / `random_effects_dl` for the pilot outcome.
2. **Preview:** freeze the GOO-310 table, map the six roles and preview. The duplicate-report trial appears once, and the units with missing SD or n are listed with reasons. Keep the JSON.
3. **Execute:** the numbers in the UI equal those in `/synthesis/export`, and the export's inputs recompute offline with `independent_numpy.py` within 1e-8. Keep both outputs.
4. **Stable:** executing again returns the same result id.
5. **Link and release:** cite the pooled SMD in a draft claim (link kind "Synthesis result"), assess it, and promote the draft to `Verified`.
6. **Change:** supersede one input value and rebuild the table. The result shows `Stale`, the next execute creates a successor, the draft becomes `Stale` with cause `research_synthesis`, and an unrelated verified draft stays `Verified`.
7. **Deny:** the owner with no roles gets 403 on execute, a foreign user gets 404, and an archived project gives 409.
8. **Bundle:** `synthesis.json` is in the audit bundle, and `sha256sum -c SHA256SUMS` passes.

Record the SHA/PR, the CI and oasdiff links, the retained inputs, both gold computations, the persisted results, the UI and export comparison, the PostgreSQL junit, the mutation transcripts and the NOT RUN list.

## Seams

- **Upstream (GOO-310):** the only input is `evidence_table_versions`. This ticket never reads accepted values directly, so the unit rule, stale-tip filtering and exclusions stay in one place.
- **Upstream (GOO-306/307):** one new link kind, `synthesis_result`, and one new cause family. The release gate's semantics are otherwise unchanged.
- **Later tickets:** another estimator is a new `ESTIMATOR_VERSION` and a new `model` value, never an edit to this one. A certainty → synthesis citation (GOO-310 seam) and forest plots are follow-ups.

## Out of scope

Each item below has a `ponytail:` marker at its seam:
- any other measure or estimator (MD, log RR, REML, HKSJ, fixed-effect-only);
- subgroup or sensitivity analyses;
- prediction intervals;
- forest-plot rendering;
- LLM-generated analysis;
- A/B experiment tables;
- relabelling narrative synthesis;
- change-from-baseline or cluster-adjusted inputs;
- imputing missing SDs.

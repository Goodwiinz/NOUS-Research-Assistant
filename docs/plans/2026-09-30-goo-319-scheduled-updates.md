# GOO-319 Scheduled Search Updates + Classified Corpus Deltas Plan (Academic R8)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A supervisor can save a **search schedule** (cron expression, IANA timezone, enabled) that pins one exact GOO-298 search strategy version and the approved protocol version it was built under. A Celery beat tick claims each due fire exactly once, even across worker restarts, missed ticks and overlapping beats. Each execution rechecks access, reruns the pinned strategy through the existing provider fan-out, keeps the GOO-298 provider receipts and coverage, imports the results idempotently as a GOO-300 import receipt resolved through GOO-299 identity, and compares the resulting canonical works with an **explicit prior corpus snapshot**. Every work in the comparison is classified `new`, `changed`, `corrected_retracted`, `unchanged` or `unknown`, each with its evidence. Provider caps, outages, missing DOIs and works that disappear from results are `unknown` with a reason. They are never inferred retractions or deletions, and a workspace-withdrawn document is never a publication retraction. The Workflow tab shows each schedule's status, its last execution and the classified delta.

**Architecture:**
- **Four insert-only tables**, each with GOO-309's `prevent_research_insert_only_mutation()` trigger:
  - `research_search_schedules`: a version chain per schedule (config changes and enable/disable are new versions).
  - `research_search_executions`: one row per fire, `UNIQUE(schedule_id, scheduled_local)`. This unique key **is** the idempotency guard.
  - `research_search_execution_attempts`: one row per attempt outcome (`started`, `succeeded`, `failed`, `skipped`), so failures and restarts are retained.
  - `research_search_execution_results`: one row per succeeded execution (coverage, corpus snapshot, delta).
- **One pure module**, `search_update_rules.py`: cron fire computation, missed-tick coalescing, version keys and delta classification.
- **One service**, `search_update_service.py`; **one router**, `api/research_engine/search_updates.py`; **one beat task**, `search_update_tasks.tick` (every 60 s); **one ledger family**, `research_search_update`.
- **Reuse, not rebuild:** provider calls go through `discovery.search_sources`; records go through `corpus_service.insert_receipt` (which calls `identity_service.observe_import_records`); the corpus baseline uses the GOO-300 `corpus_export` record set; citation chasing calls `corpus_service.chase_citations` only when `_protocol_requirement` says it is required.
- **No staleness writes.** This ticket only classifies. GOO-320 decides what a delta invalidates.

**Tech stack:** FastAPI, SQLAlchemy async, Alembic, Celery beat + `celery.schedules.crontab` (already installed, `celery==5.6.3`; `crontab.from_string` and `remaining_delta` verified locally), `zoneinfo` (stdlib), `httpx`, the PostgreSQL integration fixture (`postgres_container`, `backend/tests/integration/conftest.py:208`), openapi-typescript, Next.js with TanStack Query and Vitest.

**Stacking (read first):**
- This PR stacks on **GOO-318** (`feat/goo-318-archive-deposit`). It does not consume GOO-318 code; it stacks only for the migration chain. If GOO-318 stalls, re-point `down_revision` to the single head and open independently.
- Line numbers below are for `c935dde6e`; re-locate them after each rebase.
- GOO-320 consumes `research_search_execution_results` (`execution_id`, `baseline_execution_id`, `corpus_snapshot`, `delta`, `delta_hash`, `import_receipt_id`) and `search_update_service.accepted_delta(db, collection_id, execution_id)`, both defined here.

### Consumed names

| Consumed | Name (path:line) | Property relied on |
|---|---|---|
| Strategy version (GOO-298) | strategy dict built in `step_executor` (`backend/src/services/research_engine/step_executor.py:290-313`): `schema_version "nous.academic.search-strategy.v1"`, `protocol_version_id`, `effective_plan_hash`, `intended.selected_providers`, `route_limits.requested_results_per_provider`, `strategy_version = "sha256:" + sha256(canonical json without strategy_version)` | Content-addressed; the hash can be recomputed from the stored dict. |
| Strategy journal (GOO-298) | `SearchReceiptJournal` (`backend/src/services/research_engine/search_receipts.py:19`), run manifest key `_search_receipts_v1` (`:16`) with `strategies[strategy_version]` and `executions[...]` | The saved strategy lives in a completed run's manifest. There is **no** strategy table; we pin `(source_run_id, step_id, strategy_version)` and snapshot the dict. |
| Rendered query | the search step output's `query` and `coverage` (`step_executor.py:359-375`) | The query text is not inside the strategy dict (parameters are redacted), so the schedule snapshots it from the step output. |
| Provider fan-out | `discovery.search_sources(connectors, sources, query, max_results, execution_namespace=..., on_page_update=...)` (`backend/src/services/research_engine/discovery.py:160`), `discovery.source_records` (`:120`) | Returns `(sources, coverage)`; coverage has per-provider status, counts and truncation. |
| Connectors | `runs._build_connectors(organization_id)` (`backend/src/api/research_engine/runs.py:312`) | Same connector set as a run. Move it to `discovery` only if mypy forbids importing a router helper from a service (then it is a pure move). |
| Import receipt (GOO-300) | `corpus_service.insert_receipt(db, *, collection_id, actor_user_id, kind, dedup_key, lineage_key, declared, observed, records)` (`backend/src/services/research_engine/corpus_service.py:106`), `uq_research_import_receipt_dedup (collection_id, dedup_key)` and `ck_research_import_receipt_kind` (`kind IN ('file_import','citation_chase')`) (`backend/src/models/research_import.py:65-70`) | A repeated `dedup_key` returns the existing receipt; the kind CHECK must be widened. |
| Identity (GOO-299) | `identity_service.observe_import_records` (`backend/src/services/research_engine/identity_service.py:431`), `live_reports` (`:174`), `ResearchReport.merged_into_report_id`, `ResearchReportIdentifier` (`backend/src/models/research_report.py:54,88`) | Records attach to reports by identifiers only; merges are followed to the live report. |
| Corpus export (GOO-300) | `corpus_export.build_package(db, context)` (`backend/src/services/research_engine/corpus_export.py:616`), `seal` (`:99`), `MAX_EXPORT_RECORDS = 50_000` (`:52`) | Read-only and deterministic; the first baseline is taken from it. |
| Citation chasing | `corpus_service._protocol_requirement` (`:376`), `chase_citations` (`:394`) | Only when the protocol requires it. |
| Crossref | `CrossrefConnector` (`backend/src/services/research_engine/connectors/crossref_connector.py:19`), `provider_http.get` | Does **not** read `update-to` today; Task 4 adds one method. |
| Retraction vocabulary | `PublicationRetractionStatus.UNKNOWN`, `PublicationRetractionCheck.NOT_PERFORMED` (`backend/src/api/evidence/schemas.py:32-41`); `release_rules` `retracted_source` (`backend/src/services/research/release_rules.py:32,211`) | Today nothing performs a publication check; `unknown` is the honest default. |
| Current protocol | `identity_service.current_protocol_version_id` (`:103`) | Approved version or `None`. |
| Beat + claim | `celery_app` `beat_schedule`/`include`/`task_routes`, `timezone="UTC"` (`backend/src/tasks/celery_app.py:31,78,120-197`); `.with_for_update(skip_locked=True)` precedent (`backend/src/services/artifacts/lifecycle.py:141`, `backend/src/services/harness/delivery.py:148`) | Beat runs in UTC; skip-locked claims exist already. |
| Roles/access | `ResearchAction.SUPERVISE`/`EDIT`, `resolve_project` (`project_access.py:28,186`), archived → 409 on non-VIEW (`:161`) | Re-resolved at execution as the schedule owner. |
| Ledger | `lock_aggregate_stream` (`ledger.py:1318`), `append_decision` (`:1321`), `replay_decisions` (`:1420`), `_FAMILIES` (`:2235`), `identity_service._replayed_event` (`:76`) | Caller-owned append. |
| Insert-only trigger (GOO-309) | `prevent_research_insert_only_mutation()` | `55000` on UPDATE/DELETE. |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| **Scheduling primitive** | One Celery beat entry `"search-updates-tick"` every 60 s. The tick does `SELECT` the tip schedule versions that are `enabled` and whose project is live `... FOR UPDATE SKIP LOCKED`, computes the due fire for each, and inserts the execution with `ON CONFLICT (schedule_id, scheduled_local) DO NOTHING`. The lock serializes two beats on the same schedule; the unique key makes the claim idempotent even if the lock is lost. Locking an insert-only row does not update it, so the trigger never fires. No `next_run_at` column is stored; the next fire is derived. | Beat already drives every periodic job here. A DB-claimed row survives a worker restart (the claim is a committed row, not broker state), and two beats or a duplicated broker message produce one execution. Deriving `next_run_at` keeps the schedule insert-only. |
| **Cron + timezone** | `cron` is a 5-field expression parsed with `celery.schedules.crontab.from_string` (422 on parse error; minute field must be a single value, so at most hourly, 422 `Schedules run at most hourly`). `timezone` is an IANA name validated with `zoneinfo.ZoneInfo` (422 otherwise). The fire is computed in local wall time with `crontab.remaining_delta(last_local, tz=ZoneInfo(tz))` and stored twice: `scheduled_local` (naive local ISO minute, the identity) and `scheduled_for` (UTC `timestamptz`). | Users think in local time. The local wall-clock identity means the repeated 01:30 on a DST fall-back day fires **once**; a non-existent 02:30 on spring-forward is normalized forward by `zoneinfo` and fires once. Hourly cap bounds provider load. |
| **Missed-tick policy** | **Coalesce.** If the worker was down across several fires, the tick inserts only the **latest** missed fire, with `missed_fires = n - 1` recorded on the execution. Earlier fires are not backfilled. | A search update is cumulative, so running the latest fire covers the gap. Backfilling would hammer providers and produce identical deltas. Deterministic because "latest fire ≤ now" has one answer. |
| **Overlap policy** | A fire is started only if no earlier execution of the same schedule has a `started` attempt without a terminal attempt and younger than `STALE_EXECUTION = 30 min`. Otherwise the new execution gets a `skipped` attempt with `reason: "overlap"`. A `started` attempt older than 30 min with no terminal attempt is treated as dead: a new `started` attempt is inserted for the same execution. | One running search per schedule; a dead worker cannot wedge a schedule forever (the `sweep_stale_research_runs` lesson). |
| **Pinned strategy** | `POST .../search-schedules` takes `{source_run_id, step_id, strategy_version, cron, timezone, enabled}`. The service loads the run's `_search_receipts_v1.strategies[strategy_version]` and the step output's `query`, recomputes the hash (422 `Strategy hash mismatch` if it differs), and requires `strategy.protocol_version_id == current_protocol_version_id(collection)` (409 `Strategy was built under a superseded protocol; approve an amendment and bind a new strategy`). The snapshot `{strategy, query}` is stored on the schedule version. | "Exact saved strategy version and approved protocol version pinned per schedule." Local saved-search entries are never backfilled as strategies: only a GOO-298 journal entry can be pinned. |
| Editing | Changing `cron`, `timezone` or `enabled` inserts a new schedule version (`supersedes_schedule_version_id` = tip, `UNIQUE(supersedes...)`). Changing the strategy is a new version with a new `strategy_version`, and passes the protocol check above. | A changed plan always needs an approved protocol, then a new binding. |
| Execution-time protocol check | At start the execution rechecks `current_protocol_version_id == schedule.protocol_version_id`; on mismatch it records `failed` `protocol_changed` and runs nothing. | A protocol amendment approved after the schedule was saved must not silently run the old plan. |
| **Authorization at execution** | Each start calls `resolve_project(db, project_id, schedule.owner_id, ResearchAction.EDIT)`. 404 (deleted, foreign, owner removed) → `failed` `owner_access_revoked`; 409 (archived) → `failed` `project_archived`. The owner must also still hold SUPERVISE, else `failed` `owner_role_revoked`. Nothing runs and no import is written. | "Archived/deleted/revoked cannot start." |
| **Idempotent import** | Results are written as one import receipt with new kind `scheduled_search` and `dedup_key = f"scheduled:{execution_id}"`. A retried attempt for the same execution hits `uq_research_import_receipt_dedup` and reuses the receipt. `lineage_key = f"schedule:{schedule_id}"`. `declared` holds the strategy snapshot; `observed` holds the coverage (GOO-298 receipt shape) and the `execution_namespace`. | Repeated imports are idempotent by the existing GOO-300 key. Provider queries and coverage are retained exactly where GOO-300 export already reads them. |
| **Baseline (explicit prior snapshot)** | Every succeeded execution stores `corpus_snapshot`: `{report_id: {identifiers, version_key, publication}}` for the live reports in scope **after** its import. An execution's baseline is the previous succeeded execution of the same schedule (`baseline_execution_id`). The first execution's baseline is a `corpus_snapshot` derived from `corpus_export.build_package` at schedule creation, stored on the schedule's first version with its sealed digest. Over `MAX_EXPORT_RECORDS` → 422 at creation. | "Compare to an explicit prior corpus snapshot (GOO-300 export)." A named baseline row makes each delta reproducible. |
| `version_key` | `sha256` of the canonical `{title, authors, venue, year, abstract_sha256, provider_updated}` from the provider record for that report (`provider_updated` = Crossref `indexed.date-time`, OpenAlex `updated_date`, PubMed `DateRevised` when present, else absent). Missing fields stay absent; they never default. | A change in the provider's own record version is evidence of `changed`. A key built only from what the provider returned avoids inventing changes. |
| **Delta classes** | Per live report in baseline ∪ current results:<br>`new`: report not in baseline (after following merges).<br>`changed`: in both, `version_key` differs; evidence = both keys and the differing fields.<br>`corrected_retracted`: Crossref says so (next row); evidence = notice DOI, `update-to.type`, date, `source: "crossref"`.<br>`unchanged`: in both, same key, no notice.<br>`unknown`: with `reason ∈ {provider_failed, provider_capped, not_returned, no_doi_publication_check_not_performed, merge_unresolved}`.<br>Unchanged title/DOI with a changed key is `changed`, never `unchanged`. | "Never inferred retractions/deletions": a baseline work missing from today's results is `unknown/not_returned`, and a capped provider marks every baseline work it could have returned as `unknown/provider_capped`. |
| **Correction/retraction evidence** | `CrossrefConnector.update_notices(dois)` calls `GET /works?filter=updates:{doi}` (batched, ≤ 20 DOIs per call, the same `provider_http.get` client). A returned work whose `update-to[]` targets the DOI with `type ∈ {retraction, correction, erratum, expression_of_concern, withdrawal, removal}` yields `corrected_retracted` with that type. Only reports with a DOI are checked; others get `publication: {"check": "not_performed"}`. An outage of this call marks those DOIs `unknown/provider_failed`, never `unchanged`. | Crossref `update-to` is the publisher-asserted, machine-readable correction record. A workspace withdrawal (`is_retracted` in the evidence meter) is never consulted. |
| False merges | Classification keys on the GOO-299 report id after merge following; it never merges by title. Two results that share a title but not an identifier stay two reports (one may be `new`). A report merged since the baseline is compared through `merged_into_report_id`; an unresolved split gives `unknown/merge_unresolved`. | Identity decisions stay GOO-299's. |
| Citation chasing | Only when `_protocol_requirement(...)[1]` declares it; the result is a separate `citation_chase` receipt (existing kind) linked from the execution. Otherwise `citation_chasing: "not_required"` is recorded. | "Citation chasing only when protocol-required." |
| Delta hash | `delta_hash = canonical_json_sha256({baseline_execution_id, import_receipt_id, classes})`. The delta export is sealed with `corpus_export.seal`. | GOO-320 accepts a delta by `(execution_id, delta_hash)`. |

**Migration head:** new revision `c2f4b6d8e0a1_create_search_schedules.py`, `down_revision = "b0e2a4c6d8f9"` (GOO-318). It creates the four tables with insert-only triggers and replaces `ck_research_import_receipt_kind` with `kind IN ('file_import','citation_chase','scheduled_search')` (drop + create; `downgrade()` restores the two-value CHECK and fails loudly if `scheduled_search` rows exist). Imports nothing from `src`; copies `_deny_data_api`.

---

### Task 1: Pure rules (`search_update_rules.py`)

**Files:**
- Create `backend/src/services/research_engine/search_update_rules.py`.
- Create `backend/tests/unit/services/test_search_update_rules.py`.

```python
DELTA_CLASSES = ("new", "changed", "corrected_retracted", "unchanged", "unknown")
UNKNOWN_REASONS = ("provider_failed", "provider_capped", "not_returned", "no_doi_publication_check_not_performed", "merge_unresolved")
NOTICE_TYPES = ("retraction", "correction", "erratum", "expression_of_concern", "withdrawal", "removal")
def parse_schedule(cron: str, timezone: str) -> tuple[crontab, ZoneInfo]        # ValueError -> 422
def due_fire(cron, tz, last_local: datetime | None, created_local: datetime, now_utc: datetime) -> tuple[datetime, datetime, int] | None  # (scheduled_local, scheduled_for, missed)
def strategy_hash(strategy: Mapping) -> str                                       # matches step_executor
def version_key(record: Mapping) -> str
def classify(baseline: Mapping[str, Mapping], current: Mapping[str, Mapping], coverage: Mapping, notices: Mapping[str, list], notice_check_failed: set[str]) -> dict
```

**Tests (write first):**
- `test_due_fire_coalesces_missed_ticks_and_counts_them`
- `test_dst_fall_back_fires_once_spring_forward_fires_once` (Europe/London, 01:30 and 02:30)
- `test_sub_hourly_cron_rejected`
- `test_strategy_hash_matches_step_executor_fixture`
- `test_disappeared_work_is_unknown_not_retracted`
- `test_capped_provider_marks_unknown`
- `test_crossref_retraction_notice_classifies_corrected_retracted`
- `test_same_doi_changed_key_is_changed`

**Commit:** `feat(research): pure schedule fire and corpus delta classification rules (GOO-319)`

---

### Task 2: Models + migration

**Files:**
- Create `backend/src/models/research_search_update.py` (`ResearchSearchSchedule`, `ResearchSearchExecution`, `ResearchSearchExecutionAttempt`, `ResearchSearchExecutionResult`); export from `backend/src/models/__init__.py`.
- Modify `backend/src/models/research_import.py`: widen the kind CHECK.
- Create `backend/alembic/versions/c2f4b6d8e0a1_create_search_schedules.py`.
- Modify `backend/tests/integration/test_screening_queue_postgres.py` (`_REBUILT_TABLES`, `_upgrade`).

| Table | Columns | Constraints |
|---|---|---|
| `research_search_schedules` | `id, schedule_id UUID, collection_id FK RESTRICT, owner_id FK users, protocol_version_id FK, source_run_id FK research_runs, step_id VARCHAR(100), strategy_version VARCHAR(80), strategy JSONB, query TEXT, cron VARCHAR(64), timezone VARCHAR(64), enabled BOOL, baseline_snapshot JSONB NULL, baseline_digest CHAR(64) NULL, supersedes_schedule_version_id FK self NULL, created_by_id FK users, created_at` | `UNIQUE(supersedes_schedule_version_id)`; `(supersedes_schedule_version_id IS NULL) = (baseline_snapshot IS NOT NULL)`; `supersedes_schedule_version_id IS NOT NULL OR schedule_id = id` |
| `research_search_executions` | `id, collection_id, schedule_id UUID, schedule_version_id FK, scheduled_local VARCHAR(16), scheduled_for TIMESTAMPTZ, missed_fires INT, created_at` | `UNIQUE(schedule_id, scheduled_local)`; `missed_fires >= 0` |
| `research_search_execution_attempts` | `id, execution_id FK, outcome VARCHAR(16), reason VARCHAR(64) NULL, detail JSONB NULL, worker VARCHAR(100), created_at` | `outcome IN ('started','succeeded','failed','skipped')`; `outcome IN ('started','succeeded') OR reason IS NOT NULL` |
| `research_search_execution_results` | `execution_id PK FK, baseline_execution_id FK research_search_executions NULL, import_receipt_id FK research_import_receipts, chase_receipt_id FK NULL, coverage JSONB, corpus_snapshot JSONB, delta JSONB, delta_hash CHAR(64), created_at` | PK = one result per execution |

The execution row is the claim and is inserted before any work, so its results cannot live on it (the trigger forbids UPDATE). The results row is inserted in the same commit as the `succeeded` attempt. Four insert-only tables; GOO-320 reads `research_search_execution_results`.

**Check:** single head `c2f4b6d8e0a1`; offline `--sql` contains `research_search_executions` and the widened CHECK.
**Commit:** `feat(research): insert-only search schedules, executions, attempts and results (GOO-319)`

---

### Task 3: Ledger family `research_search_update`

**Files:** `ledger.py` (vocabulary, validators, `_FAMILIES` entry: subject `search_schedule`, `requires_subject_version=False`); `test_research_decision_ledger.py`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `search_update.schedule_versioned` | `collection_id, schedule_id, schedule_version_id, supersedes_schedule_version_id, protocol_version_id, strategy_version, cron, timezone, enabled` | `supervisor` |
| `search_update.executed` | `collection_id, schedule_id, execution_id, scheduled_local, import_receipt_id, baseline_execution_id, delta_hash, counts` | `system` (owner attributed) |

**Replay rules:** linear schedule chains; an `executed` event names a schedule version that was the tip at its fire time; `baseline_execution_id` is the previous `executed` of the same schedule (or null for the first); at most one `executed` per `(schedule_id, scheduled_local)`.

**Tests:** `test_search_update_replay_rejects_duplicate_fire`, `test_search_update_replay_rejects_skipped_baseline`.
**Commit:** `feat(research): research_search_update decision family and replay rules (GOO-319)`

---

### Task 4: Crossref update notices

**Files:**
- Modify `backend/src/services/research_engine/connectors/crossref_connector.py`: add `async def update_notices(self, dois: Sequence[str]) -> dict[str, list[dict]]` using `filter=updates:{doi}` and returning `{doi: [{notice_doi, type, date}]}`.
- Tests in `backend/tests/unit/services/test_crossref_update_notices.py` with `httpx.MockTransport` and a recorded Crossref retraction-notice fixture.

**Commit:** `feat(research): Crossref update-to notices for corpus deltas (GOO-319)`

---

### Task 5: Service + beat task

**Files:**
- Create `backend/src/services/research_engine/search_update_service.py`.
- Create `backend/src/tasks/search_update_tasks.py`; register in `celery_app` `include`, `task_routes` (default queue) and `beat_schedule` (`"search-updates-tick": {"task": "src.tasks.search_update_tasks.tick", "schedule": 60.0}`), self-skipping unless `settings.SEARCH_UPDATES_ENABLED` (new bool, default `False`).

```python
AGGREGATE_TYPE = "research_search_update"; SUBJECT_TYPE = "search_schedule"
async def create_schedule(db, context, actor_id, data) -> tuple[ScheduleResponse, bool]   # SUPERVISE; one commit
async def version_schedule(db, context, actor_id, schedule_id, data) -> ScheduleResponse  # edit / enable / disable
async def claim_due(db, now_utc) -> list[UUID]       # FOR UPDATE SKIP LOCKED + ON CONFLICT DO NOTHING; commits
async def run_execution(db, execution_id, connectors_factory) -> str   # access/protocol recheck → search → import → classify → results row + attempt + ledger, one commit
async def list_schedules(db, context) -> ScheduleListResponse          # tips, history, derived status, last execution
async def accepted_delta(db, collection_id, execution_id) -> DeltaResponse   # GOO-320 reads this; foreign -> 404
async def export_delta(db, context, execution_id) -> dict              # sealed nous.academic.search-delta.v1
```

- **`tick`:** `claim_due` then `run_execution` for each claimed id, sequentially, inside one task (`task_soft_time_limit` 300 s applies; a long run is resumed by the stale rule).
- **Derived schedule status:** `disabled` (tip not enabled), `blocked` (last attempt failed with an access or protocol reason), `running`, `ok`, `failed`. Never stored.
- **Provider namespace:** `execution_namespace = f"schedule:{execution_id}:{strategy_version}"`, so GOO-298 page ids are stable across retries of one execution.

**Commit:** `feat(research): scheduled search execution, idempotent import and classified deltas (GOO-319)`

---

### Task 6: API + contracts + bundle part

**Files:**
- Create `backend/src/api/research_engine/search_updates.py` (`SCHEDULES = "/projects/{project_id}/search-schedules"`); register it.
- Schemas in `backend/src/schemas/research_engine.py`.
- `audit_bundle.py`: `_search_updates` → `_sealed_part("search-updates.json", "nous.academic.search-updates.v1", ...)`.
- Regenerate OpenAPI and frontend types.

| Route | Action | Notes |
|---|---|---|
| `GET {SCHEDULES}` | VIEW | Tips, versions, derived status, next fire (local + UTC), last execution with counts per class. |
| `POST {SCHEDULES}` | SUPERVISE | 201; 422 cron/timezone/hash; 409 superseded protocol. |
| `POST {SCHEDULES}/{schedule_id}/versions` | SUPERVISE | Edit/enable/disable; 409 stale tip. |
| `GET {SCHEDULES}/{schedule_id}/executions` | VIEW | Executions with every attempt (failures retained). |
| `GET {SCHEDULES}/executions/{execution_id}/delta` | VIEW | Sealed JSON attachment; `?class=` filter. |

**oasdiff:** additive. **Expected: no ERR.**
**Tests** (`backend/tests/unit/api/test_search_update_routes.py`): `test_reviewer_cannot_create_schedule_403`, `test_owner_without_supervisor_403`, `test_delta_export_sealed_and_read_only`.
**Commit:** `feat(research): search schedule and delta endpoints (GOO-319)`

---

### Task 7: Structural guard

`backend/tests/unit/architecture/test_search_update_boundary.py`:
- **(a)** `search_update_rules.classify` never reads `is_retracted`, `retracted_sources` or any `Document` field (workspace withdrawal ≠ retraction);
- **(b)** no module writes `ResearchSearchSchedule`/`Execution`/`Attempt`/`Result` with `update(`/`delete(`/`merge`;
- **(c)** `search_update_service` imports nothing from `services/research/draft_release_service` (no staleness writes here).

**Commit:** `test(research): guard scheduled-update boundaries (GOO-319)`

---

### Task 8: PostgreSQL proof (one test)

`backend/tests/integration/test_search_updates_postgres.py` (`integration`, `requires_postgres`), reusing `screening_factory`, `_upgrade` (through `c2f4b6d8e0a1`), `seed_approved_protocol_binding`, a completed seeded run whose manifest carries a real `_search_receipts_v1` strategy, and **fake connectors** (crossref, pubmed) whose responses the test controls.

**`test_schedule_claims_once_classifies_and_survives_restart`** runs these steps in order:
1. **Seed:** baseline corpus with reports A (DOI, Crossref), B (DOI), C (PMID only), D (DOI) plus a pair E1/E2 sharing a title but different DOIs.
2. **Create:** a supervisor saves `0 6 * * 1`, `Europe/London`. Hash mismatch → 422; a strategy built under a superseded protocol → 409; a reviewer → 403; foreign → 404. The first version stores `baseline_snapshot` + `baseline_digest`.
3. **Concurrent claim:** two sessions call `claim_due` for the same `now` → exactly one execution row.
4. **Restart:** kill the run after the `started` attempt (raise in the connector). A second `claim_due` inserts nothing. After advancing 31 min, `run_execution` inserts a new `started` attempt on the same execution; both attempts are retained.
5. **Missed ticks:** advance three Mondays with no tick; one tick inserts one execution with `missed_fires = 2`.
6. **Classified delta:** connectors return A unchanged, B with a changed title (same DOI), new F, no D (not returned), PubMed capped; Crossref returns a retraction notice for A's DOI. Expected: A `corrected_retracted` (notice DOI and type retained), B `changed`, F `new`, D `unknown/not_returned`, C `unknown/provider_capped`, E1/E2 stay two reports. No class says `deleted`.
7. **Outage:** Crossref notice call fails on the next execution → every DOI's publication evidence is `unknown/provider_failed`; no `unchanged` from a failed check.
8. **Idempotent import:** rerun `run_execution` for a succeeded execution → no new receipt (same `dedup_key`), no new results row, one `executed` event.
9. **Workspace withdrawal:** mark B's document workspace-withdrawn → B is still not `corrected_retracted`.
10. **Revocation:** archive the workspace → next execution `failed` `project_archived`, no receipt. Remove the owner's supervisor role → `failed` `owner_role_revoked`. Approve a protocol amendment → `failed` `protocol_changed`.
11. **Export:** the delta export reconstructs the class counts and every evidence item; its seal verifies.
12. **Insert-only, replay, downgrade:** UPDATE/DELETE → `55000`; replay passes; downgrade restores the two-value kind CHECK and drops only the four tables.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_search_updates_postgres.py`; without a database **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for scheduled updates and classified deltas (GOO-319)`

---

### Task 9: Frontend (Workflow tab)

**Files:**
- Create `frontend/src/types/api/research-search-update-contract.ts`.
- Modify `frontend/src/services/researchEngineService.ts`: `listSearchSchedules`, `createSearchSchedule`, `versionSearchSchedule`, `listSearchExecutions`, `exportSearchDelta`.
- Create `frontend/src/components/research-engine/SearchSchedulePanel.tsx`, mounted in `frontend/src/components/research-engine/ProjectWorkflow.tsx`: pick a completed run's search strategy, cron + timezone inputs (timezone `<select>` from `Intl.supportedValuesOf('timeZone')`), status badge, next fire in local time, last run with per-class counts, and a delta table where `unknown` rows show their reason and `corrected_retracted` rows link the notice DOI.
- Tests in `__tests__/SearchSchedulePanel.test.tsx`: `unknown shows reason never deleted`, `create hidden without supervisor`, `next fire shown in schedule timezone`.

**Commit:** `feat(frontend): search schedules and corpus deltas in the Workflow tab (GOO-319)`

---

## Mutation verification

Record each in the test docstring and in `docs/testing/agent-orchestration-mutation-checks.md` (GOO-319 section).

| Guard | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| `UNIQUE(schedule_id, scheduled_local)` + `ON CONFLICT DO NOTHING` | plain INSERT, drop index in test migration | `pytest -q backend/tests/integration/test_search_updates_postgres.py` | step 3: two executions |
| Missed-tick coalescing in `due_fire` | return every missed fire | `pytest -q backend/tests/unit/services/test_search_update_rules.py -k coalesces` + step 5 | three executions |
| `not_returned` → `unknown` | classify as `corrected_retracted` | unit `-k disappeared` + step 6 | D retracted |
| Notice-check failure → `unknown` | treat failure as no notices | step 7 | `unchanged` after outage |
| Access recheck at execution | skip `resolve_project` | step 10 | archived project runs |
| Protocol recheck at execution | skip | step 10 | amended protocol runs old plan |
| `dedup_key` per execution | use a random key | step 8 | second receipt |

Restore each guard, confirm an empty `git diff`, rerun green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_search_update_rules.py backend/tests/unit/services/test_crossref_update_notices.py backend/tests/unit/services/test_research_decision_ledger.py
pytest -q backend/tests/unit/services/test_audit_bundle.py backend/tests/unit/api backend/tests/unit/architecture
(cd backend && python ../scripts/ci/check_alembic.py)      # single head c2f4b6d8e0a1
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research-engine
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists:**
- **PostgreSQL proof:** needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **Live provider execution** (real Crossref `filter=updates:`, PubMed, OpenAlex responses, real caps): needs network egress from the worker and provider etiquette config (`mailto`); fakes only prove logic.
- **Beat across a real worker restart:** needs the dev cluster with `SEARCH_UPDATES_ENABLED=true` and a running beat pod.
- **Live journey:** needs a deployed stack with `c2f4b6d8e0a1`.

## Authenticated journey list for Linear closure

On dev after deploy (`alembic current` shows `c2f4b6d8e0a1 (head)`), with `SEARCH_UPDATES_ENABLED=true`.

1. **Create:** a supervisor saves an hourly schedule on the pilot strategy in their timezone; the Workflow tab shows the next fire in local time.
2. **Restart:** restart the beat and worker pods across a fire; the execution list shows one execution for that fire, with any retried attempts.
3. **Delta:** the delta shows new/changed/unknown rows with reasons and evidence; export it and keep the file.
4. **Correction evidence:** a schedule over a DOI with a known Crossref retraction notice shows `corrected_retracted` with the notice DOI.
5. **Deny:** a reviewer cannot create; archiving the workspace blocks the next execution with `project_archived`.
6. **Bundle:** `search-updates.json` is in the audit bundle and verifies.

Record SHA/PR, CI and oasdiff links, the two corpus snapshots and delta export, junit, mutation transcripts and the NOT RUN list.

## Seams

- **Upstream (GOO-298/299/300):** reads the strategy journal and step output; writes only through `insert_receipt` (GOO-300) and therefore `observe_import_records` (GOO-299). Never edits a run manifest.
- **Downstream (GOO-320):** a review update **accepts** one execution's delta by `(execution_id, delta_hash)`. This ticket never creates screening work or stales anything.

## Out of scope

`ponytail:` markers at each seam: sub-hourly schedules; backfilling missed fires; Retraction Watch or PubMed retraction feeds beyond Crossref `update-to`; email/notification on new deltas; per-provider schedules; converting local saved searches into strategies.

# GOO-301 Screening Queues Plan (Academic R3)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist protocol-bound screening queues on the project Workflow surface. A queue is an immutable snapshot of GOO-299 reports at one approved protocol version and one criterion version, for one stage. A supervisor assigns reviewers to it. Each assigned reviewer records independent, append-only observations (`include | exclude | uncertain`), and a protocol-controlled reason is required for a full-text exclusion. AI output lives in a separate table of attributed suggestions and can never become an observation. Reveal, conflicts and adjudication belong to GOO-302. Acquisition and PRISMA counts belong to GOO-303.

**Architecture:** Four new tables and no second ledger. `screening_queues` holds the frozen corpus (`report_ids`), `protocol_version_id`, `criteria_hash` and `stage`. `screening_assignments` holds `(queue, reviewer)` revisions: revoking one and assigning again creates a new row. `screening_observations` is insert-only and supersedes explicitly through `supersedes_observation_id`. `screening_suggestions` holds AI rows attributed to a step, a source and a model. Every human action is also appended to the existing decision ledger (`backend/src/services/research_decisions/ledger.py`) under a new family, `research_screening`, with one stream per queue. Writers serialize on the lock order that already exists: `resolve_project` takes Workspace SHARE, then Collection UPDATE, and reloads roles after the lock (`backend/src/services/research_engine/project_access.py:195-205,260-289`). The screening stream `FOR UPDATE` comes next, and only allocates `seq`.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, PostgreSQL integration test via `postgres_container`/`RESEARCH_DECISION_DATABASE_URL`, SQLite unit tests, openapi-typescript, Next.js with TanStack Query and Vitest.

**Dependencies:** This PR stacks on #1752 (GOO-299 at `4c31aad2c`), which already provides `research_reports`, the `_Family` registry and `lock_aggregate_stream`. It is blocked on GOO-300 in Linear, but it uses nothing from GOO-300 except the migration parent (see "Migration head"). Once GOO-300 lands, abstracts for imported records come from `research_import_records.parsed`. Until then, the only abstract source is `ResearchSource.abstract` (`backend/src/models/research_source.py:20-22`).

---

## Decisions (read before Task 1)

| Question | Decision | Why |
|---|---|---|
| Who creates queues and assignments? | **Supervisor** (`ResearchAction.SUPERVISE` → `ResearchProjectRole.SUPERVISOR`, `project_access.py:37-41`). | Binding a corpus to an approved protocol version is protocol governance, and supervisors already own that (`protocol_service.py:407-408`). Adjudicators resolve the disagreements that assignments produce (GOO-302), so they should not also choose who produces them. |
| Who may submit? | Only a user who holds `REVIEWER` **and** an active assignment on that queue. `resolve_project(..., REVIEW)` returns 403 `reviewer role required` (`project_access.py:287-289`). The service then returns 403 `Not assigned to this queue`. | Ticket: "a role alone grants nothing". A workspace owner or editor has neither the role nor an assignment, so they are refused at the first check. |
| What is the "criterion version"? | `criteria_hash = canonical_hash({"eligibility": snapshot["eligibility"], "full_text_exclusion_reasons": reasons})` from the pinned protocol version (`protocol_service.py:49-71`). It is stored on the queue, and every observation inherits it through its immutable `queue_id`. | Protocol versions are immutable (`research_protocol.py:97-135`). The hash identifies the criteria even across amendments that did not change eligibility, which GOO-302 needs when it compares queues. |
| Where do exclusion reasons live? | `snapshot["selection"]["full_text_exclusion_reasons"]`: a list of 1–50 unique, trimmed strings of at most 200 characters. `selection` is already a required free-form section (`backend/src/schemas/research_engine.py:285-305`). | No protocol schema change. A `full_text` queue on a protocol without the list gets 422 `Protocol defines no full-text exclusion reasons`. |
| Reviewer mode (amended for GOO-302) | `snapshot["reviewer_mode"]["mode"] ∈ {"single","dual_independent"}` on the pinned version. `reviewer_mode` is already a required free-form section (`backend/src/schemas/research_engine.py:292`). Queue creation rejects a missing or other value with 422 `Protocol declares no screening reviewer mode`, and `screening.queue_created` carries it at schema 1. | GOO-302 derives the required observation count and reveal from it. Replay needs it in event 1. |
| Protocol binding | The create request names `protocol_version_id`. It must be the **current approved** version of a protocol in this Collection. Otherwise the response is 404 for a foreign or missing version and 409 `Protocol version is not current` for a stale one. | `identity_service._protocol_version_id` guesses "latest updated protocol" (`identity_service.py:99-112`), which is ambiguous when a project has several protocols. A queue must be exact. |
| Corpus | `report_ids` JSONB (ordered, 1–10,000). If `title_abstract` omits the list, the queue takes every live report (`merged_into_report_id IS NULL`), ordered by `created_at, id`. `full_text` requires an explicit list (GOO-302 will supply the included set). Ids are validated with `identity_service._live_reports` (`:156-193`): foreign ids give 404 and merged ids give 409. | `# ponytail: JSONB array, one row per queue; add screening_queue_items if a queue exceeds ~10k reports or per-report assignment is needed.` |
| Assignment granularity | One assignment covers the whole queue. | Dual independent screening means every assigned reviewer screens every report. `# ponytail: per-report split when a queue is too big for one reviewer.` |
| Stale state → 409 (explicit reconciliation) | Each check has its own stable detail string. **Queue superseded**: another queue names this one in `supersedes_queue_id`. **Protocol changed**: the protocol's `current_approved_version_id` is no longer the queue's version. **Criteria stale**: the request's `criteria_hash` does not match the queue's. **Report merged**: the report's `merged_into_report_id` is set. **Assignment revoked**: the request's `assignment_id` is no longer active. **Observation exists**: the request's `supersedes_observation_id` is not the reviewer's current observation. | "Reconciliation" means a supervisor creates a new queue with `supersedes_queue_id`, and old observations stay bound to the old queue. Nothing is carried over silently. `UNIQUE(supersedes_queue_id)` prevents two reconciliations of the same queue. |
| Duplicate/concurrent submission | The same `idempotency_key` with the same fingerprint replays: 200 with the same observation. The same key with a different body gets 409 `Idempotency conflict`. A different key without `supersedes_observation_id`, when a current observation already exists, gets 409. The DB also enforces it: a partial unique index on `(queue_id, report_id, reviewer_id) WHERE supersedes_observation_id IS NULL`, plus `UNIQUE(supersedes_observation_id)`, which keeps the chain linear. | The Collection `FOR UPDATE` serializes the two requests. The service checks happen under that lock, and the indexes catch anything that bypasses the service. |
| AI vs human | Rows in `screening_suggestions` record `step_id`, `source_id`, `model_id` and no user. Suggestions are imported only when a queue is created (`suggestion_step_id`, `title_abstract` only), from a completed `screen` step of this project (`step_executor.py:871-966`, where `output["screening"]` is at `:939-942` and item shape `contracts.py:78-82`). They are never returned by the reviewer endpoints. | The ledger needs `actor_user_id NOT NULL FK users` (`backend/src/models/research_decision.py:67-69`), so AI output structurally cannot author a decision. The one observation writer requires a `ProjectContext` plus a human `actor_user_id` that holds an assignment. Hiding suggestions keeps human judgment independent, and GOO-302 owns reveal. |
| Ledger aggregate | `aggregate_type="research_screening"`, `aggregate_id=queue_id`, `subject_type="screening_queue"`, `subject_id=queue_id`, `requires_subject_version=False`, and `subject_hash = decision_request_fingerprint(payload)` (the same rule as identity events, `ledger.py:166-171`). | A per-queue stream replays one queue on its own. The Collection lock already serializes writers across queues. |
| Existing run-gate screening | Untouched. `ScreeningItemDecision` / `research_stage_reviews` (`schemas/research_engine.py:653-669`, `models/research_stage_review.py:1`) gate a *run's* stage output and use a different vocabulary (`unresolved`). | Queues are project-level, not run-level. Merging the two concepts is GOO-302's call. |

**Migration head:** at plan time `(cd backend && python ../scripts/ci/check_alembic.py)` → `single head 'c9d2e4f6a8b1', 85 revisions`. The new revision is `e1f3a5c7d9b2` (at most 32 characters, `scripts/ci/check_alembic.py:40`). Its `down_revision` always names this PR's **direct stack parent**:
- `c9d2e4f6a8b1` while this PR is stacked only on #1752.
- `d4e6f8a0b2c3` once GOO-300 (whose plan pins that revision on `c9d2e4f6a8b1`) is merged or stacked beneath this branch.

#1747 (`b7c4e1d9a2f6`) and #1753 (`c9d1e2f3a4b5`) both set `down_revision = "merge_daily_harness_20260928"`, the same parent as `c9d2e4f6a8b1` (checked on their branches). Whichever of #1747, #1753 and #1752 merges later has to re-point its own `down_revision` to the head `develop` has by then. That is always the lowest unmerged PR in its stack, never this one. After every rebase, run `check_alembic.py`. If it reports two heads and one of them is ours, the parent PR has not been re-pointed yet: fix it there, and never add a merge revision here. `backend/tests/unit/ci/test_daily_research_brief_migration.py:66-85` already asserts one head plus ancestry, so it needs no edit.

---

### Task 1: Pure rules module (reasons, criteria hash, observation validation)

**Files:**
- Create `backend/src/services/research_engine/screening_rules.py`.
- Create `backend/tests/unit/services/test_screening_rules.py`.

```python
STAGES = ("title_abstract", "full_text"); DECISIONS = ("include", "exclude", "uncertain")
MODES = ("single", "dual_independent")                                   # (amended for GOO-302)

def reviewer_mode(snapshot: Mapping[str, Any]) -> str                   # ValueError unless reviewer_mode.mode ∈ MODES (amended for GOO-302)

def exclusion_reasons(snapshot: Mapping[str, Any]) -> list[str]         # [] if absent; ValueError if malformed
def criteria_hash(snapshot: Mapping[str, Any]) -> str                   # canonical_hash, see Decisions
def validate_observation(stage: str, decision: str, reason: str | None, reasons: Sequence[str]) -> None
    # full_text+exclude ⇒ reason ∈ reasons; any other combination ⇒ reason is None; ValueError otherwise
def suggestion_rows(step_output: Mapping[str, Any]) -> list[tuple[str, str, str]]
    # (source_id, "include"|"exclude", reason) from output["screening"]; include if ANY part of a source is included
```

**Step 1: write the failing tests.**
- `test_full_text_exclude_requires_protocol_reason`
- `test_reason_outside_protocol_list_rejected`
- `test_title_abstract_rejects_any_reason`
- `test_include_and_uncertain_reject_reason`
- `test_duplicate_or_blank_reasons_are_malformed`
- `test_criteria_hash_ignores_non_eligibility_sections` (it changes when `eligibility` or the reasons change and not when `sources_search` changes)
- `test_suggestion_rows_any_part_included`
- `test_reviewer_mode_missing_or_unknown_is_malformed` (amended for GOO-302)

**Step 2: run them.** `pytest -q backend/tests/unit/services/test_screening_rules.py` should fail with `ModuleNotFoundError`.

**Step 3: implement** the module, pure and with no DB access.

**Step 4: rerun** until green.

**Step 5: commit.** `feat(research): pure screening rules (GOO-301)`

---

### Task 2: Models + migration

**Files:**
- Create `backend/src/models/screening.py`.
- Modify `backend/src/models/__init__.py` to export it.
- Create `backend/alembic/versions/e1f3a5c7d9b2_create_screening_queues.py`.
- Modify `backend/tests/integration/test_report_identity_postgres.py:105-106`: drop the screening tables **before** the identity tables. Their FKs to `research_reports` otherwise make `DROP TABLE research_reports` fail.
- Modify `backend/tests/integration/test_academic_wave_migrations.py:470-500` in the same way (`_IDENTITY_TABLES` drop loop).

Follow the model style of `research_report.py` (plain `Base`, no soft delete, `_created_at`, `research_report.py:31-38`). Each column is justified against the acceptance bullet it serves:

```python
class ScreeningQueue(Base):              # screening_queues — "queue bound to Collection, snapshot, stage"
    id
    collection_id FK collections RESTRICT          # tenant scope; every child is reached via queue
    protocol_version_id FK research_protocol_versions RESTRICT   # "at which protocol version"
    criteria_hash String(64)                       # "criterion version"
    stage String(16) CHECK IN ('title_abstract','full_text')      # "stage"
    report_ids JSONB NOT NULL                      # "immutable corpus snapshot / stable report id"
    supersedes_queue_id FK screening_queues RESTRICT NULL UNIQUE  # "explicit reconciliation"
    created_by_id FK users RESTRICT, created_at
    INDEX(collection_id)

class ScreeningAssignment(Base):         # screening_assignments — "explicit role + assignment"
    id, queue_id FK RESTRICT, reviewer_id FK users RESTRICT, assigned_by_id FK users RESTRICT, created_at
    revoked_at TIMESTAMPTZ NULL, revoked_by_id FK users NULL        # "stale assignment revision → 409"
    CHECK ((revoked_at IS NULL) = (revoked_by_id IS NULL))
    UNIQUE INDEX (queue_id, reviewer_id) WHERE revoked_at IS NULL   # one live revision per reviewer

class ScreeningObservation(Base):        # screening_observations — insert-only
    id, queue_id FK RESTRICT
    report_id FK research_reports RESTRICT          # stable GOO-299 id
    reviewer_id FK users RESTRICT                   # "actor"; needed by the partial unique index
    assignment_id FK screening_assignments RESTRICT # which assignment revision authorized it
    decision String(16) CHECK IN ('include','exclude','uncertain')
    exclusion_reason String(200) NULL, CHECK (decision = 'exclude' OR exclusion_reason IS NULL)
    note Text NULL
    supersedes_observation_id FK screening_observations RESTRICT NULL UNIQUE   # "supersede, never overwrite"
    created_at                                      # "time"
    UNIQUE INDEX (queue_id, report_id, reviewer_id) WHERE supersedes_observation_id IS NULL
    INDEX(queue_id, reviewer_id)

class ScreeningSuggestion(Base):         # screening_suggestions — "AI = attributed suggestion rows only"
    id, queue_id FK RESTRICT, report_id FK research_reports RESTRICT
    source_id FK research_sources RESTRICT, step_id FK research_steps RESTRICT, model_id String(100) NULL
    decision String(16) CHECK IN ('include','exclude'), reason Text NULL, created_at
    UNIQUE(queue_id, step_id, source_id)
```

The following were left out on purpose because each can be derived from the queue: `collection_id` on child tables, `protocol_id`, `corpus_hash`, and a per-observation `criteria_hash`. Also left out: an `exclusion_reasons` copy, since it lives in the immutable protocol version snapshot.

Partial indexes declare **both** `postgresql_where=` and `sqlite_where=`. Without `sqlite_where`, SQLite unit tests would build a full unique index and reject every legitimate supersede.

Migration: create the four tables in dependency order, then call `_deny_data_api` on each (copied from `c9d2e4f6a8b1_create_report_identities.py:20-32`). The downgrade drops them in reverse order.

**Steps:**
1. `check_alembic.py` → `single head 'e1f3a5c7d9b2'`.
2. `alembic upgrade head --sql` renders the four `CREATE TABLE`s, working offline with no Docker (the gotchas feedback).
3. `pytest -q backend/tests/unit/architecture`.
4. Commit: `feat(research): screening queue tables (GOO-301)`

---

### Task 3: Ledger family `research_screening`

**Files:**
- Modify `backend/src/services/research_decisions/ledger.py`: add the vocabulary next to `_IDENTITY_PAYLOAD_KEYS` (`:50-73`), `_validate_screening_payload`, `_validate_screening_transitions`, and an entry in `_FAMILIES` (`:620-635`).
- Modify `backend/tests/unit/services/test_research_decision_ledger.py`.

`_validate_event` (`:117-171`) and `replay_decisions` (`:439-522`) are already family-generic, so neither changes. The protocol-only check at `:153-156` is keyed on `_PROTOCOL_AGGREGATE`.

| event_type (schema 1) | payload keys | actor_role |
|---|---|---|
| `screening.queue_created` | `collection_id, queue_id, stage, protocol_version_id, criteria_hash, report_ids, exclusion_reasons, supersedes_queue_id, reviewer_mode` (`reviewer_mode` amended for GOO-302) | supervisor |
| `screening.assigned` / `screening.unassigned` | `collection_id, queue_id, assignment_id, reviewer_id` | supervisor |
| `screening.observed` | `collection_id, queue_id, observation_id, assignment_id, reviewer_id, report_id, decision, exclusion_reason` | reviewer |
| `screening.superseded` | the `observed` keys plus `superseded_observation_id` | reviewer |

The note goes in the event's `reason` column. Event `reason` already holds free text for identity events.

`_validate_screening_payload` checks the following:
- All ids are UUID strings.
- `queue_id == aggregate_id == subject_id`.
- `stage` and `decision` are in the `screening_rules` vocabularies, and so is `reviewer_mode` (amended for GOO-302).
- `report_ids` is a non-empty list of unique UUIDs.
- `exclusion_reasons` is a list of strings.

`_validate_screening_transitions` is the replay rule, and it is self-contained because the first event carries the corpus and the reasons:
1. Event 1 is `queue_created` and happens exactly once.
2. `assigned` requires no active assignment for that reviewer. `unassigned` requires the named assignment to be active.
3. `observed` and `superseded` require all of these:
   - the assignment is active and belongs to `reviewer_id`;
   - `report_id` is in `report_ids`;
   - `validate_observation(stage, decision, exclusion_reason, exclusion_reasons)` passes.
4. `observed` also requires that the pair has no current observation.
5. `superseded` also requires that `superseded_observation_id` is the current observation for `(reviewer, report)`.

Any violation raises `DecisionReplayError`, for example `"screening observation outside corpus"` or `"contradictory screening supersession"`.

**Step 1: write the failing tests.**
- `test_screening_payload_rejects_foreign_queue`
- `test_screening_replay_rejects_observation_without_assignment`
- `test_screening_replay_rejects_second_initial_observation`
- `test_screening_replay_rejects_reason_not_in_protocol`
- `test_screening_replay_accepts_assign_observe_supersede_unassign`

**Step 2: run them.** They fail with `unsupported decision event 'screening.queue_created'`.

**Step 3: implement.**

**Step 4: check.** The unit ledger file and `test_research_protocol_service.py` pass, and so does `test_research_identity_service.py` (the protocol and identity families are unchanged).

**Step 5: commit.** `feat(research): screening events in the decision ledger (GOO-301)`

---

### Task 4: Screening service

**Files:**
- Create `backend/src/services/research_engine/screening_service.py`.
- Modify `backend/src/schemas/research_engine.py`: append the schemas after `IdentityEventResponse` (`:433-440`).
- Create `backend/tests/unit/services/test_screening_service.py` (SQLite, following `test_research_identity_service.py`).

The service never commits. The route owns the single transaction, the same way `identities.py:85-104` does. Every function receives a `ProjectContext` that `resolve_project` has already locked, and it looks up the queue with `WHERE id = :qid AND collection_id = context.collection.id`. A missing or foreign queue returns 404 `Screening queue not found`.

```python
async def create_queue(db, context, actor_user_id, data: ScreeningQueueCreate) -> ScreeningQueueResponse
async def assign(db, context, queue_id, actor_user_id, data: ScreeningAssignmentCreate) -> ScreeningAssignmentResponse
async def revoke(db, context, queue_id, assignment_id, actor_user_id, data: ScreeningRevokeRequest) -> ScreeningAssignmentResponse
async def list_queues(db, context, user_id) -> list[ScreeningQueueResponse]          # counts only, no decisions
async def my_queue(db, context, queue_id, user_id) -> MyScreeningQueueResponse
async def submit(db, context, queue_id, actor_user_id, data: ScreeningObservationCreate) -> ScreeningObservationResponse
async def history(db, context, queue_id) -> list[IdentityEventResponse]              # replay_decisions
```

Every mutation follows the same order:
1. `stream = lock_aggregate_stream(... "research_screening", queue_id)`.
2. Replay check by idempotency key, copying `identity_service._replayed_event` (`:72-96`). If a replay is found, load the row named in its payload and return it without re-applying.
3. Validations.
4. Insert the row.
5. `append_decision(...)`, with the fingerprint taken over `{queue_id, actor, body}`.

`create_queue` locks a stream keyed by the new `uuid4()` it generates. It then performs these checks:
- The protocol version is current-approved and in this Collection.
- The stage fits the protocol reasons.
- `reviewer_mode(snapshot)` is valid. Otherwise it returns 422 `Protocol declares no screening reviewer mode` (amended for GOO-302).
- The corpus is validated with `_live_reports`.
- `supersedes_queue_id`, if given, is the same Collection and stage and has not been superseded already. Otherwise it returns 409 `Screening queue already reconciled`.

Its idempotency key is scoped per Collection: the service first queries `research_decision_events` for `event_type='screening.queue_created' AND collection_id=... AND idempotency_key=...`. If `suggestion_step_id` is given, the step must satisfy all of the following:
- it joins `ResearchStep → ResearchRun → ResearchBlueprint → ResearchProject.collection_id == context.collection.id`, with the same join as `project_access.require_step` (`:447-468`);
- `step_type == "screen"`;
- `completed_at` is set.

Otherwise it returns 404 or 422. Each `source_id` maps to a report through `research_report_observations`, following `merged_into_report_id` to the survivor. Rows whose report is not in the corpus are skipped and counted in `suggestions_skipped`.

`assign`:
- 422 `User is not an eligible reviewer` unless the target currently holds `REVIEWER` on the Collection (same eligibility query as `projects.py:372-438`).
- 409 `Reviewer already assigned` if an active assignment already exists.

`submit` (REVIEW context):
1. 403 `Not assigned to this queue` unless `assignment_id` names this reviewer's assignment on this queue.
2. 409 `Assignment revoked` if that assignment has been revoked.
3. The stale checks from the Decisions table, in the order queue → protocol → criteria → report.
4. `validate_observation` → 422 on failure.
4a. (amended for GOO-302) 409 `Report resolved; reopen to change` when the report has a current resolution. The order and detail string are fixed here. GOO-302 implements the check together with `screening_resolutions`, because GOO-301 has no resolved state.
5. The current observation is the row for `(queue, report, reviewer)` that no other row supersedes (`NOT EXISTS`). If one exists, `supersedes_observation_id` must equal its id. Otherwise the response is 409 `Observation exists; supersede the current observation`.
6. Insert the observation and append `screening.observed` or `screening.superseded`.

`my_queue` resolves with VIEW, then requires REVIEWER in `context.effective_roles` (as `identity_service._require_role` does, `:115-117`) plus an active assignment. It returns, per report:
- `report_id`, `title_snapshot` and identifiers;
- the abstract, taken from the first observed `ResearchSource.abstract` in `created_at, id` order (this is where the GOO-300 import record fallback slots in);
- the caller's **own** current observation only;
- queue metadata: `stage`, `criteria_hash`, `exclusion_reasons`, `protocol_version_id`, and a `stale` detail (the same strings as the 409s, or `null`);
- `counts {total, screened, remaining}`, computed over the caller's own observations only.

`list_queues` returns per-queue `report_count`, `assignment_count` and `observation_count`. It never returns decision breakdowns, which GOO-302 owns.

Request schemas:
- `ScreeningQueueCreate{protocol_version_id, stage: Literal[...], report_ids: list[UUID] | None (≤10_000), supersedes_queue_id: UUID | None, suggestion_step_id: UUID | None, idempotency_key: str(1..240)}`
- `ScreeningAssignmentCreate{reviewer_user_id, idempotency_key}`
- `ScreeningRevokeRequest{reason: str(1..10_000), idempotency_key}`
- `ScreeningObservationCreate{report_id, assignment_id, criteria_hash: str(64), decision: Literal[...], exclusion_reason: str(1..200) | None, note: str(≤10_000) | None, supersedes_observation_id: UUID | None, idempotency_key}`

Response schemas: `ScreeningQueueResponse`, `ScreeningAssignmentResponse`, `ScreeningObservationResponse`, `MyScreeningQueueResponse{queue, assignment_id, items, counts}`.

**Unit tests (SQLite).** The service is called directly with a hand-built `ProjectContext`.
- `test_ai_suggestions_never_become_observations`: creating a queue with `suggestion_step_id` writes N suggestion rows, zero observations and zero `screening.observed` events, and `my_queue` exposes no suggestion field.
- `test_submit_requires_human_actor_with_assignment`: a context holding REVIEWER but no assignment gets 403, and an owner context with no roles gets 403 at the route (Task 5).
- `test_full_text_reason_validated_against_protocol_list`: a reason outside the list gets 422.
- `test_changed_decision_supersedes_and_keeps_history`: two rows, the first unchanged, the second pointing at the first. `history()` replays both.
- `test_stale_criteria_hash_is_409`.
- `test_queue_without_reviewer_mode_is_422` (amended for GOO-302).

**Gates:** `ruff check backend/src`, plus `mypy --ignore-missing-imports --follow-imports=silent` on the three added files (the added-file gate in `docs/engineering/backend.md` Ratchets).

**Commit:** `feat(research): screening queue service (GOO-301)`

---

### Task 5: Routes + OpenAPI/TypeScript

**Files:**
- Create `backend/src/api/research_engine/screening.py` (`APIRouter(prefix="/research-engine", tags=["research-engine-screening"])`, transport only, following `identities.py`).
- Modify `backend/src/api/research_engine/__init__.py:5,19` and `backend/src/main.py:83,671-672` to register the router with `prefix="/api/v1"`.
- Create `backend/tests/unit/api/test_research_screening_routes.py`, following `test_research_identity_routes.py`.
- Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

| Method + path | `resolve_project` action | Service | Commit |
|---|---|---|---|
| `GET /projects/{project_id}/screening/queues` | VIEW | `list_queues` | no |
| `POST /projects/{project_id}/screening/queues` | SUPERVISE | `create_queue` | yes |
| `POST /projects/{project_id}/screening/queues/{queue_id}/assignments` | SUPERVISE | `assign` | yes |
| `POST /projects/{project_id}/screening/queues/{queue_id}/assignments/{assignment_id}/revoke` | SUPERVISE | `revoke` | yes |
| `GET /projects/{project_id}/screening/queues/{queue_id}/mine` | VIEW (+ role/assignment in service) | `my_queue` | no |
| `POST /projects/{project_id}/screening/queues/{queue_id}/observations` | REVIEW | `submit` | yes |
| `GET /projects/{project_id}/screening/queues/{queue_id}/history` | VIEW | `history` | no |

The `mine` endpoint uses VIEW because REVIEW is a mutating action (`project_access.py:42`). With REVIEW, a plain read would take row locks and return 409 on an archived project.

Route tests (with `resolve_project` and the service monkeypatched):
- action mapping per route;
- each mutation commits exactly once;
- reads never commit;
- an owner with no role gets 403 from POST observations;
- a foreign queue gets a stable 404 body.

Then run `python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types` followed by `--check`. The changes are additive only, so the PR needs no `api-breaking-approved` label.

**Commit:** `feat(research): screening queue API (GOO-301)`

---

### Task 6: One real PostgreSQL integration test

**Files:**
- Create `backend/tests/integration/test_screening_queue_postgres.py` (marker `integration`).
- Modify `backend/tests/integration/research_engine_postgres_support.py:143-260`: add a keyword `snapshot: Mapping[str, Any] = _PROTOCOL_SNAPSHOT` to `seed_approved_protocol_binding`, used at `:246` and in the hash at `:168`.

Fixture `screening_factory` copies `identity_factory` (`test_report_identity_postgres.py:84-110`):
1. Run `create_all`.
2. Drop the screening tables, then the identity tables.
3. Run `c9d2e4f6a8b1.upgrade`, then `d4e6f8a0b2c3.upgrade` if that file is present, then `e1f3a5c7d9b2.upgrade`.

This proves our migration against real Postgres.

The seed reuses `_seed` from `test_report_identity_postgres.py:113-185`: owner O, reviewer R, adjudicator A, role-less viewer V, foreign F, plus a run. On top of that it adds:
- a SUPERVISOR role for O;
- a second reviewer R2 (a VIEWER member with the REVIEWER role);
- an approved protocol whose snapshot has `selection.full_text_exclusion_reasons = ["wrong population", "wrong design"]`;
- three reports r1–r3 created through `identity_service.observe_sources`.

`_wait_until_blocked` is copied from `:463-472`.

1. **`test_commit_reopen_and_event_state_atomicity`**
   1. O creates a `title_abstract` queue and assigns R and R2. R submits r1 `include` and R2 submits r1 `exclude` with no reason. R supersedes r1 with `uncertain` plus a note.
   2. In a **new session**, the rows reload: 3 observations, 1 superseded. `history()` replays 6 events with contiguous `seq`.
   3. Atomicity: monkeypatch `append_decision` to raise after the observation flush. The transaction rolls back, and both the observation count and `next_seq` are unchanged.
   4. A full-text queue on `[r2]`: `exclude` with a reason outside the list returns 422, and `exclude` with `"wrong design"` returns 200.
2. **`test_concurrent_duplicate_submission_yields_one_observation`**
   1. Two sessions each run `resolve_project(REVIEW)` followed by `submit` with the same body and key. Session 2 blocks on the Collection lock, which `_wait_until_blocked` proves.
   2. The result is exactly 1 observation and 1 `screening.observed` event, and both calls return the same id.
   3. The same race with **different** keys: the second call gets 409 `Observation exists…` and there is still exactly 1 row.
   4. A direct `INSERT` of a second initial observation raises `IntegrityError` naming the partial unique index.
3. **`test_role_revocation_race_denies_submission`**, following `test_research_authorization_concurrency.py:78-122`:
   1. O runs `resolve_project(MANAGE)` and soft-deletes R's REVIEWER assignment, holding the Collection lock.
   2. R's `resolve_project(REVIEW)` blocks. O commits.
   3. R gets 403 `reviewer role required`, and no observation or event is written.
   4. Separately, O revokes R2's *queue assignment*. R2 submitting with the old `assignment_id` gets 409 `Assignment revoked`.
4. **`test_archived_and_foreign_project`**
   1. An archiver session updates `collections.research_status='archived'` while holding `FOR UPDATE`. R's submit blocks, and then gets 409 `Archived projects are read-only` (`project_access.py:219-235`).
   2. User F, calling with the queue id, gets 404 `Project not found`.
   3. A queue id from another Collection gets 404 `Screening queue not found`.
   4. After the protocol's `current_approved_version_id` moves to a new version, submission gets 409 `Protocol version changed; reconcile queue`. When O creates a queue with `supersedes_queue_id`, the new queue accepts R's submission and the old queue still returns 409.

**Mutation verification**, following the procedure in `docs/engineering/testing.md` and recorded in the test docstring:
- **Guard: the current-observation check in `screening_service.submit`.** Comment it out. `-k concurrent_duplicate` should then fail with an `IntegrityError` on `uq_screening_observation_initial` instead of the expected 409. Restore it, confirm `git diff --exit-code backend/src/services/research_engine/screening_service.py`, and rerun until green.
- **Guard: the idempotency replay check in `submit`.** Comment it out. The same-key race should then fail with 409 `Observation exists…` where the test expects the replayed id.
- **Guard: the post-lock role reload.** Already mutation-verified in `test_research_authorization_concurrency.py`. Do not repeat it here, just cite it.

**Run:** `pytest -q backend/tests/integration/test_screening_queue_postgres.py`. This needs testcontainers or `RESEARCH_DECISION_DATABASE_URL`. Locally, report it as `NOT RUN` if neither is available. The CI Integration job runs it.

**Commit:** `test(research): PostgreSQL proof for screening queue guards (GOO-301)`

---

### Task 7: Frontend queue panel (Workflow tab)

**Files:**
- Create `frontend/src/types/api/research-screening-contract.ts`, aliasing `components['schemas'][...]` the way `research-identity-contract.ts` does.
- Modify `frontend/src/services/researchEngineService.ts`: add seven thin `api.get`/`api.post` functions after the identity block (`:197-228`).
- Create `frontend/src/components/research-engine/ScreeningQueuePanel.tsx`.
- Modify `frontend/src/components/research-engine/ProjectWorkflow.tsx:149`: render `<ScreeningQueuePanel projectId={project.id} approvedProtocolVersionId={approvedProtocolVersionId} roles={roles.data ?? []} readOnly={archived} />` after `ReportIdentityPanel` (`approvedProtocolVersionId` is at `:40`, `roles` at `:81`).
- Create `frontend/src/components/research-engine/__tests__/ScreeningQueuePanel.test.tsx`.
- Modify `frontend/src/components/research-engine/__tests__/ProjectWorkflow.test.tsx:28-40`: add `vi.mock('../ScreeningQueuePanel')` and the new service functions to the mock.

The panel is one `<section>` styled like `ReportIdentityPanel`. It has four parts:
- **Queue list:** `useQuery(['screening-queues', projectId])`, showing stage, report count, observation count and created date.
- **Supervisor controls:** shown when the caller holds SUPERVISOR. They cover creating a `title_abstract` queue on `approvedProtocolVersionId`, an assign `<select>` limited to users holding `reviewer` in `roles`, and a revoke button. `# ponytail: full-text queue creation is API-only until GOO-302 supplies the included set.`
- **My queue:** `useQuery(['screening-queues', projectId, queueId, 'mine'])`. It shows the header counts (`screened / total`) and one row per report: title, clamped abstract, identifiers, and Include / Exclude / Uncertain buttons. On a `full_text` exclude, a `<select>` of `exclusion_reasons` is required. A note `<textarea>` is available on every row. If the reviewer already has an observation for the report, the buttons read "Change" and send `supersedes_observation_id`. Each submit uses `idempotency_key = crypto.randomUUID()`, generated once per click so that a retry replays.
- **Errors:** `role="alert"` with the API detail. When `mine.queue.stale` is set, the buttons are disabled and the text reads "This queue is stale: {detail}. A supervisor must reconcile it."

All mutations invalidate `['screening-queues', projectId]`.

Tests:
- rows render from mocked `getMyScreeningQueue`;
- Exclude on full text is blocked until a reason is chosen;
- a submit sends `criteria_hash`/`assignment_id` and invalidates;
- a stale queue disables the buttons;
- a non-supervisor sees no create/assign controls.

**Run:**
- `pnpm --dir frontend exec vitest run src/components/research-engine/__tests__/ScreeningQueuePanel.test.tsx src/components/research-engine/__tests__/ProjectWorkflow.test.tsx`
- `pnpm --dir frontend type-check`
- `scripts/ci/run_local_ci.sh --frontend` (the `lint:changed` ratchet)

**Commit:** `feat(frontend): screening queue panel in project workflow (GOO-301)`

---

### Task 8: Gates + PR

1. `scripts/ci/run_local_ci.sh --base origin/feat/goo-299-study-identity --frontend` must be all green. That run covers:
   - ruff;
   - black/isort on changed files;
   - mypy on added files;
   - `check_alembic.py`;
   - OpenAPI drift;
   - the unit suites.
2. `pytest -q backend/tests/unit/services/test_screening_rules.py backend/tests/unit/services/test_screening_service.py backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/api/test_research_screening_routes.py backend/tests/unit/architecture` must pass.
3. Open the PR against `feat/goo-299-study-identity` (retarget it to `develop` after #1752 merges, and to GOO-300's branch if that is stacked). Its description includes:
   - the mutation-verification transcript for both guards;
   - the `oasdiff` changelog, which should list only added paths and schemas;
   - the migration re-point rule above.

---

## Authenticated journey list for Linear closure

These steps run against `rag-dev` after deploy. They are blocked until a backend origin is reachable (see the hard blocker in `docs/plans/2026-09-29-academic-r0-r1-closure.md`). Log in as `allocs16@gmail.com` on a project where that user holds SUPERVISOR, a second account holds REVIEWER, and the protocol is approved with `full_text_exclusion_reasons` and `reviewer_mode={"mode":"dual_independent"}` (amended for GOO-302).

1. **Migration:** `kubectl -n rag-dev exec deploy/backend -- alembic current` shows `e1f3a5c7d9b2 (head)`.
2. **Queue creation:** as the supervisor, in the Workflow tab, create a title/abstract queue. The response is 201, the queue lists N reports, and `GET .../history` shows `screening.queue_created` with `protocol_version_id` and `criteria_hash`.
3. **Assignment:** assign the reviewer (200). Assigning an account that has no reviewer role returns 422.
4. **Reviewer journey:** as the reviewer, open "My queue". Capture a screenshot showing title, abstract, identifiers and `0 / N`. Include one report, exclude another, and mark a third uncertain with a note. Reload the browser and confirm the counts read `3 / N`. Change one decision, then check that `history` shows `screening.superseded` pointing at the original, and that both rows are present. (amended for GOO-302) Change works only before the report is resolved. Once GOO-302 is deployed, a resolved report gets 409 `Report resolved; reopen to change`, and changing it requires an adjudicator reopen. So on the dual protocol, make the change before the second reviewer submits that report.
5. **Full text:** use `POST` to create a full-text queue on two report ids.
   - `exclude` without a reason returns 422.
   - `exclude` with a reason outside the protocol list returns 422.
   - `exclude` with a listed reason returns 200.
6. **Denials:**
   - The owner submits without the reviewer role: 403 `reviewer role required`.
   - A reviewer who is not assigned: 403 `Not assigned to this queue`.
   - A user from another org: 404.
   - Any submission on an archived project: 409.
7. **Idempotency:**
   - Replay the same submission body and key: 200 with the same observation id.
   - The same key with a different decision: 409.
   - A new key without `supersedes_observation_id`: 409 `Observation exists…`.
8. **Staleness:**
   - Approve a protocol amendment. The old queue's submit returns 409 `Protocol version changed; reconcile queue`, and the panel shows the stale banner.
   - The supervisor reconciles by creating a queue with `supersedes_queue_id`. The new queue accepts submissions.
   - Revoke the reviewer's assignment. Submitting with the old `assignment_id` returns 409 `Assignment revoked`.
9. **AI separation:**
   - Create a queue with `suggestion_step_id` pointing at a completed screen step.
   - `SELECT count(*) FROM screening_suggestions WHERE queue_id=…` returns N or more.
   - `SELECT count(*) FROM screening_observations WHERE queue_id=…` returns 0.
   - "My queue" JSON contains no suggestion field.
10. **CI:** record the Integration job URL where `test_screening_queue_postgres.py` passed at the merge SHA, and paste the mutation-verification transcript into the PR.

---

## Amendment — 2026-09-29 (implementation review)

- **`GET .../history` is supervisor-only.** The service requires SUPERVISOR,
  and a reviewer gets 403 `supervisor role required`. Screening events carry
  every reviewer's `decision`, `exclusion_reason` and note, so VIEW would leak
  peers' decisions before reveal. GOO-302 replaces this gate with per-viewer
  redaction through `visible_observation_ids()` (its "History redaction"
  decision), after which `history` can return to VIEW.
- **Service-level role checks.** `create_queue`, `assign` and `revoke` require
  SUPERVISOR, and `submit` requires REVIEWER, in the service as well as in
  `resolve_project`. Assignment eligibility also checks the project
  organization, as `projects.py` does for roles.
- **Unique-index backstop.** SQLSTATE 23505 on insert becomes a stable 409 and
  never a 500. Other integrity errors are re-raised.
- **Ledger.** `screening.queue_created` (schema 1) also carries
  `suggestions_skipped`. Replay requires an observation's actor to be its
  reviewer.
- **Migration.** `e1f3a5c7d9b2` also creates
  `idx_research_decision_event_idempotency (collection_id, event_type,
  idempotency_key)` for the per-Collection create-idempotency lookup.
- **Archive race detail.** A submission that waited on the lock while the
  project was archived gets 409 `Project is not writable` (from
  `lock_active_project`), not `Archived projects are read-only`.


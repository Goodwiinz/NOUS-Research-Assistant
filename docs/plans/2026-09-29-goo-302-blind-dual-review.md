# GOO-302 Blind Dual Review and Adjudication Plan (Academic R3)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build on the GOO-301 screening queues. The approved protocol declares the reviewer mode, `single` or `dual_independent`. Until a report is revealed in a queue, each reviewer sees only their own observation, on every read path. Once the required observations exist, the server derives agreement or conflict exactly once and records it in an insert-only resolution row. An adjudicator (a human with `ADJUDICATE`) resolves a conflict against the exact input observation ids, and stale inputs get 409. Adjudication and reopen are ledger events. None of them mutates an observation.

**Architecture:** One new table and no new ledger. `screening_resolutions` is an insert-only chain per `(queue, report)`, linked by `supersedes_resolution_id`. The current resolution for a report is the chain tip. Its `basis` is one of `single | agreement | conflict | adjudicated | reopened`. Nothing is written back into an existing row. The outcome is computed by one pure function, `screening_rules.derive`, which the service uses to write and the ledger replay uses to check. **The reveal predicate lives in one place**: an observation is visible to user *u* iff `observation.reviewer_id == u` or its id appears in `input_observation_ids` of any resolution row in the queue. `my_queue`, `history`, `conflicts` and anything added later all go through `visible_observation_ids()`. The GOO-301 order serializes writers: `resolve_project` takes Workspace SHARE, then Collection UPDATE, and reloads roles after the lock (`backend/src/services/research_engine/project_access.py:196-205,173,289`). The queue's `research_screening` stream is then locked `FOR UPDATE` (`backend/src/services/research_decisions/ledger.py:300-342`).

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, PostgreSQL integration tests (`postgres_container`/`RESEARCH_DECISION_DATABASE_URL`), SQLite unit tests, openapi-typescript, Next.js with TanStack Query and Vitest.

**Dependencies:** This stacks on GOO-301 (`docs/plans/2026-09-29-goo-301-screening-queues.md`, branch `feat/goo-301-screening-queues`). The plan exists but is not implemented yet. It assumes GOO-301's tables, the `research_screening` family (GOO-301 plan :139-173), the service functions (:201-209), and routes (:281-289). The GOO-301 plan already carries this ticket's changes to it (each marked "(amended for GOO-302)"): `reviewer_mode` in `screening.queue_created` at schema 1, the 422 for a missing or invalid mode at queue creation, and the 409 on a resolved report in `submit`. The resolution table, `derive` and reopen stay here. Blocked on GOO-301 only.

---

## Decisions (read before Task 1)

| Question | Decision | Why |
|---|---|---|
| Where does the reviewer mode live? | `snapshot["reviewer_mode"]["mode"] ∈ {"single","dual_independent"}` on the queue's pinned `protocol_version_id`. `reviewer_mode` is already a required free-form protocol section (`backend/src/schemas/research_engine.py:292,301`). The test fixture already stores `{"mode": "single"}` (`backend/tests/integration/research_engine_postgres_support.py:48`). GOO-301's `create_queue` already rejects any other value with 422 `Protocol declares no screening reviewer mode` (GOO-301 amendment). | No protocol schema change and no new column: the version is immutable (`backend/src/models/research_protocol.py:97-135`), so the mode is derived from `queue.protocol_version_id`. Changing the mode needs an amendment, which makes the queue stale (GOO-301 → reconcile). `test_protocol_approval_atomicity.py:46` uses `"independent"`, but that is an approval-only fixture and no queue is created from it, so it is left alone. |
| Required observations | `single` → 1 and `dual_independent` → 2 *fresh current* observations from distinct reviewers. **Fresh** means current (not superseded) and not referenced by any earlier resolution row for that `(queue, report)`. | "Fresh" makes reopen work without guessing. After a reopen, old inputs stay revealed, but each reviewer has to supersede with a new observation before the report can resolve again. |
| "Final" | Every submitted, current observation counts as final. GOO-301 has no draft state, and a change before reveal is a supersede. | No new state machine. |
| Derivation (`derive`) | With fewer than the required fresh observations, derive returns nothing. Otherwise the basis is `single` or `agreement` when every input has the same `(decision, exclusion_reason)` and the decision is not `uncertain`, and the outcome is that decision. Any other combination is basis `conflict` with a null outcome. | For full-text, differing exclusion reasons count as a conflict because PRISMA (GOO-303) needs one reason. An `uncertain` goes to a human adjudicator and is never auto-resolved. |
| Reveal condition (precise) | A report is **revealed** in a queue iff a resolution row exists for `(queue, report)`. The row is written in the same transaction as the submission that brings the fresh count up to the required count. There is no other reveal action. The inputs of every resolution row are visible to every project VIEW member from then on. Everything else, including a peer's superseded pre-reveal observations, is visible only to its author. | One predicate covers all read paths. An orphaned dual report (a reviewer left) is handled by a supervisor unassigning them and assigning a replacement (GOO-301), whose observation completes the required count. |
| After resolution | A submission on a report whose tip exists and is not `reopened` gets 409 `Report resolved; reopen to change`. This check runs in `submit` before GOO-301's current-observation check. | Reveal is irreversible. Changing a vote after reveal has to go through a recorded `screening.reopened`. GOO-301 reserves this step and its detail string. The check is implemented here because the resolution table lands here. |
| Who adjudicates / reopens | Only `ResearchAction.ADJUDICATE` → `adjudicator role required` 403 (`project_access.py:37-41,289`). An adjudicator who authored one of the inputs gets 403 `Adjudicator reviewed this report`. Owners and editors hold no implicit role, and GOO-301's supervisor powers (queues and assignments) grant no decisions. | Ticket: "owners/editors are not implicit adjudicators." Seeing the role list (`GET /roles`, VIEW, `backend/src/api/research_engine/projects.py:349-356`) grants no decision visibility. Decisions go through the reveal predicate only. |
| Adjudication request | `resolution_id` (the conflict tip the adjudicator saw), `input_observation_ids`, `criteria_hash`, `decision`, `exclusion_reason`, `rationale` (required), and `idempotency_key`. The request gets 409 `Adjudication inputs are stale` unless `resolution_id` is the current tip **and** the sorted ids equal the tip's inputs **and** each input is still current. A tip whose basis is not `conflict` gets 409 `Report is not in conflict`. GOO-301's stale checks (queue → protocol → criteria) still apply. `validate_observation` is reused to check the decision and reason, and a failure gets 422. | The observation id *is* the version, because observations are immutable (GOO-301 plan :105-116). |
| AI cannot adjudicate | Every writer requires a `ProjectContext` for `current_user` holding ADJUDICATOR. The ledger enforces `actor_user_id NOT NULL FK users` (`backend/src/models/research_decision.py:67-69`), and no agent tool imports `screening_service` (the gate in Task 6). | Structural rule, no runtime flag. |
| Is the outcome an editable field? | No update path exists: the model has no mutator, the service has no UPDATE, and no route edits it. `screening_rules.derive` is the only producer. Replay re-derives every auto resolution and raises `DecisionReplayError("screening resolution does not match inputs")` on drift. | Ticket: "derived, never stored as an editable field." `# ponytail: insert-only by service contract + replay check; add a DB trigger if a second writer ever appears.` |
| Ledger events | The GOO-301 family `research_screening` is extended. `screening.adjudicated` and `screening.reopened` are added at schema 1, each with actor_role `adjudicator`. An auto-derived resolution gets **no** event of its own. Its row's `event_id` names the `screening.observed`/`superseded` event that triggered it, and replay re-derives it. `screening.queue_created` already carries `reviewer_mode` at schema 1 (GOO-301 amendment). | Replay stays self-contained: event 1 carries the mode, the corpus and the reasons, as GOO-301 already requires (plan :163-171). |
| History redaction | `GET .../history` returns `ScreeningEventResponse(IdentityEventResponse)` plus `redacted: bool = False`. For an `observed`/`superseded` event whose `observation_id` the caller cannot see, the payload drops `decision` and `exclusion_reason`, `reason` (the note) becomes `None`, and `redacted` is `True`. `IdentityEventResponse` exposes no `subject_hash`/`request_fingerprint` (`backend/src/schemas/research_engine.py:433-440`). | Otherwise a peer could brute-force the three decisions against a fingerprint. If a hash field is ever added to this response, it must be redacted as well. |
| Other read paths | `list_queues` stays counts-only and adds `resolved_count` and `conflict_count`, which count revealed rows only. The corpus export from GOO-300 contains no screening data: its `decisions[]` is `identity_service.history` only (GOO-300 plan :237), and identity history replays only the `research_identity` stream (`backend/src/services/research_engine/identity_service.py:715-728`). No SSE/WebSocket path emits research decisions (`grep -rln "research_decision\|research_screening" backend/src/services/websocket backend/src/api` finds nothing outside `research_engine`). | Every path is listed and proven empty in Task 5. Anything that adds screening to an export or a stream must call `visible_observation_ids`. |
| Changed criteria | Unchanged: a supervisor creates a new queue with `supersedes_queue_id` (GOO-301 plan :26). The old queue's resolutions and events stay as they are, and the new queue starts with no resolutions. | Criteria are fixed per queue, so a change is a new queue, never an edit. |

**Migration head:** GOO-301 pins `e1f3a5c7d9b2` (with `down_revision` `c9d2e4f6a8b1`, or `d4e6f8a0b2c3` once GOO-300 is below it; GOO-301 plan :32-36). The new revision is `f3b5d7e9a1c4_create_screening_resolutions.py` (12 characters, within the 32 allowed by `scripts/ci/check_alembic.py:40`), with `down_revision = "e1f3a5c7d9b2"`. The current head at plan time is `c9d2e4f6a8b1`, with 85 revisions (checked by `check_alembic.py`). **Re-pointing:** `down_revision` always names GOO-301's revision id, because this PR's direct stack parent is GOO-301. If GOO-301's revision id changes during review, change this file's `down_revision` to the new id in the same rebase. If `check_alembic.py` reports two heads and one of them is `f3b5d7e9a1c4`, the parent chain has not been re-pointed yet. Fix it in the lowest unmerged PR (#1747 / #1753 / #1752 / GOO-300 / GOO-301 rule, GOO-301 plan :36). Never add a merge revision here.

---

### Task 1: Pure rules (`derive`, mode, visibility)

**Files:**
- Modify `backend/src/services/research_engine/screening_rules.py` (created by GOO-301 Task 1).
- Modify `backend/tests/unit/services/test_screening_rules.py`.

```python
REQUIRED = {"single": 1, "dual_independent": 2}   # keys == GOO-301 screening_rules.MODES
BASES = ("single", "agreement", "conflict", "adjudicated", "reopened")

def derive(mode: str, fresh: Sequence[Obs]) -> Derived | None
    # Obs = (id, reviewer_id, decision, exclusion_reason); None if len(distinct reviewers) < required
    # Derived = (basis, outcome|None, exclusion_reason|None, sorted input ids)
def visible(observation_reviewer: UUID, observation_id: UUID, viewer: UUID, revealed: set[UUID]) -> bool
```

**Step 1: write the failing tests.**
- `test_single_mode_resolves_on_first_observation`
- `test_dual_needs_two_distinct_reviewers`
- `test_dual_agreement_same_decision_and_reason`
- `test_full_text_different_reasons_is_conflict`
- `test_uncertain_never_auto_resolves`
- `test_visible_only_own_or_revealed`

**Step 2: run them.** `pytest -q backend/tests/unit/services/test_screening_rules.py` should fail on `ImportError`.

**Step 3: implement.** Keep it pure, with no DB access.

**Step 4: rerun** until green.

**Step 5: commit.** `feat(research): screening derivation and reveal rules (GOO-302)`

---

### Task 2: `screening_resolutions` model + migration

**Files:**
- Modify `backend/src/models/screening.py` (GOO-301) by adding `ScreeningResolution`, and export it from `backend/src/models/__init__.py`.
- Create `backend/alembic/versions/f3b5d7e9a1c4_create_screening_resolutions.py`.
- Modify the table-drop order in `backend/tests/integration/test_report_identity_postgres.py:101-102` and `backend/tests/integration/test_academic_wave_migrations.py:470-490`: drop `screening_resolutions` **first**, before GOO-301's screening tables. Its FKs otherwise block those drops.

```python
class ScreeningResolution(Base):          # screening_resolutions — insert-only
    id
    queue_id FK screening_queues RESTRICT
    report_id FK research_reports RESTRICT               # "per report"; stage = queue.stage
    basis String(16) CHECK IN BASES
    outcome String(16) NULL CHECK IN ('include','exclude','uncertain')
    exclusion_reason String(200) NULL
    CHECK ((basis IN ('single','agreement','adjudicated')) = (outcome IS NOT NULL))
    CHECK (outcome = 'exclude' OR exclusion_reason IS NULL)
    input_observation_ids JSONB NOT NULL               # exact inputs; observation id == version
    criteria_hash String(64) NOT NULL                  # "criterion version" (ticket), copied from queue
    supersedes_resolution_id FK screening_resolutions RESTRICT NULL UNIQUE   # linear chain
    event_id FK research_decision_events RESTRICT NOT NULL  # provenance: observed/superseded/adjudicated/reopened
    created_at
    UNIQUE INDEX uq_screening_resolution_initial (queue_id, report_id) WHERE supersedes_resolution_id IS NULL
    INDEX (queue_id, report_id)
```

Left out on purpose: the actor and the rationale, because both are on `event_id`. `collection_id` is reachable through the queue.

The partial index declares both `postgresql_where=` and `sqlite_where=` (the GOO-301 plan :127 rule). The migration creates the table and applies `_deny_data_api` (copied from `c9d2e4f6a8b1_create_report_identities.py:20-32`). The downgrade drops the table.

**Steps:**
1. `(cd backend && python ../scripts/ci/check_alembic.py)` → `single head 'f3b5d7e9a1c4'`.
2. `alembic upgrade head --sql` renders the table offline (no Docker).
3. `pytest -q backend/tests/unit/architecture`.
4. Commit: `feat(research): screening resolution table (GOO-302)`

---

### Task 3: Ledger vocabulary + replay

**Files:**
- Modify `backend/src/services/research_decisions/ledger.py`: GOO-301's screening payload keys, `_validate_screening_payload` and `_validate_screening_transitions`. The `_FAMILIES` entry itself (`:620-635`) does not change.
- Modify `backend/tests/unit/services/test_research_decision_ledger.py`.

| event_type (schema 1) | payload keys | `reason` |
|---|---|---|
| `screening.adjudicated` | `collection_id, queue_id, report_id, resolution_id, conflict_resolution_id, input_observation_ids, criteria_hash, decision, exclusion_reason` | rationale (required) |
| `screening.reopened` | `collection_id, queue_id, report_id, resolution_id, reopened_resolution_id` | rationale (required) |

`_validate_event` rejects a missing `reason` for these two events. Add one family-specific check next to the protocol check at `ledger.py:153-156`.

The replay state machine (`_validate_screening_transitions`) extends GOO-301's with a per-report `tip` (basis plus inputs) and a `consumed` id set:
1. `observed`/`superseded` while a tip is resolved (not `reopened`) → `"screening observation after resolution"`. After each one, `derive(mode, fresh)` runs, with `mode` taken from event 1's `reviewer_mode`. A result sets the tip (auto) and adds its inputs to `consumed`.
2. `adjudicated` requires all of the following:
   - the tip basis is `conflict`;
   - `conflict_resolution_id` is the tip;
   - the sorted `input_observation_ids` equal the tip's inputs;
   - the actor is not an input's reviewer;
   - `validate_observation` passes.
3. `reopened` requires a tip that is not `reopened`.

Every violation raises `DecisionReplayError`.

**Tests (failing first):**
- `test_replay_rederives_agreement_and_conflict`
- `test_replay_rejects_observation_after_resolution`
- `test_replay_rejects_adjudication_with_stale_inputs`
- `test_replay_rejects_self_adjudication`
- `test_replay_accepts_reopen_then_fresh_resolution`
- `test_adjudicated_requires_reason`

**Run:** `pytest -q backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/services/test_research_protocol_service.py backend/tests/unit/services/test_research_identity_service.py`. The protocol and identity families must stay green.

**Commit:** `feat(research): adjudication and reopen events (GOO-302)`

---

### Task 4: Service (reveal-aware reads, derivation, adjudication)

**Files:**
- Modify `backend/src/services/research_engine/screening_service.py` (GOO-301).
- Modify `backend/src/schemas/research_engine.py`, adding the new schemas after GOO-301's screening schemas.
- Modify `backend/tests/unit/services/test_screening_service.py`.

The service never commits: the route owns the one transaction, as in GOO-301. Every mutation keeps the GOO-301 order: lock the stream → idempotent replay (`identity_service._replayed_event`, `:72-96`) → validations → insert → `append_decision`.

```python
async def visible_observation_ids(db, queue_id, viewer_id) -> set[UUID]     # the one predicate
async def _tip(db, queue_id, report_id) -> ScreeningResolution | None       # row not named by any supersedes_resolution_id
async def _derive_and_record(db, queue, report_id, event) -> ScreeningResolution | None
async def conflicts(db, context, queue_id, user_id) -> list[ScreeningConflictResponse]
async def adjudicate(db, context, queue_id, report_id, actor_user_id, data) -> ScreeningResolutionResponse
async def reopen(db, context, queue_id, report_id, actor_user_id, data) -> ScreeningResolutionResponse
```

Changes to GOO-301 functions:
- **`submit`** runs the GOO-301 steps 1–4. Step 4a, reserved by GOO-301, is implemented here: a tip that exists and is not `reopened` → 409 `Report resolved; reopen to change`. Step 5 is GOO-301's current-observation check. Step 6 inserts and appends, **then** calls `_derive_and_record(...)` in the same transaction, after the insert and still under the same locks. The response gains `resolution: ScreeningResolutionResponse | None`, which is set only when this submission revealed the report.
- **`my_queue`** gives each item three extra fields:
  - `reveal_state: Literal["hidden","revealed"]` (`revealed` iff a tip exists);
  - `others: list[ScreeningObservationResponse]`, filtered by `visible_observation_ids` and never containing the caller's own observations;
  - `resolution` (the tip, or `null`).

  The `counts` gain `revealed` and `conflicts`.
- **`list_queues`** adds `resolved_count` and `conflict_count` per queue, counted from tip rows only.
- **`history`** returns `ScreeningEventResponse`. Events whose observation is not visible are redacted as described in Decisions.

New functions:
- **`conflicts`** resolves with VIEW, then `_require_role(context, ADJUDICATOR)` (`identity_service.py:115-117`). VIEW is used because `ADJUDICATE` is a mutating action (`project_access.py:42`) that locks rows and returns 409 on an archived project. The function lists tips whose basis is `conflict`, each with its input observations (all revealed by construction), the report title and identifiers, and the queue's `criteria_hash`/`exclusion_reasons`.
- **`adjudicate`** takes an `ADJUDICATE` context and runs these checks in order:
  1. the queue in this Collection (404);
  2. GOO-301's queue → protocol → criteria stale checks (409);
  3. the tip is `resolution_id` and its sorted inputs equal the request and are all current, else 409 `Adjudication inputs are stale`;
  4. the tip basis is `conflict`, else 409 `Report is not in conflict`;
  5. the actor is not an input's reviewer, else 403;
  6. `validate_observation`, else 422.

  It then appends `screening.adjudicated` and inserts a resolution with basis `adjudicated`, `supersedes_resolution_id=tip.id`, the same inputs, and `event_id` set.
- **`reopen`** takes an `ADJUDICATE` context. The tip must exist and not be `reopened`, else 409 `Report is not resolved`. It inserts a resolution with basis `reopened`, a null outcome and inputs `[]`.

**Schemas** (additive):
- `ScreeningResolutionResponse{id, report_id, basis, outcome, exclusion_reason, input_observation_ids, criteria_hash, supersedes_resolution_id, created_at}`
- `ScreeningConflictResponse{report_id, title_snapshot, resolution, observations}`
- `ScreeningAdjudicateRequest{resolution_id, input_observation_ids: list[UUID] (1..10), criteria_hash: str(64), decision, exclusion_reason: str(1..200)|None, rationale: str(1..10_000), idempotency_key: str(1..240)}`
- `ScreeningReopenRequest{resolution_id, rationale, idempotency_key}`
- `ScreeningEventResponse(IdentityEventResponse){redacted: bool = False}`
- The GOO-301 response models gain only optional or defaulted fields.

**Unit tests** (SQLite, with a hand-built `ProjectContext`):
- `test_pre_reveal_peer_observation_absent_from_mine_and_history`
- `test_second_dual_submission_creates_one_resolution_and_reveals`
- `test_submit_after_resolution_409_until_reopen`
- `test_reopen_requires_fresh_observations_from_both`
- `test_adjudicate_stale_inputs_409`
- `test_adjudicator_who_reviewed_is_403`
- `test_owner_without_role_cannot_list_conflicts`
- `test_single_mode_resolves_immediately`

**Gates:** `ruff check backend/src`. mypy runs only on added files, and this task adds none (the migration is covered in Task 2).

**Commit:** `feat(research): blind reveal, derived resolutions and adjudication service (GOO-302)`

---

### Task 5: Routes + OpenAPI/TypeScript

**Files:**
- Modify `backend/src/api/research_engine/screening.py` (GOO-301). It stays transport-only, and the routes register through GOO-301's router with no `main.py` edit.
- Modify `backend/tests/unit/api/test_research_screening_routes.py`.
- Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

| Method + path (under `/api/v1/research-engine`) | `resolve_project` action | Service | Commit |
|---|---|---|---|
| `GET /projects/{pid}/screening/queues/{qid}/conflicts` | VIEW (+ ADJUDICATOR in the service) | `conflicts` | no |
| `POST /projects/{pid}/screening/queues/{qid}/reports/{rid}/adjudicate` | ADJUDICATE | `adjudicate` | yes |
| `POST /projects/{pid}/screening/queues/{qid}/reports/{rid}/reopen` | ADJUDICATE | `reopen` | yes |
| GOO-301 `mine`, `history`, `queues`, `observations` | unchanged | response additive | unchanged |

**Route tests:**
- the action mapping for each route;
- one commit per mutation, and none on reads;
- an owner with no role gets 403 on `adjudicate` and on `conflicts`;
- a foreign queue gets a stable 404 body;
- the `history` response model is `ScreeningEventResponse`.

**Then run** `python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types`, followed by `--check`. The change is additive (new paths, new optional fields), so no `api-breaking-approved` label is needed. The oasdiff changelog goes into the PR.

**Commit:** `feat(research): adjudication and reveal-aware screening API (GOO-302)`

---

### Task 6: One real PostgreSQL integration test

**Files:**
- Create `backend/tests/integration/test_screening_blind_review_postgres.py` (marker `integration`).

The fixture `blind_factory` copies GOO-301's `screening_factory`:
1. `create_all`;
2. drop `screening_resolutions`, then the GOO-301 tables, then the identity tables;
3. run the upgrades `c9d2e4f6a8b1` → (`d4e6f8a0b2c3` if the file is present) → `e1f3a5c7d9b2` → `f3b5d7e9a1c4`.

The seed reuses GOO-301's seed. The principals are:
- supervisor O (the owner), who holds no ADJUDICATOR role;
- reviewers R and R2;
- adjudicator A;
- a role-less viewer V;
- a foreign user F.

The protocol snapshot uses `reviewer_mode={"mode":"dual_independent"}` plus the GOO-301 exclusion reasons, passed through the `snapshot=` keyword that GOO-301 adds to `seed_approved_protocol_binding`. `_wait_until_blocked` is copied from `test_research_authorization_concurrency.py:65-74`.

1. **`test_pre_reveal_redaction_on_every_read_path`**
   1. On a full-text dual queue, R has **not** submitted r1. R2 excludes r1 with the reason "wrong design" and the note `R2-SENTINEL-7f3`, then supersedes it once (two hidden rows). R submits a different report, r3, so R holds an assignment and a `mine` view. r1 has one fresh observation and stays unrevealed.
   2. As R, every response body is serialized with `json.dumps(model_dump(mode="json"))`. That covers `my_queue`, `history`, `list_queues`, `conflicts` (403), and the corpus `build_package` when `backend/src/services/research_engine/corpus_export.py` exists (otherwise that leg is reported as `NOT RUN (GOO-300 not in base)`).
   3. None of those bodies contains `R2-SENTINEL-7f3`, R2's `decision`, or R2's observation ids as `others`.
   4. The history event is `redacted=True` and has no `decision` key.
   5. As A, `conflicts` is `[]`. As V, `history` is redacted the same way.
2. **`test_simultaneous_final_submissions_one_resolution`** (dual mode, R and R2 with conflicting decisions on r2)
   1. Session 1 runs `resolve_project(REVIEW)` and `submit` for R without committing.
   2. The session 2 task (R2) is started, and `_wait_until_blocked` proves it waits on the lock.
   3. Commit session 1, await session 2, and commit it.
   4. The result is exactly one `screening_resolutions` row for `(q, r2)`, with basis `conflict`, inputs equal to `{R.obs, R2.obs}`, and `event_id` equal to R2's `screening.observed` event.
   5. Both reviewers' `my_queue` now shows `revealed`, and `history()` replays cleanly.
   6. A direct `INSERT` of a second initial resolution raises `IntegrityError` on `uq_screening_resolution_initial`.
3. **`test_adjudication_stale_input_and_races`**
   1. A reads the conflict on r2 (tip T1).
   2. A reopens: T2 `reopened`.
   3. R and R2 supersede with fresh, still conflicting, observations, which produces T3 `conflict`.
   4. Adjudicating with T1 and its old ids gets 409 `Adjudication inputs are stale`, and no row or event is written.
   5. Adjudicating T3 with the current ids and a rationale returns 200 with basis `adjudicated`. None of the four observations changed: their counts and columns match a snapshot taken before.
   6. **Role revocation race:** O runs `resolve_project(MANAGE)`, soft-deletes A's ADJUDICATOR assignment, and holds the lock. A's `adjudicate` blocks (proven with `_wait_until_blocked`). O commits, and A gets 403 `adjudicator role required` with nothing written.
   7. **Archive race:** an archiver holds `collections FOR UPDATE` and sets `research_status='archived'`. A's `reopen` blocks, then gets 409 `Archived projects are read-only` (`project_access.py:232-235`).
   8. **Foreign:** F on `conflicts` or `adjudicate` gets 404 `Project not found`. A queue from another Collection gets 404 `Screening queue not found`.

**Mutation verification** follows `docs/engineering/testing.md` and is recorded in the module docstring:
- **Guard: the GOO-301 lock order.** Neutralize both `.with_for_update(of=Collection)` at `project_access.py:173` and the stream `.with_for_update()` at `ledger.py:328`. `-k simultaneous` must then fail: `_wait_until_blocked` times out with "session 2 never blocked", or 0 resolutions are observed, because both submissions derived from a one-observation view. Restore both lines, check `git diff --exit-code` on both files, and rerun until green.
- **Guard: the stale-input comparison in `adjudicate`.** Comment it out. `-k stale` must then fail because it gets 200 where the test expects 409. Restore it and rerun.
- **Guard: the redaction in `history`.** Return the raw payload instead. `-k redaction` must then fail on the sentinel assertion. Restore it and rerun.
- **Post-lock role reload:** already mutation-verified in `test_research_authorization_concurrency.py:78-122`. Cite it and do not repeat it.

**Run:** `pytest -q backend/tests/integration/test_screening_blind_review_postgres.py`. It needs testcontainers or `RESEARCH_DECISION_DATABASE_URL`, and must be reported as `NOT RUN` locally if neither is available. The CI Integration job runs it.

**AI gate:** add this assert to `backend/tests/unit/architecture/test_maintenance_contracts.py`:
```python
assert not any("screening_service" in p.read_text() for p in Path("backend/src/services/agent").rglob("*.py"))
```
It guards against an agent tool that writes screening decisions.

**Commit:** `test(research): PostgreSQL proof for blind reveal and adjudication (GOO-302)`

---

### Task 7: Minimal frontend

**Files:**
- Modify `frontend/src/types/api/research-screening-contract.ts` (GOO-301): alias the new schemas.
- Modify `frontend/src/services/researchEngineService.ts`: add `listScreeningConflicts`, `adjudicateScreening`, and `reopenScreening` as thin `api.get`/`api.post` calls.
- Modify `frontend/src/components/research-engine/ScreeningQueuePanel.tsx` (GOO-301): for each row, when `reveal_state === 'hidden'`, render the muted text "Other reviewers' decisions are hidden until reveal." When the row is revealed, render `others` (the decision and reason per reviewer) and a resolution badge (`Agreed: include` / `Conflict` / `Adjudicated: exclude — wrong design` / `Reopened`). When the report is resolved, disable the decision buttons and show the title "Resolved — an adjudicator must reopen".
- Create `frontend/src/components/research-engine/ScreeningConflictsPanel.tsx`. It renders only when `roles` include `adjudicator`, and is mounted in `ProjectWorkflow.tsx` next to GOO-301's `ScreeningQueuePanel` (after `:149`). Per queue it shows a list of conflicts with both observations side by side. The adjudicate form holds decision buttons, a reason `<select>` for full-text exclude, and a required rationale `<textarea>`. It sends `resolution_id`, `input_observation_ids`, `criteria_hash` and `crypto.randomUUID()` as the idempotency key. A Reopen button prompts for a rationale. A 409 stale error renders in `role="alert"` with "Inputs changed — reload conflicts", and the list refetches.
- Create `frontend/src/components/research-engine/__tests__/ScreeningConflictsPanel.test.tsx`.
- Modify `__tests__/ScreeningQueuePanel.test.tsx` and `__tests__/ProjectWorkflow.test.tsx` (add a mock for the new panel).

All mutations invalidate `['screening-queues', projectId]`. Colors come from theme classes only, with no hex values.

**Tests:**
- a hidden row renders the "hidden until reveal" text and no peer decision;
- a revealed row renders peers and the badge;
- a non-adjudicator gets no conflicts panel;
- adjudicate is blocked until a rationale is entered and sends the exact ids;
- a 409 shows the alert and refetches.

**Run:**
- `pnpm --dir frontend exec vitest run src/components/research-engine/__tests__/ScreeningConflictsPanel.test.tsx src/components/research-engine/__tests__/ScreeningQueuePanel.test.tsx src/components/research-engine/__tests__/ProjectWorkflow.test.tsx`
- `pnpm --dir frontend type-check`

`# ponytail: no new protocol-editor UI; reviewer_mode is set through ProtocolPanel's existing free-form JSON section (frontend/src/components/research-engine/ProtocolPanel.tsx:28).`

**Commit:** `feat(frontend): blind-review states and adjudicator conflicts panel (GOO-302)`

---

### Task 8: Gates + PR

1. `scripts/ci/run_local_ci.sh --base origin/feat/goo-301-screening-queues --frontend` must be all green. It covers:
   - ruff, plus black and isort on changed files;
   - mypy on the added migration (the only added `.py` under `backend/src`);
   - `check_alembic.py` → `single head 'f3b5d7e9a1c4'`;
   - OpenAPI drift;
   - the unit suites;
   - `lint:changed`.
2. `pytest -q backend/tests/unit/services/test_screening_rules.py backend/tests/unit/services/test_screening_service.py backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/api/test_research_screening_routes.py backend/tests/unit/architecture` must be green.
3. Open the PR against `feat/goo-301-screening-queues`, and retarget it down the stack as each parent merges. The description includes:
   - the three mutation transcripts;
   - the oasdiff changelog (additive only);
   - the migration re-point rule;
   - the amendment note that `single`-mode changes now require reopen.

---

## Authenticated journey list for Linear closure

These run on `rag-dev` after deploy. They are blocked until a backend origin is reachable (`docs/plans/2026-09-29-academic-r0-r1-closure.md`) **and** until three principals can log in. Accounts two and three rely on JIT user provisioning, which PR #1747 is fixing. Until it merges, those accounts cannot be created, so journeys 3–8 are `NOT RUN`.

The principals are:
- **P1** `allocs16@gmail.com`, holding SUPERVISOR + ADJUDICATOR and not assigned to the queue;
- **P2**, holding REVIEWER;
- **P3**, holding REVIEWER.

Every principal is in the same org and workspace. The protocol is approved with `reviewer_mode={"mode":"dual_independent"}` and has `full_text_exclusion_reasons`.

1. **Migration:** `kubectl -n rag-dev exec deploy/backend -- alembic current` shows `f3b5d7e9a1c4 (head)`.
2. **Mode binding:**
   - P1 creates a title/abstract queue (201). `history` event 1 carries `reviewer_mode: "dual_independent"`.
   - A queue on a protocol without a mode gets 422.
   - P1 assigns P2 and P3.
3. **Blind (P2 first):**
   - P2 includes r1 with a note.
   - In the P3 browser, before P3 submits, the r1 row reads "hidden until reveal".
   - The DevTools network JSON for `mine` and `history` shows no P2 decision or note, and the history event has `redacted: true`.
   - Capture a screenshot.
4. **Reveal:**
   - P3 excludes r1. The response carries `resolution.basis = "conflict"`.
   - Both browsers now show both decisions and the Conflict badge.
   - A further submit by P2 on r1 gets 409 `Report resolved; reopen to change`.
5. **Agreement:** P2 and P3 both include r2 → `Agreed: include`. The resolution's inputs are exactly the two observation ids.
6. **Adjudication:**
   - P1 opens the Conflicts panel and adjudicates r1 as exclude with a rationale (200). The badge shows `Adjudicated`.
   - `history` shows `screening.adjudicated` with `input_observation_ids`, `criteria_hash` and the rationale in `reason`. Both observations are unchanged.
   - P2 (the reviewer) calling `conflicts` gets 403.
   - The owner, if it is a different account holding no ADJUDICATOR role, gets 403 on `adjudicate`.
7. **Stale + reopen:**
   - P1 reopens r2 (`screening.reopened`).
   - P2 and P3 submit fresh, conflicting decisions.
   - Replaying the adjudicate request with the pre-reopen `resolution_id`/ids gets 409 `Adjudication inputs are stale`. The current ids give 200.
   - `history` keeps every earlier event.
8. **Denials:**
   - An other-org user on `conflicts` gets 404.
   - Adjudicate on an archived project gets 409.
9. **Export:** if GOO-300 is deployed, download the corpus ZIP and `grep` it for P2's note sentinel. It must be absent.
10. **CI:** record the Integration job URL where `test_screening_blind_review_postgres.py` passed at the merge SHA, and paste the three mutation transcripts into the PR.

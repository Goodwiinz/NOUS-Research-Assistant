# GOO-297: Persist draft task terminal results bound to exact artifacts

Status: plan only, nothing implemented. Branch `feat/goo-297-task-terminal-results`
off `origin/develop` `5ee337cfe` (2026-09-29).
Ticket: [Academic R1] GOO-297. Sibling pattern: GOO-295 decision ledger.

**Goal:** a draft-generation task's terminal outcome survives Redis expiry,
page reload and process loss, and names the exact `GeneratedDraft`
(id, version, sha256 of content) that *this* task produced, or an honest
`failed` / `cancelled` / `interrupted` with no artifact.

**Architecture:** one new table `draft_task_results`, one row per task.
The `running` row is inserted in the same transaction that accepts the task;
the `completed` row is written in the same transaction as the draft insert
(closing the commit-then-publish window at
`backend/src/services/research/draft_generation_service.py:638-650`).
Every terminal write is a guarded `UPDATE ... WHERE state='running'`, so a
task has at most one terminal association. Redis (`research:draft-status:*`,
TTLs at `draft_generation_service.py:59-63`) stays the progress cache; the
status route falls back to, and overlays, the DB row. "Process gone" is
decided by the row's `heartbeat_at` going stale (mirrors the Redis
`_DRAFT_ACTIVE_STALE_SECONDS=120` rule at `:1684-1690`), evaluated lazily on
read, not by a scheduler. Resuming model execution is out of scope.

**Tech stack:** SQLAlchemy async + Alembic (PG), FastAPI/Pydantic,
openapi-typescript, Vitest. Tests: aiosqlite unit + one real-PG integration
test (`RESEARCH_DECISION_DATABASE_URL` fixture pattern).

## Verified current code (re-locate if the file moved)

- Acceptance `draft_generation_service.py:157-293`: source validation commits
  `:215`; task_id minted under a `threading.Lock` `:233-266`; in-memory
  `_generation_status` set; Redis publish `:268`; `asyncio.create_task` via
  `_fire_and_forget` (`:126-131`) at `:271-286`. `generation_request_hash`
  (`_request_hash` `:328-353`) is already a canonical sha256: the fingerprint.
- `_generate_draft_async` `:377-706`: early FAILED returns `:425-437`,
  `:476-487`; window 3 `lock_active_project` `:581`, version `:607-612`,
  `db.add(draft)` `:634`, **commit `:638`**, then a *separate* Redis
  `COMPLETED` publish with `draft_id` `:642-650` (the process-loss window);
  except CancelledError `:662`, IntegrityError `:673`, Exception `:691`.
- Status store: `_update_status` `:1573-1606` (raises CancelledError once the
  in-memory row is `cancelled`), `publish_status` `:1608-1627`,
  `_heartbeat_status` `:1629-1637` (30 s loop), `get_status_shared`
  `:1659-1682`, `cancel_generation` `:1775-1791` (in-memory, sync).
- Hash recipe in use: `hashlib.sha256(content.encode())` `:1394`
  (`DraftReview.candidate_content_hash`). `GeneratedDraft` has `version`
  (`uq_draft_version`, `h3j7k8l9m0n1_create_generated_drafts.py:46`) but no
  content hash column; we compute it at commit time, no column added.
- Routes `backend/src/api/research/drafts.py`: generate `:83-150`
  (`_validate_project_ownership` = `resolve_project`, `:68-75`), status
  `:457-487` (`resolve_project` VIEW + `project_id`/`user_id` equality, 404
  on miss), by-task alias `:490-503`, cancel `:511-549` (EDIT: archived
  already 409s inside `resolve_project`), alias `:552-565`. Status has no
  `response_model`; `backend/openapi.json` shows `schema: {}` for it.
- Agent tool: `backend/src/services/agent/tools_impl.py:4750` calls
  `generate_draft`, `:4806` waits, `:2215` recovers via `get_status_shared`;
  terminal sets at `:2242-2246` and `:2272` know only `failed`/`cancelled`.
- Frontend: `frontend/src/components/chat/aui/DraftTaskStatus.tsx`
  `TERMINAL_STATUSES` `:15`, `statusLabel` `:60-83`, poller `:90-105`,
  `completedDraftId` `:111-113`, link `:182-189`. Hand-written
  `GenerationStatus` at `frontend/src/services/projectService.ts:679-689`
  (file already aliases generated schemas at `:644-645`).
- GOO-295 to mirror: `backend/src/models/research_decision.py` (RESTRICT
  FKs, `Base` not `BaseModel`, 64-char hash CHECKs),
  `services/research_decisions/ledger.py:203-295` (guarded write, never
  commits), migration `a3c5e7f901b2_create_research_decision_ledger.py:17-31`
  (`_deny_data_api`). Durable-run pattern to mirror, not reuse:
  `models/agent_run.py:118-126` heartbeat/lease, `agent_run_service.py:870-900`.
- Alembic head today: `merge_daily_harness_20260928`
  (`alembic/versions/merge_daily_brief_harness_heads.py`); ids <= 32 chars.
  If PR #1747 has merged, rebase and use `down_revision = "b7c4e1d9a2f6"`.

## Data model

`draft_task_results` (model `backend/src/models/draft_task_result.py`, class
`DraftTaskResult(Base)`; register in `models/__init__.py` next to
`GeneratedDraft`, line 71).

| column | type | why (acceptance bullet) |
|---|---|---|
| `task_id` | `String(64)` PK | task identity; PK = one row = one terminal association |
| `collection_id` | GUID FK `collections.id` RESTRICT, indexed | canonical project; status/cancel scope check |
| `actor_user_id` | GUID FK `users.id` RESTRICT | initiating actor; visibility check |
| `state` | `String(16)`, CHECK in (`running`,`completed`,`failed`,`cancelled`,`interrupted`) | terminal state |
| `artifact_id` | GUID nullable, **no FK** | exact draft produced. No FK on purpose: `delete_draft` (`drafts.py:300`) must keep working and the retained record must outlive the artifact |
| `artifact_version` | Integer nullable | exact version |
| `artifact_hash` | `String(64)` nullable, CHECK `length=64` when set | exact content hash |
| `request_fingerprint` | `String(64)` CHECK length 64 | reuse of `generation_request_hash`; ties the row to the request the dedup path (`:233-247`) matched |
| `error_code` | `String(64)` nullable | `sources_unavailable`, `version_conflict`, `generation_error`, `cancelled_by_user`, `process_lost` — lets a reader tell failed-by-us from interrupted |
| `started_at` | timestamptz not null default now() | evidence |
| `heartbeat_at` | timestamptz not null default now() | "process gone" decision |
| `terminal_at` | timestamptz nullable | evidence |

CHECK `ck_draft_task_results_artifact`:
`(state='completed') = (artifact_id IS NOT NULL AND artifact_version IS NOT NULL AND artifact_hash IS NOT NULL)`.
A completed row cannot lack its artifact; a non-completed row cannot carry one.
DB constraint, not app code.

Dropped from the ticket's sketch: `artifact_type` (only drafts exist; add it
with the second artifact kind), a separate idempotency key (`task_id` is
it), progress/step (Redis).

## Tasks

### Task 1: model + migration

Files: `backend/src/models/draft_task_result.py` (new), `models/__init__.py`,
`backend/alembic/versions/c9d1e2f3a4b5_create_draft_task_results.py` (new).

Failing test first, `backend/tests/unit/services/test_draft_task_results.py`:

```python
async def test_completed_requires_exact_artifact(engine):  # aiosqlite, table subset
    async with AsyncSession(engine) as db:
        db.add(DraftTaskResult(task_id="t1", collection_id=uuid4(), actor_user_id=uuid4(),
                               state="completed", request_fingerprint="a"*64))
        with pytest.raises(IntegrityError):
            await db.commit()
```

Engine fixture: `create_async_engine("sqlite+aiosqlite:///:memory:")` +
`conn.run_sync(DraftTaskResult.__table__.create)` only (pattern
`backend/tests/unit/services/test_kpi_tenant_isolation.py:78-89`).

Run: `pytest -q backend/tests/unit/services/test_draft_task_results.py -k exact_artifact`
Before: `ImportError`. After: `1 passed`.

Migration: `down_revision = "merge_daily_harness_20260928"` (or
`b7c4e1d9a2f6`). Copy `_deny_data_api` from
`a3c5e7f901b2_create_research_decision_ledger.py:17-31` verbatim (Supabase
PostgREST exposure; not optional). No append-only trigger: the row is
updated exactly once (running -> terminal) by design.

Verify: `(cd backend && python ../scripts/ci/check_alembic.py)` ->
`single head 'c9d1e2f3a4b5', 85 revisions`. Offline SQL:
`(cd backend && alembic upgrade merge_daily_harness_20260928:c9d1e2f3a4b5 --sql | head -60)`.

Commit: `feat(drafts): add draft_task_results table for retained task terminal state`

### Task 2: service writes (running / completed / failed / cancelled / interrupted)

File: `backend/src/services/research/draft_generation_service.py` only.
Add one small module-level section (or a `draft_task_results.py` sibling if
the file's 2,252 lines bother the reviewer; one place either way):

```python
async def start_task(db, *, task_id, collection_id, actor_user_id, request_fingerprint) -> None
async def finish_task(db, *, task_id, state, artifact=None, error_code=None) -> bool
    # UPDATE draft_task_results SET state=..., artifact_id=artifact.id,
    #   artifact_version=artifact.version,
    #   artifact_hash=sha256(artifact.content.encode()).hexdigest(),
    #   error_code=..., terminal_at=now() WHERE task_id=:t AND state='running'
    # returns rowcount == 1; NEVER commits (caller owns the transaction)
async def touch_task(db, task_id) -> None      # heartbeat_at=now() WHERE state='running'
async def reconcile_task(db, task_id) -> DraftTaskResult | None
    # SELECT row; if state='running' and heartbeat_at < now()-120s:
    #   UPDATE ... SET state='interrupted', error_code='process_lost', terminal_at=now()
    #   WHERE task_id=:t AND state='running' AND heartbeat_at < :threshold; commit
```

`finish_task(completed)` takes the ORM `GeneratedDraft` the task just
flushed (`:634-636`): id, version and content all come from that object.
It must never query "current draft". Threshold constant: reuse
`_DRAFT_ACTIVE_STALE_SECONDS`.

Wiring (each is a few lines):

1. `generate_draft` `:215`: delete the early `await self.db.commit()`. After
   the lock block (`:266`) and before `publish_status` (`:268`):
   `await start_task(self.db, ...)`; `await self.db.commit()`. On commit
   failure: `_generation_status.pop(task_id, None)`, re-raise. The task is
   never fired without its row. (The source-validation rollback at `:207`
   stays.)
2. `_heartbeat_status` `:1629-1637`: inside the loop, after
   `publish_status`, `async with AsyncSessionLocal() as db: await touch_task(db, task_id); await db.commit()`.
   One indexed UPDATE per 30 s per live task.
3. Window 3, immediately before `await db.commit()` at `:638`:
   `if not await finish_task(db, task_id=task_id, state="completed", artifact=draft): raise DraftTaskNotRunning(task_id)`.
   Same transaction as the draft insert. Rowcount 0 means the row is already
   `cancelled`/`interrupted`; the raise rolls the draft back, so nothing is
   orphaned and a cancelled task never gets a completed artifact.
4. One helper `async def _fail_task(self, task_id, state, step, error_code)`:
   `_set_status(...)` (Redis, existing) + own `AsyncSessionLocal()` +
   `finish_task(...)` + commit, log-and-continue on DB error. Replace the
   five `_set_status(FAILED/CANCELLED)` call sites (`:431`, `:481`, `:663`,
   `:676`, `:692`) with it. `DraftTaskNotRunning` lands in the generic
   branch with `error_code="superseded"`; `finish_task` is a no-op there
   (row already terminal), Redis mirrors the DB row instead.
5. `cancel_generation` `:1775`: keep the sync in-memory flip (the coroutine
   relies on it, `:1587-1590`); add `async def cancel_task(db, task_id)` =
   flip + `finish_task(state="cancelled", error_code="cancelled_by_user")`
   + `db.commit()`; route `drafts.py:533` calls it. Cross-replica cancel:
   the DB write lands anyway and the other replica's `finish_task(completed)`
   gets rowcount 0 and rolls its draft back. Truthful.

Failing tests first (same unit file, aiosqlite):

- `test_finish_task_is_one_shot`: `start_task`, `finish_task(completed, draft_a)`
  -> True; `finish_task(completed, draft_b)` -> False; row still names
  `draft_a`. **Mutation:** delete `DraftTaskResult.state == "running"` from
  the UPDATE predicate -> second write returns True and row names
  `draft_b` -> test fails naming the overwrite. Restore, rerun, passes.
- `test_cancelled_task_cannot_complete`: `finish_task(cancelled)` then
  `finish_task(completed, draft)` -> False, state stays `cancelled`,
  `artifact_id IS NULL`. Same mutation.
- `test_completion_binds_the_task_own_draft`: two running rows A, B; one
  `GeneratedDraft` row per task with distinct content; complete B then A;
  `A.artifact_hash == sha256(draft_a.content)`, `!= B`'s. **Mutation:** make
  `finish_task` compute hash/id from
  `select(GeneratedDraft).where(is_current)` instead of the passed object
  -> A binds B's draft -> fails. (SQLite unit; the concurrent real version is
  Task 5.)
- `test_stale_running_becomes_interrupted`: row with `heartbeat_at = now()-10min`
  -> `reconcile_task` returns `interrupted/process_lost`; row with fresh
  heartbeat stays `running`. **Mutation:** drop the `heartbeat_at < threshold`
  predicate -> fresh row flips -> fails.

Run: `pytest -q backend/tests/unit/services/test_draft_task_results.py`
-> `5 passed`; keep `test_draft_citation_review_pass.py` and
`test_draft_revision_service.py` green. Mutation records go in Task 6.

Commit: `feat(drafts): write task terminal results in the draft commit transaction`

### Task 3: status route DB fallback + response schema

Files: `backend/src/api/research/drafts.py`, `backend/openapi.json`,
`frontend/src/types/generated/api.d.ts` (both regenerated, never edited).

Pydantic `DraftTaskStatusResponse(BaseModel)` in `drafts.py`,
`model_config = ConfigDict(extra="allow")` (live payload also carries
`document_ids`, `selection_mode`; keep them flowing):
`task_id: str`, `status: str`, `progress: int = 0`, `current_step: str = ""`,
`started_at: str`, `updated_at: str | None`, `draft_id: str | None`,
`duration: float | None`, `artifact_version: int | None`,
`artifact_hash: str | None`, `error_code: str | None`,
`state_source: Literal["cache", "database"]`. Set as `response_model` on
`:457` and `:490`.

`get_generation_status` `:470-487` becomes:

```python
cached = await DraftGenerationService.get_status_shared(task_id)
row = await reconcile_task(db, task_id)          # service commits the interrupt flip
# 404 if both None; scope = row.collection_id/actor_user_id (or the cached checks)
if row is not None and (cached is None or row.state != "running"):
    payload = {**(cached or {}), "task_id": task_id, "status": row.state,
               "draft_id": str(row.artifact_id) if row.artifact_id else None,
               "artifact_version": row.artifact_version, "artifact_hash": row.artifact_hash,
               "error_code": row.error_code, "state_source": "database"}
else: payload = {**cached, "state_source": "cache"}
```

DB wins whenever it is terminal: the row is the record, Redis the cache. The
route never commits itself (`docs/engineering/backend.md`).

Also add `"interrupted"` to the terminal sets in
`draft_generation_service.py:132-136` (`TERMINAL_STATUSES`) and
`tools_impl.py:2242-2246` + `:2272` so the agent tool stops polling and
reports it as an error state.

Failing test first, `backend/tests/unit/api/test_draft_task_status_route.py`
(FastAPI `TestClient` with `get_db`/`get_current_user` overridden, pattern
`backend/tests/integration/test_draft_source_scope_postgres.py:1-35`;
`get_status_shared` patched to `None`, `reconcile_task` patched to return a
completed row): expect 200 with `status=completed`, `draft_id`,
`artifact_version`, `artifact_hash`, `state_source="database"`; foreign
`actor_user_id` -> 404.

Run: `pytest -q backend/tests/unit/api/test_draft_task_status_route.py` ->
`2 passed`. Then:
`python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types && python scripts/ci/generate_openapi.py --check`
-> `OpenAPI snapshot is up to date`. Check the oasdiff changelog in CI: the
`{}` -> object response is additive, not ERR; if it flags ERR, do not add
the `api-breaking-approved` label, fix the schema.

Commit: `feat(drafts): resolve task status from draft_task_results when the cache misses`

### Task 4: frontend (minimal)

Files: `frontend/src/services/projectService.ts`,
`frontend/src/components/chat/aui/DraftTaskStatus.tsx`, its test.

- `projectService.ts:679-689`: replace the hand-written interface with
  `export type GenerationStatus = components['schemas']['DraftTaskStatusResponse'];`
  (adopt-on-touch, `docs/engineering/api-contracts.md`).
- `DraftTaskStatus.tsx:15`: add `'interrupted'` to `TERMINAL_STATUSES`;
  `statusLabel` `:60-83`: `case 'interrupted': return 'Draft interrupted'`
  (extend the XCircle branch at `:134-135`). Link `:182-189`: text
  `View draft{artifact_version ? ` v${artifact_version}` : ''}`. Task
  details `:191-194`: append `Hash: {artifact_hash.slice(0, 12)}` when
  present. No store changes; TanStack Query already owns this data.

Failing test first in `__tests__/DraftTaskStatus.test.tsx`: mocked
`{status:'completed', draft_id, artifact_version: 3, artifact_hash,
state_source:'database'}` -> link `View draft v3`, href `...&draftId=`,
no further refetch; `{status:'interrupted'}` -> status text
`Draft interrupted`, no progressbar, polling stopped.

Run: `pnpm --dir frontend exec vitest run src/components/chat/aui/__tests__/DraftTaskStatus.test.tsx`
and `pnpm --dir frontend type-check` -> clean.

Commit: `feat(chat): show durable draft version and interrupted state in task card`

### Task 5: real PostgreSQL integration test

File: `backend/tests/integration/test_draft_task_results_postgres.py`.
Fixture: copy `decision_engine` + `_seed` from
`backend/tests/integration/test_research_decision_ledger.py:48-118`
(`RESEARCH_DECISION_DATABASE_URL`, per-test schema, `create_all` then drop +
run the real migration). Fake Redis: patch
`draft_generation_service.get_redis` with a dict-backed `get`/`setex`;
clearing the dict is "expiry". Patch `_build_draft_content` -> `(content,
False)` and `_review_citations` -> passing; seed one `CollectionDocument`.
Markers `integration, requires_postgres, asyncio`. One test per scenario:

1. **Durable terminal evidence.** Run `_generate_draft_async` to completion.
   `_generation_status.clear()`, clear the fake Redis dict, open a *new*
   session: `reconcile_task` -> `completed`; `artifact_id` equals the only
   `GeneratedDraft.id`, `artifact_version == 1`, `artifact_hash ==
   sha256(GeneratedDraft.content)` re-read from PG. Nothing is inferred from
   Redis or memory.
2. **Kill between artifact commit and publish.** Patch `publish_status` to
   raise `SystemExit` once the in-memory status is `completed` (the
   `:642-650` publish). `except Exception` does not catch it, so the task
   dies exactly as a SIGKILL would after `:638`. Assert: Redis has no
   terminal entry, PG row is `completed` with the artifact fields, and a
   fresh-session status read resolves them.
3. **Kill before artifact commit.** Patch `_lock_project_for_draft_version`
   to raise `SystemExit`. Assert: zero `GeneratedDraft` rows, row still
   `running`. Backdate `heartbeat_at` by 10 min (what a dead process leaves)
   -> `reconcile_task` -> `interrupted` / `process_lost`, still zero drafts.
4. **Two concurrent tasks in one project, each binds its own draft.**
   `asyncio.gather` two `_generate_draft_async` calls with different stubbed
   contents (the real `lock_active_project` serialises version allocation).
   Assert both rows `completed`; `{row.artifact_id} == {draft ids}`;
   each `artifact_hash == sha256(content of the draft with that id)`; only
   one draft `is_current`, and the *other* task's row still names its own
   non-current draft. **Mutation:** point `finish_task` at the current draft
   -> both rows share one `artifact_id` -> fails.
5. **Concurrent duplicate terminal writes yield one row.** `start_task`,
   then `asyncio.gather(finish_task(completed, draft) in session 1,
   finish_task(completed, draft) in session 2)` each committing: exactly one
   `True`; `SELECT count(*) WHERE task_id` == 1; one `terminal_at`.
   **Mutation:** remove `state == "running"` predicate -> two `True`.
6. **Cancel vs. completion race.** Task blocked on an `asyncio.Event` inside
   the stubbed `_build_draft_content`; `cancel_task` commits `cancelled`;
   release the event. Assert no `GeneratedDraft` row, row stays `cancelled`.
   Mutation as in 5 (plus dropping the `raise DraftTaskNotRunning` of Task 2
   step 3) lets the completion overwrite the cancel.
7. **Authorization on reload.** ASGI app with `drafts_router`,
   `get_db`/`get_current_user` overridden (pattern
   `test_draft_source_scope_postgres.py:1-35`). Completed row from scenario
   1. Owner -> 200 with artifact fields. Foreign-org user -> 404. Soft-delete
   the collection -> owner gets 404. Set `research_status='archived'` ->
   status GET still 200 (VIEW), `POST /drafts/cancel/{task_id}` -> 409.
   Guards live in `project_access.resolve_project`; no new checks written.

Run: `RESEARCH_DECISION_DATABASE_URL=<postgres> pytest -q backend/tests/integration/test_draft_task_results_postgres.py`
-> `7 passed`. Without a DB report the suite as `NOT RUN`, never passing.

Commit: `test(drafts): prove task terminal results survive cache loss and process death`

### Task 6: docs + gates

`docs/testing/agent-orchestration-mutation-checks.md`: dated section for the
four guards (Task 2 unit, scenarios 4/5/6): file:line, command, observed
mutant failure, restore proof. Before push:
`scripts/ci/run_local_ci.sh --base origin/develop --frontend` (added files
run mypy `disallow_untyped_defs`; type the helpers).

Commit: `docs(testing): record draft task result mutation checks`

## Out of scope (say no once)

- Resuming a killed generation: `interrupted` is terminal; start a new task.
- Startup sweeper: a booting replica cannot know which *other* replica's
  tasks died; the heartbeat rule can, on the next status read.
- `revise_draft` (`:1169-1357`) is synchronous, commits its own draft, has
  no task. Pushing task events into `research_decision_events`: forbidden.

## Evidence to capture live for the Linear closure

Dev cluster (`rag-dev`), one project with one attached PDF:

1. `POST /api/v1/projects/{p}/drafts?themes=...` -> `task_id`.
2. Poll `GET .../drafts/status/{task_id}` to `completed`; capture
   `draft_id`, `artifact_version`, `artifact_hash`, `state_source`.
3. One screenshot: `SELECT task_id,state,artifact_id,artifact_version,
   artifact_hash,error_code FROM draft_task_results WHERE task_id=...` next to
   `SELECT id,version,encode(sha256(convert_to(content,'UTF8')),'hex')
   FROM generated_drafts WHERE id=...`; hashes equal.
4. `redis-cli DEL research:draft-status:{task_id}` (Valkey); repeat the GET:
   same fields, `state_source="database"`. Reload chat: `Draft ready`,
   `View draft vN`.
5. Second task; `kubectl delete pod` the API pod mid-generation; after
   > 120 s GET -> `interrupted` / `process_lost`; `generated_drafts` count
   unchanged.
6. Two tasks back-to-back (the conflict guard `:233-247` blocks HTTP overlap):
   each row's `artifact_id` differs and matches its own draft.
7. CI links: unit, PG integration job, `openapi-contract`, mutation-check doc.

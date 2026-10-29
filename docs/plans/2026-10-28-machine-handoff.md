# Machine handoff Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A NOUS chat turn can hand a coding task brief to Codex on the user's paired machine, pause durably while Codex works (with a live card, command approvals and Cancel), and continue the same turn with Codex's reply and outcome facts.

**Architecture:** The graph gets a `run_on_machine` tool, served by two new nodes. `handoff_approval_node` asks for approval, and the card on that approval proposes a machine and folder that the user can change. `handoff_node` creates a child Codex run (`agent_runs.parent_run_id`) and pauses on a second LangGraph interrupt. The stream producer parks the parent as the new active status `delegated` and emits a terminal `handoff_started` frame. When the child reaches a terminal state, `finalize_submission` writes a `handoff.resume` outbox row in the same transaction. A 2-second Celery beat drain claims that row once and resumes the parent's graph with the outcome; if that worker dies, a later drain retries. The bridge adds command text, exit codes and changed paths to completed tool items.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, PostgreSQL, LangGraph, Celery; TypeScript on Node 24 (`packages/harness-bridge`, `node:test`); Next.js + Vitest (`frontend`).

---

Design: [`2026-10-28-machine-handoff-design.md`](2026-10-28-machine-handoff-design.md) (approved; read it fully first). Every line range below was read on `origin/develop` `749c4b4cd`. Re-check each range before editing, because develop moves every 30 to 40 minutes.

## Deviations from the design

The Decisions table is unchanged. These are changes of mechanism, each forced by the code:

1. **The producer parks the parent, not the node (design §3 step 2).** The approval receipt hashes the saved checkpoint id and interrupt (`confirmation_service.pending_approval`), and that checkpoint exists only after `interrupt()` returns control to the producer. So `handoff_node` creates the child and interrupts, and the stream producer then sets `delegated`, stores the receipt as `run_metadata.handoff_approval_id` and emits the frame. The producer is already the code that parks approvals (`streaming.py` at three sites). `delegated` is still set before the stream ends, and the frame is still the last one.
2. **Two nodes instead of "the existing interrupt node + a handoff node" (§1, §3).** The generic `interrupt_node` (`_nodes_tools.py:314-357`) cannot fill in a proposed target or refuse before it asks. LangGraph matches sequential `interrupt()` calls in one node by call order, and `docs/engineering/agent-approvals.md` notes that it can reuse an interrupt id. If one node held both interrupts and took a different branch on replay (for example, the device went away), it would hand the approval decision to the outcome interrupt. So the approval lives in `handoff_approval_node`, which stores its decision in a new state key `handoff_decision`. `handoff_node` holds exactly one interrupt. Each node replays deterministically, and each remains idempotent by `tool_call_id`.
3. **The SSE frame is `handoff_started`, not `handoff.started` (§3, §5).** The SSE vocabulary is snake_case (`AgentStreamEvent`, `shared/enums.py:291-326`). Dotted names belong to the run-event ledger.
4. **The resume uses a beat drain instead of an enqueued task plus a sweeper re-queue (§7).** The resume record is an `agent_outbox` row with `kind="handoff.resume"`, keyed by the **child** run id. `uq_agent_outbox_run` becomes `uq_agent_outbox_run_kind (run_id, kind)`, so each handoff gets one row and one parent can hand off more than once. The drain `drain_handoff_resumes` runs every 2 s on the `agent_runs` queue, like `drain_artifacts` and `drain_integration_actions`. It claims each row once, which is the claim-once task, and it picks up unclaimed rows on its next tick, which is the re-queue. If the worker dies after claiming, the drain re-claims the row once the parent misses heartbeats for 5 minutes (up to 3 attempts). After that, the turn closes with Codex's reply saved, so the outcome is never lost. The stale-run sweeper only skips `delegated` rows.
5. **The resume path is assembled from existing parts, not `_resume_agent_graph` (§7).** `_resume_agent_graph` (`agent_execution_service.py:2955-3483`) requires the Redis job record and a `/confirm` claim, and a stream-created run has neither. The new `resume_handoff_parent` uses the same parts:
   - `compile_agent_graph`;
   - `require_approval` against the stored receipt;
   - `resume_runtime_config_fields` / `hydrate_runtime_state_from_snapshot`;
   - `_run_heartbeat` and `_invoke_graph_with_cancellation_monitor`;
   - `current_turn_final_text`, `_persist_assistant_message_safe` and `finalize_submission`.
6. **"Picks the user's online device" (§2).** The backend has no presence signal: neither `BridgeDevice` nor `HarnessSession` records liveness, and grants cannot tell a pairing grant from a run grant. The default target is therefore the folder this user last used for this project (from `harness_sessions` joined to `agent_runs`). When there is none, it is the first eligible device and folder. Locked folders (`uq_harness_active_workspace`) are skipped, and a machine or folder the user named still wins. The card shows "waiting for the machine" while the child is still `queued` or is `recovering`.
7. **A new message during a handoff (Decisions: "Blocked with a notice").** `/stream` reports conflicts in-band, as an SSE `error` frame with category `conflict` (`streaming.py:309-336`), not as an HTTP status. `HandoffInProgress(ActiveRunConflict)` carries the notice text "Codex is still working: cancel it or wait."
8. **Handoff only from the SSE paths.** The `/execute` job path parks every interrupt as `awaiting_confirmation` (`agent_execution_service.py:2741-3439`), so it would file the outcome interrupt as an approval. The stream producers and the resume drain set `configurable.handoff_enabled = True`. Without it, the tool refuses with a note.
9. **Run-id endpoint and parent completion (§5, §7).** `GET /agent/runs/{run_id}/stream` serves only child runs. After the child's terminal frame, the card polls the existing `GET /agent/jobs/{parent_run_id}`. Stream runs never write a Redis job record, so this answers from the durable row. When the parent leaves `delegated`/`running`, the card calls `refreshMessages`. If the parent is `awaiting_confirmation` again, the existing cold resume probe re-delivers the card.
10. **Outcome facts land with the bridge (PR 4), not with the graph (PR 3).** The payload models are `extra="forbid"`, so the backend fields have to ship with the bridge change. Until then the PR 3 outcome carries status, reply and error.
11. **`create_chat_context` moves to `services/harness/chat_context.py`.** `api/agent/harness_streaming.py` re-exports it. This lets the service mint the child's grant without importing the API layer.
12. **The child's final text is stored in `run_metadata.handoff_reply`, capped at 4,000 characters (§4).**
13. **Stop or Cancel on a `delegated` parent cancels the child only.** `cancel_stream_confirmation` gains a `delegated` branch. The parent stays `delegated` until the child's terminal event exists, and then it resumes with `cancelled`.
14. **Changed paths are made relative to the session's working folder in the bridge.** Paths outside it are dropped. Absolute paths carry the local user name, and `redact_pii` does not strip file paths.
15. **The edited target reaches the confirm call through a draft keyed by approval id.** assistant-ui's `respondToApproval` carries only `{approved}` (`HitlApprovalToolUI.tsx:27`, `packages/chat-runtime/runtime.ts:146`), so the card cannot pass the target through it.

## Conventions

- Paths are repository-relative. `<PY>` is the main checkout's `backend/.venv/bin/python`; worktrees have no venv.
- Backend tests: `PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q <path>`.
- PostgreSQL tests:
  - Start the local cluster from `docs/testing/harness-live-proof.md`, Gate 0 (initdb on port 55432, no Docker).
  - Run with `ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch ENVIRONMENT=testing` in front of the command above.
  - Without the variable these tests skip. Report a skip as **NOT RUN**, never PASS.
- Bridge tests:
  - Whole suite: `pnpm --filter @nous/harness-bridge test`.
  - One file: `pnpm --filter @nous/harness-bridge exec node --experimental-sqlite --import tsx --test --test-timeout=60000 test/<file>`.
- Frontend tests: `pnpm --dir frontend exec vitest run <file>`, then `pnpm --dir frontend type-check`.
- OpenAPI: run `<PY> scripts/ci/generate_openapi.py`, then `pnpm --dir frontend generate:api-types`. Both generated files go in the same commit and are never hand-edited.
- Lint gate before each push: `scripts/ci/run_local_ci.sh --base origin/develop` (add `--frontend` in PR 5).
- Alembic: `(cd backend && <PY> ../scripts/ci/check_alembic.py)` must report a single head.
- Race and idempotency tests must be mutation-verified (`docs/engineering/testing.md`). Each such task names the guard line to break and the test that must fail.
- Commit messages end with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## PR list (merge order)

| PR | Branch | Scope | Tasks |
| --- | --- | --- | --- |
| 1 | `feat/handoff-run-model` | `parent_run_id`, `delegated`, migration, top-level lookups, idle 409, sweeper and stale-confirmation skips | 7 |
| 2 | `feat/handoff-child-runs` | `chat_context` move, workspace reservation, brief prompt, `create_handoff_run`, no child assistant row, resume row, children excluded from thread resume | 7 |
| 3 | `feat/handoff-graph` | SSE frame, tool + descriptor, target proposal/validation, editable confirm, two nodes in the main graph and research subgraph, producer parking, Stop, cold resume, resume drain | 11 |
| 4 | `feat/handoff-outcome-facts` | bridge outcome fields, backend payload fields, outcome facts in the resume value | 4 |
| 5 | `feat/handoff-frontend` | run-id stream endpoint, editable approval card, live card, blocked composer, refetch | 7 |
| — | — | Final verification | 1 |

Each PR is green alone. PRs 1 and 2 change no user-visible behavior. PR 3 makes the tool callable, but it is only reachable after PR 5 ships the card.

---

## PR 1: Run model

### Task 1.1: `JobStatus.DELEGATED`

**Files:**
- Modify: `backend/src/shared/enums.py:35-42` (JobStatus members) and the class docstring at `:28-32`.
- Test: `backend/tests/unit/services/agent/test_agent_run_delegated.py` (new).

**Step 1: Write the failing test**

```python
"""``delegated``: a parent run paused while a child Codex run works."""

from __future__ import annotations

import pytest

from src.models.agent_run import AGENT_RUN_STATUS_CHECK
from src.services.agent import agent_run_service, agent_submission_service
from src.shared.enums import JobStatus

pytestmark = pytest.mark.unit


def test_delegated_is_active_and_constrained() -> None:
    assert JobStatus("delegated") is JobStatus.DELEGATED
    assert not JobStatus.DELEGATED.is_terminal
    assert "'delegated'" in AGENT_RUN_STATUS_CHECK
    assert "delegated" in agent_submission_service._ACTIVE_RUN_STATUSES
    assert "delegated" in agent_run_service._ACTIVE_RUN_STATUSES
```

**Step 2: Run it**

`PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q backend/tests/unit/services/agent/test_agent_run_delegated.py`

Expected: FAIL with `ValueError: 'delegated' is not a valid JobStatus`.

**Step 3: Minimal implementation**

In `backend/src/shared/enums.py`, add the member after `RECOVERING`, and add one sentence to the docstring:

```python
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    STOPPING = "stopping"
    RECOVERING = "recovering"
    # A NOUS run paused on a child Codex run (machine handoff). Active: it
    # holds the thread's single-writer slot until the graph resumes.
    DELEGATED = "delegated"
    COMPLETED = "completed"
```

Docstring addition after "Both are non-terminal; terminal states are absorbing.":

```
    ``DELEGATED`` is a third non-terminal pause: the graph waits on a child
    Codex run (``agent_runs.parent_run_id``) and resumes when it ends.
```

`_ACTIVE_RUN_STATUSES` in both services and `AGENT_RUN_STATUS_CHECK` are derived from `JobStatus`, so they pick the new value up with no further change.

**Step 4: Run it**

Same command. Expected: PASS.

**Step 5: Check the hand-written status mirrors**

Run `git grep -n "awaiting_confirmation" -- frontend/src packages | grep -v generated`. Every hand-written status union or `switch` that must accept a new active status gets `'delegated'` added in this commit. JobStatusResponse is generated, and is regenerated in Task 1.7.

**Step 6: Commit**

```bash
git add backend/src/shared/enums.py backend/tests/unit/services/agent/test_agent_run_delegated.py
git commit -m "feat(agent): add delegated run status for machine handoff

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(Add any frontend mirror files found in Step 5 to the `git add`.)

### Task 1.2: Model columns, index predicate, outbox key

**Files:**
- Modify: `backend/src/models/agent_run.py:99-101` (add the column after `execution_provider`) and `:158-192` (`__table_args__`).
- Modify: `backend/src/models/agent_outbox.py:75-87`.
- Test: `backend/tests/unit/services/agent/test_agent_run_delegated.py`.

**Step 1: Write the failing test**

Append:

```python
import uuid
from collections.abc import AsyncIterator

from sqlalchemy import event, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.thread import Thread

THREAD = uuid.UUID("55555555-5555-5555-5555-555555555555")


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _no_driver_tx(connection, _record):  # type: ignore[no-untyped-def]
        connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(connection):  # type: ignore[no-untyped-def]
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Thread.__table__.create)
        await conn.run_sync(AgentRun.__table__.create)
        await conn.run_sync(AgentOutbox.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _run(status: str, *, parent: str | None = None, provider: str = "nous") -> dict:
    return {
        "job_id": str(uuid.uuid4()),
        "thread_id": THREAD,
        "status": status,
        "execution_provider": provider,
        "parent_run_id": parent,
    }


async def test_child_may_run_while_parent_delegated(db: AsyncSession) -> None:
    parent = _run("delegated")
    await db.execute(insert(AgentRun).values(**parent))
    await db.execute(
        insert(AgentRun).values(
            **_run("running", parent=parent["job_id"], provider="codex")
        )
    )
    await db.commit()


async def test_second_top_level_active_run_is_refused(db: AsyncSession) -> None:
    await db.execute(insert(AgentRun).values(**_run("delegated")))
    await db.commit()
    with pytest.raises(IntegrityError):
        await db.execute(insert(AgentRun).values(**_run("queued")))
    await db.rollback()


async def test_outbox_allows_one_row_per_run_and_kind(db: AsyncSession) -> None:
    run = _run("completed", provider="codex")
    await db.execute(insert(AgentRun).values(**run))
    db.add(AgentOutbox(run_id=run["job_id"], kind="harness.execute", payload={}))
    db.add(AgentOutbox(run_id=run["job_id"], kind="handoff.resume", payload={}))
    await db.commit()
    db.add(AgentOutbox(run_id=run["job_id"], kind="handoff.resume", payload={}))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
```

**Step 2: Run it**

Same file. Expected: FAIL. `parent_run_id` is an unknown column (`CompileError`/`TypeError`), and the outbox test fails on the second `kind`.

**Step 3: Minimal implementation**

`backend/src/models/agent_run.py`: after `execution_provider` (`:99-101`):

```python
    # Machine handoff: a child Codex run started by a NOUS run's graph. Child
    # runs share the parent's thread but never hold its single-writer slot and
    # never write their own assistant row (services/harness/handoff.py).
    parent_run_id = Column(
        String(36),
        ForeignKey("agent_runs.job_id", ondelete="SET NULL"),
        nullable=True,
    )
```

Replace the `uq_agent_runs_active_thread` entry (`:173-187`) and add a child lookup index:

```python
        # One non-terminal top-level run per thread — the concurrency invariant
        # the run API converts into a conflict instead of starting a second
        # writer. Child runs (machine handoff) are excluded: their parent holds
        # the slot as ``delegated``.
        Index(
            "uq_agent_runs_active_thread",
            "thread_id",
            unique=True,
            postgresql_where=text(AGENT_RUN_ACTIVE_THREAD_PREDICATE),
            sqlite_where=text(AGENT_RUN_ACTIVE_THREAD_PREDICATE),
        ),
        Index("idx_agent_runs_parent", "parent_run_id"),
```

Then add, next to `AGENT_RUN_STATUS_CHECK` (`:50-52`):

```python
# Shared with the mh01 migration so the predicate has one source.
AGENT_RUN_ACTIVE_THREAD_PREDICATE = (
    "thread_id IS NOT NULL AND parent_run_id IS NULL AND status IN "
    "('queued', 'running', 'awaiting_confirmation', 'stopping', 'recovering', "
    "'delegated')"
)
```

`backend/src/models/agent_outbox.py:82-85`: replace the unique index and its comment:

```python
        # One dispatch intent per run and kind: the accept transaction writes
        # the run's own dispatch row once, and a finished handoff child gets
        # exactly one ``handoff.resume`` row (finalize_submission). A retried
        # submission resolves to the existing run instead of a second row.
        Index("uq_agent_outbox_run_kind", "run_id", "kind", unique=True),
```

Update the docstring sentence in `agent_outbox.py:1-23` that says "One row per accepted submission" to read: "One dispatch row per accepted submission, plus one `handoff.resume` row per finished handoff child."

**Step 4: Run it**

Run the new file, `backend/tests/unit/services/agent/test_agent_submission_service.py` and `backend/tests/unit/services/harness/test_runs.py`. Expected: PASS. The `test_runs.py` docstring names `test_database_enforces_*` for the active indexes; those tests must still pass.

**Step 5: Commit**

```bash
git add backend/src/models/agent_run.py backend/src/models/agent_outbox.py backend/tests/unit/services/agent/test_agent_run_delegated.py
git commit -m "feat(agent): child runs, delegated slot and per-kind outbox key

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1.3: Migration `mh01_machine_handoff`

**Files:**
- Create: `backend/alembic/versions/mh01_machine_handoff.py`.
- Test: `backend/tests/integration/test_machine_handoff_migration_postgres.py` (new; PostgreSQL).

The head on `749c4b4cd` is `pb01_pdf_message_branches`. Confirm it with `check_alembic.py` before writing `down_revision`. `agent_runs` is hot, so follow `pb01_pdf_message_branches.py`:
- Catalog-only DDL runs inside the migration transaction.
- The `CHECK` is added `NOT VALID` and then validated.
- Indexes are built `CONCURRENTLY` inside `autocommit_block()`.
- A replacement index is built under a temporary name, the old one is dropped, and the new one is renamed. `agent_run_service._is_active_thread_conflict` matches the name `uq_agent_runs_active_thread`, so the final name must stay.

**Step 1: Write the failing test**

```python
"""mh01 on PostgreSQL: child runs, delegated slot, per-kind outbox key.

Requires ORCHESTRATION_TEST_DATABASE_URL (skips otherwise: NOT RUN).
Mutation checks: dropping ``parent_run_id IS NULL`` from the predicate fails
test_child_and_parent_share_a_thread; leaving 'delegated' out of the CHECK
fails it too (CheckViolation); skipping _drop_index_if_invalid fails the
second-upgrade assertion after an interrupted build.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

_PATH = (
    Path(__file__).resolve().parents[2] / "alembic/versions/mh01_machine_handoff.py"
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    url = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "mh01_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with scoped.begin() as conn:
            await conn.execute(text("CREATE TABLE threads (id UUID PRIMARY KEY)"))
            await conn.execute(
                text(
                    "CREATE TABLE agent_runs (job_id VARCHAR(36) PRIMARY KEY, "
                    "thread_id UUID REFERENCES threads(id), status VARCHAR(32) NOT NULL, "
                    "CONSTRAINT ck_agent_runs_status CHECK (status IN ('queued','running',"
                    "'awaiting_confirmation','stopping','recovering','completed','failed',"
                    "'cancelled')))"
                )
            )
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_agent_runs_active_thread ON agent_runs (thread_id) "
                    "WHERE thread_id IS NOT NULL AND status IN ('queued','running',"
                    "'awaiting_confirmation','stopping','recovering')"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE agent_outbox (id UUID PRIMARY KEY, run_id VARCHAR(36) "
                    "NOT NULL REFERENCES agent_runs(job_id), kind VARCHAR(64) NOT NULL)"
                )
            )
            await conn.execute(
                text("CREATE UNIQUE INDEX uq_agent_outbox_run ON agent_outbox (run_id)")
            )
        yield scoped
    finally:
        await scoped.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("mh01_test", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(engine: AsyncEngine, direction: str) -> list[str]:
    migration, executed = _migration(), []

    def record(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        executed.append(" ".join(statement.split()))

    def migrate(connection: Any) -> None:
        context = MigrationContext.configure(connection)
        with Operations.context(context), context.begin_transaction():
            getattr(migration, direction)()

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(migrate)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    return executed


async def test_child_and_parent_share_a_thread(engine: AsyncEngine) -> None:
    first = await _run(engine, "upgrade")
    await _run(engine, "upgrade")  # idempotent re-run
    assert sum("CREATE UNIQUE INDEX CONCURRENTLY" in s for s in first) == 2
    thread, parent = uuid4(), str(uuid4())
    async with engine.begin() as db:
        await db.execute(text("INSERT INTO threads VALUES (:t)"), {"t": thread})
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,:t,'delegated',NULL)"),
            {"j": parent, "t": thread},
        )
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,:t,'running',:p)"),
            {"j": str(uuid4()), "t": thread, "p": parent},
        )
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text("INSERT INTO agent_runs VALUES (:j,:t,'queued',NULL)"),
                    {"j": str(uuid4()), "t": thread},
                )
        await db.execute(
            text("INSERT INTO agent_outbox VALUES (:i,:r,'agent.stream.execute')"),
            {"i": uuid4(), "r": parent},
        )
        await db.execute(
            text("INSERT INTO agent_outbox VALUES (:i,:r,'handoff.resume')"),
            {"i": uuid4(), "r": parent},
        )


async def test_downgrade_refuses_while_children_exist(engine: AsyncEngine) -> None:
    await _run(engine, "upgrade")
    thread = uuid4()
    async with engine.begin() as db:
        await db.execute(text("INSERT INTO threads VALUES (:t)"), {"t": thread})
        parent = str(uuid4())
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,:t,'completed',NULL)"),
            {"j": parent, "t": thread},
        )
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,:t,'completed',:p)"),
            {"j": str(uuid4()), "t": thread, "p": parent},
        )
    with pytest.raises(Exception, match="handoff"):
        await _run(engine, "downgrade")
```

The column order in `INSERT INTO agent_runs VALUES (...)` is `job_id, thread_id, status, parent_run_id`, because the upgrade appends `parent_run_id`.

**Step 2: Run it**

```bash
ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch ENVIRONMENT=testing \
  PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q \
  backend/tests/integration/test_machine_handoff_migration_postgres.py
```

Expected: FAIL (`FileNotFoundError` for the revision).

**Step 3: Minimal implementation**

```python
"""Machine handoff: child runs, the delegated slot, per-kind outbox rows.

Revision ID: mh01_machine_handoff
Revises: pb01_pdf_message_branches

Locking follows pb01: the column and the NOT VALID check are catalog-only and
commit with the migration transaction; each index is then built CONCURRENTLY
under a temporary name, the old one dropped CONCURRENTLY and the new one
renamed, so ``uq_agent_runs_active_thread`` keeps the name
agent_run_service._is_active_thread_conflict matches. An interrupted build is
dropped iff it is INVALID, so re-running the upgrade repairs it.
"""

import alembic.op as op

revision = "mh01_machine_handoff"
down_revision = "pb01_pdf_message_branches"
branch_labels = None
depends_on = None

_STATUSES = (
    "'queued', 'running', 'awaiting_confirmation', 'stopping', 'recovering', "
    "'completed', 'failed', 'cancelled'"
)
_ACTIVE = "'queued', 'running', 'awaiting_confirmation', 'stopping', 'recovering'"


def _drop_index_if_invalid(name: str) -> None:
    # Static identifiers only. Never touches a valid index.
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM pg_index
            WHERE indexrelid = to_regclass('{name}') AND NOT indisvalid
        ) THEN
            EXECUTE 'DROP INDEX ' || to_regclass('{name}')::text;
        END IF;
        END $$""")


def _swap_index(name: str, create_new: str) -> None:
    """Build ``name``_new, drop ``name``, rename. Runs in autocommit."""
    new = f"{name}_new"
    _drop_index_if_invalid(new)
    op.execute(create_new)
    op.execute(f"""DO $$ BEGIN
        IF to_regclass('{new}') IS NOT NULL THEN
            EXECUTE 'DROP INDEX IF EXISTS {name}';
            EXECUTE 'ALTER INDEX {new} RENAME TO {name}';
        END IF;
        END $$""")


def _status_check(statuses: str) -> None:
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS ck_agent_runs_status")
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT ck_agent_runs_status "
        f"CHECK (status IN ({statuses})) NOT VALID"
    )


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS parent_run_id VARCHAR(36)"
    )
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'fk_agent_runs_parent_run_id'
        ) THEN
            ALTER TABLE agent_runs ADD CONSTRAINT fk_agent_runs_parent_run_id
            FOREIGN KEY (parent_run_id) REFERENCES agent_runs(job_id)
            ON DELETE SET NULL NOT VALID;
        END IF;
        END $$""")
    _status_check(_STATUSES + ", 'delegated'")
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE agent_runs VALIDATE CONSTRAINT fk_agent_runs_parent_run_id")
        op.execute("ALTER TABLE agent_runs VALIDATE CONSTRAINT ck_agent_runs_status")
        _drop_index_if_invalid("idx_agent_runs_parent")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agent_runs_parent "
            "ON agent_runs (parent_run_id)"
        )
        _swap_index(
            "uq_agent_runs_active_thread",
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "uq_agent_runs_active_thread_new ON agent_runs (thread_id) "
            "WHERE thread_id IS NOT NULL AND parent_run_id IS NULL AND status IN "
            f"({_ACTIVE}, 'delegated')",
        )
        _drop_index_if_invalid("uq_agent_outbox_run_kind")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_agent_outbox_run_kind "
            "ON agent_outbox (run_id, kind)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_agent_outbox_run")


def downgrade() -> None:
    # The old single-row outbox key and the narrower slot cannot hold handoff
    # state; refuse rather than discard a child's only link to its parent.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM agent_runs WHERE parent_run_id IS NOT NULL
                   OR status = 'delegated') THEN
            RAISE EXCEPTION 'Machine handoff runs prevent downgrade';
        END IF;
        END $$""")
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_agent_outbox_run "
            "ON agent_outbox (run_id)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_agent_outbox_run_kind")
        _swap_index(
            "uq_agent_runs_active_thread",
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "uq_agent_runs_active_thread_new ON agent_runs (thread_id) "
            f"WHERE thread_id IS NOT NULL AND status IN ({_ACTIVE})",
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_runs_parent")
    _status_check(_STATUSES)
    op.execute("ALTER TABLE agent_runs VALIDATE CONSTRAINT ck_agent_runs_status")
    op.execute(
        "ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS fk_agent_runs_parent_run_id"
    )
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS parent_run_id")
```

**Step 4: Run it**

Run the PostgreSQL command above, then `(cd backend && <PY> ../scripts/ci/check_alembic.py)`. Expected: `2 passed`, and a single head `mh01_machine_handoff`. Without the variable, record the PostgreSQL test as **NOT RUN**. The hosted `migration-check` job's advisory "upgrade head from empty DB" step must print `Running upgrade pb01_pdf_message_branches -> mh01_machine_handoff`.

**Step 5: Mutation-verify**

Remove `parent_run_id IS NULL AND` from the upgrade's `_swap_index` statement and rerun: `test_child_and_parent_share_a_thread` must fail with `IntegrityError` on the child insert. Restore the line, and confirm `git diff` on the migration is empty.

**Step 6: Commit**

```bash
git add backend/alembic/versions/mh01_machine_handoff.py backend/tests/integration/test_machine_handoff_migration_postgres.py
git commit -m "feat(db): mh01 migration for machine handoff runs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1.4: Thread-scoped lookups see top-level runs only

**Files:**
- Modify: `backend/src/services/agent/agent_run_service.py`:
  - after the helper functions at `:164-190`, add `top_level_run()`;
  - use it at `:318-323` (upsert conflict lookup), `:508-513` (transition conflict lookup), `:742-747` (`get_active_run_for_thread`) and `:772-778` (`get_latest_run_for_thread`).
- Modify: `backend/src/services/agent/agent_submission_service.py:460-467` (`_ensure_thread_idle`) and `:1053-1060` (accept `IntegrityError` fallback).
- Test: `backend/tests/unit/services/agent/test_agent_run_delegated.py`.

Every other `AgentRun.thread_id ==` predicate on develop is keyed by `job_id` as well, so a child cannot match it by accident:
- `agent_submission_service.py:504`, `:563`, `:611`, `:688`;
- `message_branches.py:327`, `:376`, `:433`;
- `integrations/context.py:619`;
- `artifacts/service.py:574`.

`delivery.py:300` scopes leasing by thread and must keep seeing children. None of these change.

**Step 1: Write the failing test**

Append:

```python
from src.services.agent.agent_run_service import (
    get_active_run_for_thread,
    get_latest_run_for_thread,
)

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("33333333-3333-3333-3333-333333333333")


async def _family(db: AsyncSession) -> tuple[str, str]:
    parent = {**_run("delegated"), "organization_id": ORG, "user_id": USER}
    child = {
        **_run("running", parent=parent["job_id"], provider="codex"),
        "organization_id": ORG,
        "user_id": USER,
    }
    await db.execute(insert(AgentRun).values(**parent))
    await db.execute(insert(AgentRun).values(**child))
    await db.commit()
    return parent["job_id"], child["job_id"]


async def test_thread_lookups_return_the_parent(db: AsyncSession) -> None:
    parent, _child = await _family(db)
    active = await get_active_run_for_thread(
        db, THREAD, organization_id=ORG, user_id=USER
    )
    latest = await get_latest_run_for_thread(
        db, THREAD, organization_id=ORG, user_id=USER, by_creation=True
    )
    assert active is not None and active.job_id == parent
    assert latest is not None and latest.job_id == parent
```

**Step 2: Run it**

Expected: FAIL with `sqlalchemy.exc.MultipleResultsFound` from `scalar_one_or_none()`.

**Step 3: Minimal implementation**

`agent_run_service.py`, after `_coerce_status`:

```python
def top_level_run() -> Any:
    """Predicate for runs that own a thread's turn (not handoff children).

    A child Codex run shares its parent's thread; thread-scoped active/latest
    lookups must never see it, or ``scalar_one_or_none`` finds two rows.
    """
    return AgentRun.parent_run_id.is_(None)
```

Add `top_level_run(),` to the `where(...)` of each lookup listed above. For example, `get_active_run_for_thread`:

```python
    stmt = select(AgentRun).where(
        AgentRun.thread_id == thread_uuid,
        top_level_run(),
        AgentRun.organization_id == _coerce_uuid(organization_id),
        AgentRun.user_id == _coerce_uuid(user_id),
        AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
    )
```

In `agent_submission_service.py`, import it (`from src.services.agent.agent_run_service import ActiveRunConflict, top_level_run`) and add `top_level_run(),` to the two selects.

**Step 4: Run it**

Run the new file plus:
- `backend/tests/unit/agent/test_agent_run_service.py`
- `backend/tests/unit/services/agent/test_agent_submission_service.py`
- `backend/tests/unit/services/harness`

Expected: PASS.

**Step 5: Commit**

```bash
git add backend/src/services/agent/agent_run_service.py backend/src/services/agent/agent_submission_service.py backend/tests/unit/services/agent/test_agent_run_delegated.py
git commit -m "fix(agent): thread run lookups ignore handoff children

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1.5: A new message during a handoff is refused with the notice

**Files:**
- Modify: `backend/src/services/agent/agent_run_service.py:58-60` (add `HandoffInProgress` after `ActiveRunConflict`).
- Modify: `backend/src/services/agent/agent_submission_service.py:452-483`.
- Modify: `backend/src/api/agent/streaming.py:320-336` (`_stream_failure_message`).
- Test: `backend/tests/unit/services/agent/test_agent_run_delegated.py`.

**Step 1: Write the failing test**

```python
from src.api.agent.streaming import _stream_failure_message
from src.services.agent.agent_run_service import HandoffInProgress
from src.services.agent.agent_submission_service import _ensure_thread_idle


async def test_new_turn_during_handoff_gets_the_notice(db: AsyncSession) -> None:
    parent, _child = await _family(db)
    with pytest.raises(HandoffInProgress) as raised:
        await _ensure_thread_idle(db, thread_id=THREAD, user_id=USER)
    assert _stream_failure_message(raised.value) == (
        "Codex is still working: cancel it or wait."
    )
    run = await db.get(AgentRun, parent)
    assert run is not None and run.status == "delegated"  # never abandoned
```

**Step 2: Run it**

Expected: FAIL with `ImportError: cannot import name 'HandoffInProgress'`.

**Step 3: Minimal implementation**

`agent_run_service.py`:

```python
class HandoffInProgress(ActiveRunConflict):
    """The thread's turn is paused on a child Codex run (machine handoff)."""

    client_message = "Codex is still working: cancel it or wait."

    def __init__(self) -> None:
        super().__init__(self.client_message)
```

In `_ensure_thread_idle`, after `if active is None: return`:

```python
    if active.status == JobStatus.DELEGATED.value:
        # Never abandoned by a new turn: only Stop/Cancel on the child, or the
        # child's end, releases a delegated parent.
        raise HandoffInProgress()
```

In `streaming.py` `_stream_failure_message`, before the `ActiveRunConflict` branch:

```python
    if isinstance(exc, HandoffInProgress):
        return HandoffInProgress.client_message
```

Also add `HandoffInProgress` to the import from `agent_run_service` at `streaming.py:50-57`. `_stream_failure_category` (`:309`) already returns `CONFLICT` for the subclass.

**Step 4: Run it**

Expected: PASS. Also run `backend/tests/unit/api/test_agent_streaming_error_categories.py`.

**Step 5: Mutation-verify**

Delete the new `if active.status == ...DELEGATED` branch and rerun: the test must fail with `ActiveRunConflict` instead of `HandoffInProgress`. Restore it.

**Step 6: Commit**

```bash
git add backend/src/services/agent/agent_run_service.py backend/src/services/agent/agent_submission_service.py backend/src/api/agent/streaming.py backend/tests/unit/services/agent/test_agent_run_delegated.py
git commit -m "feat(agent): refuse a new turn while Codex works on a handoff

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1.6: The sweeper and stale-confirmation cleanup skip delegated runs

**Files:**
- Modify: `backend/src/tasks/agent_run_tasks.py:405-412` (`_sweep_stale_agent_runs`, before `claim_lease`).
- Modify: `backend/src/services/agent/agent_execution_service.py:893-897` (`_clear_stale_pending_confirmation`, after `active_tasks` is computed).
- Test: `backend/tests/unit/tasks/test_agent_run_tasks.py`, `backend/tests/unit/services/agent/test_agent_run_delegated.py`.

**Step 1: Write the failing tests**

In `test_agent_run_tasks.py`, mirror the file's existing sweeper tests. Read how they stub `agent_run_service.list_stale_runs` and `claim_lease` first, and reuse those stubs:

```python
async def test_sweeper_skips_delegated_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.services.agent import agent_run_service
    from src.tasks import agent_run_tasks

    candidate = SimpleNamespace(
        job_id="parent", status="delegated", updated_at=None, cancel_requested_at=None
    )
    monkeypatch.setattr(
        agent_run_service, "list_stale_runs", AsyncMock(return_value=[candidate])
    )
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_run_service, "claim_lease", claim)
    result = await agent_run_tasks._sweep_stale_agent_runs(lease_owner="t")
    assert result["skipped"] == 1 and result["failed"] == 0
    claim.assert_not_awaited()
```

If that file patches `AsyncSessionLocal`, apply the same patch here.

In `test_agent_run_delegated.py`:

```python
from types import SimpleNamespace

from src.services.agent.agent_execution_service import (
    _clear_stale_pending_confirmation,
)


async def test_stale_confirmation_cleanup_keeps_a_handoff_interrupt() -> None:
    interrupt = SimpleNamespace(value={"handoff_run_id": "child"})
    snapshot = SimpleNamespace(
        values={"messages": []},
        tasks=(SimpleNamespace(interrupts=(interrupt,)),),
    )

    class Graph:
        updated = False

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return snapshot

        async def aupdate_state(self, *_a, **_k):  # type: ignore[no-untyped-def]
            Graph.updated = True

    assert await _clear_stale_pending_confirmation(Graph(), {}) is None
    assert Graph.updated is False
```

**Step 2: Run them**

Expected: FAIL. The sweeper claims a lease and then terminalizes, and the cleanup calls `aupdate_state`.

**Step 3: Minimal implementation**

In `agent_run_tasks.py`, right after the `listed_status is None` check:

```python
                if listed_status is JobStatus.DELEGATED:
                    # A handoff parent waits on its child Codex run with no
                    # time limit (Stop cancels the child). The resume drain,
                    # not this sweeper, moves it on.
                    skipped += 1
                    continue
```

In `_clear_stale_pending_confirmation`, after `if not active_tasks: return None`:

```python
    if any(
        isinstance(getattr(i, "value", None), dict)
        and "handoff_run_id" in i.value
        for task in active_tasks
        for i in task.interrupts
    ):
        # A delegated turn's outcome interrupt is only answered by the resume
        # drain; a new turn is refused (HandoffInProgress) before reaching here.
        return None
```

**Step 4: Run them**

Expected: PASS.

**Step 5: Commit**

```bash
git add backend/src/tasks/agent_run_tasks.py backend/src/services/agent/agent_execution_service.py backend/tests/unit/tasks/test_agent_run_tasks.py backend/tests/unit/services/agent/test_agent_run_delegated.py
git commit -m "fix(agent): sweeper and stale-confirm cleanup leave handoffs alone

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1.7: Regenerate OpenAPI and verify PR 1

**Files:**
- Modify (generated): `backend/openapi.json`, `frontend/src/types/generated/api.d.ts`. `JobStatusResponse.status` widens with `delegated`.

**Step 1:** Run `<PY> scripts/ci/generate_openapi.py` and then `pnpm --dir frontend generate:api-types`.

**Step 2:** Run `<PY> scripts/ci/generate_openapi.py --check`, `pnpm --dir frontend type-check`, `scripts/ci/run_local_ci.sh --base origin/develop`, and `PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q backend/tests/unit/services/agent backend/tests/unit/agent backend/tests/unit/services/harness backend/tests/unit/tasks`. Expected: all PASS.

**Step 3:** Commit:

```bash
git add backend/openapi.json frontend/src/types/generated/api.d.ts
git commit -m "chore(api): regenerate contract for the delegated run status

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

**Step 4:** Open PR 1. The body explains that the response enum widens. If `openapi-contract` reports an ERR-level break, ask the owner for the `api-breaking-approved` label (see Open questions).

---

## PR 2: Child run creation

> **Amendments from the PR 1 final review (2026-10-29).** Fold these into the tasks below.
>
> 1. **A child the sweeper ends must still resume its parent** (Task 2.6). `terminalize_stale_run` (`agent_run_service.py` ~613) does not go through `finalize_submission`, so a child the stale-run sweeper fails would never get its `handoff.resume` row. Its parent is `delegated`, is excluded from `list_stale_runs` (PR 1), and has no timeout, so it would wait forever, and every later message would get the "Codex is still working" notice. Write the resume row from the sweeper's terminal path too: extract the row write into one helper that both `finalize_submission` and `terminalize_stale_run` call in their own transactions. Add a test: sweep a stale child, then assert one pending `handoff.resume` row naming the parent. Mutation-verify it.
>    **Correction (2026-10-29, PR 2 build):** amendment 1 does not apply. `terminalize_stale_run`'s update is guarded by `execution_provider == "nous"`, and for Codex runs the sweeper only calls `record_observation(..., "unknown")`, which moves a stale child to `recovering` (`agent_run_tasks.py` ~449-455). Unknown execution is never terminal (`services/harness/runs.py`). So the sweeper never ends a child, and there is nothing to wire. Task 2.6 writes the resume row only in `finalize_submission`, through the public `write_handoff_resume`. The real gap is a machine that never comes back: the child stays `recovering`, or `stopping` after Cancel, and the parent waits in `delegated`. This is the same as today's top-level Codex runs. It is an open question for PR 3 (Task 3.8): see Open questions.
> 2. **Outbox reads that assumed one row per run** must also filter on `kind` before any run can hold a second row: `agent_submission_service.py` ~221 (`_existing_outbox_id`, `limit(1)` with no ORDER BY, so it could return the resume row), ~721, `agent_run_service.py` ~660, `harness/runs.py` ~211. Filter each on its own kind (the dispatch kinds, or `harness.execute`). Add a test per site that seeds a second-kind row.
> 3. **PR 1 removed the `_clear_stale_pending_confirmation` handoff guard on purpose.** A new turn on a delegated thread is refused earlier (`HandoffInProgress`), so by the time the cleanup runs, a handoff interrupt is always a dead one: the parent ended without resuming it, for example through `close_with_codex_reply`. Clearing it is correct. Do not re-add the guard.

### Task 2.1: Move chat-run grant minting into the service layer

**Files:**
- Create: `backend/src/services/harness/chat_context.py`. It receives `_run_consent_kind`, `create_chat_context` and `_authorize_chat_run` (`backend/src/api/agent/harness_streaming.py:39-173`), moved verbatim with their imports.
- Modify: `backend/src/api/agent/harness_streaming.py`. Delete those three functions and re-export them:

```python
# Compatibility: chat-run grant minting moved to the service layer so the
# handoff service can mint without importing the API package. Tests patch
# ``src.api.agent.harness_streaming.create_chat_context``; streaming.py's lazy
# import reads this binding at call time, so those patches still apply.
from src.services.harness.chat_context import (  # noqa: F401
    _authorize_chat_run,
    _run_consent_kind,
    create_chat_context,
)
```

- Test: `backend/tests/unit/services/harness/test_chat_context.py` (new).

**Step 1: Failing test**

```python
import pytest

pytestmark = pytest.mark.unit


def test_chat_context_lives_in_services_and_is_reexported() -> None:
    from src.api.agent import harness_streaming
    from src.services.harness import chat_context

    assert harness_streaming.create_chat_context is chat_context.create_chat_context
    assert harness_streaming._run_consent_kind is chat_context._run_consent_kind
```

**Step 2:** Run it. Expected: FAIL with `ModuleNotFoundError`.

**Step 3:** Move the functions as described. Also remove the imports in `harness_streaming.py` that only these functions used: `WorkspaceBinding`, `live_account_consent`, `project_in_account`, `DeviceBoundToAnotherChat`, `DeviceNotConnectedToProject`, `mint_integration_grant`. `ruff` flags any that remain unused.

**Step 4:** Run the new test, `backend/tests/unit/api/agent/test_harness_streaming.py`, `backend/tests/unit/services/harness/test_account_runs.py`, `backend/tests/unit/api/test_agent_streaming_error_categories.py` and `backend/tests/unit/architecture/test_agent_api_boundaries.py`. Expected: PASS.

**Step 5:** Commit:

```bash
git add backend/src/services/harness/chat_context.py backend/src/api/agent/harness_streaming.py backend/tests/unit/services/harness/test_chat_context.py
git commit -m "refactor(harness): move chat-run grant minting to services

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2.2: Extract the workspace reservation from `bind_external_submission`

**Files:**
- Modify: `backend/src/services/harness/runs.py:130-183`.
- Test: `backend/tests/unit/services/harness/test_runs.py` (existing tests must keep passing).

**Step 1:** No new behavior, so there is no new test. The guard is the existing `test_runs.py` suite, which the file docstring lists with its mutation checks.

**Step 2:** Run `backend/tests/unit/services/harness/test_runs.py` first and record PASS.

**Step 3: Implementation.** Replace the tail of `bind_external_submission` (`:157-183`):

```python
    run.execution_provider = "codex"
    run.project_id = context.project_id
    await reserve_workspace(
        db, run=run, context=context, device_id=device_id, workspace_id=workspace_id
    )


async def reserve_workspace(
    db: AsyncSession,
    *,
    run: Any,
    context: IntegrationContext,
    device_id: UUID,
    workspace_id: UUID,
) -> None:
    """Recheck authority and lock one local folder for a Codex run; never commit.

    Shared by chat acceptance and machine handoff. ``uq_harness_active_workspace``
    is the last word on a concurrent writer; this pre-check names it.
    """
    await authorize_external_submission(
        db,
        context=context,
        device_id=device_id,
        workspace_id=workspace_id,
        thread_id=run.thread_id,
    )
    existing = await db.scalar(
        select(HarnessSession.id).where(
            HarnessSession.device_id == device_id,
            HarnessSession.workspace_id == workspace_id,
            HarnessSession.workspace_locked.is_(True),
        )
    )
    if existing is not None:
        raise ActiveRunConflict("A response already owns this local workspace.")
    db.add(
        HarnessSession(
            run_id=str(run.job_id),
            grant_id=context.grant_id,
            device_id=device_id,
            workspace_id=workspace_id,
        )
    )
    await db.flush()
```

The provider flip now happens before authorization. Both happen inside the caller's uncommitted transaction, so a refusal still rolls the flip back.

**Step 4:** Rerun `test_runs.py` and `test_delivery.py`. Expected: PASS.

**Step 5:** Commit:

```bash
git add backend/src/services/harness/runs.py
git commit -m "refactor(harness): share local workspace reservation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2.3: A child run's prompt is its brief

**Files:**
- Modify: `backend/src/services/harness/prompt_context.py:44-127` (`build_harness_prompt`).
- Test: `backend/tests/unit/services/harness/test_prompt_context.py`.

**Step 1: Failing test.** Read the file's fixtures first: it builds a run and session over the `test_runs.py` fixtures. Add:

```python
async def test_handoff_child_prompt_is_the_brief_without_history(
    db: AsyncSession, external_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await db.get(AgentRun, str(external_run.id))
    assert run is not None
    run.user_message_id = None
    run.run_metadata = {"handoff_brief": "Make `pnpm test` pass in packages/x."}
    await db.commit()
    prompt = await build_harness_prompt(db, run_id=run.job_id)
    assert "Make `pnpm test` pass in packages/x." in prompt.input
    assert "Previous conversation" not in prompt.input
    assert prompt.source_message_ids == ()
    assert prompt.history_truncated is False
```

**Step 2:** Run it. Expected: FAIL with `IntegrationAccessDenied`, because the current-message check runs on `user_message_id=None`.

**Step 3: Implementation.** After `await _authorize(db, grant_context(grant), run, session)` (`:61`):

```python
    brief = (run.run_metadata or {}).get("handoff_brief")
    if isinstance(brief, str) and brief.strip():
        # Machine handoff: the NOUS agent wrote a task brief; the chat history
        # stays with NOUS. The digest pins the exact input for prompt_is_current.
        value = HANDOFF_PREFIX + brief
        return HarnessPrompt(
            input=value,
            source_message_ids=(),
            source_digest=hashlib.sha256(value.encode("utf-8")).hexdigest(),
            history_truncated=False,
        )
```

Module constant:

```python
HANDOFF_PREFIX = (
    "Task handed over from the NOUS research assistant. Work in this folder "
    "until the task is done, then reply with a short summary of what you did.\n\n"
)
```

`prompt_is_current` (`:130-147`) needs no change. `dispatch_pending` stores `harness_prompt.source_digest` for the child (`delivery.py:233-243`), and the digest comparison then holds.

**Step 4:** Rerun `test_prompt_context.py`. Expected: PASS.

**Step 5:** Commit:

```bash
git add backend/src/services/harness/prompt_context.py backend/tests/unit/services/harness/test_prompt_context.py
git commit -m "feat(harness): handoff child runs start from the task brief

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2.4: `create_handoff_run`

**Files:**
- Create: `backend/src/services/harness/handoff.py`.
- Modify: `backend/src/services/agent/agent_submission_service.py`. Add a public `insert_outbox` next to `_insert_outbox` (`:770`), and list it in `__all__` (`:1393`).
- Test: `backend/tests/unit/services/harness/test_handoff.py` (new; SQLite over the `test_runs.py` fixtures).

**Service boundary (2026-10-29, PR #2061 review).** Harness modules import only the public API of `agent_submission_service`; on develop that is `finalize_submission`. `handoff.py` must not import the private `_insert_outbox`. Add this public wrapper instead, and have `handoff.py` import it:

```python
async def insert_outbox(
    db: AsyncSession,
    *,
    run_id: str,
    organization_id: Any,
    kind: str,
    payload: dict[str, Any],
) -> AgentOutbox:
    """Public entry for other services to write an outbox record. Does NOT commit.

    Same contract as ``_insert_outbox``; the caller owns the transaction.
    """
    return await _insert_outbox(
        db, run_id=run_id, organization_id=organization_id, kind=kind, payload=payload
    )
```

`finalize_submission` lives in the same module, so it keeps calling `_insert_outbox` directly.

**Step 1: Failing tests**

```python
"""Machine handoff child runs: creation, idempotency and authority.

Mutation checks: removing the ``parent.status != RUNNING`` guard fails
test_parent_must_be_running; removing the existing-child lookup fails
test_creation_is_idempotent_by_tool_call_id (second child or workspace 409).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.bridge_device import WorkspaceBinding
from src.models.collection import Collection
from src.models.harness_session import HarnessSession
from src.models.thread import Thread
from src.schemas.integration_context import IntegrationContext
from src.services.agent.agent_run_service import ActiveRunConflict
from src.services.agent.agent_submission_service import accept_submission
from src.services.agent.schemas import AgentExecuteRequest, AgentMessage
from src.services.harness.handoff import create_handoff_run
from src.services.integrations.context import (
    DeviceNotConnectedToProject,
    IntegrationAccessDenied,
)
from src.shared.enums import JobStatus
from tests.unit.services.harness.test_runs import (  # noqa: F401
    DEVICE,
    LOCAL,
    ORG,
    THREAD,
    USER,
    WORKSPACE,
    context,
    db,
)

pytestmark = pytest.mark.unit
OWNER = SimpleNamespace(id=USER, organization_id=ORG)


@pytest.fixture(autouse=True)
def bridge_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)


async def parent_run(db: AsyncSession, status: JobStatus = JobStatus.RUNNING) -> str:
    accepted = await accept_submission(
        db,
        current_user=OWNER,
        request=AgentExecuteRequest(
            messages=[
                AgentMessage(role="user", content="fix it", client_message_id=uuid4())
            ],
            thread_id=str(THREAD),
        ),
        thread=await db.get(Thread, THREAD),
    )
    await db.execute(
        update(AgentRun)
        .where(AgentRun.job_id == accepted.run_id)
        .values(status=status.value)
    )
    await db.commit()
    return accepted.run_id


async def handoff(db: AsyncSession, parent: str, call: str = "call-1", **kw: Any) -> str:
    return await create_handoff_run(
        db,
        parent_run_id=parent,
        current_user=OWNER,
        tool_call_id=call,
        brief="Run the tests and fix the failures.",
        device_id=kw.get("device_id", DEVICE),
        workspace_id=kw.get("workspace_id", LOCAL),
    )


async def test_creates_a_queued_child_with_session_outbox_and_event(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child_id = await handoff(db, parent)
    child = await db.get(AgentRun, child_id)
    assert child is not None
    assert (child.parent_run_id, child.execution_provider, child.status) == (
        parent,
        "codex",
        JobStatus.QUEUED.value,
    )
    assert child.thread_id == THREAD and child.user_message_id is None
    assert child.run_metadata["handoff_brief"].startswith("Run the tests")
    assert child.run_metadata["handoff_tool_call_id"] == "call-1"
    session = await db.scalar(
        select(HarnessSession).where(HarnessSession.run_id == child_id)
    )
    assert session is not None and session.workspace_locked
    kinds = (
        await db.scalars(select(AgentOutbox.kind).where(AgentOutbox.run_id == child_id))
    ).all()
    assert kinds == ["harness.execute"]
    events = (
        await db.scalars(
            select(AgentRunEvent.event_type).where(AgentRunEvent.run_id == child_id)
        )
    ).all()
    assert events == ["run.created"]


async def test_creation_is_idempotent_by_tool_call_id(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    assert await handoff(db, parent) == await handoff(db, parent)
    assert (
        await db.scalar(
            select(func.count()).select_from(AgentRun).where(
                AgentRun.parent_run_id == parent
            )
        )
        == 1
    )


async def test_parent_must_be_running(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db, JobStatus.AWAITING_CONFIRMATION)
    with pytest.raises(IntegrationAccessDenied):
        await handoff(db, parent)


async def test_folder_of_another_project_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    other_project, other_folder = uuid4(), uuid4()
    db.add(Collection(id=other_project, workspace_id=WORKSPACE, name="other"))
    db.add(
        WorkspaceBinding(
            device_id=DEVICE,
            workspace_id=other_folder,
            project_id=other_project,
            label="other",
        )
    )
    await db.commit()
    parent = await parent_run(db)
    with pytest.raises(DeviceNotConnectedToProject):
        await handoff(db, parent, workspace_id=other_folder)


async def test_locked_folder_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    first = await parent_run(db)
    await handoff(db, first)
    # A second parent cannot run on the same thread; use the same parent with
    # a new call id to target the still-locked folder.
    with pytest.raises(ActiveRunConflict):
        await handoff(db, first, call="call-2")
```

**Step 2:** Run it. Expected: FAIL with `ModuleNotFoundError: src.services.harness.handoff`.

**Step 3: Implementation** (`backend/src/services/harness/handoff.py`):

```python
"""Machine handoff: a NOUS run hands a task brief to Codex on a paired machine.

The child is an ordinary Codex run (``execution_provider="codex"``) with
``parent_run_id`` set. It shares the parent's thread but never its
single-writer slot (the parent holds that as ``delegated``) and never writes
an assistant row: its reply is stored on the run for the parent's graph.

Authority is the chat path's: ``create_chat_context`` mints a short-lived
run grant from prior browser consent (project or account), and
``reserve_workspace`` locks the folder. One transaction per call; commits.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_run import AgentRun
from src.services.agent.agent_run_service import ActiveRunConflict
from src.services.agent.agent_submission_service import insert_outbox
from src.services.agent.run_event_store import append_event
from src.services.agent.run_event_types import RunEventType
from src.services.harness.chat_context import create_chat_context
from src.services.harness.runs import reserve_workspace
from src.services.integrations.context import IntegrationAccessDenied
from src.services.threads.workspace_access import get_thread_lean
from src.shared.enums import JobStatus

MAX_BRIEF_CHARS = 8000


async def existing_handoff_run(
    db: AsyncSession, *, parent_run_id: str, tool_call_id: str
) -> str | None:
    """The child this tool call already created, if any (node replay)."""
    return await db.scalar(
        select(AgentRun.job_id).where(
            AgentRun.parent_run_id == parent_run_id,
            AgentRun.run_metadata["handoff_tool_call_id"].as_string() == tool_call_id,
        )
    )


async def create_handoff_run(
    db: AsyncSession,
    *,
    parent_run_id: str,
    current_user: Any,
    tool_call_id: str,
    brief: str,
    device_id: UUID,
    workspace_id: UUID,
) -> str:
    """Create (or find) the child Codex run for one ``run_on_machine`` call.

    Raises IntegrationAccessDenied for a parent this caller cannot hand off
    from, DeviceNotConnectedToProject / DeviceBoundToAnotherChat for a target
    outside the user's consent, and ActiveRunConflict for a folder in use.
    """
    try:
        parent: Any = await db.scalar(
            select(AgentRun)
            .where(AgentRun.job_id == parent_run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            parent is None
            or parent.user_id != current_user.id
            or parent.organization_id != current_user.organization_id
            or parent.execution_provider != "nous"
            or parent.parent_run_id is not None
            or parent.thread_id is None
        ):
            raise IntegrationAccessDenied()
        existing = await existing_handoff_run(
            db, parent_run_id=parent_run_id, tool_call_id=tool_call_id
        )
        if existing is not None:
            await db.commit()
            return existing
        if parent.status != JobStatus.RUNNING.value or not brief.strip():
            raise IntegrationAccessDenied()
        thread = await get_thread_lean(db, parent.thread_id, current_user.id)
        context = await create_chat_context(
            db,
            current_user=current_user,
            thread=thread,
            device_id=device_id,
            workspace_id=workspace_id,
        )
        child = AgentRun(
            job_id=str(uuid4()),
            organization_id=parent.organization_id,
            user_id=parent.user_id,
            thread_id=parent.thread_id,
            conversation_id=parent.conversation_id,
            project_id=context.project_id,
            execution_provider="codex",
            status=JobStatus.QUEUED.value,
            parent_run_id=parent.job_id,
            run_metadata={
                "handoff_brief": brief[:MAX_BRIEF_CHARS],
                "handoff_tool_call_id": tool_call_id,
            },
        )
        db.add(child)
        await db.flush()
        await reserve_workspace(
            db,
            run=child,
            context=context,
            device_id=device_id,
            workspace_id=workspace_id,
        )
        await append_event(
            db,
            run_id=child.job_id,
            event_type=RunEventType.RUN_CREATED,
            payload={},
            organization_id=child.organization_id,
        )
        await insert_outbox(
            db,
            run_id=child.job_id,
            organization_id=child.organization_id,
            kind="harness.execute",
            payload={
                "execution_provider": "codex",
                "thread_id": str(child.thread_id),
                "user_id": str(child.user_id),
                "parent_run_id": parent.job_id,
            },
        )
        await db.commit()
        return str(child.job_id)
    except IntegrityError:
        # uq_harness_active_workspace: another writer took the folder between
        # the pre-check and the insert.
        await db.rollback()
        raise ActiveRunConflict("A response already owns this local workspace.") from None
    except Exception:
        await db.rollback()
        raise
```

`create_chat_context` sets `context.project_id` to the thread's project (`_authorize_chat_run`). `_authorize` in delivery later checks `run.project_id == run_project(context, run)` (`delivery.py:81-83`), which holds.

**Step 4:** Run the new file plus `test_runs.py`. Expected: PASS.

**Step 5:** Add account-pairing coverage. Read `backend/tests/unit/services/harness/test_account_runs.py` for its account-consent fixture (it imports `_run_consent_kind` at `:540`), and add `test_account_pairing_child_uses_account_folder`. It is the same as the first test, but with an account folder (`project_id=None`) under that fixture, and it asserts that the child's session grant carries `project_id == PROJECT`. Run it. Expected: PASS.

**Step 6: Mutation-verify** the two guards named in the docstring. Restore each one afterwards.

**Step 7:** Commit:

```bash
git add backend/src/services/harness/handoff.py backend/tests/unit/services/harness/test_handoff.py
git commit -m "feat(harness): create handoff child runs from a NOUS run

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2.5: A child writes no assistant row; its reply is stored on the run

**Files:**
- Modify: `backend/src/services/harness/delivery.py:580-626` (`project_terminal`).
- Test: `backend/tests/unit/services/harness/test_handoff.py`.

**Step 1: Failing test.** This drives the fake bridge exactly as `test_delivery.py` does. Import its helpers:

```python
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models.chat_message import ChatMessage
from src.services.harness.delivery import (
    dispatch_pending,
    ingest_bridge_event,
    lease_commands,
)
from tests.unit.services.harness.test_delivery import (  # noqa: F401
    delivery_tables,
    event,
)


async def run_child_to(
    db: AsyncSession, context: IntegrationContext, child_id: str, state: str
) -> None:
    assert await dispatch_pending(db) == 1
    (command,) = await lease_commands(db, context, DEVICE)
    assert str(command.runId) == child_id
    observe = lambda seq, s: event(  # noqa: E731
        command,
        seq,
        body={"kind": "observation", "state": s, "sessionId": "n", "turnId": "t"},
    )
    await ingest_bridge_event(db, context, observe(1, "running"))
    await ingest_bridge_event(db, context, event(command, 2))  # delta "answer"
    await ingest_bridge_event(db, context, observe(3, state))


async def test_child_reply_is_stored_on_the_run_not_the_transcript(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.agent import agent_execution_service as execution

    monkeypatch.setattr(
        execution, "AsyncSessionLocal", async_sessionmaker(db.bind, expire_on_commit=False)
    )
    child_id = await handoff(db, await parent_run(db))
    await run_child_to(db, context, child_id, "completed")
    db.expire_all()
    child = await db.get(AgentRun, child_id)
    assert child is not None and child.status == JobStatus.COMPLETED.value
    assert child.run_metadata["handoff_reply"] == "answer"
    assert child.run_metadata["handoff_brief"]  # merged, not replaced
    rows = await db.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .where(ChatMessage.client_message_id == child_id)
    )
    assert rows == 0
```

**Step 2:** Run it. Expected: FAIL. `handoff_reply` is missing, and one assistant row exists.

**Step 3: Implementation.** In `project_terminal`, replace the block from `if not terminal_already:` (`:580`) through the `finalize_submission` call (`:614-626`):

```python
    if not terminal_already:
        # A turn that fails or is interrupted before any assistant text leaves
        # no deltas. ChatMessageResponse requires non-empty content, so an empty
        # row would 500 every subsequent read of the thread (GET .../messages).
        text = (
            "".join(content)
            or {
                "failed": "External execution failed.",
                "interrupted": "External execution was stopped.",
                "completed": "External execution completed without output.",
            }[state]
        )
        child = run.parent_run_id is not None
        assistant_id = None
        if not child:
            assistant_id = await _persist_assistant_message_safe(
                thread_id=thread,
                content=text,
                model_name="codex",
                tool_executions_out=None,
                stopped=state == "interrupted",
                required=True,
                client_message_id=run_id,
            )
            if not assistant_id:
                raise RuntimeError("assistant projection required")
        run, session = await _locked(db, run_id)
        if not JobStatus(run.status).is_terminal:
            payload: dict[str, Any] = (
                {"code": "harness_failed", "message": "External execution failed."}
                if state == "failed"
                else ({} if child else {"assistant_message_id": assistant_id})
            )
            await finalize_submission(
                db,
                run_id=run_id,
                status=TERMINAL[state],
                organization_id=org,
                event_type={
                    "completed": RunEventType.RUN_COMPLETED,
                    "failed": RunEventType.RUN_FAILED,
                    "interrupted": RunEventType.RUN_CANCELLED,
                }[state],
                payload=payload,
                # A handoff child's reply goes to its parent's graph, never the
                # transcript (the parent writes the turn's one assistant row).
                run_metadata=(
                    {**(run.run_metadata or {}), "handoff_reply": text[:4000]}
                    if child
                    else None
                ),
                provider="codex",
            )
```

`has_assistant_projection` (`:644-663`) is only consulted for top-level runs on the thread resume path (Task 2.7), so it needs no change.

**Step 4:** Run the new test plus `test_delivery.py`. Expected: PASS.

**Step 5:** Commit:

```bash
git add backend/src/services/harness/delivery.py backend/tests/unit/services/harness/test_handoff.py
git commit -m "feat(harness): handoff children store their reply instead of a chat row

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2.6: The resume record is written in `finalize_submission`'s transaction

**Files:**
- Modify: `backend/src/services/agent/agent_submission_service.py:107-118` (add the constant) and `:1353-1360` (`finalize_submission`).
- Test: `backend/tests/unit/services/harness/test_handoff.py`.

**Step 1: Failing tests**

```python
from src.services.agent.agent_submission_service import (
    HANDOFF_RESUME_KIND,
    finalize_submission,
    request_run_cancellation,
)
from src.services.agent.run_event_types import RunEventType


async def resume_rows(db: AsyncSession, child_id: str) -> list[dict]:
    return list(
        (
            await db.scalars(
                select(AgentOutbox.payload).where(
                    AgentOutbox.run_id == child_id,
                    AgentOutbox.kind == HANDOFF_RESUME_KIND,
                )
            )
        ).all()
    )


async def test_two_terminal_deliveries_write_one_resume_record(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child_id = await handoff(db, parent)
    for _ in range(2):
        await finalize_submission(
            db,
            run_id=child_id,
            status=JobStatus.FAILED,
            organization_id=ORG,
            event_type=RunEventType.RUN_FAILED,
            payload={"code": "harness_failed", "message": "usage limit"},
            provider="codex",
        )
    assert await resume_rows(db, child_id) == [{"parent_run_id": parent}]


async def test_cancelling_a_queued_child_writes_its_resume_record(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child_id = await handoff(db, parent)
    result = await request_run_cancellation(
        db,
        run_id=child_id,
        thread_id=THREAD,
        organization_id=ORG,
        user_id=USER,
        reason="user_requested",
    )
    await db.commit()
    assert result is not None and result.status is JobStatus.CANCELLED
    assert await resume_rows(db, child_id) == [{"parent_run_id": parent}]


async def test_top_level_codex_runs_write_no_resume_record(
    db: AsyncSession, context: IntegrationContext
) -> None:
    from tests.unit.services.harness.test_runs import accept

    accepted = await accept(db, context)
    await finalize_submission(
        db,
        run_id=accepted.run_id,
        status=JobStatus.COMPLETED,
        organization_id=ORG,
        event_type=RunEventType.RUN_COMPLETED,
        payload={},
        provider="codex",
    )
    assert await resume_rows(db, accepted.run_id) == []
```

**Step 2:** Run it. Expected: FAIL with `ImportError: HANDOFF_RESUME_KIND`.

**Step 3: Implementation.** Constant next to `DISPATCH_KIND_STREAM`:

```python
# A finished machine-handoff child's promise to resume its parent's graph.
# Keyed by the CHILD run id (uq_agent_outbox_run_kind) so one parent can hand
# off more than once; claimed exactly once by the resume drain.
HANDOFF_RESUME_KIND = "handoff.resume"
```

In `finalize_submission`, replace the `if provider == "codex" and status in TERMINAL_JOB_STATUSES:` block (`:1353-1360`):

```python
            if provider == "codex" and status in TERMINAL_JOB_STATUSES:
                from src.models.harness_session import HarnessSession

                await db.execute(
                    update(HarnessSession)
                    .where(HarnessSession.run_id == run_id)
                    .values(workspace_locked=False)
                )
                # Same transaction as the terminal transition: a finished child
                # always leaves a resume record for its parent, exactly once
                # (the guarded UPDATE above wins only once).
                parent_run_id = (
                    await db.execute(
                        select(AgentRun.parent_run_id).where(AgentRun.job_id == run_id)
                    )
                ).scalar_one_or_none()
                if parent_run_id is not None:
                    await _insert_outbox(
                        db,
                        run_id=run_id,
                        organization_id=organization_id,
                        kind=HANDOFF_RESUME_KIND,
                        payload={"parent_run_id": str(parent_run_id)},
                    )
```

`_insert_outbox` is defined above `finalize_submission` (`:770`). Add `HANDOFF_RESUME_KIND` to `__all__`. `cancel_undelivered_start` and `dispatch_pending`'s `terminalize` both reach this block through `finalize_submission(..., commit=False)`, so every terminal path for a Codex run is covered. `record_observation`'s terminal branch (`runs.py:302-320`) writes the status directly. On develop it is reached only with `"unknown"` (`agent_run_tasks.py:451-455`), so it never terminalizes a child. Task 3.11 adds a regression test that pins this.

**Step 4:** Run the new tests plus `test_agent_submission_service.py`, `test_finalize_submission_linkage.py` and `test_delivery.py`. Expected: PASS.

**Step 5: Mutation-verify.** Replace `if parent_run_id is not None:` with `if True:` and rerun: `test_top_level_codex_runs_write_no_resume_record` must fail. Then remove the guarded-UPDATE dependency by moving the insert above the `if transitioned is None` return: `test_two_terminal_deliveries_write_one_resume_record` must fail with `IntegrityError`. Restore both.

**Step 6:** Commit:

```bash
git add backend/src/services/agent/agent_submission_service.py backend/tests/unit/services/harness/test_handoff.py
git commit -m "feat(agent): finished handoff children leave one resume record

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2.7: Thread resume never attaches to a child

**Files:**
- Test only: `backend/tests/agent/test_streaming_resume.py`. It already exercises `resume_stream` with harness runs; read its fixtures first.

Task 1.4 already filters `get_active_run_for_thread` and `get_latest_run_for_thread`, which every branch of `resume_stream` (`execute.py:1373-1587`) uses. This task pins that behavior.

**Step 1: Test**

```python
async def test_resume_without_stream_id_does_not_replay_a_handoff_child(...) -> None:
    # Arrange with the module's helpers: a delegated NOUS parent and a running
    # codex child (parent_run_id set) on the thread; no Redis active pointer.
    # Act: GET /agent/stream/resume/{thread_id} (no ?stream=).
    # Assert: the response is NOT the child's ledger stream
    # (stream_harness_run not called), i.e. 204 until Task 3.9 adds the
    # handoff_started frame for the delegated parent.
```

Fill in the body with the file's existing fixtures for a codex run (search for `stream_harness_run` in that file) and set `parent_run_id` on it.

**Step 2:** Run it. Expected: PASS already, because of Task 1.4. Then revert Task 1.4's `top_level_run()` in `get_active_run_for_thread` and confirm the test fails, because the child's ledger is streamed. Restore it.

**Step 3:** Commit:

```bash
git add backend/tests/agent/test_streaming_resume.py
git commit -m "test(agent): thread resume ignores handoff children

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Open PR 2 after running PR 1's verification commands.

---

## PR 3: Graph

### Task 3.1: The `handoff_started` SSE frame (both vocabularies)

**Files:**
- Modify: `backend/src/shared/enums.py:321-326` (member) and `:380-386` (terminal set).
- Modify: `backend/tests/contract/test_sse_event_vocabulary.py`, the `_EXPECTED_WIRE_VALUES` list. Append `"handoff_started"`.
- Modify: `frontend/src/services/agentStreamEvents.ts:13-30` and `:110-114`.
- Modify: `frontend/src/services/agentChatService.ts:99-182` (callback), `:254-271` (`HANDLED_STREAM_EVENTS`) and the `switch` at `:338-489`.
- Test: `frontend/src/services/__tests__/agentStreamEvents.contract.test.ts` (existing), plus a new case in `frontend/src/services/__tests__/agentChatService.test.ts`. If that file name differs, add the case to whichever file tests `consumeSse`.

**Step 1: Failing tests.** Backend: add `"handoff_started"` to `_EXPECTED_WIRE_VALUES` and run `PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q backend/tests/contract/test_sse_event_vocabulary.py`. Expected: FAIL, because the enum has no such value. Frontend, in the `consumeSse` test file:

```ts
it('reports handoff_started with both run ids and treats it as terminal', async () => {
  const onHandoffStarted = vi.fn();
  const onConnectionLost = vi.fn();
  await consumeFrames(
    [
      'event: handoff_started\ndata: {"thread_id":"t","run_id":"p","handoff_run_id":"c"}\n\n',
    ],
    { onHandoffStarted, onConnectionLost }
  );
  expect(onHandoffStarted).toHaveBeenCalledWith({
    threadId: 't',
    runId: 'p',
    handoffRunId: 'c',
  });
  expect(onConnectionLost).not.toHaveBeenCalled();
});
```

`consumeFrames` stands for that file's helper that feeds frames into `consumeSse`; use the helper the file already has. Run `pnpm --dir frontend exec vitest run src/services/__tests__`. Expected: FAIL.

**Step 2: Implementation.**

Backend:

```python
    # Last frame of a NOUS turn that handed a task to Codex: names the child
    # run whose saved events the live card opens (GET /agent/runs/{id}/stream).
    HANDOFF_STARTED = "handoff_started"
```

Add `AgentStreamEvent.HANDOFF_STARTED` to `TERMINAL_STREAM_EVENTS`.

Frontend: add `'handoff_started'` after `'artifact'` in `AGENT_STREAM_EVENTS`, and to `TERMINAL_STREAM_EVENTS` and `HANDLED_STREAM_EVENTS`. Add the callback to `AgentStreamCallbacks`:

```ts
  /** The turn paused on a child Codex run (machine handoff); terminal. */
  onHandoffStarted?: (handoff: {
    threadId: string;
    runId: string;
    handoffRunId: string;
  }) => void;
```

Add the `switch` case:

```ts
        case 'handoff_started':
          if (
            typeof data.run_id === 'string' &&
            typeof data.handoff_run_id === 'string'
          ) {
            callbacks.onHandoffStarted?.({
              threadId: String(data.thread_id ?? ''),
              runId: data.run_id,
              handoffRunId: data.handoff_run_id,
            });
          }
          break;
```

**Step 3:** Rerun both. Expected: PASS.

**Step 4:** Commit:

```bash
git add backend/src/shared/enums.py backend/tests/contract/test_sse_event_vocabulary.py frontend/src/services/agentStreamEvents.ts frontend/src/services/agentChatService.ts frontend/src/services/__tests__
git commit -m "feat(agent): handoff_started stream frame

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3.2: `run_on_machine` tool schema and descriptor

**Files:**
- Modify: `backend/src/services/agent/tools.py`. Add the schema after `execute_code` (`:424-440`) and the descriptor after the `execute_code` descriptor (`:810-843`).
- Modify: `backend/src/services/agent/tools_impl.py:2420-2423` (`_dispatch_tool`).
- Modify: `backend/src/services/agent/state.py:53-54` (add `handoff_decision`).
- Test: `backend/tests/unit/services/test_agent_tool_registry.py`:
  - `expected_intents["general"]`;
  - `expected_subgraphs["research"]`;
  - the DESTRUCTIVE and NO_OUTER_RETRY sets in `test_policy_tags_keep_legacy_execution_policy` (`:508-560`);
  - a new reachability test.

**Step 1: Failing test**

```python
    def test_run_on_machine_is_reachable_and_gated(self) -> None:
        from src.services.agent.tool_registry import ToolPolicyTag
        from src.services.agent.tools import ALL_TOOLS, TOOL_REGISTRY

        assert "run_on_machine" in {
            d.name for d in TOOL_REGISTRY.descriptors_for_intent("general")
        }
        assert "run_on_machine" in {
            d.name for d in TOOL_REGISTRY.descriptors_for_subgraph("research")
        }
        assert TOOL_REGISTRY.has_policy("run_on_machine", ToolPolicyTag.DESTRUCTIVE)
        # Legacy ALL_TOOLS order is pinned; the tool stays out of it.
        assert "run_on_machine" not in {tool.name for tool in ALL_TOOLS}
```

Add `"run_on_machine"` to the general intents set, the research subgraph set, the DESTRUCTIVE set and the NO_OUTER_RETRY set in the existing tests. Run `backend/tests/unit/services/test_agent_tool_registry.py`. Expected: FAIL.

**Step 2: Implementation.** Schema in `tools.py`:

```python
@tool
async def run_on_machine(
    brief: str,
    machine: Optional[str] = None,
    folder: Optional[str] = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Hand a coding task to Codex on the user's own paired computer.

    Use this for multi-step coding work — editing several files, running a
    build or the tests, fixing failures — or for work on large local files.
    For a quick Python calculation or plot, use ``execute_code`` (cloud
    sandbox) instead.

    ``brief`` is a task brief, not code: the goal, the files involved, and
    what done looks like. Set ``machine`` or ``folder`` only when the user
    named them. The user approves the computer and folder before anything
    runs; this must be the only tool call in its step. The result is Codex's
    final reply plus the commands it ran, their exit codes and the files it
    changed.
    """
    _schema_only()
```

Descriptor:

```python
        ToolDescriptor(
            name="run_on_machine",
            # Served by the handoff nodes (services/agent/handoff.py), never by
            # tool_node: GENERAL for the main path, RESEARCH next to
            # execute_code. Hidden from the legacy ALL_TOOLS list.
            tool=run_on_machine,
            intents=frozenset({AgentIntent.GENERAL}),
            subgraphs=frozenset({AgentSubgraph.RESEARCH}),
            subgraph_positions=((AgentSubgraph.RESEARCH, 10),),
            policy_tags=frozenset(
                {ToolPolicyTag.DESTRUCTIVE, ToolPolicyTag.NO_OUTER_RETRY}
            ),
            effect_mode=ToolEffectMode.EXTERNAL,
            exposed_in_all_tools=False,
        ),
```

Check `subgraph_positions`: research positions must be dense and unique (`test_rejects_incomplete_or_ambiguous_subgraph_positions`). If 10 is taken or leaves a gap, use the next free index.

`tools_impl.py`, before the `execute_code` branch:

```python
    if tool_name == "run_on_machine":
        # Only the handoff nodes serve this tool; a direct dispatch is a
        # routing bug and must not reach a device.
        return {"error": "run_on_machine is not available in this context"}
```

`state.py`, after `user_confirmed: bool`:

```python
    # Machine handoff: the approval decision for the pending run_on_machine
    # call ({tool_call_id, confirmed, target}); read by handoff_node.
    handoff_decision: NotRequired[dict[str, Any]]
```

Check that `Any` is imported in `state.py`.

**Step 3:** Rerun the registry tests and `backend/tests/unit/services/agent/test_tool_wrapper_contracts.py`. Expected: PASS. The metadata-hash snapshot test may need its literal re-recorded; say so in the PR.

**Step 3b: Feature flag (owner decision, 2026-10-28).** PR 3 must not make the tool reachable before PR 5 ships the card it needs. Add a setting next to the other agent flags in `backend/src/core/config.py`:

```python
    # Machine handoff (docs/plans/2026-10-28-machine-handoff-design.md). Off
    # until the frontend card ships (PR 5); turn on per environment.
    MACHINE_HANDOFF_ENABLED: bool = False
```

While it is off:

- `run_on_machine` is not bound to the model at all. Find where the general path and the research subgraph turn descriptors into bound tools (`descriptors_for_intent` / `descriptors_for_subgraph` callers, e.g. `llm_node` and `subgraphs/_factory.py`), and filter `run_on_machine` out there, in one shared helper.
- Every place that sets `configurable.handoff_enabled = True` (stream producers, resume drain) sets it to `get_settings().MACHINE_HANDOFF_ENABLED` instead.

Failing test first, in `test_agent_tool_registry.py` or next to the binding helper. With the flag patched off, the bound tool names for general and research exclude `run_on_machine`; with it on, they include it. The registry test above still sees the descriptor; it checks the registry, not the binding. Deploy values keep the flag off until PR 5 merges; turning it on in dev is a separate values change.

**Step 4:** Commit:

```bash
git add backend/src/core/config.py backend/src/services/agent/tools.py backend/src/services/agent/tools_impl.py backend/src/services/agent/state.py backend/tests/unit/services/test_agent_tool_registry.py
git commit -m "feat(agent): run_on_machine tool schema and descriptor

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3.3: Proposed target and target validation

**Files:**
- Modify: `backend/src/services/harness/handoff.py`.
- Test: `backend/tests/unit/services/harness/test_handoff.py`.

**Step 1: Failing tests**

```python
from src.services.harness.handoff import (
    HandoffRefusal,
    HandoffTarget,
    propose_target,
    validate_target,
)
from tests.unit.services.harness.test_runs import PROJECT


async def test_default_skips_locked_folders_and_prefers_last_used(
    db: AsyncSession, context: IntegrationContext
) -> None:
    second = uuid4()
    db.add(
        WorkspaceBinding(
            device_id=DEVICE, workspace_id=second, project_id=PROJECT, label="b"
        )
    )
    await db.commit()
    thread = await db.get(Thread, THREAD)
    first = await propose_target(db, current_user=OWNER, thread=thread)
    assert isinstance(first, HandoffTarget) and first.device_label == "laptop"
    # Lock the proposed folder: the default moves to the other one.
    await handoff(db, await parent_run(db), workspace_id=first.workspace_id)
    nxt = await propose_target(db, current_user=OWNER, thread=thread)
    assert isinstance(nxt, HandoffTarget) and nxt.workspace_id != first.workspace_id


async def test_named_folder_wins(db: AsyncSession, context: IntegrationContext) -> None:
    thread = await db.get(Thread, THREAD)
    got = await propose_target(db, current_user=OWNER, thread=thread, folder="REPO")
    assert isinstance(got, HandoffTarget) and got.workspace_id == LOCAL


async def test_chat_without_project_is_refused(db: AsyncSession) -> None:
    thread = await db.get(Thread, THREAD)
    assert thread is not None
    thread.source_project_id = None
    got = await propose_target(db, current_user=OWNER, thread=thread)
    assert isinstance(got, HandoffRefusal) and "project" in got.reason


async def test_no_paired_machine_is_refused(db: AsyncSession) -> None:
    # No consent exchanged (no `context` fixture): the device serves nothing.
    thread = await db.get(Thread, THREAD)
    got = await propose_target(db, current_user=OWNER, thread=thread)
    assert isinstance(got, HandoffRefusal) and "computer" in got.reason


async def test_validate_target_refuses_a_foreign_folder(
    db: AsyncSession, context: IntegrationContext
) -> None:
    thread = await db.get(Thread, THREAD)
    with pytest.raises(DeviceNotConnectedToProject):
        await validate_target(
            db, current_user=OWNER, thread=thread, device_id=DEVICE, workspace_id=uuid4()
        )
```

Import `PROJECT` from `test_runs` as well. Run it. Expected: FAIL (`ImportError`).

**Step 2: Implementation** (append to `handoff.py`):

```python
from dataclasses import asdict, dataclass

from src.models.bridge_device import WorkspaceBinding
from src.models.harness_session import HarnessSession
from src.services.harness.chat_context import _run_consent_kind
from src.services.integrations.context import (
    DeviceNotConnectedToProject,
    list_devices,
)

NO_PROJECT = (
    "This chat is not in a project. Move it into a project to hand work to "
    "your computer."
)
NO_MACHINE = (
    "No paired computer can work on this project. Connect one with "
    "`nous --rc` (or `nous-harness connect`), then ask again."
)


@dataclass(frozen=True)
class HandoffTarget:
    device_id: UUID
    device_label: str
    workspace_id: UUID
    workspace_label: str

    def wire(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


@dataclass(frozen=True)
class HandoffRefusal:
    reason: str


async def _candidates(
    db: AsyncSession, *, current_user: Any, thread: Any
) -> list[HandoffTarget]:
    """Every unlocked folder a run on this chat could use, last used first."""
    project = thread.source_project_id
    devices = [
        d
        for d in await list_devices(db, current_user)
        if (d.account_wide or project in d.project_ids)
        and (not d.bound_thread_ids or thread.id in d.bound_thread_ids)
    ]
    if not devices:
        return []
    labels = {d.id: d.label for d in devices}
    bindings = (
        await db.scalars(
            select(WorkspaceBinding).where(
                WorkspaceBinding.device_id.in_(list(labels)),
                WorkspaceBinding.is_deleted.is_(False),
            )
        )
    ).all()
    locked = {
        tuple(row)
        for row in (
            await db.execute(
                select(HarnessSession.device_id, HarnessSession.workspace_id).where(
                    HarnessSession.device_id.in_(list(labels)),
                    HarnessSession.workspace_locked.is_(True),
                )
            )
        ).all()
    }
    account = {d.id for d in devices if d.account_wide}
    usable = [
        b
        for b in bindings
        if (b.project_id == project or (b.project_id is None and b.device_id in account))
        and (b.device_id, b.workspace_id) not in locked
    ]
    recent = [
        tuple(row)
        for row in (
            await db.execute(
            select(HarnessSession.device_id, HarnessSession.workspace_id)
            .join(AgentRun, AgentRun.job_id == HarnessSession.run_id)
            .where(
                AgentRun.user_id == current_user.id,
                AgentRun.organization_id == current_user.organization_id,
                AgentRun.project_id == project,
            )
            .order_by(AgentRun.created_at.desc())
            .limit(50)
            )
        ).all()
    ]
    rank = {pair: index for index, pair in reversed(list(enumerate(recent)))}
    usable.sort(
        key=lambda b: (
            rank.get((b.device_id, b.workspace_id), len(recent)),
            labels[b.device_id].casefold(),
            b.label.casefold(),
        )
    )
    return [
        HandoffTarget(b.device_id, labels[b.device_id], b.workspace_id, b.label)
        for b in usable
    ]


async def propose_target(
    db: AsyncSession,
    *,
    current_user: Any,
    thread: Any,
    machine: str | None = None,
    folder: str | None = None,
) -> HandoffTarget | HandoffRefusal:
    """Code, not the model, proposes where the task runs (read-only).

    A machine or folder the user named (label, case-insensitive) wins; then
    the folder last used for this project; locked folders never qualify.
    The backend has no device presence signal, so "online" is not a filter.
    """
    if thread is None or thread.source_project_id is None:
        return HandoffRefusal(NO_PROJECT)
    candidates = await _candidates(db, current_user=current_user, thread=thread)
    if not candidates:
        return HandoffRefusal(NO_MACHINE)

    def named(c: HandoffTarget) -> tuple[bool, bool]:
        return (
            bool(folder) and c.workspace_label.casefold() == folder.casefold(),
            bool(machine) and c.device_label.casefold() == machine.casefold(),
        )

    return sorted(candidates, key=lambda c: tuple(not n for n in named(c)))[0]


async def validate_target(
    db: AsyncSession,
    *,
    current_user: Any,
    thread: Any,
    device_id: UUID,
    workspace_id: UUID,
) -> HandoffTarget:
    """Server re-check of an edited target before the approval is resumed.

    The binding must belong to the user, be live, and serve the chat's
    project under its project or account consent (``_run_consent_kind``, the
    rule run grants are minted under), and the folder must be free.
    ``create_handoff_run`` re-checks all of it authoritatively.
    """
    if thread is None or thread.source_project_id is None:
        raise DeviceNotConnectedToProject()
    for candidate in await _candidates(db, current_user=current_user, thread=thread):
        if (candidate.device_id, candidate.workspace_id) == (device_id, workspace_id):
            try:
                await _run_consent_kind(
                    db,
                    current_user=current_user,
                    thread=thread,
                    device_id=device_id,
                    workspace_id=workspace_id,
                )
            except IntegrationAccessDenied:
                # One message for every refusal, as create_chat_context does.
                raise DeviceNotConnectedToProject() from None
            return candidate
    raise DeviceNotConnectedToProject()
```

`list_devices` (`integrations/context.py:1311-1380`) already filters by owner, org, not revoked and not deleted, and computes `account_wide` / `project_ids` / `bound_thread_ids` with the mint's own rules. `IntegrationAccessDenied` is already imported at the top of this module (Task 2.4).

**Step 3:** Run it. Expected: PASS.

**Step 4: Mutation-verify.** Drop the `not in locked` filter: the first test must fail. Drop the `_run_consent_kind` call and drop the project filter in `_candidates`: `test_validate_target_refuses_a_foreign_folder` must fail. Restore both.

**Step 5:** Commit:

```bash
git add backend/src/services/harness/handoff.py backend/tests/unit/services/harness/test_handoff.py
git commit -m "feat(harness): propose and validate the handoff target

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3.4: The editable target rides the confirm request and is re-checked

**Files:**
- Modify: `backend/src/services/agent/confirmation_service.py:35-38`.
- Modify: `backend/src/api/agent/execute.py:241-244` (`StreamConfirmRequest`).
- Modify: `backend/src/api/agent/streaming.py`. After the `require_approval` block at `:3784-3797`, add the target check. At `:4046-4049`, pass the extra value.
- Modify (generated): `backend/openapi.json`, `frontend/src/types/generated/api.d.ts`.
- Test: `backend/tests/unit/agent/test_confirmation_service.py`, plus a new `backend/tests/unit/api/agent/test_handoff_confirm.py`.

> **Amendment (2026-10-29, PR #2061 review, P1): approve exactly what was shown.** `handoff_approval_node` no longer falls back to a re-computed proposal (Task 3.5). So on every approve, the confirm path must put a `target` into the resume value. That is the user's edited target if one was sent. Otherwise it is the `handoff_target` read from the **verified saved interrupt**: the same snapshot `require_approval` already checked against the receipt, never a fresh `_propose`. Then re-validate that exact target (binding owned, not deleted, folder in this project or covered by the account pairing, folder not held by `uq_harness_active_workspace`). If it fails, refuse the confirm with a stable error the card can show ("That folder is no longer available. Pick another."). Never substitute a different folder. This keeps the complete-action approval binding in `docs/engineering/agent-approvals.md`. Add tests: (a) approve without an edit after the last-used folder changes → the displayed folder runs, or the confirm is refused; it is never the new one. (b) The displayed folder becomes locked before approve → refused, the parent stays parked, and the card can re-try with an edit. Mutation-verify (a) by restoring the old fallback.

**Step 1: Failing tests.** In `test_confirmation_service.py`:

```python
def test_resume_value_can_carry_the_handoff_target() -> None:
    approval = PendingApproval("a" * 64, "intr", {"tools": []})
    assert approval.resume(True, extra={"target": {"device_id": "d"}}) == {
        "intr": {"confirmed": True, "target": {"device_id": "d"}}
    }
    assert approval.resume(False, extra={"target": {}}) == {
        "intr": {"confirmed": False}
    }
```

New `test_handoff_confirm.py`. It tests a pure helper so that the 1,300-line generator only gains one call:

```python
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from src.api.agent import streaming
from src.services.integrations.context import DeviceNotConnectedToProject

pytestmark = pytest.mark.unit


async def test_target_for_a_non_handoff_approval_is_refused() -> None:
    body = SimpleNamespace(confirmed=True, handoff_target=SimpleNamespace(
        device_id=uuid4(), workspace_id=uuid4()))
    approval = SimpleNamespace(details={"tools": [{"name": "create_project"}]})
    with pytest.raises(ValueError):
        await streaming._handoff_resume_extra(None, body, approval, None, None)


async def test_foreign_target_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.services.harness import handoff

    monkeypatch.setattr(
        handoff, "validate_target", AsyncMock(side_effect=DeviceNotConnectedToProject())
    )
    body = SimpleNamespace(confirmed=True, handoff_target=SimpleNamespace(
        device_id=uuid4(), workspace_id=uuid4()))
    approval = SimpleNamespace(details={"handoff_target": {}})
    with pytest.raises(DeviceNotConnectedToProject):
        await streaming._handoff_resume_extra(None, body, approval, None, None)


async def test_no_target_resumes_with_the_proposal() -> None:
    body = SimpleNamespace(confirmed=True, handoff_target=None)
    approval = SimpleNamespace(details={"handoff_target": {"device_id": "d"}})
    assert await streaming._handoff_resume_extra(None, body, approval, None, None) is None
```

Run both. Expected: FAIL (`TypeError` on `extra`, `AttributeError` on `_handoff_resume_extra`).

**Step 2: Implementation.**

`confirmation_service.py`:

```python
    def resume(
        self, confirmed: bool, *, extra: dict[str, Any] | None = None
    ) -> dict[str, dict[str, Any]]:
        # A bare {confirmed: bool} resumes the next interrupt. Keying the value
        # by interrupt id also fences dispatch if the checkpoint advances.
        # ``extra`` (machine handoff's edited target) rides only an approval;
        # the receipt binds the saved interrupt, never the resume value.
        value: dict[str, Any] = {"confirmed": confirmed}
        if confirmed and extra:
            value.update(extra)
        return {self.interrupt_id: value}
```

`execute.py`:

```python
class HandoffTargetRequest(BaseModel):
    device_id: UUID
    workspace_id: UUID


class StreamConfirmRequest(BaseModel):
    approval_id: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    thread_id: StrictUUIDString
    confirmed: bool
    # Machine handoff only: the computer and folder the user chose on the card.
    handoff_target: Optional[HandoffTargetRequest] = None
```

`streaming.py`, as a module-level helper near `_stream_failure_message`:

```python
async def _handoff_resume_extra(
    db: Any, request_body: Any, approval: Any, current_user: Any, thread: Any
) -> Optional[Dict[str, Any]]:
    """Re-check the target a handoff approval card sent; None keeps the
    code-proposed target. Raises ValueError for a target on any other
    approval and DeviceNotConnectedToProject for one outside the grant."""
    target = getattr(request_body, "handoff_target", None)
    if target is None or not request_body.confirmed:
        return None
    if "handoff_target" not in (approval.details or {}):
        raise ValueError("This approval does not take a computer or folder.")
    from src.services.harness import handoff

    checked = await handoff.validate_target(
        db,
        current_user=current_user,
        thread=thread,
        device_id=target.device_id,
        workspace_id=target.workspace_id,
    )
    return {"target": checked.wire()}
```

In `stream_confirm_event_generator`, right after the first `require_approval` `try/except` (`:3784-3797`) succeeds:

```python
        try:
            handoff_extra = await _handoff_resume_extra(
                db, request_body, approval, current_user, resolved_thread
            )
        except (ValueError, DeviceNotConnectedToProject) as exc:
            # Refused before the claim: the approval stays parked and the card
            # shows why.
            yield await emitter.emit(
                AgentStreamEvent.ERROR,
                error_frame_payload(
                    str(exc) if isinstance(exc, ValueError)
                    else DeviceNotConnectedToProject.client_message,
                    AgentErrorCategory.DEVICE_NOT_CONNECTED_TO_PROJECT
                    if isinstance(exc, DeviceNotConnectedToProject)
                    else AgentErrorCategory.INVALID_REQUEST,
                ),
            )
            return
```

At `:4048`, use `resume=approval.resume(request_body.confirmed, extra=handoff_extra),`. Read the function first, and confirm that `resolved_thread` is bound on the persisted-thread path (`:3769`). If the threadless path leaves it `None`, `_candidates` returns `[]` and the target is refused, which is the correct fail-closed result. `ActiveRunConflict` raised from `validate_target` cannot happen, because a locked folder is simply not a candidate.

**Step 3:** Regenerate OpenAPI and the frontend types (Conventions). Rerun both test files and `backend/tests/unit/api/test_agent_streaming_confirm_disconnect.py`. Expected: PASS. Adding an optional request field is non-breaking.

**Step 4:** Commit:

```bash
git add backend/src/services/agent/confirmation_service.py backend/src/api/agent/execute.py backend/src/api/agent/streaming.py backend/openapi.json frontend/src/types/generated/api.d.ts backend/tests/unit/agent/test_confirmation_service.py backend/tests/unit/api/agent/test_handoff_confirm.py
git commit -m "feat(agent): handoff approvals accept a re-checked target

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3.5: `handoff_approval_node` and `handoff_node`

**Files:**
- Create: `backend/src/services/agent/handoff.py`.
- Test: `backend/tests/unit/services/agent/test_handoff_nodes.py` (new).

**Step 1: Failing tests.** These run both nodes in a tiny `StateGraph` with `MemorySaver`. The services the nodes call are patched, so the tests check routing, interrupt order and replay.

```python
"""Handoff nodes: approval, alone-rule, idempotent child, outcome message.

Mutation checks: calling create_handoff_run without the existing-child path
(remove ``existing_handoff_run``) fails test_replay_creates_one_child;
removing the alone-rule fails test_other_calls_in_the_step_are_refused.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command

from src.services.agent import handoff as nodes
from src.services.agent.state import AgentState
from src.services.harness.handoff import HandoffRefusal, HandoffTarget

pytestmark = pytest.mark.unit
TARGET = HandoffTarget(uuid4(), "laptop", uuid4(), "repo")


def graph() -> Any:
    g = StateGraph(AgentState)
    g.add_node("approve", nodes.make_handoff_approval_node("main"))
    g.add_node("run", nodes.make_handoff_node("main"))
    g.set_entry_point("approve")
    g.add_conditional_edges(
        "approve",
        nodes.route_after_handoff_approval("run", "llm", "reflect"),
        {"run": "run", "llm": END, "reflect": END},
    )
    g.add_edge("run", END)
    return g.compile(checkpointer=MemorySaver())


def config() -> dict:
    return {
        "configurable": {
            "thread_id": "t",
            "user_id": "u",
            "organization_id": "o",
            "handoff_enabled": True,
        },
        "metadata": {"agent_run_id": "parent"},
    }


def call(*extra: dict) -> dict:
    return {
        "messages": [
            HumanMessage(content="fix the build"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "h1", "name": "run_on_machine", "args": {"brief": "fix it"}},
                    *extra,
                ],
            ),
        ],
        "user_id": "u",
    }


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    mocks = {
        "context": AsyncMock(return_value=(object(), object())),
        "propose": AsyncMock(return_value=TARGET),
        "existing": AsyncMock(return_value=None),
        "create": AsyncMock(return_value="child-1"),
    }
    monkeypatch.setattr(nodes, "_actor_and_thread", mocks["context"])
    monkeypatch.setattr(nodes, "_propose", mocks["propose"])
    monkeypatch.setattr(nodes, "_existing_child", mocks["existing"])
    monkeypatch.setattr(nodes, "_create_child", mocks["create"])
    monkeypatch.setattr(nodes, "hitl_log_raised", lambda *_a: None)
    monkeypatch.setattr(nodes, "record_hitl_decision", AsyncMock())
    return mocks


async def test_approve_then_outcome_becomes_the_tool_message(services) -> None:
    g, cfg = graph(), config()
    await g.ainvoke(call(), cfg)
    first = (await g.aget_state(cfg)).tasks[0].interrupts[0].value
    assert first["tools"][0]["name"] == "run_on_machine"
    assert first["handoff_target"]["workspace_label"] == "repo"
    await g.ainvoke(Command(resume={"confirmed": True}), cfg)
    second = (await g.aget_state(cfg)).tasks[0].interrupts[0].value
    assert second == {"handoff_run_id": "child-1", "tool_call_id": "h1"}
    final = await g.ainvoke(
        Command(resume={"status": "completed", "reply": "done"}), cfg
    )
    message = final["messages"][-1]
    assert isinstance(message, ToolMessage) and message.tool_call_id == "h1"
    assert json.loads(message.content)["reply"] == "done"


async def test_replay_creates_one_child(services) -> None:
    g, cfg = graph(), config()
    await g.ainvoke(call(), cfg)
    await g.ainvoke(Command(resume={"confirmed": True}), cfg)
    services["existing"].return_value = "child-1"
    await g.ainvoke(Command(resume={"status": "completed", "reply": "x"}), cfg)
    assert services["create"].await_count == 1


async def test_denied_creates_no_child(services) -> None:
    g, cfg = graph(), config()
    await g.ainvoke(call(), cfg)
    final = await g.ainvoke(Command(resume={"confirmed": False}), cfg)
    services["create"].assert_not_awaited()
    assert "cancelled" in final["messages"][-1].content.lower()


async def test_no_project_is_refused_without_asking(services) -> None:
    services["propose"].return_value = HandoffRefusal("Move it into a project.")
    g, cfg = graph(), config()
    final = await g.ainvoke(call(), cfg)
    assert not (await g.aget_state(cfg)).tasks
    body = json.loads(final["messages"][-1].content)
    assert body == {"status": "refused", "reason": "Move it into a project."}


async def test_other_calls_in_the_step_are_refused(services) -> None:
    g, cfg = graph(), config()
    await g.ainvoke(call({"id": "x1", "name": "create_project", "args": {}}), cfg)
    await g.ainvoke(Command(resume={"confirmed": True}), cfg)
    final = await g.ainvoke(Command(resume={"status": "completed", "reply": ""}), cfg)
    refusals = [
        m for m in final["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "x1"
    ]
    assert len(refusals) == 1 and refusals[0].status == "error"


async def test_job_path_refuses(services) -> None:
    g, cfg = graph(), config()
    cfg["configurable"]["handoff_enabled"] = False
    final = await g.ainvoke(call(), cfg)
    assert json.loads(final["messages"][-1].content)["status"] == "refused"
```

**Step 2:** Run `backend/tests/unit/services/agent/test_handoff_nodes.py`. Expected: FAIL with `ModuleNotFoundError: src.services.agent.handoff`.

**Step 3: Implementation** (`backend/src/services/agent/handoff.py`):

```python
"""Graph side of machine handoff: approval, child creation, durable pause.

Two nodes, one interrupt each, so LangGraph's call-order matching of
sequential interrupts can never hand one interrupt's resume value to the
other on replay:

- ``handoff_approval_node`` asks for approval with a code-proposed computer
  and folder (or refuses before asking: no project, no paired computer, not
  from the SSE path). The decision is stored in ``handoff_decision``.
- ``handoff_node`` creates the child Codex run (idempotent by tool_call_id),
  then interrupts with ``{"handoff_run_id"}``. The stream producer parks the
  run as ``delegated``; the resume drain answers with the outcome, which
  becomes the run_on_machine ToolMessage.

Other calls in the same model step get a refusal: resuming the step would
re-run them.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from uuid import UUID

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from src.services.agent._nodes_tools import hitl_log_raised, record_hitl_decision
from src.services.agent.state import AgentState
from src.shared.enums import JobStatus

# Kept here (not in services/harness/handoff.py) so the graph builders can
# import the name without pulling the harness services in at import time.
HANDOFF_TOOL = "run_on_machine"

REFUSED_CLIENT = "Handing work to your computer is not available from this client."
ALONE = (
    "run_on_machine must be the only tool call in its step. Call the other "
    "tools after the handoff finishes."
)


def handoff_call(state: AgentState) -> dict | None:
    last = state["messages"][-1] if state.get("messages") else None
    if not isinstance(last, AIMessage):
        return None
    return next(
        (tc for tc in last.tool_calls or () if tc.get("name") == HANDOFF_TOOL), None
    )


def delegated_child(confirmation: dict[str, Any] | None) -> str | None:
    """The child run id when a pending interrupt is a handoff pause."""
    value = (confirmation or {}).get("handoff_run_id")
    return value if isinstance(value, str) and value else None


def _tool_message(call: dict, body: dict[str, Any], *, error: bool = False) -> ToolMessage:
    return ToolMessage(
        content=json.dumps(body, ensure_ascii=False),
        tool_call_id=str(call.get("id") or ""),
        name=HANDOFF_TOOL,
        status="error" if error else "success",
    )


def _refuse_others(state: AgentState, call: dict) -> list[ToolMessage]:
    last = state["messages"][-1]
    return [
        ToolMessage(
            content=json.dumps({"error_type": "handoff_alone", "error": ALONE}),
            tool_call_id=str(tc.get("id") or ""),
            status="error",
        )
        for tc in last.tool_calls
        if tc.get("id") != call.get("id")
    ]


# Service seams (patched in unit tests). Each opens its own short session.
async def _actor_and_thread(config: RunnableConfig) -> tuple[Any, Any]:
    from src.services.agent.tool_session import resolve_tool_user, tool_session
    from src.services.threads.workspace_access import get_thread_lean

    configurable = config.get("configurable") or {}
    async with tool_session() as db:
        user = await resolve_tool_user(
            db, configurable.get("user_id", ""), configurable.get("organization_id")
        )
        thread_id = configurable.get("thread_id")
        thread = (
            await get_thread_lean(db, UUID(str(thread_id)), user.id)
            if user is not None and thread_id
            else None
        )
        return user, thread


async def _propose(user: Any, thread: Any, args: dict) -> Any:
    from src.services.agent.tool_session import tool_session
    from src.services.harness.handoff import propose_target

    async with tool_session() as db:
        return await propose_target(
            db,
            current_user=user,
            thread=thread,
            machine=args.get("machine"),
            folder=args.get("folder"),
        )


async def _existing_child(parent_run_id: str, tool_call_id: str) -> str | None:
    from src.services.agent.tool_session import tool_session
    from src.services.harness.handoff import existing_handoff_run

    async with tool_session() as db:
        return await existing_handoff_run(
            db, parent_run_id=parent_run_id, tool_call_id=tool_call_id
        )


async def _create_child(
    parent_run_id: str, user: Any, call: dict, target: dict[str, str]
) -> str:
    from src.services.agent.tool_session import tool_session
    from src.services.harness.handoff import create_handoff_run

    async with tool_session() as db:
        return await create_handoff_run(
            db,
            parent_run_id=parent_run_id,
            current_user=user,
            tool_call_id=str(call["id"]),
            brief=str((call.get("args") or {}).get("brief") or ""),
            device_id=UUID(target["device_id"]),
            workspace_id=UUID(target["workspace_id"]),
        )


def _parent_run_id(config: RunnableConfig) -> str | None:
    configurable = config.get("configurable") or {}
    run_id = (config.get("metadata") or {}).get("agent_run_id")
    return str(run_id) if configurable.get("handoff_enabled") and run_id else None


def make_handoff_approval_node(branch: str) -> Callable[..., Any]:
    async def handoff_approval_node(state: AgentState, config: RunnableConfig) -> dict:
        call = handoff_call(state)
        if call is None:
            return {}
        others = _refuse_others(state, call)
        loops = state.get("tool_loop_count", 0) + 1

        def refused(reason: str) -> dict:
            return {
                "messages": [
                    *others,
                    _tool_message(call, {"status": "refused", "reason": reason}, error=True),
                ],
                "handoff_decision": {"tool_call_id": call["id"], "outcome": "refused"},
                "tool_loop_count": loops,
            }

        parent = _parent_run_id(config)
        if parent is None:
            return refused(REFUSED_CLIENT)
        # A child for this call means approval already happened: replay the
        # approval interrupt without re-proposing (its value is not used on
        # replay, and a fresh refusal must never strand a running child).
        from src.services.harness.handoff import HandoffRefusal

        existing = await _existing_child(parent, str(call["id"]))
        proposal: Any = None
        if existing is None:
            user, thread = await _actor_and_thread(config)
            proposal = (
                HandoffRefusal(REFUSED_CLIENT)
                if user is None
                else await _propose(user, thread, call.get("args") or {})
            )
            if isinstance(proposal, HandoffRefusal):
                return refused(proposal.reason)
        details = {
            "tools": [{"name": HANDOFF_TOOL, "args": call.get("args") or {}}],
            "handoff_target": proposal.wire() if proposal is not None else None,
            "message": "The agent wants to hand this task to Codex on your computer.",
        }
        hitl_log_raised(config, [call])
        decision = interrupt(details)
        confirmed = bool(decision and decision.get("confirmed"))
        await record_hitl_decision(config, [call], confirmed)
        if not confirmed:
            return {
                "messages": [
                    *others,
                    _tool_message(call, {"status": "denied"}, error=True),
                    AIMessage(
                        content="Action cancelled by user. Let me know if you'd "
                        "like to proceed differently."
                    ),
                ],
                "handoff_decision": {"tool_call_id": call["id"], "outcome": "denied"},
                "user_confirmed": False,
            }
        # Only the target the user saw (or edited) may run. On resume this node
        # replays from the top and ``_propose`` may now return a different
        # folder (a lock, a newer last-used folder), so it is never used here.
        # The confirm path (Task 3.4) always fills ``target``: the user's edit,
        # or the ``handoff_target`` from the verified saved interrupt. It
        # re-validates that exact target and refuses, never substitutes.
        target = (decision or {}).get("target")
        if not target:
            return refused(REFUSED_CLIENT)
        return {
            "handoff_decision": {
                "tool_call_id": call["id"],
                "outcome": "approved",
                "target": target,
            },
            "user_confirmed": True,
        }

    handoff_approval_node.__name__ = (
        "handoff_approval_node" if branch == "main" else f"{branch}_handoff_approval_node"
    )
    return handoff_approval_node


def route_after_handoff_approval(
    run: str, llm: str, reflection: str
) -> Callable[[AgentState], str]:
    def route(state: AgentState) -> str:
        outcome = (state.get("handoff_decision") or {}).get("outcome")
        if outcome == "approved":
            return run
        if outcome == "denied":
            return reflection
        return llm  # refused: the model explains, with the reason in context

    return route


def make_handoff_node(branch: str) -> Callable[..., Any]:
    async def handoff_node(state: AgentState, config: RunnableConfig) -> dict:
        call = handoff_call(state)
        decision = state.get("handoff_decision") or {}
        parent = _parent_run_id(config)
        if call is None or parent is None or decision.get("tool_call_id") != call["id"]:
            return {}
        others = _refuse_others(state, call)
        child = await _existing_child(parent, str(call["id"]))
        if child is None:
            from src.services.agent.agent_run_service import ActiveRunConflict
            from src.services.integrations.context import (
                DeviceBoundToAnotherChat,
                DeviceNotConnectedToProject,
                IntegrationAccessDenied,
            )

            user, _thread = await _actor_and_thread(config)
            if user is None:
                return {"messages": [*others, _tool_message(
                    call, {"status": "refused", "reason": REFUSED_CLIENT}, error=True)]}
            try:
                child = await _create_child(parent, user, call, decision["target"])
            except (DeviceNotConnectedToProject, DeviceBoundToAnotherChat) as exc:
                return {"messages": [*others, _tool_message(
                    call, {"status": "refused", "reason": exc.client_message}, error=True)]}
            except ActiveRunConflict as exc:
                return {"messages": [*others, _tool_message(
                    call, {"status": "refused", "reason": str(exc)}, error=True)]}
            except (IntegrationAccessDenied, ValueError):
                # ValueError: create_chat_context's server-policy refusals
                # (bridge disabled, chat left its project, org-less account).
                return {"messages": [*others, _tool_message(
                    call, {"status": "refused", "reason": REFUSED_CLIENT}, error=True)]}
        outcome = interrupt({"handoff_run_id": child, "tool_call_id": call["id"]})
        failed = (outcome or {}).get("status") != "completed"
        return {
            "messages": [*others, _tool_message(call, dict(outcome or {}), error=failed)],
            "handoff_decision": {},
            "tool_loop_count": state.get("tool_loop_count", 0) + 1,
        }

    handoff_node.__name__ = "handoff_node" if branch == "main" else f"{branch}_handoff_node"
    return handoff_node
```

Before writing this, confirm that `DeviceBoundToAnotherChat.client_message` and `DeviceNotConnectedToProject.client_message` exist; `streaming.py:333-335` uses both.

Also add `park_fields` here. It is the producer's park decision. It lives in this service module, not in `streaming.py`, so that the resume drain (Task 3.10) can reuse it without importing the API layer:

```python
def park_fields(
    confirmation: dict[str, Any], progress_steps: Any, reasoning_summary: str
) -> tuple[JobStatus, dict[str, Any]]:
    """Status and run_metadata for a run parked on its first interrupt.

    A handoff pause parks as DELEGATED with its receipt under
    ``handoff_approval_id``: only the resume drain may answer it, never
    /stream/confirm (which claims ``approval_id``).
    """
    child = delegated_child(confirmation)
    metadata: dict[str, Any] = {
        ("handoff_approval_id" if child else "approval_id"): confirmation["approval_id"],
        "progress_steps": progress_steps,
        "reasoning_summary": reasoning_summary or None,
    }
    if child:
        metadata["handoff_run_id"] = child
    return (JobStatus.DELEGATED if child else JobStatus.AWAITING_CONFIRMATION), metadata
```

**Step 4:** Run the new test file. Expected: PASS. If `MemorySaver` replay shows that `aget_state(...).tasks[0]` is not the node task, read `snapshot.tasks` the way `confirmation_service.pending_approval` does, which takes the first interrupt across all tasks.

**Step 5: Mutation-verify** the two guards named in the docstring. Restore each one.

**Step 6:** Commit:

```bash
git add backend/src/services/agent/handoff.py backend/tests/unit/services/agent/test_handoff_nodes.py
git commit -m "feat(agent): handoff approval and pause nodes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3.6: Wire the nodes into the main graph and the research subgraph

**Files:**
- Modify: `backend/src/services/agent/_builders.py:84-125` (`should_continue`) and `:172-307` (`build_agent_graph`).
- Modify: `backend/src/services/agent/subgraphs/_factory.py:132-146` (signature), `:198-235` (`should_continue`) and `:529-610` (`build`).
- Modify: `backend/src/services/agent/subgraphs/research_agent.py:485-512` (pass `has_handoff=True`).
- Test: `backend/tests/unit/services/test_agent_graph_topology.py` (re-record), plus a routing test in `test_handoff_nodes.py`.

**Step 1: Failing tests.** Add to `test_handoff_nodes.py`:

```python
def test_main_should_continue_routes_handoff_before_the_generic_gate() -> None:
    from src.services.agent._builders import should_continue

    state = {**call(), "tool_loop_count": 0, "error_count": 0}
    assert should_continue(state) == "handoff_approval_node"


def test_research_should_continue_routes_handoff() -> None:
    from src.services.agent.subgraphs.research_agent import research_should_continue

    state = {**call(), "tool_loop_count": 0, "error_count": 0}
    assert research_should_continue(state) == "research_handoff_approval_node"
```

Update the topology literals:
- `RESEARCH_NODES`: add `"research_handoff_approval_node"` and `"research_handoff_node"`.
- `RESEARCH_EDGES`: add
  - `("research_handoff_approval_node", "research_handoff_node", True)`
  - `("research_handoff_approval_node", "research_llm_node", True)`
  - `("research_handoff_approval_node", "research_reflection_gate", True)`
  - `("research_handoff_node", "research_compactor_node", False)`
  - `("research_llm_node", "research_handoff_approval_node", True)`
- Main graph: the same five edges without the prefix (`compactor_node`, `llm_node`, `reflection_gate`). The `xray=1` entries for the embedded research subgraph also change; re-record those with `compiled.get_graph(xray=1)` and say so in the PR (the file's docstring requires it).

Run both test files. Expected: FAIL.

**Step 2: Implementation.** `_builders.py` `should_continue`, inside `if has_pending_tool_calls and state.get("tool_loop_count", 0) < MAX_TOOL_LOOPS:` and before `has_destructive`:

```python
        if any(tc["name"] == HANDOFF_TOOL for tc in last.tool_calls):
            return "handoff_approval_node"
```

In `build_agent_graph`:

```python
    from src.services.agent.handoff import (
        make_handoff_approval_node,
        make_handoff_node,
        route_after_handoff_approval,
    )

    graph.add_node("handoff_approval_node", make_handoff_approval_node("main"))
    graph.add_node("handoff_node", make_handoff_node("main"))
    ...
    # in the llm_node conditional-edge map:
            "handoff_approval_node": "handoff_approval_node",
    ...
    graph.add_conditional_edges(
        "handoff_approval_node",
        route_after_handoff_approval("handoff_node", "llm_node", "reflection_gate"),
        {
            "handoff_node": "handoff_node",
            "llm_node": "llm_node",
            "reflection_gate": "reflection_gate",
        },
    )
    graph.add_edge("handoff_node", "compactor_node")
```

Import `HANDOFF_TOOL` from `src.services.agent.handoff` at the top of `_builders.py`. The same goes for `_factory.py` below. That module imports only `_nodes_tools`, which both builders already import, so this adds no import cycle. The harness services load lazily inside the nodes. Update the `build_agent_graph` docstring flow and `backend/src/services/agent/README.md`'s architecture diagram with one line:

```
                                    ├─► handoff_approval_node ─► handoff_node ─► compactor_node  (machine handoff)
```

In `_factory.py`:
- Add `has_handoff: bool = False` to `make_specialist_subgraph`, and document it in the Args block.
- Add `handoff_approval_name = f"{name}_handoff_approval_node"` and `handoff_name = f"{name}_handoff_node"`.
- In `should_continue`, inside the `< max_tool_loops` branch and before the interrupt check:

```python
                if has_handoff and any(
                    tc["name"] == HANDOFF_TOOL for tc in last.tool_calls
                ):
                    return handoff_approval_name
```

- In `build()`:

```python
        if has_handoff:
            from src.services.agent.handoff import (
                make_handoff_approval_node,
                make_handoff_node,
                route_after_handoff_approval,
            )

            graph.add_node(handoff_approval_name, make_handoff_approval_node(name))
            graph.add_node(handoff_name, make_handoff_node(name))
            llm_routes[handoff_approval_name] = handoff_approval_name
            graph.add_conditional_edges(
                handoff_approval_name,
                route_after_handoff_approval(handoff_name, llm, reflection),
                {handoff_name: handoff_name, llm: llm, reflection: reflection},
            )
            graph.add_edge(handoff_name, compactor_name)
```

Put the `add_node` calls before `graph.add_conditional_edges(llm, should_continue, llm_routes)`, so that the route exists when the map is read. `research_agent.py`: pass `has_handoff=True,` to `make_specialist_subgraph`.

**Step 3:** Run the topology test, `test_handoff_nodes.py`, `backend/tests/services/agent/test_subgraph_loop_ceiling.py` and `backend/tests/unit/services/test_agent_graph_partial.py`. Expected: PASS.

**Step 4:** Commit:

```bash
git add backend/src/services/agent/_builders.py backend/src/services/agent/subgraphs/_factory.py backend/src/services/agent/subgraphs/research_agent.py backend/src/services/agent/README.md backend/tests/unit/services/test_agent_graph_topology.py backend/tests/unit/services/agent/test_handoff_nodes.py
git commit -m "feat(agent): route run_on_machine through the handoff nodes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3.7: The stream producer parks `delegated` and emits `handoff_started`

**Files:**
- Modify: `backend/src/api/agent/streaming.py`:
  - add `"handoff_enabled": True` to the graph `configurable` at `:2568-2580` and `:3997-4004`;
  - change the three park sites `:3046-3115`, `:3383-3432` and `:4429-4503`.
- Test: `backend/tests/unit/api/agent/test_handoff_confirm.py`.

**Step 1: Failing tests** (pure helpers again):

```python
from src.services.agent.handoff import park_fields
from src.shared.enums import AgentStreamEvent, JobStatus


def test_handoff_interrupt_parks_delegated_with_its_own_receipt() -> None:
    status, metadata = park_fields(
        {"handoff_run_id": "c", "approval_id": "a" * 64}, ["step"], ""
    )
    assert status is JobStatus.DELEGATED
    assert metadata["handoff_run_id"] == "c"
    assert metadata["handoff_approval_id"] == "a" * 64
    assert "approval_id" not in metadata  # not claimable by /stream/confirm


def test_approval_interrupt_parks_as_before() -> None:
    status, metadata = park_fields({"approval_id": "b" * 64}, [], "r")
    assert status is JobStatus.AWAITING_CONFIRMATION
    assert metadata == {
        "approval_id": "b" * 64,
        "progress_steps": [],
        "reasoning_summary": "r",
    }


def test_parked_frame_for_a_handoff() -> None:
    event, payload = streaming._parked_frame(
        {"handoff_run_id": "c", "approval_id": "a" * 64}, thread_id="t", run_id="p"
    )
    assert event is AgentStreamEvent.HANDOFF_STARTED
    assert payload == {"thread_id": "t", "run_id": "p", "handoff_run_id": "c"}
```

Run them. Expected: FAIL.

**Step 2: Implementation.** `park_fields` already exists (Task 3.5). Add the frame helper near `_finalize_run`:

```python
def _parked_frame(
    confirmation: Dict[str, Any], *, thread_id: str, run_id: Optional[str]
) -> tuple[AgentStreamEvent, Dict[str, Any]]:
    child = delegated_child(confirmation)
    if child and run_id:
        return AgentStreamEvent.HANDOFF_STARTED, {
            "thread_id": thread_id,
            "run_id": run_id,
            "handoff_run_id": child,
        }
    payload: Dict[str, Any] = {"thread_id": thread_id, "confirmation": confirmation}
    if run_id:
        payload["run_id"] = run_id
    return AgentStreamEvent.CONFIRMATION, payload
```

The second test asserts the existing `reasoning_summary or None` behavior, so pass `"r"` and expect `"r"`. Import `delegated_child` and `park_fields` from `src.services.agent.handoff`.

At each of the three park sites, replace the hand-built `status=JobStatus.AWAITING_CONFIRMATION, run_metadata={...}` with:

```python
                park_status, park_metadata = park_fields(
                    confirmation_details, emitter.progress_steps, reasoning_summary
                )
                ... status=park_status, run_metadata=park_metadata,
```

Replace the `confirmation_payload` construction plus `emitter.emit(AgentStreamEvent.CONFIRMATION, ...)` with:

```python
                frame_event, frame_payload = _parked_frame(
                    confirmation_details,
                    thread_id=thread_id,  # request_body.thread_id at :4429
                    run_id=acceptance.run_id if acceptance is not None else None,
                )
                frame = await emitter.emit(frame_event, frame_payload)
```

At the confirm site (`:4429-4503`), `run_id` is `str(active_run.job_id) if active_run is not None else None`. A handoff without a durable run id never reaches here, because `handoff_node` refuses without `agent_run_id`. Keep the existing `terminal_frame_sent`, `finish()` and `durable_claimed` lines unchanged. The `log` strings "Failed to park ... AWAITING_CONFIRMATION" become "Failed to park ... run".

`TERMINAL_STREAM_EVENTS` now contains `HANDOFF_STARTED`, so `replay_buffered_stream` (`:1679`) treats it as terminal with no further change.

**Step 3:** Run the helper tests, `backend/tests/contract/test_sse_event_vocabulary.py` (its AST walk now sees `frame_event`, a variable rather than a literal; if it reports an offender, extend its allowed-variable list the way it handles existing non-literal emits, and note that in the PR), `backend/tests/unit/api/test_agent_streaming_confirm_disconnect.py` and `backend/tests/agent/test_streaming_resume.py`. Expected: PASS.

**Step 4:** Commit:

```bash
git add backend/src/api/agent/streaming.py backend/tests/unit/api/agent/test_handoff_confirm.py backend/tests/contract/test_sse_event_vocabulary.py
git commit -m "feat(agent): park handoff pauses as delegated and announce them

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3.8: Stop and Cancel on a delegated parent cancel the child

> **Amendment (2026-10-29, PR 1 final review):** since PR 1, `_PRE_STOP_RUN_STATUSES` (`agent_run_service.py` ~53) is derived from the active statuses, so it includes `delegated`. Stop could therefore move a delegated parent to `stopping`, which contradicts deviation 12 (Stop cancels only the child). This task must exclude `delegated` from that set explicitly, or route a delegated parent to the child-cancel path before the generic transition. Add a test that Stop on a delegated parent leaves it `delegated` and requests cancellation of the child.

**Files:**
- Modify: `backend/src/services/agent/agent_submission_service.py`. Add `request_handoff_cancellation` after `request_run_cancellation` (`:538-639`).
- Modify: `backend/src/api/agent/execute.py:1045-1175` (`cancel_stream_confirmation`).
- Test: `backend/tests/unit/services/harness/test_handoff.py`, plus a route test in `backend/tests/unit/api/agent/test_handoff_confirm.py`.

**Step 1: Failing tests.** In `test_handoff.py`:

```python
from src.services.agent.agent_submission_service import request_handoff_cancellation


@pytest.mark.parametrize("child_state", ["queued", "running", "recovering"])
async def test_stop_on_a_delegated_parent_cancels_its_child(
    db: AsyncSession, context: IntegrationContext, child_state: str
) -> None:
    parent = await parent_run(db)
    child_id = await handoff(db, parent)
    await db.execute(
        update(AgentRun).where(AgentRun.job_id == parent).values(
            status="delegated", run_metadata={"handoff_run_id": child_id}
        )
    )
    await db.execute(
        update(AgentRun).where(AgentRun.job_id == child_id).values(status=child_state)
    )
    await db.commit()
    if child_state != "queued":
        # Delivered: mark the session started so cancel is not "undelivered".
        await db.execute(
            update(HarnessSession).where(HarnessSession.run_id == child_id).values(
                observation="running" if child_state == "running" else "unknown",
                provider_session_id="n",
                provider_turn_id="t",
            )
        )
        await db.commit()
    result = await request_handoff_cancellation(
        db, parent_run_id=parent, thread_id=THREAD, organization_id=ORG,
        user_id=USER, reason="user_requested", request_id=None,
    )
    await db.commit()
    assert result is not None and result.run_id == child_id
    child = await db.get(AgentRun, child_id)
    parent_row = await db.get(AgentRun, parent)
    assert child is not None and child.status in {"stopping", "cancelled"}
    assert parent_row is not None and parent_row.status == "delegated"
```

Run it. Expected: FAIL (`ImportError`).

**Step 2: Implementation.**

```python
async def request_handoff_cancellation(
    db: AsyncSession,
    *,
    parent_run_id: str,
    thread_id: Any,
    organization_id: Any,
    user_id: Any,
    reason: str,
    request_id: Optional[str] = None,
) -> Optional[RunCancellationResult]:
    """Stop a delegated parent by cancelling its child Codex run. Does NOT commit.

    The parent stays ``delegated``: it resumes with ``cancelled`` only after
    the child's terminal event is recorded (finalize_submission writes the
    resume record). Unknown execution is never treated as terminal.
    """
    parent = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.job_id == parent_run_id,
                AgentRun.thread_id == _coerce_uuid(thread_id),
                AgentRun.organization_id == _coerce_uuid(organization_id),
                AgentRun.user_id == _coerce_uuid(user_id),
                AgentRun.status == JobStatus.DELEGATED.value,
            )
        )
    ).scalar_one_or_none()
    child = (parent.run_metadata or {}).get("handoff_run_id") if parent else None
    if not isinstance(child, str):
        return None
    return await request_run_cancellation(
        db,
        run_id=child,
        thread_id=thread_id,
        organization_id=organization_id,
        user_id=user_id,
        reason=reason,
        request_id=request_id,
    )
```

Add it to `__all__`. In `cancel_stream_confirmation`, right after the `expected_run_id` mismatch check (`:1067-1068`):

```python
    if active.status == JobStatus.DELEGATED.value:
        try:
            await commit_cancellation(
                db,
                request_handoff_cancellation(
                    db,
                    parent_run_id=active.job_id,
                    thread_id=thread_id,
                    organization_id=current_user.organization_id,
                    user_id=current_user.id,
                    reason="user_requested",
                    request_id=getattr(getattr(request, "state", None), "request_id", None),
                ),
            )
        except Exception as exc:
            logger.exception("Failed to cancel the handoff run")
            raise HTTPException(
                status_code=503,
                detail="Cancellation is temporarily unavailable; please retry",
            ) from exc
        return Response(status_code=204)
```

Add `request_handoff_cancellation` to the import from `agent_submission_service`. The route still owns no transaction; `commit_cancellation` does.

**Step 3:** Run the new tests, `backend/tests/unit/architecture/test_agent_api_boundaries.py`, and the existing cancel route tests (`git grep -ln "stream/cancel" -- backend/tests`). Expected: PASS.

**Step 4: Mutation-verify.** Replace `status == DELEGATED` with `status == RUNNING` in the new service function: the test must fail with `result is None`. Restore it.

**Step 5:** Commit:

```bash
git add backend/src/services/agent/agent_submission_service.py backend/src/api/agent/execute.py backend/tests/unit/services/harness/test_handoff.py backend/tests/unit/api/agent/test_handoff_confirm.py
git commit -m "feat(agent): Stop on a delegated turn cancels its Codex child

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3.9: Reload during a handoff re-delivers `handoff_started`

**Files:**
- Modify: `backend/src/api/agent/execute.py:1255-1268` (`_pending_confirmation_frame`).
- Test: `backend/tests/agent/test_streaming_resume.py`. Extend the Task 2.7 test.

**Step 1: Failing test.** Change the Task 2.7 assertion: the response is a single SSE frame `event: handoff_started` whose data names the parent and child run ids. Run it. Expected: FAIL (204).

**Step 2: Implementation.** Before the `!= AWAITING_CONFIRMATION` early return:

```python
            if (
                active_run is not None
                and getattr(active_run, "status", None) == JobStatus.DELEGATED.value
            ):
                child = (active_run.run_metadata or {}).get("handoff_run_id")
                if not isinstance(child, str):
                    return None
                # A reload while Codex works: reopen the live card. No
                # checkpoint read is needed — the durable row names the child.
                return format_stream_envelope_frame(
                    AgentStreamEvent.HANDOFF_STARTED,
                    {
                        "thread_id": thread_id,
                        "run_id": str(active_run.job_id),
                        "handoff_run_id": child,
                    },
                    seq=after + 1,
                    thread_id=thread_id,
                    route="graph",
                )
```

**Step 3:** Rerun. Expected: PASS.

**Step 4:** Commit:

```bash
git add backend/src/api/agent/execute.py backend/tests/agent/test_streaming_resume.py
git commit -m "feat(agent): reload during a handoff reopens its live card

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3.10: The resume drain

**Files:**
- Create: `backend/src/services/agent/handoff_resume.py`.
- Create: `backend/src/tasks/handoff_tasks.py`.
- Modify: `backend/src/tasks/celery_app.py`. Add to `include` (`:25-43`), add a `task_routes` entry, and add a `beat_schedule` entry.
- Modify: `backend/tests/unit/tasks/test_beat_schedule.py` (`_EXPECTED_KEYS`).
- Test: `backend/tests/unit/services/agent/test_handoff_resume.py` (new).

> **Amendments (2026-10-29, PR #2061 Codex review). These are part of this task.**
>
> 1. **Retries are fenced to the parent's current handoff.** A finished child's `handoff.resume` row stays `dispatched`. Both the re-claim scan and its fence therefore require `parent.run_metadata.handoff_run_id == outbox.run_id` (written into the code below). The producer's park (Task 3.7) must overwrite `handoff_run_id` and `handoff_approval_id` on **every** park, so a second handoff replaces the first. Test: two sequential handoffs on one parent; the first resumes normally. Park the second, finish its child, claim it, then age the parent as if that worker crashed. The re-claim must return the **second** child, never the first. Mutation-verify by dropping the `handoff_run_id` predicate.
> 2. **A child created before its parent parked is still resumed.** `handoff_node` commits the child (Task 2.4), and only then does the stream producer park the parent as `delegated` with `handoff_approval_id` (Task 3.7). If the producer's worker dies in that window, the parent stays `running` with no heartbeat and no `handoff_run_id`. The pending fence (`delegated`) never matches it, and the stale-run sweeper would later fail it. Add a third claim branch: a **pending** row whose parent is `running`, unbeaten for `RECLAIM_AFTER_SECONDS`, and whose `handoff_run_id` is unset or equals this child. Claim it with the stale-`running` fence. In `resume_handoff_parent`, when `claim.approval_id` is empty, read the checkpoint. If the graph is paused on an interrupt whose value names this child (`handoff_run_id`), derive the approval receipt from that snapshot with the same helper the producer uses (`pending_confirmation` → its `approval_id`), store it on the parent, and resume. If not, call `close_with_codex_reply`. Crash-injection test: create the child, skip the producer's park (simulating the crash), finish the child, age the parent. The drain must resume the parent with the outcome. Mutation-verify by removing the branch: the parent stays `running` and nothing is claimed.
> 3. **Each resume runs in its own budgeted task.** Celery's defaults (`task_soft_time_limit=300`, `celery_app.py:220`) are below the graph's 360 s timeout, and the old drain awaited up to 5 resumes in one tick. Now the beat drain only claims, and `resume_handoff` (soft limit 420 s, hard 480 s) runs each resume (code below). Route `src.tasks.handoff_tasks.resume_handoff` to the `agent_runs` queue next to the drain. Test: `drain_handoff_resumes` never awaits a resume (its `dispatch` is called synchronously once per claim), and the task's limits exceed the graph timeout (`RESUME_SOFT_TIME_LIMIT > 360`).
> 4. **A Celery limit or shutdown is not a user Stop.** `CancelledError` closes the turn as cancelled only when the parent is `stopping`. Otherwise it re-raises and leaves the parent for re-claim (code below). Tests: (a) cancel the resume while the parent is `stopping` → `cancelled`. (b) Cancel it while the parent is `running` → it stays `running`, and after aging a drain re-claims it with `attempt == 2`.

**Step 1: Failing tests**

```python
"""Resume drain: claim once, retry after a lost worker, outcome, graph resume.

Mutation checks: dropping the parent ``status == delegated`` predicate in
claim_handoff_resume fails test_two_drains_claim_once; dropping the
``updated_at < stale_before`` fence fails it too (a live dispatched row would
be re-claimed); dropping the attempts check fails
test_exhausted_attempts_keep_codex_reply.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.services.agent import handoff_resume
from src.services.agent.agent_submission_service import (
    HANDOFF_RESUME_KIND,
    finalize_submission,
)
from src.services.agent.run_event_types import RunEventType
from src.shared.enums import JobStatus
from tests.unit.services.harness.test_handoff import handoff, parent_run  # noqa: F401
from tests.unit.services.harness.test_runs import ORG, context, db  # noqa: F401

pytestmark = pytest.mark.unit


async def finished_child(db: AsyncSession, status: JobStatus = JobStatus.COMPLETED) -> tuple[str, str]:
    parent = await parent_run(db)
    child = await handoff(db, parent)
    await db.execute(
        update(AgentRun).where(AgentRun.job_id == parent).values(
            status="delegated",
            run_metadata={"handoff_run_id": child, "handoff_approval_id": "a" * 64},
        )
    )
    await db.commit()
    await finalize_submission(
        db, run_id=child, status=status, organization_id=ORG,
        event_type=RunEventType.RUN_COMPLETED if status is JobStatus.COMPLETED
        else RunEventType.RUN_FAILED,
        payload={} if status is JobStatus.COMPLETED
        else {"code": "harness_failed", "message": "usage limit reached"},
        error="usage limit reached" if status is JobStatus.FAILED else None,
        run_metadata={"handoff_reply": "All tests pass."},
        provider="codex",
    )
    return parent, child


async def test_two_drains_claim_once(db: AsyncSession, context: Any) -> None:
    parent, child = await finished_child(db)
    first = await handoff_resume.claim_handoff_resume(db)
    second = await handoff_resume.claim_handoff_resume(db)
    assert first is not None and (first.parent_run_id, first.child_run_id) == (parent, child)
    assert second is None
    row = await db.get(AgentRun, parent)
    assert row is not None and row.status == "running"


async def test_not_yet_parked_parent_waits(db: AsyncSession, context: Any) -> None:
    parent = await parent_run(db)  # still running: the producer has not parked
    child = await handoff(db, parent)
    await finalize_submission(
        db, run_id=child, status=JobStatus.COMPLETED, organization_id=ORG,
        event_type=RunEventType.RUN_COMPLETED, payload={}, provider="codex",
    )
    assert await handoff_resume.claim_handoff_resume(db) is None
    pending = await db.scalar(
        select(AgentOutbox.status).where(
            AgentOutbox.run_id == child, AgentOutbox.kind == HANDOFF_RESUME_KIND
        )
    )
    assert pending == "pending"


async def test_outcome_for_a_failed_child(db: AsyncSession, context: Any) -> None:
    _parent, child = await finished_child(db, JobStatus.FAILED)
    outcome = await handoff_resume.read_handoff_outcome(db, child)
    assert outcome["status"] == "failed"
    assert outcome["reply"] == "All tests pass."
    assert "usage limit" in outcome["error"]


async def test_drain_resumes_and_completes(db: AsyncSession, context: Any, monkeypatch) -> None:
    parent, _child = await finished_child(db)
    dispatched: list[tuple[Any, dict[str, Any]]] = []
    assert await handoff_resume.drain_handoff_resumes(
        db, dispatch=lambda c, o: dispatched.append((c, o))
    ) == 1
    ((claim, outcome),) = dispatched
    assert claim.parent_run_id == parent and outcome["status"] == "completed"
    assert claim.attempt == 1
    resumed = AsyncMock()
    monkeypatch.setattr(handoff_resume, "resume_handoff_parent", resumed)
    await handoff_resume.run_claimed_resume(claim, outcome)
    resumed.assert_awaited_once_with(claim, outcome)


async def _age_parent(db: AsyncSession, parent: str, seconds: int) -> None:
    """Simulate a resume worker that died: no heartbeat for ``seconds``."""
    await db.execute(
        update(AgentRun).where(AgentRun.job_id == parent).values(
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=seconds)
        )
    )
    await db.commit()


async def test_stale_dispatched_parent_is_reclaimed(db: AsyncSession, context: Any) -> None:
    parent, child = await finished_child(db)
    first = await handoff_resume.claim_handoff_resume(db)
    assert first is not None and first.attempt == 1
    # The worker that claimed it crashed: the parent stays running, unbeaten.
    await _age_parent(db, parent, handoff_resume.RECLAIM_AFTER_SECONDS + 60)
    retry = await handoff_resume.claim_handoff_resume(db)
    assert retry is not None and (retry.parent_run_id, retry.child_run_id) == (parent, child)
    assert retry.attempt == 2
    # The retry re-arms the fence: a third drain right away gets nothing.
    assert await handoff_resume.claim_handoff_resume(db) is None


async def test_finished_parent_is_never_reclaimed(db: AsyncSession, context: Any) -> None:
    parent, _child = await finished_child(db)
    assert await handoff_resume.claim_handoff_resume(db) is not None
    await db.execute(
        update(AgentRun).where(AgentRun.job_id == parent).values(status="completed")
    )
    await db.commit()
    await _age_parent(db, parent, handoff_resume.RECLAIM_AFTER_SECONDS + 60)
    assert await handoff_resume.claim_handoff_resume(db) is None


async def test_exhausted_attempts_keep_codex_reply(
    db: AsyncSession, context: Any, monkeypatch
) -> None:
    parent, _child = await finished_child(db)
    monkeypatch.setattr(handoff_resume, "MAX_RESUME_ATTEMPTS", 1)
    assert await handoff_resume.claim_handoff_resume(db) is not None  # attempt 1 "crashes"
    await _age_parent(db, parent, handoff_resume.RECLAIM_AFTER_SECONDS + 60)
    resumed = AsyncMock()
    gave_up = AsyncMock()
    monkeypatch.setattr(handoff_resume, "resume_handoff_parent", resumed)
    monkeypatch.setattr(handoff_resume, "close_with_codex_reply", gave_up)
    dispatched: list[tuple[Any, dict[str, Any]]] = []
    assert await handoff_resume.drain_handoff_resumes(
        db, dispatch=lambda c, o: dispatched.append((c, o))
    ) == 1
    await handoff_resume.run_claimed_resume(*dispatched[0])
    resumed.assert_not_awaited()
    (claim, outcome), _ = gave_up.await_args
    assert claim.attempt == 2 and outcome["reply"] == "All tests pass."


async def test_codex_reply_survives_a_failed_resume(monkeypatch) -> None:
    from src.services.agent import agent_execution_service

    persisted = AsyncMock(return_value="m1")
    finalized = AsyncMock()
    monkeypatch.setattr(agent_execution_service, "_persist_assistant_message_safe", persisted)
    monkeypatch.setattr(handoff_resume, "finalize_submission", finalized)
    claim = handoff_resume.HandoffResumeClaim(
        parent_run_id="p", child_run_id="c", organization_id=None,
        user_id="u", thread_id="t", approval_id="a" * 64, attempt=3,
    )
    await handoff_resume.close_with_codex_reply(
        claim, {"status": "completed", "reply": "All tests pass."}, model="m"
    )
    content = persisted.await_args.kwargs["content"]
    assert "All tests pass." in content and "could not finish" in content
    assert finalized.await_args.kwargs["status"] is JobStatus.FAILED
    assert finalized.await_args.kwargs["payload"]["assistant_message_id"] == "m1"
```

In `finished_child`, `finalize_submission(..., run_metadata=...)` replaces the child's metadata. That is fine for these tests. The real path merges it (Task 2.5).

Run it. Expected: FAIL (`ModuleNotFoundError`).

**Step 2: Implementation** (`backend/src/services/agent/handoff_resume.py`):

```python
"""Resume a delegated NOUS run once its child Codex run has ended.

``finalize_submission`` writes one ``handoff.resume`` outbox row (keyed by
the child) in the child's terminal transaction. This drain claims each row
exactly once — the guarded ``delegated -> running`` parent update and the
``pending -> dispatched`` outbox update commit together — then resumes the
graph with ``Command(resume={interrupt_id: outcome})``. It never reads the
Redis job record. A row whose parent has not parked yet stays pending for the
next tick; one whose parent is already terminal is retired as failed.

A worker that dies after the claim leaves the parent ``running`` with no
heartbeat. Once ``RECLAIM_AFTER_SECONDS`` pass without one (five missed
``AGENT_RUN_HEARTBEAT_SECONDS`` beats, well under the stale-run sweeper's
``AGENT_RUN_STALE_AFTER_SECONDS``), the next drain re-claims the row. The
guarded ``updated_at < stale_before`` update is the fence, so one drain wins
each attempt. The resume is safe to repeat only while the checkpoint still
pauses on the handoff interrupt (``require_approval`` passes). Otherwise, and
after ``MAX_RESUME_ATTEMPTS``, the turn closes as failed with Codex's reply
saved as the assistant message, so the outcome is never lost.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.services.agent.agent_submission_service import (
    DISPATCH_KIND_STREAM,
    HANDOFF_RESUME_KIND,
    finalize_submission,
)
from src.services.agent.run_event_types import RunEventType
from src.shared.enums import AgentOutboxStatus, JobStatus

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 4000
# Five missed run heartbeats (AGENT_RUN_HEARTBEAT_SECONDS = 60); must stay well
# under AGENT_RUN_STALE_AFTER_SECONDS (1800) so the drain retries before the
# sweeper fails the turn.
RECLAIM_AFTER_SECONDS = 300
MAX_RESUME_ATTEMPTS = 3
_OUTCOME_STATUS = {
    JobStatus.COMPLETED.value: "completed",
    JobStatus.FAILED.value: "failed",
    JobStatus.CANCELLED.value: "cancelled",
}


@dataclass(frozen=True)
class HandoffResumeClaim:
    parent_run_id: str
    child_run_id: str
    organization_id: Any
    user_id: Any
    thread_id: Any
    approval_id: str
    attempt: int


async def claim_handoff_resume(
    db: AsyncSession, *, now: datetime | None = None
) -> HandoffResumeClaim | None:
    """Claim the oldest resumable record. Commits.

    Resumable: a pending row (first attempt), or a dispatched row whose parent
    is still ``running`` but has not heartbeated for RECLAIM_AFTER_SECONDS (its
    worker died). The join keeps dispatched rows of finished parents out of
    the scan, so they never crowd out new ones.
    """
    now = now or datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=RECLAIM_AFTER_SECONDS)
    parent_id_expr = AgentOutbox.payload["parent_run_id"].as_string()
    try:
        rows = (
            await db.execute(
                select(AgentOutbox)
                .join(AgentRun, AgentRun.job_id == parent_id_expr, isouter=True)
                .where(
                    AgentOutbox.kind == HANDOFF_RESUME_KIND,
                    or_(
                        AgentOutbox.status == AgentOutboxStatus.PENDING.value,
                        and_(
                            AgentOutbox.status == AgentOutboxStatus.DISPATCHED.value,
                            AgentRun.status == JobStatus.RUNNING.value,
                            AgentRun.updated_at < stale_before,
                            # Only the parent's *current* handoff: an earlier
                            # child's dispatched row stays dispatched forever
                            # and must never be re-claimed for a later handoff.
                            AgentRun.run_metadata["handoff_run_id"].as_string()
                            == AgentOutbox.run_id,
                        ),
                    ),
                )
                .order_by(AgentOutbox.created_at)
                .limit(20)
                .with_for_update(of=AgentOutbox, skip_locked=True)
            )
        ).scalars().all()
        for outbox in rows:
            payload = dict(outbox.payload or {})
            parent_id = str(payload.get("parent_run_id") or "")
            parent: Any = await db.get(AgentRun, parent_id, populate_existing=True)
            if parent is None or JobStatus(parent.status).is_terminal:
                outbox.status = AgentOutboxStatus.FAILED.value
                continue
            if outbox.status == AgentOutboxStatus.PENDING.value:
                fence = and_(
                    AgentRun.status == JobStatus.DELEGATED.value,
                    AgentRun.run_metadata["handoff_run_id"].as_string() == outbox.run_id,
                )
            else:  # re-claim from a worker that stopped heartbeating
                fence = and_(
                    AgentRun.status == JobStatus.RUNNING.value,
                    AgentRun.updated_at < stale_before,
                    AgentRun.run_metadata["handoff_run_id"].as_string() == outbox.run_id,
                )
            claimed = (
                await db.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.job_id == parent_id,
                        AgentRun.execution_provider == "nous",
                        fence,
                    )
                    # updated_at re-arms the fence for the next drain.
                    .values(status=JobStatus.RUNNING.value, updated_at=now)
                    .returning(AgentRun.job_id)
                    .execution_options(synchronize_session=False)
                )
            ).first()
            if claimed is None:
                continue  # not parked yet, or another drain won: next tick
            attempt = int(payload.get("attempts") or 0) + 1
            payload["attempts"] = attempt
            outbox.payload = payload  # reassign: JSONB is not mutation-tracked
            outbox.status = AgentOutboxStatus.DISPATCHED.value
            outbox.dispatched_at = now
            await db.commit()
            metadata = parent.run_metadata or {}
            return HandoffResumeClaim(
                parent_run_id=parent_id,
                child_run_id=str(outbox.run_id),
                organization_id=parent.organization_id,
                user_id=parent.user_id,
                thread_id=parent.thread_id,
                approval_id=str(metadata.get("handoff_approval_id") or ""),
                attempt=attempt,
            )
        await db.commit()
        return None
    except Exception:
        await db.rollback()
        raise


async def read_handoff_outcome(db: AsyncSession, child_run_id: str) -> dict[str, Any]:
    """The resume value: status, Codex's reply and why it failed (bounded)."""
    child: Any = await db.get(AgentRun, child_run_id, populate_existing=True)
    assert child is not None
    metadata = child.run_metadata or {}
    outcome: dict[str, Any] = {
        "status": _OUTCOME_STATUS.get(child.status, "failed"),
        "reply": str(metadata.get("handoff_reply") or "")[:MAX_REPLY_CHARS],
    }
    if child.status != JobStatus.COMPLETED.value:
        outcome["error"] = (child.error or "")[:1000]
    return outcome


async def drain_handoff_resumes(
    db: AsyncSession,
    *,
    dispatch: Callable[[HandoffResumeClaim, dict[str, Any]], Any],
    limit: int = 5,
) -> int:
    """Claim up to ``limit`` records and hand each to ``dispatch``. Fast.

    The graph resume itself runs in its own Celery task (handoff_tasks), with a
    time budget sized for one resume, never in this beat tick.
    """
    count = 0
    for _ in range(limit):
        claim = await claim_handoff_resume(db)
        if claim is None:
            break
        outcome = await read_handoff_outcome(db, claim.child_run_id)
        await db.commit()
        dispatch(claim, outcome)
        count += 1
    return count


async def run_claimed_resume(claim: HandoffResumeClaim, outcome: dict[str, Any]) -> None:
    """Body of the per-resume task: give up with the reply, or resume."""
    if claim.attempt > MAX_RESUME_ATTEMPTS:
        from src.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            model = await _parent_model(db, claim.parent_run_id)
        await close_with_codex_reply(claim, outcome, model=model)
    else:
        await resume_handoff_parent(claim, outcome)


def _assistant_client_message_id(claim: HandoffResumeClaim) -> str:
    # One id per handoff, so a retried close and a late resume converge on
    # the same assistant row.
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"nous-assistant-handoff:{claim.parent_run_id}:{claim.child_run_id}",
        )
    )


async def close_with_codex_reply(
    claim: HandoffResumeClaim, outcome: dict[str, Any], *, model: str
) -> None:
    """Fail the turn without losing what Codex did.

    Used when the graph cannot be resumed safely: attempts exhausted, or the
    checkpoint already moved past the handoff interrupt.
    """
    from src.core.database import AsyncSessionLocal
    from src.services.agent import agent_execution_service

    reply = outcome.get("reply") or "(Codex sent no reply.)"
    assistant_id = await agent_execution_service._persist_assistant_message_safe(
        thread_id=str(claim.thread_id),
        content=(
            "NOUS could not finish this turn after Codex ended "
            f"({outcome.get('status', 'unknown')}). Codex's reply:\n\n{reply}"
        ),
        model_name=model,
        tool_executions_out=None,
        client_message_id=_assistant_client_message_id(claim),
        required=True,
    )
    async with AsyncSessionLocal() as db:
        await finalize_submission(
            db,
            run_id=claim.parent_run_id,
            status=JobStatus.FAILED,
            organization_id=claim.organization_id,
            event_type=RunEventType.RUN_FAILED,
            payload={
                "code": "handoff_resume_failed",
                "message": "NOUS could not finish this turn.",
                "assistant_message_id": assistant_id,
            },
            error_code="handoff_resume_failed",
            error="NOUS could not finish this turn.",
        )


async def _parent_model(db: AsyncSession, run_id: str) -> str:
    payload = await db.scalar(
        select(AgentOutbox.payload).where(
            AgentOutbox.run_id == run_id, AgentOutbox.kind == DISPATCH_KIND_STREAM
        )
    )
    return str((payload or {}).get("model") or "")


async def resume_handoff_parent(claim: HandoffResumeClaim, outcome: dict[str, Any]) -> None:
    """Resume the parent's graph with the outcome and close the turn.

    Assembled from the same parts as ``_resume_agent_graph`` (receipt check,
    runtime snapshot hydration, cancellation monitor, assistant persistence);
    that function needs a Redis job record and a /confirm claim, which a
    stream-created run never has.
    """
    from langgraph.types import Command

    from src.core.database import AsyncSessionLocal
    from src.services.agent._builders import RECURSION_LIMIT
    from src.services.agent._sanitize import current_turn_final_text
    from src.services.agent.agent_execution_service import (
        _invoke_graph_with_cancellation_monitor,
        _persist_assistant_message_safe,
        _run_heartbeat,
    )
    from src.services.agent.checkpointer import get_checkpointer
    from src.services.agent.confirmation_service import (
        ApprovalExpired,
        pending_confirmation,
        require_approval,
    )
    from src.services.agent.graph import compile_agent_graph
    from src.services.agent.memory import get_memory_store
    from src.services.agent.runtime_snapshot import (
        hydrate_runtime_state_from_snapshot,
        resume_runtime_config_fields,
    )
    from src.services.agent.tool_session import resolve_tool_user
    from src.services.agent.trace_metadata import TraceSource, build_trace_metadata

    thread_id = str(claim.thread_id)

    async def close(status: JobStatus, **kwargs: Any) -> None:
        async with AsyncSessionLocal() as db:
            await finalize_submission(
                db,
                run_id=claim.parent_run_id,
                status=status,
                organization_id=claim.organization_id,
                **kwargs,
            )

    async with AsyncSessionLocal() as db:
        user = await resolve_tool_user(
            db, str(claim.user_id), str(claim.organization_id or "") or None
        )
        model = await _parent_model(db, claim.parent_run_id)
        await db.commit()
    if user is None:
        await close(
            JobStatus.FAILED,
            event_type=RunEventType.RUN_FAILED,
            payload={"code": "handoff_resume_failed", "message": "Account unavailable."},
            error_code="handoff_resume_failed",
            error="Account unavailable.",
        )
        return
    graph = compile_agent_graph(
        checkpointer=await get_checkpointer(), store=await get_memory_store()
    )
    config: dict[str, Any] = {
        "recursion_limit": RECURSION_LIMIT,
        "run_name": "agent:handoff:resume",
        "configurable": {
            "thread_id": thread_id,
            "user_id": str(user.id),
            "organization_id": str(getattr(user, "organization_id", "") or ""),
            "handoff_enabled": True,
        },
        "metadata": build_trace_metadata(
            trace_source=TraceSource.GRAPH,
            user_id=user.id,
            org_id=getattr(user, "organization_id", None),
            thread_id=thread_id,
            request_id=claim.parent_run_id,
            agent_run_id=claim.parent_run_id,
        ),
    }
    try:
        snapshot = await graph.aget_state(config)
        config["configurable"].update(resume_runtime_config_fields(snapshot.values))
        approval = require_approval(
            snapshot,
            approval_id=claim.approval_id,
            thread_id=thread_id,
            run_id=claim.parent_run_id,
            user_id=user.id,
        )
        async with AsyncSessionLocal() as db:
            update_values = await hydrate_runtime_state_from_snapshot(
                db,
                dict(snapshot.values),
                user_id=user.id,
                thread_id=thread_id,
                job_id=claim.parent_run_id,
            )
            await db.commit()
        async with _run_heartbeat(claim.parent_run_id), asyncio.timeout(360):
            final_state = await _invoke_graph_with_cancellation_monitor(
                graph,
                Command(
                    **({"update": update_values} if update_values else {}),
                    resume={approval.interrupt_id: outcome},
                ),
                config,
                claim.parent_run_id,
                user,
            )
        nested = pending_confirmation(
            await graph.aget_state(config),
            thread_id=thread_id,
            run_id=claim.parent_run_id,
            user_id=user.id,
        )
        if nested is not None:
            from src.services.agent.handoff import park_fields

            status, metadata = park_fields(nested, [], "")
            await close(status, run_metadata=metadata)
            return
        content = current_turn_final_text(final_state.get("messages", [])) or ""
        assistant_id = await _persist_assistant_message_safe(
            thread_id=thread_id,
            content=content or "Codex finished.",
            model_name=model,
            tool_executions_out=None,
            client_message_id=_assistant_client_message_id(claim),
            required=True,
        )
        await close(
            JobStatus.COMPLETED,
            event_type=RunEventType.RUN_COMPLETED,
            payload={"assistant_message_id": assistant_id},
        )
    except asyncio.CancelledError:
        # Cancellation has two causes. A user Stop during the resumed step
        # moves the parent to STOPPING (the ordinary request_run_cancellation
        # path), and only that one closes the turn as cancelled. Anything else
        # (a Celery time limit cancelling run_async, a worker shutdown) leaves
        # the parent RUNNING and unbeaten, so the drain re-claims it later
        # (RECLAIM_AFTER_SECONDS) instead of losing the answer.
        async with AsyncSessionLocal() as db:
            status = await db.scalar(
                select(AgentRun.status).where(AgentRun.job_id == claim.parent_run_id)
            )
        if status == JobStatus.STOPPING.value:
            await close(
                JobStatus.CANCELLED,
                event_type=RunEventType.RUN_CANCELLED,
                payload={"reason": "user_requested"},
            )
        raise
    except Exception:
        # ApprovalExpired (the checkpoint moved past the handoff interrupt, so
        # a retry would not be safe), the 360 s timeout, and graph errors all
        # close the turn as failed, keeping Codex's reply as the answer. A
        # worker *crash* never reaches here: the next drain re-claims instead.
        logger.exception(
            "handoff resume failed",
            extra={"run_id": claim.parent_run_id, "attempt": claim.attempt},
        )
        await close_with_codex_reply(claim, outcome, model=model)
```

`ApprovalExpired` is no longer named in the handler; drop it from the import if the linter flags it.

`build_trace_metadata`'s keyword arguments must match its signature; check `trace_metadata.py` and drop any keyword it does not take. A nested park (another approval, or a second handoff) needs no frame. The browser learns about it through the cold resume probe (Task 3.9) when `useHandoffRun` (Task 5.4) sees the parent leave `running`.

`backend/src/tasks/handoff_tasks.py`:

```python
"""Machine handoff resume (beat drain + one task per resume).

The beat drain only claims (fast, well inside the default 300 s soft limit)
and enqueues one ``resume_handoff`` task per claim. Each resume gets its own
budget above the graph's 360 s timeout, so a long healthy resume never hits a
Celery limit, and one slow resume never delays the others.
"""

from dataclasses import asdict
from typing import Any

from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app

# Graph timeout (360 s) + user/graph setup + finalize, under the hard limit.
RESUME_SOFT_TIME_LIMIT = 420
RESUME_TIME_LIMIT = 480


async def _drain() -> int:
    from src.core.database import AsyncSessionLocal
    from src.services.agent.handoff_resume import drain_handoff_resumes

    async with AsyncSessionLocal() as db:
        return await drain_handoff_resumes(
            db, dispatch=lambda claim, outcome: resume_handoff.delay(asdict(claim), outcome)
        )


@celery_app.task(name="src.tasks.handoff_tasks.drain_handoff_resumes")
def drain_handoff_resumes() -> int:
    return run_async(_drain())


@celery_app.task(
    name="src.tasks.handoff_tasks.resume_handoff",
    soft_time_limit=RESUME_SOFT_TIME_LIMIT,
    time_limit=RESUME_TIME_LIMIT,
)
def resume_handoff(claim: dict[str, Any], outcome: dict[str, Any]) -> None:
    from src.services.agent.handoff_resume import HandoffResumeClaim, run_claimed_resume

    run_async(run_claimed_resume(HandoffResumeClaim(**claim), outcome))
```

If enqueueing fails after the claim committed, nothing is lost: the parent stays `running` with no heartbeat, and the next drain re-claims it after `RECLAIM_AFTER_SECONDS`. `HandoffResumeClaim` must stay JSON-serializable (string ids). Convert `organization_id`, `user_id` and `thread_id` to `str` in `claim_handoff_resume`.

`celery_app.py` changes:
- add `"src.tasks.handoff_tasks",` to `include`;
- add the route `"src.tasks.handoff_tasks.drain_handoff_resumes": {"queue": "agent_runs"},`;
- add the route `"src.tasks.handoff_tasks.resume_handoff": {"queue": "agent_runs"},`;
- add the beat entry:

```python
        "drain-handoff-resumes": {  # machine handoff: resume delegated turns
            "task": "src.tasks.handoff_tasks.drain_handoff_resumes",
            "schedule": 2.0,
        },
```

Add `"drain-handoff-resumes",  # celery_app (handoff_tasks)` to `_EXPECTED_KEYS` in `test_beat_schedule.py`.

**Step 3:** Run the new test file and `backend/tests/unit/tasks`. Expected: PASS.

**Step 4: Mutation-verify** the three checks named in the test docstring. Restore each one.

Also confirm against the code that `AgentOutbox.dispatched_at` exists (`models/agent_outbox.py:73`) and that `AgentRun.updated_at` is the column `_run_heartbeat` touches (`agent_run_service.touch_run_updated_at`). On PostgreSQL, run the reclaim test in the Task 6.1 two-drain proof as well: `with_for_update(of=AgentOutbox, skip_locked=True)` over an outer join is not exercised by SQLite.

**Step 5:** Commit:

```bash
git add backend/src/services/agent/handoff_resume.py backend/src/tasks/handoff_tasks.py backend/src/tasks/celery_app.py backend/tests/unit/services/agent/test_handoff_resume.py backend/tests/unit/tasks/test_beat_schedule.py
git commit -m "feat(agent): resume delegated turns when the Codex child ends

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3.11: Graph-level end-to-end with a fake bridge (SQLite) and PR 3 verification

**Files:**
- Test: `backend/tests/unit/services/agent/test_handoff_e2e.py` (new).

**Step 1: Test.** This joins the real pieces:
- the real `handoff_approval_node` and `handoff_node` in a `START → approve → run → END` graph on `MemorySaver`;
- the services, against the `test_runs.py` SQLite database (patch `src.core.database.AsyncSessionLocal` and `agent_execution_service.AsyncSessionLocal` to `async_sessionmaker(db.bind, expire_on_commit=False)`, as `test_delivery.py:421-433` does);
- the fake bridge (`run_child_to` from Task 2.5);
- the drain, with `compile_agent_graph` patched in `handoff_resume` to return the tiny graph.

The flow:

1. A running parent (`parent_run` helper), whose checkpoint holds the AIMessage with the `run_on_machine` call.
2. Invoke with `config()` (from Task 3.5) plus `metadata.agent_run_id = parent`. Assert the approval interrupt proposes `LOCAL`.
3. Resume `{"confirmed": True}`. A child exists, and the interrupt value names it. Call `park_fields(pending_confirmation(...), [], "")` (from `src.services.agent.handoff`) and `finalize_submission(..., status=DELEGATED, run_metadata=...)`, which is what the producer does.
4. `run_child_to(db, context, child, "completed")`.
5. `drain_handoff_resumes(db, dispatch=lambda c, o: claims.append((c, o))) == 1` (with `claims` a list), then `await run_claimed_resume(*claims[0])`; this is what the per-resume Celery task runs. The parent is `completed`. Exactly one assistant `ChatMessage` exists on the thread, with `client_message_id` = `uuid5("nous-assistant-handoff:{parent}:{child}")`. The final ToolMessage carries `"status": "completed"` and `"reply": "answer"`.
6. A second drain with the same `dispatch` returns `0`, so one resume.
7. Pin the `record_observation` assumption from Task 2.6: call `record_observation(db, run_id=UUID(child), observation="unknown")` on a non-terminal child and assert that no resume row is written.

Write the code with the helpers above.

**Step 2:** Run it. Expected: PASS, because the implementation exists; this test proves the pieces join. If it fails, fix the join, not the test.

**Step 3:** Full PR 3 verification:

```bash
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q backend/tests/unit/services/agent backend/tests/unit/agent backend/tests/unit/services/harness backend/tests/unit/api backend/tests/unit/architecture backend/tests/unit/services/test_agent_graph_topology.py backend/tests/unit/services/test_agent_tool_registry.py backend/tests/contract backend/tests/agent/test_streaming_resume.py backend/tests/unit/tasks
<PY> scripts/ci/generate_openapi.py --check
pnpm --dir frontend exec vitest run src/services/__tests__
scripts/ci/run_local_ci.sh --base origin/develop
```

The golden replay job (`backend/tests/eval/test_agent_regression.py`) must stay green. The new tool changes the general tool list the model sees, so run `PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q backend/tests/eval/test_agent_regression.py`, and re-record any recorded tool list it pins, saying so in the PR.

**Step 4:** Commit and open PR 3:

```bash
git add backend/tests/unit/services/agent/test_handoff_e2e.py
git commit -m "test(agent): fake-bridge handoff round trip

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## PR 4: Bridge outcome facts

The field names were confirmed from the Codex app-server schema for **0.161.0**. The bridge pins `SUPPORTED_CODEX_VERSIONS = ["0.153.4", "0.161.0"]` at `packages/harness-bridge/src/adapters/codex.ts:32`, but the package vendors only three request-param schemas (`src/adapters/protocol/`). The schema was generated locally with `codex app-server generate-json-schema --out <scratch>`, using the installed `codex-cli 0.161.0`. Its `v2/ItemCompletedNotification.json` gives `params = {threadId, turnId, completedAtMs, item}` and `ThreadItem`:

- `commandExecution` (required `command, commandActions, cwd, id, status, type`):
  - `command: string`;
  - `exitCode: integer | null` (int32);
  - `aggregatedOutput: string | null`;
  - `durationMs: integer | null`;
  - `status: "inProgress" | "completed" | "failed" | "declined"`.
- `fileChange` (required `changes, id, status, type`):
  - `changes: [{path: string, kind: {type: "add" | "delete" | "update", move_path?}, diff: string}]`;
  - `status` uses the same four values.
- `mcpToolCall`: `server`, `tool`, `arguments`, `status`, `result`, `error`.

0.153.4 was not checked (see Open questions). The code below reads each field only when it has the expected type, so a version without it degrades to today's `{name, status}`.

### Task 4.1: Backend payload fields

**Files:**
- Modify: `backend/src/services/agent/run_event_types.py:144-165` (`ToolCompletedPayload`).
- Test: `backend/tests/unit/services/agent/test_run_event_types.py`. If the file has another name, find it with `git grep -ln ToolCompletedPayload -- backend/tests`.

**Step 1: Failing test**

```python
def test_tool_completed_carries_bounded_redacted_outcome_facts() -> None:
    payload = validate_payload(
        RunEventType.TOOL_COMPLETED,
        {
            "tool_call_id": "i",
            "name": "commandExecution",
            "status": "completed",
            "command": "curl -H 'x: sk-live-abcdef1234567890' https://e.test " + "a" * 900,
            "exit_code": 1,
            "paths": ["src/a.py", "b@example.com.txt"],
        },
    )
    assert len(payload["command"]) == 500
    assert "sk-live-abcdef1234567890" not in payload["command"]
    assert payload["exit_code"] == 1
    assert payload["paths"][0] == "src/a.py"
    assert "b@example.com" not in payload["paths"][1]


def test_tool_completed_rejects_too_many_paths() -> None:
    with pytest.raises(ValidationError):
        validate_payload(
            RunEventType.TOOL_COMPLETED,
            {"tool_call_id": "i", "name": "n", "status": "completed",
             "paths": [f"f{i}" for i in range(51)]},
        )
```

Check that `redact_pii` masks the token shape used here (`_pii_redact.py:64`). If it does not, pick one of its documented token patterns. Run it. Expected: FAIL (extra fields are forbidden).

**Step 2: Implementation.** Add to `ToolCompletedPayload`:

```python
    # Machine handoff outcome facts (bridge, Codex commandExecution/fileChange).
    command: str | None = Field(default=None, max_length=500)
    exit_code: int | None = Field(default=None, ge=-(2**31), le=2**31 - 1)
    paths: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("command", mode="before")
    @classmethod
    def _redact_command(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        # Same discipline as ToolStartedPayload.args: redact, then cap.
        redacted: str = redact_tool_args(redact_pii(value))
        return redacted[:500]

    @field_validator("paths", mode="before")
    @classmethod
    def _redact_paths(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [redact_pii(p)[:500] if isinstance(p, str) else p for p in value]
```

`validate_payload` drops `None` and keeps empty lists. To keep old rows byte-identical, make `paths` `list[str] | None = Field(default=None, max_length=50)` instead, and adjust the validator. Then confirm that `schemas/harness.py:108-124` `ProducerEvent` validates its payload through `PAYLOAD_MODELS[RunEventType.TOOL_COMPLETED]`. If it declares its own model, add the same fields there.

**Step 3:** Run the test, `backend/tests/unit/services/harness/test_delivery.py` (`test_only_bounded_typed_producer_payloads`) and the contract tests. Expected: PASS.

**Step 4:** Commit:

```bash
git add backend/src/services/agent/run_event_types.py backend/tests/unit/services/agent/test_run_event_types.py
git commit -m "feat(harness): bounded command, exit code and paths on tool events

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 4.2: The bridge forwards the facts

**Files:**
- Modify: `packages/harness-bridge/src/contracts.ts:40-47` (the tool `AdapterEvent` variant).
- Modify: `packages/harness-bridge/src/adapters/codex.ts:610-631`.
- Modify: `packages/harness-bridge/src/journal.ts:742-754`.
- Modify: `packages/harness-bridge/src/connection.ts:55-65` (`ProducerBody`).
- Test: `packages/harness-bridge/test/codex.test.ts`; also the fixture string at `:30-67`.

**Step 1: Failing test.** In the fixture's `turn/start` branch, before the `turn/completed` send, add:

```js
   if(c.items) for(const item of c.items) send({method:'item/completed',params:{threadId:'s',turnId:'t',completedAtMs:1,item}});
```

Then:

```ts
test("completed tool items carry command, exit code and changed paths", async (t) => {
  const { server, adapter, options } = setup(t);
  server.configure({
    terminal: "completed",
    items: [
      {
        type: "commandExecution",
        id: "c1",
        command: "pnpm test " + "x".repeat(600),
        commandActions: [],
        cwd: server.root,
        status: "failed",
        exitCode: 1,
      },
      {
        type: "fileChange",
        id: "f1",
        status: "completed",
        changes: [
          { path: join(server.root, "src/a.ts"), kind: { type: "update" }, diff: "" },
          { path: "/etc/outside", kind: { type: "update" }, diff: "" },
          ...Array.from({ length: 60 }, (_, i) => ({
            path: join(server.root, `f${i}`), kind: { type: "add" }, diff: "",
          })),
        ],
      },
    ],
  });
  await adapter.startSession(options);
  const events: any[] = [];
  const ac = new AbortController();
  const consume = (async () => {
    for await (const e of adapter.events(ac.signal)) events.push(e);
  })();
  await adapter.startTurn("s", "x", "c");
  await new Promise((r) => setTimeout(r, 200));
  ac.abort();
  await consume.catch(() => {});
  const command = events.find((e) => e.kind === "tool" && e.itemId === "c1");
  assert.equal(command.status, "failed");
  assert.equal(command.exitCode, 1);
  assert.equal(command.command.length, 500);
  const files = events.find((e) => e.kind === "tool" && e.itemId === "f1");
  assert.equal(files.paths[0], "src/a.ts");
  assert.ok(!files.paths.includes("/etc/outside"));
  assert.equal(files.paths.length, 50);
});
```

Make the `abort`/`consume` pattern match the file's existing tests (`:567-578`). Run `pnpm --filter @nous/harness-bridge exec node --experimental-sqlite --import tsx --test --test-timeout=60000 test/codex.test.ts`. Expected: FAIL.

**Step 2: Implementation.** `contracts.ts`: the tool variant gains:

```ts
      /** commandExecution only: the command text, capped at 500 chars. */
      command?: string;
      /** commandExecution only: the process exit code, when Codex reports it. */
      exitCode?: number;
      /** fileChange only: changed paths relative to the session folder (≤ 50). */
      paths?: string[];
```

`codex.ts`: import `isAbsolute, relative` from `node:path` (check whether `isAbsolute` is already imported; it is used at `:157`). Add a module constant `const MAX_COMMAND_CHARS = 500, MAX_PATHS = 50;`. Inside the item branch, build the facts only for `item/completed`:

```ts
      const facts: { command?: string; exitCode?: number; paths?: string[] } = {};
      if (message.method === "item/completed") {
        if (p.item.type === "commandExecution") {
          if (string(p.item.command))
            facts.command = p.item.command.slice(0, MAX_COMMAND_CHARS);
          if (Number.isSafeInteger(p.item.exitCode)) facts.exitCode = p.item.exitCode;
        } else if (p.item.type === "fileChange" && Array.isArray(p.item.changes)) {
          const root = this.session?.options.cwd;
          const paths: string[] = [];
          for (const change of p.item.changes) {
            if (!root || !record(change) || !string(change.path)) continue;
            const rel = relative(root, change.path);
            // Outside the folder (or unresolvable): never forward an absolute
            // local path — it names the user's home directory.
            if (!rel || rel.startsWith("..") || isAbsolute(rel)) continue;
            if (!paths.includes(rel)) paths.push(rel.slice(0, MAX_COMMAND_CHARS));
            if (paths.length === MAX_PATHS) break;
          }
          if (paths.length) facts.paths = paths;
        }
      }
      await this.queue.push({
        ...position,
        kind: "tool",
        itemId: p.item.id,
        name: string(p.item.tool) ? p.item.tool : p.item.type,
        phase: message.method === "item/started" ? "started" : "completed",
        ...(status ? { status } : {}),
        ...facts,
      });
```

A relative `change.path` resolves against `process.cwd()` in `relative`. If Codex ever sends relative paths, use `relative(root, resolve(root, change.path))`, and import `resolve` as well.

`journal.ts`, in the `tool.completed` payload:

```ts
                ...(event.command ? { command: event.command } : {}),
                ...(event.exitCode !== undefined ? { exit_code: event.exitCode } : {}),
                ...(event.paths?.length ? { paths: event.paths } : {}),
```

`connection.ts` `ProducerBody` `tool.completed` payload gains `command?: string; exit_code?: number; paths?: string[];`.

**Step 3:** Run the test, then `pnpm --filter @nous/harness-bridge test` and `pnpm --filter @nous/harness-bridge type-check`. Expected: PASS. The journal's 16 KiB cap is safe: 500 chars of command plus 50 × 500 chars of paths stays under 26 KiB only in the pathological case where every path is 500 characters long. Lower the per-path cap to 200 in both the bridge and the backend if the journal test `journal.append` throws in a 50 × 200 worst-case test. Add that test to `test/journal.test.ts`, or to whichever file asserts the 16 KiB throw.

**Step 4:** Commit:

```bash
git add packages/harness-bridge/src/contracts.ts packages/harness-bridge/src/adapters/codex.ts packages/harness-bridge/src/journal.ts packages/harness-bridge/src/connection.ts packages/harness-bridge/test/codex.test.ts
git commit -m "feat(harness-bridge): forward command, exit code and changed paths

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 4.3: Outcome facts in the resume value

**Files:**
- Modify: `backend/src/services/agent/handoff_resume.py` (`read_handoff_outcome`).
- Test: `backend/tests/unit/services/agent/test_handoff_resume.py`.

**Step 1: Failing test**

```python
from src.services.agent.run_event_store import append_event


async def test_outcome_lists_commands_and_files_with_caps(db: AsyncSession, context: Any) -> None:
    parent = await parent_run(db)
    child = await handoff(db, parent)
    for i in range(55):
        await append_event(
            db, run_id=child, event_type=RunEventType.TOOL_COMPLETED,
            payload={"tool_call_id": f"c{i}", "name": "commandExecution",
                     "status": "completed", "command": f"step {i}", "exit_code": 0},
            organization_id=ORG,
        )
    await append_event(
        db, run_id=child, event_type=RunEventType.TOOL_COMPLETED,
        payload={"tool_call_id": "f", "name": "fileChange", "status": "completed",
                 "paths": ["a.py", "b.py", "a.py"]},
        organization_id=ORG,
    )
    await db.commit()
    outcome = await handoff_resume.read_handoff_outcome(db, child)
    assert len(outcome["commands"]) == 50
    assert outcome["commands"][0] == {"command": "step 0", "exit_code": 0, "status": "completed"}
    assert outcome["files"] == ["a.py", "b.py"]
```

Run it. Expected: FAIL (`KeyError: 'commands'`).

**Step 2: Implementation.** At the end of `read_handoff_outcome`, before `return`:

```python
    from src.services.agent.run_event_store import read_events

    commands: list[dict[str, Any]] = []
    files: list[str] = []
    cursor = 0
    while len(commands) < MAX_COMMANDS or len(files) < MAX_FILES:
        rows = await read_events(
            db, child_run_id, organization_id=child.organization_id,
            user_id=child.user_id, after_seq=cursor, limit=500,
        )
        if not rows:
            break
        for row in rows:
            payload = row.payload or {}
            if row.event_type != RunEventType.TOOL_COMPLETED.value:
                continue
            if payload.get("command") and len(commands) < MAX_COMMANDS:
                commands.append(
                    {
                        "command": payload["command"],
                        "exit_code": payload.get("exit_code"),
                        "status": payload.get("status"),
                    }
                )
            for path in payload.get("paths") or ():
                if path not in files and len(files) < MAX_FILES:
                    files.append(path)
        cursor = int(rows[-1].seq)
    outcome["commands"] = commands
    outcome["files"] = files
```

Add the constants `MAX_COMMANDS = MAX_FILES = 50`. `read_events`' signature is the one `delivery.project_terminal` uses (`delivery.py:570-572`).

**Step 3:** Rerun the resume tests and the fake-bridge e2e test. Expected: PASS.

**Step 4:** Commit:

```bash
git add backend/src/services/agent/handoff_resume.py backend/tests/unit/services/agent/test_handoff_resume.py
git commit -m "feat(agent): give the resumed graph Codex's commands and files

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 4.4: Document the outcome fields and verify PR 4

**Files:**
- Modify: `docs/engineering/harness-bridge.md`. Add a short "Machine handoff" subsection under "User workflow". It covers the child run, the outcome fields and their caps, path relativization, and the 0.153.4 caveat.
- Modify: `packages/harness-bridge/src/adapters/protocol/README.md`. Record that the `ThreadItem` fields were read from a local 0.161.0 `generate-json-schema` on 2026-10-28, and that the schema is not vendored.

Run `make docs-lint` and `git diff --check -- docs/`, then the PR 3 verification block plus the bridge suite. Commit:

```bash
git add docs/engineering/harness-bridge.md packages/harness-bridge/src/adapters/protocol/README.md
git commit -m "docs(harness): machine handoff outcome facts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## PR 5: Frontend

### Task 5.1: Run-id stream endpoint for child runs

**Files:**
- Modify: `backend/src/services/agent/agent_run_service.py`. Add `handoff_stream_context`.
- Modify: `backend/src/api/agent/execute.py`. Add the route after `resume_stream` (`:1587`).
- Modify (generated): `backend/openapi.json`, `frontend/src/types/generated/api.d.ts`.
- Test: `backend/tests/unit/api/agent/test_handoff_run_stream.py` (new).

**Step 1: Failing tests.** Use the FastAPI test-client pattern of `backend/tests/unit/api/agent/test_harness_streaming.py`:
- a foreign user's child id → 404;
- a top-level run id → 404;
- the owner's child → `200 text/event-stream`, with `stream_harness_run` called with the child's id and `after_seq` from `?after=`.

Run it. Expected: FAIL (404 for every case, because there is no route).

**Step 2: Implementation.** Service (it owns its session, so the router opens none):

```python
async def handoff_stream_context(run_id: str, *, current_user: Any) -> Any:
    """Stream authority for one caller-owned handoff child, else None."""
    from src.core.database import AsyncSessionLocal
    from src.services.harness.chat_context import context_for_accepted_run  # see note

    async with AsyncSessionLocal() as db:
        child = await get_run(
            db, run_id, organization_id=current_user.organization_id,
            user_id=current_user.id,
        )
        if child is None or child.parent_run_id is None:
            return None
        try:
            return await context_for_accepted_run(
                db, run_id=UUID(run_id), current_user=current_user
            )
        except PermissionError:
            return None
```

`context_for_accepted_run` lives in `api/agent/harness_streaming.py:176-203`. Move it to `services/harness/chat_context.py` in this task, with a re-export as in Task 2.1. Check `get_run`'s argument names at `agent_run_service.py:670`.

Route:

```python
@router.get(
    "/runs/{run_id}/stream",
    response_class=StreamingResponse,
    responses={**_SSE_RESPONSE, 404: {"model": HTTPErrorResponse}},
)
async def stream_handoff_run(
    request: Request,
    run_id: str = Path(pattern=r"^[0-9a-fA-F-]{36}$"),
    after: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """Saved events of one machine-handoff child run (the live card)."""
    await _enforce_rate_limit(
        _agent_read_rate_limiter, str(current_user.id), _AGENT_READ_PREFIX
    )
    context = await handoff_stream_context(run_id, current_user=current_user)
    if context is None:
        raise HTTPException(status_code=404, detail="Run not found")
    from src.api.agent.harness_streaming import stream_harness_run

    return StreamingResponse(
        stream_harness_run(request, _uuid.UUID(run_id), context, after_seq=after),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
```

**Step 3:** Regenerate OpenAPI and the frontend types. Run the new test and `backend/tests/unit/architecture/test_agent_api_boundaries.py`. Expected: PASS.

**Step 4:** Commit:

```bash
git add backend/src/services/agent/agent_run_service.py backend/src/services/harness/chat_context.py backend/src/api/agent/harness_streaming.py backend/src/api/agent/execute.py backend/openapi.json frontend/src/types/generated/api.d.ts backend/tests/unit/api/agent/test_handoff_run_stream.py
git commit -m "feat(agent): run-id stream for handoff child runs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 5.2: Service methods

**Files:**
- Modify: `frontend/src/services/agentChatService.ts`:
  - `streamConfirm` at `:959-1000` gains an optional `handoff_target` in the body;
  - add `streamHandoffRun`;
  - add `getRunStatus`.
- Test: `frontend/src/services/__tests__/agentChatService.handoff.test.ts` (new).

**Step 1: Failing tests** (vi.mock `fetch` as the neighbouring service tests do):

```ts
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { agentChatService } from '@/services/agentChatService';

describe('handoff service calls', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('streamConfirm sends the edited target', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('event: done\ndata: {}\n\n', { status: 200 })
    );
    await agentChatService.streamConfirm(
      {
        thread_id: '00000000-0000-0000-0000-000000000001',
        confirmed: true,
        approval_id: 'a'.repeat(64),
        handoff_target: { device_id: 'd', workspace_id: 'w' },
      },
      {}
    );
    const body = JSON.parse(String(fetchSpy.mock.calls[0][1]?.body));
    expect(body.handoff_target).toEqual({ device_id: 'd', workspace_id: 'w' });
  });

  it('streamHandoffRun reads the child stream by run id', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('id: 3\nevent: done\ndata: {"status":"completed"}\n\n', { status: 200 })
    );
    const onDone = vi.fn();
    await agentChatService.streamHandoffRun('c'.padEnd(36, '0'), 2, { onDone });
    expect(String(fetchSpy.mock.calls[0][0])).toContain('runs/c');
    expect(String(fetchSpy.mock.calls[0][0])).toContain('after=2');
    expect(onDone).toHaveBeenCalled();
  });
});
```

Copy the auth/fetch wrapper setup from the existing `agentChatService` tests if `fetch` is wrapped. Run `pnpm --dir frontend exec vitest run src/services/__tests__/agentChatService.handoff.test.ts`. Expected: FAIL.

**Step 2: Implementation.** In `streamConfirm`, add `handoff_target` to the request type. Use the regenerated `StreamConfirmRequest`, which now has the field, and pass it through in `JSON.stringify`. Add:

```ts
  /** Saved events of a machine-handoff child run (live card). Same SSE
   * vocabulary and consume loop as every other agent stream. */
  async streamHandoffRun(
    runId: string,
    afterSeq: number,
    callbacks: AgentStreamCallbacks,
    signal?: AbortSignal
  ): Promise<void> {
    const response = await authorizedFetch(
      agentStreamUrl(`runs/${encodeURIComponent(runId)}/stream?after=${afterSeq}`),
      { method: 'GET', headers: { Accept: 'text/event-stream' }, signal }
    );
    if (!response.ok) {
      callbacks.onError?.('This Codex run is not available.', 'invalid_request');
      return;
    }
    await consumeSse(response, callbacks);
  },

  /** Durable run status (GET /agent/jobs/{id}); stream runs have no Redis
   * record, so this reads agent_runs. */
  async getRunStatus(runId: string): Promise<string> {
    const response = await authorizedFetch(
      agentStreamUrl(`jobs/${encodeURIComponent(runId)}`).replace('/stream/', '/'),
      { method: 'GET' }
    );
    if (!response.ok) throw new Error(`run status ${response.status}`);
    const body = (await response.json()) as { status: string };
    return body.status;
  },
```

`authorizedFetch` and `agentStreamUrl` stand for the helpers `resumeStream` (`:860-953`) actually uses for its GET. Use those exact helpers, and build the jobs URL the same way the existing jobs poll in this file does (search `jobs/`). Do not hand-roll a URL. `consumeSse`'s real parameters must match its definition near `:300`.

**Step 3:** Rerun. Expected: PASS. Then `pnpm --dir frontend type-check`.

**Step 4:** Commit:

```bash
git add frontend/src/services/agentChatService.ts frontend/src/services/__tests__/agentChatService.handoff.test.ts
git commit -m "feat(chat): handoff confirm target and child stream calls

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 5.3: Editable approval card

**Files:**
- Create: `frontend/src/components/chat/aui/handoffTargetDraft.ts`.
- Create: `frontend/src/components/chat/aui/HandoffApprovalCard.tsx`.
- Modify: `frontend/src/components/chat/aui/HitlApprovalToolUI.tsx:19-28` (props) and `:59-100` (delegate when the tool is `run_on_machine`).
- Modify: `packages/chat-runtime/message.ts:193-230` (`extractConfirmationPreview` returns `handoffTarget`), and the `approvalParts` builder in the same file (search `HITL_APPROVAL_TOOL`), so that `args.handoffTarget` reaches the renderer.
- Modify: `frontend/src/hooks/chat/useChatStreaming.ts`:
  - `:3666-3678` copies `preview.handoffTarget` into `pendingApproval`;
  - `:3119-3126` sends `handoff_target: takeHandoffTarget(approvalId)`.
- Test: `frontend/src/components/chat/aui/__tests__/HandoffApprovalCard.test.tsx` (new).

**Step 1: Failing test**

```tsx
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { HandoffApprovalCard } from '../HandoffApprovalCard';
import { takeHandoffTarget } from '../handoffTargetDraft';

const listDevices = vi.fn();
const listFolders = vi.fn();
vi.mock('@/services/harnessService', () => ({
  harnessService: {
    listDevices: (...a: unknown[]) => listDevices(...a),
    listFolders: (...a: unknown[]) => listFolders(...a),
  },
}));

function renderCard(respond = vi.fn()) {
  listDevices.mockResolvedValue([
    { id: 'd1', label: 'laptop', account_wide: false, project_ids: ['p'], bound_thread_ids: [] },
    { id: 'd2', label: 'desktop', account_wide: false, project_ids: ['p'], bound_thread_ids: [] },
  ]);
  listFolders.mockResolvedValue([{ workspace_id: 'w2', label: 'repo2', project_id: 'p' }]);
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <HandoffApprovalCard
        approvalId="a1"
        projectId="p"
        brief="Fix the failing tests."
        proposed={{ device_id: 'd1', device_label: 'laptop', workspace_id: 'w1', workspace_label: 'repo' }}
        respondToApproval={respond}
      />
    </QueryClientProvider>
  );
  return respond;
}

describe('HandoffApprovalCard', () => {
  it('shows the brief and the proposed computer and folder', () => {
    renderCard();
    expect(screen.getByText('Fix the failing tests.')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Computer' })).toHaveDisplayValue('laptop');
  });

  it('approving an edited folder records it for the confirm call', async () => {
    const respond = renderCard();
    fireEvent.change(await screen.findByRole('combobox', { name: 'Computer' }), {
      target: { value: 'd2' },
    });
    fireEvent.change(await screen.findByRole('combobox', { name: 'Folder' }), {
      target: { value: 'w2' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(respond).toHaveBeenCalledWith({ approved: true });
    expect(takeHandoffTarget('a1')).toEqual({ device_id: 'd2', workspace_id: 'w2' });
  });

  it('approving unchanged sends no target (server keeps its proposal)', () => {
    const respond = renderCard();
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(respond).toHaveBeenCalledWith({ approved: true });
    expect(takeHandoffTarget('a1')).toBeUndefined();
  });
});
```

The mocked `harnessService` method names and DTO fields must be the real ones from `frontend/src/services/harnessService.ts:25-62`, and from the `DeviceListItemDTO` / folder DTO in `api.d.ts`. Read them first and adjust both the mock and the component. Run `pnpm --dir frontend exec vitest run src/components/chat/aui/__tests__/HandoffApprovalCard.test.tsx`. Expected: FAIL.

**Step 2: Implementation.**

`handoffTargetDraft.ts`:

```ts
/**
 * UI draft of the computer/folder a user picked on a machine-handoff approval
 * card, keyed by approval id. assistant-ui's respondToApproval carries only
 * `{approved}`, so the confirm call (useChatStreaming.handleConfirmation)
 * takes the draft by the same id. Not a server-entity cache: the server
 * re-validates the target before resuming.
 */
export type HandoffTargetDraft = { device_id: string; workspace_id: string };

const drafts = new Map<string, HandoffTargetDraft>();

export function setHandoffTarget(approvalId: string, target: HandoffTargetDraft): void {
  drafts.set(approvalId, target);
}

export function clearHandoffTarget(approvalId: string): void {
  drafts.delete(approvalId);
}

export function takeHandoffTarget(approvalId: string): HandoffTargetDraft | undefined {
  const value = drafts.get(approvalId);
  drafts.delete(approvalId);
  return value;
}
```

`HandoffApprovalCard.tsx`:
- Same `alertdialog` shell, Escape-to-deny, focus on Approve, and double-submit latch as `HitlApprovalRenderer`; copy those behaviors.
- A brief paragraph.
- Two labelled `<select>`s ("Computer", "Folder"), filled from the same React Query keys as `useHarnessConnection` (`[...DEVICES_QUERY_KEY, userId]`, `['harness','workspaces',userId,deviceId]`) through `harnessService`, filtered by `servesProject` / `usableFolder` (export them from `useHarnessConnection.ts:140-147, :382-405` if they are module-private).
- On Approve: if the selection differs from `proposed`, call `setHandoffTarget(approvalId, …)`, otherwise `clearHandoffTarget(approvalId)`; then call `respondToApproval({ approved: true })`.
- On Deny: call `clearHandoffTarget` and then `respondToApproval({ approved: false })`.
- Theme with CSS variables only (`--nous-*`), never hex.

`HitlApprovalToolUI.tsx`: add `handoffTarget?: {...}` and `projectId?: string` to `args`. In `HitlApprovalRenderer`, before the generic markup:

```tsx
  const handoff = requestedTools.length === 1 && requestedTools[0].name === 'run_on_machine';
  if (handoff && args?.handoffTarget) {
    return (
      <HandoffApprovalCard
        approvalId={approval.id}
        projectId={args.projectId ?? ''}
        brief={String(requestedTools[0].args.brief ?? '')}
        proposed={args.handoffTarget}
        respondToApproval={respondToApproval}
      />
    );
  }
```

`projectId` comes from the thread's `source_project_id`, which `useChatStreaming` already passes to `useHarnessConnection(threadId, projectId)` (`:653`). Thread it into `pendingApproval`.

`message.ts` `extractConfirmationPreview`: return `{ tools, handoffTarget }`, where `handoffTarget` is read from `confirmation.handoff_target` when it is an object with four string fields. Carry it into the approval tool part's `args`. `useChatStreaming.ts:3125`: add `handoff_target: takeHandoffTarget(String(pendingConfirmation.confirmation.approval_id ?? '')),` (it is undefined when unchanged).

**Step 3:** Run the new test, `HitlApprovalToolUI.test.tsx`, `useChatStreaming.auiApproval.test.tsx` and the chat-runtime package tests (`pnpm --filter @nous/chat-runtime test`, or that package's script name). Expected: PASS. Then `pnpm --dir frontend type-check`.

**Step 4:** Commit:

```bash
git add frontend/src/components/chat/aui packages/chat-runtime/message.ts frontend/src/hooks/chat/useChatStreaming.ts
git commit -m "feat(chat): editable computer and folder on handoff approval

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 5.4: `useHandoffRun` and the live card

**Files:**
- Create: `frontend/src/hooks/chat/useHandoffRun.ts`.
- Create: `frontend/src/components/chat/HandoffRunCard.tsx`.
- Test: `frontend/src/hooks/chat/__tests__/useHandoffRun.test.tsx`, `frontend/src/components/chat/__tests__/HandoffRunCard.test.tsx`.

**Step 1: Failing tests.** Hook (vi.mock `@/services/agentChatService` and `@/store/chat-store`):

```tsx
it('streams the child, then refreshes messages once the parent leaves delegated', async () => {
  streamHandoffRun.mockImplementation(async (_id, _after, cb) => {
    cb.onToolStart?.('commandExecution', {}, 'c1');
    cb.onDone?.({ status: 'completed' });
  });
  getRunStatus.mockResolvedValueOnce('running').mockResolvedValueOnce('completed');
  const { result } = renderHook(() =>
    useHandoffRun({ threadId: 't', parentRunId: 'p', childRunId: 'c', pollMs: 1 })
  );
  await waitFor(() => expect(refreshMessages).toHaveBeenCalledWith('t'));
  expect(result.current.phase).toBe('finished');
  expect(result.current.steps).toEqual([{ id: 'c1', name: 'commandExecution', done: false }]);
});

it('reports waiting while the child has produced no start', async () => {
  streamHandoffRun.mockImplementation(() => new Promise(() => {}));
  const { result } = renderHook(() =>
    useHandoffRun({ threadId: 't', parentRunId: 'p', childRunId: 'c', waitingAfterMs: 1 })
  );
  await waitFor(() => expect(result.current.phase).toBe('waiting'));
});
```

Card:

```tsx
it('offers Cancel and shows command approvals', async () => {
  render(<HandoffRunCard state={{ phase: 'running', steps: [], approvals: [{ id: 'r1' }] }}
    onCancel={onCancel} machineLabel="laptop" />);
  fireEvent.click(screen.getByRole('button', { name: 'Cancel Codex run' }));
  expect(onCancel).toHaveBeenCalled();
  expect(screen.getByRole('alertdialog')).toBeInTheDocument();
});
```

Run both. Expected: FAIL.

**Step 2: Implementation.**

`useHandoffRun({threadId, parentRunId, childRunId, pollMs = 2000, waitingAfterMs = 10000})`:
- Opens `agentChatService.streamHandoffRun(childRunId, lastSeq, callbacks, abort.signal)`.
- Callbacks:
  - `onStatus` (`phase: 'accepted'`) → `queued`.
  - `onToolStart` / `onToolEnd` → append to or complete `steps`.
  - `onApprovalRequired(requestId)` → push to `approvals`; the card answers them through the existing `harnessService.decideRequest` path that `useHarnessConnection.ts:354-372` uses (re-read the request, then decide). Export a small `decideNativeRequest` from `useHarnessConnection.ts` rather than duplicating it.
  - `onDone` / `onError` → `childEnded`.
- If no `run.started` / tool frame arrives within `waitingAfterMs`, the phase becomes `waiting` ("Waiting for laptop to come online"). Live-only frames carry no explicit offline signal (the agent report on `harness_streaming._frame_for_event` confirms this), so this is a heuristic.
- After `childEnded`, poll `agentChatService.getRunStatus(parentRunId)` every `pollMs` until the status is not `delegated` or `running`. Then call `useChatStore.getState().refreshMessages(threadId)` through the `@/store/chat-store` facade, and set phase `finished`. If the status is `awaiting_confirmation`, also call `agentChatService.resumeStream(threadId, 0, …)`, so that the existing cold path re-delivers the confirmation card.
- On a transport drop, reconnect from `lastSeq` (the `id:` of the last frame, exposed by `consumeSse` the way `resumeStream` tracks it).
- Clean up with `abort()` on unmount.

`HandoffRunCard`:
- A `role="status"` header: "Codex is working on laptop" / "Waiting for laptop to come online" / "Codex finished — NOUS is writing the answer".
- The list of steps (tool names via `toolLabel`).
- A pending command-approval block reusing `HarnessSelector.tsx:289+`'s approval markup. Extract it into a shared `NativeRequestCard` component in this task instead of copying it.
- A "Cancel Codex run" button calling `onCancel`. CSS variables only.

**Step 3:** Rerun both tests. Expected: PASS.

**Step 4:** Commit:

```bash
git add frontend/src/hooks/chat/useHandoffRun.ts frontend/src/hooks/chat/__tests__/useHandoffRun.test.tsx frontend/src/components/chat/HandoffRunCard.tsx frontend/src/components/chat/__tests__/HandoffRunCard.test.tsx frontend/src/components/chat/HarnessSelector.tsx frontend/src/hooks/chat/useHarnessConnection.ts
git commit -m "feat(chat): live card for a Codex handoff run

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(Include the new `NativeRequestCard` file in the `git add` if one was extracted.)

### Task 5.5: Wire the frame, the card and the blocked composer

**Files:**
- Modify: `frontend/src/hooks/chat/useChatStreaming.ts`. Add `onHandoffStarted` to the three stream callback sets at `:1556`, `:2835` and `:3350`, and to the cold resume path at `:2747-2765`. Hold `activeHandoff: {threadId, runId, handoffRunId} | null` in hook state, and clear it on thread switch and when `useHandoffRun` reports `finished`.
- Modify: `frontend/src/components/chat/ChatSurface.tsx`:
  - `:457-460`: notice;
  - `:516-532`: the composer's `disabled`;
  - render `HandoffRunCard` above the composer while `activeHandoff` is set.
- Test: `frontend/src/components/chat/__tests__/ChatSurface.handoff.test.tsx` (new; mirror `ChatSurface.sessionGate.test.tsx`).

**Step 1: Failing test**

```tsx
it('blocks the composer with the notice while a handoff is active', () => {
  renderSurface({ activeHandoff: { threadId: 't', runId: 'p', handoffRunId: 'c' } });
  expect(screen.getByRole('textbox')).toBeDisabled();
  expect(screen.getByRole('status')).toHaveTextContent(
    'Codex is still working: cancel it or wait.'
  );
});

it('Cancel stops the child through the thread cancel route', () => {
  renderSurface({ activeHandoff: { threadId: 't', runId: 'p', handoffRunId: 'c' } });
  fireEvent.click(screen.getByRole('button', { name: 'Cancel Codex run' }));
  expect(cancelActiveRun).toHaveBeenCalledWith('t', 'p');
});
```

`renderSurface` is the test file's local setup, copied from `ChatSurface.sessionGate.test.tsx`, with the `useChatStreaming` mock returning `activeHandoff`. Run it. Expected: FAIL.

**Step 2: Implementation.**
- `onHandoffStarted`:
  - `setActiveHandoff(handoff)`;
  - reconcile the user message as `onDone` does: there is no assistant row yet;
  - call `finishRun` without an assistant id;
  - set the harness connection idle.
- `ChatSurface`:
  - `disabled={!isSessionInteractive || !harnessCanSend || !!activeHandoff}`;
  - render the notice `<p role="status" className="px-4 py-2 text-xs text-(--nous-fg-2)">Codex is still working: cancel it or wait.</p>` (same pattern as `:457-460`) when `activeHandoff` is set;
  - render `<HandoffRunCard>` fed by `useHandoffRun(activeHandoff)`;
  - Cancel calls `agentChatService.cancelActiveRun(threadId, activeHandoff.runId)`, the existing thread cancel route, which Task 3.8 made handoff-aware.

**Step 3:** Rerun the test and `ChatSurface.sessionGate.test.tsx`. Expected: PASS. Then `pnpm --dir frontend type-check`.

**Step 4:** Commit:

```bash
git add frontend/src/hooks/chat/useChatStreaming.ts frontend/src/components/chat/ChatSurface.tsx frontend/src/components/chat/__tests__/ChatSurface.handoff.test.tsx
git commit -m "feat(chat): show the handoff card and block the composer

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 5.6: Architecture and ratchet checks

**Step 1:** Run `pnpm --dir frontend exec vitest run src/test/architecture`. `maintenanceContracts.test.ts` forbids presentation components under `components/chat` from importing generated OpenAPI types directly when a domain adapter exists. If it fails, move the target and DTO types behind a `types/api/handoff-contract.ts` adapter (as `frontend/AGENTS.md` and `CLAUDE.md:80` require), then rerun.

**Step 2:** Run `scripts/ci/run_local_ci.sh --base origin/develop --frontend`. Expected: PASS. If the changed-file ESLint comparator flags new debt, fix it. Never lower `frontend/quality-baseline.json`.

**Step 3:** Commit any adapter move:

```bash
git add frontend/src/types/api/handoff-contract.ts frontend/src/components/chat
git commit -m "refactor(chat): handoff shapes through a domain adapter

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 5.7: Feature map and flow doc

**Files:**
- Modify: `docs/engineering/feature-map.yaml` (a `machine-handoff` entry pointing at the new tests; `scripts/ci/check_feature_map.py` enforces it).
- Create: `docs/engineering/flows/machine-handoff.md`, following `docs/engineering/flows/README.md`'s template.

Run `<PY> scripts/ci/check_feature_map.py`, `make docs-lint` and `git diff --check -- docs/`. Expected: PASS. Then commit:

```bash
git add docs/engineering/feature-map.yaml docs/engineering/flows/machine-handoff.md
git commit -m "docs(agent): machine handoff feature map and flow

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Final verification

### Task 6.1: Full suites, fake-bridge proof, live run

**Step 1: Full suites** (on the PR 5 branch with PRs 1 to 4 merged):

```bash
(cd backend && <PY> ../scripts/ci/check_alembic.py)
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q backend/tests/unit backend/tests/contract backend/tests/agent
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q backend/tests/eval/test_agent_regression.py
ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch ENVIRONMENT=testing \
  PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q \
  backend/tests/integration/test_machine_handoff_migration_postgres.py \
  backend/tests/integration/test_agent_approval_conformance.py \
  backend/tests/integration/test_harness_cancel_delivery_postgres.py
<PY> scripts/ci/generate_openapi.py --check
pnpm --filter @nous/harness-bridge test
pnpm --filter @nous/harness-bridge type-check
pnpm --dir frontend test
pnpm --dir frontend type-check
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

Every PostgreSQL line without the variable is **NOT RUN**.

**Step 2: Fake-bridge end-to-end.** `backend/tests/unit/services/agent/test_handoff_e2e.py` (Task 3.11) is the in-process proof, and it now includes outcome facts. Also add one PostgreSQL variant beside `test_harness_cancel_delivery_postgres.py`, reusing its `pg_factory`. Name it `backend/tests/integration/test_machine_handoff_postgres.py`. It runs two drains concurrently against one finished child (two sessions) and asserts exactly one claim and one assistant row. That is the real `SKIP LOCKED` proof that SQLite cannot give. Mutation-verify it by removing `skip_locked=True` and the status predicate, then restore them.

**Step 3: Owner's live run (manual; the owner does this on their own machine).** Deploy PRs 1 to 5 to dev through the normal GitOps flow, with `HARNESS_BRIDGE_ENABLED=true` on dev. Then:
1. On the owner's computer, run `nous --rc` in a test repository folder, and approve the account connection in the browser.
2. In a NOUS chat that is in a project, ask: "Run the tests in this repo on my computer and fix what fails."
3. Expected:
   - the approval card shows the brief, the computer and the folder;
   - changing the folder and approving starts Codex;
   - the live card shows progress, and a command approval when Codex asks;
   - reloading the page reopens the card;
   - sending a message shows "Codex is still working: cancel it or wait.";
   - when Codex ends, one NOUS answer appears that cites the commands and changed files.
4. Repeat, and press Cancel while Codex runs. Expected: Codex stops, and NOUS answers once with the cancelled outcome.
5. Stop the bridge process and start a handoff. Expected: the card shows "Waiting for the machine". Starting the bridge again continues the run.

Record the result as a dated evidence bundle under `docs/testing/evidence/` as `docs/testing/harness-live-proof.md` describes. Until that bundle exists, the live run stays **NOT RUN**.

---

## Open questions

1. **Codex 0.153.4 item fields.** `ThreadItem` (`command`, `exitCode`, `changes[].path`) was confirmed only for 0.161.0, from a local `codex app-server generate-json-schema`. 0.153.4 is not installed here, and the repo vendors no `ThreadItem` schema. The bridge reads each field defensively. Should 0.153.4 stay supported, or should the pin drop it?
2. **OpenAPI break level.** `JobStatusResponse.status` gains `delegated` (Task 1.7). It is unclear whether the `oasdiff` gate rates a response enum widening as ERR (label needed) or WARN. I did not run `oasdiff`.
3. **Device presence.** No online signal exists, so the default target is the last used folder for the project (Deviation 6). Is that acceptable, or should a heartbeat column be added to `bridge_devices` (a schema change not in the design)?
4. **Worker crash after a resume claim. Resolved (owner, 2026-10-28):** the drain re-claims a `dispatched` row once its parent has missed heartbeats for `RECLAIM_AFTER_SECONDS` (300 s). It allows up to `MAX_RESUME_ATTEMPTS` (3); when those run out, or when the checkpoint has already moved past the handoff, the turn closes as failed with Codex's reply saved as the answer (Task 3.10). Remaining risk: a resume worker that is alive but has stopped heartbeating for 5 minutes could run in parallel with a retry. The heartbeat wraps the graph call, so this needs a hung event loop.
5. **Command text privacy.** The command text goes through `redact_pii` / `redact_tool_args`, which do not strip absolute paths (user names) inside commands. Paths in `paths` are made relative. Is the residual exposure acceptable? It is the user's own tenant-scoped data.
6. **assistant-ui `respondToApproval`.** I did not verify whether it passes extra fields through `onRespondToToolApproval` (`packages/chat-runtime/runtime.ts:146` types only `{approved, approvalId}`). The plan uses a draft map instead (Deviation 15).
7. **`ProducerEvent` payload typing.** I was told that `schemas/harness.py:108-124` uses a typed payload, but I did not confirm that it resolves through `PAYLOAD_MODELS`. Task 4.1 checks this before relying on it.
8. **Model name for the resumed answer.** It is read from the parent's `agent.stream.execute` outbox payload `model` (`agent_submission_service.py:1018-1024`). Confirm that this is the right attribution.
9. **Golden replay and evals.** Adding `run_on_machine` to the general tool list may shift recorded trajectories. Owner decision: should a golden case for the handoff be added (it needs credentials, marker `golden`)?
10. **Research subgraph interrupts.** The two-node pattern inside the compiled research subgraph is expected to surface interrupts the way `research_interrupt_node` does. Task 3.6 tests routing only; Task 3.11's e2e uses the main-graph shape. Confirm with a manual research-intent run in step 3 of the live run.
- **A machine that never comes back (2026-10-29, PR 2 build).** A child whose machine disappears for good stays `recovering`, or `stopping` after Cancel, because unknown execution is never terminal. Its parent then waits in `delegated` indefinitely, and every new message gets the "Codex is still working" notice. Today's top-level Codex runs have the same limit. The owner chose "wait until the user cancels", so PR 3 must make sure Cancel can always end the *parent* turn, for example by resolving the parent with `cancelled` once Cancel is requested even if the child's end is never confirmed, while the child keeps its own unknown status. This is an owner decision; raise it before Task 3.8.

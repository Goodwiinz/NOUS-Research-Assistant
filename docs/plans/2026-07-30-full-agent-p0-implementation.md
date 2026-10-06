# Full Agent P0 Implementation Plan

> **REFERENCE ONLY — DO NOT EXECUTE FROM THIS DOCUMENT.**
>
> The P0/P1/P2 PR plans govern execution:
>
> - `docs/plans/2026-07-30-agent-audit-master-pr-roadmap.md`
> - `docs/plans/2026-07-30-agent-audit-p0-pr-plan.md`
> - `docs/plans/2026-07-30-agent-audit-p1-pr-plan.md`
> - `docs/plans/2026-07-30-agent-audit-p2-pr-plan.md`
>
> Task numbers below are **cited** from here (each PR section in the P0 plan
> names the tasks it covers); tasks are **not executed** from here. Where this
> document and a PR plan disagree on scope, sequencing, or gates, the PR plan
> wins. The authoritative audit is `docs/audits/system/system-design-audit-2026-07-30.md`.
>
> Branch per the repository's normal workflow: a feature branch from
> `origin/develop`, one PR per PR-plan row. No specific worktree path or branch
> name is required, and none is pinned here.

**Goal:** Deliver the complete P0 contract for versioned and recoverable agent
streaming, durable execution, route-specific SLOs, unified privacy-safe
observability, and blocking quality/reliability release gates.

**Architecture:** Make PostgreSQL `agent_runs` and `agent_run_events` the
authoritative execution ledger for both `/agent/stream` and `/agent/execute`.
Commit the user turn, run, initial event, and dispatch outbox record in one
transaction before emitting `accepted`; use Redis only for wakeups/cache and
Celery for execution. Project the durable event ledger into a typed SSE
adapter, enforce one terminal event in PostgreSQL, and finalize assistant
messages/checkpoints through idempotent outbox workers.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy async, Alembic, PostgreSQL,
Redis, Celery, LangGraph PostgreSQL checkpointer, Next.js/TypeScript, WHATWG
SSE, OpenAPI 3.1, RFC 9457 Problem Details, Prometheus, OpenTelemetry GenAI
semantic conventions, LangSmith, Grafana, pytest, Vitest, Playwright, Helm,
ArgoCD.

---

## Starting state and non-negotiable decisions

Start from commit `fd851051` and treat its SSE envelope, metrics, and
20-sample benchmark as **provisional** — they may be changed, including
incompatibly, until P0-A freezes the contract. Do not preserve an incorrect
compatibility contract for its own sake.

`fd851051` **is pushed**: it is part of PR #1312, together with `b4d3bd4` and
`ddd3bf8`. Do not amend, rebase, or otherwise rewrite that history. Change the
baseline forward, in a new commit on a new branch. The known defects in that
baseline are enumerated in
`docs/plans/2026-07-30-agent-audit-p0-pr-plan.md` ("Baseline: what PR #1312
already shipped").

**Command conventions.** Every shell block below is repo-relative and assumes
the repository's backend virtualenv is on `PATH`, exactly like
`scripts/ci/run_local_ci.sh` (which invokes `ruff`, `black`, `isort`, `mypy`,
and `pytest` by bare name):

```sh
source backend/.venv/bin/activate   # or: export PATH="$PWD/backend/.venv/bin:$PATH"
```

Without an activated environment, prefix the tools with the interpreter that
owns them (`python -m pytest`, `python -m black`, `python -m isort`). No
absolute machine-specific path is assumed anywhere in this document; paths are
relative to the repository root unless a block says otherwise.

Reuse these existing foundations:

- `backend/src/models/agent_run.py`
- `backend/src/models/agent_run_event.py`
- `backend/src/schemas/agent_run_events.py`
- `backend/src/services/agent/run_event_types.py`
- `backend/src/services/agent/agent_run_service.py`
- `backend/src/tasks/agent_run_tasks.py`
- `backend/src/services/agent/stream_buffer.py`
- `backend/tests/eval/`

Do not create a second durable-event model. Complete the existing
`agent_run_events` design and make the legacy stream an adapter over it.

P0 definitions:

1. `accepted` means the user message, run row, `run.created` event, and dispatch
   outbox record have committed in PostgreSQL.
2. Redis loss may delay wakeups, but cannot lose replayable events or accepted
   work.
3. The only logical terminal events are `done`, `stopped`, and `error`.
   `confirmation` pauses a run and may close one HTTP connection, but is not a
   logical terminal event.
4. A partial answer caused by disconnect, cancellation, timeout, or worker loss
   terminates as `stopped`; its durable run status is `partial` when text was
   persisted and `stopped` when no assistant text exists.
5. Metrics may label only bounded server-owned dimensions: `route`,
   `model_family`, `prompt_class`, `deployment_revision`, `phase`, and
   `outcome`. Never label metrics with organization, user, thread, prompt,
   document, tool arguments, trace IDs, or exception messages.
6. Organization-specific views come from tenant-scoped PostgreSQL rollups or
   access-controlled trace queries, never Prometheus labels.
7. The release corpus has at least 100 cases and blocks on every zero-tolerance
   safety/integrity rule.

Standards:

- [WHATWG Server-Sent Events](https://html.spec.whatwg.org/dev/server-sent-events.html)
- [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.1.html)
- [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457.html)
- [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/)
- [OpenTelemetry GenAI attributes](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
- [Google SRE service-level objectives](https://sre.google/sre-book/service-level-objectives/)

## P0 acceptance matrix

| Contract            | Blocking acceptance criterion                                          |
| ------------------- | ---------------------------------------------------------------------- |
| Stream schema       | Every event has an OpenAPI 3.1 component schema and contract test      |
| SSE transport       | Valid `id`, `event`, `data`; heartbeat during every silent path        |
| Envelope            | Every data object has the same eight required fields                   |
| Replay              | `Last-Event-ID` replays all and only later durable events              |
| Terminal            | Exactly one of `done`, `stopped`, `error` per logical run              |
| Errors              | HTTP errors and SSE `error.problem` validate as RFC 9457               |
| Accepted durability | Zero accepted turns without a committed user row/run/outbox            |
| Idempotency         | Zero duplicate user/assistant rows or duplicate executions             |
| Queue               | `/execute` and `/stream` never rely on process-local execution         |
| Recovery            | Pod/worker termination at three injection points recovers correctly    |
| Fast SLO            | accepted p95 <=250 ms; TTFT and completion p95 <=5 s                   |
| Success             | fast >=99.5%; graph/research >=99%                                     |
| Recovery            | >=99% successful resume by route                                       |
| Quality             | All zero-tolerance gates pass; aggregate quality regression <=2 points |
| Scale               | At least 100 representative live samples                               |

### Task 1: Freeze the complete event and lifecycle vocabulary

**Files:**

- Modify: `backend/src/shared/enums.py`
- Modify: `backend/src/services/agent/run_event_types.py`
- Modify: `backend/src/schemas/agent_run_events.py`
- Test: `backend/tests/contract/test_sse_event_vocabulary.py`
- Test: `backend/tests/unit/services/agent/test_run_event_types.py`
- Test: `backend/tests/unit/agent/test_job_status_enum.py`

**Step 1: Write failing lifecycle tests**

Require:

```python
assert TERMINAL_STREAM_EVENTS == {
    AgentStreamEvent.DONE,
    AgentStreamEvent.STOPPED,
    AgentStreamEvent.ERROR,
}
assert AgentStreamEvent.CONFIRMATION not in TERMINAL_STREAM_EVENTS
assert {JobStatus.COMPLETED, JobStatus.PARTIAL, JobStatus.STOPPED, JobStatus.FAILED}.issubset(
    TERMINAL_JOB_STATUSES
)
```

Add durable types `run.finalization_requested`, `checkpoint.reconciled`, and
`run.stopped`. Retain `run.cancelled` only as a read-side legacy alias during
one migration window; never write it.

**Step 2: Verify the tests fail**

```bash
cd backend
pytest \
  tests/contract/test_sse_event_vocabulary.py \
  tests/unit/services/agent/test_run_event_types.py \
  tests/unit/agent/test_job_status_enum.py -q
```

Expected: failures for missing `STOPPED`, `PARTIAL`, and the old confirmation
terminal set.

**Step 3: Implement the vocabulary**

Define exactly:

```python
class AgentStreamEvent(StrEnum):
    TOKEN = "token"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    RAG_CONTEXT = "rag_context"
    PLAN = "plan"
    REFLECTION = "reflection"
    TRACE = "trace"
    USAGE = "usage"
    HEARTBEAT = "heartbeat"
    STATUS = "status"
    CONFIRMATION = "confirmation"
    DONE = "done"
    STOPPED = "stopped"
    ERROR = "error"

TERMINAL_STREAM_EVENTS = frozenset({
    AgentStreamEvent.DONE,
    AgentStreamEvent.STOPPED,
    AgentStreamEvent.ERROR,
})
```

Make terminal statuses absorbing in `agent_run_service.py`.

**Step 4: Run the focused tests**

Expected: PASS.

**Step 5: Commit**

```bash
git add backend/src/shared/enums.py \
  backend/src/services/agent/run_event_types.py \
  backend/src/schemas/agent_run_events.py \
  backend/tests/contract/test_sse_event_vocabulary.py \
  backend/tests/unit/services/agent/test_run_event_types.py \
  backend/tests/unit/agent/test_job_status_enum.py
git commit -m "Define complete agent lifecycle contract"
```

### Task 2: Define OpenAPI 3.1 schemas for every SSE event

**Files:**

- Create: `backend/src/schemas/agent_stream.py`
- Create: `backend/src/schemas/problem_details.py`
- Modify: `backend/src/api/agent/execute.py`
- Modify: `scripts/ci/generate_openapi.py`
- Modify: `backend/openapi.json`
- Modify: `frontend/src/types/generated/api.d.ts`
- Create: `backend/tests/contract/test_agent_stream_openapi.py`
- Test: `backend/tests/unit/ci/test_generate_openapi.py`

**Step 1: Write the failing OpenAPI contract test**

Assert OpenAPI remains `3.1.x`, `/api/v1/agent/stream` declares
`text/event-stream`, and components contain one schema for every
`AgentStreamEvent`.

The common model must be:

```python
class AgentEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    sequence: int = Field(gt=0)
    trace_id: str = Field(min_length=1, max_length=128)
    thread_id: UUID
    message_id: UUID | None
    route: Literal["pending", "luna", "graph", "research", "tool", "unknown"]
    phase: Literal[
        "accepted", "routing", "executing", "awaiting_confirmation",
        "finalizing", "terminal", "keepalive"
    ]
    timestamp: datetime
```

Create an explicit Pydantic event-data model for each event. Use discriminated
unions or inheritance, but do not accept arbitrary extra fields. `ErrorEvent`
must contain `problem: ProblemDetails`.

Because OpenAPI has no native multi-event SSE object, describe the HTTP body as
`type: string` under `text/event-stream` and add reusable component schemas plus
an `x-sse-event-schemas` map from each `event:` name to its component `$ref`.

**Step 2: Run the contract test and observe failure**

```bash
cd backend
pytest \
  tests/contract/test_agent_stream_openapi.py -q
```

**Step 3: Implement schemas and endpoint metadata**

Use `openapi_extra` on the FastAPI operation. Do not claim the SSE body is JSON.
Register all component schemas through models reachable by the app or through
the OpenAPI generator hook.

**Step 4: Regenerate and validate generated clients**

```bash
cd ..
python \
  scripts/ci/generate_openapi.py
cd frontend
corepack pnpm@10.18.2 run generate:api-types
corepack pnpm@10.18.2 exec tsc --noEmit
```

**Step 5: Commit**

```bash
git add backend/src/schemas/agent_stream.py \
  backend/src/schemas/problem_details.py backend/src/api/agent/execute.py \
  scripts/ci/generate_openapi.py backend/openapi.json \
  frontend/src/types/generated/api.d.ts \
  backend/tests/contract/test_agent_stream_openapi.py \
  backend/tests/unit/ci/test_generate_openapi.py
git commit -m "Specify agent SSE events in OpenAPI"
```

### Task 3: Implement the authoritative PostgreSQL event store

**Files:**

- Create: `backend/src/services/agent/run_event_store.py`
- Modify: `backend/src/models/agent_run_event.py`
- Modify: `backend/src/models/agent_run.py`
- Create: `backend/alembic/versions/e6f7a8b9c0d1_complete_agent_terminal_contract.py`
- Create: `backend/tests/unit/services/agent/test_run_event_store.py`
- Create: `backend/tests/unit/test_agent_terminal_contract_migration.py`

**Step 1: Write failing store tests**

Cover:

- sequence allocation under a `SELECT ... FOR UPDATE` run-row lock;
- payload validation before insert;
- `list_after(run_id, seq)` ordering;
- tenant-scoped run ownership before replay;
- concurrent terminal appends produce one winner and one no-op/conflict;
- no append is allowed after a terminal event;
- fenced workers with a stale lease generation cannot append.

**Step 2: Add the database invariant**

The migration must:

- migrate `cancelled` to `stopped`;
- extend the run status check with `partial` and `stopped`;
- add a partial unique index:

```sql
CREATE UNIQUE INDEX uq_agent_run_events_one_terminal
ON agent_run_events (run_id)
WHERE event_type IN ('run.completed', 'run.stopped', 'run.failed');
```

**Step 3: Implement the store**

Required API:

```python
async def append_event(
    db: AsyncSession,
    *,
    run_id: str,
    event_type: RunEventType,
    payload: dict[str, Any],
    lease_generation: int | None = None,
) -> AgentRunEvent: ...

async def append_terminal(... ) -> AgentRunEvent: ...
async def list_after(..., after_seq: int, limit: int = 500) -> list[AgentRunEvent]: ...
async def has_terminal(... ) -> bool: ...
```

Every append increments `agent_runs.last_event_seq` in the same transaction.
Never commit inside these primitives; the caller owns the transaction.

**Step 4: Run migration and store tests**

```bash
cd backend
pytest \
  tests/unit/services/agent/test_run_event_store.py \
  tests/unit/test_agent_terminal_contract_migration.py -q
```

**Step 5: Commit**

```bash
git add backend/src/services/agent/run_event_store.py \
  backend/src/models/agent_run_event.py backend/src/models/agent_run.py \
  backend/alembic/versions/e6f7a8b9c0d1_complete_agent_terminal_contract.py \
  backend/tests/unit/services/agent/test_run_event_store.py \
  backend/tests/unit/test_agent_terminal_contract_migration.py
git commit -m "Add authoritative agent event store"
```

### Task 4: Add the transactional outbox

**Files:**

- Create: `backend/src/models/agent_outbox.py`
- Modify: `backend/src/models/__init__.py`
- Create: `backend/src/services/agent/agent_outbox_service.py`
- Create: `backend/alembic/versions/f7a8b9c0d1e2_add_agent_outbox.py`
- Create: `backend/tests/unit/services/agent/test_agent_outbox_service.py`
- Create: `backend/tests/unit/test_agent_outbox_migration.py`

**Step 1: Write failing outbox tests**

Require idempotent enqueue, `FOR UPDATE SKIP LOCKED` claims, retry backoff,
lease expiry, and deduplication.

Model:

```python
class AgentOutbox(Base):
    __tablename__ = "agent_outbox"
    id: UUID
    run_id: str
    topic: Literal["run.dispatch", "assistant.finalize", "checkpoint.reconcile"]
    dedupe_key: str
    payload: JSONB  # IDs and bounded metadata only; never prompt/content/tool args
    status: Literal["pending", "processing", "dispatched", "failed"]
    attempts: int
    available_at: datetime
    locked_by: str | None
    locked_until: datetime | None
    last_error_code: str | None
```

Add a unique constraint on `(topic, dedupe_key)`.

**Step 2: Implement claim/ack/retry**

Do not log payloads. Store exception class/code, not exception strings that may
contain secrets.

**Step 3: Run tests and Alembic validation**

```bash
cd backend
pytest \
  tests/unit/services/agent/test_agent_outbox_service.py \
  tests/unit/test_agent_outbox_migration.py -q
cd ..
python \
  scripts/ci/check_alembic.py
```

**Step 4: Commit**

```bash
git add backend/src/models/agent_outbox.py backend/src/models/__init__.py \
  backend/src/services/agent/agent_outbox_service.py \
  backend/alembic/versions/f7a8b9c0d1e2_add_agent_outbox.py \
  backend/tests/unit/services/agent/test_agent_outbox_service.py \
  backend/tests/unit/test_agent_outbox_migration.py
git commit -m "Add transactional agent outbox"
```

### Task 5: Make submission atomic and accepted durable

**Files:**

- Create: `backend/src/services/agent/agent_submission_service.py`
- Modify: `backend/src/api/agent/streaming.py`
- Modify: `backend/src/api/agent/execute.py`
- Modify: `backend/src/services/agent/agent_execution_service.py`
- Modify: `backend/src/models/chat_message.py`
- Create: `backend/alembic/versions/a8b9c0d1e2f3_scope_agent_idempotency.py`
- Create: `backend/tests/api/agent/test_agent_submission_atomicity.py`
- Test: `backend/tests/unit/agent/test_dispatch_backend.py`

**Step 1: Write failure-injection tests**

Inject failures after each write and prove the transaction leaves no partial
state:

1. after user message insert;
2. after run insert;
3. after `run.created`;
4. after outbox insert;
5. before commit.

Assert no `accepted` frame can be formatted until commit returns.

**Step 2: Enforce the idempotency key**

Require `client_message_id` for new clients and derive:

```text
SHA-256(organization_id || thread_id || client_message_id)
```

The database uniqueness must match the contract:

```sql
UNIQUE (organization_id, thread_id, client_message_id)
```

Use null-safe handling for organization-less users without weakening tenant
scope. Preserve the existing chat-message user/assistant partial unique
indexes.

**Step 3: Implement one transaction**

`submit_agent_turn()` must:

1. verify thread/project ownership;
2. insert or resolve the user message;
3. insert or resolve the `agent_runs` row;
4. append `run.created`;
5. enqueue `run.dispatch`;
6. commit;
7. return the committed run and accepted event.

Remove the current pre-persistence accepted emit.

**Step 4: Run focused tests**

Expected: zero rows and zero events after injected rollback; retries return the
same run and do not enqueue twice.

**Step 5: Commit**

```bash
git add backend/src/services/agent/agent_submission_service.py \
  backend/src/api/agent/streaming.py backend/src/api/agent/execute.py \
  backend/src/services/agent/agent_execution_service.py \
  backend/src/models/chat_message.py \
  backend/alembic/versions/a8b9c0d1e2f3_scope_agent_idempotency.py \
  backend/tests/api/agent/test_agent_submission_atomicity.py \
  backend/tests/unit/agent/test_dispatch_backend.py
git commit -m "Persist agent turns before acceptance"
```

### Task 6: Dispatch both stream and execute through the durable queue

**Files:**

- Create: `backend/src/tasks/agent_outbox_tasks.py`
- Modify: `backend/src/tasks/celery_app.py`
- Modify: `backend/src/tasks/agent_run_tasks.py`
- Modify: `backend/src/api/agent/execute.py`
- Modify: `backend/src/api/agent/streaming.py`
- Modify: `infrastructure/helm/knowledge-graph-analytics/templates/celery-worker-deployment.yaml`
- Modify: `infrastructure/helm/knowledge-graph-analytics/values-dev.yaml`
- Create: `backend/tests/unit/tasks/test_agent_outbox_tasks.py`
- Create: `backend/tests/api/agent/test_no_process_local_dispatch.py`

**Step 1: Write architecture guard tests**

AST/source tests must fail if `/stream` or `/execute` calls
`BackgroundTasks.add_task`, `asyncio.create_task`, or the graph executor
directly.

**Step 2: Implement the outbox dispatcher**

The dispatcher publishes `run_agent_job` with a stable Celery `task_id` derived
from `run_id`, then marks the outbox row dispatched. A crash after publish but
before ack may republish; the worker claim and lease generation must make the
duplicate a no-op.

**Step 3: Make both endpoints adapters**

- `/execute`: submit and return the durable run ID.
- `/stream`: submit, emit the committed `run.created` event as `accepted`, then
  replay/subscribe to durable events for that run.

The API process must not execute model/tool work.

**Step 4: Render and test**

```bash
cd backend
pytest \
  tests/unit/tasks/test_agent_outbox_tasks.py \
  tests/api/agent/test_no_process_local_dispatch.py -q
cd ..
helm template nous-dev infrastructure/helm/knowledge-graph-analytics \
  -f infrastructure/helm/knowledge-graph-analytics/values-dev.yaml |
  rg "agent_runs|agent_outbox"
```

**Step 5: Commit**

```bash
git add backend/src/tasks/agent_outbox_tasks.py \
  backend/src/tasks/celery_app.py backend/src/tasks/agent_run_tasks.py \
  backend/src/api/agent/execute.py backend/src/api/agent/streaming.py \
  infrastructure/helm/knowledge-graph-analytics/templates/celery-worker-deployment.yaml \
  infrastructure/helm/knowledge-graph-analytics/values-dev.yaml \
  backend/tests/unit/tasks/test_agent_outbox_tasks.py \
  backend/tests/api/agent/test_no_process_local_dispatch.py
git commit -m "Route agent turns through durable dispatch"
```

### Task 7: Implement durable assistant finalization and checkpoint reconciliation

**Files:**

- Create: `backend/src/services/agent/agent_finalization_service.py`
- Create: `backend/src/tasks/agent_reconciliation_tasks.py`
- Modify: `backend/src/tasks/agent_run_tasks.py`
- Modify: `backend/src/tasks/celery_app.py`
- Modify: `backend/src/services/agent/checkpointer.py`
- Create: `backend/tests/unit/services/agent/test_agent_finalization_service.py`
- Create: `backend/tests/unit/tasks/test_agent_reconciliation_tasks.py`

**Step 1: Write failing idempotency tests**

Cover:

- replaying finalization produces one assistant row;
- assistant deltas reconstruct the same text deterministically;
- checkpoint failure leaves a retryable outbox item and no terminal event;
- retry repairs the checkpoint and appends one terminal;
- worker loss with deltas creates a partial assistant and `run.stopped`;
- worker loss without deltas creates `stopped` without a blank assistant row.

**Step 2: Implement the finalization state machine**

Worker completion appends `run.finalization_requested` and enqueues
`assistant.finalize`. The finalizer:

1. reconstructs output from durable assistant-delta events;
2. upserts the assistant message using deterministic `client_message_id`;
3. writes `assistant_message_id` to the run;
4. enqueues `checkpoint.reconcile`;
5. reconciles LangGraph checkpoint idempotently;
6. appends `checkpoint.reconciled`;
7. appends exactly one terminal event and terminal status.

Outbox payloads contain IDs only.

**Step 3: Rework the stale-run sweeper**

The sweeper must reconcile/finalize, not simply mark failed. Use:

- `partial` + `stopped` when durable deltas exist;
- `stopped` when cancellation/termination was requested;
- `failed` + `error` for unrecoverable execution errors.

**Step 4: Run tests**

Expected: all injected retries converge to one transcript and one terminal.

**Step 5: Commit**

```bash
git add backend/src/services/agent/agent_finalization_service.py \
  backend/src/tasks/agent_reconciliation_tasks.py \
  backend/src/tasks/agent_run_tasks.py backend/src/tasks/celery_app.py \
  backend/src/services/agent/checkpointer.py \
  backend/tests/unit/services/agent/test_agent_finalization_service.py \
  backend/tests/unit/tasks/test_agent_reconciliation_tasks.py
git commit -m "Reconcile agent finalization durably"
```

### Task 8: Build the canonical SSE adapter and replay path

**Files:**

- Create: `backend/src/services/agent/sse_adapter.py`
- Modify: `backend/src/api/agent/streaming.py`
- Modify: `backend/src/api/agent/execute.py`
- Modify: `backend/src/services/agent/stream_buffer.py`
- Modify: `frontend/src/services/agentChatService.ts`
- Modify: `frontend/src/services/agentStreamEvents.ts`
- Test: `backend/tests/agent/test_streaming_resume.py`
- Create: `backend/tests/contract/test_sse_sequence_contract.py`
- Test: `frontend/src/services/__tests__/agentChatService.test.ts`
- Test: `frontend/src/services/__tests__/agentStreamEvents.contract.test.ts`

**Step 1: Write the failing canonical sequence tests**

Validate:

- `id:` equals durable event sequence;
- `event:` is the frozen event name;
- `data:` validates against that event's OpenAPI schema;
- all eight common envelope fields are present;
- `Last-Event-ID=N` returns only `sequence > N`;
- query `after` remains one-release compatibility only;
- duplicate Redis wakeups cannot duplicate SSE events;
- reconnect after confirmation resumes the same logical run;
- only one terminal event appears across all reconnects.

**Step 2: Implement the adapter**

`sse_adapter.py` is the only formatter. Convert durable events to the public
event vocabulary. Replace provisional `occurred_at` with `timestamp`.
`message_id` is:

- committed user message ID for accepted/routing;
- assistant message ID when finalized;
- `None` for event types not tied to a message.

Use PostgreSQL for replay and Redis only to wake a waiter to query after its
last sequence.

**Step 3: Add heartbeats to every silent path**

Emit a comment heartbeat at most every 15 seconds:

```text
: heartbeat 2026-07-30T12:00:00Z

```

Comment heartbeats do not consume sequence IDs. If a typed heartbeat event is
retained for client telemetry, persist it only when required; do not fill the
durable table every 15 seconds.

Cover Luna, graph, tool, confirmation wait, and replay wait.

**Step 4: Update frontend resume behavior**

Track the last successfully applied durable sequence. Send `Last-Event-ID`.
Deduplicate `sequence <= lastApplied`. Treat `confirmation` as paused, not
terminal. Reconcile only on `done`, `stopped`, or `error`.

**Step 5: Run backend and frontend contracts**

```bash
cd backend
pytest \
  tests/agent/test_streaming_resume.py \
  tests/contract/test_sse_sequence_contract.py -q
cd ../frontend
corepack pnpm@10.18.2 exec vitest run \
  src/services/__tests__/agentChatService.test.ts \
  src/services/__tests__/agentStreamEvents.contract.test.ts
```

**Step 6: Commit**

```bash
git add backend/src/services/agent/sse_adapter.py \
  backend/src/api/agent/streaming.py backend/src/api/agent/execute.py \
  backend/src/services/agent/stream_buffer.py \
  frontend/src/services/agentChatService.ts \
  frontend/src/services/agentStreamEvents.ts \
  backend/tests/agent/test_streaming_resume.py \
  backend/tests/contract/test_sse_sequence_contract.py \
  frontend/src/services/__tests__/agentChatService.test.ts \
  frontend/src/services/__tests__/agentStreamEvents.contract.test.ts
git commit -m "Serve agent streams from durable events"
```

### Task 9: Standardize HTTP and stream errors on RFC 9457

**Files:**

- Create: `backend/src/api/problem_details.py`
- Modify: `backend/src/main.py`
- Modify: `backend/src/services/agent/_errors.py`
- Modify: `backend/src/api/agent/streaming.py`
- Modify: `frontend/src/services/agentChatService.ts`
- Create: `backend/tests/contract/test_problem_details.py`
- Test: `frontend/src/services/__tests__/agentChatService.test.ts`

**Step 1: Write failing HTTP and SSE error tests**

Require `application/problem+json` for pre-stream HTTP failures with:

```json
{
  "type": "https://goodwiinz.tech/problems/agent/unavailable",
  "title": "Agent temporarily unavailable",
  "status": 503,
  "detail": "Try again shortly.",
  "instance": "/api/v1/agent/stream",
  "code": "agent_unavailable",
  "trace_id": "..."
}
```

After SSE headers are sent, the `error` event contains the same bounded problem
object under `problem`. Never include stack traces, provider bodies, prompt
text, tool arguments, SQL, or secrets.

**Step 2: Implement exception mapping**

Centralize stable problem type URI, title, status, and retryability mappings.
Set `Retry-After` for retryable 429/503 cases.

**Step 3: Run tests and regenerate OpenAPI**

Expected: all declared 4xx/5xx responses reference `ProblemDetails`.

**Step 4: Commit**

```bash
git add backend/src/api/problem_details.py backend/src/main.py \
  backend/src/services/agent/_errors.py backend/src/api/agent/streaming.py \
  frontend/src/services/agentChatService.ts \
  backend/tests/contract/test_problem_details.py \
  frontend/src/services/__tests__/agentChatService.test.ts \
  backend/openapi.json frontend/src/types/generated/api.d.ts
git commit -m "Standardize agent errors with Problem Details"
```

### Task 10: Instrument the complete turn with OpenTelemetry GenAI conventions

**Files:**

- Create: `backend/src/observability/agent_tracing.py`
- Modify: `backend/src/observability/instrumentation.py`
- Modify: `backend/src/services/agent/observability.py`
- Modify: `backend/src/services/agent/llm_factory.py`
- Modify: `backend/src/services/agent/_nodes_rag.py`
- Modify: `backend/src/services/agent/_nodes_tools.py`
- Modify: `backend/src/tasks/agent_run_tasks.py`
- Create: `backend/tests/unit/observability/test_agent_genai_tracing.py`
- Test: `backend/tests/services/agent/test_pii_redact.py`

**Step 1: Write failing span-tree tests**

Assert one trace links:

```text
HTTP request
  -> agent.submit
  -> agent.route
  -> gen_ai.chat / retrieval / tool spans
  -> assistant.finalize
  -> checkpoint.reconcile
```

Require standard attributes where applicable:

- `gen_ai.operation.name`
- `gen_ai.provider.name`
- `gen_ai.request.model`
- `gen_ai.response.model`
- `gen_ai.usage.input_tokens`
- `gen_ai.usage.output_tokens`
- `error.type`

Add bounded custom attributes:

- `nous.agent.route`
- `nous.agent.prompt_class`
- `nous.agent.run_id`
- `nous.deployment.revision`
- `langsmith.trace_id`

**Step 2: Add privacy assertions**

By default spans/logs must not contain prompt content, retrieved content, model
output, tool arguments, document titles, email, auth headers, or database URLs.
Organization ID may appear only in access-controlled structured tracing/rollup
records—not metric labels—and must be covered by the retention policy.

**Step 3: Implement and run tests**

Use the repository's existing tracer provider; do not initialize a second
provider. Pin the selected semantic-convention version and document whether
GenAI attributes are stable or opt-in for that dependency version.

**Step 4: Commit**

```bash
git add backend/src/observability/agent_tracing.py \
  backend/src/observability/instrumentation.py \
  backend/src/services/agent/observability.py \
  backend/src/services/agent/llm_factory.py \
  backend/src/services/agent/_nodes_rag.py \
  backend/src/services/agent/_nodes_tools.py \
  backend/src/tasks/agent_run_tasks.py \
  backend/tests/unit/observability/test_agent_genai_tracing.py \
  backend/tests/services/agent/test_pii_redact.py
git commit -m "Trace agent turns with GenAI conventions"
```

### Task 11: Complete SLIs, organization rollups, dashboards, and burn alerts

**Files:**

- Modify: `backend/src/services/agent/observability.py`
- Modify: `backend/src/observability/celery_queue_metrics.py`
- Create: `backend/src/models/agent_sli_rollup.py`
- Create: `backend/src/tasks/agent_sli_rollup_tasks.py`
- Create: `backend/alembic/versions/b9c0d1e2f3a4_add_agent_sli_rollups.py`
- Create: `infrastructure/helm/knowledge-graph-analytics/templates/agent-prometheus-rules.yaml`
- Create: `infrastructure/helm/knowledge-graph-analytics/templates/agent-grafana-dashboard.yaml`
- Modify: `infrastructure/helm/knowledge-graph-analytics/values.yaml`
- Modify: `infrastructure/helm/knowledge-graph-analytics/values-dev.yaml`
- Test: `backend/tests/unit/services/agent/test_stream_slo_metrics.py`
- Create: `backend/tests/unit/tasks/test_agent_sli_rollup_tasks.py`
- Create: `backend/tests/unit/observability/test_agent_metric_cardinality.py`

**Step 1: Define bounded Prometheus dimensions**

Histograms/counters must support:

- accepted, first-token, completion latency;
- success by route;
- recovery attempts/success;
- accepted-message loss;
- duplicate user/assistant suppression;
- cancellation, disconnect, timeout, persistence, checkpoint failures;
- fast-path eligibility and bounded fallback reason;
- queue depth and queue wait;
- retrieval latency and source-count buckets;
- token usage and estimated cost.

Labels are limited to:

```python
route, model_family, prompt_class, deployment_revision, phase, outcome
```

All values must normalize through enums/allowlists. The cardinality test must
fail on any label named `organization_id`, `user_id`, `thread_id`, `run_id`,
`prompt`, `document_id`, `tool`, `trace_id`, or `error`.

**Step 2: Add tenant rollups outside Prometheus**

Aggregate minute/hour windows into PostgreSQL keyed by
`organization_id`, `route`, and `window_start`. Expose only through an
organization-scoped admin API. Do not export organization IDs to Prometheus.

**Step 3: Add SLO recording rules**

Create fast and graph/research recording rules for p50/p95/p99, success ratio,
recovery ratio, loss ratio, and duplication ratio.

Add multi-window burn alerts for:

- fast success below 99.5%;
- graph/research success below 99%;
- recovery below 99%;
- any accepted-message loss;
- any duplicate persisted message;
- fast accepted p95 above 250 ms;
- fast TTFT or completion p95 above 5 s.

**Step 4: Add dashboards**

Panels:

1. TTFT/completion by route/model/prompt class/revision;
2. eligibility/fallback reasons;
3. disconnect/cancel/timeout/persistence/checkpoint;
4. token use/cost;
5. queue depth/wait;
6. retrieval latency/source count;
7. recovery/loss/duplication;
8. error-budget remaining and burn.

**Step 5: Test and render**

```bash
cd backend
pytest \
  tests/unit/services/agent/test_stream_slo_metrics.py \
  tests/unit/tasks/test_agent_sli_rollup_tasks.py \
  tests/unit/observability/test_agent_metric_cardinality.py -q
cd ..
helm template nous-dev infrastructure/helm/knowledge-graph-analytics \
  -f infrastructure/helm/knowledge-graph-analytics/values-dev.yaml |
  rg "PrometheusRule|agent_stream|Grafana"
```

**Step 6: Commit**

```bash
git add backend/src/services/agent/observability.py \
  backend/src/observability/celery_queue_metrics.py \
  backend/src/models/agent_sli_rollup.py \
  backend/src/tasks/agent_sli_rollup_tasks.py \
  backend/alembic/versions/b9c0d1e2f3a4_add_agent_sli_rollups.py \
  infrastructure/helm/knowledge-graph-analytics/templates/agent-prometheus-rules.yaml \
  infrastructure/helm/knowledge-graph-analytics/templates/agent-grafana-dashboard.yaml \
  infrastructure/helm/knowledge-graph-analytics/values.yaml \
  infrastructure/helm/knowledge-graph-analytics/values-dev.yaml \
  backend/tests/unit/services/agent/test_stream_slo_metrics.py \
  backend/tests/unit/tasks/test_agent_sli_rollup_tasks.py \
  backend/tests/unit/observability/test_agent_metric_cardinality.py
git commit -m "Add agent SLO dashboards and alerts"
```

### Task 12: Add the versioned P0 evaluation corpus

**Files:**

- Create: `backend/tests/eval/datasets/agent_p0_v1.jsonl`
- Create: `backend/tests/eval/p0_schema.py`
- Modify: `backend/tests/eval/golden_examples.py`
- Modify: `backend/tests/eval/upload_golden.py`
- Create: `backend/tests/eval/test_p0_dataset_contract.py`
- Add cassettes: `backend/tests/eval/cassettes/p0/*.json`

**Step 1: Define the schema**

Each case includes:

```python
class P0Case(BaseModel):
    id: str
    version: Literal[1]
    category: P0Category
    messages: list[MessageFixture]
    page_context: dict[str, Any]
    organization_fixture: str
    expected_route: str
    forbidden_routes: list[str]
    expected_tools: list[str]
    forbidden_tools: list[str]
    expected_terminal: Literal["done", "stopped", "error"]
    must_cite: bool
    assertions: list[str]
    scenario_actions: list[ScenarioAction]
```

**Step 2: Add at least 120 cases**

Minimum distribution:

- 20 safe conversational fast-path;
- 20 RAG-required;
- 15 project/document reference;
- 10 destructive-tool/HITL;
- 10 ambiguous multi-turn follow-ups;
- 10 prompt-injection attempts;
- 10 cross-tenant attempts;
- 10 cancellation/reconnection;
- 15 citation correctness/unsupported-claim.

No real customer prompts, documents, IDs, or credentials.

**Step 3: Add dataset invariants**

Tests must fail on duplicate IDs, missing categories, fewer than 100 cases,
unrecorded cassettes, real domains/emails/secrets, or an unsafe case without a
zero-tolerance assertion.

**Step 4: Run deterministic replay**

```bash
cd backend
AGENT_GOLDEN_REPLAY=1 \
pytest \
  tests/eval/test_p0_dataset_contract.py \
  tests/eval/test_agent_regression.py -m golden -q
```

**Step 5: Commit**

```bash
git add backend/tests/eval/datasets/agent_p0_v1.jsonl \
  backend/tests/eval/p0_schema.py backend/tests/eval/golden_examples.py \
  backend/tests/eval/upload_golden.py \
  backend/tests/eval/test_p0_dataset_contract.py \
  backend/tests/eval/cassettes/p0
git commit -m "Add versioned agent P0 evaluation corpus"
```

### Task 13: Implement blocking quality and integrity evaluators

**Files:**

- Create: `backend/tests/eval/p0_evaluators.py`
- Create: `backend/tests/eval/p0_release_gate.py`
- Create: `backend/tests/eval/baselines/agent_p0_v1.json`
- Create: `backend/tests/eval/test_p0_release_gate.py`
- Modify: `backend/tests/eval/test_agent_regression.py`

**Step 1: Write failing gate tests**

The gate must report every failure without short-circuiting and exit non-zero
for:

- any protected/tool/RAG case routed to Luna;
- any tenant-isolation failure;
- any missing or duplicate user/assistant message;
- any malformed/non-monotonic SSE sequence;
- any run without exactly one terminal;
- any citation bound to a nonexistent source;
- any unsupported material claim above the evaluator threshold;
- fewer than 100 valid live samples;
- aggregate quality more than 2 absolute percentage points below baseline;
- any safety category regression, regardless of aggregate score.

**Step 2: Implement deterministic checks first**

Routing, tenancy, message counts, sequence integrity, terminal count, and
citation existence must be deterministic. Use LLM judges only for semantic
unsupported-claim/quality scoring, with fixed rubric/version and stored raw
scores.

**Step 3: Version the baseline**

Record dataset hash, evaluator version, model deployment, date, sample counts,
and category scores. Baseline updates require an explicit CLI flag and a diff;
normal CI must never rewrite it.

**Step 4: Run unit tests**

```bash
cd backend
pytest \
  tests/eval/test_p0_release_gate.py -q
```

**Step 5: Commit**

```bash
git add backend/tests/eval/p0_evaluators.py \
  backend/tests/eval/p0_release_gate.py \
  backend/tests/eval/baselines/agent_p0_v1.json \
  backend/tests/eval/test_p0_release_gate.py \
  backend/tests/eval/test_agent_regression.py
git commit -m "Gate agent releases on P0 quality"
```

### Task 14: Expand the latency benchmark to the full representative gate

**Files:**

- Modify: `scripts/perf/benchmark_agent_fast_path.py`
- Create: `scripts/perf/benchmark_agent_p0.py`
- Modify: `backend/tests/perf/test_agent_luna_fast_path.py`
- Create: `backend/tests/perf/test_agent_p0_benchmark.py`

**Step 1: Write failing benchmark tests**

Require:

- at least 100 measured samples;
- warmups excluded;
- p50/p95/p99;
- separate route/prompt-class/model/revision summaries;
- accepted, TTFT, completion, queue wait, and recovery latency;
- 100% Luna only for the safe fast corpus;
- zero failures/loss/duplicates/malformed sequences;
- machine-readable JSON and non-zero exit on any failed criterion.

**Step 2: Preserve safety bounds**

Only allow localhost and `dev-api.gen-text.app`; reject production. Require a
short-lived token. Cap concurrency and total samples. Stamp all runs as
synthetic and clean them up by run ID.

**Step 3: Implement the route-aware benchmark**

Fast gate:

- accepted p95 <=250 ms;
- TTFT p95 <=5,000 ms;
- completion p95 <=5,000 ms;
- success >=99.5%.

Graph/research:

- accepted p95 <=250 ms;
- success >=99%;
- evaluate task-specific completion targets from the dataset, not one global
  limit.

Both routes require recovery >=99%, accepted loss 0, and duplicates 0.

**Step 4: Run offline tests**

```bash
cd backend
pytest \
  tests/perf/test_agent_luna_fast_path.py \
  tests/perf/test_agent_p0_benchmark.py -q
```

Do not claim latency passed until the authenticated live command completes.

**Step 5: Commit**

```bash
git add scripts/perf/benchmark_agent_fast_path.py \
  scripts/perf/benchmark_agent_p0.py \
  backend/tests/perf/test_agent_luna_fast_path.py \
  backend/tests/perf/test_agent_p0_benchmark.py
git commit -m "Benchmark the complete agent P0 SLO"
```

### Task 15: Add pod/worker termination and reconnection verification

**Files:**

- Create: `scripts/testing/agent_failure_injection.py`
- Create: `backend/tests/integration/agent/test_durable_stream_recovery.py`
- Create: `backend/tests/integration/agent/test_terminal_exactly_once.py`
- Create: `frontend/e2e/chat-stream-recovery.spec.ts`
- Modify: `docker-compose.development.yml`

**Step 1: Add deterministic integration hooks**

Test-only hooks, disabled unless `AGENT_FAILURE_INJECTION=true`, pause/terminate
the worker:

1. after accepted commit but before dispatch;
2. after first durable assistant delta;
3. after final delta but before assistant finalization/checkpoint.

Never enable hooks in deployed values.

**Step 2: Prove recovery**

For each injection point:

- restart worker/API;
- reconnect with `Last-Event-ID`;
- assert no lost user message;
- assert one execution claim;
- assert no duplicate transcript rows;
- assert monotonic replay;
- assert one terminal;
- assert final state is completed, partial/stopped, or failed as designed.

**Step 3: Add browser coverage**

Playwright must verify the UI does not duplicate tokens/messages, retains the
correct thread, surfaces stopped/error state, and can continue after
confirmation/reconnect.

**Step 4: Run locally**

```bash
docker-compose -f docker-compose.development.yml up -d postgres redis backend celery-worker
cd backend
pytest \
  tests/integration/agent/test_durable_stream_recovery.py \
  tests/integration/agent/test_terminal_exactly_once.py -q
cd ../frontend
corepack pnpm@10.18.2 exec playwright test e2e/chat-stream-recovery.spec.ts
```

**Step 5: Commit**

```bash
git add scripts/testing/agent_failure_injection.py \
  backend/tests/integration/agent/test_durable_stream_recovery.py \
  backend/tests/integration/agent/test_terminal_exactly_once.py \
  frontend/e2e/chat-stream-recovery.spec.ts \
  docker-compose.development.yml
git commit -m "Verify agent recovery across worker loss"
```

### Task 16: Wire release workflows, documentation, rollout, and final gates

**Files:**

- Modify: `.github/workflows/agent-eval.yml`
- Create: `.github/workflows/agent-p0-release.yml`
- Modify: `docs/operations/agent-production-baseline.md`
- Create: `docs/operations/agent-p0-runbook.md`
- Modify: `infrastructure/helm/knowledge-graph-analytics/values-dev.yaml`
- Modify: `infrastructure/helm/knowledge-graph-analytics/values-staging.yaml`
- Modify: `infrastructure/helm/knowledge-graph-analytics/values-production.yaml`

**Step 1: Split PR and live release gates**

PR gate:

- schema/OpenAPI drift;
- unit/contract/integration replay tests;
- deterministic 100+ case replay;
- metric-cardinality/privacy tests;
- Helm render;
- Alembic single-head validation.

Manual/pre-release gate on an authenticated isolated dev tenant:

- live 100+ sample evaluation;
- full latency SLO;
- failure injection;
- LangSmith/OTel trace completeness;
- Prometheus query assertions;
- zero-tolerance checks.

Do not make a billing-blocked or zero-step workflow look like a test failure.

**Step 2: Add rollout controls**

Feature flags:

- `AGENT_DURABLE_STREAM_ENABLED`
- `AGENT_DURABLE_STREAM_PERCENT`
- `AGENT_OUTBOX_ENABLED`
- `AGENT_P0_RELEASE_GATE_REQUIRED`

Roll out dev at 5%, 25%, 50%, 100%; then staging. Production remains scaffolded
and must not be bumped until it is an active environment and the user
explicitly authorizes it.

Rollback disables new submissions to the durable adapter only after draining
or reconciling in-flight runs. Never roll back the database migration or stop
serving already-created durable run events.

**Step 3: Run the complete local verification**

```bash
cd backend
black --check src tests
isort --check-only src tests
pytest \
  tests/contract \
  tests/agent \
  tests/api/agent \
  tests/unit/agent \
  tests/unit/services/agent \
  tests/unit/observability \
  tests/unit/tasks \
  tests/eval \
  tests/perf -q
cd ../frontend
corepack pnpm@10.18.2 run lint
corepack pnpm@10.18.2 run type-check
corepack pnpm@10.18.2 run test
cd ..
python \
  scripts/ci/generate_openapi.py --check
python \
  scripts/ci/check_alembic.py
helm template nous-dev infrastructure/helm/knowledge-graph-analytics \
  -f infrastructure/helm/knowledge-graph-analytics/values-dev.yaml >/dev/null
git diff --check
```

Expected: all commands exit 0. Any skipped failure-injection, tenant, replay,
or contract test blocks P0. Database-dependent tests may not be waived; start
the required local services.

**Step 4: Run the authenticated release gate**

```bash
NOUS_BENCHMARK_TOKEN='<short-lived-dev-token>' \
python \
  scripts/perf/benchmark_agent_p0.py \
  --base-url https://dev-api.gen-text.app \
  --dataset backend/tests/eval/datasets/agent_p0_v1.jsonl \
  --minimum-samples 100 \
  --output artifacts/agent-p0-release.json
```

Expected: exit 0 and every acceptance-matrix row marked `passed: true`.

**Step 5: Require live observability evidence**

Attach to the PR:

- release-gate JSON;
- dataset and evaluator versions;
- exact image/commit revision;
- Prometheus SLO snapshot;
- one redacted linked OTel/LangSmith trace for Luna and graph routes;
- failure-injection results;
- migration upgrade/downgrade rehearsal result;
- rollback rehearsal result.

One successful request is not p95 or recovery evidence.

**Step 6: Commit documentation and workflows**

```bash
git add .github/workflows/agent-eval.yml \
  .github/workflows/agent-p0-release.yml \
  docs/operations/agent-production-baseline.md \
  docs/operations/agent-p0-runbook.md \
  infrastructure/helm/knowledge-graph-analytics/values-dev.yaml \
  infrastructure/helm/knowledge-graph-analytics/values-staging.yaml \
  infrastructure/helm/knowledge-graph-analytics/values-production.yaml
git commit -m "Gate releases on the complete agent P0"
```

## Definition of done

OpenCode must not say “finished,” push, or create the PR until:

- all 16 task commits exist;
- the working tree is clean;
- every local command in Task 16 exits 0;
- no database-dependent durability test is skipped;
- the live release artifact contains at least 100 valid samples;
- every zero-tolerance gate is green;
- fast and graph/research objectives are reported separately;
- failure injection passes at accepted, first-token, and final-token boundaries;
- OpenAPI generated artifacts are current;
- Helm renders for dev and staging;
- the PR description explicitly says production is not deployed;
- a human reviews the SLO/quality baseline and authorizes publication.

If a gate is blocked by credentials, billing, cluster access, or unavailable
infrastructure, report `BLOCKED` with the missing evidence. Never convert an
unrun gate into a pass.

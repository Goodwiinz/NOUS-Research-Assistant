# Agent Audit P0 PR Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to
> implement this plan task-by-task.

**Goal:** Split the full agent P0 implementation into seven reviewable PRs that
make accepted turns durable, streams typed and replayable, terminal state
unambiguous, telemetry privacy-safe, and releases evidence-gated.

**Architecture:** Complete the existing `agent_runs`/`agent_run_events` model
instead of creating a parallel store. A submission service commits the user
message, run, first event, and outbox record before `accepted`. Celery performs
execution and finalization. SSE projects the PostgreSQL event ledger and Redis
only accelerates wakeups.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy, Alembic, PostgreSQL, Redis,
Celery, LangGraph, TypeScript, OpenAPI 3.1, WHATWG SSE, RFC 9457,
OpenTelemetry, Prometheus, LangSmith, pytest, Vitest, Playwright.

---

## Detailed source

The audit this plan implements is `docs/audits/system/system-design-audit-2026-07-30.md`.

The step-by-step implementation *reference* is
`docs/plans/2026-07-30-full-agent-p0-implementation.md`. That document is
reference material, not an execution track: **this** document defines PR
boundaries, dependency order, review contracts, and deployment gates. Do not
copy tasks between PRs; cite the full-plan task numbers in each PR body using
the mapping in each section below.

## Gate mechanics

GitHub Actions is unavailable for this repository (account billing/spending
limit): every job fails within seconds with `steps: []` and 404 logs, on
`develop` too. Do not use "CI is green" as an exit gate for any PR in this
plan — that signal does not exist right now. The enforceable gate is:

```sh
scripts/ci/run_local_ci.sh --base origin/develop
```

Run it from the repo root with the backend virtualenv's pinned tools on PATH,
add `--frontend` for PRs touching `frontend/`, and paste the summary into the
PR body. Each PR's **Exit gate** below means "these assertions pass under that
script plus the named manual/service-backed checks", not "the checks tab is
green".

## Baseline: what PR #1312 already shipped

PR #1312 (commits `fd851051`, `b4d3bd4`, `ddd3bf8`) landed a first cut of the
streaming/durability baseline. Every preflight in this plan starts from that
code, not from a clean slate:

- an SSE envelope carrying `schema_version`, `sequence`, `event_id`, and
  `occurred_at` on every `data` object;
- `Last-Event-ID` request-header parsing on the resume path, with `?after=`
  retained for the compatibility window and `Last-Event-ID` winning on
  conflict;
- bounded SLO metrics (`agent_stream_accepted_duration_seconds`,
  `agent_stream_first_token_duration_seconds{route}`,
  `agent_stream_completion_duration_seconds{route}`,
  `agent_stream_routes_total{route}`, `agent_stream_turns_total{route,status}`)
  with server-owned labels only;
- a client-supplied idempotency key plus a per-user unique index on
  `agent_runs`;
- durable `/execute` dispatch through the Celery `agent_runs` queue with a
  durable row, execution lease, and stale-run sweeper.

That baseline is **provisional**, and it has four known defects. They are not
speculative — they are observed in the merged code, and a follow-up PR (folded
into P0-A/P0-E scope below) fixes them:

1. **`Last-Event-ID` is missing from `CORS_ALLOWED_HEADERS`**
   (`backend/src/core/config.py`). The browser's preflight for a cross-origin
   resume therefore fails, so cross-origin replay is dead despite the server
   supporting it.
2. **The accepted-latency clock starts inside the streaming generator.** The
   time between request receipt and generator start is unmeasured, so the
   250 ms accepted-p95 SLI does not measure what the SLO claims and cannot be
   used as a release gate as-is.
3. **The re-delivered pending-confirmation frame bypasses the envelope** and is
   written without an `id:` line, so it is both unvalidatable against the event
   schema and invisible to `Last-Event-ID` resume.
4. **`onSeq` is unwired on the confirm path** in the frontend, so sequence
   tracking (and therefore duplicate suppression and resume cursors) silently
   stops after a confirmation round-trip.

Preflights must be written against this truth: assert the four defects still
reproduce before fixing them, and do not re-implement the five shipped items.

## P0-A: Versioned lifecycle and wire contract

**Full-plan tasks:** 1-2, 9 (vocabulary, OpenAPI 3.1 event schemas, RFC 9457
problem details).

**Already shipped by #1312 (do not redo):** the eight-field envelope shape and
`schema_version` on the wire. **Fix here:** baseline defects 1 (CORS
`Last-Event-ID`), 2 (accepted-latency clock origin), and 3 (unenveloped,
id-less pending-confirmation frame) — defect 3 is a contract violation and
belongs with the freeze.

**This PR is the freeze event for `schema_version` 1.0.** Until it merges,
`docs/operations/agent-production-baseline.md` describes a provisional
contract; after it merges, breaking changes bump the version.

**Files:**

- Modify: `backend/src/shared/enums.py`
- Modify: `backend/src/services/agent/run_event_types.py`
- Modify: `backend/src/schemas/agent_run_events.py`
- Modify: `backend/src/api/agent/execute.py`
- Modify: `backend/src/api/agent/streaming.py` (pending-confirmation frame)
- Modify: `backend/src/core/config.py` (`CORS_ALLOWED_HEADERS`)
- Modify: `backend/openapi.json`
- Modify: `frontend/src/types/generated/api.d.ts`
- Create/modify contract tests under `backend/tests/contract/`

**Red test:** Every event type lacks neither an OpenAPI component nor the common
envelope; terminal vocabulary is exactly `done`, `stopped`, `error`;
`confirmation` is non-terminal; SSE error payload validates as RFC 9457. Add
one assertion per baseline defect: the re-delivered pending-confirmation frame
carries an `id:` and validates against its event schema, and
`CORS_ALLOWED_HEADERS` contains `Last-Event-ID`.

**Implementation:**

1. Freeze request, lifecycle, event, and terminal enums.
2. Define discriminated Pydantic payloads for every event.
3. Define the eight-field common envelope and typed RFC 9457 problem member.
4. Publish the event union through OpenAPI 3.1 and regenerate TypeScript.
5. Add an emit-site/handler/schema exhaustiveness contract.

**Exit gate:** OpenAPI zero-drift, frontend exhaustive switch, and malformed
event negative tests pass.

**Commit:** `feat(agent): version the streaming contract`

## P0-B: Authoritative durable event ledger

**Full-plan tasks:** 3 (authoritative PostgreSQL event store).

**Files:**

- Modify: `backend/src/models/agent_run.py`
- Modify: `backend/src/models/agent_run_event.py`
- Modify: `backend/src/services/agent/agent_run_service.py`
- Create: one Alembic revision for event/terminal constraints
- Create/modify: event-store unit and PostgreSQL integration tests

**Red test:** Concurrent writers can currently create duplicate sequences or
multiple terminals.

**Implementation:**

1. Add unique `(organization_id, run_id, sequence)` and event-id constraints.
2. Add a database-enforced one-terminal-per-run invariant.
3. Allocate sequence numbers transactionally.
4. Expose tenant-scoped append/read APIs with cursor pagination.
5. Make Redis publication post-commit and best-effort.

**Exit gate:** Concurrency tests prove ordered sequences, tenant isolation, and
one terminal event.

**Commit:** `feat(agent): make run events authoritative`

## P0-C: Atomic accepted submission and transactional outbox

**Full-plan tasks:** 4-5 (transactional outbox, atomic submission).

**Files:**

- Create: `backend/src/services/agent/agent_submission_service.py`
- Create: `backend/src/models/agent_outbox.py`
- Create: one Alembic revision for idempotency/outbox constraints
- Modify: `backend/src/api/agent/execute.py`
- Modify: `backend/src/api/agent/streaming.py`
- Create/modify submission and endpoint tests

**Red test:** Kill the request between response acceptance and persistence;
prove the current path can acknowledge work without all durable artifacts.

**Implementation:**

1. Require a client-generated idempotency key.
2. Enforce uniqueness per organization/thread/request.
3. Commit the user row, run row, `run.created`, and dispatch outbox in one
   transaction.
4. Return the existing run for an identical retry; reject key/body conflicts.
5. Emit `accepted` only after commit.

**Exit gate:** Zero acknowledged-but-uncommitted turns and zero duplicate work
under concurrent retry tests.

**Commit:** `feat(agent): atomically accept agent turns`

## P0-D: Durable execution, finalization, and reconciliation

**Full-plan tasks:** 6-7 (durable queue dispatch, assistant finalization
and checkpoint reconciliation).

**Files:**

- Modify: `backend/src/tasks/agent_run_tasks.py`
- Create/modify: outbox dispatcher and reconciliation tasks
- Modify: `backend/src/services/agent/agent_execution_service.py`
- Modify: `backend/src/services/agent/checkpointer.py`
- Create/modify failure-injection tests

**Red test:** Terminate the pod/worker after accepted, first token, and final
token; assert no loss, duplicate execution, duplicate assistant row, or
non-terminal orphan.

**Implementation:**

1. Dispatch every `/execute` and `/stream` run through the durable outbox/queue.
2. Claim execution with a lease and compare-and-swap transition.
3. Persist assistant finalization as an idempotent outbox operation.
4. Reconcile incomplete streams, final messages, and checkpoints.
5. Fail closed in shared environments when PostgreSQL checkpointing is
   unavailable.

**Exit gate:** All three pod-kill scenarios converge to one explicit terminal
state: `completed`, `partial`, `stopped`, or `failed`.

**Commit:** `feat(agent): reconcile durable agent execution`

## P0-E: Typed SSE projection and browser recovery

**Full-plan tasks:** 8 (canonical SSE adapter and replay path).

**Already shipped by #1312 (do not redo):** `Last-Event-ID` request-header
parsing on `GET /api/v1/agent/stream/resume/{thread_id}`, with `?after=` kept
for the compatibility window and `Last-Event-ID` winning when both are present;
sequence/`event_id` on every enveloped frame. **Fix here:** baseline defects 1
(`Last-Event-ID` missing from `CORS_ALLOWED_HEADERS`, which makes cross-origin
resume fail at preflight — coordinate with P0-A if it lands there first), 3
(the re-delivered pending-confirmation frame has no `id:` so resume cannot see
it) and 4 (`onSeq` unwired on the confirm path, so the browser stops tracking
sequences after a confirmation round-trip).

**Files:**

- Modify: `backend/src/api/agent/streaming.py`
- Modify: `backend/src/services/agent/stream_buffer.py`
- Modify: `backend/src/core/config.py` (`CORS_ALLOWED_HEADERS`, if not already
  fixed in P0-A)
- Modify: `frontend/src/services/agentChatService.ts` (confirm-path `onSeq`)
- Modify: `frontend/src/services/agentStreamEvents.ts`
- Modify: `frontend/src/store/chat/`
- Create/modify backend, Vitest, and Playwright replay tests

**Red test:** Disconnect after sequence N, reconnect using `Last-Event-ID`, and
assert the UI receives all and only later events with exactly one terminal.
Add three defect-specific reds: a cross-origin preflight that currently rejects
`Last-Event-ID`; a resume across a pending-confirmation frame that currently
loses that frame because it has no `id:`; and a confirm round-trip after which
`onSeq` currently stops firing.

**Implementation:**

1. Render valid SSE `id`, `event`, and JSON `data` fields.
2. Replay from PostgreSQL; use Redis only to wake blocked readers.
3. Emit heartbeat comments/events throughout silent operations.
4. Parse typed event unions and ignore duplicate sequences in the browser.
5. Reconcile terminal state and persisted transcript after reconnect.

**Exit gate:** Chunk-boundary, duplicate-frame, disconnect, replay, and
multi-tab tests pass.

**Commit:** `feat(chat): recover durable agent streams`

## P0-F: Unified observability and route SLOs

**Full-plan tasks:** 10-11 (OpenTelemetry GenAI instrumentation, SLIs /
rollups / dashboards / burn alerts).

**Files:**

- Modify: `backend/src/services/agent/observability.py`
- Modify: `backend/src/api/agent/streaming.py`
- Modify: OpenTelemetry/LangSmith instrumentation modules
- Create: Grafana dashboards and alert rules under infrastructure
- Create/modify observability tests and operations docs

**Red test:** One synthetic run must correlate browser/API/router/model/tool/
persistence/checkpoint spans while exposing no prompt, retrieved content, or
tenant identifier in metrics.

**Implementation:**

1. Add OTel GenAI operation/model/usage/error/latency attributes.
2. Correlate LangSmith and OTel using trace IDs, not metric labels.
3. Record p50/p95/p99 by bounded route/model/prompt class/revision dimensions.
4. Add TTFT, completion, fallback, queue, RAG, persistence, checkpoint, token,
   cost, and error-budget dashboards.
5. Add burn-rate alerts linked to runbooks.

**Exit gate:** Cardinality/privacy tests and dashboard query tests pass.

**Commit:** `feat(observability): enforce agent route SLOs`

## P0-G: Quality, chaos, and release gate

**Full-plan tasks:** 12-16 (evaluation corpus, blocking evaluators, full
latency benchmark, pod/worker termination and reconnection verification,
release workflow and final gates).

**Files:**

- Modify/create: versioned datasets under `backend/tests/eval/`
- Modify: `scripts/perf/benchmark_agent_fast_path.py`
- Create/modify: reliability and routing evaluators
- Modify/create: release workflow and operations runbook

**Red test:** Seed one unsafe Luna route, duplicate message, malformed stream,
tenant leak, and recovery failure; verify each blocks the release.

**Implementation:**

1. Build a 100+ case versioned corpus spanning fast, RAG, projects, destructive
   tools, ambiguity, injection, cross-tenant, cancellation, replay, citations,
   and unsupported claims.
2. Enforce zero protected/tool/RAG prompts routed to Luna.
3. Enforce zero tenant failures, missing/duplicate messages, and malformed SSE.
4. Gate fast-route latency and route-specific success/recovery SLOs.
5. Block quality regression greater than the agreed two-point tolerance.

**Exit gate:** The complete P0 acceptance matrix in the full plan passes against
the `dev` revision ArgoCD is running, evidenced by
`scripts/ci/run_local_ci.sh --base origin/develop` plus the live dev-tenant
runs. There is no staging or production app to promote to (retired in #442),
so "release" here means "declared met on `dev`".

**Commit:** `ci(agent): gate releases on durable stream quality`

## P0 merge order

`P0-A -> P0-B -> P0-C -> P0-D`, with `P0-E` branching after P0-B and rebased
after P0-D. P0-F starts after stable lifecycle names exist. P0-G merges last.
No P0 PR is deployed alone if its migration leaves old writers able to violate
new constraints; use expand/migrate/contract where required.

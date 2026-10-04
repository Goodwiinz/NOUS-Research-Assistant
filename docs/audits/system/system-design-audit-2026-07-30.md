# NOUS Agent Production Baseline v1

## Full audit and recommendations

**Audit date:** 2026-07-30  
**System:** NOUS multimodal research agent  
**Scope:** Agent request handling, streaming, durable execution, persistence,
recovery, routing, security, observability, evaluation, and minimum user-facing
behavior  
**Status:** Recommendation and implementation baseline; not a declaration that
production readiness has been achieved

---

## Executive recommendation

Define and enforce a **NOUS Agent Production Baseline v1**. It should combine
protocol standards, reliability targets, security controls, durable execution,
observability, evaluation gates, and minimum product behavior.

The present system has strong foundations:

- Resumable SSE infrastructure and sequence-aware stream handling.
- PostgreSQL-backed LangGraph checkpointing and memory.
- Durable Celery dispatch for `/agent/execute` in the shared dev environment.
- PostgreSQL `agent_runs` and `agent_run_events` foundations.
- Idempotent chat-message identifiers.
- Human confirmation for destructive tools.
- Tenant-aware document, project, and agent authorization.
- Route-aware fast-path behavior using Luna.
- Dependency-aware Kubernetes readiness.
- Queue-depth autoscaling with KEDA.
- Satellite reconciliation and retention workers.
- LangSmith tracing and Prometheus instrumentation.

However, these mechanisms do not yet form one enforceable end-to-end production
contract for `/agent/stream`.

The most important remaining gap is semantic:

> An `accepted` event must mean that the user turn and its execution intent are
> durably committed and recoverable.

Until that invariant, PostgreSQL-authoritative event replay, exactly-one
terminal state, route-specific release SLOs, and blocking quality/recovery gates
are implemented and proven together, the agent should not be described as
fully production-ready.

## Overall verdict

| Area                     | Verdict                   | Summary                                                                                                                |
| ------------------------ | ------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Streaming transport      | Partial                   | SSE and resume foundations exist, but the complete typed and durable contract is not enforced                          |
| Accepted-turn durability | Not complete              | `/stream` acceptance is not yet one atomic user/run/event/outbox transaction                                           |
| `/execute` durability    | Implemented in dev        | Celery dispatch, run projection, leases, and sweepers exist                                                            |
| Event replay             | Partial                   | Redis-assisted replay exists; PostgreSQL is not yet the complete authoritative stream source                           |
| Terminal lifecycle       | Partial                   | Terminal handling exists, but the database does not yet enforce one logical terminal event                             |
| Checkpoint durability    | Implemented in shared dev | Shared environments fail closed instead of silently using memory                                                       |
| Route performance        | Partial                   | Luna fast-path and initial metrics exist; the full 100+ sample release gate does not                                   |
| Observability            | Partial                   | Prometheus and LangSmith exist; complete OTel GenAI correlation and error-budget operations remain                     |
| Tenant isolation         | Strong foundation         | Organization scoping and ownership checks exist, but cross-tenant release tests must remain zero tolerance             |
| Tool safety              | Strong foundation         | Destructive tools require HITL confirmation; routing and prompt-injection gates need formal release enforcement        |
| Recovery testing         | Incomplete                | Targeted tests exist, but pod termination at accepted/first-token/final-token is not yet a blocking gate               |
| Quality evaluation       | Incomplete                | Evaluation infrastructure exists, but the representative, versioned production corpus is not yet the release authority |
| Minimum product behavior | Partial                   | Stop, retry, streaming, citations, and tool states exist, but terminal/recovery behavior needs one formal contract     |

---

## 1. Audit scope and method

This audit evaluates the agent as a production service rather than only as an
LLM feature.

The review covers:

1. The browser-to-agent request and streaming contract.
2. Persistence ordering and idempotency.
3. Queueing, leases, retries, and worker recovery.
4. LangGraph checkpoint and chat-message consistency.
5. Fast-path versus graph/research routing.
6. Tenant isolation and destructive tool controls.
7. Metrics, traces, logs, dashboards, and alerts.
8. Evaluation datasets and release gates.
9. User-visible loading, cancellation, partial, recovery, and error behavior.
10. Kubernetes, Redis, PostgreSQL, Celery, and ArgoCD operational behavior.

The findings distinguish:

- **Implemented:** Present in current source and configured in the active dev
  deployment.
- **Partial:** A useful mechanism exists but does not close the end-to-end
  contract.
- **Open:** The required production invariant is not implemented.
- **Verification required:** Code exists, but production readiness requires a
  live failure/load proof against the deployment revision.

Historical audit findings were treated as leads, not current truth. Current
source and active Helm values must be revalidated before implementing any item.

---

## 2. Current system architecture

The active agent path is:

```text
Browser
  -> Next.js chat client
  -> FastAPI /api/v1/agent/stream or /execute
  -> intent and route selection
  -> Luna fast path or LangGraph graph/research path
  -> model, retrieval, and tool calls
  -> chat-message persistence
  -> LangGraph PostgreSQL checkpoint
  -> SSE projection to browser
```

The deployed dev environment uses:

- PostgreSQL/Supabase for durable application and checkpoint state.
- Managed Redis/Valkey for Celery, caching, wakeups, and transient stream
  acceleration.
- Celery workers for durable background execution.
- Neo4j and DO KB as reconcilable satellite stores.
- DO Spaces for object storage.
- ArgoCD and Helm for deployment.
- KEDA for queue-aware worker scaling.
- LangSmith, Prometheus, and application logs for observability.

The recommended authority model is:

| Concern              | Authority                                    |
| -------------------- | -------------------------------------------- |
| Accepted user turn   | PostgreSQL chat/user-message row             |
| Agent execution      | PostgreSQL `agent_runs`                      |
| Stream history       | PostgreSQL `agent_run_events`                |
| Dispatch intent      | PostgreSQL transactional outbox              |
| Execution transport  | Celery                                       |
| Wakeup/cache         | Redis                                        |
| Agent checkpoint     | PostgreSQL LangGraph checkpointer            |
| Display transcript   | PostgreSQL chat messages                     |
| Knowledge satellites | Neo4j and DO KB, repaired from durable truth |

Redis may improve latency, but losing Redis must not lose accepted work,
replayable events, or the final answer.

---

## 3. P0 audit findings

P0 findings block a production-readiness declaration.

### P0.1 Accepted does not yet represent one durable transaction

**Risk:** A browser can be told that a turn was accepted before every artifact
required to recover that turn is committed.

**Required invariant:**

`accepted` means all of the following committed in one PostgreSQL transaction:

1. The user message.
2. The `agent_runs` row.
3. The initial `run.created` or accepted event.
4. The dispatch outbox record.
5. The client idempotency key.

If the transaction fails, no accepted event is emitted. If the HTTP connection
dies after commit, retrying the same key returns the existing run.

**Recommendation:**

- Require a client-generated idempotency key.
- Add a unique constraint scoped by organization, thread, and request key.
- Introduce a transactional submission service.
- Publish queue work from a transactional outbox.
- Treat Redis publication as post-commit and best-effort.

**Acceptance test:** Kill the API process at every statement boundary around
submission. There must be zero acknowledged turns lacking a committed user
message, run, initial event, and outbox record.

### P0.2 The SSE contract is useful but not fully standardized

The stream should follow the
[WHATWG SSE format](https://html.spec.whatwg.org/dev/server-sent-events.html):

```text
id: <durable-event-id>
event: <event-type>
data: <single JSON object>

```

Every event data object should use a common envelope:

```json
{
  "schema_version": "1.0",
  "sequence": 42,
  "trace_id": "opaque-trace-id",
  "thread_id": "thread-uuid",
  "message_id": "message-uuid-or-null",
  "route": "luna",
  "phase": "generation",
  "timestamp": "2026-07-30T18:00:00Z"
}
```

Required event types:

- `accepted`
- `token`
- `tool_start`
- `tool_end`
- `rag_context`
- `plan`
- `reflection`
- `trace`
- `usage`
- `heartbeat`
- `confirmation`
- `done`
- `stopped`
- `error`

**Recommendation:**

- Define every request and event payload as a Pydantic schema.
- Publish a discriminated union through OpenAPI.
- Generate the TypeScript wire contract.
- Add an exhaustiveness test: emitted events must equal documented schemas and
  handled frontend events.
- Support `Last-Event-ID`; preserve any legacy query cursor only during a
  documented compatibility window.
- Send heartbeat comments or events during silent periods.

### P0.3 Replay is not yet PostgreSQL-authoritative

Redis is an appropriate wakeup and short-lived acceleration layer. It is not the
correct authority for accepted event history.

**Recommendation:**

- Make `agent_run_events` the replay source.
- Enforce unique `(run_id, sequence)` and durable event IDs.
- Read events strictly after `Last-Event-ID`.
- Use Redis only to wake a blocked reader when new durable events commit.
- Make frontend processing idempotent by event ID/sequence.

**Acceptance test:** Delete all relevant Redis keys during a live turn. The
browser must reconnect and replay the complete ordered event history from
PostgreSQL.

### P0.4 Terminal state is not fully enforced

Every logical run must have exactly one terminal event:

- `done`
- `stopped`
- `error`

`confirmation` is a pause, not a logical terminal event.

Durable run terminal states should be explicit:

- `completed`
- `partial`
- `stopped`
- `failed`

**Recommendation:**

- Enforce one terminal event per run with a database constraint or terminal
  compare-and-swap transition.
- Define cancellation, browser disconnect, timeout, worker death, and partial
  output behavior centrally.
- Persist partial assistant text before writing `stopped`.
- Make final assistant-message creation idempotent.
- Reconcile a final event, final message, and checkpoint if any one is missing.

**Acceptance test:** Race cancellation, timeout, finalization, and worker retry.
The result must contain one terminal event and at most one assistant message.

### P0.5 Error responses are not yet one RFC 9457 contract

HTTP errors and in-stream errors should share a stable semantic model based on
[RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457.html).

Required fields:

- `type`
- `title`
- `status`
- `detail`
- `instance`

Recommended extensions:

- `code`
- `trace_id`
- `retryable`
- `phase`

The stream `error` event should contain the problem object under a stable
member such as `problem`.

Security-sensitive details, stack traces, internal URLs, SQL, prompts, retrieved
content, credentials, and cross-tenant identifiers must never appear.

### P0.6 Route-specific SLOs are not yet release authority

Fast conversational work and graph/research work are different workload
classes. They require separate objectives, consistent with
[Google SRE guidance](https://sre.google/sre-book/service-level-objectives/).

| SLI                          | Fast/Luna route             | Graph/research route |
| ---------------------------- | --------------------------- | -------------------- |
| Accepted event p95           | <=250 ms                    | <=250 ms             |
| First token p95              | <=5 s                       | Task-specific        |
| Completion p95               | <=5 s for benchmark prompts | Task-specific        |
| Successful streams           | >=99.5%                     | >=99%                |
| Accepted messages lost       | 0                           | 0                    |
| Duplicate persisted messages | 0                           | 0                    |
| Successful replay/recovery   | >=99%                       | >=99%                |

Track p50, p95, and p99 by bounded dimensions:

- Route.
- Model family.
- Prompt class.
- Deployment revision.
- Phase.
- Outcome.

Never use organization, user, thread, message, prompt, document, trace ID, tool
arguments, or exception text as metric labels.

### P0.7 Unified observability is incomplete

Every turn should connect:

```text
browser request
  -> API stream
  -> routing decision
  -> model/retrieval/tool calls
  -> persistence
  -> checkpoint
  -> durable terminal event
```

Use
[OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
alongside LangSmith.

Required dashboards:

1. Accepted latency, TTFT, and completion latency by route.
2. Fast-path eligibility and fallback reasons.
3. Disconnect, cancellation, timeout, and replay outcomes.
4. Persistence, outbox, checkpoint, and reconciliation failures.
5. Queue depth, oldest-message age, and worker lease age.
6. RAG retrieval latency and source count.
7. Token usage and estimated cost.
8. Error-budget burn.

Prompts and retrieved content should be excluded from telemetry by default or
explicitly redacted.

### P0.8 Automated quality and security gates are incomplete

Create a versioned evaluation dataset with at least 100 representative cases:

- Safe conversational fast-path prompts.
- RAG-required questions.
- Project and document references.
- Destructive tool requests.
- Ambiguous follow-ups.
- Prompt-injection attempts.
- Cross-tenant access attempts.
- Cancellation and reconnection scenarios.
- Citation correctness.
- Unsupported-claim detection.
- Tool failure and partial completion.
- Long silent operations requiring heartbeats.

Release gates:

- Zero protected, tool, or RAG prompts incorrectly routed to Luna.
- Zero tenant-isolation failures.
- Zero missing or duplicate persisted messages.
- Zero malformed or multiply-terminal SSE sequences.
- Latency and recovery SLOs pass with at least 100 live samples.
- Quality score does not regress by more than the agreed tolerance.
- Every destructive action still requires explicit HITL confirmation.

Security scenarios should include the major agent risks described by the
[OWASP GenAI Security Project](https://genai.owasp.org/llm-top-10/), including
prompt injection, sensitive information disclosure, excessive agency, improper
output handling, and unbounded consumption.

---

## 4. P1 recommendations

P1 work makes the P0 contract operable under real failure and load.

### P1.1 Recovery operations

- Add compare-and-swap ownership to every recovery worker.
- Distinguish never-dispatched, stale-running, awaiting-confirmation, partial,
  and terminal runs.
- Track outbox age, retry count, lease age, and dead-letter count.
- Pair every alert with a tested runbook and values-level rollback.
- Run Redis, worker, API-pod, and PostgreSQL failure drills in dev.

### P1.2 Multi-store convergence

- Continue recording Neo4j and DO KB outcomes per document.
- Inject satellite failures and prove the reconciler repairs them.
- Upgrade chat/checkpoint divergence detection to guarded repair.
- Reconcile referenced and actual DO Spaces objects.
- Track convergence backlog and oldest-drift age.
- Ensure every repair is organization-scoped, bounded, and idempotent.

### P1.3 Queue scaling and capacity

- Measure queue age in addition to depth.
- Validate every live Celery queue is included in KEDA and metrics.
- Load-test I/O-bound LLM tasks where CPU is not a useful saturation signal.
- Define worker concurrency, maximum queue age, scale-up time, and scale-down
  stability.
- Test broker authentication failure and values-only rollback.

### P1.4 Network isolation

- Enable ingress NetworkPolicy only after ingress controller, kubelet probes,
  and Prometheus sources are verified.
- Add least-privilege egress incrementally.
- Test required access to DNS, Supabase, Redis, Azure, DO Spaces/KB, Neo4j, and
  telemetry.
- Keep a tested rollback through Helm values and ArgoCD.

### P1.5 Semantic compatibility gates

- Keep generated OpenAPI and TypeScript drift checks.
- Add semantic breaking-change detection with a pinned `oasdiff`.
- Require an explicit, audited approval for intended breaking changes.
- Run Alembic `upgrade head` against empty PostgreSQL.
- Add `alembic check` and downgrade/upgrade smoke tests.

### P1.6 Realtime progress convergence

- Use one shared cross-process transport for document/job progress.
- Select one backend WebSocket router and one frontend client.
- Preserve authentication through `Sec-WebSocket-Protocol`.
- Remove polling or legacy socket duplication only after parity tests.

---

## 5. P2 recommendations

P2 work reduces the architectural conditions that created the audited defects.

### P2.1 Service boundaries

- Keep HTTP routes and agent tools as adapters.
- Put each business operation in one shared service.
- Collapse duplicated ownership checks.
- Prohibit service imports from API modules.
- Route all message creation through one idempotent use case.

### P2.2 Transaction ownership

- Let the request/use-case boundary own commit.
- Let leaf services flush.
- Remove `commit: bool` behavior switches.
- Pass IDs—not ORM instances or live sessions—through LangGraph configuration.
- Use one tool-session context and one worker async boundary.

### P2.3 API consolidation

- Decide whether `v2` is a real API version or the thread domain.
- Retire duplicate message and stream endpoints.
- Use shared job/document enums.
- Adopt one pagination dependency and response shape.
- Generate wire types; keep view models handwritten.

### P2.4 Dead runtime removal

- Remove superseded search generations after traffic and feature-parity proof.
- Remove deprecated WebSocket/SSE paths after migration.
- Delete unreachable packages and expired compatibility shims.
- Guard the supported route and import inventory in tests.

### P2.5 Neo4j disaster recovery

Choose and test one explicit policy:

1. **Derived-data policy:** Rebuild from PostgreSQL/documents within the
   required RTO/RPO using an automated repair process.
2. **Backup policy:** Create encrypted scheduled backups, retention, restore
   verification, and alerts.

Do not leave backup-shaped Helm values without an implemented restore contract.

### P2.6 Frontend state ownership

- Use TanStack Query as the default server-state owner.
- Keep the server-canonical chat transcript as a documented narrow Zustand
  exception.
- Do not independently cache one entity in both systems.
- Test stale response orderings, thread switches, reconnects, and HITL with
  deterministic deferred-promise interleavings.

---

## 6. Minimum product baseline

Production readiness includes user behavior, not only backend durability.

### Required user states

The UI must clearly distinguish:

- Connecting.
- Accepted/queued.
- Thinking or retrieving.
- Using a tool.
- Waiting for confirmation.
- Streaming an answer.
- Stopped with partial output.
- Recovering/reconnecting.
- Completed.
- Failed with a safe retry path.

### Required user controls

- Stop generation.
- Retry safely using the same or a new idempotency key as appropriate.
- Resume after transient disconnect.
- Approve or reject destructive tool actions.
- Inspect citations and tool outcomes.
- Preserve partial text when the run stops after producing useful output.

### Required honesty rules

- Do not show “completed” before persistence and terminal reconciliation.
- Do not silently fall back from a RAG-required request to unsupported general
  generation.
- Do not present fallback-generated content as retrieved evidence.
- Do not hide tool, retrieval, persistence, or checkpoint failure behind a
  generic success state.
- Do not retry a destructive action without renewed or durably associated
  confirmation.

---

## 7. Recommended implementation sequence

### P0-A: Contract vocabulary

Define typed lifecycle, SSE, OpenAPI, and Problem Details contracts.

### P0-B: Durable event ledger

Complete `agent_run_events`, sequence allocation, tenant-scoped reads, and the
one-terminal invariant.

### P0-C: Atomic submission

Commit user message, run, first event, idempotency key, and dispatch outbox
before accepted.

### P0-D: Durable execution and finalization

Move all accepted work through Celery; reconcile final message, checkpoint, and
terminal event.

### P0-E: SSE projection and browser recovery

Serve typed SSE from the durable ledger with heartbeat and `Last-Event-ID`
replay.

### P0-F: Observability and SLOs

Connect browser, API, routing, model/tool, persistence, checkpoint, and terminal
state through OTel/LangSmith and bounded metrics.

### P0-G: Release gate

Run the 100+ case quality, security, latency, and failure-injection suite against
the deployment revision to be promoted.

Then implement P1 recovery/convergence/capacity controls and P2 architectural
consolidation.

---

## 8. Production readiness checklist

### Protocol

- [ ] Every event has an OpenAPI schema.
- [ ] SSE uses valid `id`, `event`, and `data`.
- [ ] All data objects use the required v1 envelope.
- [ ] Heartbeats cover every silent path.
- [ ] `Last-Event-ID` replay is PostgreSQL-authoritative.
- [ ] Every run has exactly one terminal event.
- [ ] HTTP and SSE errors validate as RFC 9457 Problem Details.

### Durability

- [ ] Accepted means user/run/event/outbox committed.
- [ ] Client idempotency keys are database-enforced.
- [ ] `/execute` and `/stream` use durable execution.
- [ ] Final assistant persistence is idempotent.
- [ ] Checkpoint failure is fail-closed in shared environments.
- [ ] Reconciliation repairs incomplete terminal/final/checkpoint state.
- [ ] Pod-kill tests pass after accepted, first token, and final token.

### Reliability and performance

- [ ] Fast accepted p95 is at most 250 ms.
- [ ] Fast TTFT p95 is at most 5 seconds.
- [ ] Fast completion p95 is at most 5 seconds for benchmark prompts.
- [ ] Fast successful streams are at least 99.5%.
- [ ] Graph/research successful streams are at least 99%.
- [ ] Recovery succeeds at least 99% by route.
- [ ] Accepted message loss is zero.
- [ ] Duplicate persisted messages are zero.

### Security

- [ ] Tenant isolation tests have zero failures.
- [ ] Protected/tool/RAG prompts never route to Luna.
- [ ] Destructive tools always require HITL.
- [ ] Prompt injection and excessive-agency cases block unsafe behavior.
- [ ] Telemetry excludes or redacts prompts and retrieved content.
- [ ] Metrics contain no tenant or user identifiers.
- [ ] Error responses reveal no sensitive internals.

### Observability

- [ ] Browser-to-terminal trace correlation works.
- [ ] Route, model, token, cost, queue, retrieval, and persistence dashboards
      exist.
- [ ] Error-budget burn alerts exist.
- [ ] Every alert links to a tested runbook.
- [ ] Deployment revision is present on traces and bounded metrics.

### Quality

- [ ] The versioned release dataset contains at least 100 representative cases.
- [ ] Citation correctness and unsupported claims are evaluated.
- [ ] Cancellation and reconnection scenarios pass.
- [ ] Malformed SSE sequences are zero.
- [ ] Quality regression stays within the agreed tolerance.
- [ ] The exact deployment revision passes the release gate.

### Product behavior

- [ ] Queued, active, confirmation, reconnecting, partial, stopped, completed,
      and failed states are distinguishable.
- [ ] Stop, retry, resume, approve, and reject behave idempotently.
- [ ] Partial useful output is preserved honestly.
- [ ] Citations and tool outcomes survive reload.
- [ ] The transcript converges after reconnect.

---

## 9. Final recommendation

Adopt the following release rule:

> NOUS Agent Production Baseline v1 is complete only when the exact deployment
> revision passes every zero-tolerance integrity and security gate, the
> route-specific latency/reliability objectives, and all three pod-termination
> recovery scenarios with at least 100 representative evaluation cases.

Do not use the presence of individual mechanisms—Redis replay, Celery, an
`agent_runs` table, checkpoints, metrics, or a small fast-path benchmark—as a
substitute for the end-to-end proof.

The recommended order is:

1. Establish the versioned contract.
2. Make acceptance and event history durable.
3. Enforce exactly one terminal state.
4. Reconcile execution, message, and checkpoint outcomes.
5. Instrument route-specific SLOs and privacy-safe traces.
6. Make security, quality, latency, and recovery tests blocking.
7. Only then declare Baseline v1 achieved and promote it beyond dev.

---

## Standards and references

- [WHATWG Server-Sent Events](https://html.spec.whatwg.org/dev/server-sent-events.html)
- [OpenAPI Specification 3.1.1](https://spec.openapis.org/oas/v3.1.1.html)
- [RFC 9457: Problem Details for HTTP APIs](https://www.rfc-editor.org/rfc/rfc9457.html)
- [OpenTelemetry Generative AI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [Google SRE: Service Level Objectives](https://sre.google/sre-book/service-level-objectives/)
- [OWASP GenAI Security Project: LLM risks](https://genai.owasp.org/llm-top-10/)

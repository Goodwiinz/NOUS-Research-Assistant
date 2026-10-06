# Agent Audit P1 PR Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to
> implement this plan task-by-task.

**Goal:** Turn the P0 agent contract into an operable production system by
proving recovery, convergence, scaling, isolation, and compatibility under
realistic failure and load.

**Architecture:** Keep P0's PostgreSQL execution ledger authoritative. P1 adds
tested operations around that core: bounded sweepers, active convergence,
queue-aware capacity, deny-by-default network access, semantic compatibility
checks, and one supported realtime progress path.

**Tech Stack:** Celery, PostgreSQL, Redis, Prometheus/Grafana, KEDA, Kubernetes
NetworkPolicy, Alembic, OpenAPI/oasdiff, pytest, k6, Helm, ArgoCD.

---

## P1 preflight PR: reconcile the historical audit

**Files:**

- Modify: `docs/audits/system/system-design-audit-2026-07-10.md`
- Create: `docs/operations/agent-audit-verification-2026-07-30.md`

For every X/B/C/D finding, record `closed`, `partial`, `open`, or `regressed`,
the current commit, the proving test, and the merged remediation PR. This PR is
documentation-only and must land before behavioral P1 work so completed fixes
are not reimplemented.

**Exit gate:** All 35 findings have current evidence; no status relies only on a
commit subject or stale line number.

## P1-A: Recovery policy, drills, and runbooks

**Files:**

- Modify: `backend/src/tasks/agent_run_tasks.py`
- Modify: `backend/src/tasks/reconcile_jobs.py`
- Modify: `backend/src/tasks/celery_app.py`
- Modify: `backend/docs/redis-durability-contract.md`
- Create: `docs/operations/agent-recovery.md`
- Create/modify: sweeper and failure-injection tests

**Red tests:**

- Stale running lease, lost pre-dispatch job, parked HITL run, broker outage,
  and Redis failover each converge to the documented state.
- Two sweepers cannot claim the same repair.
- A live execution is never swept.

**Implementation:**

1. Replace policy comments with explicit compare-and-swap transitions.
2. Add bounded retry/dead-letter behavior and age/attempt metrics.
3. Define recovery time objectives per failure class.
4. Add alerts for outbox age, stale lease count, queue age, and terminal gaps.
5. Write and execute the dev drill for each failure class.

**Exit gate:** Recovery drills pass twice without manual database repair.

**Commit:** `feat(agent): operationalize durable recovery`

## P1-B: Multi-store convergence and retention proof

**Files:**

- Modify: `backend/src/tasks/reconcile_tasks.py`
- Modify: `backend/src/tasks/retention_tasks.py`
- Modify: `backend/src/services/agent/agent_execution_service.py`
- Modify: `backend/src/services/knowledge_graph/repair.py`
- Modify: storage reconciliation services/tests
- Create: `docs/operations/data-convergence.md`

**Red tests:**

- Inject failed Neo4j and DO KB writes, chat/checkpoint divergence, orphaned
  Spaces objects, and interrupted retention batches.
- Assert detection, organization-scoped repair, idempotency, and bounded work.

**Implementation:**

1. Add last-attempt, attempt-count, and terminal repair outcome telemetry.
2. Promote detection-only dual-store checks to guarded repair where safe.
3. Verify satellite and retention workers remain apply-enabled in dev.
4. Add referenced-vs-actual object reconciliation with report/apply modes.
5. Define backlog SLOs and alert thresholds for each repair queue.

**Exit gate:** Injected drift reaches convergence and repeated repair is a
no-op.

**Commit:** `feat(reconciliation): prove multi-store convergence`

## P1-C: Queue scaling and network isolation burn-in

**Files:**

- Modify: `infrastructure/helm/knowledge-graph-analytics/values-dev.yaml`
- Modify: `infrastructure/helm/knowledge-graph-analytics/templates/keda-scaledobject.yaml`
- Modify: `infrastructure/helm/knowledge-graph-analytics/templates/networkpolicy.yaml`
- Modify: `docs/decisions/worker-autoscaling.md`
- Create: `docs/operations/agent-capacity-and-isolation.md`
- Create/modify: Helm rendering and load tests

**Red tests:**

- Queue age grows under an I/O-heavy workload despite low CPU.
- Forbidden ingress/egress paths succeed before policy activation.

**Implementation:**

1. Validate KEDA triggers for every live queue and expose queue age, not only
   depth.
2. Run scale-up/down and broker-auth-failure tests; record time to recover.
3. Enable ingress NetworkPolicy only after namespace/metrics/kubelet sources
   are proven.
4. Add least-privilege egress incrementally for DNS, Supabase, Redis, Azure,
   DO Spaces/KB, Neo4j, and telemetry.
5. Keep values-only rollback and verify it.

**Exit gate:** Load SLOs pass, denied paths remain denied, required paths remain
healthy, and rollback is tested.

**Commit:** `feat(helm): harden agent scaling and isolation`

## P1-D: Semantic API and executable schema compatibility gates

**Files:**

- Modify: `.github/workflows/test-pipeline.yml`
- Modify: `scripts/ci/check_alembic.py`
- Create/modify: CI contract tests
- Create: `docs/engineering/api-contracts.md`

**Red tests:**

- A regenerated but breaking OpenAPI change passes the current drift-only gate.
- A migration graph can be single-head while failing `upgrade head` on empty
  PostgreSQL.

**Implementation:**

1. Add pinned `oasdiff breaking` against the PR base schema.
2. Add an explicit, audited breaking-change escape label.
3. Run Alembic `upgrade head` from empty PostgreSQL.
4. Add `alembic check` and downgrade/upgrade smoke, initially advisory only if
   the measured baseline is not green.
5. Ratchet each advisory to blocking after its debt reaches zero.

**Exit gate:** Representative breaking API and migration mutations fail CI.

**Commit:** `ci(contracts): block semantic API and schema regressions`

## P1-E: One realtime progress transport

**Files:**

- Modify: `backend/src/api/realtime/websocket.py`
- Modify: `backend/src/api/realtime/websocket_v2.py`
- Modify: `frontend/src/services/realtimeWebSocketService.ts`
- Modify: `frontend/src/services/websocket.ts`
- Modify: document processing progress publishers
- Create/modify: websocket and multi-worker integration tests

**Red test:** Publish progress in a Celery worker and prove a browser connected
to another API pod receives it once, with tenant authorization enforced.

**Implementation:**

1. Publish progress through Redis pub/sub or the existing shared broker.
2. Select one backend router and one frontend client as canonical.
3. Migrate live callers and mark legacy paths deprecated.
4. Verify WebSocket authentication through `Sec-WebSocket-Protocol`.
5. Remove polling duplication only after parity tests pass.

**Exit gate:** Cross-process progress, reconnect, tenant isolation, and
duplicate-delivery tests pass.

**Commit:** `refactor(realtime): converge progress transport`

## P1 merge order

Land the audit preflight first. P1-A and P1-B may run in parallel after P0.
P1-C depends on P1-A metrics. P1-D is independent after the P0 schema settles.
P1-E may run independently but must not remove compatibility paths until its
browser tests pass in dev.

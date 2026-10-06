# System Design Audit — 2026-07-10

**Scope:** architecture-level design of the whole platform (backend, agent, data stores, deployment, API contracts). This is *not* a bug hunt — correctness bugs found and fixed by prior audits (chat rounds 1–3, DB, WebSocket, tenancy/KG/auth, DO KB, confirm-stream) were explicitly excluded and are not re-flagged.

**Method:** four independent passes, merged and deduplicated:

| Pass | Auditor | Dimension | IDs |
|---|---|---|---|
| 1 | Fable agent | Service boundaries, module coupling, responsibility placement | B1–B10 |
| 2 | Fable agent | Data architecture: stores, source of truth, consistency, lifecycle | D1–D8 |
| 3 | Fable agent | External contracts: API surface, streaming protocol, FE/BE coupling | C1–C10 |
| 4 | Codex (independent) | Infra/reliability: dispatch durability, probes, scaling, isolation | X1–X7 |

All evidence is file:line-verified against the repo at commit `06b4ab88`. Working ledger (status tracking, claims, PRs): `~/.audit-ledgers/rag/system-design-2026-07-10.md`.

---

## Executive summary

35 findings: **12 High, 3 Medium-High, 14 Medium, 6 Low.** The individual findings cluster into four recurring design patterns that have been *generating* the bug classes previous audits fixed one at a time:

1. **Prototype durability on the live environment (X1–X3).** `dev` is the only live environment and is used as production, but runs on dev-tier reliability primitives: agent runs dispatched via `BackgroundTasks` with an ephemeral Redis-only job record, health probes that can't fail, and a checkpointer allowed to silently fall back to in-memory state.
2. **Inverted layering (B1, B2, B5).** Agent tool implementations live in the API layer; services import *from* `api/` through 20+ lazy-import cycle workarounds. The measurable cost is 2–3× duplication of business logic across route / agent tool / agent node.
3. **The missing reconciler (D1, D2, D5, D6, D7).** The consistency model is "Postgres is truth, satellite stores are best-effort" — defensible, except the repair half was never built. Compensation comments across the codebase promise a sweep that doesn't exist.
4. **Contract drift by construction (C1–C7).** Wire contracts (SSE events, error envelope, statuses, job states) are hand-maintained string literals in 3–4 copies per contract, with zero OpenAPI consumption despite FastAPI generating the schema for free.

### Highest-leverage sequence

1. **X1 + X2 + X3** — the "dev is actually prod" trio: durable agent-run record with lease + recovery sweeper; wire the existing readiness endpoint into the probes; forbid MemorySaver on any shared environment.
2. **B1** — mechanical file move (`tools_impl.py` → `services/agent/`) that kills most circular imports and unblocks B2/B5 dedup.
3. **B4** — pure deletion of ~26k never-imported lines.
4. **C5** — check in `openapi.json` + typegen for high-churn domains; this is the root fix that makes C1/C2/C6/C7 structurally impossible.
5. **One scheduled reconciler** driven from Postgres status columns closes the D1/D2/D5/D6/D7 family.

---

## High findings

### X1 — Agent `/execute` is not durably dispatched (Codex; merges D7 job-store half)

The execute path writes a Redis status record, then schedules the LangGraph run with FastAPI `BackgroundTasks`. A pod kill/eviction after the `job_id` response loses the execution: no queue, no lease, no recovery worker. The job store is Redis-only with a 1-hour TTL and no Postgres projection, so a Redis failover mid-run 404s every poller and orphans HITL confirm state. The assistant message still landing in `chat_messages` is an accident of the dual-persistence design, not a stated invariant.

- `backend/src/api/agent/execute.py:339`, `backend/src/api/agent/jobs.py:1142`, `backend/src/services/agent/job_store.py:1-35,168`
- **Remediation:** durable `agent_runs` record + transactional outbox/Celery dispatch, idempotency keys, lease + stale-run recovery sweeper.

### X2 — Kubernetes probes report healthy without checking dependencies (Codex)

All three active probes use `/health`, which unconditionally returns healthy. A live Redis/Supabase outage after boot leaves the pod in service while agent turns, rate limits, and job persistence fail. A dependency-aware `/health/readiness` endpoint already exists and is unused.

- `infrastructure/helm/knowledge-graph-analytics/values-dev.yaml:188`, `backend/src/main.py:648`, `backend/src/health/endpoints.py:244`
- **Remediation:** wire startup/liveness/readiness separately; readiness includes the services required by the enabled feature set.

### X3 — Live `dev` can silently downgrade agent durability to MemorySaver (Codex)

Only `production` and `staging` reject Postgres persistence failures — environments that were retired in #442. The deployed environment value is `dev`, so checkpoints and long-term memory can fall back to process memory, breaking HITL/resume and cross-restart context while `chat_messages` continues to persist (invisible divergence).

- `infrastructure/helm/knowledge-graph-analytics/values-dev.yaml:83`, `backend/src/services/agent/checkpointer.py:44`, `backend/src/services/agent/memory.py:114`
- **Remediation:** durability mandatory for any shared/live environment — key the rule on "is this shared", not on environment-name string matching.

### B1 — Agent tool implementations live in `api/`; services import from the API layer

Every LangGraph tool in `services/agent/tools.py` does a deferred `from src.api.agent.execute import _tool_*` (21 occurrences), and the graph lazily imports `execute_tool` from the API layer. The actual 2,797-line implementation module `api/agent/tools_impl.py` contains zero HTTP concerns — every function takes `(args, db, current_user)` — yet sits in `api/`. The services→api arrow is papered over with 20+ "lazy import to avoid circular dependency" sites.

- `backend/src/services/agent/tools.py:174-741`, `backend/src/services/agent/graph.py:154,173-179`, `backend/src/api/agent/tools_impl.py`
- **Remediation:** move `tools_impl.py` + `tool_helpers.py` into `services/agent/` (signatures are already service-shaped); leave re-export shims. Mechanical move.

### B2 — Business logic duplicated 2–3×: route vs agent tool vs agent node

- Note creation: `api/research/projects.py:727-757` vs `api/agent/tools_impl.py:1673-1724` (independent ORM code, different auth helpers).
- Document search: `api/documents/documents.py:921` vs `_tool_search_documents` (`tools_impl.py:1346-1394`).
- DO KB retrieval implemented twice *inside the agent alone*: `tools_impl.py:1397-1476` and `services/agent/_nodes_rag.py:279-350` — same org→kb lookup, same 404 handling, maintained separately.
- Three project-ownership guards: `_get_project_with_auth`, `_verify_project_ownership`, `_user_owns_project`.

The repeated re-fixes of scoping/count/status behavior in past audits are the empirical bill. `_tool_create_project` → `ProjectService.create_project` (`tools_impl.py:1620-1659`) is the correct adapter shape; it exists but isn't the norm.

- **Remediation:** one service function per operation; route and tool become adapters; collapse the three ownership guards into one.

### B3 — Dual sync/async DB stacks with mirrored duplicate services

Two engines + two session factories (`core/database.py:130-197`); services split 19 sync / 19 async files. `FileService` (async) and `EnhancedFileService` (sync) both implement upload, hashing, path generation, storage helpers, and org-dedup — with two upload endpoints riding them. The compensating object-delete exists **only** in the sync service (`enhanced_file_service.py:1189`), so the async upload path has different failure semantics by accident.

- **Remediation:** declare one canonical upload path and fold the other in; give Celery a single `run_async` boundary helper instead of a mirrored sync service.

### B4 — ~26k lines of never-imported packages; both multi-agent-search generations mounted

Zero importers outside their own package: `architecture/` (1,018 loc), `performance/` (9,831), `monitoring/` (11,555), `evaluation/` (2,799), `resilience/` (978) — ~11% of the 235k-line backend. Separately, `multi_agent_search_service.py` and `multi_agent_search_service_v2.py` are both live with both routers mounted (`main.py:555-556`).

- **Remediation:** delete the five packages (git remembers); retire one search generation.

### D1 — Document fan-out is fire-and-forget with manual-only reconciliation

A document lives in Postgres + DO Spaces + DO KB + Neo4j, but every satellite write swallows failure and the document reaches `COMPLETED` regardless: Neo4j indexing wrapped in warn-and-continue (`tasks/processing_tasks.py:254-289`); DO KB sync is a "clean no-op" on outage (`:291-302`) with a stale "Qdrant remains source of truth" docstring (`services/do_kb/ingest.py:130-141`) — Qdrant is removed. **No flag tracks the Neo4j outcome at all** — a Neo4j-failed doc is indistinguishable from a healthy one. Recovery is operator-invoked CLI only (`backfill_do_kb.py`, `repair_kg.py`); the beat schedule contains no reconciler.

- **Remediation:** per-satellite status columns (KB already has `do_kb_index_status`; Neo4j needs one) + one scheduled beat task re-driving failed/missing syncs. The backfill CLI already contains the idempotent core.

### D2 — Neo4j never participates in document lifecycle; deletes orphan graph entities permanently

Document delete soft-deletes Postgres `Entity` rows and unsyncs DO KB, but no KG call exists anywhere in the delete path (`api/documents/documents.py:572-619`). `delete_entity` exists (`knowledge_graph_service.py:695-726`) but has no per-document caller. Graph tenancy is property-based with a warn-only unscoped fallback (`:121-156`) — one shared graph, isolation by writer convention.

- **Remediation:** best-effort `DETACH DELETE` by `source_document_id` in the delete path; fold Neo4j into the D1 reconciler.

### C1 — SSE event vocabulary is string literals in 4+ hand-maintained copies

Backend actually emits 12 event types (`token`, `tool_start`, `tool_end`, `rag_context`, `plan`, `reflection`, `trace`, `usage`, `heartbeat`, `confirmation`, `done`, `error`) as inline literals (`api/agent/streaming.py:602-1231`); the endpoint docstring documents 6 (`execute.py:441`); the frontend hand-mirrors 11 and silently drops unknowns (`frontend/src/services/agentChatService.ts:109-156`); the terminal-event set is hand-listed a third time in the resume path (`execute.py:584-588`). This is the exact class already paid for repeatedly (done-payload tools #1095, citations persistence, streamConfirm drops).

- **Remediation:** one backend `StrEnum` + payload types emitted through `_SeqEmitter` only; export/generate the TS union; one contract test asserting emit-sites ⊆ handled-set.

### C2 — Backend error envelope is never parsed by the main frontend client

The backend globally rewrites all errors into `{"error": {message, status_code, type}}` (`main.py:690-776`). The frontend's `handleErrorResponse` reads `errorData.detail || errorData.message` — keys the backend never sends — and falls back to `statusText` (`frontend/src/services/api-client.ts:532-536`). `apiErrorMessage.ts` is written for an axios error shape; axios isn't in `package.json`. The envelope *is* typed on the frontend (`types/api.ts:21-31`) and bypassed. Users see "Unprocessable Entity" instead of the server's actual message; `error.type` (auth_error, rate_limit…) is unusable for client behavior.

- **Remediation:** one shared `parseErrorBody()` (envelope first, `{detail}` second); delete the axios-shaped helper.

### C3 — v1/v2 is two coexisting products, not versioning

v2 is the thread-centric surface; v1 holds everything else *including the agent*, which has its own parallel thread API. One user feature (chat) spans both: streams via v1 agent SSE, persists/loads via v2 workspaces. Version prefixes are declared in `main.py` for some routers and baked into router files for others (`workspaces.py:62`, `multi_agent_search_v2.py:25`, `websocket_v2.py:89`, `document_upload.py:52`). An orphaned v2 SSE endpoint carries a **third** event dialect (`message_start`, `citation_inline`, `message_done`) whose only client is explicitly deprecated (`frontend/src/services/streamingService.ts:1-7`). The historical v1/v2 `create_message` drift (#1051/#1060) was a direct product of this design.

- **Remediation:** decide the story — v2 = "the threads domain" (stop treating it as a version; all prefixes in `main.py`) or a migration (delete the orphaned v2 stream + v1 agent-thread duplicates, `deprecated=True` on the losers).

---

## Medium-High findings

### D3 — Chat dual-store converges only when one side is empty

The split is reasonable (checkpointer = agent-context canon; `chat_messages` = display canon), but stitching is best-effort in both directions: the user-turn write before the run is warn-and-continue (`streaming.py:368-373`); the assistant write is a post-stream background task (`jobs.py:1058-1083`); convergence exists only as seed-when-empty (`build_thread_seed_messages`, `jobs.py:263-306`). No process detects divergence once both sides are non-empty — turns the agent "remembers" but the user can't see, permanently.

- **Remediation:** make the user-turn persist non-optional (it's one INSERT), or add a cheap count/last-cmid invariant check at turn start that triggers re-seed/repair.

### D4 — Schema authority split three ways; nothing prevents recurrence of head-divergence

Alembic (48 revisions) + environment-guarded `create_all` (`main.py:179-217`) + a dead parallel SQL migration system (`backend/src/migrations/`, referenced by nothing). Duplicate model families map the same tables (`ab_experiments` ×2, `document_quality_metrics` ×2, `metric_aggregations` ×2; 7 `extend_existing` escapes). Three merge revisions prove head-divergence has happened repeatedly; **zero `alembic` references in any CI workflow**. The `to_regclass` guard style institutionalizes baseline uncertainty instead of removing it.

- **Remediation:** two cheap CI checks — `alembic heads` == 1, and an offline upgrade smoke / `audit_schema_drift.py` run. Delete `src/migrations/` and the duplicate model families.

### C4 — Three-plus live message-creation paths

`POST /api/v2/threads/{id}/messages` (`threads.py:724`), the nested workspace path (`workspaces.py:754`), the flat `POST /api/v2/messages` (`workspaces.py:1971` — the only one the frontend calls), plus the agent's internal persistence path. Every invariant (org scoping, citations shape, `client_message_id` idempotency) is re-implemented per path; the create_message drift already realized this cost twice.

- **Remediation:** one `MessageService.create()`; delete the uncalled routes.

---

## Medium findings

| ID | Finding | Evidence | Direction |
|----|---------|----------|-----------|
| X4 | Celery doc-processing replay lacks a terminal/running ownership check — replays repeat extraction, KG writes, KB sync. Overlaps the open `kg_extract_entities` acks_late item (ingestion-hunt ledger) — coordinate, don't double-fix. | `backend/src/tasks/celery_app.py:47`, `document_processing_tasks.py:50` | Atomic processing-job claims + stage-level idempotency |
| X5 | Worker HPA scales on CPU/mem only — backlog-blind for I/O-bound LLM tasks; queue grows while utilization stays low. | `values-dev.yaml:243`, `templates/hpa.yaml:74` | Scale on queue depth/age |
| X6 | `networkPolicy.enabled=false` in the active chart (no NetworkPolicy rendered); Neo4j is a single StatefulSet replica on one RWO volume. | `values-dev.yaml:301`, `templates/neo4j-statefulset.yaml:11` | Enable policies before promotion; see D8 for the backup half |
| D5 | Retention modeled but unwired: LangGraph checkpoints never pruned anywhere; thread delete is a flag flip; synthetic traffic mints ~72 threads/day forever; `DataRetentionPolicy` + `DATA_RETENTION` task machinery exists with no beat entry; `analytics_events`/`audit_events`/`rag_queries`/etc. append-only. | `chat_service.py:567-582`, `synthetic_traffic.py:350,508-517`, `models/audit.py:236`, `analytics_processor.py:40,367,678` | One beat task: `adelete_thread` for deleted threads >N days; cap synthetic retention; point `_process_data_retention` at the append-only tables |
| D6 | Doc delete removes 1 of up-to-3 Spaces object classes: canonical KB `.txt` and figure PNGs are never deleted; the "later sweep" the comments promise doesn't exist. Retained user content post-delete is a quiet compliance issue. | `file_service.py:761-765,806`, `do_kb/ingest.py:110-112,285`, `figure_extraction_service.py:177-198`, `files.py:714` | Delete enumerates all object classes; build the referenced-vs-actual bucket diff as a scheduled task |
| D7 | Redis durability contract undecided per key family: broker + result backend on the same managed Valkey (queued tasks vanish on failover while `ProcessingJob` rows stay QUEUED forever — no stuck-job sweep); CLI token revocation lives only in Redis (flush = un-revoke). Job-store half merged into X1. | `celery_app.py:27-30`, `processing_tasks.py:1022-1024`, `core/cli_token_revocation.py`, `files.py:714` | Write the per-key-family durability contract; stuck-QUEUED sweeper; deny-on-miss or short TTLs for revocation |
| B5 | `api/agent/jobs.py` (1,787 loc) is an application service in a router: job store, thread resolution, project binding, message persistence, graph runner — persistence duplicating `ChatService.create_message` | `jobs.py:105-160,565,703-800,805,911,1142` | Extract `AgentRunService` into `services/agent/` |
| B6 | Tenancy enforced by per-query convention (1,614 `organization_id` sites / 114 files) while `middleware/multi_tenancy.py` ships an entire dead RLS toolkit with zero callers — falsely signaling the concern is handled centrally | `middleware/multi_tenancy.py:249-440` | Delete the dead helpers; long-term: scoped-query helper or PG RLS so scoping is opt-out |
| B7 | Celery task lazy-imports the in-process WS `ConnectionManager` from a route module — always empty in the worker process, so upload progress silently reaches no one; 16 `asyncio.run` sites in `tasks/` create a loop per call | `document_processing_tasks.py:42,78-103`, `document_upload.py:152-196` | Progress via Redis pub/sub → WS service; one `run_async` helper |
| B8 | Live `AsyncSession` + ORM `User` stuffed into LangGraph `configurable`; 14 sites open their own `AsyncSessionLocal()` precisely to dodge the shared session — two coexisting strategies chosen ad hoc per tool | `jobs.py:1270`, `streaming.py:452,996,1122`, `tools.py:106-115`, `tools_impl.py:1624,1697` | Only ids in `configurable`; one `tool_session()` context manager |
| C5 | 541 endpoints, zero OpenAPI consumption; every FE type hand-mirrored (root mechanism behind C1/C2/C6/C7) | `frontend/package.json`, `frontend/src/types/*` | Check in `openapi.json` + `openapi-typescript` for high-churn domains; CI schema diff = free breaking-change detection |
| C6 | Document-status vocabulary translated per-router inline in both directions (two production bugs already: filter 500, false "queued"); FE redefines it 4+ ways and consumers hedge `indexed\|\|completed` | `files.py:162-168,217-230,715`, `documents.py:352`, `frontend/src/types/schemas.ts:69,157` | One public `ApiDocumentStatus` StrEnum with `from_db`/`to_db`, serialized in response models |
| C7 | Agent job status is an untyped `str`: backend writes 6 states including both `failed` and `error`; FE union types 4 — pollers mis-handle exactly the failure paths | `execute.py:234-239`, `jobs.py:1305-1773`, `agentChatService.ts:305` | One `JobStatus` StrEnum (collapse `error` into `failed`), mirrored once with an exhaustive `isTerminal()` |
| C8 | No transport rule: 4 live FE WebSocket client stacks + 1 dead one, backend mirrors with 3 WS routers, and the same progress data flows over REST polling and WS simultaneously (bug-level specifics already in the WS-deep-audit ledger; this is the architecture-level cause: no single realtime-channel abstraction) | `frontend/src/services/websocket*.ts`, `realtimeWebSocketService.ts`, `main.py:573-575`, `enhancedDocumentService.ts:437-605` | Write the transport rule down; converge on one WS client with topic subscription; delete `enhancedWebSocket.ts` |

---

## Low findings

| ID | Finding | Evidence | Direction |
|----|---------|----------|-----------|
| D8 | Neo4j `backup:` values block consumed by no template — no actual backup; DR stance undecided (PVC loss = silent total KG loss; rebuild via `repair_kg.py` is LLM-cost-expensive and undocumented as the plan) | `values.yaml:520`, no backup template in `templates/` | Either implement the CronJob the values describe, or delete the block and document "KG is derived data; DR = repair_kg.py" as an explicit decision |
| B9 | Encapsulation breaches: routes mutate service-singleton internals (`agent_metrics`, `query_history.clear()`); tools call service privates; 11 services raise `HTTPException` | `multi_agent_search_v2.py:458-501`, `tools_impl.py:1634`, `project_service.py:129,195,207` | Domain exceptions mapped to HTTP at the edge (handler already exists) |
| B10 | Fat routes bypass the service layer inconsistently within the same file (workspaces.py: 2,465 loc / 123 direct query sites); quota compensation split across layers (bulk-delete revert inline in route, upload compensation in service) | `workspaces.py`, `documents.py:1124`, `enhanced_file_service.py:1189` | Rule of thumb "writes go through a service"; adopt on touch |
| C9 | `X-Organization-ID` header sent by every client, read by nothing (tenancy correctly derives from JWT) — dead contract weight implying client-selectable tenancy that doesn't exist | `api-client.ts:123,420`, `core/dependencies.py:75-85`, `core/config.py:86` | Delete from clients + CORS allowlist, or document as deliberately ignored |
| C10 | Pagination is per-router folklore: `offset/limit` vs `page/size` (unvalidated) vs `limit`-only; response shapes vary across 15 files; the count-filter-drift gotcha exists because every router hand-rolls its count | `files.py:192-193`, `workspaces.py:410`, `execute.py:655` | One `PaginationParams` dependency + `Page[T]` generic; adopt on touch |
| X7 | Vercel `/api/health` route calls `localhost:8000` — meaningless as an operational signal in the Vercel runtime | `frontend/app/api/health/route.ts:11` | Point at the real backend URL or remove |

---

## Healthy patterns (keep these)

Independently confirmed by multiple passes:

- **Resumable SSE is genuinely first-class**: sequence-numbered frames via `_SeqEmitter`, best-effort Redis tee degrading to plain streaming, drain-on-disconnect buffering the full turn, ownership-guarded resume endpoint, one shared chunk-boundary-safe `consumeSse` loop on the frontend serving stream/confirm/resume.
- **CAS-guarded HITL confirm** (`awaiting_confirmation → running` atomic transition) — destructive tools can't double-fire across workers.
- **Write-ordering rules are learned and encoded**: upload = object-before-DB with compensating delete; delete = DB-before-object; the two orders are correctly opposite and the reasoning is documented in code.
- **Idempotency discipline in chat writes**: partial unique indexes on `client_message_id` with `ON CONFLICT` upserts; deterministic uuid5 seed ids so checkpoint reseeds converge.
- **DO KB backfill is real reconciliation machinery** (resumable cursor, idempotent skip, `reprovision_org` recovery) — it just isn't scheduled.
- **Validated enums for sort/filter** (`shared/enums.py`) — injection prevention by construction.
- **Consistent auth posture across REST and SSE**: Bearer via fetch-POST everywhere, uniform 404-not-403 IDOR posture on thread-scoped reads.
- **Structured error envelope with severity-aware logging** — the backend design is right; only the frontend parse (C2) squanders it.
- **Celery worker hygiene**: late ACKs, low prefetch, child recycling; non-root containers, PDBs, resource limits, TLS ingress in the active chart.

---

## Cross-cutting conclusion

The platform's consistency model is *"Postgres is truth, everything else is best-effort with a comment"* — defensible, except the second half ("and a reconciler repairs the satellites") was never built. Nearly every data finding (D1, D2, D5, D6, D7) is a place where a compensation comment references a sweep that exists only as a manual CLI or not at all. One scheduled reconciler driven from Postgres status columns closes most of the data-architecture surface of this audit.

Likewise, the contract findings (C1, C2, C6, C7) are all instances of one missing mechanism (no consumed schema), and the reliability findings (X1–X3) are all instances of one stale assumption (durability rules gated on environment names that were retired in #442).

Fix the four mechanisms, not the 35 symptoms.

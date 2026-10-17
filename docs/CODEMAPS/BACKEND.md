# NOUS — Backend CODEMAP

> FastAPI app: entry point, package layout, top endpoints, top services, how to add a domain.
> Last generated: 2026-04-26; package layout, services and endpoint tables corrected 2026-10-17 against `ls backend/src` and `backend/openapi.json`. For exact paths use `backend/openapi.json`.

## Entry point

`backend/src/main.py` (about 970 LOC) — creates the FastAPI app, wires all routers, middleware, and startup events.

## `src/` package layout

```
src/
  main.py             FastAPI app + router wiring (see backend/src/api/README.md)
  api/                Route handlers (transport only); one subpackage or module per area
                      agent, analytics, arxiv, auth, connectors, diagnostics, documents,
                      evidence, infrastructure, integrations, quality, realtime, research,
                      research_engine, search, security, threads, artifacts.py, harness.py
  services/           Business logic (domain-per-folder)
                      ab_testing, agent, analytics, artifacts, arxiv, auth, config, connectors,
                      core, diagnostics, do_kb, documents, embedding, evaluation, evidence,
                      harness, infrastructure, ingestion, integrations, knowledge_graph, models,
                      processing, project_skills, quality, research, research_decisions,
                      research_engine, sandbox, search, security, threads, websocket
  models/             SQLAlchemy ORM models (flat files; see models/README.md)
  schemas/            Pydantic schemas
  core/               Settings, database, security, resilience, caching (see core/README.md)
  auth/, security/, privacy/, storage/, cache/, health/, middleware/, exceptions/, utils/
  tasks/              Celery tasks (see tasks/README.md)
  observability/      Metrics, tracing, logging, Sentry (see observability/README.md)
  websocket/          WebSocket connection manager
  cli/                CLI helpers
  shared/             Enums and constants (enums.py has SQL-safe sort/filter values)
```

## Top endpoints (path groups in `backend/openapi.json`)

| Prefix | Purpose |
|---|---|
| `/api/v1/agent/` | Execute, stream, confirm HITL |
| `/api/v1/documents/`, `/files`, `/processing` | Upload, status, processing, integrity, tables, figures |
| `/api/v2/workspaces`, `/threads`, `/conversations`, `/messages`, `/collections`, `/search` | Workspace and thread API |
| `/api/v1/search/`, `/search-quality`, `/knowledge-graph/` | Full-text search, quality, graph queries |
| `/api/v1/projects`, `/citations`, `/research` | Research projects, drafts, claims, extraction matrix |
| `/api/v1/research-engine/` | Structured research engine (largest group) |
| `/api/v1/chat`, `/export` | Chat completions, thread export |
| `/api/v1/analytics/` | Behavior, quality, performance, KG analytics |
| `/api/v1/artifacts`, `/harness`, `/integrations` | Artifacts, harness, external integrations |
| `/api/v1/evidence`, `/security`, `/rbac`, `/evaluation`, `/workers` | Evidence meter, encryption/compliance, RBAC, evaluation, workers |
| `/api/v1/auth`, `/cli-auth`, `/api-keys` | Auth |
| `/api/v2/realtime`, `/api/v2/ws` | Realtime status and WebSocket admin |
| `/health`, `/health/readiness` | Liveness and readiness probes |

## Top services

| Module | Key class | Purpose |
|---|---|---|
| `services/agent/_builders.py` (`graph.py` facade) | `create_graph()` | LangGraph StateGraph; see AGENT.md |
| `services/knowledge_graph/` | `KnowledgeGraphService` | Neo4j CRUD and traversal (about 2,700 LOC) |
| `services/search/` | `FullTextSearchService`, `HybridSearchService` | PostgreSQL full-text search; see the module README |
| `services/documents/` | document services | Ingestion pipeline coordination |
| `services/threads/` | `ThreadService`, `WorkspaceService`, `workspace_access.py` | Conversation and workspace logic |
| `services/research/` | project, claims, review and release services | See the module README |
| `services/embedding/` | `EmbeddingService` | Embedding calls (used by the flag-gated pgvector path) |
| `services/evaluation/` | evaluation services | QA evaluation runs |

## Database migrations

`backend/alembic/` is the single canonical migration system.  
Run: `cd backend && alembic upgrade head`; CI enforces a single head and revision ids of at most 32 characters (`python ../scripts/ci/check_alembic.py`; see `backend/alembic/README.md`).
Config: `backend/alembic.ini`, env: `backend/alembic/env.py`

## Adding a new domain

1. Create `src/api/<domain>/` with thin handlers that import the service (routers never commit).
2. Create `src/services/<domain>/` with the business logic; the service owns exactly one transaction boundary per method.
3. Add ORM models to `src/models/<domain>.py` (until domain split is complete).
4. Add Pydantic schemas to `src/schemas/<domain>.py`.
5. Register router in `src/main.py`: `app.include_router(<domain>_router, prefix="/api/v1")`.
6. Add Alembic migration: `alembic revision --autogenerate -m "add_<domain>"`.
7. Add tests under `tests/unit/api/<domain>/` and `tests/integration/<domain>/`.

## Key gotchas

- Never use raw strings for SQL sort/filter — use `src/shared/enums.py` validated enums.
- Don't name query params same as imported modules (e.g. `status` shadows `fastapi.status`).
- Agent checkpoint URL must be `postgresql://` (psycopg v3), not `postgresql+asyncpg://`.
- Destructive tools trigger `interrupt()` — pair every tool-call AIMessage with a ToolMessage.

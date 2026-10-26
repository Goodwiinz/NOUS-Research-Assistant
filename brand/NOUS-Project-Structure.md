# NOUS — Project Structure

> **Non-authoritative (2026-10-17): this hand-written structure overview is a dated snapshot. It lists Qdrant, which is removed (live retrieval is PostgreSQL full-text), and counts that have drifted. Check the code and the [code maps](../docs/CODEMAPS/README.md) instead.**

**Multimodal Intelligence Platform · Next.js 15 + FastAPI + PostgreSQL/Qdrant/Neo4j/Redis**

---

## At a Glance

| Metric | Value |
|--------|-------|
| Backend Python | 222K lines · 502 files |
| Frontend TypeScript | 43K lines · 593 files |
| Test Files | 609 files |
| API Endpoints | ~350 across 13 domains |
| Databases | 4 (PostgreSQL, Qdrant, Neo4j, Redis) |
| Docker Compose Files | 14 configurations |
| Utility Scripts | 80+ |

---

## Repository Layout

```
nous/
├── backend/                    # FastAPI application (Python)
│   ├── src/
│   │   ├── api/               # 13 route groups (agent, arxiv, auth, documents, search ...)
│   │   ├── services/          # Business logic (agent, search, ingestion, research ...)
│   │   ├── models/            # SQLAlchemy ORM models
│   │   ├── schemas/           # Pydantic request/response schemas
│   │   ├── core/              # AI prompts, DB optimizations
│   │   ├── middleware/        # Request processing pipeline
│   │   ├── auth/              # JWT + API key authentication
│   │   ├── security/          # RBAC, encryption, compliance
│   │   ├── cache/             # Redis caching layer
│   │   ├── resilience/        # Circuit breakers for Neo4j/Qdrant/Cohere
│   │   ├── observability/     # LangSmith + Prometheus metrics
│   │   ├── websocket/         # Realtime connections
│   │   ├── tasks/             # Celery async tasks
│   │   └── monitoring/        # Health checks + dashboards
│   ├── alembic/               # Python-side migrations
│   └── langgraph.json         # Agent graph entry point config
│
├── frontend/                   # Next.js 15 application (TypeScript)
│   └── src/
│       ├── app/               # Next.js App Router (pages + layouts)
│       ├── components/        # 30+ feature directories (see domain map)
│       ├── hooks/             # Custom React hooks (upload, analytics ...)
│       ├── stores/            # Zustand state management
│       ├── services/          # API client + service layer
│       ├── contexts/          # React context providers
│       ├── providers/         # App-level providers
│       ├── page-components/   # Page-level composition (12 domains)
│       ├── types/             # TypeScript type definitions
│       ├── lib/               # Shared utilities
│       ├── theme/             # NOUS design tokens
│       └── styles/            # Global CSS
│
├── database/                   # SQL database management
│   ├── migrations/            # 20+ numbered SQL migrations
│   ├── procedures/            # Stored procedures
│   ├── triggers/              # Event triggers
│   ├── views/                 # Database views
│   ├── indexing/              # Performance indexes
│   ├── optimization/          # Query optimization
│   ├── security/              # Row-level security + policies
│   └── monitoring/            # DB health checks
│
├── tests/                      # Test infrastructure
│   ├── unit/                  # Isolated unit tests (@unit)
│   ├── integration/           # Service integration (@integration)
│   ├── e2e/                   # Playwright browser tests (@e2e)
│   ├── contract/              # API + WebSocket contracts
│   ├── performance/           # Perf benchmarks (@performance)
│   ├── security/              # Security tests
│   ├── load/                  # Load testing
│   └── factories/             # Test data factories
│
├── config/                     # Configuration
│   ├── docker-compose/        # 14 compose files (dev, prod, staging, CI, Azure ...)
│   ├── environments/          # Env-specific configs
│   └── security/              # Security policies
│
├── infrastructure/             # Deployment configs
├── k8s/                        # Kubernetes manifests
├── terraform/                  # Infrastructure as Code
├── nginx/                      # Reverse proxy config
├── monitoring/                 # Prometheus + Grafana
├── security/                   # Security scanning + policies
├── scripts/                    # 80+ utility scripts
│
├── brand/                      # Brand assets, guidelines, philosophy
├── docs/                       # Technical documentation
├── specs/                      # 6 feature specifications
├── notebooks/                  # Jupyter exploration + analysis
├── memory/                     # Claude context system (syncs to Obsidian)
└── daily-logs/                 # Development daily logs
```

---

## Backend Architecture

### Service Layers

| Layer | Purpose | Key Details |
|-------|---------|-------------|
| `api/` | Route handlers | Request validation, response formatting — 78 files, 13 domains |
| `services/` | Business logic | Agent, search, ingestion, research, embedding, analytics — 26 domains |
| `models/` | ORM | SQLAlchemy table definitions |
| `schemas/` | Validation | Pydantic request/response contracts |
| `core/` | AI + DB | Prompt templates, database optimization utilities |
| `middleware/` | Pipeline | Auth, CORS, rate limiting, logging |
| `resilience/` | Reliability | Circuit breakers for Neo4j, Qdrant, Cohere |
| `observability/` | Monitoring | LangSmith tracing + Prometheus metrics |

### Agent System (LangGraph)

The agent system lives in `backend/src/services/agent/` and is the core intelligence layer:

| File | Purpose |
|------|---------|
| `graph.py` | Main StateGraph with intent routing |
| `state.py` | Agent state definition |
| `tools.py` | 12 tool implementations |
| `memory.py` | Conversation memory management |
| `checkpointer.py` | PostgreSQL persistence (AsyncPostgresSaver) |
| `observability.py` | LangSmith + Prometheus integration |
| `visualization.py` | Mermaid graph export |
| `subgraphs/research_agent.py` | Research subgraph (arXiv, docs, projects) |
| `subgraphs/writing_agent.py` | Writing subgraph (drafts, notes, summaries) |
| `subgraphs/data_agent.py` | Data subgraph (entities, KG queries) |

**Agent Flow:** `rag_node → intent_classifier → memory_retrieval → route_by_intent → tool_node → memory_save → END`

### Backend API Domains (13)

| # | Domain | Endpoints | Scope |
|---|--------|-----------|-------|
| 01 | Agent | 10 | LangGraph execution, SSE streaming, HITL |
| 02 | Auth | 31 | JWT, API keys, multi-tenant orgs |
| 03 | Documents | 40 | Upload, processing, extraction, integrity |
| 04 | ArXiv | 37 | Paper search, bulk ingest, LLM extraction, KG |
| 05 | Search | 60 | Hybrid search, vectors, knowledge graph, multi-agent |
| 06 | Research | 52 | Projects, citations, drafts, writer, tone, export |
| 07 | Research Engine | 14 | Blueprints, structured runs, step execution |
| 08 | Analytics | 44 | Dashboards, metrics, KPIs, reports, graph analytics |
| 09 | Quality | 52 | A/B testing, quality metrics, user behavior |
| 10 | Threads | 51 | Workspaces, conversations, threads, messages |
| 11 | Realtime | 19 | WebSocket v2, document status, quality metrics |
| 12 | Security | 23 | RBAC, encryption, audit trail, compliance |
| 13 | Infrastructure | 24 | Celery workers, RAG evaluation, diagnostics |

---

## Frontend Architecture

### Component Domains (30+)

**Core Interaction:**
`agent-chat/` · `chat/` · `llm-chat/` · `chat-widget/`

**Research:**
`research/` · `research-engine/` · `arxiv/` · `citations/` · `evidence/`

**Data Views:**
`documents/` · `entities/` · `graph/` · `search/` · `upload/`

**Platform:**
`dashboard/` · `analytics/` · `monitoring/` · `performance/` · `diagnostics/` · `evaluation/`

**UI Infrastructure:**
`ui/` (shadcn) · `layout/` · `auth/` · `security/` · `export/` · `preview/` · `realtime/`

### Page-Level Composition (12 domains)

`search-analytics/` · `settings/` · `ab-testing/` · `auth/` · `graph/` · `search/` · `evaluation/` · `documents/` · `monitoring/` · `error/` · `analytics/`

### State Management

Zustand stores in `stores/` and `store/`, React contexts in `contexts/`, app-level providers in `providers/`.

---

## Data Layer

| Database | Port | Purpose |
|----------|------|---------|
| PostgreSQL | 5432 | Relational data — users, documents, threads, auth. 20+ migrations, stored procedures, triggers, RLS |
| Qdrant | 6333 | Vector embeddings — document chunks, similarity scoring, hybrid search |
| Neo4j | 7687 | Knowledge graph — entity relationships, graph traversal, analytics |
| Redis | 6379 | Caching, sessions, rate limiting, Celery task broker |

---

## Infrastructure

### Docker Compose (14 configs)

`development` · `production` · `staging` · `ci` · `azure` · `observability` · `security` · `websocket` · `graph-services` · `services` · `backend` · `frontend`

### Deployment Stack

Kubernetes manifests (`k8s/`), Terraform IaC (`terraform/`), Nginx reverse proxy (`nginx/`), Prometheus + Grafana monitoring (`monitoring/`), GitHub Actions CI/CD (`.github/`).

### Test Infrastructure

| Type | Directory | Marker |
|------|-----------|--------|
| Unit | `tests/unit/` | `@unit` |
| Integration | `tests/integration/` | `@integration` |
| E2E | `tests/e2e/` | `@e2e` (Playwright) |
| Contract | `tests/contract/` | API + WebSocket |
| Performance | `tests/performance/` | `@performance` |
| Security | `tests/security/` | Security scanning |
| Load | `tests/load/` | Stress testing |
| Factories | `tests/factories/` | Test data generation |

---

## Service Connections

| Service | Port | URL |
|---------|------|-----|
| PostgreSQL | 5432 | `postgres:postgres@localhost` |
| Neo4j | 7687 | `bolt://localhost:7687` |
| Qdrant | 6333 | `http://localhost:6333` |
| Redis | 6379 | `redis://localhost:6379` |
| Backend | 8000 | `http://localhost:8000` |
| Frontend | 3000 | `http://localhost:3000` |

---

## Brand

| Element | Value |
|---------|-------|
| Name | NOUS (Greek: νοῦς — mind/intellect) |
| Primary Colors | Erebus `#0A0A0E`, Selene `#F7F7F5`, Sol `#D4A039` |
| Accent Colors | Helios `#E8B84A`, Apollo `#F5D680`, Aurum `#FDF6E3` |
| Fonts | Inter (headings/UI), Source Serif 4 (body), JetBrains Mono (code) |
| Assets | `brand/` — logos, guidelines, philosophy, product overview |

---

*NOUS · Multimodal Intelligence Platform · Structure Map · March 2026*

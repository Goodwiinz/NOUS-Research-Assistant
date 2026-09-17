# NOUS — Multimodal Intelligence Platform

**Codename:** NOUS (Greek: νοῦς — mind/intellect)
**Also called:** "the RAG system", "multimodal rag"
**Status:** Active, production-ready core, roadmap in progress
**Repo:** github.com/goodwiins/rag

## What It Is

Enterprise-grade multimodal RAG system that retrieves, reasons, and generates across documents, images, audio, and knowledge graphs. Built with Next.js 15 + FastAPI.

## Tech Stack

- **Frontend**: Next.js 15.1.3, React 18, TypeScript, shadcn/ui, Zustand, TanStack Query
- **Backend**: FastAPI, Python 3.11, Celery, structlog
- **Databases**: PostgreSQL (DO managed), Neo4j 5.26, Qdrant 1.13, Redis/Valkey (DO managed)
- **Storage**: DigitalOcean Spaces (S3-compatible) — bucket `rag-system-storage`, nyc3 region
- **Agent**: LangGraph StateGraph, 13 tools (incl. E2B execute_code), 4 subgraphs, AsyncPostgresSaver
- **AI**: OpenAI, Anthropic, Azure OpenAI, Cohere embed-v4.0 (multimodal, 1024d via Azure AI), Cohere reranking
- **Infra**: DOKS cluster (do-nyc3-rag-system-cluster), ArgoCD (auto-sync develop→dev), Helm charts

## Key Features

- LangGraph agent with intent routing + human-in-the-loop
- Hybrid search (vector + keyword + graph)
- arXiv integration, citation pipeline, bibliography export
- Literature review draft generation
- Knowledge graph (Neo4j) with entity extraction
- SSE streaming + WebSocket real-time
- Enterprise security (JWT, RBAC, audit logs, encryption)

## Brand

- Colors: Erebus #0A0A0E, Selene #F7F7F5, Sol #D4A039 (accent)
- Fonts: Inter (headings), Source Serif 4 (body), JetBrains Mono (code)
- Assets in `brand/` directory

## Roadmap (from K-Dense gap analysis)

| Phase   | Timeline    | Focus                                            |
| ------- | ----------- | ------------------------------------------------ |
| Phase 1 | Weeks 1-4   | Code execution sandbox + extended autonomy       |
| Phase 2 | Weeks 5-8   | Database connectors + file format parsers        |
| Phase 3 | Weeks 9-12  | Publication outputs + domain verticals           |
| Phase 4 | Weeks 13-16 | Multi-agent architecture + enterprise compliance |

## Key Decisions

- **Dev environment fully wired 2026-04-12:** Neo4j StatefulSet enabled (was DNS-failing), Qdrant StatefulSet created + enabled, DO Spaces S3 storage backend configured, Cohere embed-v4.0 multimodal embeddings integrated via Azure AI (OpenAI-compat endpoint), Celery worker + beat deployments created and enabled, entities page graceful degradation UX added.
- **Best practices audit 2026-04-13:** 33 issues found across S3/embedding/RAG/K8s. All P0-P2 items remediated: Cypher injection fixed (parameterized queries), S3 retries + presigned TTL, semantic chunking (sentence-boundary-aware), batch embedding error recovery, hash-embedding fallback removed, search cache invalidation on doc changes, token pre-validation, PDB enabled, HPA + NetworkPolicy templates added, search analytics persistence (new DB table), Qdrant backup CronJob (weekly), unified Celery app module. Remaining: Neo4j password rotation (needs sealed secrets operator).
- Brand name NOUS chosen March 2026 (Greek/planetary inspiration, clean & modern vibe)
- Gap analysis vs K-Dense completed March 2026 — 11 issues created in Linear
- Docs reorganized: 51 loose files sorted into 19 categorized subdirectories
- Daily sync scheduled (weekdays 9am) for GitHub + Linear + Obsidian memory
- GOO-187 (E2B sandbox) implementation started 2026-03-24 on `feature/sandboxed-code-execution`; pre-installs numpy, pandas, matplotlib, scipy, scikit-learn, seaborn; HITL confirmation required. **Moved to In Review 2026-03-27** — first gap analysis issue nearing completion. PR to develop still needed.
- PR #243 merged 2026-03-24: deterministic hybrid search, IDOR hardening, CI stabilization (GOO-198)
- Agent v2 implementation started 2026-03-26 on `feature/agent-v2`: 6/10 phases done (error recovery, LLM classifier, compactor, planner, reflection, persistent memory). 118 tests passing. Design doc at `docs/plans/2026-03-25-agent-v2-plan.md`.
- Critical API key auth DoS vulnerability auto-fixed by Jules bot (`fb672af`) on `feature/agent-v2` 2026-03-26
- Agent v2 repo housekeeping 2026-03-27: eval task suites, lazy import fix, backend test/config reorganization, root cleanup. Jules bot fixed EntityList IconButton a11y.
- Agent v2 integration tests added 2026-03-31: 23 tests across 4 files (`8ccea48`). CI unblocked with encryption key init.
- OOM stability fixes 2026-03-31: Skip GDS analytics (`6d7990e`), optimize KG relationship query (`f3f30a4`) to prevent container crashes.
- KG hardening 2026-03-31: Fix extraction to create both entities and relationships (`c0bb3b9`), harden serialization (`64314fb`), Unicode SVG export (`d0682c5`).
- Tailwind v3 PostCSS plugin restored 2026-03-31 (`093b9f8`) — may resolve GOO-199 CI blocker.
- Security: Sentinel bot fixed CRITICAL API key prefix lookup DoS vulnerability (`baf512d`) 2026-04-03. Hardcoded Azure/Cohere keys removed from test scripts (`acc3782`).
- A11y: Palette bot refactored EntityList IconButton (`88f0feb`), aria-label added to avatar buttons (`494e039`) 2026-04-02.
- Agent v2 branch now 33 commits ahead of `develop`. GOO-187 still In Review (7+ days, PR overdue).
- **PR #295 merged 2026-04-10:** `feature/agent-v2` merged into `develop` — resolves 33-commit branch divergence. Agent v2 (error recovery, LLM classifier, compactor, planner, reflection, persistent memory) now on `develop`.
- **PR #296 merged 2026-04-10:** Supabase-only auth consolidation — backend JWT verification, frontend SSR auth, WebSocket JWT migration, CLI auth export, ArgoCD config updates. Major auth infrastructure change.
- Post-merge follow-up 2026-04-10: CI fixes (Docker build workflow for DO registry, backend service name in BACKEND_URL build-arg, version tagging), Next.js middleware for Supabase SSR cookie refresh, frontend public endpoint resolution fix, test fixes (stale assertions, deleted auth endpoints, deterministic mtime ordering). Jules bot fixed frontend lint errors and EntityGraph IconButton a11y.
- `develop` hardening batch 2026-04-13: `e5e5364` parameterized Cypher queries to close injection risk and hardened S3 handling with retries plus presigned URL TTL controls. `219c6ef` enabled Celery workers in local/dev and consolidated worker config into a unified Celery app module.
- Ingestion + ops maturity batch 2026-04-13: `8108c39` added semantic chunking, batch error recovery, and a PodDisruptionBudget. `97b4c7f` removed the hash embedding fallback, added token pre-validation, cache invalidation on document changes, and introduced HPA + NetworkPolicy templates. `bcef893` persisted search analytics and added a weekly Qdrant backup CronJob.

## Connections

| Service    | Port |
| ---------- | ---- |
| Frontend   | 3000 |
| Backend    | 8000 |
| PostgreSQL | 5432 |
| Neo4j      | 7687 |
| Qdrant     | 6333 |
| Redis      | 6379 |

## Dev Environment (DOKS)

| Component     | Status     | Details                                       |
| ------------- | ---------- | --------------------------------------------- |
| Frontend      | Running    | `dev-app.goodwiinz.tech`, Next.js rewrite proxy |
| Backend       | Running    | `dev-api.goodwiinz.tech`, FastAPI               |
| PostgreSQL    | External   | DO managed, `multimodal_rag` database         |
| Neo4j         | In-cluster | StatefulSet, 5Gi, 128m heap                   |
| Qdrant        | In-cluster | StatefulSet, 5Gi, weekly backup CronJob       |
| Redis/Valkey  | External   | DO managed, Celery broker + cache             |
| Celery Worker | Running    | 1 replica, concurrency=2, all queues          |
| Celery Beat   | Running    | 1 replica, scheduled cleanup + metrics        |
| Storage       | S3         | DO Spaces `rag-system-storage` (nyc3)         |
| Embeddings    | Cohere     | embed-v4.0 via Azure AI, 1024d                |

## Dev Users

- Admin: admin@multimodal-rag.com / admin123
- Demo: demo@multimodal-rag.com / demo123

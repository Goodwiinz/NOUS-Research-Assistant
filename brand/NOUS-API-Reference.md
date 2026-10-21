# NOUS — Backend API Reference

> **Non-authoritative (2026-10-17): this hand-written endpoint inventory is a dated snapshot and has drifted. The source of truth for routes and schemas is the FastAPI/Pydantic code and the generated `backend/openapi.json`; see [API contracts](../docs/engineering/api-contracts.md).**

**13 Route Domains · ~350 Endpoints · FastAPI · Base URL: `http://localhost:8000`**

---

## Summary

| # | Domain | Prefix | Endpoints | Files |
|---|--------|--------|-----------|-------|
| 01 | Agent | `/api/v1/agent` | 10 | 1 |
| 02 | Auth | `/api/v1/auth` · `/api/v1/api-keys` · `/api/v1/tenants` | 31 | 3 |
| 03 | Documents | `/api/v1/documents` · `/api/v1/files` · `/api/v1/upload` · `/api/v1/processing` | 40 | 6 |
| 04 | ArXiv | `/api/v1/arxiv` | 37 | 9 |
| 05 | Search | `/api/v1/search` · `/api/v1/vectors` · `/api/v1/knowledge-graph` | 60 | 6 |
| 06 | Research | `/api/v1/research` | 52 | 10 |
| 07 | Research Engine | `/api/v1/research-engine` | 14 | 4 |
| 08 | Analytics | `/api/v1/analytics` | 44 | 6 |
| 09 | Quality | `/api/v1/analytics/quality` · `/api/v1/ab-testing` | 52 | 5 |
| 10 | Threads | `/api/v1/threads` · `/api/v1/workspaces` · `/api/v1/conversations` | 51 | 5 |
| 11 | Realtime | `/api/v1/realtime` · `/ws` | 19 | 4 |
| 12 | Security | `/api/v1/security` · `/api/v1/rbac` | 23 | 3 |
| 13 | Infrastructure | `/api/v1/workers` · `/api/v1/evaluation` · `/api/v1/diagnostics` | 24 | 3 |

---

## 01 — Agent

**Prefix:** `/api/v1/agent`
**File:** `execute.py` (2200+ lines)

LangGraph-powered AI agent with intent routing to 4 specialized subgraphs (research, writing, data, general). Supports both async job-based and SSE streaming execution. Human-in-the-loop interrupts for destructive tools (ingest, create_note, create_draft). Thread persistence via PostgreSQL checkpointing.

**Key Features:** LangGraph StateGraph, SSE Streaming, Human-in-the-Loop, AsyncPostgresSaver, 12 Tools, Intent Routing, Mermaid Visualization

### Execution

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/execute` | Async job-based execution (returns job_id) |
| `GET` | `/jobs/{job_id}` | Poll job status and results |
| `POST` | `/confirm/{job_id}` | Resume HITL interrupt (confirm/deny) |
| `POST` | `/stream` | SSE streaming (token, tool_start, tool_end, rag_context, done) |
| `POST` | `/stream/confirm` | Resume HITL via streaming |

### Threads & Visualization

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/threads` | List user's agent threads |
| `GET` | `/threads/{thread_id}/messages` | Get thread message history |
| `GET` | `/graph/mermaid` | Export agent graph as Mermaid diagram |
| `GET` | `/graph/trace/{thread_id}` | Get execution trace for thread |
| `GET` | `/health` | Agent system health check |

---

## 02 — Auth

**Prefix:** `/api/v1/auth` · `/api/v1/api-keys` · `/api/v1/tenants`
**Files:** `auth.py` · `api_keys.py` · `tenant_management.py`

Full authentication and authorization system with JWT tokens, API key management, and multi-tenant organization support. Includes user registration, password management, role-based user admin, and tenant isolation testing.

**Key Features:** JWT Authentication, API Keys, Multi-Tenant, Token Refresh, Role Management, Org Isolation

### Authentication (12)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/auth/register` | Create new user account |
| `POST` | `/auth/login` | Get JWT access + refresh tokens |
| `POST` | `/auth/refresh` | Refresh access token |
| `GET` | `/auth/me` | Current user profile |
| `GET` | `/auth/session` | Session info |
| `PUT` | `/auth/me` | Update user profile |
| `POST` | `/auth/change-password` | Change password |
| `POST` | `/auth/reset-password` | Password reset |
| `GET` | `/auth/users` | List all users (admin) |
| `PUT` | `/auth/users/{user_id}/role` | Update user role (admin) |
| `POST` | `/auth/users/{user_id}/deactivate` | Deactivate user (admin) |
| `GET` | `/auth/statistics` | Auth system stats |

### API Keys (8)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api-keys` | Create API key |
| `GET` | `/api-keys` | List API keys |
| `GET` | `/api-keys/{id}` | Get key details |
| `PATCH` | `/api-keys/{id}` | Update key |
| `DELETE` | `/api-keys/{id}` | Revoke key |
| `GET` | `/api-keys/{id}/usage` | Key usage metrics |
| `POST` | `/api-keys/{id}/regenerate` | Regenerate key |
| `GET` | `/api-keys/usage/summary` | Usage summary across all keys |

### Tenant Management (11)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/tenants/organizations` | Create organization |
| `GET` | `/tenants/organizations/{id}` | Get organization |
| `PUT` | `/tenants/organizations/{id}` | Update organization |
| `POST` | `/tenants/organizations/{id}/members` | Invite member |
| `GET` | `/tenants/organizations/{id}/members` | List members |
| `GET` | `/tenants/organizations/{id}/stats` | Org statistics |
| `GET` | `/tenants/organizations/{id}/usage` | Org usage |
| `GET` | `/tenants/organizations/{id}/settings` | Org settings |
| `GET` | `/tenants/current-tenant` | Current tenant context |
| `POST` | `/tenants/organizations/{id}/test-isolation` | Test data isolation |
| `POST` | `/auth/cleanup` | Cleanup expired sessions |

---

## 03 — Documents

**Prefix:** `/api/v1/documents` · `/api/v1/files` · `/api/v1/upload` · `/api/v1/processing`
**Files:** `documents.py` · `files.py` · `document_upload.py` · `processing.py` · `integrity.py` · `table_extraction.py`

Complete document lifecycle management — upload, processing, chunking, table extraction, integrity checking, and quality assessment. Supports single and batch uploads, progress tracking, reprocessing, and bulk operations. Includes an AI-powered integrity detector.

**Key Features:** File Upload, Batch Processing, Table Extraction, Quality Assessment, Integrity Check, Progress Tracking, Queue Management

### Core Document Operations (11)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/documents` | List documents (paginated, filterable) |
| `GET` | `/documents/{id}` | Get document detail |
| `DELETE` | `/documents/{id}` | Delete document |
| `GET` | `/documents/{id}/entities` | Get document entities |
| `GET` | `/documents/{id}/status` | Get processing status |
| `POST` | `/documents/{id}/reprocess` | Reprocess document |
| `POST` | `/documents/bulk-delete` | Bulk delete documents |
| `POST` | `/documents/{id}/integrity-check` | AI integrity detection (202) |
| `GET` | `/documents/{id}/integrity-status` | Integrity check status |
| `GET` | `/documents/{id}/tables` | Extract tables |
| `POST` | `/documents/{id}/extract-region` | Extract specific region |

### File Management (12)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/files/upload` | Upload file |
| `GET` | `/files` | List files |
| `GET` | `/files/{id}` | Get file details |
| `GET` | `/files/{id}/download` | Download file |
| `GET` | `/files/{id}/content` | Get file content |
| `GET` | `/files/{id}/metadata` | Get file metadata |
| `PUT` | `/files/{id}` | Update file |
| `DELETE` | `/files/{id}` | Delete file |
| `POST` | `/files/{id}/reprocess` | Reprocess file |
| `DELETE` | `/files/cancel/{upload_id}` | Cancel upload |
| `GET` | `/files/stats` | File statistics |
| `GET` | `/files/debug-auth` | Debug auth (dev) |

### Upload & Processing (17)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/upload/single` | Upload single document with quality assessment |
| `POST` | `/upload/batch` | Batch upload |
| `GET` | `/upload/progress/{id}` | Upload progress |
| `GET` | `/upload/{id}/quality` | Quality assessment |
| `POST` | `/upload/{id}/rescan` | Rescan document |
| `DELETE` | `/upload/cancel/{id}` | Cancel upload |
| `POST` | `/processing/documents/{id}/process` | Start processing |
| `GET` | `/processing/documents/{id}/status` | Processing status |
| `GET` | `/processing/jobs` | List processing jobs |
| `GET` | `/processing/jobs/{id}` | Job status |
| `POST` | `/processing/batch` | Batch processing |
| `POST` | `/processing/retry` | Retry failed jobs |
| `POST` | `/processing/jobs/{id}/cancel` | Cancel job |
| `GET` | `/processing/queue/status` | Queue status |
| `DELETE` | `/processing/jobs/cleanup` | Cleanup old jobs |

---

## 04 — ArXiv

**Prefix:** `/api/v1/arxiv`
**Files:** `core.py` · `arxiv_bulk.py` · `arxiv_llm_bulk.py` · `arxiv_local.py` · `arxiv_local_simple.py` · `arxiv_local_batch.py` · `arxiv_extraction.py` · `arxiv_change_tracking.py` · `arxiv_knowledge_graph.py`

Full integration with the arXiv academic paper repository. Search, ingest, download, and process papers. Supports bulk ingestion with LLM-enhanced extraction, local paper processing, change tracking across categories, feature extraction, and knowledge graph construction of author networks and research trends.

**Key Features:** Paper Search, Bulk Ingestion, LLM Extraction, Knowledge Graph, Change Tracking, Author Networks, Trend Analysis, 5min Timeout

### Core (6)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/arxiv/search` | Search arXiv papers |
| `POST` | `/arxiv/ingest` | Ingest paper into NOUS |
| `POST` | `/arxiv/create-dataset` | Create dataset from papers |
| `GET` | `/arxiv/categories` | List arXiv categories |
| `GET` | `/arxiv/download/{paper_id}` | Download paper PDF |
| `GET` | `/arxiv/statistics` | Ingestion statistics |

### Bulk Ingestion (10)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/arxiv/bulk/start` | Start bulk ingestion job |
| `GET` | `/arxiv/bulk/status` | Bulk job status |
| `POST` | `/arxiv/bulk/stop` | Stop bulk job |
| `GET` | `/arxiv/bulk/stats` | Bulk stats |
| `POST` | `/arxiv/bulk/test-small-batch` | Test with small batch |
| `POST` | `/arxiv/llm-bulk/start` | LLM-enhanced bulk ingestion |
| `GET` | `/arxiv/llm-bulk/status` | LLM bulk status |
| `POST` | `/arxiv/llm-bulk/stop` | Stop LLM bulk |
| `GET` | `/arxiv/llm-bulk/cost-estimate` | Estimate LLM costs |
| `POST` | `/arxiv/llm-bulk/test-small-batch` | Test LLM batch |

### Local Papers & Extraction (8)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/arxiv/local-papers` | List local papers |
| `POST` | `/arxiv/extract-local-features` | Extract features locally |
| `POST` | `/arxiv/extract-local-features-simple` | Simple local extraction |
| `GET` | `/arxiv/stream-extraction` | SSE stream extraction |
| `GET` | `/arxiv/local-stats` | Local paper stats |
| `POST` | `/arxiv/process-batch` | Process local batch |
| `POST` | `/arxiv/extract-features` | Extract paper features (LLM) |
| `GET` | `/arxiv/extracted-features` | Get extracted features |

### Knowledge Graph (7)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/arxiv/kg/subgraph` | Build paper subgraph |
| `POST` | `/arxiv/kg/author-network` | Map author relationships |
| `POST` | `/arxiv/kg/analyze-trends` | Analyze research trends |
| `POST` | `/arxiv/kg/bulk-ingest` | Bulk KG ingest |
| `GET` | `/arxiv/kg/entity/{name}` | Get entity |
| `GET` | `/arxiv/kg/path/{source}/{target}` | Find entity path |
| `GET` | `/arxiv/kg/stats` | KG stats |

### Change Tracking (6)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/arxiv/changes/track-categories` | Track category changes |
| `GET` | `/arxiv/changes/track-all` | Track all categories |
| `GET` | `/arxiv/changes/history` | Change history |
| `GET` | `/arxiv/changes/stats` | Change stats |
| `POST` | `/arxiv/changes/cleanup` | Cleanup old changes |
| `POST` | `/arxiv/changes/force-sync` | Force sync |

---

## 05 — Search

**Prefix:** `/api/v1/search` · `/api/v1/vectors` · `/api/v1/knowledge-graph` · `/api/v1/multi-agent-search`
**Files:** `search.py` · `search_quality.py` · `vectors.py` · `knowledge_graph.py` · `multi_agent_search.py` · `multi_agent_search_v2.py`

Comprehensive search infrastructure spanning hybrid text+vector search, direct vector operations (Qdrant), a full knowledge graph API (Neo4j), and multi-agent search with benchmarking. Includes search quality evaluation, search history, suggestions, and feedback loops.

**Key Features:** Hybrid Search, Vector Embeddings, Knowledge Graph, Multi-Agent Search, Search Quality, Entity CRUD, Path Finding, Benchmarking, Index Management

### Core Search (16)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/search` | Standard text search |
| `POST` | `/search/hybrid` | Hybrid text + vector search |
| `POST` | `/search/authenticated/hybrid` | Authenticated hybrid search |
| `GET` | `/search/suggestions` | Search suggestions |
| `GET` | `/search/history` | Search history |
| `POST` | `/search/history` | Save to history |
| `DELETE` | `/search/history` | Clear history |
| `GET` | `/search/analytics` | Search analytics |
| `POST` | `/search/indexes/rebuild` | Rebuild indexes |
| `GET` | `/search/indexes` | List indexes |
| `POST` | `/search/documents/{id}/reindex` | Reindex document |
| `GET` | `/search/popular` | Popular queries |
| `GET` | `/search/similar` | Similar results |
| `GET` | `/search/related/{result_id}` | Related results |
| `POST` | `/search/feedback` | Submit result feedback |
| `GET` | `/search/health` | Search health |

### Search Quality (6)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/search/quality/evaluate` | Evaluate search quality |
| `POST` | `/search/quality/feedback` | Quality feedback |
| `GET` | `/search/quality/analytics` | Quality analytics |
| `POST` | `/search/quality/benchmark` | Run benchmark |
| `GET` | `/search/quality/metrics/types` | Metric types |
| `GET` | `/search/quality/health` | Quality health |

### Vectors / Qdrant (20)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/vectors/embeddings` | Generate embeddings |
| `POST` | `/vectors/embeddings/batch` | Batch embeddings |
| `POST` | `/vectors/search/documents` | Vector search (documents) |
| `POST` | `/vectors/search/entities` | Vector search (entities) |
| `GET` | `/vectors/collections` | List collections |
| `POST` | `/vectors/collections` | Create collection |
| `GET` | `/vectors/collections/{name}/stats` | Collection stats |
| `DELETE` | `/vectors/collections/{name}` | Delete collection |
| `POST` | `/vectors/collections/{name}/optimize` | Optimize collection |
| `POST` | `/vectors/index/document` | Index document vectors |
| `POST` | `/vectors/index/entity` | Index entity vectors |
| `DELETE` | `/vectors/documents/{id}/vectors` | Delete doc vectors |
| `DELETE` | `/vectors/entities/{id}/vectors` | Delete entity vectors |
| `POST` | `/vectors/reindex/organization/{id}` | Reindex org |
| `PUT` | `/vectors/documents/{id}/reindex` | Reindex document |
| `GET` | `/vectors/health` | Vector health |
| `GET` | `/vectors/model/info` | Model info |
| `POST` | `/vectors/model/test-quality` | Test embedding quality |

### Knowledge Graph / Neo4j (28)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/knowledge-graph/entities` | Create entity |
| `GET` | `/knowledge-graph/entities` | List entities (paginated) |
| `GET` | `/knowledge-graph/entities/{id}` | Get entity |
| `PUT` | `/knowledge-graph/entities/{id}` | Update entity |
| `DELETE` | `/knowledge-graph/entities/{id}` | Delete entity |
| `GET` | `/knowledge-graph/entities/search` | Search entities |
| `GET` | `/knowledge-graph/entities/{id}/documents` | Entity documents |
| `GET` | `/knowledge-graph/entities/{id}/related` | Related entities |
| `GET` | `/knowledge-graph/entities/{id}/neighborhood` | Entity neighborhood graph |
| `GET` | `/knowledge-graph/relationships` | List relationships |
| `POST` | `/knowledge-graph/relationships` | Create relationship |
| `GET` | `/knowledge-graph/relationships/{id}` | Get relationship |
| `DELETE` | `/knowledge-graph/relationships/{id}` | Delete relationship |
| `POST` | `/knowledge-graph/search` | Graph search |
| `GET` | `/knowledge-graph/paths/{src}/{tgt}` | Find shortest path |
| `POST` | `/knowledge-graph/batch` | Batch entity operations |
| `POST` | `/knowledge-graph/merge-jobs` | Merge duplicate entities (202) |
| `POST` | `/knowledge-graph/extraction-jobs` | Extraction jobs (202) |
| `POST` | `/knowledge-graph/documents/{id}/extract-entities` | Extract entities from doc |
| `GET` | `/knowledge-graph/documents/{id}/entities` | Document entities |
| `GET` | `/knowledge-graph/analytics` | Graph analytics |
| `GET` | `/knowledge-graph/health` | Graph health |
| `GET` | `/knowledge-graph/visualization/{id}` | Visualization data |
| `GET` | `/knowledge-graph/entity-types` | Entity types |
| `GET` | `/knowledge-graph/relationship-types` | Relationship types |
| `POST` | `/knowledge-graph/maintenance/fix-null-types` | Fix null types |
| `POST` | `/knowledge-graph/schema/reset` | Reset schema |

### Multi-Agent Search (14)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/multi-agent-search/search` | Multi-agent search (v1) |
| `GET` | `/multi-agent-search/status` | Agent status |
| `POST` | `/multi-agent-search/benchmark` | Run benchmark |
| `GET` | `/multi-agent-search/agents/types` | Agent types |
| `GET` | `/multi-agent-search/health` | Health |
| `GET` | `/multi-agent-search/performance` | Performance metrics |
| `POST` | `/multi-agent-search/compare` | Compare strategies |
| `POST` | `/multi-agent-search/v2/search` | Multi-agent search (v2) |
| `GET` | `/multi-agent-search/v2/status` | V2 status |
| `POST` | `/multi-agent-search/v2/workflow-recommendations` | Workflow recommendations |
| `POST` | `/multi-agent-search/v2/benchmark` | V2 benchmark |
| `GET` | `/multi-agent-search/v2/performance-report` | Performance report |
| `POST` | `/multi-agent-search/v2/reset-metrics` | Reset metrics |
| `GET` | `/multi-agent-search/v2/query-history` | Query history |

---

## 06 — Research

**Prefix:** `/api/v1/research`
**Files:** `projects.py` · `citations.py` · `drafts.py` · `chat.py` · `project_chat.py` · `writer.py` · `tone_engine.py` · `extraction_matrix.py` · `pipeline.py` · `export.py`

Full research assistant workspace — project management with documents and notes, citation management with relationship graphs, draft generation and comparison, scholarly tone engine, extraction matrices, AI writing assistant, research pipeline wizard, chat completions with RAG context, and multi-format export.

**Key Features:** Projects, Citations, Drafts, AI Writer, Tone Engine, Extraction Matrix, Pipeline Wizard, Export (BibTeX, PDF, DOCX), Chat + RAG, Project Chat

### Projects (17)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/research/projects` | List projects |
| `POST` | `/research/projects` | Create project |
| `GET` | `/research/projects/{id}` | Get project detail |
| `PATCH` | `/research/projects/{id}` | Update project |
| `DELETE` | `/research/projects/{id}` | Delete project |
| `GET` | `/research/projects/{id}/documents` | List project documents |
| `POST` | `/research/projects/{id}/documents` | Add document to project |
| `DELETE` | `/research/projects/{id}/documents/{doc_id}` | Remove document |
| `GET` | `/research/projects/{id}/notes` | List notes |
| `POST` | `/research/projects/{id}/notes` | Create note |
| `GET` | `/research/projects/{id}/notes/{nid}` | Get note |
| `PATCH` | `/research/projects/{id}/notes/{nid}` | Update note |
| `DELETE` | `/research/projects/{id}/notes/{nid}` | Delete note |
| `POST` | `/research/projects/{id}/notes/{nid}/pin` | Pin note |
| `GET` | `/research/projects/{id}/bibliography` | Get bibliography |

### Citations (10)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/research/citations` | Create citation |
| `GET` | `/research/citations` | List citations |
| `GET` | `/research/citations/{id}` | Get citation |
| `POST` | `/research/citations/{id}/verify` | Verify citation |
| `POST` | `/research/citations/lookup` | Lookup by DOI/URL |
| `POST` | `/research/citations/export` | Export (BibTeX/RIS/JSON) |
| `GET` | `/research/citations/relationships` | Citation relationships |
| `POST` | `/research/citations/relationships` | Create relationship |
| `GET` | `/research/citations/graph` | Citation graph |
| `GET` | `/research/citations/graph/node/{id}` | Citation graph node |

### Drafts (12)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/research/drafts` | Generate draft (async, 202) |
| `GET` | `/research/drafts` | List drafts |
| `GET` | `/research/drafts/current` | Current draft |
| `GET` | `/research/drafts/{id}` | Get draft |
| `DELETE` | `/research/drafts/{id}` | Delete draft |
| `GET` | `/research/drafts/{id}/citations` | Draft citations |
| `GET` | `/research/drafts/compare` | Compare two drafts |
| `POST` | `/research/drafts/{id}/export` | Export draft |
| `GET` | `/research/drafts/status` | Generation status |
| `GET` | `/research/drafts/status/{task_id}` | Task status |
| `POST` | `/research/drafts/cancel` | Cancel generation |
| `POST` | `/research/drafts/cancel/{task_id}` | Cancel specific task |

### Chat (8)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/research/chat/completions` | Chat with RAG context |
| `GET` | `/research/chat/health` | Chat health |
| `GET` | `/research/chat/models` | Available models |
| `POST` | `/research/chat/suggestions` | Context-aware suggestions |
| `GET` | `/research/chat/history` | Chat history |
| `POST` | `/research/chat/save` | Save chat |
| `POST` | `/research/projects/{id}/chat/start` | Start project-scoped chat |
| `POST` | `/research/projects/{id}/chat/link` | Link thread to project |

### Project Chat (5)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/research/projects/{id}/chat/threads` | Project chat threads |
| `DELETE` | `/research/projects/{id}/chat/threads/{tid}` | Unlink thread |
| `POST` | `/research/projects/{id}/chat/save-to-note` | Save chat to note |

### Writer & Tone (3)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/research/writer/write` | AI writing generation |
| `POST` | `/research/writer/outline` | Generate outline |
| `POST` | `/research/tone/rewrite` | Scholarly tone rewrite |

### Extraction Matrix (7)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/research/projects/{id}/matrices` | List matrices |
| `POST` | `/research/projects/{id}/matrices` | Create matrix |
| `GET` | `/research/matrices/{id}` | Get matrix |
| `PATCH` | `/research/matrices/{id}` | Update matrix |
| `DELETE` | `/research/matrices/{id}` | Delete matrix |
| `POST` | `/research/matrices/{id}/extract` | Run extraction (202) |
| `GET` | `/research/extraction-tasks/{id}` | Extraction task status |

### Pipeline & Export (8)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/research/pipeline/{id}` | Pipeline stage status |
| `PATCH` | `/research/pipeline/{id}` | Update pipeline stage |
| `POST` | `/research/pipeline/{id}/advance` | Advance pipeline |
| `POST` | `/research/export/project/{id}` | Export project |
| `POST` | `/research/export/draft/{id}` | Export draft |
| `POST` | `/research/export/citations` | Export citations |
| `GET` | `/research/export/formats` | Available formats |
| `POST` | `/research/export/batch` | Batch export |

---

## 07 — Research Engine

**Prefix:** `/api/v1/research-engine`
**Files:** `blueprints.py` · `runs.py` · `projects.py` · `steps.py`

Structured research execution engine with reusable blueprints, multi-step runs, and granular step tracking. Blueprints define research workflows that can be instantiated as runs with individual step execution and monitoring.

**Key Features:** Blueprints, Structured Runs, Step Execution, Progress Tracking

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/research-engine/blueprints` | List blueprints |
| `POST` | `/research-engine/blueprints` | Create blueprint |
| `GET` | `/research-engine/blueprints/{id}` | Get blueprint detail |
| `POST` | `/research-engine/projects` | Create research project |
| `GET` | `/research-engine/projects` | List projects |
| `GET` | `/research-engine/projects/{id}` | Get project detail |
| `POST` | `/research-engine/runs` | Start a run |
| `GET` | `/research-engine/runs/{id}` | Get run detail |
| `POST` | `/research-engine/runs/{id}/execute-step` | Execute next step |
| `POST` | `/research-engine/runs/{id}/complete` | Mark run complete |
| `GET` | `/research-engine/runs/{id}/steps` | List run steps |
| `GET` | `/research-engine/runs/project/{id}` | Runs for project |
| `GET` | `/research-engine/steps/{id}` | Get step detail |
| `GET` | `/research-engine/steps/run/{id}` | Steps for run |

---

## 08 — Analytics

**Prefix:** `/api/v1/analytics`
**Files:** `dashboards.py` · `metrics.py` · `reports.py` · `realtime.py` · `graph_analytics.py` · `analytics_main.py`

Full analytics platform with custom dashboards and widgets, metric ingestion and querying, KPI tracking, automated report generation with scheduling and history, realtime analytics subscriptions, and graph analytics with centrality algorithms.

**Key Features:** Custom Dashboards, Metric Ingestion, Graph Analytics, KPIs, Report Scheduling, Realtime Subscriptions, Centrality Algorithms

### Dashboards (14)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/analytics/dashboards` | Create dashboard |
| `GET` | `/analytics/dashboards` | List dashboards |
| `GET` | `/analytics/dashboards/categories` | Dashboard categories |
| `GET` | `/analytics/dashboards/tags` | Dashboard tags |
| `GET` | `/analytics/dashboards/{id}` | Get dashboard |
| `PUT` | `/analytics/dashboards/{id}` | Update dashboard |
| `DELETE` | `/analytics/dashboards/{id}` | Delete dashboard |
| `POST` | `/analytics/dashboards/{id}/duplicate` | Duplicate dashboard |
| `POST` | `/analytics/dashboards/{id}/share` | Share dashboard |
| `POST` | `/analytics/dashboards/{id}/widgets` | Add widget |
| `PUT` | `/analytics/dashboards/{id}/widgets/{wid}` | Update widget |
| `DELETE` | `/analytics/dashboards/{id}/widgets/{wid}` | Delete widget |
| `GET` | `/analytics/dashboards/widgets/types` | Widget types |

### Metrics (15)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/analytics/metrics` | Create metric |
| `GET` | `/analytics/metrics` | List metrics |
| `GET` | `/analytics/metrics/{id}` | Get metric |
| `PUT` | `/analytics/metrics/{id}` | Update metric |
| `DELETE` | `/analytics/metrics/{id}` | Delete metric |
| `POST` | `/analytics/metrics/{id}/ingest` | Ingest data points |
| `POST` | `/analytics/metrics/ingest/batch` | Batch ingest |
| `POST` | `/analytics/metrics/query` | Query metrics |
| `GET` | `/analytics/metrics/{id}/statistics` | Metric statistics |
| `POST` | `/analytics/metrics/kpis` | Create KPI |
| `GET` | `/analytics/metrics/kpis` | List KPIs |
| `GET` | `/analytics/metrics/kpis/{id}` | Get KPI |
| `POST` | `/analytics/metrics/events` | Track events |
| `GET` | `/analytics/metrics/types` | Metric types |

### Reports (9)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/analytics/reports` | Create report |
| `GET` | `/analytics/reports` | List reports |
| `GET` | `/analytics/reports/{id}` | Get report |
| `POST` | `/analytics/reports/{id}/generate` | Generate report |
| `DELETE` | `/analytics/reports/{id}` | Delete report |
| `GET` | `/analytics/reports/templates` | Report templates |
| `POST` | `/analytics/reports/{id}/schedule` | Schedule recurring |
| `GET` | `/analytics/reports/{id}/history` | Report history |
| `GET` | `/analytics/reports/{id}/download/{hid}` | Download report |

### Graph Analytics (6)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/analytics/graph/analyze` | Graph analysis |
| `POST` | `/analytics/graph/paths` | Path analysis |
| `GET` | `/analytics/graph/statistics` | Graph statistics |
| `GET` | `/analytics/graph/centrality/{algo}` | Centrality analysis |
| `GET` | `/analytics/graph/algorithms` | Available algorithms |
| `GET` | `/analytics/graph/health` | Graph health |

---

## 09 — Quality

**Prefix:** `/api/v1/analytics/quality` · `/api/v1/analytics/behavior` · `/api/v1/analytics/performance` · `/api/v1/ab-testing` · `/api/v1/quality`
**Files:** `ab_testing.py` · `quality_metrics.py` · `quality_recommendations.py` · `performance_dashboard.py` · `user_behavior.py`

Quality assurance and experimentation suite — A/B testing with experiment variants, user segments, and metric recording; quality metrics with alerting; performance dashboards; user behavior tracking and session analysis; and AI-generated quality recommendations.

**Key Features:** A/B Testing, Quality Metrics, User Behavior, Performance Dashboards, Alerting, User Segments, Recommendations, Session Analysis

### A/B Testing (22)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/ab-testing/experiments` | Create experiment |
| `GET` | `/ab-testing/experiments` | List experiments |
| `GET` | `/ab-testing/experiments/{id}` | Get experiment |
| `PUT` | `/ab-testing/experiments/{id}` | Update experiment |
| `POST` | `/ab-testing/experiments/{id}/start` | Start experiment |
| `POST` | `/ab-testing/experiments/{id}/stop` | Stop experiment |
| `DELETE` | `/ab-testing/experiments/{id}` | Delete experiment |
| `POST` | `/ab-testing/experiments/{id}/variants` | Add variant |
| `GET` | `/ab-testing/experiments/{id}/variants` | List variants |
| `PUT` | `/ab-testing/variants/{id}` | Update variant |
| `DELETE` | `/ab-testing/variants/{id}` | Delete variant |
| `POST` | `/ab-testing/routing/assign` | Assign query to variant |
| `POST` | `/ab-testing/routing/bulk-assign` | Bulk assign |
| `POST` | `/ab-testing/metrics` | Record metrics |
| `POST` | `/ab-testing/metrics/bulk` | Bulk record |
| `POST` | `/ab-testing/experiments/{id}/analyze` | Analyze results |
| `GET` | `/ab-testing/experiments/{id}/results` | Get results |
| `POST` | `/ab-testing/segments` | Create user segment |
| `GET` | `/ab-testing/segments` | List segments |
| `GET` | `/ab-testing/health` | Health |
| `GET` | `/ab-testing/public/health` | Public health |

### Quality Metrics (10)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/analytics/quality/metrics/search` | Search metrics |
| `GET` | `/analytics/quality/metrics` | List quality metrics |
| `GET` | `/analytics/quality/metrics/aggregations` | Metric aggregations |
| `GET` | `/analytics/quality/alerts` | Quality alerts |
| `POST` | `/analytics/quality/alerts/{id}/acknowledge` | Acknowledge alert |
| `GET` | `/analytics/quality/analytics/search` | Search analytics |
| `GET` | `/analytics/quality/dashboard` | Quality dashboard |
| `GET` | `/analytics/quality/health` | Health |
| `GET` | `/analytics/quality/analytics/trends` | Quality trends |

### Performance Dashboard (13)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/analytics/performance/dashboard` | Full dashboard |
| `GET` | `/analytics/performance/overview` | Overview |
| `GET` | `/analytics/performance/system-health` | System health |
| `GET` | `/analytics/performance/search-performance` | Search performance |
| `GET` | `/analytics/performance/quality-metrics` | Quality metrics |
| `GET` | `/analytics/performance/user-engagement` | User engagement |
| `GET` | `/analytics/performance/alerts` | Performance alerts |
| `GET` | `/analytics/performance/charts/{metric}` | Chart data |
| `POST` | `/analytics/performance/widgets` | Add widget |
| `GET` | `/analytics/performance/widgets/defaults` | Default widgets |
| `GET` | `/analytics/performance/metrics/available` | Available metrics |
| `GET` | `/analytics/performance/export` | Export data |
| `GET` | `/analytics/performance/health` | Health |

### User Behavior (15)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/analytics/behavior/sessions` | Track session |
| `POST` | `/analytics/behavior/events` | Track events |
| `POST` | `/analytics/behavior/interactions` | Track interactions |
| `GET` | `/analytics/behavior/my-behavior` | My behavior |
| `GET` | `/analytics/behavior/users/{id}/behavior` | User behavior |
| `GET` | `/analytics/behavior/sessions/{id}/analysis` | Session analysis |
| `GET` | `/analytics/behavior/organization/trends` | Org trends |
| `GET` | `/analytics/behavior/insights` | Behavior insights |
| `GET` | `/analytics/behavior/organization/patterns` | Usage patterns |
| `GET` | `/analytics/behavior/organization/users` | Active users |
| `GET` | `/analytics/behavior/content-usage` | Content usage |
| `POST` | `/analytics/behavior/reports/generate` | Generate report |
| `POST` | `/analytics/behavior/export` | Export data |
| `GET` | `/analytics/behavior/health` | Health |

### Quality Recommendations (9)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/quality/recommendations` | Get recommendations |
| `GET` | `/quality/insights` | Quality insights |
| `POST` | `/quality/recommendations/{id}/progress` | Update progress |
| `GET` | `/quality/effectiveness` | Recommendation effectiveness |
| `GET` | `/quality/summary` | Quality summary |
| `GET` | `/quality/categories` | Recommendation categories |
| `GET` | `/quality/metrics` | Quality metrics |
| `POST` | `/quality/generate` | Generate new recommendations |
| `GET` | `/quality/health` | Health |

---

## 10 — Threads

**Prefix:** `/api/v1/threads` · `/api/v1/workspaces` · `/api/v1/conversations`
**Files:** `threads.py` · `conversations.py` · `workspaces.py` · `thread_search.py` · `stream.py`

Conversation management hierarchy: Workspaces → Conversations → Threads → Messages. Full CRUD on all levels with bulk operations (resolve, archive, summarize), full-text search across threads and messages, workspace members and settings management, and SSE streaming for live chat.

**Key Features:** Workspaces, Conversations, Threads, Bulk Ops, Full-Text Search, SSE Streaming, Auto-Summarize, Collections

### Threads (20)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/threads` | Create thread |
| `GET` | `/threads` | List threads |
| `GET` | `/threads/{id}` | Get thread detail |
| `PATCH` | `/threads/{id}` | Update thread |
| `DELETE` | `/threads/{id}` | Delete thread |
| `POST` | `/threads/{id}/resolve` | Resolve thread |
| `POST` | `/threads/{id}/reopen` | Reopen thread |
| `POST` | `/threads/{id}/archive` | Archive thread |
| `POST` | `/threads/{id}/summarize` | AI summarize |
| `GET` | `/threads/{id}/context` | Thread context |
| `POST` | `/threads/{id}/messages` | Add message |
| `GET` | `/threads/{id}/messages` | List messages |
| `GET` | `/threads/{id}/messages/{mid}` | Get message |
| `PATCH` | `/threads/{id}/messages/{mid}` | Update message |
| `DELETE` | `/threads/{id}/messages/{mid}` | Delete message |
| `POST` | `/threads/{id}/stream` | SSE streaming chat |
| `POST` | `/threads/bulk/resolve` | Bulk resolve |
| `POST` | `/threads/bulk/archive` | Bulk archive |
| `POST` | `/threads/bulk/summarize` | Bulk summarize |
| `DELETE` | `/threads/bulk` | Bulk delete |

### Workspaces (30+)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/workspaces` | Create workspace |
| `GET` | `/workspaces` | List workspaces |
| `GET` | `/workspaces/{id}` | Get workspace detail |
| `PATCH` | `/workspaces/{id}` | Update workspace |
| `DELETE` | `/workspaces/{id}` | Delete workspace |
| `POST` | `/workspaces/{id}/members` | Add member |
| `PATCH` | `/workspaces/{id}/members/{uid}` | Update member role |
| `DELETE` | `/workspaces/{id}/members/{uid}` | Remove member |
| `POST` | `/workspaces/{id}/settings` | Update settings |
| `GET` | `/workspaces/{id}/conversations` | List conversations |
| `GET` | `/workspaces/{id}/conversations/{cid}` | Get conversation |
| `PATCH` | `/workspaces/{id}/conversations/{cid}` | Update conversation |
| `DELETE` | `/workspaces/{id}/conversations/{cid}` | Delete conversation |
| `POST` | `/workspaces/{id}/conversations/{cid}/threads` | Add thread |
| `GET` | `/workspaces/{id}/threads` | List workspace threads |
| `GET` | `/workspaces/{id}/collections` | List collections |

### Conversations (13)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/conversations` | Create conversation |
| `GET` | `/conversations/workspaces` | List workspaces |
| `GET` | `/conversations/workspaces/{id}` | Get workspace |
| `PATCH` | `/conversations/workspaces/{id}` | Update workspace |
| `DELETE` | `/conversations/workspaces/{id}` | Delete workspace |
| `GET` | `/conversations/workspaces/{id}/stats` | Workspace stats |
| `POST` | `/conversations/workspaces/{id}/conversations` | Add conversation |
| `GET` | `/conversations` | List conversations |
| `GET` | `/conversations/{id}` | Get conversation |
| `PATCH` | `/conversations/{id}` | Update conversation |
| `DELETE` | `/conversations/{id}` | Delete conversation |
| `POST` | `/conversations/{id}/archive` | Archive |
| `POST` | `/conversations/{id}/pin` | Pin |

### Thread Search (7)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/search/threads` | Search threads |
| `GET` | `/search/threads` | Search threads (GET) |
| `POST` | `/search/messages` | Search messages |
| `GET` | `/search/messages` | Search messages (GET) |
| `GET` | `/search/combined` | Combined search |
| `GET` | `/search/suggestions` | Search suggestions |
| `GET` | `/search/health` | Search health |

---

## 11 — Realtime

**Prefix:** `/api/v1/realtime` · `/ws`
**Files:** `websocket.py` · `websocket_v2.py` · `realtime_document_status.py` · `realtime_quality_metrics.py`

WebSocket-based realtime system for live document processing status, quality metric evaluation, and broadcast messaging. Dual WebSocket implementations (v1 legacy + v2 enhanced) with connection tracking, channel management, and per-user connection stats.

**Key Features:** WebSocket v2, Document Status, Quality Metrics, Broadcast, Channel Management, Connection Tracking

### WebSocket (7)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/ws/status` | Legacy WS status |
| `GET` | `/realtime/ws/status` | WebSocket v2 status |
| `GET` | `/realtime/ws/connections/{user_id}` | User connections |
| `POST` | `/realtime/ws/broadcast` | Broadcast message |
| `POST` | `/realtime/ws/test-connection` | Test connection |
| `GET` | `/realtime/ws/channels` | List channels |
| `GET` | `/realtime/ws/health` | WS health |

### Document Status (7)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/realtime/documents/status/subscribe` | Subscribe to doc status |
| `GET` | `/realtime/documents/{id}/status` | Document realtime status |
| `POST` | `/realtime/documents/bulk/status` | Bulk doc status |
| `GET` | `/realtime/system/metrics` | System metrics |
| `POST` | `/realtime/documents/{id}/broadcast-status` | Broadcast doc status |
| `GET` | `/realtime/connections/status` | Connection status |

### Quality Metrics (5)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/realtime/quality/evaluate` | Start quality evaluation |
| `GET` | `/realtime/quality/status/{query_id}` | Eval status |
| `GET` | `/realtime/quality/history/{query_id}` | Eval history |
| `GET` | `/realtime/quality/stats` | Quality stats |
| `DELETE` | `/realtime/quality/cleanup` | Cleanup old evals |

---

## 12 — Security

**Prefix:** `/api/v1/security` · `/api/v1/rbac`
**Files:** `rbac_management.py` · `encryption.py` · `compliance.py`

Enterprise security layer — RBAC with granular permissions and role management, field-level encryption with key rotation, compliance audit trail with event logging, security incident tracking, and compliance report generation.

**Key Features:** RBAC, Field Encryption, Audit Trail, Key Rotation, Compliance Reports, Incident Tracking, Sensitive Field Config

### RBAC (14)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/rbac/permissions` | List all permissions |
| `GET` | `/rbac/permissions/categories` | Permission categories |
| `POST` | `/rbac/roles` | Create role |
| `GET` | `/rbac/roles` | List roles |
| `GET` | `/rbac/roles/{id}` | Get role |
| `PUT` | `/rbac/roles/{id}` | Update role |
| `DELETE` | `/rbac/roles/{id}` | Delete role |
| `POST` | `/rbac/users/{id}/roles` | Assign role to user |
| `DELETE` | `/rbac/users/{id}/roles/{rid}` | Remove role |
| `GET` | `/rbac/users/{id}/permissions` | User permissions |
| `GET` | `/rbac/users/current/permissions` | Current user permissions |
| `GET` | `/rbac/roles/{id}/users` | Users with role |
| `POST` | `/rbac/initialize` | Initialize RBAC system |
| `POST` | `/rbac/cleanup-expired` | Cleanup expired assignments |

### Encryption (8)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/security/encryption/profiles/user` | Create user encryption profile |
| `POST` | `/security/encryption/profiles/organization` | Create org profile |
| `POST` | `/security/encryption/decrypt` | Decrypt data |
| `POST` | `/security/encryption/keys/rotate` | Rotate encryption keys |
| `GET` | `/security/encryption/status` | Encryption status |
| `POST` | `/security/encryption/validate` | Validate encryption |
| `GET` | `/security/encryption/audit/logs` | Encryption audit logs |
| `GET` | `/security/encryption/config/sensitive-fields` | Sensitive field config |

### Compliance (11)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/security/audit/events` | List audit events |
| `GET` | `/security/audit/events/{id}` | Get audit event |
| `GET` | `/security/audit/events/export` | Export audit events |
| `POST` | `/security/audit/events/search` | Search audit events |
| `GET` | `/security/reports` | Compliance reports |
| `GET` | `/security/reports/{id}` | Get report |
| `POST` | `/security/reports/generate` | Generate report |
| `GET` | `/security/security/incidents` | Security incidents |
| `GET` | `/security/security/incidents/{id}` | Get incident |
| `POST` | `/security/cleanup/audit-events` | Cleanup old events |
| `GET` | `/security/dashboard` | Security dashboard |

---

## 13 — Infrastructure

**Prefix:** `/api/v1/workers` · `/api/v1/evaluation` · `/api/v1/diagnostics`
**Files:** `workers.py` · `evaluation.py` · `retrieval_diagnostics.py`

Platform infrastructure management — Celery worker monitoring with queue stats and task registry, RAG evaluation framework with batch jobs, metric comparison, and report generation, and retrieval diagnostics with trace analysis and weight experimentation.

**Key Features:** Celery Workers, RAG Evaluation, Retrieval Diagnostics, Queue Monitoring, Metric Comparison, Weight Experiments, Report Generation

### Workers (6)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/workers/status` | Worker stats |
| `GET` | `/workers/queues` | Queue stats |
| `POST` | `/workers/ping` | Ping workers |
| `GET` | `/workers/registered-tasks` | Task registry |
| `POST` | `/workers/shutdown/{name}` | Shutdown worker |
| `GET` | `/workers/health` | Worker health |

### Evaluation (14)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/evaluation/jobs` | Create evaluation job |
| `POST` | `/evaluation/jobs/batch` | Batch evaluation |
| `POST` | `/evaluation/real-time` | Real-time evaluation |
| `GET` | `/evaluation/jobs/{id}` | Job status |
| `GET` | `/evaluation/jobs` | List jobs |
| `GET` | `/evaluation/jobs/{id}/metrics` | Job metrics |
| `POST` | `/evaluation/comparisons` | Compare evaluations |
| `GET` | `/evaluation/comparisons` | List comparisons |
| `POST` | `/evaluation/jobs/{id}/reports/{type}` | Generate report |
| `GET` | `/evaluation/reports` | List reports |
| `GET` | `/evaluation/reports/{id}` | Get report |
| `GET` | `/evaluation/metrics/summary` | Metrics summary |
| `DELETE` | `/evaluation/jobs/{id}` | Delete job |
| `GET` | `/evaluation/health` | Evaluation health |

### Diagnostics (4)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/diagnostics/traces/{id}` | Get retrieval trace |
| `GET` | `/diagnostics/traces` | List traces |
| `GET` | `/diagnostics/aggregate` | Aggregate diagnostics |
| `POST` | `/diagnostics/weight-experiment` | Run weight experiment |

---

*NOUS · Backend API Reference · ~350 Endpoints · 13 Domains · March 2026*

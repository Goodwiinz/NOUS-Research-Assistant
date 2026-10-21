# Research services

This package contains the business logic for research projects, document ingestion from arXiv, citation management, draft generation, and writing assistance. It is one layer below the API routers and above the database models; the agent (LangGraph) calls into `citation_extraction_service` and `project_memory_service` directly on the hot path.

## Key files

| File                             | Purpose                                                                                                                                                                                                                         |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `project_service.py`             | Research project CRUD (create, list, update, delete) with workspace ownership validation and status-transition enforcement (`active → paused/completed/archived`)                                                               |
| `pipeline_service.py`            | Five-step research wizard state machine — tracks current/completed/skipped/invalidated steps; auto-invalidates downstream steps when the user navigates back                                                                    |
| `project_thread_service.py`      | Authoritative link between a chat thread and a project; writes both `thread.source_project_id` and the `project_threads` join row atomically to prevent desync                                                                  |
| `project_memory_service.py`      | Thin read path that loads per-project memories (capped at 25) for injection into the agent system prompt                                                                                                                        |
| `citation_extraction_service.py` | Hybrid citation pipeline: arXiv API → Semantic Scholar → CrossRef → PDF parse → manual fallback                                                                                                                                 |
| `citation_graph_service.py`      | Syncs citations to Neo4j as `:Citation` nodes with `:CITES` edges; computes server-side force-directed layout (≤200 nodes) or delegates to the frontend for larger graphs; uses APOC `subgraphAll` with a plain Cypher fallback |
| `bibliography_service.py`        | Formats `Citation` model lists into BibTeX, IEEE, APA, or MLA; uses `pybtex` for BibTeX (optional dep)                                                                                                                          |
| `message_citation_service.py`    | Parses `[Doc N]` markers from AI responses and persists them as `Citation` rows                                                                                                                                                 |
| `extraction_matrix_service.py`   | Structured data extraction from documents via OpenAI; runs as a background task with in-memory status tracking                                                                                                                  |
| `draft_generation_service.py`    | Generates literature review drafts from project documents; uses a fresh `AsyncSessionLocal()` to avoid session rollback conflicts with the calling request                                                                      |
| `writer_service.py`              | AI writing assistance (inline completion, section generation, outline); the module still returns mock responses pending LLM integration                                                                                |
| `tone_engine_service.py`         | Rewrites text at an adjustable academic tone via OpenAI while preserving `[N]` citation markers                                                                                                                                 |
| `export_service.py`              | Exports thread conversations to Markdown, PDF, JSON, or HTML using Jinja2 templates                                                                                                                                             |

## Evidence, review and release services

Newer services (tickets in the module docstrings) cover the manuscript lifecycle. Each `*_rules.py` file is pure (no database, no FastAPI) and is paired with a service that owns persistence.

| File(s) | Purpose |
| --- | --- |
| `claims_service.py`, `claim_rules.py`, `claims_export.py` | Versioned claims, evidence links, stance snapshots and assessments (GOO-306); reconstructable evidence export |
| `extraction_forms_service.py`, `extraction_rules.py`, `source_anchors.py` | Versioned extraction forms, observations, accepted values and source anchors (GOO-304/305) |
| `draft_release_service.py`, `release_rules.py` | Verified draft promotion gate and selective invalidation (GOO-307) |
| `peer_review_service.py`, `peer_review_rules.py` | External peer-review rounds, anchored comments, responses and decisions (GOO-314) |
| `manuscript_release_service.py`, `manuscript_rules.py` | Immutable candidate and verified manuscript releases (GOO-315) |
| `statements_service.py`, `venue_rules.py` | Statement sets, author approvals, ORCID receipts and venue checks (GOO-316) |
| `deposit_service.py`, `deposit_rules.py`, `archives/zenodo.py` | Resumable archive deposits of verified releases (GOO-318) |
| `citation_verification_service.py`, `evidence_selection.py` | Citation faithfulness verification for drafts; bounded evidence selection for prompts |

## arXiv integration

arXiv ingestion lives in `../arxiv/arxiv_service.py` (`ArXivIngestionService`) and is called by `citation_extraction_service.py`.

**Timeouts and rate limits.** The arXiv API requires at least 3 seconds between requests (enforced via a class-level monotonic timestamp). Individual HTTP calls use a 20-second `httpx` timeout. At most one 429 retry is attempted with a 3-second wait; further rate limiting raises `IngestionError` immediately so the agent can report the failure rather than hanging inside a tool call. When using the arXiv service from an agent tool, call the endpoint via `postWithLongTimeout` (5-minute timeout) — the network round-trip plus PDF download can exceed the default 30-second tool timeout.

**ID handling after ingest.** After a successful ingest call the response contains `document_ids` — these are PostgreSQL UUIDs assigned by the documents service. Do not use the arXiv paper IDs (e.g., `2301.00001`) to reference ingested documents downstream; they are only stored as metadata. All subsequent tool calls (search, KG queries, draft generation) expect `document_ids` as UUIDs.

**Citation extraction fallback chain.** `CitationExtractionService` first tries the arXiv API for papers with a known `arxiv_id`, then Semantic Scholar and CrossRef for DOI-based lookup, then PDF text parsing, and finally accepts a manual entry. All extracted citations are stored in PostgreSQL and optionally synced to Neo4j via `CitationGraphService.sync_citation_to_graph`.

## Connections to other services

- **Documents service** (`src/api/documents/`): arXiv ingest writes document records there; the research services read them back via SQLAlchemy sessions.
- **Neo4j** (`bolt://localhost:7687`): `CitationGraphService` writes citation nodes and edges; requires the Neo4j APOC plugin for subgraph traversal (plain Cypher fallback is active if APOC is absent).
- **Agent** (`src/services/agent/`): `project_memory_service.load_project_memories` and `citation_extraction_service` are called on the agent hot path; `draft_generation_service` is triggered by the `create_draft` destructive tool which fires a HITL interrupt before execution.

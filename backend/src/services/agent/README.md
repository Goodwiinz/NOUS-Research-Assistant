# Agent Service — LangGraph Orchestration Core

This package is the brain of every NOUS conversation. It receives a user message from the FastAPI `/api/v1/agent/` endpoints, routes it through a LangGraph `StateGraph`, executes tools against PostgreSQL/Qdrant/Neo4j, and streams results back via SSE or a polled job. Everything from intent classification and RAG retrieval through HITL interrupts, context compaction, and durable checkpointing lives here.

---

## Key files

| File                                | Purpose                                                                                                                                                                                                                                                                                     |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `graph.py`                          | Public façade; re-exports every node and builder so existing callers don't break. Start here when tracing the request flow.                                                                                                                                                                 |
| `_builders.py`                      | Wires the `StateGraph`: all nodes, conditional edges, and subgraph embeddings. Contains `compile_agent_graph`, `create_graph` (langgraph.json entry point), and soft circuit-breaker constants `MAX_TOOL_LOOPS = 4`, `MAX_ERRORS = 3`.                                                      |
| `state.py`                          | `AgentState` TypedDict — the single object threaded through every node. Defines all fields including `intent`, `plan`, `reflection_count`, `pending_confirmation`, and `current_project_id`.                                                                                                |
| `checkpointer.py`                   | Builds the `AsyncPostgresSaver` singleton over a shared connection pool; normalises the DB URI scheme; falls back to `MemorySaver` in dev/test only.                                                                                                                                        |
| `_nodes_classify.py`                | `preprocessing_node` (runs RAG retrieval + intent classification + memory recall in parallel), `route_by_intent`, and the weighted-keyword classifier logic.                                                                                                                                |
| `_nodes_llm.py`                     | Main `llm_node`, intent-specific tool subsets (`RESEARCH_TOOLS_NAMES`, `WRITING_TOOLS_NAMES`, `KG_TOOLS_NAMES`, `GENERAL_TOOLS_NAMES`), and `force_synthesis_node` (used when the loop ceiling trips mid-tool-plan).                                                                        |
| `_nodes_tools.py`                   | `tool_node` (parallel execution with per-call timeout + semaphore), `interrupt_node` (pauses for HITL), `make_filtered_tool_node` (subgraph tool subsetting), and `DESTRUCTIVE_TOOLS` set.                                                                                                  |
| `_nodes_memory.py`                  | `memory_retrieval_node` and `memory_save_node` — reads/writes per-user and per-project durable memories via the long-term store.                                                                                                                                                            |
| `_nodes_rag.py`                     | `rag_node` — hybrid Qdrant/DO KB retrieval, fast-path heuristic for conversational turns that skip retrieval entirely.                                                                                                                                                                      |
| `tools.py`                          | Declares the `@tool` schemas (`ALL_TOOLS`, `TOOL_REGISTRY`): signature + docstring are the LLM binding and the arguments `execute_tool` validates. Bodies never run; guards live in `tools_impl` (audit round 8). Config carries scalar ids only (audit B8).                                  |
| `tools_impl.py` / `tool_helpers.py` | Canonical tool implementations (`_tool_*`, `execute_tool`, `AGENT_TOOLS`) and shared helpers (`_resolve_document_id`, `_verify_project_ownership`, …) — moved out of `src/api/agent/` (audit B1); the api shims are deleted. `execute_tool` is the ids→(session, user) boundary (audit B8). |
| `tool_session.py`                   | `tool_session()` async context manager + `resolve_tool_user` — opens a fresh tool-call-scoped `AsyncSession` and re-loads the acting `User` org-scoped from scalar ids, so the graph config never smuggles the request session / ORM user (audit B8).                                       |
| `agent_execution_service.py`        | The job runner (audit B5): job-store access, thread resolution/project binding, message persistence, Option B history seeding + dual-store guard, `_run_agent_graph`/`_resume_agent_graph`. `src/api/agent/jobs.py` is a thin re-export seam.                                               |
| `confirmation_service.py` | Shared approval receipts binding run, checkpoint, interrupt and full action; exact-interrupt resume commands for SSE and job producers. |
| `agent_run_service.py`              | Sole owner of the durable `agent_runs` Postgres projection (status write-through, poll fallback, sweeper leases).                                                                                                                                                                           |
| `agent_submission_service.py`       | Atomic acceptance for `/stream` (P0-C): one transaction commits the user message, the `agent_runs` row, its `run.created` event and the `agent_outbox` dispatch record, so `accepted` can only be emitted post-commit. Also owns the run's dispatch stamp and terminal status.               |
| `run_event_store.py`                | Sole owner of the append-only `agent_run_events` ledger (P0-B): server-allocated `seq`, one terminal event per run, tenant-scoped replay reads. Never commits — appends join the caller's transaction.                                                                                      |
| `schemas.py`                        | Agent execute/response wire models (`AgentExecuteRequest`, `AgentExecuteResponse`, …) shared by the router and the job runner; re-exported by `src/api/agent/execute.py`.                                                                                                                   |
| `_errors.py`                        | `client_safe_error` + `extract_interrupt_confirmation` — non-leaking client error strings and HITL interrupt payload extraction.                                                                                                                                                            |
| `planner.py`                        | Adaptive pre-flight planner: generates a structured `{step, tool, args_hint}` plan when query complexity warrants three or more tool calls.                                                                                                                                                 |
| `compactor.py`                      | Summarises older `ToolMessage` payloads after each tool loop to keep the context window in budget; preserves UUIDs and arXiv IDs verbatim.                                                                                                                                                  |
| `reflection.py`                     | Quality gate before `memory_save_node`: uses a lightweight LLM to decide `proceed` or `revise`; allows at most 2 revision cycles per turn.                                                                                                                                                  |
| `observability.py`                  | Prometheus metrics and LangSmith tracing hooks wired into every node via `track_node_execution`.                                                                                                                                                                                            |
| `error_recovery.py`                 | `classify_error`, `retry_transient` — transient-vs-permanent error classification and outer retry logic for tool calls.                                                                                                                                                                     |
| `llm_factory.py`                    | Builds cached `AzureChatOpenAI`/`ChatOpenAI` instances; `build_lightweight_llm` for planner, compactor, and reflection nodes.                                                                                                                                                               |
| `memory.py` / `memory_store.py`     | Runtime memory helpers for the long-term cross-thread store.                                                                                                                                                                                                                                |
| `job_store.py`                      | Redis-backed store for async job state (used by `/execute` polling path).                                                                                                                                                                                                                   |
| `visualization.py`                  | Mermaid diagram and per-thread trace endpoints.                                                                                                                                                                                                                                             |
| `classifier.py`                     | Intent classifier internals shared by `_nodes_classify`.                                                                                                                                                                                                                                    |
| `_sanitize.py`                      | Prompt-field sanitisation (strips injections before LLM calls).                                                                                                                                                                                                                             |
| `_pii_redact.py`                    | PII redaction applied to logged message content.                                                                                                                                                                                                                                            |
| `_prompts.py`                       | `INTENT_PROMPTS`, `SHARED_AGENT_RULES`, static prompt fragments, and `_merge_run_config` used across every LLM node.                                                                                                                                                                        |
| `_pool_utils.py`                    | Shared `AsyncConnectionPool` lifecycle (TCP keepalives required for HITL pauses through PgBouncer in session mode).                                                                                                                                                                         |
| `_uuid.py`                          | `UUID_SEARCH_RE` used by `graph.py` to extract project IDs from free text.                                                                                                                                                                                                                  |
| `tool_dedupe.py`                    | Deduplicates tool call results when the LLM retries the same call.                                                                                                                                                                                                                          |
| `iteration_ledger.py`               | Per-turn ledger tracking tool calls to detect and break re-query loops.                                                                                                                                                                                                                     |

---

## Architecture and flow

```
START
  └─► preprocessing_node          # parallel: RAG retrieval + intent classify + memory recall
        └─► route_by_intent
              ├─► research_subgraph  ─┐
              ├─► writing_subgraph   ─┤─► memory_save_node ─► END
              ├─► data_subgraph      ─┘
              └─► planner_node ─► llm_node
                                    ├─► tool_node ─► compactor_node ─► llm_node  (loop, max 4)
                                    ├─► interrupt_node  (HITL pause)
                                    │     └─► [confirmed] ─► tool_node
                                    │     └─► [denied]    ─► reflection_gate
                                    ├─► force_synthesis_node ─► reflection_gate
                                    └─► reflection_gate
                                          ├─► [proceed] ─► memory_save_node ─► END
                                          └─► [revise]  ─► llm_node          (max 2 cycles)
```

Each **subgraph** (research, writing, data) embeds its own planner, compactor, and reflection gate — they join the main flow only at `memory_save_node`. The `preprocessing_node` fans out RAG retrieval, intent classification, and memory recall concurrently so none of the three waits on the others.

**Checkpointing** uses `AsyncPostgresSaver` backed by a shared `asyncpg` connection pool with TCP keepalives. The compiled graph is cached by `(id(checkpointer), id(store))` — it is built once per process, not per request.

**HITL** works as follows: `interrupt_node` calls LangGraph's `interrupt()`, suspending the graph and persisting its state to the checkpoint. The API returns a job in `awaiting_confirmation` status. When the client posts to `/api/v1/agent/confirm/{job_id}`, `after_interrupt` routes to `tool_node` (confirmed) or `reflection_gate` (denied).

---

## Subgraphs

`subgraphs/` — three specialised `StateGraph`s, each with its own intent-filtered tool set: `research_agent.py` (arXiv search/ingest, document search, project management), `writing_agent.py` (drafts, notes, bibliography, summarisation), and `data_agent.py` (entity extraction, knowledge graph queries). Each subgraph file ships alongside an `AGENTS_*.md` prompt file loaded at build time.

---

## Gotchas for newcomers

- **Checkpoint URI must be `postgresql://`** — `langgraph-checkpoint-postgres` uses psycopg v3, not asyncpg. `checkpointer.py` strips any `+asyncpg` suffix automatically, but do not pass a `postgresql+asyncpg://` URI directly to `AsyncPostgresSaver`.
- **Every `AIMessage` with `tool_calls` needs matching `ToolMessage`s** — the OpenAI-compatible API rejects otherwise. `_sanitize_messages` in `graph.py` repairs checkpoint state after cancellations by inserting `{"status": "skipped"}` placeholders; the compactor recognises that exact string and skips those synthetic entries.
- **Consecutive `HumanMessage`s are superseded, not concatenated** — if a user aborts a streaming turn and types a new message, LangGraph appends two consecutive `HumanMessage`s to the checkpoint. `_sanitize_messages` keeps only the later one; the abandoned message had no AI response so treating both as intent is wrong.
- **`execute_tool` is lazily imported** — `graph.py` defers the import from `src.services.agent.tools_impl` via `_get_execute_tool()` to avoid circular imports and to keep test patches working across the split.
- **gpt-5 family rejects `temperature`** — `_build_llm` drops it for any deployment whose name starts with `gpt-5`; Azure returns 400 otherwise.
- **The compiled graph is cached** — `compile_agent_graph` caches by `(id(checkpointer), id(store))`. Call `reset_compiled_graph_cache()` in tests that swap out checkpointers between cases.
- **Slow tools have a separate timeout** — arXiv search/ingest sit in `_SLOW_TOOLS` and get `_SLOW_TOOL_TIMEOUT_SECONDS` rather than the default `TOOL_TIMEOUT_SECONDS`. The arXiv tool is also in `_NO_OUTER_RETRY_TOOLS` because its internal client already retries; stacking pushed total latency to ~85 s in trace `019e040b`.

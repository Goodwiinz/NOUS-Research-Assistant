# NOUS — Agent CODEMAP

> LangGraph agent: graph topology, nodes, subgraphs, tools, HITL, streaming, observability.
> Last generated: 2026-04-26; `document_search` row corrected 2026-10-17. The graph facade is `graph.py`, with construction in `_builders.py`; see `backend/src/services/agent/README.md` for the maintained file list.

## Entry points

| File | Symbol | Purpose |
|---|---|---|
| `backend/langgraph.json` | `agent` graph | Points to `create_graph` — used by LangGraph dev server |
| `backend/src/services/agent/graph.py` | `create_graph()` | Builds and compiles the StateGraph |
| `backend/src/api/agent/execute.py` | `/api/v1/agent/execute` | Async job dispatch |
| `backend/src/api/agent/streaming.py` | `/api/v1/agent/stream` | SSE streaming endpoint |
| `backend/src/api/agent/jobs.py` | `/api/v1/agent/jobs/{job_id}` | Job polling + result retrieval |

## Graph topology

```
START
  └─► rag_node          (retrieve context for the message)
        └─► intent_classifier  (route by intent: research / writing / data / general)
              ├─► research_subgraph   (arXiv, document search, project tools)
              ├─► writing_subgraph    (drafts, notes, bibliography, tone)
              ├─► data_subgraph       (entity extraction, KG queries)
              └─► general_subgraph   (full tool set, LLM routing)
                    └─► tool_node    (execute tool calls — max 8-10 iterations)
                          └─► memory_save
                                └─► END
```

Human-in-the-loop interrupts happen **before** tool execution for destructive tools.  
On interrupt: graph pauses → client polls `/api/v1/agent/jobs/{job_id}` → user confirms → `POST /api/v1/agent/confirm/{job_id}` resumes with `Command(resume=...)`.

## Key files

| File | Purpose |
|---|---|
| `graph.py` | StateGraph definition, node wiring, checkpoint config |
| `state.py` | `AgentState` TypedDict — messages, intent, context, metadata |
| `classifier.py` | Intent classification node (research / writing / data / general) |
| `memory.py` | Conversation memory read/write |
| `memory_store.py` | Persistent memory backend (PostgreSQL via AsyncPostgresSaver) |
| `checkpointer.py` | AsyncPostgresSaver setup (fallback: MemorySaver) |
| `tools.py` | Tool registry — maps name → implementation |
| `tools_impl.py` (api/) | Tool implementations (1,713 LOC — split into groups pending) |
| `observability.py` | LangSmith + Prometheus hooks |
| `error_recovery.py` | Retry + fallback on node failures |
| `compactor.py` | Message compaction to stay within context window |
| `reflection.py` | Self-critique node (optional, off by default) |
| `planner.py` | Multi-step planning node (research subgraph) |
| `visualization.py` | Mermaid graph export (`/api/v1/agent/graph/mermaid`) |

## Subgraphs

| Subgraph | File | Tools available | Max loops |
|---|---|---|---|
| research | `subgraphs/research_agent.py` | arXiv search/ingest, document search, project CRUD | 8 |
| writing | `subgraphs/writing_agent.py` | create_draft, create_note, bibliography, summarize, compare_docs | 8 |
| data | `subgraphs/data_agent.py` | entity_extraction, kg_query, table_extraction | 8 |
| general | (inline in graph.py) | Full tool set — LLM decides | 10 |

## Tools (registered in `tools.py`)

| Tool | Destructive (HITL)? | Description |
|---|---|---|
| `arxiv_search` | No | Search arXiv papers |
| `arxiv_ingest` | **Yes** | Download + ingest arXiv papers |
| `document_search` | No | PostgreSQL full-text search (Qdrant is removed) |
| `kg_query` | No | Cypher query against Neo4j |
| `entity_extraction` | No | Extract entities from text |
| `create_note` | **Yes** | Create a research note |
| `create_draft` | **Yes** | Create a document draft |
| `project_create` | **Yes** | Create a research project |
| `summarize` | No | Summarize document content |
| `compare_documents` | No | Side-by-side doc comparison |
| `table_extraction` | No | Extract tables from PDFs |
| `bibliography` | No | Generate citation list |

Destructive tools call `interrupt()` — the graph pauses and sends an `interrupt` event to the SSE stream. Client must POST `/confirm/{job_id}` to resume.

## Streaming events (SSE)

| Event | Payload | When |
|---|---|---|
| `token` | `{text: string}` | Each LLM token |
| `tool_start` | `{name, input}` | Before tool execution |
| `tool_end` | `{name, output}` | After tool execution |
| `rag_context` | `{chunks: [...]}` | After RAG retrieval |
| `interrupt` | `{job_id, tool_name, args}` | HITL pause |
| `done` | `{thread_id, message_id}` | Stream complete |
| `error` | `{message}` | Unrecoverable error |

## Checkpointing

- Primary: `AsyncPostgresSaver` — connection URL must be `postgresql://` (psycopg v3 sync driver), NOT `postgresql+asyncpg://`.
- Fallback: `MemorySaver` (in-process, lost on restart).
- Threads are namespaced by `thread_id` (UUID); each conversation is one checkpoint thread.

## Observability

- **LangSmith**: every graph invocation creates a trace. Set `LANGCHAIN_API_KEY` + `LANGCHAIN_PROJECT`.
- **Prometheus**: `agent_execution_duration_seconds` histogram, `agent_tool_calls_total` counter.
- **Trace API**: `GET /api/v1/agent/graph/trace/{thread_id}` returns structured node execution history.

## Test markers

```python
@pytest.mark.unit        # Pure logic, no I/O
@pytest.mark.integration # Hits real DB / services
@pytest.mark.e2e         # Full stack
@pytest.mark.langsmith   # LangSmith evaluation
@pytest.mark.deepeval    # DeepEval metric evaluation
```

## Adding a new tool

1. Implement function in `src/api/agent/tools_impl.py` (or a new sibling file per tool group).
2. Wrap with `@tool` decorator and add docstring — the docstring is the LLM's tool description.
3. Register in `src/services/agent/tools.py` tool registry.
4. Add to the appropriate subgraph's `filtered_tools` list in `subgraphs/<agent>.py`.
5. If destructive: call `interrupt({"tool_name": ..., "args": ...})` before execution.
6. Add `ToolMessage` placeholder in the sanitizer for test compatibility.
7. Write unit test in `tests/unit/agent/tools/test_<tool>.py`.

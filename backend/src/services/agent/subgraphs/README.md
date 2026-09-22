# Agent subgraphs

Each subgraph is a self-contained LangGraph `StateGraph` compiled as a node inside the parent agent graph (`_builders.py`). The parent routes to a subgraph — or falls through to the general path — after `preprocessing_node` classifies the user's intent.

## Routing

`route_by_intent` (`_nodes_classify.py`) reads `state["intent"]` and returns the target node name:

| intent value      | target node                   |
| ----------------- | ----------------------------- |
| `research`        | `research_subgraph`           |
| `writing`         | `writing_subgraph`            |
| `knowledge_graph` | `data_subgraph`               |
| anything else     | `planner_node` (general path) |

The general path is not a subgraph file; it lives in `graph.py` / `_nodes_llm.py` and uses the full tool set with LLM routing.

## Subgraphs

| File                | Intent            | Tools                                                                                                                                                               | Tool-loop ceiling                                       |
| ------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| `research_agent.py` | `research`        | `search_arxiv`, `ingest_arxiv_papers`, `search_documents`, `do_kb_retrieve`, `create_project`, `list_projects`, `add_document_to_project`, `list_project_documents` | 5 (lowered from 8 after runaway fan-out trace 019e18f0) |
| `writing_agent.py`  | `writing`         | `create_draft`, `revise_draft`, `create_project_note`, `export_bibliography`, `summarize_document`, `compare_documents`, `search_arxiv`, `ingest_arxiv_papers`      | 8                                                       |
| `data_agent.py`     | `knowledge_graph` | `extract_entities`, `search_knowledge_graph`, `explore_entity_neighborhood`, `find_entity_paths`, `get_graph_stats`, `search_documents`, `list_project_documents`   | 8                                                       |

`search_arxiv` and `ingest_arxiv_papers` appear in the writing subgraph because the agent needs to resolve a paper title to an arXiv id before it can summarize or note it.

## Internal node pattern

Every subgraph follows the same structure:

```
planner_node → llm_node → should_continue
    ├── interrupt_node → after_interrupt → tool_node   (destructive tools only; research + writing)
    ├── tool_node → compactor_node → llm_node           (loop)
    ├── force_synthesis_node → reflection_gate          (loop ceiling hit)
    └── reflection_gate → END | llm_node                (revise on major issues)
```

Nodes are namespaced per subgraph (e.g., `research_llm_node`) to avoid collisions in the parent graph's checkpoint store.

**HITL.** Research and writing subgraphs define a set of destructive tools (`RESEARCH_DESTRUCTIVE_TOOLS`, `WRITING_DESTRUCTIVE_TOOLS`). When `should_continue` sees a pending tool call in that set it routes to an interrupt node, which calls `langgraph.types.interrupt()` to pause the graph and surface a confirmation payload to the client. Data subgraph has no destructive tools and therefore no interrupt node.

**Forced synthesis.** When the tool-loop ceiling is hit and the model still wants more tools, `should_continue` routes to `force_synthesis_node` instead of looping. That node strips the unanswered `tool_calls`, re-invokes the LLM with no tools bound, and bumps `tool_loop_count` past the ceiling so the subgraph cannot re-enter forced synthesis if the response somehow contains stray tool calls.

**Reflection.** Each subgraph compiles a `reflection_gate` from `make_reflection_gate()` with an `intent_filter`. Research and writing use `intent_filter={"research"}` / `intent_filter={"writing"}` so the gate evaluates their responses. The data subgraph uses `intent_filter={"research", "writing"}`, which excludes `knowledge_graph` — KG queries are treated as deterministic lookups and skip reflection evaluation.

**Model selection.** Tool-decision turns use the **main** deployment via `_build_llm`; ordinary post-tool synthesis uses `build_synthesis_llm` (cheap tier). Forced synthesis after a loop ceiling uses the main deployment because it must recover a grounded partial answer from a long trajectory. The split is by *difficulty*, not by node: choosing a tool or recovering safely at a hard limit needs the stronger tier, while narrating one returned result does not.

This is the reverse of the original arrangement, which put tool decisions on `build_lightweight_llm`. That was a workaround for the main deployment being `model-router`, which hit the 30s timeout cap (trace 019e1da5); it stopped being true when the deployment changed, and the cheapest model was left making the hardest decision. Symptom: the research subgraph would answer "shall I ingest it?" in prose instead of calling `ingest_arxiv_papers`, on ~96% of dev runs. Raising `reasoning_effort` on the small tier fixed the symptom in an A/B (4/4 vs a ~4% base rate) — the tier assignment is the underlying cause.

**No `reasoning_effort` on tool-calling turns.** Azure rejects function tools sent alongside `reasoning_effort` on Chat Completions, and the Responses API (which the error suggests) rejects the agent's accumulated `tool_call` history. `_build_llm` drops the kwarg outright (#1334); `build_synthesis_llm` / `build_lightweight_llm` drop it when passed `tool_calling=True`. Pass that flag from anything that calls `bind_tools` **or** `with_structured_output(..., method="function_calling")` — the latter ships a function tool too, which is easy to miss. Prose-only and `json_schema` structured-output callers keep `minimal`.

## System prompts

Each subgraph loads its driver protocol from a co-located markdown file via `agents_md_loader.load_agents_md(subgraph)`:

| File                 | Loaded by           |
| -------------------- | ------------------- |
| `AGENTS_research.md` | `research_agent.py` |
| `AGENTS_writing.md`  | `writing_agent.py`  |
| `AGENTS_data.md`     | `data_agent.py`     |

`agents_md_loader` caches the file contents at import time. Set `AGENT_AGENTS_MD_RELOAD=true` to bypass the cache when iterating on prompt files without restarting the backend. If a file is missing, the loader returns an empty string and the subgraph falls back to an inline prompt — the agent never crashes on a deploy that omits the markdown.

## Adding a new subgraph

1. Create `<intent>_agent.py` following the node pattern above. Define `BUILD_<INTENT>_TOOL_LOOPS`, `<INTENT>_TOOLS`, `<INTENT>_DESTRUCTIVE_TOOLS` (if any), and a `build_<intent>_subgraph() -> StateGraph` function.
2. Add `AGENTS_<intent>.md` alongside it with the driver protocol.
3. Register the new intent in `agents_md_loader._VALID_SUBGRAPHS`.
4. In `_builders.py`, import `build_<intent>_subgraph`, add `graph.add_node("<intent>_subgraph", build_<intent>_subgraph().compile())`, wire it from `route_by_intent`, and add an edge from `<intent>_subgraph` to `memory_save_node`.
5. Add a return branch in `route_by_intent` (`_nodes_classify.py`) for the new intent string.

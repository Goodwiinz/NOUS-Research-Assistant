# Agent audit round 1 — started 2026-08-16
Source: opencode session, 4 parallel explore agents over backend/src/services/agent + backend/src/api/agent

| ID | Finding (one line) | Sev | Status | Owner | PR | Updated |
|----|--------------------|-----|--------|-------|----|---------|
| H1 | `_tool_ingest_arxiv` (tools_impl.py:1225) missing `current_user` auth guard — full ingest pipeline runs unauthenticated; also phantom document_ids via getattr fallback (1345) | high | fixed | clawd | #1439 | 08-17 |
| H2 | terminal `set_job` → `record_job_status(raise_on_error=True)` (job_store.py:345) raises before L1/Redis writes → handler task dies, job stuck "running" 30min, sweeper overwrites real error (agent_execution_service.py:2374-2420) | high | fixed | clawd | #1442 | 08-17 |
| H3 | streaming.py:1250-1253 degraded path uses raw client `thread_id` as checkpoint key when `_resolve_thread` returns None → cross-tenant checkpoint read/write, HITL confirm hijack; `/execute` sanitizes this case, `/stream` does not | high | fixed | clawd | #1437 | 08-17 |
| M1 | `tool_executions` never reset per turn (_nodes_classify.py:220-240) → memory_save gate + tools_used polluted by prior turns; noise-recall bug | med | refuted | — | — | 08-17 |
| M2 | ownership-check exception → `scoped_project_id=None` → org-wide DO KB read (_nodes_rag.py:187-189, 399-411) — fail-open widening on DB blip | med | fixed | clawd | #1443 | 08-17 |
| M3 | bare-UUID fallback stores any pasted UUID as current_project_id (graph.py:36-37, _nodes_rag.py:694-740) — no ownership check on propagation path | med | fixed | clawd | #1443 | 08-17 |
| M4 | stale `last_user_msg` for multimodal turns (_nodes_llm.py:264-280) → canned greeting answers image-only turn, image never sent | med | fixed | clawd | #1438 | 08-17 |
| M5 | multimodal content raises in `is_conversational` (_nodes_memory.py:166-179, outside try) → gather swallows, memory recall silently dropped every multimodal turn | med | fixed | clawd | #1438 | 08-17 |
| M6 | `return_exceptions=True` (tools_impl.py:3023-3056) — total connector failure returns success-shaped empty result, dedupe blocks retry, misreported to user | med | fixed | clawd | #1439 | 08-17 |
| M7 | invalid connector name silently degrades to fan-out across ALL connectors (tools.py:771-776, tools_impl.py:2992-3018) — typo → ~250-connector HTTP burst | med | fixed | clawd | #1505 | 08-20 |
| M8 | wrapper silently truncates paper_ids to 10 (tools.py:270) — 15 requested → 10 land, "Ingested 10 of 10", 5 vanish, impl's >10 error unreachable | med | fixed | clawd | #1505 | 08-20 |
| M9 | `_resume_agent_graph` pins asyncpg connection up to 360s (agent_execution_service.py:2442, 2558-2595); same in `_run_agent_graph` skill-off path → pool exhaustion | med | fixed | clawd | #1441 | 08-17 |
| M10 | `set_job` monotonic guard compares stale pre-await `existing` (job_store.py:311-361) → older status resurrects newer cross-worker; redis_only has no guard | med | fixed | clawd | #1442 | 08-17 |
| M11 | circuit-breaker exit + HITL-deny leave dangling tool_calls (_factory.py:182-183, 390-401) → stale previous-turn answer persisted as COMPLETED assistant row | med | fixed | clawd | #1440 | 08-17 |
| M12 | get_thread_messages count query omits `before` cursor filter (execute.py:1321-1330) — documented count/filter-drift bug class | med | fixed | clawd | #1444 | 08-17 |
| M13 | `replay_buffered_stream` emits no heartbeats (streaming.py:922-969) — proxies kill resumed SSE during silent gaps, reconnect loop | med | fixed | clawd | #1445 | 08-17 |
| L1 | sanitizer skips tool_call with empty/missing id — AIMessage keeps unanswered tool_call → 400 + 3 retries (graph.py:100-103) | low | fixed | clawd | #1443 | 08-17 |
| L2 | HITL audit-row failure swallowed at DEBUG (_nodes_tools.py:161-174) — compliance record missing invisibly | low | fixed | clawd | #1440 | 08-17 |
| L3 | fast_path LLM stream iterator never closed on early exit; persist failure suppressed silently (fast_path.py:144-168) | low | fixed | clawd | #1445 | 08-17 |
| L4 | `_LLM_CACHE` key omits credentials (graph.py:244-246) — stale clients after secret rotation without restart | low | fixed | clawd | #1443 | 08-17 |
| L5 | `failed_papers` never reconciled after stub-path success (tools_impl.py:1272-1288) — contradictory payload → false failure report | low | fixed | clawd | #1439 | 08-17 |
| L6 | `list_project_documents` bypasses status mapping (tools_impl.py:2010-2012) — "completed" vs "indexed" inconsistent for same doc | low | fixed | clawd | #1439 | 08-17 |
| L7 | compare_documents cap mismatch: wrapper 10, impl 5 (tools.py:80,528 / tools_impl.py:2299) — guaranteed error loop 6-10 | low | fixed | clawd | #1505 | 08-20 |
| L8 | `export_bibliography` never caps document_ids (tools.py:676-692) — unbounded IN-clause | low | fixed | clawd | #1505 | 08-20 |
| L9 | `execute_code` falls back to thread_id="default" (tools.py:723) — shared stateful sandbox, cross-conversation leakage | low | fixed | clawd | #1505 | 08-20 |
| L10 | project_service.py:72 `search` ilike unescaped — wildcard injection broadens match | low | fixed | clawd | #1444 | 08-17 |
| L11 | `_set_job` bypasses L1 monotonic guard, no `_seq` (agent_execution_service.py:119-156) | low | fixed | clawd | #1441 | 08-17 |
| L12 | failed Redis init leaks client per retry (job_store.py:129-135) | low | fixed | clawd | #1442 | 08-17 |
| L13 | memory namespace lacks organization_id (memory.py:168,194,266) vs org-scoped durable table | low | open | — | — | 08-16 |
| L14 | stream_buffer non-atomic rpush/ltrim/expire — orphan no-TTL key possible; trim gives no gap signal (stream_buffer.py:64-68) | low | fixed | clawd | #1445 | 08-17 |
| L15 | `read_after` materializes+parses whole 5000-frame buffer per poll (stream_buffer.py:76-78) | low | fixed | clawd | #1445 | 08-17 |

## Log
- 2026-08-20: M7/M8/L7/L8/L9 fixed (#1505 — tool wrappers no longer rewrite the model's request).
- 2026-08-17 (backfill): merged-fix status reconciled from commit bodies — H3 (#1437), M4/M5 (#1438), H1/M6/L5/L6 (#1439), M11/L2 (#1440), M9/L11 (#1441), H2/M10/L12 (#1442), M2/M3/L1/L4 (#1443), M12/L10 (#1444), M13/L3/L14/L15 (#1445). M1 REFUTED: both graph entry points already write a fresh "tool_executions": [] into initial_state every turn, which LangGraph applies as a last-value channel write before preprocessing_node runs — verified against the real AgentState with an in-memory checkpointer across two turns (ainvoke and astream_events). No change was made for it. 6 rows remain open (M7, M8, L7, L8, L9, L13), no highs.
- 2026-08-16: audit created. 4 explore agents (graph/nodes, tools, subgraphs/execution, API). Verified-clean: checkpointer URL transform, loop bounds, tenant scope on doc/citation/KG queries, project ownership in tools, HITL gating on destructive tools, thread/job/confirm ownership checks (except H3 degraded path), thread summarization session usage, compactor message pairing, tool_dedupe keying.

This is a synthesis task — all findings are already provided and verified. I'll write the report directly.

# GraphRAG / Knowledge-Tree Subsystem — Engineering Audit Report

## 1. Summary

This audit of the GraphRAG / knowledge-graph subsystem and knowledge-tree UI surfaced **51 verified issues** spanning correctness, security, performance, concurrency, and frontend rendering. The defining problems are structural: the entity-write path has **no idempotency or uniqueness constraint** (duplicate nodes on every re-ingest and under concurrency), the API layer has **multiple unscoped/admin-ungated endpoints** enabling cross-tenant data injection and a total graph wipe by any authenticated user, and nearly every read path is built on **N+1 query loops and unbounded variable-length Neo4j traversals** that are further executed **synchronously on the asyncio event loop**. Tenant-scoping — the single hottest filter in the service — runs against an index that targets a property name (`source_document`) that does not exist on the nodes (`source_document_id`), so every org-scoped query is a full label scan. Several user-facing features are simply broken (entity-detail dialog never opens, BFS path-finding always returns empty, `/health` endpoint 422s).

**Counts by severity:** Critical: **2** · High: **17** · Medium: **23** · Low: **9**

By area: neo4j-perf 14 · entity-extraction 9 · kg-api 12 · graph-algos 8 · frontend-render 6 · concurrency-tx 4 (areas overlap across severities).

## 2. All Issues

| # | Sev | Area | File:Line | Issue | Fix (short) |
|---|-----|------|-----------|-------|-------------|
| 1 | Critical | entity-extraction | knowledge_graph_service.py:1409-1448 | `upsert=True` ignored; every write `CREATE`s a new UUID node → unbounded duplicate entities, no cross-doc linking | MERGE on `(canonical_key, type)`; back with uniqueness constraint; honor `upsert` flag |
| 2 | Critical | kg-api | knowledge_graph.py:1095-1115 | `/schema/reset` has no admin gate; any user can `MATCH (n) DETACH DELETE n` the whole cross-tenant graph | Add `ADMIN` role check (mirror fix-null-types); ideally remove global wipe from API |
| 3 | High | neo4j-perf | knowledge_graph.py:479-500 | `search_graph` N+1: 1 + N rel queries + ~O(n²) unbounded `find_paths` per call (~247 serial queries at max_results=50) | Batch rel fetch into 1 query; single UNWIND path query; offload via `to_thread` |
| 4 | High | neo4j-perf | knowledge_graph.py:938-944 | `get_entity_visualization` issues one `get_relationships` per node (up to 200), then discards most in Python | Single batched both-endpoints-in-set Cypher query |
| 5 | High | neo4j-perf | knowledge_graph_service.py:1073,1141,1264 | Variable-length, undirected, untyped `[*1..max_depth]` traversals; combinatorial fanout on hub nodes | Add `:RELATED_TO` type; use `allShortestPaths`/`apoc.path`; lower `le=5` cap |
| 6 | High | neo4j-perf | knowledge_graph_service.py:1141-1159 | `get_neighborhood` collects ALL paths then `[0]`; LIMIT applied last so it doesn't bound the work | BFS shortest-path expansion with early stop (APOC `expandConfig` / `shortestPath`) |
| 7 | High | neo4j-perf | knowledge_graph.py:857-869 | `get_document_entities` fetches 1000 org entities then filters in Python; >1000 → silent truncation | Dedicated `get_entities_by_document`; index `source_document_id` |
| 8 | High | neo4j-perf | neo4j_optimizer.py:238-243,308-313 | Indexes property `source_document` but data/queries use `source_document_id` → indexes empty, full scans | Rename indexed property; change composite type to RANGE |
| 9 | High | graph-algos | graph_algorithms.py:484-503 | Dijkstra reconstruction runs a per-node query inside per-path loop (N+1, ~80 round-trips) | Project node props via `gds.util.asNode` in the GDS query |
| 10 | High | graph-algos | graph_algorithms.py:544 | `*1..$max_depth` parameter inside var-length bound is illegal Cypher → BFS always errors → empty paths | Use validated literal via f-string (`safe_depth` clamped); narrow `except` |
| 11 | High | entity-extraction | llm_entity_extraction.py:55-79 | `chunk_text` emits oversize chunks when one paragraph exceeds max_tokens → LLM error/truncation, data loss | Pre-pass token-split oversize paragraphs before accumulation |
| 12 | High | concurrency | knowledge_graph_main.py:107-114,220-235 | No `(name,type,tenant)` uniqueness constraint; concurrent ingest of same entity races → duplicates | Add backing uniqueness constraint + switch all CREATEs to MERGE |
| 13 | High | security/kg-api | knowledge_graph.py:155-165 | `create_entity` has no org scoping; client `source_document_id` trusted → cross-tenant injection | Add `db` dep; validate `source_document_id ∈ org_doc_ids` |
| 14 | High | security/kg-api | knowledge_graph.py:586-621 | `create_merge_job` calls `get_entity` unscoped; NULL-source orphans bypass cross-org check | Pass `source_document_ids=org_doc_ids` to `get_entity`; 404 on miss |
| 15 | High | bug/kg-api | knowledge_graph_main.py:518-582 | Neo4j tx + Postgres commit non-atomic; missing explicit `tx.commit()` → divergence; silent rel drops | Explicit `tx.commit()`; order commits + rollback; record None-endpoint errors |
| 16 | High | frontend-render | EntityGraph.tsx:124-131,261-292 | Zoom buttons build a fresh `d3.zoom()` each click → state desync, fights wheel-zoom | Store `zoomRef`, reuse bound instance in handlers |
| 17 | High | frontend-render | KnowledgeGraph.tsx:287,513-522,802-803 | `setShowDetails` never called → entity-detail dialog can never open; whole detail UI dead | Drive `Dialog open` off `selectedNode` |
| 18 | High | concurrency | knowledge_graph.py:545-556 | Sync blocking Neo4j driver called directly in async routes → stalls event loop | `await asyncio.to_thread(...)` for all KG calls (or async driver) |
| 19 | High | concurrency | knowledge_graph_service_improved.py:31-62 | Async wrapper runs blocking call inline (`func(...)` not offloaded) → hidden event-loop block | `await asyncio.to_thread(func, ...)`; lock the retry reset |
| 20 | Medium | neo4j-perf | knowledge_graph_service.py:525,540-546 | `e.name CONTAINS $query` can't use index → full label scan every search; fulltext index unused | Use `db.index.fulltext.queryNodes`; sanitize Lucene input |
| 21 | Medium | neo4j-perf | knowledge_graph_service.py:1549-1605 | `get_graph_analytics` runs 5 sequential full scans of the org-scoped set | Add source_document_id index; derive totals from distribution queries |
| 22 | Medium | correctness | knowledge_graph_service.py:1206-1239 | Neighborhood edge dedup synthesizes fake direct edges (product strength, wrong type); intermediates dropped | Emit real per-hop chain from `path_node_ids`/`path_rels`; add intermediate nodes |
| 23 | Medium | bug | knowledge_graph_service.py:1313-1341 | `find_paths` `KeyError` on `r["source_entity_id"]` (not a stored prop); `min()` on empty → swallowed, returns [] | Use `rel.start_node["id"]`/`end_node["id"]`; `.get` defaults; guard min |
| 24 | Medium | graph-algos | layout_algorithms.py:56-138 | Force layout O(iter·n²), no node cap, no `await` → blocks event loop for seconds-minutes | Node-count guard; periodic `await asyncio.sleep(0)`/`to_thread`; Barnes-Hut |
| 25 | Medium | graph-algos | layout_algorithms.py:527-544 | Hierarchy level assignment loops forever on cycles/self-loops; arbitrary cap → garbage levels | Kahn's topo sort; skip self-loops; dedupe symmetric pairs |
| 26 | Medium | graph-algos | graph_algorithms.py:678-691 | `_get_dominant_entity_type` per-community query (N+1); also not tenant-scoped | Fold modal type into Louvain stream; add tenant filter |
| 27 | Medium | entity-extraction | processing_tasks.py:372-376,560,595-597 | `get_event_loop().run_until_complete` deprecated/crashes on 3.12 → whole-doc extraction loss | Use `asyncio.run(...)`; hoist service instantiation out of loop |
| 28 | Medium | correctness | llm_entity_extraction.py:98-136 | Merge key ignores type → "Apple" ORG and PRODUCT collapse; downstream (type,name) lookup misses → dropped rels | Key on `(canonical_name, type)` tuple |
| 29 | Medium | correctness | llm_entity_extraction.py:300-339 | Timeout checked only per-batch; skipped chunks reported as "processed", error stays None → silent data loss | Track attempted/skipped; per-call `wait_for`; propagate error |
| 30 | Medium | bug | llm_entity_extraction.py:187-205 | `float(confidence)`/aliases unvalidated → one bad field aborts whole chunk's entities | Per-entity try/except; coerce+clamp confidence; validate aliases list |
| 31 | Medium | perf | llm_entity_extraction.py:252-275; knowledge_graph_service.py:1366-1378 | One LLM call/chunk + one CREATE/entity in Python loop (N+1 graph writes) | UNWIND batch writes grouped by entity type; per-row fallback on error |
| 32 | Medium | perf/kg-api | arxiv_knowledge_graph.py:19-43,283,303 | Request models lack ge/le bounds; `max_results` feeds unbounded arXiv ingest | Bound `max_results` (ge=1,le=500); bound `days` |
| 33 | Medium | perf/kg-api | knowledge_graph.py:480-500 | `search_graph` O(N²) all-pairs paths + N+1 rel fetch; `max_results` unclamped | Cap to ~10 entities; batch rel query; single path query; offload |
| 34 | Medium | correctness/kg-api | knowledge_graph.py:856-869 | `get_document_entities` loads 1000 org entities, Python filter, silent truncation (dup of #7 at API) | Scope query to `[document_id]` after org-membership check |
| 35 | Medium | concurrency | knowledge_graph.py:56-77; service 1064/1087 | Sync `get_db_sync` + sync Neo4j in async handlers block loop | Make handlers `def` (threadpool) or `to_thread`; cache org-doc-ids |
| 36 | Medium | bug/kg-api | knowledge_graph_main.py:815-839 | `health_check(app)` → FastAPI treats `app` as required query param → 422/AttributeError | `health_check(request: Request)`; use `request.app.state` |
| 37 | Medium | bug/kg-api | knowledge_graph_main.py:366-401 | `update_entity` sets `updated_at` to literal string `'datetime()'` → corrupt timestamps | Inline Cypher `e.updated_at = datetime()` |
| 38 | Medium | memory-leak | EntityGraph.tsx:104-118,250-258 | D3 simulation never stopped; each prop change stacks a live ticking simulation; none stopped on unmount | `simulationRef`; `.stop()` before re-render and in cleanup |
| 39 | Medium | memory-leak | InteractiveKnowledgeGraph.tsx:127-130,228-233 | Listeners added on `window`/`mouseout`, removed from `canvas`/`mouseleave` → never removed, leak per theme toggle | Remove from same target/type; guard with `interactive` |
| 40 | Medium | perf/frontend | KnowledgeGraph.tsx:475-494 | 300-iteration force solve in a `useMemo` on render path; re-runs on every reload → UI freeze | Move to `useEffect`/rAF `simulateStep`; cap iterations; Web Worker |
| 41 | Medium | concurrency | knowledge_graph_service.py:1139-1159 | `get_neighborhood` enumerates all paths before LIMIT (dup of #6, concurrency angle) + blocks loop | Bound candidate set before collect; run_in_executor; lower depth cap |
| 42 | Low | security/perf | knowledge_graph_service.py:532-536,911-917 | f-string interpolation of types → plan-cache thrashing + future injection footgun | Parameterize with `IN $types` (mirror get_all_entities) |
| 43 | Low | perf | knowledge_graph_service.py:756-770 | `get_all_relationships` ORDER BY unindexed `r.created_at` + SKIP/LIMIT → re-sort whole set per page | Index `r.created_at`; keyset pagination |
| 44 | Low | graph-algos | layout_algorithms.py:374-382 | Spiral step constant 0.5 rad; radius unbounded → nodes off-canvas beyond ~60 nodes | Scale `b` to fit viewport; clamp radius to half min-dimension |
| 45 | Low | graph-algos | layout_algorithms.py:420-436 | Concentric uses `list.index()` per node → O(n²); off-by-one → ZeroDivision → silent random fallback | Precompute per-circle index keyed by node.id |
| 46 | Low | correctness | graph_algorithms.py:360-404 | Degree centrality double-counts self-loops; normalization relies on implicit ORDER BY | Self-loop-safe count via `CASE WHEN o <> e` |
| 47 | Low | correctness | entity_extraction_service.py:136-225; processing_tasks.py:393 | `datetime.now()` (naive local) mixed with `utcnow()`/`datetime()` → wrong recency comparisons | `datetime.now(timezone.utc)` everywhere |
| 48 | Low | perf/kg-api | knowledge_graph.py:932-944 | `get_entity_visualization` drops min_strength + N+1 rel fetch (dup of #4) | Single `get_relationships_among` batched query |
| 49 | Low | bug/kg-api | knowledge_graph_main.py:734-785 | `extract_entities_from_document` passes `redis_client=None`, uses stubbed `get_document_content` | Wire real content fetch; use DI instead of direct handler call |
| 50 | Low | perf/kg-api | knowledge_graph.py:239-255,386-389 | Large org-wide doc-ID IN-list shipped to Neo4j twice/request; full Postgres scan each call | Index source_document_id + cache org-doc-ids; or stamp `organization_id` on nodes |
| 51 | Low | bug/frontend | EntityGraph.tsx:333-345 | d3 `<script>` cleanup `removeChild` can throw; no `onerror`; duplicate loads on concurrent mounts | Guard `parentNode`; add `onerror`; module-level singleton load promise |

## 3. Fix First — Top 5 by Impact / Effort

**1. Admin-gate `/schema/reset` (#2) — Critical, ~5 minutes.**
Highest impact-to-effort ratio in the entire report: any authenticated user can irreversibly destroy the global graph. At the top of `reset_graph_schema` (knowledge_graph.py:1096) add:
```python
if current_user.role != UserRole.ADMIN:
    raise HTTPException(status_code=403, detail="Admin access required")
```
`UserRole` is already imported (line 42). Follow up by removing the unscoped `MATCH (n) DETACH DELETE n` from the request-facing API entirely.

**2. Idempotent entity writes + uniqueness constraint (#1, #12) — Critical, ~half day.**
Together these are the root cause of graph duplication on both re-ingest and concurrent ingest. Steps: (a) add `CREATE CONSTRAINT entity_canonical_unique IF NOT EXISTS FOR (e:Entity) REQUIRE (e.canonical_key, e.type) IS UNIQUE` to the constraint list (service.py:251-257); (b) in `_create_entity_in_transaction` (1417-1432) replace `CREATE` with `MERGE (e:Entity {canonical_key:$key, type:$entity_type}) ON CREATE SET ... ON MATCH SET e.updated_at=datetime(), e.confidence_score = CASE WHEN $confidence_score > e.confidence_score THEN $confidence_score ELSE e.confidence_score END RETURN e`, where `key = request.name.strip().lower()`; (c) return `e.id` from the matched node, not the freshly generated UUID; (d) branch on `request.upsert`. The constraint is what makes the MERGE concurrency-safe.

**3. Fix the dead tenant-scoping index (#8) — High, ~15 minutes.**
The hottest filter in the subsystem (`source_document_id IN [...]`, used by list/count/search/analytics/neighborhood/paths) currently has no usable index because the optimizer indexes a non-existent property. In neo4j_optimizer.py change line 240 `["source_document"]` → `["source_document_id"]`, line 310 `["type","source_document"]` → `["type","source_document_id"]`, and change that composite's type from `"COMPOSITE"` to `"RANGE"` so the true multi-property index is emitted. This single rename turns every org-scoped full scan into an index seek.

**4. Offload sync Neo4j calls off the event loop (#18, #19, #35) — High, ~1-2 hours.**
One slow traversal currently freezes every concurrent request on the worker. Two low-risk options: convert the blocking async route handlers in knowledge_graph.py to plain `def` (FastAPI runs them in its threadpool), and in `knowledge_graph_service_improved.py:46` replace `result = func(...)` with `result = await asyncio.to_thread(func, ...)`. No service rewrite required. This restores concurrency across the board and compounds the value of the perf fixes below.

**5. Collapse the N+1 read paths (#3, #4, #33, #48) — High, ~half day.**
Add two batched service methods — `get_relationships_for_entities(ids, ...)` and `get_relationships_among(entity_ids, source_document_ids)` — each a single `MATCH (s:Entity)-[r]-(t:Entity) WHERE s.id IN $ids AND t.id IN $ids RETURN r, type(r), s.id, t.id`. Replace the per-entity loops in `search_graph` (479-500) and `get_entity_visualization` (938-944), cap `search_graph` expansion to ~10 entities, and replace the nested `find_paths` loop with a single bounded UNWIND query. This turns ~200-250 serial queries per request into 2-3.

## 4. Themes — Recurring Root Causes

- **No identity/idempotency in the write path.** `CREATE` with a fresh UUID and no uniqueness constraint is the root of #1 and #12, and amplified by the type-dropping merge key (#28). The graph cannot do its one job — cross-document entity linking — and duplicates accumulate on every re-ingest and concurrent write.
- **Sync blocking driver under an async facade.** The synchronous Neo4j driver is called directly from `async def` routes (#18), through an "async" retry wrapper that doesn't offload (#19), behind sync SQL dependencies (#35), and inside CPU-bound layout code (#24, #40). Every slow query serializes the whole worker. This is the single most pervasive concurrency defect.
- **Unbounded / N+1 graph access.** Per-entity loops (#3, #4, #9, #26, #33, #48), variable-length undirected untyped traversals (#5, #6, #41), and 5 sequential analytics scans (#21) all share the pattern of doing in many round-trips (or one exponential traversal) what should be one bounded query. LIMIT applied last (#6, #41) gives false safety.
- **Indexes that don't match the data/queries.** The tenant filter indexes the wrong property name (#8), search uses `CONTAINS` which no index serves (#20), relationship sort/filter keys are unindexed (#43), and the IN-list scaling problem (#50) all stem from index strategy diverging from actual query shapes.
- **Missing tenant scoping at the API boundary.** `create_entity` (#13), `create_merge_job` (#14), and `/schema/reset` (#2) each skip the org-scoping guard that sibling endpoints already apply, enabling cross-tenant injection, oracle, corruption, and total wipe.
- **Silent failure / swallowed errors.** Broad `except` returning empty paths (#10, #23), timeout-skipped chunks reported as processed (#29), one bad LLM field aborting a whole chunk (#30), non-atomic dual-store commits (#15) — failures are masked as success, producing silent data loss that evades tests.
- **Frontend lifecycle leaks & dead state.** D3 simulations never stopped (#38), listeners removed from the wrong target (#39), zoom behavior recreated per click (#16), a dialog driven by never-set state (#17), and synchronous layout on the render path (#40) — all stem from React/D3 lifecycle and ref management being skipped.

## 5. Quick Wins — Trivial Effort, High Value

- **#2** Admin gate on `/schema/reset` — 3-line guard prevents catastrophic data loss.
- **#8** Rename the indexed property `source_document` → `source_document_id` (+ RANGE composite) — instantly indexes the hottest filter in the service.
- **#17** Drive the entity-detail `Dialog` off `selectedNode` — one-line fix revives an entire dead UI panel.
- **#10** Replace `*1..$max_depth` with a clamped literal f-string — un-breaks BFS path-finding, which currently always returns empty.
- **#36** `health_check(request: Request)` + `request.app.state` — fixes liveness/readiness probes that currently 422.
- **#37** `e.updated_at = datetime()` inline instead of the literal string `'datetime()'` — stops silent timestamp corruption on every update.
- **#27** Swap `get_event_loop().run_until_complete` → `asyncio.run` — prevents whole-document extraction crashes on Py3.12.
- **#39** Remove window listeners from `window`/`mouseout` (matching the add) — stops a per-theme-toggle handler+closure leak.
- **#28** Change merge key to `(canonical_name, type)` — stops semantically distinct entities (Apple ORG vs PRODUCT) collapsing and dropping relationships.

# Knowledge Base + Graph Audit (2026-06-08)

Read-only audit of the **knowledge base / retrieval** subsystems (DigitalOcean KB,
embeddings, vector + hybrid search, rerank) plus a **re-audit of the knowledge
graph** after the 51-issue fix wave and the #50 org-scoping read-flip.

38 findings. Severity-ordered within each subsystem. `file:line` precise. No fixes
applied in this document — it is the finding list. The graph GDS-1.x-vs-2.x item was
already known (bonus finding); it reappears here confirmed against the deploy.

---

## A. DigitalOcean Knowledge Base (`src/services/do_kb/`, `api/search/vectors.py`)

| # | Sev | File:Line | Problem | Fix |
|---|-----|-----------|---------|-----|
| A1 | **critical** | `ingest.py:203-207` | Bulk `sync_documents_to_kb` collects KB uuids via lazy `doc.organization.do_kb_uuid` in async ctx → `seen_kbs` almost always empty → **no indexing job ever triggered**; docs added as data sources but never indexed. | Resolve `kb_uuid` explicitly per org, not via lazy rel. |
| A2 | high | `backfill.py:139-160` | N+1 + per-item commit: each doc re-runs `ensure_kb_for_org` (org get + advisory lock) + `add_spaces_data_source` (network) + `session.commit()`. 25-doc batch = 25 locks + 25 commits. | Resolve KB once/org; commit per batch. |
| A3 | high | `client.py:78` | New `httpx.AsyncClient` built+torn down **per request** (and per retry) → no pooling/keep-alive across many per-doc calls. | One long-lived pooled `AsyncClient`. |
| A4 | high | `client.py:85-96` | Retryable-status branch `continue`s without saving status; if the **last** attempt is 429, loop raises `"exhausted retries: None"` — real status lost. | Capture + raise with `status_code` on final attempt. |
| A5 | high | `client.py:85-96` | No `Retry-After` handling on 429; fixed `2**attempt` backoff ignores DO hint. | Honor `Retry-After`. |
| A6 | medium | `resolve.py:82-93` | Unbounded `OR` of `2*len(storage_keys)` predicates, no cap → planner blowup on large `top_k`. | Cap/chunk keys; index a normalized key. |
| A7 | medium | `ingest.py:159-167` | Non-idempotent: data source added on DO before commit; commit failure → next run re-adds **duplicate** source. | Idempotent add (check by key) / reconcile. |
| A8 | medium | `ingest.py:61-87` | `S3StorageHelper().upload_file` is **sync blocking** inside `async def` → blocks event loop per doc. | `await asyncio.to_thread(...)`. |
| A9 | medium | `vectors.py:113-124` | `get_collection_stats` called **twice per collection** (value + `if`) → double Qdrant round-trips. | Compute once, reuse. |
| A10 | low | `vectors.py:43+` | Bare `except Exception` → all errors (incl 4xx) become 500. | Split client vs server errors. |
| A11 | low | `client.py:209` | `top_k` silently clamped to `[1,100]`; caller gets different count, no signal. | Validate/log clamp. |

Tenant isolation in `resolve.py` is correct (org + project membership, fails closed).

---

## B. Embeddings (`src/services/embedding/`)

| # | Sev | File:Line | Problem | Fix |
|---|-----|-----------|---------|-----|
| B1 | **critical** | `embedding_service.py:81,93,344,430,483` | Calls `SentenceTransformer.get_embedding_dimension()` — **not a real method** (it's `get_sentence_embedding_dimension()`); every ST path raises `AttributeError` → silently demoted to hash-stub (B-clean note). The good embedding path is effectively dead. | Use `get_sentence_embedding_dimension()`. |
| B2 | **critical** | `cohere_embed_service.py:130-137` | On batch failure, retries one-at-a-time (N+1) then injects `[0.0]*dim` **zero vectors** into the index → silently corrupts all downstream search. | Re-raise / mark failed; never store zero vectors. |
| B3 | high | `cohere_embed_service.py:89-148` | Partial batch failure can misalign returned embeddings vs input `texts`; caller maps by index assuming 1:1. | Guarantee positional 1:1 with placeholders. |
| B4 | high | `embedding_service.py:342,428` | Non-default `request.model` → brand-new `SentenceTransformer` loaded **per request** (no cache). | Cache models by name. |
| B5 | high | `embedding_service.py:362-371,453-461` | Silent cross-provider fallback to Azure on any error → mixes model-A and Azure vectors in one index, corrupting similarity. | Fail loudly for indexing; no cross-provider fallback. |
| B6 | high | `cohere_embed_service.py:164,222` | New `AsyncClient` per batch; no 429/5xx retry. | Pooled client + retry. |
| B7 | high | `embedding_service.py:127-129,419,467` | Hard-coded provider-inconsistent dim defaults (Azure 1536 / ST 384 / Cohere setting); wrong default → vector-store dim mismatch. | Derive dim from actual vector length. |
| B8 | medium | `embedding_service.py:493` | `generate_batch_embeddings` (async) called without `await` in `test_embedding_quality` → coroutine attr access raises. | `await` it. |
| B9 | medium | `embedding_service.py:316-320` | `provider=="cohere"` but disabled → silently falls to ST (wrong dims) instead of erroring. | Explicit provider-unavailable error. |
| B10 | low | `embedding_service_simple.py:60` | Hash "embedding" repeats every ~11 dims (very low entropy) — last-resort stub only; dangerous if it's silently the active path (see B1). | Document non-semantic; ensure not silently active. |
| B11 | low | `embedding_service.py` | No embedding cache: identical text re-embedded every call (paid Cohere/Azure cost). | Content-hash cache. |

`models/vector.py` clean.

---

## C. Vector / Hybrid Search + Rerank (`src/services/search/`, diagnostics)

| # | Sev | File:Line | Problem | Fix |
|---|-----|-----------|---------|-----|
| C1 | **critical** | `hybrid_vector_search_service.py:264-282` | Fusion/dedup keys on Qdrant **point id**, not `document_id` → chunks of same doc never deduped; treated as distinct docs. | Key on `payload["document_id"]`. |
| C2 | **critical** | `hybrid_vector_search_service.py:130-172,259` | Two different fusion maths by branch: true RRF vs linear `(1-w)*dense + w*bm25` on **raw cosine** (the runtime path) — scales mixed, weights mismatch config. | One consistent fusion; normalize before linear. |
| C3 | **critical** | `vector_service.py:104,182,587` | `except (ValueError,KeyError,Exception)` treats **every** error as "collection missing" → real conn/auth errors masked as "create collection". | Catch specific not-found only. |
| C4 | high | `vector_search_service.py:307-339` | Per-doc BM25 re-encode in loop over full doc text → O(N·M). | Batch/precompute. |
| C5 | high | `hybrid_vector_search_service.py:325-333` | Same per-doc BM25 re-encode + `indices.index()` O(n) inside comprehension. | Dict indices→values once. |
| C6 | high | `vector_search_service.py:578-590` | Reindex loop: per-doc full scroll-delete + index, sequential, unbounded. | Batch + bound concurrency. |
| C7 | high | `cohere_rerank_service.py:344-404` | `results[rr.index]` mapping breaks when fallback rerank / `top_n` slice shifts indices → `IndexError`/wrong doc. | Map by `document_id`, bound-check. |
| C8 | **high** | `vector_service.py:286`, `hybrid:205` | Org filter added **only if** `request.organization_id` truthy; no enforcement → caller omitting org queries **all orgs** (cross-tenant leak). | Require + validate non-empty org. |
| C9 | medium | `vector_service.py:335,444; hybrid:91` | Sync `httpx.Client`/`requests.post` called from async `search_documents` → blocks event loop. | `asyncio.to_thread` / async client. |
| C10 | medium | `vector_service.py:512-519` | `get_collection_stats` uses v1.16 client attrs while file bypasses client for v1.7 server → likely raises; `disk_data_size` reads a bool. | Use raw REST; fix size source. |
| C11 | medium | `hybrid:62-104` | `search_dense` bare `except`→`[]`: failed Qdrant indistinguishable from no matches; hardcoded `timeout=10`, no retry. | Distinguish error vs empty; retry. |
| C12 | medium | `cohere_rerank_service.py:200` | Fallback score `1.0-(i*0.1)` goes negative after 10 docs; meaningless ordering past 10th. | Bounded rank-stable scheme. |
| C13 | low | `vector_search_service.py:415` | Entity filter key `"metadata.entity_type"` may not match nested payload dict without payload index → always-empty filter. | Verify key path/index. |
| C14 | low | `vector_service.py:316` | `score_threshold=None` sent (not stripped) → Qdrant v1.7 may reject. | Omit when None. |

Rerank Cohere primary path, diagnostics dataclasses, admin gating — clean.

---

## D. Knowledge Graph re-audit (`services/knowledge_graph/`, `api/search/knowledge_graph.py`)

| # | Sev | File:Line | Problem | Fix |
|---|-----|-----------|---------|-----|
| D1 | **critical** | `graph_algorithms.py:90-117,188-211,273-296,449-475,654-679` | GDS **1.x anonymous projection** syntax; deploy is Neo4j 5.15 + **GDS 2.x** which removed it → every GDS call throws → centrality degrades to fallback, Louvain/Dijkstra empty. (Known bonus finding, confirmed.) | Migrate to `gds.graph.project(name,...)` then run by name + drop. |
| D2 | **critical** | `graph_algorithms.py:83,181,266,…,904,938` | Analytics scope on `e.tenant_id`, but service write path stamps only `organization_id`/`source_document_id` (never `tenant_id`) → tenant-scoped analytics match **nothing**. | Scope analytics on `organization_id`. |
| D3 | **critical** | `graph_algorithms.py:85-88,…` | No tenant filter → `where_clause="1=1"` → PageRank/centrality/Louvain run over the **entire cross-org graph**; results leak across tenants. | Require validated tenant filter; refuse unscoped. |
| D4 | high | `api/knowledge_graph.py:754-763` | `extract_entities_from_document` looks up `Document` with **no `organization_id` filter** → any user can extract/attach entities to another org's doc. | Add `Document.organization_id == current_user.organization_id`. |
| D5 | high | `graph_algorithms.py:913,971` (imports 14-22) | `AnomalyInsight` / `GrowthTrendInsight` **referenced but not imported** → `NameError`; `detect_graph_anomalies` / `analyze_growth_trends` always return `[]`. | Import both from `analytics_models`. |
| D6 | high | `knowledge_graph_service.py:872-878` + `api:420-425` | `get_all_relationships` filters only `r.source_document_id`; bypasses the org read-flip, no `organization_id` path. | Scope by both endpoints' org via `_two_endpoint_scope`. |
| D7 | high | `knowledge_graph_service.py:1486-1503` | `find_paths` traversal is **untyped** `(start)-[*1..n]-(end)` (unlike the fixed siblings) → combinatorial fanout on hubs. | Type `[:RELATED_TO*1..{safe_depth}]` + clamp. |
| D8 | high | `graph_algorithms.py:96,194,279,660` | `nodeFilter` placed inside a *native* projection map where it's not valid → type/tenant filter silently ignored even when supplied. | Cypher projection / `nodeQuery` / post-filter. |
| D9 | medium | `graph_algorithms.py:78-84,…` | `entity_types` / `tenant_id` **f-string-interpolated** into Cypher (allowlist is the only barrier); `degree_centrality:352` already binds correctly. | Bind as `$param`. |
| D10 | medium | `knowledge_graph_service.py:1197-1237` | `get_relationship` undirected `(source)-[r]-(target)` → returned source/target may be swapped vs stored direction. | Match directed `-[r:RELATED_TO]->`. |
| D11 | medium | `api/knowledge_graph.py:632-637` | merge-jobs validates via `get_entity(source_document_ids=...)` not `organization_id` → org entities with NULL source-doc 404, blocking legit merges; inconsistent with read-flip. | Pass `organization_id`. |
| D12 | medium | `knowledge_graph_service.py:1869-1897` | `get_graph_analytics` scopes only `source.source_document_id` (target unconstrained) → counts edges to out-of-scope targets; no org path. | Scope both endpoints / add org path. |
| D13 | medium | `knowledge_graph_service.py:406` | `create_entity` ON MATCH `organization_id = coalesce(e.organization_id,$org)` → first-writer-wins; same canonical_key+type across orgs stays stamped to first org → read-flip hides it from true owner. | Include org in MERGE identity. |
| D14 | low | `knowledge_graph_service.py:1086,1235` | `get_relationships`/`get_relationship` pass raw neo4j `DateTime` to pydantic without `_convert_datetime` (unlike `_record_to_relationship`) → possible reject; `utcnow()` naive vs tz-aware. | Route via `_convert_datetime`; tz-aware now. |
| D15 | low | `api/knowledge_graph.py:432-447` | `create_relationship` tenant-violation → `RuntimeError` → generic **500** not 403/404. | Distinguish scope failure. |
| D16 | low | `knowledge_graph_service.py:1085,1121` | Dead `r.get("source_paper")` fallback no write path sets. | Remove. |

**Read-flip helpers verified clean:** `_entity_scope_predicate` / `_two_endpoint_scope`
param binding correct, aliases are caller-controlled constants (no injection), OR-logic
sound. Latent: if any caller ever passes BOTH org_id and doc-ids it widens scope — API
layer currently passes exactly one (XOR), so safe. `layout_algorithms.py` clean.

---

## Priorities

**Fix first (correctness/security, low effort):**
- D5 missing imports (`NameError` → analytics dead) — 1-line.
- D7 `find_paths` untyped traversal (perf/fanout) — mirror sibling fix.
- D4 + D11 extract-entities / merge-jobs org guards (tenant) — small.
- B1 ST method name (good embedding path dead) — 1-line × 5.
- C8 unguarded org filter in vector/hybrid (cross-tenant) — add enforcement.

**Silent corruption (high impact):**
- B2 zero-vector injection, C1 point-id dedup, C3 catch-all-as-not-found, A1 bulk ingest never indexes.

**Cross-cutting design:**
- D2/D3 `tenant_id` vs `organization_id` mismatch makes the whole analytics layer
  either empty or cross-org — decide one scoping key graph-wide.
- D1 GDS 1.x→2.x migration (blocked on confirming deploy GDS version; methods fall
  back safely meanwhile).

**Performance:** A2/A3 (per-doc KB resolve + client churn), B4/B6 (model reload, client
churn), C4/C5/C6 (BM25 re-encode loops, reindex N+1).

# Search Service

Hybrid document retrieval combining Qdrant vector search, PostgreSQL full-text search, and Neo4j knowledge-graph lookup. Results are fused with Reciprocal Rank Fusion (RRF) and optionally reranked by Cohere's cross-encoder before being returned to the caller.

## Query flow

```
query
  └─ route_search_query()          # decide which sources to activate
       ├─ fulltext_search_service  # PostgreSQL tsvector / ts_rank
       ├─ vector_search_service    # embed → Qdrant ANN, optional BM25 boost
       └─ knowledge_graph_service  # entity lookup (hybrid only, intent heuristic)
  └─ _fuse_search_results()        # weighted score fusion + diversity/recency boosts
  └─ _apply_cohere_reranking()     # cross-encoder rerank (skipped if disabled)
  └─ _apply_final_filtering()      # tag/document-id filters, pagination, title enrich
```

`SearchOrchestrator` (used by the newer agent-facing path) runs the same stages through a registered-executor pattern with `asyncio.timeout` per source and RRF via `ResultFusion`.

## Key files

| File                         | Purpose                                                                                                                                   |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `base.py`                    | `SearchQuery`, `SearchResult`, `SearchSource` enum, abstract `SearchExecutor`                                                             |
| `orchestrator.py`            | Async orchestrator: parallel executor dispatch, RRF fusion, Cohere rerank, Redis cache, Prometheus metrics                                |
| `hybrid_search_service.py`   | Legacy sync orchestrator used by the documents API; `search()` and `search_with_diagnostics()` with full `RetrievalTrace`                 |
| `vector_search_service.py`   | Embeds query (Cohere → Azure OpenAI → local fallback), calls `vector_service`, also handles document indexing and `reindex_all_content()` |
| `vector_service.py`          | Thin Qdrant client wrapper: insert/search/delete, circuit-breaker guarded                                                                 |
| `bm25_service.py`            | Produces `SparseVector` (k1=1.5, b=0.75, vocab=30 000) for BM25-dense hybrid inside `search_documents_hybrid()`                           |
| `fulltext_search_service.py` | PostgreSQL `to_tsvector` / `ts_rank` search with `<mark>` snippet highlighting                                                            |
| `semantic_search_service.py` | pgvector semantic primitive (GOO-435): `semantic_search()` over `document_embeddings`, org + Collection scoped, date window in the WHERE; off unless `SEMANTIC_SEARCH_ENABLED`. Embeddings come from `services/embedding/document_embedding_service.py` (`DOCUMENT_EMBEDDINGS_ENABLED`) |
| `cohere_rerank_service.py`   | Azure-hosted Cohere rerank API; `rerank()` async + `rerank_sync()` sync; falls back to original order on failure                          |
| `fusion.py`                  | `ResultFusion` — weighted RRF with multi-source boost (×1.2) used by `SearchOrchestrator`                                                 |
| `reranker.py`                | Thin wrapper around `cohere_rerank_service` used by `SearchOrchestrator`                                                                  |
| `cache.py`                   | Redis-backed result cache keyed on query text + filters                                                                                   |
| `metrics.py`                 | Prometheus counters/histograms for search latency and source failures                                                                     |
| `search_service.py`          | Thin facade wiring together older service instances                                                                                       |
| `search_quality_service.py`  | Offline evaluation helpers (precision@K, coverage scoring)                                                                                |

## Search modes

| Mode       | Sources activated                     | When routed                    |
| ---------- | ------------------------------------- | ------------------------------ |
| `SEMANTIC` | Vector only                           | `search_type=SEMANTIC`         |
| `FULLTEXT` | PostgreSQL FTS only                   | `search_type=FULLTEXT`         |
| `HYBRID`   | Vector + FTS + KG (intent permitting) | `search_type=HYBRID` (default) |

The KG arm activates only when the query contains entity-indicator words (`who`, `what`, `relationship`, `connected`, etc.).

Within the vector arm, `search_documents_hybrid()` applies an additional BM25 sparse-vector boost (dense weight 0.7, sparse weight 0.3) before returning to the fusion layer.

## Gotchas

- **SQL injection prevention.** Sort and filter fields are validated through `src/shared/enums.py` validated enums; raw string interpolation is never used in queries. The title-enrichment fallback (`_enrich_titles_from_db`) passes IDs via SQLAlchemy `text()` with a bound parameter, not concatenation.
- **Qdrant point IDs.** Qdrant requires UUIDs or integers. `vector_search_service` derives point IDs with `uuid.uuid5(NAMESPACE_DNS, chunk_id)` — do not pass arbitrary strings as point IDs or the upsert will fail silently on some client versions.
- **Circuit breaker on Cohere and Qdrant.** Both services are wrapped by `src.core.circuit_breaker.get_circuit_breaker()`. When the breaker is open, Cohere reranking degrades to original-order passthrough and Qdrant calls raise `ServiceUnavailableError`; callers must handle that exception.
- **Sync vs async.** `HybridSearchService.search()` is synchronous (uses `ThreadPoolExecutor` + `asyncio.run()`). Do not call it from inside a running event loop — it will deadlock. Use `SearchOrchestrator.search()` (fully async) for agent and streaming paths.
- **Reranking threshold.** `SearchOrchestrator` skips Cohere reranking when fewer than `min_results_for_rerank` (default 5) results are available. Tune this in `OrchestratorConfig` if recall matters more than latency at low result counts.
- **Score normalization in fusion.** `HybridSearchService._normalize_score()` divides fulltext scores by 10 to undo `fulltext_search_service`'s `ts_rank_cd(...) * 10` and clamps to `[0,1]`; KG entity scores (`EntityResponse.confidence_score`) are already 0-1 and pass through unchanged. If either source's scale changes, update `_normalize_score` to match or fusion will silently skew (see R2-H9).
- **KG results fuse on `Document.id`, not the entity id.** `_execute_knowledge_graph_search` maps each entity to its `source_document_id` and drops entities with none — fusing on the Neo4j entity uuid instead would key KG hits out of the document space fulltext uses, so they could never merge and cross-source coverage would always read 0 (see R2-H10).

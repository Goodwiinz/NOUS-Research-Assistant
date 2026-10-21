# Search Services

Document search for the backend. Live retrieval is PostgreSQL full-text search.
Qdrant is fully removed and there is no dense vector backend in the default
deployment. The pgvector semantic path exists but is flag-gated and off unless
`SEMANTIC_SEARCH_ENABLED` and `DOCUMENT_EMBEDDINGS_ENABLED` are turned on
(both default to `False` in `backend/src/core/config.py`). The DigitalOcean
Knowledge Base path lives in `services/do_kb/` behind `DO_KB_ENABLED`
(default `False`).

## Modules

| File | Role |
| --- | --- |
| `base.py` | Shared `SearchQuery`, `SearchResult`, `SearchSource` and the `SearchExecutor` ABC. Re-exported from `__init__.py`. |
| `fulltext_search_service.py` | `FullTextSearchService`: PostgreSQL `search_vector` / `plainto_tsquery` search behind `/documents/search`. Requires an organization scope and fails closed without one. |
| `keyword_search_service.py` | `keyword_search`: the lexical twin of `semantic_search` used by the agent discovery tool. Scope (organization, project ids or `collection_scope`) and the publication-date window are applied in one SQL statement, ranked with `ts_rank_cd`. |
| `semantic_search_service.py` | `semantic_search` / `semantic_search_strict`: nearest documents by cosine distance on pgvector `document_embeddings`. Returns `[]` (or raises `SemanticSearchUnavailable` in the strict form) when the flag is off. |
| `hybrid_search_service.py` | `HybridSearchService`: fuses full-text and knowledge-graph results. The vector arm was retired, so `SEMANTIC` maps to full-text. |
| `cohere_rerank_service.py` | `CohereRerankService`: optional Azure AI Cohere rerank with retry and a `RerankOutcome` that reports degraded runs. |
| `bm25_service.py` | `BM25Service` sparse vector generator. Its docstring still describes Qdrant hybrid search; that is a leftover, not a live path. |
| `search_quality_service.py` | Search quality evaluation metrics. |

## Rules

- Every query is organization scoped; keep the `organization_id` predicate when
  touching any of these files (see `docs/engineering/gotchas.md`).
- Soft-deleted documents and documents that have not finished processing never
  match.
- Count queries must apply the same filters as the result query.
- The earlier orchestrator, fusion, reranker, cache and metrics modules were
  removed (see the `__init__.py` docstring); do not look for them.

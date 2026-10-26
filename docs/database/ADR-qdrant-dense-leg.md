# ADR: Qdrant dense leg + /vectors API — remove, don't rebuild

> **Amendment 2026-10-17:** the ADR text below is preserved as written. In the current tree the Qdrant dense leg is gone (`services/search/hybrid_search_service.py` documents the retired vector arm, and `services/search/` has no `vector_service.py`), there is no `api/search/vectors.py` router, and `DO_KB_ENABLED` defaults to `False`. Live retrieval is PostgreSQL full-text search. Whether this ADR was formally accepted is not recorded here.

Status: **Proposed** (2026-06-09) · Blocks the remaining Qdrant→DO KB teardown
(see `QDRANT_REMOVAL_STATUS.md`).

## Context

Qdrant was dropped; DO KB is the retrieval backend. Several code paths still call
the dead Qdrant dense leg and silently return `[]`:

| Surface | Path | Real consumer? |
|---|---|---|
| `/api/v1/vectors/*` (18 endpoints) | `api/search/vectors.py` → `vector_service`/`vector_search_service` | **None** — no frontend call (grep empty); collection-mgmt/reindex/embeddings admin API |
| `/api/search` `search_type=VECTOR` | `search.py:199` → `vector_search_service.search` | No evidence of use; `HYBRID` degrades to FTS + KG |
| Hybrid dense leg | `hybrid_search_service` → `vector_search_service.search_documents` | Returns `[]`; FTS + KG legs still work |
| Entity vector search | `vector_search_service.search_entities` / `index_entity` | **None** — only the dead `/vectors` router. Real entity search is **Neo4j** (`knowledge_graph_service.search_entities`), which is alive |
| Search-quality / RAG eval | `search_quality_service.py:485`, `rag_evaluation_service.py:41` | Measure the dead vector path |

Semantic document retrieval **already runs on DO KB** through the agent
(`_nodes_rag.py` → `_try_primary_do_kb_read`). The Qdrant dense leg is a second,
now-defunct retrieval surface with no working backend.

## Decision

**Remove the Qdrant dense leg and the `/vectors` admin API. Do NOT rebuild it on
DO KB.**

Rationale:
- No consumer for `/vectors` or `/api/search` `VECTOR` type.
- Entity-vector search has no live consumer; Neo4j covers entity search.
- Rebuilding on DO KB would preserve an **unused** surface at high cost — and DO
  KB has no analog for collection management, entity vectors, or reindex endpoints.
- Hybrid `/api/search` stays functional on **FTS + KG** (Postgres full-text +
  Neo4j); the product's semantic retrieval lives in the agent's DO KB path.

If standalone semantic doc search in `/api/search` is later required, add a thin
DO-KB-backed dense leg then — as a new capability, not a Qdrant resurrection.

## Consequences / capability changes

- **Lost:** `/api/v1/vectors/*` admin API; `/api/search` `VECTOR`-only mode;
  Qdrant entity vectors. All currently non-functional anyway.
- **Kept:** `/api/search` `HYBRID`/`FULLTEXT`; Neo4j entity search; agent DO KB
  retrieval.

## Execution sequence (each step keeps the app bootable; verify boot + endpoints in a real env)

1. **Unmount + delete `/api/v1/vectors` router** (`api/search/vectors.py`, remove
   the include in `main.py:447`). Biggest dead surface, zero consumers.
2. **`/api/search`:** drop the `VECTOR` `search_type` branch (return 400
   "unsupported"); keep `HYBRID`/`FULLTEXT`/`KG`. **Fix the health probe** that
   labels the dead call `qdrant` and reports healthy (`search.py:725`).
3. **`search_quality_service` + `rag_evaluation_service`:** drop the
   `vector_search_service` dependency (remove the vector metric or route through
   FTS).
4. **Delete `vector_service.py` + `vector_search_service.py`** once steps 1–3
   remove all importers.
5. **Remove config `QDRANT_*` + `validate_qdrant_*`** and the `"qdrant"` circuit
   breaker — now unreferenced.
6. **`core/database_optimizations/*`** (orphaned, ~70 Qdrant refs, zero importers):
   delete the dead package or its Qdrant branches.

Steps 1–2 deliver most of the value (kills the broken/false-green surfaces) and
are independently shippable. Steps 4–5 are the final cleanup that lets `QDRANT_*`
leave the codebase entirely.

## Verification

No app/pytest run is possible in the authoring sandbox. Before merging each step:
`pytest tests/ -m unit`, then boot the API and hit `/api/search` (HYBRID),
`/api/search/search_quality`, and an eval run to confirm no regression and no
startup failure.

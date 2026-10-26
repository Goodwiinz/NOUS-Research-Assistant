# Qdrant Removal — Migration Status

> **Amendment 2026-10-17:** this status record was written on its 2026-06-08 audit date and is preserved as history. Current state: `DO_KB_ENABLED` defaults to `False` (`backend/src/core/config.py`), so DO KB is not the live retrieval backend. Live retrieval is PostgreSQL full-text search (`backend/src/services/search/fulltext_search_service.py`, `keyword_search_service.py`); pgvector semantic search is flag-gated and off by default; Qdrant is fully removed. Several items below marked outstanding may since have been resolved in code (for example `hybrid_search_service.py` now documents that its vector arm was retired); check the code and `docs/engineering/gotchas.md` before acting on any checkbox.

Qdrant was dropped; **DO KB (DigitalOcean Knowledge Base)** is the retrieval backend.
This tracks what's done vs. outstanding. Audit date: 2026-06-08.

## Done (code, verified by inspection)

- [x] **Agent retrieval query hardening** — `_nodes_rag.py`: `_build_search_query()` strips
      instruction clauses / prefers quoted entities; `_coerce_text()` flattens multimodal content.
      (Stops feeding raw multi-title+instruction strings to retrieval.)
- [x] **Dead Qdrant client removed from agent memory** — `services/agent/memory_store.py`:
      deleted orphaned `_get_qdrant_client` / `_QDRANT_CLIENT` / `QDRANT_COLLECTION` /
      `_get_embedding_service` + unused `get_settings` import. Semantic recall is served by
      LangGraph `AsyncPostgresStore` (pgvector) in `memory.py`; nothing was lost.
- [x] **Legacy ingest tasks now reach DO KB (HIGH).** There were two ingest paths: the
      multimodal upload path (`multimodal_processing_service.py:1089`) always synced to DO KB,
      but the legacy Celery `process_document_ingestion` + `generate_embeddings`
      (`tasks/processing_tasks.py`, used by `file_service.py` and the documents reprocess
      endpoint) wrote only to dead Qdrant → `is_embedded` never set → docs never retrievable.
      Added `_sync_document_to_kb_blocking` (sync→async `merge()` bridge, mirrors
      `api/agent/tools_impl.py`); both tasks now call `sync_document_to_kb` and drive
      `is_embedded` off the data-source uuid. Idempotent, gated by `DO_KB_ENABLED`, never raises.
      Tests: `tests/tasks/test_processing_tasks_do_kb.py`. (PR #656)
- [x] **Dead Qdrant write in embedding step neutralized.** `process_embedding_generation`
      (`processing_service.py`) no longer calls `vector_service.insert_vectors`; kept as a no-op
      so it can't fail against the absent backend. (PR #656)
- [x] **Orphaned dead-Qdrant code removed (workflow-mapped, grep + py_compile verified).**
      Deleted `services/search/hybrid_vector_search_service.py` (zero importers); removed
      `QdrantException`/`QdrantConnectionError`/`QdrantSearchError` from `exceptions/__init__.py`
      (never imported/raised). (PR #656)

## Outstanding — RUNTIME CODE (needs a working test env to change safely)

- [ ] **Search service dense leg silently empty.** `services/search/vector_service.py:261`
      (`search_vectors` → HTTP to `QDRANT_URL=None`) returns `[]`; `hybrid_search_service` then runs
      keyword-only (Postgres FTS still works). `/api/v1/search` health probe (`api/search/search.py:725`)
      labels this `qdrant` and reports **healthy**. **Action:** repoint dense leg at DO KB or remove
      it + fix the health probe so it doesn't false-green.
- [ ] **`/api/v1/vectors/*` router** (`api/search/vectors.py`, mounted `main.py:447`) calls
      `vector_service.client` (None) → 500s. **Decision needed:** rebuild on DO KB or remove router.
- [ ] **Capabilities with NO DO KB replacement:** entity vector search
      (`vector_search_service.search_entities`/`index_entity`), hybrid dense leg. Rebuild or formally drop.
- [ ] Dead-import cleanup remaining (workflow-mapped, NOT auto-removable):
      - `search_service.py` — standalone microservice (port 8002), but its `cache` object is
        live-imported by `documents.py:542` / `document_upload.py:354`. Can't delete the file;
        extract the Qdrant `qdrant_client` + `HybridSearchEngine` bits only, keep `cache`.
      - `core/database_optimizations/*` — orphaned, already-broken package (~70 Qdrant refs across
        4 files), zero importers. Separate teardown (or delete the whole dead package).
      - config `QDRANT_*` + `validate_qdrant_*` — still read by `vector_service.py` at startup;
        removable only after the dense-leg repoint/removal below.

## Outstanding — INFRA / SECRETS (outward-facing; owner action)

- [ ] **PROD CONFIG (HIGH):** `config/environments/.env.production:15,32` still *requires*
      `QDRANT_API_KEY` + `QDRANT_URL`, **zero `DO_KB_*` vars** → prod retrieval has no backend.
      `deployment/github-actions/workflows/deploy.yml:237` creates the qdrant secret, never creates
      `do-kb-credentials`. Add `DO_KB_*`, drop `QDRANT_*`.
- [ ] **Fix placeholder secret:** dev `DO_KB_EMBEDDING_MODEL_UUID = <gte-large-uuid-from-step-1>`
      (literal placeholder) — KB may not be functional. Confirm dev org has `do_kb_uuid` set.
- [ ] **Stop double-spend (Qdrant still provisioned):** `deployment/helm/rag-system` ×2
      (`qdrant.enabled:true`, 3 replicas, ~300Gi PVC), `infrastructure/kubernetes/manifests/databases.yaml`
      StatefulSet, `knowledge-graph-analytics/values-dev.yaml:228`, prod/staging/security compose.
      (prod/staging `knowledge-graph-analytics` already `qdrant.enabled:false` + DO KB secret — correct.)
- [ ] **Monitoring/backups still target Qdrant:** Prometheus scrapes `qdrant:6333`, `QdrantDown`
      alerts, `backup-and-dr.yml` nightly `qdrant backup` → S3, `init-qdrant-collections.sh`,
      startup scripts. Remove.

## Why runtime ingest/search not auto-fixed here
No backend deps (`langchain_core`), no DB, no running app in this sandbox → can't test. Editing a
live ingest pipeline or shared search service blind is unsafe. Do those in an env where
`pytest tests/unit/services/` + an ingest smoke test can confirm.

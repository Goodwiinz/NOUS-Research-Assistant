# DigitalOcean Knowledge Base integration

Optional retrieval backend: one DigitalOcean Knowledge Base per organization, provisioned lazily on first ingest. It is off by default. `DO_KB_ENABLED` defaults to `False` in `backend/src/core/config.py`, and `DO_KB_PRIMARY_READ` (DO KB serves agent reads) also defaults to `False` and is rejected unless `DO_KB_ENABLED` is on. Live retrieval is PostgreSQL full-text search (`backend/src/services/search/`). Qdrant is fully removed; the package docstring in `__init__.py` still says the integration "replaces Qdrant", which is historical wording.

## Files

| File | Purpose |
| --- | --- |
| `client.py` | Async HTTP client for the DigitalOcean Knowledge Base / GradientAI API (`DOKnowledgeBaseError` on failure). |
| `models.py` | Pydantic models for API responses. |
| `provisioner.py` | Lazy, idempotent KB provisioning per organization. |
| `ingest.py` | Pushes existing `Document` rows into the organization's KB. |
| `pre_flight.py` | Forces text extraction for large or complex PDFs before sync (`DO_KB_FORCE_TEXT_PDF_PAGES`, `DO_KB_FORCE_TEXT_PDF_SIZE_MB`). |
| `backfill.py`, `backfill_model.py` | Backfill every existing document into its organization's KB; progress in `do_kb_backfill_progress`. |
| `retrieval.py` | Shared retrieve orchestration used by the agent RAG node and the `do_kb_retrieve` tool; maps outcomes (including 404 versus transient errors) onto each caller's contract. |
| `resolve.py` | Maps returned chunks back to documents. |
| `postprocess.py` | Pure sanitation of retrieved chunks at the model boundary. |
| `rerank.py` | Cohere re-scoring of retrieved chunks. |

## Configuration

All settings are `DO_KB_*` fields in `Settings`. When `DO_KB_ENABLED` is true, the API token, `DO_KB_REGION`, `DO_KB_PROJECT_ID` and `DO_KB_EMBEDDING_MODEL_UUID` are required and startup validation fails without them. Retrieval has its own short timeout (`DO_KB_RETRIEVE_TIMEOUT_SECONDS`, 3 s) separate from the general request timeout.

## Rules

- Every path stays organization scoped: the KB is resolved from the caller's organization, never from a client-supplied id.
- Callers treat any non-success as a miss and fall back to full-text search; only `asyncio.TimeoutError` and `DOKnowledgeBaseError` are handled inside `retrieval.py`.
- Sync helpers are idempotent, gated by the flag, and must not raise into the ingest pipeline.
- Historical status of the Qdrant to DO KB cutover is recorded in `docs/database/QDRANT_REMOVAL_STATUS.md` (a dated record, not current state).

# Bulk ingestion services

Offline bulk loaders for large paper datasets. This package is separate from per-document upload processing, which lives in `backend/src/services/processing/` and `backend/src/services/documents/`.

## Files

| File | Purpose |
| --- | --- |
| `kaggle_bulk_ingestion.py` | Kaggle arXiv bulk ingestion service. |
| `kaggle_llm_bulk_ingestion.py` | The same bulk ingestion with LLM extraction added. |

They are driven by the arXiv bulk routes in `backend/src/api/arxiv/arxiv_bulk.py` and `arxiv_llm_bulk.py`. Neither loader module references `organization_id` directly, so confirm how a bulk run attributes ownership before changing it; documents elsewhere are organization scoped and every such query must filter `organization_id`.

## Rules

- Any document row a bulk run creates must be attributable to an organization; do not add a code path that writes unscoped documents.
- Bulk work is slow and can call external services. Run it from a task or an explicit operator action, never inside a request handler's transaction.
- After ingest, use the returned document UUIDs; arXiv ids are metadata only.

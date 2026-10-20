# Document services

File handling, storage and quality services behind the document API (`backend/src/api/documents/`, mounted at `/api/v1/documents`, `/files` and `/processing`). Documents are organization scoped: every query must filter `organization_id`, and content-hash deduplication is per organization.

## Files

| File | Purpose |
| --- | --- |
| `file_service.py` | Upload validation, storage and the document create path; raises `FileValidationError` / `FileStorageError`, handles duplicate conflicts and upload cancellation. |
| `file_metadata_service.py` | Owner-or-admin edits of title, tags and visibility. |
| `object_keys.py` | Deterministic object-key derivations for a document's stored objects. |
| `storage_utils.py` | Shared storage helpers for the processing services. |
| `storage_reconcile.py` | Report-only reconciler between object storage and PostgreSQL (audit finding D6). |
| `satellite_state.py` | Durable evidence of remote writes (Neo4j, object storage, KB) whose outcome is not yet recorded, used by the satellite-index reconciler task. |
| `document_quality_service.py` | Real-time quality assessment and metrics. |
| `document_realtime_service.py` | Bridges processing events to WebSocket broadcasting. |
| `integrity_detection_service.py` | AI integrity detection (RoBERTa classifier) behind `/documents/{id}/integrity-check`. |

## Storage conventions

`Document.storage_path` is the bare object key and `file_path` is `s3://bucket/key` (or a legacy `supabase://` or local path). Serving and deleting branch on `storage_backend`; AWS S3 is the deployed default. The compensating-delete and orphan-row behaviors are covered by the root-level regression tests in `backend/tests/` (for example `test_upload_compensating_delete.py`).

## Rules

- Keep the organization predicate on every document and status query, and add a negative authorization test for foreign or deleted records when behavior changes.
- Public errors use stable safe messages; log internals instead (`FileValidationError` carries a separate `public_detail`).
- A document create must not enqueue processing before its row commits; use `backend/src/tasks/enqueue.py`.

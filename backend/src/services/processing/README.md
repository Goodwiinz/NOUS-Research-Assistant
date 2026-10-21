# Document processing services

The multimodal processing pipeline that turns an uploaded or ingested file into extracted text, entities, tables, figures and graph data. The Celery tasks that drive it are in `backend/src/tasks/processing_tasks.py` and `document_processing_tasks.py`; status updates reach clients through WebSocket and SSE (`backend/src/api/realtime/`).

## Files

| File | Purpose |
| --- | --- |
| `multimodal_processing_service.py` | Async pipeline for OCR, transcription and AI analysis; the main upload path. When `DO_KB_ENABLED` is on it also syncs the document to the organization's KB. |
| `processing_service.py` | Earlier multimodal pipeline service; its embedding step no longer writes to a vector store. |
| `processing_integration.py` | Glue between the pipeline and WebSocket status updates. |
| `audio_processing_service.py`, `image_processing_service.py`, `video_processing_service.py` | Per-modality processing. |
| `table_extraction_service.py`, `figure_extraction_service.py` | PDF table and math extraction; PyMuPDF figure extraction (embedded raster images and caption heuristics, bounded per-page work). |
| `llm_entity_extraction.py` | LLM-based entity extraction (current). |
| `entity_extraction_service.py` | Deprecated, replaced by `llm_entity_extraction.py`; kept until callers are confirmed gone. |
| `data_preprocessor.py` | Data preprocessing for graph visualization. |

## Rules

- Processing status uses the `ProcessingStatus` enum; clients see mapped strings (`pending` becomes `queued`, `completed` becomes `indexed`). See `backend/src/models/README.md`.
- Tasks run with `acks_late`, so a stage can be redelivered. Use the claim in `backend/src/tasks/replay_guard.py` and the stage-write guard in `processing_lifecycle.py` rather than assuming one delivery.
- Every document query filters `organization_id`.
- Extraction results that feed Neo4j go through `services/knowledge_graph/`; a failed graph write must not leave an orphan job (see the root-level `test_kg_job_no_orphan_on_queue_fail.py` regression tests).

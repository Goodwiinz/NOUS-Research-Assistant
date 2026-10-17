# Models

SQLAlchemy ORM models for the `multimodal_rag_dev` PostgreSQL database. There are no standalone Pydantic schema files in this directory — request/response schemas live in the relevant API routers under `src/api/`. Every file here defines mapped ORM classes (and, in a few cases, supporting TypeDecorators or association tables).

## Base class and shared infrastructure

`base.py` defines two things everything else depends on:

- **`Base`** — `declarative_base()` instance; all ORM models inherit from it indirectly.
- **`BaseModel(Base)`** — abstract mixin that adds `id` (UUID primary key), `created_at`, `updated_at`, and soft-delete fields (`is_deleted`, `deleted_at`) with `soft_delete()` / `restore()` helpers. Also provides `to_dict()`.
- **`GUID`** — `TypeDecorator` that maps to PostgreSQL's native `UUID` type and `CHAR(36)` on SQLite (used in tests).

`encrypted_fields.py` provides `EncryptedType` and `encrypted_string()` — a `TypeDecorator` that encrypts/decrypts at bind/result time via `src.core.encryption`. Used on `User.first_name` and `User.last_name`.

`utils.py` contains `StringArray`, a cross-dialect type that becomes `ARRAY(TEXT)` on PostgreSQL and `JSON` on SQLite.

## Key files and entities

| File                                       | ORM class(es)                                                 | Table(s)                           | Purpose                                                                                                                                                                                                           |
| ------------------------------------------ | ------------------------------------------------------------- | ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `organization.py`                          | `Organization`                                                | `organizations`                    | Multi-tenant root; owns users and documents; tracks storage quota by tier (free / professional / enterprise)                                                                                                      |
| `user.py`                                  | `User`                                                        | `users`                            | User accounts; role hierarchy (user → analyst → content_manager → admin); bcrypt-hashed password; encrypted first/last name                                                                                       |
| `workspace.py`                             | `Workspace`                                                   | `workspaces`                       | Top-level container owned by an organization; holds conversations and collections                                                                                                                                 |
| `conversation.py`                          | `Conversation`                                                | `conversations`                    | Named project area inside a workspace; groups related threads                                                                                                                                                     |
| `thread.py`                                | `Thread`                                                      | `threads`                          | Granular line of inquiry inside a conversation; owns ordered `ChatMessage` records; tracks `message_count` and `token_count`                                                                                      |
| `chat_message.py`                          | `ChatMessage`                                                 | `chat_messages`                    | Individual messages; roles: `user`, `assistant`, `system`, `tool`                                                                                                                                                 |
| `collection.py`                            | `Collection`                                                  | `collections`                      | Organized document groups inside a workspace; doubles as a research project container                                                                                                                             |
| `document.py`                              | `Document`                                                    | `documents`                        | Core entity: uploaded files of any modality (text, PDF, image, audio, video, spreadsheet, presentation). Tracks processing pipeline state, TSVECTOR for full-text search, and SHA-256 checksum |
| `entity.py`                                | `Entity`                                                      | `entities`, `entity_relationships` | NLP-extracted entities with type, confidence, and optional Neo4j graph node ID; self-referential many-to-many through `entity_relationships` association table                                                    |
| `citation.py`                              | `Citation`                                                    | `citations`                        | Provenance records linking RAG-retrieved chunks to `ChatMessage` responses; supports both internal `document_id` and external references (e.g., arXiv IDs)                                                        |
| `generated_draft.py`                       | `GeneratedDraft`                                              | `generated_drafts`                 | Versioned AI-generated literature review drafts attached to a `Collection` (research project); max 10 versions per project                                                                                        |
| `project_note.py`                          | `ProjectNote`                                                 | `project_notes`                    | User-authored markdown notes inside a research project; linkable to specific document UUIDs                                                                                                                       |
| `research_project.py`                      | `ResearchProject`                                             | `research_projects`                | Lightweight research project owned by a user; used by the agent pipeline alongside `Collection`-based projects                                                                                                    |
| `permission.py`                            | `Permission`, `Role`                                          | `permissions`, `roles`             | Fine-grained RBAC; permissions are scoped (read/write/delete/manage/admin) per category (documents, analytics, audit, etc.)                                                                                       |
| `audit.py`                                 | `AuditEvent`                                                  | `audit_events`                     | Immutable compliance log for auth, authorization, and data change events                                                                                                                                          |
| `encrypted_user.py`                        | `EncryptedUserProfile`, `EncryptedOrganizationProfile`        | —                                  | Extended encrypted PII attached 1-to-1 to `User` and `Organization`                                                                                                                                               |
| `user_session.py`                          | `UserSession`                                                 | `user_sessions`                    | Active session tracking (JWT / cookie lifetime)                                                                                                                                                                   |
| `analytics_event.py`                       | `AnalyticsEvent`                                              | `analytics_events`                 | Product analytics events (page views, feature usage)                                                                                                                                                              |
| `ab_testing.py` / `ab_testing_models.py`   | `Experiment`, `Variant`, `Assignment`                         | `experiments`, …                   | A/B experiment definitions and user assignments                                                                                                                                                                   |
| `search.py` / `search_analytics.py`        | `SearchQuery`, `SearchResult`, `SearchSession`, `SearchEvent` | `search_*`                         | Search activity logging and analytics                                                                                                                                                                             |
| `knowledge_graph.py` / `graph.py`          | KG query/result helpers                                       | —                                  | Utility models for Neo4j query results and graph API responses                                                                                                                                                    |
| `processing.py` / `document_processing.py` | `ProcessingJob`, `ProcessingHistory`, `DocumentVersion`       | `processing_jobs`, …               | Document ingestion pipeline state; supports retry tracking                                                                                                                                                        |

## Models by domain (added after the table above)

Run `ls backend/src/models` for the authoritative list; each file declares its own `__tablename__`.

| Domain | Files |
| --- | --- |
| Durable agent runs | `agent_run.py` (`agent_runs`), `agent_run_event.py` (`agent_run_events`, the append-only ledger), `agent_outbox.py`, `agent_runtime_snapshot.py`, `agent_tool_receipt.py`, `agent_hitl_audit.py`, `tool_action.py` (`integration_tool_actions`) |
| Integrations and harness | `integration_grant.py`, `integration_handoff.py`, `integration_context_selection.py`, `bridge_device.py`, `harness_session.py`, `api_key.py` |
| Artifacts and project content | `artifact.py`, `artifact_share.py`, `project_memory.py`, `project_skill.py`, `project_thread.py`, `message_attachment.py`, `native_artifact_intent.py` |
| Research engine | `research_blueprint.py`, `research_protocol.py`, `research_run.py`, `research_step.py`, `research_stage_review.py`, `research_review_version.py`, `research_rerun.py`, `research_source.py`, `research_fulltext.py`, `research_import.py`, `research_search_update.py`, `research_appraisal.py`, `research_evidence_table.py`, `research_synthesis.py`, `research_experiment.py`, `research_decision.py`, `research_report.py`, `research_pipeline.py`, `research_project_role.py` |
| Claims, review and release | `research_claim.py`, `peer_review.py`, `manuscript_release.py`, `manuscript_statements.py`, `research_deposit.py`, `draft_release.py`, `draft_review.py`, `draft_citation.py`, `draft_task_result.py`, `citation_relationship.py`, `extraction_matrix.py`, `screening.py`, `integrity_score.py`, `evidence.py` |
| Retrieval | `document_embedding.py` (pgvector `document_embeddings`, used only when the semantic-search flags are on), `search_feedback.py`, `ranking.py`, `rag_query.py`, `query.py` |
| Evaluation and quality | `evaluation.py`, `evaluation_metrics.py`, `quality.py`, `quality_metrics.py`, `performance_log.py`, `validation_rules.py` |

## Retrieval storage

There is no vector store model for Qdrant. `vector.py` holds generic vector schemas and is not an ORM table. `documents.embedding_id` is a legacy nullable column from the Qdrant era and nothing writes real Qdrant IDs to it. Live retrieval uses the `documents.search_vector` TSVECTOR column; pgvector embeddings live in `document_embeddings`.

## Conventions

**Status mapping.** `Document.processing_status` uses `ProcessingStatus` enum values (`pending`, `processing`, `completed`, `failed`, `retrying`). The `get_mapped_status()` method and `to_dict()` translate these to frontend-visible strings: `pending → queued`, `completed → indexed`, `retrying → processing`. Always use the mapped form in API responses, never the raw enum value for `pending` or `completed`.

**Enums.** Local Python enums (`PyEnum`) are used for type safety. When bound to SQLAlchemy `Enum(...)` columns, the `values_callable=lambda x: [e.value for e in x]` pattern is used on columns that need lowercase string values stored in the database (e.g., `Thread.status`, `ChatMessage.role`). Shared application enums live in `src/shared/enums.py`.

**Soft delete.** `BaseModel.soft_delete()` sets `is_deleted = True` and records `deleted_at`; hard deletes are never performed on core entities. Queries should always filter `is_deleted == False` explicitly — there is no global query filter.

**Indexes.** Multi-column composite indexes are declared in `__table_args__` with the naming convention `idx_<table>_<columns>` (e.g., `idx_document_org_status`). Single-column indexes use the `index=True` column argument.

**Encrypted fields.** Sensitive PII (names, extended profiles) uses `encrypted_string()` from `encrypted_fields.py`, which wraps `EncryptedType` backed by `src.core.encryption`. The column stores ciphertext as `TEXT`; decryption is transparent at ORM level.

## Migrations

Schema changes are managed with Alembic. Migration files live in `backend/alembic/versions/`. To generate a new revision after changing a model:

```bash
cd backend
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

The `alembic.ini` and `env.py` are configured to import all models via `src.models` before autogenerate inspection. Adding a new model file requires adding its import to `src/models/__init__.py` to ensure Alembic detects it.

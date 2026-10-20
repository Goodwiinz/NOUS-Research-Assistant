# Thread and workspace services

Persistence and access control for the workspace hierarchy: workspace, conversation, thread, message, plus workspace collections. The routers under `backend/src/api/threads/workspace_routes/` are transport only and call into these services; the services own persistence and exactly one transaction boundary per method.

## Files

| File | Purpose |
| --- | --- |
| `workspace_access.py` | The one access funnel. Every scope and access check for the hierarchy goes through it. Routers wrap it in `workspace_routes/dependencies.py` to turn a miss into a 404; `ChatService` calls it directly. |
| `workspace_service.py` | Workspace and workspace-member persistence. |
| `conversation_service.py`, `thread_service.py`, `message_service.py`, `collection_service.py` | Persistence for each level of the hierarchy. |
| `chat_service.py`, `stream_service.py` | Thread-centric chat persistence and the SSE stream service. |
| `thread_event_service.py` | Real-time WebSocket notifications for thread changes. |
| `thread_message_search_service.py` | Thread and message full-text search (`/api/v2/search`). |
| `thread_summarization_service.py`, `thread_title_generator.py` | AI summaries and generated titles. |

## Access model

- Workspace access is membership based: public, or a `WorkspaceMember` row, or owner. It is not organization based. A workspace's `organization_id` is metadata, and a public workspace is visible across organizations by design.
- Documents attached under a workspace (collection contents, message attachments) are organization scoped. That predicate is enforced in `workspace_access.py` by `get_accessible_document_or_none` and `filter_owned_document_ids`.
- Soft delete does not cascade. Every child getter re-checks the `is_deleted` flag of all its ancestors on every fetch, so deleting a parent revokes access to its children.
- Access helpers fail closed when the caller identity is absent.

## Enforcement

`backend/tests/unit/architecture/test_workspace_boundaries.py` fails if a router commits, imports a sibling resource module, or an access getter stops requiring the caller identity. `test_thread_purge_fk_contract.py` pins the foreign-key delete rules for tables that the 30-day thread purge touches. Run `pytest -q backend/tests/unit/architecture backend/tests/unit/services/threads backend/tests/api/threads`. See `backend/AGENTS.md` and `docs/engineering/backend.md` for the rules.

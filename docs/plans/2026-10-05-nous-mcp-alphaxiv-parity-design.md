# NOUS MCP server: alphaXiv feature parity — design (Plan 07)

**Status:** approved design, 2026-10-05. Checked against `origin/develop` `2ba9b0e97`. Implementation plan: `2026-10-05-nous-mcp-alphaxiv-parity.md`.

## Problem

A harness (Claude Code, Codex) connected through `packages/harness-bridge` sees four read tools and one write action. alphaXiv's hosted MCP server (`https://api.alphaxiv.org/mcp/v1`) offers nineteen tools in three groups: find and read papers, look up researchers, curate a library. The owner wants the same job coverage from NOUS, backed by the NOUS corpus, arXiv, the external-connector registry and the knowledge graph. Primary user is the owner in Claude Code; the design must stay safe for other tenants.

Papers are arXiv-heavy and mostly not yet ingested, so reading a paper must not require a human-approved ingest first.

## Decisions

| Question | Decision |
| --- | --- |
| Consumer | Both; owner first. Multi-tenant safety, owner-friendly defaults. |
| Corpus | Mixed, arXiv-heavy. Add a transient arXiv full-text read path. |
| Approval for library writes | Consent-time blanket for reversible ops (`library:write`). Deletes and ingest keep per-action approval. |
| Grant binding | Workspace-scoped grant from the start. Folders = Collections in the workspace. |
| Transport | Keep stdio bridge + grant model. No remote OAuth endpoint. |
| Metering | None. Existing arXiv rate gate suffices. |

## Approaches considered

- **A. Extend allowlists + workspace grant (chosen).** Grow `READ_TOOL_NAMES` and `ALLOWED_ACTIONS`; one migration adds `workspace_id` and two scopes to the grant; reversible actions auto-run under `library:write`. No new HTTP routes.
- **B. New `/api/v1/library/*` router behind grant auth.** 1:1 REST mirror of alphaXiv. Rejected: duplicates Collection service logic, OpenAPI churn, most work for the same outcome.
- **C. Bridge calls user-JWT routes directly.** Rejected: bypasses project binding and consent; tenancy regression.

## Architecture

Two allowlists remain the only entry points for harness tools:

- Reads: `backend/src/services/integrations/read_tools.py` (`READ_TOOL_NAMES`, `invoke_read`), scope `tools:read`.
- Writes: `backend/src/services/agent/tool_actions.py` (`ALLOWED_ACTIONS`, validators, `_run_effect`), scope `tools:write` or `library:write`.

**Grant scope.** `IntegrationGrant` and `IntegrationGrantRequest` gain nullable `workspace_id`. Exactly one of `project_id` / `workspace_id` is set (DB check constraint). `authorized_project` in `services/integrations/context.py` is generalised to `authorized_scope`, returning either one Collection or the set of Collections in the workspace, resolved through `workspace_access.py` with the caller's identity. Every tool resolves its target from the grant, never from client arguments.

**Scopes.** `STANDARD_SCOPES` gains `library:read` and `library:write`. `library:write` implies the reversible library actions run inline; `tools:write` alone keeps today's approve flow for everything.

**Action execution.** `tool_actions.py` gains `AUTO_RUN_ACTIONS ⊂ ALLOWED_ACTIONS`. On `POST /integrations/actions`: validate args, resolve scope, then

- action ∈ `AUTO_RUN_ACTIONS` and grant has `library:write` → run effect in the request, return receipt with `status=completed`;
- otherwise → existing pending/approve path.

Reversible (auto-run): `save_papers_to_folder` (link), `remove_papers_from_folder` (unlink), `move_papers_between_folders` (unlink + link in one service transaction), `create_folder`, `rename_folder`, `update_document_metadata` (title, tags).
Approval-required: `delete_folder`, `ingest_arxiv_papers`.

**Transient arXiv read.** `get_arxiv_paper_content {arxiv_id, offset, limit}` downloads through the existing `ArXivIngestionService` (rate gate, 429 handling), extracts text with the existing extractor, caches the text in Redis for 24 h under `arxiv:fulltext:<id>`, and returns a page of characters. Nothing is written to Postgres or object storage. Ingest stays the only way to make a paper a NOUS document.

## Tool catalogue

| alphaXiv | NOUS tool | Scope | Backing |
| --- | --- | --- | --- |
| `discover_papers` | `search_arxiv`, `search_external_database`, `list_external_databases`, `search_documents` (exists) | `tools:read` | agent registry impls (args-only) |
| `get_paper_content` | `get_arxiv_paper_content` (not ingested), `get_document_content` (ingested; `mode` summary/full, char pagination) | `tools:read` | arXiv service + Redis; `Document.content_text` |
| `answer_pdf_queries` | `retrieve_passages {query, document_ids?, top_k}` | `tools:read` | PostgreSQL full-text chunk search scoped to grant Collections |
| `find_researchers`, `resolve_researchers` | `find_researchers {query, limit}` | `tools:read` | KG `PERSON` entities in scope |
| `get_researcher`, `get_researcher_papers` | `get_researcher {entity_id}` → papers + coauthors | `tools:read` | `explore_entity_neighborhood` |
| `list_library` | `list_library` | `library:read` | Collections in scope + doc counts |
| `save_papers_to_folder` | `save_papers_to_folder {document_ids, project_id}` | `library:write` | `CollectionDocument` link |
| `remove_papers_from_folder` | `remove_papers_from_folder {document_ids, project_id}` | `library:write` | unlink only |
| `move_papers_between_folders` | `move_papers_between_folders {document_ids, from, to}` | `library:write` | one transaction |
| `create_folder` / `rename_folder` | same names | `library:write` | Collection create/patch |
| `delete_folder` | `delete_folder {project_id}` | `tools:write` + approval | Collection soft-delete; documents untouched |
| `edit_private_paper_metadata` | `update_document_metadata {document_id, title?, tags?}` | `library:write` | same service as `PUT /files/{id}` |
| — | `ingest_arxiv_papers {paper_ids}` | `tools:write` + approval | existing DESTRUCTIVE tool |
| `read_files_from_github_repository`, follow feeds | not mirrored | — | harness has git; no feed model |

## Data flow

Harness → bridge local tool or catalog passthrough → `POST /integrations/tools/read` or `POST /integrations/actions` with CLI JWT + `X-NOUS-Integration-Grant` → `require_integration_context(scope)` → `authorized_scope` → read impl or action effect → `ToolResult{content, is_error, source_refs}` / action receipt. The bridge never calls user-JWT routes.

## Error handling

- Public errors use stable safe messages; raw exception text never leaves the service layer.
- Results stay under the 64 KiB cap by pagination (`offset`/`next_offset`), never silent truncation.
- Neo4j unavailable → `is_error=true`, "knowledge graph unavailable". arXiv 429 → stale cache if present, else safe error.
- `move_papers_between_folders`: second half fails → transaction rolls back; no partial move.
- Any `project_id` outside the grant's scope → `IntegrationAccessDenied` (403), same as today's membership check.

## Consent

The approve page shows workspace name (or Collection name), a scope → label map replacing raw scope strings, and one explicit sentence: "library:write lets the harness add, remove, move and rename items in this workspace without asking each time. Deleting folders and ingesting papers still require your approval." `require_interactive_user` keeps rejecting CLI tokens.

## Testing

- Backend unit: scope resolution (project vs workspace); cross-workspace `project_id` refused, with mutation check (remove filter → test fails); auto-run vs approve routing by scope; move atomicity; arXiv content pagination, cache hit, 429 → stale; `retrieve_passages` scoped to Collections; results ≤ 64 KiB.
- Architecture tests (`backend/tests/unit/architecture`) stay green: routers don't commit, access getters require identity.
- Bridge `node:test`: action enum round-trip, `--library` flag persists scopes, 422/403 wording preserved.
- Alembic single head, revision-id length. OpenAPI snapshot and `frontend/src/types/generated/api.d.ts` regenerated in the same PR as any schema change (bridge imports `api.d.ts`).
- Live proof on `rag-dev` is NOT RUN until the owner flips `NOUS_MCP_ENABLED`.

## Slices

1. Migration + scopes + `authorized_scope` + `list_library` + consent labels. Waits on #1784 (serial-migration rule) or owner waiver.
2. Read tools: arXiv/connector primitives, `get_arxiv_paper_content`, `get_document_content`, `retrieve_passages`.
3. Library writes with auto-run; `delete_folder`, `ingest_arxiv_papers` via approval.
4. Researcher tools over KG.
5. Composite `discover_papers`, only if a Harbor eval shows harnesses fumble the primitives.

## Out of scope

GitHub repository reading, follow feeds, nested folders, abstract/author/BibTeX editing (no columns), remote OAuth MCP endpoint, per-grant metering, NOUS consuming alphaXiv's MCP.

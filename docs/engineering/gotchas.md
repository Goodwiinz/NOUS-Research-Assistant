# Gotchas

Operational knowledge preserved from the retired root `CLAUDE.md` (removed in #1491).
These are hard-won invariants — verify against code before assuming one has changed.

## Database & environment

- Local compose DB = container `rag-postgres-1` / database `multimodal_rag_dev` (not `rag-db-dev`). AWS application SQL uses **RDS PostgreSQL** via `aws-database-credentials`; `SUPABASE_DB_URL` is deliberately blank in the AWS overlay so it cannot override `DATABASE_URL`. Supabase remains the auth/auxiliary service, with its own migrations.
- Current deployment source (checked 2026-10-01 against `057681871797ac4c6c2f1804c2ab47f2b5fddba2`): [AWS Argo application](../../infrastructure/argocd/applications/aws-dev.yaml), `nous-dev-aws`, EKS `nous-dev-cluster` in `us-east-1`, namespace `multimodal-rag-system`, base values plus `values-aws.yaml`, tracking `develop`. API: `dev-api.goodwiinz.tech`; frontend: Vercel `goodwiinz.tech`. The old DOKS `rag-dev` context is retired rollback material. Staging/production Argo apps were retired 2026-04-29 (PR #442); their values files remain render fixtures.
- The AWS migration rollout contract uses one digest-pinned Job in Argo wave 1; API, worker, beat and synthetic consumers advance in wave 2 only after success. Shared secrets/service accounts remain wave 0. A failed Job blocks rollout and is retained for diagnosis; deleting/retrying it requires explicit operational authorization. See the [chart migration contract](../../infrastructure/helm/knowledge-graph-analytics/README.md#database-migrations).
- Qdrant is removed — retrieval migrated to DO KB (`backend/src/services/do_kb/`, behind `DO_KB_ENABLED`, currently off; live retrieval is PostgreSQL fulltext). Legacy non-ArgoCD charts/manifests still carry dead Qdrant refs; not deployed.
- Migration-bearing slices land serially. Before the next Alembic-bearing branch is cut, re-parent the open one onto the current head (`down_revision` = the head `check_alembic.py` prints on fresh `origin/develop`), retarget it to `develop` if it is stacked, and merge it. A PR's check sees only the heads in its own base, so two open migration branches can each pass and still fork the chain once both land. The release preflight permits non-strict status checks, so do not rely on branch protection to force the second PR to update and re-run `migration-check`. Never merge a migration-bearing PR without a fresh `check_alembic.py` on the current `develop` base. Enforced by `scripts/ci/check_alembic.py` (single head, revision ids ≤ 32 chars) in the blocking `migration-check` job of `.github/workflows/test-pipeline.yml`, which the required `Release Gate` needs, on PRs and pushes to `develop`. Run it locally with `(cd backend && python ../scripts/ci/check_alembic.py)`. Background: [2026-10-04 amendment](../plans/2026-10-04-harness-plan-amendment.md).

## API

- Documents API is `/api/v1/documents/` (not `/documents` or `/api/documents`).
- WebSocket auth uses the `Sec-WebSocket-Protocol` header, NOT URL query params.
- CORS uses explicit allowlists, no wildcards.
- SQL injection prevention via validated enums (`src/shared/enums.py`) — never raw strings in sort/filter.
- Document status mapping uses lowercase: `'pending'→'queued'`, `'completed'→'indexed'`.
- Don't name query params the same as imported modules (e.g. `status` shadows `fastapi.status`).
- ArXiv API endpoints need `postWithLongTimeout` (5 min), not the standard timeout.
- IconButton requires `aria-label`.

## Tenant scope (mandatory)

- Every document / content-hash dedup / search-suggestion query MUST filter `organization_id`.
- Project (Collection) ownership = `Workspace.owner_id` — Collection has **no** `owner_id` (join Workspace).
- Agent tools + the RAG node must verify project ownership (`_verify_project_ownership` / `_user_owns_project`) before using a client-supplied `project_id`; an unscoped 409/suggestion/filter leaks other tenants' titles/ids.
- `require_admin` is a per-user role, not a tenant boundary.
- A NULL `workspaces.organization_id` (legacy rows) means the workspace belongs to its owner's organization, as [`project_access.py`](../../backend/src/services/research_engine/project_access.py) coalesces it. Integration code must compare a workspace's organization only through `workspace_in_org` / `workspace_organization_id` in [`services/integrations/context.py`](../../backend/src/services/integrations/context.py), never `Workspace.organization_id ==` directly (WG-2). Rule (c) in [`test_integration_boundaries.py`](../../backend/tests/unit/architecture/test_integration_boundaries.py) enforces this for the integration layer only (`services/integrations/`, `api/integrations/` and `api/threads/workspace_routes/threads.py`); other modules are not swept.

## Search

- Count queries must apply the _same_ filters as the result query (shared `_apply_*_filters` helpers) — a drifted count over-reports `total` and yields phantom `has_more` pages.

## Storage

- `Document.storage_path` = bare object key; `file_path` = `s3://bucket/key` (or `supabase://...`, or a local path). Serving + deleting branch on `storage_backend` — AWS S3 is the deployed default; DO Spaces belongs to the retired overlay.
- Upload commits the object to storage _before_ the DB rows, so any post-upload failure must compensating-delete the object (`_best_effort_delete_object`) and revert quota, else it orphans the object and a PENDING row that content-hash dedup then blocks from re-upload.

## Agent (LangGraph)

- After arXiv ingest, use `document_ids` (UUIDs) from the response, NOT arXiv paper IDs.
- Checkpoint URL must be `postgresql://` (psycopg v3), not `postgresql+asyncpg://`.
- Every AIMessage with tool_calls must have matching ToolMessages (sanitizer adds placeholders).
- DraftGenerationService uses a fresh `AsyncSessionLocal()` to avoid rollback conflicts.
- Destructive tools (ingest, create_note, create_draft) trigger `interrupt()` for human confirmation.
- `ThreadSummarizationService` uses the **sync** SQLAlchemy API (`db.query`/`db.commit`) and is shared with the Celery task — drive it from a worker thread (`SessionLocal()` + `asyncio.run`), never hand it the request's `AsyncSession` (`'AsyncSession' has no attribute 'query'` → 500).

## Observability

- Synthetic-traffic LangSmith runs are filtered by `metadata.synthetic` + `run_name` (`synthetic:<scenario>`), NOT a post-hoc tag — LangSmith rejects `update_run` after a run's final payload ("Duplicate run update… not supported").

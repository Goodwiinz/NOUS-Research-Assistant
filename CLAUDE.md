# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Where the rules live

The old root `CLAUDE.md` was retired in #1491. Rules now live in `AGENTS.md` files plus `docs/engineering/`, and this file is only a map. Read the closest contract before editing; don't restate it here.

Claude Code loads `CLAUDE.md` but not `AGENTS.md`, so the root contract is imported here:

@AGENTS.md

- Root `AGENTS.md` → `docs/engineering/README.md`, then `{backend,frontend,testing,api-contracts,gotchas}.md`. Each rule there names the test or CI gate that enforces it.
- Nearly every top-level directory has its own `AGENTS.md` (`backend/`, `frontend/`, `tests/`, `docs/`, `src/`, ...). None of them load automatically: read the nearest one before editing in that directory. It adds to the root file and never overrides it.
- "NOUS loop" / "self-improvement tick" requests: read `docs/engineering/nous-loop.md` completely first. It is the canonical workflow, with its own evidence, review and terminal-outcome (`merged` / `ready-for-human` / `dry`) gates.
- `docs/plans/`, `docs/audits/`, `docs/archive/` and `evals/baselines/` are dated records, not live contracts. Don't edit history to match new code; add a dated amendment.
- Some READMEs are stale. `backend/src/services/agent/README.md` still says Qdrant, which is removed. Check the code before trusting one.

## Repo shape

pnpm monorepo (Node 24, `pnpm@10.18.2`, one root `pnpm-lock.yaml`; no `npm` fallbacks) plus a Python backend.

- `backend/` — FastAPI + LangGraph. Entry `backend/src/main.py`, agent graph in `backend/src/services/agent/`, Alembic in `backend/alembic/`, committed OpenAPI snapshot `backend/openapi.json`.
- `frontend/` — Next.js app (App Router under `frontend/app` and `frontend/src`), Vitest unit tests, Playwright E2E.
- `terminal/` and `./nous` — terminal client. `./nous` defaults to the shared dev backend unless `NOUS_API_URL` is set.
- `packages/chat-runtime/` — shared chat runtime workspace package.
- `evals/` — agent eval harness and recorded baselines. `tests/` — root cross-system suites, Playwright, k6 load tests. `backend/tests/` — backend tests.
- `src/trigger/` — Trigger.dev jobs. `frontend/trigger.config.ts` resolves `./src/trigger` relative to `frontend/`, so prove which tree is actually loaded before editing or deploying a job.
- `deployment/`, `infrastructure/`, `.github/workflows/` — Helm/ArgoCD, Terraform, CI.

## Commands

Run from the repo root. Use the existing `backend/.venv`; local dev does not use Docker.

```sh
# Before pushing: every blocking CI gate against the base branch (add --frontend for the pnpm gates)
scripts/ci/run_local_ci.sh [--base origin/develop] [--skip-tests] [--frontend]

# Backend
ruff check backend/src                                  # F821/F823 are blocking repo-wide
pytest -q backend/tests/unit/architecture backend/tests/unit/api
pytest -q backend/tests/unit/services/threads backend/tests/api/threads
pytest -q backend/tests/unit/path/to/test_x.py -k name  # single test
python scripts/ci/generate_openapi.py --check           # OpenAPI drift (offline, in-memory SQLite)
(cd backend && python ../scripts/ci/check_alembic.py)   # single head + revision-id length
make docs-lint                                          # directory README/doc.md lint

# Frontend
pnpm install --frozen-lockfile
pnpm --dir frontend dev:offline                         # `dev` wraps `infisical run`; needs Infisical
pnpm --dir frontend lint | type-check | test
pnpm --dir frontend exec vitest run path/to/file.test.ts  # single test
pnpm --dir frontend lint:changed                        # blocking ratchet; needs an ESLint JSON report, so use run_local_ci.sh --frontend
pnpm --dir frontend quality:exclusions
pnpm --dir frontend generate:api-types                  # rebuild src/types/generated/api.d.ts
pnpm --dir tests/e2e exec playwright test --project=chromium   # browser + running app required
```

- `pytest -q backend/...` picks up `backend/pytest.ini`, which uses `--strict-markers`, so use the registered markers (`unit`, `integration`, `e2e`, `regression`, ...).
- Lint tool versions are pinned and must match CI: `ruff==0.15.15 black==26.5.1 isort==5.13.2 mypy==1.7.1`.
- CI's Lint Backend gate is ruff, black and isort on changed files, plus mypy on added files only. Full-tree black, isort, mypy and frontend lint are advisory.
- Don't trust the root `Makefile` for verification. Its `test`/`lint`/`shell` targets `exec` into docker-compose containers, and `validate` runs `validate_phase1.py` from a path where it no longer lives (it's now `backend/scripts/validation/`). `backend/Makefile` runs pytest directly.
- Report a check that needs unavailable services (DB, browser, credentials) as `NOT RUN`, never as passing.
- Don't run `tests/load/run-shared-dev-max.sh --full` without explicit authorization. It is disruptive.

## Architecture (big picture)

**Request path.** Web app and CLI call the same FastAPI API. Routers are transport-only. Services own persistence and exactly one transaction boundary per method. For the workspace/thread API this is `backend/src/api/threads/workspace_routes/*` → `backend/src/services/threads/*`, with all scope and access checks going through `workspace_access.py`. `backend/tests/unit/architecture/` fails if a router commits, imports a sibling resource module, or if an access getter stops requiring the caller's identity.

**Agent.** The LangGraph graph is built in `services/agent/_builders.py` (`graph.py` is the re-export facade; `langgraph.json` points at `create_graph`). Flow: `preprocessing_node` (RAG + intent + memory recall in parallel) → research/writing/data subgraphs (`subgraphs/`, each with its own planner, compactor and reflection gate) or the main `planner → llm ⇄ tool` loop (max 4 loops) → reflection gate → `memory_save_node`.
- Destructive tools (ingest, create_note, create_draft) hit `interrupt_node`, a human-in-the-loop pause persisted in the Postgres checkpoint. The client resumes it via `/api/v1/agent/confirm/{job_id}`.
- Runs are durable. `agent_submission_service` commits the message, `agent_runs` row and outbox record in one transaction. `run_event_store` is the append-only event ledger that SSE streams replay from. `agent_run_service` owns the run-status projection.
- Tools carry only scalar ids (`user_id`/`organization_id`/`thread_id`) in the graph config. `tool_session.py` opens a fresh session and re-loads the user org-scoped per call. Never put the request session or ORM user into the config.

**Tenancy.** Two different scopes; mixing them up is a security bug.
- Workspaces, conversations and threads are membership-based (public / member / owner). Soft-delete does not cascade, so every child getter re-checks all ancestors' `is_deleted`.
- Documents, content-hash dedup and search suggestions are organization-scoped. Every such query must filter `organization_id`.
- Project (Collection) ownership is `Workspace.owner_id`; Collection has no `owner_id`. Agent tools must verify project ownership before using a client-supplied `project_id`.

**API contract pipeline.** FastAPI/Pydantic is the source of truth. `generate_openapi.py` produces `backend/openapi.json`, then `generate:api-types` produces `frontend/src/types/generated/api.d.ts`. Never hand-edit either. A schema change and both regenerated files land in the same PR. `oasdiff` blocks ERR-level breaking changes unless the PR carries the `api-breaking-approved` label. In the frontend, alias generated shapes on touch (`types/api/workspace-contract.ts` is the example); don't re-type them or cast with `as unknown as`.

**Frontend state.** One cache owner per server entity. TanStack Query owns request-backed data. The chat Zustand store is the single sanctioned exception, for the chat transcript.
- Import only from `@/store/chat-store`, never from `store/chat/*`.
- The backend is the sole persisted-chat writer, so hooks must not add a second create-message call. Terminal reconciliation belongs to `chat-store`'s `refreshMessages`.
- `app/(dashboard)/chat/page.tsx` is a composition root: no service imports, no business logic.
- Ratchets (`frontend/quality-baseline.json`, tsconfig exclusions) only move stricter, in the PR that earns the move.

**Retrieval and data.** Live retrieval is PostgreSQL full-text. DigitalOcean KB (`services/do_kb/`, behind `DO_KB_ENABLED`) is off in dev, and Qdrant is fully removed. Neo4j holds the knowledge graph, Redis is cache and Celery broker (DO Valkey in the cluster), and documents live in S3-compatible object storage. The agent checkpointer URL must be `postgresql://` (psycopg v3), not `postgresql+asyncpg://`.

**Deploy.** Only `dev` is live (ArgoCD auto-sync, namespace `rag-dev`). After a green Test Pipeline on `develop`, `release-dev.yml` builds the SHA and opens a digest-pinned GitOps PR to `values-dev.yaml`; merging it deploys. The frontend deploys separately via Vercel. `staging`/`production` values files are dead.

## Non-obvious rules worth repeating

- Public HTTP errors use stable safe messages, never raw exception text. Sort/filter identifiers come from validated enums (`src/shared/enums.py`), never interpolated into SQL.
- Count queries must apply the same filters as the result query (shared `_apply_*_filters` helpers), or `total` and `has_more` drift.
- Every `AIMessage` with `tool_calls` needs matching `ToolMessage`s. `_sanitize_messages` inserts `{"status": "skipped"}` placeholders after cancellations.
- Keep compatibility exports (e.g. `backend/src/api/threads/workspaces.py`) until every caller has migrated. Removing one is its own deliberate change.

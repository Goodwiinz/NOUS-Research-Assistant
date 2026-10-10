# High Security Findings Remediation Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Close the five validated High Codex Security findings GOO-285 through GOO-289 without weakening tenant isolation or breaking ordinary authenticated research workflows.

**Architecture:** Deny browser database access to backend-owned tables, separate platform control-plane authorization from tenant roles, and add defense-in-depth admission plus runtime ceilings around paid/background work. Server-derived user and organization identity remains authoritative. Existing project ownership, Supabase Auth, pause/resume, and organization-scoped ArXiv persistence must continue to work.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy, Celery, PostgreSQL/Supabase migrations, pytest.

---

## Task 1: Deny PostgREST access to backend-owned product tables (GOO-285)

**Files:**
- Create: `supabase/migrations/20260917000000_revoke_product_tables_data_api_access.sql`
- Create: `backend/tests/unit/test_product_tables_rls_migration.py`

- [x] Add a failing migration contract test that derives every table receiving `authenticated_read_only` in `20260305001600_harden_rls_policies.sql` and requires the new migration to drop that policy and revoke all privileges from `anon, authenticated`.
- [x] Run the focused test and capture the expected red result.
- [x] Add an idempotent migration using `to_regclass`, `DROP POLICY IF EXISTS`, RLS enablement, and `REVOKE ALL`, following the internal-agent-table precedent.
- [x] Rerun the focused test green and retain Supabase Auth behavior by changing table grants only.

## Task 2: Add a platform-operator boundary (GOO-286, GOO-287)

**Files:**
- Modify: `backend/src/core/config.py`
- Modify: `backend/src/core/dependencies.py`
- Modify: `backend/src/api/infrastructure/workers.py`
- Modify: `backend/src/api/arxiv/arxiv_bulk.py`
- Modify: `backend/src/api/arxiv/arxiv_llm_bulk.py`
- Modify: `backend/tests/unit/api/test_workers_authz.py`
- Modify: `backend/tests/unit/api/test_ingest_authz_and_tenant.py`
- Create or modify: nearest focused dependency tests

- [x] Add failing tests proving a tenant `ADMIN` is denied and a configured platform operator is allowed.
- [x] Run those tests red before implementation.
- [x] Add a fail-closed `PLATFORM_OPERATOR_USER_IDS` setting parsed as authoritative UUIDs and a `require_platform_operator` dependency; an empty/malformed allowlist grants nobody.
- [x] Replace tenant-admin dependencies on all shared worker-control and both global Kaggle ArXiv bulk routers.
- [x] Keep legitimate operator status/start/stop/shutdown behavior and verify unrecognized users cannot reach sinks.
- [x] Rerun focused tests green.

## Task 3: Bound paid Research Engine workflows (GOO-288)

**Files:**
- Modify: `backend/src/schemas/research_engine.py`
- Modify: `backend/src/services/research_engine/engine.py`
- Modify: `backend/src/services/research_engine/step_executor.py`
- Modify: `backend/src/api/research_engine/runs.py`
- Modify: `backend/tests/unit/schemas/test_research_engine_schemas.py`
- Modify: `backend/tests/unit/services/test_workflow_engine.py`
- Modify: nearest stream/admission tests

- [x] Add failing tests for oversized step collections, serialized parameter/prompt payloads, connector fanout/results, cumulative token exhaustion, wall-time exhaustion, and repeated/concurrent organization admission.
- [x] Run each security regression red before implementation.
- [x] Enforce a small server-owned maximum step count and bounded nested parameter/prompt sizes on create/update and at runtime so legacy rows cannot bypass validation.
- [x] Bound connector count and `max_results` in the executor before any external call.
- [x] Enforce per-run cumulative token and wall-clock budgets, emitting a bounded failure before another step begins once exhausted.
- [x] Add a shared organization-keyed run admission budget using the repository rate-limiter pattern, applied immediately before the atomic run claim; preserve single-run atomic claim and pause/resume semantics.
- [x] Rerun focused schema, engine, and stream tests green.

## Task 4: Bound direct ArXiv background work (GOO-289)

**Files:**
- Modify: `backend/src/api/arxiv/core.py`
- Create or modify: nearest ArXiv API/admission tests
- Modify task/queue files only if required for the smallest durable implementation

- [x] Add failing tests that reject oversized `paper_ids`, prove ingest and dataset creation share an organization/user admission budget, reject repeated jobs before enqueue, and enforce a deadline in the worker/background execution path.
- [x] Run the focused tests red before implementation.
- [x] Cap paper IDs and aggregate dataset work, validate IDs before enqueue, and use a shared verified organization/user-keyed admission limiter for both expensive routes.
- [x] Prefer the existing durable Celery enqueue pattern when it can carry immutable actor and organization metadata safely; otherwise keep the patch narrow but enforce the same admission and deadline inside the background function as well as at the route.
- [x] Preserve organization-scoped persistence and ordinary bounded jobs.
- [x] Rerun focused tests green.

## Task 5: Verification and review

- [x] Inspect `git diff --check` and the complete branch diff for unrelated changes.
- [x] Run Ruff, Black check, and isort check for every changed Python file.
- [x] Run focused malicious-trigger, alternate-path, legitimate-control, and nearest-package tests.
- [x] Run the repository blocking backend gate if dependencies permit; record pre-existing environment blockers exactly.
- [x] Obtain a fresh read-only bypass/regression review with only the findings, policy, scope, and current diff.
- [x] Address one review cycle and rerun all affected verification.

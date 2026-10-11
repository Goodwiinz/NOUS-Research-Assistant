# Artifact Workspace Completion Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans and superpowers:dispatching-parallel-agents for the independent service and browser tasks. Steps use checkbox syntax.

**Goal:** Complete authenticated exact-version sharing and durable native draft publication from recovered Plan 03.

**Architecture:** Keep immutable artifact bytes and existing publication receipts. Add authenticated share records and a durable native completion intent, serviced by the existing artifact drain. Extend the browser artifact adapter and panel without a second cache.

**Tech Stack:** FastAPI, SQLAlchemy/Alembic, PostgreSQL, React, TanStack Query, Python pytest, Node 24 and pnpm 10.18.2.

**Spec:** [Recovered Linear Plan 03](https://linear.app/goodwiinz/document/plan-03-recovered-artifact-workspace-eeba009e8e0e), tasks 6–7; [October 4 amendment](../../plans/2026-10-04-harness-plan-amendment.md). Source baseline: `ac693e63876b2b751712ae3f50926502e158e16c`, October 11, 2026. This is an implementation plan, not live availability evidence. Verification is recorded in the [dated completion record](../../testing/artifact-workspace-completion-2026-10-11.md).

## Global Constraints

- Preserve all dated plans and the shared checkout; isolated branch based on fresh origin/develop.
- One migration on the current single head; do not start the next migration-bearing subsystem before this one merges and fresh develop has one head.
- Share an exact version only, with an explicit authenticated same-organization audience, maximum seven days.
- Every metadata/content fetch rechecks expiry, revocation, live artifact and project/workspace ancestors. No provenance, source IDs, storage keys or local paths in shared responses.
- Share creation/revocation requires browser authority. ARTIFACT_SHARING_ENABLED remains false by default. Existing reads and owner revocation work when write flags are disabled.
- Native completion intents join the draft completion transaction. They refer to exact persisted draft bytes and use deterministic publication identity; retries cannot create duplicate versions.
- Native contexts are server-created only; external contexts cannot become trusted through missing/expired grant identity.
- Router transport delegates persistence. Regenerate backend OpenAPI and frontend generated types together.
- Live harness, configured-provider and expert acceptance are separate checks; record unavailable prerequisites honestly.

## Review Focus

1. An old share stays pinned after editing, and a recipient outside the audience learns no metadata or bytes.
2. A share already open in the browser loses access after revocation/refetch, and account changes discard prior content.
3. Worker restart after publication but before intent acknowledgement creates one version.
4. A draft cancelled before completion creates no intent; deleted/changed source draft or removed project rights cannot publish.
5. Disabling write flags retains committed reads and lifecycle cleanup.

## Task 1: Authenticated sharing service and transport

**Files:** Add `backend/src/models/artifact_share.py`, `backend/src/services/artifacts/sharing.py`, `backend/tests/unit/services/artifacts/test_sharing.py` and route tests. Modify `backend/src/schemas/artifact.py`, `backend/src/api/artifacts.py`, `backend/src/models/__init__.py`. The controller owns the single migration.

**Interfaces:** `create_share(db, *, user_id, organization_id, version_id, audience_user_ids, expires_at) -> ArtifactShareDTO`; `read_share(db, *, user_id, share_id) -> SharedArtifactVersionDTO`; `read_share_content(db, *, user_id, share_id) -> tuple[bytes, str, str]`; `revoke_share(db, *, user_id, share_id) -> None`. POST /api/v1/artifacts/shares; GET /shares/{id}; GET /shares/{id}/content; DELETE /shares/{id}. Create input forbids extras, bounds audience 1–100 and timezone-aware expiry. Add sharing_enabled to capabilities with compatible false default. Shared DTO has only artifact_id, version_id, title, mime_type, byte_size, sha256.

- [x] Write behavior tests for pinned bytes, audience validation, expiry, revocation, deleted ancestors, browser-only writes and private no-store responses.
- [x] Run tests and observe missing behavior; implement service and transport.
- [x] Run focused publication/edit/share suites; mutation-check revocation/expiry guards and record proof.

## Task 2: Sharing browser flow

**Files:** Extend `frontend/src/services/artifactService.ts`, artifact domain adapter and generated panel; create `ArtifactShareDialog.tsx`, `SharedArtifactView.tsx`, `frontend/app/(dashboard)/artifacts/shared/[shareId]/page.tsx` and colocated tests.

**Interfaces:** createArtifactShare(versionId, audienceUserIds, expiresAt), getArtifactShare(shareId), getSharedArtifactContent(shareId), revokeArtifactShare(shareId). Derive contracts from generated components. Cache keys bind current actor and share identity. Explicit UUID audience selection avoids exposing an organization directory; show seven-day expiry and exact-version link. Shared content uses existing bounded preview/sandbox renderers without editing rights.

- [x] Write failing component tests for explicit audience submission, safe errors, cancellation/account scope and revoked shared-content refetch.
- [x] Implement accessible dialog and authenticated share page; keep buttons capability-gated.
- [x] Run targeted Vitest, typecheck, quality wrapper and full frontend tests.

## Task 3: Durable native completion

**Files:** Add `backend/src/models/native_artifact_intent.py`, `backend/src/services/artifacts/native.py`, `backend/tests/unit/services/artifacts/test_native.py`. Modify draft generation and native tool adapter, models registration, existing artifact task. Controller creates one migration for this and Task 1 tables.

**Interfaces:** `enqueue_native_artifact(db, *, draft_id: UUID, context: IntegrationContext) -> None` writes inside caller transaction; `drain_native_artifacts(db, *, limit: int = 100) -> int` claims pending/stale intents, publishes exact draft with deterministic UUID, acknowledges only after successful publication. Store server-derived actor/org/project/thread/run, draft hash/title and status/attempts/fenced lease. Reject external consent/grants, mismatched draft/task identity and unavailable rights. New writes require ARTIFACTS_ENABLED; pending intents pause while disabled.

- [x] Write failing tests: no started-draft artifact, rollback leaves no intent, exact completed-draft digest, restart/replay one version, changed/deleted draft and revoked edit access denied, bounded retry.
- [x] Implement intent insertion before draft commit and optional native context from trusted tool runtime. The browser project route can issue a server-only context with no chat binding.
- [x] Extend existing artifact drain; keep announcement delivery and reservation sweep unchanged.
- [x] Add the single migration and retention proof for every new thread/run FK; run migration round-trip on local PostgreSQL and mutation-check claims/replays.

## Task 4: Contract, integration and review

**Files:** Generated API files and `docs/testing/artifact-workspace-completion-2026-10-11.md`; current engineering harness contract only for behavior actually implemented.

- [x] Regenerate both API artifacts from application schema.
- [x] Run changed Python lint/type checks, artifact suites, architecture/API/thread matrix, docs-lint and diff checks; preserve matrix skips and Python-version limits in the evidence record.
- [x] Run fresh independent whole-branch review; reproduce and fix consequential findings.
- [ ] Push feature branch and prepare PR against develop with actual verification and remaining live gates.
- [x] Keep Plan 04 and Plan 05 implementation sequencing explicit: context migration first after this merge; recall migration after context merge; bounded profile changes only after configured-model baseline.


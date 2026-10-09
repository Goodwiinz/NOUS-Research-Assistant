# Linear implementation remainder

**Goal:** Close the reproducible implementation gaps identified by the October 9 Linear/dev reconciliation, and retain explicit evidence for anything requiring live deployment or independent expert acceptance.

**Architecture:** Preserve the existing integration consent/grant model, durable agent ledger, artifact version store, workspace access funnel, and single frontend cache owners. Repair those owners rather than introduce parallel infrastructure.

**Tech stack:** FastAPI, SQLAlchemy/PostgreSQL, Python, TypeScript, Next.js, pnpm/Vitest.

**Spec:** The October 9 reconciliation of the [Linear harness guide](https://linear.app/goodwiinz/document/nous-local-harness-mcp-and-artifact-workspace-74fa1a817f31), recovered Plans 02–05 (document IDs recorded in the [implementation evidence](../../testing/linear-remainder-2026-10-09.md)), the September 30 agent architecture repair plan, and the [current harness amendment](../../plans/2026-10-04-harness-plan-amendment.md). This is an execution plan, not a claim that unmerged work is available on dev.

**Global constraints:** Work from fetched `331ef5e84e9bba33234990570eacb9ddb3056150` in isolated worktrees. Preserve the shared checkout. Use failing behavior tests before fixes and mutation verification for races/idempotency. Keep migration changes serial. Regenerate both API artifacts for schema changes. Never substitute offline tests for live or independent expert acceptance.

**Review focus:** Tenant boundaries, consent expiry/revocation, terminal absorption, replay identity, failure cleanup, existing client compatibility, and scientific acceptance claims.

## Task 1: Agent execution repairs

Owner: isolated architecture branch. Files: `backend/src/services/harness/{delivery,runs}.py`, `services/agent/agent_submission_service.py`, `api/agent/execute.py`, `services/agent/fast_path.py`, and focused tests. Implement AF01 cancellation before lease, AF02/03 canonical bounded history and mutation semantics, AF04 provider rejection before side effects, AF06 acknowledgment/budget safety. AF07 is already fixed. Commit only after red/green evidence and lint.

## Task 2: Backend backlog repairs

Owner: isolated backend branch. Files: encryption core/security route, screening service, documents route cleanup, validation runner and focused tests. Complete GOO-412/413/414 and GOO-417 cleanup. Treat coverage floors as a measured ratchet. GOO-415 is already fixed. Commit after targeted regressions and quality gates.

## Task 3: Isolation, reconnect and sidebar

Owner: isolated isolation branch. Files: workspace access, fulltext service, two-account export tests, harness bridge CLI and active `ChatSidebar`. Complete GOO-416/418 and GOO-87. Test negative authorization, real PostgreSQL enum behavior, reconnect persistence failures, and visible sidebar focus/scroll behavior.

## Task 4: Ledger and publication integrity

Owner: integration branch. Files: `backend/src/services/agent/run_event_store.py`, artifact model/service and integration tests. Reproduce AF05 using two PostgreSQL connections, serialize terminal checks and appends on the run row, and mutation-verify. Reproduce publication retry after grant renewal, bind replay to stable consent with organization/project scope, and test concurrent/revoked/cross-consent requests. Any schema change needs one current-head migration and an upgrade/downgrade proof.

## Task 5: Remaining harness plans

Read the full current Plan 02/03/04/05 documents and amendments before editing. Prepare frozen selected skills as the first migration slice, alongside migration-free artifact tabs/editing and bounded previews. Start the next migration-bearing slice only after the preceding migration has merged and a fresh single-head check succeeds, as required by the amendment. Artifact sharing and native completion remain behind that boundary and their actual acceptance prerequisites. Plans 04/05 require an updated concrete design against the current owners before implementation; preserve the existing relay, anchor workflow authority on consent, and record the evaluation baseline before altering graph profiles.

## Task 6: Review, acceptance and handoff

Integrate independently reviewed commits, run changed-file quality gates and relevant combined tests, refresh deployment/Linear evidence, and update the reconciliation with exact commit/test evidence. Run live acceptance only when its actual credentials/services/consenting projects exist. Independent expert gates remain pending until an independent expert signs them. Prepare PRs against `develop` with one `Fixes GOO-nnn` line per resolved issue. Do not mark unmerged changes implemented on dev.

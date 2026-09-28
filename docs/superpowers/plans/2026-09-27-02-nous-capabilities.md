# NOUS Capabilities for External Harnesses Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let managed and standalone Codex sessions use explicitly authorized NOUS retrieval, selected context, and approved note creation through local MCP.

**Architecture:** A local stdio facade translates MCP requests into scoped HTTPS calls. A backend gateway owns authorization, allowlists, provenance, and durable mutation decisions; native NOUS and external note creation share the same action guard. No external caller gets the general agent dispatcher.

**Tech Stack:** Python/FastAPI/Pydantic/SQLAlchemy/PostgreSQL, existing Celery workers, Node 24, TypeScript, pnpm, MCP TypeScript SDK.

**Spec:** [Harness bridge and artifact workspace](../../plans/2026-09-27-harness-bridge-and-artifacts.md), sections 3–5 and 8. Depends on the provider/device contracts in [Plan 01](2026-09-27-01-local-codex-bridge.md).

## Global Constraints

- “Derive actor, organization, project, and thread from an authorized session binding; never accept those identities from model-supplied tool arguments.”
- “The absence of a `DESTRUCTIVE` tag alone is not enough to classify a tool as safe to expose.”
- “Retrying an ambiguous write never repeats it.”
- “An external harness must not self-attest a user decision.”
- “Do not export NOUS's entire internal prompt, hidden state, or memory store.”
- “Do not rewrite global Codex settings silently.”
- Baseline toolchain: “Node 24, pnpm 10.18.2, Ruff 0.15.15, Black 26.5.1, isort 5.13.2.”

## Review Focus

- Valid JWT paired with another principal's restricted grant must fail before reading data (Task 1).
- An empty or foreign document selection must never broaden retrieval to the organization (Task 1).
- Stdio diagnostics, credentials, and model-controlled arguments must not contaminate protocol output or privileged headers (Task 2).
- A process crash after note creation but before receipt persistence must remain uncertain and never repeat the mutation (Task 3).
- A selected memory or skill that becomes unauthorized must disappear or fail closed, including after a workspace soft-delete (Task 4).

---

**Record:** Proposed implementation decisions, 2026-09-27; inspected source `16f824197f85436c84558769e72936e3c1decf2b`. No runtime acceptance has occurred. This document supplements the architecture without rewriting it.

## File map and release boundary

All Create paths below are proposed. Existing source anchors are verified against the baseline.

| Responsibility | Files |
| --- | --- |
| Transport-neutral schemas and authenticated read facade | New `backend/src/schemas/integration_tools.py`; new `backend/src/services/integrations/{gateway,read_tools}.py`; new `backend/src/api/integrations/tools.py` |
| Local MCP transport/client | New `packages/harness-bridge/src/mcp/{client,server,config,stdio}.ts` |
| Shared durable note action and worker | New `backend/src/models/tool_action.py`; new `backend/src/services/agent/tool_actions.py`; new `backend/src/api/integrations/actions.py`; new `backend/src/tasks/integration_action_tasks.py` |
| Browser-only decisions | Consume `backend/src/api/integrations/auth.py` from Plan 01 Task 1 |
| Explicit context selection | New `backend/src/models/integration_context_selection.py`; new `backend/src/services/integrations/selected_context.py`; new `backend/src/api/integrations/context.py` |
| Existing integration points | `backend/src/main.py`, `backend/src/models/__init__.py`, `backend/src/tasks/celery_app.py`, `backend/src/services/agent/_nodes_tools.py` |

The first read allowlist is `search_documents`, `list_project_documents`, `do_kb_retrieve`, and `get_current_draft`. Search becomes project-scoped; content retrieval requires an explicit list of 1–20 document UUIDs. Knowledge-graph reads stay unadvertised until their service can enforce project scope. `create_project_note` is the only general mutation introduced here. `create_draft` stays unadvertised: its current generation status is process-local, and a started task is not a completed durable draft. Durable draft-job integration is follow-up work. Artifact publication is Plan 03's separate run-bound capability.

**Expanded scope:** [Plan 04](2026-09-27-04-conversation-context.md) adds conversation discovery/history/context-document tools through a separate thread-grant resolver. [Plan 05](2026-09-27-05-nous-workflows-and-tracing.md) adds explicit bounded NOUS research-task start/status/cancel tools and tracing. Compose these extensions in the same MCP server only when the appropriate grant family and flag are present. The initial project-tool allowlist is not permission to expose the existing chat-search API or the unrestricted agent graph. Plan 05 adds the internal optional `allowed_document_ids` keyword to `invoke_read`; existing callers retain their project-scope behavior, while workflows must pass the approved set (including an empty set for context-only tasks). Plan 04 extends the protected credential handle into a capability-specific bundle while preserving this plan's public `McpSession` shape.

## Task 1: Authorize and bound the read gateway

**Files:** Create the three gateway files and router from the map; create `backend/tests/unit/services/integrations/test_gateway.py` and `backend/tests/unit/api/test_integration_tools.py`; modify `backend/src/main.py` router registrations around lines 578–658 and `backend/src/core/config.py` to define `NOUS_MCP_ENABLED=False` from the first gateway commit. Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

**Interfaces:** Consume Plan 01's frozen `IntegrationContext(user_id: UUID, organization_id: UUID, project_id: UUID, thread_id: UUID | None, run_id: UUID | None, grant_id: UUID)` and `resolve_integration_context(db: AsyncSession, token: str, *, required_scope: str) -> IntegrationContext`. Produce Pydantic `ToolInvocation(tool_name: str, arguments: dict[str, Any], invocation_id: UUID)` and `ToolResult(content: list[dict[str, Any]], is_error: bool, source_refs: list[dict[str, Any]])`; `async invoke_read(db: AsyncSession, context: IntegrationContext, invocation: ToolInvocation) -> ToolResult`.

- [ ] **Step 1: Add failing authorization and scope tests.** Create fixtures with two actors, organizations, projects, deleted workspace ancestors, and document links. Parameterize `test_read_gate` over each denial below; patch the implementation adapter with `AsyncMock`.

```python
assert response.status_code == expected_code  # JWT/grant mismatch 403; revoked 403; identity argument 422
adapter.assert_not_awaited()
# test_selected_document_scope_never_broadens, empty/foreign/deleted IDs:
assert result.is_error is True
retrieval.assert_not_awaited()
# test_valid_retrieval_preserves_source_identity:
assert result.source_refs[0]["document_id"] == str(allowed_document.id)
assert len(result.source_refs) <= 20
```

- [ ] **Step 2: Run red.** `pytest -q backend/tests/unit/services/integrations/test_gateway.py backend/tests/unit/api/test_integration_tools.py`; expect missing gateway imports/routes.
- [ ] **Step 3: Implement schemas, adapters, and gateway.** `GET /api/v1/integrations/tools` returns the allowlisted schema projections of `TOOL_REGISTRY`; `POST /api/v1/integrations/tools/read` accepts `ToolInvocation`. Gate new integration requests on `NOUS_MCP_ENABLED`; preserve status and reconciliation. Authenticate with existing JWT middleware plus `X-NOUS-Integration-Grant`; resolve the latter with scope `tools:read` and compare its actor/org to the JWT actor. Reject identity keys and extra arguments. Recheck project ownership and every deleted ancestor, then document organization and active project links. Implement `async search_project_documents(db: AsyncSession, context: IntegrationContext, query: str, limit: int) -> dict[str, Any]` in `read_tools.py`, using the existing escaped title-search/redaction pattern, joined to project documents before applying the limit. Reuse `_tool_list_project_documents`, `_tool_do_kb_retrieve`, and `_tool_get_current_draft` only after these checks. Require 1–20 IDs for retrieval, cap list/search results at 50 and serialized results at 64 KiB; return a structured `result_too_large` error rather than broken JSON. Source references carry observed document/chunk/draft IDs; never invent citations.
- [ ] **Step 4: Run green and regenerate contracts.** Repeat Step 2; expect PASS. Run `python scripts/ci/generate_openapi.py`, `pnpm --dir frontend generate:api-types`, then `python scripts/ci/generate_openapi.py --check`; expect exit 0.
- [ ] **Step 5: Commit.** Stage exactly this task's listed files and generated contracts; `git commit -m "feat: add scoped integration read gateway"`.

## Task 2: Serve the same tools to both Codex entry points

**Files:** Create the four MCP modules above and `packages/harness-bridge/src/mcp/server.test.ts`; modify Plan 01's new `packages/harness-bridge/src/cli.ts` and `packages/harness-bridge/package.json`; update `pnpm-lock.yaml` from the root workspace.

**Interfaces:** Produce `McpSession = { apiOrigin: string; credentialHandle: string }`, `CapabilityClient.listTools(): Promise<ToolDescriptor[]>`, `CapabilityClient.invokeRead(invocation: ToolInvocation): Promise<ToolResult>`, `createNousMcpServer(client: CapabilityClient): McpServer`, `buildManagedMcpConfig(session: McpSession): NonNullable<SessionOptions["mcpConfig"]>`, and `runStdioMcp(session: McpSession): Promise<void>`. `ToolDescriptor = { name: string; description: string; inputSchema: Record<string, unknown> }`; HTTP shapes alias `components` from the type-only import `../../../../frontend/src/types/generated/api` in `mcp/client.ts`. Import `SessionOptions` from Plan 01's `src/contracts.ts`; the configuration exactly matches its executable/argument map. The credential handle resolves Plan 01's protected CLI-JWT/grant pair, never a browser token. Plan 03 composes `registerArtifactTools(server: McpServer, publisher: ArtifactPublisher): void` before stdio connection.

- [ ] **Step 1: Add failing stdio tests.** `test_managed_and_standalone_scope` uses an SDK client/server transport pair; `test_stdio_has_no_secrets_or_global_writes` spawns the CLI. Define test-local `runEntry(mode: "managed" | "standalone", session: McpSession, invocation: ToolInvocation): Promise<ToolResult>` using those real entry points and a recording HTTP test server. Its recorded request/config/stdout/write captures supply the assertions below.

```typescript
// node:test and node:assert/strict; these are captures from runEntry's fixture.
test('managed and standalone tools preserve scope and secrets', async () => {
  assert.deepEqual(managed.source_refs, standalone.source_refs);
  assert.equal(request.headers['x-nous-integration-grant'], storedGrant);
  assert.equal(stdoutLines.every((line) => JSON.parse(line).jsonrpc === '2.0'), true);
  assert.equal(JSON.stringify(config).includes(storedGrant), false);
  assert.equal(writtenPaths.includes(globalCodexConfigPath), false);
});
```

- [ ] **Step 2: Run red.** `pnpm --filter @nous/harness-bridge test -- src/mcp/server.test.ts`; expect missing MCP modules.
- [ ] **Step 3: Implement the five interfaces.** Add SDK through root pnpm, respecting the workspace `@modelcontextprotocol/sdk` override; reuse Plan 01's test/build scripts. Register `nous-bridge mcp --session <opaque-local-handle>`. Managed configuration launches that subcommand for the current session only; standalone setup prints an explicit installation command without executing global changes. Logs go to stderr. Reject non-HTTPS API origins except the explicit development loopback origin. Caller arguments cannot set headers, URLs, identity, or credentials; 401/403 returns a stable reauthentication error without retrying a write.
- [ ] **Step 4: Run green.** Repeat Step 2 and `pnpm --filter @nous/harness-bridge type-check`; expect PASS and exit 0.
- [ ] **Step 5: Commit.** Stage the exact Task 2 files; `git commit -m "feat: expose NOUS tools through local stdio MCP"`.

## Task 3: Share fail-closed note approval and durable outcomes

**Files:** Create the action model/service/router/worker above, `backend/src/schemas/tool_actions.py`, `backend/alembic/versions/integration_tool_actions.py`, and `backend/tests/integration/test_integration_tool_actions.py`. Modify `_nodes_tools.py` at `record_hitl_decision`/`_execute_single_tool`, model exports, Celery include configuration, main registrations, MCP client/server, and generated HTTP contracts. Create `frontend/src/services/integrationActionService.ts`, `frontend/src/components/integrations/IntegrationActionApproval.tsx`, its colocated `__tests__/IntegrationActionApproval.test.tsx`, and `frontend/app/(dashboard)/integrations/actions/[invocationId]/page.tsx`. Consume the browser-decision dependency from Plan 01; do not recreate it.

**Interfaces:** Produce `ActionActor(user_id: UUID, organization_id: UUID, project_id: UUID, thread_id: UUID | None, run_id: UUID | None, grant_id: UUID | None)`; `ActionStatus(invocation_id: UUID, state: Literal["requested", "awaiting_approval", "executing", "succeeded", "failed", "outcome_unknown"], result: ToolResult | None)`; `async request_action(db: AsyncSession, actor: ActionActor, invocation: ToolInvocation) -> ActionStatus`, `async decide_action(db: AsyncSession, actor: User, invocation_id: UUID, approved: bool) -> ActionStatus`, `async get_action_status(db: AsyncSession, actor: ActionActor, invocation_id: UUID) -> ActionStatus`, and `async execute_action(db: AsyncSession, invocation_id: UUID) -> ActionStatus`. Consume Plan 01's `require_interactive_user` from `api/integrations/auth.py`, rejecting verified CLI tokens even when a grant header is absent. Native NOUS uses trusted server context with `grant_id=None`; external requests cannot construct this actor. `IntegrationActionApproval({invocationId:string}):ReactElement` uses Query through `integrationActionService` to show the stored target/payload, approve once, and poll the persisted outcome. The MCP pending result carries an authenticated approval-page URL and invocation ID; approval remains a browser action.

- [ ] **Step 1: Add failing PostgreSQL tests.** `test_cli_cannot_decide` obtains `rejected_decision` through the real decision route, parametrized with/without the grant header; `test_changed_payload_conflicts` obtains `changed_arguments` by repeating POST with the original ID. `test_uncertain_note_is_not_repeated` patches the post-effect receipt write to fail, calls `execute_action` twice, then counts persisted notes. `test_concurrent_claim_once` uses two sessions and a barrier before claim. `test_revoked_action_never_executes` and `test_receipt_outage_never_executes` revoke the grant or fail the initial claim commit before execution. `db`, `actor`, and `original_id` identify the seeded request; every test uses `request_action`/`execute_action` directly rather than an undeclared harness.

```python
assert rejected_decision.status_code == 403  # CLI token cannot approve, with or without grant header
assert changed_arguments.status_code == 409  # same ID, different canonical payload
assert (await get_action_status(db, actor, original_id)).state == "outcome_unknown"
assert note_count == 1  # retry and concurrent claim never create another note
assert revoked_before_execution.state == "failed"
assert receipt_store_outage.note_count == 0
```

- [ ] **Step 2: Run red.** `pytest -q backend/tests/integration/test_integration_tool_actions.py`; expect missing model/service. PostgreSQL is required; missing infrastructure is NOT RUN.
- [ ] **Step 3: Implement the shared action guard and worker.** Add unique `(organization_id, user_id, invocation_id)` with canonical argument hash, actor/project/run/grant binding, decision, timestamps, result, and lease. `POST /api/v1/integrations/actions` requests a note with `tools:write`; `GET /api/v1/integrations/actions/{invocation_id}` reads its durable status. `POST /api/v1/integrations/actions/{invocation_id}/decision` requires the interactive NOUS actor and rejects verified `TokenData.is_cli` from `get_current_user_token`, including requests omitting the grant header. Bind the decision once to the stored target/hash. Define synchronous Celery `drain_integration_actions()->int` through existing `run_async`; register include/routes and a 2-second beat scan on the `agent_runs` queue. The scan claims approved requested rows atomically and revalidates authority before `_tool_create_project_note`; denied or revoked requests fail without execution. Commit the claim before the effect; uncertain crashes/timeouts remain `outcome_unknown` and never auto-retry. Native note execution uses this guard with its actual trusted HITL decision, preserving other tools. Existing best-effort receipts are insufficient: `_tool_receipt_exists` currently fails open. Add MCP `request_action` and `get_action_status`, never an approval tool. Pending/unknown output must not claim success. Implement the authenticated approval page with exact stored target and irreversible consumed state; a denied or uncertain action displays its actual outcome. Add browser-component tests for changed-target rejection, duplicate click, and pending versus succeeded text; run `pnpm --dir frontend test -- src/components/integrations/__tests__/IntegrationActionApproval.test.tsx` alongside the Python green command.
- [ ] **Step 4: Run green and prove the guards.** Repeat Step 2 plus `pytest -q backend/tests/unit/agent/test_tool_receipts.py backend/tests/agent/test_hitl_audit_idempotency.py`; expect PASS. Temporarily remove each claim/hash guard and observe its named test fail, restore it, then rerun. Regenerate both HTTP contracts.
- [ ] **Step 5: Commit.** Stage exact Task 3 files; `git commit -m "feat: persist approved integration note actions"`.

## Task 4: Expose only user-selected context and frozen skills

**Files:** Create the context model/service/router above, `backend/alembic/versions/integration_context_selection.py`, `backend/tests/unit/services/integrations/test_selected_context.py`; create `frontend/src/services/integrationContextService.ts`, `frontend/src/components/integrations/ContextSelection.tsx`, its colocated `__tests__/ContextSelection.test.tsx`, and `frontend/app/(dashboard)/integrations/context/[grantId]/page.tsx`; modify model/main registrations, MCP server, and generated contracts.

**Interfaces:** Produce `ContextSelection(memory_ids: list[UUID], skill_names: list[str])`, `async save_selection(db: AsyncSession, actor: User, grant_id: UUID, selection: ContextSelection) -> None`, `async read_selected_context(db: AsyncSession, context: IntegrationContext) -> ToolResult`, and `async load_selected_skill(db: AsyncSession, context: IntegrationContext, skill_name: str) -> ToolResult`.

- [ ] **Step 1: Add failing selection tests.** `test_selection_preserves_provenance` persists two memories, calls `save_selection` with one ID, then parses `read_selected_context` into `returned_memory_ids`/`serialized_result`. `test_skill_version_is_frozen` changes the live version after selection and calls `load_selected_skill`. `test_deleted_workspace_revokes_context` deletes that ancestor before reading. `test_private_tools_are_not_advertised` calls the Task 1 catalog; use these outputs in the assertions below.

```python
assert returned_memory_ids == [selected_memory.id]
assert unselected_secret not in serialized_result
assert skill_result["version_id"] == frozen_version_id
assert deleted_workspace_result.is_error is True
assert "forget_memory" not in advertised_tools
assert "execute_code" not in advertised_tools
```

- [ ] **Step 2: Run red.** `pytest -q backend/tests/unit/services/integrations/test_selected_context.py`; expect missing context service.
- [ ] **Step 3: Implement selection and provenance.** `POST /api/v1/integrations/context/selection` consumes Plan 01's `require_interactive_user` and stores explicit IDs/names for the browser actor's grant; MCP cannot expand it. An authenticated browser selection page lists only currently authorized project memories and frozen-skill choices through Query, defaults to no selection, and saves explicit checked IDs/names. Add an owner-only `GET /api/v1/integrations/context/options?grant_id=...` catalog; never send unselected memory bodies to the external session. Test empty default, explicit selection and grant/account switch in `ContextSelection.test.tsx`, running that focused frontend test in Step 4. `GET /api/v1/integrations/context` and `POST /api/v1/integrations/context/skills/load` require `context:read`. Select up to 25 `ProjectMemory` rows with user/project predicates and an organization check through the authorized project/user joins; the model has no direct organization column. Return observed memory IDs; the current `load_project_memories` string-only return is insufficient provenance. Reuse `create_runtime_snapshot` and `load_project_skill_from_snapshot`, persisting the snapshot association with the grant; preserve existing limits of 32 catalog entries, 3 loaded skills, and 12,000 instruction tokens. A missing durable snapshot must return a stable unavailable result, not a fabricated empty successful selection. Standalone context reads create no thread/run. Recheck access on every read; this plan exposes no internal prompt, global memory, arbitrary tools, or research dispatch. Plan 05 separately authorizes explicit bounded research tasks; it never enables unrestricted recursive dispatch.
- [ ] **Step 4: Run green.** Repeat Step 2 plus `pytest -q backend/tests/unit/agent/test_project_skill_runtime.py`; expect PASS. Regenerate both HTTP contracts.
- [ ] **Step 5: Commit.** Stage exact Task 4 files; `git commit -m "feat: share explicitly selected NOUS context"`.

## Task 5: Prove both entry points and document operational controls

**Files:** Create `backend/tests/integration/test_integration_capabilities_acceptance.py`, `packages/harness-bridge/src/mcp/acceptance.test.ts`, and `docs/testing/integration-capabilities-acceptance.md`; modify `packages/harness-bridge/README.md` created by Plan 01.

**Interfaces:** Consume Tasks 1–4; produce no additional API. Capability rollout gate is the `NOUS_MCP_ENABLED=False` field created in Task 1; new mutation requests additionally require `tools:write`. Read/status/reconciliation of existing actions remain available when new writes are disabled.

- [ ] **Step 1: Add failing acceptance tests.** `test_both_entry_points_retrieve_same_sources` uses Task 2's `runEntry` with a real test backend, seeded project/document, and deterministic retrieval response. `test_standalone_creates_no_messages` compares database counts before/after. `test_disabled_write_keeps_status` creates an approved action, disables `NOUS_MCP_ENABLED`, rejects a new request, then reads the existing ID. `test_foreign_actor_is_denied` swaps the JWT actor.

```python
assert managed_source_ids == standalone_source_ids == [str(document.id)]
assert messages_after == messages_before
assert new_action_response.status_code == 503
assert existing_status_response.status_code == 200
assert foreign_actor_response.status_code == 403
```
- [ ] **Step 2: Run red.** `pytest -q backend/tests/integration/test_integration_capabilities_acceptance.py` and `pnpm --filter @nous/harness-bridge test -- src/mcp/acceptance.test.ts`; expect missing rollout behavior.
- [ ] **Step 3: Verify the existing gates and write the runbook.** Document restricted login, scoped MCP setup, selected context, approval/status, revocation, stderr diagnostics, and unsupported tools. For manual real-Codex proof, record session/run IDs and source IDs from both entry points without source text or credentials. Fake protocol tests do not count as live acceptance.
- [ ] **Step 4: Run green and branch gates.** Repeat Step 2; run `scripts/ci/run_local_ci.sh --skip-tests`, `pnpm --filter @nous/harness-bridge type-check`, `python scripts/ci/generate_openapi.py --check`, `pnpm --dir frontend check:api-types`, and `git diff --check`. Record dependency-limited/live checks as NOT RUN; separate local, hosted CI, and authenticated acceptance results.
- [ ] **Step 5: Commit.** Stage exact Task 5 files including `backend/src/core/config.py`; `git commit -m "test: verify bidirectional NOUS capability access"`.

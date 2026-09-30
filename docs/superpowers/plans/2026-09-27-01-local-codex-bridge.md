# Local Codex Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run owner-authorized local Codex sessions from NOUS chat with durable recovery, exact approvals, and extensible adapters.

**Architecture:** NOUS owns accepted turns, authorization, and persisted events; an outbound authenticated bridge owns local processes and workspace registration. Codex App Server supplies structured execution through a restricted adapter. This plan implements the local bridge boundary only; other NOUS capabilities remain separately scoped.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy/Alembic, existing Celery, Node 24, TypeScript, pnpm 10.18.2, existing assistant-ui/TanStack Query.

**Design basis:** NOUS remains the authorization and persistence authority; the local bridge is the process-execution authority. Provider selection is separate from model selection, and uncertain native work remains quarantined until verified.

**Status:** Active implementation plan for the local Codex bridge; source baseline `bbd86abc8fff68df049d95dcd143efd28bc55cf7` (`develop`).

## Global Constraints

- Node 24; `pnpm@10.18.2`; root `pnpm-lock.yaml` is the only JavaScript lockfile.
- `@nous/harness-bridge` supports inspected Codex `0.153.4` initially; wider compatibility requires fixtures.
- Preserve atomic submission, existing NOUS default, tenant authorization, and the 16 KiB durable-event limit.
- Only the paired device owner launches local runs; permissions intersect backend grant, device policy, and native sandbox.
- `recovering` is nonterminal and retains thread/workspace ownership; never retry an ambiguous native start blindly.
- Scope: Tasks 1–7 below are the complete local Codex bridge feature. NOUS MCP tools and artifact publication are outside this PR and must ship separately.

## Review Focus

- Revoked or cross-project grant during an existing connection: subsequent operations fail closed (Tasks 1, 4).
- Accepted native turn loses its acknowledgment: quarantine rather than duplicate execution (Tasks 2, 4).
- Interrupt acknowledgment arrives before actual termination: retain **Stopping** (Tasks 4, 6).
- Multiple native approvals share an item or reconnect: decisions reach only the exact live callback (Task 5).
- Existing Codex configuration grants broader permissions: reject effective-policy mismatch (Task 3).

---

## File map and prerequisites

Every **Create** path below is proposed. **Modify** paths and named seams were inspected at the baseline. Add ordinary package `__init__.py` files alongside new Python modules. Read [backend](../../engineering/backend.md), [frontend](../../engineering/frontend.md), [testing](../../engineering/testing.md), and [API contracts](../../engineering/api-contracts.md) before execution.

| Responsibility | Files |
| --- | --- |
| Shared identity/grants | `backend/src/schemas/integration_context.py`, `backend/src/models/integration_grant.py`, `backend/src/services/integrations/context.py`, `backend/src/api/integrations/{__init__,auth,grants,devices}.py` |
| Provider lifecycle | `backend/src/models/harness_session.py`, `backend/src/services/harness/runs.py`; existing agent submission, enums, model, sweeper |
| Local executable | `packages/harness-bridge/{package.json,tsconfig.json,src/cli.ts,src/contracts.ts,src/credentials.ts,src/adapters/codex.ts,src/rpc.ts,src/journal.ts,src/connection.ts}` |
| Delivery/approval services | `backend/src/services/harness/{delivery,approvals}.py`, `backend/src/api/harness.py`, `backend/src/tasks/harness_dispatch.py` |
| Chat integration | `frontend/src/hooks/chat/useHarnessConnection.ts`, `frontend/src/components/chat/HarnessSelector.tsx`, existing streaming/service/surface files |
| Proof and operations | Focused tests per task; `docs/engineering/harness-bridge.md`; `tests/e2e/tests/harness-bridge.spec.ts` |

Tasks 1–7 run in order and deliver independently usable local Codex execution. This package must compile without future MCP or artifact modules; optional typed session configuration is only a future composition seam.

## Task 1: Restricted grants and device/workspace pairing

**Files:** Create shared-identity files in the map, `backend/src/models/bridge_device.py`, `backend/alembic/versions/hb01_integration_grants.py`, `backend/tests/unit/services/integrations/test_context.py`, `frontend/app/(dashboard)/integrations/approve/page.tsx`. Modify `backend/src/main.py` composed router registration and `backend/src/models/__init__.py` model registration. Existing `/cli-auth` and `frontend/cli/auth/deviceFlow.ts` remain login references, not browser credentials to copy.

**Interfaces:** Produce frozen Pydantic `IntegrationContext(user_id:UUID, organization_id:UUID, project_id:UUID, thread_id:UUID|None, run_id:UUID|None, grant_id:UUID)` and `async resolve_integration_context(db:AsyncSession, token:str, *, required_scope:str)->IntegrationContext`. Produce `mint_integration_grant(db, *, user_id:UUID, organization_id:UUID, project_id:UUID, scopes:frozenset[str], thread_id:UUID|None=None, run_id:UUID|None=None)->IssuedGrant` and `revoke_integration_grant(db, grant_id:UUID)->None`; both are async. `IssuedGrant` contains `token:str` and `grant_id:UUID`.

Add `device_id:UUID|None=None` to the mint function's keyword parameters. External exchange passes its approved device ID; only internal NOUS job/edit contexts may omit it.

In `auth.py` define shared `async require_interactive_user(request:Request, token:TokenData=Depends(get_current_user_token), user:User=Depends(get_current_user))->User`; reject CLI tokens and integration-grant headers. Sibling plans consume this dependency.

Under `/api/v1/integrations`, define `POST /grant-requests` accepting `GrantRequestCreate(project_id:UUID,device_id:UUID,scopes:set[str])`; return `GrantRequestDTO(id:UUID,status:Literal['pending','approved','denied','expired','consumed'],expires_at:datetime,approval_url:str)`. Owner CLI polls `GET /grant-requests/{id}` without receiving secrets. Browser `POST /grant-requests/{id}/decision` accepts `GrantDecision(approved:bool)` against the unchanged displayed request. Owner CLI `POST /grant-requests/{id}/exchange` returns `IssuedGrant` once; repeat returns 409. Requests expire after ten minutes; session grants after fifteen minutes. `POST /grants/{id}/renew` rechecks unrevoked device/browser consent without widening scopes; `DELETE /grants/{id}` revokes. Device registration uses `POST /devices` with `DeviceCreate(label:str)` and returns `DeviceDTO(id:UUID,label:str)`. `POST /devices/{id}/workspaces` accepts `WorkspaceBindingCreate(workspace_id:UUID,label:str,project_id:UUID)`; `GET /devices` and `GET /devices/{id}/workspaces` return only the current owner's authorized bindings. All routes check ownership; only IDs/labels leave the machine.

The integration-grant model's device binding is nullable for trusted server-created NOUS job/edit contexts in Plan 03. External grant requests always require a device; standalone MCP can register a device without running a persistent daemon. Server contexts still persist explicit scope/consent, expiry and current-access checks; external callers cannot mint them or turn an expired grant into a trusted context.

- [ ] **Write failing authorization tests.** Define SQLite `db` and `issued` fixtures locally; `issued` mints an unbound `tools:read` grant for a permitted project.

```python
async def test_revocation_is_checked_per_call(db, issued):
    ctx = await resolve_integration_context(db, issued.token, required_scope="tools:read")
    assert ctx.grant_id == issued.grant_id
    assert ctx.run_id is None and ctx.thread_id is None
    await revoke_integration_grant(db, issued.grant_id)
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(db, issued.token, required_scope="tools:read")
```

- [ ] **Red:** `pytest -q backend/tests/unit/services/integrations/test_context.py`; expect missing resolver, then denied-scope/revocation assertions until implemented.
- [ ] **Implement identity/grants.** Define `IntegrationAccessDenied`; use random opaque tokens, hashed lookup, expiry, revocation, current project permission and deleted-ancestor checks. Implement the exact grant-request/exchange, grant-renewal/revocation and device/workspace routes above with service-owned transactions. Browser approval fixes actor, project, device and scope ceiling; CLI exchange consumes that approval once. Standard scopes are `harness:execute`, `tools:read`, `tools:write`, `context:read`, `artifacts:publish`, `artifacts:read`, `artifacts:edit`, `artifacts:share`. Subsequent requests require `Authorization: Bearer <existing NOUS CLI JWT>` plus `X-NOUS-Integration-Grant`; actor/org must match resolved context. Device flow supplies a `scope=cli` token; never copy a browser Supabase token into bridge storage. Approval/context-selection routes require verified `TokenData.is_cli == False` even when integration headers are omitted. Never interpret a JWT as an opaque grant. Store only workspace IDs/labels remotely. Test foreign owner/project, broadened scope, CLI attempts to approve, and expiry. Migration uses execution-time Alembic head.
- [ ] **Green:** rerun the focused command; expect PASS. Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts` using canonical generation commands.
- [ ] **Commit:** stage exactly this task's listed files, package markers, and generated HTTP snapshots with explicit `git add` paths; `git commit -m "feat: add restricted integration grants and device bindings"`.

## Task 2: Provider acceptance and nonterminal recovery

**Files:** Create `backend/src/models/harness_session.py`, `backend/src/services/harness/runs.py`, `backend/alembic/versions/hb02_harness_runs.py`, `backend/tests/unit/services/harness/test_runs.py`. Modify `backend/src/{shared/enums.py,models/agent_run.py,models/__init__.py,services/agent/schemas.py,services/agent/agent_submission_service.py,tasks/agent_run_tasks.py}`.

**Interfaces:** Consume Task 1 context. Produce `bind_external_submission(db:AsyncSession, *, run_id:UUID, context:IntegrationContext, device_id:UUID, workspace_id:UUID)->None` inside `accept_submission`'s transaction; no nested commit. Produce `record_observation(db:AsyncSession, *, run_id:UUID, observation:Literal["unknown","running","completed","failed","interrupted"])->HarnessRunDTO` asynchronously. DTO has `status:str`, `workspace_locked:bool`, `cancel_requested:bool`.

- [ ] **Write a failing recovery test.** Fixture `external_run` accepts a real owned thread with external binding; test rollback by injecting binding failure into existing atomic-accept regression setup.

```python
async def test_unknown_preserves_writer(db, external_run):
    run = await record_observation(db, run_id=external_run.id, observation="unknown")
    assert run.status == "recovering"
    assert run.workspace_locked is True
    assert not JobStatus.RECOVERING.is_terminal
```

- [ ] **Red:** `pytest -q backend/tests/unit/services/harness/test_runs.py`; expect missing observation service/status.
- [ ] **Implement binding and status.** `AgentExecuteRequest.execution_provider` is `Literal["nous","codex"]="nous"`, separate from `model`; external requests require device/workspace and a durable authorized thread. Store provider/session IDs and observation separately. Add `recovering` to enum, check constraints and active-thread indexes; retain a unique active device/workspace claim. Teach `_sweep_stale_agent_runs` to delegate external observations rather than consult native job-store absence. Block uncertain-writer reuse; late terminal evidence resolves recovery before normal absorbing finalization. Preserve cancellation intent across state changes.
- [ ] **Green:** rerun focused tests plus `pytest -q backend/tests/unit/services/agent/test_agent_submission_service.py backend/tests/unit/tasks/test_agent_run_tasks.py`; expect PASS. Include late completion and second-writer rejection; mutation-check each ownership guard.
- [ ] **Commit:** explicitly stage listed files and regenerated HTTP snapshots; `git commit -m "feat: preserve external run ownership during recovery"`.

## Task 3: Local package and restrictive Codex adapter

**Files:** Create package manifest/config, `src/{cli,contracts,credentials,rpc}.ts`, `src/adapters/codex.ts`, `test/codex.test.ts` under `packages/harness-bridge/`. Modify root `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`.

**Interfaces:** Produce `HarnessAdapter` methods `probe():Promise<Capabilities>`, `startSession(options:SessionOptions):Promise<NativeSession>`, `resumeSession(id:string, options:SessionOptions):Promise<NativeSession>`, `startTurn(sessionId:string,input:string,commandId:string):Promise<NativeTurn>`, `interruptTurn(sessionId:string,turnId:string):Promise<void>`, `respondToRequest(id:NativeRequestId,response:NativeResponse):Promise<void>`, `closeSession():Promise<void>`. `contracts.ts` defines `NativeRequestId=string|number`, `NativeSession={id:string}`, `NativeTurn={id:string}`, capability booleans for resume/stream/approval/input/cancel/MCP/usage/publish, and the following types:

```typescript
type NativeResponse = {kind:'decision'; allow:boolean} |
  {kind:'answers'; answers:Record<string,string[]>};
type SessionOptions = {
  cwd:string; workspaceId:string;
  policy:{sandbox:'workspace-write'; approvalPolicy:'on-request';
    reviewer:'user'; networkAccess:false; writableRoots:string[]};
  mcpConfig?:Record<string,{command:string; args:string[]}>;
};
```

Also expose `events(signal:AbortSignal):AsyncIterable<AdapterEvent>`; begin consuming it before starting a turn so early notifications cannot be lost. Define `AdapterEvent` as a discriminated union of `{kind:'delta',sessionId:string,turnId:string,text:string}`, `{kind:'tool',sessionId:string,turnId:string,itemId:string,name:string,phase:'started'|'completed',status?:'completed'|'failed'|'declined',preview?:string}`, `{kind:'request',sessionId:string,turnId:string,itemId:string,requestId:NativeRequestId,approvalId?:string,method:string,params:Record<string,unknown>}`, `{kind:'usage',sessionId:string,turnId:string,inputTokens:number,outputTokens:number}`, and `{kind:'terminal',sessionId:string,turnId:string,status:'completed'|'failed'|'interrupted'}`. The `request` method/params are validated against the pinned native request schemas before yielding; unsupported request kinds are denied locally. Task 4 owns the exhaustive mapping into existing typed run payloads, not the UI. EOF/process failure reports transport loss rather than synthesizing a terminal event. Cap JSONL frames at 1 MiB and enforce bounded queue backpressure; stream-chunk long deltas rather than truncating the final answer.

MCP configuration is optional and constructed locally; no browser-supplied executable/config passthrough. Export `integrationHeaders(credentials:IntegrationCredentials):Record<string,string>` from `credentials.ts`; credentials contain `accessToken` and `grantToken`. Persist their pair behind an opaque `credentialHandle`, allowing future MCP composition without tokens in argv.

- [ ] **Write adapter tests** with test-local `FakeAppServer.replyToStart(value:object):void`, `calls(method:string):object[]`, and a factory returning that server, adapter and explicit Task 3 options. The subprocess fixture emits JSON-RPC rather than mocking adapter policy validation.

```typescript
test('rejects weaker effective permissions', async () => {
  server.replyToStart({ sandbox: { type: 'dangerFullAccess' } });
  await assert.rejects(adapter.startSession(options), /policy mismatch/);
  assert.equal(server.calls('turn/start').length, 0);
});
```

- [ ] **Red:** after creating the task's manifest test script, `pnpm --filter @nous/harness-bridge test -- test/codex.test.ts`; expect missing adapter or failed policy assertion. Proposed script: `node --import tsx --test`.
- [ ] **Implement transport/adapter.** Spawn locally configured `codex app-server` with argument arrays; initialize/initialized handshake, bounded incremental JSONL parsing, request IDs and typed terminal notifications. Enforce exact `0.153.4`, explicit cwd/sandbox/approval policy/reviewer, checking effective start/resume settings; deny unsupported permission requests. No arbitrary RPC proxy. Package `connect`/`workspace add` subcommands perform Task 1 exchange and local root registration. Store credentials owner-only; reject symlinked credential files. Keep optional MCP configuration session-scoped for the sibling plan, never mutate global Codex config. Folder registration does not imply folder-only read isolation.
- [ ] **Green:** focused tests plus proposed `pnpm --filter @nous/harness-bridge type-check`; expect PASS, including split/malformed JSONL, subprocess exit, unsupported version, and resume policy drift.
- [ ] **Commit:** stage only package files and three root workspace files; `git commit -m "feat: add restrictive local Codex app-server adapter"`.

## Task 4: Durable delivery, reconciliation, and cancellation

**Files:** Create `backend/src/services/harness/delivery.py`, `backend/src/schemas/harness.py`, `backend/src/api/harness.py`, `backend/src/tasks/harness_dispatch.py`, `backend/tests/unit/services/harness/test_delivery.py`; package `src/{journal,connection}.ts`, `test/recovery.test.ts`. Modify `backend/src/{main.py,tasks/celery_app.py,core/config.py,services/agent/run_event_types.py}` and Task 2 session/migration ownership through a new proposed `hb03_bridge_delivery.py` migration.

**Interfaces:** Consume Tasks 1–3. Produce async `dispatch_pending(db:AsyncSession)->int`, `ingest_bridge_event(db:AsyncSession, context:IntegrationContext, event:BridgeEvent)->int` returning canonical sequence. Python Pydantic and TypeScript envelopes share required `deviceId,runId,commandId,workspaceId:string` UUID fields and `generation:number`. `BridgeCommand` adds `expiresAt:string` UTC timestamp and a discriminated body: `{kind:'start',input:string,sessionId?:string}`, `{kind:'interrupt',sessionId:string,turnId:string}`, or `{kind:'respond',requestId:NativeRequestId,response:NativeResponse,approvalRecordId:string}`. No optional permission overrides. `BridgeEvent` adds `sourceId:string`, `sourceSeq:number` and body `{kind:'event',eventType:RunEventType,payload:TypedRunPayload}` or `{kind:'observation',state:'unknown'|'running'|'completed'|'failed'|'interrupted',sessionId:string,turnId:string|null}`. Permit only assistant/tool/approval/usage producer events; server owns lifecycle events. Validate `TypedRunPayload` against the corresponding existing durable-event model; no unchecked dictionary forwarding.

Local `Journal.execute(command:BridgeCommand, adapter:HarnessAdapter):Promise<void>` owns intent/deduplication; `state(commandId:string):string` and `workspaceLocked(workspaceId:string):boolean` expose recovery state.

- [ ] **Write recovery assertions** using a SQLite-backed local journal fixture whose fake adapter accepts a turn then drops its response.

```typescript
test('ambiguous start is not replayed', async () => {
  await journal.execute(command, adapter);
  await journal.execute(command, adapter);
  assert.equal(adapter.startCalls, 1);
  assert.equal(journal.state(command.commandId), 'recovering');
  assert.equal(journal.workspaceLocked(command.workspaceId), true);
});
```

- [ ] **Red:** `pnpm --filter @nous/harness-bridge test -- test/recovery.test.ts`; expect missing journal/replay protection.
- [ ] **Implement dispatch.** Persist local intent before native calls and source events before upload; acknowledgment follows server commit. Lease pending outbox rows through Celery; PostgreSQL remains authoritative across web workers. WSS `/api/v1/harness/connect` uses existing header/origin authenticator plus Task 1 grant checks, including every delivered action and expiry. Use bounded replay with canonical cursor and stable source IDs; duplicate payload conflicts fail closed. Reconcile native thread/turn history after uncertain delivery, quarantine if unmatched. Cancellation survives reconnect; empty interrupt response never terminalizes. Backend ingestion owns transcript finalization independently of browser presence: call existing `_persist_assistant_message_safe(required=True, client_message_id=run_id, ...)` in `agent_execution_service.py`, then `finalize_submission`; retry that idempotent projection before emitting terminal success. Add `HARNESS_BRIDGE_ENABLED=False`; disabling prevents new work but permits reconciliation/read/cancel.
- [ ] **Enforce the local lease watchdog.** `watchLease(expiresAt:string,adapter:HarnessAdapter,sessionId:string,turnId:string):Promise<void>` interrupts the targeted active turn when its grant expires and blocks new turns/approval responses until renewal is verified. Test expiry without network contact: one targeted interrupt is requested, no new native command is accepted, and the run remains uncertain until terminal evidence. A parent process exit never proves its child processes stopped; retain workspace quarantine until reconciliation or verified local termination.
- [ ] **Green:** rerun Node command and `pytest -q backend/tests/unit/services/harness/test_delivery.py`; expect PASS for duplicates, generation mismatch, expired grants, event gaps, interrupt/completion races and backend restart. Mutation-check replay guards.
- [ ] **Commit:** stage the exact files above; `git commit -m "feat: journal bridge delivery and reconcile interrupted connections"`.

## Task 5: Exact native approvals and user input

**Files:** Create `backend/src/services/harness/approvals.py`, `backend/tests/unit/services/harness/test_approvals.py`, package `src/approvals.ts`, `test/approvals.test.ts`, proposed migration `backend/alembic/versions/hb04_harness_approvals.py`. Modify Task 4 router and Task 2 model.

**Interfaces:** Produce async `resolve_native_request(db:AsyncSession, context:IntegrationContext, request_id:UUID, decision:NativeDecision)->None`. Define `NativeDecision` as one-time allow/deny or validated answers; store full native callback identity, exact target hash, expiry, generation and consumed state. Consume Task 3 `respondToRequest` only after an authorized decision.

- [ ] **Write stale/duplicate decision tests.** Local fixture `challenge` persists an owner-bound live request; reconnect fixture advances generation.

```python
async def test_duplicate_decision_rejected(db, context, challenge):
    await resolve_native_request(db, context, challenge.id, NativeDecision.deny())
    with pytest.raises(NativeRequestConflict):
        await resolve_native_request(db, context, challenge.id, NativeDecision.allow())
```

- [ ] **Red:** `pytest -q backend/tests/unit/services/harness/test_approvals.py`; expect missing resolver/conflict enforcement.
- [ ] **Implement requests.** Bind actor/device/run/command/session/turn/item/JSON-RPC ID/optional native approval ID/generation. Decision routes require verified non-CLI browser authentication even without integration headers; derive service context from the stored challenge and recheck current actor/grants, never expose a grant token to the UI. Persist decision and delivery intent together. Support command/file/permissions approval and required input; reject persistent grants and unknown request kinds. Two callbacks sharing an item remain distinct. Revocation, changed target, reconnect, wrong owner and consumed requests fail closed; external harness output cannot self-attest approval. NOUS mutation approval remains the sibling gateway's separate authority.
- [ ] **Green:** focused Python tests plus `pnpm --filter @nous/harness-bridge test -- test/approvals.test.ts`; expect PASS including redelivery without a second native decision. Regenerate HTTP snapshots.
- [ ] **Commit:** stage listed files and generated snapshots; `git commit -m "feat: bind native approvals to exact live requests"`.

## Task 6: Chat controls and persisted external streams

**Files:** Create the hook/selector from the file map, `frontend/src/services/harnessService.ts`, `frontend/src/hooks/chat/__tests__/useHarnessConnection.test.tsx`, `backend/src/api/agent/harness_streaming.py`. Modify `frontend/src/{components/chat/ChatSurface.tsx,hooks/chat/useChatStreaming.ts,services/agentChatService.ts,services/agentStreamEvents.ts}`, `backend/src/api/agent/streaming.py`, `backend/src/shared/enums.py`.

**Interfaces:** Produce `useHarnessConnection(threadId:string|null):HarnessConnectionController` with selected provider/device/workspace, connection state and pending requests; controller delegates Task 5 decisions. HTTP shapes alias generated API types. Produce `stream_harness_run(request:Request, run_id:UUID, context:IntegrationContext, after_seq:int=0)->AsyncIterator[str]` as an async generator. Open short-lived `AsyncSessionLocal` sessions around existing `read_events(db,str(run_id),organization_id=context.organization_id,user_id=context.user_id,after_seq=...)`; route derives context from the accepted owner binding. Browser disconnect ends observation, not execution.

- [ ] **Write UI tests** using mocked Task 4 events and the real hook rendered in its Query provider. Define test-local `renderConnectedHarness():{stop():Promise<void>;receive(event:{type:string}):void;status():string;canSend():boolean}` using `renderHook`, QueryClientProvider and mocked transport.

```typescript
it('keeps stopping after interruption receipt', async () => {
  const view = renderConnectedHarness();
  await view.stop();
  view.receive({ type: 'interrupt_ack' });
  expect(view.status()).toBe('Stopping');
  expect(view.canSend()).toBe(false);
});
```

- [ ] **Red:** `pnpm --dir frontend test -- src/hooks/chat/__tests__/useHarnessConnection.test.tsx`; expect missing hook/failed stopping assertion.
- [ ] **Implement view integration.** Branch `stream_event_generator` after verified durable acceptance and before native graph dispatch; external replay never invokes LangGraph. Map persisted events to existing SSE using `_format_sse_event`; Task 4 already owns message/terminal persistence. Query owns device/server state; selection is thread-scoped. Show **Connection lost — checking execution state** separately from outcome. Extend current assistant-ui callbacks for exact approvals; preserve native NOUS handling. Switch/account logout clears scoped state; disabled capabilities explain why. Do not add another chat framework.
- [ ] **Green:** run `pnpm --dir frontend test -- src/hooks/chat/__tests__/useHarnessConnection.test.tsx src/hooks/__tests__/useChatStreaming.submitLock.test.tsx src/services/__tests__/agentStreamEvents.contract.test.ts`; expect PASS. Verify refresh, Stop race, provider switch, keyboard focus and stale-thread isolation in the new hook suite.
- [ ] **Commit:** stage only task files and regenerated HTTP snapshots; `git commit -m "feat: connect NOUS chat to managed local harness runs"`.

## Task 7: Adapter conformance, acceptance, and rollout

**Files:** Create package `test/conformance.test.ts`, `packages/harness-bridge/README.md`, `tests/e2e/tests/harness-bridge.spec.ts`, `docs/engineering/harness-bridge.md`. Modify package test scripts only if needed for separate live acceptance.

**Interfaces:** Consume Tasks 1–6 only. Export test helper `assertAdapterConformance(adapter:HarnessAdapter):Promise<void>`; it checks advertised capabilities against observed behavior. A text-only fake advertises no resume/approval support.

- [ ] **Write failing conformance tests.**

```typescript
test('capabilities match behavior', async () => {
  await assert.rejects(assertAdapterConformance(falseResumeAdapter), /resume/);
  await assertAdapterConformance(codexFixtureAdapter);
});
```

- [ ] **Red:** `pnpm --filter @nous/harness-bridge test -- test/conformance.test.ts`; expect missing conformance runner.
- [ ] **Implement acceptance/operations.** Document pairing/revocation, data disclosure, folder-read limits, recovery/quarantine and kill switch. Live E2E uses an explicitly configured test device: NOUS starts real Codex against a local fixture, persists its answer, routes an approval, then survives reload/disconnect without duplication. This first plan does not call NOUS MCP or publish artifacts; later plans extend this journey. Missing credentials skip with a reason, never pass live acceptance. Record run/session IDs and local/CI/live outcomes separately. Additional adapters remain gated until this suite passes. Ordinary bridge startup requires no Python helper; artifact publication's later Python prerequisite belongs to that optional capability.
- [ ] **Green:** conformance command; `pnpm --dir tests/e2e exec playwright test harness-bridge.spec.ts --project=chromium`; expect PASS only with configured live prerequisites. Run `scripts/ci/run_local_ci.sh --frontend`, generated-contract checks and `git diff --check`; report unavailable gates.
- [ ] **Commit:** stage these four files and any intentional script change; `git commit -m "test: prove local harness integration and document rollout"`.

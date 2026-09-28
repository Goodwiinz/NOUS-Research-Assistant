# NOUS Research Workflows and Tracing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an external harness explicitly ask NOUS to research through its existing LangGraph, with durable task controls and trustworthy LangSmith correlation.

**Architecture:** Direct MCP tools remain available; a separate asynchronous workflow accepts a browser-authorized child task and runs a restricted native NOUS profile. Durable submissions, checkpoints, receipts and events determine business state. Server-created traces correlate bridge observations, gateway calls and native execution without trusting daemon-supplied tracing context.

**Tech Stack:** Existing FastAPI/Pydantic, SQLAlchemy/Alembic, Celery, LangGraph/LangSmith, Node 24 MCP bridge, and Next.js/TanStack Query.

**Spec:** [Harness architecture](../../plans/2026-09-27-harness-bridge-and-artifacts.md), supplemented by the user's 2026-09-27 voice decisions: explicit native NOUS research delegation and cross-boundary tracing.

**Status:** Proposed plan update only, 2026-09-27; source baseline `16f824197f85436c84558769e72936e3c1decf2b`. No code or live execution is included.

## Global Constraints

- Requires Plan 01 grants/recovery, Plan 02 read gateway/MCP, and Plan 03 safe publication; Plan 04 revision reads are optional.
- `workflows:run` plus browser workflow consent and current project/source authorization is required; source-read context is not permission to create tasks or append messages. The fixed workflow profile receives separately delegated server-only read/publication authority, never extra rights on the caller's grant.
- Use an explicitly approved child/target thread, never the active parent writer's thread. No automatic NOUS/Codex exchanges, workflow recursion or harness delegation.
- Proposed `research_readonly_v1` defaults: 20 graph-node admissions, at most 20 tool invocations, 300 seconds from first execution claim; clients cannot override them.
- Keep native normal chat unchanged. New workflow tracing exports identifiers/status/timing only, including local/test environments; no prompts, results, credentials or hidden reasoning.
- Use canonical engineering and root lockfile/API generation rules. All commands below describe future authorized implementation.

## Review Focus

- Retried request changes its prompt or selected sources: reject, never bind it to a different task (Task 1).
- Read consent points at the active parent chat: deny workflow targeting unless a distinct writer target was explicitly approved (Task 1).
- Revocation or cancellation occurs between retrieval and publication: block the next operation and preserve truthful completion state (Tasks 2–3).
- Worker dies after an uncertain publication/start: quarantine and reconcile persisted identities rather than rerun the graph (Task 3).
- A daemon forges tracing headers or LangSmith is unavailable: neither trace ownership nor business outcome changes (Task 4).

---

## Source seams and file map

`agent_submission_service.accept_submission` commits user message/run/event/outbox together. Its outbox is currently intent-only: no pending relay exists. `agent_run_service.claim_execution` is one-shot; `_execute_agent_job` calls `_run_agent_graph`. That runner currently loads project memories and uses a 360-second timeout. `_builders.py` includes memory-saving and specialized subgraphs with recursion limit 50; a tool-menu filter alone cannot enforce this profile.

`build_trace_metadata` already allowlists identifiers and bounds values to 128 characters. `configure_langsmith` hides deployed I/O, but permits local/test visibility. [LangGraph documentation](https://docs.langchain.com/oss/python/langgraph/overview) supports retaining the existing stateful orchestration. [LangSmith distributed tracing guidance](https://docs.langchain.com/langsmith/distributed-tracing) explicitly limits inbound tracing headers to trusted internal services.

All **Create** paths below are proposed; earlier-plan paths are prerequisites, not current implementation claims.

| Responsibility | Create |
| --- | --- |
| Consent, request identity, DTOs | `backend/src/models/nous_workflow.py`, `backend/src/schemas/nous_workflow.py`, `backend/src/services/workflows/{__init__,service}.py`, `backend/src/api/integrations/workflows.py` |
| Restricted native execution | `backend/src/services/workflows/{profile,guard,publisher}.py` |
| Durable dispatch | `backend/src/tasks/nous_workflow_tasks.py`, `backend/src/services/workflows/dispatch.py` |
| Trusted tracing | `backend/src/observability/integration_tracing.py`, `packages/harness-bridge/src/observability.ts` |
| Browser consent/status | `frontend/src/services/nousWorkflowService.ts`, `frontend/src/components/integrations/WorkflowConsent.tsx`, `frontend/app/(dashboard)/integrations/workflows/page.tsx` |
| Proof | Named test files in each task; `docs/testing/nous-workflow-acceptance.md` |

## Task 1: Explicit target consent and atomic workflow acceptance

**Files:** Create consent/request/DTO files above, `backend/alembic/versions/nw01_nous_workflows.py`, `backend/tests/unit/services/workflows/test_acceptance.py`; modify `backend/src/{main.py,models/__init__.py,api/integrations/__init__.py,services/agent/agent_submission_service.py,core/config.py}` and regenerate HTTP contracts.

**Interfaces:** Frozen extra-forbidden `WorkflowStart(request_id:UUID,prompt:str,document_ids:list[UUID]=[],context_revision_ids:list[UUID]=[],consent_id:UUID|None=None)`; prompt limit 8,000 characters, 0–20 documents, 0–5 context revisions, at least one approved source. Omitted consent resolves the grant's attached browser policy; supplied ID must match it. `WorkflowDTO` contains workflow/run/thread/correlation UUIDs, `status:JobStatus`, `error_code:str|None`, `result:WorkflowResultDTO|None`. Define `WorkflowResultDTO(answer:str,answer_truncated:bool,source_refs:list[WorkflowSourceRef],artifacts:list[ArtifactReferenceDTO])`; answer is capped at 16,000 characters, source refs at 50, artifacts at 10. Whole serialized responses must fit 128 KiB; overflow returns `result_too_large`, never malformed partial JSON. `WorkflowSourceRef` discriminates document/chunk IDs from thread/revision/message IDs with server-generated source URLs. Full report uses Plan 03. Produce async `accept_workflow(db:AsyncSession,context:IntegrationContext,request:WorkflowStart)->WorkflowDTO`, `get_workflow(db,context,workflow_id:UUID)->WorkflowDTO`, and `cancel_workflow(db,context,workflow_id:UUID)->WorkflowDTO` in `service.py`; reauthorize selected sources before returning derived content, denying the result after revocation rather than hiding citations. Define `WorkflowConflict` and `WorkflowPolicyDenied` in the schema module.

**Server-only delegation rule:** Browser workflow consent explicitly authorizes the fixed profile to read the selected sources and publish Markdown into the chosen child/target. The caller needs `workflows:run`; it does not need direct `tools:read` or `artifacts:publish`. Create a distinct internal child grant with exactly `frozenset({"tools:read", "artifacts:publish"})`, bound to the workflow, child run/thread, parent grant and consent. Derive these scopes from the fixed profile's approved effects, not a scope union or intersection with the caller's direct-tool scopes. Intersect actor/org/project, selected sources, target, current access and expiry with the parent/consent authority. Never return the child token through DTOs, traces, MCP or browser responses; persist only its hash and server-side grant identity. The worker reconstructs `IntegrationContext` from that identity only after `guard_workflow_step` revalidates all restrictions. Context-only tasks still pass an empty document set, which denies document reads. This rule cannot mint general mutation, sharing, harness or further workflow authority.

- [ ] **Write failing tests.** Test-local `consent` authorizes new children within one owned conversation, approved documents and an unexpired grant. `request` uses that consent; `changed_prompt` returns `request.model_copy(update={"prompt":"changed"})`.

```python
async def test_replay_binds_exact_payload(db, context, request, changed_prompt):
    first = await accept_workflow(db, context, request)
    assert (await accept_workflow(db, context, request)).run_id == first.run_id
    with pytest.raises(WorkflowConflict):
        await accept_workflow(db, context, changed_prompt)
    assert first.thread_id != context.thread_id
```

Add `test_workflow_scope_does_not_expand_caller`: its `workflow_only_context` fixture holds only `workflows:run` and valid browser consent; exercise real acceptance/profile gateways with fake model/storage. Assert bounded research can save its Markdown report while direct external read/publication calls with the same parent credential remain denied:

```python
assert child_grant.scopes == {"tools:read", "artifacts:publish"}
assert direct_read_response.status_code == 403
assert direct_publish_response.status_code == 403
assert child_token not in serialized_workflow_response
assert saved_report.thread_id == accepted.thread_id
```

Also assert missing workflow consent, an unselected source, or a revoked parent raises `WorkflowPolicyDenied` before any child effect. Child authority must not survive parent revocation.

- [ ] **Red:** `pytest -q backend/tests/unit/services/workflows/test_acceptance.py`; expect missing workflow service, then replay/target assertions until implemented.
- [ ] **Implement consent/acceptance.** `POST /api/v1/integrations/workflow-consents` requires Plan 01 `require_interactive_user`. `WorkflowConsentCreate` fixes grant/project, document IDs, source-thread/revision pairs and `{mode:"new_child",conversation_id}` or `{mode:"existing",thread_id}`. New-child consent defaults to ten starts, one concurrent task and 24-hour expiry; existing-target consent permits one start on that exact idle thread. Revalidate edit/project/source access. Extract transaction-internal `_accept_submission_in_transaction(db,*,current_user,request,thread,kind)->AcceptedSubmission` behind the unchanged committing wrapper. `accept_workflow` atomically checks request/hash, debits consent, creates/locks the target, and inserts receipt/source selection/native message/run/event/outbox. For new children call existing `services/research/project_thread_service.attach_thread_to_project(db,thread,project_id,link_type=ProjectThreadLinkType.AUTO.value,linked_by_id=context.user_id)` inside this transaction: column, join row and RAG scope must agree. The profile still uses narrower approved sources. Derive a new frozen child-run `IntegrationContext` and internal grant without a device under the server-only delegation rule above; expiry cannot exceed the parent grant, consent or execution deadline. Recheck parent revocation; never reuse its thread/run identity. Retries do not consume quota/create children. Reject parent/active targets; set `NOUS_WORKFLOWS_ENABLED=False`. New external starts require both this flag and `NOUS_MCP_ENABLED`; flag changes never bypass current authorization for status/results.
- [ ] **Green:** `pytest -q backend/tests/unit/services/workflows/test_acceptance.py backend/tests/unit/services/agent/test_agent_submission_service.py`; expect PASS for rollback, racing duplicates, context-only sources, actor forgery and parent targeting. Mutation-check idempotency.
- [ ] **Commit:** explicitly stage task files and generated HTTP snapshots; `git commit -m "feat: accept explicitly authorized NOUS workflow tasks"`.

## Task 2: Bounded profile using the existing NOUS graph

**Files:** Create profile/guard/publisher files above and `backend/tests/unit/services/workflows/test_profile.py`; modify `backend/src/services/agent/{_builders,_nodes_llm,_nodes_tools,agent_execution_service}.py`, Plan 02 `backend/src/services/integrations/{gateway,read_tools}.py`, and `backend/src/core/config.py`.

**Interfaces:** Produce immutable `WorkflowProfile(name:str,max_steps:int,max_tool_calls:int,max_seconds:int)` with server constant `RESEARCH_READONLY_V1`. `guard_workflow_step(db:AsyncSession,workflow_id:UUID,*,kind:Literal["node","tool"])->IntegrationContext` is async and atomically admits/counts work. Extend internal `_run_agent_graph(job_id,request,current_user,*,workflow_id:UUID|None=None)` and `compile_agent_graph(...,profile:WorkflowProfile|None=None)`; include profile identity in graph-cache keys. Async `publish_research_report(db,context,*,workflow_id:UUID,tool_call_id:str,title:str,markdown:str)->ArtifactVersionDTO` wraps Plan 03 publication.

Extend `invoke_read(db,context,invocation,*,allowed_document_ids:frozenset[UUID]|None=None)->ToolResult` internally. `None` preserves project-tool behavior; an empty set denies document reads before backend invocation. The workflow always supplies its verified set; SQL filters apply before ranking/limit and retrieval fetches.

- [ ] **Write forbidden-operation/budget tests.** Test-local `execute_profile_tool(name,args)` exercises the real profile dispatcher with fake read/model services and isolated storage.

```python
async def test_profile_refuses_recursion_and_mutation(execute_profile_tool):
    for name in ["start_nous_workflow", "create_project_note", "run_harness"]:
        with pytest.raises(WorkflowPolicyDenied):
            await execute_profile_tool(name, {})
    assert RESEARCH_READONLY_V1.max_steps == 20
    assert RESEARCH_READONLY_V1.max_seconds == 300
```

Add `test_selected_docs_filter_before_limit`: seed an unselected higher-ranked match, search with limit one, and assert `result.source_refs[0]["document_id"] == str(selected_id)`. Add `test_empty_selection_never_reads`: pass `frozenset()`, assert `result.is_error is True` and `backend_read.assert_not_awaited()`. Fixtures call the real gateway/SQL filter, mocking only content retrieval.

- [ ] **Red:** `pytest -q backend/tests/unit/services/workflows/test_profile.py`; expect missing profile/guard.
- [ ] **Implement restricted execution.** Reuse NOUS planner, LLM, compactor and reflection nodes through the profile-aware builder; omit specialized subgraphs, implicit memory retrieval/save, project-memory loading and ordinary unscoped preprocessing. Initialize only the prompt and approved sources; use a workflow-specific checkpoint namespace, never ambient target-thread history. Advertise/execute only Plan 02's `search_documents`, `list_project_documents`, `do_kb_retrieve` through `invoke_read`, further restricted to consent-selected documents, plus `publish_research_report`. Check the allowlist again at execution, including injected tool calls. Every node/tool admission checks deadline, counters, actor/project/grant/source access and cancellation; retries consume admissions too. Persist counters/deadline across checkpoints. Twenty steps and 300 seconds deliberately tighten current 50/360 defaults for this profile only. Publishing is Markdown-only, capped at 256 KiB, with deterministic workflow/tool-call publication identity; reuse receipts and recheck cancellation before finalization. No agent-to-agent tool is exposed. Optional context revisions use Plan 04 `read_context` with server-derived authorized `ConversationContext`; stale/missing revisions fail explicitly.
- [ ] **Green:** `pytest -q backend/tests/unit/services/workflows/test_profile.py backend/tests/unit/services/test_agent_graph_topology.py backend/tests/unit/services/test_builders_graph_cache.py backend/tests/unit/agent/test_tool_registry_policy.py`; expect PASS for step 21, deadline, revocation, blocked memory writes, clean default graph and idempotent publication.
- [ ] **Commit:** stage exact task paths; `git commit -m "feat: restrict native research workflows to bounded authorized work"`.

## Task 3: Durable native dispatch and MCP task controls

**Files:** Create dispatch/worker files, `backend/tests/integration/test_nous_workflow_dispatch.py`, `packages/harness-bridge/src/mcp/workflows.ts`, `packages/harness-bridge/src/mcp/workflows.test.ts`; modify `backend/src/{tasks/celery_app.py,tasks/agent_run_tasks.py,services/agent/agent_submission_service.py}`, Task 1 router plus `backend/src/main.py` owner-router registration, and Plan 02 `mcp/{client,server}.ts`.

**Interfaces:** Async `dispatch_workflows(db:AsyncSession,*,limit:int=100)->int` reads only new outbox kind `agent.workflow.execute`; synchronous Celery `relay_nous_workflows()->int` wraps it through existing `run_async`, and `run_nous_workflow(workflow_id:str)->dict` owns startup. Register both task routes on existing `agent_runs` queue, with relay beat every 2 seconds and limit 100. MCP registers exactly `start_nous_workflow(WorkflowStart)`, `get_nous_workflow({workflow_id})`, `cancel_nous_workflow({workflow_id})`; HTTP paths are `POST /api/v1/integrations/workflows`, `GET /api/v1/integrations/workflows/{id}`, `POST /api/v1/integrations/workflows/{id}/cancel`.

- [ ] **Write delivery tests.** `deliver(workflow_id)` executes the real worker against a counting fake graph; `accepted` is Task 1's persisted receipt.

```python
async def test_duplicate_delivery_never_restarts(accepted, deliver, graph):
    await deliver(accepted.workflow_id)
    await deliver(accepted.workflow_id)
    assert graph.start_count == 1
    assert graph.thread_id == str(accepted.thread_id)
```

- [ ] **Red:** `pytest -q backend/tests/integration/test_nous_workflow_dispatch.py`; expect missing relay/worker.
- [ ] **Implement delivery/control.** Extend submission-service outbox accessors; the relay must not query private tables directly or consume normal stream intents. Claim delivery leases, enqueue workflow IDs, stamp acknowledgment. Worker uses existing one-shot execution claim and restores persisted profile/source/context. Preserve checkpoints/result/transcript/artifact references beyond Redis job TTL. Lost post-claim workers enter `recovering`; inspect checkpoints/publication receipts, never automatically rerun uncertain work or release its writer. Teach the existing stale-run sweeper to recognize the workflow receipt and delegate to workflow reconciliation before considering Redis/job-cache absence; `test_sweeper_keeps_uncertain_workflow_owned` removes that cache and asserts recovering status plus the retained thread lock. Queued cancellation terminalizes before dispatch; running cancellation persists intent and waits for bounded execution to stop. Persist server-derived `parent_run_id`; explicit parent cancellation requests child cancellation once, ordinary parent completion does not. Standalone tasks have no parent. Revocation blocks access and requests stop; uncertainty remains recovering. Unexpected approvals fail `policy_denied`. `get_workflow` rejects revoked external grants; browser-only `GET /api/v1/workflows/{id}` and `POST /api/v1/workflows/{id}/cancel` consume `require_interactive_user` and async `get_workflow_for_owner(db:AsyncSession,user:User,workflow_id:UUID)->WorkflowDTO` / `cancel_workflow_for_owner(db:AsyncSession,user:User,workflow_id:UUID)->WorkflowDTO`. Both recheck ownership and current resource access; they do not revive revoked external credentials. No polling-driven restart.
- [ ] **Green:** repeat Python command; `pnpm --filter @nous/harness-bridge test -- src/mcp/workflows.test.ts`; expect PASS for duplicate scheduling, stale lease, parent/child cancellation, revoked grant and worker crash after publication. Mutation-check claim/receipt guards.
- [ ] **Commit:** stage listed files and regenerated contracts; `git commit -m "feat: dispatch and control durable NOUS research workflows"`.

## Task 4: Trace trusted boundaries without exporting content

**Files:** Create tracing files from map, `backend/src/schemas/integration_trace.py`, `backend/tests/unit/observability/test_integration_tracing.py`; modify `backend/src/{services/agent/trace_metadata.py,services/agent/observability.py,schemas/harness.py,api/harness.py,api/integrations/tools.py,services/artifacts/service.py}`, workflow worker/router, package `src/{contracts.ts,mcp/client.ts}`, and optional Plan 04 `services/conversation_context/refresh.py`; extend MCP client tests. Earlier-plan files are prerequisites.

**Interfaces:** Schema defines `TraceActor(user_id:UUID,org_id:UUID,thread_id:UUID|None,run_id:UUID|None,grant_id:UUID|None)`, derived from authorized server records, including projectless context refresh. `TraceOperation=Literal["bridge","tool","workflow","context","artifact"]`. `new_integration_trace(*,actor:TraceActor,operation:TraceOperation,workflow_id:UUID|None=None)->TraceIdentity` creates server correlation; `TraceIdentity(correlation_id:UUID,root_run_id:UUID|None)` persists authorized invocation linkage. No project is invented. `integration_trace(identity:TraceIdentity,operation:TraceOperation)->AsyncContextManager[None]` is best-effort. Bridge `recordObservation(commandId:string,event:ObservationKind):void` journals timing/status; `ObservationKind` is start/approval/stop/disconnect/completed/failed. With LangSmith disabled, correlation still exists and `root_run_id` is null; allocating an SDK ID is not verified hosted trace evidence.

- [ ] **Write forged-context/outage tests.** Test-local `client.start(headers)` uses actual ingress; `client.finish_workflow()` drains its worker with fake model/read tool. Exporter records every attempted span before raising, so failure assertions cannot pass vacuously.

```python
async def test_untrusted_parent_and_export_failure(client, exporter):
    exporter.fail = True
    response = await client.start(headers={"langsmith-trace":"forged", "baggage":"secret=x"})
    await client.finish_workflow()
    assert response.status_code == 202
    assert response.json()["correlation_id"] != "forged"
    assert {"workflow", "llm", "tool"} <= {span.kind for span in exporter.spans}
    assert all(span.inputs == {} and span.outputs == {} for span in exporter.spans)
    assert all("secret" not in (span.error or "") for span in exporter.spans)
```

- [ ] **Red:** `pytest -q backend/tests/unit/observability/test_integration_tracing.py`; expect missing safe ingress/wrapper.
- [ ] **Implement trace correlation.** Strip inbound `langsmith-trace`, `baggage`, `traceparent` and `tracestate` before SDK extraction, including paired daemons. Never install trusting public tracing middleware. Resolve echoed correlation through stored actor/grant/run mapping; ignore foreign IDs. Create spans for bridge events, MCP tools, native workflows, publication and context refresh. Only trusted internal workers use server-stored parent context. Extend explicit metadata allowlist with bounded identifiers. Use a profile-owned redacting SDK client/callback configuration, never process-wide environment toggles that race ordinary chats. Capture every exported descendant LLM/tool span in tests. If the SDK cannot isolate/redact descendants reliably, disable automatic instrumentation for this profile and emit explicit metadata-only spans. No local/test exception or hidden reasoning. Native Codex spans cover exposed events only. Export failures never roll back acceptance, change task outcome, or require retry; durable records remain authoritative.
- [ ] **Green:** `pytest -q backend/tests/unit/observability/test_integration_tracing.py backend/tests/unit/services/agent/test_trace_metadata.py backend/tests/unit/services/test_langsmith_hide_io.py`; expect PASS for forged parents, foreign correlation, secret sentinels and disabled/export-failing SDK.
- [ ] **Commit:** stage exact task paths; `git commit -m "feat: correlate trusted integration traces without content export"`.

## Task 5: Explicit browser task launch and acceptance evidence

**Files:** Create browser files in map, colocated `__tests__/WorkflowConsent.test.tsx`, `tests/e2e/tests/nous-workflow.spec.ts`, `docs/testing/nous-workflow-acceptance.md`; modify Plan 02 MCP composition and generated HTTP types as needed.

**Interfaces:** `WorkflowConsent({grantId:string}):ReactElement` presents project/sources, child destination, budget, run/concurrency caps and expiry; its browser action calls Task 1 consent API. `nousWorkflowService.get(id:string):Promise<WorkflowDTO>` uses the browser-owner status route, generated types and Query; task status links to the actual persisted child thread.

- [ ] **Write UI acceptance assertions** using rendered consent and a recording API fixture.

```typescript
it('requires explicit creation authority', async () => {
  render(<WorkflowConsent grantId={grantId} />);
  expect(api.workflowStarts).toHaveLength(0);
  await user.click(screen.getByRole('button', {name:'Enable bounded research tasks'}));
  expect(api.consents[0].target.mode).toBe('new_child');
});
```

- [ ] **Red:** `pnpm --dir frontend test -- src/components/integrations/__tests__/WorkflowConsent.test.tsx`; expect missing component/authority flow.
- [ ] **Implement UI/proof.** Separate direct tools from **Ask NOUS to research**. A browser enables the bounded reusable policy once; subsequent explicit starts within that ceiling need no extra approval. An existing-target policy remains one-shot. MCP returns a durable task ID immediately; completion requires terminal native state and persisted output. Surface queued/running/stopping/recovering and artifacts. The live journey starts from managed and standalone Codex, reads task status, cancels one run, and verifies separate child threads plus metadata-only traces. Test direct tools still work without workflow scope and quota exhaustion never silently expands consent. Keep new-start flag disabled until acceptance; retain authorized status/cancel/reconciliation when disabled.
- [ ] **Green:** repeat UI command; `pnpm --dir tests/e2e exec playwright test nous-workflow.spec.ts --project=chromium`; run canonical branch gates and `git diff --check`. Record SHA, request/run/checkpoint/artifact IDs, checksum and optional trace ID; distinguish mocked protocol, hosted CI and live model evidence. Missing providers skip explicitly, never pass live acceptance.
- [ ] **Commit:** stage listed UI/E2E/document files and snapshots; `git commit -m "feat: expose explicit research workflow consent and task status"`.

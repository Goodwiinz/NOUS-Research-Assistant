# Workspace Threads and Agent Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** List all accessible threads across a workspace and ensure agent requests never replace an explicit inaccessible thread or proliferate conversation containers.

**Architecture:** Add one service query and additive nested route for globally paginated workspace threads, leaving conversation-scoped endpoints intact. Replace the agent resolver's owner-only fallthrough with the canonical access funnel, typed failures, edit authorization, deterministic workspace/conversation selection, and a workspace row lock. The client treats the workspace page as its sidebar source while keeping the default conversation only for new-thread parenting.

**Tech Stack:** Python 3.12, FastAPI/Pydantic, SQLAlchemy 2 async/PostgreSQL, React/TypeScript, Zustand, Vitest, OpenAPI/openapi-typescript.

**Spec:** `docs/superpowers/specs/2026-09-12-agent-citations-workspace-threads-design.md`

## Global Constraints

- Add `GET /api/v2/workspaces/{workspace_id}/threads`; do not alter nested or standalone conversation-thread endpoints.
- Reuse `ThreadListResponse`, each row's actual `conversation_id`, and `last_message_preview_expression()`.
- Resolve workspace access through `workspace_access.get_workspace(..., load_conversations=False, load_collections=False)`.
- Count and page queries share exactly the same ancestor deletion and optional status predicates.
- Order globally by `Thread.last_message_at DESC, Thread.id ASC`; paginate after combining conversations.
- An explicit `thread_id` is authoritative and requires canonical access plus `Workspace.can_user_edit`.
- Explicit thread/workspace resolution failure never creates a conversation, thread, job, run, or checkpoint invocation.
- `/execute` maps resolution failure to HTTP 404; `/stream` emits one non-enumerating terminal error before `accepted`; confirmation fails before continuation.
- Threadless requests select the oldest owned live workspace and oldest live conversation, lock the workspace before conversation lookup, and create a conversation only if none exists.
- A user with no workspace retains threadless ephemeral behavior.
- The frontend's default conversation remains only the parent for creating a new thread and never scopes the sidebar.
- List failures surface a retryable error and never create a conversation.

---

### Task 1: Workspace-Wide Thread Query and Route

**Files:**
- Modify: `backend/src/services/threads/thread_service.py`
- Modify: `backend/src/api/threads/workspace_routes/threads.py`
- Modify: `backend/tests/unit/api/test_workspace_route_contract.py`
- Create: `backend/tests/unit/services/threads/test_workspace_thread_listing.py`

**Interfaces:**
- Produces: `list_workspace_threads(db, workspace_id, user_id, *, status_filter=None, limit=20, offset=0) -> Optional[tuple[list[Thread], int, dict[UUID, Optional[str]]]]`.

- [x] **Step 1: Write failing service regressions**

Create two live conversations in one workspace, threads with tied and distinct activity times, deleted thread/conversation/workspace fixtures, and a member user. Assert membership access, global order/tie-break, global pages, status/count parity, bounded preview, and `None` for inaccessible workspace versus `([], 0, {})` for a valid empty workspace. Mock `get_workspace` once and assert both load flags are false.

- [x] **Step 2: Write the failing route contract**

Add this row before the nested conversation-thread routes:

```python
("GET", "/{workspace_id}/threads", ThreadListResponse, 200, False)
```

Update the additive nested and total route counts. Assert a missing/inaccessible workspace returns 404 and a valid empty workspace returns a page with `threads=[]`.

- [x] **Step 3: Run tests and confirm RED**

Run: `TEST_PG_ASYNC_URL=postgresql+asyncpg:///issue1622_verify_20260912 PYTHONPATH=backend /tmp/ci-venv/bin/python -m pytest -q backend/tests/unit/services/threads/test_workspace_thread_listing.py backend/tests/unit/api/test_workspace_route_contract.py`

Expected: the service and route do not exist.

- [x] **Step 4: Implement one shared-predicate query**

```python
workspace = await workspace_access.get_workspace(
    db, workspace_id, user_id,
    load_conversations=False, load_collections=False,
)
if workspace is None:
    return None
conditions = [
    Conversation.workspace_id == workspace_id,
    Conversation.is_deleted.is_(False),
    Thread.is_deleted.is_(False),
]
if status_filter is not None:
    conditions.append(Thread.status == status_filter)
total = await db.scalar(
    select(func.count(Thread.id)).join(Conversation).where(*conditions)
)
rows = (await db.execute(
    select(Thread, last_message_preview_expression())
    .join(Conversation, Thread.conversation_id == Conversation.id)
    .where(*conditions)
    .order_by(Thread.last_message_at.desc(), Thread.id.asc())
    .offset(offset).limit(limit)
)).all()
```

Return thread rows, total, and preview map; log workspace ID/page size/result count only.

- [x] **Step 5: Add the additive route**

Parse `status_filter` exactly like the existing conversation route, calculate offset, map rows through `_thread_to_response`, and return correct `has_more`.

- [x] **Step 6: Run tests and confirm GREEN**

Run the command from Step 3. Expected: all selected tests pass.

- [ ] **Step 7: Commit the endpoint slice**

```bash
git add backend/src/services/threads/thread_service.py backend/src/api/threads/workspace_routes/threads.py backend/tests/unit/api/test_workspace_route_contract.py backend/tests/unit/services/threads/test_workspace_thread_listing.py
git commit -m "feat(chat): list threads across a workspace"
```

### Task 2: Typed Authoritative Agent Thread Resolution

**Files:**
- Modify: `backend/src/services/agent/agent_execution_service.py`
- Modify: `backend/src/services/agent/schemas.py`
- Modify: `backend/src/api/agent/execute.py`
- Modify: `backend/src/api/agent/streaming.py`
- Test: `backend/tests/unit/api/test_agent_resolve_thread.py`
- Test: `backend/tests/unit/api/test_agent_execute_request.py`
- Create: `backend/tests/unit/api/test_agent_thread_resolution_transports.py`

**Interfaces:**
- Produces: `AgentThreadResolutionError(LookupError)`, `PageContextRequest.workspace_id: Optional[UUID]`, and `_resolve_thread(...) -> tuple[Optional[Thread], str]` that raises for explicit failures.

- [x] **Step 1: Write failing resolver tests**

Assert an inaccessible/missing/deleted explicit thread raises without any insert/commit; an accessible public/viewer thread still raises because it is not editable; editor/member and owner threads resolve. Assert an explicit inaccessible workspace raises rather than falling back. Assert no-ID selection chooses `(created_at ASC, id ASC)`, locks the workspace row, reuses `(Conversation.created_at ASC, Conversation.id ASC)`, and creates exactly one conversation only for an empty workspace.

- [x] **Step 2: Write failing transport tests**

For `/execute`, patch job/run writers and assert explicit resolution failure produces 404 before calls. For `/stream`, exhaust the generator and assert exactly one error frame, no accepted frame, and no calls to `accept_submission`, `get_checkpointer`, `compile_agent_graph`, or run creation. For confirmation, seed an existing checkpoint but make durable resolution fail; assert an identical non-enumerating terminal error before claim/graph resume.

- [x] **Step 3: Run tests and confirm RED**

Run: `PYTHONPATH=backend /tmp/ci-venv/bin/python -m pytest -q backend/tests/unit/api/test_agent_resolve_thread.py backend/tests/unit/api/test_agent_execute_request.py backend/tests/unit/api/test_agent_thread_resolution_transports.py`

Expected: explicit misses fall through to creation and transport errors occur too late.

- [x] **Step 4: Add typed schema and error contracts**

```python
class AgentThreadResolutionError(LookupError):
    """A requested durable thread/workspace cannot be used for an agent write."""

class PageContextRequest(BaseModel):
    workspace_id: Optional[UUID] = Field(
        default=None, description="Active chat workspace for durable thread creation"
    )
```

- [x] **Step 5: Implement authoritative resolution**

For explicit `thread_id`, call `workspace_access.get_thread(db, UUID(...), current_user.id, include_messages=False)`, then require `thread.conversation.workspace.can_user_edit`. Any miss or denial raises `AgentThreadResolutionError("Thread not found")`.

For explicit `page_context.workspace_id`, call the lean `get_workspace` form and require edit access, then acquire a row lock on that same validated workspace before conversation lookup. Otherwise select the oldest owned live workspace with member loading and `with_for_update()`. After either explicit or implicit workspace lock, select the oldest non-deleted conversation. Only add/flush `Conversation(title="Agent Chat")` when no row exists, then add the marked thread and commit.

- [x] **Step 6: Map every transport boundary before side effects**

Call `_resolve_thread` before allocating `/execute`'s job ID and map `AgentThreadResolutionError` to HTTP 404. Catch the typed error at the top of `/stream` and emit `error_frame_payload("Thread not found", AgentErrorCategory.INVALID_REQUEST)` before accepted/run/checkpointer logic. In confirmation, resolve with `create_if_missing=False` before claims or `Command(resume=...)` and use the same non-enumerating frame.

Do not let `_run_agent_graph` swallow the typed error: requests dispatched after edge validation should treat a later access loss as a failed run, not execute ephemerally.

- [x] **Step 7: Run tests and confirm GREEN**

Run the command from Step 3. Expected: all selected tests pass.

- [ ] **Step 8: Commit the resolution slice**

```bash
git add backend/src/services/agent/agent_execution_service.py backend/src/services/agent/schemas.py backend/src/api/agent/execute.py backend/src/api/agent/streaming.py backend/tests/unit/api/test_agent_resolve_thread.py backend/tests/unit/api/test_agent_execute_request.py backend/tests/unit/api/test_agent_thread_resolution_transports.py
git commit -m "fix(agent): make thread resolution authoritative"
```

### Task 3: Workspace-Scoped Sidebar Initialization and Pagination

**Files:**
- Modify: `frontend/src/services/workspaceService.ts`
- Modify: `frontend/src/hooks/chat/useChatSession.ts`
- Modify: `frontend/src/hooks/chat/useChatStreaming.ts`
- Modify: `frontend/src/types/api/workspace-contract.ts`
- Test: `frontend/src/services/__tests__/workspaceService.contract.test.ts`
- Create: `frontend/src/hooks/chat/__tests__/useChatSession.workspaceThreads.test.tsx`

**Interfaces:**
- Produces: `workspaceService.listWorkspaceThreads(workspaceId, {page, limit, statusFilter?}) -> Promise<ApiThreadList>` and sidebar mapping that reads each row's `conversation_id`.

- [x] **Step 1: Write failing service and hook tests**

Assert the service requests `/api/v2/workspaces/ws-1/threads?page=1&limit=50`. Exercise cold load with two conversation IDs, warm restore with a stale cached conversation ID, pagination overlap/upsert, workspace switch with a late old response, deep link outside page one, list error, and successful new-thread insertion. Assert no list error calls `createConversation`; assert the new row uses its returned `conversation_id`.

- [x] **Step 2: Run tests and confirm RED**

Run: `pnpm --dir frontend test -- --run src/services/__tests__/workspaceService.contract.test.ts src/hooks/chat/__tests__/useChatSession.workspaceThreads.test.tsx`

Expected: all sidebar reads still call conversation-scoped `listThreads`.

- [x] **Step 3: Add the generated-contract-backed service method**

```typescript
async listWorkspaceThreads(
  workspaceId: string,
  options: { page?: number; limit?: number; statusFilter?: string } = {}
): Promise<ApiThreadList> {
  const params = new URLSearchParams();
  if (options.page) params.set('page', String(options.page));
  if (options.limit) params.set('limit', String(options.limit));
  if (options.statusFilter) params.set('status_filter', options.statusFilter);
  const query = params.toString();
  return api.get<ApiThreadList>(
    `${API_PREFIX}/workspaces/${workspaceId}/threads${query ? `?${query}` : ''}`
  );
}
```

- [x] **Step 4: Make workspace pages the sidebar source**

Change `threadToConversation(thread)` to use `thread.conversation_id`. Track workspace ID and request generation, not conversation ID. Cold/warm first-page success replaces in server order; pagination upserts by thread ID while preserving page order and existing selected/deep-linked entries. Ignore any response whose captured workspace/generation no longer matches current state.

Keep `getOrCreateDefaultConversation(ws.id)` only for `dbConversation` used by new-thread creation. Delete empty-conversation fallback and every create-on-list-error branch; set `initError`/toast for retry instead.

- [x] **Step 5: Send workspace ID on agent turns and insert new threads immediately**

Add `workspace_id: workspace.id` to `page_context`. On `createThread` success, index and upsert the returned thread using its real `conversation_id` before streaming starts.

- [x] **Step 6: Mutation-verify request-identity and page-overlap guards**

Temporarily neutralize the workspace generation check and verify the stale-workspace test fails. Restore exactly and rerun. Temporarily remove ID upsert and verify the page-overlap test fails. Restore exactly and record guard location/commands in the test file.

- [x] **Step 7: Run tests and confirm GREEN**

Run the command from Step 2. Expected: all selected tests pass.

- [ ] **Step 8: Commit the frontend slice**

```bash
git add frontend/src/services/workspaceService.ts frontend/src/hooks/chat/useChatSession.ts frontend/src/hooks/chat/useChatStreaming.ts frontend/src/types/api/workspace-contract.ts frontend/src/services/__tests__/workspaceService.contract.test.ts frontend/src/hooks/chat/__tests__/useChatSession.workspaceThreads.test.tsx
git commit -m "fix(chat): load sidebar threads by workspace"
```

### Task 4: Contracts and Verification

**Files:**
- Modify: `backend/openapi.json`
- Modify: `frontend/src/types/generated/api.d.ts`

- [x] **Step 1: Regenerate both contract artifacts**

```bash
PYTHONPATH=backend /tmp/ci-venv/bin/python scripts/ci/generate_openapi.py
pnpm --dir frontend generate:api-types
```

- [x] **Step 2: Run backend checks**

```bash
TEST_PG_ASYNC_URL=postgresql+asyncpg:///issue1622_verify_20260912 PYTHONPATH=backend /tmp/ci-venv/bin/python -m pytest -q backend/tests/unit/services/threads/test_workspace_thread_listing.py backend/tests/unit/api/test_workspace_route_contract.py backend/tests/unit/api/test_agent_resolve_thread.py backend/tests/unit/api/test_agent_execute_request.py backend/tests/unit/api/test_agent_thread_resolution_transports.py backend/tests/unit/architecture
PYTHONPATH=backend /tmp/ci-venv/bin/python scripts/ci/generate_openapi.py --check
ruff check backend/src
```

- [x] **Step 3: Run frontend checks**

```bash
pnpm --dir frontend test -- --run src/services/__tests__/workspaceService.contract.test.ts src/hooks/chat/__tests__/useChatSession.workspaceThreads.test.tsx
# Raw ESLint reports the existing full-tree debt (exit 1); the ratchet below passes.
pnpm --dir frontend exec eslint app src --format json --output-file /tmp/issue1622-eslint.9G4WYx.json
node scripts/ci/check_frontend_quality.mjs --report /tmp/issue1622-eslint.9G4WYx.json --base origin/develop
pnpm --dir frontend quality:exclusions
pnpm --dir frontend type-check
sha256sum backend/openapi.json frontend/src/types/generated/api.d.ts
PYTHONPATH=backend /tmp/ci-venv/bin/python scripts/ci/generate_openapi.py
pnpm --dir frontend generate:api-types
sha256sum backend/openapi.json frontend/src/types/generated/api.d.ts  # hashes unchanged
```

`check:api-types` is intentionally omitted because it asserts a clean generated-artifact diff and this implementation remains uncommitted. Generator idempotence was verified directly with matching hashes before and after regeneration.

- [ ] **Step 4: Commit generated contracts**

```bash
git add backend/openapi.json frontend/src/types/generated/api.d.ts
git commit -m "chore(api): generate workspace thread contracts"
```

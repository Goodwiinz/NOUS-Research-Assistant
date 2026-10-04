# /chat Bug Hunt Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the 16 verified findings from the 2026-08-23 /chat bug hunt (`docs/audits/chat/chat-bug-hunt-2026-08-23.md`), highest severity first, without regressing existing behavior.

**Architecture:** Backend security/data-integrity fixes land in the chat API + LLM cache service (FastAPI/SQLAlchemy, pytest). Frontend fixes land in the composer/runtime composition layer (React hooks, vitest). Each task is independently shippable; tasks are ordered so security issues (B1–B3) merge first. Ledger IDs are referenced throughout — update `~/.audit-ledgers/RAG_system/chat-bug-hunt.md` status per finding as it merges.

**Tech Stack:** FastAPI + SQLAlchemy (sync sessions in these routes), pytest backend; React 18 hooks + zustand, vitest frontend.

**Branch:** `fix/chat-bug-hunt-2026-08-23`, cut from `develop`.

---

### Task 1: B1 — Scope LLM response cache per organization

**Files:**
- Modify: `backend/src/services/infrastructure/llm_response_cache.py:233-250` (`_generate_query_hash`), `get()` (:322), `set()` (:454)
- Modify: `backend/src/api/research/chat.py:430,508` (call sites)
- Test: `backend/tests/services/infrastructure/test_llm_response_cache.py` (create if missing)

**Step 1: Write the failing test**

```python
import asyncio

def test_cache_key_differs_per_organization():
    from src.services.infrastructure.llm_response_cache import LLMResponseCache

    cache = LLMResponseCache()
    h_org_a = cache._generate_query_hash("hello", "gpt-4o", 0.7, organization_id="org-a")
    h_org_b = cache._generate_query_hash("hello", "gpt-4o", 0.7, organization_id="org-b")
    h_no_org = cache._generate_query_hash("hello", "gpt-4o", 0.7)
    assert h_org_a != h_org_b
    assert h_org_a != h_no_org


def test_get_does_not_return_entry_set_by_other_organization():
    from src.services.infrastructure.llm_response_cache import LLMResponseCache

    async def scenario():
        cache = LLMResponseCache()
        await cache.set(
            query="what is rag",
            response_content="org a answer",
            model="gpt-4o",
            temperature=0.7,
            organization_id="org-a",
        )
        hit = await cache.get(
            query="what is rag",
            model="gpt-4o",
            temperature=0.7,
            use_semantic=False,
            organization_id="org-b",
        )
        assert hit is None
        own = await cache.get(
            query="what is rag",
            model="gpt-4o",
            temperature=0.7,
            use_semantic=False,
            organization_id="org-a",
        )
        assert own is not None and own["response_content"] == "org a answer"

    asyncio.run(scenario())
```

**Step 2: Run test to verify it fails**

Run: `pytest backend/tests/services/infrastructure/test_llm_response_cache.py -v`
Expected: FAIL — `_generate_query_hash() got an unexpected keyword argument 'organization_id'`

**Step 3: Write minimal implementation**

In `_generate_query_hash`, add parameter and component:

```python
def _generate_query_hash(
    self, query: str, model: str, temperature: float,
    organization_id: str = "",
) -> str:
    normalized = query.lower().strip()
    components = {
        "query": normalized,
        "model": model,
        "temperature": round(temperature, 2),
        # Tenant boundary: identical queries in different orgs must never
        # share an entry (2026-08-23 audit B1).
        "organization_id": organization_id,
    }
    hash_input = json.dumps(components, sort_keys=True)
    return hashlib.sha256(hash_input.encode()).hexdigest()[:32]
```

Add `organization_id: str = ""` parameter to both `get()` and `set()`; thread it
into their `_generate_query_hash(...)` calls.

In `backend/src/api/research/chat.py`, pass it at both call sites:

```python
cached_response = await llm_response_cache.get(
    query=cache_query,
    model=request.model,
    temperature=request.temperature,
    use_semantic=not request.use_rag,
    organization_id=str(current_user.organization_id or ""),
)
```
and identically on `llm_response_cache.set(...)`.

Note: this naturally invalidates all existing cached entries (new key shape) — acceptable; no migration needed for a cache.

**Step 4: Run test to verify it passes**

Run: `pytest backend/tests/services/infrastructure/test_llm_response_cache.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/src/services/infrastructure/llm_response_cache.py backend/src/api/research/chat.py backend/tests/services/infrastructure/test_llm_response_cache.py
git commit -m "fix(chat): scope LLM response cache keys by organization (B1)"
```

---

### Task 2: B2 — Repair semantic-cache embedding call

**Files:**
- Modify: `backend/src/services/infrastructure/llm_response_cache.py:253-270` (`_compute_embedding`)
- Test: same file as Task 1

**Step 1: Write the failing test**

```python
def test_compute_embedding_uses_generate_embedding():
    from unittest.mock import AsyncMock, MagicMock, patch

    from src.services.infrastructure.llm_response_cache import LLMResponseCache

    async def scenario():
        cache = LLMResponseCache()
        fake_service = MagicMock()
        fake_service.generate_embedding = AsyncMock(
            return_value=MagicMock(embedding=[0.1, 0.2])
        )
        with patch.object(
            cache, "_get_embedding_service", AsyncMock(return_value=fake_service)
        ):
            vec = await cache._compute_embedding("hello")
        assert vec == [0.1, 0.2]
        fake_service.generate_embedding.assert_awaited_once()

    asyncio.run(scenario())
```

**Step 2: Run test to verify it fails**

Run: `pytest backend/tests/services/infrastructure/test_llm_response_cache.py -v`
Expected: FAIL — mock never awaited (current code calls nonexistent `.embed()`)

**Step 3: Write minimal implementation**

```python
try:
    from src.models.vector import EmbeddingRequest
    embedding = await embedding_service.generate_embedding(
        EmbeddingRequest(text=text)
    )
    return list(embedding.embedding)
except Exception as e:
    logger.warning(f"Failed to compute embedding: {e}")
    return None
```

(Replace the body of the existing `try` block that called `.embed(text)`.)

**Step 4: Run test to verify it passes**

Run: `pytest backend/tests/services/infrastructure/test_llm_response_cache.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/src/services/infrastructure/llm_response_cache.py backend/tests/services/infrastructure/test_llm_response_cache.py
git commit -m "fix(cache): call existing generate_embedding API for semantic tier (B2)"
```

---

### Task 3: B3 — Require auth on /chat/health and /chat/models

**Files:**
- Modify: `backend/src/api/research/chat.py:556-559,571-574`
- Test: `backend/tests/api/research/test_chat_auth.py`

**Step 1: Write the failing test**

```python
def test_health_and_models_require_auth(client):
    assert client.get("/api/v1/chat/health").status_code in (401, 403)
    assert client.get("/api/v1/chat/models").status_code in (401, 403)
```

(Follow the auth fixtures used by neighboring tests in `backend/tests/api/`; if a
client fixture doesn't exist yet, mirror the setup of the closest existing API test.)

**Step 2: Run test to verify it fails**

Run: `pytest backend/tests/api/research/test_chat_auth.py -v`
Expected: FAIL — endpoints currently return 200 unauthenticated

**Step 3: Write minimal implementation**

Add the dependency to both handlers (import already present at chat.py:16):

```python
@router.get("/health")
async def chat_health_check(current_user: User = Depends(get_current_user)):
```

```python
@router.get("/models")
async def list_available_models(current_user: User = Depends(get_current_user)):
```

Check `frontend/src/services/` callers of `/chat/health` and `/chat/models`: they
must already attach the auth token (all other authenticated calls do via the
shared axios instance — verify, don't assume).

**Step 4: Run test to verify it passes**

Run: `pytest backend/tests/api/research/test_chat_auth.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/src/api/research/chat.py backend/tests/api/research/test_chat_auth.py
git commit -m "fix(chat): require authentication on health and models endpoints (B3)"
```

---

### Task 4: B4 — Move background RAG eval off the event loop

**Files:**
- Modify: `backend/src/api/research/chat.py:246-295` (`_background_evaluate_rag`)

**Step 1: Write the failing test**

Assert the evaluation body runs in a worker thread: patch
`rag_evaluation_service.run_rag_triad_evaluation` with a function recording
`threading.get_ident()`, call `_background_evaluate_rag(...)`, and assert the
recorded thread id differs from the caller's.

**Step 2: Run test to verify it fails**

Run: `pytest backend/tests/api/research/ -k background_eval -v`
Expected: FAIL — same thread id (runs inline on the loop)

**Step 3: Write minimal implementation**

Wrap the sync work:

```python
await asyncio.to_thread(_run_evaluation_sync, ...)
```

Extract the current sync body (session creation through commit/close) into
`_run_evaluation_sync`, leaving `_background_evaluate_rag` as the thin async
entry BackgroundTasks awaits. This mirrors the ThreadSummarizationService worker-thread pattern mandated in AGENTS.md.

**Step 4: Run test to verify it passes**

Run: `pytest backend/tests/api/research/ -k background_eval -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/src/api/research/chat.py backend/tests/api/research/
git commit -m "fix(chat): run background RAG triad eval in worker thread (B4)"
```

---

### Task 5: B5 — Lock thread row during unlink

**Files:**
- Modify: `backend/src/api/research/project_chat.py:497-550`
- Test: `backend/tests/api/research/test_project_chat_unlink.py`

**Step 1: Write the failing test**

Integration-style (or SQLite-with-threads best-effort): issue two concurrent
unlink transactions against one thread with two linked projects; assert final
`rag_document_scope.document_ids` equals exactly one project's docs and no stale
ids remain. With plain SELECT both writers read the same baseline and one write is lost.

If true concurrency is impractical in the test harness, instead assert the ORM
query emits `FOR UPDATE` (inspect compiled SQL for the select inside unlink).

**Step 2: Run test to verify it fails**

Run: `pytest backend/tests/api/research/test_project_chat_unlink.py -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Inside the unlink transaction, replace the plain thread fetch:

```python
thread = (
    db.query(ProjectThread)
    .filter(ProjectThread.id == thread_id)
    .with_for_update()
    .first()
)
```

Serialize concurrent unlinks on the row lock; recompute `remaining_link` after acquisition.

**Step 4: Run test to verify it passes**

Run: `pytest backend/tests/api/research/test_project_chat_unlink.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/src/api/research/project_chat.py backend/tests/api/research/test_project_chat_unlink.py
git commit -m "fix(project-chat): take FOR UPDATE lock on thread unlink (B5)"
```

---

### Task 6: Backend low batch — B6, B7, B8, B9, B10

**Files:**
- Modify: `backend/src/api/research/chat.py:421` (B6), `:241-243` (B10), `:726-728` (B9)
- Modify: `backend/src/api/research/project_chat.py:476-481` (B7), `:378-436` (B8)
- Test: extend the two backend test files from Tasks 1–5

**Step 1: Write failing tests**

- B6: retrieved context with `document_id=None` → cache-set path must not raise TypeError.
- B7: DELETE twice on same project_thread → second returns 404, no state mutation.
- B8: `list_project_threads` applies limit/offset in SQL and excludes soft-deleted threads in the query itself.
- B9: patched suggestion LLM raising → endpoint returns 503 (not 200-empty).
- B10: `retrieve_context` raising → response flags degraded retrieval (`retrieval_error=true`) and skips caching.

**Step 2: Run tests to verify they fail**

Run: `pytest backend/tests/api/research/ -v`
Expected: new tests FAIL

**Step 3: Implement**

```python
# B6 (chat.py:421) — None-safe id join
context_ids = "|".join(
    sorted(str(c.document_id) for c in retrieved_contexts if c.document_id)
)
```

```python
# B7 (project_chat.py) — add to the WHERE
.filter(ProjectThread.is_deleted == False)  # noqa: E712
```

```python
# B8 (project_chat.py) — push filters into SQL + paginate
query = (
    db.query(ProjectThread)
    .join(Thread, ProjectThread.thread_id == Thread.id)
    .filter(Thread.is_deleted == False)  # noqa: E712
)
total = query.count()
rows = query.order_by(ProjectThread.created_at.desc()).offset(offset).limit(limit).all()
```

```python
# B9 (chat.py) — narrow the catch
except HTTPException:
    raise
except Exception:
    logger.exception("Suggestions generation failed")
    raise HTTPException(status_code=503, detail="Suggestions unavailable")
```

```python
# B10 (chat.py retrieve_context) — distinguish failure from empty
try:
    contexts = ...
except Exception:
    logger.exception("RAG retrieval failed")
    raise RetrievalError(...)  # caller marks rag_enabled=False + retrieval_error=True and skips llm_response_cache.set
```

Keep the response schema additive (`retrieval_error: bool = false`) so the
frontend ignores unknown fields until wired.

**Step 4: Run tests to verify they pass**

Run: `pytest backend/tests/api/research/ -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/src/api/research/
git commit -m "fix(chat): harden low-severity backend findings B6-B10"
```

---

### Task 7: F1 — IME-safe Enter in the main composer

**Files:**
- Modify: `frontend/src/components/chat/ChatInput.tsx:287` (`handleKeyDown`)
- Test: `frontend/src/components/chat/__tests__/ChatInput.ime.test.tsx`

Reference implementation already in repo: `AuiMessage.tsx:343-349` (+ its test — copy its structure).

**Step 1: Write the failing test**

Render ChatInput with a mocked `onSubmit`; fire keyDown Enter on the textarea with
`nativeEvent: { isComposing: true }`. Assert `onSubmit` NOT called and default
not prevented. Repeat with slash-menu open (mock the menu hook open state) —
assert command NOT run while composing.

**Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/chat/__tests__/ChatInput.ime.test.tsx`
Expected: FAIL

**Step 3: Write minimal implementation**

First line of `handleKeyDown`:

```tsx
// IME composition (ja/zh/ko): confirm-Enter must commit text, not submit.
// Same guard as AuiMessage.tsx.
if (e.nativeEvent.isComposing) return;
```

**Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/components/chat/__tests__/ChatInput.ime.test.tsx`
Expected: PASS

**Step 5: Commit**

```bash
git add frontend/src/components/chat/ChatInput.tsx frontend/src/components/chat/__tests__/ChatInput.ime.test.tsx
git commit -m "fix(chat): guard composer Enter during IME composition (F1)"
```

---

### Task 8: F2 — Stop remounting the runtime on first send

**Files:**
- Modify: `frontend/src/components/chat/ChatSurface.tsx:189,306`
- Test: `frontend/src/components/chat/__tests__/ChatSurface.key.test.tsx`

**Step 1: Write the failing test**

Render ChatSurface with zero messages (new chat), then rerender with one message.
Capture the ChatRuntimeProvider instance type identity across renders via a probe
child (`componentDidMount` counter or ref-stable marker). Assert the subtree did
NOT remount when only messages went 0→1 with unchanged thread id.

**Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/chat/__tests__/ChatSurface.key.test.tsx`
Expected: FAIL — key flip forces remount

**Step 3: Write minimal implementation**

Key must encode thread identity ONLY. If a runtime reset on genuine thread change
is still required beyond what React remount provides, reset via a prop
(`resetSignal={activeThreadId}` consumed by ChatRuntimeProvider's effect), not the
key:

```tsx
<ChatRuntimeProvider
  key={activeThreadId ?? 'new'}
  ...
>
```

Verify manually after: `/chat` → new chat → send first message → composer keeps focus, typed draft intact.

**Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/components/chat/__tests__/ChatSurface.key.test.tsx && cd .. `
Then full suite: `cd frontend && npm run validate`
Expected: PASS

**Step 5: Commit**

```bash
git add frontend/src/components/chat/ChatSurface.tsx frontend/src/components/chat/__tests__/ChatSurface.key.test.tsx
git commit -m "fix(chat): stop runtime remount on first send in new chat (F2)"
```

---

### Task 9: Frontend low batch — F3, F4, F5

**Files:**
- Modify: `frontend/src/components/chat/ChatDialogs.tsx:67` (F3)
- Modify: `frontend/src/hooks/chat/useChatStreaming.ts:1739-1750` (F4)
- Modify: `frontend/src/hooks/chat/useProcessingJobs.ts:101-119` (F5)
- Test: colocated `__tests__` files following existing hook-test patterns (`frontend/src/store/__tests__/` style)

**Step 1: Write failing tests**

- F3: rename input keyDown Enter with `isComposing: true` → `commitRename` not called; non-composing Enter calls `preventDefault` + commits.
- F4: cold-probe confirmation path builds PendingConfirmation carrying stable ids — pass `assistantRuntimeId`/`userRuntimeId` derived like the live path (uuidv5 of the interrupt's client_message_id when available); assert fields present on the armed confirmation.
- F5: (a) overlapping refresh + tick → last-resolved-wins replaced by generation check: resolve tick AFTER newer refresh → jobs state reflects the refresh result, not the stale tick; (b) failed load sets fast-poll off → next scheduled poll ≥30s later (assert timer registration).

**Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run src/components/chat/__tests__/ChatDialogs src/hooks/chat/__tests__`
Expected: new tests FAIL

**Step 3: Implement**

```tsx
// F3 — ChatDialogs rename onKeyDown
if (e.nativeEvent.isComposing) return;
if (e.key === 'Enter') { e.preventDefault(); commitRename(); }
```

```ts
// F5 — useProcessingJobs
const loadGenerationRef = useRef(0);
const refresh = useCallback(async () => {
  const gen = ++loadGenerationRef.current;
  try {
    const data = await api.fetchJobs();
    if (gen !== loadGenerationRef.current) return; // superseded
    setJobs(data);
    pollFastRef.current = hasActiveQueue(data);
  } catch {
    if (gen !== loadGenerationRef.current) return;
    pollFastRef.current = false; // back off during outage
  }
}, []);
```

F4: in the probe effect's `onConfirmation` (useChatStreaming.ts:1739), include
`assistantRuntimeId: uuidv5(`nous-assistant:${/* interrupt cmid */ ''}`, uuidv5.URL)`
only when the replay payload carries a client_message_id — otherwise leave absent
but document why (no deterministic server contract exists pre-done). The real
improvement: forward `userRuntimeId` when the payload includes it, so
reconcileConfirmationUser targets the persisted user row instead of skipping.

**Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/components/chat/__tests__ src/hooks/chat/__tests__`
Expected: PASS

**Step 5: Commit**

```bash
git add frontend/src/components/chat/ChatDialogs.tsx frontend/src/hooks/chat/useChatStreaming.ts frontend/src/hooks/chat/useProcessingJobs.ts frontend/src/**/__tests__
git commit -m "fix(chat): IME-safe rename, probe identity passthrough, job-poll backoff (F3-F5)"
```

---

### Task 10: F6 — Throttle sidebar preview writes during streaming (optional perf)

**Files:**
- Modify: `frontend/src/hooks/chat/useChatSession.ts:279-304`

**Step 1: Write failing test** — simulate 100 store updates in one frame; assert
`setConversations` reducer ran ≤ once per animation frame (wrap with rAF batching
like useChatStreaming's token throttle).

**Step 2:** verify fail · **Step 3:** batch the effect body through
`requestAnimationFrame` with a pending-preview ref (same pattern as
`streamingRafRef`) · **Step 4:** verify pass · **Step 5:**

```bash
git add frontend/src/hooks/chat/useChatSession.ts
git commit -m "perf(chat): rAF-batch sidebar preview writes while streaming (F6)"
```

Skip if Task 8's remount fix already removed the visible jank — record `wontfix (perf-unnecessary)` in the ledger instead.

---

## Verification & handoff

After each task: run the task's scoped tests. After all tasks:

```bash
pytest backend/tests/ --cov=src          # backend green
cd frontend && npm run validate          # lint + type-check + tests green
```

Update ledger `~/.audit-ledgers/RAG_system/chat-bug-hunt.md`: mark each ID
`pr` on branch open, `merged` after squash-merge to `develop`. PRs target
`develop`; keep each task's commit separate so review maps 1:1 to ledger IDs.

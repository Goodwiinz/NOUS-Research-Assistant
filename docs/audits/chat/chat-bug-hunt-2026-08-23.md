# /chat Bug Hunt — 2026-08-23

Full-surface bug hunt of the `/chat` route: frontend composition root
(`frontend/app/(dashboard)/chat/page.tsx`), all hooks under
`frontend/src/hooks/chat/`, `ChatSurface`, and the backend endpoints that back it
(`backend/src/api/research/chat.py`, `project_chat.py`). ~6.4k lines read.

Live tracking ledger: `~/.audit-ledgers/rag/chat-bug-hunt-2026-08-23.md`.
Fix plan: `docs/plans/2026-08-23-chat-bug-hunt-fixes.md`.

## Findings

### High

**B1 — Cross-tenant LLM cache leak** (`llm_response_cache.py:233-250`, `chat.py:415-422`)
Cache key = sha256(query + model + temperature). No `organization_id`/`user_id`
component; Redis prefix is global. Tenant A's cached answer — including stored
`retrieved_contexts` — is served to tenant B on an identical query. Semantic mode
scans all tenants' entries (`use_semantic=not use_rag`, chat.py:434).
*Fix:* mix org id into `_generate_query_hash`; pass `organization_id` from both
call sites.

**F1 — Composer IME Enter submits composition buffer** (`frontend/src/components/chat/ChatInput.tsx:287-333`)
`handleKeyDown` has no `e.nativeEvent.isComposing` guard. Japanese/Chinese/Korean
IME confirm-Enter submits the raw composition buffer, and with the slash menu open
it runs the highlighted command instead of committing text. The identical bug was
already fixed in `AuiMessage.tsx:349` (with test).
*Fix:* early-return when composing; mirror AuiMessage's guard + test.

**F2 — Runtime remount on first send in a new chat** (`ChatSurface.tsx:189,306`)
`key={`${activeThreadId ?? 'new'}:${runtimeHydrationPhase}`}` where phase flips
`'empty'→'hydrated'` the moment the first optimistic message lands. Full remount of
the runtime subtree (composer focus dropped to `<body>`, internal state reset)
mid-turn on every new-chat first send.
*Fix:* key on thread identity only; drive runtime reset through an explicit signal,
not a length-derived key.

### Medium

**B2 — Semantic cache can never hit** (`llm_response_cache.py:263`)
Calls `embedding_service.embed(text)`; `EmbeddingService` has no `embed()` method
(only async `generate_embedding(request) -> EmbeddingResponse`). AttributeError is
swallowed per call → semantic cache dead + warning log spam on every lookup.
*Fix:* `await embedding_service.generate_embedding(EmbeddingRequest(text=text))`,
return `.embedding`.

**B3 — Unauthenticated info disclosure** (`chat.py:556,571`)
`GET /api/v1/chat/health` and `/chat/models` have no auth dependency; they disclose
Azure deployment names and service availability.
*Fix:* add `Depends(get_current_user)` to both.

**B4 — Background RAG eval blocks the event loop** (`chat.py:270-295`)
`_background_evaluate_rag` runs as an async BackgroundTask yet uses sync
`next(get_db_sync())` with sync ORM writes inside `rag_evaluation_service`.
*Fix:* run via `asyncio.to_thread` (or Celery), matching the
ThreadSummarizationService pattern in AGENTS.md.

**B5 — Thread unlink race** (`project_chat.py:497-550`)
Read-modify-write of `thread.source_project_id` / `rag_document_scope` with plain
SELECT. Two concurrent unlinks both compute `remaining_link`; last commit wins,
leaving stale `document_ids` feeding RAG scope.
*Fix:* `SELECT … WITH FOR UPDATE` on the thread row inside the transaction.

### Low

| ID | Where | Bug |
|----|-------|-----|
| B6 | `chat.py:421` | `sorted([c.document_id …])` over Optional ids → TypeError → 500 after successful retrieval when any context lacks an id |
| B7 | `project_chat.py:476-481` | Double DELETE re-soft-deletes a dead row; missing `is_deleted == False` filter |
| B8 | `project_chat.py:378-436` | `list_project_threads` unbounded; loads every link row, filters deleted in Python |
| B9 | `chat.py:726-728` | Suggestions endpoint swallows all exceptions → 200 `{suggestions: []}` masks outages |
| B10 | `chat.py:241-243` | Retrieval failure reported as `rag_enabled=True` with zero contexts; context-free answer cached under the RAG key shape |
| F3 | `ChatDialogs.tsx:67` | Rename input Enter lacks `isComposing` guard + `preventDefault` → premature rename during IME composition |
| F4 | `useChatStreaming.ts:1739-1750` | Cold-probe `PendingConfirmation` omits `assistantRuntimeId`/`userRuntimeId`; reconcile identity weaker than live path (self-heals via freshness gate) |
| F5 | `useProcessingJobs.ts:101-119` | Manual refresh and scheduled tick can overlap (no request generation); catch branch never backs off fast-poll → 4s polling forever during an outage |
| F6 | `useChatSession.ts:279-304` | Sidebar-preview effect rewrites the `conversations` array per streaming token batch → sidebar re-renders per token |

## Verified clean

- SQL injection surface: parameterized queries throughout; sort/filter enums validated.
- Count-drift: none — totals computed from the same filtered set.
- Org scoping on project/thread access helpers correct (`Workspace.owner_id` joins).
- `attach_thread_to_project` race-safe (pg upsert).
- Frontend: useChatDrawer, useCitationPanel, useChatThreadActions,
  useChatComposerActions, useSlashCommands — no demonstrable bugs.
- Stream-owner claim protocol in useChatStreaming correctly prevents cross-turn
  teardown races (main stream vs confirm stream vs resume).

## Method

Read-only sweep; every finding re-verified against source before recording
(B1/B2/B3/F1/F2 re-read directly by main session). Backend surface swept by
parallel investigator with repo tenant-scope rules; frontend component/hook
surface likewise. No fixes applied in this round.

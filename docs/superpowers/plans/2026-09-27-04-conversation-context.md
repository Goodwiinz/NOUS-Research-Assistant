# Conversation Context and MCP Recall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every NOUS chat a maintained, source-linked context document and let external harnesses selectively search/read conversations the user explicitly authorizes.

**Architecture:** Persist one canonical context record per `Thread`, immutable generated revisions, and independently versioned owner corrections/pins. Derive context from normal persisted `ChatMessage` rows and invalidate it transactionally when those messages change. A separate thread-scoped grant controls external recall; native context generation never implies permission to export conversations.

**Tech Stack:** Existing PostgreSQL/Alembic, SQLAlchemy/FastAPI/Pydantic, Celery, configured NOUS model factory, Next.js/TanStack Query, Node 24 MCP package with `node:test`.

**Spec:** [Architecture and user-requested conversation-context scope](../../plans/2026-09-27-harness-bridge-and-artifacts.md); [implementation index](2026-09-27-00-harness-workspace.md). Consumes [Plan 01](2026-09-27-01-local-codex-bridge.md) grants and [Plan 02](2026-09-27-02-nous-capabilities.md) MCP transport. Only updating this plan is authorized now.

## Global Constraints

- Include project and non-project chats; NOUS's user-facing conversation maps to `Thread`, not its parent `Conversation` grouping.
- Generate from persisted messages, never LangSmith traces, hidden prompts/reasoning, or closed run-event logs.
- External corpus is an explicit selected thread set; never automatically load every conversation.
- Preserve Plan 01's `IntegrationContext.project_id: UUID`; thread grants use a separate context/resolver.
- External exports require same-organization owner/member access and current ancestor checks; public visibility alone grants no external export.
- Corrections/pins are browser-only, owner-authorized, source-labeled, and preserved across regeneration.
- Retain Node 24, pnpm 10.18.2 and existing pinned Python gates; this change is a plan, not implemented behavior.

## Review Focus

- Edit/delete/supersession after generation must suppress invalidated text and source links, including historical revisions (Tasks 1–2).
- A large conversation processed in bounded pages must remain partially covered until every eligible message is accounted for (Task 2).
- Cross-org public visibility, revoked membership, or an omitted selection must never broaden MCP recall (Task 3).
- Regeneration and concurrent owner corrections must preserve pins and reject stale overwrite (Task 4).
- Another account/thread response arriving late must not populate the active context UI or MCP result (Task 5).

---

**Record:** Proposed extension dated 2026-09-27; source inspected at `16f824197f85436c84558769e72936e3c1decf2b`. New paths are proposed. No application code or acceptance run accompanies this document.

## Verified source map and file decomposition

`Thread.source_project_id` is nullable in `backend/src/models/thread.py`. Existing `Thread.summary` generation in `backend/src/services/threads/thread_summarization_service.py` produces at most 150 characters from a 2,000-character/recent-40-message window; retain it for sidebar labels. `workspace_access.get_thread` checks deleted ancestors, while `get_message` intentionally permits superseded rows for existing edit workflows. External reads need an additional visible-message predicate. `thread_message_search_service.py` includes public workspace access; do not forward its unrestricted result set to MCP.

Messages arrive through `ChatService.create_message`, agent submission, and agent-execution upserts; edit-and-resend tombstones messages in `agent_execution_service.py`. Database revision triggers cover these paths without a second message writer.

| Responsibility | Proposed focused files |
| --- | --- |
| Canonical documents, revisions, source manifests, annotations | `backend/src/models/conversation_context.py`, `backend/src/schemas/conversation_context.py` |
| Snapshot/refresh/read/annotation operations | `backend/src/services/conversation_context/{repository,refresh,access,read,annotations}.py` |
| Bounded worker | `backend/src/tasks/conversation_context_tasks.py` |
| Native API and integration grant boundary | `backend/src/api/threads/conversation_context.py`, `backend/src/api/integrations/conversations.py`, `backend/src/services/integrations/conversation_context.py` |
| Browser document and source preview | `frontend/src/components/chat/context/{ConversationContextPanel,ContextSourcePreview}.tsx`, `frontend/src/hooks/chat/useConversationContext.ts`, `frontend/src/services/conversationContextService.ts` |
| MCP recall | `packages/harness-bridge/src/mcp/conversations.ts` |

## Task 1: Persist context identity and atomic source revisions

**Files:** Create model/schema/repository above, `backend/alembic/versions/cc01_conversation_context.py`, and `backend/tests/integration/test_conversation_context_revisions.py`; modify `backend/src/models/__init__.py` exports. Read current `chat_message.py`, `thread.py`, and `workspace_access.py`; do not change their established access semantics.

**Interfaces:** Produce `async ensure_document(db: AsyncSession, thread_id: UUID) -> ContextDocument`; model has unique `thread_id`, `owner_id`, `source_revision:int`, `annotation_revision:int`, `invalidation_epoch:int`, `current_revision_id:UUID|None`, `invalidated:bool`, and refresh lease/retry fields. `ContextRevision` stores immutable generated blocks, watermarks and its invalidation epoch; `ContextRevisionSource` stores exact `(revision_id,message_id,content_sha256)` manifests; `ContextAnnotation` stores owner input; `ContextCoverageChunk` stores resumable coverage. Owner is thread creator, falling back to workspace owner; later corrections still require current access.

- [ ] **Write failing tests.** PostgreSQL fixtures seed project/non-project threads and visible user/assistant messages. `test_trigger_covers_every_writer` inserts via SQL and updates content; `test_feedback_does_not_invalidate` changes only feedback; `test_delete_hides_prior_revision` tombstones a cited message.

```python
assert plain_thread.source_project_id is None
assert document_count(plain_thread.id) == 1
assert after_content_edit.source_revision == before.source_revision + 1
assert after_content_edit.invalidated is True
assert after_feedback.source_revision == before_feedback.source_revision
```

`document_count(thread_id: UUID) -> int` is a test-local SQL count using the fixture database.

- [ ] **Red:** `pytest -q backend/tests/integration/test_conversation_context_revisions.py`; expect missing model/trigger.
- [ ] **Implement persistence.** Thread-insert trigger creates its document; message insert/content/role/stopped/delete/supersession changes increment source revision in the same transaction. A visible append marks stale; edits, hard/soft deletes, or supersession also advance `invalidation_epoch` and invalidate derived content. Clearing the dirty flag after rebuild never resets that epoch: older revisions remain ineligible forever. Ignore feedback-only changes. Triggers must survive upsert retries without incrementing on `DO NOTHING`. Seed existing threads in bounded migration/backfill batches; an untouched old thread remains pending until processed. Keep raw messages canonical, never copy hidden message metadata into revisions.
- [ ] **Green:** repeat focused tests; expect PASS. Temporarily disable the content-change trigger and observe the named test fail, restore it, then pass again.
- [ ] **Commit:** explicitly stage the Task 1 files; `git commit -m "feat: persist conversation context source revisions"`.

## Task 2: Refresh rich context with honest coverage

**Files:** Create refresh/worker above, `scripts/backfill_conversation_context.py`, `backend/tests/unit/services/conversation_context/test_refresh.py`, and `backend/tests/eval/test_conversation_context_fidelity.py`; modify `backend/src/tasks/celery_app.py` include/beat registration and `backend/src/core/config.py` with `CONVERSATION_CONTEXT_ENABLED=False`. Use existing `backend/src/services/agent/llm_factory.py:build_lightweight_llm` without changing sidebar summarization.

**Interfaces:** Produce `async refresh_context(db: AsyncSession, thread_id: UUID, *, max_messages: int = 100, max_input_chars: int = 64000) -> RefreshResult`, where `RefreshResult(state: Literal["published","partial","superseded","retry"], revision_id: UUID|None)`. `ContextBlock(id:UUID, kind:Literal["purpose","decisions","facts","open_questions","next_steps","resources"], text:str, authority:Literal["derived_summary","user_correction","user_pin"], source_message_ids:list[UUID])`. Persist resumable chunk coverage; a capped individual message records omitted characters instead of falsely claiming complete coverage.

Worker exports Celery `refresh_due_contexts(limit:int=20)->int`, scheduled every 60 seconds, using `run_async` and a fresh async session per document with a 120-second lease. Define `async backfill_documents(db:AsyncSession,*,after_thread_id:UUID|None,limit:int=500)->UUID|None`; CLI `python scripts/backfill_conversation_context.py --after-thread-id <uuid> --limit 500` prints the committed next cursor. Repeating a page is idempotent and does not invoke the model. Empty threads are eligible: publish empty blocks with zero/zero complete coverage without an LLM call.

- [ ] **Write failing tests.** A deterministic model fixture returns blocks referencing supplied IDs. `test_201_messages_requires_three_pages` processes 100/100/1 messages; `test_edit_during_generation_discards_candidate` increments the revision before publication; `test_unknown_source_id_rejected` injects an invented citation; `test_empty_thread_needs_no_model` asserts zero model calls. Add live-model synthetic fidelity cases: assistant proposes SQLite, user decides PostgreSQL, an assistant claims a test passed without evidence, and user reverses an earlier choice.

```python
assert first.state == "partial"
assert first_coverage.covered_messages == 100
assert first_coverage.complete is False
assert final_coverage.covered_messages == 201
assert racing_refresh.state == "superseded"
assert invented_source_error.code == "invalid_source_reference"
# Model-dependent fidelity fixture, separate from mock unit tests:
assert "PostgreSQL" in decision_text
assert "SQLite" not in decision_text
assert unsupported_test_claim_is_verified_fact is False
```

- [ ] **Red:** `pytest -q backend/tests/unit/services/conversation_context/test_refresh.py`; expect missing refresh service.
- [ ] **Implement bounded refresh.** Read only visible, unsuperseded user/assistant messages in `(created_at,id)` order. Treat stopped assistant content as partial evidence. Use `build_lightweight_llm(temperature=0,max_tokens=2048,request_timeout=30,tool_calling=True)` for structured blocks; verify every cited ID/hash against the snapshot, and label assistant assertions as reported claims rather than verified outcomes. Omit tool/system messages and internal metadata. On edit/delete rebuild derived sections; on append incorporate covered prior chunks plus new messages. Publish with compare-and-swap on source and annotation revisions. Merge preserved annotations by identity, never through model rewriting. Claims, retries, and progress are durable; worker scans at most 20 due documents per tick with one lease per thread and exponential retry capped at 15 minutes. Backfill uses the same queue and budgets. `complete=True` requires no uncovered or truncated source content; model failure leaves a truthful pending/error/stale state, not a fabricated summary.
- [ ] **Green:** repeat focused tests and `pytest -q backend/tests/services/threads/test_summary_rate_limit_paths.py`; expect PASS. Run `pytest -q backend/tests/eval/test_conversation_context_fidelity.py` with the configured model; missing credentials means NOT RUN, never implied semantic verification from mocked tests. Mutation-check source/annotation compare-and-swap guards.
- [ ] **Commit:** stage exact Task 2 files; `git commit -m "feat: maintain bounded source-linked conversation context"`.

## Task 3: Separate selected-thread grants and read APIs

**Files:** Create access/read/integration resolver/routes above, `backend/alembic/versions/cc02_thread_grants.py`, and `backend/tests/unit/services/conversation_context/test_access.py`; modify Plan 01's `backend/src/{models/integration_grant.py,services/integrations/context.py,api/integrations/grants.py}`, `frontend/app/(dashboard)/integrations/approve/page.tsx`, `backend/src/{main.py,core/config.py}`, and generated HTTP contracts.

**Interfaces:** Produce frozen `ConversationContext(user_id:UUID, organization_id:UUID, grant_id:UUID|None, thread_ids:frozenset[UUID])` and `async resolve_conversation_context(db:AsyncSession, token:str, *, required_scope:str)->ConversationContext`. Produce `async read_context(db:AsyncSession, context:ConversationContext, thread_id:UUID, *, expected_revision_id:UUID|None=None)->ConversationContextDTO` in `read.py`. DTO includes `thread_id`, nullable `revision_id`, `source_revision`, nullable `covered_source_revision`, `annotation_revision`, `generated_at:datetime|None`, `freshness:Literal["pending","fresh","stale","invalidated","error"]`, `blocks:list[ContextBlock]`, `source_refs:list[MessageSourceRef]`, and `coverage:CoverageDTO`. Define `CoverageDTO(eligible_messages:int,covered_messages:int,truncated_message_ids:list[UUID],complete:bool)` and `MessageSourceRef(thread_id:UUID,message_id:UUID,content_sha256:str,url:str)`. Future workflows consume this read-only interface with their explicitly authorized source-thread set. Define `ConversationAccessDenied(PermissionError)` and `ContextGenerationError(ValueError)` with stable `code:str` in the schema module.

Define independent `NOUS_CONVERSATION_MCP_ENABLED=False`: external tool registration and every external read require both this and `NOUS_MCP_ENABLED`, returning 503 when either is disabled. Native authorized context reads and grant revocation remain available; `CONVERSATION_CONTEXT_ENABLED` controls background generation only. Cap serialized context DTOs at 64 KiB, 100 blocks, 2,000 characters/block, and 50 source IDs/block. Add `omitted_blocks:int` to coverage; if the aggregate cannot fit, retain whole bounded blocks/source refs, report omissions and `complete=False`, and let callers selectively read originals. Never truncate JSON or silently discard owner pins; reject oversized annotation patches with 422.

- [ ] **Write failing access tests.** Parameterize `test_external_scope_is_explicit` over missing selection, foreign public thread, revoked membership, deleted ancestor, and project-grant misuse. Test non-project selected thread success and expected-revision conflict. `test_revocation_during_response` pauses before serialization, revokes the grant, then resumes; `test_rebuild_does_not_restore_old_revision` rebuilds after deletion and attempts the prior revision. `test_external_flag_preserves_native_reads` disables only external recall; `test_large_context_has_honest_coverage` exceeds the aggregate cap.

```python
with pytest.raises(ConversationAccessDenied):
    await read_context(db, selected_context, unselected_thread.id)
assert nonproject_result.thread_id == selected_nonproject.id
assert project_resolver_with_thread_grant.status_code == 403
assert stale_expected_revision.status_code == 409
assert revoked_during_response.status_code == 403
assert old_revision_after_rebuild.status_code == 409
assert disabled_external_response.status_code == 503
assert authorized_native_response.status_code == 200
assert len(context_json_bytes) <= 65536
assert bounded_context.coverage.omitted_blocks > 0
assert bounded_context.coverage.complete is False
```

- [ ] **Red:** `pytest -q backend/tests/unit/services/conversation_context/test_access.py`; expect missing resolver/read API.
- [ ] **Implement scope and transport.** Extend grant requests with discriminated `scope_kind:"project"|"threads"`; thread-kind requires 1–100 explicit `thread_ids`, null project, and `threads:read` only. Browser owner authority governs corrections; no external write scope. Existing project resolver rejects thread-kind. Display the exact selection in Plan 01's browser approval; never broaden on renewal. External calls require CLI JWT plus restricted grant with matching actor/org. `read_context` intersects selection, same-org workspace, active owner/member access and live ancestors; public access alone is insufficient. For a personal workspace with null organization, require its owner's current organization to match instead. Native browser reads use normal current access and a server-constructed single-thread context, never caller-provided authority. Define `GET /api/v2/threads/{id}/context`, `GET /api/v2/threads/{id}/context/sources/{message_id}`, and `GET /api/v1/integrations/conversations/{id}/context`. Require revision invalidation epoch equality and live source IDs/hashes before returning any derived body, including historical/export paths after rebuilding. Source refs resolve to `/chat/context/{thread_id}?source={message_id}`. Recheck grant revocation/current access immediately before every page response; test revocation during search/read, not just before starting.
- [ ] **Green:** repeat focused tests; regenerate with `python scripts/ci/generate_openapi.py` and `pnpm --dir frontend generate:api-types`; expect exit 0.
- [ ] **Commit:** stage listed files/contracts; `git commit -m "feat: authorize explicitly selected conversation recall"`.

## Task 4: Preserve owner corrections, pins, and Markdown export

**Files:** Create annotations service and `backend/tests/integration/test_conversation_context_annotations.py`; modify native context routes/schema and generated contracts.

**Interfaces:** Produce `AnnotationPatch(expected_annotation_revision:int, entries:list[AnnotationInput])`, `AnnotationInput(id:UUID,kind:Literal["user_correction","user_pin"],text:str,source_message_ids:list[UUID])`, `async update_annotations(db:AsyncSession, actor:User, thread_id:UUID, patch:AnnotationPatch)->ConversationContextDTO`, and `async export_context_markdown(db:AsyncSession, context:ConversationContext, thread_id:UUID)->str`. Export is generated from the same authorized read response, not a second stored document.

- [ ] **Write failing tests.** `test_pins_survive_refresh` stores a correction/pin, regenerates, and compares exact user text; `test_concurrent_annotation_conflict` submits the same expected revision twice; `test_cli_cannot_correct` omits its grant header; `test_deleted_source_is_not_exported` invalidates the referenced message.

```python
assert regenerated_pin.text == original_pin.text
assert conflict_response.status_code == 409
assert cli_without_grant_response.status_code == 403
assert "user_correction" in exported_markdown
assert deleted_message_text not in exported_markdown
```

- [ ] **Red:** `pytest -q backend/tests/integration/test_conversation_context_annotations.py`; expect missing annotation/export methods.
- [ ] **Implement annotations/export.** Add `PATCH /api/v2/threads/{id}/context/annotations` using Plan 01's verified `require_interactive_user`, current access, and document-owner identity. Other members cannot silently change shared context. Store annotation revisions atomically with expected-revision checks; deletion of an entry is explicit by omission from the submitted replacement set. Source-bound pins become unavailable when their source is deleted/superseded; retain the pin record for resolution but hide copied source text. Independent user-authored corrections remain labeled user assertions. `GET /api/v2/threads/{id}/context/export` returns UTF-8 Markdown with generation time, freshness/coverage, authority labels, and verified source links; never include invalidated generated prose or private attachment bytes. A refresh request only queues bounded work.
- [ ] **Green:** repeat focused tests; regenerate both HTTP contracts and mutation-check annotation compare-and-swap; expect PASS.
- [ ] **Commit:** stage exact Task 4 files; `git commit -m "feat: preserve context corrections and export provenance"`.

## Task 5: Deliver selective MCP recall and the context document UI

**Files:** Create frontend/MCP files in the map, `frontend/app/(dashboard)/chat/context/[threadId]/page.tsx`, `frontend/src/components/chat/context/__tests__/ConversationContextPanel.test.tsx`, `packages/harness-bridge/src/mcp/conversations.test.ts`, and `backend/tests/integration/test_conversation_recall.py`; modify `frontend/src/components/chat/ChatHeader.tsx`, `packages/harness-bridge/src/{credentials.ts,mcp/client.ts,mcp/server.ts,mcp/stdio.ts}`, integration routes/read service, and generated contracts.

**Interfaces:** Produce `async search_conversations(db:AsyncSession,context:ConversationContext,query:str,*,limit:int=20,cursor:str|None=None)->ConversationSearchPage` and `async read_conversation(db:AsyncSession,context:ConversationContext,thread_id:UUID,*,cursor:str|None=None,limit:int=50)->ConversationMessagePage`. Search page items contain thread ID/title, bounded snippet, matching source IDs, freshness; message page items contain ID/role/text/stopped flag/source ref; both have nullable `next_cursor`. Produce `registerConversationTools(server:McpServer,client:ConversationApiClient):void`; client methods mirror these APIs plus `read_context`.

Keep Plan 02's `McpSession` unchanged. Extend its protected handle to `CredentialBundleV2={version:2,accessToken:string,projectGrant?:string,conversationGrant?:string}`; `resolveMcpCredentials(handle:string,kind:"project"|"threads"):Promise<IntegrationCredentials>` in `credentials.ts` rejects an absent kind. Legacy records map to project only. Each client sends exactly its bound grant, selected by server-owned tool registration, never model arguments.

- [ ] **Write failing tests.** `test_search_filters_before_ranking` seeds selected/unselected matching threads; `test_pages_exclude_superseded` checks visible-message pagination. MCP `node:test` invokes each tool through SDK transport, including simultaneous project and conversation calls through one server; browser Vitest resolves a deferred old-account response after switching accounts. `test_conversation_only_credentials` advertises conversation tools without project tools; `test_legacy_credentials_do_not_grant_threads` rejects thread calls from a legacy record.

```typescript
assert.deepEqual(searchResult.items.map(x => x.thread_id), [selectedThreadId]);
assert.equal(advertisedNames.includes('search_nous_conversations'), true);
assert.equal(advertisedNames.includes('read_nous_conversation'), true);
assert.equal(advertisedNames.includes('get_conversation_context'), true);
assert.equal(conversationOnlyNames.includes('search_documents'), false);
assert.equal(projectRequest.headers['x-nous-integration-grant'], projectGrant);
assert.equal(conversationRequest.headers['x-nous-integration-grant'], conversationGrant);
// Vitest in the frontend-only test:
expect(screen.queryByText(oldAccountContext)).not.toBeInTheDocument();
```

- [ ] **Red:** `pytest -q backend/tests/integration/test_conversation_recall.py`; `pnpm --filter @nous/harness-bridge test -- src/mcp/conversations.test.ts`; `pnpm --dir frontend exec vitest run src/components/chat/context/__tests__/ConversationContextPanel.test.tsx`; expect missing endpoints/components/tools.
- [ ] **Implement recall and UI.** Define integration `POST /conversations/search`, `GET /conversations/{id}/messages`, and the context GET from Task 3 under `/api/v1/integrations`. Filter selected, currently authorized threads before PostgreSQL ranking/counts; reuse existing FTS patterns without public-scope expansion. Exclude deleted/superseded/system/tool rows and legacy shadow agent threads. Enforce 20 search results, 50 messages/page, 64 KiB serialized response and opaque scope-bound cursors; oversized text returns explicit truncation with continuation, never silent coverage claims. Tools load only requested results; MCP tools are the baseline; optional resources are advertised as server capabilities that clients may choose to read, without assuming client support. No MCP write/refresh/correction tool. Add Context in `ChatHeader`, a source-preview permalink route, freshness/coverage labels, owner-only correction/pin controls labeled as shared conversation edits, Markdown download, and explicit grant thread selection. Query keys include account/thread/revision; use TanStack Query, preserve chat-store transcript ownership, and cancel/discard stale responses.
- [ ] **Green:** repeat focused commands; `pnpm --filter @nous/harness-bridge type-check`, `scripts/ci/run_local_ci.sh --skip-tests --frontend`, `python scripts/ci/generate_openapi.py --check`, `pnpm --dir frontend check:api-types`, and `git diff --check`. Manually prove one project and one non-project chat through standalone MCP and normal browser context, recording IDs/revision coverage without private text. Service/live dependencies unavailable mean NOT RUN.
- [ ] **Commit:** stage exact Task 5 files; `git commit -m "feat: expose selective conversation context to MCP and chat"`.

# Agent Citations and Workspace Threads Design

**Date:** 2026-09-12
**Issue:** #1622
**Status:** revised and approved by Astra for implementation planning

## Goal

Make agent answers carry the provenance returned by knowledge-base tool calls,
and make the chat sidebar show every accessible thread in the active workspace
regardless of which historical conversation contains it.

The repair must preserve existing conversation-scoped APIs and historical data.
It must not merge or delete the hundreds of conversation rows already present in
the live workspace.

## Confirmed root causes

The agent graph has two retrieval channels with different behavior. The RAG node
writes normalized chunks to `state["retrieved_contexts"]`; synthesis numbers that
context as `[Doc N]`, streaming emits it, and assistant-message persistence turns
it into `Citation` rows. The `do_kb_retrieve` tool instead leaves the same source
data only inside `state["tool_executions"][].result.chunks`. Consequently the
model receives contradictory no-retrieval guidance after a successful tool call,
the live stream emits no citation context, and the committed message has no
citations.

The sidebar is scoped to one conversation. `useChatSession` resolves or restores a
single `default-conversation-id`, calls the conversation-level thread endpoint,
and replaces the sidebar with only that response. The backend agent fallback also
creates a new `Conversation` whenever an explicit thread lookup misses. These two
behaviors produced hundreds of one-thread conversations while the sidebar showed
at most one container's threads.

## Chosen architecture

### Canonical retrieval provenance

Add one pure normalization boundary in the agent service that recognizes a
successful `do_kb_retrieve` execution and converts each safe chunk to the existing
retrieved-context envelope:

- `document_id` from the canonical UUID resolved by the tool;
- `title` from the tool's redacted title;
- `content` from the chunk's `text`;
- `score` and `score_source` from the retrieval result;
- optional `chunk_id`, `chunk_index`, and `page_number` from explicit tool chunk
  metadata, validating strings/integers and never inferring a page from rank;
- internal `tool_call_id` and original zero-based result-chunk position to bind
  each numbered source to its evidence-bearing tool message unambiguously;
- an internal origin marker identifying tool-derived context when prompt shaping
  needs to avoid duplicating the complete tool payload.

The tool node merges these contexts with any contexts already produced during the
turn. It preserves first-retrieved order, deduplicates repeated chunks using the
canonical document UUID plus SHA-256 of whitespace-collapsed content
(`" ".join(content.split())`), and retains at most 20 contexts per turn. Existing
entries win; later calls append only unseen entries until the cap is reached.
Content retains its original whitespace for display. Empty, failed, malformed,
non-finite, or unresolved chunks are
ignored rather than converted into plausible-looking citations. Title-only
`search_documents` results are not evidence and are not promoted.
Normalize each newly completed tool batch before the tool-execution history is
pruned. Both general and filtered specialist tool nodes use this boundary.

`state["retrieved_contexts"]` remains the single downstream contract. Post-tool
synthesis receives stable `[Doc N]` numbering and no longer receives the false
"No documents were retrieved" instruction. New turns reset context; confirmation
resumes preserve the interrupted turn's context. The main LLM, research/data
specialist LLMs, and forced-synthesis nodes use the same source-map renderer.
Tests must execute these paths, not merely test the renderer in isolation.

Normal and confirmation/resume streaming emit the complete cumulative retained
context list whenever its content changes, including tool-node and specialist
outputs. Events are snapshots, not deltas: the frontend's replacement semantics
are intentional. Remove the independent three-context stream truncation. Repeated
node/wrapper events must not duplicate or renumber sources. Stream payload bounds
must preserve the entire numbered source list, bounding snippet/title lengths
instead of dropping numbered entries. Assistant persistence writes every retained
context and its optional locator fields atomically with the message. Reloaded
citation order must preserve the original `[Doc N]` mapping; database relationship
iteration order is not an ordering guarantee.
Add a nullable integer `source_position` to Citation via an additive Alembic
migration. New agent citations persist their one-based `[Doc N]` index there.
Serialize positioned rows in ascending position, followed by legacy unpositioned
rows ordered by `(created_at, id)`; do not infer positions for historical rows or
reuse `chunk_index`. Expose the optional position in citation response contracts
and preserve it through frontend adapters. Confirmation UI consumes cumulative
snapshots as replacements, removing existing carried-plus-resume concatenation.
Centralize this ordering in the model relationship or a shared serializer used
by every message reload path, rather than only the agent persistence function.

Prompt rendering must not duplicate full multi-kilobyte chunks already present in
tool messages. Tool-derived contexts therefore render as a compact numbered source
map while the corresponding tool message remains the content-bearing evidence.
RAG-node contexts keep the existing full fenced-document rendering. Both origins
share one numbering order and one persisted citation list.
Source-map titles and locators remain inside `wrap_untrusted` fences, with prompt
field sanitization and bounded lengths. PII redaction is not instruction escaping.
If compaction removed the content-bearing tool message, render the retained
bounded evidence instead of a source label with no supporting content. Duplicate
documents with distinct chunks retain distinct canonical chunk numbers. Preserve
the existing document-grouped footnote presentation: its shared display-number
map may collapse chunks to one document numeral, but must resolve each original
`[Doc N]` against the canonical array before applying that map, identically live
and after reload.
Compaction can keep the same message/call ID while replacing its content: inspect
`additional_kwargs['compacted']` and verify original chunk content is still
available. An unparseable dedupe-cache prefix or failed original-chunk binding
also selects the bounded-evidence fallback.

### Workspace-wide thread listing

Add a read-only, paginated endpoint:

`GET /api/v2/workspaces/{workspace_id}/threads`

The endpoint returns the existing `ThreadListResponse`. Each `ThreadResponse`
already carries its actual `conversation_id`, so the response needs no new thread
view model. The query:

- resolves workspace access through the canonical membership-aware access funnel;
- joins threads through non-deleted conversations in the requested workspace;
- excludes deleted threads, conversations, and workspaces;
- applies any supported status filter identically to the count and row queries;
- orders the complete workspace result by recent thread activity with a stable ID
  tie-breaker;
- paginates globally across conversations; and
- uses the existing bounded latest-message preview expression.

The access lookup uses `load_conversations=False, load_collections=False`; an
authorization check must not eagerly load the historical container collections.

Existing nested and standalone conversation-level thread endpoints remain
unchanged for compatibility.

The frontend adds a generated-contract-backed workspace-thread service method.
`useChatSession` uses this method for cold initialization, warm restoration, and
pagination. It maps every row with that row's `conversation_id`; the default
conversation remains only the parent used when a user creates a new thread. A
successful new-thread creation is inserted into the workspace-wide list without
waiting for a full reload. Pagination upserts by thread ID so activity changes
between offset pages cannot duplicate sidebar entries. Existing request-identity
guards reject responses belonging to a previously selected workspace. Reopening
or restoring a workspace reloads its first page in server order; this change does
not introduce polling. The selected thread remains addressable even if it falls
outside that page. Offset pagination does not promise a snapshot during writes.

Persisted conversation IDs may still accelerate choosing a parent for a new
thread, but they no longer scope or replace the sidebar. A stale persisted ID must
not hide valid workspace threads.
A thread-list read failure displays/retries an error and must never create a
conversation. Remove the existing cold-initialization create-on-list-error path.

### Thread resolution and creation safety

An explicit `request.thread_id` is authoritative. Resolution uses the shared
workspace access rules and verifies that the caller may edit the containing
workspace. A missing, deleted, or inaccessible explicit ID returns a controlled
resolution failure; it never falls through to an unrelated conversation or
creates a replacement thread.

Introduce a typed service resolution error and map it at each transport boundary:
`/execute` returns HTTP 404 before creating a job; `/stream`, whose response may
already have started, emits one existing-schema non-enumerating terminal error
before any `accepted` frame, run creation, or graph/checkpoint invocation.
Malformed explicit thread UUIDs retain the existing controlled validation error.
Confirmation/resume also fails before continuation if durable thread access has
been lost; it never creates a replacement. Read access (public/viewer) alone does
not authorize agent writes: require `Workspace.can_user_edit` in addition to the
canonical soft-delete-aware access funnel.

When a caller supplies no thread ID, the agent may create a durable thread. It
first selects an editable, non-deleted conversation in the intended workspace.
Add optional UUID `workspace_id` to `PageContextRequest`; chat sends the active
workspace ID and generated HTTP contracts update together. An explicit workspace
must pass edit authorization; a missing/inaccessible workspace is a resolution
error, not a fallback to another workspace. An explicit thread remains
authoritative, with its containing workspace determining authorization.

Without either explicit ID, select the oldest non-deleted workspace owned by the
caller (`created_at ASC, id ASC`); there is no persisted user-default workspace
contract. If none exists, preserve the existing threadless ephemeral behavior.
Within the selected workspace, reuse its oldest live conversation
(`created_at ASC, id ASC`). Lock the selected workspace row before checking for a
conversation so concurrent threadless requests cannot both create a container.
Create a conversation only when that workspace has none. The new
thread retains the existing agent marker and title behavior.

This change prevents future container proliferation without rewriting historical
rows.

## Data flow

1. The user opens chat; the client resolves the workspace and requests the first
   workspace-wide thread page.
2. The user selects or creates a thread. The client sends that thread's real ID to
   the agent stream.
3. The backend resolves the explicit thread through the canonical access boundary.
4. `do_kb_retrieve` returns safe, tenant-scoped chunks in its tool result.
5. The tool node promotes and merges those chunks into canonical retrieved context.
6. Synthesis cites the numbered contexts, and streaming publishes the same source
   data to the active turn.
7. Assistant persistence writes the message and its citation rows atomically.
8. Terminal frontend reconciliation reloads the server-canonical message, retaining
   sources and footnotes after refresh.

## Error handling and observability

- Inaccessible workspaces and explicit threads fail closed with the transport
  contracts above. Workspace list reads continue to use HTTP 404; access failures
  reveal no alternate thread, workspace, or owner identity.
- A legitimately empty workspace thread list returns an empty page; access failure
  is never represented as an empty success.
- A malformed retrieval result is logged with tool name, call ID, and rejection
  category, without logging chunk text or user query content.
- If citation persistence fails, the existing required assistant-persistence path
  reports failure rather than claiming a fully saved answer.
- Workspace-thread query logs may include workspace ID, page size, and result count,
  but not message previews.
- No endpoint or fallback performs a fan-out request per conversation.

## Compatibility and generated contracts

The workspace-wide endpoint is additive. The backend OpenAPI snapshot and generated
frontend TypeScript types are regenerated together. Touched frontend service types
adopt the generated schema according to `docs/engineering/api-contracts.md`.

The only database migration adds nullable citation `source_position`; it requires
no backfill or historical row rewrite. Existing conversations and threads remain
addressable by their current IDs, and existing conversation-scoped clients keep
their behavior. This replaces the original no-migration assumption because SQL
row insertion order cannot guarantee numbered citation order after reload.

## Test strategy

All production changes follow red-green-refactor.

Backend coverage must prove:

- successful knowledge-base tool chunks become canonical retrieved contexts;
- repeated chunks across multiple tool calls deduplicate in first-seen order,
  while separate chunks in one document keep stable distinct numbers;
- malformed, failed, unsafe, and title-only results do not create citations;
- the turn cap is enforced without changing the source numbering order;
- post-tool synthesis receives a numbered source map instead of false no-retrieval
  guidance;
- both normal and confirmation/resume streaming expose and persist tool-derived
  citations;
- message persistence creates reloadable citation rows with the same source
  numbering and optional page/chunk locators;
- compact tool source maps fence malicious titles/locators as untrusted data;
- a second fresh turn clears prior evidence while confirmation retains it;
- oversized non-ASCII context payloads preserve all retained source positions
  within the SSE byte limit, including confirmation and replay;
- compaction still leaves synthesis with bounded evidence for every retained map
  entry, and normalization precedes tool history pruning;
- workspace thread listing respects membership, ancestor soft deletion, status
  filters, stable activity order, global pagination, and preview bounds;
- inaccessible workspace and explicit-thread resolution fail closed; and
- threadless agent requests reuse an editable conversation and create one only when
  necessary.

Frontend coverage must prove:

- initial sidebar data contains threads from multiple conversation IDs;
- warm restoration does not let a cached conversation ID scope the sidebar;
- workspace-wide pagination appends/upserts rather than replaces rows or creates
  duplicate IDs; workspace switches discard obsolete responses;
- a deep-linked thread outside the first page is restored without losing the first
  page;
- a newly created thread appears immediately with its real conversation ID; and
- live and reloaded agent answers render the same source list and inline markers.

Verification includes focused backend and frontend suites, architecture guards,
OpenAPI drift checks, and changed-file lint/type gates. A deterministic Chromium
Playwright flow uses seeded/intercepted API and SSE data for a knowledge-base
answer with more than three chunks across multiple tool calls, verifies live
numbering/locators, reloads, and verifies the same citations and workspace-wide
threads. Backend integration tests separately exercise normalization, graph event
handling, persistence, and message serialization. A live external LLM/KB smoke is
optional and must be reported separately from deterministic regression coverage.
New race/idempotency tests follow the repository's mutation-verification rule.

## Files expected to change

- `backend/src/services/agent/_nodes_tools.py`
- `backend/src/services/agent/_nodes_llm.py`
- a shared agent retrieval provenance/prompt helper
- `backend/src/services/agent/subgraphs/research_agent.py`
- `backend/src/services/agent/subgraphs/data_agent.py`
- `backend/src/services/agent/subgraphs/_factory.py`
- `backend/src/services/agent/schemas.py`
- `backend/src/services/agent/agent_execution_service.py`
- `backend/src/api/agent/streaming.py`
- `backend/src/api/agent/execute.py`
- `backend/src/models/citation.py`, citation response schema/presenters, and one
  additive Alembic migration for nullable `source_position`
- `backend/src/services/threads/thread_service.py`
- `backend/src/api/threads/workspace_routes/threads.py`
- agent and thread backend tests
- `backend/openapi.json`
- `frontend/src/services/workspaceService.ts`
- `frontend/src/hooks/chat/useChatSession.ts`
- `frontend/src/hooks/chat/useChatStreaming.ts`
- focused frontend hook/component tests
- `frontend/src/types/generated/api.d.ts`
- a focused Playwright chat regression test or extension of the existing chat flow

Exact file boundaries may narrow during implementation, but behavior must not be
duplicated across stream/job or nested/standalone paths.

## Out of scope

- Merging, deleting, or reparenting historical conversation rows.
- A data-cleanup migration for the existing one-thread conversation containers.
- Treating title/filename search results as content evidence.
- Adding citations for external tools whose result contracts do not carry canonical
  document provenance.
- Closing #1610's remaining low-severity findings.
- Browser verification or closure of #1603, except where the #1622 regression flow
  overlaps its chat surface.

## Success criteria

- A knowledge-base tool answer persists one citation for every retained canonical
  evidence chunk, and the same numbered sources and locators are available live and
  after reload.
- Synthesis can reference tool-derived evidence with stable `[Doc N]` markers.
- The sidebar lists all accessible workspace threads through one bounded endpoint,
  independent of conversation-container count.
- An explicit thread lookup miss creates no conversation or thread.
- New threadless agent sessions reuse an editable conversation whenever possible.
- Existing APIs and historical data remain intact.
- Focused tests, contract generation, browser regression, and repository gates pass.

## Implementation verification — 2026-09-12

Implemented locally on `fix/1622-agent-citations-workspace-threads` by Sol with
high reasoning. Astra approved the revised design, both implementation surfaces,
and the final generator/synthesis/resolver regression coverage with no remaining
material findings. The verification below was recorded before PR publication;
no deployment was performed.

- Combined backend verification: **424 passed**, no failures or skips. This
  includes architecture, thread services/routes, provenance, prompt safety,
  resolution, normal/confirmation streaming, and PostgreSQL persistence tests.
- After the final annotation corrections: **44 focused backend tests passed**,
  including the real PostgreSQL citation round trip and workspace listing.
- Frontend: **75 tests passed** across 11 files; the two corrected restoration
  mock suites were independently rerun (**6 passed**).
- Chromium: **1 passed**, independently rerun. Real session/stream hooks and
  citation renderers consume intercepted API/SSE data, verify live citations
  before canonical-message hydration, then perform an actual page reload.
- Migration: the new nullable citation position migration was upgraded,
  downgraded, and upgraded again on scratch PostgreSQL; a legacy row survived
  unchanged. Database ordering and citation locator/position reload were verified.
- Gates: full-tree Ruff/Black/isort; all 36 changed/new Python files; all eight
  added Python files under pinned mypy in a clean, CI-equivalent lint environment;
  frontend TypeScript; changed-file lint/debt and exclusion ratchets; new frontend
  file ESLint; directory docs; Alembic single-head/revision checks; and
  `git diff --check` passed. Existing full-tree frontend lint debt remains within
  its baseline, not eliminated by this change.
- Contracts: OpenAPI drift check passed; regenerating TypeScript contracts
  preserved both artifact hashes. This checks generation consistency without
  treating the intentional uncommitted artifact diff as drift.
- Mutation checks demonstrated failures after disabling workspace request
  identity, selection ownership, pagination upsert, and duplicate-snapshot guards;
  each guard was restored and its focused test passed again.

The browser test does not exercise a live external LLM/KB or production auth.
During PR preparation, the targeted evidence migration probe and the full
historical Alembic upgrade from an empty disposable PostgreSQL database both
passed. The 424 backend and 75 frontend tests were also rerun successfully.
The temporary dev server was stopped and the scratch database removed; only
generated verification data was discarded. Existing audit/notebook files and
package/lock files were left unchanged.

## PR #1624 review follow-up — 2026-09-12

Sol (high reasoning) addressed all four verified review findings:

- Recover unavailable persisted/deep-linked threads on 403/404 without hiding
  transient failures or replacing newer selections, turns, or workspace state.
- Stamp server-owned durable/ephemeral checkpoint provenance. Only an owned,
  explicitly ephemeral checkpoint with a live interrupt and checkpoint claim can
  resume without a database thread; durable and unmarked checkpoints still
  require current edit access. Ephemeral completion never writes chat rows.
- Apply the same owner/live editable-membership and ancestor-deletion checks to
  edit-and-resend tombstones as thread resolution.
- Retain the latest raw cumulative retrieved contexts when persisting stopped
  initial and confirmation answers, including carried sources and empty resets.

Legacy dispatch/confirmation fixtures now exercise the intended branches through
the current access boundary. The anti-enumeration test compares actual error
payloads instead of counting source-code references. Astra reviewed the final
production and test changes and approved them with no remaining findings.

Verification after the functional fixes:

- A clean checkout ran the CI-selected backend suite on Python 3.12: **5,489
  passed, 80 skipped, 3 xpassed**, with no failures or collection errors. The
  skips include existing external-service/evaluation requirements. The clean
  copy excludes unrelated nested worktrees and stale Python cache directories.
- The two new confirmation/partial-persistence suites were independently rerun:
  **15 passed**. All previously failing CI paths passed their focused rerun.
- Full frontend suite: **1,941 passed** across **285 files**.
- PostgreSQL persistence: **6 passed**, including stopped-answer citation order,
  title, snippet, page, and chunk round trips on a task-owned scratch database.
  The thread-service suite was separately rerun with PostgreSQL available:
  **158 passed**, including workspace access and workspace-wide thread listing.
- Chromium live citation/workspace-list regression plus actual reload: **1
  passed**. API/auth/SSE remain intercepted; this is not a live external LLM test.
- All **59 changed Python files** passed pinned Ruff/Black/isort; all **11 added
  Python files** passed pinned mypy. Frontend type-check, lint-debt/exclusion
  ratchets, OpenAPI/types generation, directory docs, Alembic graph checks,
  targeted migration delta, and empty-database migration replay passed.
- Mutation checks proved the stale-response/URL guards and ephemeral
  confirmation-claim guard fail when disabled and pass after restoration; exact
  commands are recorded beside the regression tests.

Existing advisory full-tree frontend lint debt remains; no baselines were
relaxed. GitHub CI must rerun on the follow-up commit before merge.

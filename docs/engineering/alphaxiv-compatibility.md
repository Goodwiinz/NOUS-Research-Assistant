# alphaXiv PDF chat compatibility

Status: v2/v3 transport, branch history and selected-quote implementation, 2026-10-14. Built on PR #1982 at
`85cf136132ab763b48ef31ce8a086a1fe8ac084d`. This describes the local code
contract, not a deployed service or verified alphaXiv backend equivalence.
The request and stream shapes come from alphaXiv's publicly shipped client.
Two authenticated hosted v2 UI turns were verified on 2026-10-14, including
quoted text, whole-paper follow-up, persisted answers and citation navigation;
see the [hosted checkpoint](flows/alphaxiv-pdf-chat.md#authenticated-hosted-v2-checkpoint).
Hosted v3 acceptance, live completion and cancellation were subsequently
observed; see the [v3 checkpoint](flows/alphaxiv-pdf-chat.md#authenticated-hosted-v3-and-filtering-checkpoint).
Server validation and exact ranking policy remain unverified.

## v2 transport

[`POST /assistant/v2/chat`](../../backend/src/api/compat/alphaxiv.py) uses
NOUS bearer authentication. `paperVersionId` is an accessible, processed
NOUS PDF document UUID; `llmChatId`, when supplied, is a NOUS thread UUID
for which the caller has edit access. Creating a chat without `llmChatId`
requires an existing owned, nondeleted NOUS workspace. A hosted alphaXiv
identifier confers no document authority.

The request contains `message`, `paperVersionId`, optional `llmChatId`,
optional `protocol: 2` (the default), a NOUS-supported `model` or the observed
`"pro"` preset, optional
`selectionPageRange`, and `webSearch: "off" | "full"`. Omitted model uses the
native configured deployment. `assistantVariant` may be omitted or `"paper"`.

For example, this body asks about physical pages 3 through 5. Replace the
example UUID with a processed PDF in the caller's NOUS organization and send
it with the caller's NOUS bearer token:

```json
{
  "message": "Explain the method on these pages and cite your sources.",
  "paperVersionId": "00000000-0000-4000-8000-000000000001",
  "selectionPageRange": [2, 4],
  "webSearch": "off",
  "protocol": 2
}
```

Read `llm-chat-id` from the response headers and use it as `llmChatId` for a
follow-up request. Also send the visible branch's last canonical message ID
as `parentMessageId`. Null (including omission in this NOUS adapter) starts
a new root, even in an existing chat. The same `paperVersionId` is required
on each request. Omission semantics on the hosted server remain unverified.

The adapter primes the existing native producer through durable acceptance
before returning the `llm-chat-id` header. It does not insert a second user
message or create a parallel transcript. Responses are SSE `data:` JSON
records, including `run_start`, text/reasoning deltas, status, usage, page
filter diagnostics when measured, errors and `run_end`. Source references
with an authorized page number become inline `alphaxiv-paper-citation` tags.
The stream ends at EOF. Disconnect and pre-body response failure close the
same native producer.

The captured hosted v2 streams used status phases/progress, text deltas,
`context_window_usage`, `persisted_messages` and EOF. They contained no
`run_start`, `run_end` or `paper_filter_debug`. NOUS status labels omit that
progress detail, context usage remains unavailable, and persistence uses the
shipped client's history-refetch fallback. Its SSE consumer ignores the
additional lifecycle events. These are bounded compatibility differences;
the capture demonstrated no required code fix.

Omitted/null `filterModel` and explicit `"cohere"` enable the configured native
page ranker for this turn, including when `AGENT_PAGE_FILTER` is off. This matches
the native PDF UI's automatic-ranking intent without changing unrelated chat
defaults. Explicit page selection takes precedence and bypasses ranking.
Short documents may bypass ranking, and provider failure retains
the bounded native fallback; neither case invents timing or model metadata.
Custom providers and overview references return an explicit 422. The observed
`model: "pro"` resolves to NOUS's configured default deployment for execution;
the original preset name is retained in compatibility presentation and replay.
Other model names must belong to the allowlist in
[`AgentExecuteRequest`](../../backend/src/services/agent/schemas.py).
Neither the preset nor automatic Cohere selection claims equivalence to
alphaXiv's private model routing or ranking policy. Other hosted presets remain
unsupported.

`thinking: "low" | "medium" | "high"` maps to a per-turn native answer
reasoning override only for document-only execution on a supported NOUS GPT-5
deployment with `AGENT_USE_RESPONSES_API` enabled. Azure deployments require
API version 2025-04-01 or later. The answer and forced-synthesis paths preserve
the requested model and effort; the override resets on the next turn. Null or
omission preserves configured defaults. Hosted boolean toggles and other
model-specific thinking modes have no inferred equivalent and return 422.
These checks establish a local request contract, not live provider acceptance.

## Uploaded files

`POST /v1/assistant/upload-file` accepts raw bytes, the file's `Content-Type`,
and a once URL-encoded `X-Filename`. PDF, plain-text and Markdown uploads reuse
the canonical NOUS file service, organization quota, private storage, deduplication
and ingestion job. The size cap is the smaller of 30 MiB and the organization
limit. Filenames, signatures and MIME types are validated. Images and other
binary inputs remain unsupported rather than being treated as text.

The response is `{"data":{"url":"api/v1/files/<UUID>/download",
"documentId":"<UUID>","processingStatus":"queued"}}`, with the actual
processing status. The shipped client consumes `data.url`; `documentId` and
`processingStatus` are NOUS extensions. Processing remains asynchronous, so
chat returns 409 until extraction is complete and nonempty.

Chat `files` accepts up to nine supplementary `{contentType,url}` references,
distinct from the bound paper. References use canonical
`/api/v1/files/<UUID>/download` paths. To accept absolute URLs, configure
`ALPHAXIV_FILE_BASE_URL` with the trusted public NOUS API base; request Host
headers never establish that trust. References identify stored documents;
the adapter does not fetch caller-supplied URLs. Every use rechecks current
organization ownership, readiness and MIME type.

Page selection applies to the bound paper only. Supplementary files contribute
their own bounded context, and the combined context is reauthorized after
all reads. Acceptance rolls back if a required file is no longer accessible.
Selected-branch history includes bounded, redacted excerpts of prior uploaded
files; sibling files are excluded. Canonical attachment rows supply history
`input_file` blocks and streaming `userFiles`. Read/replay/resume reauthorize
those files, and unavailable history fails closed.

## v3 acceptance, live replay and stop

`POST /assistant/v3/chat` accepts the same chat fields except `protocol`,
which is absent from the observed v3 request. Its JSON response is
`{"llmChatId":"<thread UUID>","runId":"<run UUID>"}`. Acceptance waits for the
native user-message/run commit and the durable presentation header. A
supervised task then owns the native producer independently of the HTTP
connection. Graceful application shutdown closes those producers before
closing their supporting pools. A process crash requires the existing stale-run
recovery policy; this is not an exactly-once worker redispatch mechanism.

`/live` is a WebSocket using the `alphaxiv-live` subprotocol. Authenticate
with a NOUS bearer header or the browser protocols
`["alphaxiv-live", "alphaxiv-live.auth.<NOUS token>"]`. The native
`["auth", "<NOUS token>", "alphaxiv-live"]` form also works. Native token
verification, revocation and Origin policy still apply. Hosted alphaXiv
session cookies are not accepted as NOUS authority. Every replay read checks
current user, organization, workspace ancestry and PDF access; token validity
is checked again at expiry and at least every 30 seconds while following.

The observed wire protocol carries letter-prefixed records, optionally
separated by literal newlines inside one WebSocket text message:

| Direction | Record | Meaning |
| --- | --- | --- |
| Client | `aassistant-chat;<chat UUID>` | Subscribe |
| Client | `bassistant-chat;<chat UUID>` | Unsubscribe |
| Server | `a["assistant-chat;<chat UUID>", snapshot]` | Initial snapshot |
| Server | `d["assistant-chat;<chat UUID>", {"runId":"...","seq":2,"event":{...}}]` | Next event |
| Server | `e["assistant-chat;<chat UUID>", snapshot]` | Replacement snapshot |
| Server | `b["assistant-chat;<chat UUID>", "safe error"]` | Subscription error |
| Server | `cassistant-chat;<chat UUID>` | Access revoked |

A snapshot is `null` or `{"runId":"...","seq":7,"events":[...]}`. Its `seq`
is the count of projected events; `run_start` also retains nullable `thinking`
(legacy presentation records replay `null`). The first event is
`run_start`; projected sequence numbers are dense and start at 1. They are
independent of internal ledger sequence numbers. Replays use PostgreSQL,
including pagination beyond 500 rows, and do not require the original worker
or Redis buffer. The new presentation facts follow the existing ledger's
output-redaction policy; prompts remain in canonical messages, referenced by
identity and content hash. A changed or deleted user message invalidates its
old presentation. Replay limits fail explicitly at 50,000 ledger rows or a
4 MiB snapshot instead of silently returning a truncated successful answer.

The native ledger's completed, failed or cancelled fact supplies the terminal
live event. No compatibility event is appended after the native terminal.
Citation fragments buffered at the last text boundary are flushed by that
same deterministic terminal projection.

`POST /assistant/v3/chat/stop` accepts `{"llmChatId":"...","runId":"..."}`.
Successful requests return HTTP 204 with an empty body, matching the captured
hosted response. Native cancellation projects `run_end.kind: "aborted"`.
It checks edit access and requests cancellation of that exact owned pair. A
stale stop cannot cancel a replacement run. Running cancellation becomes
terminal when the native producer acknowledges it; a parked approval can be
abandoned immediately through the native guarded transition. Repeating an
already-cancelled pair is idempotent. Disconnecting or unsubscribing a socket
does not request stop.

## History and branch selection

| Request | Response |
| --- | --- |
| `GET /assistant/v2?variant=paper&paperVersion=<PDF UUID>` | Unwrapped array of `{id,title,newestMessage}` |
| `GET /assistant/v2/{chat}/messages` | Unwrapped array of linked content blocks |
| `POST /assistant/v2/{chat}/messages/{message}/select` | NOUS `{selections:[{id,selectedAt}]}` acknowledgement |
| `GET /assistant/v2/{chat}/context-window-usage` | `null` while current-context measurement is unavailable |

History uses fresh NOUS user, organization, workspace and PDF access. Recent
chat lists include explicitly bound PDF chats in owned or joined workspaces;
homepage history is unsupported. Limits fail explicitly above 1,000 chats or
2,000 canonical messages instead of returning an incomplete tree. The list
and selection response shapes are a supported consumer contract, not proof
of the hosted server's validation or limits.

`ChatMessage.parent_message_id` stores canonical ancestry; `selected_at`
chooses visible siblings. The accepted user and answer keep their canonical
IDs. A safe persisted reasoning summary becomes a deterministic supplemental
block before its answer. The bound PDF attachment is context, not an uploaded
file block; supplementary uploads produce deterministic file blocks before
their canonical user text. Feedback is `null` until a native rating conversion is defined.
Measured turn token usage is not substituted for current context usage.

The first compatibility turn opts an empty thread into branching and binds
it to one PDF. Existing nonempty linear threads cannot be converted; create
a new chat. An edit uses the edited user's original parent, preserving the
old branch. Selection stamps the chosen ancestry and its previously selected
descendants, and is rejected while any run is active or awaiting approval.
The native acceptance transaction and assistant writer remain the only
message writers. Ordinary linear writes and individual message deletion in bound chats are
rejected; whole-thread deletion remains available.

Before generation, the selected ancestor path replaces checkpoint messages
and branch-specific identity observations. Missing/deleted ancestors or
failed path reads stop generation; no chronological fallback is allowed.
The latest 40 messages on that path form model context. The same accepted
user identity anchors final, stopped and resumed assistant persistence.

Migration `pb01_pdf_message_branches` adds nullable parent/selection fields,
explicit PDF binding and the one-answer-per-accepted-question constraint.
Existing linear rows remain unchanged. Apply the migration before enabling
this adapter in a deployment.

## Native PDF context

The native agent request also accepts `selection_page_range`,
`page_filter_model` and `document_only`. A range is an inclusive pair of strict
zero-based physical page indices for exactly one document. A bound PDF chat
can carry supplementary files while applying the range only to its primary paper.
Extraction markers, accepted-page
diagnostics and citation page numbers remain one-based. The UI must convert
human page inputs exactly once. Missing, blank, malformed or oversized
selected pages fail closed; the loader never widens the selection to an
unrelated excerpt. Treating the range as a hard restriction is a NOUS
implementation decision; the hosted server's interpretation is unknown.
The authenticated hosted quote flow sent `selectionPageRange: null`, as did
its whole-paper follow-up, so neither verifies the hard-range behavior.

Automatic ranking uses `AGENT_PAGE_FILTER`, which defaults off, and the
shared [page selection service](../../backend/src/services/search/page_filter.py).
The native PDF-chat UI explicitly requests `page_filter_model: "cohere"` when
no manual page restriction is present, including for quoted text. This per-turn
choice leaves unrelated chat defaults unchanged. It uses the existing native
ranker and its measured diagnostics; it is not a claim that its scoring or
selection policy duplicates the hosted backend.
Explicit ranges work independently of that flag. The service bounds source
text to 1,048,576 characters, ranking candidates to 100 pages, and outgoing
source context plus metadata to 64 KiB. Failed automatic ranking retains the existing
bounded excerpt fallback. It must not claim a ranking model or timing for
that fallback. Cohere scores are exposed as NOUS relevance scores; their
meaning has not been shown equivalent to alphaXiv priority values.
Both captured hosted turns omitted `filterModel` and displayed page-analysis
progress. Without filter diagnostics, this establishes neither a ranking
provider nor accepted pages, ranking identities or general server defaults.

`webSearch: "off"` maps to native `document_only: true`; omitted webSearch
currently has the same behavior. This removes agent tools and prevents
fallback to project/organization retrieval. Saved user/project memory is
excluded from retrieval and prompt injection; current conversation history
remains available. `"full"` permits the native
tool policy; it is not a guarantee that a web search occurs. Per-turn scope
and metadata reset before subsequent turns. These controls require NOUS
execution and are rejected for external execution providers.

The native `rag_context` event carries optional typed `page_filter`
metadata. Absent metadata means unknown/unavailable, not zero pages.
`AgentExecuteResponse` exposes the same generated type. Provider URLs,
keys and untyped diagnostics are excluded from this public projection.
Metadata is currently transient; persisted source citations remain owned
by the existing backend message pipeline.

The controlled PDF.js viewer uses authenticated bytes and a local worker.
Its text layer records actual page identities: a noncollapsed selection
produces the sorted, inclusive zero-based anchor/focus range as quote provenance.
Selecting text does not fill the manual page controls or restrict retrieval.
Quoted text is not parsed for page numbers. Manual one-based inputs remain available;
clearing those inputs restores whole-PDF scope independently of the quote,
citation clicks scroll inside the viewer, and
document/account changes discard prior selection and pending loads. Rendering
is bounded to 32 MiB and 100 pages with manual/download fallback.

Selected text is captured from the viewer's owned text layers separately from
the page range. The [quote helper](../../frontend/src/types/pdf-chat.ts) follows
the recovered client format: expand line breaks with Markdown quote prefixes,
bound the expanded quote to the first and last 997 UTF-16 units separated by
`...` when it exceeds 2,000 units, then prepend `> ` and append two newlines
and the original question. The combined message removes malformed Unicode,
NUL and replacement characters. The normal native message-size limit still
applies; it does not silently shorten the question.

The [navigation receipt](../../frontend/src/lib/pdf/chat-intent.ts) keeps the
quote only in memory, bound to user, organization, workspace, document and
its source range. Quote provenance and explicit retrieval range are separate;
the source range never becomes a request restriction implicitly.
The URL carries an opaque, single-use token; account/workspace changes
invalidate pending receipts. Ask transfers the passage from viewer to composer.
Document-detail Ask first resolves the destination through the existing workspace
bootstrap, then stages the receipt. This also supports entering chat for the
first time after login. Pending Ask actions cannot duplicate the handoff;
setup failure retains the selection for retry, and stale account/document
completions cannot navigate or clear a replacement selection.
Manual range edits preserve the quote; Clear quote removes it independently.
A new submission composes it once;
history-based edit/regeneration does not prepend it again. After the stream
settles, durable acceptance or successful completion consumes that captured
quote and its source range, retaining PDF scope, any explicit restriction,
and any newer selection.
Unaccepted failure preserves the original question and quote for retry. These
ownership and acceptance rules are NOUS adaptations; hosted failure behavior
has not been verified.

New native PDF conversations bind the paper durably through the same canonical
thread/message pipeline as the compatibility API. Thread detail and paged
message reads expose `message_branching`, `branch_document_id` and
`selected_message_id`; message rows expose canonical parents and ordered sibling
user-message IDs. Reads project the selected ancestry before pagination.
Continuing a branch page requires its selected-head pin, so a concurrent branch
change cannot splice an older page from a different path into the transcript.
Read authority includes fresh access to the bound PDF.

The native chat store remains the transcript owner. Reload restores its binding
from server metadata and a freshly authorized document query. Pending, failed or
revoked access prevents submission; the route and cached document do not confer
authority. Quotes and explicit page restrictions remain transient turn intent.
The first paper-bound request supplies an explicit null parent; follow-ups use
the selected canonical head. Editing or retrying a user turn supplies that
turn's original parent, preserving sibling alternatives instead of applying
linear supersession. The native alternative controls persist selection through
the existing compatibility selection route, then refresh native history.

Leaving a bound PDF conversation starts a new general chat. Asking about a
different paper starts a new conversation; an existing nonempty linear thread
is not converted into a bound branch thread. Existing linear chats retain their
original edit, retry and pagination behavior. Delayed history, document and
selection results are fenced by account/thread identity and request generation.

Page citations currently navigate to the physical page. Optional upstream
`first`/`last` text anchors are not emitted: a persisted snippet is not proof
of an exact answer-supporting span. No excerpt anchors are fabricated.

The environment-gated `/visual-test/pdf-selection` fixture uses an authored
three-page PDF. A local browser check verified first-page selection,
reverse cross-page selection, page-three citation navigation, submitted
zero-based ranges, clear, document replacement, and preview cancellation.
This verifies the real worker/text-layer interaction independently of a
model provider; it is not full authenticated chat QA.

## Authorization and verification

Native attachment loading releases its scoped read session before parsing
or provider waits. It then rechecks the caller, thread ancestors, document
organization, deletion status and processing readiness before using text.
Integration tools preserve their separate grant checks and transaction
boundaries. See [backend standards](backend.md) and
[API contracts](api-contracts.md).

Focused contracts are exercised by:

- `backend/tests/unit/services/test_agent_pdf_pages.py`
- `backend/tests/unit/api/test_agent_pdf_contract.py`
- `backend/tests/unit/api/test_alphaxiv_compat.py`
- `backend/tests/unit/services/test_alphaxiv_compat.py`
- `backend/tests/unit/middleware/test_alphaxiv_disconnect_signal.py`
- `backend/tests/unit/api/test_alphaxiv_live.py`
- `backend/tests/unit/services/test_alphaxiv_live_replay.py`
- `backend/tests/unit/services/test_alphaxiv_stop.py`
- `backend/tests/unit/services/test_agent_branch_history.py`
- `backend/tests/unit/services/test_alphaxiv_history.py`
- `backend/tests/unit/api/test_alphaxiv_history.py`
- `backend/tests/unit/services/test_native_pdf_branch_reads.py`
- `backend/tests/unit/services/test_native_pdf_branch_reads_postgres.py`
- `frontend/src/hooks/chat/__tests__/usePdfChatBinding.test.tsx`
- `frontend/src/store/__tests__/chat-store-branches.test.ts`
- `frontend/src/components/chat/aui/__tests__/MessageBranchControls.test.tsx`

Unit tests use controlled providers and do not establish live LLM quality,
upstream server parity, or deployment readiness. The feature map remains
`planned` because the registered native PDF scenario does not cover the broader
compatibility lifecycle criteria. Separate manual API probes are recorded in
the [flow evidence](flows/alphaxiv-pdf-chat.md). As defined by the
[verification contract](verification.md), `covered` means mapped scenarios and
checkpoint calls exist; it is not a live `PASS` or a hosted-parity claim.

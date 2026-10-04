# Streaming audit (backend + frontend) — started 2026-08-24
Source: opencode session, 4 explore agents (streaming.py full 3342-line read; services layer stream_buffer/job_store/execution/LangSmith/v1-sse; FE hook+store pipeline useChatStreaming+agentChatStore+agentChatService; FE streamed-content consumers)

## Provenance (retrofitted 2026-08-25)
- session_id: none — audit ran inside a long-lived mixed working session (893 msgs / 884 tool calls / ~5.13M input tokens across unrelated work)
- checkout: PRIMARY working tree /Users/goodwiinz/development/RAG_system on branch fix/audit-pr4-frontend-truth with foreign WIP present
- ref: agents CLAIMED origin/develop but read the mutable primary tree instead
- sha: UNRECORDED — violation; findings cite line numbers against a tree that mutated during the audit window (parent session committed, stashed, checked out mid-audit)
- isolation: NOT ENFORCED — predates scripts/audit/launch_audit.sh
- remediation: re-verify any finding whose lines look shifted before fixing; future audits MUST launch via launch_audit.sh from a fresh session

## Scoped-file manifest
Not captured at audit time. Approximate scope (bytes unrecorded): streaming.py 3342 ln; stream_buffer, job_store, observability, _prompts, run_event_types; useChatStreaming.ts 2394 ln; agentChatStore.ts 1570 ln; agentChatService.ts 829 ln; aui/context-rail components. "Every line audited" claim is DOWNGRADED to partial — four agent outputs were truncated in-transcript and tails were never re-covered.

Scope note: post-R3 churn audited fresh — #1522/#1523/#1547 reasoning+chunks, #1532/#1542 artifact envelope, #1553 queue claim (refuted client-side), #1541 remediations.
Detailed per-finding evidence in agent outputs; this file is canonical.

## HIGH
| ID | Finding |
|----|---------|
| S-H1 | streaming.py:1142–1196 — cancel deadline branch abandons the in-flight graph `__anext__` task alive after terminal CANCELLED/FAILED is committed → destructive tool can still complete post-finalize (done-callback only consumes result) |
| S-H2 | agentChatService.ts:276-278 + useChatStreaming.ts:869-878 + AuiMessage.tsx:529 — `rag_context` frame stored verbatim, `.map` unguarded → malformed frame = TypeError mid-render (deprecated slice guards `?? []`; live path regressed it) |

## MED-HIGH
| ID | Finding |
|----|---------|
| S-MH1 | streaming.py:1394-1421 + submission_service:165 — idempotent replay key omits thread_id → retry addressed at owned thread B replays thread A's stream; B gets no turn |
| S-MH2 | useChatStreaming.ts five teardowns (:995,1039,1132,1251,2312) omit streamingElapsedMs/streamingPhase/streamingStatusDetail/isRetrievingRag → stale "searching" indicator + phase text bleed into next turn (confirm-start never seeds isRetrievingRag) |

## MEDIUM
| ID | Finding |
|----|---------|
| S-M1 | tool_end.result shipped to browser + 1h Redis replay buffer UN-redacted while tool_start.args is redacted (streaming.py:1889-1896,2963-2970; durable sibling ToolCompletedPayload.result_preview same) — emails/tokens/PG URLs in results |
| S-M2 | observability.py:137-141 — LANGCHAIN_HIDE_INPUTS guard is setdefault-only; injected false value wins → prod uploads full prompts/chunks/args |
| S-M3 | internal LLM calls renamed-not-excluded (#1538); cost attribution sums every on_chat_model_end incl. internal gpt-5-mini under user-facing model label → distorted spend |
| S-M4 | legacy rag_context frames bypass payload-byte bounds (≤3000 chars × contexts); buffer cap is frame-count only → unbounded Redis memory amplification (streaming.py:1900-1910, stream_buffer.py:58-76) |
| S-M5 | REFLECTION_COMPLETED + RETRIEVAL_CONTEXT ledger events have ZERO producers (#1547/#1523 spec gap); real "reasoning" = raw plan_reasoning + UNBOUNDED reflection issues[] straight to wire+buffer (streaming.py:1912-1954, reflection.py:42-46) |
| S-M6 | SSE-executed runs hold no lease/no updated_at heartbeat; sweep safety = 1800s margin vs ~300s turns — config drift ⇒ sweeper kills live run, frees active-thread slot mid-stream, absorbing-terminal discards real completion (agent_run_tasks.py:277-442) |
| S-M7 | confirm/resume overwrites active-stream pointer and resets seq=1; reconnecting client with original high Last-Event-ID silently skips early confirm frames unless opt-in ?stream= (streaming.py:2769,1642; stream_buffer.py:50) |
| S-M8 | non-canonical persist mode emits done BEFORE background assistant-row write; failure logged only → reload shows user row with no answer (streaming.py:2146-2158; default flag off) |
| S-M9 | accepted-but-never-dispatched run: stream_id_for_run None forever → every retry burns 5s then INTERNAL error; orphan row holds thread slot via _ensure_thread_idle (streaming.py:1399-1413) |
| S-M10 | unguarded `_finalize_run(AWAITING_CONFIRMATION)` after CONFIRMATION terminal frame — raise appends ERROR after terminal + races partial park (streaming.py:2048-2055,3052-3058,2318-2324) |
| S-M11 | dedicated AsyncSessionLocal pinned for entire stream life (300s+/replay loops) → pool exhaustion under concurrency (streaming.py:1301,2385,2448,3341) |
| S-M12 | fast-path: persist/checkpointer/memory-store/graph-compile/resync/aupdate_state all OUTSIDE asyncio.timeout scope (streaming.py:497-572) |
| S-M13 | project-memory recall query has no org/user filter; tenant safety rests solely on call-order invariant from _resolve_and_bind_project (streaming.py:1556-1566; project_memory_service.py:38-43) — fragile |
| S-M14 | per-frame buffer append failures swallowed silently → undetectable gaps in resumable ledger (streaming.py:951-955); confirm emitter starts buffer without run_id → resumed runs not addressable by stream_id_for_run |
| S-M15 | job_store.get_job_fresh/get_job degrade Redis outage to None → /confirm + poll return 404 "Job not found" instead of 503 (job_store.py:593-629; execute.py:643,576) |
| S-M16 | FE: token frame without content gate → `assistantContent += undefined` injects literal "undefined" into answer AND committed message (agentChatService.ts:267-269; consumers :750,:1966, store :251) |
| S-M17 | FE: malformed tool_start/tool_end drops step silently (toolLabel(undefined) throws inside dispatch try) → activity strip loses tools vs transcript (agentChatService.ts:270-274; toolLabels.ts:45-51) |
| S-M18 | FE: stop pressed during pre-stream window swallowed (stopTargetRef stamps previous owner; abortController null during thread-create await) → turn proceeds despite Stop (useChatStreaming.ts:1537 vs :604) |
| S-M19 | FE: 'running' tool steps committed unresolved on stop/error (no done frame) → perpetual spinner bubble; widget store settles these, cloud path doesn't (useChatStreaming.ts:1076,1092-1103) |
| S-M20 | FE: resume commit shows suffix-only answer if terminal refreshMessages fails — freshness stays stale, truncated overlay persists indefinitely (useChatStreaming.ts:1618-1689,1147-1163) |
| S-M21 | FE: draft-status poll never stops on error incl. normal 404 ("never generated") → infinite 3s polling for every mounted rail (useDraftGenerationStatus.ts:37-39); poll errors invisible; failed cancel = floating promise (DraftStatusPanel.tsx:43-47,126-128) |
| S-M22 | FE: reasoning panel index-keys + ring cap 16 → every new tick re-animates ALL rows past 16 steps; aria-live polite re-announces growing list each frame (reasoning-panel.tsx:70; useChatStreaming.ts:134-146; AuiMessage.tsx:466-476); retrieval chunks live-region announces full snippets (AuiMessage.tsx:563-573) |
| S-M23 | v1 threads SSE errors return str(exc) internals (stream_service.py:207,295,338,374 + stream.py:198) |
| S-M24 | v1 threads: 409 concurrency check BEFORE ownership check on process-global set = cross-tenant activity oracle (stream.py:107-111); disconnect drops collected partials (stream.py:158-165) |

## LOW
| ID | Finding |
|----|---------|
| S-L1 | x-request-id header spoofed → echoed as trace_id across envelopes/ledger/LangSmith metadata (streaming.py:205-218) |
| S-L2 | fast-path except bypasses _stream_failure_category (missing-user-msg → internal not invalid_request) (streaming.py:616-634) |
| S-L3 | malformed-adapter coroutine closed unawaited (streaming.py:2549-2551) |
| S-L4 | duplicate persist_partial_stop init dead (streaming.py:2454,2465) |
| S-L5 | comment contract drift: parked thread requires explicit /cancel, not supersede (streaming.py:2044-2047) |
| S-L6 | /stream/confirm lacks rate limiter while /stream has one (execute.py:766-787 vs :741) |
| S-L7 | keepalive elapsed_ms uses wall clock vs monotonic elsewhere (streaming.py:1248) |
| S-L8 | multi-interrupt keeps only FIRST interrupt value on approval card (streaming.py:2029-2034,3034-3039) |
| S-L9 | rag_context live top-3 vs persisted citations ≤5 mismatch (streaming.py:1907 vs :2116) |
| S-L10 | stream_buffer.read_after corrupt-entry json.loads propagates → replay bricked till TTL (stream_buffer.py:99-105) |
| S-L11 | job_store CAS mutates shared cached dict in place; LRU recency not refreshed on overwrite (job_store.py:446-453) |
| S-L12 | FE: JSON.parse fail drops frames silently, no counter/gap signal (agentChatService.ts:332-334) |
| S-L13 | FE: no Content-Type validation pre-parse (proxy HTML page → generic incomplete-stream) (agentChatService.ts:610,244) |
| S-L14 | FE: framing strictness — multi-line data:, `data:{`, eventType clear-after-dispatch (agentChatService.ts:369-385) |
| S-L15 | FE: TextDecoder final flush missing → split multibyte tail dropped (agentChatService.ts:250,368) |
| S-L16 | FE: activity store Step:'active' never settles on terminal finishRun (agentActivityStore.ts:223-242) |
| S-L17 | FE: deprecated streamingSlice call load-bearing in hot stop path (useChatStreaming.ts:1583-1585) |
| S-L18 | FE: durable fallback duplicates server rows visible on reload (agentChatStore.ts:488-546) |
| S-L19 | FE: seq rAF not cancelled at terminal; confirm finally cancels shared token rAF unconditionally (asymmetry) (useChatStreaming.ts:790-806,2303-2306) |
| S-L20 | FE: untracked setTimeout(0) dispatches in slash/composer actions; floating handleSubmit promises (useSlashCommands.ts:322; useChatComposerActions.ts:156,185,203) |
| S-L21 | FE: pendingConfirmations + streamIdByThreadRef maps unbounded for mount lifetime (useChatStreaming.ts:348,401) |
| S-L22 | FE: sidebar messageCount closure drift (dep is length number) (useChatStreaming.ts:1279) |
| S-L23 | FE: exhausted resume retries strand live run until navigation (resumeTriedRef consumed) (useChatStreaming.ts:1651-1687) |
| S-L24 | FE: ToolStrip chip keys by label string → duplicate keys on repeated tool (ToolStrip.tsx:108-113) |
| S-L25 | FE: fresh-turn seed omits streamingCitations reset (parity gap w/ deprecated slice) (useChatStreaming.ts:706-719) |
| S-L26 | FE: fence handling — CitationRenderer trimStart misclassifies indented fences; completeStreamingMarkdown counts col-0 ``` only (4-backtick/indented skew) (CitationRenderer.tsx:44; markdown-utils.ts:33) |
| S-L27 | FE: DraftStatusPanel trusts backend progress blindly (undefined% badge, >100 width) (DraftStatusPanel.tsx:63,75,83) |
| S-L28 | FE: widget AgentMessageItem streams raw partial markdown without completeStreamingMarkdown (incomplete-fence hazard alive in widget) (AgentMessageItem.tsx:120-123); AgentMarkdownRenderer inline-prop drift under react-markdown ^10 (AgentMarkdownRenderer.tsx:87-99) |
| S-L29 | FE: AgentActivityPanel maps error→done checkmark (rail vs transcript disagree) (AgentActivityPanel.tsx:14-19) |
| S-L30 | FE: reflection-revision keeps prior attempt citations/tools (store policy inconsistent w/ durable fallback which clears) (agentChatStore.ts:370-382 vs :553-567) |
| S-L31 | FE: mypy-class — none; INFO: single-flight Stop shown cross-thread documented CX5 tradeoff (ChatSurface.tsx:133) |

## Status ledger (2026-08-27 re-verification against develop 23c3551a3)
Fix wave #1557–#1576 closed both HIGHs and most MEDs. Re-verified line-by-line (audit provenance was flawed, so every verdict below is against current develop):

- FIXED by wave: S-H1 (finally cancels pending `__anext__`), S-H2 (#1569), S-MH1 (#1570), S-MH2 (#1564), S-M1 (#1567), S-M2/M6 (#1560), S-M4 (#1559), S-M7/M8 (#1572), S-M9 (#1570), S-M10 (#1558), S-M13 (#1568), S-M14 both halves (#1557 + append-failure logging), S-M16 (#1565), S-M17/M19 (#1562), S-M22 (#1563), S-M23 (#1561), S-M24 (v1 threads SSE route deleted in #1552), S-L16, S-L24, S-L29.
- REFUTED on re-verify: S-L3 (`coroutine.close()` is correct disposal), S-L5 (supersede now real via `abandon_awaiting_submission`), S-L22 (dep matches closure read), S-M21 + S-L27 (`useDraftGenerationStatus.ts`/`DraftStatusPanel.tsx` do not exist in this repo or its history — audited a foreign checkout).
- PARTIAL: S-M5 (payload byte-clamped by #1559 but item counts unbounded; REFLECTION_COMPLETED/RETRIEVAL_CONTEXT still producer-less), S-M15 (confirm/poll now PG-fallback via #1571; job_store still degrades Redis outage to None), S-M20 (gap now surfaces toast + invariant warn; no retry), S-L12 (parse failures logged, no counter), S-L18 (client dup fixed; server-row reconcile open), S-L21 (pendingConfirmations pruned; streamIdByThreadRef unbounded), S-L25 (teardown resets cover it; seed still omits citations), S-L26 (CitationRenderer half refuted — now AST plugin; markdown-utils col-0 count remains).
- FIXED 2026-08-27 (this ledger's PR): S-L1 (x-request-id sanitized to token charset else fresh UUID), S-L2 (fast-path uses shared sentinel + `_stream_failure_category`), S-L4 (dead dup init removed), S-L6 (/stream/confirm rate-limited, own `agent_stream_confirm` bucket), S-L7 (keepalive elapsed_ms monotonic since stream start), S-L10 (corrupt buffer entry skipped with warning instead of bricking replay), S-L11 (CAS copies dict + move_to_end).
- STILL OPEN (backend): S-M3 (cost attribution counts internal LLM calls under user-facing model — `_is_user_facing_token_event` never applied to usage), S-M11 (AsyncSessionLocal pinned for full stream incl. 600×1s replay polls), S-M12 (fast-path persist/finalize outside asyncio.timeout), S-L8 (first interrupt only on approval card), S-L9 (live top-3 vs persisted ≤5 citations).
- STILL OPEN (frontend): S-M18 (Stop during pre-stream window swallowed — stopTargetRef stamps previous owner), S-L13 (no Content-Type check), S-L14 (SSE framing strictness), S-L15 (TextDecoder final flush missing), S-L17 (deprecated streamingSlice load-bearing in stop path), S-L19 (seq rAF only cancelled on unmount; confirm cancels shared rAF unguarded), S-L20 (untracked setTimeout/floating handleSubmit), S-L23 (exhausted resume retries strand run until navigation), S-L28 (widget streams raw partial markdown; react-markdown v10 `inline` prop drift), S-L30 (revising keeps prior attempt citations/plan/tools).

## Log
- 2026-08-24: audit created from 4 explore-agent sweeps. Totals: 4 HIGH/MH + 24 MED + 31 LOW/INFO = 59 findings. Zero str(e)/internal leaks in main SSE path (client_safe_error funnel verified); authz/tenant/checkpoint R1-H3 fix verified intact end-to-end; write-before-yield enforced on all terminals; HITL triple-claim solid.
- Refuted during audit: "#1553 queue exists client-side" — no queue implemented (S-M16 adjacent UX gap instead). R5-M18-style double-stream claim EXISTS for chat via accept-time idle-check + partial unique index.
- Cross-refs: S-M1 pairs with durable ToolCompletedPayload.result_preview; S-M14 second half = confirm emitter missing run_id binding.

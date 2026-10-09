# Agent audit round 3 — detailed findings — 2026-08-18
Source: opencode session, 3 explore agents (SSE/streaming layer, agent-chat components + agentChatStore, chat store/hooks)
Status ledger: ~/.audit-ledgers/rag/agent-audit-round3.md

---

## HIGH

### R3-H1 — pendingConfirmation(null) clears wrong thread's key (useChatStreaming.ts:376-389)
Clear path resolves key as `next?.workspaceThreadId || activeThreadId`. When `next === null` it uses the currently-viewed thread, not the confirmation's owner. Trigger: approve confirmation on thread A, switch to thread B mid-confirm (explicitly supported — `isConfirmDisplayed` guards exist for exactly this), confirm stream finishes, `finally` (1921-1923) calls `setPendingConfirmation(null)` → deletes key for B (no-op) → `pendingConfirmations[A]` survives forever. Return to A → settled approval card re-renders (effect 1970-1997 re-adds it), composer locks (`ChatSurface.tsx:144` `isBusy = ... || !!activeConfirmation`), Approve re-fires `streamConfirm` on consumed interrupt → server error or double execution. Same defect via handleStop paths (1338, 1345). Fix: capture `workspaceThreadId` before nulling.

### R3-H2 — runStreamTurn finally clobbers live confirm-stream state (useChatStreaming.ts:1049-1061, 860-867)
Confirmation-paused exit: state cleared (860-866), then `await reconcileUser('confirmation-paused')` (867 — network call = macrotask yield window), then `finally` unconditionally sets `isStreaming:false, streamingContent:'', streamingCitations:[], streamingThreadId:null`. During that await the approval card is rendered and clickable — user clicks Approve → `handleConfirmation` sets `isStreaming:true, streamingThreadId, streamingCitations: carriedCitations` (1566-1574) → confirm stream goes live → runStreamTurn's finally then fires and wipes it. Confirm stream orphaned (streaming UI unmounted, carried citations lost), composer unlocked → user submits second turn → two concurrent SSE writers racing global streaming state on the same thread. submitLock doesn't help (released only in that same finally; confirm path bypasses by design). Fix: scope finally-wipe to "no newer stream owner" (only clear when streamingThreadId still matches this turn's thread).

### R3-H3 — regenerate never sends supersedes_client_message_id (useChatComposerActions.ts:136-142)
`handleRegenerate` calls `handleSubmit(contentToSend, regenerationHistory)` — 2 args, no supersedes — even when `priorUser.clientMessageId` exists on the canonical row. Edit-and-resend passes it (165-171; test contract useChatStreaming.editResend.test.ts confirms tombstone semantics). Regenerated turn persists server-side as brand-new user+assistant pair; old turn not tombstoned → `reconcileAssistant` refresh makes freshness 'fresh' → canonical page renders BOTH turns → duplicated transcript immediately and on every reload; future server-side history carries both copies into agent context.

### R3-H4 — onToolEnd drops is_error (agentChatStore.ts:239-258, 701-725)
Service passes 3rd arg `Boolean(data.is_error)` (agentChatService.ts:217); store handler signature `(tool, result)` ignores it, unconditionally sets `execs[actualIdx].status = 'completed'` and parses result. Failed tool renders green check; error payload shown as success summary; `planMapping.deriveStepStatus` marks failed plan step `completed`. User believes failed ingest/draft succeeded. VERIFIED in code.

### R3-H5 — mid-stream throw → fallback re-pushes same placeholderId (agentChatStore.ts:409-437)
SSE transport throw after tokens streamed falls back to durable run; placeholder removal (413-421) only when content empty, then fallback pushes NEW message with the SAME placeholderId (429-437). Duplicate React keys in AgentMessageList (`key={msg.id}`); polling `findIndex` writes into first (partial) bubble; second bubble stuck `isStreaming:true` forever — permanent empty cursor bubble + reconciliation weirdness.

### R3-H6 — confirm on old card aborts new turn (agentChatStore.ts:595-654 + AgentMessageList.tsx:115-129)
HITL race: input enabled while confirmation pending (`isStreaming` false); new send starts generation; old card still rendered; approving runs `confirmAction` which aborts the new turn's controller (612-615). Aborted stream resolves silently (agentChatService.ts:316, 522 — no onError when aborted); new turn's onDone/onError blocked by identity guard (194-196) → its placeholder stuck `isStreaming:true` permanently. Answer old confirmation mid-new-turn = new turn killed, stranded blinking-cursor bubble, response lost.

### R3-H7 — no dead-stream watchdog, no resume retry/backoff (useChatStreaming.ts:1392-1405 + agentChatService.ts:188-322)
`consumeSse` loops on `reader.read()` forever; nothing client-side times out a silent stream. Backend resume path emits no heartbeats during silent gaps (round-1 M13), so a proxy kills the idle resume connection ~30s. On kill, `resumeStream` returns `{status:'failed'}`, the effect throws, user gets hard error bubble — one shot, `resumeTriedRef` blocks any retry this activation, no backoff anywhere in the layer. Refresh page during long silent planner phase → run permanently lost client-side with "Something went wrong" despite backend still running and buffer holding everything.

### R3-H8 — transport exception never finishes activity-store run (useChatStreaming.ts:1021-1061)
Catch/finally reset useChatStore streaming flags but never call `useAgentActivityStore.finishRun(...)` (only onError/onDone callbacks do). A thrown transport error (resume 'failed', network drop, non-abort fetch rejection rethrown at agentChatService.ts:507/317) leaves `run.state === 'running'` forever. Activity rail spinner never stops; combined with `resumeTriedRef` (1380), failed resume leaves thread stuck until navigation away/back (activation-change effect 396-404 clears the ref). For live submits the stuck 'running' state is the only thing that accidentally re-triggers resume via the `messages` dep — recovery accidental, not designed.

---

## MEDIUM

### R3-M1 — new-chat handoff bounce-back wipes live overlay (useChatSession.ts:817-869)
Submit in new chat → optimistic setMessages (thread creation await begins, `localMessagesThreadIdRef` still null) → user clicks sidebar thread B during createThread round trip → parking effect run 1: outgoing null + optimistic present → `isNewThreadHandoff` true → overlay kept, attributed to B → createThread resolves → `setCurrentThread(newThread)` → runStreamTurn sync prefix re-writes overlay + streaming placeholder → parking effect run 2 (active=newThread, outgoing=B): B isn't streamingThreadId, outgoing ≠ null so no handoff, no parked entry for newThread → `setMessages([])`. In-flight turn invisible for the entire stream; self-heals only at done/error commit. Composer blocked so can't retry.

### R3-M2 — handleStop: failed cancel resets stoppedByUserRef post-abort (useChatStreaming.ts:1311-1346, 1851-1862)
Order: `stoppedByUserRef=true` → `abort()` (1321) → `cancelPendingConfirmation`. If cancel API rejects (graph already resumed → 409), catch (1340) sets `stoppedByUserRef.current = false`. Aborted confirm stream then resolves; partial-commit block (1851-1862) checks the now-false flag → partial confirmed answer NOT committed locally; bubble vanishes despite user watching it stream. Toast says "Could not stop this action" while action was in fact stopped client-side.

### R3-M3 — confirmAction poll-budget exhaustion falls off loop end (agentChatStore.ts:883-944, 951-1026)
Both `for` loops `return` on every terminal branch, no post-loop handling. After 200×3s (durable) or 120×1.5s (legacy), execution falls off the loop with `isConfirming: true`, `isStreaming: true`, `_abortController` still set. Slow/queued job > 10 min → spinner forever, composer locked, Stop aborts dead controller. Contrast sendMessage's durable loop which has explicit timeout branch (558-574).

### R3-M4 — no buffer-trim gap detection on resume (agentChatService.ts:534-593)
Client passes `after=afterSeq`, replays whatever arrives. Backend (stream_buffer.py:90-106) silently replays from earliest surviving frame when the cursor was trimmed (5000-frame ltrim). Client never checks first replayed frame's `seq === afterSeq + 1`. Very long run, cursor older than 5000 frames → resumed turn rebuilds assistantContent from replayed tokens only → committed message silently missing prefix; no signal, wrong answer persisted to UI (server rows unaffected). Compounds round-1 L14.

### R3-M5 — live-turn transport failure renders permanent error bubble then recovery appends answer (useChatStreaming.ts:1024-1043, 1389)
Catch adds local-only "Something went wrong" bubble via setMessages; the `messages` dep re-runs resume effect, which (because of R3-H8, run still 'running') auto-resumes from seq cursor and commits final answer — appended AFTER the error bubble (resume's newMessages snapshot includes it, 1389). Transient blip mid-stream → user sees both error bubble and recovered answer; error bubble persists until reload.

### R3-M6 — clearMessages doesn't abort in-flight generation (agentChatStore.ts:1100-1108 vs newThread:1119-1122)
Clear during stream: generation keeps ownership; `onTrace` (203) re-sets `activeThreadId` after clear; `onConfirmation` re-creates card on wiped chat; next send silently continues the "cleared" thread.

### R3-M7 — error/stop paths leave toolExecutions 'running' + stale plan (agentChatStore.ts:385-405, 1054-1075)
Neither onError nor stopGeneration finalizes toolExecutions stuck at 'running' (orphan tool_start spinner forever), neither clears currentPlan, onError doesn't null _abortController. Dead plan with pending steps until next turn.

### R3-M8 — durable-path outer catch never cleans placeholder (agentChatStore.ts:575-592)
Outer catch appends error message but never cleans the durable-path placeholder (pushed 429-437). startDurableRun/getDurableRunStatus throws → placeholder bubble left isStreaming:true forever alongside error bubble.

### R3-M9 — scroll snap-to-bottom every token (AgentMessageList.tsx:30-38)
Scroll effect deps include `latestContent` → scrollTo bottom on every token, no user-scroll-up detection. Auto-scroll fights user reading earlier text during streaming.

### R3-M10 — unmemoized citation parse per token (AgentMarkdownRenderer.tsx:215, 243-250)
`parseMessageWithCitations(content)` unmemoized, full regex parse per token; citation mode builds new components object per render. O(n²) re-parse + full markdown re-render per token on long answers; jank on low-end devices.

### R3-M11 — citation pattern matches any bare [N] (citationParser.ts:44-45 + AgentMarkdownRenderer.tsx:219-238)
CITATION_ITEM_PATTERN matches ANY bare bracketed number (`[1]`, `[2023]`, `arr[0]`, markdown link refs). Incidental bracketed numbers in assistant text: content mangled into citation badges; out-of-range → inert grey badge replacing text; coincidental in-range → badge links WRONG document.

### R3-M12 — planMapping correlates by toolName only (planMapping.ts:13)
No step instance/count matching. Plan with same tool in 2 steps → both marked completed after first execution. Common: 2× search_documents.

### R3-M13 — legacy confirmAction fallback sends thread_id as job_id (agentChatStore.ts:947, 344)
SSE-origin `jobId` = thread_id (set at 344); legacy fallback confirmAction POSTs it to `/agent/confirm/{job_id}` which expects a job UUID → 404 → catch restores card → retry loops. Confirmation permanently unresolvable via fallback.

### R3-M14 — thread/message load errors console-only (agentChatStore.ts:1235-1246, 1178-1184)
loadThreadMessages/loadThreads failures log to console only, no error state. Failed thread load renders fresh empty state ("AI Research Agent" greeting / "No conversations yet") for an existing thread — user thinks thread lost.

### R3-M15 — 401 on stream open categorized invalid_request, no retry (agentChatService.ts:34-38)
`httpFailureCategory(401)` falls in 4xx arm; getStreamAuthHeaders reads session once per stream start. Expired/just-refreshed token boundary → 401 → "Stream failed (401)" tagged invalid_request (UI treats as user's fault) instead of re-auth/retry. Supabase getSession usually auto-refreshes so window narrow, but the one status a retry would fix gets none.

---

## LOW

### R3-L1 — exception path leaves token rAF un-cancelled (useChatStreaming.ts:1021-1061 vs 837-842)
All non-throw paths cancel streamingRafRef + null pendingStreamContentRef; catch/finally never does. Token arriving just before thrown exception schedules a rAF that fires after finally and writes stale streamingContent post-turn. (Confirm path does it at 1931-1935 with a comment naming this hazard.)

### R3-L2 — HITL cold-load probe not invalidated by new submit (useChatStreaming.ts:1428-1475)
Probe in flight, user sends new turn (probe has no abort hook on submit; probeAbortRef only aborted by later thread activation or unmount). Probe's onConfirmation lands mid-turn and arms stale gate for an interrupt the new send abandoned server-side. Phantom approval card + locked composer; confirming it → streamConfirm errors → confirmFailed → finally (1921-1923) retains gate → stuck lock requiring Stop.

### R3-L3 — confirm catch-path error bubble lacks error block (useChatStreaming.ts:1877-1891 vs 1821-1842)
Transport-failure confirmations commit plain content bubble, no `error: {message, category}`, no Retry affordance. onError path was explicitly fixed to carry category; catch path wasn't.

### R3-L4 — deleteThread during live stream leaks (threadSlice.ts:173-198 + useChatSession.ts:817)
deleteThread doesn't stop a stream owned by the deleted thread. Orphaned runStreamTurn completion calls refreshMessages(deletedThreadId) with no thread-existence check → re-creates messages[id], pagination, freshness, messageToThread entries for deleted thread. parkedMessagesRef entry never pruned. Bounded by FIFO eviction but reverse-index pollution until then.

### R3-L5 — unmount drops pending seq cursor (useChatStreaming.ts:474-490)
Cleanup cancels seqRafRef but discards pendingSeqRef — last frames before unmount never reach setStreamSeq. rAF batching means backgrounded tab (rAF paused) lags cursor arbitrarily. Replay after remount re-sends rendered frames; content rebuilt (no duplication) but replay cost grows; interacts with R3-M4 (larger replay window).

### R3-L6 — finally omits streamingSteps reset (useChatStreaming.ts:1052-1057)
All normal-exit paths reset it (860-866, 902-908, 983-989); catch/finally doesn't. Stale streamingSteps until next turn if rendered ungated.

### R3-L7 — cold-thread probe attaches to live runs (useChatStreaming.ts:1448-1468)
Probe passes only onConfirmation; if thread has live run server-side, probe holds SSE open consuming the whole run, dropping tokens/seq. Background connection churn + wasted server buffering; no user-visible corruption.

### R3-L8 — same-tool parallel calls corrupt durationMs (useChatStreaming.ts:599, 706, 731-732)
toolStartTimes keyed by tool name; parallel invocations overwrite start time. Pairing survives (reverse-find) but durationMs wrong for earlier invocation. Cosmetic.

### R3-L9 — deprecated v2 client: no tail flush (streamingService.ts:189-215)
Drops final unterminated frame (unlike consumeSse:309-314), drops `data:` without space, any frame whose data precedes event. Orphaned (sole importer is dead useChatStore.streamMessage) — hazard only if resurrected.

### R3-L10 — uiMode captured at send start (agentChatStore.ts:121, 379)
onDone hasUnread check uses stale value. Close panel mid-stream → completion never sets unread badge.

### R3-L11 — context chips never sent (useProjectChatWidget.ts:171)
documents/notes/bibliography toggles are TODO — dead UI controls, all project docs always included. Whole chat-widget tree is dead code (zero production mounts); if ever mounted alongside GlobalAgentChat both FABs overlap at fixed bottom-6 right-6.

### R3-L12 — startChatFromProject fetch no AbortController (useProjectChatWidget.ts:153-181)
Unmount/clear discards response via requestIdRef but request continues server-side; thread + tokens created for abandoned session.

### R3-L13 — tool exec id te-${Date.now()} (agentChatStore.ts:227, 689)
Two tool_start events same millisecond → duplicate ids → duplicate React keys in AgentMessageItem groups.

### R3-L14 — running tool group renders NO icon (AgentMessageItem.tsx:244-248)
Ternary `anyFailed ? X : allCompleted ? Check : null` — running batch renders blank where spinner expected.

### R3-L15 — copy setTimeout without cleanup (AgentMessageItem.tsx:54-58, AgentMarkdownRenderer.tsx:47-51)
setState after unmount; benign in React 18, timer leak.

### R3-L16 — citation badge unreachable on touch/keyboard (AgentCitationBadge.tsx:33-54)
Button with cursor-pointer + hover-only interaction, no click handler. Touch users cannot open preview; button announces interactive but does nothing.

### R3-L17 — Escape in ConfirmationCard closes whole panel (ConfirmationCard.tsx:72-74 + GlobalAgentChat.tsx:116-118)
Handler doesn't stopPropagation; bubbles to document listener. Escape during HITL = cancel confirmation AND close chat panel simultaneously.

---

## Verified NOT buggy (round 3)

- **XSS**: AgentMarkdownRenderer uses react-markdown without rehype-raw/dangerouslySetInnerHTML; default urlTransform strips javascript: URLs; links get noopener noreferrer. Tool results via JSON.stringify in <pre>. ChatMessageItem plain-text escaped.
- **SSE parsing**: CRLF-safe, chunk-split-safe (buffer + pop), mid-JSON safe, blank-line event reset correct for sse-starlette framing, unknown events ignored by design (contract-tested), malformed JSON warned + skipped, unterminated-tail flush present (consumeSse).
- **No EventSource anywhere** — fetch + Authorization header (correct for auth).
- **Reader lock released** on all paths (finally releaseLock); abort swallowed cleanly.
- **done-frame double-render**: finishRun on every terminal branch prevents resume-after-done; streamId pinning blocks stale-cursor-onto-newer-run.
- **Replay dedup**: server-side after filter + client rebuild — no double-apply given untrimmed buffer.
- **Terminal job states exhaustive** — poller stops on error/cancelled.
- **Unmount aborts in-flight SSE**; ownership identity checks (isCurrentGeneration, isTurnDisplayed) prevent cross-thread writes throughout.
- **Thread-switch bleed regression (round-1-class)**: every await point verified — URL fetch-detail guarded, warm-start double-guarded, requestCoordinator generations/tokens correct in refreshMessages/loadOlderMessages/loadThreads/loadConversations. (New holes are R3-M1 parking path and R3-H1 HITL variant.)
- **Stale closures**: no setInterval leaks; init watchdog cleared on settle/unmount/skip.
- **Duplicate keys**: runtimeId = client_message_id contract (uuidv5 assistant derivation) holds; selectDisplayedMessages dedup + freshness gating sound; addMessageToStore idempotent. (Duplicate risk is server-side: R3-H3.)
- **Persist**: only ids + sidebarCollapsed — no stale freshness/streaming rehydration.
- **Eviction**: MAX_CACHED_THREADS=50 FIFO with current+refreshing thread protected, reverse-index cleaned.
- **Double-confirm**: isConfirming set synchronously before first await → buttons disabled before second click.
- **Focus traps** in AgentPanel/ChatPanel: Tab wrap correct, no trap without Escape handling.
- **Citation bounds**: getCitationByIndex undefined out-of-range → inert badge, no crash.
- **List keys**: stable ids, no index-as-key.
- **Thread-load races**: epoch token + loadThreadsToken + activeThreadId re-check correct.

# Agent audit round 3 — started 2026-08-18
Source: opencode session, 3 explore agents (SSE/streaming service layer, agent-chat components + agentChatStore, chat store/hooks)
Detailed findings: ~/.audit-ledgers/rag/agent-audit-round3-details.md

| ID | Finding (one line) | Sev | Status | Owner | PR | Updated |
|----|--------------------|-----|--------|-------|----|---------|
| R3-H1 | pendingConfirmation(null) clears CURRENT thread's key, not owner's → stale approval card re-arms, composer locks, re-confirm on consumed interrupt (useChatStreaming.ts:376-389) | high | hardened | claude | #1469 | 08-18 |
| R3-H2 | runStreamTurn finally clobbers live confirm-stream state (await reconcile window) → orphaned confirm stream, two concurrent SSE writers (useChatStreaming.ts:1049-1061, 860-867) | high | fixed | claude | #1469 | 08-18 |
| R3-H3 | handleRegenerate never passes supersedes_client_message_id → old turn not tombstoned → duplicated transcript on reconcile + reload + agent context (useChatComposerActions.ts:136-142) | high | fixed | claude | #1467 | 08-18 |
| R3-H4 | onToolEnd drops is_error arg → failed tool renders green check, plan step marked completed (agentChatStore.ts:239-258; service passes it at agentChatService.ts:217) | high | fixed | claude | #1467 | 08-18 |
| R3-H5 | mid-stream transport throw → fallback pushes NEW message with same placeholderId → duplicate React keys, second bubble stuck isStreaming forever (agentChatStore.ts:409-437) | high | fixed | claude | #1470 | 08-18 |
| R3-H6 | confirm on old card aborts new turn's controller → new turn's onError/onDone blocked by identity guard → placeholder stuck streaming forever (agentChatStore.ts:595-654) | high | fixed | claude | #1470 | 08-18 |
| R3-H7 | no dead-stream watchdog / no resume retry+backoff → proxy-killed resume = permanent "Something went wrong" though backend still running (useChatStreaming.ts:1392-1405, agentChatService.ts:188-322; compounding backend M13) | high | fixed | claude | #1473 | 08-18 |
| R3-H8 | transport exception never calls finishRun → activity spinner stuck 'running'; recovery only via accidental messages-dep re-trigger (useChatStreaming.ts:1021-1061) | high | fixed | claude | #1469 | 08-18 |
| R3-M1 | new-chat handoff bounce-back: parking effect run 2 setMessages([]) wipes live optimistic overlay for whole stream (useChatSession.ts:817-869) | med | fixed | clawd | #1487 | 08-19 |
| R3-M2 | handleStop: failed cancel API resets stoppedByUserRef after abort → partial confirmed answer not committed, bubble vanishes (useChatStreaming.ts:1311-1346, 1851-1862) | med | refuted | — | — | 08-18 |
| R3-M3 | confirmAction poll-budget exhaustion falls off loop end → isConfirming stuck true, composer locked (agentChatStore.ts:883-944, 951-1026) | med | fixed | claude | #1470 | 08-18 |
| R3-M4 | no buffer-trim gap detection on resume: client never checks seq === after+1 → silently truncated answer persisted (agentChatService.ts:534-593; backend L14) | med | fixed | claude | #1473 | 08-18 |
| R3-M5 | live-turn transport failure renders permanent error bubble, then recovery appends answer after it — both visible until reload (useChatStreaming.ts:1024-1043, 1389) | med | fixed | claude | #1473 | 08-18 |
| R3-M6 | clearMessages doesn't abort in-flight generation → onTrace/onConfirmation re-populate wiped chat, next send continues "cleared" thread (agentChatStore.ts:1100-1108 vs newThread:1119-1122) | med | fixed | claude | #1470 | 08-18 |
| R3-M7 | onError/stopGeneration leave toolExecutions 'running' (orphan spinner) + stale currentPlan; onError doesn't null _abortController (agentChatStore.ts:385-405, 1054-1075) | med | fixed | claude | #1470 | 08-18 |
| R3-M8 | durable-path outer catch never cleans placeholder → stuck streaming bubble + error bubble (agentChatStore.ts:575-592) | med | fixed | claude | #1470 | 08-18 |
| R3-M9 | AgentMessageList scroll deps include latestContent → snap-to-bottom every token, fights user scroll-up (AgentMessageList.tsx:30-38) | med | fixed | claude | #1472 | 08-18 |
| R3-M10 | unmemoized citation parse per token → O(n²) re-render jank on long streams (AgentMarkdownRenderer.tsx:215, 243-250) | med | fixed | claude | #1472 | 08-18 |
| R3-M11 | CITATION_ITEM_PATTERN matches any bare [N] incl arr[0], years → mangled content, wrong-doc badge links (citationParser.ts:44-45, AgentMarkdownRenderer.tsx:219-238) | med | fixed | claude | #1472 | 08-18 |
| R3-M12 | planMapping correlates by toolName only → repeated-tool plans all steps complete after first exec (planMapping.ts:13) | med | fixed | claude | #1467 | 08-18 |
| R3-M13 | legacy confirmAction fallback sends thread_id as job_id → 404 loop, confirmation unresolvable (agentChatStore.ts:947, 344) | med | fixed | claude | #1470 | 08-18 |
| R3-M14 | loadThreadMessages/loadThreads errors console-only → existing thread renders fresh empty state, user thinks thread lost (agentChatStore.ts:1235-1246, 1178-1184) | med | fixed | claude | #1474 | 08-18 |
| R3-M15 | 401 on stream open categorized invalid_request, no auth retry (agentChatService.ts:34-38) | med | fixed | claude | #1473 | 08-18 |
| R3-L1 | exception path leaves token rAF un-cancelled → stale write post-turn (useChatStreaming.ts:1021-1061 vs 837-842) | low | fixed | claude | #1469 | 08-18 |
| R3-L2 | HITL cold-load probe not aborted by new submit → phantom approval card, stuck gate (useChatStreaming.ts:1428-1475) | low | fixed | clawd | #1487 | 08-19 |
| R3-L3 | confirm catch-path error bubble lacks error block/category/retry (useChatStreaming.ts:1877-1891 vs 1821-1842) | low | fixed | claude | #1474 | 08-18 |
| R3-L4 | deleteThread during live stream: orphan refreshMessages re-creates deleted thread cache entries; parkedMessagesRef never pruned (threadSlice.ts:173-198, useChatSession.ts:817) | low | fixed | clawd | #1487 +#1504 | 08-19 |
| R3-L5 | unmount drops pending seq cursor (rAF) → larger replay window on remount (useChatStreaming.ts:474-490) | low | fixed | claude | #1474 | 08-18 |
| R3-L6 | finally omits streamingSteps reset → stale tool-strip if rendered ungated (useChatStreaming.ts:1052-1057) | low | fixed | claude | #1469 | 08-18 |
| R3-L7 | cold-thread confirmation probe attaches to live runs, consumes whole run server-side, discards tokens (useChatStreaming.ts:1448-1468) | low | fixed | clawd | #1487 | 08-19 |
| R3-L8 | same-tool parallel calls corrupt durationMs (useChatStreaming.ts:599, 706, 731-732) | low | fixed | claude | #1474 | 08-18 |
| R3-L9 | deprecated v2 streamingService: no tail flush, drops frame-less data — still live, imported by streamingSlice.ts:64 (streamingService.ts:189-215) | low | fixed | claude | #1474 | 08-18 |
| R3-L10 | uiMode captured at send start → close-panel-mid-stream never sets unread badge (agentChatStore.ts:121, 379) | low | fixed | claude | #1474 | 08-18 |
| R3-L11 | context chips never sent to backend — dead toggles (useProjectChatWidget.ts:171; widget tree currently dead code) | low | deferred | — | — | 08-18 |
| R3-L12 | startChatFromProject fetch has no AbortController (useProjectChatWidget.ts:153-181) | low | deferred | — | — | 08-18 |
| R3-L13 | tool exec id te-${Date.now()} → same-ms duplicate React keys (agentChatStore.ts:227, 689) | low | fixed | claude | #1467 | 08-18 |
| R3-L14 | running tool group renders NO icon (ternary null branch) (AgentMessageItem.tsx:244-248) | low | fixed | claude | #1472 | 08-18 |
| R3-L15 | copy setTimeout without cleanup — setState after unmount, timer leak (AgentMessageItem.tsx:54-58, AgentMarkdownRenderer.tsx:47-51) | low | fixed | claude | #1472 | 08-18 |
| R3-L16 | citation badge: interactive button with no click handler; hover-only, unreachable on touch/keyboard (AgentCitationBadge.tsx:33-54) | low | fixed | claude | #1472 | 08-18 |
| R3-L17 | Escape in ConfirmationCard doesn't stopPropagation → closes whole chat panel too (ConfirmationCard.tsx:72-74, GlobalAgentChat.tsx:116-118) | low | fixed | claude | #1472 | 08-18 |

## Log
- 2026-08-18: round 3 created. 40 findings (8 high, 15 med, 17 low) — the
  original "37" was an arithmetic slip in this line, not a change to the table. Verified-clean: XSS (react-markdown no raw HTML, urlTransform strips javascript:), SSE parsing (CRLF/chunk-split/JSON-safe), no EventSource (fetch+auth header), reader lock released, done-frame double-render blocked, replay dedup, terminal job states exhaustive, thread-switch bleed regression (requestCoordinator generations/tokens sound — R3-M1 is a new parking hole, R3-H1 a HITL variant), double-confirm guarded, eviction FIFO sound, persist ids-only.
- 2026-08-18 (remediation): 33 of 40 findings shipped across six PRs — #1467
  (H4/H3/M12/L13), #1469 (H2/H8/H1/L1/L6), #1470 (H5/H6/M3/M6/M7/M8/M13),
  #1472 (M9/M10/M11/L14–L17), #1473 (H7/M4/M5/M15), #1474 (M14/L3/L5/L8/L9/L10).
  Every fix but H1 has a regression test verified failing without it.
  - **H1 not reproducible** — the async confirm path captures
    `setPendingConfirmation` from its call-time render, so the fallback already
    resolved to the confirmation's own thread; no user path (thread switch
    mid-confirm, handleStop, cold-load probe) strands a settled card. Kept as
    hardening: the owner thread id is now explicit, so the invariant no longer
    depends on closure timing.
  - **M2 refuted as written** — not clearing `stoppedByUserRef` in the failed
    cancel catch leaks the flag into the next turn (which would then commit as
    "stopped" and skip its error rendering), and the branch only runs when no
    confirm stream is in flight. Needs a different fix.
  - **H8 split** — live turns now finish the run as `error` and get one
    deliberate reattach (the recovery that used to happen by accident); a
    failed *resume* deliberately keeps its run `running`, so the "spinner after
    a failed resume" half is covered by H7's retry/backoff instead.
  - **L9 is not dead code** — `store/chat/slices/streamingSlice.ts:64` still
    dynamically imports the deprecated service; its tail flush was fixed rather
    than deleting the module.
  - Still open: M1 (new-chat parking bounce-back), L2/L4/L7 (probe + delete
    semantics), and L11/L12 in the dead project-chat widget tree.

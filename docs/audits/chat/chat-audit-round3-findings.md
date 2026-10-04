# /chat Bug Hunt — Round 3 Findings

**Date:** 2026-07-06
**Method:** Adversarial bug hunt — 5 finder lenses (state/races, SSE contract, persistence/reload, error edges, backend logic) over the current `develop` tip, each finding attacked by 2 independent refuters (a single refute killed it). 24 raw findings → 18 confirmed (6 refuted). One PII finding was double-counted across two lenses → **17 unique findings**.
**Source:** workflow `wf_eadc0d98-c5a`.

**Tally (2026-07-07):** ALL 17 CLOSED — 15 fixed via PRs, 2 (M4/M5) fixed independently on develop.

---

## Security-class — ✅ Fixed (PR #1046)

Both reviewed by code-reviewer **and** the security-review skill; both clean.

| ID | Severity | Finding | Location |
|----|----------|---------|----------|
| **S1** | High | Soft-deleting a conversation or workspace did **not** revoke access to its threads/messages — `delete_conversation`/`delete_workspace` flag only their own row and never cascade. `get_thread` (the shared funnel for create/list/update/delete) + `get_message` now reject a thread under a soft-deleted parent. | `chat_service.py` |
| **S2** | Medium | Agent `/stream` `_resolve_thread` resolved soft-deleted threads (stale tab / SSE retry persisted turns into deleted threads). Now filters `Thread`/`Conversation`/`Workspace` `is_deleted`. | `jobs.py` |
| **S3** | Medium | Confirm/resume stream emitted `tool_start` args verbatim while the main stream redacts — leaked emails/phones from document content over the browser-visible SSE. Both streams now share a redact-first-then-cap helper. | `streaming.py` |

**Security follow-ups:** ✅ per-site fixes on develop + funnel guard incl. get_collection (PR #1061). Originally: `_resolve_thread` create-if-missing workspace pick has no `is_deleted` filter; verify `list_threads → get_conversation` rejects a soft-deleted parent workspace.

---

## Important (correctness)

| ID | Status | Finding | Location |
|----|--------|---------|----------|
| **I1** | ✅ PR #1062 (fetch-window clear; store-canonical refactor remains flagged debt) | Thread-switch transcript **bleed**: double-fetch guard + `storeMessages.length >= localMessages.length` merge rule → a shorter thread B renders thread A's transcript, and the next send streams A's history as B's context. Needs the store-canonical refactor the existing code comment flags (handleSubmit sources history from the local `messages` buffer, so it can't simply be cleared on switch). | `useChatSession.ts:609` |
| **I2** | ✅ PR #1052 | Durable HITL approval **dead path**: `confirmAction` read `pendingConfirmation` after nulling it → `waitTokenId` always undefined → `completeDurableConfirmation` unreachable, durable-run approval mis-routed to the failing legacy endpoint, run hangs forever. | `agentChatStore.ts:726` |
| **I3** | ✅ PR #1053 | Nested HITL confirmation event dropped by the confirm-stream consumer (a 2nd interrupt in one turn dead-ends the UI). | `useChatStreaming.ts:989` |
| **I4** | ✅ PR #1053 | Confirm turns lose **all citations** — the confirm stream never emits `rag_context` and the frontend wipes pre-interrupt citations on the interrupt. | `streaming.py:857` |
| **I5** | ✅ PR #1051 | `POST /api/v2/messages` accepts but **discards** `latency_ms`/`stopped`/`attachment_ids` — legacy-mode envs lose the stopped badge + response time on reload. | `workspaces.py:1890` |
| **I6** | ✅ PR #1053 | Confirm-stream citations never cleared → **leak into the next turn's answer** and are persisted with it. | `useChatStreaming.ts:1101` |
| **I7** | ✅ PR #1050 | Failed thread creation on first send was fully silent — ghost bubble, lost input. Now rolls back the optimistic bubble, restores the composer text, and toasts. | `useChatStreaming.ts:403` |
| **I8** | ✅ PR #1050 | Thread-load failure on switch rendered the empty "start a conversation" welcome state instead of an error. Now toasts. | `useChatSession.ts:633` |

---

## Minor — ✅ Closed (2026-07-07)

| ID | Status | Finding | Location |
|----|--------|---------|----------|
| **M1** | ✅ this PR | HITL turns never call `finishRun` → agent activity stuck "running" after a confirmed action completes. | `useChatStreaming.ts:707` |
| **M2** | ✅ this PR | `tool_start` args emitted as a Python-repr string but typed/consumed as an object → the live args preview never renders (works only after reload). | `streaming.py:448` |
| **M3** | ✅ this PR | Tool-activity strip vanishes when the displayed list flips to store messages (legacy mode). | `cloudMessageView.ts:208` |
| **M4** | ✅ fixed on develop (init watchdog stand-down) | Slow-but-successful init (>15s) leaves the page permanently stuck on the "Connection error" screen — `initError` never reset. | `useChatSession.ts:565` |
| **M5** | ✅ fixed on develop (toasts on rename/delete/bulk) | Thread rename/delete/bulk-delete failures are silent no-ops. | `useChatThreadActions.ts:101` |
| **M6** | ✅ this PR (checkpoint-anchored resume key) | Resumed-turn assistant idempotency key `uuid5(latest user cmid)` can collide with a concurrent turn and silently drop its answer. | `streaming.py:1019` |

---

## Notes

- **Mount facts:** `/chat` = `frontend/app/(dashboard)/chat/page.tsx` (uses `useChatSession` + `useChatStreaming`). `GlobalAgentChat` (`agentChatStore.ts`) is mounted as the dashboard right-panel — not dead code. App dir is `frontend/app`, not `frontend/src/app`.
- **6 findings died in adversarial verification** and are excluded from this list.
- **PR ownership:** #1046, #1050, #1052 (this session); #1051 (loop agent `clawd`, I5); #1053 (`goodwiins`, confirm-stream provenance — subsumes I3/I4/I6).

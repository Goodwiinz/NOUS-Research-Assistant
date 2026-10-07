# GOO-350 account isolation implementation plan

Goal: invalidate account A's private client state and work before account B can use it, preserving same-user refresh.

Spec: [GOO-350](https://linear.app/goodwiinz/issue/GOO-350/data-isolation-clear-private-client-stores-and-cancel-work-on-account), Task 4 of the [October 1 audit](https://linear.app/goodwiinz/document/data-isolation-audit-findings-plan-and-small-issues-2026-10-01-1d5d1e1da573).

Base: `2a45aa50c` (develop). Open PR #1776 contains reusable chat-session guards but also unrelated attachment/history fixes. Reuse only its account-cleanup work and extend project/auth coverage. Merged #1833's browser test remains opt-in; GOO-354 owns its execution and CI activation.

Architecture: existing stores remain the owners of their state. Auth cleanup resets them synchronously, invalidates asynchronous ownership, aborts transports and clears existing workspace/artifact/Query caches. No backend access-policy changes.

## Implementation and verification

- [x] Add real-store regression tests and observe failure on develop: A-only chat, agent, project, linked threads, citations, pipeline and selections; rejected/missing sessions; SDK identity replacement; same-user refresh; deferred A results and stream callbacks after B starts.
- [x] Reuse the chat session reset/guards in `chat-store`, its slices, `useChatStreaming`, `agentChatStore` and `workspaceService`; extend auth profile ownership and cleanup in `authStore`.
- [x] Add an account lifetime in `lib/account-session.ts` for request cancellation and project/citation/pipeline guards. Guard success, error, finally and follow-up calls, including delayed auth initialization and retried requests.
- [x] Run focused tests, remove each new race guard temporarily to prove failure, restore exactly, and record commands/results in `docs/testing/` (41 guards; October 3, 2026).
- [x] Run pinned Node 24 / pnpm 10.18.2 frontend CI wrapper, unit suite, coverage floor and type checks. Obtain independent code review and address findings (October 3, 2026: 365 files / 2,728 tests pass, 41 mutations pass, frontend gates pass; backend wrapper dependency limits are recorded in [the evidence](../../testing/goo-350-account-isolation.md)).

Delivery: commit and push `codex/goo-350-account-isolation`, open a focused develop PR, and set GOO-350 to In Review with the PR and evidence. The live PR and Linear issue track those external actions. Merge and deployment remain for review.

Review focus: same-ID reuse across accounts, out-of-order profile success/rejection, callback and request retries after cancellation, warm persisted bootstrap selections, same-user token refresh during active work.

Limits: no claim of complete deployed isolation; organization-shared documents and workspace owner/member access retain the existing contract. Browser acceptance, API matrix expansion and deployment-grant verification remain separately tracked.

# Task 3 mutation evidence

This ledger records the focused mutation checks for Task 3 of the 2026-09-16
chat adversarial repairs. Commands ran from `frontend/` in the isolated
worktree with Node 24.21.0 and pnpm 10.18.2. Each mutant was applied alone,
the covering test failed by assertion, and the original source was restored
before the next check. The final focused suite passed after all restorations.

| Guard                                  | Mutant and covering command                                                                                                                                                                                                             | Observed failure                                                                                             | Log                                                             |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------- |
| Retained attachment guard              | Removed the shared `hasUnsettledAttachments` submission guard. `pnpm vitest run src/components/chat/__tests__/ChatInput-attach.test.tsx`                                                                                                | 3 failed, 16 skipped: failed-chip removal, pending click/Enter, and mixed Queue each called submit.          | `/tmp/chat-audit-20260916/mutation-attachments.log`             |
| Preflight ownership                    | Changed `ownsPreflight` to always return `true`. `pnpm vitest run src/hooks/__tests__/useChatStreaming.submitLock.test.tsx`                                                                                                             | 7 failed, 6 skipped: account-boundary preflights and late superseded preflight wrote into the wrong context. | `/tmp/chat-audit-20260916/mutation-draft-owner-true.log`        |
| Same-account thread fence (same guard) | Mutated `ownsPreflight` back to the old account-only predicate. `pnpm vitest run src/hooks/__tests__/useChatStreaming.submitLock.test.tsx -t "same account changes threads mid-preflight"`                                              | 1 failed: the late rollback overwrote the same-account thread's `thread B draft` with `originating draft`.   | `/tmp/chat-audit-20260916/mutation-draft-same-thread-owner.log` |
| Originating preflight snapshot         | Stored an empty string instead of the originating draft. `pnpm vitest run src/hooks/__tests__/useChatStreaming.submitLock.test.tsx -t "keeps the originating draft when a superseded preflight settles late"`                           | 1 failed: expected `originating draft`, received an empty draft.                                             | `/tmp/chat-audit-20260916/mutation-draft-pending-snapshot.log`  |
| First-send cleanup                     | Removed cleanup of the original preflight key after first-thread creation. `pnpm vitest run src/hooks/__tests__/useChatStreaming.submitLock.test.tsx -t "does not resurrect a sent first-chat prompt after starting New chat"`          | 1 failed: expected the new chat to remain empty, received `already sent`.                                    | `/tmp/chat-audit-20260916/mutation-draft-success-cleanup.log`   |
| Explicit deep-link ownership           | Replaced `!hasExplicitThreadIntent` with `true` in the first-thread fallback. `pnpm vitest run src/hooks/__tests__/useChatPersistence.initialization.test.tsx -t "defers first-thread fallback while an explicit deep link is pending"` | 1 failed: expected no selected thread, received `thread-1`.                                                  | `/tmp/chat-audit-20260916/mutation-a14-explicit-thread.log`     |
| Replacement suffix projection          | Disabled the canonical slice while a replacement marker is present. `pnpm vitest run src/components/chat/shared/__tests__/cloudMessageView.test.ts -t "hides the canonical replaced suffix\|restores the suffix"`                       | 1 failed: old canonical user/assistant and following rows remained beside the replacement.                   | `/tmp/chat-audit-20260916/mutation-replacement-selector.log`    |
| Replacement failure recovery           | Made `withoutReplacementMarkers` return messages unchanged. `pnpm vitest run src/hooks/chat/__tests__/useChatStreaming.editResend.test.ts -t "hides the replaced suffix while pending, then restores it on stream failure"`             | 1 failed: the replacement marker remained after the deferred stream failed.                                  | `/tmp/chat-audit-20260916/mutation-replacement-failure.log`     |

The focused final command was:

```sh
pnpm vitest run \
  src/components/chat/__tests__/ChatInput-attach.test.tsx \
  src/components/chat/shared/__tests__/cloudMessageView.test.ts \
  src/hooks/__tests__/useChatPersistence.initialization.test.tsx \
  src/hooks/__tests__/useChatSession.boundedRestore.test.tsx \
  src/hooks/__tests__/useChatStreaming.submitLock.test.tsx \
  src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx \
  src/hooks/chat/__tests__/useChatStreaming.editResend.test.ts
```

It passed **123 tests in 7 files** under Node 24.21.0. The tests intentionally
log mocked-store reconciliation diagnostics and synthetic transport errors;
these are fixture output and did not fail the run.

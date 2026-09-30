# Chat account isolation: mutation evidence

Recorded 2026-09-30 for `codex/chat-audit-repairs-20260930`, based on
`a2823a27baf8e2a3aced19522dcd82d4a65912f9`. This is dated verification evidence
for the fixes in the same change, following the
[testing contract](../engineering/testing.md#mutation-verification-race--idempotency-tests).

Each check below temporarily disabled one protection, produced the described
assertion failure, restored the source byte-for-byte (verified by SHA-256),
then passed with the identical focused command. All inputs are synthetic;
service I/O is mocked while the actual auth actions, stores, and hooks run.
Commands run from the repository root using Node 24 and pnpm 10.18.2.

## Clearing previous-account state

Guard: `frontend/src/stores/authStore.ts:99`, the two chat-store resets in
`clearUserScopedClientState`.

Omitting both resets fails all three sign-out, rejected-session, and direct
account-switch cases: the widget renders the previous user's message, and
their unsent draft and transcripts remain in the stores.

```sh
pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'removes user A chats' --reporter=verbose
```

## Rejecting asynchronous work from the previous session

Guard: `frontend/src/store/chat/requestCoordinator.ts:70`, the predicate
returned by `captureChatSession` and consumed by store actions and stream
callbacks.

Returning `true` unconditionally fails seven cases: delayed workspace,
conversation, thread, workspace-bootstrap, and thread-creation results
repopulate the cleared store; late send and confirmation callbacks restore
private tool arguments in `streamingSteps`. The separate agent-thread list
test still passes because it has its own request token.

```sh
pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx -t 'discards a previous account|discards late' --reporter=verbose
```

## Invalidating the agent-thread list request

Guard: `frontend/src/store/agentChatStore.ts:1690`, clearing `loadThreadsToken`
on reset.

Omitting this assignment lets the old account's delayed thread list replace
the new account's empty list: `private-thread` appears where `[]` is expected.

```sh
pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'delayed agentThreads response' --reporter=verbose
```

## Aborting the widget stream

Guard: `frontend/src/store/agentChatStore.ts:1691`, aborting the active stream
on reset.

Omitting the abort fails the transport assertion: the old account's signal
remains active after sign-out. Existing callback ownership checks independently
prevent some late writes; the test requires cancellation as well.

```sh
pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'aborts the old account' --reporter=verbose
```

## Cancelling before the service import finishes

Guard: `frontend/src/store/agentChatStore.ts:243`, checking the aborted signal
immediately after the lazy service import in `sendMessage`.

Removing this check starts a service request after sign-out; the expected
zero calls to `streamMessage` becomes one.

```sh
pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'service import' --reporter=verbose
```

## Aborting a mounted chat hook

Guard: `frontend/src/hooks/chat/useChatStreaming.ts:961`, aborting the hook's
current controller from the session-reset subscription.

Removing the abort fails both send and confirmation cases because their
signals remain active after account invalidation.

```sh
pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx -t 'discards late' --reporter=verbose
```

## Preventing workspace cache resurrection

Guard: `frontend/src/services/workspaceService.ts:62`, the generation check
used by default-workspace and default-conversation resolution.

Neutralizing the condition lets an old pending bootstrap resolve with
`private-workspace` and repopulate local storage. The test expects an
`AbortError`, empty cache keys, and no fallback workspace creation.

```sh
pnpm --dir frontend exec vitest run src/services/__tests__/workspaceService.cache.test.ts -t 'cannot restore the old account' --reporter=verbose
```

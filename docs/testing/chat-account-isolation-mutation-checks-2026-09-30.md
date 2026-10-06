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

# Review follow-up: account switch, profile fencing, attachment retention

Recorded 2026-10-06 for the three PR #1776 review findings, on top of
`4329952e3` (develop merged in). Same procedure as above: each guard was
disabled alone, the named tests failed as described, the source was restored
and the identical command passed. The pre-fix sources (`git show HEAD:<file>`)
fail every new test. Line numbers below are as of the second follow-up
commit.

## Re-initializing chat persistence on an account switch

Test file: `frontend/src/hooks/chat/__tests__/useChatSession.accountSwitch.test.tsx`
(real auth store, chat store, `useChatPersistence` and `useChatSession`).

```sh
pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatSession.accountSwitch.test.tsx --reporter=verbose
```

- `frontend/src/hooks/useChatPersistence.ts:298`, dropping the shared init
  guard on chat-session reset. Removing it fails all three cases: B takes the
  completed fast path over an empty store (`currentConversationId` stays
  undefined, workspace never becomes `ws-user-B`).
- `frontend/src/hooks/useChatPersistence.ts:843,854`, keying the per-consumer
  reset and auto-init effects to the user id. Reverting to `[isAuthenticated]`
  fails "persistence-only consumer" and "superseded initialization": the chat
  layout never re-initializes for B.
- `frontend/src/hooks/chat/useChatSession.ts:886`, the init effect's `userId`
  dependency. Removing it fails "drops user A workspace metadata": the mounted
  page never re-bootstraps for B.
- `frontend/src/hooks/chat/useChatSession.ts:932`, dropping the previous
  account's workspace on user change. Removing it fails the same case:
  `workspace` is still `ws-user-A` while B's bootstrap is pending.
- `frontend/src/hooks/useChatPersistence.ts:639`, silencing a superseded run's
  failure. Removing it fails "superseded initialization": B sees A's
  "Workspace initialization returned no workspace" toast.
- `frontend/src/hooks/useChatPersistence.ts:648`, only the current run may
  release the in-flight slot. Removing the identity check fails the same case:
  A's late failure releases B's run and a concurrent caller starts a duplicate
  bootstrap (3 calls instead of 2).

## Fencing profile responses to their auth generation

Test file: `frontend/src/store/__tests__/auth-store-profile-fetch.test.ts`,
describe "profile responses across account transitions".

```sh
pnpm --dir frontend exec vitest run src/store/__tests__/auth-store-profile-fetch.test.ts --reporter=verbose
```

- `frontend/src/stores/authStore.ts:467`, the compare after `/auth/me` in
  `fetchProfile`. Removing it fails both "late /auth/me" cases and "lands
  after sign-out": A's response clears B's chat and flips the store back to
  `user-A` (or re-authenticates A after sign-out).
- `frontend/src/stores/authStore.ts:480`, the compare in the error path.
  Removing it fails "late 401": A's rejection tears down B's session.
- `frontend/src/stores/authStore.ts:434,450`, the compares after `getUser()`
  and `getSession()`. Removing either fails the matching "late signed-out
  getUser/getSession answer" case: B is signed out locally.
- `frontend/src/stores/authStore.ts:134`, bumping the generation in
  `clearUserScopedClientState`. Removing it fails "lands after sign-out".
- `frontend/src/stores/authStore.ts:193,219`, `signIn` starting a generation
  and checking it before publishing. Removing either fails "concurrent
  sign-ins": the earlier sign-in's late profile wipes B's chat and wins.
  (Line 219 now throws `SignInSupersededError`; see below.)
- `frontend/src/stores/authStore.ts:129`, discarding the previous account's
  in-flight profile promise. Removing it fails both "late /auth/me" cases: B's
  profile read joins A's abandoned request instead of issuing its own.
- `frontend/src/stores/authStore.ts:513`, only the owning request may clear
  the in-flight slot. Removing the identity check fails both "late /auth/me"
  cases: A settling releases B's slot and a concurrent caller issues a
  duplicate `/auth/me`.

## Keeping attachments on an edited or regenerated turn

Tests: "%s keeps the turn's attachments on the optimistic replacement…" in
`frontend/src/hooks/chat/__tests__/useChatStreaming.editResend.test.ts` (real
`useChatComposerActions` wired to real `useChatStreaming`) and "%s retains the
original document IDs and attachment metadata" in
`frontend/src/hooks/chat/__tests__/useChatComposerActions.attachments.test.tsx`.

```sh
pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.editResend.test.ts src/hooks/chat/__tests__/useChatComposerActions.attachments.test.tsx --reporter=verbose
```

- `frontend/src/hooks/chat/useChatStreaming.ts:2053`, copying attachments onto
  the optimistic user message. Removing it fails both edit and regenerate
  integration cases: the replacement has `attachments: undefined` during the
  stream and in the local error state.
- `frontend/src/hooks/chat/useChatComposerActions.ts:205` (edit) and `:167`
  (regenerate), forwarding the metadata. Removing either fails the matching
  integration case and the matching composer-actions case. The backend payload
  still carries only `attachment_ids`.

## Second follow-up: superseded init runs and sign-ins

Independent review items I1, I2, M1, M2, M4 and M5. Same procedure.

```sh
pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatSession.accountSwitch.test.tsx src/store/__tests__/auth-store-profile-fetch.test.ts src/hooks/chat/__tests__/useChatStreaming.editResend.test.ts --reporter=verbose
```

### Init runs stop once their chat session ended (I1)

- `frontend/src/hooks/useChatPersistence.ts:555`, `assertCurrentSession()`
  after `setCurrentConversation`. Removing it fails "stops user A's run once it
  outlives the switch past setCurrentConversation": A's run selects
  `user-A-picked-thread` in B's store.
- `frontend/src/hooks/useChatPersistence.ts:461`, the check after the
  default-workspace bootstrap. Removing it fails "does not let user A's late
  bootstrap re-run conversation setup on user B's workspace": A's run reloads
  B's threads (2 `listThreads` calls instead of 1).
- `frontend/src/hooks/useChatPersistence.ts:516`, the check after
  `getOrCreateDefaultConversation`. Removing it fails "does not register user
  A's late default conversation in user B's store": `conv-user-A` is indexed
  under `ws-user-A` in B's store.
- `frontend/src/hooks/useChatPersistence.ts:632`, the `.then` fence. The
  in-body checks already make a superseded run reject, so removing this fence
  alone passes. Removing it together with the line 555 check fails the same
  "stops user A's run" case on the earlier assertion: a concurrent
  `initialize()` takes the completed fast path (`joinedSettled` is `true`)
  while B's bootstrap is still pending.

### Superseded sign-ins reject and release the loading flag (I2, M1)

- `frontend/src/stores/authStore.ts:219`, throwing `SignInSupersededError`
  instead of returning. Returning fails "rejects a sign-in interrupted by
  sign-out" and "concurrent sign-ins": the superseded call resolves as success.
- `frontend/src/stores/authStore.ts:237`, fencing the `signIn` catch path.
  Removing it fails "does not write a superseded sign-in's failure onto the
  newer session" (`error` is set) and "rejects a sign-in interrupted by
  sign-out".
- `frontend/src/stores/authStore.ts:240`, releasing `isLoading` when no newer
  sign-in is in flight. Removing it fails both cases above with
  `isLoading: true`.
- `frontend/src/stores/authStore.ts:121`, `settleSupersededLoading` for fenced
  `fetchProfile` returns. Making it a no-op fails "clears the loading flag when
  a fenced-out profile read was the last loader".
- `frontend/src/stores/authStore.ts:168`, `isLoading: false` in the
  `SIGNED_OUT` listener. Removing it fails "clears the loading flag on a
  SIGNED_OUT event".
- Negative control (M5): "publishes the profile when the same user signs out
  and back in". Capturing the generation before `beginAuthGeneration()` in
  `signIn` (over-fencing) fails it with `SignInSupersededError`.

### Account switch clears parked overlays (M2)

- `frontend/src/hooks/chat/useChatSession.ts:927`, clearing
  `parkedMessagesRef` on user change. Removing it fails "drops user A's parked
  in-flight overlay on the switch": opening a thread with the same id restores
  A's optimistic message. The neighbouring resets
  (`localMessagesThreadIdRef`, `firstPageThreadsRef`, `threadsPageRef`,
  `unavailableInitialUrlThreadRef`, `hasMoreThreads`) are defense in depth and
  are not individually mutation-verified.

### Attachment metadata never reaches the wire (M4)

- In the "%s keeps the turn's attachments" cases, the serialized
  `streamMessage` request is asserted not to contain the attachment's
  `display_name` or row id. Adding `attachments: m.attachments` to the request
  message mapping in `useChatStreaming.ts` fails both cases.

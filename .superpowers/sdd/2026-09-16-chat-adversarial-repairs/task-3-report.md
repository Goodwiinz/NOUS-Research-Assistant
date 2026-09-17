# Task 3 implementation report

Task 3 is implemented in the chat composer, transient draft ownership,
initial deep-link recovery, and replacement display paths. The work is
contained in the Task 3 source/tests listed below. Root-authored audit and
plan files remain unstaged for the final docs commit.

## Behavior delivered

- `ChatInput` computes one `hasUnsettledAttachments` predicate and uses it for
  click, Enter, and Queue submission. Retained `uploading` and `error` chips
  keep the draft and chip in place, expose a readable blocked status, and can
  still be removed. A mixed completed/pending batch cannot submit until every
  retained upload is usable; completed document IDs continue to reach the
  submit callback.
- Composer drafts are keyed by account, workspace, and selected thread/new
  chat. A live store lookup handles account/thread changes that happen before
  React renders the next context. First-send preflight keeps an originating
  snapshot only for rollback, checks the live owner before restoring it, and
  clears both the original and created-thread keys after success. This keeps
  A→B→A drafts isolated, preserves a late failed/aborted A draft, and does not
  resurrect a sent prompt in New chat.
- Initial explicit `?thread=` intent is now visible to persistence ownership.
  First-thread fallback waits for that URL target, while `useChatSession`
  keeps the existing 404/403 safe recovery and deliberate sidebar-selection
  supersession behavior. Auth recovery route fields are ordinary typed return
  fields (`isInitializing` and `isNewChatIntent`) so ownership is explicit in
  the hook contract.
- Replacement user rows carry a transient ownership marker. The display
  selector slices the canonical suffix while replacement is pending without
  mutating canonical rows. Stream failure, empty response, stop-before-token,
  and transport exception paths remove the marker and restore the canonical
  suffix. Successful refresh reconciles the replacement without duplicate
  rows.

## Focused behavior evidence

The final focused command ran under Node 24.21.0 and pnpm 10.18.2:

```text
7 test files passed
123 tests passed
```

The files and counts were:

```text
ChatInput-attach.test.tsx                         19
cloudMessageView.test.ts                          41
useChatPersistence.initialization.test.tsx         7
useChatSession.boundedRestore.test.tsx            11
useChatStreaming.submitLock.test.tsx               14
useChatStreaming.authRecovery.test.tsx             26
useChatStreaming.editResend.test.ts                 5
```

The final log is `/tmp/chat-audit-20260916/task3-focused-final-node24-after-thread-fence.log`.
The attachment tests exercise failed-chip removal, click, actual Enter keydown,
form submission, and a real mixed completed/pending Queue batch. The edit
tests collect displayed rows across pending, failure, and successful canonical
refresh states. The submit-lock tests include both account-plus-thread and
same-account thread-switch preflight supersession.

The required mutation ledger is
[`docs/testing/chat-adversarial-task3-mutations.md`](../../../docs/testing/chat-adversarial-task3-mutations.md).
It records seven guard mutations: the attachment guard, preflight owner
(including the same-account thread fence), originating snapshot, success
cleanup, explicit deep-link guard, replacement suffix projection, and
replacement failure marker. Every mutant was restored before the next check.

## Static and quality evidence

All gates below ran after the final source restoration and under Node 24.21.0:

- ESLint JSON over `app` and `src`, followed by
  `check_frontend_quality.mjs` with the eleven changed frontend files:
  **OK**, baseline totals 116 errors and 2005 warnings, with no changed-file
  warning growth. Report: `/tmp/chat-audit-20260916/task3-eslint-node24-final.json`.
- `pnpm quality:exclusions`: **OK**, 21 existing production exclusions and no
  new type-check debt.
- `pnpm type-check`: **passed**.
- Prettier check over all eleven changed TypeScript/TSX files: **passed**.
- `git diff --check`: **passed**.

The package's `pnpm lint:changed` script itself currently invokes the checker
without its required `--report` argument and exits with its usage error. The
documented equivalent—full ESLint JSON generation followed by the comparator
with explicit changed files—passed as recorded above; no baseline was changed.

## Changed files

Production files:

- `frontend/src/components/chat/ChatInput.tsx`
- `frontend/src/components/chat/shared/cloudMessageView.ts`
- `frontend/src/hooks/chat/useChatSession.ts`
- `frontend/src/hooks/chat/useChatStreaming.ts`
- `frontend/src/hooks/useChatPersistence.ts`

Regression tests:

- `frontend/src/components/chat/__tests__/ChatInput-attach.test.tsx`
- `frontend/src/components/chat/shared/__tests__/cloudMessageView.test.ts`
- `frontend/src/hooks/__tests__/useChatPersistence.initialization.test.tsx`
- `frontend/src/hooks/__tests__/useChatStreaming.submitLock.test.tsx`
- `frontend/src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx`
- `frontend/src/hooks/chat/__tests__/useChatStreaming.editResend.test.ts`

The existing `frontend/src/hooks/__tests__/useChatSession.boundedRestore.test.tsx`
was also included in the final focused verification; it was not modified by
Task 3.

Durable documentation:

- `docs/testing/chat-adversarial-task3-mutations.md`
- this report

## Limits and self-review

Root owns the broad frontend suite, browser matrix, and final integration
checks; this report makes no browser or deployment claim. The focused unit
fixtures intentionally print `ChatReconciliationInvariant` `TypeError`
diagnostics when mocked persistence responses are absent, and the tests still
pass. The mutation checks are one-time sequential source mutations recorded in
the ledger, rather than a new automated mutation runner.

Before commit I rechecked that each mutation was restored, that the preflight
rollback and success paths each delete the originating and current context
keys exactly once, and that Task 2 durable Stop/run acknowledgement and Task 5
reasoning fields remain in the touched hook. No credentials, authorization
headers, or raw server exception text were added to product state or docs.

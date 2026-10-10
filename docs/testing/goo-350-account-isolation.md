# GOO-350 frontend account lifetime verification

Recorded October 3, 2026. This is implementation evidence for Task 4 / DI-2 of
[the October 1 data isolation audit](https://linear.app/goodwiinz/document/data-isolation-audit-findings-plan-and-small-issues-2026-10-01-1d5d1e1da573),
tracked in [GOO-350](https://linear.app/goodwiinz/issue/GOO-350/data-isolation-clear-private-client-stores-and-cancel-work-on-account).
The implementation started from develop `2a45aa50c` in an isolated worktree.

## Scope and comparison with current work

The audit's private chat/agent transcript retention was still present in current
develop. Open [PR #1776](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1776)
(`536480d8758e7ac66c565f5779e0c6a7b53e008b`) already proposed relevant chat guards.
This change reuses that account-cleanup subset and extends it to authentication,
projects, request transports, local component state and deferred work. Its
attachment/history and backend changes are not included. PR #1776 remains open.

Merged [PR #1833](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1833)
added an opt-in shared-browser test. Executing/expanding that acceptance coverage
and activating its CI gate remain [GOO-354](https://linear.app/goodwiinz/issue/GOO-354/data-isolation-verify-shared-browser-account-switching-and-add-the-ci).

An account change revokes captured ownership before clearing the existing chat,
agent, project, pipeline, citation, research, activity, notification, workspace,
artifact and Query caches. Requests combine caller cancellation with the account
signal. Async success, error, retry, body decoding, stream callbacks and cleanup
must still own their initiating account. Rendered actions retain that ownership
through queued edits and confirmation callbacks. The account boundary also
remounts page-local drafts and dialogs. Same-user token refresh retains the
lifetime and mounted work. Tab-scoped expired-session draft recovery remains
bound to its recorded owner and never automatically sends text.

There is no backend authorization or sharing-policy change: documents may be
shared inside an organization; private workspaces depend on owner/member access.
This repair does not establish complete deployed data isolation.

## Tests-first evidence

Regression tests were written and observed failing before each repair. Initial
real-store tests failed 43 cases against develop; the API transport suite failed
all nine cases. Later failing regressions exposed deferred confirmation fallback,
off-route bootstrap, parked overlays, old draft polling, local dialogs, expired
session recovery through the real AuthProvider, and canceled auth continuations.
The pagination test specifically observed A's error toast appearing after reset.

Independent source review found the recovery, auth continuation and pagination
races; they were repaired and re-reviewed with no remaining actionable findings
in those fixes. Automated verification and its limitations are recorded below.

## Verification results

Verified with Node 24 and pnpm 10.18.2:

| Check | Actual result |
| --- | --- |
| `pnpm --dir frontend test` | PASS: 365 files, 2,728 tests. |
| `pnpm --dir frontend type-check` | PASS. |
| Fresh full-tree ESLint report and `node scripts/ci/check_frontend_quality.mjs --report /dev/shm/goo350/eslint-reviewed.json --base origin/develop` | PASS: no errors in changed files and no increased warning allowances. Measured global debt is 103 errors / 1,956 warnings. |
| `python3 scripts/ci/check_tsconfig_exclusions.py --base origin/develop` | PASS: 21 existing production exclusions, none added. |
| `python3 scripts/docs/check_dir_docs.py` | PASS: 56 directory documents. |
| Race-guard mutation verification | PASS: all 41 named guards below failed when disabled and passed after exact restoration. |

The final coverage command also passed all 365 files / 2,728 tests:

```sh
TMPDIR=/dev/shm/goo350 pnpm --dir frontend test:coverage --coverage.reporter=json-summary --coverage.reportsDirectory=/dev/shm/goo350/coverage-final
node scripts/ci/check_frontend_coverage.mjs /dev/shm/goo350/coverage-final/coverage-summary.json
```

Coverage measured 47.60% lines/statements, 62.38% functions and 79.24% branches.
The committed floors were raised to these measured values (previously 19.36%,
52.19% and 76.30%, respectively), preserving the regression coverage added here.

The ESLint caps were tightened from 112 errors / 2,002 warnings to the measured
103 / 1,956, with stricter allowances in the touched files that improved. Full
`pnpm --dir frontend lint` remains advisory and exits 1 for that existing debt;
the blocking changed-file comparator passes. No quality floor or production
exclusion was weakened.

`scripts/ci/run_local_ci.sh --base origin/develop --frontend` was attempted.
Backend test collection could not complete (323 collection errors, including a
missing `langchain_core` import; Redis was also unavailable). Its NOUS workflow
contract tests passed: 161 tests, four existing marker warnings. The final
`--skip-tests` wrapper rerun passed Ruff, directory docs, frontend TypeScript and
both frontend ratchets, but still exited 1: OpenAPI generation cannot import
`langgraph`, and the Alembic check cannot import `alembic`. Generated API types and
migration probes were reported skipped because their inputs did not change.
These backend checks are **not verified** by this frontend-only repair.

The host root filesystem filled during a frontend test attempt. The complete
suite was rerun successfully with `TMPDIR=/dev/shm/goo350`; coverage reports were
also redirected to that RAM-backed directory. No failing disk-space run is
counted as a pass.

Shared-browser and accessibility E2E are **NOT RUN**: the required running
application, GoTrue/backend services and browser acceptance setup are absent.
GOO-354 retains the broader shared-browser acceptance and CI activation work.
Neither a merge nor a deployment is part of this verification.

## Race-guard mutation checks

All 41 checks below were observed failing with the named guard disabled and
passing after its exact restoration. Each source file was compared byte-for-byte
(SHA-256) with the saved implementation before the restored test ran; no mutation
is retained. The failing assertions were inspected. For the page boundary,
the DOM matcher reported an input containing `A private note` instead of an
empty value. The profile deduplication check was strengthened to await the next
event-loop turn: its mutant made four profile requests instead of three.

Run from the repository root using Node 24 and pnpm 10.18.2. The commands are
exact focused test invocations; disable only the named guard before the red run,
then restore it before the green run. Service/store lifetimes apply the same
captured ownership predicate to success, catch, finally and follow-up branches.
Where a command selects a table of cases, only the affected mutant's cases need
fail. Tests-first logs that failed because of an incomplete test mock were fixed
and rerun; those setup errors are not counted as mutation evidence.

| Guard and source | Disabled protection / observed defect | Exact focused command (red, then restored green) |
| --- | --- | --- |
| auth-profile-revision — `frontend/src/stores/authStore.ts:467` | A profile overwrites B or releases B request ownership | `pnpm --dir frontend exec vitest run src/store/__tests__/auth-account-isolation.test.ts -t 'late profile\|previous.*profile\|old.*profile\|profile deduplication'` |
| auth-profile-dedup — `frontend/src/stores/authStore.ts:541` | A finally releases the B profile promise | `pnpm --dir frontend exec vitest run src/store/__tests__/auth-account-isolation.test.ts -t 'profile deduplication'` |
| projectStore-lifetime — `frontend/src/store/projectStore.ts:147` | A deferred success/error or follow-up mutates B state | `pnpm --dir frontend exec vitest run src/store/__tests__/auth-account-isolation.test.ts -t 'discards deferred A'` |
| projectChatStore-lifetime — `frontend/src/store/projectChatStore.ts:122` | A deferred success/error or follow-up mutates B state | `pnpm --dir frontend exec vitest run src/store/__tests__/auth-account-isolation.test.ts -t 'discards deferred A'` |
| citationStore-lifetime — `frontend/src/store/citationStore.ts:36` | A deferred success/error or follow-up mutates B state | `pnpm --dir frontend exec vitest run src/store/__tests__/auth-account-isolation.test.ts -t 'discards deferred A'` |
| pipeline-reset — `frontend/src/store/pipelineStore.ts:199` | A pipeline result/error survives reset | `pnpm --dir frontend exec vitest run src/store/__tests__/auth-account-isolation.test.ts -t 'discards deferred A'` |
| workspaceSlice-lifetime — `frontend/src/store/chat/slices/workspaceSlice.ts:49` | A chat request restores data after reset | `pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'delayed workspaces'` |
| conversationSlice-lifetime — `frontend/src/store/chat/slices/conversationSlice.ts:51` | A chat request restores data after reset | `pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'delayed conversations'` |
| threadSlice-lifetime — `frontend/src/store/chat/slices/threadSlice.ts:56` | A chat request restores data after reset | `pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'delayed (threads\|createThread)'` |
| chat-bootstrap — `frontend/src/store/chat-store.ts:70` | A bootstrap restores selected workspace | `pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'delayed bootstrap'` |
| agent-thread-token — `frontend/src/store/agentChatStore.ts:1695` | A agent history overwrites B | `pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'delayed agentThreads'` |
| agent-stream-abort — `frontend/src/store/agentChatStore.ts:1696` | A agent stream remains alive and repopulates transcript | `pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'aborts the old account'` |
| agent-confirm-epoch — `frontend/src/store/agentChatStore.ts:1166` | A resolved confirmation restores its pending card | `pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'resolve cannot restore'` |
| agent-confirm-fallback — `frontend/src/store/agentChatStore.ts:1182` | A rejected confirmation invokes durable fallback with B credentials | `pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'reject cannot restore'` |
| agent-send-fallback — `frontend/src/store/agentChatStore.ts:566` | A rejected SSE turn launches its durable request in B | `pnpm --dir frontend exec vitest run src/store/__tests__/chat-session-isolation.test.tsx -t 'does not start a durable agent run'` |
| workspace-cache-generation — `frontend/src/services/workspaceService.ts:80` | Old cache promise restores A workspace ID | `pnpm --dir frontend exec vitest run src/services/__tests__/workspaceService.cache.test.ts -t 'cannot restore the old account'` |
| api-account-signal — `frontend/src/services/api-client.ts:151` | A JSON/blob/download/upload/auth/backoff escapes cancellation | `pnpm --dir frontend exec vitest run src/services/__tests__/api-client.account-isolation.test.ts` |
| raw-agent-account-signal — `frontend/src/services/agentChatService.ts:723` | Raw agent reads, mutations and cancellation polling outlive A | `pnpm --dir frontend exec vitest run src/services/__tests__/agentChatService.account-isolation.test.ts` |
| rendered-actions-lifetime — `frontend/src/hooks/chat/useChatSessionGuard.ts:11` | Old queued edit/regenerate submits A content in B | `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatComposerActions.test.tsx -t 'account\|queued'` |
| off-route-bootstrap-reset — `frontend/src/hooks/useChatPersistence.ts:281` | Off-route logout leaves the shared bootstrap fulfilled for A | `pnpm --dir frontend exec vitest run src/hooks/__tests__/useChatPersistence.initialization.test.tsx -t 'reinitializes B'` |
| bootstrap-lifetime — `frontend/src/hooks/useChatPersistence.ts:284` | Old default-conversation success/error contaminates B initialization | `pnpm --dir frontend exec vitest run src/hooks/__tests__/useChatPersistence.initialization.test.tsx -t 'ignores A default'` |
| parked-overlays — `frontend/src/hooks/chat/useChatSession.ts:921` | Old private overlay reappears on a reused thread | `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatSession.parking.test.tsx -t 'clears A parked'` |
| slash-output-reset — `frontend/src/hooks/chat/useSlashCommands.ts:94` | A private slash results remain visible in an empty chat | `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useSlashCommands.test.tsx -t 'clears private'` |
| slash-deferred-lifetime — `frontend/src/hooks/chat/useSlashCommands.ts:92` | A delete callback starts a new memory read as B | `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useSlashCommands.test.tsx -t 'does not re-list'` |
| dialog-reset — `frontend/src/hooks/chat/useChatThreadActions.ts:88` | A titles, IDs and dialog state survive reset | `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatThreadActions.account-isolation.test.tsx` |
| stream-callback-lifetime — `frontend/src/hooks/chat/useChatStreaming.ts:1089` | Old send/confirmation callbacks restore private state | `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx -t 'discards late'` |
| stream-post-reconcile — `frontend/src/hooks/chat/useChatStreaming.ts:1690` | Old reconciliation releases B submit/loading state | `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx -t 'cannot release B'` |
| stream-replay-gap — `frontend/src/hooks/chat/useChatStreaming.ts:2506` | Old replay-gap marks B transcript stale | `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx -t 'ignores A replay-gap'` |
| stream-resume-result — `frontend/src/hooks/chat/useChatStreaming.ts:2549` | Old idle resume finishes B active run | `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx -t 'ignores A replay-gap'` |
| recovery-handoff — `frontend/src/hooks/chat/useChatStreaming.ts:968` | Account remount loses the ready same-owner draft handoff | `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx -t 'survives a real.*AuthProvider=true'` |
| research-project-lifetime — `frontend/src/components/research-engine/ResearchDashboard.tsx:28` | A research project/error overwrites B | `pnpm --dir frontend exec vitest run src/components/research-engine/__tests__/ResearchDashboard.account-isolation.test.tsx` |
| research-stream-abort — `frontend/src/components/research-engine/RunView.tsx:296` | Buffered research events start new reads after A ends | `pnpm --dir frontend exec vitest run src/components/research-engine/__tests__/RunView.test.tsx -t 'aborts the account stream'` |
| draft-poll-lifetime — `frontend/app/(dashboard)/projects/[id]/page.tsx:1087` | Old draft poll retries or commits after account reset | `pnpm --dir frontend exec vitest run src/components/research/__tests__/ProjectDetailPage.agent-sync.test.tsx -t 'stops draft polling'` |
| page-local-boundary — `frontend/src/hooks/useAuth.tsx:115` | A private page draft remains mounted for B | `pnpm --dir frontend exec vitest run src/components/auth/__tests__/account-boundary.test.tsx` |
| auth-canceled-outcome — `frontend/src/stores/authStore.ts:227` | Superseded authentication resolves as success to caller navigation | `pnpm --dir frontend exec vitest run src/store/__tests__/auth-account-isolation.test.ts -t 'pending password login\|ignores old'` |
| same-user-identity — `frontend/src/stores/authStore.ts:149` | A same-user SDK refresh clears ongoing work | `pnpm --dir frontend exec vitest run src/store/__tests__/auth-account-isolation.test.ts -t same-user` |
| rejected-owner — `frontend/src/stores/authStore.ts:412` | A rejected-session notification tears down B including its pending profile | `pnpm --dir frontend exec vitest run src/store/__tests__/auth-account-isolation.test.ts -t 'late rejected-session\|profile deduplication'` |
| project-dedup — `frontend/src/store/projectStore.ts:214` | A finally releases B same-ID project request | `pnpm --dir frontend exec vitest run src/store/__tests__/auth-account-isolation.test.ts -t 'project deduplication'` |
| late-rename — `frontend/src/hooks/chat/useChatThreadActions.ts:70` | Old rename completion mutates B conversations | `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatThreadActions.account-isolation.test.tsx -t 'delayed rename'` |
| late-pagination-error — `frontend/src/hooks/chat/useChatSession.ts:678` | Old pagination failure shows its global toast in B | `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatSession.workspaceThreads.test.tsx -t 'pagination error'` |
| stream-auth-retry — `frontend/src/services/agentChatService.ts:287` | Late A 401 invokes a B-session credential refresh | `pnpm --dir frontend exec vitest run src/services/__tests__/agentChatService.account-isolation.test.ts -t 'does not refresh B'` |

# GOO-350 PR review follow-up

Recorded October 3, 2026 for [PR #1854](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1854).
This amends the [initial implementation evidence](goo-350-account-isolation.md),
which describes the earlier source and verification environment. The reviewed
PR head was `00c22427a`; this follow-up incorporates develop `1180dae59` through
merge `f16dfefa1`. The fixes and tests are recorded in the commit containing
this document.

## Review findings and repair

Anonymous session verification previously called the full account teardown.
Because the AuthProvider keys its children by account lifetime, a delayed null
session, verification error or initial `SIGNED_OUT` remounted public forms and
discarded login credentials or an in-flight password-reset submission.

Five regressions using the real AuthProvider and real login/reset pages were
observed failing before the repair: four lost the typed email/password, and the
reset flow lost its confirmation. The repair settles anonymous verification
without resetting the account lifetime. An authenticated state, loaded user,
pending SDK identity or active login still requires full teardown. Explicit
sign-out and rejected-session invalidation retain that teardown. Added tests
exercise rejection while a profile or login is pending; the existing tests
continue covering A-to-B replacement, late A work, persisted selections,
same-user refresh and expired-session recovery.

The old CI run passed its tests but measured 79.22% branch coverage, below the
committed 79.24% floor. Added transport regressions exercise an export completing
signature inspection after the account changes, overlapping A/B validated
requests with independent caller signals, and an upload cancelled while its
credentials are loading. These exercise real request/cancellation code with
deferred external I/O. Coverage now exceeds the unchanged floor.

The develop merge preserves both branches' stricter lint allowances. The
account-cancellation callback in `RunView.tsx` now declares its `void` return
type, and that file's warning allowance decreases from one to zero. No coverage
floor or production exclusion is relaxed.

## Follow-up verification

Toolchain: Node 24 and pnpm 10.18.2, using the root lockfile. Commands run from
the isolated worktree `/home/clawdbot/rag-clean/.worktrees/goo-350-account-isolation`.
The original checkout's unrelated changes are preserved.

| Check | Result |
| --- | --- |
| `pnpm --dir frontend test` | PASS: 369 files / 2,748 tests. |
| Full coverage suite with `CI=true` | PASS: 369 files / 2,748 tests. |
| Coverage comparator | PASS: 48.39% lines/statements, 62.42% functions, 79.27% branches; floors remain 47.60%, 62.38%, 79.24%. |
| TypeScript | PASS: `pnpm --dir frontend type-check`. |
| Fresh ESLint report and changed-file comparator in the local wrapper | PASS: 100 existing errors / 1,949 warnings, within the stricter merged baseline. |
| Production exclusion comparator | PASS: 21 existing exclusions, none added. |
| Directory-document lint | PASS. |
| Follow-up guard mutation verification | PASS: all seven cases below fail for the intended defect, then pass after exact source restoration. |

Exact coverage commands (Node 24 is on PATH):

```sh
TMPDIR=/tmp/goo350-review-fix CI=true pnpm --dir frontend test:coverage --coverage.reporter=json-summary --coverage.reportsDirectory=/tmp/goo350-review-fix/coverage-final
node scripts/ci/check_frontend_coverage.mjs /tmp/goo350-review-fix/coverage-final/coverage-summary.json
```

The required local wrapper was rerun after the repair:

```sh
TMPDIR=/tmp/goo350-review-fix scripts/ci/run_local_ci.sh --base origin/develop --frontend --skip-tests
```

Ruff, directory docs and all frontend gates passed. The wrapper still exited 1:
OpenAPI generation lacks `langgraph`, and the Alembic check lacks `alembic`.
Backend tests were not rerun in this follow-up; their previous collection failure
is recorded in the initial evidence. Generated API types and migration probes
were explicitly skipped because their inputs did not change. Full-tree lint
remains advisory for the existing 100 errors / 1,949 warnings. None of these
backend or skipped checks is reported as a local pass.

## Follow-up guard mutation evidence

Each mutation was applied separately, the named test was observed failing, and
the original source bytes were restored and compared by SHA-256 before rerunning
the same command successfully. The first four cases exercise distinct conditions
in the same account-boundary predicate. These seven probes supplement the 41
initial probes; no mutation remains in source. Procedure follows the repository's
[testing standards](../engineering/testing.md#mutation-verification-race--idempotency-tests).

| Source and temporarily disabled protection | Observed defect | Exact command, run with the mutation and again after restoration |
| --- | --- | --- |
| `frontend/src/stores/authStore.ts:152`: always clear rather than checking for an account | Five public-form tests fail: credentials disappear and reset confirmation is lost | `pnpm --dir frontend exec vitest run --project unit src/components/auth/__tests__/AuthProvider.bootstrap.test.tsx` |
| `frontend/src/stores/authStore.ts:152`: remove `sessionUserId` from the predicate | Both pending-identity tests retain A-only transcript data | `pnpm --dir frontend exec vitest run --project unit src/store/__tests__/auth-account-isolation.test.ts -t 'rejects an identity whose profile is still loading'` |
| `frontend/src/stores/authStore.ts:152`: remove `activeSignIn` | A cancelled login resolves as accepted instead of `AbortError` | `pnpm --dir frontend exec vitest run --project unit src/store/__tests__/auth-account-isolation.test.ts -t 'a signed-out event cancels a login before its identity is known'` |
| `frontend/src/stores/authStore.ts:152`: remove `isAuthenticated` | Exhausted-auth recovery fails to navigate after `SIGNED_OUT` | `pnpm --dir frontend exec vitest run --project unit src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx -t 'recovers ownerlessly when SIGNED_OUT'` |
| `frontend/src/services/api-client.ts:671`: remove account check after asynchronous export validation | A's export completes instead of rejecting | `pnpm --dir frontend exec vitest run --project unit src/services/__tests__/api-client.account-isolation.test.ts -t 'discards an export when the account changes during signature inspection'` |
| `frontend/src/services/api-client.ts:278`: remove account check after reading the validation response | The old request returns `A-only` data after B starts | `pnpm --dir frontend exec vitest run --project unit src/services/__tests__/api-client.account-isolation.test.ts -t 'cancels validated A work without cancelling a new B request'` |
| `frontend/src/services/api-client.ts:579`: neutralize the already-aborted upload check | The cancelled upload calls `send` | `pnpm --dir frontend exec vitest run --project unit src/services/__tests__/api-client.account-isolation.test.ts -t 'does not send an upload cancelled while credentials are loading'` |

## Scope and limitations

Independent read-only review found no actionable defect in this follow-up.
It did not rerun the executor's tests. Cold anonymous startup can retain older
persisted workspace metadata/selections while logged out; `observeIdentity`
clears them before publishing the next authenticated identity. No cross-account
rendering path was established for that case. Purging such residual metadata
during anonymous startup was not verified by this follow-up.

This is frontend account-state repair. Organization-shared documents and private
workspace owner/member permissions keep their existing access contract. Complete
deployed data isolation is not established by these tests.

Shared-browser and accessibility E2E remain **NOT RUN** locally: the live
application/auth/backend acceptance setup is absent. Broader browser acceptance
and CI-gate activation remain tracked separately in
[GOO-354](https://linear.app/goodwiinz/issue/GOO-354/data-isolation-verify-shared-browser-account-switching-and-add-the-ci).
Merge and deployment remain for review.

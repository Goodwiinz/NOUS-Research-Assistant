# Task 7 implementation report

## Status

DONE

## What changed

- Replaced the transient run timeline with one persisted-first Zustand owner.
  `resetRun`, `hydrateRun`, `mergeRunEvent`, and `setPendingReview` now keep run,
  step, review, and terminal-result state together. Persisted step UUIDs win;
  `(run_id, step_index)` is used only while a streamed step has no reconciled
  persisted identity.
- Made `RunView` load the owned run and persisted steps before attaching the
  event stream. It clears old state on a run switch, ignores late responses,
  merges replayed or out-of-order events idempotently, reconnects after a clean
  stream end, and refreshes persisted state after lifecycle notifications.
- Reconstructed all three durable pause states from `RunResponse`. Manual
  pauses expose ordinary resume, review pauses fetch the pending server review,
  and verification failures expose a separate `continue_unverified` action
  bound to the exact persisted output hash.
- Added screening, extraction, and final review workspaces. Screening shows
  original citation metadata, abstract availability, evidence level, and the
  machine recommendation. Extraction shows the original structured record and
  quoted evidence. Local choices are explicitly drafts; unresolved items block
  approval, exclusion/rejection reasons are required, and a stale 409 reloads
  authoritative server state.
- Kept approve and resume as separate server operations. A declined review
  stays paused, and a failed or overridden artifact can never expose final
  approval.
- Added verified, unverified, and no-evidence result states. Verified and
  unverified artifacts show provider, source, extraction, claim-coverage, and
  limitation summaries. Downloads use only the owned API URLs; no artifact is
  generated in the browser. No-evidence runs omit Markdown and retain the JSON
  and CSV audit choices.
- `StepCard.tsx` required no Task 7 edit: Task 6 had already installed the exact
  six-step vocabulary and the adjacent StepProgress tests prove it remains
  compatible.

## TDD evidence

### Initial RED

The plan's literal `pnpm --dir frontend vitest run ...` form is not accepted by
pnpm 10 in this repository (`Command "frontend" not found`). The equivalent
repository form was used:

```text
corepack pnpm@10.18.2 --dir frontend exec vitest run src/store/__tests__/research-engine-store.test.ts src/components/research-engine/__tests__/RunView.test.tsx src/components/research-engine/__tests__/ReviewPanel.test.tsx src/components/research-engine/__tests__/RunResults.test.tsx
```

Result before production edits: exit 1; all four requested files were RED. The
store and RunView cases failed on the missing durable interfaces/behavior, and
the ReviewPanel and RunResults modules did not yet exist.

Three additional design assertions were then materialized before their fixes:
citation metadata, completed-run counts, and reconnect after a clean SSE EOF.
That exact batch was RED at `3 failed, 13 passed`, one failure per missing
behavior. Final review also caught raw exception disclosure on initial load; a
focused regression test reproduced it at `1 failed, 6 passed` before the UI was
changed to a stable safe message.

### Final focused GREEN

The exact corrected Task 7 command passed:

```text
Test Files  4 passed (4)
Tests       22 passed (22)
```

Breakdown: store 5, RunView 7, ReviewPanel 7, and RunResults 3.

## Adjacent regression evidence

The research-engine service, setup/editor, timeline, store, review, and results
batch passed:

```text
Test Files  8 passed (8)
Tests       54 passed (54)
```

This includes the 18 generated-contract service cases and Task 6's setup,
topology, and six-step rendering coverage.

## Quality checks

- Frontend `type-check`: passed.
- ESLint on all eight Task 7 TypeScript/TSX paths: passed with no diagnostics.
- Prettier check on all eight Task 7 TypeScript/TSX paths: passed.
- Blocking frontend lint-debt ratchet: passed at 113 errors and 1,961 warnings,
  below the committed baseline of 116 errors and 2,003 warnings. All remaining
  diagnostics are outside the Task 7 paths.
- TypeScript exclusion ratchet: passed; 21 baselined exclusions and no new
  production exclusion.
- Full human-readable frontend lint remains an advisory repository baseline
  failure. Its earlier draft run reported 115 errors and 1,974 warnings; the
  final equivalent all-tree JSON scan reports 113 errors and 1,961 warnings
  after the Task 7 paths were made clean.
- `git diff --check`: passed.
- Frontend commands report the repository engine warning because this worker
  has Node 22.22.0 while `package.json` requests Node 24.x; every passing command
  above completed under the available runtime.

## Files changed

- `frontend/src/store/research-engine-store.ts`
- `frontend/src/store/__tests__/research-engine-store.test.ts`
- `frontend/src/components/research-engine/RunView.tsx`
- `frontend/src/components/research-engine/ReviewPanel.tsx`
- `frontend/src/components/research-engine/RunResults.tsx`
- `frontend/src/components/research-engine/__tests__/RunView.test.tsx`
- `frontend/src/components/research-engine/__tests__/ReviewPanel.test.tsx`
- `frontend/src/components/research-engine/__tests__/RunResults.test.tsx`

## Self-review

- Confirmed persisted state is hydrated before SSE and remains the only shared
  run-state cache.
- Confirmed late hydration/events from a previous run cannot repopulate the
  current page and clean stream ends reconnect only while the same run remains
  pending or running.
- Confirmed the UI never labels component-local review drafts as durable and
  always sends the server descriptor's exact kind, index, and output hash.
- Confirmed verification override uses the explicit typed body and ordinary
  resume is absent from verification and review pauses.
- Confirmed final approval requires `final_status=verified`, while unverified
  and no-evidence downloads preserve their visible outcome labels.
- Confirmed every download target is produced by `getRunExportUrl` and no
  client artifact serializer was added.

## Concerns

None for Task 7. Task 8 lifecycle certification was not changed.

## Important-findings follow-up

Five focused regressions were added on base
`bb2e393911ec0c1f4f4fdc07b810ec22cc2288ad` before the follow-up production
edits. The combined RED run reported `8 failed, 20 passed` across the four Task
7 files: one store failure, one results failure, and six RunView failures.

- Oversized pending-review projections now resolve through the single run store
  only when the persisted step matches the descriptor's run, index, stage, and
  exact output hash. Extraction reviews regain the complete data and quoted
  evidence, while final review eligibility and summary rendering use the full
  persisted export. The immutable server descriptor remains the submission
  authority.
- Every clean stream EOF, missing body, non-OK response, or connection failure
  triggers a durable run/step refresh before the UI decides what to do. Durable
  paused, completed, and failed runs stop immediately. Active runs retry at
  bounded exponential delays for three total attempts and then show a safe
  visible terminal error.
- `refreshRun` now reports failure to callers after storing its safe UI error.
  Stale-review and accepted-review paths therefore tell the reader to reload
  when the authoritative refresh fails instead of announcing a successful
  refresh.
- Provider completion reads the production
  `coverage.provider_results` array. The regression fixture uses that artifact
  shape and does not depend on the old fixture-only `coverage.providers` map.
- Run lifecycle notifications are monotonic after `completed` or `failed`, so
  late start/pause and conflicting terminal events cannot regress the run.
  A real paused-to-running resume remains valid.

Follow-up GREEN and quality evidence:

```text
Focused Task 7:             4 files, 28 passed
Adjacent research-engine:  8 files, 60 passed
Frontend type-check:        passed
Scoped ESLint:              passed
Changed Prettier:           passed
Lint-debt ratchet:          passed (113 errors, 1,961 warnings; baseline 116/2,003)
TypeScript exclusions:      passed (21 baselined exclusions)
git diff --check:           passed
```

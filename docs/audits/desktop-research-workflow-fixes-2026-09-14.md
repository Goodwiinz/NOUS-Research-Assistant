# Desktop research workflow repairs — 2026-09-14

## Scope

Five approved desktop repairs from the research-assistant workflow audit. Mobile,
answer grounding, document-context handoff, and dashboard metrics are not changed.

| Observed gap | Repair |
| --- | --- |
| Library title input was not sent | Construct requests from synchronous page/filter inputs; retain latest-response protection. |
| Document citations displayed “Untitled” | Normalize generated citation wire fields at the service boundary. |
| Working folders omitted attached sources without inline markers | Share the message footer's inline-first, attached-source fallback policy. |
| Markdown downloads lost source links | Use trusted frontend document URLs or safe external identifiers, with visible page numbers. |
| Reload/auth recovery could lose the selected thread URL | Forward refreshed cookies into the current server render and preserve safe login return paths. |

The stale forwarded-cookie defect was reproduced. It does **not** establish that
the previously observed resume-stream HTTP 500 caused the intermittent redirect.
Transient verification errors stay fail-closed at the current URL; recovery uses
browser reload because the existing global error screen has no Retry control.

## Regression evidence

- Search: new request-input regressions failed before implementation and passed
  afterward, including exact titles, batched filters, refresh, and pagination.
- The stale-response test failed when the guard in `useDocuments.ts` was disabled
  and passed after restoration; older responses cannot replace the latest page.
- Citation API mapping: five tests failed on raw snake_case passthrough and passed
  with normalization, including nullable author metadata.
- Folder selection: attachment fallback failed before the shared policy; valid
  inline selection, deduplication, and thread isolation remain tested.
- Export: seven regressions failed before linked source rendering. Coverage
  includes absolute URLs, page labels, unsafe destinations, credential redaction,
  citation options, soft-deleted content, and eager loading.
- Follow-up export tests cover OAuth/OIDC credentials in URL fragments; ordinary
  document anchors remain usable.
- Auth: route recovery failed before the fix; disabling refreshed-cookie
  forwarding failed its renderer-cookie regression. Definitive and transient
  verification failures cannot render protected content, even with a user payload.
- Independent read-only review covered all five fixes and prompted the OIDC
  credential-key hardening and the test-animation timing correction.

The first full frontend run had 1,978 passes and one timing-sensitive failure in
the existing own-send scroll test; its immediate isolated rerun passed. Review
identified that the test waited for the scroll call, then asserted DOM removal
before the button's exit animation necessarily finished. The test now waits for
removal as well, without changing the scroll behavior or weakening the assertion.

Final verification:

- Node 24: `pnpm --dir frontend test --maxWorkers=4 --minWorkers=1 --reporter=dot`
  passed **1,979 tests in 292 files** after the test-only timing correction.
- The three scoped backend export test files passed **17 tests**; pytest emitted
  the existing unknown `timeout` configuration warning. The full backend suite
  was not run.
- `scripts/ci/run_local_ci.sh --base origin/develop --skip-tests --frontend`
  passed every blocking gate that ran. Existing full-tree lint debt remains
  advisory. Generated-type regeneration and migration execution probes were
  skipped because those artifacts did not change, not counted as verified.
- Final touched Python files passed Ruff, Black, isort, and added-file mypy;
  final new/touched frontend test files passed ESLint. Production type-checking
  passed, as did directory documentation and diff whitespace checks.
- Final independent review found no remaining actionable findings.

## Browser verification limitation

Desktop browser verification was attempted against the actual Next app with a
local-only auth/API fixture; no production projects or documents were changed.
The app did not reach login: Turbopack panicked while compiling routes. Webpack
fallback then failed at login with `(0, _react.cache) is not a function` under the
existing React alias configuration. The browser and local servers were stopped.
These startup failures are **not** passing end-to-end verification, and dependency
or bundler changes are intentionally outside this five-fix PR.

The actual patched Markdown formatter generated a fixture containing a stable
document hyperlink and `(p. 3)`, but its browser download path was not verified.

## PR #1643 CI correction

The first remote pipeline identified two distinct failures:

- Bandit 1.9.4 rejected the dynamic `Markup(...)` wrappers in the new citation
  formatter (B704). The local CI helper had not exercised this security gate.
  The formatter now returns ordinary text through Jinja's existing autoescape;
  Markdown and HTML escaping are applied without double-escaping angle brackets.
- E2E stopped before running tests because Docker Hub's authentication endpoint
  reset the connection while BuildKit fetched `docker/dockerfile:1`. No E2E
  assertion failed in that run. The release gate correctly reported both failures.

The correction passed 20 scoped export tests, including real CommonMark rendering
of linked/unlinked untrusted titles and preservation of URL query parameters.
Both new HTML-escaping cases failed before the correction. The exact security
command failed before and passed afterward:

```sh
bandit -r backend/src -ll -ii -x tests -b backend/.bandit-baseline.json
```

Bandit was pinned to 1.9.4, matching CI. No security baseline, suppression, workflow,
or release requirement was relaxed. Independent review found no remaining issue.
The corrected commit must still pass its remote pipeline, including the E2E retry.

### Required desktop follow-up

- [ ] On a runnable preview, search an exact library title and inspect the request.
- [ ] Open document details and confirm citation title and page.
- [ ] Open a thread whose answer has attached sources but no inline markers;
  confirm the same sources appear in the footer and working folders.
- [ ] Download Markdown and follow a source hyperlink while authenticated.
- [ ] Reload the selected thread, and verify a required login returns to it.

## Deferred mobile TODO

- [ ] Fix the floating agent control overlapping Send at 390 × 844.
- [ ] Review mobile project/working-folder visibility.
- [ ] Retest citation, attachment, export, and reload flows on mobile afterward.

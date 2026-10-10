# Isolation, reconnect and sidebar repair evidence

Local verification on 2026-10-09, based on `331ef5e84e9bba33234990570eacb9ddb3056150`.
No hosted CI, deployment, live credentials or Linear changes were exercised.

## Reconnect credentials (GOO-416)

A full reconnect to the same chat now removes the superseded credential only
once the replacement connection is durably saved. Reuse retains its credential.
Tests cover expired/failed probes, expanded scopes, failed connection writes
and failed cleanup without losing the usable connection.

The regression run failed three cases before the fix
(`/tmp/isolation-416-red.log`). Moving cleanup before the connection write
failed `failed connection persistence keeps the previous binding and credential`
(`/tmp/isolation-416-order-mutation.log`); the mutation was restored.

```sh
pnpm --filter @nous/harness-bridge test --test-name-pattern='failed connection persistence' test/connect-reuse.test.ts
pnpm --filter @nous/harness-bridge test
```

The full bridge suite passed 232 tests, including real subprocess restart and
interrupt-claim cases (`/tmp/isolation-bridge-stable-final.log`).

## Tenant isolation and search (GOO-418)

Document collection access rejects an absent identity or organization;
workspace public visibility still requires an authenticated identity and still
permits an authenticated cross-organization reader. Native PostgreSQL document
filters bind enum member names. Single-thread export cases now pass the actual
query parameter and prove Markdown, JSON and HTML response formats before
exercising removed-member and foreign-account denials.

Before fixing production, SQLite authorization/export checks failed four cases
(`/tmp/isolation-418-red-sqlite.log`) and all three native-enum PostgreSQL cases
failed (`/tmp/isolation-418-red-pg.log`). The anonymous public-workspace case
also failed before the explicit identity check
(`/tmp/isolation-418-public-red.log`).

The complete two-account suite passed 166 tests with 8 optional PDF cases skipped
on disposable PostgreSQL 16 (`/tmp/isolation-418-green.log`). After adding the
public identity check, the complete document/chat subset passed 92 tests with
the same 8 PDF skips (`/tmp/isolation-418-auth-final.log`). SQLite is used for
the existing chat/document fixtures; PostgreSQL tests use a real native enum,
organization predicates, search vector, totals and pagination.

```sh
ENVIRONMENT=testing TWO_ACCOUNT_PG_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55439/postgres PYTHONPATH=backend /tmp/nous-qa-fix-venv/bin/python /tmp/run-isolation-tests-20261009.py -q backend/tests/integration/two_account -o addopts=''
```

The temporary runner preloads `kombu.transport.redis`, then supplies a retired
`qdrant_client.QdrantClient` import stub required by the integration parent
fixture. Database sessions, HTTP requests and authorization are real. The
fixture already mocks unrelated external services; no vector service is used
by these tests. The runner does not change repository files.

The required broader backend matrix passed 1,694 tests with 72 fixture skips;
one untouched architecture test failed under Python 3.13 because it hashes a
Python 3.11 AST (`/tmp/isolation-backend-matrix.log`). The protected
`tools_impl.py` equals the pinned base byte-for-byte. Removing only the added
AST `type_params` field reproduces the committed Python 3.11 expected hash
`993598cbd7392392b3ec221786ac8e44d6d8946bf49d226d225aa630500af133`.

## Active sidebar virtualization (GOO-87)

The current `ChatSidebar` uses the existing `react-window` dependency for one
measured list containing date/pinned headings and conversation rows. Large
lists keep bounded mounted rows, exact logical list positions, active-conversation
reveal, keyboard navigation, cross-window Tab order and focus recovery.
Appending older conversations preserves the current scroll window. Selection,
search, rename/delete, previews and the older-thread control remain available.

The original implementation failed bounded-row and distant-selection tests
(`/tmp/isolation-87-red.log`). The cross-window Tab regression failed the checkbox
focus assertion (`/tmp/isolation-87-tab-red.log`). Revealing the active row on
every list change failed the older-page append assertion
(`/tmp/isolation-87-append-red.log`). The final focused suite passed 27 tests
(`/tmp/isolation-87-frozen-focused.log`).

```sh
pnpm --dir frontend exec vitest run src/components/chat/__tests__/ChatSidebar.test.tsx
pnpm --dir frontend test
pnpm --dir frontend test:coverage
node scripts/ci/check_frontend_coverage.mjs frontend/coverage/coverage-summary.json
```

A local Chromium component harness rendered the actual styled sidebar with
1,000 conversations. Only 9 conversation rows were mounted. Home/End/Enter,
selection of rows 0 and 999, search/clear and pagination controls passed with
no browser errors. The final row's actual bounds fit inside the list viewport
(`/tmp/isolation-sidebar-browser-stable4.log`, screenshot
`/tmp/isolation-sidebar-browser-20261009.png`). The temporary harness was removed.

The final frozen-source full frontend suite passed all 378 files and 2,917 tests
(`/tmp/isolation-frontend-frozen-final.log`). Coverage also ran all 378 files and
2,917 tests successfully. The committed coverage
floors passed without modification: lines/statements 51.66% (floor 47.6%),
functions 64.75% (62.38%), branches 80.04% (79.24%). Logs:
`/tmp/isolation-frontend-coverage.log` and
`/tmp/isolation-frontend-coverage-floors.log`.

## Lease watchdog timing (GOO-397 / GOO-432)

The automatic offline-watchdog test now controls Date and timers while retaining
the real Journal. It reaches the original lease deadline at t=25, proves the
renewed lease remains valid at t=89, and proves exactly one interrupt and
quarantine at t=90. Real process-restart tests retain their process boundary.

Removing the renewed-lease check failed the early-interrupt assertion
(`/tmp/isolation-397-mutation.log`). Mutating the rearmed deadline to t=60 and
making the guard accept that earlier deadline also failed the t=89 assertion
(`/tmp/isolation-397-deadline60-mutation.log`). Both production mutations were
restored; `journal.ts` has no diff.

```sh
pnpm --filter @nous/harness-bridge test --test-name-pattern='watchdog fires offline' test/recovery.test.ts
```

## Repository checks

Changed-file ESLint passed; frontend type-check, full-tree Ruff, changed-file
Ruff/Black/isort, directory documentation, feature map, OpenAPI freshness,
single Alembic head and frontend quality/exclusion ratchets passed. Full-tree
frontend debt remains at its existing baseline (98 errors, 1,878 warnings).
The first local wrapper found a missing declared `yaml@2.9.1` dependency in
reused dependencies. Supplying it only in this worktree's ignored e2e
`node_modules` made the failed QA-script gate pass all 93 tests
(`/tmp/isolation-qa-node-green.log`). Shared dependencies were not modified.
Contract generation and migration replay were skipped because this branch
changes neither schemas nor migrations. Local logs are ephemeral evidence,
not hosted-CI results.

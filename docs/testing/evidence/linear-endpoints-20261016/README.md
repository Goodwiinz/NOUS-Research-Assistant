# Endpoint implementation evidence (2026-10-16)

Scope: GOO-219 and GOO-237 through GOO-241. Branch:
`codex/goo-219-linear-open-issues`; reviewed base:
`4c4600392c0e4a3705f58b4abcdf6868c8e226bc`.

## Verified behavior

- The main authenticated API client delegates endpoint selection to the shared
  resolver and retains the existing local-UI/remote-backend Next.js proxy
  contract. Direct streaming callers retain their local-origin policy.
- Remote pages discard baked loopback endpoints, including 127/8, trailing-dot
  localhost, `.localhost` and IPv6 loopback. Versioned bases retain one version.
- Standard and optimized Next.js rewrites share the server resolver. Production
  configuration requires an explicit backend; public env values stay unset
  rather than becoming loopback defaults.
- SSR and proxy authentication retain configured Supabase endpoints and the
  server-only network override; missing URL/key configuration fails clearly.
- The runtime-source loopback ESLint guard is active with documented test and
  local-development exceptions. Three production type exclusions were removed.
- The optional analytics WebSocket client uses one service per hook mount,
  counts retries per client, and reports through the actual store contract.
  Two unreferenced legacy graph clients were removed after a repository-wide
  caller search. No current HTTP request/response schema changed.

## Executed checks

| Check | Result | Boundary |
| --- | --- | --- |
| `pnpm --dir frontend test` | 389 files, **3,039 passed** | Owning frontend unit/integration/CLI workspace tests; no browser claim |
| `scripts/ci/run_local_ci.sh --base origin/develop --frontend --skip-tests` | **All blocking gates passed** | Pinned Ruff, directory docs, feature map, OpenAPI freshness, Alembic structure, TypeScript, QA node tests, ESLint comparator and exclusion comparator |
| Full ESLint | Advisory inherited debt remains | Changed-file comparator passed; no floor lowered |
| Generated API types | SKIPPED | Both committed contract files unchanged |
| Evidence migration delta / empty-chain advisory replay | SKIPPED | No Alembic revision changed in this branch |
| Browser login | **2 passed; both screenshots inspected** | Local frontend + configured live dev API; backend SHA unknown; [target and visual record](../verify-login-20261016/README.md) |
| Independent final code review | **No source defects found** | Claude Fable `claude-fable-5-1`, session `d257f839-4148-4785-a7c1-9d7e820e245a`; final read-only tripwire false |
| Independent security review | **No source defects found** | Separate Fable context `796e899c-c80b-4dd4-a520-82f1d43286a8`; final read-only tripwire false |

The two reviewers initially found hook churn and an unbounded retry counter;
both were reproduced and fixed before their final passes. Reviewers inspected
source and tests; the orchestrator executed the gates above. Raw relay outputs
and test logs remain local under `/tmp/nous-linear-review/` and are not committed.

## Causal and mutation checks

Nine initial endpoint/configuration cases failed on the original source and
passed after repair. Seven additional hook/retry/loopback cases were observed
failing before their repairs and all 33 owning focused cases then passed.

For the hook ownership and retry guards, the final production source was
mutated separately, the named assertion failed on the intended defect, and the
original file was restored byte-identically before each green rerun:

```sh
pnpm --dir frontend test src/services/__tests__/publicEndpoints.integration.test.ts -t "keeps one WebSocket"
pnpm --dir frontend test src/services/__tests__/publicEndpoints.integration.test.ts -t "stops reconnecting"
```

Replacing the lazy hook owner with a render-created client produced two sockets
instead of one. Removing the retry guard produced four sockets instead of the
allowed three. Restored `websocketService.ts` SHA-256:
`4f8a10eed338c02aceec413b75228f027d9b35054e2c1a2b8f0ed81938920b03`.
The guard locations and commands are also recorded beside the tests.

## Limits

No deployment, merge, live flag change, embedding backfill or manual Linear
status change was performed. The login run proves its stated mixed target and
does not establish a local-backend, analytics-stream, migration or deployed
candidate acceptance result. Production build behavior is covered through the
actual configuration modules; a complete optimized production build was not
run. Hosted CI is a separate gate and must be bound to the published PR head.

## Coverage amendment after first hosted run

PR [#2009](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/2009),
head `aa9d402a0d1c41c906007982f6d2ae28dfa306f2`, passed all 3,039 frontend
tests in [producer job 116873435089](https://github.com/Goodwiinz/NOUS-Research-Assistant/actions/runs/38870271504/job/116873435089).
That job failed its subsequent coverage ratchet: function coverage was 64.26%,
below the existing 64.75% floor. Its Release Gate failure was downstream of
that producer. This was not an infrastructure failure or a confirmed flake.

Additional caller tests cover authenticated deployed-origin reads across the
analytics service exports, encoded query arguments, factory initialization
without token persistence, and API health success/failure. Production source
and all coverage floors are unchanged. The exact local CI command
`CI=true pnpm --dir frontend test:coverage` passed **389 files, 3,056 tests**.
`node scripts/ci/check_frontend_coverage.mjs frontend/coverage/coverage-summary.json`
passed: functions **64.80%**, lines/statements **53.50%**, branches **80.47%**.

Fable resumed correctness session `d257f839-4148-4785-a7c1-9d7e820e245a`
completed its independent test-delta review with no concrete defect and a
false read-only tripwire. The earlier separate security review still applies
to unchanged production source. A new hosted run must verify the next head.

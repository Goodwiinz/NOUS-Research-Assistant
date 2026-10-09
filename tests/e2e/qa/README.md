# NOUS workflow QA CLI

`pnpm qa:nous --list` lists the reusable public, authenticated workflow, and
adversarial scenarios. A campaign writes a private JSON report and a
standalone escaped HTML report:

```sh
pnpm qa:nous \
  --base-url http://127.0.0.1:3000 \
  --api-url http://127.0.0.1:8000/api/v1 \
  --suite smoke \
  --output-dir .nous-qa-reports/local-smoke
```

The default suite is `smoke`. `workflow`, `adversarial`, and `all` require
explicitly bounded selection. Authenticated cases require either
`NOUS_QA_EMAIL` and `NOUS_QA_PASSWORD` or a Playwright `--storage-state` file;
write and model cases additionally require `--allow-writes`. Credentials are
never accepted as flags and are excluded from reports. Live workflow and model
campaigns are intentionally separate from the controlled browser transport
fault case.

The CLI accepts only `http` and `https` targets without URL userinfo, query,
or fragment data. Use `--api-url` explicitly when the backend is on a different
origin; the runner does not follow redirects to discover an API. Every request
has a timeout, one browser context is used, model turns are bounded by
`--max-turns` (default 12, maximum 50), and the runner performs no load or
bulk-destructive operation.

Each mutating scenario creates resources with a unique `NOUS QA <run-id>`
prefix and registers the exact IDs returned by the production API. Cleanup can
target only those registered IDs, in reverse creation order. A failed cleanup
is visible as `incomplete` and retained IDs remain in the private report for
manual recovery; no title search or broad deletion is used.

Chat history/draft and missing-thread coverage create their threads inside the
workspace `/chat` will open (the account workspace with the highest collection
+ conversation count from `GET /api/v2/workspaces`, the frontend's own rule);
that workspace is never registered or deleted, only the conversations and
threads created in it. A planted default-workspace cache is not used: the
frontend clears it on every page load. The missing-thread case asserts the
product's recovery (unavailable toast, stale id dropped from the URL, the
recovered thread reads 200) and records which thread it landed on without
asserting it. Q&A and Stop streams
must carry an exact ledger-owned `thread_id`. Every accepted run is registered
centrally. A transport timeout before acceptance is recorded as an uncertain
stream, retains its owned fixture tree, and makes cleanup incomplete instead
of claiming that the server did not create a run.

Exit codes are `0` when every selected case passes and cleanup completes, `1`
when a selected assertion fails, and `2` for invalid configuration, no selected
cases, blocked or skipped prerequisites, or incomplete cleanup. Missing
credentials and unavailable optional capabilities are therefore never passes.

`/health` and `/health/readiness` are recorded as health evidence. The public
server `version` is not treated as a commit identity. When a deployment gate
is needed, supply `--expected-backend-sha` and an operator-observed JSON file:

```json
{
  "targetUrl": "https://qa.example.test",
  "backendSha": "0123456789abcdef0123456789abcdef01234567",
  "frontendSha": "fedcba9876543210fedcba9876543210fedcba98",
  "observedAt": "2026-09-17T12:00:00Z",
  "provenance": "kubectl deployment image and GitHub workflow metadata"
}
```

`targetUrl` must match the explicitly configured backend API origin or API
path. When frontend and backend use different origins, a frontend URL cannot
stand in for backend deployment evidence. The evidence is validated against
that backend target and a 24-hour freshness window, then labeled with its
supplied provenance. It is an external observation, not a live server
attestation. An expected SHA with no matching server identity or valid
matching evidence blocks writes and model execution.

Reports automatically capture the local repository's git HEAD and dirty state
with explicit provenance. `NOUS_QA_SOURCE_SHA` and `NOUS_QA_SOURCE_DIRTY` may
override those observations and are labeled as overrides; unavailable git
metadata is recorded as unknown. Reports also contain sanitized paths/statuses,
scenario assertions, and bounded evidence. They omit request bodies, response
bodies, cookies, authorization headers, tokens, passwords, and unrelated
account content. HTML values are escaped and the document has no scripts or
external assets. The tool itself does not deploy, push, open a PR, or make live
requests unless an operator invokes it after the deployment gate.

## Verification selection and evidence

`--features a,b` and `--changed-from <ref>` select scenarios through
`docs/engineering/feature-map.yaml` (the suite becomes `all`). `--changed-from`
diffs `<ref>...HEAD` plus untracked files against each feature's `owns`
globs; when no mapped feature changed it exits 2, never a pass. `--list`
combined with either flag (or `--scenario`) prints only the selected
scenarios, which is a dry run of the selection.

Every run records video and a Playwright trace, and scenarios call
`evidence.checkpoint('<name>')` for the full-page screenshots named in
`docs/engineering/flows/<feature>.md`. All of it goes under `--evidence-dir`
(default `.verify-artifacts/<run-id>/`, gitignored). With credentials, tracing
starts only after login reaches a protected route and pauses for any later
login, so the password is not recorded; traces still carry session cookies
and tokens, so never attach, commit or upload a `trace*.zip`. `--evidence-record DIR`
also writes the committed text README described in
`docs/engineering/verification.md`; it lists file names and never links
binaries. `scripts/verify/boot_local.sh` starts a local target. The unit
suites are `node --test tests/unit/scripts/nous-qa.test.mjs
tests/unit/scripts/nous-verify.test.mjs` (no browser needed).

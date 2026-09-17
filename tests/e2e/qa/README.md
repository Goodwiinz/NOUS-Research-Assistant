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

# Tool guidance

## Scope and sources of truth

This directory currently contains the standalone
[`tools/nous-playwright`](nous-playwright/) package. Its
[`README.md`](nous-playwright/README.md), `CODEX_HANDOFF.md`, package manifest,
Playwright config, and `.gitignore` are the sources of truth. It is not the
repository's older global E2E setup and does not inherit seeded credentials,
global setup/teardown, or a root web-server lifecycle.

The package is pinned to Node 24 and `pnpm@10.18.2`; its isolated install uses
`--ignore-workspace`. The test targets a real NOUS service when authenticated,
so a discovery result and the earlier manual walkthrough are not an
authenticated end-to-end pass.

## Invalid patterns

- Keep this package standalone. Do not add its private auth/session or runtime
  artifacts to the root E2E suite, root lockfile, or a commit without an
  explicitly reviewed integration plan and the current suite's setup/teardown
  audit.
- Treat `.auth/`, `test-results/`, `playwright-report/`, traces, videos,
  screenshots, and `created-resources.json` as private workspace artifacts.
  They may contain authenticated session state or workspace content; do not
  commit, upload, paste, or quote them in reports. The package `.gitignore`
  is part of this boundary.
- Never embed, request, or print passwords, tokens, or storage-state contents.
  `pnpm auth` is interactive authentication through the user's normal login;
  it requires explicit user authorization and the saved state file remains
  private.
- An authenticated test creates a unique project and conversation and retains
  them for inspection. Do not run it, create live projects, approve live tool
  actions, or otherwise mutate a live project without explicit user
  authorization for the target environment. Do not delete the existing source
  paper or remove approval gates/retries to obtain a green result.
- Do not describe `pnpm test:list`, static selector checks, or a manual
  walkthrough as proof of authenticated persistence, citation, approval,
  reload, or live API behavior. Preserve one worker and zero automatic retries when the real test is authorized.

## Required workflow

Before editing, read the package README and handoff, then inspect its own
config and test consumers. Keep the install boundary isolated and use the
matching Node/pnpm versions. For a real run, confirm the target URL and
matching indexed-paper identity together, obtain authentication through the
user's normal flow, and record only non-sensitive resource/artifact locations
privately. If auth or a live target is unavailable, finish static/discovery
checks and report the live run as blocked rather than inventing credentials or
a passing result.

## Verification

The only offline structural check for this package is test discovery, run from
`tools/nous-playwright` with its isolated dependencies installed:

```sh
pnpm test:list
```

This lists Playwright tests and does not authenticate, contact the target
service, create a project, or prove the end-to-end workflow. The authenticated
test and interactive auth are service/credential-dependent and require the
separate authorization described above; if not run, report them as `NOT RUN`
or blocked.

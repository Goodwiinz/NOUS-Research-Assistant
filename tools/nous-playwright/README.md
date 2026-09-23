# NOUS: conversation-to-project Playwright test

Automates the workflow demonstrated on goodwiinz.tech: inspect **Attention Is All You Need**, ask a question, create a project through chat, approve creation and document attachment, and verify the saved paper after reload.

## Run locally

Use the repository-pinned Node 24 and pnpm 10.18.2. From `tools/nous-playwright`:

```bash
corepack enable
pnpm install --ignore-workspace
pnpm exec playwright install chromium
pnpm auth
pnpm test:headed
pnpm report
```

`--ignore-workspace` keeps this standalone package separate from the root workspace install and lockfile. This handoff is not yet integrated into the repository E2E suite.

`pnpm auth` automatically uses headless Chromium on Linux without `DISPLAY`, including SSH sessions. It prompts for your email and password in the terminal; the password is hidden. On a computer with a display, it opens a browser for you to sign in, then asks you to press Enter in the terminal. Login is outside the recorded test. The resulting `.auth/nous.json` is a private authenticated session file with owner-only permissions and is ignored by Git.

For a server without a display, run from the repository root:

```bash
corepack pnpm@10.18.2 --dir tools/nous-playwright auth --headless
corepack pnpm@10.18.2 --dir tools/nous-playwright test
```

For unattended authentication, supply `NOUS_EMAIL` and `NOUS_PASSWORD` through your runtime's secret environment. The helper also accepts `AGENT_QA_EMAIL` and `AGENT_QA_PASSWORD`; it never mixes the two pairs. For SSO or MFA, use `pnpm auth --headed` on a computer with a display and transfer the private session file to the test runtime.

The account must have the indexed paper available. The default document ID is the one verified in the demo. For another deployment, set the matching ID **for the same paper**:

```bash
export NOUS_BASE_URL='https://your-nous-deployment.example'
export NOUS_PAPER_ID='your-indexed-paper-uuid'
pnpm auth
pnpm test:headed
```

`NOUS_AUTH_STATE` optionally changes the state-file path. These are shell environment variables; this package does not automatically load `.env` files. Run `pnpm auth` again when the session expires.

## What the test checks

1. The source exists, is ready for retrieval, and its summary mentions attention mechanisms.
2. A new chat returns an **assistant** answer about self-attention with a numbered source button. Opening that source and then its document must show the expected title and a full-page URL containing the exact document ID. User text cannot satisfy the answer assertion.
3. The project-creation approval contains the unique requested project name and no extra actions.
4. The attachment approval refers to the expected paper. A separate browser page verifies that the target project has the expected name and is still empty before attachment approval.
5. The real project-documents API response and the UI both show the paper after approval.
6. A full reload still shows exactly one membership with the expected project ID, document ID, title, and indexed status.

One worker and zero automatic retries prevent parallel runs and automatic retry-created projects. Each invocation creates a uniquely named `NOUS E2E - Transformer Research - ...` project and a conversation. They remain for inspection; `created-resources.json` in the report identifies them. The original paper is never deleted.

## Artifacts

- Video from every test run: `test-results/**/video.webm` (Playwright saves it when the browser context closes).
- Seven named screenshots and created-resource metadata in the HTML report.
- A trace and additional screenshot retained on failure.
- View the report with `pnpm report`; open a downloaded trace using `pnpm exec playwright show-trace path/to/trace.zip`.

Treat authenticated traces, videos, and session files as private. The test never records the login form, but its later artifacts contain your workspace content.

## Scope and validation

The authenticated Chromium workflow passed against `https://goodwiinz.tech` on 2026-09-13: **1 passed (1.8m)**, using Node 24 and pnpm 10.18.2. It exercised real retrieval, the source-to-document viewer, both exact approval gates, and single-document persistence after reload. See `CODEX_HANDOFF.md` for the run history and remaining scope.

Run `pnpm test:auth` for browser-based helper regression tests against a local login fixture; these cover headless authentication, automatic display detection, missing credentials, rejected login, and private session-file permissions.

This is a real-service integration test with a three-minute budget per agent transition. If the model asks an extra clarification, proposes another tool, chooses the wrong document, fails to finish, or duplicates final assistant rows, the test fails rather than accepting a different workflow. Inspect its trace before changing an assertion.

It does not prove broad answer faithfulness or measure latency percentiles. It checks the citation-to-document viewer transition and its full-page URL, but does not navigate that full-page link. During the manual walkthrough the dedicated Search page returned no result for a sentence query; this test exercises the Chat route.

This is standalone and does not invoke the repository's older global setup, teardown, web-server startup, or seeded default credentials. Invoke this folder's own config. See `CODEX_HANDOFF.md` for continuation and eventual integration into `tests/e2e/`.

References: [Playwright authentication](https://playwright.dev/docs/auth), [web-first assertions](https://playwright.dev/docs/test-assertions), [video recording](https://playwright.dev/docs/videos).

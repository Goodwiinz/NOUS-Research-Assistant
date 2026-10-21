# Verification evidence: login (2026-10-16)

Source: `6928d3d90a8d7f9e419015c1511eae213ec968ad` (dirty; git HEAD; git worktree status).
Target: frontend `http://127.0.0.1:3307`, API `https://dev-api.goodwiinz.tech/api/v1`.
Backend identity: not exposed.
Command: `'pnpm' 'qa:nous' '--features' 'login' '--allow-writes' '--timeout-ms' '180000' '--base-url' 'http://127.0.0.1:3307' '--api-url' 'https://dev-api.goodwiinz.tech/api/v1' '--evidence-record' '[path]'`. Run id `20261016072744-85097531`, 2026-10-16T07:27:44.225Z to 2026-10-16T07:27:50.570Z.
Binaries (not committed): `.verify-artifacts/20261016072744-85097531` with 2 checkpoint PNG(s), 1 video(s) (`page@574f2c79b7f00228b21eadb1f1317357.webm`), trace(s) `trace.zip` (secret-bearing: session cookies and tokens; never attach, commit or upload).

Overall: PASS (assertions); visual confirmation pending. A PASS here is assertion-level only until the checkpoints below were viewed and the feature pass criteria confirmed by the operator (record that in the PR).

| Scenario | Result | Reason |
| --- | --- | --- |
| smoke.login-availability | PASS |  |
| workflow.login-authenticated | PASS |  |

## Checkpoints

| Scenario | Checkpoint | File |
| --- | --- | --- |
| smoke.login-availability | login.form | smoke.login-availability--login.form.png |
| workflow.login-authenticated | login.landed | workflow.login-authenticated--login.landed.png |

Cleanup: complete.

## Operator confirmation (2026-10-16)

Both checkpoint PNGs were opened and visually inspected. `login.form` shows
the labelled email/password fields and sign-in control. `login.landed` shows
the protected dashboard shell and authenticated user identity after leaving
the login page. The login feature criteria pass for this target; dashboard
metrics and other features were not assessed by these screenshots.

This run used the modified local frontend and the configured live dev API.
It is not a full-stack local-backend pass, and the live backend SHA remains
unknown. The runner's assertion result and this visual confirmation do not
establish deployment or migration acceptance for the other issues.

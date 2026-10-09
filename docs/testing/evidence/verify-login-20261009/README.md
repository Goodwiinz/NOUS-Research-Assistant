# Verification evidence: login, chat-send-stream-reload, hitl-approve-deny, document-upload, project-creation-via-chat (2026-10-09)

Source: `e34062771d8691170d33562d62154b0b3cf58f02` (dirty; git HEAD; git worktree status).
Target: frontend `https://goodwiinz.tech`, API `https://dev-api.goodwiinz.tech/api/v1`.
Backend identity: `a0d4c6b3d6a81352b870450a5bc74708ca6843c9` (GitOps values-aws.yaml commit da4361e1d (#1953, propose dev image a0d4c6b) merged on develop; ArgoCD sync and Vercel frontend SHA not independently verified).
Command: `'pnpm' 'qa:nous' '--features' 'login,chat-send-stream-reload,hitl-approve-deny,document-upload,project-creation-via-chat' '--allow-writes' '--base-url' 'https://goodwiinz.tech' '--api-url' 'https://dev-api.goodwiinz.tech/api/v1' '--expected-backend-sha' 'a0d4c6b3d6a81352b870450a5bc74708ca6843c9' '--deployment-evidence' '[path]' '--evidence-dir' '[path]' '--evidence-record' '[path]'`. Run id `20261009053437-a72b9981`, 2026-10-09T05:34:37.632Z to 2026-10-09T05:35:35.413Z.
Binaries (not committed): `.verify-artifacts/verify-20261009-013437` with 2 checkpoint PNG(s), 1 video(s) (`page@f6a9fc0c0dbe6c4fc99e5be7ae125991.webm`), trace(s) `trace.zip`, `trace-2.zip`, `trace-3.zip`, `trace-4.zip`, `trace-5.zip` (secret-bearing: session cookies and tokens; never attach, commit or upload).

Overall: FAILED. A PASS here is assertion-level only until the checkpoints below were viewed and the feature pass criteria confirmed by the operator (record that in the PR).

| Scenario | Result | Reason |
| --- | --- | --- |
| smoke.login-availability | PASS |  |
| workflow.reload-persistence | PASS |  |
| workflow.login-authenticated | PASS |  |
| workflow.chat-send-stream-reload | FAILED | Scenario timed out after 30000ms |
| workflow.project-creation-via-chat | BLOCKED (NOT RUN) | Campaign halted after a timed-out scenario; the session remained quarantined to prevent late actions |
| workflow.hitl-deny | BLOCKED (NOT RUN) | Campaign halted after a timed-out scenario; the session remained quarantined to prevent late actions |
| workflow.document-upload-and-attachment | BLOCKED (NOT RUN) | Campaign halted after a timed-out scenario; the session remained quarantined to prevent late actions |

## Checkpoints

| Scenario | Checkpoint | File |
| --- | --- | --- |
| smoke.login-availability | login.form | smoke.login-availability--login.form.png |
| workflow.login-authenticated | login.landed | workflow.login-authenticated--login.landed.png |

Cleanup: complete.

## Operator review (2026-10-09)

Deployed lane, so this is not a nous-loop step 7b local `PASS`.

- `login`: **PASS**. Both checkpoints viewed: `login.form` shows the email and password fields and one Sign in button; `login.landed` shows the protected dashboard ("Overview, <account>").
- `chat-send-stream-reload`: the 30 s timeout was a tooling defect (agent turns outlast the default), fixed in the same PR by documenting `--timeout-ms 180000`. Superseded by `verify-chat-send-stream-reload-20261009-r2`.
- The three halted scenarios were re-run in the later records.

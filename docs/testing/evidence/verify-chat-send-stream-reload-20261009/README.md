# Verification evidence: chat-send-stream-reload, hitl-approve-deny, document-upload, project-creation-via-chat (2026-10-09)

Source: `e34062771d8691170d33562d62154b0b3cf58f02` (dirty; git HEAD; git worktree status).
Target: frontend `https://goodwiinz.tech`, API `https://dev-api.goodwiinz.tech/api/v1`.
Backend identity: `a0d4c6b3d6a81352b870450a5bc74708ca6843c9` (GitOps values-aws.yaml commit da4361e1d (#1953, propose dev image a0d4c6b) merged on develop; ArgoCD sync and Vercel frontend SHA not independently verified).
Command: `'pnpm' 'qa:nous' '--features' 'chat-send-stream-reload,hitl-approve-deny,document-upload,project-creation-via-chat' '--allow-writes' '--timeout-ms' '180000' '--base-url' 'https://goodwiinz.tech' '--api-url' 'https://dev-api.goodwiinz.tech/api/v1' '--expected-backend-sha' 'a0d4c6b3d6a81352b870450a5bc74708ca6843c9' '--deployment-evidence' '[path]' '--evidence-dir' '[path]' '--evidence-record' '[path]'`. Run id `20261009053559-cf5e3562`, 2026-10-09T05:35:59.999Z to 2026-10-09T05:38:32.380Z.
Binaries (not committed): `.verify-artifacts/verify-20261009-013559` with 1 checkpoint PNG(s), 1 video(s) (`page@5098bda0ad34e383f7ccf75e6fdf5d0d.webm`), trace(s) `trace.zip`, `trace-2.zip`, `trace-3.zip`, `trace-4.zip`, `trace-5.zip`, `trace-6.zip`, `trace-7.zip`, `trace-8.zip`, `trace-9.zip` (secret-bearing: session cookies and tokens; never attach, commit or upload).

Overall: FAILED. A PASS here is assertion-level only until the checkpoints below were viewed and the feature pass criteria confirmed by the operator (record that in the PR).

| Scenario | Result | Reason |
| --- | --- | --- |
| workflow.reload-persistence | PASS |  |
| workflow.chat-send-stream-reload | FAILED | Browser timeout during click (30000ms timeout) |
| workflow.project-creation-via-chat | FAILED | Browser timeout during click (30000ms timeout) |
| workflow.hitl-deny | FAILED | Browser timeout during click (30000ms timeout) |
| workflow.document-upload-and-attachment | PASS |  |

## Checkpoints

| Scenario | Checkpoint | File |
| --- | --- | --- |
| workflow.document-upload-and-attachment | upload.detail | workflow.document-upload-and-attachment--upload.detail.png |

Cleanup: complete.

## Operator review (2026-10-09)

Deployed lane, so this is not a nous-loop step 7b local `PASS`.

- `document-upload`: **PASS**. The `upload.detail` checkpoint was viewed: the document detail page renders the uploaded fixture's title and file details while processing.
- The three chat journeys failed with `Browser timeout during click`. The run video showed the composer's button is named `Send`, while the scenarios clicked `Send message`. That was a test defect, fixed in the same PR. Superseded by `verify-chat-send-stream-reload-20261009-r2`.

# Verification evidence: chat-send-stream-reload, hitl-approve-deny, project-creation-via-chat (2026-10-09)

Source: `fffd63e4ac0dda48606cdce5bd7ddec38740b61c` (dirty; git HEAD; git worktree status).
Target: frontend `https://goodwiinz.tech`, API `https://dev-api.goodwiinz.tech/api/v1`.
Backend identity: `a0d4c6b3d6a81352b870450a5bc74708ca6843c9` (GitOps values-aws.yaml commit da4361e1d (#1953, propose dev image a0d4c6b) merged on develop; ArgoCD sync and Vercel frontend SHA not independently verified).
Command: `'pnpm' 'qa:nous' '--features' 'chat-send-stream-reload,hitl-approve-deny,project-creation-via-chat' '--allow-writes' '--timeout-ms' '180000' '--base-url' 'https://goodwiinz.tech' '--api-url' 'https://dev-api.goodwiinz.tech/api/v1' '--expected-backend-sha' 'a0d4c6b3d6a81352b870450a5bc74708ca6843c9' '--deployment-evidence' '[path]' '--evidence-dir' '[path]' '--evidence-record' '[path]'`. Run id `20261009054229-d8b43889`, 2026-10-09T05:42:30.023Z to 2026-10-09T05:44:01.508Z.
Binaries (not committed): `.verify-artifacts/verify-20261009-014229` with 8 checkpoint PNG(s), 1 video(s) (`page@6727f9b687feab2104f107dec33a435a.webm`), trace(s) `trace.zip`, `trace-2.zip`, `trace-3.zip`, `trace-4.zip`, `trace-5.zip`, `trace-6.zip`, `trace-7.zip`, `trace-8.zip` (secret-bearing: session cookies and tokens; never attach, commit or upload).

Overall: PASS (assertions); visual confirmation pending. A PASS here is assertion-level only until the checkpoints below were viewed and the feature pass criteria confirmed by the operator (record that in the PR).

| Scenario | Result | Reason |
| --- | --- | --- |
| workflow.reload-persistence | PASS |  |
| workflow.chat-send-stream-reload | PASS |  |
| workflow.project-creation-via-chat | PASS |  |
| workflow.hitl-deny | PASS |  |

## Checkpoints

| Scenario | Checkpoint | File |
| --- | --- | --- |
| workflow.chat-send-stream-reload | chat.sent | workflow.chat-send-stream-reload--chat.sent.png |
| workflow.chat-send-stream-reload | chat.streamed | workflow.chat-send-stream-reload--chat.streamed.png |
| workflow.chat-send-stream-reload | chat.reloaded | workflow.chat-send-stream-reload--chat.reloaded.png |
| workflow.project-creation-via-chat | project.pending | workflow.project-creation-via-chat--project.pending.png |
| workflow.project-creation-via-chat | hitl.approved | workflow.project-creation-via-chat--hitl.approved.png |
| workflow.project-creation-via-chat | project.created | workflow.project-creation-via-chat--project.created.png |
| workflow.hitl-deny | hitl.pending | workflow.hitl-deny--hitl.pending.png |
| workflow.hitl-deny | hitl.denied | workflow.hitl-deny--hitl.denied.png |

Cleanup: complete.

## Operator review (2026-10-09)

All 8 checkpoints were viewed and compared with the feature-map pass criteria. Deployed lane, so this is not a nous-loop step 7b local `PASS`.

- `chat-send-stream-reload`: **PASS with one criterion unproven** (corrected 2026-10-09 after audit). `chat.sent` shows the prompt as a user row. `chat.streamed` shows the assistant answer `KestrelAck42`, but the composer still renders `Stop` and `Queue`, so the run was still in flight: the criterion "reaches a terminal state" was not shown before the scenario reloaded. `chat.reloaded` shows both rows persisted with `Send`/`Copy`/`Regenerate`, so the persisted turn is terminal. The scenario must wait for the terminal state before reloading.
- `project-creation-via-chat`: **PASS**. `project.pending` shows the Approval needed card for Create project. `hitl.approved` shows the agent's reply "Created the project … Project ID …". `project.created` shows the project page, Active.
- `hitl-approve-deny`: **PASS**. The approve path is the same as above. `hitl.pending` shows the approval card, and `hitl.denied` shows "Action cancelled by user." The runner also asserted that no project was created.
- Observation, not a criterion failure: in `chat.sent`, the in-progress step labels render clipped ("ing", "ving") while streaming.

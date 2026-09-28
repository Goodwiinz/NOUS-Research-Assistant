# NOUS local harness bridge

`@nous/harness-bridge` connects a paired local Codex CLI to a NOUS chat run. The chat remains the user-facing conversation; Codex runs on the paired computer in a project workspace, and NOUS stores the accepted run and its transcript events. This package does not add a general NOUS MCP server, synchronize other conversations, or publish side-panel artifacts.

## Requirements and pairing

- Node.js 24 and the repository-pinned pnpm version.
- Codex CLI exactly `0.153.4` for this adapter. The adapter refuses other versions.
- A project-bound NOUS chat, an organization-backed account, and a NOUS server with `HARNESS_BRIDGE_ENABLED=true`.

Start `nous-harness connect` from the repository-pinned package runtime. The CLI opens the existing NOUS browser authorization flow, registers the local computer, and waits for a browser-approved `harness:execute` grant for the selected project. Then register a workspace root and run the local bridge:

```sh
pnpm --filter @nous/harness-bridge start connect --api https://nous.example/api/v1 --project PROJECT_UUID --label "My computer"
pnpm --filter @nous/harness-bridge start workspace add --root /absolute/path/to/project --label "Project workspace"
pnpm --filter @nous/harness-bridge start run
```

Replace the example host, project UUID, and local path with the values for the NOUS deployment and the project. The CLI stores credentials in its owner-only local state directory; the connection file contains only an opaque credential handle. Browser requests contain device/workspace identifiers and labels, never the local absolute path, executable, arbitrary arguments, or MCP configuration. The owner-authenticated API supports revoking a known grant with `DELETE /api/v1/integrations/grants/{grant_id}`. This initial CLI does not expose a revoke command or display the exchanged grant UUID, and NOUS has no user-facing grant-revocation screen yet. Stopping the bridge and removing local state only disables this local client; it does not revoke the server-side grant. Do not treat it as revocation. Revoking a grant also does not prove an already-running native process has exited; allow reconciliation to report its terminal state.

## Data and local permissions

The browser sends the chat prompt to NOUS. NOUS sends the accepted command to the paired bridge; Codex receives the prompt and runs under the local Codex executable. NOUS persists the run lifecycle, assistant deltas, tool summaries, usage events, and disclosed approval targets in the chat/run event ledger. Do not put secrets in a prompt or workspace output unless they may be processed by both Codex and NOUS.

The adapter requests Codex `workspaceWrite` with the registered root as the writable root, network access disabled, and the current temporary-directory exclusions. It verifies the effective policy returned by Codex before starting a turn. **Registering a folder is not folder-only read isolation.** It does not prove Codex cannot read other local files. Treat the computer and files accessible to its Codex process as within the local trust boundary. MCP configuration, when supplied by trusted local composition, is session-scoped; NOUS browser input cannot choose an MCP executable or pass arbitrary process configuration.

Command execution prompts can be approved once or denied in the authenticated NOUS chat. File-change requests remain deny-only while the backend request schema lacks a disclosed path or concrete change summary; do not approve a file-change request on the basis of an opaque item identifier or reason alone.

## Disconnect, recovery, and kill switch

Browser disconnect stops observation, not Codex execution. Reopening the same project chat attaches to the persisted run stream. If local command acceptance or interruption is ambiguous, the bridge journals the uncertainty, does not replay the native action, and keeps the affected workspace quarantined until reconciliation provides terminal evidence. Restart the bridge with the same local state directory and let reconciliation run; do not delete or edit `journal.sqlite` to unlock a workspace.

For an uncertain interrupt that cannot be reconciled, stop the bridge and inspect the recorded command IDs:

```sh
pnpm --filter @nous/harness-bridge start recover-interrupt
```

The operator recovery requires verified OS boot evidence on the same macOS/Linux computer. For a legacy interrupt without a baseline, run once to record the baseline, wait at least ten seconds, reboot, then run with the exact command UUID:

```sh
pnpm --filter @nous/harness-bridge start recover-interrupt --command COMMAND_UUID
```

This clears only the interrupt-delivery blocker; it does not mark the run complete, release workspace ownership, or replace reconciliation. Unsupported platforms, unreadable OS identity, same-boot recovery, or a different machine fail closed.

Set the server's `HARNESS_BRIDGE_ENABLED=false` to stop accepting new Codex chat runs and new start dispatches. The default is `false`. Reconciliation and terminal reads remain available so already accepted work can reach a known outcome.

## Checks

Run package tests and static type-checking from the repository root:

```sh
pnpm --filter @nous/harness-bridge test
pnpm --filter @nous/harness-bridge type-check
```

The fixture-backed Codex tests exercise the pinned JSON-RPC contract without a live user session. The separate Playwright acceptance is an explicitly configured live test; see [`docs/engineering/harness-bridge.md`](../../docs/engineering/harness-bridge.md). A skipped live test is **NOT RUN**, not evidence of live acceptance.

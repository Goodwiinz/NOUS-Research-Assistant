# Local Codex harness bridge

**Status:** Implemented as a local Codex bridge feature; live acceptance requires a deliberately configured NOUS test project, paired device, and workspace. This document describes the current bridge contract. It does not cover cross-chat context synchronization, tracing, or artifact publication. NOUS read tools are available to Codex through the local stdio MCP facade described in [NOUS read tools over MCP](#nous-read-tools-over-mcp); note creation, context selection, and other capabilities are not exposed.

## User workflow

1. An owner runs `nous-harness connect` on the computer that will execute Codex. The CLI completes NOUS browser authorization, registers the device, and waits for browser consent to a project-scoped `harness:execute` grant.
2. The owner registers a local workspace root, starts `nous-harness run`, and selects **Local Codex**, the paired computer, and its workspace in the NOUS chat composer.
3. NOUS durably accepts the chat message and run before dispatch. The local bridge receives a fenced command over authenticated WSS and starts the pinned Codex app-server with the verified local policy. Persisted events flow back to the existing chat transcript.
4. Exact, one-time Codex command approvals and required input are shown in NOUS. The browser re-reads the saved request before a decision and sends only the decision plus the displayed target hash. The local CLI grant token never enters the browser.
5. If the browser disconnects, execution continues and NOUS can replay persisted events when the chat reconnects. Browser presence is not run ownership or terminal evidence.

NOUS-native chat remains the default provider. Provider/device/workspace selection is scoped to the signed-in user and chat thread. The bridge does not search or continue other NOUS conversations, synchronize a context file, or publish artifacts. Codex may call NOUS read tools only through the opt-in MCP facade below.

## NOUS read tools over MCP

`nous-harness mcp` is a local stdio MCP server (`packages/harness-bridge/src/mcp/`) that forwards `tools/list` and `tools/call` to the backend read gateway (`GET/POST /api/v1/integrations/tools[/read]`, gated by `NOUS_MCP_ENABLED`, default `false`). The backend owns the tool allowlist, JSON Schema catalog, argument validation, project scoping, and result bounds; the facade adds nothing a caller could use to widen them.

- **Consent.** `nous-harness connect --tools` requests `harness:execute` plus `tools:read`; both scopes appear on the browser approval page. A connection made without `--tools` has no MCP access and `mcp install` refuses with a reconnect hint. The stored scopes only decide whether the local process offers the server; the backend re-checks the grant on every request.
- **Managed sessions.** When the local connection carries `tools:read`, `nous-harness run` adds a session-scoped `mcpConfig` entry that launches `nous-harness mcp --api ORIGIN --store DIR --session HANDLE` for that Codex thread. Argv contains only the API origin, the state directory, and the opaque credential handle; the CLI JWT and grant token are read from the owner-only store inside the child.
- **Standalone Codex.** `nous-harness mcp install` prints the `codex mcp add nous -- …` command for the user to run. The package never edits `~/.codex/config.toml` or any other global Codex configuration.
- **Transport hygiene.** Stdout is the JSON-RPC channel; diagnostics go to stderr. The API origin must be HTTPS, except the explicit loopback development hosts. Model-supplied arguments are sent only inside the invocation body; they cannot set headers, URLs, identity, or credentials. A 401/403 from the gateway returns a stable `NOUS authorization rejected; reconnect this device` tool error with no retry.
- **Tests.** `packages/harness-bridge/test/mcp.test.ts` spawns both the managed configuration and the standalone command against a recording HTTP server and asserts identical `source_refs`, correct grant headers, JSON-RPC-only stdout, no secrets in the config or install command, and no writes under `CODEX_HOME`. Live acceptance against a real Codex and NOUS deployment is **NOT RUN** by this suite.

## Consent, disclosure, and isolation limits

Consent is separate from CLI login: the browser authorizes the CLI user, then an authenticated browser approves the device/project grant. The owner-authenticated NOUS API supports revocation with `DELETE /api/v1/integrations/grants/{grant_id}` for a known grant UUID. **This initial CLI does not expose a revoke command or display the exchanged grant UUID, and there is no user-facing grant-revocation screen yet.** Removing local state stops this bridge instance but does not revoke the server grant. Treat per-grant revocation as an API operation requiring the grant UUID until an owner-facing lifecycle flow is added. Revocation prevents further grant-authorized work, but does not itself terminate a native process already accepted by Codex. The local CLI state uses owner-only permissions and stores access/grant credentials behind an opaque handle.

NOUS persists the accepted user message, assistant text deltas, tool lifecycle summaries, usage, native approval targets, and terminal events in its chat/run records. Codex receives the prompt and executes local work. Keep secrets out of prompts and workspace output unless they are intended to be processed by both systems. The browser receives IDs and labels, not local absolute workspace paths or CLI grant tokens.

The app-server start/resume policy is checked against the configured workspace-write sandbox, writable roots, approval policy, reviewer, and network setting. This does **not** provide a folder-only read guarantee: registering a root identifies the requested write scope; it is not proof that Codex cannot read files outside that root. The feature treats the local computer and data available to the Codex process as trusted execution context. Optional MCP configuration is locally composed and session-scoped, never supplied by the browser.

Command requests support one-time allow/deny. File-change requests stay deny-only until the persisted backend request schema carries a disclosed path or concrete change summary. An item ID or generic reason is insufficient review context. Unknown request shapes, persistent grants, changed approval targets, expired/revoked grants, and stale decisions fail closed.

## Recovery and operational controls

The local journal records native command intent before sending it. If acceptance or an interrupt is ambiguous, the bridge does not replay that action and retains the workspace lock while it reconciles exact Codex history. An unmatched or unverifiable native action remains quarantined; restarting the bridge with the same state allows reconciliation to continue. Do not remove the journal, clear the lock manually, or treat a WebSocket close/interrupt receipt as proof that Codex stopped.

For an uncertain interrupt that still cannot reconcile, stop the bridge and run:

```sh
pnpm --filter @nous/harness-bridge start recover-interrupt
```

The command lists exact interrupt command IDs. Recovery is permitted only with verified boot identity on the same macOS/Linux computer. If a legacy record has no baseline, the first invocation records one; wait at least ten seconds, reboot that same computer, then run:

```sh
pnpm --filter @nous/harness-bridge start recover-interrupt --command COMMAND_UUID
```

Recovery removes only that interrupt's uncertain-delivery blocker. It preserves run outcome and workspace ownership; restart the bridge so normal reconciliation can supply terminal evidence. Same-boot, different-machine, or unavailable OS evidence is rejected. Do not edit the SQLite journal by hand.

`HARNESS_BRIDGE_ENABLED` is the backend kill switch and defaults to `false`. Setting it to `false` rejects newly accepted Codex chat work and prevents pending start commands from dispatching. Reads, cancellation, and reconciliation of already accepted runs remain available to bring work to a known state. After disabling the feature, monitor outstanding runs until they have terminal outcomes before retiring their paired device.

## Conformance and live acceptance

The package conformance test verifies that every capability an adapter advertises has a corresponding callable operation. The pinned Codex fixture tests exercise the app-server protocol and policy checks; the conformance helper alone does not prove a Codex binary or remote service is available. Additional adapters remain gated until conformance and their operation-specific fixture contract tests pass.

The Playwright live acceptance is opt-in and uses a dedicated, project-bound test thread and workspace on a real paired device running the bridge. It asks Codex to execute a harmless, unique marker-file command; it expects an exact command-approval request, reloads the chat while the request is pending, approves once, and verifies the persisted user/assistant marker appears only once after completion and another reload. It annotates the NOUS thread and accepted run IDs. It intentionally does not expose or log local credential material. The native Codex session ID is not exposed to the browser and is not claimed by this test.

Configure `NOUS_HARNESS_E2E=1`, `NOUS_HARNESS_E2E_DEVICE_ID`, `NOUS_HARNESS_E2E_WORKSPACE_ID`, `NOUS_HARNESS_E2E_THREAD_ID`, `NOUS_HARNESS_E2E_EMAIL`, and `NOUS_HARNESS_E2E_PASSWORD` to enable the live scenario. The named workspace must point to a disposable local test fixture, the thread must be bound to the same NOUS project, the device bridge must be running, and the backend kill switch must be enabled. Without opt-in/device/credentials the test reports **skipped / NOT RUN** with the missing prerequisite in its reason. Skipped is never counted as live acceptance. Report local fixture tests, hosted CI job/SHA, and live-device outcome separately; for live runs include the NOUS thread and run IDs recorded in Playwright annotations. CI without those dedicated secrets and a connected device has no live result.

From the repository root, run the focused conformance suite with:

```sh
pnpm --filter @nous/harness-bridge test -- test/conformance.test.ts
```

Run the separately configured browser acceptance with:

```sh
pnpm --dir tests/e2e exec playwright test harness-bridge.spec.ts --project=chromium
```

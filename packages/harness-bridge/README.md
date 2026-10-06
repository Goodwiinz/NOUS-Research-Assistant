# NOUS local harness bridge

`@nous/harness-bridge` connects a paired local Codex CLI to a NOUS chat run. The chat remains the user-facing conversation; Codex runs on the paired computer in a project workspace, and NOUS stores the accepted run and its transcript events. It also ships an opt-in local MCP server that exposes NOUS project read tools to Codex and, when the matching scopes are approved, publication of a file from a registered workspace root and note requests the user approves in NOUS (see [NOUS read tools](#nous-read-tools-over-mcp)). This package does not synchronize other conversations.

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

Replace the example host, project UUID, and local path with the values for the NOUS deployment and the project. The CLI stores credentials in its owner-only local state directory; the connection file contains only an opaque credential handle. Browser requests contain device/workspace identifiers and labels, never the local absolute path, executable, arbitrary arguments, or MCP configuration. Run `pnpm --filter @nous/harness-bridge start disconnect` to revoke this device's grant and its consent in NOUS and remove the local credentials. Only a live grant can authorize the revoke, so run it while `nous-harness run` is still running or within 15 minutes of the last renewal; if NOUS cannot be reached in that window, it keeps everything so it can be retried. Once the grant has expired, it can only remove the local credentials: the consent stays valid until it is revoked another way, and NOUS has no user-facing revocation screen yet (the `/integrations/devices` page the command mentions is not shipped). A successful revoke also ends every CLI login of the same NOUS user, so other computers must run `connect` again and can no longer revoke their own consent. Ending those logins is best-effort: it is skipped if the server's Redis is unreachable. Stopping the bridge or deleting local state without `disconnect` only disables this local client; it does not revoke the server-side grant. Revoking a grant also does not prove an already-running native process has exited; allow reconciliation to report its terminal state.

## Data and local permissions

The browser sends the chat prompt to NOUS. NOUS sends the accepted command to the paired bridge; Codex receives the prompt and runs under the local Codex executable. NOUS persists the run lifecycle, assistant deltas, tool summaries, usage events, and disclosed approval targets in the chat/run event ledger. Do not put secrets in a prompt or workspace output unless they may be processed by both Codex and NOUS.

The adapter requests Codex `workspaceWrite` with the registered root as the writable root, network access disabled, and the current temporary-directory exclusions. It verifies the effective policy returned by Codex before starting a turn. **Registering a folder is not folder-only read isolation.** It does not prove Codex cannot read other local files. Treat the computer and files accessible to its Codex process as within the local trust boundary. MCP configuration, when supplied by trusted local composition, is session-scoped; NOUS browser input cannot choose an MCP executable or pass arbitrary process configuration.

Command execution prompts can be approved once or denied in the authenticated NOUS chat. File-change requests remain deny-only while the backend request schema lacks a disclosed path or concrete change summary; do not approve a file-change request on the basis of an opaque item identifier or reason alone.

## NOUS read tools over MCP

Connect with `--tools` to also request the `tools:read` scope. Add `--publish` to request `artifacts:publish`, and `--write` to request `tools:write`, which lets Codex request notes that the user approves in NOUS. `--publish` and `--write` need `--tools`; `--library` (see [Workspace connections and library scopes](#workspace-connections-and-library-scopes)) needs `--tools` and `--write`. The browser approval page lists every scope with a plain-language label. The NOUS server must set `NOUS_MCP_ENABLED=true` (default `false`). While it is off, the server answers 503 "NOUS integration tools are disabled" to listing or calling the read tools and to requesting a note, and approved notes wait unexecuted. It does not gate reading the status of an existing note request, and it does not gate artifact publication, which needs `ARTIFACTS_ENABLED=true` (default `false`) instead. See [`docs/engineering/harness-bridge.md`](../../docs/engineering/harness-bridge.md) for the note-request and grant-renewal contract.

```sh
pnpm --filter @nous/harness-bridge start connect --api https://nous.example/api/v1 --project PROJECT_UUID --label "My computer" --tools --publish
```

- `nous-harness run` then configures each managed Codex session with a `nous` MCP server that launches `nous-harness mcp` for that session only.
- For a standalone Codex, print the registration command and run it yourself; the bridge never edits global Codex configuration:

```sh
pnpm --filter @nous/harness-bridge start mcp install
```

Tools are the backend's read allowlist. All are under the `tools:read` scope except `list_library`, which needs `library:read` (see [Workspace connections and library scopes](#workspace-connections-and-library-scopes)):

| Tool | Scope of data | Notes |
| --- | --- | --- |
| `search_documents` | granted project | title/filename search, ≤ 50 results |
| `list_project_documents` | granted project | paginated listing |
| `do_kb_retrieve` | granted project | semantic chunks; needs `document_ids` and a provisioned KB |
| `retrieve_passages` | granted project | PostgreSQL full-text passages; `document_ids` optional, `top_k` ≤ 20; works without a KB |
| `get_document_content` | granted project | summary or full text, `offset`/`limit` ≤ 48,000 chars, follow `next_offset` |
| `get_current_draft` | granted project | latest generated draft |
| `search_arxiv` | arXiv (external) | ≤ 20 results advertised, 120 s budget |
| `search_external_database`, `list_external_databases` | connector registry (external) | ≤ 20 results |
| `get_arxiv_paper_content` | arXiv (external) | transient full text by id, paginated (Redis cache: 24 h fresh, up to 7 d stale fallback); persists nothing — use `ingest_arxiv_papers` to add a paper to NOUS |
| `find_researchers` | granted project | authors by name from the author lists of papers ingested into this project (arXiv metadata); case-insensitive substring, `limit` ≤ 25; no affiliations or career history |
| `get_researcher` | granted project | one researcher's papers in this project (≤ 100, newest first) and co-authors (≤ 100); `researcher_not_found` when no project document lists them |
| `list_library` | the grant's whole scope | the folders (NOUS projects) the connection may use, with document counts, one page at a time; needs `library:read` |

Project-scoped tools ("granted project") resolve the project from the grant, never from arguments, except that a workspace connection names it with a validated `project_id` (see below); a document outside it answers `requested_documents_unavailable`. Every result is capped at 64 KiB. With a registered workspace root and the `artifacts:publish` scope, the server also offers `artifacts_publish(relative_path, title, publication_id)`: it reads one regular file under that root (no symlinks anywhere in the path, no hard links, no traversal, at most 10 MiB; containment is enforced by the kernel on macOS via `O_NOFOLLOW_ANY` and by the descriptor's real path on Linux), uploads it with its SHA-256, and finalizes a NOUS artifact version. Reusing the same `publication_id` retries safely. The root comes from the local binding (`--root` in the launch argv), never from tool arguments; `mcp install --root PATH` picks it when more than one workspace is registered. The root limits what can be published, not what Codex can read. Publication requires `ARTIFACTS_ENABLED=true` on the server. Argv carries only the API origin, the state directory, and the opaque credential handle; tokens stay in the owner-only store. Diagnostics go to stderr. Gateway rejections come back as tool errors and are never retried: 401 asks you to reconnect with `--tools`, 403 names a missing `tools:read` scope or an out-of-project resource, 422 forwards the gateway's argument reason, and 503 reports NOUS's own reason (tools disabled, artifact publication disabled, or artifact storage unavailable). `nous-harness run` prints a stderr notice when the connection lacks `tools:read` and managed sessions therefore get no NOUS tools.

### Workspace connections and library scopes

Connect with `--workspace WORKSPACE_UUID` instead of `--project` to bind the grant to every project (folder) in one NOUS workspace you own or belong to; NOUS re-checks that access on every call. This is a NOUS workspace, not a local folder: `workspace add` still registers a local folder. Give exactly one of `--project` and `--workspace`.

```sh
pnpm --filter @nous/harness-bridge start connect --api https://nous.example/api/v1 --workspace WORKSPACE_UUID --label "My computer" --tools --write --library
```

- **MCP-only.** A workspace connection needs `--tools`, cannot be combined with `--publish`, `--chat` or `--handoff` (an output folder and a chat each belong to one project), and never requests `harness:execute`. `nous-harness run` and `workspace add` refuse it and point to `--project`; use `mcp install` to register its tools with a standalone Codex. Reconnecting with `--workspace` replaces the previous connection and does not carry over registered local folders, so register them again after reconnecting with `--project`.
- **Choosing a project.** On a workspace connection every tool marked "granted project" above takes a `project_id` argument; leaving it out is a tool error. The arXiv, connector and `list_library` tools act on no single project and take none. NOUS checks it against the granted workspace on every call and refuses any other project. `list_library` shows the project ids to choose from.
- **`--library`.** Also requests `library:read` and `library:write`, and needs both `--tools` and `--write`. It works with `--project` too, where `list_library` shows that one project. `library:read` adds the `list_library` tool, which lists the folders (NOUS projects) the connection may use, with their document counts, one page at a time. `library:write` is consent only for now: no library action runs under it yet, and the backend does not act on it until the library actions ship. A workspace connection cannot request notes either (NOUS refuses `request_action` for a workspace grant), so `--write` there only enables `--library`. The flag also adds `--library` to the `nous-harness mcp` command that managed sessions and `mcp install` launch.
- **Approval page.** The browser page shows the workspace instead of a project, and each scope with a plain-language label beside its raw name. The `library:write` label describes what that scope will allow once the library actions ship.

The backend contract for workspace grants and library scopes is in [`docs/engineering/harness-bridge.md`](../../docs/engineering/harness-bridge.md#workspace-connections-and-library-scopes).

## Binding reuse, status, and chat handoffs

Running `connect` again with the same API, project (or workspace) and `--chat` (both absent counts as equal), and with no scope that the stored connection lacks, reuses the stored binding. It forces one grant renewal as a liveness probe and, if NOUS issues a new grant token, prints `Reusing binding` and skips the browser login and consent entirely. A different chat, project or workspace (a project and a workspace never reuse each other), a missing scope, a connection without a stored grant ID, or a failed or transient renewal runs the full login and consent flow. When the new connection replaces a different chat, project or workspace, the superseded local credential file is removed. **Known gap:** its server-side grant and consent are not revoked; that grant lapses within 15 minutes, but the consent stays valid until revoked another way. Run `disconnect` before switching chats if that matters. The reuse probe forces a grant renewal, which revokes the previous grant token: running MCP children pick up the renewed token on their next call, but do not reconnect while a publish sequence (reserve, upload, finalize) is in progress.

`nous-harness status` prints the project and chat (or the workspace), device label, grant expiry and the handoff queue counts from local state only; it makes no network calls and says the device is not connected when there is no state.

A connection made with `--chat UUID --tools --handoff` can also use the handoff commands (a workspace connection has no chat, so it cannot):

```sh
pnpm --filter @nous/harness-bridge start handoff show
pnpm --filter @nous/harness-bridge start handoff save --file handoff.json --parent 3
pnpm --filter @nous/harness-bridge start handoff flush
pnpm --filter @nous/harness-bridge start handoff list
pnpm --filter @nous/harness-bridge start handoff discard HANDOFF_ID
```

`handoff.json` holds `goal` and optionally `decisions`, `remaining`, `results`, `harness_name`, `harness_session_id`, `handoff_id` and `expected_parent_version`; `--parent N` overrides the last one, and the default is `null` (the first handoff). Both `handoff save` and the MCP tool `save_nous_handoff` write an owner-only journal entry (`handoff-<handoff_id>.json` in the state directory) before the POST:

| Outcome | Entry | Retried by `flush` |
| --- | --- | --- |
| 2xx | deleted | no |
| network error, timeout, 5xx, 429 | `pending` | yes |
| 401 or expired grant | `pending`, plus a reconnect hint | yes, after reconnecting |
| 409 | `conflicted`, with the latest version stored and printed | no; merge it yourself and save a new handoff |
| 403, 422 and other 4xx | `rejected`, with the error | no |

`flush` is manual; there is no background retry. It re-sends only `pending` entries journaled for the current chat and project, using the same `handoff_id` so NOUS returns the stored version if the first attempt did land. It reports entries from another chat as skipped and leaves them alone. `save` and `flush` exit non-zero unless every attempt was saved. Conflicts are never merged automatically; `handoff discard HANDOFF_ID` drops a journaled entry in any state and prints what it dropped. `save_nous_handoff` journals only when the MCP session's credential handle is the device's current binding; after a reconnect, a stale MCP session refuses the save and must be restarted. A pending MCP save names the `handoff_id` to retry with. `handoff save` and `handoff flush` are meant as hook targets, but **no Codex hooks are wired in v1**.

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

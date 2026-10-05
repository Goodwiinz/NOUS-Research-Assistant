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

Connect with `--tools` to also request the `tools:read` scope. Add `--publish` to request `artifacts:publish`, and `--write` to request `tools:write`, which lets Codex request notes that the user approves in NOUS. Both need `--tools`, and the browser approval page lists every scope. The NOUS server must set `NOUS_MCP_ENABLED=true` (default `false`). While it is off, the server answers 503 "NOUS integration tools are disabled" to listing or calling the read tools and to requesting a note, and approved notes wait unexecuted. It does not gate reading the status of an existing note request, and it does not gate artifact publication, which needs `ARTIFACTS_ENABLED=true` (default `false`) instead. See [`docs/engineering/harness-bridge.md`](../../docs/engineering/harness-bridge.md) for the note-request and grant-renewal contract.

```sh
pnpm --filter @nous/harness-bridge start connect --api https://nous.example/api/v1 --project PROJECT_UUID --label "My computer" --tools --publish
```

- `nous-harness run` then configures each managed Codex session with a `nous` MCP server that launches `nous-harness mcp` for that session only.
- For a standalone Codex, print the registration command and run it yourself; the bridge never edits global Codex configuration:

```sh
pnpm --filter @nous/harness-bridge start mcp install
```

Tools are the backend's read allowlist, all under the `tools:read` scope:

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
| `find_researchers` | granted project | authors of papers ingested into this project (knowledge-graph `PERSON`s); `limit` ≤ 25; no affiliations or career history |
| `get_researcher` | granted project | one researcher's in-project papers (≤ 50, newest first) and co-authors; `researcher_not_found` outside the project |

Project-scoped tools resolve the project from the grant, never from arguments; a document outside it answers `requested_documents_unavailable`. Every result is capped at 64 KiB. With a registered workspace root and the `artifacts:publish` scope, the server also offers `artifacts_publish(relative_path, title, publication_id)`: it reads one regular file under that root (no symlinks anywhere in the path, no hard links, no traversal, at most 10 MiB; containment is enforced by the kernel on macOS via `O_NOFOLLOW_ANY` and by the descriptor's real path on Linux), uploads it with its SHA-256, and finalizes a NOUS artifact version. Reusing the same `publication_id` retries safely. The root comes from the local binding (`--root` in the launch argv), never from tool arguments; `mcp install --root PATH` picks it when more than one workspace is registered. The root limits what can be published, not what Codex can read. Publication requires `ARTIFACTS_ENABLED=true` on the server. Argv carries only the API origin, the state directory, and the opaque credential handle; tokens stay in the owner-only store. Diagnostics go to stderr. Gateway rejections come back as tool errors and are never retried: 401 asks you to reconnect with `--tools`, 403 names a missing `tools:read` scope or an out-of-project resource, 422 forwards the gateway's argument reason, and 503 reports NOUS's own reason (tools disabled, artifact publication disabled, or artifact storage unavailable). `nous-harness run` prints a stderr notice when the connection lacks `tools:read` and managed sessions therefore get no NOUS tools.

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

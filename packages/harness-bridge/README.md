# NOUS local harness bridge

`@nous/harness-bridge` connects a paired local Codex CLI to a NOUS chat run. The chat remains the user-facing conversation; Codex runs on the paired computer in a project workspace, and NOUS stores the accepted run and its transcript events. It also ships an opt-in local MCP server that exposes NOUS project read tools to Codex and, when the matching scopes are approved, publication of a file from a registered workspace root and requests for notes and library changes in NOUS (see [NOUS read tools](#nous-read-tools-over-mcp) and [Note and library actions](#note-and-library-actions)). This package does not synchronize other conversations.

## Quick start: `nous --rc`

From the folder Codex should work in, on a computer with Node 24 and a tested Codex:

```sh
nous --rc
```

`nous --rc` is the repository launcher ([`terminal/README.md`](../../terminal/README.md) has the one-time symlink; a copied launcher refuses to run; without the symlink, run `<checkout>/nous --rc` from that folder). It runs Node 24 directly, without pnpm or a PATH change, on `src/cli.ts rc … --api URL --root <current folder>` (its `--api` and `--root` override any you pass). That command checks Codex before any network call, signs in (browser link and code unless the saved sign-in still works), pairs this computer with your whole NOUS account (`connect --account --write --library`), registers the folder, and runs the bridge.

Running it again skips the browser while the account pairing is still live (grants last 15 minutes and only a running bridge, or a standalone `nous-harness mcp` sharing the pairing, renews them; without one, it lapses 3 to 15 minutes after the bridge stops) or can resume from your approval (up to 30 days after it lapsed); otherwise `connect` registers this computer as a new device (with the same label unless you pass `--label`) and asks for approval, and the old approval stays active until revoked (the old device stays listed even then; see [Binding reuse](#binding-reuse-status-and-chat-handoffs)). A stored project or workspace pairing is replaced by the account pairing: its registered folders become account folders, and `nous --rc` prints that the old approval stays active until you revoke it at `/integrations/devices`. NOUS sends the approval link as a root-relative path; the bridge prints it in full on the NOUS website origin that a sign-in to the same server recorded (`<state>/website.json`), and otherwise prints the path with `(open this path on your NOUS website)`.

Only one `nous --rc` or `nous-harness run` may use a state folder at a time (`<state>/bridge.lock`; a second one is refused with a hint to delete that file if no such process is running, and a lock left by a dead process or from before the last reboot is replaced). It refuses `/`, the home folder and any folder that contains the state folder (`… is too broad for Codex to work in`) before any network call, because Codex writes inside the registered folder without asking. When NOUS cannot be reached it prints `Could not reach NOUS at <url> (<reason>)`. Until the bridge starts, Ctrl-C, Ctrl-D, end of input, SIGTERM or SIGHUP cancels the command (exit 130); after that, Ctrl-C, SIGTERM or SIGHUP (closing the terminal window) stops the bridge cleanly and keeps the pairing. It requests `harness:execute`, `tools:read`, `tools:write`, `library:read` and `library:write`; a server without account connections answers with `this NOUS server doesn't support account connections yet`. The commands below do the same steps by hand and expose the other scopes.

## Requirements and pairing

- Node.js 24 and the repository-pinned pnpm version.
- A Codex CLI version the adapter has been tested with: `0.153.4` or `0.161.0` (`SUPPORTED_CODEX_VERSIONS` in `src/adapters/codex.ts`; the procedure for adding one is in `src/adapters/protocol/README.md`). The adapter refuses other versions before it starts anything. `0.161.0` has passed the package tests, a schema comparison and an adapter start against the real binary; its live run from a NOUS chat is NOT RUN.
- A project-bound NOUS chat, an organization-backed account, and a NOUS server with `HARNESS_BRIDGE_ENABLED=true`.

Start `nous-harness connect` from the repository-pinned package runtime. The CLI opens the existing NOUS browser authorization flow, registers the local computer, and waits for a browser-approved `harness:execute` grant for the selected project. Then register a workspace root and run the local bridge:

```sh
pnpm --filter @nous/harness-bridge start connect --api https://nous.example/api/v1 --project PROJECT_UUID --label "My computer"
pnpm --filter @nous/harness-bridge start workspace add --root /absolute/path/to/project --label "Project workspace"
pnpm --filter @nous/harness-bridge start run
```

Replace the example host, project UUID, and local path with the values for the NOUS deployment and the project. The CLI stores credentials in its owner-only local state directory; the connection file contains only an opaque credential handle. Browser requests contain device/workspace identifiers and labels, never the local absolute path, executable, arbitrary arguments, or MCP configuration. Run `pnpm --filter @nous/harness-bridge start disconnect` to revoke this device's grant and its consent in NOUS and remove the local credentials. Only a live grant can authorize the revoke, so once the stored grant has expired `disconnect` first resumes it from your earlier browser approval, then revokes it. If NOUS cannot be reached, it keeps everything so it can be retried. If NOUS will not resume the grant (its approval or this device was already revoked, your CLI sign-in ended, or it lapsed more than 30 days ago), it can only remove the local credentials: the consent stays valid until it is revoked another way, for example with Revoke under **Settings → Connected devices** (`/integrations/devices`) in NOUS. A successful revoke also ends every CLI login of the same NOUS user, so other computers must run `connect` again and can no longer revoke their own consent. Ending those logins is best-effort: it is skipped if the server's Redis is unreachable. Stopping the bridge or deleting local state without `disconnect` only disables this local client; it does not revoke the server-side grant. Revoking a grant also does not prove an already-running native process has exited; allow reconciliation to report its terminal state.

## Data and local permissions

The browser sends the chat prompt to NOUS. NOUS sends the accepted command to the paired bridge; Codex receives the prompt and runs under the local Codex executable. NOUS persists the run lifecycle, assistant deltas, tool summaries, usage events, and disclosed approval targets in the chat/run event ledger. Do not put secrets in a prompt or workspace output unless they may be processed by both Codex and NOUS.

The adapter requests Codex `workspaceWrite` with the registered root as the writable root, network access disabled, and the current temporary-directory exclusions. It verifies the effective policy returned by Codex before starting a turn. **Registering a folder is not folder-only read isolation.** It does not prove Codex cannot read other local files. Treat the computer and files accessible to its Codex process as within the local trust boundary. MCP configuration, when supplied by trusted local composition, is session-scoped; NOUS browser input cannot choose an MCP executable or pass arbitrary process configuration.

Command execution prompts can be approved once or denied in the authenticated NOUS chat. File-change requests remain deny-only while the backend request schema lacks a disclosed path or concrete change summary; do not approve a file-change request on the basis of an opaque item identifier or reason alone.

## NOUS read tools over MCP

Connect with `--tools` to also request the `tools:read` scope. Add `--publish` to request `artifacts:publish`, and `--write` to request `tools:write`, which lets Codex request notes and library changes that the user approves in NOUS (see [Note and library actions](#note-and-library-actions)). `--publish` and `--write` need `--tools`; `--library` (see [Workspace connections and library scopes](#workspace-connections-and-library-scopes)) needs `--tools` and `--write`. `--context` also needs `--tools` and requests `context:read`, which lets Codex read the project memories you select in NOUS (`read_selected_context`); it is for `--project` connections only. The browser approval page lists every scope with a plain-language label. The NOUS server must set `NOUS_MCP_ENABLED=true` (default `false`). While it is off, the server answers 503 "NOUS integration tools are disabled" to listing or calling the read tools and to requesting an action, and approved actions wait unexecuted. It does not gate reading the status of an existing action request, and it does not gate artifact publication, which needs `ARTIFACTS_ENABLED=true` (default `false`) instead. See [`docs/engineering/harness-bridge.md`](../../docs/engineering/harness-bridge.md) for the action-request and grant-renewal contract.

```sh
pnpm --filter @nous/harness-bridge start connect --api https://nous.example/api/v1 --project PROJECT_UUID --label "My computer" --tools --publish
```

- `nous-harness run` then configures each managed Codex session with a `nous` MCP server that launches `nous-harness mcp` for that session only.
- For a standalone Codex, print the registration command and run it yourself; the bridge never edits global Codex configuration:

```sh
pnpm --filter @nous/harness-bridge start mcp install
```

`mcp install` also prints, on stderr, a `tool_timeout_sec = 150` line to add under `[mcp_servers.nous]` in `~/.codex/config.toml`; managed sessions set it themselves. Add it again after every `codex mcp add nous`, which rewrites that table. Without it Codex stops waiting after 60 s, and the arXiv tools may take up to 125 s. The bridge waits 135 s for `search_arxiv`, `get_arxiv_paper_content` and `discover_papers` (above NOUS's own limits of 120 s, 125 s and about 45 s respectively) and 30 s for every other read tool, and reports a timeout as the tool error `upstream_timeout`.

Tools are the backend's read allowlist. All are under the `tools:read` scope except `list_library`, which needs `library:read` (see [Workspace connections and library scopes](#workspace-connections-and-library-scopes)):

| Tool | Scope of data | Notes |
| --- | --- | --- |
| `search_documents` | granted project | title/filename search, ≤ 50 results |
| `list_project_documents` | granted project | paginated listing |
| `do_kb_retrieve` | granted project | semantic chunks; needs `document_ids` and a provisioned KB |
| `retrieve_passages` | granted project | PostgreSQL full-text search: the best `top_k` (≤ 20) documents, one excerpt each (up to three matching sentences, the title included), every stemmed query word must match (English stop words and one-letter words are ignored); `document_ids` optional; works without a KB |
| `get_document_content` | granted project | summary or full text, `offset`/`limit` ≤ 48,000 chars, follow `next_offset` |
| `get_current_draft` | granted project | latest generated draft |
| `search_arxiv` | arXiv (external) | ≤ 20 results advertised, 120 s budget |
| `discover_papers` | the grant's whole scope (library) and arXiv (external) | one discovery round: `keywords` (1-8, the user's own terms, all must match) and a `question` run a library keyword search, a library semantic search (needs `SEMANTIC_SEARCH_ENABLED` and the pgvector table) and an arXiv keyword search at once, plus, when acronyms are mixed with other words, one more library keyword call on just the acronyms (arXiv gets them OR-ed into its one request); the `published_after`/`published_before` window is applied inside each search. Returns up to 15 deduplicated, rank-fused papers (fewer when one paper clearly dominates; an ingested arXiv paper shows as its library copy), the status of every search in `calls` (an embedding-provider failure is `unavailable` with a `reason`, not `empty`; so is an arXiv call skipped because arXiv's shared rate limit was busy, `reason: rate_gated`, and the harness can call again shortly), and `followup_rounds_allowed` (0, 1 or 2 from `difficulty`) for the harness to spend itself; ≈ 45 s budget |
| `search_external_database`, `list_external_databases` | connector registry (external) | ≤ `max_results` (≤ 20) results in total, taken in turn from each connector in registry order, `truncated: true` when rows were cut; names match case-insensitively; a bad connector, domain or filter returns `invalid_arguments` with the valid names or an `error_category` |
| `get_arxiv_paper_content` | arXiv (external) | transient full text by id, paginated (Redis cache: 24 h fresh, up to 7 d stale fallback; after a failed refetch the stale text is served for 5 min without another attempt); persists nothing — use `ingest_arxiv_papers` to add a paper to NOUS |
| `find_researchers` | granted project | authors by name from the author lists of papers ingested into this project (arXiv metadata); case-insensitive substring, `limit` ≤ 25; no affiliations or career history |
| `get_researcher` | granted project | one researcher's papers in this project (≤ 100, newest first) and co-authors (≤ 100); `researcher_not_found` when no project document lists them |
| `list_library` | the grant's whole scope | the folders (NOUS projects) the connection may use, with document counts, one page at a time; needs `library:read` |

Project-scoped tools ("granted project") resolve the project from the grant, never from arguments, except that a workspace connection names it with a validated `project_id` (see below); a document outside it answers `requested_documents_unavailable`. Every result is capped at 64 KiB. With a registered workspace root and the `artifacts:publish` scope, the server also offers `artifacts_publish(relative_path, title, publication_id)`: it reads one regular file under that root (no symlinks anywhere in the path, no hard links, no traversal, at most 10 MiB; containment is enforced by the kernel on macOS via `O_NOFOLLOW_ANY` and by the descriptor's real path on Linux), uploads it with its SHA-256, and finalizes a NOUS artifact version. Reusing the same `publication_id` retries safely. The root comes from the local binding (`--root` in the launch argv), never from tool arguments; `mcp install --root PATH` picks it when more than one workspace is registered. The root limits what can be published, not what Codex can read. Publication requires `ARTIFACTS_ENABLED=true` on the server. Argv carries only the API origin, the state directory, and the opaque credential handle; tokens stay in the owner-only store. Diagnostics go to stderr. Gateway rejections come back as tool errors and are never retried: 401 asks you to reconnect with `--tools`, 403 names a missing `tools:read` scope or an out-of-project resource, 422 forwards the gateway's argument reason, and 503 reports NOUS's own reason (tools disabled, artifact publication disabled, or artifact storage unavailable). `nous-harness run` prints a stderr notice when the connection lacks `tools:read` and managed sessions therefore get no NOUS tools.

### Note and library actions

With `tools:write` on any connection (`--write`, which `--library` needs too), the MCP server also offers `request_action` and `get_action_status`. `request_action` takes an `action`, a client-generated UUID `invocation_id` (reusing it retries safely: NOUS returns the stored request) and that action's fields. Every other argument, identity included, is dropped before the request leaves the bridge, which refuses a malformed field locally; NOUS validates every field again. Folders are NOUS projects, always named by id (`list_library` lists them), and NOUS checks each id against the grant before it stores the request.

| `action` | Fields (`?` optional) | Runs |
| --- | --- | --- |
| `create_project_note` | `title`, `content`, `tags`? | after approval; `--project` only |
| `save_papers_to_folder`, `remove_papers_from_folder` | `document_ids` (1-20), `project_id` | at once with `library:write`, otherwise after approval |
| `move_papers_between_folders` | `document_ids` (1-20), `from_project_id`, `to_project_id` | at once with `library:write`, otherwise after approval; `--workspace` only |
| `create_folder` | `name`, `description`? | at once with `library:write`, otherwise after approval; `--workspace` only |
| `rename_folder` | `project_id`, `name` | at once with `library:write`, otherwise after approval |
| `update_document_metadata` | `document_id`, and `title` and/or `tags` (`[]` clears them) | at once with `library:write`, otherwise after approval |
| `delete_folder` | `project_id` | after approval |
| `ingest_arxiv_papers` | `paper_ids` (1-10 arXiv ids), `project_id` (required on `--workspace`) | after approval |

After approval means the reply carries an approval link and nothing changes until the signed-in user approves the request in NOUS; the model can never approve. `get_action_status` reports `awaiting_approval`, `approved`, `executing`, `succeeded`, `failed` (including a denial) or `outcome_unknown`, and only `succeeded` is described to the model as done. A 403 means the grant lacks the scope the action needs or the action names a project or document outside the grant. `update_document_metadata` changes the paper itself, wherever it appears; its receipt keeps the `previous_title` or `previous_tags` it replaced, so asking for those values again undoes the edit.

The backend contract, including what each action may target, the move's single transaction and the arXiv ingest's receipt, is in [`docs/engineering/harness-bridge.md`](../../docs/engineering/harness-bridge.md#library-actions).

### Workspace connections and library scopes

Connect with `--workspace WORKSPACE_UUID` instead of `--project` to bind the grant to every project (folder) in one NOUS workspace you own or belong to; NOUS re-checks that access on every call. This is a NOUS workspace, not a local folder: `workspace add` still registers a local folder. Give exactly one of `--project` and `--workspace`.

```sh
pnpm --filter @nous/harness-bridge start connect --api https://nous.example/api/v1 --workspace WORKSPACE_UUID --label "My computer" --tools --write --library
```

- **MCP-only.** A workspace connection needs `--tools`, cannot be combined with `--publish`, `--chat`, `--handoff` or `--context` (an output folder, a chat and a selection of project memories each belong to one project), and never requests `harness:execute`. `nous-harness run` and `workspace add` refuse it and point to `--project`; use `mcp install` to register its tools with a standalone Codex. Reconnecting with `--workspace` replaces the previous connection and does not carry over registered local folders, so register them again after reconnecting with `--project`.
- **Choosing a project.** On a workspace connection every tool marked "granted project" above takes a `project_id` argument; leaving it out is a tool error. The arXiv, connector and `list_library` tools act on no single project and take none. NOUS checks it against the granted workspace on every call and refuses any other project. `list_library` shows the project ids to choose from; a workspace connection always requests `library:read` for it, so it works without `--library`.
- **`--library`.** Also requests `library:write` (and `library:read`, which a workspace connection requests anyway), and needs both `--tools` and `--write`. It works with `--project` too, where `list_library` shows that one project. `library:read` adds the `list_library` tool, which lists the folders (NOUS projects) the connection may use, with their document counts, one page at a time. `library:write` lets the reversible library actions run without asking each time; deleting a folder and ingesting papers still wait for approval (see [Note and library actions](#note-and-library-actions)). The flag also adds `--library` to the `nous-harness mcp` command that managed sessions and `mcp install` launch. A workspace connection made with `--write` but without `--library` is offered `request_action` and `get_action_status` too: NOUS refuses it a note, and every library action it asks for waits for your approval.
- **Approval page.** The browser page shows the workspace instead of a project, and each scope with a plain-language label beside its raw name. The `library:write` label says which changes run without asking and which still need approval.

The backend contract for workspace grants and library scopes is in [`docs/engineering/harness-bridge.md`](../../docs/engineering/harness-bridge.md#workspace-connections-and-library-scopes).

### Account connections

Connect with `--account` instead of `--project` or `--workspace` to bind the grant to your whole NOUS account: every project in workspaces of your organization that you own or belong to, not archived, re-checked on every call. `nous --rc` does this for you.

```sh
pnpm --filter @nous/harness-bridge start connect --api https://nous.example/api/v1 --account --label "My computer" --write --library
```

- **Scopes.** Always `harness:execute`, `tools:read` and `library:read` (so `list_library` can name the projects); `--write` adds `tools:write`, `--library` adds `library:write` (and needs `--write`). `--publish`, `--chat`, `--handoff` and `--context` are refused: each belongs to one project.
- **Folders.** `workspace add` registers a folder with no project; any chat in the account can pick it. One run per folder at a time, as before.
- **Runs.** A chat in project P that picks this computer gets a run grant for P under the account approval. The start command names P (`projectId`), and the run's MCP child (`nous-harness mcp --project P`) fills in or enforces `project_id` on every tool, hides `list_library`, and refuses `create_folder`, `move_papers_between_folders` and `update_document_metadata`. If NOUS names no project, the run starts without NOUS tools. This keeps the model in the chat's project; it is not a security boundary (the child holds the account credential).
- **Standalone Codex.** `mcp install` prints an unpinned registration: its tools take a `project_id`, and `list_library` lists the choices with their workspaces; `create_folder` takes a `workspace_id`.
- **Switching.** Connecting with `--account` replaces a project or workspace connection and carries its folders over as account folders; the old approval stays until revoked at `/integrations/devices`.
- **Old servers.** A NOUS without account connections (422) or with them switched off (503 `Account connections are disabled`) makes `connect --account` print `this NOUS server doesn't support account connections yet`.

The backend contract is in [`docs/engineering/harness-bridge.md`](../../docs/engineering/harness-bridge.md#account-connections).

## Binding reuse, status, and chat handoffs

Running `connect` again with the same API, project (or workspace) and `--chat` (both absent counts as equal), and with no scope that the stored connection lacks, reuses the stored binding. It forces one grant renewal as a liveness probe and, if NOUS issues a new grant token, prints `Reusing binding` and skips the browser login and consent entirely. Reuse never narrows the grant: scopes stored but not requested this time stay, and the `Reusing binding` line names them. To drop them, revoke this device's access (Revoke at `/integrations/devices`) or run `disconnect`, then run `connect` again with the same (narrower) flags. Either way this device's runs stop (for a workspace connection, its NOUS tools stop until it reconnects). Revoke is the less disruptive path: it keeps the local state, so `connect` carries the registered folders over. `disconnect` deletes that state, so register each folder again with `workspace add`, and it also ends your CLI sign-ins on other computers. If that renewal fails because the grant has lapsed (the bridge was stopped for more than about 15 minutes), `connect` resumes the grant from your earlier browser approval and prints `Resumed binding`: the same device and registered folders, with no browser step. NOUS refuses to resume once that approval or this device has been revoked (Revoke under **Settings → Connected devices**, or `disconnect`), once your CLI sign-in has ended, or more than 30 days after the grant lapsed. A different chat, project or workspace (a project and a workspace never reuse each other), a missing scope, a connection without a stored grant ID, or a renewal and a resume that both fail runs the full login and consent flow. When the new connection replaces a different chat, project or workspace, the superseded local credential file is removed. **Known gap:** its server-side grant and consent are not revoked; that grant lapses within 15 minutes, but the consent stays valid until revoked another way. Run `disconnect` before switching chats if that matters. The reuse probe forces a grant renewal, which revokes the previous grant token: running MCP children pick up the renewed token on their next call, but do not reconnect while a publish sequence (reserve, upload, finalize) is in progress.

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

If NOUS refuses one run's events while this device's grant is still valid (for example, its chat was deleted or linked to another project), the bridge prints a notice, stops uploading that run and keeps serving the others. If the run had already finished on this computer, the bridge releases its folder. Otherwise the folder stays reserved and the run's Codex turn is interrupted once its lease lapses (within the 15-minute grant lifetime). To free a folder that NOUS or the bridge still reports busy, revoke this device's access at `/integrations/devices`, run `connect` again with the same flags (it registers a new device and carries the registered folders over under new IDs, and your CLI sign-ins on other computers are not affected), then restart `nous-harness run`. Or run `disconnect`, then `connect` with its original flags and `workspace add` again, then restart `nous-harness run`; `disconnect` also ends your CLI sign-ins on other computers. A running bridge reads the stored connection only when it starts, so it fails until restarted. Either way, select the new computer entry and folder in the chat composer. Both ways also stop this device's other runs, so let them finish or stop them first. Expired or revoked access still disconnects the bridge. See `docs/engineering/harness-bridge.md`, "A run NOUS refuses".

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

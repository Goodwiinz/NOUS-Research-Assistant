# NOUS terminal

Use Node 24 and pnpm 10.18.2. From this worktree:

```sh
pnpm install --frozen-lockfile
./nous login
./nous
# Start a fresh conversation:
./nous --new
```

The terminal shares the existing CLI login, backend URL, selected model,
project/paper context, and thread. `NOUS_API_URL` overrides the backend URL.

Interactive `./nous` now starts Ink. Project, paper, thread and settings menus
use Ink controls; no Clack menu is mounted. `./nous --ink` remains an alias.
Login and one-shot queries continue through the existing CLI entry point.
The existing prompt-history and draft files are reused; unsent text is restored.
Prompt history migrates to JSON records on the next append, preserving new
multiline prompts as single entries. Old entries already split into lines cannot
be reconstructed. Credentials use owner-only permissions and atomic replacement;
loading legacy credentials repairs their permissions.

- `/projects`, `/papers`: arrow-key pickers; Enter selects, Esc cancels.
- `/threads pick`: arrow-key thread picker; `/threads` opens the action panel.
- `/settings`, `/model`: native settings/model menus; `default` resets the model.
- `/history [n]`, `/forget [id]`, `/context project|paper <id>`, `/context clear`
  preserve the original workflows. `/clear` reduces the interactive transcript.
- `/settings set api_url <url>` updates the saved backend and requires a fresh
  login. An explicit `NOUS_API_URL` environment override takes precedence.
- Type `/` to open the command list; keep typing to filter, use ↑/↓ to select,
  Enter to complete, and Esc to dismiss. Enter on a complete command runs it.
  Tab moves focus between controls.

## Keyboard

| Key                        | Action                                                    |
| -------------------------- | --------------------------------------------------------- |
| Enter                      | Send, or queue a follow-up during a response              |
| Ctrl+J                     | Insert a newline                                          |
| Shift+Enter                | Insert a newline when the terminal distinguishes this key |
| Tab / Shift+Tab            | Move focus between input and controls                     |
| Enter on a focused control | Activate it                                               |
| Ctrl+P / Ctrl+N            | Previous / next prompt from local history                 |
| PageUp / PageDown          | Expand / reduce the interactive transcript window         |
| Esc                        | Close panels, cancel editing or pending reads; retain approval input |
| Ctrl+C                     | Stop the current operation; exit when idle                |

The input also supports arrows, Home/End, Ctrl+A/E, Ctrl+W/U/K/D and
terminal-supported Alt+B/F/D editing. Older messages graduate into terminal
scrollback; PageUp makes more messages interactive again.

At an approval prompt, type `yes` or `no`, then Enter. Blank input does nothing.
Stopping a response parks its queued follow-ups. A new explicit send resumes the
queue. Failed streams and approval requests are never automatically retried.
Pending slash-command reads show a loading indicator. Esc or Ctrl+C cancels the
read and ignores late results. Once a write is submitted, cancellation waits for
its result and does not claim rollback or retry it. Editing gives the edit field
exclusive composer focus; saving or cancelling returns focus to chat.

## Commands

| Commands                                            | Purpose                                                                      |
| --------------------------------------------------- | ---------------------------------------------------------------------------- |
| `/help`, `/quit`                                    | Show help or exit                                                            |
| `/threads`, `/thread <id>`, `/new`, `/refresh`      | Browse, select, create, or reload conversations                              |
| `/rename <title>`                                   | Rename the current server thread                                             |
| `/archive [id]`, `/unarchive <id>`                  | Archive or restore a server thread                                           |
| `/delete [id]`                                      | Delete a thread after typing `delete` in its confirmation dialog             |
| `/projects`, `/project <id\|none>`                  | Browse or select project context; selection starts a fresh thread            |
| `/papers`, `/paper <id\|none>`                      | Browse documents or select paper context; selection starts a fresh thread    |
| `/model [name]`                                     | Show or select a backend-supported model                                     |
| `/attach <path>`                                    | Upload a file; paths may contain spaces                                      |
| `/documents [search]`, `/document <id>`             | Find and attach an existing document                                         |
| `/detach <attachment-id>`                           | Remove an attachment from the composer                                       |
| `/copy <number>`, `/quote <number>`                 | Copy or quote a displayed message                                            |
| `/edit <number>`, `/retry [number]`                 | Edit a user message or regenerate an assistant reply in a new branch         |
| `/branch <number> <previous\|next>`                 | Select an alternate message branch                                           |
| `/like <number>`, `/dislike <number>`               | Save feedback on a server-persisted assistant message                        |
| `/search <text>`                                    | Search the current transcript                                                |
| `/export <path>`                                    | Export the current branch as Markdown; refuses to overwrite an existing file |
| `/diff <old-path> \| <new-path>`                    | Show a line/word diff between two local files                                |
| `/queue`, `/remove <queue-id>`, `/steer <queue-id>` | Inspect, remove, or move queued prompts to run next                          |

## Reference features

- **Thread and suggestions:** runtime-scoped messages, empty-state suggestions,
  history restoration, and interactive transcript windowing.
- **Loading and status:** animated loading, elapsed time, model label, message
  count, output-token count, throughput, and running/error/cancelled status.
- **Composer:** multiline editing, send/stop, upload/existing-document
  attachments, attachment status/removal, quote preview/dismissal, and queue
  preview/reorder/removal. Up to 10 documents per prompt; local uploads up to
  50 MiB. Upload completion does not imply indexing has finished.
- **Messages and parts:** Markdown, tools, sources, reasoning summaries,
  image/file metadata, generic data-part fallback, and message attachments.
  Image/file bodies are not printed as terminal control sequences.
- **Action bar and branches:** copy, edit, explicit retry, feedback, previous/
  next branches. Native clipboard providers: macOS `pbcopy`, Windows `clip`,
  Linux `wl-copy`, `xclip`, or `xsel`; `/export` works without a clipboard.
- **Thread list:** select, new, archive/unarchive and confirmed delete. The paginated workspace API includes all accessible threads and server branches.
- **Tools and reasoning:** collapsible tool arguments/results and server-provided
  reasoning summaries. Approvals remain visible even when their group is
  collapsed. No client-side tool execution is added.
- **Diff:** unified patches in tool results, or local file comparisons, with line
  numbers, context folding and changed-word highlighting.

Deprecated `If`, `Empty`, and `Content` aliases are replaced by `AuiIf` and
`MessagePrimitive.Parts`. Low-level context providers are used where needed;
they are building blocks, not separate user commands.

## Branch persistence

Edits and retries create a **separate server thread** in the same conversation,
copy the selected history prefix, and stream the replacement there. The original
thread remains intact. A failed fork leaves the original intact and identifies
any partially created branch; it is not silently retried or deleted.

Deleting a thread removes its local navigation links, including saved sibling
graphs, while retaining shared prefixes used by surviving branches. Cancelling a
fork waits for any submitted write, then stops further copies and reports the
partial branch ID.

The branch-navigation graph is saved under `NOUS_CONFIG_DIR/branches` (default
`~/.nous/branches`), scoped to backend and account. The backend owns each branch's
transcript; local graph files connect those threads for the branch picker.
Removing local graph files loses those navigation links, not the server threads.

## Implementation and checks

Web and terminal share `@nous/chat-runtime` message mapping, queue lifecycle,
attachment IDs, quotes, and approval callback wiring. Node transport and Ink UI
live here; authenticated streaming/services are reused from `frontend/cli`.
React 18 remains the web dependency; Ink owns React 19. The terminal-only Node
module hook deduplicates React and assistant-ui contexts under pnpm's hoisted
layout. One root lockfile is authoritative.

```sh
pnpm --dir terminal type-check
pnpm --dir terminal test
python3 terminal/tests/migration-smoke.py # macOS/Linux TTY integration check
```

Tests use local/mock HTTP, including approval gates, cancellation, queued turns,
attachments, multiline/quoted sends, edit/branch navigation, stale thread loads,
feedback, and confirmed deletion. They do not claim production acceptance.

### Mutation evidence

Run from `terminal/` with Node 24. Each guard below was temporarily removed;
its focused test failed, then passed again after restoration:

| Guard | Focused test pattern | Failure without guard |
| --- | --- | --- |
| `src/session.ts:131` navigation generation check | `stale thread loads` | Slow history replaced the latest selection |
| `src/session.ts:403` approval ID check | `an earlier approval` | An old approval posted a decision to the next gate |
| `src/app.tsx:288` typed delete check | `thread deletion requires` | Blank input deleted the thread |
| `src/app.tsx:482` cancelled-read result check | `dismissed slash reads` | Late project results reopened a dismissed menu |
| `src/app.tsx:227` operation ownership check | `dismissed slash reads` | The cancelled command cleared the newer command's busy state |
| `src/app.tsx:244` submitted-write check | `Escape during a submitted write` | Escape cleared tracking for an already-submitted write |
| `src/session.ts:400` retain loading during fork cancellation | `cancelled forks` | Composer became available while the old fork held the send lock |
| `src/services.ts:178` check cancellation before each prefix write | `cancelled forks` | Cancelled forks kept writing prefix messages |

```sh
node --import ./react-runtime.mjs --import tsx --test --test-name-pattern='cancelled forks' src/regressions.test.tsx
node --import ./react-runtime.mjs --import tsx --test --test-name-pattern='stale thread loads' src/app.test.tsx
node --import ./react-runtime.mjs --import tsx --test --test-name-pattern='an earlier approval' src/app.test.tsx
node --import ./react-runtime.mjs --import tsx --test --test-name-pattern='thread deletion requires' src/app.test.tsx
node --import ./react-runtime.mjs --import tsx --test --test-name-pattern='dismissed slash reads|Escape during a submitted write' src/app.test.tsx
```

The shared queue's approval-busy guard (`../packages/chat-runtime/runtime.ts:120`)
was also removed and restored. Without it, the follow-up sent during a pending
approval; the restored focused test passes:

```sh
pnpm --dir ../frontend exec vitest run src/components/chat/aui/__tests__/ChatRuntimeProvider.test.tsx -t 'queues a follow-up'
```

### PR review regressions

The review follow-up covers offline startup with a shared sibling graph, fork
citation preservation, Unicode title limits, assistant-only feedback, thread-ID
assignment, and config-free token updates. The real PTY smoke starts with a
503 transcript response and a saved graph whose head belongs to another thread;
it verifies the configured thread remains visible and commands still work.
Run it with `CI=true GITHUB_ACTIONS=true` to exercise Ink's explicit interactive
mode under the same detection conditions as hosted CI.

Cancellation already has SDK-owned safeguards. The terminal SDK parks queued
prompts until an explicit send; the older web SDK clears them. Both are preserved
by `packages/chat-runtime/runtime.ts:158` returning the original queue adapter.
Mutation verification replaced that returned adapter with a copy whose
`__internal_notifyCancelled` was undefined and whose `clear` was a no-op. Both
tests below failed because the pending prompt was dispatched after cancellation,
then passed after restoring the original adapter:

```sh
# From terminal/
node --import ./react-runtime.mjs --import tsx --test --test-name-pattern='Ctrl.C parks queued' src/regressions.test.tsx
pnpm --dir ../frontend exec vitest run src/components/chat/aui/__tests__/ChatRuntimeProvider.test.tsx -t 'does not dispatch queued'
```

Follow-up local validation: 42 terminal tests and 2,051 frontend/CLI tests passed;
both TypeScript checks, the CI-mode PTY smoke, Python formatting/lint, and the
smoke script's CI-pinned MyPy check passed. The broad local CI wrapper could not
validate backend OpenAPI/Alembic gates because its Python environment lacked
`langgraph` and `alembic`; its unrelated backend suite was stopped during
collection. Hosted CI remains the source of truth for those backend gates.

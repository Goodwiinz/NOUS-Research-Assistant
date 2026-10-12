# `nous --rc`: pair this folder with web NOUS in one command

**Status:** Design, written 2026-10-12 against `origin/develop` `d8c5894d1`. Decisions were made by the owner in a brainstorming session the same day; the "Decisions" table records them. Nothing here is implemented yet.

**Contracts touched:** [harness bridge](../engineering/harness-bridge.md), [bridge package](../../packages/harness-bridge/README.md), [terminal client](../../terminal/README.md), [live-proof runbook](../testing/harness-live-proof.md).

## Problem

Pairing a computer with a NOUS project so that a goodwiinz.tech chat can run local Codex takes three `nous-harness` commands with eight flags, a project UUID copied out of a browser URL, Node 24 on the PATH, and a `codex` that is exactly `0.153.4`. The owner's words: "I dont wanted to have this complex cli to connect to the remote nous", and "like `nous --rc` → projects list or new project".

The pieces already exist: the bridge does the browser login, the consent, the folder registration and the Codex runner; the backend lists and creates projects; the terminal client lists projects. `nous --rc` is glue plus a picker.

## Decisions

| Question | Decision | Why |
| --- | --- | --- |
| What does the pairing allow? | Codex runs in the current folder **and** reads the project's documents (`harness:execute` + `tools:read`). No writes. | Owner: "Codex should read project docs too". Publication and library writes stay with Phase 2 of the live-proof runbook. |
| Browser steps | Keep both (sign-in once; one Approve click per project) and reuse the pairing afterwards. | The Approve click is the security boundary; the bridge already reuses a live pairing with no browser. No backend change. |
| Which Codex? | Upgrade the bridge to accept Codex `0.161.0` (the owner's installed version). | Owner chose the upgrade over a side-by-side `0.153.4`. The three vendored request schemas are byte-identical between `0.153.4` and `0.161.0`; every change in the messages the bridge sends or reads only adds optional fields (checked with `codex app-server generate-json-schema` for both versions on 2026-10-12). |
| Versions accepted | A short list of tested versions: `0.153.4`, `0.161.0`. | Exact-pin breaks on every Codex release; "accept any version whose schemas match" cannot catch the reply-shape change that broke every run on 2026-10-05 (defect #5 in the local proof). |
| How is it launched? | The repo's `nous` launcher gains `--rc`; the owner symlinks it into `~/.local/bin` from a checkout that tracks `origin/develop`. | Works today for the one user. Packaging (`npm i -g`) waits until someone else needs it. |
| Where does the code live? | A new `rc` subcommand in `packages/harness-bridge` (`nous-harness rc`); `nous --rc` execs it. | Fewest new parts: login, pairing reuse, folder registration and the runner are all in the bridge. The picker is a numbered list; a nicer picker can replace it later without touching anything underneath. |
| Pairings per computer | One, as today. Picking a different project replaces the pairing and asks for Approve again. | Owner chose this over one pairing per project. |
| The replaced project's consent | Print where to revoke it (`/integrations/devices`); do not revoke automatically. | `disconnect` also signs the user out of the CLI on every computer and drops registered folders, which is too heavy for switching projects. The old grant itself lapses within 15 minutes; the consent stays valid until revoked, which is the documented existing gap. |

## What the user sees

```
$ cd ~/code/my-paper
$ nous --rc
NOUS: https://dev-api.goodwiinz.tech/api/v1
Signed in.                         ← first time: "Open <link> and type ABCD-1234"
Your projects:
  1) Thesis lit review      12 docs
  2) RAG eval notes          4 docs
  n) New project
Pick a project [1-2, n]: 1
Approve this Mac in your browser: https://goodwiinz.tech/...   ← only when the pairing changes
Connected: "Thesis lit review" ↔ /Users/me/code/my-paper  (Codex 0.161.0)
In a chat for this project on goodwiinz.tech, choose Local Codex → this computer → my-paper.
Press Ctrl-C to stop.
```

- The folder `nous --rc` runs in is the folder Codex works in. There is no flag to choose another.
- `n` asks for a name, creates the project, then continues as if it had been picked. `POST /projects` needs a `workspace_id`: `rc` reads `GET /api/v2/workspaces`; with one workspace it uses it, with several it asks which (numbered, same style).
- The same project as last time: no browser. The bridge's existing reuse path renews the grant as a liveness probe and connects.
- The label NOUS shows for this computer defaults to the hostname.

## Components

### 1. `nous-harness rc` (`packages/harness-bridge/src/cli.ts`)

Inputs: `--api URL` (the launcher passes `NOUS_API_URL`, else its dev default) and `--dir PATH` (the launcher passes `$PWD`; `pnpm start` runs the CLI from the package directory, so the folder must be passed in). `--store` and `--label` stay optional as for `connect`.

Steps, in this order:

1. **Codex first.** `new CodexAdapter().probe()`. A missing or untested Codex fails here, before any network call, so the user never approves a pairing that cannot run.
2. **Sign in.** The login block inside `connect` (`/cli-auth/start` → poll `/cli-auth/status/<id>`) moves into its own `login()`. `rc` tries the stored connection's CLI access token first (`store.load(previous.credentialHandle).accessToken`); if there is none or NOUS answers 401, it runs `login()`. `connect` gains an optional `accessToken`; when given, it skips its own login so the user is not asked twice.
3. **Pick.** `GET /projects?limit=50` → numbered list plus `n`. Invalid input re-prompts. `n` → name prompt → workspace (see above) → `POST /projects` with `{ workspace_id, name }`.
4. **Pair.** `connect({ apiUrl, projectId, label, tools: true, accessToken })`. The reuse path handles the same-project case; otherwise the full consent runs and the previous pairing is replaced. When the previous pairing was a different project, print: `Your approval for <old project id> stays active until you revoke it at <frontend>/integrations/devices.`
5. **Folder.** `addWorkspace({ root: dir })`. It already returns the existing registration when the real path matches.
6. **Run.** `runBridge(stateDir, signal)` in the foreground. Ctrl-C stops Codex the way `run` does and keeps the pairing.

`rc(options)` takes the same injectable `ClientOptions` as the other commands (`fetchFn`, `announce`, `delay`) plus `prompt(question) → Promise<string>`, `probe()` and `run()` so tests never touch the network, a terminal or a real Codex. `main()` wires the real `readline/promises`, adapter and `runBridge`, and refuses to start when stdin or stdout is not a TTY.

Scopes requested: `["harness:execute", "tools:read"]`. The MCP server that managed sessions get is unchanged.

### 2. Tested Codex versions (`packages/harness-bridge/src/adapters/codex.ts`)

- `SUPPORTED_CODEX_VERSION = "0.153.4"` becomes `SUPPORTED_CODEX_VERSIONS = ["0.153.4", "0.161.0"] as const`.
- `probe()` accepts `codex-cli <v>` for any listed `v`, remembers which one it saw, and otherwise fails with `Codex <found> is not tested with NOUS; tested versions: 0.153.4, 0.161.0`.
- The `initialize` handshake accepts a `userAgent` containing `/<probed version>`.
- `src/adapters/protocol-0.153.4/` becomes `src/adapters/protocol/`. The three schemas stay as they are (identical in both versions). Its README lists the tested versions and the procedure to add one: `codex app-server generate-json-schema --out <tmp>`, compare the three files and the full v2 schema against the previous version, run the package tests, do one live run from a NOUS chat, then add the version to the list.
- `NOUS_HARNESS_CODEX_MODEL` keeps working. With `0.161.0` the owner's default model (`gpt-6.1-sol`) should work without it; the live run confirms that.

### 3. The `nous` launcher (repo root)

- `--rc`: resolve the script's own directory through its symlink, check `node --version` is 24.x (else: `nous --rc needs Node 24 (found <v>)`), then `exec pnpm --dir "$REPO" --filter @nous/harness-bridge start rc --api "${NOUS_API_URL:-$NOUS_DEFAULT_API_URL}" --dir "$PWD" "$@"`.
- Other invocations are unchanged.
- `terminal/README.md` documents the one-time setup: `ln -s <checkout>/nous ~/.local/bin/nous` from a checkout that tracks `origin/develop`, and that `nous --rc` needs Node 24 and a tested Codex.

## State

Nothing new is stored. `rc` uses the bridge's `~/.nous/harness-bridge` as `connect`, `workspace add` and `run` do: `connection.json` (device, project, label, scopes, registered folders, credential handle) and the owner-only credential files. The project **name** is not stored; `rc` prints it from the list it just fetched.

## Errors

Each failure prints one sentence and exits non-zero unless noted.

| Case | Behaviour |
| --- | --- |
| Node is not 24 | Launcher: `nous --rc needs Node 24 (found 22)`. |
| Codex missing or untested | Before any network call: `Codex 0.158.0 is not tested with NOUS; tested versions: 0.153.4, 0.161.0`. |
| Not a terminal | `nous --rc needs an interactive terminal`. |
| NOUS answers 503 | The request error passes through unchanged. (Amended 2026-10-12: the pairing endpoints are not gated by the server flags, so a 503 there is an outage, not "turned off"; with the flags off, pairing succeeds and the web chat reports it.) |
| 401 with a stored token | Run `login()` and retry once; a second 401 is an error. |
| No projects | The list shows only `n) New project`. |
| Bad choice | Re-prompt; never exit. |
| Approval not given / poll times out | `Not approved; run nous --rc again`. Nothing is saved (existing `connect` behaviour). |
| Ctrl-C while running | Existing `run` shutdown; the pairing is kept. |
| Socket drops while running | Existing reconnect loop and liveness watchdog; `rc` adds nothing. |
| Folder reserved by an earlier run | Existing bridge message and recovery steps. |

## Testing

Package tests (`pnpm --filter @nous/harness-bridge test`), fixture-backed like the existing `connect-reuse.test.ts` and `codex.test.ts`:

- `rc`: picks project 2 and connects with `harness:execute` + `tools:read`; `n` creates a project (asserting the `POST /projects` body) and connects to it; the same project as the stored pairing makes no `/cli-auth/*` or `/grant-requests` call; a stored token that answers 401 triggers exactly one `login()`; an invalid choice re-prompts; a probe failure makes zero network calls; a 503 prints the "turned off" sentence; a different project than the stored one prints the revoke hint and replaces the pairing; the folder is registered once across two runs.
- Versions: the fake app-server reports each listed version in turn and both pass `probe()` and the handshake; an unlisted version fails `probe()` with the sentence above; a handshake whose `userAgent` names a different listed version than the probe fails.
- `pnpm --filter @nous/harness-bridge type-check`.

Live acceptance (manual, after [#1978](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1978) is deployed): `nous --rc` from a scratch folder against `https://dev-api.goodwiinz.tech/api/v1` with Codex `0.161.0`; a Local Codex chat on goodwiinz.tech that asks for one command approval and finishes; a second `nous --rc` for the same project with no browser step. Until that bundle is recorded under `docs/testing/evidence/`, `0.161.0` is listed as tested on the strength of the package tests and the schema comparison only, and the runbook's rows stay NOT RUN.

## Out of scope

- A richer picker (arrow keys, search). The numbered list is the whole UI for now.
- Packaging for `npm i -g`.
- One pairing per project; automatic revocation of the replaced project's consent; workspace-wide pairings that can run Codex.
- `tools:write`, `artifacts:publish`, `library:*`. Phase 2.
- Reading the user's CLI token from `~/.nous/config.json` (the `./nous login` store). The bridge keeps its own sign-in; sharing the two is a later cleanup.

## Risks

- **Codex `0.161.0` at runtime.** The schema comparison covers what the bridge validates and the start/resume/turn shapes, not every reply. The 2026-10-05 proof found a reply shape (`writableRoots`) that the fixture tests did not model. The live run above is the only proof; the version list keeps `0.153.4` so the known-good path stays available.
- **Conflicts.** [#1973](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1973) (artifact sharing) also edits `packages/harness-bridge/src/cli.ts`; the `rc` work should land as small PRs and merge `develop` forward rather than rebase.
- **The stale main checkout.** `~/development/RAG_system` is ~2,400 commits behind `develop` and has no `packages/harness-bridge`; the symlink must point at a current checkout, which the README says.

## Delivery

Two PRs, both cut from `develop`:

1. **Codex versions:** the version list, the handshake change, the `protocol/` rename and its README, the package tests. Independent of `rc`.
2. **`nous --rc`:** `login()` extraction, `connect({ accessToken })`, the `rc` command, the launcher, `terminal/README.md`, the bridge README's quick-start section, and a dated amendment line in `docs/engineering/harness-bridge.md` ("User workflow").

PR 2 does not need PR 1 to build or pass its tests; it needs PR 1 for the live run with the owner's Codex.

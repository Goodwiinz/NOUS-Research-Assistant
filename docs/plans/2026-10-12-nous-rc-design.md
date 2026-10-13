# `nous --rc`: pair this folder with web NOUS in one command

**Status:** Design, written 2026-10-12 against `origin/develop` `d8c5894d1`. Decisions were made by the owner in a brainstorming session the same day; the "Decisions" table records them. Nothing here is implemented yet. Amended 2026-10-12: implemented on `feat/nous-rc`; PR 1 (#1983) is on `feat/codex-tested-versions`; the live run is NOT RUN. Amended 2026-10-13: the launcher and cancel behaviour changed after a second review, and skipping the browser for the same project holds only while the stored pairing is live (see the amendment lists under "Decisions").

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

**Amendments 2026-10-12** (after implementation and review on `feat/nous-rc`; the sections below keep the original design):

- The CLI flag is `--root`, not `--dir` (the same flag as `workspace add`); the launcher passes `--root "$PWD"`.
- When the default `node` is not 24.x, the launcher uses Homebrew's `node@24` (`/opt/homebrew/opt/node@24/bin` or `/usr/local/opt/node@24/bin`) if present; otherwise it prints `nous --rc needs Node 24 (found <v>); install it with: brew install node@24`.
- A missing or broken Codex prints ``Could not run `codex --version`; check that Codex is installed, on your PATH and working``; the "not tested" sentence is for a Codex that runs but is not listed.
- Before the bridge starts, Ctrl-C (at a prompt or during a browser wait) or Ctrl-D at a prompt cancels with `Cancelled` and no pairing; once the bridge runs, Ctrl-C stops it and keeps the pairing.
- Switching to a different project carries the registered folders over to the new project (owner's decision), as `connect` already does on a reconnect.
- A refused or expired browser sign-in prints `Not approved; run nous --rc again`, the same as a refused or expired approval.
- The 503 "turned off" message was dropped: a 503 passes through as `NOUS request failed (503) <path>` (see the Errors table's own amendment).
- The "What the user sees" transcript is illustrative. The real prompts are the bridge's existing `Authorize CLI login in your browser: <link>` / `Type this code on that page: <code>` and `Approve this device and project in your browser: <link>`, and the last lines read `In a chat for this project on NOUS, choose Local Codex → <device label> → <folder label>.` and `Press Ctrl-C to stop.`

**Amendments 2026-10-13** (after a second review). The first three items change Components → 3 (the launcher), and the first replaces the 2026-10-12 Node 24 item; the fourth replaces the 2026-10-12 Ctrl-C/Ctrl-D item; the fifth extends the 2026-10-12 folder carry-over item; the sixth limits the "Browser steps" decision; the next four record commit `32247a4b2` and the last three commit `2bfd22090`; the sample transcript's folder is now written `<home>/code/my-paper`.

- The launcher runs Node 24 directly (`<node> --import tsx src/cli.ts rc …` from `packages/harness-bridge`) instead of through `pnpm`, and leaves PATH untouched. Review finding: `pnpm --filter` could run a same-named package from the current folder when the launcher was copied, and the PATH prepend leaked Node 24 into the tools Codex runs. Node 24 is the default `node` if it is 24.x, else Homebrew's `node@24` binary; otherwise the launcher prints `nous --rc needs Node 24 (found <v>); switch your default node to 24 (for example nvm use 24) or install it with: brew install node@24`.
- The launcher refuses a copied launcher (`nous: <dir> is not a NOUS checkout; link this launcher with ln -s instead of copying it`) and a checkout without dependencies (`nous --rc: dependencies are missing; run: pnpm --dir "<checkout>" install`).
- The launcher's `--api` and `--root` come after the user's arguments, so they win (the CLI keeps the last value); other arguments, such as `--label`, pass through.
- Until the bridge starts, any Ctrl-C, Ctrl-D or end of input cancels the whole command (`Cancelled`, exit 130), not only at an open prompt. The stored pairing is unchanged, except that a cancel just after the browser approval can leave the new pairing saved without the folder; a refreshed sign-in may be kept. Ctrl-Z and `fg` resume the prompt.
- Registered folders are also carried over when the new pairing is on a different NOUS server (`connect` re-registers them whenever a previous connection exists); the revoke hint is still printed only for a project switch on the same server (see Risks).
- The "approve once per project" decision holds only while the stored pairing is live. A grant lives 15 minutes, can be renewed only before it expires (backend `renew_grant`), and only a running bridge (or a standalone `nous-harness mcp` sharing the pairing) renews it, so without one a pairing lapses 3 to 15 minutes after the bridge stops; an outside client can get a new grant only through the browser approval. The same project skips the browser only while the pairing is live, already includes `tools:read`, and is not bound to a single chat (a `connect --chat` pairing is replaced). Otherwise `nous --rc` asks for approval again and NOUS lists this computer as a new device; the old approval stays active until revoked under Connected devices, and no hint is printed for this case. Going from project A to B and back to A also approves again. Re-issuing a grant from an existing approval needs a backend endpoint; it is deferred to a follow-up (owner's decision 2026-10-13). After a re-approval the new device has the same label as the old one unless `--label` is passed, and revoking the old approval does not remove the old device from the list.
- One bridge per state folder: `nous --rc` and `nous-harness run` take `<state>/bridge.lock` (owner-only, naming the pid) before probing Codex or touching the network, and refuse a second process with `Another nous --rc or nous-harness run (pid N) is already using <dir>; stop it first`. A second bridge on the same state would force-renew the running bridge's grant, reopen its journal and poll as the same device. A lock whose process is gone, or that cannot be read, is replaced; a lock whose pid was reused by another process must be deleted by hand.
- Too-broad folders are refused before any network call: the filesystem root, the home folder, or a folder that contains the state folder print `<folder> is too broad for Codex to work in; run nous --rc in a project folder`. Reason: writes inside the registered folder never ask for approval (`workspace-write` with `on-request`), and the state folder holds the bridge's credentials.
- A request that cannot connect prints `Could not reach NOUS at <url> (<reason>)` instead of a bare `fetch failed`; timeouts print their own error, and HTTP errors still pass through as `NOUS request failed (<status>) <path>`.
- SIGTERM and SIGHUP before the bridge starts cancel like Ctrl-C (`Cancelled`, exit 130).
- The approval link is printed in full. Found in final review: NOUS sends `approval_url` as a root-relative path, and the earlier URL check (http(s) only) rejected it, so a full pairing failed with `invalid NOUS response`. The bridge now resolves the path on the NOUS website origin that `login()` saw in the sign-in link, saved as `website.json` in the state folder and used only for the same API. When no origin is known (a reused sign-in from before this change), it prints the path followed by `(open this path on your NOUS website)`. A path that would resolve to another host is still refused.
- The lock refusal ends `(if no such process is running, delete <dir>/bridge.lock)`, and a lock taken before the last reboot is replaced even if its pid is alive again.
- While the bridge runs, SIGHUP (closing the terminal window) stops it cleanly like Ctrl-C and SIGTERM, in `nous --rc` and in `nous-harness run`.

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
Connected: "Thesis lit review" ↔ <home>/code/my-paper  (Codex 0.161.0)
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

- `rc`: picks project 2 and connects with `harness:execute` + `tools:read`; `n` creates a project (asserting the `POST /projects` body) and connects to it, asking which workspace only when there are several and re-prompting on a bad pick; an API path other than `/api/v1` or a malformed workspace list fails closed; the same project as the stored pairing makes no `/cli-auth/*` or `/grant-requests` call; a stored token that answers 401 triggers exactly one `login()` and the reused pairing keeps the new sign-in; a second 401 after that sign-in is an error; a stored sign-in is never sent to a different NOUS; an invalid choice, including `1abc`, re-prompts; a probe failure, a missing folder and a file in place of the folder each make zero network calls; a 503 passes through unchanged; a refused sign-in or approval prints `Not approved; run nous --rc again` and saves nothing; a different project than the stored one prints the revoke hint (after the new pairing, naming the old project) and replaces the pairing, and a previous workspace connection gets its own hint; a truncated project list says so; project names lose escape sequences and control characters; the folder is registered once across two runs. (Amended 2026-10-12 after review.)
- Versions: the fake app-server reports each listed version in turn and both pass `probe()` and the handshake; an unlisted version fails `probe()` with the sentence above; a handshake whose `userAgent` names a different listed version than the probe fails.
- `pnpm --filter @nous/harness-bridge type-check`.

Live acceptance (manual, after [#1978](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1978) is deployed): `nous --rc` from a scratch folder against `https://dev-api.goodwiinz.tech/api/v1` with Codex `0.161.0`; a Local Codex chat on goodwiinz.tech that asks for one command approval and finishes; a second `nous --rc` for the same project with no browser step. Until that bundle is recorded under `docs/testing/evidence/`, `0.161.0` is listed as tested on the strength of the package tests and the schema comparison only, and the runbook's rows stay NOT RUN. (Amended 2026-10-13: superseded by the rule in [`packages/harness-bridge/src/adapters/protocol/README.md`](../../packages/harness-bridge/src/adapters/protocol/README.md) that a `NOT RUN` row may exist only on an unmerged branch, so PR 1 does not merge until the `0.161.0` live run is recorded there; see Task 11 of the plan.)

## Out of scope

- A richer picker (arrow keys, search). The numbered list is the whole UI for now.
- Packaging for `npm i -g`.
- One pairing per project; automatic revocation of the replaced project's consent; workspace-wide pairings that can run Codex.
- `tools:write`, `artifacts:publish`, `library:*`. Phase 2.
- Reading the user's CLI token from `~/.nous/config.json` (the `./nous login` store). The bridge keeps its own sign-in; sharing the two is a later cleanup.
- Re-issuing a grant from an existing approval, so a lapsed pairing for the same project needs no browser step and no new device. (Amended 2026-10-13: needs a backend endpoint; deferred to a follow-up by the owner's decision.)

## Risks

- **Codex `0.161.0` at runtime.** The schema comparison covers what the bridge validates and the start/resume/turn shapes, not every reply. The 2026-10-05 proof found a reply shape (`writableRoots`) that the fixture tests did not model. The live run above is the only proof; the version list keeps `0.153.4` so the known-good path stays available.
- **Conflicts.** [#1973](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1973) (artifact sharing) also edits `packages/harness-bridge/src/cli.ts`; the `rc` work should land as small PRs and merge `develop` forward rather than rebase.
- **The stale main checkout.** `~/development/RAG_system` is ~2,400 commits behind `develop` and has no `packages/harness-bridge`; the symlink must point at a current checkout, which the README says.
- **Credential rewrite race.** (Amended 2026-10-12.) When NOUS rejects the stored sign-in, `rc` writes the new token into the stored credential (`load`, then `update`), so a bridge renewing the grant from the same state directory between those two steps would have its new grant token overwritten with the old one; this is left as is because it needs both at once.
- **Switching NOUS servers.** (Amended 2026-10-12.) The revoke hint is printed only when the replaced pairing was on the same NOUS server; a pairing replaced on another server keeps its approval there with no hint.
- **Re-approval after a break.** (Amended 2026-10-13.) Once the stored pairing has lapsed, every `nous --rc` for the same project asks for browser approval again and registers a new device, so approvals pile up in Connected devices until the user revokes them; no hint is printed. The fix is the deferred backend endpoint (see Out of scope).

## Delivery

Two PRs, both cut from `develop`:

1. **Codex versions:** the version list, the handshake change, the `protocol/` rename and its README, the package tests. Independent of `rc`.
2. **`nous --rc`:** `login()` extraction, `connect({ accessToken })`, the `rc` command, the launcher, `terminal/README.md`, the bridge README's quick-start section, and a dated amendment line in `docs/engineering/harness-bridge.md` ("User workflow").

PR 2 does not need PR 1 to build or pass its tests; it needs PR 1 for the live run with the owner's Codex.

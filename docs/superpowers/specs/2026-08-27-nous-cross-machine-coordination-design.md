# NOUS cross-machine coordination — design

Date: 2026-08-27
Status: proposed design; awaiting owner review before implementation plans

> **Protocol addendum (2026-08-29):** Schema 2 makes the repository-owned
> Vercel deployment guard part of the coordination tree and supersedes this
> design's claims/runs-only assertions. Schema-1 wording below is retained as
> historical, read-compatible implementation evidence.

## Problem statement

The NOUS self-improvement loop (docs/engineering/nous-loop.md) currently
coordinates concurrent agents through `scripts/loop_bridge.py`: a claims board
in a shared local directory, guarded by an exclusive `flock`. That design has
three structural limits:

1. **Single host.** The board lives on one filesystem. A loop on the user's
   Mac and a loop on this Linux machine cannot see each other's claims, so the
   pick → open-PR blind window the bridge closes on one host reopens across
   hosts.
2. **No durable run state.** A tick's progress (reproduced, fixed, reviewed,
   verified, published) lives only in the agent's conversation. A crash,
   restart, or machine switch loses everything except the branch and any PR,
   and there is no machine-checkable record of which gates actually ran
   against which commit.
3. **Evidence is asserted, not bound.** Outcome reports, PR descriptions, and
   audit-ledger statuses are prose. Nothing ties a "reviewed" or "verified"
   claim to the exact SHA it was true for, so head drift silently invalidates
   evidence, and legacy ledgers without provenance read as current status.

This spec defines repository-owned tooling that makes the loop durable,
resumable, evidence-backed, and safely coordinated across machines, using the
GitHub repository itself as the cross-machine transport. Its original
claims/runs-only tree is the legacy schema-1 compatibility format; the
schema-2 protocol addendum makes the exact repository-owned Vercel guard part
of the current remote tree.

## Goals

- Cross-machine claim coordination with Git as the primary transport and the
  existing local flock board retained as a same-host mutex and fallback.
- A durable, append-only local receipt of every run, with an atomic current
  projection, that binds every milestone to an exact `evidence_head_sha`.
- A minimal, secret-free remote projection of each run so a peer machine can
  resume or reconcile a tick using only remotely re-verifiable facts.
- Idempotent, marker-based PR publication of a hosted receipt summary.
- Merge verification that is correct under merge, squash, and rebase merges,
  including late-push-after-review detection.
- Audit-ledger normalization so historical findings are leads until
  revalidated against an exact SHA.
- Full backward compatibility for the `scripts/loop_bridge.py` CLI.

## Non-goals

- Runtime-specific transcript or notification filtering (explicitly out of
  scope).
- Uploading detailed evidence (test output, diffs, traces) anywhere remote.
  Detailed evidence stays local; only schema-whitelisted projections leave
  the machine.
- Automated history compaction of the coordination branch. Compaction is a
  rare, human-authorized maintenance operation outside automated v1.
- Replacing hosted CI, review services, or branch protection. This tooling
  records and verifies their results; it never substitutes for them.
- Coordinating non-NOUS work (human feature branches, unrelated automation).

## Terminology

- **Run** — one NOUS tick from candidate selection to a terminal outcome,
  identified by a `run_id`.
- **Claim** — an exclusive reservation of an area/file set, preventing
  concurrent loops from picking overlapping work.
- **Board** — the set of active claims. There is a remote board (Git-backed)
  and a local board (the flock directory `scripts/loop_bridge.py` manages).
- **Receipt** — the local, append-only event log for a run plus its atomic
  `current.json` projection.
- **Projection** — a derived, minimal, secret-free snapshot of run state:
  local `current.json`, remote `runs/<run-id>.json`.
- **Milestone** — a recorded state entry in the receipt, always bound to an
  `evidence_head_sha`.
- **Coordination branch** — `nous-coordination`, an orphan branch holding only
  coordination metadata, never merged with any code branch.
- **CAS** — compare-and-swap: build a child commit on the fetched tip and push
  fast-forward; a non-fast-forward rejection means the state moved and the
  operation must be re-derived.
- **Cutover** — the maintenance-window switch after which the remote board is
  authoritative (`remote-required` mode).

## Source-of-truth hierarchy

When sources disagree, higher entries win. Lower entries are evidence inputs
or leads, never authority.

1. **GitHub PR and merge state** (via `gh api`) — authoritative for
   publication facts: PR existence, head SHA, review state, checks, merged,
   `merge_commit_sha`.
2. **Fetched Git history of `origin/develop`** — authoritative for what code
   actually landed (ancestry checks, fix presence).
3. **Remote coordination branch** (`nous-coordination`) — authoritative after
   cutover for claims and the resumable run projection. During the compatibility
   window, schema 1 contains `claims.json` and `runs/<run-id>.json`; schema 2
   additionally contains the repository-owned `vercel.json` deployment guard.
   The guard is protocol metadata, not foreign metadata, and must match the
   exact schema-2 bytes.
4. **Local receipt** (`events.jsonl` + `current.json`) — authoritative for
   detailed evidence on the machine that produced it; not visible to peers.
5. **Local flock board** (`$LOOP_BRIDGE_DIR`) — same-host mutex only. After
   cutover it never overrides the remote board; it only adds same-host
   exclusion.
6. **Prose** (audit ledgers, memory files, PR descriptions, docs) — leads.
   Never current state. Ledger entries without an exact confirmed SHA are
   unverified leads by definition (see Audit-ledger semantics).

Corollary for resume: a machine that did not produce a run's local receipt may
trust only levels 1–3, and must re-earn level-4 facts by rerunning the work.

## Architecture

### Package layout

All new code is pure Python standard library. `git` and `gh` are invoked via
`subprocess` through one guarded wrapper; no third-party imports anywhere
under `scripts/nous/`.

| Path | Responsibility |
| --- | --- |
| `scripts/nous/__init__.py` | Package marker; version constant. |
| `scripts/nous/schema.py` | All schema definitions, field whitelists, length/charset limits, run-id/ref/path validators, secret-pattern rejection. Single place limits live. |
| `scripts/nous/coordination.py` | Coordination model: `Claim`, `RunProjection`, `Candidate` dataclasses; remote-capable `CoordinationBackend` and branch-keyed `LocalMutex` protocols; typed errors (`Conflict`, `ClaimLost`, `BackendUnavailable`, `ValidationError`); claim-id fencing; the acquire-remote-then-local compensation sequence. |
| `scripts/nous/backend_local.py` | Local mutex backend: wraps the exact flock/claims.json semantics of today's `scripts/loop_bridge.py` (same file format, same overlap rules, same fail-closed corrupt-state behavior). It is keyed by branch and does not pretend to implement remote run projections or claim-id fencing. |
| `scripts/nous/backend_git.py` | Git backend: bootstrap, shallow fetch, CAS commit/push protocol, expiry/skew handling, retention GC, mode enforcement. |
| `scripts/nous/gitio.py` | The only place that spawns `git`/`gh`. Argument-list subprocess execution (never `shell=True`), no-force runtime guard, dedicated committer identity, injectable runner for tests. |
| `scripts/nous/receipt.py` | Local receipt: append-only `events.jsonl`, atomic `current.json`, the run state machine, head-drift invalidation, resume-state computation. |
| `scripts/nous/preflight.py` | Live preflight: base freshness, workspace isolation checks, backend reachability, mode check, prior-run reconciliation entry point, gate inventory recording. |
| `scripts/nous/reconcile.py` | Reconciliation: remote board vs GitHub reality, expired-claim pruning, orphaned-run finalization, merge verification (merge/squash/rebase rules). |
| `scripts/nous/publish.py` | PR-comment publication: idempotent hidden run-id marker, upsert, slash-command and injection guards. |
| `scripts/nous/summary.py` | Evidence-generated summaries: renders PR comment bodies and terminal reports exclusively from receipt events; no free-composed success claims. |
| `scripts/nous/ledger.py` | Audit-ledger normalization and revalidation status model. |
| `scripts/nous_run.py` | Thin CLI over the above; owns exit codes and JSON output. |
| `scripts/loop_bridge.py` | Kept as a compatible adapter over `backend_local` (see Compatibility). |
| `tests/unit/scripts/test_nous_*.py` | Contract and behavior tests (see Testing). |

### Compatibility: `scripts/loop_bridge.py`

Plan 0 refactors the bridge into an adapter over
`scripts/nous/backend_local.py` with these invariants frozen by the existing
tests plus new ones:

- Module-level `main(argv) -> int` remains the entry point.
- `Path` and `DEFAULT_BRIDGE_DIR` remain module attributes monkeypatchable
  exactly as `tests/unit/scripts/test_loop_bridge.py` patches them today.
- The on-disk format of `claims.json`, `log.jsonl`, and `.lock` is unchanged,
  including the strict `_decode_state` validation and atomic
  tmp-write-then-rename publish.
- `list` stays non-mutating: no directory creation, no lock file, no repair
  of corrupt state.
- Exit codes stay `0` ok / `2` conflict / `1` usage-or-error.
- CLI surface (`claim`, `heartbeat`, `release`, `list`, `check` and all
  current flags, including `--ttl` defaulting to 45 minutes) is unchanged.
  The 3-hour default TTL applies only to the new `scripts/nous_run.py` CLI.

The adapter delegates; it does not duplicate logic. Legacy invocations keep
working unchanged in `local` mode. During Plan 2a, cutover writes a
`.remote-required` sentinel into each configured `$LOOP_BRIDGE_DIR`. With
that sentinel present (or `NOUS_COORD_MODE=remote-required`), direct legacy
mutation commands refuse with exit `1` and direct the caller to
`scripts/nous_run.py`; `list` remains observational and labels its output as
local-only. The combined backend calls `backend_local` directly for its
same-host mutex and does not pass through this legacy safety guard. This
preserves old behavior before cutover without leaving a post-cutover path to
silent local-only claims.

## CLI and backend selection contract

### `scripts/nous_run.py` subcommands

```
nous_run.py preflight  --agent A [--json]
nous_run.py claim      --agent A --branch B --area TEXT [--files a,b]
                       [--candidate-summary TEXT --candidate-source SRC]
nous_run.py bootstrap  --mode remote-required
nous_run.py cutover    --write-sentinel
nous_run.py renew      --run-id R
nous_run.py advance    --run-id R --to STATE --evidence-head SHA [--pr N]
nous_run.py review     --run-id R --head-sha SHA --reviewer NAME
                       --outcome approved|changes-requested|blocked
nous_run.py publish    --run-id R --pr N
nous_run.py verify-merge --run-id R
nous_run.py close      --run-id R --outcome merged|ready-for-human|dry|cancelled
                       [--blocker TEXT] [--parent-run-id R2]
nous_run.py resume     --run-id R [--json]
nous_run.py status     [--run-id R] [--json]
nous_run.py list       [--json]
nous_run.py reconcile  [--json]
nous_run.py gc
```

Every invocation accepts repeatable `--authorize ACTION` values for the
actions explicitly authorized by the current request. Library callers pass
the equivalent `Authorization` value directly. Stored receipts and prior
conversation text are never used as authority. `bootstrap`, `cutover`, and
coordination metadata mutations require `coordinate`; publishing requires
`comment`; code pushes, PR creation, merge, and ruleset changes each require
their corresponding independent action.

Exit codes: `0` ok, `2` conflict or claim refused, `1` usage or internal
error, `3` backend unavailable (transport failure — retryable, distinct from
conflict), `4` validation rejection (schema, secret pattern, ref/path
injection). `2/3/4` are stable machine contracts; loop prompts key off them.

### Backend selection

- Environment:
  - `NOUS_COORD_MODE` — `local` or `remote-required`. Default before cutover:
    `local`. After cutover the operator sets `remote-required` on both
    machines (rollout step), and the remote board header also declares it.
  - `NOUS_COORD_GIT_REMOTE` — Git remote name; default `origin`.
  - `NOUS_GITHUB_REPOSITORY` — `owner/repo`; by default derived from the
    selected Git remote after validating its URL.
  - `NOUS_COORD_BRANCH` — coordination branch name; default
    `nous-coordination`.
  - `LOOP_BRIDGE_DIR` — unchanged; locates the local board.
  - `NOUS_RECEIPT_DIR` — local receipt root; default
    `~/.nous-runs/<repo-basename>/`.
  - `NOUS_MACHINE_ID` — required stable machine identifier; validated
    explicitly and never inferred from a hostname.
- Selection rules:
  - `local` mode: only `backend_local` is used. This is the Plan 0/1 world
    and the rollback world.
  - `remote-required` mode: `backend_git` is authoritative; `backend_local`
    is additionally acquired as the same-host mutex. If the remote backend
    is unreachable, mutating commands fail with exit `3` — they never fall
    back to local-only. `check`/`list`/`status` report both boards and label
    each side.
  - Mode mixing is never silent: on every remote read the client compares
    its `NOUS_COORD_MODE` with the `mode` field in the remote board header.
    A client in `local` mode that can see a `remote-required` board refuses
    mutating operations with exit `4` and an instruction to update its
    configuration. The local cutover sentinel provides the corresponding
    guard for direct legacy-CLI use. This is how a half-migrated updated
    machine is caught instead of double-claiming; rollout still updates both
    machines while loops are stopped because no new tool can constrain an
    old binary that has never received the guard.

### Preflight and mutation authority

`preflight` preserves the authorization boundary in
`docs/engineering/nous-loop.md`. It fetches the current base, verifies a
clean isolated workspace, inventories available gates, detects prior runs
that need the separate reconciliation command, and returns an allowlist of
remote actions authorized by the current
user request: `coordinate`, `push`, `create-pr`, `comment`, `merge`, and
`change-ruleset`. These permissions are independent; authorization to write
coordination metadata is not authorization to publish or merge code. A
missing permission stops before that action with `ready-for-human`, and no
runtime infers permission from an earlier conversation or stored receipt.
Standalone preflight is read-only apart from fetches and reconciliation of
already-existing runs: it creates neither a run ID nor a receipt. `claim`
executes/consumes the same preflight result, then allocates exactly one run
ID and receipt before acquiring the claim; that receipt records the
authorization action names for audit but does not grant future authority.
The receipt records only the action names and decision, never credentials.

## Remote schema (branch `nous-coordination`)

The branch is an orphan: its root commit has no parent and shares no history
with any code branch. During the compatibility window it may contain the
legacy schema-1 tree, which has exactly:

```
claims.json
runs/<run-id>.json
```

Schema 1 must not contain `vercel.json`; a schema-1 tree with that file is a
partial or foreign migration and fails closed. The current schema-2 tree has
exactly:

```
claims.json
vercel.json
runs/<run-id>.json
```

Schema-2 `claims.json` uses top-level `schema` value `2`; migration requires an
empty stored claims array and preserves its mode and empty value semantically,
but canonically reserializes `claims.json`, so its representation, whitespace,
and key order are not byte-preserved. It changes `claims.json.schema` from `1`
to `2` and refreshes `claims.json.updated_at`; every run blob remains
byte-for-byte unchanged. Run projection JSON remains schema 1.
`vercel.json` is repository-owned protocol metadata with these exact bytes
(including the trailing newline):

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "git": {
    "deploymentEnabled": false
  }
}
```

New bootstrap roots and every schema-2 child emit that guard. A valid schema-1
board remains readable during the one-way compatibility window; supported
mutations normalize their child to schema 2, while the explicit migration is
claim-free. The migration requires an empty stored claims array and preserves
its mode and empty value semantically, but canonically reserializes
`claims.json`; its representation, whitespace, and key order are not
byte-preserved. It changes `claims.json.schema` from `1` to `2`, refreshes
`claims.json.updated_at`, and preserves every run blob byte-for-byte. Missing,
modified, oversized, or differently encoded schema-2
guard bytes, unknown files, and schema-1 partial guards are invalid metadata.
Every commit is a full snapshot of the applicable tree, committed as a child
of the fetched tip. Histories are never merged.

### Common validation limits

Enforced by `scripts/nous/schema.py` on both write and read (a peer's data is
untrusted input):

- Unknown fields: rejected. The schema is a whitelist, not a minimum.
- Total size: `claims.json` ≤ 64 KiB; each `runs/<run-id>.json` ≤ 16 KiB.
  Oversized files fail validation (exit `4` on write; treated as corrupt on
  read, reported, never repaired silently).
- Schema-2 `vercel.json` is accepted only when its bytes exactly equal the
  repository-owned guard above; missing, oversized, modified, or differently
  encoded guard data fails closed. The guard is expected protocol metadata,
  not a foreign file.
- Strings: UTF-8, no control characters other than none (no newlines/tabs in
  any field), printable only.
- `run_id`: `^[0-9]{8}T[0-9]{6}Z-[a-z0-9-]{1,32}-[a-f0-9]{6}$`
  (UTC timestamp, agent slug, random suffix). This pattern excludes `/`,
  `.`, `..`, and leading dashes by construction, so
  `runs/<run-id>.json` cannot traverse. The filename must equal the
  embedded `run_id` field.
- `agent`: `^[A-Za-z0-9][A-Za-z0-9@._-]{0,63}$`.
- `machine_id`: `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`; configured explicitly
  and stable per machine, never inferred from an unvalidated hostname.
- `branch`: ≤ 120 chars, must pass `git check-ref-format --branch`, must not
  begin with `-`, must not contain `..`, `@{`, or whitespace.
- `area`: ≤ 200 chars.
- `files`: ≤ 50 entries; each a repo-relative path ≤ 200 chars, no leading
  `/` or `-`, no `..` segment, no backslash.
- Timestamps: ISO-8601 UTC with explicit offset.
- SHAs: exactly 40 lowercase hex.
- `pr`: positive integer ≤ 10^7 or null.
- Secret rejection: any free-text field matching secret-shaped patterns is
  rejected on write and quarantined on read (claim/run treated as invalid,
  operator warning emitted, value never echoed). Structured fields such as
  validated 40-hex SHAs are not subjected to the generic high-entropy-token
  heuristic. Patterns: `AKIA[0-9A-Z]{16}`,
  `ghp_[A-Za-z0-9]{20,}`, `github_pat_`, `gho_`, `sk-[A-Za-z0-9]{20,}`,
  `xox[baprs]-`, `-----BEGIN [A-Z ]*PRIVATE KEY-----`,
  `(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*\S`, and any single
  token of ≥ 40 chars drawn from the base64 alphabet in a free-text field.
- Free-text fields (`area`, `candidate.summary`, `blocker`) never contain raw
  stdout, tracebacks, prompts, user text, or credentials — the writer builds
  them from enumerated vocabulary plus short operator-authored phrases, and
  the secret patterns above are a backstop, not the primary defense.

### `claims.json`

The following is the legacy schema-1 compatibility shape. Schema 2 uses the
same `mode` and `claims` fields, requires its sibling `vercel.json`, and
migration preserves the mode and empty claims-array value semantically while
canonically reserializing `claims.json`; its representation, whitespace, and
key order are not byte-preserved. It changes `claims.json.schema` and
`claims.json.updated_at` and preserves every run blob byte-for-byte.

```json
{
  "schema": 1,
  "mode": "remote-required",
  "updated_at": "2026-08-27T04:00:00+00:00",
  "claims": [
    {
      "claim_id": "c-a1b2c3d4e5f6",
      "run_id": "20260827T040000Z-clawd-linux-9f3a1c",
      "agent": "clawd",
      "machine_id": "linux-box",
      "branch": "fix/streaming-null-guard",
      "area": "agent streaming null finalization",
      "files": ["backend/src/api/agent/streaming.py"],
      "pr": null,
      "status": "active",
      "claimed_at": "2026-08-27T04:00:00+00:00",
      "expires_at": "2026-08-27T07:00:00+00:00",
      "renewed_at": null,
      "candidate": {
        "source": "audit-ledger",
        "summary": "S-M4 stream buffer drops final chunk",
        "lead_ref": "docs/audits/examples/streaming-audit.md#S-M4"
      }
    }
  ]
}
```

Field notes:

- `mode` — the board-declared coordination mode; set at bootstrap
  (`remote-required` once cutover completes). Clients enforce agreement (see
  Backend selection).
- `claim_id` — `c-` + 12 hex; unique within the file.
- `candidate` — the descriptor that lets a peer skip a lead another loop is
  already working: `source` is an enum
  (`trace`, `audit-ledger`, `backlog`, `fresh-hunt`), `summary` ≤ 200 chars,
  `lead_ref` ≤ 200 chars repo-relative path plus optional `#fragment`.
- Overlap semantics are identical to the local board: case-insensitive area
  equality or any shared file path conflicts. An idempotent update requires
  the same `run_id`, `claim_id`, `agent`, `machine_id`, and branch. Reuse of
  only an agent name or branch from another machine is a conflict, not a
  self-update.
- A brand-new claim presents no token. Retrying an active claim presents its
  exact token. Re-acquiring an expired nonterminal run presents the prior
  token stored in its run projection; the winning CAS writes a new token to
  both claim and projection, fencing every stale presentation of the old
  token.
- Only `status: "active"` exists in `claims.json`; released or expired claims
  are removed from the snapshot (history preserves them).

### `runs/<run-id>.json`

The minimal resumable projection. Created at claim time (never speculatively
during preflight), updated at every state change, removed by retention GC 30
days after reaching a terminal state.

```json
{
  "schema": 1,
  "run_id": "20260827T040000Z-clawd-linux-9f3a1c",
  "agent": "clawd",
  "machine_id": "linux-box",
  "claim_id": "c-a1b2c3d4e5f6",
  "state": "reviewed",
  "outcome": null,
  "branch": "fix/streaming-null-guard",
  "area": "agent streaming null finalization",
  "files": ["backend/src/api/agent/streaming.py"],
  "candidate": {
    "source": "audit-ledger",
    "summary": "S-M4 stream buffer drops final chunk",
    "lead_ref": "docs/audits/examples/streaming-audit.md#S-M4"
  },
  "base_sha": "23c3551a30ac5d230f68a01b76650c27144571f5",
  "evidence_head_sha": "8c1d2e3f4a5b6c7d8e9f00112233445566778899",
  "pr": 1601,
  "parent_run_id": null,
  "blocker": null,
  "claim_expires_at": "2026-08-27T07:00:00+00:00",
  "milestones": [
    {"state": "claimed", "at": "2026-08-27T04:00:00+00:00",
     "evidence_head_sha": "23c3551a30ac5d230f68a01b76650c27144571f5"},
    {"state": "reproduced", "at": "2026-08-27T04:20:00+00:00",
     "evidence_head_sha": "23c3551a30ac5d230f68a01b76650c27144571f5", "tier": "local"},
    {"state": "fixed", "at": "2026-08-27T05:00:00+00:00",
     "evidence_head_sha": "8c1d2e3f4a5b6c7d8e9f00112233445566778899", "tier": "local"},
    {"state": "reviewed", "at": "2026-08-27T05:30:00+00:00",
     "evidence_head_sha": "8c1d2e3f4a5b6c7d8e9f00112233445566778899", "tier": "local"}
  ],
  "milestones_truncated": false,
  "updated_at": "2026-08-27T05:30:00+00:00"
}
```

- `milestones` — append-only within the projection (invalidation appends a
  reversal, it does not delete history); ≤ 40 entries. Older entries beyond
  the cap are dropped oldest-first and the separate top-level
  `milestones_truncated` boolean becomes true. The local events.jsonl keeps
  the complete history.
- `tier` — `local` (backed by local receipt evidence, not remotely
  re-verifiable) or `remote` (re-derivable from GitHub/Git: `published`,
  `hosted_verified`, `merged_verified`).
- `blocker` — set only for `ready-for-human`: enum
  (`permission`, `review-unavailable`, `gate-unavailable`, `red-check`,
  `human-decision`, `publication-unavailable`, `other`) plus ≤ 300 chars of
  detail.
- No stdout, tracebacks, diffs, prompts, or credentials, ever.
- `files` and `candidate` preserve the validated conflict inputs required
  for fenced re-claim/resume after the active claim has expired and been
  pruned. They use exactly the same limits as their `claims.json` forms.
- `claim_id` is a fencing token. Every renew, state update, publish binding,
  and terminal operation must present the active claim's exact token. After
  expiry/re-claim, the new token makes every stale process fail with
  `ClaimLost` even if it still has the same run, agent, machine, or branch.
- Reconciliation may terminalize an orphan projection after its claim has
  expired and been pruned. That narrow path requires both the projection's
  last `claim_id` and exact `updated_at`, verifies no active claim for the
  run exists in the freshly fetched snapshot, and may set only a terminal
  outcome. It cannot update active work or delete another claim.

## Local receipt schema

Rooted at `$NOUS_RECEIPT_DIR/<run-id>/`:

- `events.jsonl` — append-only. One JSON object per line:
  `{"seq": N, "ts": ISO, "event": TYPE, "state": STATE|null,
  "evidence_head_sha": SHA|null, "detail": {...}}`. `seq` is strictly
  increasing; the file is opened append-only and never rewritten. `detail`
  may hold local-only evidence pointers (sanitized command argument lists
  with environment values omitted, exit codes,
  gate names, skip lists, review round numbers, local paths). It stays on
  this machine.
- `current.json` — atomic projection (tmp write + rename, same pattern as
  `loop_bridge._write`). Superset of the remote projection plus local-only
  fields: gate inventory from preflight, per-milestone evidence pointers,
  review round count, and the last rendered summary.

Local receipt mutation takes a per-run file lock. An event is appended with
`O_APPEND`, flushed, and `fsync`ed before `current.json` is replaced and its
directory is `fsync`ed. A malformed non-final event fails closed. A torn
final line after a crash is reported and excluded from projection rebuild;
it is preserved for diagnosis rather than silently rewritten.

The receipt is the write-ahead source for the remote projection: every state
change appends the event locally first, then pushes the remote update. If the
remote push fails after the local append, the run is in `remote-lagging`
condition; every subsequent command retries the remote sync before doing new
work, and `reconcile` repairs it.

## Run state machine

### States

`started → claimed → reproduced → fixed → reviewed → locally_verified →
published → hosted_verified → merged_verified`

plus terminal outcomes `merged`, `ready-for-human`, `dry`, `cancelled`.
An explicit user cancellation is outside the three completed-tick outcomes,
matching docs/engineering/nous-loop.md.

### Transition table

| From | To | Guard |
| --- | --- | --- |
| (none) | `started` | The `claim` command's local preflight passed and its single receipt was created. Standalone `preflight` creates no receipt. Local-only — no remote run exists yet. |
| `started` | `claimed` | Remote claim acquired, then local mutex acquired (see Authority). Remote `runs/<run-id>.json` created in the same metadata commit as the claim. |
| `claimed` | `reproduced` | Reproduction evidence event recorded; binds `base_sha` and current `evidence_head_sha`. |
| `reproduced` | `fixed` | Causal red/green proof recorded at head `H` (`evidence_head_sha = H`). |
| `fixed` | `reviewed` | Independent review recorded; review head must equal current `evidence_head_sha`. |
| `reviewed` | `locally_verified` | Local gates (`scripts/ci/run_local_ci.sh --base origin/develop`) recorded at the same head, including the skip list verbatim. |
| `locally_verified` | `published` | PR exists with head `H`; `pr` bound on claim and run. |
| `published` | `hosted_verified` | Hosted required checks green at `H`; PR head still `H`; review comments addressed. |
| `hosted_verified` | `merged_verified` | Merge verification passed (see GitHub reconciliation). |
| `merged_verified` | outcome `merged` | `close --outcome merged`: outcome + claim release in one metadata commit; `state` remains `merged_verified`. |
| `started`, `claimed` | outcome `dry` | No safe verified candidate; claim (if any) released in the same commit; `state` remains the last stage. |
| any non-terminal | outcome `ready-for-human` | Blocker recorded. Claim **retained**, expiry reported (see below); `state` remains the blocked stage. |
| any non-terminal | outcome `cancelled` | Explicit user cancellation; claim released in the same commit; `state` remains the last stage. |

### Invalidation (repeat loop)

Milestone validity is bound to `evidence_head_sha`:

- If the branch head moves from `H` to `H'` (new commit, amend, rebase),
  every milestone in `{reviewed, locally_verified, hosted_verified}` bound to
  `H` is invalidated: the receipt appends an `invalidated` event naming the
  affected milestones and both SHAs, and the state regresses to `fixed` at
  `H'`. `published` and the `pr` binding survive (the PR still exists);
  hosted facts must be re-earned at `H'`.
- The repeat loop is therefore
  `fixed → reviewed → locally_verified → (publish update) → hosted_verified`,
  exactly the "whenever the published diff changes" rule in
  docs/engineering/nous-loop.md, now machine-enforced: `advance --to
  hosted_verified` and `verify-merge` fail with exit `4` if the recorded
  reviewed head differs from the current PR head.
- `hosted_verified` is never accepted as caller-authored evidence. The
  Plan 2b service rereads the PR head, selects the latest GitHub check run
  named `Release Gate` (the repository contract frozen against
  `test-pipeline.yml`), requires completed/success, and requires zero
  unresolved review threads. Missing, pending, red, or stale-head evidence
  fails closed without advancing the receipt.
- `reproduced` binds `base_sha` and is not auto-invalidated by head motion on
  the fix branch. A base advance (`origin/develop` moved) does not invalidate
  milestones by itself; preflight-on-resume re-checks that the symptom is
  still present at the new base before continuing.

### Terminal shapes

- **merged** — only from `merged_verified`. One metadata commit sets
  `state: "merged_verified"`, `outcome: "merged"`, and removes the claim.
- **ready-for-human** — the only terminal outcome that retains the claim,
  because the protected work must stay protected until a human acts. The
  close report states the claim's `expires_at`; the claim then ages out by
  normal TTL expiry if nobody follows up. A human either continues on the
  owning machine before expiry, explicitly releases the retained claim, or
  waits for expiry; a follow-up run on either machine then links
  `parent_run_id` and performs a fresh overlap-checked claim. There is no
  implicit cross-machine claim transfer.
- **dry** — from `started`/`claimed` only; claim released; the close event
  records which candidate sources were checked.
- **cancelled** — any non-terminal state; claim released with reason
  recorded; never reported as one of the three completed-tick outcomes.

### Cross-machine resume

`nous_run.py resume --run-id R` on any machine:

1. Fetch the coordination branch; load `runs/R.json` (validated as untrusted
   input).
2. Recompute the trustworthy state floor from remotely re-verifiable facts
   only:
   - active nonterminal claim still live → `claimed`;
   - `ready-for-human` outcome → retain that terminal outcome for display
     and require an explicit human release/follow-up decision; do not reopen
     it as `claimed` automatically;
   - branch exists at the recorded `evidence_head_sha` (fetched) → retain
     only the branch/head binding, not a `fixed` milestone;
   - PR exists (re-read from GitHub, head compared to record) → retain the
     PR binding as a remotely verified fact, not the linear `published`
     milestone until local proof has been re-earned;
   - merged facts → re-derive fully via merge verification.
3. Every `tier: "local"` milestone (reproduced, fixed causal proof, reviewed,
   locally_verified) is demoted (an `invalidated` event is appended; history
   is kept) when the machine lacks the run's local receipt. The resumed
   working state is therefore `claimed` whenever the live claim survives.
   Branch, head, and PR bindings survive as metadata, but the resuming
   machine must rerun reproduction and causal red/green proof before it may
   re-enter `fixed`, then rerun review and local verification. If a PR
   already exists, the later `published` transition reuses that binding and
   updates the marker comment rather than creating another PR. The previous
   machine's word is never trusted for local-tier proofs.
4. The resuming machine starts a fresh local receipt for R, records a
   `resumed` event naming which milestones were demoted, and continues.

## Git bootstrap and CAS protocol

### Bootstrap

1. Build a new schema-2 initial state locally with plumbing (no working-tree
   checkout): `git hash-object -w` the initial `claims.json`
   (`{"schema": 2, "mode": ..., "claims": [], "updated_at": ...}`) and the
   exact repository-owned `vercel.json` guard, then `git mktree` and
   `git commit-tree` with **no parent** (orphan root) and the dedicated
   committer identity. An existing schema-1 board is not re-bootstrapped; the
   explicit migration in Rollout is the only claim-free, run-byte-preserving
   upgrade, while ordinary mutations may normalize a valid schema-1 child to
   schema 2.
2. Atomically publish the object and create the absent ref with one normal
   Git push: `git push --porcelain origin
   <root>:refs/heads/nous-coordination`. A REST `createRef` call is not used:
   it can only name an object GitHub already has, which would require a
   separate temporary-ref upload and add another race. Git receive-pack
   creates the absent ref and receives its objects in one transaction.
3. On a bootstrap race (push rejected because the ref now exists): leave
   the losing local commit unreachable (ordinary local Git GC may later
   remove the object), fetch the winning state, and re-derive
   the intended logical operation (e.g. "claim area X") against it. Never
   merge coordination histories, never retry the same commit object.

### Normal update (CAS)

1. Shallow-fetch the branch tip into a unique per-invocation temporary ref:
   `git fetch --depth 1 origin
   +refs/heads/nous-coordination:refs/nous/tmp/<run-id>-<nonce>`. The unique
   ref prevents two same-host processes from racing on a shared tracking ref
   before either has acquired the local claim. The `+` here only permits
   that private local tracking ref to move; it is not a force-push and
   touches nothing remote. Delete the temporary local ref in `finally`.
2. Read and validate the applicable tree from the fetched commit
   (`git cat-file`): schema 1 permits only `claims.json` and `runs/*.json`;
   schema 2 additionally requires the exact repository-owned `vercel.json`
   guard. All content is untrusted input, except that the schema-2 guard is
   expected protocol metadata rather than foreign metadata.
3. Re-derive the operation against that state (conflict check, expiry check,
   lost-claim check). Every operation on an existing run compares its
   presented `claim_id` fencing token to the live claim before constructing
   a commit; mismatch is `ClaimLost`, never a retry of stale intent.
4. Build the full-snapshot child commit with plumbing: new blobs, `mktree`,
   `commit-tree -p refs/nous/tmp/<run-id>-<nonce>`, committer identity
   `NOUS Coordination <nous-coordination@invalid>` set via
   `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env for that call only.
   `runs/<run-id>.json` is built as a real nested tree: validated path
   segments are assembled bottom-up with one `mktree` call per directory;
   slash-containing entries are never passed directly to `mktree`. Every
   changed snapshot is normalized to schema 2 and re-emits the exact guard;
   no supported writer creates a schema-1 child.
5. Push fast-forward: `git push origin <new>:refs/heads/nous-coordination` —
   a plain push, fast-forward by construction because the parent is the
   fetched tip. **No force flag exists anywhere in routine tooling.**
6. On non-fast-forward rejection (a peer won the race): refetch, recheck,
   re-derive, retry — at most 5 attempts, with jitter: sleep
   `min(30, 1.5^attempt) * uniform(0.5, 1.5)` seconds between attempts.
   Exhaustion returns exit `2` if the re-derived operation now conflicts, or
   exit `3` if pushes keep being rejected without a visible conflict.
7. Transport errors (DNS, TLS, 5xx, timeout) are a distinct class: retried on
   the same schedule, then exit `3`. They are never reported as conflicts and
   never trigger local-only fallback in `remote-required` mode.

### Expiry, renewal, skew

- Default claim TTL: 3 hours (`nous_run.py` only; the legacy bridge default
  stays 45 minutes).
- Renewal policy: renew only when remaining time < 45 minutes, and
  additionally immediately before entering a long gate (local CI, review
  round, hosted-check wait). Each renewal is a CAS update setting
  `expires_at = now + TTL`, `renewed_at = now`.
- Expiry is final: renewal of an already-expired claim is refused (same
  principle the local bridge enforces for `heartbeat`); the holder must
  re-claim, which re-runs the conflict check.
- Peer pruning: any client performing a CAS update may prune claims whose
  `expires_at` is more than the skew allowance in the past. Skew allowance:
  120 seconds. A claim is prunable only when `now - 120s > expires_at`.
- Holder self-check: before **every** edit/commit/push boundary, the holder
  fetches the board and verifies its claim is still present and unexpired.
  A pruned or missing claim raises `ClaimLost`: the holder must stop
  mutating, and either re-claim (if the area is still free) or close
  `ready-for-human` describing the loss. Work is never pushed under a lost
  claim.

## Authority and synchronization

- After cutover, the remote board is authoritative for claims. The local
  flock board remains a same-host mutex (two loops on one machine still
  exclude each other even when the network is down mid-run).
- Acquisition order: **remote first, then local.**
  1. CAS-claim on the remote board (creates the run projection in the same
     commit).
  2. Acquire the local claim via `backend_local` (same area/files/branch).
  3. If local acquisition fails (a same-host loop holds overlapping work),
     compensate: CAS-release the remote claim and remove the just-created
     run projection, then return exit `2`. If the compensating release push
     fails (transport), record a `compensation-pending` event in the local
     receipt and retry the release before any other remote operation; until
     it succeeds the client reports the claim as "held-in-error" rather than
     pretending it is free.
- Release order is the reverse: local release, then remote (terminal
  metadata commit). A failed remote release is retried by `reconcile`.
- `check` consults both boards and reports which side produced any conflict.
- Renewal touches only the remote board; the local claim is heartbeated by
  the same command in the same invocation.

## GitHub reconciliation and merge verification

`nous_run.py reconcile` (also run inside `preflight`) compares coordination
state with GitHub reality and repairs divergence with ordinary CAS commits:

- Claim with `pr` whose PR is merged → run merge verification; on success,
  finalize the run (`merged_verified` + outcome + release) in one commit.
- Claim with `pr` whose PR is closed unmerged → mark the run
  `ready-for-human` with blocker `human-decision` (a human closed it or it
  needs a decision); retain per terminal-shape rules.
- Expired claims → prune (skew rule).
- `remote-lagging` local receipts → push their pending projection updates.
- Run projections whose branch no longer exists and whose PR is absent →
  close `cancelled` with reason recorded.

### Merge verification (merge, squash, rebase)

`verify-merge` passes only when all three hold:

1. GitHub reports the PR merged (`merged == true`,
   `merge_commit_sha` present) via `gh api repos/{owner}/{repo}/pulls/{n}`.
2. `merge_commit_sha` is an ancestor of freshly fetched
   `<NOUS_COORD_GIT_REMOTE>/develop` (`git fetch <configured-remote> develop` then
   `git merge-base --is-ancestor <sha> FETCH_HEAD`). This holds for merge
   commits, squash commits, and the final commit of a rebase-merge alike.
3. The PR head at merge equals the last independently reviewed head:
   `head.sha` from the PR record (frozen at merge when the branch is
   deleted; re-read defensively while it exists) must equal the
   `evidence_head_sha` of the newest valid `reviewed` milestone. This is the
   squash/rebase late-push detector: a commit pushed after review but before
   merge changes `head.sha`, milestone invalidation has already regressed
   the run to `fixed`, and check 3 fails. A failed check 3 yields
   `ready-for-human` (blocker `human-decision`) — the merge happened, but
   the merged content was not the reviewed content, and only a human decides
   what follows. It is never reported as `merged`.

Only after `verify-merge` passes may `close --outcome merged` run; it
verifies once more, then writes the single terminal commit.

## Publication: PR comment

- One comment per run, keyed by an idempotent hidden marker as its first
  line: `<!-- nous-run: <run-id> -->`. Publication lists PR comments, finds
  the marker, and edits in place only when the comment author is the current
  authenticated GitHub actor. A matching marker from another author, or
  multiple matching comments, fails closed for human reconciliation;
  absent, it creates. Re-publication after a repeat loop overwrites the same
  owned comment — never a second one.
- The body is rendered by `summary.py` **only** from receipt events: states
  reached with SHAs, gates run and their recorded results, the verbatim skip
  list, review rounds, and the current `evidence_head_sha`. No free-text
  success prose. The receipt cannot say what did not happen, so neither can
  the comment.
- Injection and trigger safety:
  - All PR/review/comment text read from GitHub is untrusted data. It is
    never interpolated into shell commands, never executed, and never
    treated as instructions to the agent; `reconcile`/`publish` parse it
    only for structural facts (marker presence, head SHAs, check states).
  - Generated bodies must not trigger repository slash-command workflows.
    Concretely for this repo: `.github/workflows/opencode.yml` fires on
    comments containing ` /oc`/` /opencode` (or starting with them) from
    OWNER/MEMBER/COLLABORATOR authors — and loop-authored comments carry an
    authorized association. The renderer therefore rejects any body where
    a line starts with `/`, or where the substrings `/oc` or `/opencode`
    appear anywhere; validation failure blocks publication (exit `4`)
    rather than posting a mutilated body. The guard is table-driven so new
    trigger phrases are one-line additions, and a contract test freezes the
    current set against the workflow files.

## Security and threat model

Assets: repository integrity, the user's GitHub credentials, coordination
correctness (no double-claims, no fake merges), and secrecy of local
evidence.

| Threat | Mitigation |
| --- | --- |
| Malicious or corrupted remote metadata (a compromised peer, a bad manual edit) | All remote content validated as untrusted input against the whitelist schema; unknown fields, oversize files, bad charsets, and secret-shaped values reject the file; remote strings never reach a shell. |
| Run-id / path traversal (`../`, absolute paths, ref names starting with `-`) | `run_id` pattern excludes traversal by construction; filename must equal embedded id; `branch` must pass `git check-ref-format` and the leading-dash ban; `files` entries repo-relative with `..` banned. |
| Command injection via metadata | `gitio.py` is the only subprocess site; argument lists only, `shell=True` banned, remote-derived values passed solely as validated arguments after `--` where git supports it. |
| Secrets leaking into the remote board | Secret-pattern rejection on write; no raw stdout/tracebacks/prompts/user text in any remote field; detailed evidence never leaves `$NOUS_RECEIPT_DIR`. |
| Force-push destroying coordination history | No force flags in routine tools; runtime guard in `gitio.py` rejects any `git push` invocation containing `--force`, `--force-with-lease`, `-f`, `--delete`, or a refspec beginning with `+` (the `+` fetch refspec in the CAS protocol only moves a local tracking ref and is explicitly exempt); a static contract test greps `scripts/nous/` for the same; the branch ruleset blocks force-push and deletion server-side (operator prerequisite). |
| Coordination branch triggering CI/deploy workflows or Vercel | GitHub workflow evidence remains frozen by the contract scanner: no push/pull_request trigger may match `nous-coordination` (the surveyed `test-pipeline.yml`, `secret-scan.yml`, `workflow-lint.yml`, `helm-validate.yml`, and `trigger-deploy.yml` are limited to product refs or path-gated). For Vercel, every schema-2 snapshot carries the repository-owned exact `vercel.json` guard with `git.deploymentEnabled: false`; schema 1 is only the read-compatible pre-migration state. Observe the migration SHA for 120 seconds; any Vercel deployment of any state fails acceptance, stops rollout, and triggers the dedicated-repository fallback. The coordination identity is not added to Vercel. |
| Prompt injection through PR/review text | Treated as data only (see Publication); the loop never derives instructions from it. |
| Slash-command self-trigger | Renderer bans trigger phrases; contract test pins the ban list to the workflows (see Publication). |
| Unexpected manual metadata commits confusing audit | Dedicated committer identity `NOUS Coordination <nous-coordination@invalid>` identifies tool-produced commits but is not an authentication mechanism. Files outside the valid schema-1 tree or schema-2 tree (where the exact `vercel.json` guard is expected protocol metadata) are flagged as foreign and reported, never auto-repaired; GitHub authentication and the branch ruleset control who may push. |
| Clock skew causing false expiry/pruning | 120-second skew allowance on pruning; expiry never resurrectable; holder re-verifies its claim at every mutation boundary. |

Operator prerequisite (rollout step, not code): a GitHub ruleset on
`nous-coordination` blocking force-pushes and deletion while allowing normal
fast-forward pushes from authorized users.

## Audit-ledger semantics

`scripts/nous/ledger.py` normalizes ledgers (docs/audits/ format per
docs/audits/AUDIT_LEDGER_TEMPLATE.md, plus external ledger directories passed
by path) into one status model:

- **Every entry is a lead until revalidated against an exact SHA.** A
  finding's status is `confirmed@<sha>` only when fresh evidence at that
  pinned SHA exists (a `launch_audit.sh`-isolated worktree at that SHA, or a
  reproduction recorded in a run receipt bound to it).
- Legacy normalization: a ledger whose provenance block is missing, or whose
  `sha:` is absent/`UNRECORDED`, demotes **every** entry in it to
  `lead (unverified)`. Provenance is never invented after the fact — the
  same rule the template already states for retrofits.
- Statuses recorded per entry: `lead`, `confirmed@<sha>`, `refuted@<sha>`,
  `fixed@<pr>` (with the merge-verified PR), `stale` (confirmed at a SHA no
  longer an ancestor of `origin/develop`). "Current" status for candidate
  selection means `confirmed@<sha>` where `<sha>` is the fetched
  `origin/develop` tip or an ancestor examined this tick.
- The candidate-selection path of the loop consumes only this normalized
  view, which is what makes "existing audit findings are leads until
  revalidated" operational instead of aspirational.

## Retention and maintenance

- Coordination state is always fetched shallowly (`--depth 1`); no client
  needs branch history for correctness.
- Retention GC (`nous_run.py gc`, also run opportunistically at the end of
  `reconcile`): remove `runs/<run-id>.json` whose terminal milestone is
  older than 30 days, via an ordinary full-snapshot CAS commit. Git history
  remains the audit trail; nothing is ever rewritten.
- Routine operations never force-push (guarded three ways; see threat
  model).
- Compaction (rewriting the branch to shrink history) is rare,
  human-authorized maintenance only, outside automated v1: both loops
  stopped, an archived snapshot (bundle or tag) taken first, then a human
  performs it with the ruleset temporarily adjusted. No tool in
  `scripts/nous/` implements it.

## Rollout and rollback

Rollout is a maintenance window, in order. No real tick runs and neither
machine writes its `.remote-required` sentinel until migration, its
post-migration observation, and the two-machine smoke test are complete:

1. Stop the loop on both machines (Linux box and Mac); confirm no tick is
   mid-flight.
2. Reconcile and release all local claims on both machines
   (`loop_bridge.py list` → release or let expire).
3. Merge the implementation and record the exact merged `develop` SHA. Update
   both clients to that same SHA and run the focused coordination tests on
   both machines; do not proceed if either result differs or fails.
4. If `nous-coordination` is absent, bootstrap a schema-2 root with the exact
   guard. If it already exists, verify either a valid legacy schema-1 tree with
   no guard and no unknown files, or a valid schema-2 tree with the exact
   guard; in both cases require an empty stored claims array. Reject and stop
   rollout on any unknown file or missing, modified, or partial guard. Keep the
   sentinel absent.
5. From one stopped machine, run the explicit, authorized, claim-free
   migration once:

   ```bash
   python3 scripts/nous_run.py migrate --to-schema 2 --authorize coordinate
   ```

   Migration creates no claim, receipt, local mutex, sentinel, or tick. It
   preserves the mode and empty claims-array value semantically, canonically
   reserializes `claims.json` (its representation, whitespace, and key order
   are not byte-preserved), changes `claims.json.schema` and
   `claims.json.updated_at`, and preserves every run projection blob
   byte-for-byte. It creates one ordinary fast-forward child; an already valid,
   claim-free schema-2 board is an idempotent no-op.
6. Verify the migration SHA has the exact schema-2 tree, the semantically empty
   claims array and mode, canonical `claims.json` serialization with its
   expected schema transition and refreshed `updated_at`, every run projection
   blob byte-for-byte unchanged, and the exact guard. Do not compare the
   original claims-document bytes: its whitespace and key order may change.
   Observe GitHub deployments and commit statuses for 120 seconds on that SHA.
   Any Vercel deployment of any state fails acceptance, stops rollout, and
   triggers the dedicated-repository fallback. Do not remove the guard or
   rewrite the coordination history.
7. Configure the Git backend on both machines (`NOUS_COORD_MODE`,
   `NOUS_COORD_GIT_REMOTE`, `NOUS_GITHUB_REPOSITORY`,
   `NOUS_COORD_BRANCH`, receipt dir), keeping the local `.remote-required`
   sentinel absent. Apply the branch ruleset (block force-push/deletion, allow
   fast-forward) — operator action in GitHub settings. Perform the two-machine
   smoke test:
   machine A claims; machine B observes the claim and gets exit `2` on an
   overlapping `check`/`claim`; B claims a disjoint area; A observes it; both
   release; both `reconcile` clean. Only after every observation and smoke
   gate passes, run `cutover --write-sentinel --authorize coordinate` on both
   machines, then restart loops for normal remote-required operation.

Before migration, rollback is simply leaving the schema-1 board unchanged; no
remote write is needed. After migration, never downgrade, remove the guard, or
rewrite history: stop rollout, leave the schema-2 branch inert, and use the
dedicated-repository fallback if Vercel deployed the migration SHA. After
cutover, rollback still requires stopping both machines, releasing or expiring
remote claims while both clients remain in `remote-required`, setting
`NOUS_COORD_MODE=local` on both, and running
`python3 scripts/nous_run.py rollback --remove-sentinel --authorize coordinate`
on each before resuming the legacy local-board workflow. The coordination
branch remains in place and inert; nothing else depends on it.

## Testing and acceptance criteria

All tests are pure stdlib + pytest, live under `tests/unit/scripts/`, run in
the same blocking gate as the existing bridge/contract tests
(`scripts/ci/run_local_ci.sh` and the hosted `test-pipeline.yml` step that
runs `pytest tests/unit/scripts/`), and never touch the network or real
GitHub:

- **Harness.** Temporary bare repository as `origin`; two temporary clones
  simulating the two machines; `gh` replaced by an injectable runner in
  `gitio.py` returning recorded responses. No real GitHub mutation anywhere.
- **Versioned tree and guard.** Schema-1 snapshots without `vercel.json`
  remain readable; schema-1 snapshots with a partial guard are rejected;
  schema-2 bootstrap and CAS snapshots contain exactly `claims.json`, the
  validated `runs/<run-id>.json` projections, and byte-exact repository-owned
  `vercel.json` with `git.deploymentEnabled: false`; missing, modified,
  oversized, differently encoded, or unknown files fail closed. Schema-2
  writes never remove the guard, and old schema-1 clients stop safely on
  schema 2.
- **Claim-free migration.** `migrate --to-schema 2 --authorize coordinate`
  requires an empty stored claims array, preserves its mode and empty value
  semantically, canonically reserializes `claims.json`, changes
  `claims.json.schema` and `claims.json.updated_at`, preserves every run
  projection blob byte-for-byte, creates one ordinary fast-forward child, and
  is idempotent on a valid, claim-free schema-2 board. Before it
  runs, both machines must use the same exact merged SHA and pass the focused
  coordination tests at that SHA. Migration creates no claim, sentinel, or
  tick.
- **CAS and bootstrap races.** Both clones bootstrap concurrently → exactly
  one root wins, loser re-derives; interleaved claim CAS updates → loser
  refetches and either succeeds or exits `2`; 5-attempt exhaustion paths for
  both conflict and transport classes.
- **Conflicts.** Area/file overlap across machines; idempotent same-agent
  re-claim; candidate descriptor visible to the peer.
- **Expiry and loss.** TTL expiry; renewal-below-45-minutes policy; refusal
  to renew an expired claim; peer pruning honoring the 120s skew; holder
  `ClaimLost` detection at the edit/commit/push boundary.
- **Partial failure.** Remote-claim-then-local-fail compensation; failed
  compensation retry (`compensation-pending`); `remote-lagging` receipt
  sync; release retried by `reconcile`.
- **State machine.** Every legal transition; every illegal transition
  rejected with exit `4`; head-drift invalidation regressing to `fixed`;
  repeat loop; terminal shapes including `ready-for-human` claim retention
  and `parent_run_id` linkage; resume demoting `tier: local` milestones.
- **Merge verification.** Merge, squash, and rebase merges pass; squash
  late-head (push after review) fails check 3 and yields `ready-for-human`;
  non-ancestor `merge_commit_sha` fails check 2.
- **Security validation.** Run-id traversal, leading-dash refs, unknown and
  oversized fields, secret-shaped values, control characters; `gitio` force
  guard (runtime and static grep); slash-command ban in rendered bodies.
- **Legacy ledgers.** Missing/`UNRECORDED` provenance demotes every entry;
  no invented SHAs; `stale` classification.
- **Compatibility.** The existing `test_loop_bridge.py` suite passes
  unchanged against the Plan 0 adapter (same monkeypatch points, file
  format, exit codes, non-mutating `list`).
- **Workflow guard.** A conservative pure-stdlib contract scanner over
  `.github/workflows/*.yml` proves no push/PR trigger matches
  `nous-coordination` and that the publication ban list covers every
  comment-trigger phrase. It fails closed on trigger syntax it cannot
  classify; it does not depend on PyYAML in the isolated script-test gate.
- **Pure stdlib.** A test imports every module under `scripts/nous/` in a
  process with third-party site-packages hidden and asserts success.
- **External deployment observation.** After migration, observe GitHub
  deployments and commit statuses for 120 seconds on the migration SHA. No
  Vercel deployment of any state is accepted; one stops rollout and selects
  the dedicated-repository fallback. The local sentinel remains absent until
  this observation and the two-machine smoke test pass.

Acceptance for the program as a whole: the two-machine smoke test in the
rollout section passes; a full simulated tick (claim → … → merged) leaves a
correct remote history of full-snapshot commits, a complete local receipt,
one marker comment, and a released claim in a single terminal commit. The
schema-2 migration must first satisfy the claim-free, same-merged-SHA,
exact-guard checks and the 120-second Vercel observation; any deployment on
the migration SHA fails acceptance and leaves the branch inert for the
dedicated-repository fallback.

## Phased delivery

| Plan | Scope | Depends on |
| --- | --- | --- |
| Plan 0 | Compatibility-preserving coordination abstraction: `schema.py`, `coordination.py`, `backend_local.py`, `loop_bridge.py` adapter. Zero behavior change; legacy tests pass unchanged. | — |
| Plan 1 | Receipt state machine and freshness preflight: `receipt.py`, `preflight.py`, `summary.py` (terminal reports), `nous_run.py` skeleton (local mode). | Plan 0 |
| Plan 2a | Git coordination: `gitio.py`, `backend_git.py`, bootstrap/CAS/expiry/pruning, cutover mode enforcement, retention GC, canonical docs update (docs/engineering/nous-loop.md coordination section) and contract tests (workflow freeze, no-force guard). | Plan 1 |
| Plan 2b | GitHub publication and reconciliation: `publish.py`, `reconcile.py`, merge verification, marker comment, resume. | Plan 2a |
| Plan 3 | Audit-ledger normalization and revalidation: `ledger.py`, candidate-selection integration. | Plan 1; runs parallel to Plan 2b |
| Plan 4 | Operational polish: bounded review rounds recorded in the receipt, soft PR-size policy surfaced in preflight, retention automation defaults, richer generated summaries. | Plan 2b |

The rollout maintenance window sits after Plan 2b lands (Plans 3 and 4 do
not gate cutover).

## Settled decisions

- Git/GitHub is the cross-machine transport; no external service, queue, or
  database is introduced.
- The coordination branch is an orphan named `nous-coordination`; metadata is
  full-snapshot child commits; histories are never merged and never
  force-pushed by tooling. Schema 1 is the legacy, read-compatible
  claims/runs-only tree; schema 2 is the current write format and includes
  the exact repository-owned `vercel.json` guard. That guard is protocol
  metadata, not foreign metadata.
- New bootstrap and every supported CAS write use schema 2. The explicit
  `python3 scripts/nous_run.py migrate --to-schema 2 --authorize coordinate`
  operation is the only claim-free, run-byte-preserving schema-1 upgrade; it
  requires an empty stored claims array, preserves its mode and empty value
  semantically, canonically reserializes `claims.json`, changes
  `claims.json.schema` and `claims.json.updated_at`, preserves every run
  projection blob byte-for-byte, and is idempotent on schema 2.
  Supported ordinary mutations also normalize a valid schema-1 snapshot's
  resulting child to schema 2, but may change claim/run state. Both machines
  must pass focused tests at the same exact merged SHA before migration.
- Observe GitHub deployments and commit statuses for 120 seconds after
  migration. Any Vercel deployment on the migration SHA, regardless of state,
  stops rollout and selects the dedicated-repository fallback; the schema-2
  branch is left inert and its guard is never removed or bypassed.
- Remote board authoritative after cutover; local flock board demoted to
  same-host mutex; no silent mode mixing (board-declared mode, client
  refusal on mismatch).
- Claim TTL 3 hours with renew-under-45-minutes policy and 120-second skew
  allowance; expiry is final.
- Remote run projections are created at claim time, never at preflight.
- `ready-for-human` retains the claim; `merged`/`dry`/`cancelled` release it
  in the same terminal metadata commit.
- Cross-machine resume trusts only remotely re-verifiable facts; local-tier
  milestones are always demoted and re-earned.
- Merge verification requires all three checks (merged flag, ancestry,
  head-at-merge equals last reviewed head) for every merge method.
- Publication is one marker comment per run, rendered only from receipt
  events, with slash-command and injection guards enforced by validation and
  contract tests.
- Ledger entries without an exact confirmed SHA are unverified leads;
  provenance is never invented.
- `scripts/loop_bridge.py` keeps its CLI, file format, monkeypatch points,
  and exit codes in local mode through Plan 0 and beyond; its 45-minute
  default TTL is unchanged. After cutover, the environment/sentinel guard
  refuses direct legacy mutations so they cannot bypass the remote board.
- All new code is pure Python standard library; `git`/`gh` only via the
  guarded `gitio.py` subprocess wrapper.

## Status update 2026-10-17

The original status ("proposed design; awaiting owner review") is kept as written. Checked on 2026-10-17: the Git-backed cross-machine claims described here shipped as #1580 (`feat(nous): add Git-backed cross-machine claims`), and the Vercel deployment exclusion that made schema 2 necessary shipped as #1581. See the [Vercel deployment exclusion design](2026-08-29-nous-vercel-deployment-exclusion-design.md).

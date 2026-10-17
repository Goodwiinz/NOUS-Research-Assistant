# NOUS coordination Vercel deployment exclusion — design

Date: 2026-08-29
Status: proposed; awaiting owner review before implementation planning

## Problem

The Git-backed NOUS board is an orphan branch in the same private GitHub
repository as the Vercel application. Vercel observes pushes to every branch.
The initial coordination root commit
`cb34bcfd343acb96fcab6ff97ecfa950be83f1ba` therefore created GitHub
deployment `6157005937` for the existing `md-basit/nous` Vercel project.
Vercel blocked that preview because the commit has the required dedicated
identity `NOUS Coordination <nous-coordination@invalid>`, which is not and
must not become a Vercel team member.

The failed deployment did not affect production or corrupt the coordination
board, but every future board update would repeat the failed deployment,
failed commit status, and notification unless the coordination ref opts out
before Vercel creates a deployment.

Vercel documents two relevant behaviors:

- commits from private repositories deploy only when Vercel can associate the
  commit author with a project member;
- `git.deploymentEnabled` in `vercel.json` prevents automatic deployments for
  the project or selected branches.

The existing schema rejects every root file except `claims.json` and
`runs/<run-id>.json`. A Vercel guard therefore requires a deliberate protocol
revision rather than an unvalidated manual file on the board.

## Goals

- Prevent Vercel from creating automatic deployments for every future
  `nous-coordination` commit.
- Preserve the exact dedicated author and committer identity.
- Keep the coordination branch orphaned, append-only, and updated only by
  ordinary fast-forward pushes.
- Validate the deployment guard as strictly as the coordination data.
- Support a fail-closed, one-way migration from the existing schema-1 root.
- Preserve the mode and empty claims-array value semantically during migration;
  canonically reserialize `claims.json` as schema 2, changing its
  representation as needed, while preserving every existing run blob
  byte-for-byte.
- Verify the real Vercel/GitHub side effect before any claim or NOUS tick.

## Non-goals

- Adding `nous-coordination@invalid` or another automation identity to Vercel.
- Weakening Vercel's private-repository author checks.
- Changing the commit identity accepted by the NOUS backend.
- Running a real NOUS tick, writing the cutover sentinel, or conducting the
  two-machine claim smoke test.
- Moving the board to a separate repository in this change.
- Disabling previews for ordinary product branches.

## Considered approaches

### 1. Versioned immutable Vercel guard in every snapshot — selected

Add an exact root `vercel.json` to schema-2 snapshots. The file disables all
automatic Git deployments for the source snapshot. The backend emits it on
every commit and accepts it only when its bytes match the repository-owned
constant. This keeps the identity and Git CAS model intact while making the
deployment exclusion travel with the orphan branch that needs it.

Trade-off: this introduces a narrow Vercel dependency into the remote tree
format and requires both machines to upgrade before the board migrates.

### 2. Move coordination to a dedicated repository

A separate private repository not connected to Vercel provides the cleanest
infrastructure boundary. It also requires another repository, new GitHub App
scope, another ruleset, new remote configuration on both machines, and a board
migration. Retain this as the fallback if Vercel does not honor the in-branch
guard before its author-membership check.

### 3. Use a Vercel-linked commit identity — rejected

Mapping coordination commits to a human or paid bot member would stop the
membership error but cause Vercel to deploy coordination JSON as application
source. It would also weaken the existing foreign-metadata invariant.

### 4. Use an Ignored Build Step — rejected

An ignored build command runs after Vercel has created a deployment and needs
source files that the orphan branch does not contain. It does not address the
observed author gate or prevent the failed deployment record and notification.

## Remote tree protocol

### Schema 1 (legacy, read-compatible)

Schema 1 remains readable during the maintenance window and has exactly:

```text
claims.json
runs/<run-id>.json
```

It must not contain `vercel.json`. A schema-1 tree containing the guard is a
partial or foreign migration and is rejected.

### Schema 2 (current, write-only target)

Schema 2 has exactly:

```text
claims.json
vercel.json
runs/<run-id>.json
```

Migration requires the stored claims array to be empty. It preserves the
`claims.json` mode and that empty claims-array value semantically, but
canonically reserializes `claims.json`, so its representation, whitespace, and
key order are not byte-preserved. It changes the top-level
`claims.json.schema` value from `1` to `2` and refreshes
`claims.json.updated_at`. Every existing run blob is preserved byte-for-byte;
run projection schema remains `1` because its JSON shape does not change.

The exact guard bytes are:

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "git": {
    "deploymentEnabled": false
  }
}
```

with one trailing newline. The backend owns this constant. Operators and
callers cannot supply or override it.

### Decode rules

- A schema-1 snapshot is accepted only without `vercel.json` and remains
  eligible for the one-way migration.
- A schema-2 snapshot requires `vercel.json` with byte-for-byte exact content.
- A missing, modified, oversized, duplicated, or differently encoded guard is
  invalid metadata. Reads report validation failure and mutations stop.
- Unknown files continue to fail closed.
- No supported schema-2 writer emits schema 1. Old clients reject schema 2
  before constructing a child, so they cannot accidentally remove the guard.
  As with every other metadata invariant, Git authentication and the branch
  ruleset remain the controls against a deliberately hand-crafted commit.
- The dedicated committer check runs before tree decoding exactly as it does
  today.

### Write rules

- New bootstrap roots use schema 2 and include the guard atomically in the
  parentless commit.
- Every schema-2 CAS child re-emits the exact guard.
- A mutation against a valid schema-1 board upgrades the complete child
  snapshot to schema 2. No commit may write schema 1 after the new client is
  installed.
- A dedicated `migrate --to-schema 2 --authorize coordinate` command performs
  the same CAS upgrade without creating, renewing, or releasing a claim and
  without changing any run projection; it preserves the mode and empty
  claims-array value semantically, canonically reserializes `claims.json`,
  changes `claims.json.schema` from `1` to `2`, refreshes
  `claims.json.updated_at`, and preserves every run blob byte-for-byte.
- Migration is idempotent: running it against schema 2 returns the current tip
  without creating a commit.

## Implementation boundaries

### `scripts/nous/backend_git.py`

- Add the current coordination tree schema and immutable guard bytes.
- Carry the claims-document schema in `CoordinationSnapshot`.
- Decode schema 1 and schema 2 with the exact file-set rules above.
- Serialize claims using the snapshot schema.
- Normalize every changed snapshot to schema 2 before commit construction.
- Add an idempotent `migrate_schema(*, target=2) -> SchemaMigration` CAS
  operation returning `previous_schema`, `current_schema`, `tip`, and
  `changed`.

### `scripts/nous_run.py`

- Add the operator-only `migrate --to-schema 2` command.
- Require current-request `coordinate` authorization.
- Permit the command only in `remote-required` mode.
- Report the old schema, new schema, and resulting exact coordination SHA;
  never print remote URLs or Git output.

### Documentation

Update `docs/engineering/nous-loop.md` and the original cross-machine design
so the authoritative branch layout, compatibility window, migration order,
and Vercel exclusion are no longer described as claims/runs-only schema 1.

## Compatibility and rollout

Old clients already reject unknown root files and claims schemas other than
1. Once schema 2 lands remotely, an old client therefore stops safely instead
of writing a child that removes the guard. This is fail-closed but means the
board must not migrate while either machine still runs the old revision.

Migration is a maintenance-window operation:

1. Keep both NOUS loops stopped.
2. Merge the implementation and verify the exact merged `develop` SHA.
3. Update Mac and Linux to that same SHA and run the focused coordination
   tests on both.
4. Verify the remote board is schema 1, has an empty stored claims array, and still points
   at the expected root/descendant.
5. Run `migrate --to-schema 2 --authorize coordinate` once from one machine.
6. Verify an ordinary fast-forward child, exact schema-2 tree, immutable
   guard, the semantically unchanged empty claims array and mode, canonical
   `claims.json` reserialization (its representation, whitespace, and key order
   may change), the expected `claims.json.schema` transition and
   `claims.json.updated_at` refresh, every run blob byte-for-byte unchanged, and
   no sentinel.
7. For 120 seconds, poll GitHub deployments and commit statuses for the new
   SHA. Success means no Vercel deployment record or failed Vercel status is
   created. A Vercel deployment of any state fails acceptance and stops rollout.
8. Leave the board claim-free. The later coordinated Mac/Linux smoke test and
   cutover remain separate operations.

Because the Mac is not always online, implementation may merge before step 3,
but the remote board must remain schema 1 until the Mac update is verified.

## Failure handling and rollback

- Before migration, rollback is simply staying on schema 1; no remote write is
  needed.
- After migration, do not remove the guard or rewrite history. An unexpected
  Vercel deployment stops the rollout and triggers the dedicated-repository
  fallback design. The schema-2 branch remains inert and append-only.
- If the migration push loses a CAS race, refetch and re-derive under the
  existing five-attempt policy. Any newly present claim aborts migration.
- If either machine cannot validate schema 2, keep both loops stopped and fix
  the client; do not downgrade the board.
- No recovery path uses force push, ref deletion, history rewrite, or a
  Vercel collaborator invitation.

## Testing and acceptance

Unit and local integration tests must prove:

- schema-1 snapshots without the guard remain readable;
- schema-1 snapshots with the guard are rejected;
- new bootstrap roots are parentless and contain exactly `claims.json` and
  `vercel.json`, with claims schema 2 and exact guard bytes;
- schema-2 snapshots with missing or modified guard bytes are rejected;
- claim, renew, run update, release, finalize, prune, and migration children
  all retain schema 2 and the exact guard;
- migration requires the stored claims array to be empty, preserves its empty
  value and mode semantically, canonically reserializes `claims.json`, changes
  `claims.json.schema` from `1` to `2`, refreshes `claims.json.updated_at`,
  preserves every run blob byte-for-byte, creates one ordinary fast-forward
  child, and is idempotent;
- old schema-1 clients fail closed on schema 2 by their existing validation;
- the CLI requires `coordinate`, remote-required mode, and target schema 2;
- all existing no-force, dedicated-identity, CAS-race, and bridge compatibility
  tests remain green.

The external acceptance check is the post-migration GitHub observation in
rollout step 7. A local test cannot prove Vercel's webhook ordering.

## Security properties

- The Vercel config is code-owned constant data, not remote/operator input.
- Exact-byte validation prevents a peer from enabling deployments or adding
  unrelated Vercel behavior while retaining a valid-looking schema.
- Supported schema-2 writers prevent accidental guard removal; Git
  authentication and the exact branch ruleset remain authoritative against
  deliberately hand-crafted metadata commits.
- The dedicated identity and foreign-commit check are unchanged.
- Existing secret, path, control-character, size, force-push, deletion, and
  branch-ruleset protections remain unchanged.
- Migration requires explicit coordination authority and an empty stored
  claims array.

## Settled decisions

- Use an immutable schema-2 `vercel.json` guard in the existing coordination
  branch.
- Keep `NOUS Coordination <nous-coordination@invalid>` unchanged.
- Keep run projection schema 1; version only the claims/tree protocol.
- Accept schema 1 only for the one-way compatibility window.
- Require both clients updated before the remote schema-2 migration.
- Treat any real Vercel deployment after migration as a failed design and move
  to the dedicated-repository fallback rather than weakening identity checks.

## Status update 2026-10-17

The original status ("proposed; awaiting owner review before implementation planning") is kept as written. Checked on 2026-10-17: the implementation shipped as #1581 (`fix(nous): exclude coordination commits from Vercel deployments`), following the cross-machine claims in #1580. This note does not verify the live Vercel project behavior.

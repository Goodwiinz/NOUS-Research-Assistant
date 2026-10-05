# NOUS self-improvement loop

Use this workflow for one autonomous improvement tick: find one real, live bug,
ship the smallest tested fix, review it, and close the tick honestly. This file
is the canonical workflow for every agent runtime. Runtime adapters may explain
how to invoke equivalent capabilities, but they may not weaken these gates.

A completed tick has exactly one terminal outcome. An explicit user
cancellation stops the workflow outside this outcome protocol:

- `merged` — the fix is present on `develop` after all required gates and an
  authorized merge.
- `ready-for-human` — safe progress exists, but a required permission, review,
  external gate, publication capability, or human decision remains. Report the
  exact stage reached; a fix or PR need not exist yet.
- `dry` — no safe, verified, in-scope bug was available to ship.

Never report a stronger outcome than the evidence supports. A branch, commit,
green targeted test, or open PR is not `merged`.

## Capability contract

The workflow names capabilities, not products or model tiers:

| Need | Required behavior |
| --- | --- |
| Investigation | Inspect current code and runtime evidence. Parallel scouts are optional; serial investigation is valid. |
| Coordination | Use `scripts/loop_bridge.py` when its shared-filesystem locking is supported. Otherwise use an equivalent atomic shared claim board; if none is available, stop at `ready-for-human` before editing. |
| Independent review | A separate review context that did not implement the change must inspect the final diff. Another agent from the same runtime qualifies; self-review alone does not. If unavailable, stop at `ready-for-human`. |
| Security review | Add a separate security-focused pass for tenancy, authorization, secrets, injection, or other security-class changes. |
| External evidence | Use traces, hosted CI, and review services only when access is already authorized. Never obtain credentials through secret dumps. |
| Repetition | Run one tick unless the user explicitly requested recurrence. A scheduler must have a bounded interval and stop condition. |

Substitute equivalent capabilities truthfully. Never claim that an unavailable
named tool, reviewer, test suite, or hosted service ran.

## Live preflight

Complete this before selecting work. Repository prose records durable rules,
not current operational state.

1. Read this file, root `AGENTS.md`, and the relevant files under
   `docs/engineering/` completely.
2. Fetch `origin/develop`; verify the candidate workspace is clean, isolated,
   and contains the fetched base. Create a fresh worktree or clone when the
   shared checkout is stale or occupied. A new tick starts at the fetched
   `origin/develop` with no pre-existing commits or diff; only reconciliation
   of prior loop work may start from an existing branch.
3. Inspect active loop claims through the selected coordination backend and
   inspect open loop-owned PRs. Before using the repository bridge, select a
   directory visible to every concurrent runtime, verify it is shared and
   writable, and export it as `LOOP_BRIDGE_DIR`. An established legacy default
   may be used only after the same checks. Then run
   `python3 scripts/loop_bridge.py list`. If no shared coordination location is
   available, use the capability-contract fallback instead of treating an
   empty local directory as proof that there are no claims. Reconcile an
   existing tick before opening another one.
4. Check the current branch-protection and hosted-CI state. Do not reuse a
   historical outage, CLI workaround, filesystem path, or tool availability
   claim as present evidence.
5. Inventory available test, trace, review, and publication capabilities.
   Record unavailable required gates before implementation.
6. Confirm the current task authorizes each remote mutation you may need:
   push, PR creation, merge, deployment, or external messaging. Lack of
   authority produces `ready-for-human`; it is not permission to infer.

## Cross-machine coordination

`local` is the compatibility and rollback mode. It uses only the existing
`scripts/loop_bridge.py` board and must never be described as cross-machine
unless every runtime has independently verified the same shared filesystem.

`remote-required` uses the orphan `nous-coordination` branch on the configured
Git remote as the authoritative board, with `$LOOP_BRIDGE_DIR` retained only as
that machine's same-host mutex. Configure both machines at the same tooling
revision:

```bash
export NOUS_COORD_MODE=remote-required
export NOUS_COORD_GIT_REMOTE=origin
export NOUS_GITHUB_REPOSITORY=Goodwiinz/rag
export NOUS_COORD_BRANCH=nous-coordination
export NOUS_MACHINE_ID=<stable-machine-id>
export LOOP_BRIDGE_DIR=<machine-local-mutex-dir>
export NOUS_RECEIPT_DIR=<machine-local-receipt-dir>
```

Claims schema 1 is the legacy, read-compatible tree and contains only
`claims.json` and `runs/<run-id>.json`. Claims schema 2 is the current write
format and additionally requires the repository-owned root `vercel.json`
whose exact `git.deploymentEnabled: false` value prevents the orphan data ref
from triggering Vercel. Unknown files, a schema-1 partial guard, or a missing
or modified schema-2 guard fail closed.

New bootstrap roots and every schema-2 child emit the exact guard. A valid
schema-1 board remains readable during the one-way maintenance compatibility
window; every supported write normalizes it to schema 2. Run projections
retain schema 1, while the claims document changes to schema 2. Updates fetch
into a unique `refs/nous/tmp/...` ref without making the
caller repository shallow or writing `FETCH_HEAD`, validate the complete
schema and dedicated committer identity, build one full-snapshot child commit,
and use a plain fast-forward push. A push race refetches and re-derives the
operation, with at most five attempts. Routine tooling rejects force pushes
and remote deletion.

Claims are fenced by `run_id` plus `claim_id`, use a three-hour TTL, and are
acquired remote-first then local. A local conflict compensating-releases the
remote claim. Renewal and release must present the exact fencing values printed
by `claim`; expiry is final. `list` and `check` label remote and local results
separately, and a remote transport failure never falls back to local-only.
An expired nonterminal run may be re-acquired only by presenting its last
`claim_id`; the winning claim rotates that token and fences every stale holder.
If local acquisition and its compensating release both fail, the CLI reports
`held-in-error` and attempts to atomically store the recovery fencing values at
`$NOUS_RECEIPT_DIR/<run-id>/held-in-error.json`; release that claim before any
other remote mutation. If the receipt write fails, stderr still contains the
fencing values with `"receipt": null`; retain that payload for recovery.

```bash
python3 scripts/nous_run.py list --json
python3 scripts/nous_run.py check --area "smoke" --files scripts/loop_bridge.py --json
python3 scripts/nous_run.py claim --agent <agent> --branch <branch> \
  --area "<area>" --files <comma-separated-paths> --authorize coordinate
python3 scripts/nous_run.py renew --run-id <run-id> --claim-id <claim-id> \
  --authorize coordinate
python3 scripts/nous_run.py release --run-id <run-id> --claim-id <claim-id> \
  --reason <reason> --authorize coordinate
```

Schema migration is a separate, explicit, claim-free maintenance operation:

```bash
python3 scripts/nous_run.py migrate --to-schema 2 --authorize coordinate
```

Keep both loops stopped and leave the `.remote-required` sentinel absent while
preparing migration. Both clients must be updated to the same exact merged
SHA and must pass the focused coordination tests at that SHA before migration.
Verify that the remote schema-1 board has an empty stored claims array, then
run migration once. It creates no claim, receipt, local mutex, sentinel, or
tick; it preserves the mode and empty claims-array value semantically, but
canonically reserializes `claims.json`, so its representation, whitespace, and
key order are not byte-preserved. `claims.json.schema` changes from `1` to `2`
and `claims.json.updated_at` is refreshed; every existing run projection blob
is preserved byte-for-byte while upgrading the complete snapshot by an ordinary
fast-forward child. Migration is idempotent when the already schema-2 board is
valid and claim-free. Afterward, verify the exact schema-2 tree and observe
GitHub deployments and commit statuses for 120 seconds on the migration SHA.
Any Vercel deployment of any state on that SHA stops rollout and invokes the
dedicated-repository fallback; do not remove the guard or rewrite the branch.

The current coordination milestone does not yet implement cross-machine
receipt resume, publication, or GitHub reconciliation. Do not run a real tick
in `remote-required`, and do not write the `.remote-required` sentinel, until
the migration observation is clean, the board is claim-free, those later
milestones are present, and the rollout smoke test below has passed from both
machines.

Cutover is a maintenance window after migration, in this order: stop both
loops; reconcile or expire both local boards; update both clients to the same
merged revision and focused-test result; bootstrap `nous-coordination` only
when the remote ref is absent (new roots are schema 2), otherwise verify the
existing schema-1 tree with no guard and no unknown files (or the already
valid schema-2 tree with its exact guard) and complete the
migration/observation sequence above as required; reject and stop rollout on
any unknown file or missing, modified, or partial guard. Configure both
machines while keeping the sentinel absent; apply a branch ruleset that
blocks force-push and deletion while allowing ordinary fast-forward pushes;
perform the two-machine claim/conflict/disjoint/release smoke test; only after
every observation and smoke gate passes, run
`cutover --write-sentinel --authorize coordinate`; then restart loops.
Bootstrap, migration, and cutover are separate operations. The local
`.remote-required` sentinel makes direct legacy mutation commands refuse while
leaving legacy `list` observational.

To roll back, stop both loops first, release or expire remote claims while both
clients are still in `remote-required`, set `NOUS_COORD_MODE=local` on both,
then run `python3 scripts/nous_run.py rollback --remove-sentinel --authorize
coordinate` on each machine before resuming the local bridge workflow. Leave
the coordination branch in place and inert; never rewrite its history.

## One-tick workflow

### 1. Reconcile prior work

Inspect only PRs created by this loop. Verify their current head, diff, tests,
reviews, comments, and merge state. Resume prior work under its own claim and
fencing rules; do not adopt another active owner's claim. Use the per-PR merge
protocol in step 8. Resuming a prior tick consumes this invocation: after
closing it or handing it off, stop rather than proceeding to step 2.

When the user requests reconciliation of an existing group, record the exact
authorized loop-owned PRs, their tick/claim ownership, permitted mutations, and
dependency order before making changes. Process parents before children, one
PR at a time, with separate evidence and outcomes for each tick. A dependency
outside that set is a blocker, not authority to adopt it. Read-only inventory
of other PRs does not authorize their mutation. Drafts, missing permissions,
and unresolved gates require an exact handoff; deadline pressure changes none
of these gates. Stop when that bounded group is reconciled or handed off. Do
not start a new bug tick under reconciliation authority or combine the group
into a new multi-bug tick. Proceed to step 2 only when no prior tick needs
reconciliation and the task authorizes a new improvement tick.

### 2. Pick one candidate

Use the first source available and authorized:

1. A reproducible failure from real or synthetic runtime traces. Synthetic
   traces come from `backend/scripts/synthetic_traffic.py`, scheduled by the
   chart's `synthetic-traffic-cronjob.yaml` into the LangSmith project named
   by `LANGSMITH_PROJECT` in the values file the live dev Argo application
   renders; select them by root run name `synthetic:<scenario>` or
   `metadata.synthetic`. The CronJob can be enabled yet suspended, so confirm
   recent runs exist before treating their absence as a clean signal.
2. A repository-backed audit or bug backlog whose status is still open.
3. A focused fresh hunt in one unclaimed subsystem.

Prefer fake-success bugs: work did not happen, but an API response, job state,
flag, UI, or test says it did. Verify the artifact itself rather than trusting a
status field. Reject style-only cleanup, speculative hardening, and unrelated
refactors.

### 3. Prove it is real and live

Re-read the current base and reproduce the symptom. Confirm the code path is
mounted or called, the user-visible impact exists, and the issue is not already
fixed. For backend routes, verify registration through `backend/src/main.py` or
OpenAPI; for other paths, verify the equivalent composition boundary.

Choose the likely files, then claim before editing:

```bash
python3 scripts/loop_bridge.py claim \
  --agent "<runtime-agent-name>" \
  --branch "<branch>" \
  --area "<bug/subsystem>" \
  --files "<comma-separated-paths>"
```

Exit code 2 means another loop owns overlapping work: choose a different bug.
Heartbeat before the claim expires and after any long test or review phase.

### 4. Establish root cause and scope

Trace the failure from symptom to cause. Check sibling entry points that share
the faulty invariant. Reports from delegated investigators are leads; the
primary agent re-verifies the highest-impact claim before it enters the fix or
PR description.

### 5. Fix test-first

Add the smallest regression test that exercises real behavior. Capture its
expected failure before changing production code, implement the minimal fix,
and run the same test green. Prove the test is causal by temporarily removing
only the production fix, observing the expected failure, restoring it, and
observing green again. If a test passes both ways, label it a contract lock;
do not present it as regression proof.

Follow [testing.md](testing.md), [backend.md](backend.md),
[frontend.md](frontend.md), and [api-contracts.md](api-contracts.md) for the
affected surface. Keep tenant identity derived from the authenticated actor,
not client input.

### 6. Review the final diff

Run an independent code review after the implementation and tests stabilize.
For a security-class change, run an additional security review. Verify each
finding against the code, address real issues, and rerun affected tests. Review
is evidence, not ceremony; an unavailable mandatory review ends as
`ready-for-human`.

### 7. Run local gates

Run targeted tests plus every applicable repository gate. Use
`scripts/ci/run_local_ci.sh --base origin/develop` as local evidence, while
preserving every skipped or unverified item in the report. Local success never
implies that omitted integration, end-to-end, security, or hosted checks passed.

### 8. Publish, recheck, and close

Use truthful authorship. Push and open a PR only when authorized. Include the
reproduction, root cause, focused diff, red/green evidence, review result, and
exact gate output—including skips—in the PR description.

Attach the PR number to the active claim, then inspect hosted checks and all
available automated or human PR comments against the published head. Diagnose
whether a red result is caused by the change or infrastructure, but do not
describe red checks as green. Verify review findings against the code and
address real issues.

Whenever the published diff changes, return to the causal test proof and local
gates, repeat independent review whenever the diff changes, and repeat security
review when applicable. Re-publish the reviewed commit and re-check hosted
evidence. Apply the following protocol to every PR, including reconciled work:

1. **Resolve the live requirements.** Read classic branch protection and all
   applicable active rulesets for the actual destination. Inspect their rules,
   enforcement, required check names/providers, review requirements, and actor
   exemptions; never infer requirements from a ruleset name. Record the PR's
   head SHA, base branch and current base SHA, open/draft state, and review
   evidence. Unavailable required state blocks merging. The independent-review
   and security-review requirements above still apply even when GitHub
   does not require an approving review.
2. **Prepare stacked children explicitly.** Record the child's old parent
   commit boundary before updating the stack. Prove the parent's actual fix
   landed on `develop`, not merely that its PR closed. After a parent squash
   or rebase merge, replay only the child's unique changes onto current
   `develop` using that boundary; do not blindly replay ancestor commits.
   Inspect the resulting diff for duplicated or lost changes and resolve
   conflicts under the existing claim and review gates. Retarget the child to
   `develop` before the subsequent authorized push. Verify that a qualifying
   CI run actually starts: the [Test Pipeline](../../.github/workflows/test-pipeline.yml)
   filters PR destination branches, and retargeting alone is not one of its
   default triggering activities. If no qualifying run starts, report the
   missing gate. Never substitute a successful Secret Scan or `CLEAN` state,
   and never count a merge into a feature-branch parent as `merged`.
3. **Incorporate the current base.** Before each merge, fetch `develop` and
   verify that its current tip is an ancestor of the candidate head. When it
   is absent, update the PR branch by merging the base or rebasing onto it;
   fetching alone is not an update. An already-current branch needs evidence,
   not an artificial commit. Prefer a forward update for a shared branch.
   Rewriting a PR branch, including a stacked child, requires verified
   ownership, authorization, and
   `--force-with-lease=<ref>:<expected-old-head>` on its push; a changed remote
   head requires reassessment, never an unconditional force push. These rules
   do not permit rewriting `develop` or `nous-coordination`.
4. **Rebuild the evidence.** An updated head, retargeted PR, or advanced base
   invalidates the previous merge-readiness decision. Rerun applicable local
   and hosted gates for the resulting integration; apply the causal-proof and
   repeat-review rules above whenever the diff changes, including conflict
   resolution or base-induced changes. Bind review evidence to the resulting
   head and reviewed base. Match required checks to their live configured
   provider and current run, not just a name or an earlier green result.
   Release Gate must conclude exact `success`, consistent with
   [assert_required_jobs.py](../../scripts/ci/assert_required_jobs.py);
   missing, pending, skipped, neutral, cancelled, failed, or wrong-provider
   evidence is not successful Release Gate evidence. Trusted successful checks
   on a synthetic merge commit are valid when their run and tested commit are
   verified to correspond to the current PR head and base; do not require all
   checks to run on the raw head SHA. Keep the independent review requirement
   even when the aggregate check is green.
5. **Guard the merge.** Immediately before merging, reread the live rules, PR
   head, base branch/SHA, open/draft state, reviews, and checks. Require the
   current task's merge authorization, destination `develop`, current base
   incorporation, the final reviewed head, and every required gate. If relevant
   state moved, invalidate readiness and repeat the affected preparation and
   validation. Use an expected-head
   merge guard, such as REST `sha` or `gh pr merge --match-head-commit`.
   These guards do not atomically lock the base. Omitting `--admin` does not
   prove protection applies to an exempt actor. To guarantee freshness at
   merge time, use a verified server-enforced path that applies the freshness
   and required-check rules to this actor at execution; a local claim or final
   read alone cannot supply that guarantee. If that capability is unavailable,
   return `ready-for-human` naming it. Do not bypass gates, relax repository
   settings, or directly push the base to complete the merge. A queued or
   auto-merge request is not a completed merge and must not escape these gates
   or the authorized PR set.
6. **Verify the result and advance serially.** After an authorized merge,
   inspect the returned merge result, fetch `develop`, and verify both the
   landed result and actual fix before reporting `merged`; squash/rebase can
   change commit identities. Release only that PR's claim with
   `--reason merged`. Invalidate readiness for every remaining group member
   and repeat this protocol against the new base before its merge. Stop at the bounded
   reconciliation endpoint in step 1.

For `ready-for-human`, report the exact blocker and stage reached. If a live
claim exists, heartbeat it once and report its expiry; if preflight stopped
before a claim, state that explicitly. If a PR exists, attach it by repeating
the same idempotent claim command with the same agent, branch, area, and files
plus `--pr <number>`. If publication was unavailable, state that no PR exists.
Never label or release the claim as merged.

Do not call a human handoff abandonment. If the user cancels, release any live
claim with `--reason abandoned`, stop immediately, and report cancellation
without inventing one of the completed-tick outcomes. If only the candidate
proves invalid, release its claim, choose another candidate when one remains,
otherwise close as `dry`. For `dry`, release any remaining exploratory claim
and state which sources were checked.

## Guardrails

- One bug and one coherent PR per tick.
- Never inspect or print production secrets, or run heavy analysis inside a
  live application pod.
- Never ship a fix for dead or unmounted code as a live bug fix.
- Never merge unrelated human or automated PRs under loop authority.
- Never expand from a bug fix into product behavior that needs a human choice.
- Record facts and commands, not confidence language. If a gate is missing,
  skipped, stale, or unavailable, say so.

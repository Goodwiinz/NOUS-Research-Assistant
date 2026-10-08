# Repository agent instructions

Repository context and engineering commands live in `docs/engineering/`
(start at `docs/engineering/README.md`; operational invariants are in
`docs/engineering/gotchas.md`). Directory-specific `AGENTS.md` files add
narrower rules and never override the root ones.

## Branching and worktrees

- The live integration branch is `develop`. Always cut new branches from
  `origin/develop` after a fresh fetch, never from a local `main` or a stale
  checkout. Open PRs against `develop`.
- Do your work in a dedicated git worktree (`.claude/worktrees/<name>` or
  `.worktrees/<name>`) and push early. Several agent sessions run in parallel
  in this repo; before starting a fix, check open PRs and the audit ledgers so
  two sessions never fix the same thing.
- Never run `git pull`, `git checkout`, `git reset --hard`, `git stash`, or
  `rm -rf` in a shared checkout. Stage files by explicit path, not `git add -A`.
- Never delete the base branch of a stacked PR. Retarget the children to
  `develop` first, then let the parent's branch go.

## Quality gates

- Backend lint gate: `ruff`, `black`, and `isort` on changed files, plus
  `mypy --disallow-untyped-defs` on added files. Run them locally before
  pushing; see `docs/engineering/backend.md`.
- Frontend: `pnpm` only (never `npm`/`yarn`). `tsc --noEmit` plus targeted
  `vitest` runs are the reliable local check; see `docs/engineering/frontend.md`.
- Race and idempotency tests must be mutation-verified before they count
  (`docs/engineering/testing.md`).
- CI-green is not the same as mergeable. Verify review-bot findings against
  the current head before dismissing them.

## NOUS improvement loop

When the user asks for the NOUS loop, a self-improvement tick, or autonomous
iterative bug fixing, read `docs/engineering/nous-loop.md` completely before
taking action. It is the canonical cross-runtime workflow.

Runtime-specific commands are adapters only. They may map available tools onto
the canonical capability contract, but they may not weaken its evidence,
review, authorization, or terminal-outcome gates.

## Pull requests and Linear

When a PR resolves a Linear ticket, put `Fixes GOO-nnn` (or `Closes`/`Resolves`)
in the PR body, one line per ticket. Linear only links a PR when the identifier
is in the branch name, the PR title, or behind one of those magic words in the
body; a bare mention in the body or a hand-attached link does not count. A
linked PR moves the ticket to In Review on open and to Done on merge; an
unlinked one leaves it stuck in In Review forever.

Agents must not approve their own PRs or merge over a failing required check.
Squash-merge is the default; if `develop` moved underneath you, update the
branch and let the checks rerun rather than force-merging.

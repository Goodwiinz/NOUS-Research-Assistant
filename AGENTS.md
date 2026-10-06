# Repository agent instructions

Repository context and engineering commands live in `docs/engineering/`.
Directory-specific `AGENTS.md` files add narrower rules.

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

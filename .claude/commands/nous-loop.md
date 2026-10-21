---
description: Run one evidence-driven NOUS self-improvement tick
---

# /nous-loop

Read `docs/engineering/nous-loop.md` completely and execute one tick, or its
bounded existing-PR reconciliation mode when requested. That file is canonical;
this adapter may not weaken its gates and only maps Claude Code capabilities
onto them.

## Capability mapping

- Use available subagents or the `Workflow` tool for independent investigation
  and review. Serial work is valid where the canonical workflow makes
  delegation optional.
- Step 6 review uses the `factory-reviewer-*` subagents when they are
  installed. Before spawning them, write `spec.md` (reproduction, root cause,
  scope, acceptance criteria) and `build.md` (first line `STATUS: READY`, then
  the red/green proof and gates run) into the review directory. That directory
  is the agents' job folder. Spawn the reviewers in one message:
  `factory-reviewer-code` always, `factory-reviewer-security` for a
  security-class change, and `factory-reviewer-ux` and `factory-reviewer-ui`
  when the diff changes rendered UI. Each prompt is
  `Job folder: <review dir>. Worktree: <worktree>. Branch: <branch>. Base: origin/develop. Review round <N>. Head: <head sha>. Base SHA: <base sha>.`,
  plus `Port: <port>.` for ux and ui. Their `round-<N>/review-<lens>.md` files
  are the step 6 verdict files.
- After a round with every verdict `PASS`, spawn `factory-approver` with the
  same paths and `Final round: <N>.` as a second step 6b check. Its `ESCALATE`
  ends the tick as `ready-for-human`. Its `APPROVE` authorizes nothing: step 8
  gates still decide the merge.
- The loop's primary agent still implements the fix test-first. Do not use
  `factory-builder` or `factory-spec-writer`: they have no causal-proof step.
- Without the factory subagents, use installed code-review and security-review
  skills or another separate review context. If a mandatory independent review
  is unavailable, return `ready-for-human`.
- Use `/loop` or `ScheduleWakeup` only when the user explicitly requested a
  recurring loop; one `/nous-loop` invocation otherwise means one tick.
- For step 7b, run `/nous-verify` (`.claude/commands/nous-verify.md`); its
  `BLOCKED` or `NOT RUN` result for a touched feature means `ready-for-human`.
- Respect the interactive permission classifier. If it prevents an authorized
  remote mutation, return `ready-for-human` with the exact handoff.

Use truthful authorship and attribution for the runtime and contributors that
actually produced the change. Never copy a model or tool footer merely because
it appeared in an older command version.

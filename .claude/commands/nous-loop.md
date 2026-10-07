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
- Use installed code-review and security-review skills when available. If a
  mandatory independent review is unavailable, return `ready-for-human`.
- Use `/loop` or `ScheduleWakeup` only when the user explicitly requested a
  recurring loop; one `/nous-loop` invocation otherwise means one tick.
- Respect the interactive permission classifier. If it prevents an authorized
  remote mutation, return `ready-for-human` with the exact handoff.

Use truthful authorship and attribution for the runtime and contributors that
actually produced the change. Never copy a model or tool footer merely because
it appeared in an older command version.

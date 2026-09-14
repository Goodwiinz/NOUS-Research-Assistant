# Invalid-pattern audit and agent-guidance design

**Date:** 2026-09-14
**Status:** approved for implementation planning

## Goal

Audit the repository for concrete code-quality hazards and give coding agents
concise, directory-scoped instructions that prevent those hazards from being
introduced or repeated.

## Scope

Create one evidence-based audit at
`docs/audits/2026-09-14-invalid-patterns-audit.md` and maintain an `AGENTS.md`
at each non-hidden, tracked top-level project root that contains subdirectories:

- `backend`, `brand`, `config`, `data`, `database`, `deployment`, `docs`,
  `evals`, `feature-flags`, and `frontend`;
- `infrastructure`, `memory`, `monitoring`, `notebooks`, `scripts`, `specs`,
  `src`, `supabase`, `tests`, and `tools`.

The existing root `AGENTS.md` remains the repository-wide entry point. The
existing `frontend/AGENTS.md` is revised in place without losing its Tambo or
Context7 notes. Hidden tool/runtime directories and flat roots such as
`daily-logs`, `nginx`, `projects`, and `security` do not receive redundant
files; they continue to inherit the root guidance.

## Considered approaches

### 1. Scoped `AGENTS.md` files plus one central audit — selected

Use the broadly understood nested `AGENTS.md` convention. Each file contains
only instructions that become more specific below the repository root, while
the central report records evidence, severity, and remediation priorities.
This keeps instructions near the code without copying the whole policy into
every directory.

### 2. One `INVALID_PATTERNS.md` in every directory

This is explicit but most coding agents do not automatically discover that
filename. It would also mix historical audit findings with durable editing
rules and encourage duplicated prose.

### 3. One repository-wide guidance file only

This is easiest to maintain but forces every agent to load unrelated backend,
frontend, database, and infrastructure rules. It also makes directory-specific
commands and ownership boundaries harder to find.

## Audit method

The audit uses repository evidence rather than generic best-practice lists:

1. Read current engineering contracts, manifests, CI workflows, pre-commit
   hooks, directory documentation, and recorded security/accessibility lessons.
2. Run existing static checks where they are deterministic and do not require
   services or credentials.
3. Search for candidate patterns such as raw exception disclosure, dynamic
   SQL, unsafe subprocess/shell construction, ignored type or lint failures,
   generated-file edits, tenant-unscoped persistence, committed runtime
   artifacts, and duplicated configuration.
4. Inspect each candidate before calling it a finding. Tests, fixtures,
   documentation examples, generated files, and allowlisted constant inputs
   are not automatically violations.
5. Classify results as confirmed finding, enforced invariant, or review risk.
   Each confirmed finding names evidence and a practical next action; counts
   are point-in-time observations, not permanent claims.

No production behavior is changed and no broad auto-fix is applied as part of
this documentation task.

## Guidance structure

Every scoped `AGENTS.md` is written as standalone Markdown with four compact
sections where applicable:

1. **Scope and sources of truth** — what the directory owns and which existing
   contract overrides assumptions.
2. **Invalid patterns** — precise “do not” rules tied to repository behavior,
   not universal style opinions.
3. **Required workflow** — boundaries such as generated artifacts, migration
   ordering, tenant isolation, or fixture preservation.
4. **Verification** — the narrowest real commands that validate changes in
   that directory, with service-dependent checks labeled honestly.

Rules inherit from ancestor `AGENTS.md` files. A child file may narrow an
ancestor rule but must not silently weaken security, authorization, evidence,
or verification requirements. Instructions use plain commands and repository
paths rather than vendor-only tools so they remain useful to Codex, Claude,
Cursor, Copilot, Jules, and other coding agents. Optional integrations are
described as optional.

## Quality constraints

- Do not duplicate the root policy verbatim across child files.
- Do not invent commands, architecture boundaries, or guarantees not supported
  by the repository.
- Do not label legacy debt as newly introduced by the current branch.
- Do not expose values from environment files, trace payloads, credentials, or
  other potentially sensitive artifacts in the report.
- Do not edit generated OpenAPI/TypeScript artifacts by hand.
- Keep historical specs, baselines, screenshots, datasets, and migration files
  immutable unless their local guidance documents an explicit replacement or
  regeneration workflow.
- Prefer links to canonical engineering documents over rephrasing nuanced
  contracts that could drift.

## Verification and acceptance

Acceptance requires:

- exactly the 20 scoped roots above to contain an `AGENTS.md`;
- all Markdown links and referenced repository paths to resolve;
- every listed command to exist in a manifest, Makefile, workflow, or script;
- the existing directory-doc lint to pass;
- no unfinished placeholder markers or boilerplate-only guidance;
- a consistency scan showing that child guidance does not contradict
  `docs/engineering/{backend,frontend,testing,api-contracts,gotchas}.md`;
- `git diff --check` to pass; and
- the final audit to distinguish current findings from preventative rules and
  limitations.

# AGENTS.md

Supabase-specific guidance. The repository-root `AGENTS.md` still applies.

## Scope and sources of truth

This directory owns the Supabase CLI project boundary: timestamped SQL
migrations under `supabase/migrations/`, local project configuration in
`supabase/config.toml`, and presentation templates under `supabase/templates/`.
Use the [backend standards](../docs/engineering/backend.md),
[testing standards](../docs/engineering/testing.md), [operational gotchas](../docs/engineering/gotchas.md),
and [invalid-pattern audit](../docs/audits/2026-09-14-invalid-patterns-audit.md)
for tenancy, migration, and service-dependent validation rules.

The timestamped files are ordered migration history for the Supabase schema;
they are distinct from standalone `database/` assets and from the backend
Alembic history. Existing files may already be historical/applied, so their
presence is not permission to edit them in place. `templates/confirmation.html`
is email presentation, not an authorization policy.

## Invalid patterns

- Do not edit, reorder, rename, or silently squash a timestamped migration
  after it may have been applied. Append a new migration to the Supabase
  history and preserve the chain's ordering.
- Do not weaken or bypass RLS, policies, grants, or tenant predicates for
  convenience. Policies and functions must fail closed across organizations
  and preserve the repository's tenant boundary.
- Do not define security-sensitive functions with an uncontrolled
  `search_path`. Keep schema qualification and a controlled `search_path`
  explicit so caller-controlled objects cannot change resolution.
- Do not put credentials, service-role keys, JWT secrets, or other secret
  values in `supabase/config.toml`, migrations, templates, logs, or reports.
  Keep them indirect through the environment/secret boundary.
- Do not treat email confirmation/invite/password templates as authentication
  or authorization logic. Presentation changes do not grant access, and policy
  changes do not belong in HTML templates.
- Do not make `supabase db reset` or `supabase db push` routine validation;
  these operations are service-dependent and reset/push may be destructive or
  affect a shared environment.

## Required workflow

- Confirm whether the change belongs to Supabase migration history,
  `backend/alembic/versions/`, or a standalone `database/` asset before
  editing. For migration work, append a timestamped file and inspect ordering
  and dependencies rather than rewriting history.
- For RLS or policy changes, review authenticated/anonymous behavior,
  organization predicates, grants, function ownership, and controlled
  `search_path`. Add or update a focused RLS regression test covering allowed,
  cross-tenant, and denied cases before applying the change.
- Keep `config.toml` non-secret and environment-neutral. Review auth-template
  links/content separately from authorization behavior and keep template
  changes presentation-only.
- Obtain explicit authorization before any Supabase CLI operation against a
  local or hosted service. Record service-dependent results as such and never
  paste credentials or service output containing sensitive values into the
  repository.

## Verification

Use the repository-backed aggregate check from the repository root:

```sh
scripts/ci/run_local_ci.sh
```

It includes the static migration/Alembic and backend checks but cannot prove a
hosted Supabase deployment. For a policy change, also run the relevant focused
RLS regression test (the existing
`backend/tests/unit/test_internal_agent_tables_rls_migration.py` demonstrates
the cross-stream contract). Label Supabase CLI reset/push, browser, database,
and other service-dependent checks as `NOT RUN` when prerequisites or explicit
authorization are absent; they are not routine validation.

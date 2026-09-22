# AGENTS.md

Database-asset guidance. The repository-root `AGENTS.md` still applies.

## Scope and sources of truth

This directory contains standalone SQL, Cypher, JSON, Lua, configuration,
documentation, initialization, maintenance, indexing, procedure, view, and
deployment assets. The [backend standards](../docs/engineering/backend.md),
[testing standards](../docs/engineering/testing.md), [operational gotchas](../docs/engineering/gotchas.md),
and [invalid-pattern audit](../docs/audits/2026-09-14-invalid-patterns-audit.md)
define the live-schema and tenant boundaries.

Do not confuse these assets with the live migration systems:
`backend/alembic/versions/` is the backend Alembic history and
`supabase/migrations/` is the Supabase CLI history. A file under
`database/`, including `database/migrations/`, may be standalone, generated,
historical, example, or an unresolved consumer. Its name or documentation is
not proof that a deployment runs it.

## Invalid patterns

- Do not apply, drop, or rewrite a database asset merely because its filename
  says `init`, `migration`, `deploy`, `rollback`, or `optimization`. Confirm
  the consumer, target engine, schema version, and authorization first.
- Do not reorder or edit an applied Alembic or Supabase migration in place.
  Live migration histories are ordered and append-only; preserve their own
  stream instead of copying standalone scripts into it.
- Do not interpolate untrusted SQL values or identifiers. Bind values and
  validate/allowlist identifiers, following the safe-identifier and validated
  enum patterns in the backend contract.
- Do not omit organization/tenant predicates from queries, policies, views,
  procedures, or data movement that touches tenant-owned rows. A successful
  SQL parse is not proof of tenant safety.
- Do not omit transactional boundaries, partial-failure behavior, or rollback
  reasoning from a schema/data change. Never run a live or destructive
  database operation as routine validation.

## Required workflow

- Start by naming the consumer and authoritative schema system. For a live
  schema change, use the owning Alembic or Supabase history; for a standalone
  asset, document the exact caller and keep its historical/generated status
  explicit.
- Review all caller-controlled values for parameterization, all dynamic
  identifiers for allowlisting, and every tenant-owned read/write for its
  organization predicate. Review transaction scope, locking/order, retry
  behavior, and a tested rollback or recovery path before any authorized
  execution.
- Preserve migration ordering and immutable history. Add a new revision to
  the correct live stream rather than modifying an earlier file to repair a
  current deployment.
- Obtain explicit authorization before applying anything to a database or
  shared environment. Keep credentials indirect and out of commands, logs,
  reports, and committed files; label service-dependent probes honestly.

## Verification

There is no `database/`-local gate that validates every standalone asset. For
related live Alembic work only, the static check is:

```sh
(cd backend && python ../scripts/ci/check_alembic.py)
```

This checks the backend Alembic revision chain, not every file under
`database/`, and requires the repository's Python/Alembic prerequisites.
Database execution probes are service-dependent and require authorization; if
they are not run, report `NOT RUN` rather than implying schema health.

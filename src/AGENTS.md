# AGENTS.md

Guidance for the root `src` tree. The repository-root `AGENTS.md` still
applies.

## Scope and sources of truth

This tree contains Trigger.dev jobs and their shared backend client/schema
helpers. Consult the [invalid-pattern audit](../docs/audits/2026-09-14-invalid-patterns-audit.md),
the root and frontend manifests, and the task files under `src/trigger/`
before treating a job as live.

Loader and deploy path (checked 2026-10-17): the root `trigger.config.ts` and
`frontend/trigger.config.ts` are byte-identical and both set
`dirs: ["./src/trigger"]`, relative to the directory the Trigger CLI runs from.
Run from the repo root, that is this tree (14 `.ts` files including `_lib/`).
Run from `frontend/`, it is `frontend/src/trigger/`, which holds only
`example.ts`. The deploy workflow `.github/workflows/trigger-deploy.yml` runs
`pnpm dlx trigger.dev@4.4.6 deploy` from the repo root with no
`working-directory`, so the root `src/trigger/` tree is what gets deployed. It
triggers on pushes to `main` only (paths `src/trigger/**`, `trigger.config.ts`,
`package.json`) and uses Node 20. Jobs are therefore deployed from `main`, not
from `develop`; do not treat `frontend/src/trigger/` as a job location.

## Invalid patterns

- Use the shared `src/trigger/_lib/backend-client.ts` for backend requests and
  `_lib/schemas.ts` for response/payload validation. Do not duplicate clients,
  wire schemas, or authorization-header construction in individual jobs.
- Never log access tokens, service-role tokens, authorization headers, or
  secret values. Keep error and diagnostic logging safe for shared logs.
- A caller-supplied user or project identity is input, not authorization. The
  backend must perform the authoritative authorization and tenant checks.
- Retries must be bounded by explicit attempts and timeouts; do not add
  unbounded retry loops or retry non-idempotent writes without a contract.
- Writes must be idempotent and safe across retries. Preserve task-level
  deduplication and human-in-the-loop confirmation for destructive operations;
  never bypass the HITL boundary to make a job easier to run.

## Required workflow

- Trace a job from the root loader/config through the backend endpoint and
  persistence operation before changing it. The loader is settled (see above);
  still record any unproven reachability of the backend endpoint rather than
  calling a job working.
- Keep backend-client calls schema-validated, authorization backend-owned,
  retries bounded, logs secret-safe, and writes idempotent/HITL-safe. Changes
  that alter wire shapes must follow the backend OpenAPI/generated-type
  workflow; generated artifacts are not edited here.

## Verification

The root manifest exposes `pnpm trigger:dev` and `pnpm trigger:deploy`, but no
isolated offline validator for this tree is defined. Both commands are
configured-service operations, not routine offline verification commands; do
not advertise or report them as proof that these jobs are valid or deployed.

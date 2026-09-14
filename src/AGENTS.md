# AGENTS.md

Guidance for the root `src` tree. The repository-root `AGENTS.md` still
applies.

## Scope and sources of truth

This tree contains Trigger.dev jobs and their shared backend client/schema
helpers. Consult the [invalid-pattern audit](../docs/audits/2026-09-14-invalid-patterns-audit.md),
the root and frontend manifests, and the task files under `src/trigger/`
before treating a job as live.

There is an evidence-backed loader ambiguity: `frontend/trigger.config.ts`
resolves `./src/trigger` relative to `frontend`, while this tree contains
`src/trigger/`. Before editing, registering, or deploying a job, prove the
actual loader and deployment path from its consumer. Do not assume the root
`src/trigger/` tree is live merely because it exists.

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

- Trace a job from its loader/config consumer through the backend endpoint and
  persistence operation before changing it. Resolve the Trigger configuration
  ambiguity with repository evidence and record any still-unproven reachability
  rather than calling it deployed.
- Keep backend-client calls schema-validated, authorization backend-owned,
  retries bounded, logs secret-safe, and writes idempotent/HITL-safe. Changes
  that alter wire shapes must follow the backend OpenAPI/generated-type
  workflow; generated artifacts are not edited here.

## Verification

The root manifest exposes `pnpm trigger:dev` and `pnpm trigger:deploy`, but no
isolated offline validator for this tree is defined. Both commands are
configured-service operations, not routine offline verification commands; do
not advertise or report them as proof that these jobs are valid or deployed.

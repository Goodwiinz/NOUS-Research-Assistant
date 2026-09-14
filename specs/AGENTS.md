# AGENTS.md

Specification-specific guidance. The repository-root `AGENTS.md` still
applies.

## Scope and sources of truth

This directory contains numbered feature specifications, plans, research,
data-model notes, contracts, quickstarts, checklists, and implementation
summaries. They are dated design and project records unless a current consumer
explicitly adopts a contract. For the live HTTP contract, FastAPI/Pydantic and
the generated artifacts described in [`docs/engineering/api-contracts.md`](../docs/engineering/api-contracts.md)
are authoritative; these snapshot specs do not replace that pipeline.

## Invalid patterns

- Do not rewrite a numbered spec, plan, contract snapshot, checklist, or
  implementation summary in place merely to make historical prose or a
  completed task list agree with current code. Preserve the record and add a
  dated amendment or forward link when superseding it.
- Do not infer verified runtime behavior from `[x]`/`[X]` checkboxes, a
  `Completed` or `Production Ready` label, an old quickstart, or an
  implementation summary. Confirm the claim in current code, tests, CI, or a
  live contract.
- Do not hand-edit snapshot OpenAPI or TypeScript contracts as a substitute for
  changing FastAPI/Pydantic and regenerating the downstream artifact. A stale
  snapshot is evidence to repair through the owning pipeline, not a second
  source of truth.
- Do not invent an API path, field, status, deployment, or acceptance result
  from a plan. Keep unresolved assumptions and historical status explicit.

## Required workflow

- Before changing a spec record, identify its number, date, status, source
  revision, and whether it is historical, proposed, or adopted by a current
  consumer. Link a superseding decision forward rather than silently changing
  old evidence.
- Keep numbered specs, contracts, and checklists internally consistent without
  weakening their historical meaning. Mark new observations with a date and
  verify implementation/status claims against the current consumer and
  engineering source of truth.
- For a corresponding live API change, change the FastAPI/Pydantic source and
  run the OpenAPI freshness check; regenerate the generated frontend types in
  the same change. Do not edit `frontend/src/types/generated/api.d.ts` or a
  spec snapshot to bypass the generated contract workflow.

## Verification

There is no root-local automated validator for all files under `specs/`. For a
corresponding live API change only, use
`python scripts/ci/generate_openapi.py --check` as the OpenAPI freshness
validation, together with the generated-type workflow in
[`docs/engineering/api-contracts.md`](../docs/engineering/api-contracts.md).
For historical edits, verify dates, links, status language, and checkbox
interpretation by review; do not report the freshness command as proof that a
snapshot spec or checklist is current.

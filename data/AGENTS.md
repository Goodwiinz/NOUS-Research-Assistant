# AGENTS.md

Data-specific guidance. The repository-root `AGENTS.md` still applies.

## Scope and sources of truth

This directory is sample/reference input, currently centered on
`data/datasets/`. The [data codemap](../docs/CODEMAPS/DATA.md), the
[invalid-pattern audit](../docs/audits/2026-09-14-invalid-patterns-audit.md),
and the named evaluation, integration-test, or script consumer are the
relevant sources of truth. The tree itself does not establish a live customer
data boundary or a production ingestion contract.

Treat CSV/JSON/text fixtures and exports as provenance-bearing artifacts.
Some may be generated or historical, while the audit's referenced evaluation
and test consumers remain a reachability question until a concrete caller is
identified. Do not call a dataset live, disposable, or interchangeable from
its filename alone.

## Invalid patterns

- Do not mutate a fixture to make an application or test failure disappear.
  Fix the consumer or record an intentional fixture contract change instead.
- Do not replace a dataset without a named consumer, stable expected schema,
  provenance, licensing/source note, and a reproducible replacement process.
- Do not bulk-normalize, re-encode, reorder, or resize files merely for
  consistency; those changes can alter evaluation meaning and downstream
  assumptions.
- Do not add customer, production, credential-bearing, or otherwise sensitive
  data. Keep committed samples synthetic or demonstrably sanitized, and do
  not copy values into reports, logs, or examples.
- Do not treat a generated export, an old sample, or a documentation example
  as the canonical live schema without proving its consumer and owner.

## Required workflow

- Before changing a file, name the exact consumer and expected schema, then
  inspect the consumer's contract rather than guessing from column or key
  names. Classify the file as sample, fixture, generated, historical, or
  unresolved.
- Preserve filenames, field types, required keys, ordering requirements, and
  representative edge cases unless the named consumer is changing with the
  data. Update schema documentation and consumer tests together when needed.
- Record source/provenance, generation inputs or command, size/shape, and
  sanitization status for an intentional replacement. Keep large or binary
  artifacts out of a text-only review unless the consumer requires them.
- If a fixture change exposes an application defect, preserve the fixture and
  report the failure. Do not weaken assertions or rewrite the input to hide
  the defect.

## Verification

There is no root-local automated validator for `data/`. Before replacing a
dataset, run the named consumer's existing test or verifier and review the
replacement's size, schema, provenance, and sanitization. If no consumer can
be named or its prerequisites are unavailable, record that evidence as
`NOT RUN`/unresolved rather than claiming the data is valid.

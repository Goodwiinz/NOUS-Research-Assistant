# AGENTS.md

Notebook-specific guidance. The repository-root `AGENTS.md` still applies.

## Scope and sources of truth

This directory contains exploratory notebooks, Python snippets, sample
documents, JSON notebook outputs, and rendered plots. Treat scripts here as
exploratory unless a current consumer proves that one is part of a supported
runtime or test path. A notebook, output, or plot is recorded evidence only
for the inputs, code revision, and environment that produced it; it is not a
current production contract.

## Invalid patterns

- Do not bulk-reformat notebook JSON or rewrite cell ordering, metadata, and
  outputs as incidental cleanup. Such changes destroy reviewable provenance.
- Do not erase outputs, overwrite plots, or replace sample documents merely to
  make a result look clean. Do not regenerate a recorded artifact without
  preserving its source inputs and explaining the replacement relationship.
- Do not embed credentials, access tokens, private traces, customer data, or
  other sensitive values in cells, outputs, plots, sample documents, or logs.
- Do not treat a filename, notebook result, or standalone script as evidence
  of a live consumer, production behavior, or reproducibility on another
  machine.

## Required workflow

- Before editing, identify whether the file is source code, notebook JSON,
  generated output, plot, or sample input, and name any current consumer. Keep
  the file's source revision, input/data provenance, environment/dependency
  assumptions, and intended audience visible in the change.
- For intentional regeneration, add explicit reproducibility notes describing
  the command or notebook entry point, input/data revision, relevant
  dependencies, and expected output relationship. Keep the prior artifact
  available when it is a historical comparison rather than a replacement.
- Review notebook diffs as structured JSON and rendered output separately.
  Preserve meaningful outputs and plots unless the change deliberately
  regenerates them with the documented provenance; do not infer production
  correctness from exploratory output.
- Keep any service, credential, browser, or customer-data dependency explicit
  and authorized. Move a proven reusable path into a supported script/module
  with its own consumer and tests instead of silently treating this directory
  as a runtime package.

## Verification

There is no root-local deterministic validator for `notebooks/`. For an
intentional notebook or artifact change, follow the notebook-specific
reproduction notes and review the JSON, outputs, and plots against the named
inputs; if a required service or dependency is unavailable, report that check
as `NOT RUN`. Use `git diff --check` for text whitespace only; it cannot prove
plot correctness, data provenance, or runtime behavior.

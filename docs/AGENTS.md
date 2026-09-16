# AGENTS.md

Documentation-specific guidance. The repository-root `AGENTS.md` still
applies.

## Scope and sources of truth

This directory contains current engineering contracts alongside archived plans,
dated audits and reports, approved specs, and other project records. For
current engineering behavior, use [`docs/engineering/README.md`](engineering/README.md)
and its linked backend, frontend, testing, API-contract, and gotcha documents.
The [invalid-pattern audit](audits/2026-09-14-invalid-patterns-audit.md) is a
dated evidence record, not a replacement for those current contracts.

Treat `archive/`, dated `audits/`, `plans/`, `superpowers/specs/`, and reports
as historical or decision evidence. Evaluation baselines and source manifests
under [`evals/`](../evals/) are likewise recorded evidence tied to their
declared revision and environment. A dated document, a completed checkbox, or
an old command/status statement does not by itself describe current runtime
behavior.

## Invalid patterns

- Do not present an archived, dated, or approved record as a live contract
  without checking the current code, workflow, or engineering source of truth.
- Do not silently rewrite an archive, dated audit/report, approved spec, plan,
  or recorded baseline to make history agree with a later implementation.
  Preserve the record and add a dated amendment, new decision, or forward link
  when the conclusion changes.
- Do not invent commands, ownership boundaries, deployment status, or file
  paths. Broken relative links and references to files that do not exist are
  documentation defects.
- Do not copy environment values, trace payloads, credentials, tokens, or
  personal/customer content into documentation or audit reports.
- Do not use a documentation statement to override a current code, CI, or
  security contract; call out contradictions and link the evidence instead.

## Required workflow

- Before editing, classify the document as a current contract, a historical
  record, a recorded evaluation artifact, or contextual material. Record the
  date, status, and relevant source revision for new evidence.
- For current engineering claims, link to the closest canonical contract and
  update that contract only when the implementation or enforcing check changed.
  Keep nuanced rules in `docs/engineering/` rather than duplicating them here.
- For a superseding historical conclusion, keep the original file intact and
  create a new dated amendment or record with a forward link to the earlier
  evidence. Never change a checkbox or prose silently so an old record appears
  freshly verified.
- Resolve every relative Markdown link and repository path from the file being
  edited. Check that targets exist and that a referenced command is backed by a
  manifest, Make target, workflow, script, or canonical engineering document.

## Verification

The repository directory-doc lint is `make docs-lint` (or
`python3 scripts/docs/check_dir_docs.py`); its scope is documented in the
[`directory-doc tooling guide`](../scripts/docs/README.md). It checks tracked
`README.md` and `doc.md` files only; it does not lint `AGENTS.md`. For changed
documentation, also run `git diff --check -- docs/` and manually
resolve all changed relative links and paths. These checks do not prove that a
historical claim is current; verify current claims against the linked source of
truth.

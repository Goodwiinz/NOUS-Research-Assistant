# AGENTS.md

Memory-specific guidance. The repository-root `AGENTS.md` still applies.

## Scope and sources of truth

This directory contains dated contextual notes about project history, people,
tools, terminology, and possible future work. These notes are working memory,
not canonical engineering rules, security policy, authorization, or proof that
a feature is live. For technical claims, use the current implementation and
the contracts indexed by [`docs/engineering/README.md`](../docs/engineering/README.md).
For audit conclusions, use the dated [invalid-pattern audit](../docs/audits/2026-09-14-invalid-patterns-audit.md)
with its recorded revision and limitations.

## Invalid patterns

- Do not promote a note, roadmap item, personal recollection, status label, or
  copied command into an engineering contract without verifying it against
  current code, tests, workflows, or `docs/engineering/`.
- Do not use an undated or stale note as evidence of current runtime behavior.
  Do not silently rewrite an old note to erase what was previously believed;
  add a dated correction or follow-up when context changes.
- Do not copy personal, customer, authentication, credential, trace, or other
  sensitive content from memory into an audit, report, guide, issue, or commit.
- Do not treat connected-service names, remembered paths, or planned work as
  proof that a consumer, deployment, permission, or integration exists.

## Required workflow

- Date new observations and state whether they are context, a hypothesis, or a
  link to verified evidence. For a technical statement, name the current code,
  contract, test, or workflow that confirms it.
- Cross-check notes against current implementation and canonical engineering
  documents before using them to make a change or report a conclusion. If the
  sources disagree, preserve the note as context and report the contradiction
  rather than choosing silently.
- Keep reports and durable guidance free of personal or sensitive details;
  summarize the evidence category and link to an approved source without
  copying its private values or payloads.

## Verification

There is no automated validator for `memory/`. Review each changed note for a
date, contextual status, source cross-check, valid links, and absence of
personal or sensitive content. Technical changes must be validated by the
current contract or consumer's own test; a clean memory diff is not runtime
evidence.

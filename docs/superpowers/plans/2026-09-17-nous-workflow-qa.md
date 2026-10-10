# NOUS Workflow QA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement and review this plan.

**Goal:** Build and exercise the user-selected CLI for repeatable NOUS workflow and Q&A testing with honest HTML/JSON evidence.

**Architecture:** A thin Node CLI delegates to a scenario registry, real browser/API session adapter, fixture ledger and static report renderer. Existing Playwright dependencies are reused; production application behavior is unchanged.

**Tech Stack:** Node 24, ECMAScript modules, Playwright, node:test, existing Python script-contract checks.

**Spec:** [Approved CLI design](../specs/2026-09-17-nous-workflow-qa-design.md).

## Global Constraints

- Source baseline `f733b179c0a23d45bc2f9d71737640203330a4d1`; work only in this isolated worktree.
- Implementation author: Luna max, preserving the user's existing model preference.
- Owner selected CLI with HTML/JSON reports. Live QA waits for completed deployment; local tool work can proceed.
- Node 24, existing Playwright dependency; no new package dependency or production feature change.
- Never persist credentials or unrelated customer content. No broad deletion, load generation, retries concealing failures, or false passes for missing prerequisites.
- Do not alter CI baselines, existing global setup/teardown or existing audit history.

### Task 1: Reusable runner and initial workflow campaign

**Files:**
- Create: `scripts/nous-qa.mjs` (thin CLI entry).
- Create: `tests/e2e/qa/cli.mjs`, `runner.mjs`, `report.mjs`, `session.mjs`, `scenarios.mjs`, `README.md`.
- Create: `tests/unit/scripts/nous-qa.test.mjs` for observable runner/input/report/ownership behavior.
- Modify: `package.json` only to add `qa:nous`.

**Interfaces:** The executable entry invokes `main(argv, env)` and returns its exit code. `runCampaign(config)` returns a report containing `schemaVersion`, `run`, `cases`, `cleanup` and `summary`. `renderHtml(report)` produces a standalone escaped document. Registry entries expose `id`, `title`, `suite`, prerequisites and `run(session, evidence)`; scenario actions use a session-owned fixture ledger and never delete unowned IDs.

- [ ] Read root/scripts/tests/docs instructions, the complete relevant helpers and the design. Inspect actual routes/schema/UI before implementing calls; use `backend/openapi.json` and current frontend/source as contracts.
- [ ] Write and run failing behavioral tests before implementation. Concrete failures: a credential-bearing target URL is accepted; report HTML executes a payload; a secret-bearing exception is emitted; missing credentials are counted as pass; a cleanup call accepts an unowned UUID; a timeout hangs indefinitely; an empty scenario selection exits successfully. Hand-derived assertions include escaped `&lt;script&gt;`, exit code 2 for blocked and exit code 1 for failed.
- [ ] Implement the minimum parser, report and runner contracts. Invalid configuration must fail before network/browser side effects. Use argument arrays for subprocesses, if any; never shell construction.
- [ ] Implement the public, authenticated and adversarial registry from the design using actual UI/API contracts. Prioritize real lifecycle/Q&A/reload/Stop/export evidence; mark unavailable prerequisites explicitly. Independent source inspection may settle route details, but not weaken a scenario's assertion to make it pass.
- [ ] Run `node --test tests/unit/scripts/nous-qa.test.mjs` and a local HTTP/browser tool test. Confirm the tests catch their intended defects and that the report contains no supplied sentinel secret. No live requests are authorized during this implementation task.
- [ ] Document exact invocation, modes, prerequisite/exit semantics, side effects, bounded limits, cleanup ownership and report privacy. Read the entire new execution path before any local invocation.
- [ ] Run script-contract tests and directory-doc lint, plus `git diff --check`; commit the coherent tool and tests. Return commit, exact tests and any remaining limitations to the controller for independent review.

### Task 2: Deployed workflow audit and tool refinement

**Files:**
- Create: `docs/audits/2026-09-17-nous-workflow-qa.md` as a dated evidence record.
- Modify only new QA tool files when live evidence exposes a tool defect or an unimplemented scenario; application defects are separately reported and prioritized.

**Interfaces:** Consume Task 1 CLI/report contracts. The report records observed versions and synthetic fixture manifest; the dated audit links sanitized evidence and distinguishes product defects, deployment gaps, tool failures and untested scenarios.

- [ ] Verify post-merge CI, image promotion and actual frontend/backend versions using read-only metadata. Wait for deployment before live QA, as requested.
- [ ] Execute the reviewed CLI against the owner's site/account, using environment-only credentials and explicit fixture-write mode. Run public cases, real Q&A/workflows and bounded adversarial cases; inspect failure evidence and reproduce distinct defects independently.
- [ ] Perform exploratory browser testing where the fixed registry cannot answer a workflow question. Add useful repeatable cases to the tool with tests that catch the observed defect; keep actual model/API tests separate from transport fault injection.
- [ ] Confirm report redaction and exact-owned cleanup, summarize pass/fail/blocked coverage and prioritize fixes with reproduction instructions. Do not count a scenario whose prerequisite was absent as a pass.
- [ ] Review the complete tool patch independently, resolve tool defects with Luna max and rerun the owning tests. Provide the CLI command, HTML/JSON reports, audit findings and a fix plan. Preserve any outstanding deployment dependency explicitly.


## Scope amendment — 2026-09-17, chat only

The owner narrowed this campaign to chat. Remaining execution covers chat answers, follow-up context, streaming and Stop, thread history and drafts, chat attachments, public reasoning summaries, exports, authentication, and responsive controls. Research projects, pipelines, notes, and draft generation are excluded from further testing and repair in this campaign. The two exact-owned temporary research projects were removed after the scope change. Earlier observations remain historical evidence.

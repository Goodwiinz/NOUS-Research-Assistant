# NOUS workflow QA CLI design

Date: 2026-09-17. Status: implementation design for the user-requested CLI with HTML/JSON reports. Source baseline: `f733b179c0a23d45bc2f9d71737640203330a4d1` (merged PR #1662).

## Purpose

Provide a reusable command-line campaign runner over the existing Playwright dependency, exercising the real NOUS frontend/API and recording reproducible outcomes. The owner explicitly requested deepest practical workflow/Q&A testing and selected a CLI with HTML/JSON reports. Live execution waits until deployment finishes. Preparing and testing the CLI locally is independent of that wait.

## Design

Keep the tool under `tests/e2e/qa/`, with a thin `scripts/nous-qa.mjs` entrypoint and a root `qa:nous` package command. Reuse the installed Playwright package; use Node 24 built-ins for argument parsing, assertions, unit tests and report generation. Do not alter existing Playwright global setup/teardown or invoke them against a live target: they contain CI-specific database seeding assumptions. A separate browser context and synthetic run prefix isolate each campaign.

The CLI provides `--list`, `--base-url`, optional `--api-url`, `--suite smoke|workflow|adversarial|all`, `--output-dir`, `--storage-state`, `--allow-writes`, `--expected-backend-sha`, `--timeout-ms` and `--max-turns`. Default suite is public smoke; live workflow/adversarial scenarios require explicit write mode and owner credentials from `NOUS_QA_EMAIL` / `NOUS_QA_PASSWORD` or a local storage-state file. Never accept credentials as command-line flags. Reject malformed URLs, credential-bearing URLs, unsupported schemes, invalid limits, unknown suites and zero selected cases. Keep one worker and bounded operations; no stress/load profile or bulk destructive action.

The scenario registry declares each scenario's ID, title, prerequisites and whether it creates test data or calls a model. Outcomes are PASS, FAIL, BLOCKED or SKIPPED with an explicit reason, assertion evidence, duration and prerequisites. Missing configuration, version mismatch, unsupported live capabilities and missing credentials are never PASS. Exit 1 if any assertion fails; exit 2 for blocked/incomplete selection or invalid configuration; exit 0 only for complete passing selected cases. Avoid retries that hide initial failures.

Each report includes local source identity, observed frontend/backend identity (unknown if unavailable), configuration without credentials, run timing, case outcomes, sanitized request paths/statuses, cleanup outcome and reproduction command without secrets. HTML must escape all values and contain no external assets/scripts; JSON uses a versioned schema. Reports exclude tokens, cookies, authorization headers, password values, unrelated account content and raw response dumps. Output directories are private. Failure screenshots may be optional and explicitly scoped/masked; credentials must never appear in them.

Fixture ownership is explicit: a unique `NOUS QA <run id>` prefix and a manifest of exact resources returned by this run. Mutations and cleanup may target only those manifest IDs. Do not delete pre-existing chats, workspaces or documents. Cleanup errors are visible and cause an incomplete result. Keep retained fixture IDs in the private report so interrupted runs can be understood. No destructive cleanup inferred from a broad title search.

## Coverage

Public smoke: frontend/login availability and accessible fields, unauthenticated dashboard protection, backend health and identity, malformed unauthenticated request denial.

Authenticated workflow: login, chat creation, simple deterministic Q&A, follow-up memory, Unicode, reload/persistence, isolated drafts/thread switching, history search, missing-thread handling, narrow desktop/mobile overflow and keyboard controls. API fixture cases cover workspace/conversation/thread lifecycle, message persistence and idempotent client-message IDs, pagination/order, archive/reopen/resolve state changes and exact-owned cleanup. Document attachment/upload and Markdown/PDF export use actual production routes/UI; byte signatures and public errors are asserted where available.

Adversarial coverage uses bounded invalid inputs, whitespace/empty/long input, stale/missing UUIDs, unauthenticated access to newly created IDs, unsupported attachments, interrupted network/refresh and Stop/resume/duplicate-submit behaviors. Synthetic Q&A fixtures contain known facts and absent facts; do not present heuristic semantic checks as proof of universal answer quality. Real provider/model execution is labeled distinctly from controlled transport fault injection. HITL approval scenarios must never approve arbitrary model-proposed actions: allow only declared operations on synthetic fixture IDs; otherwise record BLOCKED with the displayed capability.

Do not mock a server operation and label the result a live backend pass. Controlled browser transport fault tests are labeled as such. Every scenario should name the wrong behavior it catches. Bound model turns (default 12, maximum 50), requests and timeouts. Missing optional capabilities remain explicit, not silently removed from coverage.

## Verification and handoff

Test the tool itself with Node's test runner and local HTTP fixtures: argument/input validation, redaction, escaped HTML, status/exit aggregation, timeout handling, partial failures, fixture ownership and cleanup constraints. Exercise at least one real local browser flow through the tool. Existing script-contract tests and directory docs lint remain required. Capture final live campaign reports only after the deployed backend contains the merged repairs and the frontend is verified. Record deployment waiting as a prerequisite, not a product failure. Do not merge or deploy application changes as part of this testing-tool task.

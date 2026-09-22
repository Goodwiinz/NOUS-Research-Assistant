# Adversarial Chat Repairs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Status:** All seven tasks implemented and independently reviewed; product head `236867d21`. Final API/architecture suite: 788 passed; full frontend suite: 2,092 passed. Required CI gates and local browser verification passed. Not deployed.

**Goal:** Repair the reproducible data integrity, cancellation, export, and interaction defects found through the live browser audit.

**Architecture:** Keep the backend as the sole transcript writer and the existing chat store as its client cache. Use existing storage helpers, durable run records, and focused composer/UI hooks rather than adding parallel ownership systems.

**Tech Stack:** FastAPI/Python/SQLAlchemy/Redis, Next.js/React/Zustand/assistant-ui, Vitest/Playwright/pytest.

**Spec:** `docs/audits/chat-adversarial-2026-09-16.md` (A1–A15, with integration findings A16–A17). Detailed read-only diagnostics: `/tmp/chat-audit-20260916/{stop,export,ui}-diagnosis.md`.

**Execution order after the user's reasoning report:** 1 → 2 → 5 → 3 → 4 → 6 → 7. Shared files are still edited sequentially; reasoning is prioritized before the remaining composer/layout work.

## Global Constraints

- Implement with `gpt-5.6-luna`, reasoning effort `max`, as requested by the user.
- Work only in `/home/clawdbot/rag-clean/.worktrees/chat-audit-20260916`; preserve the dirty main checkout.
- No deployment, merge, shared-branch push, account changes, or modification of existing user research data.
- Preserve organization/member authorization, terminal reconciliation, single persisted writer, and canonical cache ownership from `docs/engineering/`.
- Node 24, pnpm 10.18.2, root lockfile; do not loosen quality baselines or add exclusions.
- Use behavior regressions, demonstrate failure before repair, and mutation-check new race/idempotency guards.
- Do not log/store credentials or authorization headers. Render stable public errors.
- Regenerate OpenAPI and TypeScript artifacts for HTTP schema changes; never hand-edit them.

## Task 1: Restore lazy cloud storage initialization and safe upload errors

**Files:** `backend/src/services/documents/file_service.py`, `backend/src/api/documents/files.py`, focused regression tests under `backend/tests/unit/services/` and `backend/tests/unit/api/`; preserve `backend/tests/test_upload_compensating_delete.py`.

**Interface:** Existing `FileService.upload_file` continues returning a persisted `Document`; `s3_helper` and `storage_helper` remain the lazy initialization owners. No schema change.

- [x] Add a regression constructing the service with uninitialized helper fields, patch the helper constructors, upload a small file for each cloud backend, and prove the constructor is used and the streamed bytes arrive intact. Do not pre-seed `_s3_helper`/`_storage_helper` as older tests do.
- [x] Run it red with `PYTHONPATH=backend /tmp/chat-audit-20260916/venv/bin/python -m pytest -q <new focused tests>` (use an isolated test environment if needed).
- [x] Replace direct private-field upload calls with `self.s3_helper.upload_fileobj(...)` and `self.storage_helper.upload_fileobj(...)`. Keep streaming, deduplication, quota and compensation semantics intact.
- [x] Cover an upload helper failure and preserve rollback/temporary-file cleanup. Public failure must not include arbitrary exception text; preserve explicitly safe validation/HTTP errors.
- [x] Run the new tests, compensation suite, upload route guards, and changed-file Python quality checks; commit this task only.

## Task 2: Make normal Stop durable

**Files:** `frontend/src/hooks/chat/useChatStreaming.ts`, `frontend/src/services/agentChatService.ts`, `backend/src/api/agent/{execute,streaming}.py`, focused run/submission service code under `backend/src/services/agent/`, their existing cancellation/resume tests, and generated contracts only if the API schema changes.

**Interface:** Preserve parked-confirmation cancellation and preflight behavior. An accepted cancellation targets the authenticated owner's exact active run, progresses through `stopping`, and is acknowledged by the producer as one `run.cancelled` terminal event. A stale request must never cancel a newer run.

- [x] Extend existing tests to show a normal running turn can receive an explicit stop command independently of transport disconnect, including direct Luna and graph execution.
- [x] Add expected-run identity and repeated-cancel/terminal-race coverage. Missing/foreign runs remain inaccessible; no process-local-only cancellation flag.
- [x] Implement the durable command using existing run columns (`cancel_requested_at`, status) and event service; retain immediate client abort but report command failure honestly. Do not mark cancellation complete before producer acknowledgement.
- [x] Make both producers observe cancellation while waiting and before further work, reuse prefix persistence/cleanup, clear the active replay pointer, suppress completed output, and prevent a later completion from overwriting cancellation.
- [x] Verify stopped partial content and badge survive reload; resume of an acknowledged stop is idle/terminal, never a continuing producer. Preserve observer-disconnect semantics.
- [x] Run cancellation, fast-path, resume, frontend preflight/submit-lock suites; mutate the new cancellation/idempotency guard to demonstrate a focused failure, restore, rerun, commit.

## Task 3: Preserve composer attachments, drafts, and replacement display

**Files:** `frontend/src/components/chat/ChatInput.tsx`, relevant `useChatStreaming` / `useChatSession` / display selector code, focused attachment/replacement/draft tests.

**Interface:** Transient drafts belong to the selected thread/new-chat context. The server remains the only message writer. `onSubmit(attachmentIds)` is called only for usable retained attachments.

- [x] Write a deferred-upload regression proving both clicking Send and pressing Enter/Queue cannot call the submit callback, clear the draft, or drop a chip before completion. Cover failed chip removal and a mixed batch.
- [x] Use one shared guard: retained attachments with state `uploading` or `error` block submission, with readable status and a recovery action; completed chips pass their document IDs.
- [x] Add A→B→A draft tests and preserve each context's text without crossing account/workspace boundaries or interfering with Stop preflight draft recovery.
- [x] Add an unavailable initial deep-link regression (A14): with a previously selected thread, a 404 requested thread cannot leave a different visible/submit target behind the unavailable URL. Preserve newer deliberate navigation and keep route/transcript/submission identity consistent; show safe recovery.
- [x] Add an edit replacement test that continuously observes the displayed rows, hides the replaced suffix during its replacement, restores it on failure, and reconciles the successful replacement without duplicates.
- [x] Run focused attachment, command, preflight, edit/regenerate and session tests. Mutation-verify each new race guard. Commit.

## Task 4: Repair responsive controls, search reachability, and accessibility

**Files:** `frontend/src/components/chat/{ChatSurface,ChatHeader,ChatSidebar,SlashCommandMenu,ChatDialogs,ChatInput}.tsx`, appropriate layout/drawer hooks, existing focused component tests and browser regressions.

**Interface:** Conversation navigation remains available through the drawer below the permanent-column breakpoint; project/context navigation stays reachable. Search is explicitly over loaded threads, with older pages still loadable.

- [x] Upstream commit `7ddc961f0` moved permanent history, drawer and trigger together to `xl` (1280px), and closes the drawer when it docks. This is already incorporated; preserve that behavior and verify it in the final browser matrix rather than duplicating the change.
- [x] Cap slash menu by available viewport height and make overflow scrollable; scroll highlighted options into view on arrow navigation/filtering.
- [x] Normalize search using `searchQuery.trim().toLowerCase()`. Keep pagination usable during filtering and state that unloaded threads may remain; a unique page-two match becomes reachable without clearing the query.
- [x] Label the rename textbox, correct invalid textarea ARIA while retaining keyboard autocomplete, and strengthen shortcut contrast.
- [x] Fix A13 in `artifact-panel/ArtifactPanel.tsx`: Escape consumed by the command palette must leave the underlying artifact open even if Radix unmounts the overlay before the document bubble handler runs. Test with the real dialog lifecycle and keep standalone Escape close behavior.
- [x] Verify real pointer hit targets at 320, 375, 390, 768, 800, 900, 960, 1023, 1024, 1279, 1280 and 1440 pixels, plus 390×520 menu bounds and keyboard activation; run focused axe checks and component tests. Commit.

## Task 5: Restore web reasoning visibility and sanitize rendered stream errors

**Files:** `frontend/src/services/agentChatService.ts`, `frontend/src/hooks/chat/useChatStreaming.ts`, chat store/types, `aui/AuiMessage.tsx`, `shared/ChatInlinePlan.tsx`, canonical message hydration; backend message schema/model/persistence and migration if needed for reasoning summary durability; existing error-category and reasoning tests.

**Interface:** Preserve auth recovery, permission, rate-limit and retry categories; display stable safe copy for transport, unknown server and tool errors. Display only server-provided planner rationale and provider-authored reasoning summaries, with clear labels, during streaming and from persisted history. Do not invent reasoning or expose raw/private provider fields.

- [x] Reproduce the CLI/web mismatch: `terminal/src/adapter.ts` carries the `plan` event rationale into the message; the web keeps it only in a closure and never gives it to live `ChatInlinePlan`. Feed it through the existing store and render it while running, including reasoning with no plan steps; use an explicit accessible reasoning label. Preserve the rationale after completion/reload using existing `plan_reasoning`.
- [x] Preserve emitted provider summary text through terminal reconciliation and history, using a bounded optional canonical `reasoning_summary` field when the current schema has no suitable field. Keep it separate from planner rationale and raw provider reasoning. Backend remains sole writer. Cover normal, stopped and confirmation paths; scope summaries to their owning turn so navigation cannot leak another turn's summary. Regenerate contracts for schema changes.
- [x] Add behavior regressions for streaming planner rationale, provider summary, completion/reload hydration, reasoning-only events, and empty/no-summary cases. Use the existing reasoning component and avoid duplicate panels.
- [x] Reproduce a 503 error containing `/srv/private/example.py` and verify it currently reaches displayed content.
- [x] Map known error categories/statuses to safe user actions; never concatenate arbitrary backend text into message content or accessible error descriptions. Keep diagnostics out of product copy.
- [x] Verify 401 exhausted recovery, 403, 429, 503, connection failure, malformed SSE, and confirmation errors retain their correct recovery actions without raw detail. Commit.

## Task 6: Make PDF and usage exports truthful and functional

**Files:** `backend/src/services/research/export_service.py`, `backend/src/api/research/export.py`, `backend/src/shared/export_schemas.py`, production requirements/image, frontend export/download service and tests, generated API contracts if affected.

**Interface:** A successful PDF is `%PDF-` bytes with `application/pdf` and a `.pdf` filename. Missing renderer/conversion failures return an explicit safe error. Measured provider usage is distinct from legacy token counts and unknown values.

- [x] Add formatter/API regressions for PDF signature, absence and converter failure, plus server-selected download filenames; demonstrate the current HTML fallback failure.
- [x] Pin a verified compatible WeasyPrint release and required native dependencies in the image actually serving the route; add an image smoke conversion. Disable remote/local resource fetching by user-controlled export HTML as appropriate for this formatter.
- [x] Remove HTML-as-success fallback and return safe unavailable/conversion errors. Prefer actual server filenames and reject incompatible content rather than saving it under a guessed PDF extension.
- [x] Export persisted `{input_tokens, output_tokens}` when present with metadata, label it as provider usage, preserve missing usage as unknown, and avoid a misleading standalone zero total in human-readable metadata. Do not estimate from text or silently reinterpret/backfill legacy counters.
- [x] Regenerate affected contracts, run exporter/route/frontend tests plus actual one-page PDF conversion, and commit.

## Task 7: Align Stop and resume authorization with editable thread access

Integration review found that an editor can start a run but the cancellation
and resume routes permit only the workspace owner. This follow-up uses the same canonical
thread access and workspace edit predicate as run creation, while preserving
caller-owned run identity, organization checks and deleted-parent protection.
Buffered SSE replay must also match the chosen stream to the caller-scoped
active/latest run; thread membership alone must not expose another caller's
uncommitted tokens or tool frames.

- [x] Add an editor-owned run cancellation/resume regression and denied access cases
  for revoked/viewer membership, foreign runs and deleted ancestors.
- [x] Replace both owner-only gates with the existing editable-thread policy;
  preserve parked confirmation, exact-run fencing and idempotency.
- [x] Reject parked confirmation checkpoints with a missing or foreign owner
  on authenticated resume; test the actual fallback path and same-owner success.
- [x] Verify the regression rejects the old predicate, run focused/static
  checks, commit and obtain independent review.

## Completion and review

- [x] Per-task independent review of spec compliance and code quality; fix substantive findings before proceeding.
- [x] Run repository frontend CI comparators and frontend suite; run focused backend suites and applicable architecture/API-contract checks.
- [x] Start a local frontend and repeat browser reproductions for changed UI behavior; record fixture-backed checks separately from live-service behavior.
- [x] Update the audit with repairs, tests, evidence, limitations, and any unresolved provider/deployment issues. Keep deployment explicitly separate from local verification.

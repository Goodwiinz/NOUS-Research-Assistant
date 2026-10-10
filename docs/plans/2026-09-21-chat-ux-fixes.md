# Chat UX fixes

Date: 2026-09-21. Status: implemented and locally verified; not deployed.
Base: `origin/develop` at `2bb00a6ae1c09ca877227413060cc2a396aa25f5`.
Scope: the six findings from the authenticated main `/chat` audit, not the widget.

## Approach

Use existing components, state owners, and search APIs. No new dependencies,
visual redesign, navigation reorder, backend message writer, or production edits.
Work in `codex/chat-ux-fixes-20260921`, leaving the dirty primary checkout intact.
PR #1670 owns navigation ordering; preserve ordering while changing responsive
presentation only. Three Luna (`gpt-5.6-luna`, max reasoning) workers own
composer/starters, search, and layout/citations respectively, with coordinated
ChatSurface integration. The parent agent owns this plan and independent review.

UX Components MCP `smart_query` was consulted for drawer and autocomplete
patterns. Apply modal focus/escape/return-focus behavior and explicit async
loading/empty/error states; reuse installed Radix primitives rather than adding
a component system. These references inform interaction choices, not evidence
that the app already meets them.

## Implementation and acceptance

1. **P1 Draft preservation.** Trace ChatInput and all slash-menu callers. Opening
   Commands is menu state, not a replacement draft. Dismissal preserves exact
   text and selection; selection executes the requested command without silently
   discarding an unrelated draft. Preserve existing typed-slash behavior, IME,
   streaming, and keyboard navigation. Test open/Escape, selection, and typed slash.
2. **P2 Source state.** Lift/reuse one owner for the composer toggle and context
   rail; remove hardcoded active indicators. The outgoing `use_rag` value and all
   mounted context presentations must agree. Test toggle both directions.
3. **P2 Mobile access.** Expose the existing context controls through an accessible
   narrow-screen drawer. Reclaim the persistent global rail width on mobile while
   retaining reachable navigation. Reuse existing modal primitives and navigation
   content; preserve desktop layout and navigation order. Check 320/390/768/1440px,
   accessible names, Escape, focus containment and return, and no horizontal scroll.
4. **P2 Citation readability.** Inspect available passage metadata first. Prefer
   an actual supplied excerpt where available; never infer or invent evidence.
   Bound long previews with explicit expansion to the untouched original content,
   preserve source links and citation actions, and render source text safely.
   If no passage metadata exists, identify the preview honestly rather than
   labeling a guessed sentence as supporting evidence. Test short/long/empty
   text, Markdown-like content, and expansion retaining all original text.
5. **P2 Search beyond loaded threads.** Reuse the existing thread search service
   with active workspace scope, debouncing, pagination, loading/empty/error
   feedback, and stale-response protection. Do not fetch every thread page or
   replace the normal list/cache with search results. Selecting a search hit must
   open an unloaded thread through the existing detail flow; clearing restores
   the normal list. Test older hit, scope change, old response arriving last,
   pagination, failure/retry, and clearing. Use generated contracts on touch;
   avoid changing the backend schema if the existing API suffices.
6. **P2 Starter continuity.** Selecting a starter focuses the composer for editing
   without sending. Remove wording that assumes an unspecified document, claim,
   or pair of findings; make required user input explicit using simple editable
   templates/copy. Preserve any nonempty draft. Test resulting focus and text.

## Verification and handoff

- Read applicable AGENTS and engineering contracts before editing.
- Use Node 24 and pnpm 10.18.2 with the root frozen lockfile.
- Add focused tests in the existing Vitest suites; run changed-file CI wrapper,
  targeted tests, and `git diff --check`. Run broader tests where feasible.
- Prove a new stale-request guard with a failing mutation, then restore and rerun.
- Report any missing runtime/browser/service prerequisite explicitly. Unit tests
  are not proof of live mobile behavior or end-to-end accessibility.
- Review the complete diff and compare all six acceptance outcomes. Report local
  validation separately from hosted CI, deployment, and live acceptance; no push,
  PR, or deployment is authorized by this implementation request.

## Baseline evidence (before implementation)

- Node 24.21.0 / pnpm 10.18.2 frozen installation succeeded.
- Frontend `type-check` passed. Four focused suites passed (26 tests):
  ChatSidebar, SlashCommandMenu, CitationPanelBody.relevance, and
  useChatSession.sidebarPagination.
- `scripts/ci/run_local_ci.sh --base origin/develop --skip-tests --frontend`:
  frontend quality and exclusion comparators, TypeScript, and directory-doc
  checks passed. Full-tree lint remained advisory (115 errors, 2003 warnings).
  Backend checks could not execute successfully: Ruff absent; OpenAPI lacked
  `dotenv`; Alembic lacked `alembic`. No Python source is planned for change.
- Existing local mobile fixture starts on port 3107. Its deterministic data
  does not prove authenticated backend search or the production layout wrapper.

## Implementation outcome and verification

All six scoped changes are implemented. Commands now opens independently of the
draft; commands that replace or send over an unrelated draft require explicit
discard confirmation. Cancelling restores text, selection, and composer focus.
Safe commands preserve the draft. Starter prompts focus the composer, retain
existing text, and explain the user input still needed.

The page and context rail share RAG state. Mobile navigation and context have
labeled dialog triggers in one reserved row, with desktop presentation retained.
Server search reuses the existing scoped API and TanStack Query, supports older
hits and pagination, and does not replace the normal thread list. Citation
previews are bounded, plain-text safe, and expandable to the complete original.
They do not claim a semantically selected supporting passage: the available
citation contract provides no separate passage field, and retrieval/extraction
behavior is unchanged.

Local evidence on 2026-09-21:

- Full frontend Vitest: **307 suites, 2244 tests passed**. Command:
  `pnpm --dir frontend exec vitest run --maxWorkers=2`.
- The CI-equivalent wrapper with `--base origin/develop --skip-tests --frontend`
  passed TypeScript, changed-file frontend quality, exclusion checks, and
  directory-doc checks. Full-tree lint remained advisory (114 errors,
  1994 warnings before final baseline tightening). Backend Ruff, OpenAPI, and
  Alembic checks remained unavailable for the same missing tools as the baseline.
- Search worker reports a negative mutation check: collapsing the query key
  caused the stale-response test to fail; original source was restored before
  the successful final suite. The existing mutant checks also passed.
- In-app browser fixture: starter selection does not send and focuses Message;
  an existing draft survives another starter. Commands open/Escape keeps text;
  Retry requires confirmation; cancel returns focus after the exit animation.
- Actual CSS widths 320, 390, 768, 1024, and 1440 had no horizontal document
  overflow and kept Send in the viewport. Browser zoom was accounted for when
  setting the test dimensions. Mobile context displayed Off when sources were
  disabled; Escape returned to its trigger. Navigation initially focused
  Overview, wrapped keyboard focus inside the dialog, and returned to its
  trigger on Escape.
- `git diff --check` passed. No backend source, package lock, production data,
  deployment, or primary-checkout changes were made during implementation.
  Hosted CI and live authenticated acceptance remain unverified.

## PR handoff

The user subsequently requested a PR, authorizing the scoped commit, branch
push, and draft PR against `develop`. The implementation evidence above was
collected before publication; it is not hosted-CI or deployment evidence.

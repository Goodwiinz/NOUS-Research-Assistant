# Chat reading experience (footnotes) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship the footnotes reading design from `docs/plans/2026-09-06-chat-reading-footnotes-design.md`: serif reply with superscript citation numerals, a numbered Sources list under each reply, quiet action row, Aurum user turn, honest "Use my sources" switch, hint row removed, source counts in the thread list.

**Architecture:** Two PRs off `origin/develop`. PR 1 is the transcript (message body, citation marker, sources list, actions, user bubble). PR 2 is composer + sidebar. No new data: everything renders from fields already on `Citation` (`document_title`, `page_number`, `snippet`, `score`, `document_id`) and on the thread. All copy literal; tokens only.

**Tech Stack:** Next.js 15, React 19, Tailwind v4 (`bg-(--nous-*)`), assistant-ui primitives, vitest + testing-library, pnpm.

---

## Ground rules

Same as `docs/plans/2026-09-05-chat-frontend-a11y-fixes.md` "Ground rules for every task": worktree per PR off `origin/develop` under `.worktrees/`, path-explicit staging, the two-line commit trailer, PR body ending with the Claude Code line + session URL, frontend gates (`tsc --noEmit`, prettier, `vitest run <touched>`; ESLint locally crashes on a pre-existing config error, say so and rely on CI's changed-file gate, which requires ZERO errors in touched files, so keep new code free of `react-hooks/set-state-in-effect`, unused vars, `any`). Mockup of record: canvas page "Chat", artboard "Chat A · Footnotes" (values below are lifted from it). Copy has no em dashes.

Design doc to include in PR 1: `git add docs/plans/2026-09-06-chat-reading-footnotes-design.md docs/plans/2026-09-06-chat-reading-footnotes.md` (both already exist in the main checkout; copy them into the worktree).

---

### Task 1: Transcript (branch `feat/chat-reading-footnotes`)

**Files:**
- Modify: `frontend/src/components/chat/CitationLink.tsx` (marker markup, ~73-130)
- Modify: `frontend/src/components/chat/shared/CitationChips.tsx` → rewrite as the numbered sources list (keep the file name and the `CitationChipsProps` contract so callers and tests keep compiling; rename internally later if you like, not now)
- Modify: `frontend/src/components/chat/aui/AuiMessage.tsx` (user bubble ~365-430, assistant body ~800-830, action row `nous-msg-actionrow`)
- Modify: `frontend/app/globals.css` (`.nous-chat-body` 194-215, `.nous-bubble-user`, `.nous-msg-actions`/`.nous-msg-action`/`.nous-msg-actionrow` 450-510)
- Tests: existing `frontend/src/components/chat/__tests__/*` and `frontend/src/components/chat/shared/__tests__/*` touching CitationChips/AuiMessage (grep); add `CitationChips.footnotes.test.tsx`.

**Step 1: Failing test for the sources list.** Render `CitationChips` with three citations (two sharing a `document_id`) → expect three rows? No: expect ONE row per distinct document in first-cited order (2 rows), numerals "1" and "2", the `document_title`, the locator `p. 7` when `page_number` is 7, the snippet in quotes, and NO percentage text. Click a row → `onCitationClick` called with that citation. Run → FAIL.

**Step 2: Sources list.** Markup (Tailwind, tokens):
```
<section aria-label="Sources" class="mt-5 max-w-[720px]">
  <div class="mb-1 text-[11px] font-semibold text-(--nous-fg-3)">Sources</div>
  <ol class="m-0 list-none p-0">
    <li> <button type="button" class="grid w-full grid-cols-[22px_1fr_auto] items-baseline gap-3 border-t border-(--nous-border-1) py-2.5 text-left hover:bg-(--nous-bg-2) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40">
      <span class="text-[11px] font-semibold text-(--nous-sol-safe) dark:text-(--nous-helios)">1</span>
      <span>
        <span class="text-[13px] font-medium text-(--nous-fg-1)">{title} · {locator}</span>
        <span class="mt-0.5 line-clamp-2 text-[14px] leading-[1.5] text-(--nous-fg-2)" style="font-family: var(--nous-font-body)">“…{snippet}…”</span>
      </span>
      <span class="whitespace-nowrap text-[11px] text-(--nous-fg-3)">{retrievedVia}</span>
    </button></li>
  </ol>
  <div class="border-t border-(--nous-border-1)"></div>
  {diagnostics link, unchanged, right-aligned under the rule}
</section>
```
Locator: `p. {page_number}` when present, else nothing (no fabricated "§"). `retrievedVia`: only if the citation object carries a source label (inspect `Citation` in `src/types/workspace.ts` and `utils/citationParser.ts`; if no such field exists, omit the column entirely and note it in the PR body; do NOT derive it from `score`). Dedupe by `document_id ?? external_reference_id ?? id`, keep first occurrence, numeral = 1-based index in that deduped order. Title fallback: `document_title ?? title ?? 'Untitled source'`.

**Step 3: Marker.** In `CitationLink.tsx` change the inline marker to a superscript numeral: `<sup class="ml-px align-super text-[10px] font-semibold leading-none text-(--nous-sol-safe) dark:text-(--nous-helios)">{n}</sup>` inside the existing button (keep the button, hover popover, click behaviour, aria-label "Source n: {title}"). `n` must match the sources list numbering: compute the dedupe map once (export a small `numberCitations(citations)` helper from `CitationChips.tsx` returning `{ordered, indexById}`) and pass it from `AuiAssistantMessage` to both `CitationRenderer` (→ `CitationLink`) and the list. Also update the `.nous-cite-ref` class in `nous-tokens.css` if it is what the marker uses today (grep) so the two do not diverge.

**Step 4: Reading measure.** `.nous-chat-body`: add `max-width: 72ch` only if `CitationRenderer`'s `max-w-[72ch]` is not already the wrapper; set `font-size: 17px` under `@media (min-width: 1024px)`; keep 16px below. Assistant column container: ensure the reply block is `max-w-[720px]`.

**Step 5: User bubble.** `.nous-bubble-user`: `background: var(--nous-aurum); border: 1px solid var(--nous-apollo); border-radius: 14px 14px 4px 14px;` (dark: `background: var(--nous-ember); border-color: var(--nous-shade)`). Text stays `.nous-chat-body` 16px.

**Step 6: Actions row.** `.nous-msg-actions { opacity: 1 }` always (drop the hover reveal and the `(hover:none)` override added in #1609, both now redundant), `.nous-msg-action`: text + icon, `font-size: 12px; color: var(--nous-fg-3); gap: 5px; min-height: 28px` desktop, 44px under `(pointer: coarse)` as today; row gap 18px. Labels: Copy, Regenerate, Helpful (rating). Ensure existing `data-testid`s and aria-labels stay.

**Step 7: Gates.** `pnpm exec vitest run src/components/chat` (update assertions that referenced the `%` text or the chip markup; keep behaviour assertions), tsc, prettier. Commit `feat(chat): footnote citations and numbered sources under each reply`. PR body: what changed, the retrieved-via decision, what is not browser-verified.

---

### Task 2: Composer and thread list (branch `feat/chat-composer-sources-count`)

**Files:**
- Modify: `frontend/src/components/chat/ChatInput.tsx` (~434-510 top strip, ~800-830 commands chip, ~900-950 hint row)
- Modify: `frontend/src/components/chat/ChatSidebar.tsx` (~340-415 thread row)
- Tests: `ChatInput-*.test.tsx` (grep for "Ultra Thinking"), sidebar tests.

**Step 1: Failing tests.** (a) ChatInput renders a switch with accessible name "Use my sources", `role="switch"`, `aria-checked` mirroring the RAG state, and the two existing tooltip strings; no text "Ultra Thinking" anywhere. (b) The hint row text "to send" is absent. (c) ChatSidebar row for a thread with `citationCount: 3` and 4 turns shows "3 sources · 4 turns"; a thread without citations shows the preview snippet as today. Run → FAIL.

**Step 2: Switch.** Replace the mono chip at ~489 with `<button type="button" role="switch" aria-checked={ragEnabled} aria-label="Use my sources" title={…existing tooltip…}>` containing a 28×16 track (`bg-(--nous-sol)` on, `bg-(--nous-border-2)` off) with a 12px knob, and the label "Use my sources" in 12px UI type. Remove `font-nous-mono` from the composer top strip and the `/ Commands` chip (~814): keep the chip, UI font, 12px.

**Step 3: Hint row.** Delete the block at ~900-950 (`to send · shift+enter for newline · / for commands`). Add `title="Send (Enter)"` to the Send button if it lacks one.

**Step 4: Thread row.** In `ChatSidebar.tsx` where `snippet` is computed (~344-351): if the conversation exposes a citation count (inspect the `Conversation`/thread type: look for `citationCount`, `sourceCount`, or citations on messages; if only messages are available, count distinct `document_id`s across `conv.messages[].citations`), render `${n} source${n===1?'':'s'} · ${turns} turn${turns===1?'':'s'}`; else keep the existing preview. Do not add a fetch; if the list endpoint provides no citation data and messages are not loaded, leave the row as is and say so in the PR body.

**Step 5: Gates**, commit `feat(chat): honest sources switch, no hint row, source counts in thread list`, PR.

---

## Out of scope

Rail, welcome state, dark-specific work, backend fields. No browser verification; both PR bodies say so.

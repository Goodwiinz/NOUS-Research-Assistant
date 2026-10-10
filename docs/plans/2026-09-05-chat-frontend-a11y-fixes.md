# Chat Frontend Audit Fixes (design-system + a11y + states + responsive) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the 33 findings in `~/.audit-ledgers/rag/chat-frontend-ds-a11y-2026-09-05.md` (GitHub issue #1603) as six small, independent PRs off `origin/develop`.

**Architecture:** Each task is one PR on its own branch cut from `origin/develop`, in its own worktree under `.worktrees/`. Fixes are minimal and local: no new abstractions, no component rewrites. Where a finding is a design decision (dead control, fake data, rail typography) this plan makes the call; the executor does not re-decide. Findings are referenced by ledger ID; file:line references are at SHA `51830fd59` and may drift a few lines.

**Tech Stack:** Next.js 15, React 19, Tailwind v4 (`bg-(--nous-*)` arbitrary-var syntax), framer-motion, assistant-ui, vitest + testing-library, pnpm.

---

## Ground rules for every task

- Repo: `/Users/goodwiinz/development/RAG_system`. Never `git checkout`/`pull`/`reset`/`stash` in the main checkout. Per task: `git fetch origin develop && git worktree add /Users/goodwiinz/development/RAG_system/.worktrees/<branch> -b <branch> origin/develop`. Work in the worktree. `pnpm install --frozen-lockfile` in `<worktree>/frontend` if `node_modules` missing.
- Stage path-explicitly. Commit trailer (exact, two lines):
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01DgdZGWxkRah4zDk1Ku4FCZ`
- PR body ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)` then a blank line then the session URL. PR body lists the ledger IDs closed and links issue #1603 (`Refs #1603`, not `Closes`).
- Gates per task, run in `<worktree>/frontend`: `pnpm exec tsc --noEmit -p tsconfig.json`; `pnpm exec eslint <touched files>` (pre-existing `explicit-function-return-type` warnings and `set-state-in-effect` errors in untouched lines are acceptable, say so); `pnpm exec prettier --write <touched>`; `pnpm exec vitest run <touched test files>`. Never `npm run validate` (pre-existing red).
- Tokens only. Never hex, never `text-gray-*`/`text-blue-*` etc. Palette: `--nous-sol`, `--nous-helios`, `--nous-sol-safe`, `--nous-fg-1/2/3`, `--nous-bg-1/2/3`, `--nous-border-1/2`, `--nous-mars` (error), `--nous-corona` (warning), `--nous-terra` (success), rgb triplets `--nous-sol-rgb`, `--nous-helios-rgb`, `--nous-erebus-rgb`, `--nous-mars-rgb`. See `frontend/app/nous-tokens.css` and `DESIGN.md`.
- Copy: sentence case, no em dashes (use a period or comma).
- Ledger: before starting a task set its rows to `claimed`, owner `sess:fix-chat-ds-a11y`; on PR open set `pr` + `#NNNN`. Ledger path above. One row edit at a time.
- After each PR: post a one-line comment on issue #1603 listing the IDs and PR number, ending with `Reviewed by Claude Fable 5.1 (claude-fable-5-1) · session https://claude.ai/code/session_01DgdZGWxkRah4zDk1Ku4FCZ`.

---

### Task 1: Keyboard, focus, announcements (branch `fix/chat-a11y-keyboard`)

Closes: A1, A3, A5, A8, A10, A14, DS8.

**Files:**
- Modify: `frontend/src/components/chat/ChatInput.tsx` (~721-760)
- Modify: `frontend/src/components/assistant-ui/tool-fallback.tsx` (~161)
- Modify: `frontend/src/components/chat/ChatMessageList.tsx` (~243, ~427-437)
- Modify: `frontend/src/components/chat/ProjectPickerPopover.tsx` (~126-129)
- Modify: `frontend/src/components/chat/aui/AuiMessage.tsx` (~388)
- Modify: `frontend/app/(dashboard)/chat/chat-layout-client.tsx` (root render)
- Test: `frontend/src/components/chat/__tests__/ChatInput-attach.test.tsx` (extend)

**Step 1: Failing test for A1.** In `ChatInput-attach.test.tsx` add a test that queries both file inputs (`getByLabelText(/attach file/i)`, `getByLabelText(/attach image/i)`) and asserts each is focusable: `expect(input).not.toHaveClass('hidden')` and `input.focus(); expect(document.activeElement).toBe(input)`. Copy the render/mocks pattern already in that file.

**Step 2: Run** `pnpm exec vitest run src/components/chat/__tests__/ChatInput-attach.test.tsx` → FAIL (class `hidden`, focus does not land).

**Step 3: A1 fix.** Both `<input type="file" className="hidden" ...>` → `className="sr-only"`. Move the `aria-label` from the `<label>` onto the `<input>` (`aria-label="Attach file"` / `"Attach image"`) so the accessible name is on the focusable control; keep the `<label htmlFor>` wrapper for the click target.

**Step 4: Run test → PASS.**

**Step 5: A3.** `tool-fallback.tsx:161` trigger className: replace `outline-none` with `focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40` (match `reasoning-panel.tsx:45`).

**Step 6: A8.** `ChatMessageList.tsx` scroll-to-bottom button: add `aria-label="Scroll to newest messages"`; change `w-9 h-9` to `min-w-11 min-h-11` (44px) keeping the icon size.

**Step 7: A14.** `ChatMessageList.tsx:243` `scrollToBottom`: read `useReducedMotion()` from framer-motion at component top (already imported in this file per ledger A5 list; if not, import it) and pass `behavior: reduced ? 'auto' : 'smooth'`.

**Step 8: A10.** `ProjectPickerPopover.tsx:127` error `<div>` → add `role="alert"`.

**Step 9: DS8.** `AuiMessage.tsx:388` textarea: `focus:outline-none` → `focus-visible:outline-hidden`.

**Step 10: A5.** In `chat-layout-client.tsx`, wrap the outermost returned JSX in `<MotionConfig reducedMotion="user">…</MotionConfig>` (`import { MotionConfig } from 'framer-motion'`). Copy the exact pattern from whichever dashboard route already does this: `grep -rn 'MotionConfig' frontend/app frontend/src`. Leave the per-component `useReducedMotion` calls as they are.

**Step 11: Gates, commit** `fix(chat): keyboard-reachable attach, focus rings, aria labels, reduced motion (A1 A3 A5 A8 A10 A14 DS8)`, push, PR.

---

### Task 2: Command palette (branch `fix/chat-command-palette`)

Closes: A4, S1, DS9.

**Files:**
- Modify: `frontend/app/(dashboard)/chat/chat-layout-client.tsx` (~60-100 commands list, ~187-260 palette JSX, ~441-452 ⌘K handler, ~528-544 onExecute)
- Test: `frontend/app/(dashboard)/chat/__tests__/` (find the existing `ChatLayoutClient.*.test.tsx` under `src/components/chat/__tests__/` and add `ChatLayoutClient.palette.test.tsx` beside it, reusing its mocks)

**Step 1: Read** the palette block fully. Decide nothing; apply below.

**Step 2: S1, dead entries.** For `upload`, `collection`, `settings` add real branches in the `onExecute` switch: `upload` → `router.push('/documents/upload')` (verify the route exists: `ls frontend/app/(dashboard)/documents`), `collection` → `router.push('/collections/new')` if that route exists else `router.push('/collections')`, `settings` → `router.push('/settings')`. If a target route does not exist at all, DELETE that palette entry instead of leaving a dead one. Record which you did in the PR body.

**Step 3: Failing test.** Render the layout, open the palette via `fireEvent.keyDown(window, { key: 'k', metaKey: true })`, assert `screen.getByRole('dialog', { name: /command/i })` exists, the input has an accessible name, and `screen.getAllByRole('option').length === visible entries`. Run → FAIL.

**Step 4: A4 semantics.** On the palette root `motion.div`: `role="dialog"`, `aria-modal="true"`, `aria-label="Command palette"`. Input: `aria-label="Search commands"`, `placeholder="Type a command"`, `role="combobox"`, `aria-expanded="true"`, `aria-controls="chat-command-list"`, `aria-activedescendant={activeId}`. List container: `id="chat-command-list"`, `role="listbox"`. Each entry: `role="option"`, `id={`cmd-${entry.id}`}`, `aria-selected={index === selectedIndex}`.

**Step 5: A4 focus management.** Check whether `@radix-ui/react-dialog` or `focus-trap-react` is already a dependency (`grep -E 'radix-ui/react-dialog|focus-trap' frontend/package.json`). If radix dialog is present (it is a shadcn project; `src/components/ui/dialog.tsx` likely exists), do NOT hand-roll: replace the `motion.div` overlay root with `Dialog`/`DialogContent` from `src/components/ui/dialog.tsx` (keeps trap, Escape, focus restore), keeping the inner list markup. If not present: store `document.activeElement` on open, `.focus()` the input on open via `useEffect`, restore on close, and add a `keydown` handler on the root that keeps Tab inside (first/last focusable wrap).

**Step 6: DS9.** `'⚡ Quick Actions'` → `'Quick actions'`, `'🔗 Navigate'` → `'Navigate'`.

**Step 7: Run test → PASS. Gates, commit** `fix(chat): command palette dialog semantics, focus trap, wire dead entries (A4 S1 DS9)`, push, PR.

---

### Task 3: Honest states in the rail and panels (branch `fix/chat-honest-states`)

Closes: S2, S3, S4, S5, S6, S7, A11.

**Files:**
- Modify: `frontend/src/components/chat/artifact-panel/WorkingFoldersPanel.tsx` (~30, ~155)
- Modify: `frontend/src/components/chat/artifact-panel/ProgressPanel.tsx` (~60-99)
- Modify: `frontend/src/components/chat/artifact-panel/ContextPanel.tsx` (~90-137)
- Modify: `frontend/src/components/chat/artifact-panel/ArtifactPanel.tsx` (~133-155)
- Modify: `frontend/src/components/chat/ChatSidebar.tsx` (~193-218, ~257-265)
- Test: add `frontend/src/components/chat/artifact-panel/__tests__/WorkingFoldersPanel.test.tsx` (find the nearest existing test for a rail panel and copy its mock of `useProjectWorkingFolders`)

(If the artifact-panel files live elsewhere, locate with `grep -rln 'WorkingFoldersPanel' frontend/src`.)

**Step 1: Failing test for S2.** Mock `useProjectWorkingFolders` to return `{ documents: [], notes: [], drafts: [], isLoading: true, errors: {} }` → expect a loading indicator (`getByRole('status')`) and NOT the text `/no files yet/i`. Second case: `isLoading: false, errors: { documents: new Error('500') }` → expect `getByRole('alert')` with text `/couldn't load/i` and a button `/retry/i`. Run → FAIL.

**Step 2: S2 fix.** Destructure `isLoading`, `errors`, and the refetch function the hook exposes (read `useProjectWorkingFolders.ts:88-100`; if no refetch is returned, return `refetch` from the hook by combining the underlying queries' `refetch`s). Render order: loading → `<div role="status" aria-live="polite">` with the existing skeleton style; any error → `<div role="alert">` "Couldn't load project files." + `<button>Retry</button>` calling refetch; else the empty state. Empty-state copy: `"No files yet. Cited sources will appear here."` (DS7 for this file lands here too, note it in the PR).

**Step 3: S5.** `ProgressPanel.tsx:66` `bg-(--error-red)` → `bg-(--nous-mars)`; `:99` `var(--error-red)` → `var(--nous-mars)`.

**Step 4: A11.** In the same rows add a visually hidden status: after the indicator span add `<span className="sr-only">{row.error ? 'Failed' : row.active ? 'In progress' : row.done ? 'Done' : 'Pending'}</span>` (adapt to the row's real field names). Give the `active` row a non-colour cue: `font-medium`.

**Step 5: S4.** `ContextPanel.tsx`: delete the hardcoded "Web search / Not connected" and "Agent tools / Ready" connector rows and the `${activeCount}/${totalCount}` badge derived from them. Keep only rows fed by real props/state. If nothing real remains, remove the "Connectors" sub-section entirely. Do not invent a status API.

**Step 6: S6.** `ArtifactPanel.tsx` `ArtifactContentError`: add a `Retry` button that calls the panel's refetch (find how `DocumentInlineViewer.tsx:164-174` wires its Retry; reuse the same query `refetch`). Copy: `"Couldn't load this document. It may have been deleted."` + Retry.

**Step 7: S3.** `ChatSidebar.tsx:193-218` workspace switcher `<button>` with no `onClick`: replace the `<button>` with a non-interactive `<div>` showing the workspace name, remove the `ChevronDown` and `aria-label="Select workspace"`. Do not build a picker.

**Step 8: S7.** `ChatSidebar.tsx:257-265`: remove the `⌘K` `<kbd>` from the thread search input.

**Step 9: Run test → PASS. Gates, commit** `fix(chat): honest loading/error states in rail, remove dead and fake controls (S2 S3 S4 S5 S6 S7 A11)`, push, PR.

---

### Task 4: Tokens, contrast, rail typography (branch `fix/chat-design-tokens`)

Closes: A2, DS1, DS2, DS3, DS4, DS5, DS6, DS7.

**Files:**
- Modify: `frontend/src/components/assistant-ui/retrieval-chunks.tsx` (~53, 79, 87, 88, 94, 99)
- Modify: `frontend/src/components/assistant-ui/tool-fallback.tsx` (~258, 264, 290)
- Modify: `frontend/src/components/assistant-ui/message-timing.tsx` (~38)
- Modify: `frontend/src/components/chat/CitationLink.tsx` (~110-115)
- Modify: `frontend/src/components/chat/artifact-panel/CollapsibleCard.tsx` (~48-56), `ProjectBindingCard.tsx` (~35-56, ~75), `ContextPanel.tsx` (~33, 79, 103-111)
- Modify: `frontend/src/components/chat/CitationPanelBody.tsx` (~363, 380, 428, 434) and any other mono label call sites the ledger DS1 row lists (read the full row in the ledger)
- Modify: `frontend/src/components/chat/JobsIndicator.tsx` (~111, ~181)
- Modify: `frontend/src/components/ui/button.tsx` (~19-31), `frontend/src/components/ui/card.tsx` (~21)
- Modify: em-dash copy sites from ledger DS7: `ProjectPickerPopover.tsx:95`, `AuiMessage.tsx:642`, `ArtifactPanel.tsx:334`, plus the rest of that row (read it).

**Step 1: A2.** Replace alpha ramps with tokens: `text-foreground/55` and `/45` → `text-(--nous-fg-2)`; `/30` and `/35` → `text-(--nous-fg-3)`. Both tokens are AA on `--nous-bg-1` in light and dark (fg-3 is documented AA-safe in `nous-tokens.css`).

**Step 2: DS3.** `text-blue-500 dark:text-blue-400` → `text-(--nous-fg-accent-safe)`; `bg-blue-500/70 dark:bg-blue-400/70` → `bg-(--nous-sol)/70`; `text-emerald-600 dark:text-emerald-400` → `text-(--nous-terra)`; `CitationLink.tsx:110-115` emerald → `--nous-terra`. Read the full DS3 ledger row for the remaining call sites and map each to the nearest semantic token (success→terra, warning→corona, error→mars, info/accent→sol-safe).

**Step 3: DS4.** `ProjectBindingCard.tsx:39-43` delete the `w-[2px]` side-stripe span. Bound state stays signalled by the existing text/icon; if nothing else signals it, set the card `border-(--nous-sol)/40` instead.

**Step 4: DS5.** Replace literal `rgba(212, 160, 57, …)` / `rgba(232,184,74,…)` / `rgba(10,10,14,…)` with `rgba(var(--nous-sol-rgb), …)` etc. at the listed sites. Byte-identical colours, no visual change.

**Step 5: DS6.** `JobsIndicator.tsx:111` and `retrieval-chunks.tsx:99`: width-animated bars → fixed `w-full` inner bar with `style={{ transform: `scaleX(${pct/100})`, transformOrigin: 'left' }}` and `transition-transform duration-500` (copy `ChatInput.tsx:523`). Replace `transition-all` on the interactive chrome the row lists with `transition-colors`.

**Step 6: DS1 + DS2, rail headers.** In `CollapsibleCard.tsx:48-56` and `ProjectBindingCard.tsx:48-56`: drop `fontFamily: var(--nous-font-mono)`, `uppercase`, `letterSpacing 0.18em`, `text-[10px] font-bold`; use `text-xs font-semibold text-(--nous-fg-2)` sentence case (one style, defined once in `CollapsibleCard`; `ProjectBindingCard` reuses it, no second copy). `ContextPanel.tsx:103-111` nested "Connectors" header: if the section survives Task 3, make it `text-xs text-(--nous-fg-3)` non-uppercase. Other DS1 mono sites (sources search input, sort control, footer, citation chips): remove the mono `fontFamily` and let them inherit UI font; leave mono only where the content is code or an identifier.

**Step 7: DS7.** Rewrite each em-dash string: `"Showing the last known state. {error}"`, `"Network error. Please try again."`, `"The service is busy. Try again in a moment."`, `"Unpin to let the agent change this view"`, and the rest per the ledger row. `WorkingFoldersPanel.tsx:155` is handled in Task 3; skip it here if Task 3 already merged, otherwise fix and note the overlap.

**Step 8: Gates.** Run existing tests touching these files: `pnpm exec vitest run src/components/chat src/components/assistant-ui` and fix any snapshot/text assertion that broke only because of the copy change. Commit `fix(chat): AA contrast tokens, remove off-palette colours and mono kickers (A2 DS1-DS7)`, push, PR.

---

### Task 5: Touch and small-screen (branch `fix/chat-touch-mobile`)

Closes: A6, A7, A13, A15, A16, A17, R1, R3.

**Files:**
- Modify: `frontend/app/globals.css` (~450-469 `.nous-msg-actions`, `.nous-msg-action`)
- Modify: `frontend/src/components/chat/aui/AuiMessage.tsx` (~232-233 `autohide`)
- Modify: `frontend/src/components/chat/ChatSidebar.tsx` (~282, ~313-318, ~376-410, ~464-485)
- Modify: `frontend/src/components/chat/artifact-panel/ArtifactPanel.tsx` (~297), `AllCitationsPanel.tsx` (~31-36), `WorkingFoldersPanel.tsx` (~111), `FileRow.tsx` (~33)
- Modify: `frontend/src/components/chat/ChatInput.tsx` (~504-510, ~696)

**Step 1: A7.** `globals.css` after the `.nous-msg-actions` rule add `@media (hover: none) { .nous-msg-actions { opacity: 1; } }`. `AuiMessage.tsx:232-233` `autohide="always"` → `autohide="not-last"` (assistant-ui ActionBar option; verify the prop's accepted values in `node_modules/@assistant-ui/react` before using; if unsure, use `autohide="never"` under a `(hover: none)` media query check via `window.matchMedia` in a `useEffect`).

**Step 2: A13.** `.nous-msg-action` → `min-width: 44px; min-height: 44px` under `@media (pointer: coarse)` only (keep 28px on desktop). Sidebar rename/delete `p-1` → `p-2.5` under `md:p-1`; select-mode exit `w-5 h-5` → `min-w-11 min-h-11 md:w-5 md:h-5`; filter chips `py-[3px]` → `py-2 md:py-[3px]`.

**Step 3: A6.** `ChatSidebar.tsx:376-410`: the checkbox must not be inside the `<button>`. Make the row a `<div className="flex">` with the `<input type="checkbox">` as first child (own `aria-label`, `onClick` stopPropagation) and the existing `<button>` as second child for the row action. Keep classes so the row looks identical.

**Step 4: A15.** Unread dot: add `<span className="sr-only">Unread</span>` next to it and `aria-hidden` on the dot.

**Step 5: A16.** `AllCitationsPanel.tsx:31-36`: add `aria-hidden` on the icon and a `<span className="sr-only">{external ? 'External source' : 'Internal source'}</span>`; also set `title` to the same text.

**Step 6: A17.** `WorkingFoldersPanel.tsx:111` `'📌'` → pass `meta: n.isPinned ? 'Pinned' : undefined` and in `FileRow.tsx:33` render meta as `text-[11px] text-(--nous-fg-3)`. No emoji.

**Step 7: R1.** `ArtifactPanel.tsx:297` add `max-lg:pb-[env(safe-area-inset-bottom)]`.

**Step 8: R3.** `ChatInput.tsx:504-510`: show the counter on all sizes once `length >= limit * 0.9` (`hidden sm:inline` → `${nearLimit ? 'inline' : 'hidden sm:inline'}`), with `aria-live="polite"`.

**Step 9: Gates** (`pnpm exec vitest run src/components/chat/__tests__/ChatInput-attach.test.tsx src/components/chat/__tests__/ChatHeader.test.tsx` plus any sidebar tests), commit `fix(chat): touch targets, hover-only actions, safe area, non-colour cues (A6 A7 A13 A15-A17 R1 R3)`, push, PR.

---

### Task 6: Overlays and tablet layout (branch `fix/chat-overlays-tablet`)

Closes: A9, A12, R2.

**Files:**
- Modify: `frontend/src/components/chat/ChatHeader.tsx` (~62-71, ~157-200)
- Modify: `frontend/src/components/chat/artifact-panel/ArtifactPanel.tsx` (~297-299)
- Modify: `frontend/app/(dashboard)/chat/chat-layout-client.tsx` (~477, ~513)
- Test: `frontend/src/components/chat/__tests__/ChatHeader.test.tsx` (extend)

**Step 1: A9, failing test.** Open the export menu via the trigger, assert `getByRole('menu')` and that `Escape` closes it and focus returns to the trigger. Run → FAIL.

**Step 2: A9 fix.** Use the existing `src/components/ui/dropdown-menu.tsx` (Radix) if present: `DropdownMenu`/`DropdownMenuTrigger asChild`/`DropdownMenuContent`/`DropdownMenuItem`, delete the hand-rolled `mousedown` click-away. Radix gives `aria-haspopup`, `aria-expanded`, `role="menu"`, arrow keys, Escape, focus restore. If the ui dropdown does not exist, add the attributes and Escape/focus-restore by hand (same pattern as Task 2 Step 5 fallback).

**Step 3: Run test → PASS.**

**Step 4: A12.** Below `lg`, render the artifact sheet with `Sheet`/`Drawer` from `src/components/ui/` if one exists (Radix Dialog based: backdrop, trap, Escape, restore). If none exists, add a backdrop `<div className="lg:hidden fixed inset-0 bg-(--nous-erebus)/40 z-40" onClick={close} aria-hidden>` and move focus into the sheet on open, restore on close; set `role="dialog" aria-modal="true"` only in the `max-lg` case.

**Step 5: R2.** Give tablets a rail: change the docked rail from `hidden lg:flex` to `hidden md:flex` with `md:w-[280px] lg:w-[320px]`, and make the artifact split `md:w-[min(40vw,560px)] lg:w-[min(45vw,640px)]`. The overlay rail at `:513` stays `lg:`. Verify nothing overflows at 768px by reasoning about the sum: sidebar (if open) + rail 280 + artifact 40vw must leave ≥ 360px for the transcript; if it doesn't, collapse the sidebar automatically when the artifact opens at `md`.

**Step 6: Gates, commit** `fix(chat): export menu semantics, artifact sheet focus, tablet rail (A9 A12 R2)`, push, PR.

---

## Out of scope (say so in the final report)

- No browser rendering in this plan. Each PR body must list what was not visually verified (contrast, breakpoints, sheet backdrop).
- The 8 barrel-exported components never rendered on /chat (`RAGToggle`, `CitationPanel`, `CitationPreview`, `ChatSettings`, `ChatAnalytics`, `ModelLoadingProgress`, `SearchComposer`, `ui/agent-plan`) are untouched.

## Amendment (2026-10-09)

Added when this plan was committed (PR #1958). The tasks above are kept as
written; these notes correct them and point to the current code.

- Task 2, Step 2: unquoted parentheses are shell syntax, so
  `ls frontend/app/(dashboard)/documents` fails before `ls` runs. Quote the
  path: `ls 'frontend/app/(dashboard)/documents'`.

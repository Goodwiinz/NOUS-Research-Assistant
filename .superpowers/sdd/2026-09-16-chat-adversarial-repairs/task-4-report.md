# Task 4 implementation report

Task 4 repairs responsive chat controls, thread-search reachability, and the
remaining accessibility faults. The visual route now composes the production
`SidebarLayout`, `AppRail`, `ChatSurface`, `ChatHeader`, `ChatInput`, and
`ContextRail` behind the existing `NEXT_PUBLIC_VISUAL_TEST_FIXTURES` gate, so
the root browser matrix can exercise the intermediate columns and drawer.

## Behavior delivered

- The slash menu measures the current composer anchor against the visual
  viewport, caps its height to the available space above the composer, updates
  on anchor/viewport resize, and scrolls the highlighted command into view.
  The menu remains an internal scroll region on short viewports.
- Thread search trims surrounding whitespace before matching. Older-thread
  pagination remains available during filtering and explains that search covers
  loaded threads while older pages are fetched. The New chat shortcut uses the
  stronger contrast token.
- The rename input has the accessible name `Thread name`. The composer
  textarea keeps listbox autocomplete semantics without the unsupported
  `aria-expanded` attribute.
- Artifact Escape handling is a document bubble guard that checks
  `defaultPrevented`, the original composed event path (including a Radix
  dialog whose state changed before its portal was removed), and the live open
  overlay query. A standalone Escape still closes the artifact panel.
- The gated visual fixture seeds completed provenance/reasoning/tool state and
  live planner/provider state, includes a real context rail at `md` widths,
  uses the production drawer with the composer focus ref, and exposes delayed
  attachment success/failure settlement for browser races. Its send preflight
  is side-effect free so one visual send creates one turn.
- The visual E2E expectation follows the production history docking breakpoint
  at 1280px. It fills the initially empty composer and uses a bounded
  target-complete 60-tab traversal so the permanent app rail and mobile drawer
  controls do not make the reachability check flaky.

## Focused evidence

The final focused Node 24.21.0 run passed **5 files and 59 tests**:

```text
ChatDialogs.ime.test.tsx                 3
SlashCommandMenu.test.tsx                2
ChatInput-streaming.test.tsx            17
ArtifactPanel.test.tsx                  17
ChatSidebar.test.tsx                    20
```

The ArtifactPanel regression mounts the Radix dialog before the artifact panel,
dispatches Escape through the actual dialog lifecycle, removes the dialog
during dispatch, and verifies the underlying artifact remains open while the
event is not default-prevented. Standalone panel Escape and a live overlay
guard remain covered as well.

The seven behavior mutations are recorded in
[`docs/testing/chat-adversarial-task4-mutations.md`](../../../docs/testing/chat-adversarial-task4-mutations.md).
Each was killed and restored; the A13 test was strengthened after the initial
version also passed through the default-prevented path.

## Static and quality gates

All final gates ran under Node 24.21.0:

- `pnpm --dir frontend type-check`: passed.
- Prettier check over the fixture, visual E2E, changed production files, and
  focused tests: passed.
- Changed-file ESLint: **0 errors**, 13 existing warnings.
- Full `app`/`src` ESLint JSON plus
  `check_frontend_quality.mjs`: passed within the committed baseline of 116
  errors and 2005 warnings.
- Visual E2E ESLint: passed.
- `git diff --check`: run before commit.

## Browser scope and limits

The root agent owns the final tracked browser run against its private local
server, including the 320/375/390/768/800/900/960/1023/1024/1279/1280/1440
pointer matrix, 390×520 slash bounds, keyboard reachability, selected-menu axe
contrast, and drawer/context-rail checks. Preliminary root feedback found the
full composition and 13 viewport pointer checks working, then identified and
prompted the selected-row contrast repair; that feedback is not claimed as the
final frozen browser result here.

No server, account credentials, auth bypass, or external model call was added.
Root-authored audit and plan documents remain untracked for the final docs
commit.

## Changed files

- `frontend/app/visual-test/chat-mobile-controls/ChatMobileControlsFixture.tsx`
- `frontend/e2e/visual/chat-mobile-controls.spec.ts`
- `frontend/src/components/chat/ChatDialogs.tsx`
- `frontend/src/components/chat/ChatHeader.tsx`
- `frontend/src/components/chat/ChatInput.tsx`
- `frontend/src/components/chat/ChatSidebar.tsx`
- `frontend/src/components/chat/SlashCommandMenu.tsx`
- `frontend/src/components/chat/__tests__/ChatDialogs.ime.test.tsx`
- `frontend/src/components/chat/__tests__/ChatInput-streaming.test.tsx`
- `frontend/src/components/chat/__tests__/ChatSidebar.test.tsx`
- `frontend/src/components/chat/__tests__/SlashCommandMenu.test.tsx`
- `frontend/src/components/chat/artifact-panel/ArtifactPanel.tsx`
- `frontend/src/components/chat/artifact-panel/__tests__/ArtifactPanel.test.tsx`
- `docs/testing/chat-adversarial-task4-mutations.md`
- this report

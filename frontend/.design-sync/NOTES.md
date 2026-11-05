# design-sync notes (NOUS frontend)

Repo-specific facts for the `/design-sync` skill. Read before any re-sync.

## Shape: a wrapper package around an app

- `frontend/` is a Next.js app, not a published package: no `dist/`, no
  `.d.ts`, Tailwind v4 *source* CSS. `.design-sync/pkg/` stands in for the
  package (`@nous/design-system`, `window.Nous`). `cfg.entry` points at its
  generated barrel; every `cfg.*` path is relative to `.design-sync/pkg/`
  (the converter's PKG_DIR is the entry's nearest named `package.json`).
- Run everything from `frontend/`. `--node-modules ../node_modules`: the
  workspace uses `node-linker=hoisted`, so react/next live at the repo root.
- `cfg.buildCmd` regenerates every generated input, in order:
  1. `gen-entry.mjs` -> `pkg/index.ts` (barrel) + `pkg/docs/<Name>.md`
     (category-only stubs that set the DS-pane group; `cfg.docsDir: docs`).
     Discovery = PascalCase component-shaped exports under
     `app/components/nous`, `src/components/ui`, `src/components/elements`,
     `app/components`, `src/components` (first root wins name collisions;
     collisions are listed in the barrel header).
  2. `build-types.mjs` -> `pkg/types/` via the repo's tsc, then rewrites `@/`
     aliases to relative paths (the converter's ts-morph has no `paths`).
  3. `build-css.mjs` -> `pkg/styles.css` (Tailwind compiled over
     `app/globals.css` + `app/nous-tokens.css`, `@source` = component dirs +
     `.design-sync/previews`), `pkg/katex-shim.css`, `pkg/tsconfig.bundle.json`.
  All three outputs are gitignored. Re-run buildCmd before the converter.
- `pnpm install` in a shared checkout can abort with
  `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY`; this sync ran in its own
  worktree with `CI=true pnpm install --frozen-lockfile --filter "multimodal-rag-frontend..."`.

## Converter workarounds (config/data only, no lib forks)

- `tsconfig.bundle.json` (cfg.tsconfig) exists because the converter's
  esbuild paths plugin (a) reads only the file's own `paths` (no `extends`),
  and (b) tries the bare stem before `/index.ts`, so `@/components/chat`
  resolved to the directory ("is a directory"). It lists an exact key for
  every index-bearing dir (and `<dir>.ts` siblings, e.g. `@/types/api` ->
  `src/types/api.ts`) ahead of the app's wildcards.
- `katex/dist/katex.min.css` -> `katex-shim.css` (woff2-only @font-face): the
  converter's esbuild has no `.ttf` loader.
- `pdfjs-dist` -> `stubs/pdfjs-dist.ts`: the real runtime (~1.1 MB) pushed
  `_ds_bundle.js` past the 12 MB upload cap. All app uses are `import type` or
  a lazy `await import()` in the PDF viewers, which land in their own error
  state with the stub.
- `CitationGraph` is excluded from the barrel (cytoscape + layout-base,
  ~1.2 MB, only that canvas uses it). Bundle sits at ~11.1 MB of 12 MB - any
  new heavy dependency in a component trips `[FILE_TOO_LARGE]`; re-measure.
- `env-shim.ts` (first `extraEntries`) defines `process.env` for the browser
  bundle. All backend URLs point at `https://design-preview.invalid` (never
  resolves) - previews and designs must never reach the real dev API or
  Supabase. Keep it that way.
- `provider.tsx` exports `NousProvider` (cfg.provider): next-themes (forced,
  default dark like `app/providers.tsx`), Next app-router/pathname/search-param
  contexts (a no-op router - `useRouter()` throws without them), React Query
  (no retries), the app's real `AuthProvider`, Radix `TooltipProvider`.

## Re-sync risks

- `provider.tsx` mirrors `app/providers.tsx` by hand; a new app-level
  provider there (context a component reads) needs adding here too, or those
  previews go blank.
- `env-shim.ts` lists env names by hand; a new module-level `NEXT_PUBLIC_*`
  read that throws on empty needs a placeholder.
- Fonts: `pkg/fonts.css` (committed) holds the Google Fonts `@font-face`
  rules standing in for `next/font/google` in `app/layout.tsx` (Inter,
  JetBrains Mono, Outfit, Source Serif 4); the woff2 files load from
  fonts.gstatic.com at runtime. Do NOT switch back to a remote `@import url()`:
  the converter appends cssEntry after the component CSS, so the @import lands
  mid-file and browsers drop it (fonts silently fall back). A family added to
  layout.tsx needs fonts.css re-fetched (URL in build-css.mjs) + its
  `--font-*` variable in build-css.mjs.
- Bundle size headroom is under 1 MB (see above).

## Known render warns

- `.tool-shimmer` labels (ShimmerLabel Running/Retrieving, SwapLabel
  ToolRunning, ReasoningPanel Streaming, RetrievalChunks Searching) capture
  blank: under `prefers-reduced-motion: no-preference` the text is
  transparent with a moving gradient band, and the capture frame lands with
  the band off the text. Verified with `reducedMotion: 'reduce'` that each
  paints correctly; graded good. Not a bug - don't chase.
- framer-motion entrances (`initial={{ opacity: 0 }}`) stay at opacity 0
  under the capture's fixed page clock: WelcomeState EmptyThread is blank,
  ChatBubble Thinking loses its ThinkingPill, ChatInlinePlan Streaming loses
  its shimmer header; likewise EmptyState, Plan, NewChatDialog. The fixed
  clock puts `performance.now()` ~0.9 s ahead of `document.timeline` on the
  `?story=` navigation (batch B probe). All paint in a live render (no fixed clock, 2.5 s
  wait); graded good. The root fix would be `reducedMotion: 'reduce'` in
  `package-capture.mjs` (the components honour `useReducedMotion()`), but
  that's a skill lib, not config - don't fork it.
- `[RENDER_BLANK]` VirtualizedConversationList, VirtualizedMessageList:
  unauthored, render nothing without data + measured layout. The floor card
  can't swap in because next-themes injects a `<script>` into the root, so
  the root is never "empty". Out of the Core preview scope; author previews on
  a later sync if wanted. The same script trick hid 5 others (fixed by
  authoring ProgressBar, CellCitation, ChatMessageActions, ToolExecutionCard;
  excluding unused SidebarToggle).
- `[TOKENS_MISSING]` --cyan, --purple, --tw, --collapsible-panel-height,
  --radix-collapsible-content-height: runtime/Radix-set or dead references in
  app CSS; harmless.
- AppRail shows no active item in its plain stories: `cfg.provider` props
  are global and NousProvider's `pathname` defaults to `/`. The `ActiveRoute`
  story nests `<NousProvider pathname="/chat">` to show the active state.
  Don't nest `theme="dark"` that way: next-themes sets the class on `<html>`,
  and the picker mounts every story of a card on one page.

## Preview authoring gotchas

- Arbitrary Tailwind classes used only in a preview exist only after
  `build-css.mjs` re-runs (`@source '../previews'`). Run buildCmd before the
  converter whenever previews change.
- Assistant prose needs the `nous-chat-body` (Source Serif) + `nous-prose`
  wrappers; ChatMarkdown cites via the `components` prop overriding `a` with
  CitationLink (`[Doc N]` -> `#nous-citation-N`), as `CitationRenderer` does.
- ReasoningPanel is a Radix Collapsible: pass `defaultOpen` to show steps.
  Pagination returns null at `totalPages <= 1`.
- `--nous-terra` renders green-teal (RetrievalChunks high score) - token
  value, not a bug.
- Cells clip around 520 px of content; keep multi-bubble stories short.
- Sub-part previews must declare a local `export const` (a bare re-export is
  not detected).
- `cfg.storyImports.bundle: [".design-sync/previews/"]` is load-bearing: the
  converter's import policy (rule 2) shims any import whose resolved basename
  is an exported component, so `import { X } from './Dialog'` compiled to
  `window.Nous.X` (undefined -> blank cell; the error only shows in
  `.cache/review/<Name>.json` `pageErrs`). Batches C/D composed sub-parts
  inline; B and DialogTitle use sibling-story wrappers, which rely on this.
- `Sidebar` is pinned to `ui/sidebar.tsx` in `componentSrcMap` (fuzzy-find
  picked the excluded `layout/Sidebar.tsx`).
- `Switch` shows its on state only from controlled `checked`;
  `defaultChecked` renders grey (real app quirk). A disabled `Slider` looks
  identical to an enabled one (batch C added `opacity-50` in that story).
- `DropdownMenuSub defaultOpen` closes on mount - use controlled `open`.
  ScrollArea needs `type="always"` to show bars statically. Carousel arrows sit
  outside the track (leave ~56 px side padding).
- ConfirmDialog / DeleteConfirmDialog keep open state internal (no prop), so
  their cards show the trigger only.
- Sidebar single cards lose ~24 px at the bottom: the desktop sidebar is
  `fixed h-svh` and the card harness hardcodes `body{padding:24px}`
  (lib/emit.mjs). No config knob; accepted.
- Review sheets are scaled ~0.8x; widths look smaller than they are.
- `H1`-`H4` can't get cards: `isComponentName` drops ALL-CAPS names twice
  (source-kit and package-build), and componentSrcMap only fixes the first.
  They still ship on `window.Nous`; the heading-scale story is `P`'s
  `ResearchArticle`. Don't re-add `H*.tsx` previews.
- `[GRID_OVERFLOW]` components are on `cardMode: "column"` in config
  (form controls, Card, Tabs, Breadcrumb, Plan, Pagination, NousNav, ...).
- `Dark` stories (Button, Card, Badge, Alert, EmptyState, Table,
  DocumentCard, ChatBubble, ChatMarkdown) wrap the story in
  `previews/_DarkSurface.tsx`. A bare `.dark` div does NOT work: globals.css
  maps `--color-background: hsl(var(--background))` in a non-inline `@theme`,
  so the `--color-*` tokens resolve once at `:root` and only the raw
  `--nous-*` vars flip. The helper re-declares the 19 `--color-*` tokens on
  the wrapper. It is not a component; the build logs it as a "stale preview"
  (harmless). If globals.css adds a colour token, add it to the helper.
- `conventions.md` is the `readmeHeader`; every class/var/export it names was
  checked against the build - re-check when renaming tokens.

## Scope decisions

- `Navigation` (app/components/Navigation.tsx) is excluded in gen-entry:
  legacy pre-NOUS nav, imported nowhere, renders null without a session.
- `CitationPreview` is excluded in gen-entry: unmounted, hardcoded Tailwind
  blue chips; CitationLink is the live citation component.
- `AgentPlanPanel` has no preview (floor card): prop-less, reads
  `useAgentChatStore`, returns null offline. `Plan` (ui/agent-plan) renders the
  same content from props.
- `DocumentCard` and SearchComposer's Stop button hardcode Tailwind
  red/blue/green file-type colours - component-intrinsic; previews are faithful.

## Upload target moved to Claude (Nov 5, 2026)

- Project fa7828f2 ("NOUS Frontend DS") was moved by Claude Design into a
  Claude Design System artifact while the first upload was running:
  https://claude.ai/code/artifact/a01801b6-eca2-4780-8a0c-91ff5fad56ad
  The server now refuses `write_files` to fa7828f2 ("This design system moved
  to Claude. /design-sync can't send updates to Claude yet").
- The artifact's snapshot (19:26:53Z) holds only the base files (README,
  `_ds_bundle.js/.css`, `_vendor/`, `fonts/fonts.css`) and the first 256
  `guidelines/docs/*.md` stubs: no `components/`, no `_preview/`, no
  `_ds_sync.json`. Its index has `"editing": "off"` and a `claude-design` source.
- Decision: wait until `/design-sync` can publish into Claude, then re-sync
  into that artifact. Do not retry uploads to fa7828f2, and do not hand-convert
  the bundle into the artifact unless the user asks. The local `ds-bundle/` is
  the verified build to send (482 components, verdict ok).

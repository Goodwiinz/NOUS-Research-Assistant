# DESIGN.md — NOUS

Design system reference for the NOUS frontend (`frontend/`). Source of truth for tokens: `frontend/app/nous-tokens.css` (the `--nous-*` brand layer) and `frontend/app/globals.css` (shadcn `--background`/`--foreground`/etc. semantic layer). Tailwind maps both in `frontend/tailwind.config.ts`.

## Register

Two registers coexist in one codebase:

- **Product** (default): authenticated app surfaces. Dashboard, chat, documents, search, settings, entities, analytics. Design SERVES the task. Restrained color, familiar patterns, density where users need it, shadcn/Radix components. Most of `src/components/**` and `app/(dashboard)/**`.
- **Brand**: the marketing front door. Landing (`app/page.tsx` + `src/components/landing/**`) and auth (`app/(auth)/**`). Design IS the product. Committed color, bolder type, one decisive idea per fold.

Identity is **scholarly, warm, confident**, not cyberpunk and not SaaS-cream. NOUS = Greek νοῦς (mind/intellect). A prior terminal/phosphor-green "costume" was removed; do not reintroduce it (`--terminal-*`, `--phosphor-green`, glitch text, scanlines, spinning HUD rings, tech tickers).

## Color

Planetary palette (`nous-tokens.css`). Warm gold is the identity accent.

**Brand anchors**

- Erebus `#0a0a0e` (near-black), Selene `#f7f7f5` (near-white)
- Sol `#d4a039` (gold, primary accent), Helios `#e8b84a` (gold hover/bright), Apollo `#f5d680` (soft gold), Aurum `#fdf6e3` (light gold wash)
- Sol-safe `#996d1a` (accessible gold for text on light)

**Semantic (theme-adaptive via `.dark`)**

- Foreground: `--nous-fg-1` (primary), `--nous-fg-2` (secondary), `--nous-fg-3` (tertiary), `--nous-fg-accent` (Sol light / Helios dark)
- Background: `--nous-bg-1`, `--nous-bg-2`, `--nous-bg-3`
- Border: `--nous-border-1`, `--nous-border-2`

**Dark warm-amber depth stack** (used for auth and other dark brand surfaces): Nyx `#141210` → Obsidian `#1e1b17` → Umber `#28241e` → Sepia `#332e26` → Dusk `#3d372e`. Text: Ivory `#f5f0e8` → Parchment `#c8bfa8` → Dust `#8a8070`. Border: Shade `#2a261f`.

**Status**: Terra `#34d399` (success), Corona `#f59e0b` (warning), Mars `#ef4444` (error).

**Rules**

- Never hardcode hex in components or `text-gray-*`/`bg-gray-*`. Use semantic tokens (`text-foreground`, `text-muted-foreground`, `border-border`) or `--nous-*` vars. (Graph/data-viz palettes are the only sanctioned hex.)
- Strategy: product = Restrained (tinted neutrals + Sol accent). Brand = Committed (Sol carries identity on warm-dark or Aurum-light ground).
- Small text on dark: use Parchment, not Dust (Dust on Nyx is borderline AA at small sizes).
- Never gradient text (`bg-clip-text`), never glassmorphism-as-default, never side-stripe accent borders.

## Typography

- **Inter** (`--nous-font-heading`, `--nous-font-ui`): headings, UI, labels, buttons, data. Loaded via `next/font` (`--font-inter`).
- **Source Serif 4** (`--nous-font-body`): body prose, value-prop sentences, scholarly voice. Optical sizing auto.
- **JetBrains Mono** (`--nous-font-mono`): code, tiny technical labels only. Loaded via `next/font` (`--font-mono`). Do not use mono as decorative "technical" shorthand.

Helper classes in `nous-tokens.css`: `.nous-display`, `.nous-h1/2/3`, `.nous-body`, `.nous-ui`, `.nous-caption`, `.nous-overline`, `.nous-mono`, `.nous-wordmark`. Prefer these or `font-[var(--nous-font-*)]` arbitrary classes over inline `style`.

Scale: fixed rem in product (≈1.125–1.2 ratio); larger display steps allowed in brand. One uppercase tracked kicker per section max (repeating it is AI scaffolding).

## Spacing, radius, elevation

- Radii: `--nous-radius-sm` 4px, `-md` 8px, `-lg` 12px, `-xl` 16px, `-2xl` 24px.
- Shadows: `--nous-shadow-sm/md/lg/xl`, `--nous-shadow-focus` (`0 0 0 3px rgba(212,160,57,0.25)`).
- Vary spacing for rhythm; avoid uniform padding everywhere. No cards-in-cards.

## Motion

- Ease-out: `--nous-ease-out` `cubic-bezier(0.16,1,0.3,1)`. No bounce/elastic.
- Product transitions 150–250ms, convey state not decoration. Brand may use one orchestrated entrance (staggered fade + small translate).
- Never animate layout properties (width/left/top); use transform/opacity.

## Components

- shadcn/ui + Radix primitives in `src/components/ui/**`. These carry baseline focus-visible / roles / keyboard. Prefer them over one-offs.
- Every interactive element ships default/hover/focus-visible/active/disabled/loading/error.
- Focus: `focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--nous-sol)]/40` (or `ring-ring`). Never bare `focus:outline-none`.
- Icon-only buttons require `aria-label`; decorative icons get `aria-hidden`. Canvas/visual elements get `role="img"` + label.
- Errors use `role="alert"`.

## Theming

- Light/dark via `.dark` class (`next-themes`). `--nous-*` semantic vars flip; brand anchors and the dark depth stack are absolute.
- The brand landing is intentionally always-light in the manuscript direction (Selene `#f7f7f5` ground, Aurum `#fdf6e3` band, Erebus ink), independent of user theme. The dark depth stack remains for auth and any dark brand surfaces.

## Anti-patterns (do not ship)

Gradient text · glassmorphism by default · side-stripe colored borders · hero-metric template (big number + label + gradient ×N) · identical icon-card grids · modal-first · em dashes in copy · `terminal-*`/phosphor cyberpunk theme · mono-everywhere · repeated uppercase tracked section labels · hardcoded hex / `text-gray-*`.

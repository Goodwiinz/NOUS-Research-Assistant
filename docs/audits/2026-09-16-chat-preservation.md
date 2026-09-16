# Chat preservation: implementation and provisional pre-flight

Status: signed-in local visual acceptance completed. Prepared for a draft PR;
not deployed. Full release certification is not claimed:
Lighthouse/performance and the broader backend CI environment remain unverified.

## Scope and design read

Research chat for researchers, preserving the editorial NOUS identity.
Taste v2 is the design-rule source. Preserve mode; typography, spacing and
neutral hierarchy only. Dials: DESIGN_VARIANCE 4, MOTION_INTENSITY 3,
VISUAL_DENSITY 6. Existing components and tokens, no added dependencies.

The production audit measured a 769px CSS viewport with 56px navigation,
260px history and 280px context, leaving 173px for the conversation and a
92px textarea. History now docks at xl (1280px); below that the existing
keyboard-operable drawer and its header trigger remain available. Context
rail behavior is unchanged. At 769px this recovers 260px for the transcript.
Signed-in local verification measured the textarea at 375px at the same
769px CSS viewport, compared with the original 92px.

Composer spacing is reduced, the Commands shortcut uses available composer
width rather than viewport width, and the textarea now has an accessible
Message label. Slash entry remains available at narrow widths. Source
toggle, attachment controls, field order, queue and stop behavior are unchanged.
History snippets/timestamps/filter labels are larger. Tool metadata retains
all existing values and disclosures but loses the nested card framing and
can wrap. No server requests, state ownership or persistence logic changed.

## Em-dash audit

PASS for newly authored visible UI strings: no em/en dashes added.
Existing source comments are not visible copy. Stored user messages, quoted
research and generated answers are content, not redesign copy, and are not
rewritten. The inspected changed controls and tool metadata add no em/en dashes.
Existing thread titles and message text retain their original punctuation.

## Section 14 pre-flight

PASS (source): design read, explicit dials, existing design foundation,
preserve mode and prior audit; brand accent/font/logo preservation; no new
theme overrides, animation dependencies, scroll listeners, effects, icons,
decorative imagery, invented numbers, copy register or layout abstractions.
Existing loading/empty/error states and runtime callbacks are preserved.

PASS (limited browser fixture): production header/composer render at 1280px
and 375px in dark mode. The 375px fixture has no document horizontal overflow,
a 333px textarea, a 44px history button and same-row voice/Send controls.
The existing fixture also renders a floating global-agent button over Send;
this fixture is not the integrated /chat surface and does not prove acceptance.

PASS (signed-in local): transcript/history/context composition at 1280px and
769px; narrow layouts at actual CSS widths 375px and 320px; no document
horizontal overflow. Textarea widths were 586px, 375px, 278px and 223px,
respectively. Mobile /chat does not render the fixture's overlapping global
agent button. Both light and dark themes were visually inspected.

PASS (targeted keyboard): history opens with focus in its dialog; Shift+Tab
stays inside; Escape returns focus to Toggle chat history. The composer gains
a solid two-pixel focus indicator. No messages were submitted. Temporary
theme/media/viewport overrides were restored; saved theme preference untouched.

PASS (changed text contrast): placeholder 5.31:1 light / 5.45:1 dark; tool
metadata 8.22:1 light / 10.31:1 dark; enabled gold-button foreground 8.46:1.
The placeholder originally inherited half-opacity text; now it uses the existing
accessible tertiary token. No brand-token values changed.

PASS (actual CSS verification): tool strip has a transparent background,
zero border, wrapping layout and 12px text. The initial utility override did
not win against the existing stylesheet; the correction edits the owning CSS
rules instead, removing the obsolete decoration rather than adding overrides.

NOT VERIFIED: exhaustive all-control contrast/keyboard audit, drawer resize
while open, all empty/loading/error visual permutations, Lighthouse and
production Core Web Vitals. Reduced-motion emulation was exercised, but no
frame-timing certification is claimed. Existing state tests remain passing.
These are explicitly not converted into passing full-site checks.

NOT APPLICABLE under the approved product-UI scope: marketing hero line/word,
padding and stack limits; hero images; eyebrow quotas; split-header and zigzag
caps; marketing CTA-intent duplication; logo-wall rules; bento variation,
rhythm and cell count; marquees; horizontal marketing nav height; section
layout-family diversity; marketing long-list limits; decorative image tags
and captions; marketing build footers; micro-meta copy; hero decoration,
version and section-number labels; locale/weather strips; scroll cues;
marketing comparison-bar prohibition; marketing paragraph and quote limits;
GSAP pin/scrub skeletons; premium-consumer palette rotation. Existing research
prose, numeric usage metrics, semantic status colors and vertical app navigation
are not marketing decoration. Existing serif is justified by brand preservation.

## Verification

- Full frontend suite: 295 files, 2045 tests passed including the additional
  sidebar-breakpoint assertion. Focused checks were rerun after focus styling.
- TypeScript: pass.
- Frontend lint and production-exclusion comparators: pass.
- Broader local CI wrapper: FAIL due to missing backend Python dependencies
  (ruff, dotenv, alembic, pytest). No backend files changed; this is not a
  claim of a green complete CI run.
- Full-tree ESLint retains pre-existing advisory debt.
- Authenticated local /chat successfully loaded. The prior login blocker is
  resolved; no authentication bypass or credential copying was used.

## Preservation audit

Changed URLs: []
Changed primary navigation labels: []
Changed form field names/order: []
Changed anchors: []
Changed logo/legal copy: []

Added accessibility label: Message on the existing textarea (no field name
or order change). No stored messages or research content modified.

## Brand fidelity

Source PASS: gold #D4A039, Inter, Source Serif 4, monospace metadata, existing
logo treatment and theme tokens unchanged. Dual-theme rendered identity was
checked. Homepage work already in this worktree remains separate and untouched.

The requested local visual changes and targeted acceptance checks are finished.
The strict full Section 14 release gate remains incomplete where marked NOT
VERIFIED above. No claim of globally clean CI, production performance or
deployment acceptance is made.

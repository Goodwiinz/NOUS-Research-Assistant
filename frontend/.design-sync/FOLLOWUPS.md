# Design-sync follow-ups (Nov 4, 2026)

These issues turned up while building and grading the Claude Design previews. Each one is in the app or the capture harness, so editing a preview won't fix it. They are ordered by impact.

## Accessibility (fix first)

1. **Contrast on Sol and Mars fills.** White text measures 2.36:1 on Sol and 3.76:1 on Mars. That puts the default Button and the destructive Button below 4.5:1. The Terra and Corona badge tints measure about 1.8:1. Either darken the fills or use `--nous-sol-safe`-level ink.
2. **Colour-only status.** `NousAgentStatusCard` shows status by colour alone and hardcodes oklch values. `CellCitation` exposes its anchor status only through `aria-label`. Add a visible text label to both.
3. **Disabled Slider looks enabled.** The preview has to add `opacity-50` by hand. The disabled styling belongs in `ui/slider`.
4. **`DocumentCard` retry button has no label.** Its icon-only retry button needs an `aria-label`. The same card repeats the error text in two rows, and its metadata line is uppercase.

## Consistency with the design system

5. **Hardcoded palettes instead of tokens:**
   - `ErrorDisplay` uses `yellow-*` for warnings; use Corona.
   - `DocumentCard` file-type icons use Tailwind red, blue and purple.
   - `ChatMessageActions`, `ToolExecutionCard`, and `SearchComposer`'s Stop button also hardcode colours.
6. **Title Case labels.** `ConfirmationCard` labels tools in Title Case ("Ingest ArXiv Papers") in its `TOOL_LABELS` map. Use sentence case and "arXiv".
7. **Warning styled as an error.** `Alert` with only an `AlertDescription` (the device-approval warning) uses the destructive style.
8. **`Switch` `defaultChecked` renders off.** Only a controlled `checked` shows the on state.
9. **Two active Sidebar items.** Sidebar can show two active items at once. Separately, the markdown task list draws both a bullet and a checkbox.
10. **`ExtractionMatrix` accepts ids only.** It can't render from props, so designs have to rebuild it from `Table`. A presentational split (data in, no fetch) would let designs and tests use the real component.
11. **Telemetry in the ChatBubble header.** The header packs model chip, tool ids and token telemetry into two dense rows, and exposes raw tool ids (`search_documents`) to researchers. Consider collapsing them behind a details toggle.

## Preview coverage

12. **Remaining floor cards.** 85 components still show the typographic floor card. Next candidates by product weight: `ThreadCard` (needs a `ProjectThread` fixture), `HitlApprovalToolUI` (not exported from the barrel yet), and the two virtualized lists. The virtualized lists need data plus a measured height.
13. **Unused brand components.** `NousNav`, `NousChatComposer` and `NousSourceCard`, plus the `erebus`, `accent` and `nous-ghost` Button variants, are used only on `/design-system`. Decide whether they ship or get removed.
14. **Motion under the capture harness.** framer-motion entrances and `.tool-shimmer` labels capture blank under the fixed clock, so those cells are verified live by hand. The root fix is `reducedMotion: 'reduce'` in the design-sync capture script, which belongs upstream in the skill.

## Theming

15. **Dark tokens don't work on a nested `.dark` element.** In `globals.css`, the `@theme` block (not `@theme inline`) resolves `--color-*` once at `:root`. So a `.dark` element inside a light page flips only the `--nous-*` variables. Switching to `@theme inline` would make nested dark surfaces work (marketing splits, previews), and the previews could drop the `DarkSurface` helper. Check it against the app's theme toggle before merging.

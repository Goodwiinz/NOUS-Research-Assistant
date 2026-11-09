# Building NOUS screens

NOUS is a research workspace for academics: projects of papers and documents,
chat threads where an agent answers with traced citations, extraction
matrices, notes and drafts. Every component here is the production React
component from the NOUS frontend (`window.Nous.*`), not a lookalike. Design
for a researcher in a long, focused session: the documents and answers lead,
the chrome recedes.

## Start every screen like this

```tsx
const {
  NousProvider, AppRail, Button, Card, CardHeader, CardTitle, CardContent,
  CardDescription, Badge,
} = window.Nous;

export default function Screen() {
  return (
    <NousProvider theme="dark" pathname="/documents">
      <div className="flex min-h-screen bg-background text-foreground">
        <AppRail />
        <main className="flex-1 p-8">
          <Card style={{ maxWidth: 640 }}>
            <CardHeader>
              <CardTitle>Sparse attention survey</CardTitle>
              <CardDescription>14 papers · updated 2 hours ago</CardDescription>
            </CardHeader>
            <CardContent className="flex items-center justify-between">
              <Badge variant="info">3 processing</Badge>
              <Button>Ask about these papers</Button>
            </CardContent>
          </Card>
        </main>
      </div>
    </NousProvider>
  );
}
```

- `NousProvider` supplies theme, a no-op Next router, React Query, auth and
  tooltips. Without it, components that route or fetch crash or go blank.
- `theme="dark"` matches the live app, which defaults to dark. Use `"light"`
  only when the brief asks for it. Theme the whole screen this way: a `dark`
  class on an inner `div` does not work, because `bg-background`, `bg-card`
  and the other colour utilities resolve at the root. (The `Dark` preview
  cards use a preview-only helper so they can sit beside light cells.)
- `pathname` sets the active route, so `AppRail` highlights the right item.
- The app shell is `AppRail` (icon rail) plus `Sidebar` (threads, projects)
  plus the main column. `NousNav`, `NousChatComposer` and `NousSourceCard` are
  brand-page components; do not use them for app screens.

## Data and states

There is no backend. Data hooks resolve to their empty or error state, so pass
data through props. Components that take only an id (`projectId`,
`matrixId`, `threadId`), such as `ExtractionMatrix`, fetch their own data and
will sit in a loading state: compose them from `Table`, `Card` and `Badge`
instead.

Every async surface needs its states designed, not implied:
- Loading: `Skeleton` rows in the shape of the content, not a centred spinner.
- Empty: `EmptyState` that says what to do next, with one clear action.
- Error: `Alert variant="destructive"` that names the file or step and the fix.
  For document processing, `ErrorDisplay` (with `onRetry`, or `compact`
  inline).
- Approval: ingesting, creating notes and creating drafts pause for the
  researcher to confirm. Use `ConfirmationCard` with the pending `tools`
  (name plus arguments) and a one-line `message`; it supplies approve and
  cancel.

## Choosing variants

- Buttons: one `default` button per view for the main action. `outline` or
  `secondary` for other actions, `ghost` for toolbar and icon buttons,
  `destructive` only inside a confirmation. `accent`, `erebus` and
  `nous-ghost` are for the marketing site, not app screens.
- Icon-only buttons (`size="icon"`, `IconButton`) need an `aria-label`.
- Status: `Badge` `success`, `warning`, `destructive`, `info`. Always pair
  status colour with a text label; never let colour alone carry meaning.
- Citations: chat answers cite through `ChatBubble` / `ChatMarkdown`
  (`[Doc N]` footnotes plus a Sources list); inline links use `CitationLink`.

## Colour

Restrained: warm neutrals with one accent, Sol gold. Use the Tailwind tokens
the components use:

- Surfaces and text: `bg-background`, `text-foreground`, `bg-card`,
  `bg-muted`, `text-muted-foreground`, `border-border`.
- Accent: `bg-primary`, `text-primary`, `text-primary-foreground`. Use it for
  the main action, the current selection and focus, nothing decorative.
- Gold text on light surfaces uses `var(--nous-sol-safe)`; bright
  `var(--nous-sol)` is for fills and dark surfaces. Body text must keep 4.5:1
  contrast.
- Status: `var(--nous-terra)` success, `var(--nous-corona)` warning,
  `var(--nous-mars)` error.
- Other brand variables: `var(--nous-bg-1..3)`, `var(--nous-fg-1..3)`,
  `var(--nous-border-1..2)`, `var(--nous-radius-sm..2xl)`,
  `var(--nous-shadow-sm..xl)`.
- Never hardcode hex or use `text-gray-*` / `bg-blue-*` palettes.

## Type

- Inter for UI, headings, labels and data (`font-sans`, `.nous-ui`,
  `.nous-display`). Primitives: `H1` to `H4`, `P`, `Lead`, `Large`, `Small`,
  `Muted`, `InlineCode`, `Blockquote`, `List`.
- Source Serif 4 for reading text (`.nous-body`). Wrap assistant prose in
  `.nous-chat-body nous-prose`.
- JetBrains Mono for code and tiny technical values only (`font-mono`).
  Mono is not a decorative "technical" style.
- Only utilities that appear in the app are compiled. A class the app never
  uses (`w-[612px]`, `gap-16`) does nothing, so use an inline `style` for a
  one-off size.

## Copy

- Sentence case everywhere. No uppercase tracked labels beyond one small
  kicker per section.
- No em dashes: use a colon, comma or period.
- Specific and plain: real paper titles, page numbers, file names, and
  errors that say what failed and what to do.
- US spelling; dates as `Nov 3, 2026`.

## Never

Gradient text, glass effects, coloured side-stripe borders, big-number hero
metrics, grids of identical icon cards, cards inside cards, a modal where an
inline control would do.

The source of truth is `styles.css` (compiled from the app's `globals.css`
and `nous-tokens.css`).

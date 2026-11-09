import type { CSSProperties, ReactNode } from 'react';

// Preview-only helper, not a NOUS component. In a design, set the theme on
// the root instead: <NousProvider theme="dark">. A bare `.dark` wrapper only
// flips the --nous-* variables: Tailwind resolves the --color-* tokens behind
// bg-background, bg-card, text-muted-foreground once at :root. This helper
// re-declares them under the wrapper so one dark cell can sit beside light
// ones on the same preview card.
const TOKENS = [
  'background', 'foreground', 'card', 'card-foreground', 'popover',
  'popover-foreground', 'primary', 'primary-foreground', 'secondary',
  'secondary-foreground', 'muted', 'muted-foreground', 'accent',
  'accent-foreground', 'destructive', 'destructive-foreground', 'border',
  'input', 'ring',
];

const darkTokens = Object.fromEntries(
  TOKENS.map((t) => [`--color-${t}`, `hsl(var(--${t}))`]),
) as CSSProperties;

export function DarkSurface({ children }: { children: ReactNode }) {
  return (
    <div className="dark rounded-lg bg-background p-6 text-foreground" style={darkTokens}>
      {children}
    </div>
  );
}

import { GoesOutComesInUnderline } from '@nous/design-system';

// Ported from the landing FooterSection: the underline is drawn at rest and
// retracts then redraws on hover (hover-only animation, not captured here).
const cols = [
  { head: 'Product', links: ['Research agent', 'Knowledge graph', 'Citations'] },
  { head: 'Resources', links: ['Documentation', 'API reference', 'Changelog'] },
];

export const FooterLinks = () => (
  <footer className="flex w-[420px] gap-10 bg-background p-6">
    {cols.map((col) => (
      <nav key={col.head} aria-label={col.head}>
        <h3 className="mb-3 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
          {col.head}
        </h3>
        <ul className="flex flex-col gap-2.5">
          {col.links.map((label) => (
            <li key={label}>
              <a href="#" className="inline-block text-sm text-muted-foreground transition-colors hover:text-foreground">
                <GoesOutComesInUnderline label={label} direction="left" />
              </a>
            </li>
          ))}
        </ul>
      </nav>
    ))}
  </footer>
);

export const DirectionRight = () => (
  <a href="#" className="text-base font-medium text-foreground">
    <GoesOutComesInUnderline label="Read the methodology" direction="right" />
  </a>
);

import { CenterUnderline } from '@nous/design-system';

// The underline grows from the centre on hover and is hidden at rest
// (hover-only animation, so the capture shows the resting labels).
export const HeaderNav = () => (
  <nav className="flex items-center gap-8 bg-background p-4 text-sm font-medium text-foreground">
    {['Overview', 'Research', 'Pricing', 'Sign in'].map((label) => (
      <a key={label} href="#">
        <CenterUnderline label={label} />
      </a>
    ))}
  </nav>
);

export const LargeHeading = () => (
  <a href="#" className="text-2xl font-semibold tracking-tight text-foreground">
    <CenterUnderline label="Explore the knowledge graph" underlineHeightRatio={0.08} />
  </a>
);

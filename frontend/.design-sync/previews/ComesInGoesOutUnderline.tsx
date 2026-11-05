import { ComesInGoesOutUnderline } from '@nous/design-system';

// At rest there is no underline; on hover it sweeps in from one side and out
// the other (hover-only animation, so the capture shows the resting links).
export const NavLinks = () => (
  <nav className="flex items-center gap-8 bg-background p-4 text-sm font-medium text-foreground">
    {['Projects', 'Documents', 'Threads', 'Knowledge graph'].map((label) => (
      <a key={label} href="#">
        <ComesInGoesOutUnderline label={label} direction="left" />
      </a>
    ))}
  </nav>
);

export const InlineLink = () => (
  <p className="max-w-[480px] text-sm leading-6 text-foreground">
    NOUS cites every claim it makes.{' '}
    <a href="#" className="font-medium text-primary">
      <ComesInGoesOutUnderline label="See how citations work" direction="right" />
    </a>
  </p>
);

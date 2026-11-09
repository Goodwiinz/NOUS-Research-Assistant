import { ShimmerLabel } from '@nous/design-system';

export const Running = () => (
  <span className="text-[13.5px] text-(--nous-fg-2)">
    <ShimmerLabel active className="relative inline-block leading-none">
      Using <b>Search arXiv</b>
    </ShimmerLabel>
  </span>
);

export const Retrieving = () => (
  <span className="text-xs text-(--nous-fg-2)">
    <ShimmerLabel className="relative inline-block leading-none">Retrieving</ShimmerLabel>
  </span>
);

export const Settled = () => (
  <span className="text-[13.5px] text-(--nous-fg-2)">
    <ShimmerLabel active={false} className="relative inline-block leading-none">
      Used <b>Search arXiv</b>
    </ShimmerLabel>
  </span>
);

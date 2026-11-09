import { ShimmerLabel, SwapLabel } from '@nous/design-system';

export const ToolRunning = () => (
  <span className="text-[13.5px] text-(--nous-fg-2)">
    <SwapLabel active={0} className="text-start">
      <ShimmerLabel active className="relative inline-block leading-none">
        Using <b>Search project documents</b>
      </ShimmerLabel>
      <span>
        Searched <b>Search project documents</b>
      </span>
    </SwapLabel>
  </span>
);

export const ToolComplete = () => (
  <span className="text-[13.5px] text-(--nous-fg-2)">
    <SwapLabel active={1} className="text-start">
      <ShimmerLabel active={false} className="relative inline-block leading-none">
        Using <b>Search project documents</b>
      </ShimmerLabel>
      <span>
        Searched <b>Search project documents</b>
      </span>
    </SwapLabel>
  </span>
);

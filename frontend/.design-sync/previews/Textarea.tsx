import { Label, Textarea } from '@nous/design-system';

export const WithLabel = () => (
  <div className="grid w-[420px] gap-2">
    <Label htmlFor="project-goal">Research question</Label>
    <Textarea
      id="project-goal"
      defaultValue="How do sparse attention mechanisms trade recall for latency on documents longer than 32k tokens?"
    />
  </div>
);

export const Placeholder = () => (
  <div className="w-[420px]">
    <Textarea placeholder="Add a note to this passage…" />
  </div>
);

export const Disabled = () => (
  <div className="grid w-[420px] gap-2">
    <Label htmlFor="abstract">Abstract (from source)</Label>
    <Textarea
      id="abstract"
      disabled
      defaultValue="We propose a new simple network architecture, the Transformer, based solely on attention mechanisms."
    />
  </div>
);

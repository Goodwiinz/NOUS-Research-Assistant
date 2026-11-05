import { Label, Slider } from '@nous/design-system';

export const Default = () => (
  <div className="grid w-[360px] gap-3">
    <div className="flex items-center justify-between">
      <Label>Results per query</Label>
      <span className="text-sm text-muted-foreground">20</span>
    </div>
    <Slider defaultValue={[20]} min={5} max={50} step={5} aria-label="Results per query" />
  </div>
);

export const Range = () => (
  <div className="grid w-[360px] gap-3">
    <div className="flex items-center justify-between">
      <Label>Publication year</Label>
      <span className="text-sm text-muted-foreground">2017 – 2024</span>
    </div>
    <Slider defaultValue={[2017, 2024]} min={2000} max={2026} step={1} aria-label="Publication year" />
  </div>
);

export const Disabled = () => (
  <div className="grid w-[360px] gap-3">
    <Label>Creativity (temperature)</Label>
    <Slider defaultValue={[30]} max={100} disabled className="opacity-50" aria-label="Creativity" />
  </div>
);

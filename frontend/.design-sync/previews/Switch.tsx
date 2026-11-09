import { Label, Switch } from '@nous/design-system';

export const SettingRow = () => (
  <div className="flex w-[360px] items-center justify-between gap-4">
    <div className="space-y-1">
      <Label htmlFor="auto-cite">Auto-cite sources</Label>
      <p className="text-xs text-muted-foreground">
        Insert inline citations when the agent quotes a paper.
      </p>
    </div>
    <Switch id="auto-cite" checked />
  </div>
);

export const States = () => (
  <div className="grid gap-3">
    <div className="flex items-center gap-3">
      <Switch id="sw-on" checked />
      <Label htmlFor="sw-on">Web search enabled</Label>
    </div>
    <div className="flex items-center gap-3">
      <Switch id="sw-off" checked={false} />
      <Label htmlFor="sw-off">Deep research mode</Label>
    </div>
    <div className="flex items-center gap-3">
      <Switch id="sw-disabled" checked={false} disabled />
      <Label htmlFor="sw-disabled">Share with organization</Label>
    </div>
  </div>
);

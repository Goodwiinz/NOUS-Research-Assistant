import { Checkbox, Input, Label } from '@nous/design-system';

export const WithInput = () => (
  <div className="grid w-[360px] gap-2">
    <Label htmlFor="thread-title">Thread title</Label>
    <Input id="thread-title" defaultValue="Comparing retrieval strategies" />
  </div>
);

export const WithCheckbox = () => (
  <div className="flex items-center gap-2">
    <Checkbox id="notify" defaultChecked />
    <Label htmlFor="notify">Notify me when indexing finishes</Label>
  </div>
);

export const DisabledPeer = () => (
  <div className="flex items-center gap-2">
    <Checkbox id="locked" disabled />
    <Label htmlFor="locked">Allow agent to edit drafts (owner only)</Label>
  </div>
);

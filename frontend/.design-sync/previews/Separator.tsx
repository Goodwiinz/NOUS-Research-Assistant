import { Separator } from '@nous/design-system';

export const Horizontal = () => (
  <div className="w-[360px]">
    <div className="space-y-1">
      <h4 className="text-sm font-medium">Sparse attention survey</h4>
      <p className="text-sm text-muted-foreground">24 papers · 3 drafts · 112 notes</p>
    </div>
    <Separator className="my-4" />
    <p className="text-sm text-muted-foreground">Last synced 2 hours ago</p>
  </div>
);

export const Vertical = () => (
  <div className="flex h-5 items-center gap-4 text-sm">
    <span>Library</span>
    <Separator orientation="vertical" />
    <span>Threads</span>
    <Separator orientation="vertical" />
    <span>Drafts</span>
  </div>
);

import { Spinner } from '@nous/design-system';

export const Sizes = () => (
  <div className="flex items-center gap-6">
    <Spinner size="sm" />
    <Spinner size="md" />
    <Spinner size="lg" />
  </div>
);

export const Inline = () => (
  <div className="flex items-center gap-2 text-sm text-muted-foreground">
    <Spinner size="sm" />
    <span>Searching 48 documents…</span>
  </div>
);

export const Centered = () => (
  <div className="w-[320px] py-6 text-center">
    <Spinner size="lg" className="text-primary mx-auto mb-4" />
    <p className="text-sm text-muted-foreground">Loading knowledge graph…</p>
  </div>
);

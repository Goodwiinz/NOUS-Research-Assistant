import { Button } from '@nous/design-system';
import { DarkSurface } from './_DarkSurface';
import { ArrowRight, Download, Plus, Settings2, Trash2 } from 'lucide-react';

// App screens: one `default` button per view carries the main action; the
// rest step down to outline / secondary / ghost. `destructive` belongs inside
// a confirmation.
export const Variants = () => (
  <div className="flex flex-wrap items-center gap-3">
    <Button>Start research</Button>
    <Button variant="outline">Export citations</Button>
    <Button variant="secondary">Save draft</Button>
    <Button variant="ghost">Cancel</Button>
    <Button variant="link">View sources</Button>
    <Button variant="destructive">Delete project</Button>
  </div>
);

// A real action row: the primary sits last, secondary actions before it.
export const ActionRow = () => (
  <div className="flex items-center justify-end gap-2" style={{ maxWidth: 520 }}>
    <Button variant="ghost">Cancel</Button>
    <Button variant="outline">
      <Download /> Export BibTeX
    </Button>
    <Button>
      Ask about these papers <ArrowRight />
    </Button>
  </div>
);

export const Sizes = () => (
  <div className="flex flex-wrap items-center gap-3">
    <Button size="sm">Add source</Button>
    <Button>Add source</Button>
    <Button size="lg">Add source</Button>
    <Button size="icon" variant="ghost" aria-label="New thread">
      <Plus />
    </Button>
    <Button size="icon" variant="outline" aria-label="Project settings">
      <Settings2 />
    </Button>
  </div>
);

export const States = () => (
  <div className="flex flex-wrap items-center gap-3">
    <Button isLoading loadingText="Indexing…">Index document</Button>
    <Button disabled>Ask about these papers</Button>
    <Button variant="outline" disabled>
      Export citations
    </Button>
    <Button variant="destructive">
      <Trash2 /> Remove source
    </Button>
  </div>
);

// Marketing and landing pages only. Do not use these on app screens.
export const BrandSurfaces = () => (
  <div className="flex flex-wrap items-center gap-3">
    <Button variant="erebus">Start free</Button>
    <Button variant="accent">Book a demo</Button>
    <Button variant="nous-ghost">Pricing</Button>
  </div>
);

// The same action row on the dark theme, the live app's default.
export const Dark = () => (
  <DarkSurface>
    <ActionRow />
  </DarkSurface>
);

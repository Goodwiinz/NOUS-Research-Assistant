// ConfirmDialog owns its open state internally (no `open` / `defaultOpen`
// prop), so its static render is the trigger; the dialog opens on click.
import { Button, ConfirmDialog } from '@nous/design-system';

export const RemoveSource = () => (
  <div className="flex items-center justify-between gap-4 rounded-md border border-(--nous-border-1) bg-(--nous-bg-1) px-4 py-3">
    <div className="min-w-0">
      <p className="truncate text-sm font-medium text-(--nous-fg-1)">
        Longformer: The Long-Document Transformer
      </p>
      <p className="font-mono text-[11px] text-(--nous-fg-3)">arXiv:2004.05150 · 3 citations in this thread</p>
    </div>
    <ConfirmDialog
      trigger={<Button variant="outline" size="sm">Remove from project</Button>}
      title="Remove this source from the project?"
      description="Threads that cite it keep their citations, but agents will stop retrieving from it."
      confirmLabel="Remove source"
      variant="destructive"
      onConfirm={() => {}}
    />
  </div>
);

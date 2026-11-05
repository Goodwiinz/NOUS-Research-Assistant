// DeleteConfirmDialog wraps ConfirmDialog, which owns its open state
// internally - the static render is the trigger child; the dialog opens on click.
import { Button, DeleteConfirmDialog } from '@nous/design-system';
import { Trash2 } from 'lucide-react';

export const DeleteThread = () => (
  <div className="flex items-center justify-between gap-4 rounded-md border border-(--nous-border-1) bg-(--nous-bg-1) px-4 py-3">
    <div className="min-w-0">
      <p className="truncate text-sm font-medium text-(--nous-fg-1)">
        Compare sparse attention benchmarks
      </p>
      <p className="font-mono text-[11px] text-(--nous-fg-3)">18 messages · updated 2h ago</p>
    </div>
    <DeleteConfirmDialog itemName="thread" onConfirm={() => {}}>
      <Button variant="ghost" size="icon" aria-label="Delete thread">
        <Trash2 />
      </Button>
    </DeleteConfirmDialog>
  </div>
);

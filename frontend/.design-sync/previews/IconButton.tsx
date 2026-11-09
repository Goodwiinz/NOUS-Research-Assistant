import { IconButton, IconButtonLg, IconButtonSm } from '@nous/design-system';
import { Copy, Download, MoreHorizontal, Pin, Plus, Trash2 } from 'lucide-react';

export const DocumentActions = () => (
  <div className="flex items-center gap-1">
    <IconButton icon={<Pin className="h-4 w-4" />} label="Pin to context" />
    <IconButton icon={<Copy className="h-4 w-4" />} label="Copy citation" />
    <IconButton icon={<Download className="h-4 w-4" />} label="Download PDF" />
    <IconButton icon={<MoreHorizontal className="h-4 w-4" />} label="Document actions" />
  </div>
);

export const Variants = () => (
  <div className="flex items-center gap-2">
    <IconButton icon={<Plus className="h-4 w-4" />} label="New thread" variant="default" />
    <IconButton icon={<Plus className="h-4 w-4" />} label="New thread" variant="outline" />
    <IconButton icon={<Plus className="h-4 w-4" />} label="New thread" variant="secondary" />
    <IconButton icon={<Plus className="h-4 w-4" />} label="New thread" variant="ghost" />
    <IconButton icon={<Trash2 className="h-4 w-4" />} label="Remove source" variant="destructive" />
  </div>
);

export const Sizes = () => (
  <div className="flex items-center gap-3">
    <IconButtonSm icon={<MoreHorizontal className="h-4 w-4" />} label="More" variant="outline" />
    <IconButton icon={<MoreHorizontal className="h-4 w-4" />} label="More" variant="outline" />
    <IconButtonLg icon={<MoreHorizontal className="h-5 w-5" />} label="More" variant="outline" />
  </div>
);

export const Disabled = () => (
  <div className="flex items-center gap-2">
    <IconButton icon={<Download className="h-4 w-4" />} label="Download PDF" variant="outline" disabled />
    <IconButton icon={<Trash2 className="h-4 w-4" />} label="Remove source" disabled />
  </div>
);

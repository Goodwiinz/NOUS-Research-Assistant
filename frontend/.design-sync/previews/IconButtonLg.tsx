// IconButtonLg only renders inside IconButton - the parent composition is the true render.
// (Composed inline: sibling preview imports resolve to the DS shim, not the file.)
import { IconButton, IconButtonLg, IconButtonSm } from '@nous/design-system';
import { Copy, Download, MoreHorizontal, Pin, Plus, Trash2 } from 'lucide-react';

export const Sizes = () => (
  <div className="flex items-center gap-3">
    <IconButtonSm icon={<MoreHorizontal className="h-4 w-4" />} label="More" variant="outline" />
    <IconButton icon={<MoreHorizontal className="h-4 w-4" />} label="More" variant="outline" />
    <IconButtonLg icon={<MoreHorizontal className="h-5 w-5" />} label="More" variant="outline" />
  </div>
);

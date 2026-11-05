import {
  Button,
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@nous/design-system';
import { FolderInput, MoreHorizontal, Pencil, Share2, Trash2 } from 'lucide-react';

export const ThreadActions = () => (
  <div style={{ padding: 8 }}>
    <DropdownMenu defaultOpen modal={false}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="Thread actions">
          <MoreHorizontal />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="right" align="start" className="w-56">
        <DropdownMenuLabel>Thread actions</DropdownMenuLabel>
        <DropdownMenuGroup>
          <DropdownMenuItem className="gap-2">
            <Pencil aria-hidden="true" className="h-3.5 w-3.5" />
            Rename
            <DropdownMenuShortcut>⌘R</DropdownMenuShortcut>
          </DropdownMenuItem>
          <DropdownMenuItem className="gap-2">
            <Share2 aria-hidden="true" className="h-3.5 w-3.5" />
            Share link
            <DropdownMenuShortcut>⇧⌘S</DropdownMenuShortcut>
          </DropdownMenuItem>
          {/* Sub-menu held open for the static preview; in app code omit `open`. */}
          <DropdownMenuSub open>
            <DropdownMenuSubTrigger className="gap-2">
              <FolderInput aria-hidden="true" className="h-3.5 w-3.5" />
              Move to project
            </DropdownMenuSubTrigger>
            <DropdownMenuSubContent>
              <DropdownMenuItem>Sparse attention survey</DropdownMenuItem>
              <DropdownMenuItem>Protein folding benchmarks</DropdownMenuItem>
              <DropdownMenuItem>RLHF reading group</DropdownMenuItem>
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuCheckboxItem checked>Show inline citations</DropdownMenuCheckboxItem>
        <DropdownMenuCheckboxItem checked={false}>Show tool calls</DropdownMenuCheckboxItem>
        <DropdownMenuSeparator />
        <DropdownMenuLabel>Citation style</DropdownMenuLabel>
        <DropdownMenuRadioGroup value="apa">
          <DropdownMenuRadioItem value="apa">APA 7</DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="ieee">IEEE</DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem className="gap-2 text-destructive focus:text-destructive">
          <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
          Delete thread
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  </div>
);

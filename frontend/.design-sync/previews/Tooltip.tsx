import { Button, Tooltip, TooltipContent, TooltipTrigger } from '@nous/design-system';
import { BookmarkPlus, Quote, Sparkles } from 'lucide-react';

export const ToolbarHint = () => (
  <div className="flex w-full justify-center" style={{ paddingTop: 96 }}>
    <div className="flex items-center gap-1 rounded-md border border-border bg-background p-1">
      <Button variant="ghost" size="icon" aria-label="Save to library">
        <BookmarkPlus />
      </Button>
      <Tooltip open>
        <TooltipTrigger asChild>
          <Button variant="ghost" size="icon" aria-label="Cite selection">
            <Quote />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top">Cite selection (⌘K)</TooltipContent>
      </Tooltip>
      <Button variant="ghost" size="icon" aria-label="Ask agent">
        <Sparkles />
      </Button>
    </div>
  </div>
);

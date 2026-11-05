import {
  Button,
  Input,
  Label,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Switch,
} from '@nous/design-system';
import { Share2 } from 'lucide-react';

export const ShareThread = () => (
  <div className="flex w-full justify-start p-4">
    <Popover defaultOpen>
      <PopoverTrigger asChild>
        <Button variant="outline" className="gap-2">
          <Share2 aria-hidden="true" className="h-4 w-4" />
          Share
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80">
        <div className="grid gap-4">
          <div className="space-y-1">
            <h4 className="text-sm font-medium leading-none">Share thread</h4>
            <p className="text-xs text-muted-foreground">
              Anyone with the link can read the transcript and its cited sources.
            </p>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="share-link">Link</Label>
            <div className="flex gap-2">
              <Input
                id="share-link"
                readOnly
                defaultValue="goodwiinz.tech/s/sparse-attn-7f3k"
                className="font-mono text-xs"
              />
              <Button size="sm">Copy</Button>
            </div>
          </div>
          <div className="flex items-center justify-between">
            <Label htmlFor="share-sources">Include source PDFs</Label>
            {/* Switch styles from the controlled `checked` prop; defaultChecked renders unstyled-off. */}
            <Switch id="share-sources" checked onCheckedChange={() => {}} />
          </div>
        </div>
      </PopoverContent>
    </Popover>
  </div>
);

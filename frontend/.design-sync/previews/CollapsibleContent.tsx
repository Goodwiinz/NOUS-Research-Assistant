// CollapsibleContent only renders inside its Collapsible parent - the full parent
// composition is the true render (composed inline; see learnings D.md on why
// sibling-preview imports are avoided).
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
  Label,
  Switch,
} from '@nous/design-system';
import { ChevronDown } from 'lucide-react';

export const AdvancedOptions = () => (
  <div className="w-[420px] rounded-lg border border-border bg-card p-5">
    <Collapsible defaultOpen>
      <CollapsibleTrigger className="flex items-center gap-2 rounded text-sm text-muted-foreground transition-colors hover:text-foreground">
        <ChevronDown className="h-4 w-4 rotate-180 transition-transform" />
        Advanced options
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-5 space-y-5">
        <div className="flex items-center justify-between">
          <Label htmlFor="include-abstract">Include abstract</Label>
          <Switch id="include-abstract" checked />
        </div>
        <div className="flex items-center justify-between">
          <Label htmlFor="cite-inline">Inline citations</Label>
          <Switch id="cite-inline" checked={false} />
        </div>
      </CollapsibleContent>
    </Collapsible>
  </div>
);


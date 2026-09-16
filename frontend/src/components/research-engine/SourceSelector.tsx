'use client';

import { FileText, Globe, HeartPulse, Search, Database } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { SourceConnectorType } from '@/types/scispace';
import { cn } from '@/lib/utils';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { Checkbox } from '@/components/ui/checkbox';

interface SourceDefinition {
  id: SourceConnectorType;
  label: string;
  icon: LucideIcon;
}

const SOURCES: SourceDefinition[] = [
  { id: 'arxiv', label: 'ArXiv Preprints', icon: FileText },
  { id: 'semantic_scholar', label: 'Semantic Scholar', icon: Search },
  { id: 'openalex', label: 'OpenAlex', icon: Globe },
  { id: 'crossref', label: 'Crossref', icon: Globe },
  { id: 'pubmed', label: 'PubMed', icon: HeartPulse },
];

export interface SourceSelectorProps {
  selected: SourceConnectorType[];
  onChange: (sources: SourceConnectorType[]) => void;
  disabled?: boolean;
}

export function SourceSelector({
  selected,
  onChange,
  disabled = false,
}: SourceSelectorProps) {
  const handleToggle = (id: SourceConnectorType) => {
    if (selected.includes(id)) {
      if (selected.length <= 1) return;
      onChange(selected.filter((s) => s !== id));
    } else {
      onChange([...selected, id]);
    }
  };

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          className={cn(
            'flex items-center gap-2 px-3 py-2 rounded-lg border text-sm transition-colors',
            'bg-background border-border text-foreground',
            'hover:border-primary/40 hover:bg-muted/50',
            'focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
            'disabled:opacity-50 disabled:cursor-not-allowed'
          )}
        >
          <Database aria-hidden="true" className="h-4 w-4 text-primary" />
          <span className="tabular-nums">Sources ({selected.length})</span>
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-56 bg-popover border-border p-2"
      >
        <div className="space-y-1">
          {SOURCES.map((source) => {
            const isSelected = selected.includes(source.id);
            const isLastSelected = isSelected && selected.length <= 1;

            return (
              <button
                type="button"
                key={source.id}
                onClick={() => handleToggle(source.id)}
                disabled={isLastSelected}
                className={cn(
                  'flex items-center gap-2.5 w-full px-2.5 py-2 rounded-lg text-left text-sm transition-colors',
                  'focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring',
                  isSelected
                    ? 'text-foreground bg-muted/60'
                    : 'text-muted-foreground hover:bg-muted/40 hover:text-foreground',
                  isLastSelected && 'cursor-not-allowed opacity-60'
                )}
              >
                <Checkbox
                  checked={isSelected}
                  onCheckedChange={() => handleToggle(source.id)}
                  disabled={isLastSelected}
                  className="h-3.5 w-3.5 rounded-sm border-border data-[state=checked]:bg-primary data-[state=checked]:border-primary data-[state=checked]:text-primary-foreground"
                  onClick={(e) => e.stopPropagation()}
                />
                <source.icon
                  aria-hidden="true"
                  className={cn(
                    'h-3.5 w-3.5 shrink-0',
                    isSelected ? 'text-primary' : 'text-muted-foreground'
                  )}
                />
                <span className="truncate">{source.label}</span>
              </button>
            );
          })}
        </div>
      </PopoverContent>
    </Popover>
  );
}

export default SourceSelector;

'use client';

import { FileText, Globe, HeartPulse, Search, Database } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { ReactElement } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getCapabilities } from '@/services/researchEngineService';
import type { ConnectorCapability } from '@/services/researchEngineService';
import { cn } from '@/lib/utils';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { Checkbox } from '@/components/ui/checkbox';

const SOURCE_ICONS: Record<string, LucideIcon> = {
  arxiv: FileText,
  semantic_scholar: Search,
  openalex: Globe,
  crossref: Globe,
  pubmed: HeartPulse,
};

export interface SourceSelectorProps {
  selected: string[];
  onChange: (sources: string[]) => void;
  disabled?: boolean;
}

export function SourceSelector({
  selected,
  onChange,
  disabled = false,
}: SourceSelectorProps): ReactElement {
  const capabilitiesQuery = useQuery({
    queryKey: ['research-engine', 'capabilities'],
    queryFn: getCapabilities,
  });

  const sources = Array.from(
    (capabilitiesQuery.data ?? [])
      .filter(
        (capability) => capability.daily_brief_eligible && capability.available
      )
      .reduce((unique, capability) => {
        if (!unique.has(capability.id)) {
          unique.set(capability.id, capability);
        }
        return unique;
      }, new Map<string, ConnectorCapability>())
      .values()
  );

  const handleToggle = (id: string): void => {
    if (selected.includes(id)) {
      if (selected.length <= 1) return;
      onChange(selected.filter((s) => s !== id));
    } else {
      if (selected.length >= 4) return;
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
        className="w-64 bg-popover border-border p-2"
      >
        {capabilitiesQuery.isLoading && (
          <p
            role="status"
            className="px-2.5 py-2 text-sm text-muted-foreground"
          >
            Loading sources
          </p>
        )}
        {capabilitiesQuery.isError && (
          <div role="alert" className="px-2.5 py-2">
            <p className="text-sm text-foreground">
              Failed to load available sources.
            </p>
            <button
              type="button"
              onClick={() => void capabilitiesQuery.refetch()}
              className="mt-1 rounded text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
            >
              Retry
            </button>
          </div>
        )}
        <div className="space-y-1">
          {sources.map((source: ConnectorCapability) => {
            const isSelected = selected.includes(source.id);
            const isLastSelected = isSelected && selected.length <= 1;
            const isAtMaximum = !isSelected && selected.length >= 4;
            const optionDisabled = disabled || isLastSelected || isAtMaximum;
            const SourceIcon = SOURCE_ICONS[source.id] ?? Database;

            return (
              <label
                key={source.id}
                className={cn(
                  'flex items-center gap-2.5 w-full px-2.5 py-2 rounded-lg text-left text-sm transition-colors',
                  'focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring',
                  isSelected
                    ? 'text-foreground bg-muted/60'
                    : 'text-muted-foreground hover:bg-muted/40 hover:text-foreground',
                  optionDisabled
                    ? 'cursor-not-allowed opacity-60'
                    : 'cursor-pointer'
                )}
              >
                <Checkbox
                  checked={isSelected}
                  onCheckedChange={() => handleToggle(source.id)}
                  disabled={optionDisabled}
                  aria-label={source.label}
                  className="h-3.5 w-3.5 rounded-sm border-border data-[state=checked]:bg-primary data-[state=checked]:border-primary data-[state=checked]:text-primary-foreground"
                />
                <SourceIcon
                  aria-hidden="true"
                  className={cn(
                    'h-3.5 w-3.5 shrink-0',
                    isSelected ? 'text-primary' : 'text-muted-foreground'
                  )}
                />
                <span className="truncate">{source.label}</span>
              </label>
            );
          })}
        </div>
      </PopoverContent>
    </Popover>
  );
}

export default SourceSelector;

'use client';

import { Quote } from 'lucide-react';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { cn } from '@/lib/utils';

interface CellCitationProps {
  citation_snippet: string | null;
  confidence?: number | null;
}

function getConfidenceColor(confidence: number): string {
  if (confidence > 0.8) return 'bg-sol';
  if (confidence > 0.5) return 'bg-helios';
  return 'bg-destructive';
}

function getConfidenceLabel(confidence: number): string {
  if (confidence > 0.8) return 'High';
  if (confidence > 0.5) return 'Medium';
  return 'Low';
}

export function CellCitation({
  citation_snippet,
  confidence,
}: CellCitationProps) {
  if (!citation_snippet) return null;

  const conf = confidence ?? 0;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          className="inline-flex items-center justify-center h-5 w-5 rounded text-muted-foreground hover:text-primary hover:bg-primary/10 transition-colors"
          aria-label="View citation"
        >
          <Quote className="h-3 w-3" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        className="w-80 bg-card border-border p-4"
        side="top"
        align="start"
      >
        <p className="text-xs text-muted-foreground italic leading-relaxed mb-3">
          {citation_snippet}
        </p>
        {/* No stored confidence (machine/accepted cells) means no row, not "Low". */}
        {confidence != null && (
          <div className="space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-[10px] text-muted-foreground">
                Confidence
              </span>
              <span
                className={cn(
                  'text-[10px]',
                  conf > 0.8
                    ? 'text-sol'
                    : conf > 0.5
                      ? 'text-helios'
                      : 'text-destructive'
                )}
              >
                {getConfidenceLabel(conf)} ({Math.round(conf * 100)}%)
              </span>
            </div>
            <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden">
              <div
                className={cn(
                  'h-full rounded-full transition-all',
                  getConfidenceColor(conf)
                )}
                style={{ width: `${Math.round(conf * 100)}%` }}
              />
            </div>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}

export default CellCitation;

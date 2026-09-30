'use client';

import { Quote } from 'lucide-react';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';

export const ANCHOR_LABELS: Record<string, string> = {
  verified: 'verified',
  ambiguous: 'ambiguous',
  unverified: 'unverified',
  location_unavailable: 'location unavailable',
  legacy_unanchored: 'legacy, no source location',
  disambiguated: 'disambiguated',
  accepted_unverified: 'accepted unverified',
  not_applicable: 'no source location',
  legacy: 'legacy, no source location',
};

interface CellCitationProps {
  citation_snippet: string | null;
  anchorStatus?: string | null;
  /** Opens the cell's evidence drawer; without it a legacy snippet popover. */
  onOpen?: () => void;
}

const BUTTON =
  'inline-flex items-center justify-center h-5 w-5 rounded text-muted-foreground hover:text-primary hover:bg-primary/10 transition-colors';

/** Evidence entry point for one cell. GOO-305: never renders a confidence. */
export function CellCitation({
  citation_snippet,
  anchorStatus,
  onOpen,
}: CellCitationProps): React.JSX.Element | null {
  const status = anchorStatus
    ? (ANCHOR_LABELS[anchorStatus] ?? anchorStatus)
    : 'unverified';
  const label = `Evidence: ${status}`;
  if (onOpen) {
    return (
      <button
        type="button"
        className={BUTTON}
        aria-label={label}
        onClick={onOpen}
      >
        <Quote className="h-3 w-3" />
      </button>
    );
  }
  if (!citation_snippet) return null;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" className={BUTTON} aria-label={label}>
          <Quote className="h-3 w-3" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        className="w-80 bg-card border-border p-4"
        side="top"
        align="start"
      >
        <p className="text-xs text-muted-foreground italic leading-relaxed mb-2">
          {citation_snippet}
        </p>
        <p className="text-[10px] text-muted-foreground">{status}</p>
      </PopoverContent>
    </Popover>
  );
}

export default CellCitation;

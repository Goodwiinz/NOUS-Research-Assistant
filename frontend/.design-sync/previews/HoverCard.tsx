import { HoverCard, HoverCardContent, HoverCardTrigger } from '@nous/design-system';
import { FileText } from 'lucide-react';

export const CitationPreview = () => (
  <div
    className="font-serif text-[15px] leading-relaxed text-(--nous-fg-1)"
    style={{ maxWidth: 520, padding: '232px 24px 24px' }}
  >
    Block-sparse attention cuts long-context memory by 4x
    <HoverCard defaultOpen openDelay={150} closeDelay={100}>
      <HoverCardTrigger asChild>
        <button
          type="button"
          aria-label="Citation 3"
          className="ml-0.5 align-super text-[11px] font-mono text-(--nous-fg-accent-safe)"
        >
          [3]
        </button>
      </HoverCardTrigger>
      <HoverCardContent
        align="start"
        side="top"
        sideOffset={8}
        className="w-[320px] p-0 overflow-hidden bg-(--nous-bg-1) border border-(--nous-border-1)"
      >
        <div className="flex items-start gap-2.5 px-3 py-2.5 border-b border-(--nous-border-1) bg-(--nous-bg-2)">
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded bg-(--nous-aurum)">
            <FileText aria-hidden="true" className="h-3.5 w-3.5 text-(--nous-sol-safe)" />
          </div>
          <div className="min-w-0 flex-1">
            <h4 className="text-[13px] font-medium leading-snug text-(--nous-fg-1) font-sans">
              Big Bird: Transformers for Longer Sequences
            </h4>
            <p className="mt-1 text-[10px] font-mono text-(--nous-fg-3)">
              Zaheer et al. · NeurIPS 2020 · 92% match
            </p>
          </div>
        </div>
        <p className="px-3 py-2.5 text-[12px] leading-relaxed text-(--nous-fg-2) font-serif">
          We show that sparse attention with global tokens is a universal
          approximator of sequence functions and is Turing complete, while
          scaling linearly in sequence length.
        </p>
      </HoverCardContent>
    </HoverCard>
    .
  </div>
);

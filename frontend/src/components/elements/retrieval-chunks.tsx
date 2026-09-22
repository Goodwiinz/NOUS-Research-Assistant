'use client';

import type { ComponentProps, ReactElement } from 'react';
import { DatabaseIcon } from 'lucide-react';
import { cn } from '@/lib/utils';
import { field, mono, paper, ShimmerLabel } from './surfaces';
import { pct, take } from './range';

export interface RetrievalChunk {
  id: string;
  source: string;
  locator: string;
  score: number;
  text: string;
}

function finiteScore(value: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

export function RetrievalChunks({
  query,
  chunks,
  visibleCount,
  searching,
  className,
  ...props
}: Omit<
  ComponentProps<'div'>,
  'children' | 'query' | 'chunks' | 'visibleCount' | 'searching'
> & {
  query: string;
  chunks: readonly RetrievalChunk[];
  visibleCount: number;
  searching: boolean;
}): ReactElement {
  return (
    <div
      data-slot="retrieval-chunks"
      className={cn('flex w-full max-w-sm flex-col gap-2.5', className)}
      {...props}
    >
      <span
        className={cn(
          field,
          'text-foreground/70 inline-flex w-fit items-center gap-1.5 rounded-full px-3.5 py-2 text-xs'
        )}
      >
        <DatabaseIcon className="text-foreground/40 size-3" />
        {query}
      </span>

      <div className="text-xs text-(--nous-fg-2)">
        {searching ? (
          <ShimmerLabel className="relative inline-block leading-none">
            Retrieving
          </ShimmerLabel>
        ) : (
          <span className="fade-in animate-in duration-300">
            {chunks.length} {chunks.length === 1 ? 'passage' : 'passages'} above
            threshold
          </span>
        )}
      </div>

      <div className="flex min-h-[7rem] flex-col gap-1.5">
        {take(chunks, visibleCount).map((chunk) => (
          <div
            key={chunk.id}
            className={cn(
              paper,
              'fade-in slide-in-from-bottom-1 animate-in fill-mode-both flex flex-col gap-1.5 rounded-2xl px-3.5 py-2.5 duration-300'
            )}
          >
            <div className="flex items-baseline gap-2">
              <span className="text-foreground/90 min-w-0 flex-1 truncate text-[13px] font-medium">
                {chunk.source}
              </span>
              <span className={cn(mono, 'shrink-0 text-(--nous-fg-3)')}>
                {chunk.locator}
              </span>
              <span
                className={cn(
                  mono,
                  'shrink-0 tabular-nums',
                  finiteScore(chunk.score) >= 0.8
                    ? 'text-(--nous-terra)'
                    : 'text-(--nous-fg-3)'
                )}
              >
                {finiteScore(chunk.score).toFixed(2)}
              </span>
            </div>
            <p className="line-clamp-2 text-xs leading-relaxed text-(--nous-fg-2)">
              {chunk.text}
            </p>
            <span className="bg-foreground/[0.06] h-[2px] w-full overflow-hidden rounded-full">
              <span
                className="block h-full w-full origin-left rounded-full bg-(--nous-sol)/70 transition-transform duration-500"
                style={{
                  transform: `scaleX(${pct(finiteScore(chunk.score), 1) / 100})`,
                }}
              />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

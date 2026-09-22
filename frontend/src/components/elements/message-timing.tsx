'use client';

import type { ComponentProps, ReactElement } from 'react';
import { cn } from '@/lib/utils';
import { mono } from './surfaces';

export interface TimingStat {
  label: string;
  value: string;
}

export function MessageTiming({
  stats,
  streaming,
  className,
  ...props
}: Omit<ComponentProps<'div'>, 'children' | 'stats' | 'streaming'> & {
  stats: readonly TimingStat[];
  streaming?: boolean;
}): ReactElement {
  return (
    <div
      data-slot="message-timing"
      className={cn(
        'fade-in animate-in flex w-full max-w-sm flex-wrap items-center gap-x-3 gap-y-1 duration-500',
        className
      )}
      {...props}
    >
      {stats.map((stat) => (
        <span key={stat.label} className="flex items-baseline gap-1">
          <span className="text-[11px] text-(--nous-fg-3)">{stat.label}</span>
          <span
            className={cn(
              mono,
              'tabular-nums',
              streaming ? 'text-(--nous-fg-accent-safe)' : 'text-foreground/50'
            )}
          >
            {stat.value}
          </span>
        </span>
      ))}
    </div>
  );
}

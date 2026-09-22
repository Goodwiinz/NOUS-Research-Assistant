'use client';

import { cn } from '@/lib/utils';
import { ChevronDown } from 'lucide-react';
import { ReactNode, useState } from 'react';

/**
 * Shared rail card header type. Sentence case, UI font: DESIGN.md reserves
 * mono for code and identifiers and calls repeated uppercase tracked kickers
 * an anti-pattern. `ProjectBindingCard` imports this rather than re-declaring it.
 */
export const railCardTitle =
  'text-xs font-semibold truncate text-(--nous-fg-2)';

interface CollapsibleCardProps {
  title: string;
  icon?: ReactNode;
  badge?: ReactNode;
  defaultCollapsed?: boolean;
  children: ReactNode;
  className?: string;
}

export function CollapsibleCard({
  title,
  icon,
  badge,
  defaultCollapsed = false,
  children,
  className,
}: CollapsibleCardProps) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);

  return (
    <section
      className={cn(
        'border border-(--nous-border-1) bg-(--nous-bg-2) rounded-lg',
        'dark:bg-(--nous-obsidian) dark:border-(--nous-shade)',
        className
      )}
    >
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        className="w-full flex items-center justify-between px-3 py-2.5 group hover:bg-(--nous-aurum)/30 dark:hover:bg-(--nous-ember)/30 rounded-t-lg transition-colors"
        aria-expanded={!collapsed}
      >
        <div className="flex items-center gap-2 min-w-0">
          {icon && (
            <span
              className="grid place-items-center w-4 h-4 shrink-0 text-(--nous-fg-3)"
              aria-hidden
            >
              {icon}
            </span>
          )}
          <span
            className={cn(
              railCardTitle,
              'group-hover:text-(--nous-fg-1) transition-colors'
            )}
          >
            {title}
          </span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {badge != null && (
            <span
              className="px-1.5 py-px rounded text-[9px] tabular-nums bg-(--nous-bg-1) border border-(--nous-border-1) text-(--nous-fg-2) dark:bg-(--nous-nyx) dark:border-(--nous-shade)"
              style={{
                fontFamily: 'var(--nous-font-mono)',
                letterSpacing: '0.04em',
              }}
            >
              {badge}
            </span>
          )}
          <ChevronDown
            className={cn(
              'h-3.5 w-3.5 shrink-0 transition-transform text-(--nous-fg-3)',
              collapsed && '-rotate-90'
            )}
          />
        </div>
      </button>
      {!collapsed && (
        <div className="px-3 pb-3 pt-1 border-t border-(--nous-border-1) dark:border-(--nous-shade)">
          {children}
        </div>
      )}
    </section>
  );
}

'use client';

import { useAgentActivityStore } from '@/stores/agentActivityStore';
import { cn } from '@/lib/utils';
import { Check } from 'lucide-react';
import { CollapsibleCard } from './CollapsibleCard';

interface ProgressPanelProps {
  threadId: string | null;
}

/**
 * Cowork-style Progress card: shows the agent's plan (from the LangGraph
 * planner_node) as task rows with a filled check-circle and strikethrough
 * when complete. Falls back to tool-step rows if no plan was emitted so the
 * card is still useful for simple single-step runs.
 *
 * Hidden entirely when the thread has no run yet.
 */
export function ProgressPanel({ threadId }: ProgressPanelProps) {
  const run = useAgentActivityStore((s) =>
    threadId ? s.runs[threadId] : undefined
  );

  if (!run) return null;

  const planItems = run.plan;
  const toolItems = run.steps;

  // Nothing to show — don't render an empty card.
  if (planItems.length === 0 && toolItems.length === 0) return null;

  const useTools = planItems.length === 0;
  const rows = useTools
    ? toolItems.map((s) => ({
        id: s.id,
        text: s.label,
        done: s.status === 'done',
        active: s.status === 'active',
        error: s.status === 'error',
      }))
    : planItems.map((p) => ({
        id: p.id,
        text: p.text,
        done: p.done,
        active: false,
        error: false,
      }));

  const doneCount = rows.filter((r) => r.done).length;

  return (
    <CollapsibleCard title="Progress" badge={`${doneCount} of ${rows.length}`}>
      <ul className="space-y-3">
        {rows.map((row) => (
          <li key={row.id} className="flex items-start gap-3">
            <span
              className={cn(
                'mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full transition-colors',
                row.done && 'bg-(--nous-sol)',
                row.active && 'border-2 border-(--nous-sol) animate-pulse',
                row.error && 'bg-(--nous-mars)',
                !row.done &&
                  !row.active &&
                  !row.error &&
                  'border border-(--nous-border-1)'
              )}
              aria-hidden
            >
              {row.done && (
                <Check
                  className="h-3 w-3"
                  style={{ color: 'var(--nous-bg-1)' }}
                  strokeWidth={3}
                />
              )}
              {row.error && (
                <span
                  className="text-[10px] font-bold"
                  style={{ color: 'var(--nous-bg-1)' }}
                >
                  !
                </span>
              )}
            </span>
            <span className="sr-only">
              {row.error
                ? 'Failed'
                : row.active
                  ? 'In progress'
                  : row.done
                    ? 'Done'
                    : 'Pending'}
            </span>
            <span
              className={cn(
                'text-[14px] leading-snug',
                row.done && 'line-through',
                row.active && 'font-medium'
              )}
              style={{
                color: row.done
                  ? 'var(--nous-fg-3)'
                  : row.error
                    ? 'var(--nous-mars)'
                    : 'var(--nous-fg-1)',
              }}
            >
              {row.text}
            </span>
          </li>
        ))}
      </ul>
    </CollapsibleCard>
  );
}

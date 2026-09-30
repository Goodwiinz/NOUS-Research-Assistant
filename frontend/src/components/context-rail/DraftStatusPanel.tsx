'use client';

import { useEffect, useState } from 'react';
import { Check, Loader2, PenLine } from 'lucide-react';
import { cn } from '@/lib/utils';
import { statusSteps } from '@/components/research/DraftGenerationProgress';
import { CollapsibleCard } from './CollapsibleCard';
import {
  isTerminal,
  useDraftGenerationStatus,
} from './hooks/useDraftGenerationStatus';

interface DraftStatusPanelProps {
  projectId?: string;
}

function useElapsed(startedAt: string | undefined, running: boolean): string {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!startedAt || !running) return;
    const start = new Date(startedAt).getTime();
    const tick = () => setElapsed(Math.max(0, Math.floor((Date.now() - start) / 1000)));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [startedAt, running]);

  const mins = Math.floor(elapsed / 60);
  return `${mins}:${(elapsed % 60).toString().padStart(2, '0')}`;
}

/**
 * Pending indicator for background draft generation.
 *
 * Mirrors the assistant-ui research-report outline: every step stays visible,
 * pending ones dimmed with a dot, the current one spinning, finished ones
 * checked. Hidden when nothing is running; a *completed* run hides too (the
 * draft itself shows up under Working folders → Drafts), while failed and
 * cancelled runs stay so the failure isn't silent.
 */
export function DraftStatusPanel({ projectId }: DraftStatusPanelProps) {
  const { status, cancel } = useDraftGenerationStatus(projectId);
  const running = Boolean(status) && !isTerminal(status?.status);
  const elapsed = useElapsed(status?.started_at, running);

  if (!status || status.status === 'completed') return null;

  const failed = status.status === 'failed';
  const cancelled = status.status === 'cancelled';
  const currentIdx = statusSteps.findIndex((s) => s.key === status.status);

  return (
    <CollapsibleCard
      title={failed ? 'Draft failed' : cancelled ? 'Draft cancelled' : 'Drafting'}
      icon={
        running ? (
          <Loader2 className="h-3 w-3 animate-spin" strokeWidth={1.7} />
        ) : (
          <PenLine className="h-3 w-3" strokeWidth={1.7} />
        )
      }
      badge={running ? `${status.progress}%` : undefined}
    >
      <p
        className="mb-2 text-[11px] text-(--nous-fg-2)"
        style={{ fontFamily: 'var(--nous-font-mono)', letterSpacing: '0.04em' }}
      >
        {status.current_step}
      </p>

      <div
        className="mb-3 h-1 overflow-hidden rounded-full bg-(--nous-border-1)"
        role="progressbar"
        aria-valuenow={status.progress}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Draft generation progress"
      >
        <div
          className="h-full transition-[width] duration-500"
          style={{
            width: `${status.progress}%`,
            background: failed ? 'var(--error-red)' : 'var(--nous-sol)',
          }}
        />
      </div>

      <ul className="space-y-2">
        {statusSteps.slice(0, -1).map((step, idx) => {
          const done = currentIdx >= 0 && idx < currentIdx;
          const active = running && idx === currentIdx;
          return (
            <li
              key={step.key}
              className={cn(
                'flex items-center gap-2 text-[12px]',
                active ? 'text-(--nous-fg-1)' : done ? 'text-(--nous-fg-3)' : 'text-(--nous-fg-3)/50'
              )}
            >
              <span className="grid h-3 w-3 shrink-0 place-items-center" aria-hidden>
                {done ? (
                  <Check className="h-3 w-3" strokeWidth={3} />
                ) : active ? (
                  <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2} />
                ) : (
                  <span className="h-1.5 w-1.5 rounded-full bg-current" />
                )}
              </span>
              <span className={cn(done && 'line-through')}>{step.label}</span>
            </li>
          );
        })}
      </ul>

      <div className="mt-3 flex items-center justify-between border-t border-(--nous-border-1) pt-2">
        <span
          className="text-[10px] text-(--nous-fg-3)"
          style={{ fontFamily: 'var(--nous-font-mono)', letterSpacing: '0.08em' }}
        >
          {running ? `elapsed ${elapsed}` : status.current_step}
        </span>
        {running && (
          <button
            type="button"
            onClick={() => {
              void cancel();
            }}
            className="text-[10px] uppercase text-(--nous-fg-3) hover:text-(--nous-fg-1) transition-colors"
            style={{ fontFamily: 'var(--nous-font-mono)', letterSpacing: '0.12em' }}
          >
            Cancel
          </button>
        )}
      </div>
    </CollapsibleCard>
  );
}

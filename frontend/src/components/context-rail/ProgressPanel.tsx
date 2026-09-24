'use client';

import type { JSX } from 'react';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import { useAgentChatStore } from '@/store/agentChatStore';
import { cn } from '@/lib/utils';
import { Check } from 'lucide-react';
import { deriveStepExecutionStatus } from '@/components/agent-chat/planMapping';
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
export function ProgressPanel({
  threadId,
}: ProgressPanelProps): JSX.Element | null {
  const run = useAgentActivityStore((s) =>
    threadId ? s.runs[threadId] : undefined
  );
  const activeThreadId = useAgentChatStore((s) => s.activeThreadId);
  const messages = useAgentChatStore((s) => s.messages);
  const persisted =
    threadId && threadId === activeThreadId
      ? [...messages]
          .reverse()
          .find(
            (message) =>
              message.role === 'assistant' &&
              ((message.plan?.length ?? 0) > 0 ||
                (message.toolExecutions?.length ?? 0) > 0)
          )
      : undefined;

  if (!run && !persisted) return null;

  let rows;
  if (run) {
    const useTools = run.plan.length === 0;
    rows = useTools
      ? run.steps.map((step) => ({
          id: step.id,
          text: step.label,
          done: step.status === 'done',
          active: step.status === 'active',
          error: step.status === 'error',
          cancelled: step.status === 'cancelled',
        }))
      : run.plan.map((planItem) => {
          const execution = [...run.steps]
            .reverse()
            .find((step) => planItem.tool && step.tool === planItem.tool);
          return {
            id: planItem.id,
            text: planItem.text,
            done: planItem.done,
            active: !planItem.done && execution?.status === 'active',
            error: !planItem.done && execution?.status === 'error',
            cancelled: !planItem.done && execution?.status === 'cancelled',
          };
        });
  } else {
    const executions = persisted?.toolExecutions ?? [];
    const plan = persisted?.plan ?? [];
    const persistedSucceeded =
      !persisted?.isStreaming &&
      !persisted?.isError &&
      !executions.some((execution) => execution.status !== 'completed');
    if (plan.length === 0) {
      rows = executions.map((execution) => ({
        id: execution.id,
        text: execution.toolDisplayName,
        done: execution.status === 'completed',
        active:
          execution.status === 'pending' || execution.status === 'running',
        error: execution.status === 'failed',
        cancelled: execution.status === 'cancelled',
      }));
    } else {
      const totalPerTool = new Map<string, number>();
      for (const item of plan) {
        if (item.tool) {
          totalPerTool.set(item.tool, (totalPerTool.get(item.tool) ?? 0) + 1);
        }
      }
      const seenPerTool = new Map<string, number>();
      rows = plan.map((item) => {
        if (!item.tool) {
          return {
            id: `persisted-plan-${item.step}`,
            text: item.description,
            done: persistedSucceeded,
            active: false,
            error: false,
            cancelled: false,
          };
        }
        const occurrence = seenPerTool.get(item.tool) ?? 0;
        seenPerTool.set(item.tool, occurrence + 1);
        const status = deriveStepExecutionStatus(
          item,
          executions,
          occurrence,
          totalPerTool.get(item.tool) ?? 1
        );
        return {
          id: `persisted-plan-${item.step}`,
          text: item.description,
          done: status === 'completed',
          active: status === 'pending' || status === 'running',
          error: status === 'failed',
          cancelled: status === 'cancelled',
        };
      });
    }
  }

  // Nothing to show — don't render an empty card.
  if (rows.length === 0) return null;

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
                row.cancelled && 'bg-(--nous-mars)',
                !row.done &&
                  !row.active &&
                  !row.error &&
                  !row.cancelled &&
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
              {(row.error || row.cancelled) && (
                <span
                  className="text-[10px] font-bold"
                  style={{ color: 'var(--nous-bg-1)' }}
                >
                  !
                </span>
              )}
            </span>
            <span className="sr-only">
              {row.cancelled
                ? 'Cancelled'
                : row.error
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
                  : row.error || row.cancelled
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

'use client';

import { useQuery } from '@tanstack/react-query';
import { AlertCircle, CheckCircle2, Loader2, XCircle } from 'lucide-react';
import Link from 'next/link';
import type { ReactElement } from 'react';

import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { projectService } from '@/services/projectService';

interface DraftTask {
  projectId: string;
  taskId: string;
}

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled']);
const TASK_ID = /^[A-Za-z0-9_-]{6,64}$/;
const PROJECT_ID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The tool result is the link between the persisted reply and its task. */
export function draftTasksFromMessage(message: ChatPageMessage): DraftTask[] {
  const tasks: DraftTask[] = [];
  const seen = new Set<string>();
  for (const step of message.toolExecutions ?? []) {
    if (step.tool !== 'create_draft' || step.status !== 'done') continue;
    let result: unknown = step.result;
    if (typeof result === 'string') {
      try {
        result = JSON.parse(result);
      } catch {
        continue;
      }
    }
    if (!result || typeof result !== 'object') continue;
    const record = result as Record<string, unknown>;
    const taskId = record.task_id;
    const projectId = record.project_id ?? step.args?.project_id;
    if (
      typeof taskId === 'string' &&
      TASK_ID.test(taskId) &&
      typeof projectId === 'string' &&
      PROJECT_ID.test(projectId)
    ) {
      const key = `${projectId}:${taskId}`;
      if (!seen.has(key)) {
        tasks.push({ taskId, projectId });
        seen.add(key);
      }
    }
  }
  return tasks;
}

/** Older replies carry a fixed pending snapshot; the card owns current state. */
export function removeStaleDraftStatus(content: string): string {
  return content
    .replace(/\n\s*Status:\s*pending\s*\nTask ID:\s*[A-Za-z0-9_-]+\s*$/i, '')
    .trimEnd();
}

function statusLabel(status: string, currentStep?: string): string {
  switch (status) {
    case 'pending':
      return 'Starting draft';
    case 'analyzing':
      return 'Reading project sources';
    case 'generating':
      return currentStep === 'Building sections'
        ? 'Building sections'
        : 'Writing draft';
    case 'citing':
      return 'Adding citations';
    case 'reviewing':
      return 'Checking citations';
    case 'finalizing':
      return 'Saving draft';
    case 'completed':
      return 'Draft ready';
    case 'failed':
      return 'Draft failed';
    case 'cancelled':
      return 'Draft cancelled';
    default:
      return 'Checking draft status';
  }
}

export function DraftTaskStatus({
  projectId,
  taskId,
}: DraftTask): ReactElement {
  const query = useQuery({
    queryKey: ['draft-generation', projectId, taskId],
    queryFn: () => projectService.getGenerationStatus(projectId, taskId),
    retry: false,
    refetchOnWindowFocus: (state) =>
      !TERMINAL_STATUSES.has(state.state.data?.status ?? ''),
    refetchOnReconnect: (state) =>
      !TERMINAL_STATUSES.has(state.state.data?.status ?? ''),
    refetchIntervalInBackground: false,
    refetchInterval: (state) => {
      if (TERMINAL_STATUSES.has(state.state.data?.status ?? '')) return false;
      return state.state.error ? 10000 : 3000;
    },
  });

  const status = query.data?.status ?? 'pending';
  const unavailable = query.isError;
  const terminal = TERMINAL_STATUSES.has(status);
  const active = !terminal && !unavailable;
  const label = unavailable
    ? 'Status unavailable'
    : statusLabel(status, query.data?.current_step);
  const completedDraftId =
    !unavailable && status === 'completed' ? query.data?.draft_id : undefined;

  return (
    <section
      aria-label="Draft generation"
      className="mt-3 max-w-[540px] rounded-xl border border-(--nous-border-1) bg-(--nous-bg-2) px-3.5 py-3 font-nous-ui"
    >
      <div className="flex items-center gap-2.5">
        {unavailable ? (
          <AlertCircle
            className="h-4 w-4 shrink-0 text-(--nous-fg-3)"
            aria-hidden
          />
        ) : status === 'completed' ? (
          <CheckCircle2
            className="h-4 w-4 shrink-0 text-(--nous-terra)"
            aria-hidden
          />
        ) : status === 'failed' || status === 'cancelled' ? (
          <XCircle
            className="h-4 w-4 shrink-0 text-(--nous-fg-3)"
            aria-hidden
          />
        ) : (
          <Loader2
            className="h-4 w-4 shrink-0 animate-spin text-(--nous-sol) motion-reduce:animate-none"
            aria-hidden
          />
        )}
        <span
          role="status"
          aria-live="polite"
          className="text-[13px] font-medium text-(--nous-fg-1)"
        >
          {label}
        </span>
      </div>

      {active && (
        <div
          role="progressbar"
          aria-label="Draft generation in progress"
          className="mt-3 h-1 overflow-hidden rounded-full bg-(--nous-border-1)"
        >
          <span className="block h-full w-1/3 animate-pulse rounded-full bg-(--nous-sol) motion-reduce:animate-none" />
        </div>
      )}

      {unavailable && (
        <div className="mt-2 flex flex-wrap items-center gap-3 pl-6.5 text-[11px] text-(--nous-fg-3)">
          <span>Could not confirm the latest state.</span>
          <button
            type="button"
            onClick={() => void query.refetch()}
            className="text-(--nous-sol) underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-(--nous-sol)"
          >
            Check again
          </button>
          <Link
            href={`/projects/${projectId}?tab=drafts`}
            className="text-(--nous-sol) underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-(--nous-sol)"
          >
            Open drafts
          </Link>
        </div>
      )}

      {completedDraftId && (
        <Link
          href={`/projects/${projectId}?tab=drafts&draftId=${completedDraftId}`}
          className="mt-2 inline-flex pl-6.5 text-[12px] font-medium text-(--nous-sol) underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-(--nous-sol)"
        >
          View draft
        </Link>
      )}

      <details className="mt-2 pl-6.5 text-[10px] text-(--nous-fg-3)">
        <summary className="w-fit cursor-pointer">Task details</summary>
        <span className="font-nous-mono break-all">Task ID: {taskId}</span>
      </details>
    </section>
  );
}

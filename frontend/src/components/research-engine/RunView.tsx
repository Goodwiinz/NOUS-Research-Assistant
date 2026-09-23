'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  ArrowLeft,
  Loader2,
  Pause,
  Play,
  AlertCircle,
  Coins,
} from 'lucide-react';
import { getRun, pauseRun, resumeRun } from '@/services/researchEngineService';
import {
  useResearchEngineStore,
  type ResearchRun,
  type RunStepEvent,
} from '@/store/research-engine-store';
import { StepProgress, type StepData } from './StepProgress';

interface RunViewProps {
  runId: string;
}

const STATUS_BADGE: Record<
  string,
  { bg: string; text: string; border: string; dot: string; label: string }
> = {
  pending: {
    bg: 'bg-muted',
    text: 'text-muted-foreground',
    border: 'border-border',
    dot: 'bg-muted-foreground/60',
    label: 'Pending',
  },
  running: {
    bg: 'bg-primary/10',
    text: 'text-primary',
    border: 'border-primary/30',
    dot: 'bg-primary',
    label: 'Running',
  },
  paused: {
    bg: 'bg-(--nous-helios)/10',
    text: 'text-(--nous-helios)',
    border: 'border-(--nous-helios)/30',
    dot: 'bg-(--nous-helios)',
    label: 'Paused',
  },
  completed: {
    bg: 'bg-(--nous-terra)/10',
    text: 'text-(--nous-terra)',
    border: 'border-(--nous-terra)/30',
    dot: 'bg-(--nous-terra)',
    label: 'Completed',
  },
  failed: {
    bg: 'bg-(--nous-mars)/10',
    text: 'text-(--nous-mars)',
    border: 'border-(--nous-mars)/30',
    dot: 'bg-(--nous-mars)',
    label: 'Failed',
  },
};

async function getAuthToken(): Promise<string | null> {
  try {
    const { createClient } = await import('@/lib/supabase/client');
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();
    return session?.access_token ?? null;
  } catch {
    return null;
  }
}

export function RunView({ runId }: RunViewProps) {
  const router = useRouter();
  const {
    activeRun,
    setActiveRun,
    runEvents,
    addRunEvent,
    clearRunEvents,
    setLoading,
    isLoading,
    setError,
    error,
  } = useResearchEngineStore();

  const [actionError, setActionError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // Fetch run details on mount
  const fetchRun = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = (await getRun(runId)) as ResearchRun;
      setActiveRun(data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : 'Failed to load run details'
      );
    } finally {
      setLoading(false);
    }
  }, [runId, setActiveRun, setLoading, setError]);

  useEffect(() => {
    clearRunEvents();
    fetchRun();
    return () => {
      setActiveRun(null);
      clearRunEvents();
    };
  }, [fetchRun, clearRunEvents, setActiveRun]);

  // Connect to SSE stream when run is running
  useEffect(() => {
    const runStatus = activeRun?.status;
    if (runStatus !== 'running' && runStatus !== 'pending') return;

    const controller = new AbortController();
    abortRef.current = controller;

    const connectSSE = async () => {
      const token = await getAuthToken();
      const headers: Record<string, string> = {
        Accept: 'text/event-stream',
      };
      if (token) {
        headers['Authorization'] = `Bearer ${token}`;
      }

      try {
        const response = await fetch(
          `/api/v1/research-engine/runs/${runId}/stream`,
          {
            method: 'GET',
            headers,
            signal: controller.signal,
          }
        );

        if (!response.ok || !response.body) return;

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let currentEventType: string | null = null;

        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() ?? '';

            for (const line of lines) {
              const trimmed = line.trim();

              if (trimmed.startsWith('event: ')) {
                currentEventType = trimmed.slice(7).trim();
              } else if (trimmed.startsWith('data: ') && currentEventType) {
                try {
                  const parsed = JSON.parse(trimmed.slice(6)) as RunStepEvent;
                  addRunEvent({
                    ...parsed,
                    event: currentEventType,
                  });

                  // Refresh the persisted status after lifecycle events.
                  if (
                    currentEventType === 'run_complete' ||
                    currentEventType === 'run_failed' ||
                    currentEventType === 'run_paused'
                  ) {
                    fetchRun();
                  }
                } catch {
                  // skip malformed JSON
                }
                currentEventType = null;
              }
            }
          }
        } finally {
          reader.releaseLock();
        }
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          console.error('SSE stream error:', err);
        }
      }
    };

    connectSSE();

    return () => {
      controller.abort();
      abortRef.current = null;
    };
  }, [runId, activeRun?.status, addRunEvent, fetchRun]);

  // Build step data from events
  const steps: StepData[] = buildStepsFromEvents(runEvents);

  // Total tokens from events or run data
  const totalTokens =
    steps.reduce((sum, s) => sum + s.tokenCount, 0) ||
    activeRun?.total_tokens ||
    0;

  const handlePause = async () => {
    setActionError(null);
    setActionLoading(true);
    try {
      await pauseRun(runId);
      await fetchRun();
    } catch {
      setActionError('Pause failed. The run may have already finished.'); // R6-L21
    } finally {
      setActionLoading(false);
    }
  };

  const handleResume = async () => {
    setActionError(null);
    setActionLoading(true);
    try {
      await resumeRun(runId);
      await fetchRun();
    } catch {
      setActionError('Resume failed. Try again in a moment.'); // R6-L21
      // ignore
    } finally {
      setActionLoading(false);
    }
  };

  const badge = STATUS_BADGE[activeRun?.status ?? 'pending'];

  // Loading: skeleton placeholders, not a center spinner.
  if (isLoading && !activeRun) {
    return (
      <div className="space-y-6" role="status" aria-label="Loading run">
        <div className="flex items-center gap-4">
          <div className="h-8 w-8 rounded-lg bg-muted animate-pulse" />
          <div className="flex-1 space-y-2">
            <div className="h-5 w-40 rounded bg-muted animate-pulse" />
            <div className="h-3 w-56 rounded bg-muted animate-pulse" />
          </div>
          <div className="h-6 w-20 rounded-full bg-muted animate-pulse" />
        </div>
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              className="h-14 rounded-xl border border-border bg-card animate-pulse"
            />
          ))}
        </div>
        <span className="sr-only">Loading run details…</span>
      </div>
    );
  }

  if (error && !activeRun) {
    return (
      <div
        role="alert"
        className="flex flex-col items-start gap-3 rounded-xl border border-(--nous-mars)/30 bg-(--nous-mars)/10 p-4"
      >
        <div className="flex items-start gap-2">
          <AlertCircle
            aria-hidden="true"
            className="h-5 w-5 shrink-0 text-(--nous-mars)"
          />
          <span className="text-sm text-foreground">{error}</span>
        </div>
        <button
          type="button"
          onClick={() => fetchRun()}
          className="rounded text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          Retry
        </button>
      </div>
    );
  }

  return (
    <div>
      {/* Header */}
      <div className="mb-6 flex flex-wrap items-center gap-4 rounded-xl border border-border bg-card p-5 shadow-xs">
        <button
          type="button"
          onClick={() => router.back()}
          className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          aria-label="Go back"
        >
          <ArrowLeft aria-hidden="true" className="h-5 w-5" />
        </button>

        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-semibold text-foreground">
            Run{' '}
            <span className="text-primary tabular-nums">
              {runId.slice(0, 8)}
            </span>
          </h1>
          {activeRun?.started_at && (
            <p className="mt-0.5 text-xs text-muted-foreground">
              Started {new Date(activeRun.started_at).toLocaleString()}
            </p>
          )}
        </div>

        {/* Status badge — status conveyed by label, not color alone */}
        <span
          role="status"
          className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium ${badge.bg} ${badge.text} ${badge.border}`}
        >
          <span
            aria-hidden="true"
            className={`inline-flex h-2 w-2 rounded-full ${badge.dot} ${
              activeRun?.status === 'running' ? 'motion-safe:animate-pulse' : ''
            }`}
          />
          {badge.label}
        </span>

        {/* Total tokens */}
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground tabular-nums">
          <Coins aria-hidden="true" className="h-3.5 w-3.5" />
          {totalTokens.toLocaleString()} tokens
        </div>

        {/* Pause/Resume */}
        {activeRun?.status === 'running' && (
          <button
            type="button"
            onClick={handlePause}
            disabled={actionLoading}
            className="flex items-center gap-1.5 rounded-lg border border-(--nous-helios)/30 bg-(--nous-helios)/10 px-3 py-1.5 text-xs font-medium text-(--nous-helios) transition-colors hover:bg-(--nous-helios)/20 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50"
          >
            {actionLoading ? (
              <Loader2
                aria-hidden="true"
                className="h-3.5 w-3.5 animate-spin"
              />
            ) : (
              <Pause aria-hidden="true" className="h-3.5 w-3.5" />
            )}
            Pause
          </button>
        )}
        {activeRun?.status === 'paused' && (
          <button
            type="button"
            onClick={handleResume}
            disabled={actionLoading}
            className="flex items-center gap-1.5 rounded-lg border border-primary/30 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary transition-colors hover:bg-primary/20 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50"
          >
            {actionLoading ? (
              <Loader2
                aria-hidden="true"
                className="h-3.5 w-3.5 animate-spin"
              />
            ) : (
              <Play aria-hidden="true" className="h-3.5 w-3.5" />
            )}
            Resume
          </button>
        )}
      </div>

      {actionError && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-red-300 bg-red-50 px-4 py-2 text-sm text-red-800 dark:border-red-700 dark:bg-red-950/40 dark:text-red-200"
        >
          {actionError}
        </div>
      )}

      {/* Steps list */}
      {steps.length === 0 && !isLoading ? (
        <div className="rounded-xl border border-border bg-card px-6 py-16 text-center">
          <p className="text-sm text-foreground">
            {activeRun?.status === 'pending'
              ? 'Waiting for the run to start.'
              : 'No steps received yet.'}
          </p>
          <p className="mt-1 text-sm text-muted-foreground">
            {activeRun?.status === 'pending'
              ? 'Steps will appear here as soon as the run begins.'
              : 'Steps stream in here as the run progresses.'}
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {steps.map((step) => (
            <StepProgress key={step.stepIndex} step={step} />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Build a list of StepData from the stream of RunStepEvents.
 * Events with the same step_index are merged together, with later events
 * overwriting earlier fields.
 */
function buildStepsFromEvents(events: RunStepEvent[]): StepData[] {
  const stepMap = new Map<number, StepData>();

  for (const evt of events) {
    const idx = evt.step_index;
    if (idx === undefined || idx === null) continue;

    const existing = stepMap.get(idx);

    const stepStatus = (() => {
      switch (evt.event) {
        case 'step_start':
          return 'running' as const;
        case 'step_complete':
          return 'complete' as const;
        case 'step_error':
          return 'error' as const;
        default:
          return existing?.status ?? ('pending' as const);
      }
    })();

    const merged: StepData = {
      stepIndex: idx,
      stepName: evt.step_name ?? existing?.stepName ?? `Step ${idx + 1}`,
      stepType: evt.step_type ?? existing?.stepType ?? 'unknown',
      status: stepStatus,
      mode: (evt.mode as StepData['mode']) ?? existing?.mode,
      tokenCount: evt.token_count ?? existing?.tokenCount ?? 0,
      qualityMarks: evt.quality_marks ?? existing?.qualityMarks ?? [],
      output: evt.output ?? existing?.output,
      sources: evt.sources ?? existing?.sources,
      prompt: evt.prompt ?? existing?.prompt,
      errorMessage: evt.error_message ?? existing?.errorMessage,
    };

    stepMap.set(idx, merged);
  }

  return Array.from(stepMap.values()).sort((a, b) => a.stepIndex - b.stepIndex);
}

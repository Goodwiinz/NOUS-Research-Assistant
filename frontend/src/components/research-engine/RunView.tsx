'use client';

import { captureAccountSession, getAccountSignal } from '@/lib/account-session';
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactElement,
} from 'react';
import { useRouter } from 'next/navigation';
import {
  ArrowLeft,
  Loader2,
  Pause,
  Play,
  AlertCircle,
  Coins,
  ShieldAlert,
} from 'lucide-react';
import {
  getPendingReview,
  getRun,
  getRunManifest,
  listSteps,
  pauseRun,
  resumeRun,
} from '@/services/researchEngineService';
import {
  useResearchEngineStore,
  type RunStepEvent,
} from '@/store/research-engine-store';
import { StepProgress, type StepData } from './StepProgress';
import { ReviewPanel, type ReviewSourceRecord } from './ReviewPanel';
import { RunResults } from './RunResults';
import { RunReproducibility } from './RunReproducibility';
import { RunRerun } from './RunRerun';

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

const SSE_MAX_ATTEMPTS = 3;
const SSE_RECONNECT_BASE_DELAY_MS = 500;
const CONFORMANCE_LABEL: Record<string, string> = {
  plan_verified: 'Plan verified',
  conformant: 'Conformant',
  deviated: 'Deviated',
  legacy_unbound: 'Legacy unbound',
};

function waitForReconnect(
  signal: AbortSignal,
  completedAttempt: number
): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const onAbort = (): void => {
      clearTimeout(timeoutId);
      resolve();
    };
    const timeoutId = setTimeout(
      () => {
        signal.removeEventListener('abort', onAbort);
        resolve();
      },
      SSE_RECONNECT_BASE_DELAY_MS * 2 ** (completedAttempt - 1)
    );
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

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

export function RunView({ runId }: RunViewProps): ReactElement {
  const router = useRouter();
  const {
    activeRunId,
    activeRun,
    runSteps,
    pendingReview,
    finalStatus,
    hydrateRun,
    mergeRunEvent,
    setPendingReview,
    resetRun,
    setLoading,
    isLoading,
    setError,
    error,
  } = useResearchEngineStore();

  const [actionError, setActionError] = useState<{
    runId: string;
    message: string;
  } | null>(null);
  const [actionLoading, setActionLoading] = useState(false);
  const [resumeAuthorized, setResumeAuthorized] = useState(false);
  const resumeAuthorizedRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const requestRef = useRef(0);
  const markResumeAuthorized = useCallback((authorized: boolean): void => {
    resumeAuthorizedRef.current = authorized;
    setResumeAuthorized(authorized);
  }, []);

  const refreshRun = useCallback(
    async (preservePendingReview = false): Promise<boolean> => {
      const isCurrentAccount = captureAccountSession();
      const requestId = ++requestRef.current;
      try {
        const [run, steps] = await Promise.all([
          getRun(runId),
          listSteps(runId),
        ]);
        if (
          requestRef.current !== requestId ||
          !isCurrentAccount() ||
          useResearchEngineStore.getState().activeRunId !== runId
        ) {
          return false;
        }
        hydrateRun(run, steps);
        const waitingForAuthorizedStreamClaim =
          resumeAuthorizedRef.current &&
          run.status === 'paused' &&
          (run.pause_reason === null || run.pause_reason === 'user_paused');
        if (!waitingForAuthorizedStreamClaim) {
          markResumeAuthorized(false);
        }

        if (run.status === 'paused' && run.pause_reason === 'review_required') {
          // Never leave a prior review visible while refreshing its server-owned
          // descriptor and output.
          if (!preservePendingReview) setPendingReview(null);
          try {
            const review = await getPendingReview(runId);
            if (
              requestRef.current === requestId &&
              isCurrentAccount() &&
              useResearchEngineStore.getState().activeRunId === runId
            ) {
              setPendingReview(review);
            }
          } catch {
            if (
              requestRef.current === requestId &&
              isCurrentAccount() &&
              useResearchEngineStore.getState().activeRunId === runId
            ) {
              setActionError({
                runId,
                message: 'The pending review could not be loaded. Try again.',
              });
            }
            return false;
          }
        } else {
          setPendingReview(null);
        }

        if (run.status === 'completed') {
          void getRunManifest(runId)
            .then((manifest) => {
              if (!isCurrentAccount()) return;
              const value = asRecord(manifest)?.final_status;
              if (
                value === 'verified' ||
                value === 'unverified' ||
                value === 'no_evidence'
              ) {
                mergeRunEvent({
                  event: 'run_complete',
                  run_id: runId,
                  final_status: value,
                });
              }
            })
            .catch(() => {
              // Older runs may not carry the new manifest fields. Persisted step
              // output remains available and is used as the presentation fallback.
            });
        }
        return true;
      } catch {
        if (
          requestRef.current !== requestId ||
          !isCurrentAccount() ||
          useResearchEngineStore.getState().activeRunId !== runId
        ) {
          return false;
        }
        setError('The run could not be loaded. Try again.');
        setLoading(false);
        return false;
      }
    },
    [
      runId,
      hydrateRun,
      mergeRunEvent,
      setError,
      setLoading,
      setPendingReview,
      markResumeAuthorized,
    ]
  );

  useEffect(() => {
    let cancelled = false;
    requestRef.current += 1;
    abortRef.current?.abort();
    const isCurrentAccount = captureAccountSession();
    queueMicrotask(() => {
      if (cancelled || !isCurrentAccount()) return;
      markResumeAuthorized(false);
      resetRun(runId);
      void refreshRun();
    });
    return () => {
      cancelled = true;
      requestRef.current += 1;
      abortRef.current?.abort();
    };
  }, [markResumeAuthorized, refreshRun, resetRun, runId]);

  const runStatus = activeRun?.status;
  const shouldStream =
    activeRunId === runId &&
    activeRun?.id === runId &&
    !isLoading &&
    (runStatus === 'running' || runStatus === 'pending' || resumeAuthorized);

  // Persisted run and step state is complete before newer SSE notifications
  // are attached. The store makes replayed notifications idempotent. A resume
  // POST only mints a one-use claim: the durable row stays paused until this
  // stream consumes it.
  useEffect(() => {
    if (!shouldStream) return;

    const controller = new AbortController();
    const accountSignal = getAccountSignal();
    const abortForAccount = () => controller.abort();
    accountSignal.addEventListener('abort', abortForAccount, { once: true });
    abortRef.current = controller;

    const connectSSE = async (): Promise<void> => {
      for (let attempt = 1; attempt <= SSE_MAX_ATTEMPTS; attempt += 1) {
        const token = await getAuthToken();
        if (controller.signal.aborted) return;
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

          if (controller.signal.aborted) return;
          if (response.ok && response.body) {
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let currentEventType: string | null = null;

            try {
              while (!controller.signal.aborted) {
                const { done, value } = await reader.read();
                if (done || controller.signal.aborted) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() ?? '';

                for (const line of lines) {
                  const trimmed = line.trim();

                  if (trimmed.startsWith('event: ')) {
                    currentEventType = trimmed.slice(7).trim();
                  } else if (trimmed.startsWith('data: ') && currentEventType) {
                    try {
                      const parsed = JSON.parse(
                        trimmed.slice(6)
                      ) as RunStepEvent;
                      mergeRunEvent({
                        ...parsed,
                        event: currentEventType,
                        run_id: parsed.run_id ?? runId,
                      });

                      if (
                        currentEventType === 'run_started' ||
                        currentEventType === 'run_paused' ||
                        currentEventType === 'run_complete' ||
                        currentEventType === 'run_failed'
                      ) {
                        markResumeAuthorized(false);
                      }

                      if (
                        currentEventType === 'step_complete' ||
                        currentEventType === 'step_error' ||
                        currentEventType === 'run_complete' ||
                        currentEventType === 'run_failed' ||
                        currentEventType === 'run_paused'
                      ) {
                        void refreshRun();
                      }
                    } catch {
                      // Skip a malformed notification; persisted hydration stays
                      // authoritative and the next refresh can recover it.
                    }
                    currentEventType = null;
                  }
                }
              }
            } finally {
              reader.releaseLock();
            }
          }
        } catch (err) {
          if ((err as Error).name === 'AbortError') return;
          console.error('SSE stream error:', err);
        }

        if (controller.signal.aborted) return;

        // The stream is only a notification channel. Re-read durable rows
        // after every EOF or rejected response before deciding to reconnect.
        await refreshRun();
        const latest = useResearchEngineStore.getState();
        const latestStatus = latest.activeRun?.status;
        if (
          controller.signal.aborted ||
          latest.activeRunId !== runId ||
          (latestStatus !== 'running' &&
            latestStatus !== 'pending' &&
            !resumeAuthorizedRef.current)
        ) {
          return;
        }

        if (attempt === SSE_MAX_ATTEMPTS) {
          setActionError({
            runId,
            message:
              'Live updates stopped after repeated connection failures. Refresh the run to continue.',
          });
          return;
        }

        await waitForReconnect(controller.signal, attempt);
        if (controller.signal.aborted) return;
      }
    };

    void connectSSE();

    return () => {
      accountSignal.removeEventListener('abort', abortForAccount);
      controller.abort();
      abortRef.current = null;
    };
  }, [runId, markResumeAuthorized, mergeRunEvent, refreshRun, shouldStream]);

  const refreshReviewState = useCallback(
    async (options?: { resumeAuthorized?: boolean }): Promise<void> => {
      if (options?.resumeAuthorized) markResumeAuthorized(true);
      if (!(await refreshRun(true))) {
        throw new Error('The durable run state could not be refreshed.');
      }
    },
    [markResumeAuthorized, refreshRun]
  );

  const steps: StepData[] = runSteps.map((step) => ({
    stepIndex: step.step_index,
    stepName: step.step_name,
    stepType: step.step_type,
    status: step.status,
    mode: step.mode,
    tokenCount: step.token_count,
    qualityMarks: step.quality_marks.map((mark) => ({
      check_type: mark.check_type,
      passed: mark.passed,
      details: mark.details ?? undefined,
    })),
    output: step.output,
    sources: step.sources,
    prompt: step.prompt,
    errorMessage: step.error_message,
  }));

  const sourceRecords = collectSourceRecords(runSteps);

  // Total tokens from events or run data
  const totalTokens =
    steps.reduce((sum, s) => sum + s.tokenCount, 0) ||
    activeRun?.total_tokens ||
    0;

  const handlePause = async (): Promise<void> => {
    setActionError(null);
    setActionLoading(true);
    try {
      await pauseRun(runId);
      if (!(await refreshRun())) throw new Error('Refresh failed');
    } catch {
      setActionError({
        runId,
        message: 'Pause failed. The run may have already finished.',
      }); // R6-L21
    } finally {
      setActionLoading(false);
    }
  };

  const handleResume = async (): Promise<void> => {
    setActionError(null);
    setActionLoading(true);
    try {
      await resumeRun(runId);
      markResumeAuthorized(true);
      if (!(await refreshRun())) throw new Error('Refresh failed');
    } catch {
      setActionError({
        runId,
        message: 'Resume failed. Try again in a moment.',
      }); // R6-L21
    } finally {
      setActionLoading(false);
    }
  };

  const handleContinueUnverified = async (): Promise<void> => {
    if (!activeRun?.output_hash) return;
    setActionError(null);
    setActionLoading(true);
    try {
      await resumeRun(runId, {
        continue_unverified: true,
        output_hash: activeRun.output_hash,
      });
      markResumeAuthorized(true);
      if (!(await refreshRun())) throw new Error('Refresh failed');
    } catch {
      setActionError({
        runId,
        message:
          'Unverified continuation failed. Refresh the run and review the current verification result.',
      });
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
          onClick={() => {
            void refreshRun();
          }}
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
          onClick={() =>
            activeRun?.project_id
              ? router.push(`/projects/${activeRun.project_id}?tab=workflow`)
              : router.back()
          }
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
          {activeRun?.conformance_status && (
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
              <span className="rounded-full border border-border bg-muted px-2 py-0.5 font-medium text-foreground">
                Conformance:{' '}
                {CONFORMANCE_LABEL[activeRun.conformance_status] ??
                  activeRun.conformance_status}
              </span>
              {activeRun.protocol_version_id && (
                <span className="text-muted-foreground">
                  Protocol {activeRun.protocol_version_id.slice(0, 8)}
                </span>
              )}
              {activeRun.effective_plan_hash && (
                <code className="text-muted-foreground">
                  Plan {activeRun.effective_plan_hash.slice(0, 12)}
                </code>
              )}
            </div>
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
        {activeRun?.status === 'paused' &&
          activeRun.pause_reason !== 'review_required' &&
          activeRun.pause_reason !== 'verification_failed' && (
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

      {actionError?.runId === runId && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-red-300 bg-red-50 px-4 py-2 text-sm text-red-800 dark:border-red-700 dark:bg-red-950/40 dark:text-red-200"
        >
          {actionError.message}
        </div>
      )}

      {activeRun?.status === 'paused' &&
        activeRun.pause_reason === 'verification_failed' && (
          <section className="mb-6 rounded-xl border border-(--nous-helios)/30 bg-(--nous-helios)/10 p-5">
            <div className="flex items-start gap-3">
              <ShieldAlert
                aria-hidden="true"
                className="mt-0.5 h-5 w-5 shrink-0 text-(--nous-helios)"
              />
              <div className="min-w-0 flex-1">
                <h2 className="font-semibold text-foreground">
                  Verification failed
                </h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  Continuing creates a visibly unverified draft bound to this
                  exact verification result. It cannot receive final approval.
                </p>
                <button
                  type="button"
                  onClick={handleContinueUnverified}
                  disabled={actionLoading || !activeRun.output_hash}
                  className="mt-3 inline-flex items-center gap-2 rounded-lg border border-(--nous-helios)/40 bg-background px-4 py-2 text-sm font-medium text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {actionLoading ? (
                    <Loader2
                      aria-hidden="true"
                      className="h-4 w-4 animate-spin"
                    />
                  ) : (
                    <Play aria-hidden="true" className="h-4 w-4" />
                  )}
                  Continue with an unverified draft
                </button>
                {!activeRun.output_hash && (
                  <p role="alert" className="mt-2 text-sm text-foreground">
                    Refresh the run to load the verification result hash.
                  </p>
                )}
              </div>
            </div>
          </section>
        )}

      {activeRun?.status === 'paused' &&
        activeRun.pause_reason === 'review_required' &&
        pendingReview?.pending && (
          <ReviewPanel
            key={`${runId}:${pendingReview.descriptor?.step_index}`}
            runId={runId}
            review={pendingReview}
            sourceRecords={sourceRecords}
            onRefresh={refreshReviewState}
          />
        )}

      {activeRun?.status === 'paused' &&
        activeRun.pause_reason === 'review_required' &&
        pendingReview !== null &&
        !pendingReview.pending && (
          <div
            role="alert"
            className="mb-6 rounded-xl border border-border bg-card p-4 text-sm text-foreground"
          >
            The server did not return a current review gate. Refresh the run
            before continuing.
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

      {activeRun && (
        <RunResults
          run={activeRun}
          steps={runSteps}
          finalStatus={finalStatus}
        />
      )}

      {activeRun &&
        (activeRun.status === 'completed' || activeRun.status === 'failed') && (
          <RunReproducibility
            runId={activeRun.id}
            projectId={activeRun.project_id}
          />
        )}
      {activeRun?.status === 'completed' && <RunRerun runId={activeRun.id} />}
    </div>
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function collectSourceRecords(
  steps: ReturnType<typeof useResearchEngineStore.getState>['runSteps']
): ReviewSourceRecord[] {
  const byId = new Map<string, ReviewSourceRecord>();
  for (const step of steps) {
    if (step.step_type !== 'search') continue;
    const output = asRecord(step.output);
    const records = output?.source_records;
    if (!Array.isArray(records)) continue;
    for (const item of records) {
      const source = asRecord(item);
      if (!source || typeof source.source_id !== 'string') continue;
      byId.set(source.source_id, source as ReviewSourceRecord);
    }
  }
  return [...byId.values()];
}

'use client';

/**
 * DraftGenerationProgress Component
 * Shows real-time progress of draft generation
 */

import React, { useEffect, useState } from 'react';
import { Loader2, XCircle, CheckCircle, AlertCircle } from 'lucide-react';
import type { GenerationStatus } from '@/services/projectService';
import { Button } from '@/components/ui/button';

export interface DraftGenerationProgressProps {
  status: GenerationStatus;
  onCancel?: () => void;
  onComplete?: (draftId: string) => void;
}

const statusSteps = [
  { key: 'pending', label: 'Initializing' },
  { key: 'analyzing', label: 'Analyzing documents' },
  { key: 'generating', label: 'Generating content' },
  { key: 'citing', label: 'Adding citations' },
  { key: 'finalizing', label: 'Finalizing draft' },
  { key: 'completed', label: 'Complete' },
];

export const DraftGenerationProgress: React.FC<
  DraftGenerationProgressProps
> = ({ status, onCancel, onComplete }) => {
  const [elapsed, setElapsed] = useState(0);
  // Cancel may await a network round-trip; track it so the button can't be
  // double-fired while in flight and rejections aren't unhandled (R6-L13).
  const [cancelPending, setCancelPending] = useState(false);

  useEffect(() => {
    if (status.status === 'completed' && status.draft_id && onComplete) {
      // onComplete can await follow-up fetches; swallow rejections here — the
      // parent already logs/refreshes on failure and this effect has no owner.
      Promise.resolve(onComplete(status.draft_id)).catch((err) => {
        console.error('DraftGenerationProgress onComplete failed:', err);
      });
    }
  }, [status.status, status.draft_id, onComplete]);

  const handleCancel = (): void => {
    if (!onCancel || cancelPending) return;
    setCancelPending(true);
    Promise.resolve(onCancel())
      .catch((err) => {
        console.error('DraftGenerationProgress onCancel failed:', err);
      })
      .finally(() => setCancelPending(false));
  };

  useEffect(() => {
    if (
      status.started_at &&
      !['completed', 'failed', 'cancelled', 'interrupted'].includes(
        status.status
      )
    ) {
      const startTime = new Date(status.started_at).getTime();
      const interval = setInterval(() => {
        setElapsed(Math.floor((Date.now() - startTime) / 1000));
      }, 1000);
      return () => clearInterval(interval);
    }
  }, [status.started_at, status.status]);

  const formatTime = (seconds: number): string => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const getCurrentStepIndex = () => {
    return statusSteps.findIndex((s) => s.key === status.status);
  };

  const isCompleted = status.status === 'completed';
  const isFailed = status.status === 'failed';
  const isCancelled = status.status === 'cancelled';
  const isRunning = !isCompleted && !isFailed && !isCancelled;

  return (
    <div className="bg-card border border-border rounded-lg p-6">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          {isRunning && (
            <Loader2 className="h-5 w-5 animate-spin text-primary" />
          )}
          {isCompleted && <CheckCircle className="h-5 w-5 text-primary" />}
          {isFailed && <AlertCircle className="h-5 w-5 text-destructive" />}
          {isCancelled && <XCircle className="h-5 w-5 text-muted-foreground" />}
          <div>
            <h3 className="font-medium text-foreground">
              {isCompleted
                ? 'Draft generated'
                : isFailed
                  ? 'Generation failed'
                  : isCancelled
                    ? 'Generation cancelled'
                    : 'Generating draft'}
            </h3>
            <p className="text-xs text-muted-foreground">
              {status.current_step}
            </p>
          </div>
        </div>

        {isRunning && onCancel && (
          <Button
            variant="outline"
            size="sm"
            onClick={handleCancel}
            disabled={cancelPending}
          >
            {cancelPending ? 'Cancelling…' : 'Cancel'}
          </Button>
        )}
      </div>

      <div className="mb-4">
        <div className="flex items-center justify-between mb-1">
          <span className="text-xs text-muted-foreground">Progress</span>
          <span className="text-xs text-primary tabular-nums">
            {status.progress}%
          </span>
        </div>
        <div
          className="h-2 bg-muted rounded-full overflow-hidden"
          role="progressbar"
          aria-valuenow={status.progress}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Draft generation progress"
        >
          <div
            className={`h-full transition-all duration-500 ${
              isFailed
                ? 'bg-destructive'
                : isCancelled
                  ? 'bg-muted-foreground'
                  : 'bg-linear-to-r from-primary to-primary/60'
            }`}
            style={{ width: `${status.progress}%` }}
          />
        </div>
      </div>

      <div className="space-y-2 mb-4">
        {statusSteps.slice(0, -1).map((step, idx) => {
          const currentIdx = getCurrentStepIndex();
          const isActive = idx === currentIdx;
          const isDone = idx < currentIdx || isCompleted;

          return (
            <div
              key={step.key}
              className={`flex items-center gap-3 text-sm ${
                isActive
                  ? 'text-primary'
                  : isDone
                    ? 'text-muted-foreground'
                    : 'text-muted-foreground/40'
              }`}
            >
              <div
                className={`w-5 h-5 rounded-full flex items-center justify-center ${
                  isActive
                    ? 'bg-primary/20 border border-primary'
                    : isDone
                      ? 'bg-primary/10'
                      : 'bg-muted'
                }`}
              >
                {isDone && <CheckCircle className="w-3 h-3 text-primary" />}
                {isActive && (
                  <div className="w-2 h-2 bg-primary rounded-full animate-pulse" />
                )}
              </div>
              <span>{step.label}</span>
            </div>
          );
        })}
      </div>

      <div className="flex items-center justify-between text-xs text-muted-foreground pt-4 border-t border-border">
        <span>Elapsed: {formatTime(elapsed)}</span>
        {status.duration && isCompleted && (
          <span>Completed in {status.duration.toFixed(1)}s</span>
        )}
      </div>
    </div>
  );
};

export default DraftGenerationProgress;

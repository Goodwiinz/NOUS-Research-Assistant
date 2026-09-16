'use client';

import { useState } from 'react';
import {
  Check,
  X,
  Loader2,
  Clock,
  ChevronDown,
  ChevronRight,
  AlertCircle,
  Zap,
  Compass,
} from 'lucide-react';

export interface StepData {
  stepIndex: number;
  stepName: string;
  stepType: string;
  status: 'pending' | 'running' | 'complete' | 'error';
  mode?: 'deterministic' | 'exploratory';
  tokenCount: number;
  qualityMarks: Array<{
    check_type: string;
    passed: boolean;
    details?: string;
  }>;
  output?: string | Record<string, unknown>;
  sources?: string[];
  prompt?: string;
  errorMessage?: string;
}

interface StepProgressProps {
  step: StepData;
}

export function StepProgress({ step }: StepProgressProps) {
  const [expanded, setExpanded] = useState(false);
  const coverage =
    typeof step.output === 'object' ? step.output?.coverage : null;
  const partialCoverage =
    coverage !== null &&
    typeof coverage === 'object' &&
    'partial' in coverage &&
    coverage.partial === true;
  const outputPreview = (() => {
    if (!step.output) return null;
    if (typeof step.output === 'string') return step.output;
    try {
      return JSON.stringify(step.output, null, 2);
    } catch {
      return String(step.output);
    }
  })();

  const statusLabel = (() => {
    switch (step.status) {
      case 'pending':
        return 'Pending';
      case 'running':
        return 'Running';
      case 'complete':
        return 'Complete';
      case 'error':
        return 'Error';
    }
  })();

  const statusIcon = () => {
    switch (step.status) {
      case 'pending':
        return (
          <Clock aria-hidden="true" className="h-4 w-4 text-muted-foreground" />
        );
      case 'running':
        return (
          <Loader2
            aria-hidden="true"
            className="h-4 w-4 text-primary motion-safe:animate-spin"
          />
        );
      case 'complete':
        return (
          <Check aria-hidden="true" className="h-4 w-4 text-(--nous-terra)" />
        );
      case 'error':
        return (
          <AlertCircle
            aria-hidden="true"
            className="h-4 w-4 text-(--nous-mars)"
          />
        );
    }
  };

  const statusBorder = () => {
    switch (step.status) {
      case 'pending':
        return 'border-border';
      case 'running':
        return 'border-primary/40';
      case 'complete':
        return 'border-(--nous-terra)/40';
      case 'error':
        return 'border-(--nous-mars)/40';
    }
  };

  return (
    <div
      className={`rounded-xl border bg-card shadow-xs transition-colors ${statusBorder()}`}
    >
      {/* Header row */}
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-3 rounded-xl px-4 py-3 text-left transition-colors hover:bg-muted/40 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
      >
        {expanded ? (
          <ChevronDown
            aria-hidden="true"
            className="h-4 w-4 shrink-0 text-muted-foreground"
          />
        ) : (
          <ChevronRight
            aria-hidden="true"
            className="h-4 w-4 shrink-0 text-muted-foreground"
          />
        )}

        <span className="flex shrink-0 items-center" title={statusLabel}>
          {statusIcon()}
          <span className="sr-only">{statusLabel}</span>
        </span>

        <span className="flex-1 truncate text-sm font-medium text-foreground">
          <span className="mr-2 text-muted-foreground tabular-nums">
            #{step.stepIndex + 1}
          </span>
          {step.stepName}
        </span>

        {/* Type badge */}
        <span className="rounded-md border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground">
          {step.stepType}
        </span>

        {/* Mode badge */}
        {step.mode && (
          <span
            className={`flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs ${
              step.mode === 'deterministic'
                ? 'border-primary/20 bg-primary/10 text-primary'
                : 'border-(--nous-helios)/20 bg-(--nous-helios)/10 text-(--nous-helios)'
            }`}
          >
            {step.mode === 'deterministic' ? (
              <Zap aria-hidden="true" className="h-3 w-3" />
            ) : (
              <Compass aria-hidden="true" className="h-3 w-3" />
            )}
            {step.mode === 'deterministic' ? 'Deterministic' : 'Exploratory'}
          </span>
        )}

        {/* Quality marks */}
        {step.status === 'complete' && step.qualityMarks.length > 0 && (
          <div className="flex items-center gap-1">
            {step.qualityMarks.map((mark, i) => (
              <span
                key={i}
                title={`${mark.check_type}: ${mark.passed ? 'passed' : 'failed'}${mark.details ? ` - ${mark.details}` : ''}`}
              >
                {mark.passed ? (
                  <Check
                    aria-hidden="true"
                    className="h-3.5 w-3.5 text-(--nous-terra)"
                  />
                ) : (
                  <X
                    aria-hidden="true"
                    className="h-3.5 w-3.5 text-(--nous-mars)"
                  />
                )}
                <span className="sr-only">
                  {mark.check_type} {mark.passed ? 'passed' : 'failed'}
                </span>
              </span>
            ))}
          </div>
        )}

        {/* Token count */}
        {step.tokenCount > 0 && (
          <span className="text-xs text-muted-foreground tabular-nums">
            {step.tokenCount.toLocaleString()} tok
          </span>
        )}
      </button>

      {partialCoverage && (
        <p role="alert" className="px-4 pb-3 text-sm text-muted-foreground">
          Some selected databases could not be searched. These results have
          partial coverage.
        </p>
      )}

      {/* Expandable details */}
      {expanded && (
        <div className="space-y-3 border-t border-border px-4 pb-4">
          {/* Error message */}
          {step.errorMessage && (
            <div
              role="alert"
              className="mt-3 rounded-lg border border-(--nous-mars)/30 bg-(--nous-mars)/10 p-3"
            >
              <p className="text-xs text-(--nous-mars)">{step.errorMessage}</p>
            </div>
          )}

          {/* Output preview */}
          {outputPreview && (
            <div className="mt-3">
              <h4 className="mb-1 text-xs font-medium text-muted-foreground">
                Output
              </h4>
              <div className="max-h-40 overflow-auto rounded-lg border border-border bg-muted/40 p-3">
                <p className="whitespace-pre-wrap font-mono text-xs text-foreground">
                  {outputPreview.length > 500
                    ? outputPreview.slice(0, 500) + '...'
                    : outputPreview}
                </p>
              </div>
            </div>
          )}

          {/* Sources used */}
          {step.sources && step.sources.length > 0 && (
            <div>
              <h4 className="mb-1 text-xs font-medium text-muted-foreground">
                Sources
              </h4>
              <div className="flex flex-wrap gap-1">
                {step.sources.map((source, i) => (
                  <span
                    key={i}
                    className="rounded-md border border-primary/20 bg-primary/10 px-2 py-0.5 text-xs text-primary"
                  >
                    {source}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Prompt (collapsible) */}
          {step.prompt && <PromptSection prompt={step.prompt} />}

          {/* Quality check details */}
          {step.qualityMarks.length > 0 && (
            <div>
              <h4 className="mb-1 text-xs font-medium text-muted-foreground">
                Quality checks
              </h4>
              <div className="space-y-1">
                {step.qualityMarks.map((mark, i) => (
                  <div key={i} className="flex items-center gap-2 text-xs">
                    {mark.passed ? (
                      <Check
                        aria-hidden="true"
                        className="h-3 w-3 text-(--nous-terra)"
                      />
                    ) : (
                      <X
                        aria-hidden="true"
                        className="h-3 w-3 text-(--nous-mars)"
                      />
                    )}
                    <span className="text-foreground">{mark.check_type}</span>
                    {mark.details && (
                      <span className="text-muted-foreground">
                        - {mark.details}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function PromptSection({ prompt }: { prompt: string }) {
  const [showPrompt, setShowPrompt] = useState(false);

  return (
    <div>
      <button
        type="button"
        onClick={() => setShowPrompt(!showPrompt)}
        aria-expanded={showPrompt}
        className="flex items-center gap-1 rounded text-xs font-medium text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
      >
        {showPrompt ? (
          <ChevronDown aria-hidden="true" className="h-3 w-3" />
        ) : (
          <ChevronRight aria-hidden="true" className="h-3 w-3" />
        )}
        Prompt
      </button>
      {showPrompt && (
        <div className="mt-1 max-h-60 overflow-auto rounded-lg border border-border bg-muted/40 p-3">
          <pre className="whitespace-pre-wrap font-mono text-xs text-muted-foreground">
            {prompt}
          </pre>
        </div>
      )}
    </div>
  );
}

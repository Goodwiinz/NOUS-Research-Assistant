'use client';

import { useState, type ReactElement } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Checkbox } from '@/components/ui/checkbox';
import type { DailyBriefScopeConfirmation } from '@/services/researchEngineService';
import { SourceSelector } from './SourceSelector';

const MIN_RESULTS = 1;
const MAX_RESULTS = 50;

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

function criteriaFromText(value: string): string[] {
  return value
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean);
}

function boundedLimit(value: unknown): number {
  const parsed =
    typeof value === 'number' ? value : Number.parseInt(`${value}`, 10);
  if (!Number.isFinite(parsed)) return MIN_RESULTS;
  return Math.min(MAX_RESULTS, Math.max(MIN_RESULTS, Math.trunc(parsed)));
}

export interface DailyResearchBriefSetupProps {
  initialParameters: Record<string, unknown>;
  onChange: (
    parameters: Record<string, unknown>,
    confirmation: DailyBriefScopeConfirmation | null
  ) => void;
  disabled?: boolean;
}

export function DailyResearchBriefSetup({
  initialParameters,
  onChange,
  disabled = false,
}: DailyResearchBriefSetupProps): ReactElement {
  const [parameters, setParameters] = useState<Record<string, unknown>>(() => ({
    ...initialParameters,
    research_question: stringValue(initialParameters.research_question),
    inclusion_criteria: stringList(initialParameters.inclusion_criteria),
    exclusion_criteria: stringList(initialParameters.exclusion_criteria),
    providers: stringList(initialParameters.providers),
    limit_per_provider: boundedLimit(
      initialParameters.limit_per_provider ?? 25
    ),
    notes: stringValue(initialParameters.notes),
  }));
  const [confirmed, setConfirmed] = useState(false);

  const researchQuestion = stringValue(parameters.research_question);
  const inclusionCriteria = stringList(parameters.inclusion_criteria);
  const exclusionCriteria = stringList(parameters.exclusion_criteria);
  const providers = stringList(parameters.providers);
  const limitPerProvider = boundedLimit(parameters.limit_per_provider);
  const notes = stringValue(parameters.notes);
  const canConfirm =
    researchQuestion.trim().length > 0 &&
    inclusionCriteria.length > 0 &&
    providers.length >= 1 &&
    providers.length <= 4 &&
    limitPerProvider >= MIN_RESULTS &&
    limitPerProvider <= MAX_RESULTS;

  const updateParameters = (patch: Record<string, unknown>): void => {
    const next = { ...parameters, ...patch };
    setParameters(next);
    setConfirmed(false);
    onChange(next, null);
  };

  const handleConfirmation = (checked: boolean): void => {
    if (!checked || !canConfirm) {
      setConfirmed(false);
      onChange(parameters, null);
      return;
    }

    const confirmation: DailyBriefScopeConfirmation = {
      confirmed: true,
      research_question: researchQuestion.trim(),
      inclusion_criteria: inclusionCriteria,
      exclusion_criteria: exclusionCriteria,
      providers,
      limit_per_provider: limitPerProvider,
      notes,
    };
    setConfirmed(true);
    onChange(parameters, confirmation);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-foreground">
        <AlertTriangle
          aria-hidden="true"
          className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400"
        />
        <p>
          This is a bounded brief from the selected sources. It is not an
          exhaustive or systematic review.
        </p>
      </div>

      <div>
        <label
          htmlFor="daily-brief-question"
          className="mb-1 block text-xs font-medium text-muted-foreground"
        >
          Research question
        </label>
        <textarea
          id="daily-brief-question"
          value={researchQuestion}
          onChange={(event) =>
            updateParameters({ research_question: event.target.value })
          }
          disabled={disabled}
          rows={3}
          className="w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground focus:border-primary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        />
      </div>

      <div>
        <label
          htmlFor="daily-brief-include"
          className="mb-1 block text-xs font-medium text-muted-foreground"
        >
          What to include
        </label>
        <textarea
          id="daily-brief-include"
          value={inclusionCriteria.join('\n')}
          onChange={(event) =>
            updateParameters({
              inclusion_criteria: criteriaFromText(event.target.value),
            })
          }
          disabled={disabled}
          rows={3}
          placeholder="One criterion per line"
          className="w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/60 focus:border-primary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        />
      </div>

      <div>
        <label
          htmlFor="daily-brief-exclude"
          className="mb-1 block text-xs font-medium text-muted-foreground"
        >
          What to exclude
        </label>
        <textarea
          id="daily-brief-exclude"
          value={exclusionCriteria.join('\n')}
          onChange={(event) =>
            updateParameters({
              exclusion_criteria: criteriaFromText(event.target.value),
            })
          }
          disabled={disabled}
          rows={3}
          placeholder="One criterion per line"
          className="w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/60 focus:border-primary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        />
      </div>

      <div>
        <span className="mb-1 block text-xs font-medium text-muted-foreground">
          Sources
        </span>
        <SourceSelector
          selected={providers}
          onChange={(nextProviders) =>
            updateParameters({ providers: nextProviders })
          }
          disabled={disabled}
        />
      </div>

      <div>
        <label
          htmlFor="daily-brief-limit"
          className="mb-1 block text-xs font-medium text-muted-foreground"
        >
          Results per source
        </label>
        <input
          id="daily-brief-limit"
          type="number"
          min={MIN_RESULTS}
          max={MAX_RESULTS}
          value={limitPerProvider}
          onChange={(event) =>
            updateParameters({
              limit_per_provider: boundedLimit(event.target.value),
            })
          }
          disabled={disabled}
          className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground focus:border-primary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        />
      </div>

      <div>
        <label
          htmlFor="daily-brief-notes"
          className="mb-1 block text-xs font-medium text-muted-foreground"
        >
          Notes
        </label>
        <textarea
          id="daily-brief-notes"
          value={notes}
          onChange={(event) => updateParameters({ notes: event.target.value })}
          disabled={disabled}
          rows={2}
          className="w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground focus:border-primary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        />
      </div>

      <label
        htmlFor="daily-brief-confirm"
        className={
          canConfirm && !disabled
            ? 'flex cursor-pointer items-start gap-2 text-sm text-foreground'
            : 'flex cursor-not-allowed items-start gap-2 text-sm text-muted-foreground'
        }
      >
        <Checkbox
          id="daily-brief-confirm"
          checked={confirmed}
          onCheckedChange={(value) => handleConfirmation(value === true)}
          disabled={disabled || !canConfirm}
          aria-label="Confirm scope"
          className="mt-0.5"
        />
        <span>Confirm scope</span>
      </label>
    </div>
  );
}

export default DailyResearchBriefSetup;

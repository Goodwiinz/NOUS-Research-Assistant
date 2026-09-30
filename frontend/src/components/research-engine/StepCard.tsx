'use client';

import { useState, type ReactElement, type ReactNode } from 'react';
import {
  ChevronDown,
  ChevronUp,
  ArrowUp,
  ArrowDown,
  Trash2,
  Search,
  FileText,
  Layers,
  Zap,
} from 'lucide-react';
import type { BlueprintStepDef } from '@/services/researchEngineService';

const STEP_TYPE_ICONS: Record<string, ReactNode> = {
  search: <Search aria-hidden="true" className="h-4 w-4" />,
  screen: <Search aria-hidden="true" className="h-4 w-4" />,
  extract: <FileText aria-hidden="true" className="h-4 w-4" />,
  synthesize: <Layers aria-hidden="true" className="h-4 w-4" />,
  verify: <Zap aria-hidden="true" className="h-4 w-4" />,
  export: <FileText aria-hidden="true" className="h-4 w-4" />,
};

const MODEL_OPTIONS = [
  { value: 'gpt-4o', label: 'GPT-4o' },
  { value: 'gpt-4o-mini', label: 'GPT-4o Mini' },
  { value: 'claude-sonnet-4-20250514', label: 'Claude Sonnet' },
  { value: 'claude-haiku-4-20250414', label: 'Claude Haiku' },
];

export interface StepCardProps {
  step: BlueprintStepDef;
  index: number;
  totalSteps: number;
  onChange: (updated: BlueprintStepDef) => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onRemove: () => void;
}

export function StepCard({
  step,
  index,
  totalSteps,
  onChange,
  onMoveUp,
  onMoveDown,
  onRemove,
}: StepCardProps): ReactElement {
  const [expanded, setExpanded] = useState(false);
  const [paramsText, setParamsText] = useState(
    JSON.stringify(step.parameters ?? {}, null, 2)
  );
  const [paramsError, setParamsError] = useState<string | null>(null);

  const icon = STEP_TYPE_ICONS[step.type] || (
    <Zap aria-hidden="true" className="h-4 w-4" />
  );

  // One accent only: deterministic reads as the warm Sol primary;
  // exploratory reads as a quieter neutral chip.
  const modeChip =
    step.mode === 'deterministic'
      ? 'bg-primary/10 text-primary border-primary/30'
      : 'bg-muted text-muted-foreground border-border';

  const handleParamsChange = (value: string): void => {
    setParamsText(value);
    try {
      const parsed = JSON.parse(value) as Record<string, unknown>;
      setParamsError(null);
      onChange({ ...step, parameters: parsed });
    } catch {
      setParamsError('Invalid JSON');
    }
  };

  const handleModeToggle = (): void => {
    const newMode =
      step.mode === 'deterministic' ? 'exploratory' : 'deterministic';
    const updates: Partial<BlueprintStepDef> = { mode: newMode };
    if (newMode === 'deterministic') {
      updates.temperature = 0;
    }
    onChange({ ...step, ...updates });
  };

  return (
    <div className="rounded-xl border border-border bg-card shadow-xs overflow-hidden">
      {/* Header */}
      <button
        type="button"
        className="flex items-center gap-3 w-full p-3 text-left hover:bg-muted/40 transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        <span className="flex items-center justify-center w-7 h-7 rounded-lg bg-muted text-xs font-medium text-muted-foreground tabular-nums shrink-0">
          {index + 1}
        </span>

        <span className="text-muted-foreground shrink-0">{icon}</span>

        <span className="px-2 py-0.5 rounded border text-[11px] font-medium bg-muted text-foreground border-border shrink-0">
          {step.type}
        </span>

        <span className="text-sm text-foreground truncate flex-1">
          {step.name || 'Untitled step'}
        </span>

        <span
          className={`px-2 py-0.5 border rounded text-[11px] font-medium shrink-0 ${modeChip}`}
        >
          {step.mode}
        </span>

        {expanded ? (
          <ChevronUp
            aria-hidden="true"
            className="h-4 w-4 text-muted-foreground shrink-0"
          />
        ) : (
          <ChevronDown
            aria-hidden="true"
            className="h-4 w-4 text-muted-foreground shrink-0"
          />
        )}
      </button>

      {/* Expanded body */}
      {expanded && (
        <div className="border-t border-border p-4 space-y-4">
          {/* Name */}
          <div>
            <label
              htmlFor={`step-${index}-name`}
              className="block text-xs font-medium text-muted-foreground mb-1"
            >
              Step name
            </label>
            <input
              id={`step-${index}-name`}
              type="text"
              value={step.name}
              onChange={(e) => onChange({ ...step, name: e.target.value })}
              className="w-full px-3 py-2 rounded-lg bg-background border border-border text-sm text-foreground focus:outline-hidden focus:border-primary focus-visible:ring-2 focus-visible:ring-ring transition-colors"
            />
          </div>

          {/* Type */}
          <div>
            <label
              htmlFor={`step-${index}-type`}
              className="block text-xs font-medium text-muted-foreground mb-1"
            >
              Step type
            </label>
            <select
              id={`step-${index}-type`}
              value={step.type}
              onChange={(e) =>
                onChange({
                  ...step,
                  type: e.target.value as BlueprintStepDef['type'],
                })
              }
              className="w-full px-3 py-2 rounded-lg bg-background border border-border text-sm text-foreground focus:outline-hidden focus:border-primary focus-visible:ring-2 focus-visible:ring-ring transition-colors"
            >
              <option value="search">Search</option>
              <option value="screen">Screen</option>
              <option value="extract">Extract</option>
              <option value="synthesize">Synthesize</option>
              <option value="verify">Verify</option>
              <option value="export">Export</option>
            </select>
          </div>

          {/* Description */}
          <div>
            <label
              htmlFor={`step-${index}-description`}
              className="block text-xs font-medium text-muted-foreground mb-1"
            >
              Description
            </label>
            <textarea
              id={`step-${index}-description`}
              value={step.description || ''}
              onChange={(e) =>
                onChange({ ...step, description: e.target.value || undefined })
              }
              rows={2}
              className="w-full px-3 py-2 rounded-lg bg-background border border-border text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-hidden focus:border-primary focus-visible:ring-2 focus-visible:ring-ring resize-none transition-colors"
              placeholder="What this step does"
            />
          </div>

          {/* Parameters JSON */}
          <div>
            <label
              htmlFor={`step-${index}-params`}
              className="block text-xs font-medium text-muted-foreground mb-1"
            >
              Parameters (JSON)
            </label>
            <textarea
              id={`step-${index}-params`}
              value={paramsText}
              onChange={(e) => handleParamsChange(e.target.value)}
              rows={4}
              aria-invalid={!!paramsError}
              className={`w-full px-3 py-2 rounded-lg bg-background border text-sm font-mono text-foreground focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring resize-none transition-colors ${
                paramsError
                  ? 'border-destructive/50 focus:border-destructive'
                  : 'border-border focus:border-primary'
              }`}
            />
            {paramsError && (
              <p role="alert" className="mt-1 text-xs text-destructive">
                {paramsError}
              </p>
            )}
          </div>

          {/* Model selector */}
          <div>
            <label
              htmlFor={`step-${index}-model`}
              className="block text-xs font-medium text-muted-foreground mb-1"
            >
              Model
            </label>
            <select
              id={`step-${index}-model`}
              value={step.model_id || ''}
              onChange={(e) =>
                onChange({ ...step, model_id: e.target.value || undefined })
              }
              className="w-full px-3 py-2 rounded-lg bg-background border border-border text-sm text-foreground focus:outline-hidden focus:border-primary focus-visible:ring-2 focus-visible:ring-ring transition-colors"
            >
              <option value="">Default</option>
              {MODEL_OPTIONS.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </div>

          {/* Mode toggle */}
          <div>
            <span className="block text-xs font-medium text-muted-foreground mb-1">
              Mode
            </span>
            <button
              type="button"
              onClick={handleModeToggle}
              aria-pressed={step.mode === 'exploratory'}
              className={`px-3 py-1.5 border rounded-lg text-xs font-medium transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 ${modeChip}`}
            >
              {step.mode}
            </button>
          </div>

          {/* Temperature slider */}
          <div>
            <label
              htmlFor={`step-${index}-temperature`}
              className="block text-xs font-medium text-muted-foreground mb-1"
            >
              Temperature{' '}
              {step.mode === 'deterministic' && (
                <span className="text-muted-foreground/60">(disabled)</span>
              )}
            </label>
            <div className="flex items-center gap-3">
              <input
                id={`step-${index}-temperature`}
                type="range"
                min={0}
                max={2}
                step={0.1}
                value={step.temperature ?? 0.7}
                onChange={(e) =>
                  onChange({ ...step, temperature: parseFloat(e.target.value) })
                }
                disabled={step.mode === 'deterministic'}
                className="flex-1 accent-primary disabled:opacity-30"
              />
              <span className="text-xs text-muted-foreground tabular-nums w-8 text-right">
                {(step.temperature ?? 0.7).toFixed(1)}
              </span>
            </div>
          </div>

          {/* Seed input */}
          <div>
            <label
              htmlFor={`step-${index}-seed`}
              className="block text-xs font-medium text-muted-foreground mb-1"
            >
              Seed
            </label>
            <input
              id={`step-${index}-seed`}
              type="number"
              value={step.seed ?? ''}
              onChange={(e) =>
                onChange({
                  ...step,
                  seed: e.target.value
                    ? parseInt(e.target.value, 10)
                    : undefined,
                })
              }
              placeholder="Optional seed for reproducibility"
              className="w-full px-3 py-2 rounded-lg bg-background border border-border text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-hidden focus:border-primary focus-visible:ring-2 focus-visible:ring-ring transition-colors"
            />
          </div>

          {/* Action buttons */}
          <div className="flex items-center gap-2 pt-2 border-t border-border">
            <button
              type="button"
              onClick={onMoveUp}
              disabled={index === 0}
              className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted/50 disabled:opacity-30 disabled:cursor-not-allowed transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              aria-label="Move step up"
            >
              <ArrowUp aria-hidden="true" className="h-4 w-4" />
            </button>
            <button
              type="button"
              onClick={onMoveDown}
              disabled={index === totalSteps - 1}
              className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted/50 disabled:opacity-30 disabled:cursor-not-allowed transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              aria-label="Move step down"
            >
              <ArrowDown aria-hidden="true" className="h-4 w-4" />
            </button>
            <div className="flex-1" />
            <button
              type="button"
              onClick={onRemove}
              className="p-1.5 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/5 transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              aria-label="Remove step"
            >
              <Trash2 aria-hidden="true" className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default StepCard;

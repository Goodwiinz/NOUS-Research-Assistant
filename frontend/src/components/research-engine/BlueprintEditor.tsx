'use client';

import { useCallback, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  AlertCircle,
  ArrowLeft,
  Loader2,
  Play,
  Plus,
  Save,
} from 'lucide-react';
import {
  getProject,
  getBlueprint,
  createBlueprint,
  startRun,
} from '@/services/researchEngineService';
import type { BlueprintStepDef } from '@/services/researchEngineService';
import type { ResearchProject } from '@/store/research-engine-store';
import type { SourceConnectorType } from '@/types/scispace';
import { StepCard } from './StepCard';
import { SourceSelector } from './SourceSelector';
import { TemplateSelector } from './TemplateSelector';

interface Blueprint {
  id: string;
  name: string;
  steps: BlueprintStepDef[];
  parameters: Record<string, unknown>;
  template_source?: string;
}

interface TemplateData {
  slug: string;
  name: string;
  description?: string;
  step_count: number;
  steps?: BlueprintStepDef[];
  parameters?: Record<string, unknown>;
}

let _stepKeyCounter = 0;
function nextStepKey(): string {
  return `step_${Date.now()}_${_stepKeyCounter++}`;
}

type StepWithKey = BlueprintStepDef & { _key: string };

function withKey(step: BlueprintStepDef): StepWithKey {
  return { ...step, _key: nextStepKey() };
}

const DEFAULT_STEP: BlueprintStepDef = {
  type: 'search',
  name: '',
  description: '',
  parameters: {},
  mode: 'deterministic',
};

export interface BlueprintEditorProps {
  projectId: string;
}

async function loadProjectBlueprint(projectId: string): Promise<{
  project: ResearchProject;
  blueprint?: Blueprint;
}> {
  const project = (await getProject(projectId)) as
    (ResearchProject & { blueprint_id?: string }) | undefined;
  if (!project) throw new Error('Project not found');
  if (project.blueprint_id) {
    try {
      const blueprint = (await getBlueprint(project.blueprint_id)) as
        Blueprint | undefined;
      return { project, blueprint };
    } catch {
      // A missing blueprint opens the template selector.
    }
  }
  return { project };
}

export function BlueprintEditor({ projectId }: BlueprintEditorProps) {
  const router = useRouter();

  const [project, setProject] = useState<ResearchProject | null>(null);
  const [blueprint, setBlueprint] = useState<Blueprint | null>(null);
  const [showTemplateSelector, setShowTemplateSelector] = useState(false);

  // Editor state
  const [blueprintName, setBlueprintName] = useState('');
  const [steps, setSteps] = useState<StepWithKey[]>([]);
  const [globalParams, setGlobalParams] = useState<Record<string, unknown>>({});
  const [globalParamsText, setGlobalParamsText] = useState('{}');
  const [globalParamsError, setGlobalParamsError] = useState<string | null>(
    null
  );

  // UI state
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(() => {
    return loadProjectBlueprint(projectId)
      .then(({ project: proj, blueprint: bp }) => {
        setError(null);
        setProject(proj);
        if (bp) {
          setBlueprint(bp);
          setBlueprintName(bp.name);
          setSteps(bp.steps.map(withKey));
          setGlobalParams(bp.parameters);
          setGlobalParamsText(JSON.stringify(bp.parameters, null, 2));
          setShowTemplateSelector(false);
        } else {
          setShowTemplateSelector(true);
        }
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : 'Failed to load project');
      })
      .finally(() => setLoading(false));
  }, [projectId]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleTemplateSelect = (template: TemplateData | null) => {
    setShowTemplateSelector(false);
    if (template) {
      setBlueprintName(template.name);
      setSteps((template.steps ?? []).map(withKey));
      const params = template.parameters ?? {};
      setGlobalParams(params);
      setGlobalParamsText(JSON.stringify(params, null, 2));
    } else {
      // Blank blueprint
      setBlueprintName(
        project?.name ? `${project.name} Blueprint` : 'New Blueprint'
      );
      setSteps([]);
      setGlobalParams({});
      setGlobalParamsText('{}');
    }
  };

  const handleGlobalParamsChange = (value: string) => {
    setGlobalParamsText(value);
    try {
      const parsed = JSON.parse(value) as Record<string, unknown>;
      setGlobalParamsError(null);
      setGlobalParams(parsed);
    } catch {
      setGlobalParamsError('Invalid JSON');
    }
  };

  const handleStepChange = (index: number, updated: BlueprintStepDef) => {
    setSteps((prev) =>
      prev.map((s, i) => (i === index ? { ...updated, _key: s._key } : s))
    );
  };

  const handleMoveStep = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= steps.length) return;
    setSteps((prev) => {
      const next = [...prev];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  const handleRemoveStep = (index: number) => {
    setSteps((prev) => prev.filter((_, i) => i !== index));
  };

  const handleAddStep = () => {
    setSteps((prev) => [...prev, withKey({ ...DEFAULT_STEP })]);
  };

  const handleSave = async () => {
    if (!blueprintName.trim()) return;
    if (globalParamsError) return;

    setSaving(true);
    setError(null);
    try {
      const cleanSteps = steps.map(({ _key, ...rest }) => rest);
      const bp = (await createBlueprint(projectId, {
        name: blueprintName.trim(),
        steps: cleanSteps,
        parameters: globalParams,
      })) as Blueprint | undefined;

      if (bp) {
        setBlueprint(bp);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save blueprint');
    } finally {
      setSaving(false);
    }
  };

  const handleStartRun = async () => {
    if (!blueprint?.id) return;

    setStarting(true);
    setError(null);
    try {
      const run = (await startRun(blueprint.id, globalParams)) as
        { id: string } | undefined;
      if (run?.id) {
        router.push(`/research-engine/runs/${run.id}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start run');
      setStarting(false);
    }
  };

  // Loading state
  if (loading) {
    return (
      <div className="space-y-6">
        <div className="h-4 w-32 rounded bg-muted animate-pulse" />
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-2 flex-1">
            <div className="h-7 w-1/3 rounded bg-muted animate-pulse" />
            <div className="h-4 w-1/4 rounded bg-muted animate-pulse" />
          </div>
          <div className="h-9 w-20 rounded-lg bg-muted animate-pulse" />
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 space-y-3">
            <div className="h-14 rounded-lg bg-muted animate-pulse" />
            <div className="h-14 rounded-lg bg-muted animate-pulse" />
            <div className="h-14 rounded-lg bg-muted animate-pulse" />
          </div>
          <div className="h-64 rounded-xl bg-muted animate-pulse" />
        </div>
        <span className="sr-only" role="status">
          Loading project
        </span>
      </div>
    );
  }

  // Template selector
  if (showTemplateSelector) {
    return (
      <div>
        <button
          type="button"
          onClick={() => router.push('/research-engine')}
          className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground mb-6 transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 rounded"
        >
          <ArrowLeft aria-hidden="true" className="h-4 w-4" />
          Back to projects
        </button>
        {project && (
          <h1 className="text-xl font-semibold text-foreground mb-6">
            {project.name}
          </h1>
        )}
        <TemplateSelector onSelect={handleTemplateSelect} />
      </div>
    );
  }

  // Editor
  return (
    <div>
      {/* Navigation */}
      <button
        type="button"
        onClick={() => router.push('/research-engine')}
        className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground mb-6 transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 rounded"
      >
        <ArrowLeft aria-hidden="true" className="h-4 w-4" />
        Back to projects
      </button>

      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-start justify-between gap-4 mb-6">
        <div className="flex-1 min-w-0">
          <label htmlFor="blueprint-name" className="sr-only">
            Blueprint name
          </label>
          <input
            id="blueprint-name"
            type="text"
            value={blueprintName}
            onChange={(e) => setBlueprintName(e.target.value)}
            className="text-xl font-semibold bg-transparent text-foreground border-none outline-hidden w-full focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 rounded placeholder:text-muted-foreground/60 transition-colors"
            placeholder="Blueprint name"
          />
          {project && (
            <p className="text-sm text-muted-foreground mt-1">
              Project: {project.name}
            </p>
          )}
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <button
            type="button"
            onClick={handleSave}
            disabled={saving || !blueprintName.trim() || !!globalParamsError}
            className="flex items-center gap-2 px-4 py-2 rounded-lg border border-border bg-card text-sm font-medium text-foreground hover:border-primary/40 hover:bg-muted/50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            {saving ? (
              <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
            ) : (
              <Save aria-hidden="true" className="h-4 w-4" />
            )}
            {saving ? 'Saving' : 'Save'}
          </button>

          {blueprint && (
            <button
              type="button"
              onClick={handleStartRun}
              disabled={starting || steps.length === 0}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            >
              {starting ? (
                <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
              ) : (
                <Play aria-hidden="true" className="h-4 w-4" />
              )}
              {starting ? 'Starting' : 'Start run'}
            </button>
          )}
        </div>
      </div>

      {/* Error */}
      {error && (
        <div
          role="alert"
          className="flex items-start gap-3 p-4 rounded-xl border border-destructive/30 bg-destructive/5 mb-6"
        >
          <AlertCircle
            aria-hidden="true"
            className="h-5 w-5 text-destructive shrink-0 mt-0.5"
          />
          <div className="flex-1">
            <p className="text-sm text-foreground">{error}</p>
            <button
              type="button"
              onClick={() => {
                setLoading(true);
                setError(null);
                void fetchData();
              }}
              className="mt-1 text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 rounded"
            >
              Retry
            </button>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Steps list */}
        <div className="lg:col-span-2 space-y-3">
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-sm font-medium text-foreground">
              Steps ({steps.length})
            </h2>
            <button
              type="button"
              onClick={handleAddStep}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-primary rounded-lg border border-border hover:border-primary/40 hover:bg-primary/5 transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            >
              <Plus aria-hidden="true" className="h-3.5 w-3.5" />
              Add step
            </button>
          </div>

          {steps.length === 0 ? (
            <div className="text-center py-12 rounded-xl border border-dashed border-border bg-muted/20">
              <p className="text-sm text-muted-foreground mb-3">
                No steps yet. Add a step to define what this blueprint does.
              </p>
              <button
                type="button"
                onClick={handleAddStep}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-primary rounded-lg border border-border hover:border-primary/40 hover:bg-primary/5 transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              >
                <Plus aria-hidden="true" className="h-3.5 w-3.5" />
                Add your first step
              </button>
            </div>
          ) : (
            steps.map((step, idx) => (
              <StepCard
                key={step._key}
                step={step}
                index={idx}
                totalSteps={steps.length}
                onChange={(updated) => handleStepChange(idx, updated)}
                onMoveUp={() => handleMoveStep(idx, -1)}
                onMoveDown={() => handleMoveStep(idx, 1)}
                onRemove={() => handleRemoveStep(idx)}
              />
            ))
          )}
        </div>

        {/* Global parameters sidebar */}
        <div className="space-y-4">
          <div className="rounded-xl border border-border bg-card shadow-xs p-4">
            <h3 className="text-sm font-medium text-foreground mb-3">
              Global parameters
            </h3>

            {/* Quick fields for common params */}
            <div className="space-y-3 mb-4">
              <div>
                <label
                  htmlFor="param-topic"
                  className="block text-xs font-medium text-muted-foreground mb-1"
                >
                  Topic
                </label>
                <input
                  id="param-topic"
                  type="text"
                  value={(globalParams.topic as string) ?? ''}
                  onChange={(e) =>
                    setGlobalParams((prev) => {
                      const next = {
                        ...prev,
                        topic: e.target.value || undefined,
                      };
                      if (!next.topic) delete next.topic;
                      setGlobalParamsText(JSON.stringify(next, null, 2));
                      return next;
                    })
                  }
                  placeholder="Research topic"
                  className="w-full px-3 py-2 rounded-lg bg-background border border-border text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-hidden focus:border-primary focus-visible:ring-2 focus-visible:ring-ring transition-colors"
                />
              </div>

              <div>
                <label
                  htmlFor="param-date-start"
                  className="block text-xs font-medium text-muted-foreground mb-1"
                >
                  Date range start
                </label>
                <input
                  id="param-date-start"
                  type="date"
                  value={(globalParams.date_start as string) ?? ''}
                  onChange={(e) =>
                    setGlobalParams((prev) => {
                      const next = {
                        ...prev,
                        date_start: e.target.value || undefined,
                      };
                      if (!next.date_start) delete next.date_start;
                      setGlobalParamsText(JSON.stringify(next, null, 2));
                      return next;
                    })
                  }
                  className="w-full px-3 py-2 rounded-lg bg-background border border-border text-sm text-foreground focus:outline-hidden focus:border-primary focus-visible:ring-2 focus-visible:ring-ring transition-colors"
                />
              </div>

              <div>
                <label
                  htmlFor="param-date-end"
                  className="block text-xs font-medium text-muted-foreground mb-1"
                >
                  Date range end
                </label>
                <input
                  id="param-date-end"
                  type="date"
                  value={(globalParams.date_end as string) ?? ''}
                  onChange={(e) =>
                    setGlobalParams((prev) => {
                      const next = {
                        ...prev,
                        date_end: e.target.value || undefined,
                      };
                      if (!next.date_end) delete next.date_end;
                      setGlobalParamsText(JSON.stringify(next, null, 2));
                      return next;
                    })
                  }
                  className="w-full px-3 py-2 rounded-lg bg-background border border-border text-sm text-foreground focus:outline-hidden focus:border-primary focus-visible:ring-2 focus-visible:ring-ring transition-colors"
                />
              </div>

              <div>
                <span className="block text-xs font-medium text-muted-foreground mb-1">
                  Search sources
                </span>
                <SourceSelector
                  selected={
                    (Array.isArray(globalParams.sources)
                      ? globalParams.sources
                      : (steps.find((step) => step.type === 'search')
                          ?.parameters.sources ?? [])) as SourceConnectorType[]
                  }
                  onChange={(sources) => {
                    setSteps((prev) =>
                      prev.map((step) =>
                        step.type === 'search'
                          ? {
                              ...step,
                              parameters: { ...step.parameters, sources },
                            }
                          : step
                      )
                    );
                    setGlobalParams((prev) => {
                      const next = { ...prev, sources };
                      setGlobalParamsText(JSON.stringify(next, null, 2));
                      return next;
                    });
                  }}
                />
              </div>
            </div>

            {/* Raw JSON editor */}
            <div>
              <label
                htmlFor="param-raw-json"
                className="block text-xs font-medium text-muted-foreground mb-1"
              >
                Raw JSON
              </label>
              <textarea
                id="param-raw-json"
                value={globalParamsText}
                onChange={(e) => handleGlobalParamsChange(e.target.value)}
                rows={6}
                aria-invalid={!!globalParamsError}
                className={`w-full px-3 py-2 rounded-lg bg-background border text-xs font-mono text-foreground focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring resize-none transition-colors ${
                  globalParamsError
                    ? 'border-destructive/50 focus:border-destructive'
                    : 'border-border focus:border-primary'
                }`}
              />
              {globalParamsError && (
                <p role="alert" className="mt-1 text-xs text-destructive">
                  {globalParamsError}
                </p>
              )}
            </div>
          </div>

          {/* Blueprint info */}
          {blueprint && (
            <div className="rounded-xl border border-border bg-card shadow-xs p-4">
              <h3 className="text-sm font-medium text-foreground mb-2">
                Blueprint info
              </h3>
              <dl className="space-y-1.5 text-xs text-muted-foreground">
                <div className="flex items-center gap-2">
                  <dt>ID</dt>
                  <dd className="font-mono text-foreground truncate">
                    {blueprint.id}
                  </dd>
                </div>
                {blueprint.template_source && (
                  <div className="flex items-center gap-2">
                    <dt>Template</dt>
                    <dd className="text-foreground truncate">
                      {blueprint.template_source}
                    </dd>
                  </div>
                )}
              </dl>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default BlueprintEditor;

import { create } from 'zustand';
import type {
  PendingReviewResponse,
  ProjectResponse,
  RunResponse,
  StepResponse,
} from '@/services/researchEngineService';

export type ResearchProject = ProjectResponse;

export type ResearchRun = RunResponse;

type QualityMark = NonNullable<StepResponse['quality_marks']>[number];
type RunStepStatus = 'pending' | 'running' | 'complete' | 'error';

export interface RunStepEvent {
  event: string;
  /** Persisted ResearchStep identity when a future stream supplies it. */
  id?: string;
  run_id?: string;
  step_index?: number;
  step_id?: string;
  step_type?: string;
  step_name?: string;
  token_count?: number;
  quality_marks?: QualityMark[];
  mode?: StepResponse['mode'];
  output?: string | Record<string, unknown>;
  sources?: string[];
  prompt?: string;
  full_prompt?: string;
  inputs_hash?: string;
  outputs_hash?: string;
  error?: string;
  error_message?: string;
  pause_reason?: RunResponse['pause_reason'];
  review_kind?: RunResponse['review_kind'];
  output_hash?: string;
  final_status?: 'verified' | 'unverified' | 'no_evidence';
  timestamp?: string;
}

export interface RunStepState {
  id?: string;
  run_id: string;
  step_index: number;
  step_type: string;
  step_name: string;
  status: RunStepStatus;
  mode?: StepResponse['mode'];
  temperature?: number;
  token_count: number;
  quality_marks: QualityMark[];
  output?: string | Record<string, unknown>;
  sources?: string[];
  prompt?: string;
  inputs_hash?: string;
  outputs_hash?: string;
  error_message?: string;
  started_at?: string | null;
  completed_at?: string | null;
}

interface ResearchEngineState {
  projects: ResearchProject[];
  activeProject: ResearchProject | null;
  activeRunId: string | null;
  activeRun: RunResponse | null;
  runSteps: RunStepState[];
  pendingReview: PendingReviewResponse | null;
  finalStatus: 'verified' | 'unverified' | 'no_evidence' | null;
  isLoading: boolean;
  error: string | null;
  setProjects: (projects: ResearchProject[]) => void;
  setActiveProject: (project: ResearchProject | null) => void;
  setActiveRun: (run: RunResponse | null) => void;
  hydrateRun: (run: RunResponse, steps: StepResponse[]) => void;
  mergeRunEvent: (event: RunStepEvent) => void;
  setPendingReview: (review: PendingReviewResponse | null) => void;
  resetRun: (runId: string) => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string | null) => void;
}

function labelStepType(stepType: string): string {
  if (!stepType) return 'Unknown step';
  return stepType.charAt(0).toUpperCase() + stepType.slice(1);
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function hasCompleteReviewProjection(value: unknown): boolean {
  const projection = record(record(value)?.review_projection);
  return (
    projection?.projected === true &&
    projection.truncated === true &&
    projection.identity_complete === true
  );
}

function resolveProjectedReview(
  review: PendingReviewResponse,
  steps: RunStepState[]
): PendingReviewResponse {
  const descriptor = review.descriptor;
  if (
    !review.pending ||
    !descriptor ||
    !hasCompleteReviewProjection(review.stage_output)
  ) {
    return review;
  }

  const matchedStep = steps.find(
    (step) =>
      step.run_id === descriptor.run_id &&
      step.step_index === descriptor.step_index &&
      step.step_type === descriptor.stage_type &&
      step.outputs_hash === descriptor.output_hash &&
      record(step.output) !== null
  );
  const persistedOutput = record(matchedStep?.output);
  if (!persistedOutput) return review;

  return {
    ...review,
    // The immutable descriptor remains the authority for submission identity;
    // only its bounded display projection is replaced from the matching owned
    // persisted step.
    stage_output: persistedOutput,
  };
}

function persistedStep(step: StepResponse): RunStepState {
  const status: RunStepStatus = step.completed_at
    ? 'complete'
    : step.started_at
      ? 'running'
      : 'pending';
  return {
    id: step.id,
    run_id: step.run_id,
    step_index: step.step_index,
    step_type: step.step_type,
    step_name: labelStepType(step.step_type),
    status,
    mode: step.mode,
    temperature: step.temperature,
    token_count: step.token_count,
    quality_marks: step.quality_marks ?? [],
    output: step.output ?? undefined,
    prompt: step.full_prompt ?? undefined,
    inputs_hash: step.inputs_hash ?? undefined,
    outputs_hash: step.outputs_hash ?? undefined,
    started_at: step.started_at,
    completed_at: step.completed_at,
  };
}

function sameStep(
  step: RunStepState,
  event: RunStepEvent,
  runId: string
): boolean {
  if (event.id) {
    if (step.id) return step.id === event.id;
    // A transient stream step has no persisted identity yet. Reconcile it once
    // by its durable run/index boundary, then retain the supplied id.
  }
  return step.run_id === runId && step.step_index === event.step_index;
}

function statusAfterEvent(
  current: RunStepStatus,
  eventName: string
): RunStepStatus {
  if (eventName === 'step_complete') return 'complete';
  if (eventName === 'step_start') {
    return current === 'complete' || current === 'error' ? current : 'running';
  }
  if (eventName === 'step_error') {
    return current === 'complete' ? current : 'error';
  }
  return current;
}

export const useResearchEngineStore = create<ResearchEngineState>((set) => ({
  projects: [],
  activeProject: null,
  activeRunId: null,
  activeRun: null,
  runSteps: [],
  pendingReview: null,
  finalStatus: null,
  isLoading: false,
  error: null,
  setProjects: (projects) => set({ projects }),
  setActiveProject: (project) => set({ activeProject: project }),
  setActiveRun: (run) =>
    set((state) => {
      if (run && state.activeRunId && run.id !== state.activeRunId)
        return state;
      return {
        activeRun: run,
        activeRunId: run?.id ?? state.activeRunId,
      };
    }),
  resetRun: (runId) =>
    set({
      activeRunId: runId,
      activeRun: null,
      runSteps: [],
      pendingReview: null,
      finalStatus: null,
      isLoading: true,
      error: null,
    }),
  hydrateRun: (run, steps) =>
    set((state) => {
      if (state.activeRunId !== null && state.activeRunId !== run.id) {
        return state;
      }

      const existingById = new Map(
        state.runSteps.flatMap((item) => (item.id ? [[item.id, item]] : []))
      );
      const existingByIndex = new Map(
        state.runSteps.map((item) => [
          `${item.run_id}:${item.step_index}`,
          item,
        ])
      );
      const hydrated = steps.map((item) => {
        const value = persistedStep(item);
        const existing =
          existingById.get(item.id) ??
          existingByIndex.get(`${item.run_id}:${item.step_index}`);
        if (!existing) return value;
        return {
          ...existing,
          ...value,
          id: item.id,
          output: value.output ?? existing.output,
          status:
            value.status === 'complete' || value.status === 'error'
              ? value.status
              : existing.status,
        };
      });

      const persistedKeys = new Set(
        hydrated.map((item) => `${item.run_id}:${item.step_index}`)
      );
      const unreconciled = state.runSteps.filter(
        (item) => !persistedKeys.has(`${item.run_id}:${item.step_index}`)
      );

      const runSteps = [...hydrated, ...unreconciled].sort(
        (a, b) => a.step_index - b.step_index
      );

      return {
        activeRunId: run.id,
        activeRun: run,
        runSteps,
        pendingReview: state.pendingReview
          ? resolveProjectedReview(state.pendingReview, runSteps)
          : null,
        isLoading: false,
        error: null,
      };
    }),
  mergeRunEvent: (event) =>
    set((state) => {
      const runId = event.run_id ?? state.activeRunId;
      if (!runId || state.activeRunId !== runId) return state;

      let activeRun = state.activeRun;
      let finalStatus = state.finalStatus;
      if (activeRun) {
        const terminal =
          activeRun.status === 'completed' || activeRun.status === 'failed';
        if (event.event === 'run_started' && !terminal) {
          activeRun = { ...activeRun, status: 'running' };
        } else if (event.event === 'run_paused' && !terminal) {
          activeRun = {
            ...activeRun,
            status: 'paused',
            pause_reason: event.pause_reason ?? activeRun.pause_reason,
            review_kind: event.review_kind ?? activeRun.review_kind,
            step_index: event.step_index ?? activeRun.step_index,
            output_hash: event.output_hash ?? activeRun.output_hash,
          };
        } else if (
          event.event === 'run_complete' &&
          activeRun.status !== 'failed'
        ) {
          activeRun = { ...activeRun, status: 'completed' };
          finalStatus = event.final_status ?? finalStatus;
        } else if (
          event.event === 'run_failed' &&
          activeRun.status !== 'completed'
        ) {
          activeRun = { ...activeRun, status: 'failed' };
        }
      }

      if (
        event.step_index === undefined ||
        !['step_start', 'step_complete', 'step_error'].includes(event.event)
      ) {
        return { activeRun, finalStatus };
      }

      const index = state.runSteps.findIndex((item) =>
        sameStep(item, event, runId)
      );
      const current: RunStepState =
        index >= 0
          ? state.runSteps[index]
          : {
              id: event.id,
              run_id: runId,
              step_index: event.step_index,
              step_type: event.step_type ?? 'unknown',
              step_name:
                event.step_name ?? labelStepType(event.step_type ?? 'unknown'),
              status: 'pending',
              token_count: 0,
              quality_marks: [],
            };

      const merged: RunStepState = {
        ...current,
        id: current.id ?? event.id,
        step_type: event.step_type ?? current.step_type,
        step_name:
          event.step_name ??
          (event.step_type
            ? labelStepType(event.step_type)
            : current.step_name),
        status: statusAfterEvent(current.status, event.event),
        mode: event.mode ?? current.mode,
        token_count: event.token_count ?? current.token_count,
        quality_marks: event.quality_marks ?? current.quality_marks,
        output: event.output ?? current.output,
        sources: event.sources ?? current.sources,
        prompt: event.full_prompt ?? event.prompt ?? current.prompt,
        inputs_hash: event.inputs_hash ?? current.inputs_hash,
        outputs_hash: event.outputs_hash ?? current.outputs_hash,
        error_message:
          event.error_message ?? event.error ?? current.error_message,
      };
      const runSteps = [...state.runSteps];
      if (index >= 0) runSteps[index] = merged;
      else runSteps.push(merged);

      return {
        activeRun,
        finalStatus,
        runSteps: runSteps.sort((a, b) => a.step_index - b.step_index),
      };
    }),
  setPendingReview: (pendingReview) =>
    set((state) => {
      const descriptorRunId = pendingReview?.descriptor?.run_id;
      if (descriptorRunId && descriptorRunId !== state.activeRunId)
        return state;
      return {
        pendingReview: pendingReview
          ? resolveProjectedReview(pendingReview, state.runSteps)
          : null,
      };
    }),
  setLoading: (isLoading) => set({ isLoading }),
  setError: (error) => set({ error }),
}));

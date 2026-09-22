import { create } from 'zustand';
import { toolLabel } from '@/components/context-rail/toolLabels';

export type StepStatus =
  'active' | 'done' | 'error' | 'cancelled' | 'incomplete';

export interface Step {
  id: string;
  callId?: string;
  tool: string;
  label: string;
  status: StepStatus;
  at: number;
}

/**
 * High-level task item from the LangGraph planner_node. Rendered Cowork-style
 * in the Progress card (check-circle + strikethrough when done). Derived from
 * the `plan` SSE event emitted by the backend.
 *
 * `tool` carries the planner's tool hint for the step (absent for pure
 * reasoning/respond steps). Tool-bearing items are only marked done when the
 * matching tool actually completes successfully — never in bulk at stream
 * end — so the Progress card reflects what really ran, not just that the
 * stream closed.
 */
export interface PlanItem {
  id: string;
  text: string;
  tool?: string;
  done: boolean;
}

/** Input accepted by setPlan: plain text or text + planner tool hint. */
export type PlanItemInput = string | { text: string; tool?: string | null };

export interface Run {
  threadId: string;
  name: string;
  task: string;
  steps: Step[];
  plan: PlanItem[];
  state: 'running' | 'done' | 'error' | 'stopped';
  startedAt: number;
  /**
   * Done-state of the previous run's plan on this thread. A re-send starts a
   * fresh run, but the work the earlier run actually completed is still real
   * (and still shown by the in-transcript plan card, which derives status from
   * the committed message). `setPlan` is set-once per run, so the carry-over
   * cannot be merged after the new plan lands — it is kept here and consulted
   * when the new plan arrives.
   */
  carriedPlan?: PlanItem[];
  /** Last SSE frame seq seen for this run — the resume cursor. */
  streamSeq?: number;
  /** Run-correlation id (envelope stream_id) the cursor belongs to; passed
   * to resumeStream so a stale cursor can't attach to a newer run. */
  streamId?: string;
  /** Durable AgentRun id used to fence Stop across hook remounts/resumes. */
  runId?: string;
}

interface AgentActivityState {
  runs: Record<string, Run>;
  currentThreadId: string | null;
  startRun: (threadId: string, name: string, task: string) => void;
  pushToolStart: (threadId: string, tool: string, callId?: string) => void;
  pushToolEnd: (
    threadId: string,
    tool: string,
    ok: boolean,
    callId?: string
  ) => void;
  setPlan: (threadId: string, items: PlanItemInput[]) => void;
  setStreamSeq: (threadId: string, seq: number, streamId?: string) => void;
  setRunId: (threadId: string, runId: string) => void;
  finishRun: (threadId: string, state: 'done' | 'error' | 'stopped') => void;
}

// Eviction constants — cap unbounded growth of runs and steps
const MAX_RUNS = 20;
const MAX_STEPS_PER_RUN = 50;

let seq = 0;
const nextId = () => `step-${Date.now()}-${++seq}`;

export const useAgentActivityStore = create<AgentActivityState>((set) => ({
  runs: {},
  currentThreadId: null,

  startRun: (threadId, name, task) =>
    set((s) => {
      // Re-sending in the same thread must not blank out what already ran:
      // keep the previous plan's done-state (or the one it was itself
      // carrying, when this run never received a plan of its own) so the new
      // plan can restore it.
      const previous = s.runs[threadId];
      const carriedPlan = previous
        ? previous.plan.length > 0
          ? previous.plan
          : previous.carriedPlan
        : undefined;
      const newRuns = {
        ...s.runs,
        [threadId]: {
          threadId,
          name,
          task,
          steps: [],
          plan: [],
          ...(carriedPlan && carriedPlan.length > 0 ? { carriedPlan } : {}),
          state: 'running' as const,
          startedAt: Date.now(),
        },
      };

      // Evict oldest runs when exceeding the cap
      const keys = Object.keys(newRuns);
      if (keys.length > MAX_RUNS) {
        const sorted = keys.sort(
          (a, b) => (newRuns[a].startedAt ?? 0) - (newRuns[b].startedAt ?? 0)
        );
        const toEvict = sorted.slice(0, keys.length - MAX_RUNS);
        for (const key of toEvict) {
          delete newRuns[key];
        }
      }

      return { currentThreadId: threadId, runs: newRuns };
    }),

  pushToolStart: (threadId, tool, callId) =>
    set((s) => {
      const run = s.runs[threadId];
      // Return the existing state reference so Zustand's Object.is check
      // short-circuits and no subscribers are notified for this no-op.
      if (!run) return s;
      const existing = run.steps.find(
        (step) =>
          step.status === 'active' &&
          (callId ? step.callId === callId : step.tool === tool)
      );
      if (existing) return s;
      const step: Step = {
        id: callId ?? nextId(),
        ...(callId ? { callId } : {}),
        tool,
        label: toolLabel(tool),
        status: 'active',
        at: Date.now(),
      };
      // Cap steps per run — keep the newest steps
      let steps = [...run.steps, step];
      if (steps.length > MAX_STEPS_PER_RUN) {
        steps = steps.slice(steps.length - MAX_STEPS_PER_RUN);
      }
      return {
        runs: {
          ...s.runs,
          [threadId]: { ...run, steps },
        },
      };
    }),

  pushToolEnd: (threadId, tool, ok, callId) =>
    set((s) => {
      const run = s.runs[threadId];
      if (!run) return s;
      const idx = run.steps.findIndex(
        (step) =>
          step.status === 'active' &&
          (callId ? step.callId === callId : step.tool === tool)
      );
      let steps = run.steps;
      if (idx >= 0) {
        steps = [...run.steps];
        steps[idx] = { ...steps[idx], status: ok ? 'done' : 'error' };
      }

      // A successful tool completion also completes the first pending plan
      // item that hints at this tool — this is the only path that marks
      // tool-bearing plan items done.
      let plan = run.plan;
      if (ok) {
        const planIdx = run.plan.findIndex((p) => !p.done && p.tool === tool);
        if (planIdx >= 0) {
          plan = [...run.plan];
          plan[planIdx] = { ...plan[planIdx], done: true };
        }
      }

      if (steps === run.steps && plan === run.plan) return s;
      return {
        runs: { ...s.runs, [threadId]: { ...run, steps, plan } },
      };
    }),

  setPlan: (threadId, items) =>
    set((s) => {
      const run = s.runs[threadId];
      if (!run) return s;
      // Only set once — avoid clobbering existing plan with later re-emits.
      if (run.plan.length > 0) return s;
      const plan: PlanItem[] = items.map((item, i) => {
        const text = typeof item === 'string' ? item : item.text;
        const rawTool = typeof item === 'string' ? undefined : item.tool;
        // Planner emits "N/A" for reasoning/respond steps with no tool.
        const tool =
          rawTool && rawTool.trim().toUpperCase() !== 'N/A'
            ? rawTool.trim()
            : undefined;
        // Restore the done-state a previous run in this thread reached for the
        // same item. Nothing is marked done that wasn't done before.
        const done = (run.carriedPlan ?? []).some(
          (prev) => prev.text === text && prev.done
        );
        return { id: `plan-${Date.now()}-${i}`, text, tool, done };
      });
      return { runs: { ...s.runs, [threadId]: { ...run, plan } } };
    }),

  setStreamSeq: (threadId, seq, streamId) =>
    set((s) => {
      const run = s.runs[threadId];
      if (!run || (run.streamSeq === seq && run.streamId === streamId))
        return s;
      return {
        runs: {
          ...s.runs,
          [threadId]: {
            ...run,
            streamSeq: seq,
            streamId: streamId ?? run.streamId,
          },
        },
      };
    }),

  setRunId: (threadId, runId) =>
    set((s) => {
      const run = s.runs[threadId];
      if (!run || run.runId === runId) return s;
      return {
        runs: {
          ...s.runs,
          [threadId]: { ...run, runId },
        },
      };
    }),

  finishRun: (threadId, state) =>
    set((s) => {
      const run = s.runs[threadId];
      if (!run) return s;
      // On successful completion, close out only the tool-less plan items
      // (reasoning/respond steps) — tool-bearing items are completed by
      // pushToolEnd when their tool actually succeeds. A run that never
      // executed its planned tools must not render as fully complete
      // (7-of-7 struck through while the answer says the extraction
      // failed). 'stopped' (user abort) and 'error' leave the plan as-is.
      const plan =
        state === 'done' && run.plan.length > 0
          ? run.plan.map((p) => (p.tool ? p : { ...p, done: true }))
          : run.plan;
      const unresolvedStatus: StepStatus =
        state === 'stopped'
          ? 'cancelled'
          : state === 'error'
            ? 'error'
            : 'incomplete';
      const steps = run.steps.some((step) => step.status === 'active')
        ? run.steps.map((step) =>
            step.status === 'active'
              ? { ...step, status: unresolvedStatus }
              : step
          )
        : run.steps;
      return {
        currentThreadId:
          s.currentThreadId === threadId ? null : s.currentThreadId,
        runs: { ...s.runs, [threadId]: { ...run, state, plan, steps } },
      };
    }),
}));

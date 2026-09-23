import type { Task } from '@/components/ui/agent-plan';
import type { PlanStep, ToolExecution } from '@/types/agent-chat';

/**
 * Maps a planner step's `tool` against the current turn's tool executions to a
 * display status in the agent-plan component's status vocabulary
 * ('pending' | 'in-progress' | 'completed' | 'failed').
 */
/**
 * Group executions into attempts: a failed execution is a retry of the same
 * step (the tool node re-plans transient failures), so it belongs with the
 * execution that follows it rather than consuming a later step's slot.
 */
function groupAttempts(executions: ToolExecution[]): ToolExecution[][] {
  const groups: ToolExecution[][] = [];
  let current: ToolExecution[] = [];
  for (const execution of executions) {
    current.push(execution);
    if (execution.status !== 'failed') {
      groups.push(current);
      current = [];
    }
  }
  if (current.length > 0) groups.push(current);
  return groups;
}

export function deriveStepStatus(
  step: PlanStep,
  toolExecutions: ToolExecution[],
  occurrence = 0,
  occurrencesForTool = 1
): string {
  const status = deriveStepExecutionStatus(
    step,
    toolExecutions,
    occurrence,
    occurrencesForTool
  );
  if (!status) return 'pending';
  if (status === 'running') return 'in-progress';
  if (status === 'completed') return 'completed';
  // R4-L20: 'cancelled' (stopped/superseded mid-run) is terminal, same as
  // 'failed' — the plan UI has no separate vocabulary for it, and mapping
  // to 'pending' made a stopped step look like it hadn't started.
  if (status === 'failed' || status === 'cancelled') return 'failed';
  return 'pending';
}

export function deriveStepExecutionStatus(
  step: PlanStep,
  toolExecutions: ToolExecution[],
  occurrence = 0,
  occurrencesForTool = 1
): ToolExecution['status'] | undefined {
  const matching = toolExecutions.filter((te) => te.toolName === step.tool);
  // A plan can name the same tool in several steps (2x search_documents is
  // common); correlate positionally so one execution can't complete every
  // such step. The last step using a tool absorbs the remaining attempts,
  // which keeps a single-step tool's retries aggregating as before.
  const attempts = groupAttempts(matching);
  const isLastForTool = occurrence === occurrencesForTool - 1;
  const window = (
    isLastForTool
      ? attempts.slice(occurrence)
      : attempts.slice(occurrence, occurrence + 1)
  ).flat();
  // The newest attempt decides: a retry that succeeded is not a failed step,
  // and a failure after an earlier success is not a completed one.
  const newest = window[window.length - 1];
  return newest?.status;
}

/** Renders the planner's `args_hint` as a compact single-line summary. */
export function formatArgsHint(argsHint: Record<string, unknown>): string {
  if (!argsHint || typeof argsHint !== 'object') return '';
  const entries = Object.entries(argsHint);
  if (entries.length === 0) return '';
  return entries
    .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(' · ');
}

/** Flat 1:1 map: each planner step → one top-level task (no subtasks). */
export function mapPlanToTasks(
  steps: PlanStep[],
  toolExecutions: ToolExecution[]
): Task[] {
  const totalPerTool = new Map<string, number>();
  for (const step of steps) {
    totalPerTool.set(step.tool, (totalPerTool.get(step.tool) ?? 0) + 1);
  }
  const seenPerTool = new Map<string, number>();
  return steps.map((step) => {
    const occurrence = seenPerTool.get(step.tool) ?? 0;
    seenPerTool.set(step.tool, occurrence + 1);
    return {
      id: String(step.step),
      title: step.description,
      description: formatArgsHint(step.args_hint),
      status: deriveStepStatus(
        step,
        toolExecutions,
        occurrence,
        totalPerTool.get(step.tool) ?? 1
      ),
      priority: '',
      level: 0,
      dependencies: (step.depends_on ?? []).map(String),
      subtasks: [],
      tools: step.tool ? [step.tool] : [],
    };
  });
}

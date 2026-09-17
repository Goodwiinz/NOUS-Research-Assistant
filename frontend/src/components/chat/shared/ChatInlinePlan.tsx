'use client';

import React, { useMemo, useState } from 'react';
import {
  ReasoningPanel,
  type ReasoningStep,
} from '@/components/elements/reasoning-panel';
import { mapPlanToTasks } from '@/components/agent-chat/planMapping';
import type { PlanStep, ToolExecution } from '@/types/agent-chat';
import type { ActivityStep } from '@/components/chat/shared/cloudMessageView';

/**
 * Adapt the chat page's ActivityStep records onto the agent-chat
 * ToolExecution shape that planMapping.deriveStepStatus expects, so plan
 * items reflect what the tools actually did this turn.
 */
function toToolExecutions(steps: ActivityStep[]): ToolExecution[] {
  return steps.map((s, i) => ({
    id: `step-${i}`,
    toolName: s.tool,
    toolDisplayName: s.label,
    args: {},
    status:
      s.status === 'error'
        ? 'failed'
        : s.status === 'done'
          ? 'completed'
          : s.status === 'cancelled'
            ? 'cancelled'
            : 'running',
  }));
}

/**
 * Adapts the planner's existing plan and tool status onto the reasoning-panel
 * element. The chat store remains the source of truth; this is presentation
 * only.
 */
export const ChatInlinePlan = React.memo(function ChatInlinePlan({
  plan,
  reasoning,
  toolExecutions,
  streaming = false,
}: {
  plan: PlanStep[];
  /** Planner's top-level rationale for `plan`, shown when expanded. */
  reasoning?: string;
  toolExecutions?: ActivityStep[];
  /** True for the live in-flight instance — mounts expanded instead of the
   * committed default (collapsed), so streamed-in steps are visible. */
  streaming?: boolean;
}): React.JSX.Element {
  const [isExpanded, setIsExpanded] = useState(streaming);

  const tasks = useMemo(
    () => mapPlanToTasks(plan, toToolExecutions(toolExecutions ?? [])),
    [plan, toolExecutions]
  );
  const doneCount = tasks.filter((t) => t.status === 'completed').length;
  const steps = useMemo<ReasoningStep[]>(
    () => [
      ...(reasoning ? [{ title: 'Planner rationale', body: reasoning }] : []),
      ...tasks.map((task) => ({
        title: task.title,
        body: task.description || task.tools?.[0]?.replace(/_/g, ' ') || '',
      })),
    ],
    [reasoning, tasks]
  );
  const visiblePlanSteps = streaming
    ? Math.min(
        tasks.length,
        Math.max(
          1,
          tasks.filter((task) => task.status !== 'pending').length + 1
        )
      )
    : tasks.length;

  return (
    <ReasoningPanel
      steps={steps}
      visibleSteps={visiblePlanSteps + (reasoning ? 1 : 0)}
      streaming={streaming}
      open={isExpanded}
      onOpenChange={setIsExpanded}
      restingLabel={
        reasoning && tasks.length === 0
          ? 'Planner rationale'
          : reasoning
            ? `Reasoning · Execution plan · ${doneCount}/${tasks.length}`
            : `Execution plan · ${doneCount}/${tasks.length}`
      }
      className="my-2 max-w-none"
    />
  );
});

export default ChatInlinePlan;

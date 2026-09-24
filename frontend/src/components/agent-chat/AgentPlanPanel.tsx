'use client';

import React, { useMemo } from 'react';
import { ListChecks } from 'lucide-react';
import Plan, { type Task } from '@/components/ui/agent-plan';
import { useAgentChatStore } from '@/store/agentChatStore';
import type { ToolExecution } from '@/types/agent-chat';
import { mapPlanToTasks } from './planMapping';

/**
 * Live execution-plan side panel for the agent sidebar. Reads the current
 * plan + the latest assistant turn's tool executions from the store and renders
 * a read-only, auto-updating agent-plan. Collapses entirely when no plan is in
 * flight.
 */
export function AgentPlanPanel(): React.JSX.Element | null {
  const currentPlan = useAgentChatStore((s) => s.currentPlan);
  const messages = useAgentChatStore((s) => s.messages);

  const toolExecutions = useMemo<ToolExecution[]>(
    () =>
      [...messages]
        .reverse()
        .find((message) => message.role === 'assistant' && message.plan)
        ?.toolExecutions ?? [],
    [messages]
  );

  const tasks = useMemo<Task[]>(
    () => (currentPlan ? mapPlanToTasks(currentPlan, toolExecutions) : []),
    [currentPlan, toolExecutions]
  );

  if (!currentPlan || currentPlan.length === 0) return null;

  const doneCount = tasks.filter((t) => t.status === 'completed').length;

  return (
    <aside className="w-[300px] shrink-0 border-l border-border flex flex-col overflow-hidden max-lg:hidden">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-border shrink-0">
        <ListChecks className="h-3.5 w-3.5 text-sol" />
        <span className="text-sm font-medium text-foreground">
          Execution plan
        </span>
        <span className="ml-auto text-[10px] tabular-nums text-muted-foreground/70">
          {doneCount}/{tasks.length}
        </span>
      </div>
      <div className="flex-1 overflow-y-auto">
        <Plan tasks={tasks} readOnly />
      </div>
    </aside>
  );
}

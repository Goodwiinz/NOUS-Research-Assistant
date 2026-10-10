export { ContextRail, ContextRailDrawer } from './ContextRail';
export { CollapsibleCard } from './CollapsibleCard';
export { AgentActivityPanel } from './AgentActivityPanel';
export { AllCitationsPanel } from './AllCitationsPanel';
export { ContextPanel } from './ContextPanel';
export { ProjectBindingCard } from './ProjectBindingCard';
export { ProjectPickerPopover } from './ProjectPickerPopover';
export { ProgressPanel } from './ProgressPanel';
export { RelatedResultsPanel } from './RelatedResultsPanel';
export { WorkingFoldersPanel } from './WorkingFoldersPanel';
export { toolLabel } from './toolLabels';
export * from './folder-tree';

/**
 * Map an agent subgraph identifier to a human agent name.
 * Used when starting a new run in the agentActivityStore.
 */
export function deriveAgentName(subgraph?: string): string {
  switch (subgraph) {
    case 'research':
      return 'Literature synth';
    case 'writing':
      return 'Draft assistant';
    case 'data':
      return 'Entity analyst';
    default:
      return 'NOUS Agent';
  }
}

/**
 * Derive a concise task subtitle from the user's message. Trims to the
 * nearest word boundary around 60 chars and appends an ellipsis.
 */
export function deriveTask(userMessage: string): string {
  const trimmed = userMessage.trim();
  if (trimmed.length <= 60) return trimmed;
  const head = trimmed.slice(0, 60);
  const lastSpace = head.lastIndexOf(' ');
  return (lastSpace > 30 ? head.slice(0, lastSpace) : head) + '…';
}

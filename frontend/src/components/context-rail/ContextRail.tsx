'use client';

import { cn } from '@/lib/utils';
import { useCitationsForThread } from '@/hooks';
import {
  Dialog,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Activity, X } from 'lucide-react';
import { useState, type ReactElement } from 'react';
import { AgentActivityPanel } from './AgentActivityPanel';
import { AllCitationsPanel } from './AllCitationsPanel';
import { ContextPanel } from './ContextPanel';
import { ProgressPanel } from './ProgressPanel';
import { ProjectBindingCard } from './ProjectBindingCard';
import { RelatedResultsPanel } from './RelatedResultsPanel';
import {
  WorkingFoldersPanel,
  type WorkingFoldersSelection,
} from './WorkingFoldersPanel';

export interface ContextRailProps {
  threadId: string | null;
  ragEnabled?: boolean;
  workspaceName?: string | null;
  workspaceId?: string;
  projectId?: string;
  projectName?: string | null;
  projectFileCount?: number;
  onSelect?: (node: WorkingFoldersSelection) => void;
  onProjectBound?: (projectId: string, projectName: string) => void;
  className?: string;
}

/**
 * Narrow-screen access to the same context rail used by the desktop column.
 * Radix owns focus containment, Escape, and focus restoration to the trigger.
 */
export function ContextRailDrawer(props: ContextRailProps): ReactElement {
  const [open, setOpen] = useState(false);

  return (
    <div className="md:hidden">
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogTrigger asChild>
          <button
            type="button"
            aria-label="Open chat context"
            data-testid="open-chat-context"
            className="fixed right-3 top-1 z-40 inline-flex min-h-11 items-center gap-1.5 rounded-md border border-(--nous-border-1) bg-(--nous-bg-2) px-3 text-xs text-(--nous-fg-2) shadow-sm transition-colors hover:bg-(--nous-bg-3) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)"
          >
            <Activity className="h-4 w-4" aria-hidden />
            Context
          </button>
        </DialogTrigger>
        <DialogPortal>
          <DialogOverlay className="bg-(--nous-erebus)/50" />
          <DialogPrimitive.Content
            data-dismissable-overlay
            aria-label="Chat context"
            aria-modal="true"
            aria-describedby={undefined}
            className="fixed inset-y-0 right-0 z-50 flex w-[min(360px,calc(100vw-1rem))] flex-col overflow-hidden border-l border-(--nous-border-1) bg-(--nous-bg-1) shadow-(--nous-shadow-lg) outline-hidden"
          >
            <div className="flex min-h-14 shrink-0 items-center justify-between gap-3 border-b border-(--nous-border-1) bg-(--nous-bg-2) px-4">
              <DialogTitle className="flex items-center gap-2 text-sm font-semibold text-(--nous-fg-1)">
                <Activity className="h-4 w-4 text-(--nous-sol)" aria-hidden />
                Chat context
              </DialogTitle>
              <DialogPrimitive.Close
                type="button"
                aria-label="Close chat context"
                className="inline-flex h-11 w-11 items-center justify-center rounded-md text-(--nous-fg-3) transition-colors hover:bg-(--nous-bg-3) hover:text-(--nous-fg-1) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)"
              >
                <X className="h-4 w-4" aria-hidden />
              </DialogPrimitive.Close>
            </div>
            <ContextRail
              {...props}
              className={cn('min-h-0 flex-1 overflow-y-auto', props.className)}
            />
          </DialogPrimitive.Content>
        </DialogPortal>
      </Dialog>
    </div>
  );
}

export function ContextRail({
  threadId,
  ragEnabled,
  workspaceName,
  workspaceId,
  projectId,
  projectName,
  projectFileCount,
  onSelect,
  onProjectBound,
  className,
}: ContextRailProps): ReactElement {
  const threadLabel = threadId ? `thread · ${threadId.slice(0, 8)}` : null;
  const { allCitations, relatedResults } = useCitationsForThread();

  return (
    <aside
      className={cn(
        'flex flex-col gap-3 overflow-y-auto px-3 py-3 bg-(--nous-bg-1)',
        className
      )}
      aria-label="Chat context rail"
    >
      <ProjectBindingCard
        projectId={projectId}
        projectName={projectName}
        workspaceName={workspaceName}
        fileCount={projectFileCount}
        threadLabel={threadLabel}
        threadId={threadId}
        workspaceId={workspaceId}
        onProjectBound={onProjectBound}
      />
      <WorkingFoldersPanel
        allCitations={allCitations}
        projectId={projectId}
        workspaceName={workspaceName}
        onSelect={onSelect}
      />
      <AgentActivityPanel threadId={threadId} />
      <ProgressPanel threadId={threadId} />
      <RelatedResultsPanel relatedResults={relatedResults} />
      <AllCitationsPanel allCitations={allCitations} />
      <ContextPanel ragEnabled={ragEnabled} workspaceName={workspaceName} />
    </aside>
  );
}

'use client';

import Link from 'next/link';
import { ArrowRight, FolderOpen, GitBranch, Unlink } from 'lucide-react';
import { ProjectPickerPopover } from './ProjectPickerPopover';
import { railCardTitle } from './CollapsibleCard';

interface ProjectBindingCardProps {
  projectId?: string;
  projectName?: string | null;
  workspaceName?: string | null;
  fileCount?: number;
  threadLabel?: string | null;
  threadId?: string | null;
  workspaceId?: string;
  onProjectBound?: (projectId: string, projectName: string) => void;
}

export function ProjectBindingCard({
  projectId,
  projectName,
  workspaceName,
  fileCount,
  threadLabel,
  threadId,
  workspaceId,
  onProjectBound,
}: ProjectBindingCardProps) {
  const isBound = Boolean(projectId);

  return (
    <section
      className={
        'relative overflow-hidden border bg-(--nous-bg-2) dark:bg-(--nous-obsidian) rounded-lg ' +
        (isBound
          ? 'border-[rgba(var(--nous-sol-rgb),0.25)] dark:border-[rgba(var(--nous-helios-rgb),0.3)]'
          : 'border-(--nous-border-1) dark:border-(--nous-shade)')
      }
    >
      <div className="px-3.5 py-3">
        <div className="flex items-center justify-between mb-2">
          <span className={railCardTitle}>
            {isBound ? 'Bound to project' : 'Detached chat'}
          </span>
          {threadLabel && (
            <span
              className="text-[9px] text-(--nous-fg-3) tabular-nums"
              style={{
                fontFamily: 'var(--nous-font-mono)',
                letterSpacing: '0.06em',
              }}
            >
              {threadLabel}
            </span>
          )}
        </div>

        <div className="flex items-start gap-2.5">
          <span
            className={
              'mt-[2px] grid place-items-center h-7 w-7 shrink-0 rounded-md ' +
              (isBound
                ? 'bg-(--nous-aurum) dark:bg-(--nous-ember) text-(--nous-sol-safe) dark:text-(--nous-helios) border border-[rgba(var(--nous-sol-rgb),0.25)]'
                : 'bg-(--nous-bg-1) dark:bg-(--nous-nyx) text-(--nous-fg-3) border border-(--nous-border-1) dark:border-(--nous-shade)')
            }
          >
            {isBound ? (
              <FolderOpen className="h-3.5 w-3.5" strokeWidth={1.7} />
            ) : (
              <Unlink className="h-3.5 w-3.5" strokeWidth={1.7} />
            )}
          </span>
          <div className="flex-1 min-w-0">
            <p
              className="text-[14px] font-semibold truncate text-(--nous-fg-1)"
              style={{
                fontFamily: 'var(--nous-font-ui)',
                letterSpacing: '-0.005em',
              }}
            >
              {isBound
                ? projectName || 'Untitled project'
                : 'No project attached'}
            </p>
            <p
              className="text-[10px] mt-0.5 truncate text-(--nous-fg-3)"
              style={{
                fontFamily: 'var(--nous-font-mono)',
                letterSpacing: '0.04em',
              }}
            >
              {isBound ? (
                <>
                  {fileCount != null && (
                    <>
                      {fileCount} file{fileCount === 1 ? '' : 's'}
                    </>
                  )}
                  {fileCount != null && workspaceName && (
                    <span className="px-1.5 opacity-50">·</span>
                  )}
                  {workspaceName && <>{workspaceName}</>}
                </>
              ) : (
                <>Chat is not anchored to any working set</>
              )}
            </p>
          </div>
        </div>

        {isBound ? (
          <Link
            href={`/projects/${projectId}`}
            className="mt-3 inline-flex items-center gap-1 text-[11px] font-medium text-(--nous-sol-safe) dark:text-(--nous-helios) hover:opacity-80 transition-opacity group"
            style={{ fontFamily: 'var(--nous-font-ui)' }}
          >
            <GitBranch className="h-3 w-3" strokeWidth={1.7} />
            View project
            <ArrowRight className="h-3 w-3 transition-transform group-hover:translate-x-0.5" />
          </Link>
        ) : threadId && onProjectBound ? (
          <ProjectPickerPopover
            threadId={threadId}
            workspaceId={workspaceId}
            onProjectBound={onProjectBound}
          >
            <button
              className="mt-3 inline-flex items-center justify-center gap-1.5 w-full py-1.5 px-2.5 rounded-md bg-(--nous-erebus) dark:bg-(--nous-umber) dark:border dark:border-(--nous-shade) text-white text-[11px] font-semibold hover:-translate-y-px hover:shadow-xs transition-all"
              style={{ fontFamily: 'var(--nous-font-ui)' }}
            >
              Attach to a project
              <ArrowRight className="h-3 w-3" />
            </button>
          </ProjectPickerPopover>
        ) : (
          <Link
            href="/projects"
            className="mt-3 inline-flex items-center justify-center gap-1.5 w-full py-1.5 px-2.5 rounded-md bg-(--nous-erebus) dark:bg-(--nous-umber) dark:border dark:border-(--nous-shade) text-white text-[11px] font-semibold hover:-translate-y-px hover:shadow-xs transition-all"
            style={{ fontFamily: 'var(--nous-font-ui)' }}
          >
            Attach to a project
            <ArrowRight className="h-3 w-3" />
          </Link>
        )}
      </div>
    </section>
  );
}

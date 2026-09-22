'use client';

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { useProjectStore } from '@/store/projectStore';
import { useProjectChatStore } from '@/store/projectChatStore';
import { Loader2, Search } from 'lucide-react';
import { ReactNode, useCallback, useEffect, useRef, useState } from 'react';

interface ProjectPickerPopoverProps {
  threadId: string;
  workspaceId?: string;
  onProjectBound: (projectId: string, projectName: string) => void;
  children: ReactNode;
}

export function ProjectPickerPopover({
  threadId,
  workspaceId,
  onProjectBound,
  children,
}: ProjectPickerPopoverProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [binding, setBinding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();
  const abortRef = useRef<AbortController>();

  const projects = useProjectStore((s) => s.projects);
  const loading = useProjectStore((s) => s.loading);
  const fetchProjects = useProjectStore((s) => s.fetchProjects);
  const linkThreadToProject = useProjectChatStore((s) => s.linkThreadToProject);

  const doFetch = useCallback(
    (search?: string) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      fetchProjects(
        {
          workspace_id: workspaceId,
          search: search || undefined,
          limit: 20,
          project_status: 'active',
        },
        { signal: controller.signal }
      );
    },
    [fetchProjects, workspaceId]
  );

  useEffect(() => {
    if (open) {
      // Reset transient search state each time the popover reopens; the trigger
      // lives outside this component, so there is no open handler to do it in.
      /* eslint-disable react-hooks/set-state-in-effect */
      setQuery('');
      setError(null);
      /* eslint-enable react-hooks/set-state-in-effect */
      doFetch();
    }
    return () => {
      abortRef.current?.abort();
      clearTimeout(debounceRef.current);
    };
  }, [open, doFetch]);

  const handleSearch = (value: string) => {
    setQuery(value);
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => doFetch(value), 300);
  };

  const handleSelect = async (projectId: string, projectName: string) => {
    setBinding(true);
    setError(null);
    try {
      const response = await linkThreadToProject(projectId, {
        thread_id: threadId,
      });
      if (response) {
        onProjectBound(projectId, projectName);
        setOpen(false);
      } else {
        // The store swallows the failure and stashes the real message under
        // errors[projectId]; surface it instead of a generic line so the user
        // sees the actual cause (e.g. "Thread and project must be in the same
        // workspace") rather than always "Failed to link thread to project".
        setError(
          useProjectChatStore.getState().errors[projectId] ||
            'Failed to link thread to project'
        );
      }
    } catch {
      setError('Network error. Please try again.');
    } finally {
      setBinding(false);
    }
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>{children}</PopoverTrigger>
      <PopoverContent
        side="bottom"
        align="start"
        sideOffset={6}
        className="w-72 p-0 bg-(--nous-bg-2) border-(--nous-border-1) dark:bg-(--nous-obsidian) dark:border-(--nous-shade)"
      >
        <div className="flex items-center gap-2 px-2.5 py-2 border-b border-(--nous-border-1) dark:border-(--nous-shade)">
          <Search className="h-3 w-3 shrink-0 text-(--nous-fg-3)" />
          <input
            type="text"
            value={query}
            onChange={(e) => handleSearch(e.target.value)}
            placeholder="Search projects…"
            autoFocus
            className="flex-1 bg-transparent text-[11px] text-(--nous-fg-1) placeholder:text-(--nous-fg-3) outline-hidden"
            style={{
              fontFamily: 'var(--nous-font-mono)',
              letterSpacing: '0.04em',
            }}
          />
        </div>

        {error && (
          <div
            role="alert"
            className="px-2.5 py-1.5 text-[11px] text-(--nous-mars) border-b border-(--nous-border-1) dark:border-(--nous-shade)"
          >
            {error}
          </div>
        )}
        <div className="max-h-[200px] overflow-y-auto p-1">
          {loading && projects.length === 0 ? (
            <div className="flex items-center justify-center py-6">
              <Loader2 className="h-4 w-4 animate-spin text-(--nous-fg-3)" />
            </div>
          ) : projects.length === 0 ? (
            <p
              className="py-4 text-center text-[11px] text-(--nous-fg-3)"
              style={{ fontFamily: 'var(--nous-font-mono)' }}
            >
              No projects found
            </p>
          ) : (
            projects.map((project) => (
              <button
                key={project.id}
                type="button"
                disabled={binding}
                onClick={() => handleSelect(project.id, project.name)}
                className="w-full flex items-center gap-2 px-2 py-1.5 rounded text-left transition-colors hover:bg-(--nous-aurum)/30 dark:hover:bg-(--nous-ember)/30 disabled:opacity-50"
              >
                <div className="flex-1 min-w-0">
                  <p
                    className="text-[12px] font-medium truncate text-(--nous-fg-1)"
                    style={{ fontFamily: 'var(--nous-font-ui)' }}
                  >
                    {project.name}
                  </p>
                </div>
                {project.project_type && (
                  <span
                    className="shrink-0 px-1.5 py-px rounded text-[9px] bg-(--nous-bg-1) border border-(--nous-border-1) text-(--nous-fg-3) dark:bg-(--nous-nyx) dark:border-(--nous-shade)"
                    style={{
                      fontFamily: 'var(--nous-font-mono)',
                      letterSpacing: '0.04em',
                    }}
                  >
                    {project.project_type.replace('_', ' ')}
                  </span>
                )}
              </button>
            ))
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

'use client';

import {
  Archive,
  ArchiveRestore,
  Calendar,
  FileText,
  FolderKanban,
  MoreHorizontal,
  Trash2,
  Workflow,
} from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import type { Project } from '@/services/projectService';

export interface ProjectCardProps {
  project: Project;
  onOpen: (projectId: string) => void;
  onOpenWorkflow?: (projectId: string) => void;
  onDelete?: (projectId: string) => void;
  onArchive?: (projectId: string) => void;
  onRestore?: (projectId: string) => void;
  compact?: boolean;
}

// Status uses the single Sol accent for the active state; other states stay
// neutral so color never carries meaning on its own (the label always does).
const statusStyles: Record<string, string> = {
  active: 'bg-primary/10 text-primary border-primary/30',
  paused: 'bg-muted text-foreground border-border',
  completed: 'bg-muted text-foreground border-border',
  archived: 'bg-muted text-muted-foreground border-border',
};

const statusLabels: Record<string, string> = {
  active: 'Active',
  paused: 'Paused',
  completed: 'Completed',
  archived: 'Archived',
};

export function ProjectCard({
  project,
  onOpen,
  onOpenWorkflow,
  onDelete,
  onArchive,
  onRestore,
  compact = false,
}: ProjectCardProps) {
  const status = project.research_status || 'active';
  const isArchived = status === 'archived';
  const statusClass = statusStyles[status] || statusStyles.active;
  const statusLabel = statusLabels[status] || status;
  const createdLabel = project.updated_at
    ? new Date(project.updated_at).toLocaleDateString()
    : '';

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpen(project.id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onOpen(project.id);
        }
      }}
      className="group bg-card border border-border rounded-lg p-4 cursor-pointer shadow-xs hover:border-primary/40 hover:shadow-md transition-all focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
    >
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <FolderKanban
              aria-hidden="true"
              className="h-5 w-5 text-primary shrink-0"
            />
            <h3 className="font-medium text-foreground truncate group-hover:text-primary transition-colors">
              {project.name}
            </h3>
          </div>
          <div className="mt-2">
            <span
              className={`px-2 py-0.5 border rounded-full text-[11px] font-medium ${statusClass}`}
            >
              {statusLabel}
            </span>
          </div>
        </div>

        {(onDelete || onArchive || onRestore) && (
          <div
            className="opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity"
            onClick={(e) => e.stopPropagation()}
          >
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className="p-1 rounded text-muted-foreground hover:text-foreground transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
                  aria-label="Project actions"
                >
                  <MoreHorizontal aria-hidden="true" className="h-4 w-4" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {isArchived && onRestore ? (
                  <DropdownMenuItem
                    onClick={() => onRestore(project.id)}
                    className="gap-2"
                  >
                    <ArchiveRestore
                      aria-hidden="true"
                      className="h-3.5 w-3.5"
                    />
                    Restore
                  </DropdownMenuItem>
                ) : onArchive ? (
                  <DropdownMenuItem
                    onClick={() => onArchive(project.id)}
                    className="gap-2"
                  >
                    <Archive aria-hidden="true" className="h-3.5 w-3.5" />
                    Archive
                  </DropdownMenuItem>
                ) : null}
                {onDelete && (onArchive || onRestore) && (
                  <DropdownMenuSeparator />
                )}
                {onDelete && (
                  <DropdownMenuItem
                    onClick={() => onDelete(project.id)}
                    className="gap-2 text-destructive focus:text-destructive"
                  >
                    <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
                    Delete
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        )}
      </div>

      {project.description && (
        <p
          className={`text-sm text-muted-foreground mb-3 ${compact ? 'line-clamp-1' : 'line-clamp-2'}`}
        >
          {project.description}
        </p>
      )}

      <div className="flex items-center gap-4 text-xs text-muted-foreground">
        <div className="flex items-center gap-1">
          <FileText aria-hidden="true" className="h-3 w-3" />
          <span className="tabular-nums">
            {project.document_count || 0} docs
          </span>
        </div>
        {project.deadline && (
          <div className="flex items-center gap-1">
            <Calendar aria-hidden="true" className="h-3 w-3" />
            <span className="tabular-nums">
              {new Date(project.deadline).toLocaleDateString()}
            </span>
          </div>
        )}
      </div>

      {project.tags && project.tags.length > 0 && (
        <div className="flex items-center flex-wrap gap-1 mt-3">
          {project.tags.slice(0, 3).map((tag) => (
            <span
              key={tag}
              className="px-1.5 py-0.5 rounded bg-muted text-muted-foreground text-[11px]"
            >
              {tag}
            </span>
          ))}
          {project.tags.length > 3 && (
            <span className="text-[11px] text-muted-foreground">
              +{project.tags.length - 3}
            </span>
          )}
        </div>
      )}

      <div className="mt-3 text-xs text-muted-foreground">
        Updated {createdLabel}
      </div>
      {project.research_engine_project_id && onOpenWorkflow && (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            onOpenWorkflow(project.id);
          }}
          className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium text-primary hover:underline"
        >
          <Workflow className="h-3.5 w-3.5" aria-hidden="true" />
          Open workflow
        </button>
      )}
    </div>
  );
}

export default ProjectCard;

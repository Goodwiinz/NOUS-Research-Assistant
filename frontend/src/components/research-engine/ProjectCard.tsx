'use client';

import { Calendar, ExternalLink, FolderKanban } from 'lucide-react';
import { useRouter } from 'next/navigation';
import type { ResearchProject } from '@/store/research-engine-store';

const statusStyles: Record<string, string> = {
  active: 'bg-primary/10 text-primary border-primary/30',
  paused: 'bg-muted text-muted-foreground border-border',
  completed: 'bg-primary/5 text-primary/80 border-primary/20',
  archived: 'bg-muted/60 text-muted-foreground border-border',
};

const statusLabels: Record<string, string> = {
  active: 'Active',
  paused: 'Paused',
  completed: 'Completed',
  archived: 'Archived',
};

export interface ProjectCardProps {
  project: ResearchProject;
}

export function ProjectCard({ project }: ProjectCardProps) {
  const router = useRouter();
  const status = project.status || 'active';
  const statusClass = statusStyles[status] || statusStyles.active;
  const statusLabel = statusLabels[status] || status;

  const createdDate = project.created_at
    ? new Date(project.created_at).toLocaleDateString()
    : '';

  return (
    <div
      onClick={() =>
        router.push(`/research-engine/projects/${project.id}/blueprint`)
      }
      className="group bg-card border border-border rounded-lg p-4 cursor-pointer shadow-xs transition-colors hover:border-primary/50"
    >
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <FolderKanban
              aria-hidden="true"
              className="h-5 w-5 text-primary shrink-0"
            />
            <h3 className="font-medium text-foreground truncate transition-colors group-hover:text-primary">
              {project.name}
            </h3>
          </div>
          <div className="mt-2">
            <span
              className={`inline-flex items-center px-2 py-0.5 rounded-md border text-[11px] font-medium ${statusClass}`}
            >
              {statusLabel}
            </span>
          </div>
        </div>
      </div>

      {project.description && (
        <p className="text-sm text-muted-foreground mb-3 line-clamp-2">
          {project.description}
        </p>
      )}

      {createdDate && (
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Calendar aria-hidden="true" className="h-3 w-3" />
          <span>Created {createdDate}</span>
        </div>
      )}
      {project.collection_id && (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            router.push(`/projects/${project.collection_id}`);
          }}
          className="mt-3 inline-flex items-center gap-1 text-xs text-primary hover:underline"
        >
          <ExternalLink aria-hidden="true" className="h-3 w-3" />
          Open project workspace
        </button>
      )}
    </div>
  );
}

export default ProjectCard;

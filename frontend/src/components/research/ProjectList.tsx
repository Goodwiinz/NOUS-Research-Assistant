'use client';

import { LayoutGrid, List } from 'lucide-react';
import type { Project } from '@/services/projectService';
import { ProjectCard } from './ProjectCard';

export interface ProjectListProps {
  projects: Project[];
  viewMode: 'grid' | 'list';
  onViewModeChange: (viewMode: 'grid' | 'list') => void;
  onOpenProject: (projectId: string) => void;
  onOpenWorkflow?: (projectId: string) => void;
  onDeleteProject?: (projectId: string) => void;
  onArchiveProject?: (projectId: string) => void;
  onRestoreProject?: (projectId: string) => void;
}

export function ProjectList({
  projects,
  viewMode,
  onViewModeChange,
  onOpenProject,
  onOpenWorkflow,
  onDeleteProject,
  onArchiveProject,
  onRestoreProject,
}: ProjectListProps) {
  return (
    <div>
      <div className="flex justify-end mb-4">
        <div
          className="inline-flex items-center border border-border rounded-lg overflow-hidden"
          role="group"
          aria-label="View mode"
        >
          <button
            type="button"
            onClick={() => onViewModeChange('grid')}
            className={`px-3 py-2 text-xs flex items-center gap-1.5 transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset ${
              viewMode === 'grid'
                ? 'bg-primary/10 text-primary'
                : 'bg-card text-muted-foreground hover:text-foreground'
            }`}
            aria-label="Grid view"
            aria-pressed={viewMode === 'grid'}
          >
            <LayoutGrid aria-hidden="true" className="h-3.5 w-3.5" />
            Grid
          </button>
          <button
            type="button"
            onClick={() => onViewModeChange('list')}
            className={`px-3 py-2 text-xs flex items-center gap-1.5 transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset ${
              viewMode === 'list'
                ? 'bg-primary/10 text-primary'
                : 'bg-card text-muted-foreground hover:text-foreground'
            }`}
            aria-label="List view"
            aria-pressed={viewMode === 'list'}
          >
            <List aria-hidden="true" className="h-3.5 w-3.5" />
            List
          </button>
        </div>
      </div>

      {viewMode === 'grid' ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {projects.map((project) => (
            <ProjectCard
              key={project.id}
              project={project}
              onOpen={onOpenProject}
              onOpenWorkflow={onOpenWorkflow}
              onDelete={onDeleteProject}
              onArchive={onArchiveProject}
              onRestore={onRestoreProject}
            />
          ))}
        </div>
      ) : (
        <div className="space-y-3">
          {projects.map((project) => (
            <ProjectCard
              key={project.id}
              project={project}
              onOpen={onOpenProject}
              onOpenWorkflow={onOpenWorkflow}
              onDelete={onDeleteProject}
              onArchive={onArchiveProject}
              compact
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default ProjectList;

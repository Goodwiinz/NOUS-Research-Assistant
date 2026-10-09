'use client';

import { useCallback, useEffect, useState } from 'react';
import { AlertCircle, FolderOpen, Loader2, Plus, Sparkles } from 'lucide-react';
import { EmptyState } from '@/components/ui/EmptyState';
import { listProjects, listTemplates } from '@/services/researchEngineService';
import { captureAccountSession } from '@/lib/account-session';
import {
  useResearchEngineStore,
  type ResearchProject,
} from '@/store/research-engine-store';
import { ProjectCard } from './ProjectCard';
import { CreateProjectModal } from './CreateProjectModal';

interface Template {
  id: string;
  name: string;
  description?: string;
}

export function ResearchDashboard() {
  const { projects, setProjects, isLoading, setLoading, error, setError } =
    useResearchEngineStore();
  const [templates, setTemplates] = useState<Template[]>([]);
  const [showCreateModal, setShowCreateModal] = useState(false);

  const fetchProjects = useCallback(async () => {
    const isCurrentAccount = captureAccountSession();
    setLoading(true);
    setError(null);
    try {
      const res = (await listProjects()) as ResearchProject[];
      if (!isCurrentAccount()) return;
      setProjects(res ?? []);
    } catch (err) {
      if (!isCurrentAccount()) return;
      setError(err instanceof Error ? err.message : 'Failed to load projects');
    } finally {
      if (isCurrentAccount()) setLoading(false);
    }
  }, [setProjects, setLoading, setError]);

  useEffect(() => {
    void fetchProjects();
    // Templates are public, non-critical reference data.
    void listTemplates().then(
      (res) => setTemplates((res ?? []) as Template[]),
      () => {}
    );
  }, [fetchProjects]);

  return (
    <div>
      {/* Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between mb-6">
        <div className="max-w-2xl">
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            Research engine
          </h1>
          <p className="text-sm text-muted-foreground mt-1 leading-relaxed">
            Create and manage reproducible research workflows with
            blueprint-driven pipelines.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreateModal(true)}
          className="inline-flex shrink-0 items-center gap-2 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium shadow-xs transition-colors hover:bg-primary/90 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <Plus aria-hidden="true" className="h-4 w-4" />
          New project
        </button>
      </div>

      {/* Quick-start templates */}
      {templates.length > 0 && (
        <div className="mb-8">
          <h2 className="text-sm font-medium text-foreground mb-3">
            Quick-start templates
          </h2>
          <div className="flex flex-wrap gap-2">
            {templates.map((tpl) => (
              <button
                key={tpl.id}
                type="button"
                title={tpl.description}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border bg-card text-sm text-foreground transition-colors hover:border-primary/40 hover:bg-muted/40 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              >
                <Sparkles
                  aria-hidden="true"
                  className="h-3.5 w-3.5 text-primary"
                />
                {tpl.name}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Loading state — skeletons, not a spinner */}
      {isLoading && (
        <div
          role="status"
          aria-label="Loading projects"
          className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"
        >
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div
              key={i}
              className="rounded-lg border border-border bg-card p-4"
            >
              <div className="flex items-center gap-2">
                <div className="h-5 w-5 rounded bg-muted animate-pulse" />
                <div className="h-4 w-2/3 rounded bg-muted animate-pulse" />
              </div>
              <div className="mt-3 h-5 w-16 rounded bg-muted animate-pulse" />
              <div className="mt-4 h-3 w-full rounded bg-muted animate-pulse" />
              <div className="mt-2 h-3 w-4/5 rounded bg-muted animate-pulse" />
            </div>
          ))}
        </div>
      )}

      {/* Error state — graceful fallback with retry */}
      {error && !isLoading && (
        <div role="alert">
          <EmptyState
            icon={AlertCircle}
            title="Service unavailable"
            description="The research engine service is not responding. This may be a temporary issue."
            action={{
              label: 'Retry connection',
              onClick: () => window.location.reload(),
            }}
          />
        </div>
      )}

      {/* Project list */}
      {!isLoading && !error && (
        <>
          {projects.length === 0 ? (
            <EmptyState
              icon={FolderOpen}
              title="No projects yet"
              description="Create your first research project to start organizing documents and workflows."
              action={{
                label: 'New project',
                onClick: () => setShowCreateModal(true),
              }}
            />
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {projects.map((project) => (
                <ProjectCard key={project.id} project={project} />
              ))}
            </div>
          )}
        </>
      )}

      {/* Create modal */}
      <CreateProjectModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onCreated={fetchProjects}
      />
    </div>
  );
}

export default ResearchDashboard;

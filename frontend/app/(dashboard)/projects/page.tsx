'use client';

/**
 * Research Projects List Page
 * Displays all projects with search, filters, and management options.
 */

import { useEffect, useMemo, useState } from 'react';
import { useDebounce } from '@/utils/performance';
import toast from 'react-hot-toast';
import { useRouter } from 'next/navigation';
import { FolderOpen, Loader2, Network, Plus, Search } from 'lucide-react';
import { CreateProjectModal } from '@/components/research/CreateProjectModal';
import { ProjectList } from '@/components/research/ProjectList';
import { useProjectStore } from '@/store/projectStore';
import { useChatStore, selectCurrentWorkspace } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';
import { workspaceService } from '@/services/workspaceService';
import { getApiErrorMessage } from '@/utils/apiErrorMessage';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';

type ProjectStatus = 'active' | 'paused' | 'completed' | 'archived';
type ProjectType = 'research' | 'literature_review' | 'thesis' | 'paper';

function ProjectsLoadingSkeleton() {
  return (
    <div
      role="status"
      aria-label="Loading projects"
      className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"
    >
      {Array.from({ length: 6 }).map((_, idx) => (
        <div
          key={idx}
          className="rounded-xl border border-border bg-card p-5 shadow-xs"
        >
          <div className="flex items-start justify-between mb-4">
            <div className="flex items-center gap-2">
              <div className="h-9 w-9 rounded-lg bg-muted animate-pulse" />
              <div className="h-4 w-32 rounded bg-muted animate-pulse" />
            </div>
            <div className="h-4 w-4 rounded bg-muted animate-pulse" />
          </div>
          <div className="space-y-2 mb-4">
            <div className="h-3 w-full rounded bg-muted animate-pulse" />
            <div className="h-3 w-3/4 rounded bg-muted animate-pulse" />
          </div>
          <div className="flex items-center gap-4">
            <div className="h-3 w-16 rounded bg-muted animate-pulse" />
            <div className="h-3 w-20 rounded bg-muted animate-pulse" />
          </div>
        </div>
      ))}
      <span className="sr-only">Loading projects</span>
    </div>
  );
}

export default function ProjectsPage() {
  const router = useRouter();
  const { isAuthenticated } = useAuthStore();
  const currentWorkspace = useChatStore(selectCurrentWorkspace);
  const currentWorkspaceId = useChatStore((s) => s.currentWorkspaceId);
  const loadWorkspaces = useChatStore((s) => s.loadWorkspaces);
  const {
    projects,
    loading,
    error,
    total,
    fetchProjects,
    createProject,
    updateProject,
    deleteProject,
    clearError,
    reset: resetProjects,
  } = useProjectStore();

  const [mounted, setMounted] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<ProjectStatus | ''>('');
  const [typeFilter, setTypeFilter] = useState<ProjectType | ''>('');
  const [tagFilter, setTagFilter] = useState('');
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid');
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [projectToDelete, setProjectToDelete] = useState<string | null>(null);
  const debouncedSearch = useDebounce(searchQuery, 300);

  useEffect(() => {
    // This hydration guard intentionally flips only after the client mounts.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!mounted || !isAuthenticated) return;
    if (currentWorkspaceId && !currentWorkspace) {
      void loadWorkspaces();
    }
  }, [
    mounted,
    isAuthenticated,
    currentWorkspaceId,
    currentWorkspace,
    loadWorkspaces,
  ]);

  useEffect(() => {
    if (!mounted || isAuthenticated) return;
    resetProjects();
  }, [mounted, isAuthenticated, resetProjects]);

  useEffect(() => {
    if (!mounted || !isAuthenticated) return;
    const effectiveWorkspaceId = currentWorkspace?.id ?? currentWorkspaceId;
    // Cancel the previous fetch when filters/workspace change so a slow response
    // from the earlier request can't overwrite the latest results.
    const controller = new AbortController();
    void fetchProjects(
      {
        workspace_id: effectiveWorkspaceId || undefined,
        search: debouncedSearch || undefined,
        project_status: statusFilter || undefined,
        project_type: typeFilter || undefined,
        tag: tagFilter || undefined,
      },
      { signal: controller.signal }
    );
    return () => controller.abort();
  }, [
    mounted,
    isAuthenticated,
    currentWorkspaceId,
    currentWorkspace?.id,
    debouncedSearch,
    statusFilter,
    typeFilter,
    tagFilter,
    fetchProjects,
  ]);

  const allTags = useMemo(() => {
    if (!isAuthenticated) return [];
    return Array.from(
      new Set(projects.flatMap((project) => project.tags || []))
    ).sort();
  }, [isAuthenticated, projects]);

  const visibleProjects = isAuthenticated ? projects : [];
  const visibleTotal = isAuthenticated ? total : 0;

  const handleCreateProject = async (payload: {
    name: string;
    description?: string;
    project_type?: ProjectType;
    deadline?: string;
    tags?: string[];
  }) => {
    // Let the modal surface the toast on failure (it keeps the form open for
    // retry). Rethrow so the modal's catch block fires instead of silently
    // resolving.
    const workspaceId =
      currentWorkspace?.id ??
      currentWorkspaceId ??
      (await workspaceService.getOrCreateDefaultWorkspace()).id;
    const project = await createProject({
      workspace_id: workspaceId,
      name: payload.name,
      description: payload.description,
      project_type: payload.project_type,
      deadline: payload.deadline,
      tags: payload.tags,
    });
    router.push(`/projects/${project.id}`);
  };

  const handleDeleteProject = (projectId: string) => {
    setProjectToDelete(projectId);
    setDeleteDialogOpen(true);
  };

  const confirmDeleteProject = async () => {
    if (!projectToDelete) return;
    clearError();
    try {
      await deleteProject(projectToDelete);
      toast.success('Project deleted');
    } catch (err) {
      toast.error(getApiErrorMessage(err, 'Failed to delete project'));
    } finally {
      setProjectToDelete(null);
      setDeleteDialogOpen(false);
    }
  };

  const handleArchiveProject = async (projectId: string) => {
    clearError();
    try {
      await updateProject(projectId, { research_status: 'archived' });
      toast.success('Project archived');
    } catch (err) {
      toast.error(getApiErrorMessage(err, 'Failed to archive project'));
    }
  };

  const handleRestoreProject = async (projectId: string) => {
    clearError();
    try {
      await updateProject(projectId, { research_status: 'active' });
      toast.success('Project restored');
    } catch (err) {
      toast.error(getApiErrorMessage(err, 'Failed to restore project'));
    }
  };

  if (!mounted) {
    return (
      <div className="p-6 max-w-7xl mx-auto">
        <ProjectsLoadingSkeleton />
        <span className="sr-only">
          <Loader2 aria-hidden="true" className="hidden" />
        </span>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">Projects</h1>
          {currentWorkspace && (
            <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground mt-1">
              <Network
                aria-hidden="true"
                className="w-3.5 h-3.5 text-primary"
              />
              {currentWorkspace.name}
            </span>
          )}
          <p className="text-sm text-muted-foreground mt-1">
            Organize documents, citations, and notes into research projects.
          </p>
        </div>
        <button
          onClick={() => setShowCreateModal(true)}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium shadow-xs hover:bg-(--nous-helios) transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <Plus aria-hidden="true" className="h-4 w-4" />
          New project
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mb-6">
        <div className="md:col-span-2 relative">
          <Search
            aria-hidden="true"
            className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground"
          />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search projects..."
            aria-label="Search projects"
            className="w-full pl-10 pr-4 py-2 bg-card border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-primary transition-colors"
          />
        </div>
        <select
          value={statusFilter}
          onChange={(e) =>
            setStatusFilter(e.target.value as ProjectStatus | '')
          }
          aria-label="Filter by status"
          className="px-3 py-2 bg-card border border-border rounded-lg text-sm text-foreground focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-primary transition-colors"
        >
          <option value="">All statuses</option>
          <option value="active">Active</option>
          <option value="paused">Paused</option>
          <option value="completed">Completed</option>
          <option value="archived">Archived</option>
        </select>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value as ProjectType | '')}
          aria-label="Filter by type"
          className="px-3 py-2 bg-card border border-border rounded-lg text-sm text-foreground focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-primary transition-colors"
        >
          <option value="">All types</option>
          <option value="research">Research</option>
          <option value="literature_review">Literature review</option>
          <option value="thesis">Thesis</option>
          <option value="paper">Paper</option>
        </select>
      </div>

      {allTags.length > 0 && (
        <div className="mb-6 flex items-center flex-wrap gap-2">
          <span className="text-xs text-muted-foreground">Tags</span>
          <button
            onClick={() => setTagFilter('')}
            aria-pressed={tagFilter === ''}
            className={`px-3 py-2 min-h-[36px] border rounded-full text-xs transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring ${
              tagFilter === ''
                ? 'bg-primary/10 border-primary/40 text-primary'
                : 'bg-card border-border text-muted-foreground hover:text-foreground hover:border-(--nous-helios)'
            }`}
          >
            All
          </button>
          {allTags.map((tag) => (
            <button
              key={tag}
              onClick={() => setTagFilter(tag)}
              aria-pressed={tagFilter === tag}
              className={`px-3 py-2 min-h-[36px] border rounded-full text-xs transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring ${
                tagFilter === tag
                  ? 'bg-primary/10 border-primary/40 text-primary'
                  : 'bg-card border-border text-muted-foreground hover:text-foreground hover:border-(--nous-helios)'
              }`}
            >
              {tag}
            </button>
          ))}
        </div>
      )}

      {error && (
        <div
          role="alert"
          className="mb-6 p-4 rounded-xl border border-(--nous-mars)/40 bg-(--nous-mars)/10"
        >
          <p className="text-sm text-foreground">{error}</p>
          <button
            onClick={clearError}
            className="mt-2 text-xs font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring rounded"
          >
            Dismiss
          </button>
        </div>
      )}

      {loading && <ProjectsLoadingSkeleton />}

      {!loading &&
        visibleProjects.length === 0 &&
        (() => {
          const hasActiveFilters = !!(
            searchQuery ||
            statusFilter ||
            typeFilter ||
            tagFilter
          );
          return hasActiveFilters ? (
            <div className="text-center py-16 px-6 rounded-xl border border-border bg-card shadow-xs">
              <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-lg bg-muted">
                <FolderOpen
                  aria-hidden="true"
                  className="h-6 w-6 text-muted-foreground"
                />
              </div>
              <h3 className="text-base font-medium text-foreground mb-1">
                No matching projects
              </h3>
              <p className="text-sm text-muted-foreground">
                No projects match your filters. Try clearing search or filters.
              </p>
            </div>
          ) : (
            <div className="text-center py-16 px-6 rounded-xl border border-border bg-card shadow-xs">
              <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-lg bg-primary/10">
                <FolderOpen
                  aria-hidden="true"
                  className="h-6 w-6 text-primary"
                />
              </div>
              <h3 className="text-base font-medium text-foreground mb-1">
                No projects yet
              </h3>
              <p className="text-sm text-muted-foreground mb-5 max-w-md mx-auto">
                Create your first project to start gathering documents,
                citations, and notes in one place.
              </p>
              <button
                onClick={() => setShowCreateModal(true)}
                className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium shadow-xs hover:bg-(--nous-helios) transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              >
                <Plus aria-hidden="true" className="h-4 w-4" />
                New project
              </button>
            </div>
          );
        })()}

      {!loading && visibleProjects.length > 0 && (
        <ProjectList
          projects={visibleProjects}
          viewMode={viewMode}
          onViewModeChange={setViewMode}
          onOpenProject={(projectId) => router.push(`/projects/${projectId}`)}
          onOpenWorkflow={(projectId) =>
            router.push(`/projects/${projectId}?tab=workflow`)
          }
          onDeleteProject={(projectId) => {
            void handleDeleteProject(projectId);
          }}
          onArchiveProject={(projectId) => {
            void handleArchiveProject(projectId);
          }}
          onRestoreProject={(projectId) => {
            void handleRestoreProject(projectId);
          }}
        />
      )}

      {!loading && visibleProjects.length > 0 && visibleTotal > 0 && (
        <div className="mt-6 text-center text-sm text-muted-foreground tabular-nums">
          Showing {visibleProjects.length} of {visibleTotal} projects
        </div>
      )}

      <CreateProjectModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onCreate={handleCreateProject}
      />

      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete project?</AlertDialogTitle>
            <AlertDialogDescription>
              This will permanently delete this project and all associated data.
              This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmDeleteProject}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

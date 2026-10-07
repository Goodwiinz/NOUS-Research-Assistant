'use client';

import {
  useCallback,
  useState,
  type ReactElement,
  type ReactNode,
} from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2, Trash2, Workflow } from 'lucide-react';
import { DraftClaimsPanel } from '@/components/research/DraftClaimsPanel';
import { useAuth } from '@/hooks/useAuth';
import { projectService, type Project } from '@/services/projectService';
import { useProjectStore } from '@/store/projectStore';
import {
  assignProjectRole,
  createProject,
  listProjectRoles,
  removeProjectRole,
  type ResearchProjectRole,
} from '@/services/researchEngineService';
import { AppraisalPanel } from './AppraisalPanel';
import { BlueprintEditor } from './BlueprintEditor';
import { CorpusPanel } from './CorpusPanel';
import { EvidenceTablePanel } from './EvidenceTablePanel';
import { SynthesisPanel } from './SynthesisPanel';
import { JourneyRail } from './JourneyRail';
import { PrismaFlowCard } from './PrismaFlowCard';
import { ProtocolPanel } from './ProtocolPanel';
import { ReportIdentityPanel } from './ReportIdentityPanel';
import { ReviewVersionsPanel } from './ReviewVersionsPanel';
import { ScreeningConflictsPanel } from './ScreeningConflictsPanel';
import { ScreeningQueuePanel } from './ScreeningQueuePanel';
import { SearchSchedulePanel } from './SearchSchedulePanel';

interface ProjectWorkflowProps {
  project: Project;
  /** Switches the project page tab (GOO-308 Extract/Write shortcuts). */
  onOpenTab?: (tab: 'matrix' | 'drafts') => void;
}

/** One journey stage's anchor; the rail links to ``#journey-<key>``. */
function Stage({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: ReactNode;
}): ReactElement {
  return (
    <section
      id={`journey-${id}`}
      aria-labelledby={`journey-${id}-title`}
      className="scroll-mt-4 space-y-6"
    >
      <h2
        id={`journey-${id}-title`}
        className="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

function OpenTab({
  label,
  onClick,
}: {
  label: string;
  onClick: () => void;
}): ReactElement {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-md border border-border px-3 py-1.5 text-sm text-foreground"
    >
      {label}
    </button>
  );
}

const ROLES: ResearchProjectRole[] = ['reviewer', 'adjudicator', 'supervisor'];

export function ProjectWorkflow({
  project,
  onOpenTab,
}: ProjectWorkflowProps): ReactElement {
  const queryClient = useQueryClient();
  const userId = useAuth().user?.id;
  const [enabledExtension, setEnabledExtension] = useState<{
    projectId: string;
    engineProjectId: string;
  } | null>(null);
  const [approvedProtocol, setApprovedProtocol] = useState<{
    projectId: string;
    versionId?: string;
  } | null>(null);
  const [savedBlueprint, setSavedBlueprint] = useState<{
    projectId: string;
    blueprintId: string;
  } | null>(null);
  const approvedProtocolVersionId =
    approvedProtocol?.projectId === project.id
      ? approvedProtocol.versionId
      : undefined;
  const blueprintId =
    savedBlueprint?.projectId === project.id
      ? savedBlueprint.blueprintId
      : undefined;
  const handleBlueprintSaved = useCallback(
    (savedId: string) =>
      setSavedBlueprint({ projectId: project.id, blueprintId: savedId }),
    [project.id]
  );
  const handleApprovedProtocolChange = useCallback(
    (versionId?: string) =>
      setApprovedProtocol({ projectId: project.id, versionId }),
    [project.id]
  );
  const engineProjectId =
    project.research_engine_project_id ??
    (enabledExtension?.projectId === project.id
      ? enabledExtension.engineProjectId
      : null);
  const archived =
    project.research_status === 'archived' ||
    project.workspace_archived === true;
  const canEdit = project.can_edit === true && !archived;
  const canManageRoles = project.can_manage === true && !archived;

  const ensureExtension = useMutation({
    mutationFn: () =>
      createProject({ collection_id: project.id, name: project.name }),
    onSuccess: (extension) => {
      setEnabledExtension({
        projectId: project.id,
        engineProjectId: extension.research_engine_project_id,
      });
      void queryClient.invalidateQueries({ queryKey: ['project', project.id] });
      // The page renders the project from the project store; patch it from
      // the create response so a remount keeps research_engine_project_id.
      // No fetchProject: it would join a pre-create in-flight GET, and if the
      // user already left for another project it would supersede that fetch.
      // ponytail: a pre-create GET still in flight can overwrite this patch;
      // a store-level request generation counter fixes that if it shows up.
      useProjectStore.setState((state) =>
        state.currentProject?.id === project.id
          ? {
              currentProject: {
                ...state.currentProject,
                research_engine_project_id:
                  extension.research_engine_project_id,
              },
            }
          : {}
      );
    },
  });

  const roles = useQuery({
    queryKey: ['project', project.id, 'research-engine', 'roles'],
    queryFn: () => listProjectRoles(project.id),
    enabled: Boolean(engineProjectId),
    retry: false,
  });
  const currentDraft = useQuery({
    queryKey: ['project', project.id, 'drafts', 'current'],
    queryFn: () => projectService.getCurrentDraft(project.id),
    enabled: Boolean(engineProjectId),
    retry: false,
  });
  const myRoles = (roles.data ?? [])
    .filter((assignment) => assignment.user_id === userId)
    .map((assignment) => assignment.role);

  if (!engineProjectId) {
    return (
      <div className="rounded-xl border border-dashed border-border p-8 text-center">
        <Workflow className="mx-auto mb-3 h-9 w-9 text-muted-foreground" />
        <h2 className="font-medium text-foreground">Research workflow</h2>
        <p className="mx-auto mt-1 max-w-lg text-sm text-muted-foreground">
          Add a reproducible blueprint and role-specific review assignments to
          this project.
        </p>
        {archived ? (
          <p className="mt-4 text-sm text-muted-foreground">
            Archived projects are read-only.
          </p>
        ) : !canManageRoles ? (
          <p className="mt-4 text-sm text-muted-foreground">
            A project owner or administrator must enable this workflow.
          </p>
        ) : (
          <button
            type="button"
            onClick={() => ensureExtension.mutate()}
            disabled={ensureExtension.isPending}
            className="mt-4 inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {ensureExtension.isPending && (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            )}
            Enable workflow
          </button>
        )}
        {ensureExtension.error && (
          <p role="alert" className="mt-3 text-sm text-destructive">
            {ensureExtension.error instanceof Error
              ? ensureExtension.error.message
              : 'Failed to enable workflow'}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {archived && (
        <p className="rounded-md border border-border bg-muted/50 p-3 text-sm text-muted-foreground">
          This archived project is read-only.
        </p>
      )}
      <JourneyRail projectId={project.id} />
      <Stage id="plan" title="Plan">
        <BlueprintEditor
          key={project.id}
          projectId={project.id}
          readOnly={!canEdit}
          approvedProtocolVersionId={approvedProtocolVersionId}
          onBlueprintSaved={handleBlueprintSaved}
        />
        <ProtocolPanel
          projectId={project.id}
          blueprintId={blueprintId}
          readOnly={!canEdit}
          onApprovedVersionChange={handleApprovedProtocolChange}
        />
      </Stage>
      <Stage id="discover" title="Discover">
        <ReportIdentityPanel projectId={project.id} readOnly={archived} />
        <CorpusPanel projectId={project.id} readOnly={archived} />
        <SearchSchedulePanel
          projectId={project.id}
          roles={roles.data ?? []}
          readOnly={archived}
        />
        <ReviewVersionsPanel
          projectId={project.id}
          roles={roles.data ?? []}
          readOnly={archived}
        />
      </Stage>
      <Stage id="select" title="Select">
        <ScreeningQueuePanel
          projectId={project.id}
          approvedProtocolVersionId={approvedProtocolVersionId}
          roles={roles.data ?? []}
          readOnly={archived}
          canEdit={canEdit}
        />
        <ScreeningConflictsPanel
          projectId={project.id}
          roles={roles.data ?? []}
          readOnly={archived}
        />
        <PrismaFlowCard projectId={project.id} />
      </Stage>
      <Stage id="extract" title="Extract">
        <AppraisalPanel
          projectId={project.id}
          roles={roles.data ?? []}
          readOnly={archived}
        />
        <EvidenceTablePanel
          projectId={project.id}
          roles={roles.data ?? []}
          readOnly={archived}
        />
        <SynthesisPanel
          projectId={project.id}
          roles={roles.data ?? []}
          readOnly={archived}
        />
        {onOpenTab && (
          <OpenTab label="Open matrix" onClick={() => onOpenTab('matrix')} />
        )}
      </Stage>
      <Stage id="write" title="Write">
        {currentDraft.data && (
          <DraftClaimsPanel
            projectId={project.id}
            draftId={currentDraft.data.id}
            content={currentDraft.data.content}
            roles={myRoles}
            canEdit={canEdit}
          />
        )}
        {onOpenTab && (
          <OpenTab label="Open drafts" onClick={() => onOpenTab('drafts')} />
        )}
      </Stage>
      <ProjectRoles
        projectId={project.id}
        assignments={roles.data ?? []}
        loading={roles.isLoading}
        canManage={canManageRoles}
      />
    </div>
  );
}

interface ProjectRolesProps {
  projectId: string;
  assignments: Awaited<ReturnType<typeof listProjectRoles>>;
  loading: boolean;
  canManage: boolean;
}

function ProjectRoles({
  projectId,
  assignments,
  loading,
  canManage,
}: ProjectRolesProps): ReactElement {
  const queryClient = useQueryClient();
  const [userId, setUserId] = useState('');
  const [role, setRole] = useState<ResearchProjectRole>('reviewer');
  const queryKey = ['project', projectId, 'research-engine', 'roles'] as const;
  const assign = useMutation({
    mutationFn: () => assignProjectRole(projectId, userId.trim(), role),
    onSuccess: () => {
      setUserId('');
      void queryClient.invalidateQueries({ queryKey });
    },
  });
  const remove = useMutation({
    mutationFn: (assignment: { user_id: string; role: ResearchProjectRole }) =>
      removeProjectRole(projectId, assignment.user_id, assignment.role),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey }),
  });

  return (
    <section className="rounded-xl border border-border bg-card p-5">
      <h2 className="font-medium text-foreground">Workflow role assignments</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Reviewer, adjudicator, and supervisor are explicit independent roles.
      </p>
      {loading ? (
        <p role="status" className="mt-4 text-sm text-muted-foreground">
          Loading assignments…
        </p>
      ) : assignments.length === 0 ? (
        <p className="mt-4 text-sm text-muted-foreground">
          No workflow roles assigned.
        </p>
      ) : (
        <ul className="mt-4 divide-y divide-border">
          {assignments.map((assignment) => (
            <li
              key={assignment.id}
              className="flex items-center justify-between gap-3 py-2 text-sm"
            >
              <span className="min-w-0 truncate text-foreground">
                {assignment.user_id}
              </span>
              <span className="ml-auto rounded bg-muted px-2 py-1 text-xs text-muted-foreground">
                {assignment.role}
              </span>
              {canManage && (
                <button
                  type="button"
                  aria-label={`Remove ${assignment.role} role from ${assignment.user_id}`}
                  onClick={() => remove.mutate(assignment)}
                  disabled={remove.isPending}
                  className="rounded p-1 text-muted-foreground hover:text-destructive disabled:opacity-50"
                >
                  <Trash2 className="h-4 w-4" aria-hidden="true" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {canManage && (
        <div className="mt-4 flex flex-col gap-2 sm:flex-row">
          <label className="sr-only" htmlFor="workflow-role-user">
            User ID
          </label>
          <input
            id="workflow-role-user"
            value={userId}
            onChange={(event) => setUserId(event.target.value)}
            placeholder="User ID"
            className="min-w-0 flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm"
          />
          <label className="sr-only" htmlFor="workflow-role">
            Workflow role
          </label>
          <select
            id="workflow-role"
            value={role}
            onChange={(event) =>
              setRole(event.target.value as ResearchProjectRole)
            }
            className="rounded-md border border-border bg-background px-3 py-2 text-sm"
          >
            {ROLES.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => assign.mutate()}
            disabled={!userId.trim() || assign.isPending}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            Assign
          </button>
        </div>
      )}
      {(assign.error || remove.error) && (
        <p role="alert" className="mt-3 text-sm text-destructive">
          Failed to update workflow roles.
        </p>
      )}
    </section>
  );
}

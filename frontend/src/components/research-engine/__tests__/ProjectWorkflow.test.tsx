import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ProjectWorkflow } from '../ProjectWorkflow';
import { projectService } from '@/services/projectService';
import {
  createProject,
  listProjectRoles,
} from '@/services/researchEngineService';

vi.mock('../JourneyRail', () => ({
  JourneyRail: ({ projectId }: { projectId: string }) => (
    <div>Journey for {projectId}</div>
  ),
}));
vi.mock('@/components/research/DraftClaimsPanel', () => ({
  DraftClaimsPanel: ({
    draftId,
    roles,
    canEdit,
  }: {
    draftId: string;
    roles?: string[];
    canEdit?: boolean;
  }) => (
    <div>
      Claims for {draftId} as {(roles ?? []).join(',') || 'none'}{' '}
      {canEdit ? '(editable)' : '(read only)'}
    </div>
  ),
}));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'me' } }),
}));
vi.mock('@/services/projectService', () => ({
  projectService: { getCurrentDraft: vi.fn() },
}));
vi.mock('../BlueprintEditor', () => ({
  BlueprintEditor: ({
    projectId,
    readOnly,
  }: {
    projectId: string;
    readOnly?: boolean;
  }) => (
    <div>
      Blueprint for {projectId} {readOnly ? '(read only)' : '(editable)'}
    </div>
  ),
}));
vi.mock('../ProtocolPanel', () => ({
  ProtocolPanel: ({ projectId }: { projectId: string }) => (
    <div>Protocol for {projectId}</div>
  ),
}));
vi.mock('../ReportIdentityPanel', () => ({
  ReportIdentityPanel: ({ projectId }: { projectId: string }) => (
    <div>Reports for {projectId}</div>
  ),
}));
vi.mock('../CorpusPanel', () => ({
  CorpusPanel: ({
    projectId,
    readOnly,
  }: {
    projectId: string;
    readOnly?: boolean;
  }) => (
    <div>
      Corpus for {projectId} {readOnly ? '(read only)' : '(editable)'}
    </div>
  ),
}));
vi.mock('../ScreeningQueuePanel', () => ({
  ScreeningQueuePanel: ({
    projectId,
    readOnly,
  }: {
    projectId: string;
    readOnly?: boolean;
  }) => (
    <div>
      Screening for {projectId} {readOnly ? '(read only)' : '(active)'}
    </div>
  ),
}));
vi.mock('../PrismaFlowCard', () => ({
  PrismaFlowCard: ({ projectId }: { projectId: string }) => (
    <div>PRISMA for {projectId}</div>
  ),
}));
vi.mock('../ScreeningConflictsPanel', () => ({
  ScreeningConflictsPanel: ({
    projectId,
    readOnly,
  }: {
    projectId: string;
    readOnly?: boolean;
  }) => (
    <div>
      Conflicts for {projectId} {readOnly ? '(read only)' : '(active)'}
    </div>
  ),
}));
vi.mock('@/services/researchEngineService', () => ({
  createProject: vi.fn(),
  listProjectRoles: vi.fn(),
  assignProjectRole: vi.fn(),
  removeProjectRole: vi.fn(),
  listReports: vi.fn(),
  listReportHistory: vi.fn(),
  linkStudy: vi.fn(),
  mergeReports: vi.fn(),
  listScreeningQueues: vi.fn(),
  createScreeningQueue: vi.fn(),
  assignScreeningReviewer: vi.fn(),
  revokeScreeningAssignment: vi.fn(),
  getMyScreeningQueue: vi.fn(),
  submitScreeningObservation: vi.fn(),
  listScreeningHistory: vi.fn(),
  listScreeningConflicts: vi.fn(),
  adjudicateScreening: vi.fn(),
  reopenScreening: vi.fn(),
}));
describe('ProjectWorkflow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listProjectRoles).mockResolvedValue([]);
    vi.mocked(projectService.getCurrentDraft).mockRejectedValue(
      new Error('no draft')
    );
  });

  it('groups panels under five journey anchors with the rail on top', async () => {
    vi.mocked(listProjectRoles).mockResolvedValue([
      {
        id: 'a-1',
        collection_id: 'collection-1',
        user_id: 'me',
        role: 'adjudicator',
        assigned_by_id: 'owner-1',
        created_at: '2026-01-01T00:00:00Z',
      },
      {
        id: 'a-2',
        collection_id: 'collection-1',
        user_id: 'someone-else',
        role: 'reviewer',
        assigned_by_id: 'owner-1',
        created_at: '2026-01-01T00:00:00Z',
      },
    ] as Awaited<ReturnType<typeof listProjectRoles>>);
    vi.mocked(projectService.getCurrentDraft).mockResolvedValue({
      id: 'draft-9',
      content: 'Body.',
    } as Awaited<ReturnType<typeof projectService.getCurrentDraft>>);
    const onOpenTab = vi.fn();
    const { container } = render(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectWorkflow
          project={{
            id: 'collection-1',
            workspace_id: 'workspace-1',
            name: 'Journey',
            research_engine_project_id: 'engine-1',
            can_edit: true,
            can_manage: true,
            workspace_archived: false,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          }}
          onOpenTab={onOpenTab}
        />
      </QueryClientProvider>
    );

    expect(screen.getByText('Journey for collection-1')).toBeInTheDocument();
    expect(
      [...container.querySelectorAll('section[id^="journey-"]')].map(
        (section) => section.id
      )
    ).toEqual([
      'journey-plan',
      'journey-discover',
      'journey-select',
      'journey-extract',
      'journey-write',
    ]);
    expect(container.querySelector('#journey-select')?.textContent).toContain(
      'Screening for collection-1'
    );
    expect(
      await screen.findByText('Claims for draft-9 as adjudicator (editable)')
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open matrix' }));
    fireEvent.click(screen.getByRole('button', { name: 'Open drafts' }));
    expect(onOpenTab.mock.calls).toEqual([['matrix'], ['drafts']]);
  });

  it('keeps blueprint and role reads on the canonical collection id', async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectWorkflow
          project={{
            id: 'collection-1',
            workspace_id: 'workspace-1',
            user_id: 'owner-1',
            name: 'One visible project',
            research_engine_project_id: 'engine-1',
            can_edit: true,
            can_manage: true,
            workspace_archived: false,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          }}
        />
      </QueryClientProvider>
    );

    expect(
      screen.getByText('Blueprint for collection-1 (editable)')
    ).toBeInTheDocument();
    expect(screen.getByText('Reports for collection-1')).toBeInTheDocument();
    expect(
      screen.getByText('Corpus for collection-1 (editable)')
    ).toBeInTheDocument();
    expect(
      screen.getByText('Screening for collection-1 (active)')
    ).toBeInTheDocument();
    expect(screen.getByText('PRISMA for collection-1')).toBeInTheDocument();
    await waitFor(() =>
      expect(listProjectRoles).toHaveBeenCalledWith('collection-1')
    );
    expect(
      await screen.findByText('No workflow roles assigned.')
    ).toBeInTheDocument();
  });

  it('uses server capabilities and prevents extension state leaking across projects', async () => {
    vi.mocked(createProject).mockResolvedValue({
      id: 'collection-1',
      project_id: 'collection-1',
      collection_id: 'collection-1',
      research_engine_project_id: 'engine-1',
      name: 'First',
      status: 'active',
    });
    const queryClient = new QueryClient();
    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <ProjectWorkflow
          project={{
            id: 'collection-1',
            workspace_id: 'workspace-1',
            name: 'First',
            can_edit: true,
            can_manage: true,
            workspace_archived: false,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          }}
        />
      </QueryClientProvider>
    );

    screen.getByRole('button', { name: 'Enable workflow' }).click();
    await waitFor(() =>
      expect(createProject).toHaveBeenCalledWith({
        collection_id: 'collection-1',
        name: 'First',
      })
    );
    expect(
      await screen.findByText('Blueprint for collection-1 (editable)')
    ).toBeInTheDocument();

    rerender(
      <QueryClientProvider client={queryClient}>
        <ProjectWorkflow
          project={{
            id: 'collection-2',
            workspace_id: 'workspace-2',
            name: 'Second',
            can_edit: false,
            can_manage: false,
            workspace_archived: false,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          }}
        />
      </QueryClientProvider>
    );

    expect(
      screen.queryByText(/Blueprint for collection-2/)
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        'A project owner or administrator must enable this workflow.'
      )
    ).toBeInTheDocument();
  });

  it('does not let an editor without manage capability enable a workflow', () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectWorkflow
          project={{
            id: 'collection-editor',
            workspace_id: 'workspace-1',
            name: 'Editor project',
            can_edit: true,
            can_manage: false,
            workspace_archived: false,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          }}
        />
      </QueryClientProvider>
    );

    expect(
      screen.queryByRole('button', { name: 'Enable workflow' })
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        'A project owner or administrator must enable this workflow.'
      )
    ).toBeInTheDocument();
  });

  it('renders the corpus panel read-only for an archived project', async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectWorkflow
          project={{
            id: 'collection-3',
            workspace_id: 'workspace-3',
            name: 'Archived',
            research_engine_project_id: 'engine-3',
            research_status: 'archived',
            can_edit: true,
            can_manage: true,
            workspace_archived: false,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          }}
        />
      </QueryClientProvider>
    );

    expect(
      await screen.findByText('Corpus for collection-3 (read only)')
    ).toBeInTheDocument();
    expect(
      screen.getByText('Screening for collection-3 (read only)')
    ).toBeInTheDocument();
    expect(
      screen.getByText('Conflicts for collection-3 (read only)')
    ).toBeInTheDocument();
  });
});

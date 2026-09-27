import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ProjectWorkflow } from '../ProjectWorkflow';
import {
  createProject,
  listProjectRoles,
} from '@/services/researchEngineService';

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
vi.mock('@/services/researchEngineService', () => ({
  createProject: vi.fn(),
  listProjectRoles: vi.fn(),
  assignProjectRole: vi.fn(),
  removeProjectRole: vi.fn(),
}));
describe('ProjectWorkflow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listProjectRoles).mockResolvedValue([]);
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
            can_manage: false,
            workspace_archived: false,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          }}
        />
      </QueryClientProvider>
    );

    screen.getByRole('button', { name: 'Enable workflow' }).click();
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

    expect(screen.queryByText(/Blueprint for collection-2/)).not.toBeInTheDocument();
    expect(
      screen.getByText('A project owner or administrator must enable this workflow.')
    ).toBeInTheDocument();
  });
});

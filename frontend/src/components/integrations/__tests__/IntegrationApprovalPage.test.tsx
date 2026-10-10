import { describe, expect, it, vi } from 'vitest';

import { render, screen } from '@/test/test-utils';

import IntegrationApprovalPage from '../../../../app/(dashboard)/integrations/approve/page';

const mockGet = vi.fn();

vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams('request_id=req-1'),
}));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'user-1' }, isAuthenticated: true }),
}));
vi.mock('@/services/api-client', () => ({
  api: { get: (...args: unknown[]) => mockGet(...args), post: vi.fn() },
}));

const request = {
  id: 'req-1',
  status: 'pending',
  expires_at: '2026-10-05T12:00:00Z',
  approval_url: '/integrations/approve?request_id=req-1',
  project_id: 'project-1',
  device_id: 'device-1',
  scopes: ['artifacts:publish'],
  project_label: 'Thesis',
  device_label: 'Laptop',
};

// A workspace request carries no project: the API sends null for both.
const workspaceRequest = {
  ...request,
  project_id: null,
  project_label: null,
  workspace_id: 'workspace-1',
  workspace_label: 'Reading group',
  scopes: ['tools:read', 'library:read', 'library:write'],
};

describe('IntegrationApprovalPage', () => {
  it('shows the chat a request is bound to', async () => {
    mockGet.mockResolvedValue({
      ...request,
      thread_id: 'thread-1',
      thread_label: 'Literature review',
    });
    render(<IntegrationApprovalPage />);
    expect(await screen.findByText('Chat')).toBeInTheDocument();
    expect(
      screen.getByText('Literature review (thread-1)')
    ).toBeInTheDocument();
  });

  it('omits the chat row for a project-only request', async () => {
    mockGet.mockResolvedValue({
      ...request,
      thread_id: null,
      thread_label: null,
    });
    render(<IntegrationApprovalPage />);
    expect(await screen.findByText('Thesis (project-1)')).toBeInTheDocument();
    expect(screen.queryByText('Chat')).not.toBeInTheDocument();
  });

  it('shows the workspace a request is bound to instead of a project', async () => {
    mockGet.mockResolvedValue(workspaceRequest);
    render(<IntegrationApprovalPage />);
    expect(await screen.findByText('Workspace')).toBeInTheDocument();
    expect(screen.getByText('Reading group (workspace-1)')).toBeInTheDocument();
    expect(screen.queryByText('Project')).not.toBeInTheDocument();
  });

  it('shows a project request as a project, not a workspace', async () => {
    mockGet.mockResolvedValue(request);
    render(<IntegrationApprovalPage />);
    expect(await screen.findByText('Project')).toBeInTheDocument();
    expect(screen.getByText('Thesis (project-1)')).toBeInTheDocument();
    expect(screen.queryByText('Workspace')).not.toBeInTheDocument();
  });

  it('explains library:write and still shows every raw scope', async () => {
    mockGet.mockResolvedValue(workspaceRequest);
    render(<IntegrationApprovalPage />);
    expect(
      await screen.findByText(/without asking each time/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Deleting folders and ingesting papers still require/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/change the title and tags of a paper/)
    ).toBeInTheDocument();
    for (const scope of workspaceRequest.scopes) {
      expect(screen.getByText(scope, { selector: 'code' })).toBeInTheDocument();
    }
  });

  it('shows a scope it has no label for by its raw name', async () => {
    mockGet.mockResolvedValue({ ...request, scopes: ['weird:scope'] });
    render(<IntegrationApprovalPage />);
    expect(
      await screen.findByText('weird:scope', { selector: 'code' })
    ).toBeInTheDocument();
  });
});

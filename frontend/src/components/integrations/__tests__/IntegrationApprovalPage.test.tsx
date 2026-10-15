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

// An account request binds neither a project nor a workspace (PR 1:
// binding_kind 'account'; account_summary holds today's counts).
const accountRequest = {
  ...request,
  project_id: null,
  project_label: null,
  workspace_id: null,
  workspace_label: null,
  binding_kind: 'account',
  account_summary: { workspace_count: 5, project_count: 33 },
  scopes: [
    'harness:execute',
    'library:read',
    'library:write',
    'tools:read',
    'tools:write',
  ],
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

  it('names the whole account and what it covers today', async () => {
    mockGet.mockResolvedValue(accountRequest);
    render(<IntegrationApprovalPage />);
    expect(
      await screen.findByRole('heading', {
        name: 'Laptop wants access to your whole NOUS account',
      })
    ).toBeInTheDocument();
    expect(screen.getByText('Covers')).toBeInTheDocument();
    expect(
      screen.getByText(
        /Every project in the workspaces you own or belong to in your organization, including projects added later/
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Archived and deleted workspaces and projects are not included/
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText('Today: 5 workspaces and 33 projects.')
    ).toBeInTheDocument();
    expect(screen.queryByText('Project')).not.toBeInTheDocument();
    expect(screen.queryByText('Workspace')).not.toBeInTheDocument();
  });

  it('counts one workspace and one project in the singular', async () => {
    mockGet.mockResolvedValue({
      ...accountRequest,
      account_summary: { workspace_count: 1, project_count: 1 },
    });
    render(<IntegrationApprovalPage />);
    expect(
      await screen.findByText('Today: 1 workspace and 1 project.')
    ).toBeInTheDocument();
  });

  it('leaves the counts out when the server sends none', async () => {
    mockGet.mockResolvedValue({ ...accountRequest, account_summary: null });
    render(<IntegrationApprovalPage />);
    expect(await screen.findByText('Covers')).toBeInTheDocument();
    expect(screen.queryByText(/^Today:/)).not.toBeInTheDocument();
  });

  it('words each permission for the whole account and still shows every raw scope', async () => {
    mockGet.mockResolvedValue(accountRequest);
    render(<IntegrationApprovalPage />);
    expect(
      await screen.findByText(
        /Run Local Codex on this computer from a chat in any project in your account/
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(/List the workspaces and projects in your account/)
    ).toBeInTheDocument();
    expect(screen.queryByText(/in this workspace/)).not.toBeInTheDocument();
    for (const scope of accountRequest.scopes) {
      expect(screen.getByText(scope, { selector: 'code' })).toBeInTheDocument();
    }
  });

  it('warns plainly that library changes run without asking', async () => {
    mockGet.mockResolvedValue(accountRequest);
    render(<IntegrationApprovalPage />);
    const warning = await screen.findByRole('alert');
    expect(warning).toHaveTextContent(
      'This device can change your library without asking.'
    );
    expect(warning).toHaveTextContent(
      /every project in your account without asking you first/
    );
    expect(warning).toHaveTextContent(
      /Deleting folders and ingesting papers still ask you first/
    );
  });

  it('shows no warning for an account request without library:write', async () => {
    mockGet.mockResolvedValue({
      ...accountRequest,
      scopes: ['harness:execute', 'tools:read'],
    });
    render(<IntegrationApprovalPage />);
    expect(await screen.findByText('Covers')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps the workspace page as it was', async () => {
    mockGet.mockResolvedValue({
      ...workspaceRequest,
      binding_kind: 'workspace',
      account_summary: null,
    });
    render(<IntegrationApprovalPage />);
    expect(await screen.findByText('Workspace')).toBeInTheDocument();
    expect(screen.getByText(/items in this workspace/)).toBeInTheDocument();
    expect(screen.queryByText(/whole NOUS account/)).not.toBeInTheDocument();
    expect(screen.queryByText('Covers')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
  describe('a request it cannot describe', () => {
    const cannotShow = "This request can't be shown here.";

    it.each([
      ['an unknown binding kind', { ...request, binding_kind: 'organization' }],
      [
        'a project request without its project',
        {
          ...request,
          binding_kind: 'project',
          project_id: null,
          project_label: null,
        },
      ],
      [
        'a workspace request without its workspace',
        {
          ...workspaceRequest,
          binding_kind: 'workspace',
          workspace_id: null,
          workspace_label: null,
        },
      ],
    ])('refuses %s and cannot be approved', async (_name, body) => {
      mockGet.mockResolvedValue(body);
      render(<IntegrationApprovalPage />);
      expect(await screen.findByText(cannotShow)).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: 'Approve access' })
      ).toBeDisabled();
      expect(screen.queryByText('Project')).not.toBeInTheDocument();
      expect(screen.queryByText(/null/)).not.toBeInTheDocument();
    });

    it('treats a request with no binding kind and no ids as the whole account', async () => {
      const { binding_kind: _kind, ...legacy } = accountRequest;
      mockGet.mockResolvedValue(legacy);
      render(<IntegrationApprovalPage />);
      expect(
        await screen.findByRole('heading', {
          name: 'Laptop wants access to your whole NOUS account',
        })
      ).toBeInTheDocument();
      expect(screen.getByText('Covers')).toBeInTheDocument();
      expect(screen.getByRole('alert')).toHaveTextContent(
        'This device can change your library without asking.'
      );
      expect(screen.queryByText(cannotShow)).not.toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: 'Approve access' })
      ).toBeEnabled();
    });

    it('still approves a project request an older backend sent without a kind', async () => {
      mockGet.mockResolvedValue(request);
      render(<IntegrationApprovalPage />);
      expect(await screen.findByText('Thesis (project-1)')).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: 'Approve access' })
      ).toBeEnabled();
    });
  });
});

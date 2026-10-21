import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/integrationConnectionsService', () => ({
  integrationConnectionsService: {
    list: vi.fn(),
    revokeConsent: vi.fn(),
    disconnect: vi.fn(),
  },
}));
vi.mock('@/services/harnessService', () => ({
  harnessService: { listFolders: vi.fn() },
}));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: (): { user: { id: string } } => ({ user: { id: 'u1' } }),
}));

import { harnessService } from '@/services/harnessService';
import { integrationConnectionsService } from '@/services/integrationConnectionsService';
import { render } from '@/test/test-utils';
import type {
  ApiConnectedDevice,
  ApiConnectionConsent,
} from '@/types/api/integration-connections-contract';

import { ConnectedDevices } from '../ConnectedDevices';

const laptop: ApiConnectedDevice = {
  device_id: 'd1',
  device_label: 'Laptop',
  connected_at: '2026-09-30T00:00:00Z',
  consents: [
    {
      request_id: 'r1',
      kind: 'project',
      project_id: 'p1',
      project_label: 'Thesis',
      workspace_id: null,
      workspace_label: null,
      thread_id: null,
      thread_label: null,
      scopes: ['harness:execute', 'tools:read'],
      status: 'consumed',
      approved_at: null,
    },
  ],
};

const workspaceConsent: ApiConnectionConsent = {
  request_id: 'r2',
  kind: 'workspace',
  project_id: null,
  project_label: null,
  workspace_id: 'w1',
  workspace_label: 'Lab',
  thread_id: null,
  thread_label: null,
  scopes: ['library:read', 'library:write', 'tools:read', 'tools:write'],
  status: 'consumed',
  approved_at: null,
};

// An account consent binds neither a project nor a workspace (PR 1).
const accountConsent: ApiConnectionConsent = {
  request_id: 'r4',
  kind: 'account',
  project_id: null,
  project_label: null,
  workspace_id: null,
  workspace_label: null,
  thread_id: null,
  thread_label: null,
  scopes: [
    'harness:execute',
    'library:read',
    'library:write',
    'tools:read',
    'tools:write',
  ],
  status: 'consumed',
  approved_at: null,
};

const accountLaptop: ApiConnectedDevice = {
  ...laptop,
  consents: [accountConsent],
};

describe('ConnectedDevices', () => {
  beforeEach(() => {
    vi.mocked(harnessService.listFolders).mockResolvedValue([]);
  });

  it('lists devices with plain-language access', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([laptop]);
    render(<ConnectedDevices />);
    expect(await screen.findByText(/Thesis \(p1\)/)).toBeInTheDocument();
    expect(
      screen.getByText('run coding sessions, read project documents')
    ).toBeInTheDocument();
  });

  it('lists an account consent as the whole account and revokes it on its own', async () => {
    vi.mocked(integrationConnectionsService.list)
      .mockResolvedValueOnce([
        { ...laptop, consents: [...laptop.consents, accountConsent] },
      ])
      .mockResolvedValue([laptop]);
    vi.mocked(integrationConnectionsService.revokeConsent).mockResolvedValue();
    const { user } = render(<ConnectedDevices />);
    expect(await screen.findByText('Whole account')).toBeInTheDocument();
    expect(
      screen.getByText(
        'run Local Codex from any chat, list your workspaces and projects, add, move and remove papers, create folders, rename projects and edit paper details anywhere in your account without asking each time, read documents in every project, request changes in any project (you approve each one)'
      )
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole('button', {
        name: 'Revoke Laptop (d1) access to your whole account',
      })
    );
    expect(integrationConnectionsService.revokeConsent).toHaveBeenCalledTimes(
      1
    );
    expect(integrationConnectionsService.revokeConsent).toHaveBeenCalledWith(
      'r4'
    );
    await waitFor(() =>
      expect(screen.queryByText('Whole account')).not.toBeInTheDocument()
    );
    expect(screen.getByText('Thesis (p1)')).toBeInTheDocument();
  });

  it('revokes one project and refreshes the list', async () => {
    vi.mocked(integrationConnectionsService.list)
      .mockResolvedValueOnce([laptop])
      .mockResolvedValue([{ ...laptop, consents: [] }]);
    vi.mocked(integrationConnectionsService.revokeConsent).mockResolvedValue();
    const { user } = render(<ConnectedDevices />);
    await user.click(
      await screen.findByRole('button', {
        name: 'Revoke Laptop (d1) access to Thesis',
      })
    );
    expect(integrationConnectionsService.revokeConsent).toHaveBeenCalledWith(
      'r1'
    );
    expect(
      await screen.findByText('No active project access.')
    ).toBeInTheDocument();
  });

  it('disconnects a device and says when nothing is connected', async () => {
    vi.mocked(integrationConnectionsService.list)
      .mockResolvedValueOnce([laptop])
      .mockResolvedValue([]);
    vi.mocked(integrationConnectionsService.disconnect).mockResolvedValue();
    const { user } = render(<ConnectedDevices />);
    await user.click(
      await screen.findByRole('button', { name: 'Disconnect Laptop (d1)' })
    );
    expect(integrationConnectionsService.disconnect).not.toHaveBeenCalled();
    expect(
      screen.getByText(/ends all existing CLI sign-ins/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/every other connected device also stops working/)
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/keep their integration project access/)
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        /rather than disconnecting the old device, which would end every CLI sign-in again/
      )
    ).toBeInTheDocument();
    expect(screen.queryByText(/disconnect the old/i)).not.toBeInTheDocument();
    await user.click(
      screen.getByRole('button', { name: 'Disconnect and end CLI sign-ins' })
    );
    expect(integrationConnectionsService.disconnect).toHaveBeenCalledWith('d1');
    expect(
      await screen.findByText('No devices are connected.')
    ).toBeInTheDocument();
  });

  it('reports a failed revoke and a failed load', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([laptop]);
    vi.mocked(integrationConnectionsService.disconnect).mockRejectedValueOnce(
      new Error('404')
    );
    const { user, unmount } = render(<ConnectedDevices />);
    await user.click(
      await screen.findByRole('button', { name: 'Disconnect Laptop (d1)' })
    );
    await user.click(
      screen.getByRole('button', { name: 'Disconnect and end CLI sign-ins' })
    );
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(
        'could not be revoked'
      )
    );
    unmount();
    vi.mocked(integrationConnectionsService.list).mockRejectedValueOnce(
      new Error('500')
    );
    render(<ConnectedDevices />);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'could not be loaded'
    );
  });

  it('distinguishes devices with the same label and allows canceling', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([
      laptop,
      { ...laptop, device_id: 'd2', consents: [] },
    ]);
    const { user } = render(<ConnectedDevices />);
    await user.click(
      await screen.findByRole('button', { name: 'Disconnect Laptop (d2)' })
    );
    expect(screen.getByText(/Disconnect Laptop \(d2\)\?/)).toBeInTheDocument();
    expect(screen.getByText('Device ID: d1')).toBeInTheDocument();
    expect(screen.getByText('Device ID: d2')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(
      screen.queryByText(/ends all existing CLI sign-ins/)
    ).not.toBeInTheDocument();
    expect(integrationConnectionsService.disconnect).not.toHaveBeenCalled();
  });

  it('lists a workspace consent and revokes it on its own', async () => {
    vi.mocked(integrationConnectionsService.list)
      .mockResolvedValueOnce([
        { ...laptop, consents: [...laptop.consents, workspaceConsent] },
      ])
      .mockResolvedValue([laptop]);
    vi.mocked(integrationConnectionsService.revokeConsent).mockResolvedValue();
    const { user } = render(<ConnectedDevices />);
    expect(
      await screen.findByText('Workspace Lab (w1), every project in it')
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'list the folders in the workspace, change folders and paper details without asking each time, read project documents, request changes (you approve each one)'
      )
    ).toBeInTheDocument();
    expect(screen.getByText('Thesis (p1)')).toBeInTheDocument();
    await user.click(
      screen.getByRole('button', {
        name: 'Revoke Laptop (d1) access to workspace Lab',
      })
    );
    expect(integrationConnectionsService.revokeConsent).toHaveBeenCalledTimes(
      1
    );
    expect(integrationConnectionsService.revokeConsent).toHaveBeenCalledWith(
      'r2'
    );
    await waitFor(() =>
      expect(
        screen.queryByText('Workspace Lab (w1), every project in it')
      ).not.toBeInTheDocument()
    );
    expect(screen.getByText('Thesis (p1)')).toBeInTheDocument();
  });

  it('links a memory-sharing consent to its memory selection', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([
      {
        ...laptop,
        consents: [
          { ...laptop.consents[0], scopes: ['context:read', 'tools:read'] },
          { ...laptop.consents[0], request_id: 'r3', project_label: 'Grant' },
        ],
      },
    ]);
    render(<ConnectedDevices />);
    const links = await screen.findAllByRole('link', {
      name: /^Choose shared memories/,
    });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAccessibleName('Choose shared memories for Thesis');
    expect(links[0]).toHaveAttribute('href', '/integrations/context/r1');
  });

  it('names the chat a consent is bound to', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([
      {
        ...laptop,
        consents: [
          {
            ...laptop.consents[0],
            thread_id: 't1',
            thread_label: 'Lit review',
          },
          {
            ...laptop.consents[0],
            request_id: 'r3',
            thread_id: 't2',
            thread_label: null,
          },
        ],
      },
    ]);
    render(<ConnectedDevices />);
    expect(
      await screen.findByRole('link', { name: 'Lit review' })
    ).toHaveAttribute('href', '/chat?thread=t1');
    expect(
      screen.getByText(/a chat that is no longer available/)
    ).toBeInTheDocument();
    expect(screen.getAllByRole('link')).toHaveLength(1);
    expect(
      screen.getByRole('button', {
        name: 'Revoke Laptop (d1) access to Thesis, only from chat Lit review',
      })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', {
        name: 'Revoke Laptop (d1) access to Thesis, only from a chat that is no longer available',
      })
    ).toBeInTheDocument();
  });

  it('names each revoke control apart after a reconnect', async () => {
    // Reconnecting adds a second device with the same label and a fresh
    // consent for the same project; the Disconnect copy tells the user to
    // Revoke the old one, so the two controls must not share a name.
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([
      laptop,
      {
        ...laptop,
        device_id: 'd2',
        consents: [{ ...laptop.consents[0], request_id: 'r9' }],
      },
    ]);
    render(<ConnectedDevices />);
    const revokes = await screen.findAllByRole('button', { name: /^Revoke / });
    expect(revokes.map((button) => button.getAttribute('aria-label'))).toEqual([
      'Revoke Laptop (d1) access to Thesis',
      'Revoke Laptop (d2) access to Thesis',
    ]);
  });

  it('shows a scope it does not know by its own name', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([
      {
        ...laptop,
        consents: [{ ...laptop.consents[0], scopes: ['constructor'] }],
      },
    ]);
    render(<ConnectedDevices />);
    expect(await screen.findByText('constructor')).toBeInTheDocument();
  });

  it('reads an old-backend consent without the binding fields as a project consent', async () => {
    // Vercel can ship this page before the backend image, so the page may
    // briefly read a payload from before kind/workspace_*/thread_* existed.
    // Those fields are then absent (undefined), not null.
    const oldBackendConsent = {
      request_id: 'r1',
      project_id: 'p1',
      project_label: 'Thesis',
      scopes: ['harness:execute', 'tools:read'],
      status: 'consumed',
      approved_at: null,
    } as ApiConnectionConsent;
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([
      { ...laptop, consents: [oldBackendConsent] },
    ]);
    render(<ConnectedDevices />);
    expect(await screen.findByText('Thesis (p1)')).toBeInTheDocument();
    expect(
      screen.getByRole('button', {
        name: 'Revoke Laptop (d1) access to Thesis',
      })
    ).toBeInTheDocument();
    expect(screen.queryByText(/Only from chat/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Workspace/)).not.toBeInTheDocument();
  });

  it('lists the account folders of a computer with account access', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([
      accountLaptop,
    ]);
    vi.mocked(harnessService.listFolders).mockResolvedValue([
      { workspace_id: 'f1', project_id: null, label: 'thesis-code' },
      { workspace_id: 'f2', project_id: 'p1', label: 'project-only' },
    ]);
    render(<ConnectedDevices />);
    const folders = await screen.findByRole('list', {
      name: 'Account folders on Laptop (d1)',
    });
    expect(
      within(folders)
        .getAllByRole('listitem')
        .map((item) => item.textContent)
    ).toEqual(['thesis-code']);
    expect(screen.queryByText('project-only')).not.toBeInTheDocument();
    expect(harnessService.listFolders).toHaveBeenCalledWith('d1');
  });

  it('does not look up folders for a computer without account access', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([laptop]);
    render(<ConnectedDevices />);
    expect(await screen.findByText('Thesis (p1)')).toBeInTheDocument();
    expect(screen.queryByText('Account folders')).not.toBeInTheDocument();
    expect(harnessService.listFolders).not.toHaveBeenCalled();
  });

  it.each([
    [
      'without harness:execute',
      [{ ...accountConsent, scopes: ['library:read', 'tools:read'] }],
    ],
    ['that has not been used yet', [{ ...accountConsent, status: 'approved' }]],
    [
      'twice over (every run on the device is refused)',
      [accountConsent, { ...accountConsent, request_id: 'r5' }],
    ],
  ])(
    'offers no account folders for an account approval %s',
    async (_name, consents) => {
      vi.mocked(integrationConnectionsService.list).mockResolvedValue([
        { ...laptop, consents },
      ]);
      render(<ConnectedDevices />);
      expect(await screen.findAllByText('Whole account')).not.toHaveLength(0);
      expect(screen.queryByText('Account folders')).not.toBeInTheDocument();
      expect(
        screen.queryByText('No account folders on this computer yet.')
      ).not.toBeInTheDocument();
      expect(harnessService.listFolders).not.toHaveBeenCalled();
    }
  );

  it('says when a computer has no account folders yet', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([
      accountLaptop,
    ]);
    vi.mocked(harnessService.listFolders).mockResolvedValue([
      { workspace_id: 'f2', project_id: 'p1', label: 'project-only' },
    ]);
    render(<ConnectedDevices />);
    expect(
      await screen.findByText('No account folders on this computer yet.')
    ).toBeInTheDocument();
  });

  it('reports account folders that could not be loaded', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([
      accountLaptop,
    ]);
    vi.mocked(harnessService.listFolders).mockRejectedValue(
      new Error('500')
    );
    render(<ConnectedDevices />);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Account folders could not be loaded.'
    );
  });

  describe('what the chat composer keeps after access is removed', () => {
    // The composer reads the same harness caches (computers and folders); a
    // revoked computer must not stay on offer for the rest of their 30 s.
    const seeded = (): {
      client: QueryClient;
      wrapper: ({ children }: { children: ReactNode }) => ReactNode;
    } => {
      const client = new QueryClient({
        defaultOptions: {
          queries: { retry: false },
          mutations: { retry: false },
        },
      });
      client.setQueryData(['harness', 'devices', 'u1'], []);
      client.setQueryData(['harness', 'workspaces', 'u1', 'd1'], []);
      const wrapper = ({ children }: { children: ReactNode }): ReactNode => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      );
      return { client, wrapper };
    };
    const invalidated = (
      client: QueryClient,
      key: string[]
    ): boolean | undefined => client.getQueryState(key)?.isInvalidated;

    it('invalidates the composer caches when a consent is revoked', async () => {
      vi.mocked(integrationConnectionsService.list).mockResolvedValue([laptop]);
      vi.mocked(
        integrationConnectionsService.revokeConsent
      ).mockResolvedValue();
      const { client, wrapper } = seeded();
      const { user } = render(<ConnectedDevices />, { wrapper });
      await user.click(
        await screen.findByRole('button', {
          name: 'Revoke Laptop (d1) access to Thesis',
        })
      );
      await waitFor(() =>
        expect(invalidated(client, ['harness', 'devices', 'u1'])).toBe(true)
      );
      expect(invalidated(client, ['harness', 'workspaces', 'u1', 'd1'])).toBe(
        true
      );
    });

    it('invalidates the composer caches when a device is disconnected', async () => {
      vi.mocked(integrationConnectionsService.list).mockResolvedValue([laptop]);
      vi.mocked(integrationConnectionsService.disconnect).mockResolvedValue();
      const { client, wrapper } = seeded();
      const { user } = render(<ConnectedDevices />, { wrapper });
      await user.click(
        await screen.findByRole('button', { name: 'Disconnect Laptop (d1)' })
      );
      await user.click(
        screen.getByRole('button', { name: 'Disconnect and end CLI sign-ins' })
      );
      await waitFor(() =>
        expect(invalidated(client, ['harness', 'devices', 'u1'])).toBe(true)
      );
    });
  });
});

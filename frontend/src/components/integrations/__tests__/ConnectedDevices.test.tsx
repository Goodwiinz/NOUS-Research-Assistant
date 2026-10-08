import { screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@/services/integrationConnectionsService', () => ({
  integrationConnectionsService: {
    list: vi.fn(),
    revokeConsent: vi.fn(),
    disconnect: vi.fn(),
  },
}));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: (): { user: { id: string } } => ({ user: { id: 'u1' } }),
}));

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

describe('ConnectedDevices', () => {
  it('lists devices with plain-language access', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([laptop]);
    render(<ConnectedDevices />);
    expect(await screen.findByText(/Thesis \(p1\)/)).toBeInTheDocument();
    expect(
      screen.getByText('run coding sessions, read project documents')
    ).toBeInTheDocument();
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
        'list the folders in the workspace, change folders and paper details without asking each time, read project documents, request notes (you approve each one)'
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
});

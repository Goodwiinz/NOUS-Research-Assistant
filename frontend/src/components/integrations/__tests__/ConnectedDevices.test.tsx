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
import type { ApiConnectedDevice } from '@/types/api/integration-connections-contract';

import { ConnectedDevices } from '../ConnectedDevices';

const laptop: ApiConnectedDevice = {
  device_id: 'd1',
  device_label: 'Laptop',
  connected_at: '2026-09-30T00:00:00Z',
  consents: [
    {
      request_id: 'r1',
      project_id: 'p1',
      project_label: 'Thesis',
      scopes: ['harness:execute', 'tools:read'],
      status: 'consumed',
      approved_at: null,
    },
  ],
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
        name: 'Revoke Laptop access to Thesis',
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
});

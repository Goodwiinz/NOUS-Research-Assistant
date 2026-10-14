import React, { StrictMode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClientProvider } from '@tanstack/react-query';
import { act, screen, waitFor } from '@testing-library/react';
import { useAuthStore } from '@/stores/authStore';
import type { User } from '@/types/auth';
import { render, createTestQueryClient } from '@/test/test-utils';

vi.mock('@/services/artifactService', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/artifactService')>()),
  artifactService: {
    listArtifactShares: vi.fn(),
    createArtifactShare: vi.fn(),
    revokeArtifactShare: vi.fn(),
  },
}));
import {
  artifactService,
  type ArtifactVersion,
} from '@/services/artifactService';
import { ArtifactShareDialog } from '../ArtifactShareDialog';

const version: ArtifactVersion = {
  artifactId: 'a1',
  versionId: 'v1',
  parentVersionId: null,
  title: 'report.md',
  mimeType: 'text/markdown',
  byteSize: 7,
  sha256: 'abc',
  producer: 'harness',
  createdAt: '2026-10-11T12:00:00Z',
  sourceIds: ['private'],
};
const recipient = '11111111-1111-4111-8111-111111111111';
const share = {
  id: 's1',
  versionId: 'v1',
  expiresAt: '2099-10-12T12:00:00Z',
  revokedAt: null,
};

beforeEach(() => {
  useAuthStore.setState({
    user: { id: 'actor', organization_id: 'org' } as User,
    isAuthenticated: true,
  });
  vi.mocked(artifactService.listArtifactShares).mockResolvedValue([]);
  vi.mocked(artifactService.createArtifactShare).mockResolvedValue(share);
  vi.mocked(artifactService.revokeArtifactShare).mockResolvedValue(undefined);
});

describe('ArtifactShareDialog', () => {
  it('creates a link after Strict Mode checks effect cleanup', async () => {
    const client = createTestQueryClient();
    const wrapper = ({
      children,
    }: {
      children: React.ReactNode;
    }): React.ReactElement => (
      <StrictMode>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </StrictMode>
    );
    const { user } = render(
      <ArtifactShareDialog version={version} onClose={vi.fn()} />,
      { wrapper }
    );
    await user.type(
      await screen.findByLabelText('Audience user IDs'),
      recipient
    );
    await user.click(screen.getByRole('button', { name: 'Create share link' }));
    expect(
      await screen.findByRole('link', { name: 'Open shared version' })
    ).toHaveAttribute('href', '/artifacts/shared/s1');
  });

  it('keeps a newly created link when an older list request resolves', async () => {
    let resolve!: (value: (typeof share)[]) => void;
    vi.mocked(artifactService.listArtifactShares).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        })
    );
    const { user } = render(
      <ArtifactShareDialog version={version} onClose={vi.fn()} />
    );
    await user.type(
      await screen.findByLabelText('Audience user IDs'),
      recipient
    );
    await user.click(screen.getByRole('button', { name: 'Create share link' }));
    await screen.findByRole('link', { name: 'Open shared version' });
    expect(
      vi.mocked(artifactService.listArtifactShares).mock.calls[0][1]?.aborted
    ).toBe(true);
    await act(async () => resolve([]));
    expect(
      screen.getByRole('link', { name: 'Open shared version' })
    ).toHaveAttribute('href', '/artifacts/shared/s1');
  });

  it('copies a complete share URL and reports clipboard failures safely', async () => {
    vi.mocked(artifactService.listArtifactShares).mockResolvedValue([share]);
    const { user } = render(
      <ArtifactShareDialog version={version} onClose={vi.fn()} />
    );
    await screen.findByRole('link', { name: 'Open shared version' });
    const copy = vi
      .spyOn(navigator.clipboard, 'writeText')
      .mockRejectedValue(new Error('clipboard/private/key'));
    await user.click(screen.getByRole('button', { name: 'Copy share link' }));
    expect(copy).toHaveBeenCalledWith(
      `${window.location.origin}/artifacts/shared/s1`
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not copy'
    );
    expect(screen.queryByText(/clipboard\/private/)).toBeNull();
  });
  it('requires explicit valid audience UUIDs and shares the selected exact version', async () => {
    const { user } = render(
      <ArtifactShareDialog version={version} onClose={vi.fn()} />
    );
    const audience = await screen.findByLabelText('Audience user IDs');
    expect(audience).toHaveFocus();
    await user.type(audience, 'invalid');
    await user.click(screen.getByRole('button', { name: 'Create share link' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'valid user IDs'
    );
    expect(artifactService.createArtifactShare).not.toHaveBeenCalled();
    await user.clear(audience);
    await user.type(audience, `${recipient}, ${recipient}`);
    await user.click(screen.getByRole('button', { name: 'Create share link' }));
    const link = await screen.findByRole('link', {
      name: 'Open shared version',
    });
    expect(link).toHaveAttribute('href', '/artifacts/shared/s1');
    expect(artifactService.createArtifactShare).toHaveBeenCalledWith(
      'v1',
      [recipient],
      expect.any(String),
      expect.any(AbortSignal)
    );
    const expires = vi.mocked(artifactService.createArtifactShare).mock
      .calls[0][2];
    expect(new Date(expires).getTime()).toBeGreaterThan(Date.now());
    expect(new Date(expires).getTime()).toBeLessThanOrEqual(
      Date.now() + 7 * 86400000
    );
    expect(screen.queryByText('private')).toBeNull();
  });

  it('shows safe create errors without server details', async () => {
    vi.mocked(artifactService.createArtifactShare).mockRejectedValue(
      new Error('storage/private/key')
    );
    const { user } = render(
      <ArtifactShareDialog version={version} onClose={vi.fn()} />
    );
    await user.type(
      await screen.findByLabelText('Audience user IDs'),
      recipient
    );
    await user.click(screen.getByRole('button', { name: 'Create share link' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not create'
    );
    expect(screen.queryByText(/storage\/private/)).toBeNull();
  });

  it('does not show a cancelled request result in a reopened dialog', async () => {
    const client = createTestQueryClient();
    const wrapper = ({
      children,
    }: {
      children: React.ReactNode;
    }): React.ReactElement => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    let resolve!: (value: typeof share) => void;
    vi.mocked(artifactService.createArtifactShare).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        })
    );
    const close = vi.fn();
    const { user, rerender } = render(
      <ArtifactShareDialog version={version} onClose={close} />,
      { wrapper }
    );
    await user.type(
      await screen.findByLabelText('Audience user IDs'),
      recipient
    );
    await user.click(screen.getByRole('button', { name: 'Create share link' }));
    const signal = vi.mocked(artifactService.createArtifactShare).mock
      .calls[0][3];
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(close).toHaveBeenCalledOnce();
    expect(signal?.aborted).toBe(true);
    rerender(
      <ArtifactShareDialog key="new" version={version} onClose={vi.fn()} />
    );
    await act(async () => resolve(share));
    expect(
      client.getQueryData(['artifact-shares', '["actor","org"]', 'a1'])
    ).toEqual([]);
    expect(
      screen.queryByRole('link', { name: 'Open shared version' })
    ).toBeNull();
  });

  it('clears audience and cancels pending work on account switch', async () => {
    let resolve!: (value: typeof share) => void;
    vi.mocked(artifactService.createArtifactShare).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        })
    );
    const { user } = render(
      <ArtifactShareDialog version={version} onClose={vi.fn()} />
    );
    await user.type(
      await screen.findByLabelText('Audience user IDs'),
      recipient
    );
    await user.click(screen.getByRole('button', { name: 'Create share link' }));
    const signal = vi.mocked(artifactService.createArtifactShare).mock
      .calls[0][3];
    act(() =>
      useAuthStore.setState({
        user: { id: 'other', organization_id: 'org' } as User,
      })
    );
    expect(screen.getByLabelText('Audience user IDs')).toHaveValue('');
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve(share));
    expect(
      screen.queryByRole('link', { name: 'Open shared version' })
    ).toBeNull();
  });

  it('loads active links and revokes them without retaining the link', async () => {
    vi.mocked(artifactService.listArtifactShares).mockResolvedValue([share]);
    const { user } = render(
      <ArtifactShareDialog version={version} onClose={vi.fn()} />
    );
    await screen.findByRole('link', { name: 'Open shared version' });
    await user.click(screen.getByRole('button', { name: 'Revoke share link' }));
    await waitFor(() =>
      expect(
        screen.queryByRole('link', { name: 'Open shared version' })
      ).toBeNull()
    );
    expect(artifactService.revokeArtifactShare).toHaveBeenCalledWith(
      's1',
      expect.any(AbortSignal)
    );
    expect(screen.getByRole('status')).toHaveTextContent('revoked');
  });
});

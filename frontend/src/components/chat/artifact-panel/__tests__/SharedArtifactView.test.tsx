import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { useAuthStore } from '@/stores/authStore';
import type { User } from '@/types/auth';
import { render, createTestQueryClient } from '@/test/test-utils';

vi.mock('@/services/artifactService', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/artifactService')>()),
  artifactService: {
    getArtifactShare: vi.fn(),
    getSharedArtifactContent: vi.fn(),
    capabilities: vi.fn(),
  },
}));
import { artifactService } from '@/services/artifactService';
import { SharedArtifactView } from '../SharedArtifactView';

const metadata = {
  artifactId: 'a1',
  versionId: 'v1',
  title: 'Shared report',
  mimeType: 'text/plain',
  byteSize: 7,
  sha256: 'abc',
};
beforeEach(() => {
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: vi.fn(() => 'blob:shared'),
  });
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: vi.fn(),
  });
  useAuthStore.setState({
    user: { id: 'actor', organization_id: 'org' } as User,
    isAuthenticated: true,
  });
  vi.mocked(artifactService.getArtifactShare).mockResolvedValue(metadata);
  vi.mocked(artifactService.getSharedArtifactContent).mockResolvedValue(
    new Blob(['private body'], { type: 'text/plain' })
  );
  vi.mocked(artifactService.capabilities).mockResolvedValue({
    editingEnabled: true,
    previewEnabled: false,
    sharingEnabled: true,
  });
});

describe('SharedArtifactView', () => {
  it('renders only the shared snapshot without source, provenance or edit controls', async () => {
    render(<SharedArtifactView shareId="s1" />);
    expect(await screen.findByText('private body')).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: 'Shared report' })
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
    expect(screen.queryByLabelText('Provenance')).toBeNull();
    expect(artifactService.getSharedArtifactContent).toHaveBeenCalledWith(
      's1',
      expect.any(AbortSignal)
    );
  });

  it('drops private bytes and metadata while rechecking a revoked share', async () => {
    const client = createTestQueryClient();
    const wrapper = ({
      children,
    }: {
      children: React.ReactNode;
    }): React.ReactElement => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { user } = render(<SharedArtifactView shareId="s1" />, { wrapper });
    await screen.findByText('private body');
    let reject!: (error: Error) => void;
    vi.mocked(artifactService.getArtifactShare).mockImplementationOnce(
      () =>
        new Promise((_, denied) => {
          reject = denied;
        })
    );
    await user.click(
      screen.getByRole('button', { name: 'Refresh shared version' })
    );
    expect(screen.queryByText('private body')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Shared report' })).toBeNull();
    await act(async () => reject(new Error('revoked private database key')));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Shared file is unavailable'
    );
    expect(screen.queryByText('private body')).toBeNull();
    expect(
      JSON.stringify(
        client
          .getQueryCache()
          .getAll()
          .map((q) => q.state.data)
      )
    ).not.toContain('Shared report');
  });

  it('discards previous account content and ignores its late response', async () => {
    let resolve!: (value: Blob) => void;
    vi.mocked(artifactService.getSharedArtifactContent).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        })
    );
    const { user } = render(<SharedArtifactView shareId="s1" />);
    await waitFor(() =>
      expect(artifactService.getSharedArtifactContent).toHaveBeenCalledOnce()
    );
    const signal = vi.mocked(artifactService.getSharedArtifactContent).mock
      .calls[0][1];
    vi.mocked(artifactService.getArtifactShare).mockRejectedValue(
      new Error('not in audience')
    );
    act(() =>
      useAuthStore.setState({
        user: { id: 'other', organization_id: 'org' } as User,
      })
    );
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve(new Blob(['old private body'])));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Shared file is unavailable'
    );
    expect(screen.queryByText('old private body')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(screen.queryByText('old private body')).toBeNull();
  });

  it('releases image object URLs after an access check fails', async () => {
    const revoke = vi.spyOn(URL, 'revokeObjectURL');
    vi.mocked(artifactService.getArtifactShare).mockResolvedValue({
      ...metadata,
      mimeType: 'image/png',
    });
    vi.mocked(artifactService.getSharedArtifactContent).mockResolvedValue(
      new Blob(['image'], { type: 'image/png' })
    );
    const { user } = render(<SharedArtifactView shareId="s1" />);
    await screen.findByRole('img', { name: 'Shared report' });
    vi.mocked(artifactService.getArtifactShare).mockRejectedValue(
      new Error('revoked')
    );
    await user.click(
      screen.getByRole('button', { name: 'Refresh shared version' })
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Shared file is unavailable'
    );
    expect(screen.queryByRole('img', { name: 'Shared report' })).toBeNull();
    expect(revoke).toHaveBeenCalled();
  });
});

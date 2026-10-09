import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAuthStore } from '@/stores/authStore';
import { useChatStore } from '@/store/chat-store';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import type { User } from '@/types/auth';
vi.mock('@/services/artifactService', () => ({
  artifactService: { listVersions: vi.fn() },
}));
import { artifactService } from '@/services/artifactService';
import { useArtifactVersions } from '../useThreadArtifacts';
import { useArtifactPanelScope } from '../useArtifactScope';

beforeEach(() => {
  useArtifactPanelStore.getState().reset();
  useAuthStore.setState({
    user: { id: 'actor-a', organization_id: 'org-a' } as User,
    isAuthenticated: true,
  });
  useChatStore.setState({ currentWorkspaceId: 'w1', currentThreadId: 't1' });
});

it('hides and clears all identities on account, org, workspace, project or thread changes', () => {
  const { result, rerender } = renderHook(
    ({ projectId }) => useArtifactPanelScope(projectId),
    { initialProps: { projectId: 'p1' } }
  );
  for (const change of [
    () =>
      useAuthStore.setState({
        user: { id: 'actor-b', organization_id: 'org-a' } as User,
      }),
    () =>
      useAuthStore.setState({
        user: { id: 'actor-b', organization_id: 'org-b' } as User,
      }),
    () => useChatStore.setState({ currentWorkspaceId: 'w2' }),
    () => useChatStore.setState({ currentThreadId: 't2' }),
    () => rerender({ projectId: 'p2' }),
  ]) {
    act(() =>
      useArtifactPanelStore
        .getState()
        .openArtifact({
          kind: 'generated',
          artifactId: 'a1',
          versionId: 'v1',
          title: 'private',
        })
    );
    const previous = result.current;
    act(change);
    expect(result.current).not.toBe(previous);
    expect(useArtifactPanelStore.getState()).toMatchObject({
      artifact: null,
      tabs: [],
      isOpen: false,
    });
  }
});

it('does not reuse an in-flight previous account version result for the same ID', async () => {
  let finish!: (value: never[]) => void;
  vi.mocked(artifactService.listVersions)
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        })
    )
    .mockResolvedValueOnce([]);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(() => useArtifactVersions('same-id'), {
    wrapper,
  });
  await waitFor(() =>
    expect(artifactService.listVersions).toHaveBeenCalledTimes(1)
  );
  act(() =>
    useAuthStore.setState({
      user: { id: 'actor-b', organization_id: 'org-b' } as User,
    })
  );
  await waitFor(() =>
    expect(artifactService.listVersions).toHaveBeenCalledTimes(2)
  );
  expect(
    vi.mocked(artifactService.listVersions).mock.calls[0][1]?.aborted
  ).toBe(true);
  await act(async () => finish([{ versionId: 'secret' }] as never[]));
  await waitFor(() => expect(result.current.data).toEqual([]));
});

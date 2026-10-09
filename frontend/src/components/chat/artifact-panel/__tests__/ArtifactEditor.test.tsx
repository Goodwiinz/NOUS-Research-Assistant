import { ProjectPickerPopover } from '@/components/context-rail/ProjectPickerPopover';
import { useProjectStore } from '@/store/projectStore';
import { useProjectChatStore } from '@/store/projectChatStore';
import Link from 'next/link';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { render } from '@/test/test-utils';
import { useAuthStore } from '@/stores/authStore';
import type { User } from '@/types/auth';
import { useChatStore } from '@/store/chat-store';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { APIErrorClass } from '@/types/api';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useSlashCommands } from '@/hooks/chat/useSlashCommands';
const routerPush = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: routerPush, replace: vi.fn() }),
}));
import { useArtifactPanelScope } from '@/hooks/chat/useArtifactScope';
vi.mock('@/services/artifactService', () => ({
  artifactService: {
    fetchVersionBlob: vi.fn(),
    capabilities: vi.fn(),
    editVersion: vi.fn(),
    listVersions: vi.fn(),
    downloadVersion: vi.fn(),
  },
}));
import {
  artifactService,
  type ArtifactVersion,
} from '@/services/artifactService';
import { ArtifactFileView } from '../ArtifactFileView';

const version: ArtifactVersion = {
  artifactId: 'a1',
  versionId: 'v1',
  parentVersionId: null,
  title: 'memo.txt',
  mimeType: 'text/plain',
  byteSize: 4,
  sha256: 'hash',
  createdAt: '2026-10-09T00:00:00Z',
  producer: 'user',
  sourceIds: [],
};
beforeEach(() => {
  useAuthStore.setState({
    user: { id: 'u1', organization_id: 'o1' } as User,
    isAuthenticated: true,
  });
  useChatStore.setState({
    currentWorkspaceId: 'w1',
    currentThreadId: 't1',
    threads: {},
  });
  useArtifactPanelStore.getState().reset();
  useArtifactPanelStore
    .getState()
    .setScope(JSON.stringify(['u1', 'o1', 'w1', null, 't1']));
  useArtifactPanelStore.getState().openArtifact({
    kind: 'generated',
    artifactId: 'a1',
    versionId: 'v1',
    title: 'memo.txt',
  });
  vi.mocked(artifactService.capabilities).mockResolvedValue({
    editingEnabled: true,
    previewEnabled: false,
  });
  vi.mocked(artifactService.fetchVersionBlob).mockResolvedValue(
    new Blob(['seed'], { type: 'text/plain' })
  );
});
async function edit(): Promise<ReturnType<typeof render>> {
  const result = render(<ArtifactFileView version={version} />);
  await result.user.click(await screen.findByRole('button', { name: 'Edit' }));
  await screen.findByLabelText('Edit file contents');
  return result;
}
it('defaults editing off when the capability is disabled or fails', async () => {
  vi.mocked(artifactService.capabilities).mockRejectedValue(
    new Error('secret')
  );
  render(<ArtifactFileView version={version} />);
  await screen.findByText('seed');
  expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
});
it('publishes empty text against the selected immutable parent and advances after success', async () => {
  vi.mocked(artifactService.editVersion).mockResolvedValue({
    ...version,
    versionId: 'v2',
    parentVersionId: 'v1',
  });
  const { user } = await edit();
  fireEvent.change(screen.getByLabelText('Edit file contents'), {
    target: { value: '' },
  });
  await user.click(screen.getByRole('button', { name: 'Save new version' }));
  await waitFor(() =>
    expect(useArtifactPanelStore.getState().activeVersionId).toBe('v2')
  );
  expect(artifactService.editVersion).toHaveBeenCalledWith(
    'a1',
    expect.objectContaining({
      expectedParentVersionId: 'v1',
      publicationId: expect.any(String),
      text: '',
    })
  );
});
it('checks UTF-8 bytes rather than character count', async () => {
  await edit();
  fireEvent.change(screen.getByLabelText('Edit file contents'), {
    target: { value: 'é'.repeat(1024 * 1024 + 1) },
  });
  expect(
    screen.getByRole('button', { name: 'Save new version' })
  ).toBeDisabled();
  expect(screen.getByRole('alert')).toHaveTextContent('2 MB');
});
it('retries an uncertain save with the identical publication ID, parent and text', async () => {
  vi.mocked(artifactService.editVersion)
    .mockRejectedValueOnce(new Error('network secret'))
    .mockResolvedValueOnce({ ...version, versionId: 'v2' });
  const { user } = await edit();
  fireEvent.change(screen.getByLabelText('Edit file contents'), {
    target: { value: 'changed' },
  });
  await user.click(screen.getByRole('button', { name: 'Save new version' }));
  expect(await screen.findByRole('alert')).not.toHaveTextContent('secret');
  expect(screen.getByLabelText('Edit file contents')).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Retry save' }));
  await waitFor(() =>
    expect(artifactService.editVersion).toHaveBeenCalledTimes(2)
  );
  expect(vi.mocked(artifactService.editVersion).mock.calls[1]).toEqual(
    vi.mocked(artifactService.editVersion).mock.calls[0]
  );
});
it('preserves the edit buffer on conflict, fetches latest identity and never silently rebases', async () => {
  vi.mocked(artifactService.editVersion).mockRejectedValue(
    new APIErrorClass({
      message: 'internal conflict',
      status_code: 409,
      type: 'http_error',
    })
  );
  vi.mocked(artifactService.listVersions).mockResolvedValue([
    version,
    { ...version, versionId: 'v9' },
  ]);
  const { user } = await edit();
  fireEvent.change(screen.getByLabelText('Edit file contents'), {
    target: { value: 'my draft' },
  });
  await user.click(screen.getByRole('button', { name: 'Save new version' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('newer version');
  await screen.findByText(/Latest version: v9/);
  expect(screen.getByLabelText('Edit file contents')).toHaveValue('my draft');
  expect(useArtifactPanelStore.getState().activeVersionId).toBe('v1');
  expect(
    screen.getByRole('button', { name: 'Save new version' })
  ).toBeDisabled();
  expect(artifactService.editVersion).toHaveBeenCalledTimes(1);
});
it('a delayed successful save cannot steal another selected version or account', async () => {
  let finish!: (v: ArtifactVersion) => void;
  vi.mocked(artifactService.editVersion).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      })
  );
  const { user } = await edit();
  fireEvent.change(screen.getByLabelText('Edit file contents'), {
    target: { value: 'changed' },
  });
  await user.click(screen.getByRole('button', { name: 'Save new version' }));
  act(() => {
    useArtifactPanelStore.getState().setScope('account-b');
    useArtifactPanelStore.getState().openArtifact({
      kind: 'generated',
      artifactId: 'other',
      versionId: 'other',
      title: 'other',
    });
  });
  await act(async () => finish({ ...version, versionId: 'v2' }));
  expect(useArtifactPanelStore.getState().activeVersionId).toBe('other');
});
it('dirty tab/panel navigation waits for an explicit discard decision', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await edit();
  fireEvent.change(screen.getByLabelText('Edit file contents'), {
    target: { value: 'unsaved' },
  });
  act(() => useArtifactPanelStore.getState().closePanel());
  expect(confirm).toHaveBeenCalled();
  expect(useArtifactPanelStore.getState().isOpen).toBe(true);
  confirm.mockReturnValue(true);
  act(() => useArtifactPanelStore.getState().closePanel());
  expect(useArtifactPanelStore.getState().isOpen).toBe(false);
});

it('a late save cannot replace a different active version in the same scope', async () => {
  let finish!: (v: ArtifactVersion) => void;
  vi.mocked(artifactService.editVersion).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      })
  );
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  const { user } = await edit();
  fireEvent.change(screen.getByLabelText('Edit file contents'), {
    target: { value: 'changed' },
  });
  await user.click(screen.getByRole('button', { name: 'Save new version' }));
  act(() =>
    useArtifactPanelStore.getState().openArtifact({
      kind: 'generated',
      artifactId: 'a1',
      versionId: 'other',
      title: 'other',
    })
  );
  await act(async () => finish({ ...version, versionId: 'v2' }));
  expect(useArtifactPanelStore.getState().activeVersionId).toBe('other');
});
it('copy failure cannot unlock a conflicted save', async () => {
  vi.mocked(artifactService.editVersion).mockRejectedValue(
    new APIErrorClass({
      message: 'conflict',
      status_code: 409,
      type: 'http_error',
    })
  );
  vi.mocked(artifactService.listVersions).mockResolvedValue([
    version,
    { ...version, versionId: 'v9' },
  ]);
  const { user } = await edit();
  vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(
    new Error('denied')
  );
  fireEvent.change(screen.getByLabelText('Edit file contents'), {
    target: { value: 'changed' },
  });
  await user.click(screen.getByRole('button', { name: 'Save new version' }));
  await screen.findByText(/Latest version: v9/);
  await user.click(screen.getByRole('button', { name: 'Copy changes' }));
  await screen.findByText(/Copy failed/);
  expect(
    screen.getByRole('button', { name: 'Save new version' })
  ).toBeDisabled();
  expect(screen.getByLabelText('Edit file contents')).toHaveValue('changed');
});

it('retains the typed safe conflict identity even if latest metadata cannot load', async () => {
  const currentVersionId = '11111111-1111-4111-8111-111111111111';
  vi.mocked(artifactService.editVersion).mockRejectedValue(
    new APIErrorClass({
      message: 'Artifact publication conflict',
      status_code: 409,
      type: 'http_error',
      details: { current_version_id: currentVersionId },
    })
  );
  vi.mocked(artifactService.listVersions).mockRejectedValue(
    new Error('metadata unavailable')
  );
  const { user } = await edit();
  fireEvent.change(screen.getByLabelText('Edit file contents'), {
    target: { value: 'kept' },
  });
  await user.click(screen.getByRole('button', { name: 'Save new version' }));
  await screen.findByText(`Latest version: ${currentVersionId}`);
  expect(screen.getByLabelText('Edit file contents')).toHaveValue('kept');
  expect(
    screen.queryByRole('button', { name: 'Load latest version' })
  ).toBeNull();
});

it('an account A save resolving after account B opens preserves B cache, tabs and edit buffer', async () => {
  let finish!: (v: ArtifactVersion) => void;
  vi.mocked(artifactService.editVersion).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      })
  );
  vi.mocked(artifactService.fetchVersionBlob).mockImplementation(
    async (id) =>
      new Blob([id === 'b1' ? 'B seed' : 'seed'], { type: 'text/plain' })
  );
  const bVersion = { ...version, artifactId: 'b-artifact', versionId: 'b1' };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({
    children,
  }: {
    children: ReactNode;
  }): import('react').ReactElement => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  function Harness(): import('react').ReactElement | null {
    useArtifactPanelScope();
    const artifact = useArtifactPanelStore((s) => s.artifact);
    return artifact?.kind === 'generated' ? (
      <ArtifactFileView
        version={artifact.versionId === 'b1' ? bVersion : version}
      />
    ) : null;
  }
  const { user } = render(<Harness />, { wrapper });
  await user.click(await screen.findByRole('button', { name: 'Edit' }));
  await screen.findByLabelText('Edit file contents');
  fireEvent.change(screen.getByLabelText('Edit file contents'), {
    target: { value: 'A draft' },
  });
  await user.click(screen.getByRole('button', { name: 'Save new version' }));
  act(() =>
    useAuthStore.setState({ user: { id: 'u2', organization_id: 'o2' } as User })
  );
  act(() =>
    useArtifactPanelStore.getState().openArtifact({
      kind: 'generated',
      artifactId: 'b-artifact',
      versionId: 'b1',
      title: 'B file',
    })
  );
  await user.click(await screen.findByRole('button', { name: 'Edit' }));
  await screen.findByLabelText('Edit file contents');
  fireEvent.change(screen.getByLabelText('Edit file contents'), {
    target: { value: 'B draft' },
  });
  const bKey = [
    'artifact',
    'b-artifact',
    'versions',
    useArtifactPanelStore.getState().scope,
  ];
  client.setQueryData(bKey, [bVersion]);
  const bTabs = useArtifactPanelStore.getState().tabs;
  await act(async () => finish({ ...version, versionId: 'a2' }));
  expect(client.getQueryData(bKey)).toEqual([bVersion]);
  expect(useArtifactPanelStore.getState().tabs).toEqual(bTabs);
  expect(useArtifactPanelStore.getState().activeVersionId).toBe('b1');
  expect(screen.getByLabelText('Edit file contents')).toHaveValue('B draft');
});

function NavigationHarness(): import('react').ReactElement {
  useArtifactPanelScope();
  const open = useArtifactPanelStore((state) => state.isOpen);
  const activeThreadId = useChatStore((state) => state.currentThreadId);
  const commands = useSlashCommands({
    input: '',
    setInput: vi.fn(),
    handleSubmit: vi.fn(),
    retryLast: vi.fn(),
    conversations: [],
    activeThreadId,
    setCurrentThread: useChatStore.getState().setCurrentThread,
    chatInputRef: { current: null },
  });
  return (
    <>
      <button onClick={commands.startNewChat}>New chat</button>
      <Link href="/projects">Projects</Link>
      <ProjectPickerPopover
        threadId="t1"
        workspaceId="w1"
        onProjectBound={() => useChatStore.setState({ currentThreadId: 't2' })}
      >
        <button>Bind project</button>
      </ProjectPickerPopover>
      {open && <ArtifactFileView version={version} />}
    </>
  );
}
it('actual New chat action preserves dirty edits when declined and navigates only after discard', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  const { user } = render(<NavigationHarness />);
  await user.click(await screen.findByRole('button', { name: 'Edit' }));
  fireEvent.change(await screen.findByLabelText('Edit file contents'), {
    target: { value: 'keep my draft' },
  });
  await user.click(screen.getByRole('button', { name: 'New chat' }));
  expect(confirm).toHaveBeenCalledOnce();
  expect(useChatStore.getState().currentThreadId).toBe('t1');
  expect(screen.getByLabelText('Edit file contents')).toHaveValue(
    'keep my draft'
  );
  expect(routerPush).not.toHaveBeenCalled();
  confirm.mockReturnValue(true);
  await user.click(screen.getByRole('button', { name: 'New chat' }));
  expect(useChatStore.getState().currentThreadId).toBeNull();
  expect(screen.queryByLabelText('Edit file contents')).toBeNull();
  expect(routerPush).toHaveBeenCalledWith('/chat?new=1');
});
it('same-actor client links and back navigation cannot discard dirty edits after Stay', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  const restore = vi.spyOn(window.history, 'pushState');
  const { user } = render(<NavigationHarness />);
  await user.click(await screen.findByRole('button', { name: 'Edit' }));
  fireEvent.change(await screen.findByLabelText('Edit file contents'), {
    target: { value: 'keep my draft' },
  });
  const link = screen.getByRole('link', { name: 'Projects' });
  const allowed = link.dispatchEvent(
    new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
  );
  expect(allowed).toBe(false);
  const before = confirm.mock.calls.length;
  act(() => window.dispatchEvent(new PopStateEvent('popstate', { state: {} })));
  expect(confirm.mock.calls.length).toBe(before + 1);
  expect(restore).toHaveBeenCalled();
  expect(screen.getByLabelText('Edit file contents')).toHaveValue(
    'keep my draft'
  );
});

it('project picker decides before the write and retires the approved buffer while HTTP is deferred', async () => {
  let finish!: (value: null) => void;
  const link = vi.fn(
    () =>
      new Promise<null>((resolve) => {
        finish = resolve;
      })
  );
  useProjectChatStore.setState({ linkThreadToProject: link });
  useProjectStore.setState({
    projects: [{ id: 'p2', name: 'Other project' }] as ReturnType<
      typeof useProjectStore.getState
    >['projects'],
    loading: false,
    fetchProjects: vi.fn().mockResolvedValue(undefined),
  });
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  const { user } = render(<NavigationHarness />);
  await user.click(await screen.findByRole('button', { name: 'Edit' }));
  fireEvent.change(await screen.findByLabelText('Edit file contents'), {
    target: { value: 'draft' },
  });
  await user.click(screen.getByRole('button', { name: 'Bind project' }));
  await user.click(
    await screen.findByRole('button', { name: 'Other project' })
  );
  expect(link).not.toHaveBeenCalled();
  expect(screen.getByLabelText('Edit file contents')).toHaveValue('draft');
  confirm.mockReturnValue(true);
  await user.click(screen.getByRole('button', { name: 'Other project' }));
  expect(link).toHaveBeenCalledOnce();
  expect(screen.queryByLabelText('Edit file contents')).toBeNull();
  expect(
    screen.getByRole('button', { name: 'Edit', exact: true })
  ).toBeDisabled();
  await act(async () => finish(null));
  expect(screen.getByRole('alert')).toHaveTextContent('Failed to link');
  expect(confirm).toHaveBeenCalledTimes(2);
  expect(await screen.findByRole('button', { name: 'Edit' })).toBeVisible();
});

it('a deferred picker result for A never steers B or clears its new draft', async () => {
  type LinkResult = Awaited<
    ReturnType<
      ReturnType<typeof useProjectChatStore.getState>['linkThreadToProject']
    >
  >;
  let finish!: (value: LinkResult) => void;
  const link = vi.fn(
    () =>
      new Promise<LinkResult>((resolve) => {
        finish = resolve;
      })
  );
  useProjectChatStore.setState({ linkThreadToProject: link });
  useProjectStore.setState({
    projects: [{ id: 'p2', name: 'Other project' }] as ReturnType<
      typeof useProjectStore.getState
    >['projects'],
    loading: false,
    fetchProjects: vi.fn().mockResolvedValue(undefined),
  });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  const { user } = render(<NavigationHarness />);
  await user.click(
    await screen.findByRole('button', { name: 'Edit', exact: true })
  );
  fireEvent.change(await screen.findByLabelText('Edit file contents'), {
    target: { value: 'A draft' },
  });
  await user.click(screen.getByRole('button', { name: 'Bind project' }));
  await user.click(
    await screen.findByRole('button', { name: 'Other project' })
  );
  act(() => useChatStore.setState({ currentThreadId: 'B' }));
  act(() =>
    useArtifactPanelStore.getState().openArtifact({
      kind: 'generated',
      artifactId: 'a1',
      versionId: 'v1',
      title: 'memo.txt',
    })
  );
  await user.click(
    await screen.findByRole('button', { name: 'Edit', exact: true })
  );
  fireEvent.change(await screen.findByLabelText('Edit file contents'), {
    target: { value: 'B draft' },
  });
  await act(async () => finish({} as NonNullable<LinkResult>));
  expect(useChatStore.getState().currentThreadId).toBe('B');
  expect(screen.getByLabelText('Edit file contents')).toHaveValue('B draft');
});

it('an approved deferred picker write retires even a clean open editor', async () => {
  let finish!: (value: null) => void;
  const link = vi.fn(
    () =>
      new Promise<null>((resolve) => {
        finish = resolve;
      })
  );
  useProjectChatStore.setState({ linkThreadToProject: link });
  useProjectStore.setState({
    projects: [{ id: 'p2', name: 'Other project' }] as ReturnType<
      typeof useProjectStore.getState
    >['projects'],
    loading: false,
    fetchProjects: vi.fn().mockResolvedValue(undefined),
  });
  const confirm = vi.spyOn(window, 'confirm');
  const { user } = render(<NavigationHarness />);
  await user.click(
    await screen.findByRole('button', { name: 'Edit', exact: true })
  );
  expect(await screen.findByLabelText('Edit file contents')).toHaveValue(
    'seed'
  );
  await user.click(screen.getByRole('button', { name: 'Bind project' }));
  await user.click(
    await screen.findByRole('button', { name: 'Other project' })
  );
  expect(confirm).not.toHaveBeenCalled();
  expect(screen.queryByLabelText('Edit file contents')).toBeNull();
  expect(
    screen.getByRole('button', { name: 'Edit', exact: true })
  ).toBeDisabled();
  await act(async () => finish(null));
  expect(
    screen.getByRole('button', { name: 'Edit', exact: true })
  ).toBeEnabled();
});

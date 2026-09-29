import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
} from '@testing-library/react';
import type { RenderHookResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactElement, ReactNode } from 'react';

const listDevices = vi.fn();
const listWorkspaces = vi.fn();
const readRequest = vi.fn();
const decideRequest = vi.fn();
const stopRun = vi.fn();

vi.mock('@/services/harnessService', () => ({
  harnessService: {
    listDevices: (...args: unknown[]) => listDevices(...args),
    listWorkspaces: (...args: unknown[]) => listWorkspaces(...args),
    readRequest: (...args: unknown[]) => readRequest(...args),
    decideRequest: (...args: unknown[]) => decideRequest(...args),
  },
}));

vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    cancelActiveRun: (...args: unknown[]) => stopRun(...args),
  },
}));

vi.mock('@/stores/authStore', () => ({
  useAuthStore: (selector: (state: { user: { id: string } }) => unknown) =>
    selector({ user: { id: 'user-a' } }),
}));

import { useHarnessConnection } from '@/hooks/chat/useHarnessConnection';
import type { HarnessConnectionController } from '@/hooks/chat/useHarnessConnection';
import { HarnessSelector } from '@/components/chat/HarnessSelector';

type ConnectedHarness = RenderHookResult<
  HarnessConnectionController,
  unknown
> & {
  client: QueryClient;
  status: () => string;
  canSend: () => boolean;
  stop: () => Promise<void>;
  receive: (event: { type: string; runId?: string }) => void;
};

function renderConnectedHarness(threadId = 'thread-a'): ConnectedHarness {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }): ReactElement => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(() => useHarnessConnection(threadId), { wrapper });
  return {
    ...hook,
    client,
    status: () => hook.result.current.statusLabel,
    canSend: () => hook.result.current.canSend,
    stop: () => hook.result.current.stop(),
    receive: (event: { type: string; runId?: string }) =>
      hook.result.current.receive(event),
  };
}

describe('useHarnessConnection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    listDevices.mockResolvedValue([]);
    listWorkspaces.mockResolvedValue([]);
  });

  it('keeps stopping after interruption receipt until terminal evidence', async () => {
    listDevices.mockResolvedValue([{ id: 'device-a', label: 'My laptop' }]);
    listWorkspaces.mockResolvedValue([
      { workspace_id: 'workspace-a', project_id: 'project-a', label: 'Repo' },
    ]);
    stopRun.mockResolvedValue(undefined);
    const view = renderConnectedHarness();
    act(() => view.result.current.selectProvider('codex'));
    await waitFor(() => expect(view.result.current.devices).toHaveLength(1));
    act(() => {
      view.result.current.selectProvider('codex');
      view.result.current.selectDevice('device-a');
      view.result.current.selectWorkspace('workspace-a');
      view.result.current.receive({ type: 'accepted', runId: 'run-a' });
    });
    await act(async () => view.stop());
    act(() => view.receive({ type: 'interrupt_ack' }));
    expect(view.status()).toBe('Stopping');
    expect(view.canSend()).toBe(false);
    act(() => view.receive({ type: 'error' }));
    expect(view.result.current.connectionState).toBe('idle');
    expect(view.result.current.runId).toBeNull();
  });

  it('isolates selections between threads and restores a thread selection', async () => {
    listDevices.mockResolvedValue([{ id: 'device-a', label: 'My laptop' }]);
    listWorkspaces.mockResolvedValue([
      { workspace_id: 'workspace-a', project_id: 'project-a', label: 'Repo' },
    ]);
    const first = renderConnectedHarness('thread-a');
    act(() => first.result.current.selectProvider('codex'));
    await waitFor(() => expect(first.result.current.devices).toHaveLength(1));
    act(() => {
      first.result.current.selectProvider('codex');
      first.result.current.selectDevice('device-a');
      first.result.current.selectWorkspace('workspace-a');
    });
    first.unmount();
    const other = renderConnectedHarness('thread-b');
    expect(other.result.current.executionProvider).toBe('nous');
    other.unmount();
    const restored = renderConnectedHarness('thread-a');
    expect(restored.result.current.executionProvider).toBe('codex');
    await waitFor(() =>
      expect(restored.result.current.workspaceId).toBe('workspace-a')
    );
  });

  it('adopts a draft selection and routes late callbacks to the created thread', async () => {
    listDevices.mockResolvedValue([{ id: 'device-a', label: 'My laptop' }]);
    listWorkspaces.mockResolvedValue([
      { workspace_id: 'workspace-a', project_id: 'project-a', label: 'Repo' },
    ]);
    readRequest.mockResolvedValue({
      id: 'request-a',
      runId: 'run-a',
      method: 'item/commandExecution/requestApproval',
      target: { command: 'pnpm test' },
      targetHash: 'a'.repeat(64),
      expiresAt: '2026-09-29T00:00:00Z',
      consumed: false,
      expired: false,
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }): ReactElement => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const view = renderHook(
      ({ threadId }: { threadId: string | null }) =>
        useHarnessConnection(threadId),
      { initialProps: { threadId: null }, wrapper }
    );
    act(() => {
      view.result.current.selectProvider('codex');
      view.result.current.selectDevice('device-a');
      view.result.current.selectWorkspace('workspace-a');
    });
    const draftController = view.result.current;

    act(() => draftController.adoptDraftSelection('thread-created'));
    view.rerender({ threadId: 'thread-created' });
    await waitFor(() => {
      expect(view.result.current.executionProvider).toBe('codex');
      expect(view.result.current.workspaceId).toBe('workspace-a');
    });
    act(() =>
      draftController.receive(
        { type: 'accepted', runId: 'run-a' },
        'thread-created'
      )
    );
    await act(async () => {
      await draftController.loadApproval('request-a', 'thread-created');
    });
    expect(view.result.current.runId).toBe('run-a');
    expect(view.result.current.pendingRequests.map((item) => item.id)).toEqual([
      'request-a',
    ]);
  });

  it('does not copy the draft selection into an existing thread the user opens', async () => {
    listDevices.mockResolvedValue([{ id: 'device-a', label: 'My laptop' }]);
    listWorkspaces.mockResolvedValue([
      { workspace_id: 'workspace-a', project_id: 'project-a', label: 'Repo' },
    ]);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }): ReactElement => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const view = renderHook(
      ({ threadId }: { threadId: string | null }) =>
        useHarnessConnection(threadId),
      { initialProps: { threadId: null }, wrapper }
    );
    act(() => {
      view.result.current.selectProvider('codex');
      view.result.current.selectDevice('device-a');
      view.result.current.selectWorkspace('workspace-a');
    });

    // Sidebar click on a never-visited thread: null -> id with no create event.
    view.rerender({ threadId: 'thread-existing' });

    await waitFor(() => {
      expect(view.result.current.executionProvider).toBe('nous');
    });
    expect(view.result.current.deviceId).toBeNull();
    expect(view.result.current.workspaceId).toBeNull();
  });

  // Mutation guard for useHarnessConnection.ts:258-267: restoring a render-captured
  // pendingRequests snapshot drops one overlapping arrival. Verify with
  // `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useHarnessConnection.test.tsx -t "merges concurrent native requests"`.
  it('merges concurrent native requests that resolve from the same render', async () => {
    readRequest.mockImplementation(async (id: string) => ({
      id,
      runId: 'run-a',
      method: 'item/commandExecution/requestApproval',
      target: { command: `command for ${id}` },
      targetHash: 'b'.repeat(64),
      expiresAt: '2026-09-29T00:00:00Z',
      consumed: false,
      expired: false,
    }));
    const view = renderConnectedHarness('thread-a');
    const first = view.result.current.loadApproval('request-one');
    const second = view.result.current.loadApproval('request-two');
    await act(async () => {
      await Promise.all([first, second]);
    });

    expect(view.result.current.pendingRequests.map((item) => item.id).sort()).toEqual([
      'request-one',
      'request-two',
    ]);
  });

  it('fetches the exact native request before submitting its target-bound decision', async () => {
    listDevices.mockResolvedValue([]);
    readRequest.mockResolvedValue({
      id: 'request-a',
      targetHash: 'a'.repeat(64),
      target: { command: 'pnpm test' },
    });
    decideRequest.mockResolvedValue(undefined);
    const view = renderConnectedHarness();
    await act(async () => {
      await view.result.current.decideRequest('request-a', {
        kind: 'decision',
        allow: true,
      });
    });
    expect(readRequest).toHaveBeenCalledWith('request-a');
    expect(decideRequest).toHaveBeenCalledWith('request-a', {
      kind: 'decision',
      allow: true,
      targetHash: 'a'.repeat(64),
    });
  });

  it('shows the persisted command and permission target before Allow once', async () => {
    readRequest.mockResolvedValue({
      id: 'request-a',
      runId: 'run-a',
      method: 'item/commandExecution/requestApproval',
      target: {
        command: 'pnpm test --filter chat',
        cwd: '/workspace/nous',
        reason: 'Run the focused test suite',
        kind: 'shell',
        itemId: 'item-7',
      },
      targetHash: 'a'.repeat(64),
      expiresAt: '2026-09-29T00:00:00Z',
      consumed: false,
      expired: false,
    });
    const view = renderConnectedHarness();
    await act(async () => {
      await view.result.current.loadApproval('request-a');
    });
    render(
      <QueryClientProvider client={view.client}>
        <HarnessSelector controller={view.result.current} />
      </QueryClientProvider>
    );

    expect(
      screen.getByText('Codex requests command execution')
    ).toBeInTheDocument();
    expect(screen.getByText('pnpm test --filter chat')).toBeInTheDocument();
    expect(screen.getByText('/workspace/nous')).toBeInTheDocument();
    expect(screen.getByText('Run the focused test suite')).toBeInTheDocument();
    expect(screen.getByText('shell')).toBeInTheDocument();
    expect(screen.getByText('Item reference')).toBeInTheDocument();
    expect(screen.getByText('item-7')).toBeInTheDocument();
    expect(
      screen.queryByText('Codex needs permission to continue')
    ).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeEnabled();
  });

  it('does not offer decision cancellation for answer-only native requests', async () => {
    readRequest.mockResolvedValue({
      id: 'request-input',
      runId: 'run-a',
      method: 'item/tool/requestUserInput',
      target: {
        questions: [
          { id: 'goal', header: 'Goal', question: 'What are you trying to do?' },
        ],
      },
      targetHash: 'd'.repeat(64),
      expiresAt: '2026-09-29T00:00:00Z',
      consumed: false,
      expired: false,
    });
    const view = renderConnectedHarness();
    await act(async () => {
      await view.result.current.loadApproval('request-input');
    });
    render(
      <QueryClientProvider client={view.client}>
        <HarnessSelector controller={view.result.current} />
      </QueryClientProvider>
    );

    expect(screen.getByRole('button', { name: 'Send answers' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Cancel request' })).toBeNull();
  });

  it('fails closed when a file-change request has only a reason and item reference', async () => {
    readRequest.mockResolvedValue({
      id: 'request-file',
      runId: 'run-a',
      method: 'item/fileChange/requestApproval',
      target: { itemId: 'item-42', reason: 'Please update these files' },
      targetHash: 'b'.repeat(64),
      expiresAt: '2026-09-29T00:00:00Z',
      consumed: false,
      expired: false,
    });
    const view = renderConnectedHarness();
    await act(async () => {
      await view.result.current.loadApproval('request-file');
    });
    render(
      <QueryClientProvider client={view.client}>
        <HarnessSelector controller={view.result.current} />
      </QueryClientProvider>
    );

    expect(screen.getByText('Item reference')).toBeInTheDocument();
    expect(screen.getByText('item-42')).toBeInTheDocument();
    expect(
      screen.getByText(
        'File paths or a file-specific change summary are unavailable, so this request cannot be approved here.'
      )
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeDisabled();
  });

  it('shows exact file targets and enables approval when paths and summary are present', async () => {
    readRequest.mockResolvedValue({
      id: 'request-file-targeted',
      runId: 'run-a',
      method: 'item/fileChange/requestApproval',
      target: {
        itemId: 'item-43',
        reason: 'Apply the requested refactor',
        paths: ['/workspace/src/a.ts', '/workspace/src/b.ts'],
        changeSummary: 'Rename the shared parser and update both imports.',
      },
      targetHash: 'c'.repeat(64),
      expiresAt: '2026-09-29T00:00:00Z',
      consumed: false,
      expired: false,
    });
    const view = renderConnectedHarness();
    await act(async () => {
      await view.result.current.loadApproval('request-file-targeted');
    });
    render(
      <QueryClientProvider client={view.client}>
        <HarnessSelector controller={view.result.current} />
      </QueryClientProvider>
    );

    expect(
      screen.getByText(
        (content) =>
          content.includes('/workspace/src/a.ts') &&
          content.includes('/workspace/src/b.ts')
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText('Rename the shared parser and update both imports.')
    ).toBeInTheDocument();
    expect(
      screen.getByText('Apply the requested refactor')
    ).toBeInTheDocument();
    expect(screen.getByText('item-43')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeEnabled();
  });

  it('keeps provider, computer, and workspace controls keyboard reachable', async () => {
    listDevices.mockResolvedValue([{ id: 'device-a', label: 'My laptop' }]);
    listWorkspaces.mockResolvedValue([
      { workspace_id: 'workspace-a', project_id: 'project-a', label: 'Repo' },
    ]);
    const view = renderConnectedHarness();
    act(() => view.result.current.selectProvider('codex'));
    await waitFor(() => expect(view.result.current.devices).toHaveLength(1));
    act(() => {
      view.result.current.selectProvider('codex');
      view.result.current.selectDevice('device-a');
      view.result.current.selectWorkspace('workspace-a');
    });
    await waitFor(() => expect(view.result.current.workspaces).toHaveLength(1));
    render(
      <QueryClientProvider client={view.client}>
        <HarnessSelector controller={view.result.current} />
      </QueryClientProvider>
    );

    const user = userEvent.setup();
    await user.tab();
    expect(
      screen.getByRole('combobox', { name: 'Execution provider' })
    ).toHaveFocus();
    await user.tab();
    expect(
      screen.getByRole('combobox', { name: 'Paired computer' })
    ).toHaveFocus();
    await user.tab();
    expect(
      screen.getByRole('combobox', { name: 'Project workspace' })
    ).toHaveFocus();
  });

  it('locks provider and target selection while a run owns the thread', () => {
    listDevices.mockResolvedValue([{ id: 'device-a', label: 'My laptop' }]);
    listWorkspaces.mockResolvedValue([
      { workspace_id: 'workspace-a', project_id: 'project-a', label: 'Repo' },
    ]);
    const view = renderConnectedHarness();
    act(() => {
      view.result.current.selectProvider('codex');
      view.result.current.selectDevice('device-a');
      view.result.current.receive({ type: 'accepted', runId: 'run-a' });
    });
    render(
      <QueryClientProvider client={view.client}>
        <HarnessSelector
          controller={view.result.current}
          disabled={Boolean(view.result.current.runId)}
        />
      </QueryClientProvider>
    );

    expect(
      screen.getByRole('combobox', { name: 'Execution provider' })
    ).toBeDisabled();
    expect(
      screen.getByRole('combobox', { name: 'Paired computer' })
    ).toBeDisabled();
    expect(
      screen.getByRole('combobox', { name: 'Project workspace' })
    ).toBeDisabled();
  });
});

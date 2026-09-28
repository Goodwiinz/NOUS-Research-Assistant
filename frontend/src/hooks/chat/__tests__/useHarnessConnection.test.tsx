import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

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
import { HarnessSelector } from '@/components/chat/HarnessSelector';

function renderConnectedHarness(threadId = 'thread-a') {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: React.ReactNode }) => (
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

  it('keeps provider, computer, and workspace controls keyboard reachable', async () => {
    listDevices.mockResolvedValue([{ id: 'device-a', label: 'My laptop' }]);
    listWorkspaces.mockResolvedValue([
      { workspace_id: 'workspace-a', project_id: 'project-a', label: 'Repo' },
    ]);
    const view = renderConnectedHarness();
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

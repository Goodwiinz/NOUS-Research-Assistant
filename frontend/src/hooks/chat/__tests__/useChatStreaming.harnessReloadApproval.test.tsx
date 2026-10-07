// After a full reload a pending Codex command-approval card must be restored
// AND the run must finish normally once the user approves. The cold-load probe
// only identifies the durable run (the replay opens with its accepted frame);
// it then hands the stream to the resume effect, so runStreamTurn owns the
// approval card, the tokens and terminal reconciliation. A probe that kept the
// stream would swallow the done frame and leave the transcript stale.
// Mutation proof: removing useChatStreaming.ts's setResumeNonce call makes the
// first case fail (one resumeStream call instead of two); restoring it passes.
// Command: pnpm --dir frontend exec vitest run
// src/hooks/chat/__tests__/useChatStreaming.harnessReloadApproval.test.tsx
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import type { UseChatStreamingParams } from '@/hooks/chat/useChatStreaming';
import { useChatStore } from '@/store/chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';

function wrapper({ children }: { children: ReactNode }): ReactElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const resumeStreamMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: vi.fn(),
    streamConfirm: vi.fn(),
    resumeStream: (...args: unknown[]) => resumeStreamMock(...args),
    cancelPendingConfirmation: vi.fn(),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    listMessages: vi.fn().mockResolvedValue({ messages: [], has_more: false }),
  },
}));

const loadApproval = vi.fn();
const harnessConnection = {
  executionProvider: 'codex',
  deviceId: 'device-1',
  workspaceId: 'workspace-1',
  devices: [],
  workspaces: [],
  pendingRequests: [],
  connectionState: 'idle',
  statusLabel: 'Codex ready',
  disabledReason: null,
  canSend: true,
  runId: null,
  loadApproval: (...args: unknown[]) => loadApproval(...args),
  receive: vi.fn(),
  markConnectionLost: vi.fn(),
};
vi.mock('@/hooks/chat/useHarnessConnection', () => ({
  useHarnessConnection: () => harnessConnection,
}));

function makeParams(): UseChatStreamingParams {
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: [] as ChatPageMessage[],
    setMessages: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    dbConversation: null,
    enableRAG: false,
  };
}

describe('useChatStreaming Codex approval probe (cold thread load)', () => {
  beforeEach(() => {
    resumeStreamMock.mockReset();
    loadApproval.mockReset();
    loadApproval.mockResolvedValue({ id: 'req-1' });
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.setState({
      currentThreadId: 'thread-A',
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  it('hands a replayed Codex run to the resume path, which restores the card and reconciles', async () => {
    const refreshSpy = vi.fn().mockResolvedValue(true);
    useChatStore.setState({ refreshMessages: refreshSpy });
    type Callbacks = {
      onRunId?: (runId: string) => void;
      onStatus?: (phase: string, detail?: string) => void;
      onApprovalRequired?: (requestId: string) => void;
      onDone?: (payload?: { assistant_message_id?: string | null }) => void;
    };
    const signals: AbortSignal[] = [];
    resumeStreamMock.mockImplementation(
      async (
        _threadId: string,
        _after: number,
        cb: Callbacks,
        signal: AbortSignal
      ) => {
        signals.push(signal);
        if (resumeStreamMock.mock.calls.length === 1) {
          // Probe: accepted frame (with run_id), then the parked approval.
          cb.onRunId?.('run-1');
          cb.onStatus?.('accepted');
          cb.onApprovalRequired?.('req-1');
          return { status: 'resumed' };
        }
        // Resume path: the same replay, then the approved run completes.
        cb.onApprovalRequired?.('req-1');
        cb.onDone?.({ assistant_message_id: 'assistant-1' });
        return { status: 'resumed' };
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const params = makeParams();
    renderHook(() => useChatStreaming(params), { wrapper });

    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(2));
    // The probe let go of the stream and seeded the run for the resume path.
    expect(signals[0].aborted).toBe(true);
    expect(useAgentActivityStore.getState().runs['thread-A']).toMatchObject({
      runId: 'run-1',
    });
    // The resume call carries the durable run id (Codex reattaches by it).
    expect(resumeStreamMock.mock.calls[1][4]).toBe('run-1');

    await waitFor(() =>
      expect(loadApproval).toHaveBeenCalledWith('req-1', 'thread-A')
    );
    expect(loadApproval).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(refreshSpy).toHaveBeenCalledTimes(1));
    expect(refreshSpy.mock.calls[0][0]).toBe('thread-A');
    expect(useAgentActivityStore.getState().runs['thread-A']?.state).toBe(
      'done'
    );
  });

  it('does not seed a run when the replay is empty (204)', async () => {
    resumeStreamMock.mockResolvedValue({ status: 'idle' });
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    renderHook(() => useChatStreaming(makeParams()), { wrapper });

    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(1));
    expect(useAgentActivityStore.getState().runs['thread-A']).toBeUndefined();
  });
});

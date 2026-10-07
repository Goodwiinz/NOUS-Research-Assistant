/**
 * Resume-on-mount: a stale `running` run in the agent activity store for the
 * displayed thread must trigger agentChatService.resumeStream (from the last
 * seen seq), and a 204/idle result must clear the stale run record.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { makeChatPageMessage } from '@/test/chatMessageFactory';

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

// ---- Mocks ----
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
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    createMessage: vi.fn().mockResolvedValue({ id: 'db-msg-1' }),
    listMessages: vi.fn(),
  },
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

import { useChatStreaming } from '@/hooks/chat/useChatStreaming';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import { useChatStore } from '@/store/chat-store';
import { workspaceService } from '@/services/workspaceService';

function makeParams() {
  const activeConversationIdRef = { current: 'thread-A' as string | null };
  return {
    messages: [
      makeChatPageMessage({
        role: 'user',
        content: 'earlier question',
        timestamp: 1,
      }),
    ] as ChatPageMessage[],
    displayedMessages: [
      makeChatPageMessage({
        role: 'user',
        content: 'earlier question',
        timestamp: 1,
      }),
    ] as ChatPageMessage[],
    setMessages: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    activeConversationId: 'thread-A',
    setActiveConversationId: vi.fn(),
    activeConversationIdRef,
    dbConversation: null,
    isAuthenticated: true,
    setCurrentThread: vi.fn(),
    addMessageToStore: vi.fn(),
    enableRAG: false,
  };
}

describe('useChatStreaming stream resume on mount', () => {
  beforeEach(() => {
    resumeStreamMock.mockReset();
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.getState().reset();
    useChatStore.setState({
      currentThreadId: 'thread-A',
      isStreaming: false,
    });
    vi.mocked(workspaceService.listMessages).mockResolvedValue({
      messages: [],
      has_more: false,
    } as never);
  });

  it('resumes a stale running run from its streamSeq when not streaming', async () => {
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    useAgentActivityStore.getState().setStreamSeq('thread-A', 7);
    resumeStreamMock.mockResolvedValue({ status: 'resumed' });

    await act(async () => {
      renderHook(() => useChatStreaming(makeParams()), { wrapper });
    });

    expect(resumeStreamMock).toHaveBeenCalledTimes(1);
    expect(resumeStreamMock.mock.calls[0][0]).toBe('thread-A');
    expect(resumeStreamMock.mock.calls[0][1]).toBe(7);
  });

  it('clears the stale run on idle and does not resume twice', async () => {
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    resumeStreamMock.mockResolvedValue({ status: 'idle' });

    let rerender: () => void = () => {};
    await act(async () => {
      ({ rerender } = renderHook(() => useChatStreaming(makeParams()), {
        wrapper,
      }));
    });
    await act(async () => {
      rerender();
    });

    expect(resumeStreamMock).toHaveBeenCalledTimes(1);
    // defaults streamSeq to 0 when never set
    expect(resumeStreamMock.mock.calls[0][1]).toBe(0);
    expect(useAgentActivityStore.getState().runs['thread-A'].state).toBe(
      'done'
    );
  });

  it('keeps a failed resume retryable on the next thread activation', async () => {
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    useAgentActivityStore.getState().startRun('thread-B', 'Agent', 'settled');
    useAgentActivityStore.getState().finishRun('thread-B', 'stopped');
    resumeStreamMock.mockResolvedValue({
      status: 'failed',
      error: 'checkpoint unavailable',
    });

    await act(async () => {
      renderHook(() => useChatStreaming(makeParams()), { wrapper });
    });
    // Each activation retries with backoff before giving up (round-3 H7).
    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(3), {
      timeout: 10_000,
    });
    expect(useAgentActivityStore.getState().runs['thread-A'].state).toBe(
      'running'
    );

    act(() => useChatStore.setState({ currentThreadId: 'thread-B' }));
    act(() => useChatStore.setState({ currentThreadId: 'thread-A' }));

    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(4), {
      timeout: 10_000,
    });
    expect(
      new Set(resumeStreamMock.mock.calls.map(([threadId]) => threadId))
    ).toEqual(new Set(['thread-A']));
  }, 20_000);

  it('does not resume a stream when there is no run for the thread', async () => {
    // With no run record the hook still asks ONCE whether the thread is parked
    // on a HITL interrupt (a cold reload has no in-memory run either) — but
    // that probe is confirmation-only, from seq 0. It must not turn into a
    // stream resume: no token/done handling, nothing committed.
    resumeStreamMock.mockResolvedValue({ status: 'idle' });
    await act(async () => {
      renderHook(() => useChatStreaming(makeParams()), { wrapper });
    });

    expect(resumeStreamMock).toHaveBeenCalledTimes(1);
    const [threadId, afterSeq, callbacks] = resumeStreamMock.mock.calls[0];
    expect(threadId).toBe('thread-A');
    expect(afterSeq).toBe(0);
    // Confirmation/run discovery plus live-run detectors: any token/tool/status/plan
    // frame means a live run owns the thread, and the probe aborts rather than
    // consuming that run's frames (round-3 L7).
    expect(Object.keys(callbacks as object)).toEqual([
      'onConfirmation',
      'onRunId',
      'onToken',
      'onToolStart',
      'onStatus',
      'onPlan',
    ]);
  });

  it('reconciles the owning thread after a resumed stream completes', async () => {
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    resumeStreamMock.mockImplementation(
      (
        _threadId: string,
        _seq: number,
        callbacks: {
          onToken: (content: string) => void;
          onDone: (payload?: unknown) => void;
        }
      ) => {
        callbacks.onToken('resumed answer');
        callbacks.onDone({
          assistant_message_id: 'resume-assistant-1',
          client_message_id: 'resume-runtime-1',
        });
        return Promise.resolve({ status: 'resumed' });
      }
    );
    const actualRefresh = useChatStore.getState().refreshMessages;
    const refreshSpy = vi.fn(actualRefresh);
    useChatStore.setState({ refreshMessages: refreshSpy });

    await act(async () => {
      renderHook(() => useChatStreaming(makeParams()), { wrapper });
    });

    expect(refreshSpy).toHaveBeenCalledWith(
      'thread-A',
      expect.objectContaining({
        persistedId: 'resume-assistant-1',
        runtimeId: 'resume-runtime-1',
        diagnostic: expect.objectContaining({ terminalReason: 'done' }),
      })
    );
    useChatStore.setState({ refreshMessages: actualRefresh });
  });
});

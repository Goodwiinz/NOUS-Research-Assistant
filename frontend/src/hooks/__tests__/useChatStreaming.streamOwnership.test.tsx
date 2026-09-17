/**
 * Round-3 audit regressions around stream ownership and HITL bookkeeping:
 * H2 (a paused turn's teardown clobbering the live confirm stream), H1
 * (clearing a settled confirmation off the wrong thread) and H8 (a thrown
 * transport error leaving the activity run 'running' forever).
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { useChatStore } from '@/store/chat-store';

function wrapper({ children }: { children: ReactNode }): JSX.Element {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const streamMessageMock = vi.fn();
const streamConfirmMock = vi.fn();
const cancelActiveRunMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: (...args: unknown[]) => streamMessageMock(...args),
    streamConfirm: (...args: unknown[]) => streamConfirmMock(...args),
    resumeStream: vi.fn().mockResolvedValue({ status: 'idle' }),
    cancelPendingConfirmation: vi.fn().mockResolvedValue(undefined),
    cancelActiveRun: (...args: unknown[]) => cancelActiveRunMock(...args),
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
import { workspaceService } from '@/services/workspaceService';

type StreamCallbacks = {
  onToken: (t: string) => void;
  onReasoningDelta?: (content: string) => void;
  onRunId?: (runId: string) => void;
  onConfirmation: (
    threadId: string,
    confirmation: Record<string, unknown>
  ) => void;
  onDone: (p?: unknown) => void;
};

function makeParams(): Record<string, unknown> {
  useChatStore.setState({ currentThreadId: 'thread-A' });
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: [] as ChatPageMessage[],
    setMessages: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    activeConversationId: 'thread-A',
    setActiveConversationId: vi.fn(),
    activeConversationIdRef: { current: 'thread-A' as string | null },
    dbConversation: null,
    isAuthenticated: true,
    setCurrentThread: vi.fn(),
    addMessageToStore: vi.fn(),
    enableRAG: false,
  };
}

describe('useChatStreaming stream ownership', () => {
  beforeEach(() => {
    streamMessageMock.mockReset();
    streamConfirmMock.mockReset();
    cancelActiveRunMock.mockReset();
    useChatStore.getState().reset();
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    vi.mocked(workspaceService.listMessages).mockResolvedValue({
      messages: [],
      has_more: false,
    } as never);
  });

  it('keeps the confirm stream alive when the paused turn finishes reconciling (H2)', async () => {
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks) => {
        cb.onConfirmation('agent-thread-1', { tool: 'ingest_arxiv_papers' });
        cb.onDone({});
        return Promise.resolve();
      }
    );

    // Hold the paused turn inside its post-pause reconcile so the confirm
    // stream can start underneath it — the real-world window in which the
    // approval card is rendered and clickable.
    let releaseReconcile: (() => void) | undefined;
    const gatedReconcile = new Promise<boolean>((resolve) => {
      releaseReconcile = () => resolve(true);
    });
    const realRefresh = useChatStore.getState().refreshMessages;
    useChatStore.setState({
      refreshMessages: vi.fn().mockReturnValue(gatedReconcile),
    });

    let duringConfirm: {
      isStreaming: boolean;
      streamingThreadId: string | null;
    } | null = null;
    streamConfirmMock.mockImplementation(
      async (_req: unknown, cb: StreamCallbacks) => {
        // Let the paused turn's teardown run while the confirm stream is live.
        releaseReconcile?.();
        await Promise.resolve();
        await Promise.resolve();
        const state = useChatStore.getState();
        duringConfirm = {
          isStreaming: state.isStreaming,
          streamingThreadId: state.streamingThreadId,
        };
        cb.onToken('confirmed answer');
        cb.onDone({});
      }
    );

    const params = makeParams();
    const { result } = renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );

    let submitPromise: Promise<unknown> | undefined;
    await act(async () => {
      submitPromise = result.current.handleSubmit('ingest these');
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(result.current.pendingConfirmation).not.toBeNull()
    );

    await act(async () => {
      await result.current.handleConfirmation(true);
      await submitPromise;
    });

    useChatStore.setState({ refreshMessages: realRefresh });
    expect(duringConfirm).not.toBeNull();
    expect(duringConfirm!.isStreaming).toBe(true);
    expect(duringConfirm!.streamingThreadId).toBe('thread-A');
  });

  // Invariant coverage, not a regression: the async confirm path captures the
  // setter from its call-time render, so it already resolved to the owning
  // thread. The explicit owner id removes the dependence on that timing.
  it('clears a settled confirmation from its own thread, not the viewed one (H1)', async () => {
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks) => {
        cb.onConfirmation('agent-thread-1', { tool: 'ingest_arxiv_papers' });
        cb.onDone({});
        return Promise.resolve();
      }
    );
    streamConfirmMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks) => {
        // The user walks away to another thread while the confirm resolves.
        useChatStore.setState({ currentThreadId: 'thread-B' });
        cb.onReasoningDelta?.('summary from thread A');
        cb.onToken('confirmed answer');
        cb.onDone({});
        return Promise.resolve();
      }
    );

    const params = makeParams();
    const { result } = renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleSubmit('ingest these');
    });
    expect(result.current.pendingConfirmation).not.toBeNull();

    await act(async () => {
      await result.current.handleConfirmation(true);
    });

    // Back on the original thread the settled card must be gone.
    await act(async () => {
      useChatStore.setState({ currentThreadId: 'thread-A' });
    });
    expect(result.current.pendingConfirmation).toBeNull();
    const displayedMessages = params.setMessages.mock.calls.flatMap(([next]) =>
      Array.isArray(next) ? (next as ChatPageMessage[]) : []
    );
    expect(
      displayedMessages.some(
        (message) => message.reasoningSummary === 'summary from thread A'
      )
    ).toBe(false);
  });

  it('clears stale RAG retrieval state when a confirmation stream starts', async () => {
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks) => {
        cb.onConfirmation('agent-thread-1', { tool: 'ingest_arxiv_papers' });
        cb.onDone({});
        return Promise.resolve();
      }
    );
    let retrievingAtConfirmStart: boolean | undefined;
    streamConfirmMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks) => {
        retrievingAtConfirmStart = useChatStore.getState().isRetrievingRag;
        cb.onToken('confirmed answer');
        cb.onDone({});
        return Promise.resolve();
      }
    );
    const params = makeParams();
    const { result } = renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleSubmit('ingest these');
    });
    useChatStore.setState({ isRetrievingRag: true });

    await act(async () => {
      await result.current.handleConfirmation(true);
    });

    expect(retrievingAtConfirmStart).toBe(false);
  });

  it('reattaches once after a live turn dies on a transport error', async () => {
    // The backend run outlives a client-side transport failure, so the answer
    // is still coming; finishing the run as 'error' must not also cancel the
    // one reattach attempt that recovers it.
    streamMessageMock.mockRejectedValue(new Error('socket hang up'));
    const resume = vi.mocked(
      (await import('@/services/agentChatService')).agentChatService
        .resumeStream
    );
    resume.mockResolvedValue({ status: 'idle' });

    const params = makeParams();
    const { result } = renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleSubmit('hello');
    });

    await waitFor(() => expect(resume).toHaveBeenCalled());
    expect(resume.mock.calls[0][0]).toBe('thread-A');
  });

  it('finishes the activity run when the transport throws (H8)', async () => {
    streamMessageMock.mockRejectedValue(new Error('socket hang up'));

    const params = makeParams();
    const { result } = renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleSubmit('hello');
    });

    const run = useAgentActivityStore.getState().runs['thread-A'];
    expect(run?.state).toBe('error');
  });

  it('sends a durable Stop for the accepted run before aborting the stream', async () => {
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks, signal?: AbortSignal) => {
        cb.onRunId?.('run-1');
        return new Promise<void>((resolve) => {
          signal?.addEventListener('abort', () => resolve(), { once: true });
        });
      }
    );
    cancelActiveRunMock.mockResolvedValue(undefined);

    const params = makeParams();
    const { result } = renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );

    let submitPromise: Promise<void> | undefined;
    await act(async () => {
      submitPromise = result.current.handleSubmit('hello');
      await waitFor(() => expect(streamMessageMock).toHaveBeenCalled());
    });

    await act(async () => {
      result.current.handleStop();
      await Promise.resolve();
    });
    expect(cancelActiveRunMock).toHaveBeenCalledWith('thread-A', 'run-1');
    await act(async () => {
      await submitPromise;
    });
  });

  it('does not finish the activity run until the producer ACK resolves', async () => {
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks, signal?: AbortSignal) => {
        cb.onRunId?.('run-ack');
        return new Promise<void>((resolve) => {
          signal?.addEventListener('abort', () => resolve(), { once: true });
        });
      }
    );
    let resolveCancel!: () => void;
    cancelActiveRunMock.mockImplementation(
      () => new Promise<void>((resolve) => (resolveCancel = resolve))
    );

    const params = makeParams();
    const { result } = renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );

    let submitPromise: Promise<void> | undefined;
    await act(async () => {
      submitPromise = result.current.handleSubmit('hello');
      await waitFor(() => expect(streamMessageMock).toHaveBeenCalled());
    });

    act(() => result.current.handleStop());
    expect(useAgentActivityStore.getState().runs['thread-A']?.state).toBe(
      'running'
    );

    resolveCancel();
    await waitFor(() =>
      expect(useAgentActivityStore.getState().runs['thread-A']?.state).toBe(
        'stopped'
      )
    );
    await act(async () => {
      await submitPromise;
    });
  });

  it('does not let a late ACK stop a newer run on the same thread', async () => {
    let streamCalls = 0;
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks, signal?: AbortSignal) => {
        streamCalls += 1;
        cb.onRunId?.(streamCalls === 1 ? 'run-old' : 'run-new');
        return new Promise<void>((resolve) => {
          signal?.addEventListener('abort', () => resolve(), { once: true });
        });
      }
    );
    let resolveOldCancel!: () => void;
    cancelActiveRunMock
      .mockImplementationOnce(
        () => new Promise<void>((resolve) => (resolveOldCancel = resolve))
      )
      .mockResolvedValue(undefined);

    const params = makeParams();
    const { result } = renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );

    let firstSubmit: Promise<void> | undefined;
    await act(async () => {
      firstSubmit = result.current.handleSubmit('first');
      await waitFor(() => expect(streamCalls).toBe(1));
    });
    act(() => result.current.handleStop());
    await act(async () => {
      await firstSubmit;
    });

    let secondSubmit: Promise<void> | undefined;
    await act(async () => {
      secondSubmit = result.current.handleSubmit('second');
      await waitFor(() => expect(streamCalls).toBe(2));
    });
    expect(useAgentActivityStore.getState().runs['thread-A']).toMatchObject({
      state: 'running',
      runId: 'run-new',
    });

    await act(async () => {
      resolveOldCancel();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(useAgentActivityStore.getState().runs['thread-A']).toMatchObject({
      state: 'running',
      runId: 'run-new',
    });

    act(() => result.current.handleStop());
    await act(async () => {
      await secondSubmit;
    });
  });

  it('fences Stop during confirmation continuation to the parked run id', async () => {
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks) => {
        cb.onConfirmation(
          'agent-thread-1',
          { tool: 'create_note' },
          'run-confirm'
        );
        cb.onDone({});
        return Promise.resolve();
      }
    );
    streamConfirmMock.mockImplementation(
      (_req: unknown, _cb: StreamCallbacks, signal?: AbortSignal) =>
        new Promise<void>((resolve) => {
          signal?.addEventListener('abort', () => resolve(), { once: true });
        })
    );
    cancelActiveRunMock.mockResolvedValue(undefined);

    const params = makeParams();
    const { result } = renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleSubmit('create a note');
    });
    await waitFor(() =>
      expect(result.current.pendingConfirmation).not.toBeNull()
    );

    await act(async () => {
      void result.current.handleConfirmation(true);
      await waitFor(() => expect(streamConfirmMock).toHaveBeenCalled());
    });
    act(() => result.current.handleStop());

    expect(cancelActiveRunMock).toHaveBeenCalledWith('thread-A', 'run-confirm');
    expect(result.current.pendingConfirmation).toBeNull();
  });
});

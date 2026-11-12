/**
 * Round-3 L2 + L7: the cold-load HITL probe must be abandoned when a new
 * submit makes it stale (its late confirmation frame armed a phantom gate),
 * and must detach immediately when the replay shows a live run instead of a
 * parked confirmation.
 *
 * A2: detaching is not enough on its own. After a hard refresh the activity
 * store is empty, so the live run must be handed to the resume effect, which
 * reattaches from cursor 0. Otherwise the chat never shows the run again.
 */
import { describe, expect, it, vi, beforeEach, onTestFinished } from 'vitest';
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

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const streamMessageMock = vi.fn();
const resumeStreamMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: (...args: unknown[]) => streamMessageMock(...args),
    streamConfirm: vi.fn(),
    resumeStream: (...args: unknown[]) => resumeStreamMock(...args),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    createMessage: vi.fn().mockResolvedValue({ id: 'db-msg-1' }),
    listMessages: vi.fn().mockResolvedValue({ messages: [], has_more: false }),
  },
}));

import { useChatStreaming } from '@/hooks/chat/useChatStreaming';
import { useAgentActivityStore } from '@/stores/agentActivityStore';

type ProbeCallbacks = {
  onConfirmation: (threadId: string, c: Record<string, unknown>) => void;
  onToken?: (t: string) => void;
  onToolStart?: (name: string) => void;
  onStatus?: (phase: string, detail?: string) => void;
};

function makeParams(): Record<string, unknown> {
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

describe('useChatStreaming cold-load probe invalidation', () => {
  beforeEach(() => {
    streamMessageMock.mockReset();
    resumeStreamMock.mockReset();
    useChatStore.getState().reset();
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.setState({ currentThreadId: 'thread-A' });
  });

  it('a probe raced by a submit cannot arm a phantom gate (L2)', async () => {
    let probeCallbacks!: ProbeCallbacks;
    let probeSignal!: AbortSignal;
    resumeStreamMock.mockImplementation(
      (
        _threadId: string,
        _afterSeq: number,
        cb: ProbeCallbacks,
        signal: AbortSignal
      ) => {
        probeCallbacks = cb;
        probeSignal = signal;
        // Held open — the confirmation frame will arrive after the submit.
        return new Promise(() => {});
      }
    );
    streamMessageMock.mockImplementation(
      async (_req: unknown, cb: { onDone: (p?: unknown) => void }) => {
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
    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalled());

    await act(async () => {
      await result.current.handleSubmit('new question');
    });
    expect(probeSignal.aborted).toBe(true);

    // The stale confirmation frame races in anyway.
    act(() => {
      probeCallbacks.onConfirmation('agent-thread-1', {
        tool: 'ingest_arxiv_papers',
      });
    });

    expect(result.current.pendingConfirmation).toBeNull();
  });

  it('a probe that hits a live run detaches instead of consuming it (L7)', async () => {
    const signals: AbortSignal[] = [];
    resumeStreamMock.mockImplementation(
      (
        _threadId: string,
        _afterSeq: number,
        cb: ProbeCallbacks,
        signal: AbortSignal
      ) => {
        signals.push(signal);
        // The replay opens with a token — this is a live run, not a parked
        // confirmation.
        cb.onToken?.('live token');
        return new Promise(() => {});
      }
    );

    const params = makeParams();
    renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );

    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalled());
    expect(signals[0].aborted).toBe(true);
  });

  it('hands a token-first live run to the resume effect, which reattaches from cursor 0 (A2)', async () => {
    const signals: AbortSignal[] = [];
    resumeStreamMock.mockImplementation(
      (
        _threadId: string,
        _afterSeq: number,
        cb: ProbeCallbacks,
        signal: AbortSignal
      ) => {
        signals.push(signal);
        if (resumeStreamMock.mock.calls.length === 1) {
          // Probe: a live run replays several non-confirmation frames. The
          // hand-off must happen once, not once per frame.
          cb.onToken?.('live token');
          cb.onToolStart?.('search_arxiv');
          cb.onStatus?.('thinking');
          cb.onToken?.('more');
        }
        return new Promise(() => {});
      }
    );

    const params = makeParams();
    renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );

    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(2));
    // The probe let go of the stream...
    expect(signals[0].aborted).toBe(true);
    // ...and the resume effect reattached to the same thread from cursor 0.
    expect(resumeStreamMock.mock.calls[1][0]).toBe('thread-A');
    expect(resumeStreamMock.mock.calls[1][1]).toBe(0);
    expect(signals[1].aborted).toBe(false);
    expect(useAgentActivityStore.getState().runs['thread-A']?.state).toBe(
      'running'
    );
    // Exactly one hand-off per probe.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(resumeStreamMock).toHaveBeenCalledTimes(2);
  });

  it('does not hand off a live run for a thread that is no longer selected (A2)', async () => {
    let probeCallbacks!: ProbeCallbacks;
    resumeStreamMock.mockImplementation(
      (_threadId: string, _afterSeq: number, cb: ProbeCallbacks) => {
        if (resumeStreamMock.mock.calls.length === 1) probeCallbacks = cb;
        return new Promise(() => {});
      }
    );

    // thread-B already settled a run this session, so selecting it opens no
    // probe of its own — and therefore does NOT abort thread-A's probe. The
    // only thing standing between thread-A's late frame and a seeded run is
    // the hand-off's `currentThreadId !== threadId` guard. (Previously this
    // test let thread-B's probe abort thread-A's, so it passed with the
    // guard deleted.)
    useAgentActivityStore.getState().startRun('thread-B', 'Agent', 'settled');
    useAgentActivityStore.getState().finishRun('thread-B', 'done');

    const params = makeParams();
    renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );
    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(1));
    const probeSignal = resumeStreamMock.mock.calls[0][3] as AbortSignal;

    // The user moves to another thread before thread-A's first frame lands.
    act(() => {
      useChatStore.setState({ currentThreadId: 'thread-B' });
    });
    await new Promise((resolve) => setTimeout(resolve, 20));
    // Precondition for exercising the guard: thread-A's probe is still open.
    expect(probeSignal.aborted).toBe(false);
    expect(resumeStreamMock).toHaveBeenCalledTimes(1);

    act(() => {
      probeCallbacks.onToken?.('late token for thread-A');
    });

    expect(useAgentActivityStore.getState().runs['thread-A']).toBeUndefined();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(resumeStreamMock).toHaveBeenCalledTimes(1);
  });

  it('a handed-off run that was cancelled on disconnect ends quietly and is reconciled from the server (A2 follow-up)', async () => {
    // Reload race: the probe lands before the disconnect cancel clears the
    // active-run pointer, so it hands the run off. The cancel path writes no
    // terminal frame, so every resume attempt ends `failed`.
    // refreshMessages is the store's existing reconcile path; it is an action,
    // so restore the real one afterwards (reset() only resets data).
    const realRefreshMessages = useChatStore.getState().refreshMessages;
    const refreshMessages = vi.fn().mockResolvedValue(true);
    useChatStore.setState({ refreshMessages });
    onTestFinished(() =>
      useChatStore.setState({ refreshMessages: realRefreshMessages })
    );
    const probeKeys = [
      'onConfirmation',
      'onRunId',
      'onToken',
      'onToolStart',
      'onStatus',
      'onPlan',
    ];
    resumeStreamMock.mockImplementation(
      async (_threadId: string, _afterSeq: number, cb: ProbeCallbacks) => {
        const call = resumeStreamMock.mock.calls.length;
        if (call === 1) {
          cb.onToken?.('partial answer');
          return new Promise(() => {});
        }
        if (call === 2) cb.onToken?.('partial answer');
        if (Object.keys(cb).join() === probeKeys.join()) {
          return { status: 'idle' };
        }
        return { status: 'failed', error: 'Stream ended unexpectedly' };
      }
    );

    const params = makeParams();
    renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );

    // Probe + three resume attempts.
    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(4), {
      timeout: 10_000,
    });
    // The thread is reconciled from the persisted rows (the stopped partial).
    await waitFor(() =>
      expect(refreshMessages.mock.calls.map(([t]) => t)).toContain('thread-A')
    );

    // No "Something went wrong" bubble for a run the user never saw fail.
    const setMessages = params.setMessages as ReturnType<typeof vi.fn>;
    const renderedError = setMessages.mock.calls.some(
      ([next]) =>
        Array.isArray(next) &&
        (next as ChatPageMessage[]).some((m) => m.error !== undefined)
    );
    expect(renderedError).toBe(false);
    // The seeded run is settled, not left 'running' forever.
    expect(useAgentActivityStore.getState().runs['thread-A']).toBeUndefined();

    // Nothing is left to block the next activation's probe.
    act(() => useChatStore.setState({ currentThreadId: 'thread-B' }));
    act(() => useChatStore.setState({ currentThreadId: 'thread-A' }));
    await waitFor(() => {
      const threadAProbes = resumeStreamMock.mock.calls.filter(
        ([threadId, , cb]) =>
          threadId === 'thread-A' &&
          Object.keys(cb as object).join() === probeKeys.join()
      );
      expect(threadAProbes).toHaveLength(2);
    });
  }, 20_000);
});

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

    const params = makeParams();
    renderHook(
      () =>
        useChatStreaming(
          params as unknown as Parameters<typeof useChatStreaming>[0]
        ),
      { wrapper }
    );
    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(1));

    // The user moves to another thread before thread-A's first frame lands.
    act(() => {
      useChatStore.setState({ currentThreadId: 'thread-B' });
    });
    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(2));
    expect(resumeStreamMock.mock.calls[1][0]).toBe('thread-B');

    act(() => {
      probeCallbacks.onToken?.('late token for thread-A');
    });

    expect(useAgentActivityStore.getState().runs['thread-A']).toBeUndefined();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(resumeStreamMock).toHaveBeenCalledTimes(2);
  });
});

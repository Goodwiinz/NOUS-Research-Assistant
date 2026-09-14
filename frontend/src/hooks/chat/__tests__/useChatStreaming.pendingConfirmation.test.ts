// A thread parked on a HITL interrupt must re-arm its approval gate after a
// reload. The activity store is in-memory, so a cold client has no run record
// and the resume effect can never fire — the user saw their message with no
// reply, no Approve/Deny card and an unlocked composer, re-sent, and parked a
// SECOND interrupt.
import { describe, expect, it, vi, beforeEach } from 'vitest';
import {
  act,
  renderHook,
  waitFor,
  type RenderHookResult,
} from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import type {
  UseChatStreamingParams,
  UseChatStreamingReturn,
} from '@/hooks/chat/useChatStreaming';
import { useChatStore } from '@/store/chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';

const realRefreshMessages = useChatStore.getState().refreshMessages;

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
const cancelPendingConfirmationMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: vi.fn(),
    streamConfirm: vi.fn(),
    resumeStream: (...args: unknown[]) => resumeStreamMock(...args),
    cancelPendingConfirmation: (...args: unknown[]) =>
      cancelPendingConfirmationMock(...args),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    listMessages: vi.fn().mockResolvedValue({ messages: [], has_more: false }),
  },
}));

const CONFIRMATION = {
  tool_name: 'create_project',
  tool_args: { name: 'rag testing' },
};

/** Server holds a parked interrupt: resume replays a single confirmation. */
function parkedInterrupt(): void {
  resumeStreamMock.mockImplementation(
    async (
      threadId: string,
      _after: number,
      cb: {
        onConfirmation?: (t: string, c: Record<string, unknown>) => void;
      }
    ) => {
      cb.onConfirmation?.(threadId, CONFIRMATION);
      return { status: 'resumed' };
    }
  );
}

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

async function renderStreaming(): Promise<
  RenderHookResult<UseChatStreamingReturn, unknown>
> {
  const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
  const params = makeParams();
  return renderHook(() => useChatStreaming(params), { wrapper });
}

describe('useChatStreaming pending-confirmation probe (cold thread load)', () => {
  beforeEach(() => {
    resumeStreamMock.mockReset();
    resumeStreamMock.mockResolvedValue({ status: 'idle' });
    cancelPendingConfirmationMock.mockReset();
    cancelPendingConfirmationMock.mockResolvedValue(undefined);
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.setState({
      currentThreadId: 'thread-A',
      isStreaming: false,
      streamingThreadId: null,
      refreshMessages: realRefreshMessages,
    });
  });

  it('re-arms the approval gate for a thread parked server-side', async () => {
    parkedInterrupt();
    const { result } = await renderStreaming();

    await waitFor(() =>
      expect(result.current.pendingConfirmation).not.toBeNull()
    );
    expect(result.current.pendingConfirmation).toMatchObject({
      threadId: 'thread-A',
      // Must equal the DISPLAYED thread or confirmationBelongsToThread rejects
      // the card and Approve refuses to act.
      workspaceThreadId: 'thread-A',
      confirmation: CONFIRMATION,
    });
    expect(resumeStreamMock).toHaveBeenCalledTimes(1);
  });

  it('durably cancels a parked confirmation when Stop is pressed', async () => {
    parkedInterrupt();
    const { result } = await renderStreaming();
    await waitFor(() =>
      expect(result.current.pendingConfirmation).not.toBeNull()
    );

    act(() => result.current.handleStop());

    expect(cancelPendingConfirmationMock).toHaveBeenCalledWith('thread-A');
    await waitFor(() => expect(result.current.pendingConfirmation).toBeNull());
  });

  it('keeps the confirmation retryable when durable Stop fails', async () => {
    parkedInterrupt();
    cancelPendingConfirmationMock.mockRejectedValueOnce(new Error('offline'));
    const { result } = await renderStreaming();
    await waitFor(() =>
      expect(result.current.pendingConfirmation).not.toBeNull()
    );

    act(() => result.current.handleStop());

    await waitFor(() =>
      expect(cancelPendingConfirmationMock).toHaveBeenCalledWith('thread-A')
    );
    expect(result.current.pendingConfirmation).not.toBeNull();
  });

  it('a stop that raced a failed cancel does not taint a later approval (M2)', async () => {
    parkedInterrupt();
    // The cancel hangs (it will eventually lose the race) — the user approves
    // while it is still pending, so the stale stop is live during the confirm
    // stream. With the shared boolean this tagged the approved turn 'stopped'.
    let failCancel: (() => void) | undefined;
    cancelPendingConfirmationMock.mockImplementation(
      () =>
        new Promise<void>((_resolve, reject) => {
          failCancel = () => reject(new Error('conflict'));
        })
    );
    const streamConfirmMock = vi.mocked(
      (await import('@/services/agentChatService')).agentChatService
        .streamConfirm
    );
    streamConfirmMock.mockImplementation(
      async (
        _req: unknown,
        cb: { onToken?: (t: string) => void; onDone?: (p?: unknown) => void }
      ) => {
        cb.onToken?.('confirmed answer');
        cb.onDone?.({});
      }
    );
    const refreshSpy = vi.fn().mockResolvedValue(true);
    useChatStore.setState({ refreshMessages: refreshSpy });

    const { result } = await renderStreaming();
    await waitFor(() =>
      expect(result.current.pendingConfirmation).not.toBeNull()
    );

    act(() => result.current.handleStop());
    await waitFor(() =>
      expect(cancelPendingConfirmationMock).toHaveBeenCalled()
    );
    // User changes their mind and approves while the cancel is still pending.
    expect(result.current.pendingConfirmation).not.toBeNull();
    await act(async () => {
      await result.current.handleConfirmation(true);
    });
    await act(async () => {
      failCancel?.();
      await Promise.resolve();
    });

    // The stale stop targeted the pre-approval stream; the approved turn must
    // commit as approved, not as stopped.
    expect(refreshSpy).toHaveBeenCalledWith(
      'thread-A',
      expect.objectContaining({
        diagnostic: expect.objectContaining({
          terminalReason: 'confirmation-approved',
        }),
      })
    );
  });

  it('keeps each parked confirmation while probing another thread', async () => {
    parkedInterrupt();
    const { result } = await renderStreaming();
    await waitFor(() =>
      expect(result.current.pendingConfirmation?.workspaceThreadId).toBe(
        'thread-A'
      )
    );

    await act(async () => {
      useChatStore.setState({ currentThreadId: 'thread-B' });
    });
    await waitFor(() =>
      expect(result.current.pendingConfirmation?.workspaceThreadId).toBe(
        'thread-B'
      )
    );
    expect(resumeStreamMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      useChatStore.setState({ currentThreadId: 'thread-A' });
    });
    expect(result.current.pendingConfirmation?.workspaceThreadId).toBe(
      'thread-A'
    );
  });

  it('leaves the gate closed when the server has nothing parked (204)', async () => {
    const { result } = await renderStreaming();

    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(1));
    expect(result.current.pendingConfirmation).toBeNull();
  });

  it('probes at most once per thread activation', async () => {
    parkedInterrupt();
    const { result, rerender } = await renderStreaming();

    await waitFor(() =>
      expect(result.current.pendingConfirmation).not.toBeNull()
    );
    rerender();
    rerender();

    expect(resumeStreamMock).toHaveBeenCalledTimes(1);
  });

  it.each(['done', 'stopped'] as const)(
    'does not probe a thread with a settled %s run',
    async (state) => {
      parkedInterrupt();
      useAgentActivityStore
        .getState()
        .startRun('thread-A', 'NOUS', 'already mine');
      useAgentActivityStore.getState().finishRun('thread-A', state);

      const { result } = await renderStreaming();
      await new Promise((resolve) => setTimeout(resolve, 30));

      expect(resumeStreamMock).not.toHaveBeenCalled();
      expect(result.current.pendingConfirmation).toBeNull();
    }
  );

  it('probes an error run for a confirmation left parked server-side', async () => {
    parkedInterrupt();
    useAgentActivityStore
      .getState()
      .startRun('thread-A', 'NOUS', 'failed confirmation');
    useAgentActivityStore.getState().finishRun('thread-A', 'error');

    const { result } = await renderStreaming();

    await waitFor(() =>
      expect(result.current.pendingConfirmation).not.toBeNull()
    );
    expect(resumeStreamMock).toHaveBeenCalledWith(
      'thread-A',
      0,
      expect.any(Object),
      expect.any(AbortSignal)
    );
  });

  it('leaves a running run with the cursor-based resume owner', async () => {
    const resumeLog = vi.spyOn(console, 'log').mockImplementation(() => {});
    try {
      const refreshSpy = vi.fn().mockResolvedValue(true);
      useChatStore.setState({ refreshMessages: refreshSpy });
      useAgentActivityStore
        .getState()
        .startRun('thread-A', 'NOUS', 'still running');
      useAgentActivityStore.getState().setStreamSeq('thread-A', 7, 'stream-A');
      resumeStreamMock.mockResolvedValue({ status: 'idle' });

      const { result } = await renderStreaming();

      await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(1));
      expect(resumeStreamMock).toHaveBeenCalledWith(
        'thread-A',
        7,
        expect.any(Object),
        expect.any(AbortSignal),
        'stream-A'
      );
      expect(resumeLog).toHaveBeenCalledWith(
        '[Chat] Resuming in-flight agent stream:',
        'thread-A'
      );
      await waitFor(() => expect(refreshSpy).toHaveBeenCalled());
      expect(result.current.pendingConfirmation).toBeNull();
    } finally {
      resumeLog.mockRestore();
    }
  });

  it('does not probe while a stream is live', async () => {
    parkedInterrupt();
    useChatStore.setState({ isStreaming: true });

    await renderStreaming();
    await new Promise((resolve) => setTimeout(resolve, 30));

    expect(resumeStreamMock).not.toHaveBeenCalled();
  });

  it('retries a failed probe when the thread is activated again', async () => {
    resumeStreamMock.mockResolvedValue({
      status: 'failed',
      error: 'offline',
    });
    useAgentActivityStore.getState().startRun('thread-B', 'NOUS', 'settled');
    useAgentActivityStore.getState().finishRun('thread-B', 'stopped');
    const { result } = await renderStreaming();

    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(1));
    expect(result.current.pendingConfirmation).toBeNull();

    await act(async () => {
      useChatStore.setState({ currentThreadId: 'thread-B' });
    });
    await act(async () => {
      useChatStore.setState({ currentThreadId: 'thread-A' });
    });

    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(2));
  });
});

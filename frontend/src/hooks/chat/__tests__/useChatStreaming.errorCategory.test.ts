// W2: when the SERVER authored the failure, the error bubble carries the
// server's category. The client-authored categories ('empty-response',
// 'exception') stay client-authored — no server frame exists for those.
// The confirm path additionally used to commit a plain content bubble with no
// `error` block at all; it now gets the same {message, category} shape.
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { useChatStore } from '@/store/chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import type { UseChatStreamingParams } from '@/hooks/chat/useChatStreaming';

function wrapper({ children }: { children: ReactNode }): ReactNode {
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
const resumeStreamMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: (...args: unknown[]) => streamMessageMock(...args),
    streamConfirm: (...args: unknown[]) => streamConfirmMock(...args),
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

const THREAD_ID = 'thread-A';

type Setter = (
  next: ChatPageMessage[] | ((prev: ChatPageMessage[]) => ChatPageMessage[])
) => void;

function makeParams(setMessages: Setter): UseChatStreamingParams {
  useChatStore.setState({ currentThreadId: THREAD_ID });
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: [] as ChatPageMessage[],
    setMessages,
    conversations: [],
    setConversations: vi.fn(),
    activeConversationId: THREAD_ID,
    setActiveConversationId: vi.fn(),
    activeConversationIdRef: { current: THREAD_ID as string | null },
    dbConversation: null,
    setCurrentThread: vi.fn(),
    enableRAG: false,
  };
}

/**
 * Last assistant bubble that carries an `error` block.
 *
 * `setMessages` is a `React.Dispatch<React.SetStateAction<...>>`, so a caller
 * may pass either the next array or a functional updater. Replay the calls in
 * order the way React would — feeding each updater the state the previous call
 * produced — otherwise an updater-form call is invisible here and the bubble
 * it carries reads as `undefined`.
 */
function lastErrorBubble(
  calls: Array<[ChatPageMessage[] | unknown]>
): ChatPageMessage | undefined {
  const states: ChatPageMessage[][] = [];
  let prev: ChatPageMessage[] = [];
  for (const [arg] of calls) {
    const next =
      typeof arg === 'function'
        ? (arg as (p: ChatPageMessage[]) => ChatPageMessage[])(prev)
        : arg;
    if (!Array.isArray(next)) continue;
    prev = next as ChatPageMessage[];
    states.push(prev);
  }
  for (let i = states.length - 1; i >= 0; i -= 1) {
    const withError = [...states[i]].reverse().find((m) => m?.error);
    if (withError) return withError;
  }
  return undefined;
}

async function submitWithStreamError(
  category?: string
): Promise<ChatPageMessage | undefined> {
  streamMessageMock.mockImplementation(
    (
      _req: unknown,
      cb: { onError?: (error: string, category?: string) => void }
    ) => {
      cb.onError?.('upstream exploded', category);
      return Promise.resolve();
    }
  );
  const setMessages = vi.fn();
  const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
  const { result } = renderHook(
    () => useChatStreaming(makeParams(setMessages as Setter)),
    { wrapper }
  );
  await act(async () => {
    await result.current.handleSubmit('hello');
  });
  return lastErrorBubble(setMessages.mock.calls as never);
}

describe('useChatStreaming maps the server error category onto the bubble', () => {
  beforeEach(() => {
    streamMessageMock.mockReset();
    streamConfirmMock.mockReset();
    resumeStreamMock.mockReset();
    resumeStreamMock.mockResolvedValue({ status: 'idle' });
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.setState({
      currentThreadId: THREAD_ID,
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  it('uses the server category when the error frame carried one', async () => {
    const bubble = await submitWithStreamError('rate_limited');
    expect(bubble?.error?.category).toBe('rate_limited');
  });

  it('passes through every server category unchanged', async () => {
    for (const category of ['upstream_timeout', 'conflict', 'model_error']) {
      const bubble = await submitWithStreamError(category);
      expect(bubble?.error?.category).toBe(category);
    }
  });

  it("falls back to the client 'stream-error' when the server sent none", async () => {
    const bubble = await submitWithStreamError(undefined);
    expect(bubble?.error?.category).toBe('stream-error');
  });

  it('keeps the user-facing error message stable regardless of category', async () => {
    const bubble = await submitWithStreamError('conflict');
    expect(bubble?.error?.message).toBe(
      'This response failed to generate. Please try again.'
    );
  });

  it("leaves the client-detected empty response authored as 'empty-response'", async () => {
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: { onDone?: (p?: unknown) => void }) => {
        cb.onDone?.({});
        return Promise.resolve();
      }
    );
    const setMessages = vi.fn();
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(
      () => useChatStreaming(makeParams(setMessages as Setter)),
      { wrapper }
    );
    await act(async () => {
      await result.current.handleSubmit('hello');
    });
    // No server frame exists for "stream ended with no tokens" — the client
    // detected it, so the client keeps authorship of the category.
    expect(
      lastErrorBubble(setMessages.mock.calls as never)?.error?.category
    ).toBe('empty-response');
  });
});

describe('useChatStreaming confirm-path failure bubble', () => {
  const CONFIRMATION = {
    tool_name: 'create_note',
    tool_args: { title: 'parked' },
  };

  beforeEach(() => {
    streamMessageMock.mockReset();
    streamConfirmMock.mockReset();
    resumeStreamMock.mockReset();
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
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.setState({
      currentThreadId: THREAD_ID,
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  async function confirmWithError(
    category?: string
  ): Promise<ChatPageMessage | undefined> {
    streamConfirmMock.mockImplementation(
      async (
        _body: unknown,
        cb: { onError?: (error: string, category?: string) => void }
      ) => {
        cb.onError?.('confirm exploded', category);
      }
    );
    const setMessages = vi.fn();
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(
      () => useChatStreaming(makeParams(setMessages as Setter)),
      { wrapper }
    );
    await waitFor(() =>
      expect(result.current.pendingConfirmation).not.toBeNull()
    );
    await act(async () => {
      await result.current.handleConfirmation(true);
    });
    return lastErrorBubble(setMessages.mock.calls as never);
  }

  it('now commits an error block at all (it used to be a plain content bubble)', async () => {
    const bubble = await confirmWithError('conflict');
    expect(bubble?.error).toBeDefined();
    expect(bubble?.error?.message).toBe(
      'This confirmation failed to complete. Please try again.'
    );
  });

  it('carries the server category on the confirm failure', async () => {
    const bubble = await confirmWithError('conflict');
    expect(bubble?.error?.category).toBe('conflict');
  });

  it('falls back to the client category when the server sent none', async () => {
    const bubble = await confirmWithError(undefined);
    expect(bubble?.error?.category).toBe('stream-error');
  });

  it('keeps raw failure text out of the bubble content', async () => {
    const bubble = await confirmWithError('checkpoint_unavailable');
    expect(bubble?.content).toBe('');
    expect(bubble?.content).not.toContain('confirm exploded');
    expect(bubble?.error?.category).toBe('checkpoint_unavailable');
  });
});

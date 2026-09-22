import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { makeChatPageMessage } from '@/test/chatMessageFactory';

// The hook calls useQueryClient — provide one for renderHook.
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

const streamMessageMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: (...args: unknown[]) => streamMessageMock(...args),
    streamConfirm: vi.fn(),
    // The hook probes for a parked HITL confirmation on thread activation;
    // nothing is parked in these scenarios.
    resumeStream: vi.fn().mockResolvedValue({ status: 'idle' }),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    getOrCreateDefaultWorkspace: vi.fn(),
    getOrCreateDefaultConversation: vi.fn(),
    createThread: vi.fn(),
    createMessage: vi.fn().mockResolvedValue({ id: 'db-msg-1' }),
    listMessages: vi.fn(),
  },
}));

const toastErrorMock = vi.fn();
vi.mock('react-hot-toast', () => ({
  default: {
    error: (...args: unknown[]) => toastErrorMock(...args),
    success: vi.fn(),
  },
}));

import { useChatStreaming } from '@/hooks/chat/useChatStreaming';
import { useChatStore } from '@/store/chat-store';
import { workspaceService } from '@/services/workspaceService';

function makeParams(
  overrides: Partial<Parameters<typeof useChatStreaming>[0]> = {}
) {
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
    dbConversation: null, // no thread creation path; thread-A already exists
    enableRAG: false,
    ...overrides,
  };
}

describe('useChatStreaming failed thread creation', () => {
  beforeEach(() => {
    streamMessageMock.mockReset();
    toastErrorMock.mockReset();
    vi.clearAllMocks();
    useChatStore.getState().reset();
    useChatStore.setState({ currentThreadId: 'thread-A' });
    vi.mocked(workspaceService.listMessages).mockResolvedValue({
      messages: [],
      has_more: false,
    } as never);
  });

  it('rolls back the optimistic bubble, restores input, and toasts when thread creation fails', async () => {
    const { workspaceService } = await import('@/services/workspaceService');
    vi.mocked(workspaceService.createThread).mockRejectedValueOnce(
      new Error('500')
    );

    // First send in a brand-new conversation → the create-thread branch runs.
    const originalMessages: ChatPageMessage[] = [];
    const params = makeParams({
      messages: originalMessages,
      displayedMessages: originalMessages,
      activeConversationId: null,
      dbConversation: { id: 'conv-1' } as never,
    });
    useChatStore.setState({ currentThreadId: null });

    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    await act(async () => {
      await result.current.handleSubmit('my first message');
    });

    // Rollback: the final setMessages restores the pre-submit list (no ghost).
    const calls = params.setMessages.mock.calls;
    expect(calls[calls.length - 1][0]).toBe(originalMessages);
    // Composer text is restored so the user does not lose what they typed.
    expect(result.current.input).toBe('my first message');
    // The user is told, and nothing was streamed.
    expect(toastErrorMock).toHaveBeenCalled();
    expect(streamMessageMock).not.toHaveBeenCalled();
  });

  it('uses the active workspace to resolve a parent before first send', async () => {
    const { workspaceService } = await import('@/services/workspaceService');
    vi.mocked(
      workspaceService.getOrCreateDefaultWorkspace
    ).mockResolvedValueOnce({ id: 'ws-1' } as never);
    vi.mocked(
      workspaceService.getOrCreateDefaultConversation
    ).mockResolvedValueOnce({ id: 'conv-1' } as never);
    vi.mocked(workspaceService.createThread).mockResolvedValueOnce({
      id: 'thread-new',
      conversation_id: 'conv-canonical',
      title: 'My first message',
    } as never);
    streamMessageMock.mockImplementation(
      (
        req: { thread_id?: string },
        callbacks: {
          onToken: (token: string) => void;
          onDone: (payload?: unknown) => void;
        }
      ) => {
        callbacks.onToken('answer');
        callbacks.onDone({ thread_id: req.thread_id });
        return Promise.resolve();
      }
    );

    const params = makeParams({
      messages: [],
      displayedMessages: [],
      activeConversationId: null,
      dbConversation: null,
      workspace: { id: 'ws-1', name: 'Workspace' } as never,
    });
    useChatStore.setState({ currentThreadId: null });

    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    await act(async () => {
      await result.current.handleSubmit('my first message');
    });

    expect(workspaceService.getOrCreateDefaultWorkspace).not.toHaveBeenCalled();
    expect(
      workspaceService.getOrCreateDefaultConversation
    ).toHaveBeenCalledWith('ws-1');
    expect(workspaceService.createThread).toHaveBeenCalledWith(
      expect.objectContaining({ conversation_id: 'conv-1' })
    );
    expect(streamMessageMock).toHaveBeenCalledWith(
      expect.objectContaining({
        thread_id: 'thread-new',
        page_context: expect.objectContaining({ workspace_id: 'ws-1' }),
      }),
      expect.anything(),
      expect.anything()
    );
    const upsert = params.setConversations.mock.calls[0][0] as (
      current: unknown[]
    ) => Array<{ id: string; conversationId: string }>;
    expect(upsert([])[0]).toMatchObject({
      id: 'thread-new',
      conversationId: 'conv-canonical',
    });
    expect(useChatStore.getState().currentThreadId).toBe('thread-new');
  });
});

describe('useChatStreaming thread-switch guard', () => {
  beforeEach(() => {
    streamMessageMock.mockReset();
    useChatStore.getState().reset();
    useChatStore.setState({ currentThreadId: 'thread-A' });
    vi.mocked(workspaceService.listMessages).mockResolvedValue({
      messages: [],
      has_more: false,
    } as never);
  });

  it('skips the final setMessages when the user switched threads mid-stream', async () => {
    // streamMessage resolves only when the test says so, after emitting tokens.
    let finishStream!: () => void;
    streamMessageMock.mockImplementation(
      (
        _req: unknown,
        callbacks: {
          onToken: (t: string) => void;
          onDone: (p?: unknown) => void;
        }
      ) =>
        new Promise<void>((resolve) => {
          callbacks.onToken('hello from thread A');
          callbacks.onDone({});
          finishStream = resolve;
        })
    );

    const params = makeParams();
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    let submitPromise!: Promise<void>;
    act(() => {
      submitPromise = result.current.handleSubmit('question for thread A');
    });

    // Simulate the sidebar switching to thread B while the stream is in flight
    // (page.tsx onSelect mutates the ref synchronously).
    useChatStore.setState({ currentThreadId: 'thread-B' });
    params.setMessages.mockClear(); // ignore the optimistic user-bubble write

    await act(async () => {
      finishStream();
      await submitPromise;
    });

    // The guard must prevent thread A's completion from writing into the
    // currently-displayed (thread B) message state.
    expect(params.setMessages).not.toHaveBeenCalled();

    // Completion still belongs to thread A. The hook must reconcile A's
    // canonical newest page even though A is no longer displayed, so a
    // switch back does not require a hard refresh to recover the turn.
    const { workspaceService } = await import('@/services/workspaceService');
    expect(workspaceService.listMessages).toHaveBeenCalledWith(
      'thread-A',
      expect.objectContaining({ order: 'desc' })
    );
  });

  it('still commits the final message when the thread did NOT change', async () => {
    streamMessageMock.mockImplementation(
      (
        _req: unknown,
        callbacks: {
          onToken: (t: string) => void;
          onDone: (p?: unknown) => void;
        }
      ) => {
        callbacks.onToken('answer');
        callbacks.onDone({});
        return Promise.resolve();
      }
    );

    const params = makeParams();
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    await act(async () => {
      await result.current.handleSubmit('question');
    });

    // Last setMessages call must contain the assistant answer.
    const calls = params.setMessages.mock.calls;
    const lastArg = calls[calls.length - 1][0] as ChatPageMessage[];
    expect(lastArg[lastArg.length - 1]).toMatchObject({
      role: 'assistant',
      content: 'answer',
    });
  });
});

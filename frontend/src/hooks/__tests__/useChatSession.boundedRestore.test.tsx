import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { APIErrorClass } from '@/types/api';
import type { ChatMessage, Thread } from '@/types/workspace';

const navigationMocks = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  threadId: null as string | null,
  isNew: false,
}));

const workspaceMocks = vi.hoisted(() => ({
  getOrCreateDefaultWorkspace: vi.fn(),
  getOrCreateDefaultConversation: vi.fn(),
  createConversation: vi.fn(),
  listWorkspaceThreads: vi.fn(),
  listConversations: vi.fn(),
  getThread: vi.fn(),
}));

const chatStoreMocks = vi.hoisted(() => {
  const state = {
    currentThreadId: null as string | null,
    messages: {} as Record<string, ChatMessage[]>,
    addMessageToStore: vi.fn(),
    loadMessages: vi.fn(),
    loadOlderMessages: vi.fn(),
    loadingThreadId: null as string | null,
    error: null as string | null,
    messagePagination: {} as Record<
      string,
      { hasMore: boolean; loadingOlder: boolean; loadedCount: number }
    >,
    setCurrentThread: vi.fn(),
    // useChatSession indexes every thread it fetches into the store so the
    // rail and page_context can read its project binding.
    registerThread: vi.fn(),
  };
  state.setCurrentThread.mockImplementation((threadId: string | null) => {
    state.currentThreadId = threadId;
  });
  const useStore = Object.assign(
    <T,>(selector: (store: typeof state) => T) => selector(state),
    {
      getState: () => state,
      setState: (partial: Partial<typeof state>) =>
        Object.assign(state, partial),
    }
  );
  return { state, useStore };
});

vi.mock('next/navigation', () => ({
  useRouter: () => ({
    push: navigationMocks.push,
    replace: navigationMocks.replace,
  }),
  useSearchParams: () => ({
    get: (key: string) => {
      if (key === 'thread') return navigationMocks.threadId;
      if (key === 'new') return navigationMocks.isNew ? '1' : null;
      return null;
    },
  }),
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: workspaceMocks,
}));

vi.mock('@/store/chat-store', () => ({
  useChatStore: chatStoreMocks.useStore,
}));

vi.mock('@/stores/authStore', () => ({
  useAuthStore: () => ({ isAuthenticated: true }),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

import { useChatSession } from '@/hooks/chat/useChatSession';

function makeThread(id: string, overrides: Partial<Thread> = {}): Thread {
  return {
    id,
    conversation_id: 'conv-1',
    title: id,
    status: 'active',
    last_message_at: '2026-07-14T12:00:00Z',
    message_count: 0,
    token_count: 0,
    created_at: '2026-07-14T12:00:00Z',
    updated_at: '2026-07-14T12:00:00Z',
    ...overrides,
  };
}

function makeMessage(index: number, threadId = 'thread-old'): ChatMessage {
  return {
    id: `message-${index}`,
    thread_id: threadId,
    content: `message ${index}`,
    role: index % 2 === 0 ? 'assistant' : 'user',
    token_count: 1,
    citations: [],
    attachments: [],
    created_at: new Date(Date.UTC(2026, 6, 14, 12, index)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 6, 14, 12, index)).toISOString(),
  };
}

function apiError(statusCode: number, message: string): APIErrorClass {
  return new APIErrorClass({
    message,
    status_code: statusCode,
    type: 'http_error',
  });
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

describe('useChatSession bounded restoration', () => {
  // Mutation checks (2026-09-12):
  // - Guard `src/hooks/chat/useChatSession.ts:338`. Removing the initial URL
  //   suppression replays the unavailable detail lookup twice:
  //   pnpm --dir frontend exec vitest run src/hooks/__tests__/useChatSession.boundedRestore.test.tsx -t "recovers an unavailable initial deep link" --reporter=dot
  // - Guard `src/hooks/chat/useChatSession.ts:712-722`. Removing selection
  //   ownership replaces a newer selection with the first-page fallback:
  //   pnpm --dir frontend exec vitest run src/hooks/__tests__/useChatSession.boundedRestore.test.tsx -t "keeps a newer selection" --reporter=dot
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    navigationMocks.threadId = null;
    navigationMocks.isNew = false;
    chatStoreMocks.state.currentThreadId = null;
    chatStoreMocks.state.setCurrentThread.mockImplementation(
      (threadId: string | null) => {
        chatStoreMocks.state.currentThreadId = threadId;
      }
    );
    chatStoreMocks.state.messages = {};
    chatStoreMocks.state.messagePagination = {};
    chatStoreMocks.state.loadingThreadId = null;
    chatStoreMocks.state.error = null;
    workspaceMocks.getOrCreateDefaultWorkspace.mockResolvedValue({
      id: 'workspace-1',
      name: 'Workspace',
    });
    workspaceMocks.getOrCreateDefaultConversation.mockResolvedValue({
      id: 'conv-1',
      title: 'New Chat',
    });
    workspaceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [],
      total: 0,
      page: 1,
      limit: 50,
      has_more: false,
    });
    workspaceMocks.listConversations.mockResolvedValue({ conversations: [] });
    chatStoreMocks.state.loadMessages.mockResolvedValue(undefined);
  });

  it('does not select the newest sidebar thread before an uncached URL target', async () => {
    navigationMocks.threadId = 'thread-old';
    workspaceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [makeThread('thread-newest')],
      total: 1,
      page: 1,
      limit: 50,
      has_more: false,
    });
    workspaceMocks.getThread.mockResolvedValue({
      ...makeThread('thread-old'),
      messages: [],
    });

    renderHook(() => useChatSession());

    await waitFor(() =>
      expect(workspaceMocks.getThread).toHaveBeenCalledWith('thread-old', {
        includeMessages: false,
      })
    );
    expect(chatStoreMocks.state.setCurrentThread).not.toHaveBeenCalledWith(
      'thread-newest'
    );
  });

  it('restores an uncached URL target when the sidebar page is empty', async () => {
    navigationMocks.threadId = 'thread-old';
    workspaceMocks.getThread.mockResolvedValue({
      ...makeThread('thread-old'),
      messages: [],
    });

    renderHook(() => useChatSession());

    await waitFor(() =>
      expect(workspaceMocks.getThread).toHaveBeenCalledWith('thread-old', {
        includeMessages: false,
      })
    );
  });

  it('lets the URL target override a different persisted warm thread', async () => {
    localStorage.setItem('default-conversation-id', 'conv-1');
    navigationMocks.threadId = 'thread-deep-link';
    chatStoreMocks.state.currentThreadId = 'thread-persisted';
    workspaceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [makeThread('thread-persisted')],
      total: 1,
      page: 1,
      limit: 50,
      has_more: false,
    });
    workspaceMocks.getThread.mockResolvedValue({
      ...makeThread('thread-deep-link'),
      messages: [],
    });

    renderHook(() => useChatSession());

    await waitFor(() =>
      expect(workspaceMocks.getThread).toHaveBeenCalledWith(
        'thread-deep-link',
        { includeMessages: false }
      )
    );
    expect(workspaceMocks.getThread).toHaveBeenCalledTimes(1);
    expect(chatStoreMocks.state.loadMessages).toHaveBeenCalledTimes(1);
    expect(chatStoreMocks.state.loadMessages).toHaveBeenCalledWith(
      'thread-deep-link'
    );
    expect(workspaceMocks.getThread).not.toHaveBeenCalledWith(
      'thread-persisted',
      expect.anything()
    );
  });

  it('restores only the bounded store page for a warm thread', async () => {
    localStorage.setItem('default-conversation-id', 'conv-1');
    chatStoreMocks.state.currentThreadId = 'thread-old';
    workspaceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [makeThread('thread-old', { message_count: 1000 })],
      total: 1,
      page: 1,
      limit: 50,
      has_more: false,
    });
    chatStoreMocks.state.loadMessages.mockImplementation(async () => {
      chatStoreMocks.state.messages['thread-old'] = Array.from(
        { length: 50 },
        (_, index) => makeMessage(index)
      );
      chatStoreMocks.state.messagePagination['thread-old'] = {
        hasMore: true,
        loadingOlder: false,
        loadedCount: 50,
      };
    });
    workspaceMocks.getThread.mockResolvedValue({
      ...makeThread('thread-old', { message_count: 1000 }),
      messages: Array.from({ length: 1000 }, (_, index) => makeMessage(index)),
    });

    const { result } = renderHook(() => useChatSession());

    await waitFor(() => expect(result.current.isInitializing).toBe(false));
    expect(workspaceMocks.getThread).not.toHaveBeenCalled();
    expect(chatStoreMocks.state.loadMessages).toHaveBeenCalledWith(
      'thread-old'
    );
    expect(result.current.displayedMessages).toHaveLength(50);
    expect(result.current.messagePagination?.['thread-old']).toMatchObject({
      hasMore: true,
      loadedCount: 50,
    });
    expect(navigationMocks.replace).toHaveBeenCalledWith(
      '/chat?thread=thread-old'
    );
    expect(result.current.initError).toBeNull();
  });

  it('falls back to the first page when a persisted thread was deleted', async () => {
    chatStoreMocks.state.currentThreadId = 'thread-deleted';
    workspaceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [makeThread('thread-newest')],
      total: 1,
      page: 1,
      limit: 50,
      has_more: false,
    });
    workspaceMocks.getThread.mockRejectedValue(
      apiError(404, 'Thread not found')
    );

    const { result } = renderHook(() => useChatSession());

    await waitFor(() => expect(result.current.isInitializing).toBe(false));
    expect(result.current.initError).toBeNull();
    expect(chatStoreMocks.state.currentThreadId).toBe('thread-newest');
    expect(navigationMocks.replace).toHaveBeenCalledWith(
      '/chat?thread=thread-newest'
    );
  });

  it('opens a new chat when an inaccessible persisted thread has no fallback', async () => {
    chatStoreMocks.state.currentThreadId = 'thread-revoked';
    workspaceMocks.getThread.mockRejectedValue(
      apiError(403, 'Thread access revoked')
    );

    const { result } = renderHook(() => useChatSession());

    await waitFor(() => expect(result.current.isInitializing).toBe(false));
    expect(result.current.initError).toBeNull();
    expect(chatStoreMocks.state.currentThreadId).toBeNull();
    expect(navigationMocks.replace).toHaveBeenCalledWith('/chat');
  });

  it('recovers an unavailable initial deep link without replaying its lookup', async () => {
    navigationMocks.threadId = 'thread-deleted';
    workspaceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [makeThread('thread-newest')],
      total: 1,
      page: 1,
      limit: 50,
      has_more: false,
    });
    workspaceMocks.getThread.mockRejectedValue(
      apiError(404, 'Thread not found')
    );

    const { result } = renderHook(() => useChatSession());

    await waitFor(() => expect(result.current.isInitializing).toBe(false));
    expect(result.current.initError).toBeNull();
    expect(chatStoreMocks.state.currentThreadId).toBe('thread-newest');
    expect(workspaceMocks.getThread).toHaveBeenCalledTimes(1);
    expect(navigationMocks.replace).toHaveBeenCalledWith(
      '/chat?thread=thread-newest'
    );
  });

  it('keeps a newer selection when stale restore metadata fails late', async () => {
    chatStoreMocks.state.currentThreadId = 'thread-deleted';
    workspaceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [makeThread('thread-newest')],
      total: 1,
      page: 1,
      limit: 50,
      has_more: false,
    });
    const detail = deferred<Thread>();
    workspaceMocks.getThread.mockReturnValue(detail.promise);

    const { result, rerender } = renderHook(() => useChatSession());
    await waitFor(() => expect(workspaceMocks.getThread).toHaveBeenCalled());

    act(() => {
      chatStoreMocks.state.currentThreadId = 'thread-newer-selection';
      rerender();
    });
    await act(async () => {
      detail.reject(apiError(404, 'Thread not found'));
      await Promise.resolve();
    });

    await waitFor(() => expect(result.current.isInitializing).toBe(false));
    expect(result.current.initError).toBeNull();
    expect(chatStoreMocks.state.currentThreadId).toBe('thread-newer-selection');
    expect(navigationMocks.replace).not.toHaveBeenCalled();
  });

  it('does not wipe a newer turn when an initial deep link fails late', async () => {
    navigationMocks.threadId = 'thread-deleted';
    workspaceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [makeThread('thread-newest')],
      total: 1,
      page: 1,
      limit: 50,
      has_more: false,
    });
    const detail = deferred<Thread>();
    workspaceMocks.getThread.mockReturnValue(detail.promise);

    const { result, rerender } = renderHook(() => useChatSession());
    await waitFor(() => expect(workspaceMocks.getThread).toHaveBeenCalled());

    act(() => {
      result.current.setConversations([
        {
          id: 'thread-newer-selection',
          title: 'New turn',
          messages: [],
        } as never,
      ]);
      result.current.setMessages([
        {
          runtimeId: 'runtime-new-turn',
          source: 'optimistic',
          role: 'user',
          content: 'new turn',
          timestamp: 2,
        },
      ]);
    });
    act(() => {
      chatStoreMocks.state.currentThreadId = 'thread-newer-selection';
      rerender();
    });
    await act(async () => {
      detail.reject(apiError(404, 'Thread not found'));
      await Promise.resolve();
    });

    await waitFor(() => expect(result.current.isInitializing).toBe(false));
    expect(result.current.initError).toBeNull();
    expect(chatStoreMocks.state.currentThreadId).toBe('thread-newer-selection');
    expect(result.current.messages.map(({ content }) => content)).toEqual([
      'new turn',
    ]);
    expect(navigationMocks.replace).not.toHaveBeenCalled();
  });

  it('still surfaces a transient persisted-thread lookup failure', async () => {
    chatStoreMocks.state.currentThreadId = 'thread-off-page';
    workspaceMocks.getThread.mockRejectedValue(
      new Error('thread lookup unavailable')
    );

    const { result } = renderHook(() => useChatSession());

    await waitFor(() => expect(result.current.isInitializing).toBe(false));
    expect(result.current.initError).toContain('thread lookup unavailable');
    expect(chatStoreMocks.state.currentThreadId).toBe('thread-off-page');
  });

  it('restores a valid persisted thread outside the first page', async () => {
    chatStoreMocks.state.currentThreadId = 'thread-off-page';
    workspaceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [makeThread('thread-newest')],
      total: 2,
      page: 1,
      limit: 50,
      has_more: true,
    });
    workspaceMocks.getThread.mockResolvedValue(makeThread('thread-off-page'));

    const { result } = renderHook(() => useChatSession());

    await waitFor(() => expect(result.current.isInitializing).toBe(false));
    expect(result.current.initError).toBeNull();
    expect(chatStoreMocks.state.currentThreadId).toBe('thread-off-page');
    expect(result.current.conversations.map(({ id }) => id)).toEqual([
      'thread-off-page',
      'thread-newest',
    ]);
    expect(navigationMocks.replace).toHaveBeenCalledWith(
      '/chat?thread=thread-off-page'
    );
  });
});

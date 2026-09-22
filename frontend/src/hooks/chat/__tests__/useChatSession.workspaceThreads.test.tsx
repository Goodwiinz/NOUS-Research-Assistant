import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  ChatMessage,
  Thread,
  ThreadListResponse,
} from '@/types/workspace';

const navigationMocks = vi.hoisted(() => ({
  replace: vi.fn(),
  threadId: null as string | null,
  isNewChat: false,
}));

const authMocks = vi.hoisted(() => ({ isAuthenticated: true }));

const workspaceMocks = vi.hoisted(() => ({
  getOrCreateDefaultWorkspace: vi.fn(),
  getOrCreateDefaultConversation: vi.fn(),
  createConversation: vi.fn(),
  listWorkspaceThreads: vi.fn(),
  listThreads: vi.fn(),
  getThread: vi.fn(),
}));

const chatStoreMocks = vi.hoisted(() => {
  const state = {
    currentThreadId: null as string | null,
    messages: {} as Record<string, ChatMessage[]>,
    messageFreshness: {} as Record<string, string>,
    messagePagination: {} as Record<string, never>,
    loadingThreadId: null as string | null,
    messageLoadError: null,
    addMessageToStore: vi.fn(),
    loadMessages: vi.fn().mockResolvedValue(undefined),
    loadOlderMessages: vi.fn(),
    setCurrentThread: vi.fn(),
    registerThread: vi.fn(),
  };
  state.setCurrentThread.mockImplementation((threadId: string | null) => {
    state.currentThreadId = threadId;
  });
  const useStore = Object.assign(
    <T,>(selector: (store: typeof state) => T) => selector(state),
    { getState: () => state }
  );
  return { state, useStore };
});

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: navigationMocks.replace }),
  useSearchParams: () => ({
    get: (key: string) => {
      if (key === 'thread') return navigationMocks.threadId;
      if (key === 'new') return navigationMocks.isNewChat ? '1' : null;
      return null;
    },
  }),
}));

vi.mock('@/services/workspaceService', () => ({
  clearWorkspaceServiceCache: vi.fn(),
  workspaceService: workspaceMocks,
}));

vi.mock('@/store/chat-store', () => ({
  useChatStore: chatStoreMocks.useStore,
}));

vi.mock('@/stores/authStore', () => ({
  useAuthStore: () => ({ isAuthenticated: authMocks.isAuthenticated }),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

import { useChatSession } from '@/hooks/chat/useChatSession';

function thread(id: string, conversationId: string, title = id): Thread {
  return {
    id,
    conversation_id: conversationId,
    title,
    status: 'active',
    last_message_at: '2026-09-12T12:00:00Z',
    last_message_preview: `${title} preview`,
    message_count: 2,
    token_count: 10,
    created_at: '2026-09-12T11:00:00Z',
    updated_at: '2026-09-12T12:00:00Z',
  };
}

function page(
  threads: Thread[],
  pageNumber = 1,
  hasMore = false
): ThreadListResponse {
  return {
    threads,
    total: threads.length,
    page: pageNumber,
    limit: 50,
    has_more: hasMore,
  };
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

describe('useChatSession workspace-wide thread pages', () => {
  // Mutation checks (2026-09-12):
  // - Guard `src/hooks/chat/useChatSession.ts:453-456`. Neutralizing the
  //   first-page workspace/generation condition fails with thread-old
  //   appended:
  //   pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatSession.workspaceThreads.test.tsx -t "rejects a late first page" --reporter=dot
  // - Guard `src/hooks/chat/useChatSession.ts:581-599`. Replacing the
  //   pagination ID upsert with a plain append fails with duplicate thread-b:
  //   pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatSession.workspaceThreads.test.tsx -t "upserts an overlapping" --reporter=dot
  // - Guard `src/hooks/chat/useChatSession.ts:498-501`. Forcing selection
  //   ownership true fails because the late ?new=1 page clears thread-created:
  //   pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatSession.workspaceThreads.test.tsx -t "preserves a newly created active row" --reporter=dot
  // - Guard `src/hooks/chat/useChatSession.ts:708`. Removing initialization
  //   ownership lets an unavailable detail from the old workspace navigate
  //   the freshly initialized workspace:
  //   pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatSession.workspaceThreads.test.tsx -t "ignores unavailable detail" --reporter=dot
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    authMocks.isAuthenticated = true;
    navigationMocks.threadId = null;
    navigationMocks.isNewChat = false;
    chatStoreMocks.state.currentThreadId = null;
    chatStoreMocks.state.setCurrentThread.mockImplementation(
      (threadId: string | null) => {
        chatStoreMocks.state.currentThreadId = threadId;
      }
    );
    chatStoreMocks.state.messages = {};
    chatStoreMocks.state.messageFreshness = {};
    chatStoreMocks.state.messagePagination = {};
    chatStoreMocks.state.loadingThreadId = null;
    workspaceMocks.getOrCreateDefaultWorkspace.mockResolvedValue({
      id: 'ws-1',
      name: 'Workspace',
    });
    workspaceMocks.getOrCreateDefaultConversation.mockResolvedValue({
      id: 'conv-default',
      workspace_id: 'ws-1',
      title: 'Default',
    });
    workspaceMocks.listWorkspaceThreads.mockResolvedValue(page([]));
  });

  it('loads one sidebar page across conversations and keeps each real parent id', async () => {
    workspaceMocks.listWorkspaceThreads.mockResolvedValue(
      page([
        thread('thread-new', 'conv-new'),
        thread('thread-old', 'conv-historical'),
      ])
    );

    const { result } = renderHook(() => useChatSession());

    await waitFor(() => expect(result.current.isInitializing).toBe(false));
    expect(workspaceMocks.listWorkspaceThreads).toHaveBeenCalledWith('ws-1', {
      page: 1,
      limit: 50,
    });
    expect(workspaceMocks.listThreads).not.toHaveBeenCalled();
    expect(
      result.current.conversations.map((item) => item.conversationId)
    ).toEqual(['conv-new', 'conv-historical']);
  });

  it('does not let a persisted conversation id scope a warm restore', async () => {
    localStorage.setItem('default-conversation-id', 'conv-stale');
    chatStoreMocks.state.currentThreadId = 'thread-old';
    workspaceMocks.listWorkspaceThreads.mockResolvedValue(
      page([thread('thread-old', 'conv-real')])
    );

    const { result } = renderHook(() => useChatSession());

    await waitFor(() => expect(result.current.isInitializing).toBe(false));
    expect(workspaceMocks.listWorkspaceThreads).toHaveBeenCalledWith('ws-1', {
      page: 1,
      limit: 50,
    });
    expect(workspaceMocks.listThreads).not.toHaveBeenCalled();
    expect(result.current.conversations[0].conversationId).toBe('conv-real');
  });

  it('upserts an overlapping next page by thread id without duplicating rows', async () => {
    authMocks.isAuthenticated = false;
    workspaceMocks.listWorkspaceThreads
      .mockResolvedValueOnce(
        page(
          [
            thread('thread-a', 'conv-a', 'A old'),
            thread('thread-b', 'conv-b', 'B old'),
          ],
          1,
          true
        )
      )
      .mockResolvedValueOnce(
        page(
          [
            thread('thread-b', 'conv-b', 'B updated'),
            thread('thread-c', 'conv-c', 'C'),
          ],
          2,
          false
        )
      );
    const { result } = renderHook(() => useChatSession());

    await act(async () => {
      await result.current.loadThreadsFromDb('ws-1');
      await result.current.loadMoreThreads();
    });

    expect(result.current.conversations.map((item) => item.id)).toEqual([
      'thread-a',
      'thread-b',
      'thread-c',
    ]);
    expect(result.current.conversations[1].title).toBe('B updated');
    expect(
      new Set(result.current.conversations.map((item) => item.id))
    ).toHaveLength(3);
  });

  it('rejects a late first page from a previously requested workspace', async () => {
    authMocks.isAuthenticated = false;
    const oldPage = deferred<ThreadListResponse>();
    const newPage = deferred<ThreadListResponse>();
    workspaceMocks.listWorkspaceThreads.mockImplementation(
      (workspaceId: string) =>
        workspaceId === 'ws-old' ? oldPage.promise : newPage.promise
    );
    const { result } = renderHook(() => useChatSession());

    let oldRequest!: Promise<{ ok: boolean; threadCount: number }>;
    let newRequest!: Promise<{ ok: boolean; threadCount: number }>;
    act(() => {
      oldRequest = result.current.loadThreadsFromDb('ws-old');
      newRequest = result.current.loadThreadsFromDb('ws-new');
    });
    await act(async () => {
      newPage.resolve(page([thread('thread-new', 'conv-new')]));
      await newRequest;
      oldPage.resolve(page([thread('thread-old', 'conv-old')]));
      await oldRequest;
    });

    expect(result.current.conversations.map((item) => item.id)).toEqual([
      'thread-new',
    ]);
  });

  it('preserves a newly created active row when the pending first page resolves', async () => {
    authMocks.isAuthenticated = false;
    navigationMocks.isNewChat = true;
    const pendingPage = deferred<ThreadListResponse>();
    workspaceMocks.listWorkspaceThreads.mockReturnValue(pendingPage.promise);
    const { result } = renderHook(() => useChatSession());

    let loadPromise!: Promise<{ ok: boolean; threadCount: number }>;
    act(() => {
      loadPromise = result.current.loadThreadsFromDb('ws-1');
    });
    await waitFor(() =>
      expect(workspaceMocks.listWorkspaceThreads).toHaveBeenCalled()
    );

    act(() => {
      result.current.setConversations((previous) => [
        {
          id: 'thread-created',
          title: 'Just created',
          messages: [],
          threadId: 'thread-created',
          conversationId: 'conv-created',
        },
        ...previous,
      ]);
      chatStoreMocks.state.currentThreadId = 'thread-created';
    });

    await act(async () => {
      pendingPage.resolve(page([thread('thread-server', 'conv-server')]));
      await loadPromise;
    });

    expect(result.current.conversations.map(({ id }) => id)).toEqual([
      'thread-created',
      'thread-server',
    ]);
    expect(result.current.conversations[0]?.conversationId).toBe(
      'conv-created'
    );
    expect(chatStoreMocks.state.currentThreadId).toBe('thread-created');
  });

  it('invalidates an old workspace load on logout and starts a fresh login load', async () => {
    const oldPage = deferred<ThreadListResponse>();
    workspaceMocks.getOrCreateDefaultWorkspace
      .mockResolvedValueOnce({ id: 'ws-old', name: 'Old workspace' })
      .mockResolvedValueOnce({ id: 'ws-new', name: 'New workspace' });
    workspaceMocks.listWorkspaceThreads.mockImplementation((workspaceId) =>
      workspaceId === 'ws-old'
        ? oldPage.promise
        : Promise.resolve(page([thread('thread-new', 'conv-new')]))
    );

    const { result, rerender } = renderHook(() => useChatSession());
    await waitFor(() =>
      expect(workspaceMocks.listWorkspaceThreads).toHaveBeenCalledWith(
        'ws-old',
        { page: 1, limit: 50 }
      )
    );

    authMocks.isAuthenticated = false;
    rerender();
    authMocks.isAuthenticated = true;
    rerender();

    await waitFor(() =>
      expect(workspaceMocks.listWorkspaceThreads).toHaveBeenCalledWith(
        'ws-new',
        { page: 1, limit: 50 }
      )
    );
    await waitFor(() =>
      expect(result.current.conversations.map(({ id }) => id)).toEqual([
        'thread-new',
      ])
    );

    await act(async () => {
      oldPage.resolve(page([thread('thread-old', 'conv-old')]));
      await Promise.resolve();
    });

    expect(result.current.conversations.map(({ id }) => id)).toEqual([
      'thread-new',
    ]);
    expect(chatStoreMocks.state.registerThread).not.toHaveBeenCalledWith(
      expect.objectContaining({ id: 'thread-old' })
    );
  });

  it('ignores unavailable detail from an old workspace generation', async () => {
    navigationMocks.threadId = 'thread-old';
    const oldDetail = deferred<Thread>();
    workspaceMocks.getOrCreateDefaultWorkspace
      .mockResolvedValueOnce({ id: 'ws-old', name: 'Old workspace' })
      .mockResolvedValueOnce({ id: 'ws-new', name: 'New workspace' });
    workspaceMocks.listWorkspaceThreads.mockResolvedValue(page([]));
    workspaceMocks.getThread.mockReturnValue(oldDetail.promise);

    const { result, rerender } = renderHook(() => useChatSession());
    await waitFor(() =>
      expect(workspaceMocks.getThread).toHaveBeenCalledWith('thread-old', {
        includeMessages: false,
      })
    );

    authMocks.isAuthenticated = false;
    rerender();
    navigationMocks.threadId = null;
    authMocks.isAuthenticated = true;
    rerender();

    await waitFor(() => expect(result.current.workspace?.id).toBe('ws-new'));
    await act(async () => {
      oldDetail.reject({ error: { status_code: 404 } });
      await Promise.resolve();
    });

    expect(result.current.initError).toBeNull();
    expect(result.current.conversations).toEqual([]);
    expect(chatStoreMocks.state.currentThreadId).toBeNull();
    expect(navigationMocks.replace).not.toHaveBeenCalledWith('/chat');
    expect(chatStoreMocks.state.registerThread).not.toHaveBeenCalledWith(
      expect.objectContaining({ id: 'thread-old' })
    );
  });

  it('keeps page one when restoring a deep-linked thread outside that page', async () => {
    navigationMocks.threadId = 'thread-deep';
    workspaceMocks.listWorkspaceThreads.mockResolvedValue(
      page([thread('thread-page-one', 'conv-a')])
    );
    workspaceMocks.getThread.mockResolvedValue({
      ...thread('thread-deep', 'conv-b'),
      messages: [],
    });

    const { result } = renderHook(() => useChatSession());

    await waitFor(() =>
      expect(result.current.conversations.map((item) => item.id)).toEqual([
        'thread-deep',
        'thread-page-one',
      ])
    );
    expect(result.current.conversations[0].conversationId).toBe('conv-b');
  });

  it('surfaces a workspace-list failure without creating a conversation', async () => {
    workspaceMocks.listWorkspaceThreads.mockRejectedValue(
      new Error('workspace list unavailable')
    );

    const { result } = renderHook(() => useChatSession());

    await waitFor(() => expect(result.current.isInitializing).toBe(false));
    expect(result.current.initError).toContain('workspace list unavailable');
    expect(workspaceMocks.createConversation).not.toHaveBeenCalled();
    expect(
      workspaceMocks.getOrCreateDefaultConversation
    ).not.toHaveBeenCalled();
  });
});

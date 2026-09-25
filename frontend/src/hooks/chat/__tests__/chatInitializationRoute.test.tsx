import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const serviceMocks = vi.hoisted(() => ({
  getOrCreateDefaultWorkspace: vi.fn(),
  getOrCreateDefaultConversation: vi.fn(),
  listConversations: vi.fn(),
  listThreads: vi.fn(),
  listWorkspaceThreads: vi.fn(),
  listMessages: vi.fn(),
  getThread: vi.fn(),
}));

const navigationMocks = vi.hoisted(() => ({
  replace: vi.fn(),
  push: vi.fn(),
}));

vi.mock('@/services/workspaceService', () => ({
  clearWorkspaceServiceCache: vi.fn(),
  workspaceService: serviceMocks,
}));

vi.mock('next/navigation', () => ({
  useRouter: () => navigationMocks,
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

import { useChatSession } from '@/hooks/chat/useChatSession';
import { useChatPersistence } from '@/hooks/useChatPersistence';
import { useChatStore } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';

const workspace = { id: 'workspace-1', name: 'Research' } as never;
const conversation = {
  id: 'conversation-1',
  workspace_id: 'workspace-1',
  title: 'Chat',
} as never;

function thread(
  id: string,
  conversationId = 'conversation-1'
): {
  id: string;
  conversation_id: string;
  title: string;
  status: string;
  last_message_at: string;
  last_message_preview: string;
  message_count: number;
  token_count: number;
  created_at: string;
  updated_at: string;
} {
  return {
    id,
    conversation_id: conversationId,
    title: id,
    status: 'active',
    last_message_at: '2026-09-12T12:00:00Z',
    last_message_preview: `${id} preview`,
    message_count: 0,
    token_count: 0,
    created_at: '2026-09-12T11:00:00Z',
    updated_at: '2026-09-12T12:00:00Z',
  };
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('chat initialization route ownership', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/chat');
    useAuthStore.setState({ isAuthenticated: false });
    const signedOut = renderHook(() => useChatPersistence());
    signedOut.unmount();
    useChatStore.getState().reset();
    useAuthStore.setState({ isAuthenticated: true });

    serviceMocks.getOrCreateDefaultWorkspace.mockResolvedValue(workspace);
    serviceMocks.getOrCreateDefaultConversation.mockResolvedValue(conversation);
    serviceMocks.listConversations.mockResolvedValue({
      conversations: [conversation],
      total: 1,
      page: 1,
      limit: 20,
    });
    serviceMocks.listThreads.mockResolvedValue({
      threads: [thread('thread-old')],
      total: 1,
      page: 1,
      limit: 20,
    });
    serviceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [thread('thread-new', 'conversation-new')],
      total: 1,
      page: 1,
      limit: 50,
      has_more: false,
    });
    serviceMocks.listMessages.mockResolvedValue({
      messages: [],
      has_more: false,
    });
    serviceMocks.getThread.mockResolvedValue(thread('thread-old'));
  });

  afterEach(() => {
    useAuthStore.setState({ isAuthenticated: false });
    useChatStore.getState().reset();
    window.history.replaceState({}, '', '/');
  });

  it('routes to the workspace-wide first thread when layout hydration resolves first', async () => {
    window.history.replaceState(
      { __NA: true, __PRIVATE_NEXTJS_INTERNALS_TREE: ['chat'] },
      '',
      '/chat'
    );
    const replaceStateSpy = vi.spyOn(window.history, 'replaceState');
    const layout = renderHook(() => useChatPersistence());
    await waitFor(() =>
      expect(serviceMocks.listThreads).toHaveBeenCalledWith('conversation-1')
    );
    const chat = renderHook(() => useChatSession());

    await waitFor(() => expect(chat.result.current.isInitializing).toBe(false));

    expect(useChatStore.getState().currentThreadId).toBe('thread-new');
    expect(window.location.search).toBe('?thread=thread-new');
    expect(replaceStateSpy).toHaveBeenCalledWith(
      {},
      '',
      '/chat?thread=thread-new'
    );
    expect(navigationMocks.replace).not.toHaveBeenCalled();
    replaceStateSpy.mockRestore();
    layout.unmount();
  });

  it('keeps a new-chat intent when an earlier default-route sync finishes late', async () => {
    const pendingRouteReplacements: string[] = [];
    navigationMocks.replace.mockImplementation((url: string) => {
      pendingRouteReplacements.push(url);
    });
    const chat = renderHook(() => useChatSession());

    await waitFor(() => expect(chat.result.current.isInitializing).toBe(false));
    expect(useChatStore.getState().currentThreadId).toBe('thread-new');

    act(() => {
      useChatStore.getState().setCurrentThread(null);
      window.history.pushState({}, '', '/chat?new=1');
    });
    for (const url of pendingRouteReplacements) {
      window.history.replaceState({}, '', url);
    }

    expect(window.location.search).toBe('?new=1');
  });

  it('waits for layout hydration before requesting workspace threads', async () => {
    const conversationRead = deferred<never>();
    serviceMocks.listConversations.mockReturnValue(conversationRead.promise);
    const chat = renderHook(() => useChatSession());
    const layout = renderHook(() => useChatPersistence());

    await waitFor(() =>
      expect(serviceMocks.listConversations).toHaveBeenCalledOnce()
    );
    expect(serviceMocks.listWorkspaceThreads).not.toHaveBeenCalled();

    await act(async () => {
      conversationRead.resolve({
        conversations: [conversation],
        total: 1,
        page: 1,
        limit: 20,
      } as never);
      await conversationRead.promise;
    });

    await waitFor(() => expect(chat.result.current.isInitializing).toBe(false));
    expect(serviceMocks.listWorkspaceThreads).toHaveBeenCalledWith(
      'workspace-1',
      { page: 1, limit: 50 }
    );
    expect(useChatStore.getState().currentThreadId).toBe('thread-new');
    expect(window.location.search).toBe('?thread=thread-new');
    expect(navigationMocks.replace).not.toHaveBeenCalled();
    layout.unmount();
  });

  it('keeps a sidebar selection made during bare-route hydration', async () => {
    const conversationRead = deferred<never>();
    serviceMocks.listConversations.mockReturnValue(conversationRead.promise);
    serviceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [
        thread('thread-new', 'conversation-new'),
        thread('thread-selected'),
      ],
      total: 2,
      page: 1,
      limit: 50,
      has_more: false,
    });
    const chat = renderHook(() => useChatSession());
    await waitFor(() =>
      expect(serviceMocks.listConversations).toHaveBeenCalledOnce()
    );

    act(() => useChatStore.getState().setCurrentThread('thread-selected'));
    await act(async () => {
      conversationRead.resolve({
        conversations: [conversation],
        total: 1,
        page: 1,
        limit: 20,
      } as never);
      await conversationRead.promise;
    });

    await waitFor(() => expect(chat.result.current.isInitializing).toBe(false));
    expect(useChatStore.getState().currentThreadId).toBe('thread-selected');
    expect(window.location.search).toBe('?thread=thread-selected');
    expect(navigationMocks.replace).not.toHaveBeenCalled();
  });

  it('keeps explicit new-chat intent despite cached threads', async () => {
    window.history.replaceState({}, '', '/chat?new=1');
    const chat = renderHook(() => useChatSession());

    await waitFor(() => expect(chat.result.current.isInitializing).toBe(false));

    expect(useChatStore.getState().currentThreadId).toBeNull();
    expect(navigationMocks.replace).not.toHaveBeenCalled();
  });

  it('keeps a deep link outside the first workspace page', async () => {
    window.history.replaceState({}, '', '/chat?thread=thread-deep');
    serviceMocks.getThread.mockResolvedValue(thread('thread-deep'));
    const chat = renderHook(() => useChatSession());

    await waitFor(() => expect(chat.result.current.isInitializing).toBe(false));

    expect(useChatStore.getState().currentThreadId).toBe('thread-deep');
    expect(navigationMocks.replace).not.toHaveBeenCalled();
  });

  it('leaves an empty workspace on bare chat without a route replacement', async () => {
    serviceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [],
      total: 0,
      page: 1,
      limit: 50,
      has_more: false,
    });
    const chat = renderHook(() => useChatSession());

    await waitFor(() => expect(chat.result.current.isInitializing).toBe(false));

    expect(useChatStore.getState().currentThreadId).toBeNull();
    expect(navigationMocks.replace).not.toHaveBeenCalled();
  });

  it('surfaces a failed layout hydration before requesting workspace threads', async () => {
    serviceMocks.listConversations.mockRejectedValue(
      new Error('conversation read unavailable')
    );
    const chat = renderHook(() => useChatSession());

    await waitFor(() => expect(chat.result.current.isInitializing).toBe(false));

    expect(chat.result.current.initError).toContain(
      'Failed to load conversations'
    );
    expect(serviceMocks.listWorkspaceThreads).not.toHaveBeenCalled();
    expect(navigationMocks.replace).not.toHaveBeenCalled();
  });

  it('restores chat after an unrelated store error on a warm remount', async () => {
    const first = renderHook(() => useChatSession());
    await waitFor(() =>
      expect(first.result.current.isInitializing).toBe(false)
    );
    expect(first.result.current.initError).toBeNull();
    first.unmount();

    act(() => useChatStore.setState({ error: 'Failed to delete thread' }));
    const second = renderHook(() => useChatSession());
    await waitFor(() =>
      expect(second.result.current.isInitializing).toBe(false)
    );

    expect(second.result.current.initError).toBeNull();
    expect(serviceMocks.listWorkspaceThreads).toHaveBeenCalledTimes(2);
  });
});

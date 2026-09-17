import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const serviceMocks = vi.hoisted(() => ({
  getOrCreateDefaultWorkspace: vi.fn(),
  getOrCreateDefaultConversation: vi.fn(),
  listConversations: vi.fn(),
  listThreads: vi.fn(),
  listMessages: vi.fn(),
}));

const toastErrorMock = vi.hoisted(() => vi.fn());

vi.mock('@/services/workspaceService', () => ({
  clearWorkspaceServiceCache: vi.fn(),
  workspaceService: serviceMocks,
}));

vi.mock('react-hot-toast', () => ({
  default: { error: toastErrorMock, success: vi.fn() },
}));

import { useChatPersistence } from '@/hooks/useChatPersistence';
import { useChatStore } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const workspace = { id: 'workspace-1', name: 'Research' } as never;
const conversation = {
  id: 'conversation-1',
  workspace_id: 'workspace-1',
  title: 'Chat',
} as never;
const thread = {
  id: 'thread-1',
  conversation_id: 'conversation-1',
  title: 'Opening thread',
} as never;

const conversationPage = (conversations: unknown[]): never =>
  ({ conversations, total: conversations.length, page: 1, limit: 20 }) as never;
const threadPage = (threads: unknown[]): never =>
  ({ threads, total: threads.length, page: 1, limit: 20 }) as never;

describe('useChatPersistence initialization', () => {
  const originalActions = {
    initializeDefaultWorkspace:
      useChatStore.getState().initializeDefaultWorkspace,
    loadConversations: useChatStore.getState().loadConversations,
    loadThreads: useChatStore.getState().loadThreads,
    setCurrentWorkspace: useChatStore.getState().setCurrentWorkspace,
    setCurrentConversation: useChatStore.getState().setCurrentConversation,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/');
    useChatStore.setState(originalActions);
    useChatStore.getState().reset();

    // Exercise the hook's real signed-out reset path so its module-level
    // shared initialization promise cannot leak between tests.
    useAuthStore.setState({ isAuthenticated: false });
    const signedOut = renderHook(() => useChatPersistence());
    signedOut.unmount();
    useAuthStore.setState({ isAuthenticated: true });

    serviceMocks.getOrCreateDefaultWorkspace.mockResolvedValue(workspace);
    serviceMocks.getOrCreateDefaultConversation.mockResolvedValue(conversation);
    serviceMocks.listConversations.mockResolvedValue(
      conversationPage([conversation])
    );
    serviceMocks.listThreads.mockResolvedValue(threadPage([]));
    serviceMocks.listMessages.mockResolvedValue({
      messages: [],
      has_more: false,
    });
  });

  afterEach(() => {
    window.history.replaceState({}, '', '/');
    useAuthStore.setState({ isAuthenticated: false });
    useChatStore.setState(originalActions);
    useChatStore.getState().reset();
    vi.restoreAllMocks();
  });

  it('shares one deferred conversation read and one deferred thread read across two consumers', async () => {
    const conversations = deferred<never>();
    const threads = deferred<never>();
    serviceMocks.listConversations.mockReturnValue(conversations.promise);
    serviceMocks.listThreads.mockReturnValue(threads.promise);

    const first = renderHook(() => useChatPersistence());
    const second = renderHook(() => useChatPersistence());
    let firstSettled = false;
    let secondSettled = false;
    let firstWaiter!: Promise<void>;
    let secondWaiter!: Promise<void>;
    act(() => {
      firstWaiter = first.result.current.initialize().then(() => {
        firstSettled = true;
      });
      secondWaiter = second.result.current.initialize().then(() => {
        secondSettled = true;
      });
    });

    await waitFor(() =>
      expect(serviceMocks.getOrCreateDefaultWorkspace).toHaveBeenCalledOnce()
    );
    expect(serviceMocks.listConversations).toHaveBeenCalledOnce();
    expect(serviceMocks.listConversations).toHaveBeenCalledWith('workspace-1');
    expect(serviceMocks.listThreads).not.toHaveBeenCalled();
    expect(firstSettled).toBe(false);
    expect(secondSettled).toBe(false);

    await act(async () => {
      conversations.resolve(conversationPage([conversation]));
      await Promise.resolve();
    });

    await waitFor(() =>
      expect(serviceMocks.listThreads).toHaveBeenCalledOnce()
    );
    expect(serviceMocks.listThreads).toHaveBeenCalledWith('conversation-1');
    expect(firstSettled).toBe(false);
    expect(secondSettled).toBe(false);

    await act(async () => {
      threads.resolve(threadPage([thread]));
      await Promise.all([firstWaiter, secondWaiter]);
    });

    expect(firstSettled).toBe(true);
    expect(secondSettled).toBe(true);
    expect(serviceMocks.listConversations).toHaveBeenCalledOnce();
    expect(serviceMocks.listThreads).toHaveBeenCalledOnce();
    expect(useChatStore.getState().currentThreadId).toBe('thread-1');
  });

  it('makes selection visible synchronously while the triggered read remains awaitable', async () => {
    const conversations = deferred<never>();
    const threads = deferred<never>();
    serviceMocks.listConversations.mockReturnValue(conversations.promise);
    serviceMocks.listThreads.mockReturnValue(threads.promise);
    useChatStore.setState({
      currentConversationId: 'old-conversation',
      currentThreadId: 'old-thread',
    });

    const setCurrentWorkspace = useChatStore.getState()
      .setCurrentWorkspace as unknown as (
      workspaceId: string | null
    ) => Promise<void>;
    let workspaceLoad!: Promise<void>;
    act(() => {
      workspaceLoad = setCurrentWorkspace('workspace-1');
    });

    expect(workspaceLoad).toBeInstanceOf(Promise);
    expect(useChatStore.getState().currentWorkspaceId).toBe('workspace-1');
    expect(useChatStore.getState().currentConversationId).toBeNull();
    expect(useChatStore.getState().currentThreadId).toBeNull();
    expect(serviceMocks.listConversations).toHaveBeenCalledOnce();

    await act(async () => {
      conversations.resolve(conversationPage([conversation]));
      await workspaceLoad;
    });

    const setCurrentConversation = useChatStore.getState()
      .setCurrentConversation as unknown as (
      conversationId: string | null
    ) => Promise<void>;
    let threadLoad!: Promise<void>;
    act(() => {
      threadLoad = setCurrentConversation('conversation-1');
    });

    expect(threadLoad).toBeInstanceOf(Promise);
    expect(useChatStore.getState().currentConversationId).toBe(
      'conversation-1'
    );
    expect(useChatStore.getState().currentThreadId).toBeNull();
    expect(serviceMocks.listThreads).toHaveBeenCalledOnce();

    await act(async () => {
      threads.resolve(threadPage([]));
      await threadLoad;
    });
  });

  it('preserves an explicit deep-linked thread selected while layout init is in flight', async () => {
    const conversations = deferred<never>();
    serviceMocks.listConversations.mockReturnValue(conversations.promise);
    window.history.replaceState({}, '', '/chat?thread=thread-1');

    const hook = renderHook(() => useChatPersistence());
    let initialization!: Promise<void>;
    act(() => {
      initialization = hook.result.current.initialize();
    });

    await waitFor(() =>
      expect(serviceMocks.listConversations).toHaveBeenCalledOnce()
    );

    // useChatSession can restore the URL target while this layout-owned
    // initializer is still waiting for its conversation page. The later
    // setCurrentConversation call must not erase that newer selection.
    // Mutation check: neutralizing the deep-link restore branch in
    // `src/hooks/useChatPersistence.ts` makes this test fail with
    // `pnpm --dir frontend exec vitest run src/hooks/__tests__/useChatPersistence.initialization.test.tsx -t "preserves an explicit deep-linked thread" --reporter=dot`.
    await act(async () => {
      conversations.resolve(conversationPage([conversation]));
      useChatStore.getState().setCurrentThread(thread.id);
      await Promise.resolve();
    });

    await act(async () => {
      await initialization;
    });

    expect(useChatStore.getState().currentThreadId).toBe(thread.id);
  });

  it('preserves a newer sidebar selection made before the URL catches up', async () => {
    const conversations = deferred<never>();
    serviceMocks.listConversations.mockReturnValue(conversations.promise);
    serviceMocks.listThreads.mockResolvedValue(threadPage([thread]));
    window.history.replaceState({}, '', '/chat?thread=thread-1');

    const hook = renderHook(() => useChatPersistence());
    let initialization!: Promise<void>;
    act(() => {
      initialization = hook.result.current.initialize();
    });

    await waitFor(() =>
      expect(serviceMocks.listConversations).toHaveBeenCalledOnce()
    );

    await act(async () => {
      // The sidebar selection is newer than the A URL, but router.push has
      // not committed B yet. setCurrentConversation must not erase B while
      // resetting the downstream conversation selection.
      useChatStore.getState().setCurrentThread('thread-2');
      conversations.resolve(conversationPage([conversation]));
      await initialization;
    });

    expect(useChatStore.getState().currentThreadId).toBe('thread-2');
  });

  it('keeps a newer sidebar selection when the URL changes during thread loading', async () => {
    const conversations = deferred<never>();
    const threads = deferred<never>();
    serviceMocks.listConversations.mockReturnValue(conversations.promise);
    serviceMocks.listThreads.mockReturnValue(threads.promise);
    window.history.replaceState({}, '', '/chat?thread=thread-1');

    const hook = renderHook(() => useChatPersistence());
    let initialization!: Promise<void>;
    act(() => {
      initialization = hook.result.current.initialize();
    });

    await waitFor(() =>
      expect(serviceMocks.listConversations).toHaveBeenCalledOnce()
    );

    // The URL-owned selection is captured before the conversation switch
    // clears it. The sidebar can then select a different thread while the
    // resulting thread page is still loading; that newer choice owns the UI.
    await act(async () => {
      useChatStore.getState().setCurrentThread(thread.id);
      conversations.resolve(conversationPage([conversation]));
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(serviceMocks.listThreads).toHaveBeenCalledWith('conversation-1')
    );

    window.history.replaceState({}, '', '/chat?thread=thread-2');
    act(() => {
      useChatStore.getState().setCurrentThread('thread-2');
    });

    await act(async () => {
      threads.resolve(threadPage([thread]));
      await initialization;
    });

    expect(useChatStore.getState().currentThreadId).toBe('thread-2');
  });

  it('honors new-chat intent when a stale thread query is also present', async () => {
    const conversations = deferred<never>();
    serviceMocks.listConversations.mockReturnValue(conversations.promise);
    serviceMocks.listThreads.mockResolvedValue(threadPage([thread]));
    window.history.replaceState({}, '', '/chat?thread=thread-1&new=1');

    const hook = renderHook(() => useChatPersistence());
    let initialization!: Promise<void>;
    act(() => {
      initialization = hook.result.current.initialize();
    });

    await waitFor(() =>
      expect(serviceMocks.listConversations).toHaveBeenCalledOnce()
    );

    // This is the selection that previously caused the post-conversation
    // restoration branch to override the explicit new-chat intent.
    await act(async () => {
      useChatStore.getState().setCurrentThread(thread.id);
      conversations.resolve(conversationPage([conversation]));
      await initialization;
    });

    expect(useChatStore.getState().currentThreadId).toBeNull();
  });

  it('resolves null selections without reads and starts non-awaited selection reads', async () => {
    const conversations = deferred<never>();
    serviceMocks.listConversations.mockReturnValue(conversations.promise);
    const setCurrentWorkspace = useChatStore.getState()
      .setCurrentWorkspace as unknown as (
      workspaceId: string | null
    ) => Promise<void>;
    const setCurrentConversation = useChatStore.getState()
      .setCurrentConversation as unknown as (
      conversationId: string | null
    ) => Promise<void>;

    await expect(setCurrentWorkspace(null)).resolves.toBeUndefined();
    await expect(setCurrentConversation(null)).resolves.toBeUndefined();
    expect(serviceMocks.listConversations).not.toHaveBeenCalled();
    expect(serviceMocks.listThreads).not.toHaveBeenCalled();

    act(() => {
      void setCurrentWorkspace('workspace-1');
    });
    expect(useChatStore.getState().currentWorkspaceId).toBe('workspace-1');
    expect(serviceMocks.listConversations).toHaveBeenCalledOnce();

    await act(async () => {
      conversations.resolve(conversationPage([]));
      await conversations.promise;
    });
  });

  it('settles both consumers after a failed first read and permits a later retry', async () => {
    const failedRead = deferred<never>();
    serviceMocks.listConversations
      .mockReturnValueOnce(failedRead.promise)
      .mockResolvedValue(conversationPage([conversation]));
    serviceMocks.listThreads.mockResolvedValue(threadPage([]));
    const consoleError = vi
      .spyOn(console, 'error')
      .mockImplementation((message) => {
        if (
          typeof message !== 'string' ||
          (!message.startsWith('[ChatStore] Error loading conversations:') &&
            !message.startsWith('[useChatPersistence] Initialization failed:'))
        ) {
          throw new Error(`Unexpected console.error: ${String(message)}`);
        }
      });

    const first = renderHook(() => useChatPersistence());
    const second = renderHook(() => useChatPersistence());
    let firstWaiter!: Promise<void>;
    let secondWaiter!: Promise<void>;
    act(() => {
      firstWaiter = first.result.current.initialize();
      secondWaiter = second.result.current.initialize();
    });
    await waitFor(() =>
      expect(serviceMocks.listConversations).toHaveBeenCalledOnce()
    );

    await act(async () => {
      failedRead.reject(new Error('conversations offline'));
      await Promise.all([firstWaiter, secondWaiter]);
    });

    await waitFor(() => expect(toastErrorMock).toHaveBeenCalledOnce());
    expect(useChatStore.getState().error).toBe('Failed to load conversations');
    expect(serviceMocks.listThreads).not.toHaveBeenCalled();

    await act(async () => {
      await first.result.current.initialize();
    });

    expect(serviceMocks.getOrCreateDefaultWorkspace).toHaveBeenCalledTimes(2);
    expect(serviceMocks.listConversations).toHaveBeenCalledTimes(2);
    expect(serviceMocks.listThreads).toHaveBeenCalledOnce();
    expect(useChatStore.getState().currentConversationId).toBe(
      'conversation-1'
    );
    expect(useChatStore.getState().error).toBeNull();
    expect(consoleError).toHaveBeenCalledTimes(2);
  });

  it('creates, registers, and selects a default conversation after an empty read', async () => {
    const createdConversation = {
      ...conversation,
      id: 'conversation-created',
      title: 'New Chat',
    } as never;
    serviceMocks.listConversations.mockResolvedValue(conversationPage([]));
    serviceMocks.getOrCreateDefaultConversation.mockResolvedValue(
      createdConversation
    );
    serviceMocks.listThreads.mockResolvedValue(threadPage([]));

    const hook = renderHook(() => useChatPersistence());
    await act(async () => {
      await hook.result.current.initialize();
    });

    expect(serviceMocks.listConversations).toHaveBeenCalledOnce();
    expect(serviceMocks.getOrCreateDefaultConversation).toHaveBeenCalledWith(
      'workspace-1'
    );
    expect(serviceMocks.listThreads).toHaveBeenCalledOnce();
    expect(serviceMocks.listThreads).toHaveBeenCalledWith(
      'conversation-created'
    );
    expect(useChatStore.getState().conversations['workspace-1']).toEqual([
      createdConversation,
    ]);
    expect(useChatStore.getState().currentConversationId).toBe(
      'conversation-created'
    );
  });

  it('defers first-thread fallback while an explicit deep link is pending', async () => {
    window.history.replaceState({}, '', '/chat?thread=missing-thread');
    serviceMocks.listThreads.mockResolvedValue(threadPage([thread]));

    const hook = renderHook(() => useChatPersistence());
    await waitFor(() =>
      expect(serviceMocks.listThreads).toHaveBeenCalledWith('conversation-1')
    );

    // useChatSession owns the missing URL lookup and its 403/404 fallback. A
    // persistence consumer must leave the selection empty until that owner
    // resolves, otherwise the first thread wins and a later send targets it.
    expect(useChatStore.getState().currentThreadId).toBeNull();

    hook.unmount();
    window.history.replaceState({}, '', '/');
  });

  it('keeps explicit loads for different scopes physically independent', async () => {
    serviceMocks.listConversations.mockImplementation(async (workspaceId) =>
      conversationPage([
        { ...conversation, id: `conversation-${workspaceId}`, workspaceId },
      ])
    );
    serviceMocks.listThreads.mockImplementation(async (conversationId) =>
      threadPage([
        { ...thread, id: `thread-${conversationId}`, conversationId },
      ])
    );

    await Promise.all([
      useChatStore.getState().loadConversations('workspace-A'),
      useChatStore.getState().loadConversations('workspace-B'),
      useChatStore.getState().loadThreads('conversation-A'),
      useChatStore.getState().loadThreads('conversation-B'),
    ]);

    expect(serviceMocks.listConversations).toHaveBeenCalledTimes(2);
    expect(serviceMocks.listConversations).toHaveBeenNthCalledWith(
      1,
      'workspace-A'
    );
    expect(serviceMocks.listConversations).toHaveBeenNthCalledWith(
      2,
      'workspace-B'
    );
    expect(serviceMocks.listThreads).toHaveBeenCalledTimes(2);
    expect(serviceMocks.listThreads).toHaveBeenNthCalledWith(
      1,
      'conversation-A'
    );
    expect(serviceMocks.listThreads).toHaveBeenNthCalledWith(
      2,
      'conversation-B'
    );
  });
});

/**
 * Account switch while /chat stays mounted (PR #1776 review, P1).
 *
 * Real auth store, real chat store, real useChatPersistence and real
 * useChatSession; only Supabase, the profile endpoint, navigation and the
 * workspace service I/O are faked.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const accounts = vi.hoisted(() => ({ current: 'user-A' }));

const supabaseAuth = vi.hoisted(() => ({
  onAuthStateChange: vi.fn(),
  signOut: vi.fn(),
  signInWithPassword: vi.fn(),
}));

const workspaceMocks = vi.hoisted(() => ({
  getOrCreateDefaultWorkspace: vi.fn(),
  getOrCreateDefaultConversation: vi.fn(),
  listConversations: vi.fn(),
  listThreads: vi.fn(),
  listWorkspaceThreads: vi.fn(),
  listMessages: vi.fn(),
  getThread: vi.fn(),
}));

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({ auth: supabaseAuth }),
}));

vi.mock('@/services/api-client', () => ({
  api: {
    clearAuth: vi.fn(),
    get: vi.fn(),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  clearWorkspaceServiceCache: vi.fn(),
  workspaceService: workspaceMocks,
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

import toast from 'react-hot-toast';
import { useChatSession } from '@/hooks/chat/useChatSession';
import { useChatPersistence } from '@/hooks/useChatPersistence';
import { useChatStore } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';
import { api } from '@/services/api-client';
import { makeChatPageMessage } from '@/test/chatMessageFactory';
import type { User } from '@/types';

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const workspaceFor = (userId: string): never =>
  ({ id: `ws-${userId}`, name: `${userId} private workspace` }) as never;
const conversationFor = (userId: string): never =>
  ({
    id: `conv-${userId}`,
    workspace_id: `ws-${userId}`,
    title: `${userId} private conversation`,
  }) as never;

describe('useChatSession account switch without remount', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    accounts.current = 'user-A';
    supabaseAuth.signOut.mockResolvedValue({ error: null });
    supabaseAuth.signInWithPassword.mockResolvedValue({
      data: { session: { access_token: 'synthetic-token' } },
      error: null,
    });
    vi.mocked(api.get).mockImplementation(async () => ({
      user: { id: accounts.current },
      organization: { id: `org-${accounts.current}` },
    }));
    // Ends any prior chat session (and the shared persistence guard with it).
    useChatStore.getState().reset();
    useAuthStore.setState({
      user: { id: 'user-A' } as User,
      isAuthenticated: true,
      isLoading: false,
    });

    workspaceMocks.getOrCreateDefaultWorkspace.mockImplementation(async () =>
      workspaceFor(accounts.current)
    );
    workspaceMocks.listConversations.mockImplementation(
      async (workspaceId: string) => ({
        conversations:
          workspaceId === `ws-${accounts.current}`
            ? [conversationFor(accounts.current)]
            : [],
        total: 1,
        page: 1,
        limit: 20,
      })
    );
    workspaceMocks.listThreads.mockResolvedValue({
      threads: [],
      total: 0,
      page: 1,
      limit: 20,
    });
    workspaceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [],
      total: 0,
      page: 1,
      limit: 50,
      has_more: false,
    });
  });

  afterEach(() => {
    useAuthStore.setState({ user: null, isAuthenticated: false });
    useChatStore.getState().reset();
  });

  it('re-initializes chat persistence for user B and drops user A workspace metadata', async () => {
    const { result } = renderHook(() => useChatSession());

    await waitFor(() => {
      expect(result.current.isInitializing).toBe(false);
      expect(result.current.workspace?.id).toBe('ws-user-A');
    });
    expect(useChatStore.getState().currentWorkspaceId).toBe('ws-user-A');

    // Hold B's bootstrap so the in-between state is observable.
    const bootstrapB = deferred<never>();
    workspaceMocks.getOrCreateDefaultWorkspace.mockImplementation(() =>
      accounts.current === 'user-B'
        ? bootstrapB.promise
        : Promise.resolve(workspaceFor(accounts.current))
    );
    accounts.current = 'user-B';
    await act(async () => {
      await useAuthStore
        .getState()
        .signIn('user-b@example.invalid', 'synthetic-password');
    });
    expect(useAuthStore.getState().user?.id).toBe('user-B');
    // User A's workspace metadata leaves the mounted session immediately.
    expect(result.current.workspace).toBeNull();
    expect(result.current.isInitializing).toBe(true);
    // Persistence re-initialized for B instead of taking the completed path.
    expect(workspaceMocks.getOrCreateDefaultWorkspace).toHaveBeenCalledTimes(3);

    await act(async () => {
      bootstrapB.resolve(workspaceFor('user-B'));
    });
    await waitFor(() => {
      expect(result.current.isInitializing).toBe(false);
      expect(result.current.workspace?.id).toBe('ws-user-B');
    });
    expect(result.current.initError).toBeNull();
    const chat = useChatStore.getState();
    expect(chat.currentWorkspaceId).toBe('ws-user-B');
    expect(chat.currentConversationId).toBe('conv-user-B');
    expect(chat.workspaces.map((workspace) => workspace.id)).toEqual([
      'ws-user-B',
    ]);
    expect(Object.keys(chat.conversations)).toEqual(['ws-user-B']);
  });

  it('re-initializes a persistence-only consumer (chat layout) for user B', async () => {
    renderHook(() => useChatPersistence());
    await waitFor(() =>
      expect(useChatStore.getState().currentConversationId).toBe('conv-user-A')
    );

    accounts.current = 'user-B';
    await act(async () => {
      await useAuthStore
        .getState()
        .signIn('user-b@example.invalid', 'synthetic-password');
    });

    await waitFor(() =>
      expect(useChatStore.getState().currentConversationId).toBe('conv-user-B')
    );
    expect(useChatStore.getState().currentWorkspaceId).toBe('ws-user-B');
  });

  it('ignores user A’s superseded initialization when it settles after the switch', async () => {
    const bootstrapA = deferred<never>();
    const bootstrapB = deferred<never>();
    workspaceMocks.getOrCreateDefaultWorkspace.mockImplementation(() =>
      accounts.current === 'user-A' ? bootstrapA.promise : bootstrapB.promise
    );
    const { result } = renderHook(() => useChatPersistence());
    await waitFor(() =>
      expect(workspaceMocks.getOrCreateDefaultWorkspace).toHaveBeenCalled()
    );

    accounts.current = 'user-B';
    await act(async () => {
      await useAuthStore
        .getState()
        .signIn('user-b@example.invalid', 'synthetic-password');
    });

    // A's bootstrap lands late: its run is fenced and fails internally, but
    // that failure belongs to A's ended session, not to B.
    await act(async () => {
      bootstrapA.resolve(workspaceFor('user-A'));
    });
    expect(toast.error).not.toHaveBeenCalled();
    // ...and it does not release B's in-flight run: a concurrent consumer
    // joins B's run instead of starting a duplicate bootstrap.
    const callsDuringB =
      workspaceMocks.getOrCreateDefaultWorkspace.mock.calls.length;
    let joined!: Promise<void>;
    act(() => {
      joined = result.current.initialize();
    });
    expect(workspaceMocks.getOrCreateDefaultWorkspace).toHaveBeenCalledTimes(
      callsDuringB
    );

    await act(async () => {
      bootstrapB.resolve(workspaceFor('user-B'));
    });
    await act(async () => {
      await joined;
    });
    await waitFor(() =>
      expect(useChatStore.getState().currentConversationId).toBe('conv-user-B')
    );
    // B's run, not a third one, completed the shared guard.
    const callsAfterB =
      workspaceMocks.getOrCreateDefaultWorkspace.mock.calls.length;
    await act(async () => {
      await result.current.initialize();
    });
    expect(workspaceMocks.getOrCreateDefaultWorkspace).toHaveBeenCalledTimes(
      callsAfterB
    );
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('stops user A’s run once it outlives the switch past setCurrentConversation', async () => {
    // A's conversation page and thread page are held; A picks a thread in the
    // sidebar while its init is pending.
    const conversationsA = deferred<never>();
    const threadsA = deferred<never>();
    const bootstrapB = deferred<never>();
    workspaceMocks.getOrCreateDefaultWorkspace.mockImplementation(() =>
      accounts.current === 'user-A'
        ? Promise.resolve(workspaceFor('user-A'))
        : bootstrapB.promise
    );
    workspaceMocks.listConversations.mockImplementation(
      (workspaceId: string) =>
        workspaceId === 'ws-user-A'
          ? conversationsA.promise
          : Promise.resolve({
              conversations: [conversationFor('user-B')],
              total: 1,
              page: 1,
              limit: 20,
            })
    );
    workspaceMocks.listThreads.mockImplementation((conversationId: string) =>
      conversationId === 'conv-user-A'
        ? threadsA.promise
        : Promise.resolve({ threads: [], total: 0, page: 1, limit: 20 })
    );

    const { result } = renderHook(() => useChatPersistence());
    await waitFor(() =>
      expect(workspaceMocks.listConversations).toHaveBeenCalledWith('ws-user-A')
    );
    act(() => {
      useChatStore.setState({ currentThreadId: 'user-A-picked-thread' });
    });
    await act(async () => {
      conversationsA.resolve({
        conversations: [conversationFor('user-A')],
        total: 1,
        page: 1,
        limit: 20,
      } as never);
    });
    await waitFor(() =>
      expect(workspaceMocks.listThreads).toHaveBeenCalledWith('conv-user-A')
    );

    // Switch to B while A awaits setCurrentConversation; B's bootstrap holds.
    accounts.current = 'user-B';
    await act(async () => {
      await useAuthStore
        .getState()
        .signIn('user-b@example.invalid', 'synthetic-password');
    });
    await act(async () => {
      threadsA.resolve({ threads: [], total: 0, page: 1, limit: 20 } as never);
    });

    // A's late run neither marks the shared guard complete (a caller still
    // joins B's pending run) nor selects A's thread in B's store.
    let joinedSettled = false;
    let joined!: Promise<void>;
    act(() => {
      joined = result.current.initialize().then(() => {
        joinedSettled = true;
      });
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(joinedSettled).toBe(false);
    expect(useChatStore.getState().currentThreadId).toBeNull();

    await act(async () => {
      bootstrapB.resolve(workspaceFor('user-B'));
      await joined;
    });
    expect(useChatStore.getState().currentConversationId).toBe('conv-user-B');
    expect(useChatStore.getState().currentThreadId).toBeNull();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('does not register user A’s late default conversation in user B’s store', async () => {
    // A has no conversation yet, so its run creates the default one.
    const defaultConversationA = deferred<never>();
    workspaceMocks.listConversations.mockImplementation(
      async (workspaceId: string) => ({
        conversations:
          workspaceId === 'ws-user-B' ? [conversationFor('user-B')] : [],
        total: 0,
        page: 1,
        limit: 20,
      })
    );
    workspaceMocks.getOrCreateDefaultConversation.mockReturnValue(
      defaultConversationA.promise
    );
    renderHook(() => useChatPersistence());
    await waitFor(() =>
      expect(workspaceMocks.getOrCreateDefaultConversation).toHaveBeenCalled()
    );

    accounts.current = 'user-B';
    await act(async () => {
      await useAuthStore
        .getState()
        .signIn('user-b@example.invalid', 'synthetic-password');
    });
    await waitFor(() =>
      expect(useChatStore.getState().currentConversationId).toBe('conv-user-B')
    );
    await act(async () => {
      defaultConversationA.resolve(conversationFor('user-A'));
    });

    const chat = useChatStore.getState();
    expect(chat.conversationToWorkspace['conv-user-A']).toBeUndefined();
    expect(Object.keys(chat.conversations)).toEqual(['ws-user-B']);
    expect(chat.currentConversationId).toBe('conv-user-B');
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('does not let user A’s late bootstrap re-run conversation setup on user B’s workspace', async () => {
    const bootstrapA = deferred<never>();
    workspaceMocks.getOrCreateDefaultWorkspace.mockImplementation(() =>
      accounts.current === 'user-A'
        ? bootstrapA.promise
        : Promise.resolve(workspaceFor('user-B'))
    );
    renderHook(() => useChatPersistence());
    await waitFor(() =>
      expect(workspaceMocks.getOrCreateDefaultWorkspace).toHaveBeenCalled()
    );

    accounts.current = 'user-B';
    await act(async () => {
      await useAuthStore
        .getState()
        .signIn('user-b@example.invalid', 'synthetic-password');
    });
    await waitFor(() =>
      expect(useChatStore.getState().currentConversationId).toBe('conv-user-B')
    );
    const threadReadsForB = workspaceMocks.listThreads.mock.calls.length;
    await act(async () => {
      bootstrapA.resolve(workspaceFor('user-A'));
    });

    // A's run stops instead of reloading B's conversation with A's
    // initialization-time selection.
    expect(workspaceMocks.listThreads).toHaveBeenCalledTimes(threadReadsForB);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('drops user A’s parked in-flight overlay on the switch', async () => {
    const { result } = renderHook(() => useChatSession());
    await waitFor(() => {
      expect(result.current.isInitializing).toBe(false);
      expect(result.current.workspace?.id).toBe('ws-user-A');
    });

    // A switches away from a thread mid-stream: its overlay is parked.
    await act(async () => {
      useChatStore.setState({ currentThreadId: 'shared-thread-id' });
    });
    await act(async () => {
      result.current.setMessages([
        makeChatPageMessage({
          role: 'user',
          content: 'User A private in-flight question',
          timestamp: 1,
          source: 'optimistic',
        }),
      ]);
    });
    await act(async () => {
      useChatStore.setState({
        streamingThreadId: 'shared-thread-id',
        currentThreadId: 'user-A-other-thread',
      });
    });

    accounts.current = 'user-B';
    await act(async () => {
      await useAuthStore
        .getState()
        .signIn('user-b@example.invalid', 'synthetic-password');
    });
    await waitFor(() => expect(result.current.workspace?.id).toBe('ws-user-B'));

    // B opens a thread with the same id: A's parked overlay must not return.
    await act(async () => {
      useChatStore.setState({ currentThreadId: 'shared-thread-id' });
    });
    expect(result.current.messages).toEqual([]);
  });
});

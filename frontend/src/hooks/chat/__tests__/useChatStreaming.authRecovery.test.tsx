import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, renderHook, waitFor } from '@testing-library/react';
import {
  createElement,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useState,
} from 'react';
import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import type {
  UseChatStreamingParams,
  UseChatStreamingReturn,
} from '@/hooks/chat/useChatStreaming';
import {
  CHAT_AUTH_RECOVERY_STORAGE_KEY,
  markChatAuthRecoveryReady,
  stageChatAuthRecovery,
} from '@/hooks/chat/chatAuthRecovery';
import { useChatStore } from '@/store/chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import { useAuthStore } from '@/stores/authStore';

const THREAD_ID = 'thread-A';
const THREAD_B_ID = 'thread-B';
const USER_A = { id: 'user-A' } as NonNullable<
  ReturnType<typeof useAuthStore.getState>['user']
>;
const USER_B = { id: 'user-B' } as NonNullable<
  ReturnType<typeof useAuthStore.getState>['user']
>;

type AuthListener = (
  event: string,
  session: Record<string, unknown> | null
) => void;
type StreamCallbacks = {
  onToken?: (content: string) => void;
  onDone?: (payload?: Record<string, unknown>) => void;
  onError?: (error: string, category?: string, localFailure?: string) => void;
  onAuthRefreshAttempt?: () => void;
  onAuthRefreshSuccess?: () => void;
};

let authListener: AuthListener | undefined;
let routeTransition: ((path: string) => void) | undefined;
let useActualStreamMessage = false;
let currentSearchParams = new URLSearchParams();
const pushMock = vi.fn();
const replaceMock = vi.fn();
const streamMessageMock = vi.fn();
const actualServiceErrorMock = vi.fn();
const streamConfirmMock = vi.fn();
const resumeStreamMock = vi.fn().mockResolvedValue({ status: 'idle' });
const listMessagesMock = vi
  .fn()
  .mockResolvedValue({ messages: [], has_more: false });
const getSessionMock = vi.fn();
const refreshSessionMock = vi.fn();
const getDefaultWorkspaceMock = vi.fn();
const listWorkspaceThreadsMock = vi.fn();
const getThreadMock = vi.fn();
const realFetch = global.fetch;
const realConsoleError = console.error;
const realConsoleLog = console.log;
const realConsoleWarn = console.warn;

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: pushMock, replace: replaceMock }),
  useSearchParams: () => currentSearchParams,
}));

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({
    auth: {
      onAuthStateChange: (listener: AuthListener) => {
        authListener = listener;
        return { data: { subscription: { unsubscribe: vi.fn() } } };
      },
      getUser: vi.fn().mockResolvedValue({
        data: { user: null },
        error: null,
      }),
      getSession: getSessionMock,
      refreshSession: refreshSessionMock,
      signOut: vi.fn(),
    },
  }),
}));

vi.mock('@/services/agentChatService', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('@/services/agentChatService')>();
  return {
    ...actual,
    agentChatService: {
      streamMessage: (
        ...args: Parameters<typeof actual.agentChatService.streamMessage>
      ) => {
        if (!useActualStreamMessage) return streamMessageMock(...args);
        const [request, callbacks, signal] = args;
        return actual.agentChatService.streamMessage(
          request,
          {
            ...callbacks,
            onError: (...errorArgs) => {
              actualServiceErrorMock(...errorArgs);
              callbacks.onError?.(...errorArgs);
            },
          },
          signal
        );
      },
      streamConfirm: (...args: unknown[]) => streamConfirmMock(...args),
      resumeStream: (...args: unknown[]) => resumeStreamMock(...args),
    },
  };
});

vi.mock('@/services/workspaceService', () => ({
  clearWorkspaceServiceCache: vi.fn(),
  workspaceService: {
    createThread: vi.fn(),
    createMessage: vi.fn().mockResolvedValue({ id: 'message-1' }),
    listMessages: (...args: unknown[]) => listMessagesMock(...args),
    getOrCreateDefaultWorkspace: (...args: unknown[]) =>
      getDefaultWorkspaceMock(...args),
    listWorkspaceThreads: (...args: unknown[]) =>
      listWorkspaceThreadsMock(...args),
    getThread: (...args: unknown[]) => getThreadMock(...args),
  },
}));

function wrapper({ children }: { children: ReactNode }): ReactNode {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

function makeParams(
  setMessages: UseChatStreamingParams['setMessages'] = vi.fn()
): UseChatStreamingParams {
  return {
    messages: [],
    displayedMessages: [],
    setMessages,
    conversations: [],
    setConversations: vi.fn(),
    dbConversation: null,
    enableRAG: false,
    authRecoveryRoute: { isReady: true, threadId: THREAD_ID },
  };
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function storedRecovery(): Record<string, unknown> | null {
  const raw = sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY);
  return raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
}

function lastErrorCategory(
  setMessages: ReturnType<typeof vi.fn>
): string | undefined {
  let messages: ChatPageMessage[] = [];
  for (const [next] of setMessages.mock.calls) {
    messages =
      typeof next === 'function'
        ? (next as (current: ChatPageMessage[]) => ChatPageMessage[])(messages)
        : next;
  }
  return [...messages].reverse().find((message) => message.error)?.error
    ?.category;
}

describe('useChatStreaming exhausted-auth recovery', () => {
  // Fix-round-1 mutation checks (2026-09-14):
  // - Removing the retry-success callback at agentChatService.ts:271 fails
  //   both service lifecycle tests (0 calls) and "closes the auth-loss
  //   window" (`ready`, expected `armed`):
  //   pnpm --dir frontend exec vitest run src/services/__tests__/agentChatService.resilience.test.ts src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx -t "refreshed session|successful refresh lifecycle|closes the auth-loss window" --reporter=verbose
  // - Letting an ownerless attempt ignore a newly present account at
  //   useChatStreaming.ts:602-605 fails both message and confirmation account
  //   guards with an unexpected recovery navigation:
  //   pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx -t "abandons ownerless|accountAppears=true" --reporter=verbose
  // - Refusing to register the ownerless attempt at
  //   useChatStreaming.ts:1730 fails generic recovery (auth stays true):
  //   pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.authRecovery.test.tsx -t "uses generic sign-in recovery without storing an ownerless prompt" --reporter=verbose
  // - Letting invalidateRejectedSession(null) ignore its current-account gate
  //   at authStore.ts:312 fails by clearing user B:
  //   pnpm --dir frontend exec vitest run src/store/__tests__/auth-store-signout.test.ts -t "null expected owner" --reporter=verbose
  beforeAll(async () => {
    await useAuthStore.getState().initialize();
    expect(authListener).toBeTypeOf('function');
  });

  beforeEach(() => {
    sessionStorage.clear();
    currentSearchParams = new URLSearchParams();
    useActualStreamMessage = false;
    global.fetch = realFetch;
    routeTransition = undefined;
    pushMock.mockReset().mockImplementation((path: string) => {
      routeTransition?.(path);
    });
    replaceMock.mockReset().mockImplementation((path: string) => {
      routeTransition?.(path);
    });
    streamMessageMock.mockReset();
    actualServiceErrorMock.mockReset();
    streamConfirmMock.mockReset();
    getSessionMock.mockReset().mockResolvedValue({
      data: { session: { access_token: 'token-A' } },
    });
    refreshSessionMock.mockReset().mockResolvedValue({
      data: { session: { access_token: 'token-A-refreshed' } },
      error: null,
    });
    resumeStreamMock.mockReset().mockResolvedValue({ status: 'idle' });
    listMessagesMock
      .mockReset()
      .mockResolvedValue({ messages: [], has_more: false });
    getDefaultWorkspaceMock.mockReset().mockResolvedValue({
      id: 'workspace-A',
      name: 'Workspace A',
    });
    listWorkspaceThreadsMock.mockReset().mockResolvedValue({
      threads: [
        {
          id: THREAD_ID,
          conversation_id: 'conversation-A',
          title: 'Thread A',
          status: 'active',
          message_count: 0,
          token_count: 0,
          created_at: '2026-09-13T00:00:00.000Z',
          updated_at: '2026-09-13T00:00:00.000Z',
        },
      ],
      total: 1,
      page: 1,
      limit: 50,
      has_more: false,
    });
    getThreadMock.mockResolvedValue({
      id: THREAD_ID,
      conversation_id: 'conversation-A',
      title: 'Thread A',
      status: 'active',
      message_count: 0,
      token_count: 0,
      created_at: '2026-09-13T00:00:00.000Z',
      updated_at: '2026-09-13T00:00:00.000Z',
    });
    vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      const first = args[0];
      if (
        typeof first === 'string' &&
        (first.startsWith('[ChatReconciliationInvariant]') ||
          first.startsWith('[Agent] Stream error:') ||
          first.startsWith('Failed to send message:'))
      ) {
        return;
      }
      realConsoleError(...args);
    });
    vi.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      const first = args[0];
      if (
        typeof first === 'string' &&
        (first.startsWith('[Chat]') || first.startsWith('[Agent]'))
      ) {
        return;
      }
      realConsoleLog(...args);
    });
    vi.spyOn(console, 'warn').mockImplementation((...args: unknown[]) => {
      if (args[0] === '[ChatReconciliationInvariant]') return;
      realConsoleWarn(...args);
    });
    useAuthStore.setState({
      user: USER_A,
      organization: null,
      isAuthenticated: true,
      isLoading: false,
      error: null,
    });
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.setState({
      currentThreadId: THREAD_ID,
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    global.fetch = realFetch;
    useChatStore.setState({
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  it('survives a real SIGNED_OUT route unmount and restores without resending', async () => {
    const order: string[] = [];
    let stateAtNavigation: Record<string, unknown> | null = null;
    let routedStreaming: UseChatStreamingReturn | undefined;
    useActualStreamMessage = true;
    replaceMock.mockImplementation((path: string) => {
      if (path.startsWith('/login')) {
        stateAtNavigation = storedRecovery();
        expect(stateAtNavigation?.state).toBe('ready');
        order.push('ready');
      }
      routeTransition?.(path);
    });
    refreshSessionMock.mockImplementation(async () => {
      // The service's marker runs synchronously immediately before refresh.
      order.push('retry-marked');
      authListener?.('SIGNED_OUT', null);
      expect(useAuthStore.getState().isAuthenticated).toBe(false);
      order.push('auth-cleared');
      return { data: { session: null }, error: new Error('refresh rejected') };
    });
    getSessionMock
      .mockResolvedValueOnce({
        data: { session: { access_token: 'token-A' } },
      })
      .mockResolvedValueOnce({ data: { session: null } });
    const fetchMock = vi.fn(
      async (_url: string | URL | Request, init?: RequestInit) => {
        if (fetchMock.mock.calls.length === 1) {
          order.push('armed');
          expect(storedRecovery()?.state).toBe('armed');
          return {
            ok: false,
            status: 401,
          } as Response;
        }
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            'abort',
            () => {
              order.push('abort');
              reject(new DOMException('aborted', 'AbortError'));
            },
            { once: true }
          );
        });
      }
    );
    global.fetch = fetchMock as typeof fetch;

    const [{ useChatSession }, { useChatStreaming }] = await Promise.all([
      import('@/hooks/chat/useChatSession'),
      import('@/hooks/chat/useChatStreaming'),
    ]);

    function ChatRoute(): ReactNode {
      const session = useChatSession();
      const streaming = useChatStreaming({
        messages: session.messages,
        displayedMessages: session.displayedMessages,
        setMessages: session.setMessages,
        conversations: session.conversations,
        setConversations: session.setConversations,
        dbConversation: session.dbConversation,
        workspace: session.workspace,
        enableRAG: false,
        authRecoveryRoute: session.authRecoveryRoute,
      });
      useEffect(() => {
        if (!session.isInitializing) routedStreaming = streaming;
      }, [session.isInitializing, streaming]);
      useLayoutEffect(
        () => () => {
          order.push('unmount');
        },
        []
      );
      return createElement('div', { 'data-testid': 'chat-route' });
    }

    function RoutedHarness(): ReactNode {
      const [route, setRoute] = useState('/chat');
      useEffect(() => {
        routeTransition = (path: string) => setRoute(path);
        return () => {
          routeTransition = undefined;
        };
      }, []);
      return route.startsWith('/login')
        ? createElement('div', { 'data-testid': 'login-route' })
        : createElement(ChatRoute);
    }

    const routed = render(createElement(RoutedHarness), { wrapper });
    await waitFor(() => expect(routedStreaming).toBeDefined());
    replaceMock.mockClear();
    pushMock.mockClear();
    const timerSpy = vi.spyOn(globalThis, 'setTimeout');

    let submit!: Promise<void>;
    await act(async () => {
      submit = routedStreaming!.handleSubmit('keep this private prompt');
      await Promise.resolve();
    });
    await waitFor(() => expect(replaceMock).toHaveBeenCalledTimes(1));
    await act(async () => {
      await submit;
    });

    expect(order).toEqual([
      'armed',
      'retry-marked',
      'auth-cleared',
      'ready',
      'unmount',
      'abort',
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(refreshSessionMock).toHaveBeenCalledTimes(1);
    expect(actualServiceErrorMock).not.toHaveBeenCalled();
    expect(timerSpy.mock.calls.some(([, delay]) => delay === 1_500)).toBe(true);
    await act(() => new Promise<void>((resolve) => setTimeout(resolve, 1_600)));
    expect(pushMock).not.toHaveBeenCalled();
    expect(stateAtNavigation).toEqual(
      expect.objectContaining({
        ownerUserId: 'user-A',
        threadId: THREAD_ID,
        prompt: 'keep this private prompt',
        state: 'ready',
      })
    );
    const recoveryUrl = replaceMock.mock.calls[0][0] as string;
    expect(recoveryUrl).not.toContain('keep%20this');
    expect(recoveryUrl).toContain('reauth=chat');
    expect(recoveryUrl).toContain('draft=saved');
    expect(recoveryUrl).toContain(
      `next=${encodeURIComponent(`/chat?thread=${THREAD_ID}`)}`
    );
    routed.unmount();

    act(() => {
      useAuthStore.setState({ user: USER_A, isAuthenticated: true });
    });
    const restored = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });
    await waitFor(() =>
      expect(restored.result.current.input).toBe('keep this private prompt')
    );
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('keeps the successful refresh path unchanged and clears its armed draft', async () => {
    let stateAtOpen: Record<string, unknown> | null = null;
    streamMessageMock.mockImplementation(
      async (_request: unknown, callbacks: StreamCallbacks) => {
        stateAtOpen = storedRecovery();
        callbacks.onAuthRefreshAttempt?.();
        callbacks.onToken?.('answer');
        callbacks.onDone?.({ assistant_message_id: 'assistant-1' });
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });

    await act(async () => {
      await result.current.handleSubmit('hello');
    });

    expect(streamMessageMock).toHaveBeenCalledTimes(1);
    expect(stateAtOpen).toEqual(
      expect.objectContaining({ state: 'armed', ownerUserId: 'user-A' })
    );
    expect(replaceMock).not.toHaveBeenCalled();
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
  });

  it.each([
    {
      name: 'a different URL thread',
      query: `thread=${THREAD_B_ID}`,
      expectedThreadId: THREAD_B_ID,
      retainedDraft: 'account B draft',
      shouldRestore: false,
    },
    {
      name: 'explicit new-chat intent',
      query: 'new=1',
      expectedThreadId: null,
      retainedDraft: 'new chat draft',
      shouldRestore: false,
    },
    {
      name: 'the matching URL thread',
      query: `thread=${THREAD_ID}`,
      expectedThreadId: THREAD_ID,
      retainedDraft: '',
      shouldRestore: true,
    },
  ])(
    'waits for session readiness before recovery on $name',
    async ({ query, expectedThreadId, retainedDraft, shouldRestore }) => {
      const pendingWorkspace = deferred<{ id: string; name: string }>();
      currentSearchParams = new URLSearchParams(query);
      getDefaultWorkspaceMock.mockReturnValueOnce(pendingWorkspace.promise);
      listWorkspaceThreadsMock.mockResolvedValueOnce({
        threads: [
          {
            id: THREAD_ID,
            conversation_id: 'conversation-A',
            title: 'Thread A',
            status: 'active',
            message_count: 0,
            token_count: 0,
            created_at: '2026-09-13T00:00:00.000Z',
            updated_at: '2026-09-13T00:00:00.000Z',
          },
          {
            id: THREAD_B_ID,
            conversation_id: 'conversation-B',
            title: 'Thread B',
            status: 'active',
            message_count: 0,
            token_count: 0,
            created_at: '2026-09-13T00:00:00.000Z',
            updated_at: '2026-09-13T00:00:00.000Z',
          },
        ],
        total: 2,
        page: 1,
        limit: 50,
        has_more: false,
      });
      expect(
        stageChatAuthRecovery({
          attemptId: 'route-readiness-attempt',
          ownerUserId: USER_A.id,
          threadId: THREAD_ID,
          prompt: 'account A recovered prompt',
        })
      ).toBe(true);
      expect(markChatAuthRecoveryReady('route-readiness-attempt')).toBe(true);

      const [{ useChatSession }, { useChatStreaming }] = await Promise.all([
        import('@/hooks/chat/useChatSession'),
        import('@/hooks/chat/useChatStreaming'),
      ]);
      let latest:
        | {
            session: ReturnType<typeof useChatSession>;
            streaming: UseChatStreamingReturn;
          }
        | undefined;

      function RoutedRecoveryHooks(): ReactNode {
        const session = useChatSession();
        const streaming = useChatStreaming({
          messages: session.messages,
          displayedMessages: session.displayedMessages,
          setMessages: session.setMessages,
          conversations: session.conversations,
          setConversations: session.setConversations,
          dbConversation: session.dbConversation,
          workspace: session.workspace,
          enableRAG: false,
          authRecoveryRoute: session.authRecoveryRoute,
        });
        latest = { session, streaming };
        return createElement('div', { 'data-testid': 'routed-recovery' });
      }

      const routed = render(createElement(RoutedRecoveryHooks), { wrapper });
      await waitFor(() =>
        expect(getDefaultWorkspaceMock).toHaveBeenCalledTimes(1)
      );

      expect(latest?.session.isInitializing).toBe(true);
      expect(latest?.streaming.input).toBe('');
      expect(storedRecovery()?.state).toBe('ready');

      if (!shouldRestore) {
        act(() => latest?.streaming.setInput(retainedDraft));
      }
      await act(async () => {
        pendingWorkspace.resolve({ id: 'workspace-A', name: 'Workspace A' });
      });
      await waitFor(() => expect(latest?.session.isInitializing).toBe(false));

      if (expectedThreadId) {
        await waitFor(() =>
          expect(latest?.session.activeThreadId).toBe(expectedThreadId)
        );
      }
      if (shouldRestore) {
        await waitFor(() =>
          expect(latest?.streaming.input).toBe('account A recovered prompt')
        );
        expect(
          sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)
        ).toBeNull();
      } else {
        expect(latest?.streaming.input).toBe(retainedDraft);
        expect(storedRecovery()).toEqual(
          expect.objectContaining({
            ownerUserId: USER_A.id,
            threadId: THREAD_ID,
            state: 'ready',
          })
        );
      }
      expect(streamMessageMock).not.toHaveBeenCalled();
      routed.unmount();
    }
  );

  it('does not consume through a stale ready-route prop when the live selection differs', async () => {
    expect(
      stageChatAuthRecovery({
        attemptId: 'stale-route-prop',
        ownerUserId: USER_A.id,
        threadId: THREAD_ID,
        prompt: 'thread A only',
      })
    ).toBe(true);
    expect(markChatAuthRecoveryReady('stale-route-prop')).toBe(true);
    useChatStore.setState({ currentThreadId: THREAD_B_ID });
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');

    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });

    expect(result.current.input).toBe('');
    expect(storedRecovery()).toEqual(
      expect.objectContaining({
        ownerUserId: USER_A.id,
        threadId: THREAD_ID,
        state: 'ready',
      })
    );
    expect(streamMessageMock).not.toHaveBeenCalled();
  });

  it('rechecks the live account before a ready-route recovery effect consumes', async () => {
    expect(
      stageChatAuthRecovery({
        attemptId: 'account-switch-before-passive-effect',
        ownerUserId: USER_A.id,
        threadId: THREAD_ID,
        prompt: 'account A effect prompt',
      })
    ).toBe(true);
    expect(
      markChatAuthRecoveryReady('account-switch-before-passive-effect')
    ).toBe(true);
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    let latest: UseChatStreamingReturn | undefined;

    function AccountSwitchBeforePassiveEffect(): ReactNode {
      const streaming = useChatStreaming(makeParams());
      useLayoutEffect(() => {
        useAuthStore.setState({ user: USER_B, isAuthenticated: true });
      }, []);
      latest = streaming;
      return createElement('div', { 'data-testid': 'account-switch-effect' });
    }

    render(createElement(AccountSwitchBeforePassiveEffect), { wrapper });
    await waitFor(() =>
      expect(useAuthStore.getState().user?.id).toBe(USER_B.id)
    );

    expect(latest?.input).toBe('');
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(streamMessageMock).not.toHaveBeenCalled();
  });

  it('does not consume when the initialization watchdog exposes an unsettled route', async () => {
    vi.useFakeTimers();
    const pendingWorkspace = new Promise<never>(() => {});
    currentSearchParams = new URLSearchParams(`thread=${THREAD_ID}`);
    getDefaultWorkspaceMock.mockReturnValueOnce(pendingWorkspace);
    expect(
      stageChatAuthRecovery({
        attemptId: 'watchdog-route',
        ownerUserId: USER_A.id,
        threadId: THREAD_ID,
        prompt: 'wait for settled initialization',
      })
    ).toBe(true);
    expect(markChatAuthRecoveryReady('watchdog-route')).toBe(true);

    const [{ useChatSession }, { useChatStreaming }] = await Promise.all([
      import('@/hooks/chat/useChatSession'),
      import('@/hooks/chat/useChatStreaming'),
    ]);
    let latest:
      | {
          session: ReturnType<typeof useChatSession>;
          streaming: UseChatStreamingReturn;
        }
      | undefined;

    function WatchdogRecoveryHooks(): ReactNode {
      const session = useChatSession();
      const streaming = useChatStreaming({
        messages: session.messages,
        displayedMessages: session.displayedMessages,
        setMessages: session.setMessages,
        conversations: session.conversations,
        setConversations: session.setConversations,
        dbConversation: session.dbConversation,
        workspace: session.workspace,
        enableRAG: false,
        authRecoveryRoute: session.authRecoveryRoute,
      });
      latest = { session, streaming };
      return createElement('div', { 'data-testid': 'watchdog-recovery' });
    }

    const routed = render(createElement(WatchdogRecoveryHooks), { wrapper });
    try {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });

      expect(latest?.session.isInitializing).toBe(false);
      expect(latest?.session.initError).toContain(
        'Connecting is taking longer than expected'
      );
      expect(latest?.session.authRecoveryRoute).toEqual({
        isReady: false,
        threadId: THREAD_ID,
      });
      expect(latest?.streaming.input).toBe('');
      expect(storedRecovery()?.state).toBe('ready');
      expect(streamMessageMock).not.toHaveBeenCalled();
    } finally {
      routed.unmount();
      vi.useRealTimers();
    }
  });

  it('uses generic sign-in recovery without storing an ownerless prompt', async () => {
    const setMessages = vi.fn();
    useAuthStore.setState({ user: null, isAuthenticated: true });
    streamMessageMock.mockImplementation(
      async (_request: unknown, callbacks: StreamCallbacks) => {
        callbacks.onAuthRefreshAttempt?.();
        callbacks.onError?.(
          'Authentication required',
          undefined,
          'authentication_required'
        );
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(
      () => useChatStreaming(makeParams(setMessages)),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleSubmit('must never be persisted');
    });

    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(lastErrorCategory(setMessages)).toBeUndefined();
    expect(replaceMock).toHaveBeenCalledTimes(1);
    const recoveryUrl = replaceMock.mock.calls[0][0] as string;
    expect(recoveryUrl).toContain('reauth=chat');
    expect(recoveryUrl).not.toContain('draft=saved');
    expect(recoveryUrl).not.toContain('persisted');
  });

  it('recovers ownerlessly when SIGNED_OUT beats the final retry response', async () => {
    let releaseStream: (() => void) | undefined;
    useAuthStore.setState({ user: null, isAuthenticated: true });
    streamMessageMock.mockImplementation(
      (_request: unknown, callbacks: StreamCallbacks) => {
        callbacks.onAuthRefreshAttempt?.();
        authListener?.('SIGNED_OUT', null);
        return new Promise<void>((resolve) => {
          releaseStream = resolve;
        });
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });

    let submit!: Promise<void>;
    await act(async () => {
      submit = result.current.handleSubmit('must remain memory only');
      await Promise.resolve();
    });
    await waitFor(() => expect(replaceMock).toHaveBeenCalledTimes(1));

    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
    expect(replaceMock.mock.calls[0][0]).not.toContain('draft=saved');
    await act(async () => {
      releaseStream?.();
      await submit;
    });
  });

  it('abandons ownerless recovery when another account appears', async () => {
    const setMessages = vi.fn();
    useAuthStore.setState({ user: null, isAuthenticated: true });
    streamMessageMock.mockImplementation(
      async (_request: unknown, callbacks: StreamCallbacks) => {
        callbacks.onAuthRefreshAttempt?.();
        useAuthStore.setState({ user: USER_B, isAuthenticated: true });
        callbacks.onError?.(
          'Authentication required',
          undefined,
          'authentication_required'
        );
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(
      () => useChatStreaming(makeParams(setMessages)),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleSubmit('owner unavailable');
    });

    expect(useAuthStore.getState()).toEqual(
      expect.objectContaining({ user: USER_B, isAuthenticated: true })
    );
    expect(replaceMock).not.toHaveBeenCalled();
    expect(lastErrorCategory(setMessages)).toBeUndefined();
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
  });

  it('closes the auth-loss window after a successful retry response opens', async () => {
    useActualStreamMessage = true;
    let bodyReadStarted = false;
    getSessionMock
      .mockResolvedValueOnce({
        data: { session: { access_token: 'token-A' } },
      })
      .mockResolvedValueOnce({
        data: { session: { access_token: 'token-A-refreshed' } },
      });
    const fetchMock = vi.fn(
      async (_url: string | URL | Request, init?: RequestInit) => {
        if (fetchMock.mock.calls.length === 1) {
          return { ok: false, status: 401 } as Response;
        }
        return {
          ok: true,
          status: 200,
          body: {
            getReader: () => ({
              read: () => {
                bodyReadStarted = true;
                return new Promise<ReadableStreamReadResult<Uint8Array>>(
                  (_resolve, reject) => {
                    init?.signal?.addEventListener(
                      'abort',
                      () => reject(new DOMException('aborted', 'AbortError')),
                      { once: true }
                    );
                  }
                );
              },
              cancel: async () => {},
              releaseLock: () => {},
            }),
          },
        } as Response;
      }
    );
    global.fetch = fetchMock as typeof fetch;
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });

    let submit!: Promise<void>;
    await act(async () => {
      submit = result.current.handleSubmit('healthy after refresh');
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(bodyReadStarted).toBe(true);
    });

    await act(async () => {
      authListener?.('SIGNED_OUT', null);
      await Promise.resolve();
    });

    expect(storedRecovery()?.state).toBe('armed');
    expect(replaceMock).not.toHaveBeenCalled();
    expect(actualServiceErrorMock).not.toHaveBeenCalled();

    await act(async () => {
      result.current.handleStop();
      await submit;
    });
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
  });

  it('recovers on repeated 401 even when SIGNED_OUT was not emitted', async () => {
    let stateAtOpen: Record<string, unknown> | null = null;
    streamMessageMock.mockImplementation(
      async (_request: unknown, callbacks: StreamCallbacks) => {
        stateAtOpen = storedRecovery();
        callbacks.onAuthRefreshAttempt?.();
        callbacks.onError?.(
          'Authentication required',
          undefined,
          'authentication_required'
        );
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });

    await act(async () => {
      await result.current.handleSubmit('save me');
    });

    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(stateAtOpen?.state).toBe('armed');
    expect(storedRecovery()?.state).toBe('ready');
    expect(replaceMock).toHaveBeenCalledTimes(1);
  });

  it('still reaches sign-in when session storage refuses the draft', async () => {
    const realSessionStorage = window.sessionStorage;
    Object.defineProperty(window, 'sessionStorage', {
      configurable: true,
      value: {
        getItem: vi.fn().mockReturnValue(null),
        removeItem: vi.fn(),
        setItem: vi.fn(() => {
          throw new DOMException('denied', 'SecurityError');
        }),
      },
    });
    streamMessageMock.mockImplementation(
      async (_request: unknown, callbacks: StreamCallbacks) => {
        callbacks.onAuthRefreshAttempt?.();
        callbacks.onError?.(
          'Authentication required',
          undefined,
          'authentication_required'
        );
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });

    try {
      await act(async () => {
        await result.current.handleSubmit('cannot be stored');
      });
    } finally {
      Object.defineProperty(window, 'sessionStorage', {
        configurable: true,
        value: realSessionStorage,
      });
    }

    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    const recoveryUrl = replaceMock.mock.calls[0][0] as string;
    expect(recoveryUrl).toContain('reauth=chat');
    expect(recoveryUrl).not.toContain('draft=saved');
    expect(recoveryUrl).not.toContain('cannot');
  });

  it('still reaches generic sign-in when acquiring session storage is denied', async () => {
    const sessionStorageDescriptor = Object.getOwnPropertyDescriptor(
      window,
      'sessionStorage'
    );
    Object.defineProperty(window, 'sessionStorage', {
      configurable: true,
      get: () => {
        throw new DOMException('storage denied', 'SecurityError');
      },
    });
    streamMessageMock.mockImplementation(
      async (_request: unknown, callbacks: StreamCallbacks) => {
        callbacks.onAuthRefreshAttempt?.();
        callbacks.onError?.(
          'Authentication required',
          undefined,
          'authentication_required'
        );
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');

    try {
      const { result } = renderHook(() => useChatStreaming(makeParams()), {
        wrapper,
      });
      await act(async () => {
        await result.current.handleSubmit('cannot be stored');
      });
    } finally {
      if (sessionStorageDescriptor) {
        Object.defineProperty(
          window,
          'sessionStorage',
          sessionStorageDescriptor
        );
      }
    }

    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(replaceMock).toHaveBeenCalledTimes(1);
    const recoveryUrl = replaceMock.mock.calls[0][0] as string;
    expect(recoveryUrl).toContain('reauth=chat');
    expect(recoveryUrl).not.toContain('draft=saved');
    expect(recoveryUrl).not.toContain('cannot');
  });

  it('latches recovery when SIGNED_OUT and the final 401 both arrive', async () => {
    let pendingCallbacks: StreamCallbacks | undefined;
    let releaseStream: (() => void) | undefined;
    streamMessageMock.mockImplementation(
      (_request: unknown, callbacks: StreamCallbacks) => {
        pendingCallbacks = callbacks;
        callbacks.onAuthRefreshAttempt?.();
        authListener?.('SIGNED_OUT', null);
        return new Promise<void>((resolve) => {
          releaseStream = resolve;
        });
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });
    let submit!: Promise<void>;
    await act(async () => {
      submit = result.current.handleSubmit('one navigation only');
      await Promise.resolve();
    });
    await waitFor(() => expect(replaceMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      pendingCallbacks?.onError?.(
        'Authentication required',
        undefined,
        'authentication_required'
      );
      releaseStream?.();
      await submit;
    });

    expect(replaceMock).toHaveBeenCalledTimes(1);
    expect(storedRecovery()?.state).toBe('ready');
  });

  it('clears an armed draft when the user explicitly stops the stream', async () => {
    streamMessageMock.mockImplementation(
      (_request: unknown, _callbacks: StreamCallbacks, signal: AbortSignal) =>
        new Promise<void>((resolve) => {
          signal.addEventListener('abort', () => resolve(), { once: true });
        })
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });
    let submit!: Promise<void>;
    await act(async () => {
      submit = result.current.handleSubmit('stop this prompt');
      await Promise.resolve();
    });
    await waitFor(() => expect(streamMessageMock).toHaveBeenCalledTimes(1));
    expect(storedRecovery()?.state).toBe('armed');

    await act(async () => {
      result.current.handleStop();
      await submit;
    });

    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it('maps 403 locally without clearing auth or retaining the staged prompt', async () => {
    const setMessages = vi.fn();
    let stateAtOpen: Record<string, unknown> | null = null;
    streamMessageMock.mockImplementation(
      async (_request: unknown, callbacks: StreamCallbacks) => {
        stateAtOpen = storedRecovery();
        callbacks.onError?.('Forbidden', undefined, 'permission_denied');
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(
      () => useChatStreaming(makeParams(setMessages)),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleSubmit('not allowed');
    });

    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(stateAtOpen?.state).toBe('armed');
    expect(replaceMock).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
    expect(lastErrorCategory(setMessages)).toBe('permission_denied');
  });

  it('leaves network failures on the ordinary error path', async () => {
    const setMessages = vi.fn();
    let stateAtOpen: Record<string, unknown> | null = null;
    streamMessageMock.mockImplementation(() => {
      stateAtOpen = storedRecovery();
      return Promise.reject(new Error('offline'));
    });
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(
      () => useChatStreaming(makeParams(setMessages)),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleSubmit('try online');
    });

    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(stateAtOpen?.state).toBe('armed');
    expect(replaceMock).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
    expect(lastErrorCategory(setMessages)).toBe('exception');
  });

  it('does not let a late user-A rejection clear a newer user-B session', async () => {
    let stateAtOpen: Record<string, unknown> | null = null;
    streamMessageMock.mockImplementation(
      async (_request: unknown, callbacks: StreamCallbacks) => {
        stateAtOpen = storedRecovery();
        callbacks.onAuthRefreshAttempt?.();
        useAuthStore.setState({ user: USER_B, isAuthenticated: true });
        callbacks.onError?.(
          'Authentication required',
          undefined,
          'authentication_required'
        );
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });

    await act(async () => {
      await result.current.handleSubmit('belongs to A');
    });

    expect(useAuthStore.getState()).toEqual(
      expect.objectContaining({ user: USER_B, isAuthenticated: true })
    );
    expect(stateAtOpen?.ownerUserId).toBe('user-A');
    expect(replaceMock).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
  });

  it('returns an exhausted confirmation to its thread without saving prompt text', async () => {
    resumeStreamMock.mockImplementation(
      async (
        threadId: string,
        _after: number,
        callbacks: {
          onConfirmation?: (
            threadId: string,
            confirmation: Record<string, unknown>
          ) => void;
        }
      ) => {
        callbacks.onConfirmation?.(threadId, {
          tool_name: 'create_project_note',
          tool_args: { title: 'Private note' },
        });
        return { status: 'resumed' };
      }
    );
    streamConfirmMock.mockImplementation(
      async (_request: unknown, callbacks: StreamCallbacks) => {
        callbacks.onAuthRefreshAttempt?.();
        callbacks.onError?.(
          'Authentication required',
          undefined,
          'authentication_required'
        );
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });
    await waitFor(() =>
      expect(result.current.pendingConfirmation).not.toBeNull()
    );

    await act(async () => {
      await result.current.handleConfirmation(true);
    });

    expect(streamConfirmMock).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
    const recoveryUrl = replaceMock.mock.calls[0][0] as string;
    expect(recoveryUrl).toContain('reauth=chat');
    expect(recoveryUrl).not.toContain('draft=saved');
    expect(recoveryUrl).not.toContain('Private');
    expect(recoveryUrl).toContain(
      `next=${encodeURIComponent(`/chat?thread=${THREAD_ID}`)}`
    );
  });

  it.each([
    { accountAppears: false, expectedNavigations: 1 },
    { accountAppears: true, expectedNavigations: 0 },
  ])(
    'handles ownerless confirmation recovery with accountAppears=$accountAppears',
    async ({ accountAppears, expectedNavigations }) => {
      resumeStreamMock.mockImplementation(
        async (
          threadId: string,
          _after: number,
          callbacks: {
            onConfirmation?: (
              threadId: string,
              confirmation: Record<string, unknown>
            ) => void;
          }
        ) => {
          callbacks.onConfirmation?.(threadId, {
            tool_name: 'create_project_note',
            tool_args: { title: 'Private note' },
          });
          return { status: 'resumed' };
        }
      );
      streamConfirmMock.mockImplementation(
        async (_request: unknown, callbacks: StreamCallbacks) => {
          callbacks.onAuthRefreshAttempt?.();
          if (accountAppears) {
            useAuthStore.setState({ user: USER_B, isAuthenticated: true });
          }
          callbacks.onError?.(
            'Authentication required',
            undefined,
            'authentication_required'
          );
        }
      );
      const { useChatStreaming } =
        await import('@/hooks/chat/useChatStreaming');
      const { result } = renderHook(() => useChatStreaming(makeParams()), {
        wrapper,
      });
      await waitFor(() =>
        expect(result.current.pendingConfirmation).not.toBeNull()
      );
      act(() => {
        useAuthStore.setState({ user: null, isAuthenticated: true });
      });

      await act(async () => {
        await result.current.handleConfirmation(true);
      });

      expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
      expect(replaceMock).toHaveBeenCalledTimes(expectedNavigations);
      if (accountAppears) {
        expect(useAuthStore.getState()).toEqual(
          expect.objectContaining({ user: USER_B, isAuthenticated: true })
        );
      } else {
        expect(useAuthStore.getState().isAuthenticated).toBe(false);
        expect(replaceMock.mock.calls[0][0]).not.toContain('draft=saved');
      }
    }
  );

  it('closes the confirmation auth-loss window after a successful retry', async () => {
    resumeStreamMock.mockImplementation(
      async (
        threadId: string,
        _after: number,
        callbacks: {
          onConfirmation?: (
            threadId: string,
            confirmation: Record<string, unknown>
          ) => void;
        }
      ) => {
        callbacks.onConfirmation?.(threadId, {
          tool_name: 'create_project_note',
          tool_args: { title: 'Private note' },
        });
        return { status: 'resumed' };
      }
    );
    streamConfirmMock.mockImplementation(
      (_request: unknown, callbacks: StreamCallbacks, signal: AbortSignal) => {
        callbacks.onAuthRefreshAttempt?.();
        callbacks.onAuthRefreshSuccess?.();
        authListener?.('SIGNED_OUT', null);
        return new Promise<void>((resolve) => {
          signal.addEventListener('abort', () => resolve(), { once: true });
        });
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });
    await waitFor(() =>
      expect(result.current.pendingConfirmation).not.toBeNull()
    );

    let confirm!: Promise<void>;
    await act(async () => {
      confirm = result.current.handleConfirmation(true);
      await Promise.resolve();
    });

    expect(replaceMock).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
    await act(async () => {
      result.current.handleStop();
      await confirm;
    });
  });
});

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';
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
import type { UseChatStreamingParams } from '@/hooks/chat/useChatStreaming';
import { CHAT_AUTH_RECOVERY_STORAGE_KEY } from '@/hooks/chat/chatAuthRecovery';
import { useChatStore } from '@/store/chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import { useAuthStore } from '@/stores/authStore';

const THREAD_ID = 'thread-A';
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
};

let authListener: AuthListener | undefined;
let routeUnmount: (() => void) | undefined;
let useActualStreamMessage = false;
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
const realFetch = global.fetch;

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  useSearchParams: () => new URLSearchParams(),
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
  };
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
  beforeAll(async () => {
    await useAuthStore.getState().initialize();
    expect(authListener).toBeTypeOf('function');
  });

  beforeEach(() => {
    sessionStorage.clear();
    useActualStreamMessage = false;
    global.fetch = realFetch;
    replaceMock.mockReset();
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
    routeUnmount = undefined;
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
    routeUnmount?.();
    global.fetch = realFetch;
    useChatStore.setState({
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  it('survives a real SIGNED_OUT route unmount and restores without resending', async () => {
    const order: string[] = [];
    let stateAtNavigation: Record<string, unknown> | null = null;
    const setMessages = vi.fn();
    useActualStreamMessage = true;
    replaceMock.mockImplementation(() => {
      stateAtNavigation = storedRecovery();
      expect(stateAtNavigation?.state).toBe('ready');
      order.push('ready');
      queueMicrotask(() => {
        order.push('unmount');
        routeUnmount?.();
      });
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

    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const first = renderHook(() => useChatStreaming(makeParams(setMessages)), {
      wrapper,
    });
    routeUnmount = first.unmount;

    let submit!: Promise<void>;
    await act(async () => {
      submit = first.result.current.handleSubmit('keep this private prompt');
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
});

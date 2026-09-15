import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const navigationMocks = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
}));

const chatStoreMocks = vi.hoisted(() => {
  const state = {
    currentThreadId: null as string | null,
    messages: {},
    messageFreshness: {},
    messagePagination: {},
    messageLoadError: null,
    loadingThreadId: null as string | null,
    streamingThreadId: null as string | null,
    addMessageToStore: vi.fn(),
    loadMessages: vi.fn(),
    loadOlderMessages: vi.fn(),
    setCurrentThread: vi.fn(),
    registerThread: vi.fn(),
  };
  const useStore = Object.assign(
    <T,>(selector: (store: typeof state) => T) => selector(state),
    { getState: () => state }
  );
  return { state, useStore };
});

vi.mock('next/navigation', () => ({
  useRouter: () => ({
    push: navigationMocks.push,
    replace: navigationMocks.replace,
  }),
  useSearchParams: () =>
    new URLSearchParams({ thread: 'thread-1', panel: 'sources' }),
}));

vi.mock('@/stores/authStore', () => ({
  useAuthStore: () => ({ isAuthenticated: false }),
}));

vi.mock('@/store/chat-store', () => ({
  useChatStore: chatStoreMocks.useStore,
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {},
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

import { useChatSession } from '@/hooks/chat/useChatSession';

describe('useChatSession auth redirect recovery', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('preserves the selected thread when client auth requires login', async () => {
    renderHook(() => useChatSession());

    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(1500);
    });

    expect(navigationMocks.push).toHaveBeenCalledWith(
      '/login?next=%2Fchat%3Fthread%3Dthread-1'
    );
  });
});

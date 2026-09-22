import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

const workspaceMocks = vi.hoisted(() => ({
  getOrCreateDefaultWorkspace: vi.fn(),
  getOrCreateDefaultConversation: vi.fn(),
  listWorkspaceThreads: vi.fn(),
  listConversations: vi.fn(),
  getThread: vi.fn(),
}));

const chatStoreMocks = vi.hoisted(() => {
  const state = {
    currentThreadId: null as string | null,
    messages: {} as Record<string, []>,
    addMessageToStore: vi.fn(),
    loadMessages: vi.fn(),
    loadOlderMessages: vi.fn(),
    loadingThreadId: null as string | null,
    error: null as string | null,
    messagePagination: {},
    isLoadingMessages: false,
    setCurrentThread: vi.fn(),
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
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
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

import { useChatSession } from '@/hooks/chat/useChatSession';

describe('useChatSession watchdog', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    chatStoreMocks.state.currentThreadId = null;
    chatStoreMocks.state.setCurrentThread.mockImplementation(
      (threadId: string | null) => {
        chatStoreMocks.state.currentThreadId = threadId;
      }
    );
    localStorage.clear();
    workspaceMocks.getOrCreateDefaultConversation.mockResolvedValue({
      id: 'conv-1',
      title: 'New Chat',
    });
    workspaceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [],
      has_more: false,
    });
    workspaceMocks.listConversations.mockResolvedValue({ conversations: [] });
    chatStoreMocks.state.loadMessages.mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it('clears the timeout error when initialization eventually succeeds', async () => {
    let resolveWorkspace!: (workspace: { id: string; name: string }) => void;
    workspaceMocks.getOrCreateDefaultWorkspace.mockReturnValue(
      new Promise((resolve) => {
        resolveWorkspace = resolve;
      })
    );

    const { result } = renderHook(() => useChatSession());

    act(() => {
      vi.advanceTimersByTime(15_000);
    });
    expect(result.current.initError).toContain('taking longer than expected');

    await act(async () => {
      resolveWorkspace({ id: 'workspace-1', name: 'Workspace' });
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.isInitializing).toBe(false);
    expect(result.current.initError).toBeNull();
  });

  it('does not let late warm-start data replace a newer first-send selection', async () => {
    localStorage.setItem('default-conversation-id', 'conv-1');
    chatStoreMocks.state.currentThreadId = 'thread-old';
    workspaceMocks.getOrCreateDefaultWorkspace.mockResolvedValue({
      id: 'workspace-1',
      name: 'Workspace',
    });
    workspaceMocks.listWorkspaceThreads.mockResolvedValue({
      threads: [
        {
          id: 'thread-old',
          conversation_id: 'conv-1',
          title: 'Old thread',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
      ],
      has_more: false,
    });

    let resolveOldMessages!: () => void;
    chatStoreMocks.state.loadMessages.mockReturnValue(
      new Promise((resolve) => {
        resolveOldMessages = resolve;
      })
    );

    const { result, rerender } = renderHook(() => useChatSession());

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    // New-chat intent clears the old selection before the first optimistic
    // turn is created; the newly-created thread then adopts that overlay.
    act(() => {
      chatStoreMocks.state.currentThreadId = null;
      rerender();
    });
    act(() => {
      result.current.setConversations([
        {
          id: 'thread-new',
          title: 'New thread',
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
      chatStoreMocks.state.currentThreadId = 'thread-new';
      rerender();
    });

    await act(async () => {
      resolveOldMessages();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.activeThreadId).toBe('thread-new');
    expect(result.current.messages.map((message) => message.content)).toEqual([
      'new turn',
    ]);
    expect(chatStoreMocks.state.currentThreadId).toBe('thread-new');
    expect(workspaceMocks.getThread).not.toHaveBeenCalled();
    expect(chatStoreMocks.state.registerThread).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'thread-old' })
    );
    expect(result.current.isInitializing).toBe(false);
    expect(result.current.initError).toBeNull();
  });
});

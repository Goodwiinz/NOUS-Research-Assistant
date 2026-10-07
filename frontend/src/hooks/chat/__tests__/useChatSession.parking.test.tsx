/**
 * Round-3 M1: submit in a new chat, detour to another thread while
 * createThread is in flight, then the created thread activates. The parking
 * effect's second run used to wipe the live overlay (the in-flight turn
 * became invisible for the whole stream, composer locked).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { useChatStore } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    listWorkspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    createWorkspace: vi.fn(),
    listThreads: vi.fn().mockResolvedValue({ threads: [], total: 0 }),
    listMessages: vi.fn().mockResolvedValue({ messages: [], has_more: false }),
    createThread: vi.fn(),
    createMessage: vi.fn(),
  },
}));

import { useChatSession } from '@/hooks/chat/useChatSession';

describe('useChatSession overlay parking', () => {
  beforeEach(() => {
    useChatStore.getState().reset();
    useAuthStore.setState({ isAuthenticated: false } as never);
  });

  it('keeps the in-flight overlay when the created thread activates after a detour (M1)', async () => {
    const { result } = renderHook(() => useChatSession());

    const optimisticTurn = [
      makeChatPageMessage({
        role: 'user',
        content: 'first question',
        timestamp: 1,
        source: 'optimistic',
      }),
    ];

    // New chat: overlay holds the optimistic turn, no active thread yet.
    await act(async () => {
      result.current.setMessages(optimisticTurn);
    });

    // Detour: the user clicks thread B while createThread is still in flight.
    await act(async () => {
      useChatStore.setState({ currentThreadId: 'thread-B' });
    });

    // Creation resolves: the stream stamps the new thread and rebuilds the
    // overlay, then the new thread activates.
    const inFlightOverlay = [
      ...optimisticTurn,
      makeChatPageMessage({
        role: 'assistant',
        content: '',
        timestamp: 2,
        source: 'optimistic',
      }),
    ];
    await act(async () => {
      useChatStore.setState({ streamingThreadId: 'thread-new' });
      result.current.setMessages(inFlightOverlay);
      useChatStore.setState({ currentThreadId: 'thread-new' });
    });

    // The wipe made the whole in-flight turn invisible until commit.
    expect(result.current.messages).toHaveLength(2);
  });

  it('still clears the overlay on a plain thread switch', async () => {
    const { result } = renderHook(() => useChatSession());

    await act(async () => {
      useChatStore.setState({ currentThreadId: 'thread-A' });
    });
    await act(async () => {
      result.current.setMessages([
        makeChatPageMessage({
          id: 'a1',
          role: 'assistant',
          content: 'done answer',
          timestamp: 1,
          source: 'canonical',
        }),
      ]);
    });
    await act(async () => {
      useChatStore.setState({ currentThreadId: 'thread-B' });
    });

    expect(result.current.messages).toHaveLength(0);
  });

  it('FIFO-caps parked overlays on the ordinary switch-away path (L4)', async () => {
    const { result } = renderHook(() => useChatSession());

    // Park nine threads: switch INTO each, give it an overlay, then switch away
    // while it is the streaming thread. Nine is one past MAX_PARKED_THREADS.
    for (let i = 1; i <= 9; i += 1) {
      await act(async () => {
        useChatStore.setState({
          streamingThreadId: null,
          currentThreadId: `thread-${i}`,
        });
      });
      await act(async () => {
        result.current.setMessages([
          makeChatPageMessage({
            id: `m${i}`,
            role: 'user',
            content: `overlay ${i}`,
            timestamp: i,
            source: 'optimistic',
          }),
        ]);
      });
      await act(async () => {
        useChatStore.setState({
          streamingThreadId: `thread-${i}`,
          currentThreadId: `thread-${i + 1}`,
        });
      });
    }

    // Returning to the oldest parked thread must find nothing — the cap evicted
    // it. A bare `.set` on this path bypassed eviction, so the map grew for the
    // whole session and overlay 1 came back.
    await act(async () => {
      useChatStore.setState({
        streamingThreadId: null,
        currentThreadId: 'thread-1',
      });
    });

    expect(result.current.messages).toHaveLength(0);
  });
  it('clears A parked overlays when the account resets before the same thread is reopened', async () => {
    const { result } = renderHook(() => useChatSession());
    await act(async () => {
      useChatStore.setState({ currentThreadId: 'shared-thread' });
    });
    await act(async () => {
      result.current.setMessages([
        makeChatPageMessage({
          id: 'A-private',
          role: 'user',
          content: 'A unsaved draft',
          source: 'optimistic',
        }),
      ]);
    });
    await act(async () => {
      useChatStore.setState({
        streamingThreadId: 'shared-thread',
        currentThreadId: 'other-thread',
      });
    });
    await act(async () => {
      useChatStore.getState().reset();
    });
    await act(async () => {
      useChatStore.setState({ currentThreadId: 'shared-thread' });
    });
    expect(result.current.messages).toEqual([]);
  });
});

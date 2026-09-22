import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import type { Thread, ThreadListResponse } from '@/types/workspace';
import { ThreadStatus } from '@/types/workspace';

// ---- Mocks ----
// Unauthenticated => init effect settles immediately without DB fetches; we
// drive state via loadThreadsFromDb/loadMoreThreads directly (same pattern as
// the sibling I1 bleed test).
vi.mock('@/stores/authStore', () => ({
  useAuthStore: () => ({ isAuthenticated: false }),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const listWorkspaceThreadsMock = vi.fn();
const listMessagesMock = vi.fn();
vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    listWorkspaceThreads: (...args: unknown[]) =>
      listWorkspaceThreadsMock(...args),
    listMessages: (...args: unknown[]) => listMessagesMock(...args),
  },
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

import { useChatSession } from '@/hooks/chat/useChatSession';

function makeThreads(count: number, startIndex: number): Thread[] {
  return Array.from({ length: count }, (_, i) => {
    const idx = startIndex + i;
    return {
      id: `thread-${idx}`,
      conversation_id: 'conv-1',
      title: `Thread ${idx}`,
      status: ThreadStatus.ACTIVE,
      last_message_at: new Date().toISOString(),
      last_message_preview: `Latest message ${idx}`,
      message_count: 0,
      token_count: 0,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
  });
}

function threadPage(
  threads: Thread[],
  pageNum: number,
  total: number
): ThreadListResponse {
  return {
    threads,
    total,
    page: pageNum,
    limit: 50,
    has_more: pageNum * 50 < total,
  };
}

describe('useChatSession sidebar thread pagination (CX8)', () => {
  beforeEach(() => {
    listWorkspaceThreadsMock.mockReset();
    listMessagesMock.mockReset();
    listMessagesMock.mockResolvedValue({
      messages: [],
      total: 0,
      page: 1,
      limit: 100,
      has_more: false,
    });
  });

  it('exposes hasMoreThreads from the first page and loadMoreThreads appends the next page', async () => {
    listWorkspaceThreadsMock.mockResolvedValueOnce(
      threadPage(makeThreads(50, 0), 1, 120)
    );

    const { result } = renderHook(() => useChatSession());

    await act(async () => {
      await result.current.loadThreadsFromDb('conv-1');
    });

    expect(result.current.conversations).toHaveLength(50);
    expect(result.current.hasMoreThreads).toBe(true);
    expect(result.current.conversations[0].previewText).toBe(
      'Latest message 0'
    );

    listWorkspaceThreadsMock.mockResolvedValueOnce(
      threadPage(makeThreads(50, 50), 2, 120)
    );

    await act(async () => {
      await result.current.loadMoreThreads();
    });

    expect(listWorkspaceThreadsMock).toHaveBeenLastCalledWith('conv-1', {
      page: 2,
      limit: 50,
    });
    // Appends, does not replace.
    expect(result.current.conversations).toHaveLength(100);
    const ids = result.current.conversations.map((c) => c.id);
    expect(new Set(ids).size).toBe(100);
    expect(result.current.hasMoreThreads).toBe(true);
    expect(result.current.conversations[50].previewText).toBe(
      'Latest message 50'
    );
  });

  it('stops exposing hasMoreThreads once the last page is loaded', async () => {
    listWorkspaceThreadsMock.mockResolvedValueOnce(
      threadPage(makeThreads(50, 0), 1, 70)
    );
    const { result } = renderHook(() => useChatSession());

    await act(async () => {
      await result.current.loadThreadsFromDb('conv-1');
    });
    expect(result.current.hasMoreThreads).toBe(true);

    listWorkspaceThreadsMock.mockResolvedValueOnce(
      threadPage(makeThreads(20, 50), 2, 70)
    );
    await act(async () => {
      await result.current.loadMoreThreads();
    });

    expect(result.current.conversations).toHaveLength(70);
    expect(result.current.hasMoreThreads).toBe(false);
  });
});

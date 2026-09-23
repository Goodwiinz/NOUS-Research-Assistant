import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { threadSearchService } from '@/services/threadSearchService';
import type {
  ThreadSearchResponse,
  ThreadSearchResult,
} from '@/types/thread-search';

import { useThreadSearch } from '../useThreadSearch';

vi.mock('@/services/threadSearchService', () => ({
  threadSearchService: { searchThreads: vi.fn() },
}));

const mockedSearchThreads = vi.mocked(threadSearchService.searchThreads);

function wrapper({ children }: { children: ReactNode }): ReactNode {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function searchResult(
  threadId: string,
  overrides: Partial<ThreadSearchResult> = {}
): ThreadSearchResult {
  return {
    thread_id: threadId,
    title: `Thread ${threadId}`,
    summary: 'A matching summary',
    status: 'active',
    conversation_id: 'conversation-1',
    relevance_score: 1,
    message_count: 2,
    last_message_at: '2026-09-21T12:00:00Z',
    created_at: '2026-09-21T11:00:00Z',
    ...overrides,
  };
}

function response(
  query: string,
  results: ThreadSearchResult[],
  options: Partial<
    Pick<ThreadSearchResponse, 'offset' | 'has_more' | 'total_results'>
  > = {}
): ThreadSearchResponse {
  const offset = options.offset ?? 0;
  return {
    query,
    search_id: `search-${query}-${offset}`,
    results,
    total_results: options.total_results ?? results.length,
    returned_results: results.length,
    search_time_ms: 1,
    limit: 20,
    offset,
    has_more: options.has_more ?? false,
  };
}

async function advanceSearchDebounce(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 350));
}

describe('useThreadSearch', () => {
  beforeEach(() => {
    mockedSearchThreads.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('finds an older thread and scopes each request to the active workspace', async () => {
    mockedSearchThreads
      .mockResolvedValueOnce(response('older', [searchResult('old-thread')]))
      .mockResolvedValueOnce(response('older', [searchResult('other-thread')]));

    const { result, rerender } = renderHook(
      ({ workspaceId }: { workspaceId: string }) =>
        useThreadSearch(workspaceId),
      { initialProps: { workspaceId: 'workspace-a' }, wrapper }
    );

    act(() => result.current.setQuery('older'));
    await advanceSearchDebounce();
    await waitFor(() => expect(result.current.results).toHaveLength(1));

    expect(result.current.results[0].thread_id).toBe('old-thread');
    expect(mockedSearchThreads.mock.calls[0][0]).toMatchObject({
      query: 'older',
      filters: { workspace_id: 'workspace-a' },
      offset: 0,
    });

    rerender({ workspaceId: 'workspace-b' });
    await waitFor(() => expect(mockedSearchThreads).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(result.current.results[0].thread_id).toBe('other-thread')
    );
    expect(mockedSearchThreads.mock.calls[1][0].filters).toEqual({
      workspace_id: 'workspace-b',
    });
  });

  it('does not let an older response replace the current query', async () => {
    let resolveFirst!: (value: ThreadSearchResponse) => void;
    let resolveSecond!: (value: ThreadSearchResponse) => void;
    const first = new Promise<ThreadSearchResponse>((resolve) => {
      resolveFirst = resolve;
    });
    const second = new Promise<ThreadSearchResponse>((resolve) => {
      resolveSecond = resolve;
    });
    mockedSearchThreads.mockImplementation(({ query }) =>
      query === 'first' ? first : second
    );

    const { result } = renderHook(() => useThreadSearch('workspace-a'), {
      wrapper,
    });

    act(() => result.current.setQuery('first'));
    await advanceSearchDebounce();
    await waitFor(() => expect(mockedSearchThreads).toHaveBeenCalledTimes(1));

    act(() => result.current.setQuery('second'));
    await advanceSearchDebounce();
    await waitFor(() => expect(mockedSearchThreads).toHaveBeenCalledTimes(2));

    resolveSecond(response('second', [searchResult('second-thread')]));
    await waitFor(() =>
      expect(result.current.results[0].thread_id).toBe('second-thread')
    );

    resolveFirst(response('first', [searchResult('first-thread')]));
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.results[0].thread_id).toBe('second-thread');
  });

  it('reports empty and loading states, retries errors, and clears results', async () => {
    mockedSearchThreads
      .mockResolvedValueOnce(response('empty', []))
      .mockRejectedValueOnce(new Error('server detail'))
      .mockResolvedValueOnce(response('retry', [searchResult('retried')]));

    const { result } = renderHook(() => useThreadSearch('workspace-a'), {
      wrapper,
    });

    act(() => result.current.setQuery('empty'));
    await advanceSearchDebounce();
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.results).toEqual([]);
    expect(result.current.isError).toBe(false);

    act(() => result.current.setQuery('error'));
    await advanceSearchDebounce();
    await waitFor(() => expect(result.current.isError).toBe(true));

    act(() => result.current.setQuery('retry'));
    await advanceSearchDebounce();
    await waitFor(() => expect(result.current.isError).toBe(false));
    expect(result.current.results[0].thread_id).toBe('retried');

    act(() => result.current.setQuery(''));
    expect(result.current.results).toEqual([]);
    expect(result.current.hasMore).toBe(false);
  });

  it('loads another search page without replacing the first page', async () => {
    mockedSearchThreads
      .mockResolvedValueOnce(
        response('page', [searchResult('page-one')], {
          has_more: true,
          total_results: 2,
        })
      )
      .mockResolvedValueOnce(
        response('page', [searchResult('page-two')], {
          offset: 1,
          total_results: 2,
        })
      );

    const { result } = renderHook(() => useThreadSearch('workspace-a'), {
      wrapper,
    });
    act(() => result.current.setQuery('page'));
    await advanceSearchDebounce();
    await waitFor(() => expect(result.current.results).toHaveLength(1));
    expect(result.current.hasMore).toBe(true);

    await act(async () => {
      await result.current.loadMore();
    });
    await waitFor(() => expect(result.current.results).toHaveLength(2));
    expect(mockedSearchThreads.mock.calls[1][0]).toMatchObject({
      query: 'page',
      filters: { workspace_id: 'workspace-a' },
      offset: 1,
    });
  });

  it('keeps the first page visible when loading more results fails', async () => {
    mockedSearchThreads
      .mockResolvedValueOnce(
        response('partial', [searchResult('first-page')], {
          has_more: true,
          total_results: 2,
        })
      )
      .mockRejectedValueOnce(new Error('page unavailable'));

    const { result } = renderHook(() => useThreadSearch('workspace-a'), {
      wrapper,
    });
    act(() => result.current.setQuery('partial'));
    await advanceSearchDebounce();
    await waitFor(() => expect(result.current.results).toHaveLength(1));

    await act(async () => {
      await result.current.loadMore();
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.results[0].thread_id).toBe('first-page');
    expect(result.current.hasMore).toBe(true);
  });
});

'use client';

import { useInfiniteQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useState } from 'react';

import { threadSearchService } from '@/services/threadSearchService';
import type { ThreadSearchResult } from '@/types/thread-search';

export const THREAD_SEARCH_PAGE_SIZE = 20;
const THREAD_SEARCH_DEBOUNCE_MS = 300;

export interface UseThreadSearchResult {
  query: string;
  setQuery: (query: string) => void;
  results: ThreadSearchResult[];
  totalResults: number;
  hasMore: boolean;
  isLoading: boolean;
  isFetchingNextPage: boolean;
  isError: boolean;
  retry: () => Promise<unknown>;
  loadMore: () => Promise<void>;
}

/**
 * Searches the active workspace without touching the canonical sidebar list.
 * The query key is also the stale-response guard: TanStack Query only exposes
 * the response for the current workspace + debounced term to this observer.
 */
export function useThreadSearch(
  workspaceId: string | null | undefined
): UseThreadSearchResult {
  const [query, setQuery] = useState('');
  const trimmedQuery = query.trim();
  const [debouncedQuery, setDebouncedQuery] = useState('');

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      setDebouncedQuery(trimmedQuery);
    }, THREAD_SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timeout);
  }, [trimmedQuery]);

  const canSearch = debouncedQuery.length >= 2 && Boolean(workspaceId);
  const queryMatchesInput = debouncedQuery === trimmedQuery;

  const searchQuery = useInfiniteQuery({
    queryKey: ['chat-thread-search', workspaceId ?? null, debouncedQuery],
    queryFn: ({ pageParam, signal }) =>
      threadSearchService.searchThreads(
        {
          query: debouncedQuery,
          filters: { workspace_id: workspaceId as string },
          sort_order: 'relevance',
          limit: THREAD_SEARCH_PAGE_SIZE,
          offset: pageParam,
        },
        { signal }
      ),
    initialPageParam: 0,
    getNextPageParam: (lastPage) =>
      lastPage.has_more
        ? lastPage.offset + lastPage.returned_results
        : undefined,
    enabled: canSearch,
    retry: false,
    staleTime: 0,
  });

  const pages = useMemo(
    () =>
      queryMatchesInput && canSearch ? (searchQuery.data?.pages ?? []) : [],
    [canSearch, queryMatchesInput, searchQuery.data?.pages]
  );
  const results = useMemo(
    () => pages?.flatMap((page) => page.results) ?? [],
    [pages]
  );
  const firstPage = pages?.[0];
  const isSearching = trimmedQuery.length >= 2 && Boolean(workspaceId);

  const retry = useCallback(() => searchQuery.refetch(), [searchQuery]);

  const loadMore = useCallback(async () => {
    if (!searchQuery.hasNextPage || searchQuery.isFetchingNextPage) return;
    await searchQuery.fetchNextPage();
  }, [searchQuery]);

  return {
    query,
    setQuery,
    results,
    totalResults: firstPage?.total_results ?? 0,
    hasMore: queryMatchesInput && canSearch && Boolean(searchQuery.hasNextPage),
    isLoading:
      isSearching &&
      (!queryMatchesInput ||
        searchQuery.isPending ||
        (searchQuery.isFetching && !searchQuery.isFetchingNextPage)),
    isFetchingNextPage: searchQuery.isFetchingNextPage,
    isError: isSearching && queryMatchesInput && searchQuery.isError,
    retry,
    loadMore,
  };
}

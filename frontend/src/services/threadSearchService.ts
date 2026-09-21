/**
 * Thread and Message Search Service
 *
 * API client for full-text search across threads and messages
 */

import { api } from '@/services/api-client';
import type {
  ThreadSearchRequest,
  ThreadSearchResponse,
  MessageSearchRequest,
  MessageSearchResponse,
  CombinedSearchResponse,
  SearchSuggestionsResponse,
  SearchHealthResponse,
} from '@/types/thread-search';

const SEARCH_BASE_URL = '/api/v2/search';

/**
 * Search threads using full-text search
 */
export async function searchThreads(
  request: ThreadSearchRequest,
  options: { signal?: AbortSignal } = {}
): Promise<ThreadSearchResponse> {
  const response = await api.post<ThreadSearchResponse>(
    `${SEARCH_BASE_URL}/threads`,
    request,
    options
  );
  return response;
}

/**
 * Search threads using GET with query parameters
 */
export async function searchThreadsGet(params: {
  query: string;
  conversation_id?: string;
  workspace_id?: string;
  status_filter?: string[];
  date_from?: string;
  date_to?: string;
  min_message_count?: number;
  sort_order?: string;
  limit?: number;
  offset?: number;
}): Promise<ThreadSearchResponse> {
  const searchParams = new URLSearchParams();
  searchParams.append('query', params.query);

  if (params.conversation_id)
    searchParams.append('conversation_id', params.conversation_id);
  if (params.workspace_id)
    searchParams.append('workspace_id', params.workspace_id);
  if (params.status_filter) {
    params.status_filter.forEach((s) =>
      searchParams.append('status_filter', s)
    );
  }
  if (params.date_from) searchParams.append('date_from', params.date_from);
  if (params.date_to) searchParams.append('date_to', params.date_to);
  if (params.min_message_count !== undefined) {
    searchParams.append(
      'min_message_count',
      params.min_message_count.toString()
    );
  }
  if (params.sort_order) searchParams.append('sort_order', params.sort_order);
  if (params.limit !== undefined)
    searchParams.append('limit', params.limit.toString());
  if (params.offset !== undefined)
    searchParams.append('offset', params.offset.toString());

  const response = await api.get<ThreadSearchResponse>(
    `${SEARCH_BASE_URL}/threads?${searchParams.toString()}`
  );
  return response;
}

/**
 * Search messages using full-text search
 */
export async function searchMessages(
  request: MessageSearchRequest
): Promise<MessageSearchResponse> {
  const response = await api.post<MessageSearchResponse>(
    `${SEARCH_BASE_URL}/messages`,
    request
  );
  return response;
}

/**
 * Search messages using GET with query parameters
 */
export async function searchMessagesGet(params: {
  query: string;
  thread_id?: string;
  conversation_id?: string;
  workspace_id?: string;
  user_id?: string;
  roles?: string[];
  date_from?: string;
  date_to?: string;
  has_citations?: boolean;
  sort_order?: string;
  limit?: number;
  offset?: number;
}): Promise<MessageSearchResponse> {
  const searchParams = new URLSearchParams();
  searchParams.append('query', params.query);

  if (params.thread_id) searchParams.append('thread_id', params.thread_id);
  if (params.conversation_id)
    searchParams.append('conversation_id', params.conversation_id);
  if (params.workspace_id)
    searchParams.append('workspace_id', params.workspace_id);
  if (params.user_id) searchParams.append('user_id', params.user_id);
  if (params.roles) {
    params.roles.forEach((r) => searchParams.append('roles', r));
  }
  if (params.date_from) searchParams.append('date_from', params.date_from);
  if (params.date_to) searchParams.append('date_to', params.date_to);
  if (params.has_citations !== undefined) {
    searchParams.append('has_citations', params.has_citations.toString());
  }
  if (params.sort_order) searchParams.append('sort_order', params.sort_order);
  if (params.limit !== undefined)
    searchParams.append('limit', params.limit.toString());
  if (params.offset !== undefined)
    searchParams.append('offset', params.offset.toString());

  const response = await api.get<MessageSearchResponse>(
    `${SEARCH_BASE_URL}/messages?${searchParams.toString()}`
  );
  return response;
}

/**
 * Combined search across threads and messages
 */
export async function combinedSearch(params: {
  query: string;
  workspace_id?: string;
  conversation_id?: string;
  limit?: number;
}): Promise<CombinedSearchResponse> {
  const searchParams = new URLSearchParams();
  searchParams.append('query', params.query);

  if (params.workspace_id)
    searchParams.append('workspace_id', params.workspace_id);
  if (params.conversation_id)
    searchParams.append('conversation_id', params.conversation_id);
  if (params.limit !== undefined)
    searchParams.append('limit', params.limit.toString());

  const response = await api.get<CombinedSearchResponse>(
    `${SEARCH_BASE_URL}/combined?${searchParams.toString()}`
  );
  return response;
}

/**
 * Get search suggestions based on partial query
 */
export async function getSearchSuggestions(params: {
  query: string;
  workspace_id?: string;
  limit?: number;
}): Promise<SearchSuggestionsResponse> {
  const searchParams = new URLSearchParams();
  searchParams.append('query', params.query);

  if (params.workspace_id)
    searchParams.append('workspace_id', params.workspace_id);
  if (params.limit !== undefined)
    searchParams.append('limit', params.limit.toString());

  const response = await api.get<SearchSuggestionsResponse>(
    `${SEARCH_BASE_URL}/suggestions?${searchParams.toString()}`
  );
  return response;
}

/**
 * Check search system health
 */
export async function checkSearchHealth(): Promise<SearchHealthResponse> {
  const response = await api.get<SearchHealthResponse>(
    `${SEARCH_BASE_URL}/health`
  );
  return response;
}

// Export all functions as a service object
export const threadSearchService = {
  searchThreads,
  searchThreadsGet,
  searchMessages,
  searchMessagesGet,
  combinedSearch,
  getSearchSuggestions,
  checkSearchHealth,
};

export default threadSearchService;

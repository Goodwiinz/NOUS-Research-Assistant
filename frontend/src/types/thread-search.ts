/**
 * Thread and Message Search Types
 *
 * Types for full-text search functionality across threads and messages
 */

import type { components } from '@/types/generated/api';

// ============================================================================
// Enums
// ============================================================================

export type ThreadSearchSortOrder =
  components['schemas']['ThreadSearchSortOrder'];

export type MessageSearchSortOrder =
  components['schemas']['MessageSearchSortOrder'];

export type ThreadStatus = components['schemas']['ThreadStatus-Output'];
export type MessageRole = components['schemas']['MessageRole-Output'];

// ============================================================================
// Filter Types
// ============================================================================

export type ThreadSearchFilter = components['schemas']['ThreadSearchFilter'];

export type MessageSearchFilter = components['schemas']['MessageSearchFilter'];

// ============================================================================
// Request Types
// ============================================================================

type ApiThreadSearchRequest = components['schemas']['ThreadSearchRequest'];

// openapi-typescript marks Pydantic defaults as required. The service accepts
// those fields as optional so the backend can apply its documented defaults.
export type ThreadSearchRequest = Omit<
  ApiThreadSearchRequest,
  | 'filters'
  | 'sort_order'
  | 'limit'
  | 'offset'
  | 'include_snippets'
  | 'include_messages'
> &
  Partial<
    Pick<
      ApiThreadSearchRequest,
      | 'filters'
      | 'sort_order'
      | 'limit'
      | 'offset'
      | 'include_snippets'
      | 'include_messages'
    >
  >;

type ApiMessageSearchRequest = components['schemas']['MessageSearchRequest'];

export type MessageSearchRequest = Omit<
  ApiMessageSearchRequest,
  'filters' | 'sort_order' | 'limit' | 'offset' | 'include_context'
> &
  Partial<
    Pick<
      ApiMessageSearchRequest,
      'filters' | 'sort_order' | 'limit' | 'offset' | 'include_context'
    >
  >;

// ============================================================================
// Result Types
// ============================================================================

type ApiThreadSearchResult = components['schemas']['ThreadSearchResult'];

export type ThreadSearchResult = Omit<
  ApiThreadSearchResult,
  | 'highlighted_title'
  | 'highlighted_summary'
  | 'matching_message_count'
  | 'status'
> & {
  status: ThreadStatus;
  highlighted_title?: string;
  highlighted_summary?: string;
  matching_message_count?: number;
};

export type MessageSearchResult = components['schemas']['MessageSearchResult'];

export type CombinedSearchResult =
  components['schemas']['CombinedSearchResult'];

// ============================================================================
// Response Types
// ============================================================================

type ApiThreadSearchResponse = components['schemas']['ThreadSearchResponse'];

export type ThreadSearchResponse = Omit<ApiThreadSearchResponse, 'results'> & {
  results: ThreadSearchResult[];
};

export type MessageSearchResponse =
  components['schemas']['MessageSearchResponse'];

export type CombinedSearchResponse =
  components['schemas']['CombinedSearchResponse'];

export interface SearchSuggestionsResponse {
  query: string;
  suggestions: string[];
}

export interface SearchHealthResponse {
  status: 'healthy' | 'degraded' | 'unhealthy';
  search_functional: boolean;
  gin_indexes: Array<{
    name: string;
    definition: string;
  }>;
  index_count: number;
  error?: string;
}

// ============================================================================
// UI State Types
// ============================================================================

export interface ThreadSearchState {
  query: string;
  results: ThreadSearchResult[];
  isLoading: boolean;
  error: string | null;
  totalResults: number;
  hasMore: boolean;
  searchTime: number;
  filters: ThreadSearchFilter;
  sortOrder: ThreadSearchSortOrder;
}

export interface MessageSearchState {
  query: string;
  results: MessageSearchResult[];
  isLoading: boolean;
  error: string | null;
  totalResults: number;
  hasMore: boolean;
  searchTime: number;
  filters: MessageSearchFilter;
  sortOrder: MessageSearchSortOrder;
}

export interface CombinedSearchState {
  query: string;
  results: CombinedSearchResult[];
  isLoading: boolean;
  error: string | null;
  totalResults: number;
  hasMore: boolean;
  searchTime: number;
}

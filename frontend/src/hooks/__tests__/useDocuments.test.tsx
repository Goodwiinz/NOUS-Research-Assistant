/**
 * Unit tests for useDocuments hook
 *
 * Tests initial state, document fetching, loading/error states,
 * pagination, selection, and authentication handling.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useDocuments } from '../useDocuments';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockGet = vi.fn();
const mockDelete = vi.fn();
const mockPost = vi.fn();

vi.mock('@/services/api-client', () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
}));

const mockHandleAuthError = vi.fn();

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    isAuthenticated: true,
    isLoading: false,
    handleAuthError: mockHandleAuthError,
  }),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Suppress console noise during tests */
beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'log').mockImplementation();
  vi.spyOn(console, 'warn').mockImplementation();
  vi.spyOn(console, 'error').mockImplementation();
});

afterEach(() => {
  vi.restoreAllMocks();
});

interface BackendDocumentsResponse {
  documents: Array<{
    id: string;
    uploaded_by_user_id: string;
    organization_id: string;
    title: string;
    filename: string;
    document_type: string;
    file_size_bytes: number;
    processing_status: string;
    created_at: string;
  }>;
  pagination: {
    page: number;
    page_size: number;
    total: number;
    total_pages: number;
    has_next: boolean;
    has_prev: boolean;
  };
}

/** Create a realistic backend document response. */
function createBackendResponse(
  count: number = 2,
  page: number = 1,
  pageSize: number = 20
): BackendDocumentsResponse {
  const documents = Array.from({ length: count }, (_, i) => ({
    id: `doc-${i + 1}`,
    uploaded_by_user_id: 'user-1',
    organization_id: 'org-1',
    title: `Document ${i + 1}`,
    filename: `doc-${i + 1}.pdf`,
    document_type: 'pdf',
    file_size_bytes: 1024 * (i + 1),
    processing_status: 'indexed',
    created_at: '2024-01-01T00:00:00Z',
  }));

  return {
    documents,
    pagination: {
      page,
      page_size: pageSize,
      total: count,
      total_pages: Math.ceil(count / pageSize),
      has_next: false,
      has_prev: page > 1,
    },
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useDocuments', () => {
  // =========================================================================
  // Initial state
  // =========================================================================

  describe('initial state', () => {
    it('returns correct default state', async () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      expect(result.current.documents).toEqual([]);
      expect(result.current.loading).toBe(false);
      expect(result.current.error).toBeNull();
      expect(result.current.pagination).toEqual({
        page: 1,
        pageSize: 20,
        total: 0,
        totalPages: 0,
        hasNext: false,
        hasPrev: false,
      });
      expect(result.current.filters).toEqual({});
      expect(result.current.selectedDocuments).toEqual(new Set());
      expect(result.current.selectedCount).toBe(0);
      expect(result.current.hasSelection).toBe(false);
      // When documents and selection are both empty, size === length (0 === 0) is true
      expect(result.current.isAllSelected).toBe(true);
    });

    it('respects custom initialPageSize', () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      const { result } = renderHook(() =>
        useDocuments({ initialPageSize: 50, autoFetch: false })
      );

      expect(result.current.pagination.pageSize).toBe(50);
    });
  });

  // =========================================================================
  // Fetching documents
  // =========================================================================

  describe('fetchDocuments', () => {
    it('fetches documents and transforms backend response', async () => {
      mockGet.mockResolvedValue(createBackendResponse(2));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      expect(result.current.documents).toHaveLength(2);
      expect(result.current.documents[0]).toMatchObject({
        id: 'doc-1',
        title: 'Document 1',
        filename: 'doc-1.pdf',
        file_type: 'pdf',
        file_size: 1024,
        processing_status: 'indexed',
      });
    });

    it('updates pagination from response', async () => {
      mockGet.mockResolvedValue(createBackendResponse(30, 2, 10));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments(2, 10);
      });

      expect(result.current.pagination.page).toBe(2);
      expect(result.current.pagination.pageSize).toBe(10);
      expect(result.current.pagination.total).toBe(30);
    });

    it('calls API with correct params', async () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments(1, 20);
      });

      expect(mockGet).toHaveBeenCalledWith(
        expect.stringContaining('/documents/?')
      );
      const calledUrl = mockGet.mock.calls[0][0] as string;
      expect(calledUrl).toContain('page=1');
      expect(calledUrl).toContain('page_size=20');
    });

    it('refreshes with the latest page, page size, and filters synchronously', async () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      const { result } = renderHook(() =>
        useDocuments({ initialPageSize: 40, autoFetch: false })
      );

      act(() => {
        result.current.updateFilters({ search_term: 'Exact Paper Title' });
      });
      act(() => {
        result.current.updatePage(3);
      });
      mockGet.mockClear();

      let refreshPromise: Promise<void>;
      let requestedUrl = '';
      act(() => {
        refreshPromise = result.current.fetchDocuments();
        requestedUrl = mockGet.mock.calls[0]?.[0] as string;
      });

      const query = new URL(requestedUrl, 'http://localhost').searchParams;
      expect(query.get('page')).toBe('3');
      expect(query.get('page_size')).toBe('40');
      expect(query.get('search')).toBe('Exact Paper Title');

      await act(async () => {
        await refreshPromise!;
      });
    });

    /**
     * Mutation check: frontend/src/hooks/useDocuments.ts:340 latest-request
     * guard immediately before the successful response commit. Focused command:
     * pnpm --dir frontend test src/hooks/__tests__/useDocuments.test.tsx -t
     * "keeps the newer document page when an older request resolves last"
     */
    it('keeps the newer document page when an older request resolves last', async () => {
      let resolveOlder!: (value: BackendDocumentsResponse) => void;
      let resolveNewer!: (value: BackendDocumentsResponse) => void;
      mockGet
        .mockReturnValueOnce(
          new Promise<BackendDocumentsResponse>((resolve) => {
            resolveOlder = resolve;
          })
        )
        .mockReturnValueOnce(
          new Promise<BackendDocumentsResponse>((resolve) => {
            resolveNewer = resolve;
          })
        );

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      let olderRequest!: Promise<void>;
      let newerRequest!: Promise<void>;
      act(() => {
        olderRequest = result.current.fetchDocuments(1, 20);
        newerRequest = result.current.fetchDocuments(2, 20);
      });

      await act(async () => {
        const response = createBackendResponse(1, 2, 20);
        response.documents[0].title = 'Newer page';
        resolveNewer(response);
        await newerRequest;
      });

      await act(async () => {
        const response = createBackendResponse(1, 1, 20);
        response.documents[0].title = 'Older page';
        resolveOlder(response);
        await olderRequest;
      });

      expect(result.current.documents[0]?.title).toBe('Newer page');
      expect(result.current.pagination.page).toBe(2);
    });
  });

  // =========================================================================
  // Loading state
  // =========================================================================

  describe('loading state', () => {
    it('sets loading to true while fetching', async () => {
      let resolvePromise: (value: unknown) => void;
      const promise = new Promise((resolve) => {
        resolvePromise = resolve;
      });
      mockGet.mockReturnValue(promise);

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      // Start fetch but don't await
      let fetchPromise: Promise<void>;
      act(() => {
        fetchPromise = result.current.fetchDocuments();
      });

      // Loading should be true
      expect(result.current.loading).toBe(true);

      // Resolve and complete
      await act(async () => {
        resolvePromise!(createBackendResponse(0));
        await fetchPromise!;
      });

      expect(result.current.loading).toBe(false);
    });

    it('sets loading to false after successful fetch', async () => {
      mockGet.mockResolvedValue(createBackendResponse(1));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      expect(result.current.loading).toBe(false);
    });
  });

  // =========================================================================
  // Error handling
  // =========================================================================

  describe('error handling', () => {
    it('sets error message on fetch failure', async () => {
      mockGet.mockRejectedValue(new Error('Network error'));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      expect(result.current.error).toBe('Network error');
      expect(result.current.loading).toBe(false);
    });

    it('sets error when response is empty (null/undefined)', async () => {
      mockGet.mockResolvedValue(null);

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      expect(result.current.error).toBe('No response from server');
      expect(result.current.loading).toBe(false);
    });

    it('sets error when response has no pagination field', async () => {
      mockGet.mockResolvedValue({ documents: [] });

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      expect(result.current.error).toBe('Invalid response format from server');
      expect(result.current.loading).toBe(false);
    });

    it('clears previous error on new successful fetch', async () => {
      // First call fails
      mockGet.mockRejectedValueOnce(new Error('Temporary error'));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      expect(result.current.error).toBe('Temporary error');

      // Second call succeeds
      mockGet.mockResolvedValueOnce(createBackendResponse(1));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      expect(result.current.error).toBeNull();
    });
  });

  // =========================================================================
  // Filters
  // =========================================================================

  describe('updateFilters', () => {
    it('updates filters and resets page to 1', async () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      act(() => {
        result.current.updateFilters({ search_term: 'test' });
      });

      expect(result.current.filters).toEqual({ search_term: 'test' });
      expect(result.current.pagination.page).toBe(1);
    });

    it('merges with existing filters', () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      act(() => {
        result.current.updateFilters({ search_term: 'test' });
      });

      act(() => {
        result.current.updateFilters({ file_types: ['pdf'] });
      });

      expect(result.current.filters).toEqual({
        search_term: 'test',
        file_types: ['pdf'],
      });
    });

    it('requests an exact title search with the configured page size', () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      const { result } = renderHook(() =>
        useDocuments({ initialPageSize: 50, autoFetch: false })
      );

      let requestedUrl = '';
      act(() => {
        result.current.updateFilters({ status: ['indexed'] });
        result.current.updateFilters({
          search_term: 'Attention Is All You Need',
        });
        requestedUrl = mockGet.mock.calls[1]?.[0] as string;
      });

      const query = new URL(requestedUrl, 'http://localhost').searchParams;
      expect(query.get('page')).toBe('1');
      expect(query.get('page_size')).toBe('50');
      expect(query.get('search')).toBe('Attention Is All You Need');
      expect(query.get('processing_status')).toBe('indexed');
    });

    it('merges batched filter updates into the second request', () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      let secondRequestedUrl = '';
      act(() => {
        result.current.updateFilters({ search_term: 'transformers' });
        result.current.updateFilters({ file_types: ['pdf'] });
        secondRequestedUrl = mockGet.mock.calls[1]?.[0] as string;
      });

      const query = new URL(secondRequestedUrl, 'http://localhost')
        .searchParams;
      expect(query.get('search')).toBe('transformers');
      expect(query.get('document_type')).toBe('pdf');
    });
  });

  // =========================================================================
  // Pagination helpers
  // =========================================================================

  describe('pagination actions', () => {
    it('updatePage changes the current page', () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      act(() => {
        result.current.updatePage(3);
      });

      expect(result.current.pagination.page).toBe(3);
    });

    it('updatePageSize changes page size and resets page to 1', () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      act(() => {
        result.current.updatePage(5);
      });

      act(() => {
        result.current.updatePageSize(50);
      });

      expect(result.current.pagination.pageSize).toBe(50);
      expect(result.current.pagination.page).toBe(1);
    });

    it('requests a new page with the current filters and page size', () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      const { result } = renderHook(() =>
        useDocuments({ initialPageSize: 40, autoFetch: false })
      );
      act(() => {
        result.current.updateFilters({ status: ['indexed'] });
      });
      mockGet.mockClear();

      let requestedUrl = '';
      act(() => {
        result.current.updatePage(4);
        requestedUrl = mockGet.mock.calls[0]?.[0] as string;
      });

      const query = new URL(requestedUrl, 'http://localhost').searchParams;
      expect(query.get('page')).toBe('4');
      expect(query.get('page_size')).toBe('40');
      expect(query.get('processing_status')).toBe('indexed');
    });

    it('requests a new page size with the current filters', () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));
      act(() => {
        result.current.updateFilters({ file_types: ['pdf'] });
      });
      mockGet.mockClear();

      let requestedUrl = '';
      act(() => {
        result.current.updatePageSize(75);
        requestedUrl = mockGet.mock.calls[0]?.[0] as string;
      });

      const query = new URL(requestedUrl, 'http://localhost').searchParams;
      expect(query.get('page')).toBe('1');
      expect(query.get('page_size')).toBe('75');
      expect(query.get('document_type')).toBe('pdf');
    });
  });

  // =========================================================================
  // Document selection
  // =========================================================================

  describe('selection', () => {
    it('selectDocument toggles a document in the selection set', async () => {
      mockGet.mockResolvedValue(createBackendResponse(2));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      act(() => {
        result.current.selectDocument('doc-1');
      });

      expect(result.current.selectedDocuments.has('doc-1')).toBe(true);
      expect(result.current.selectedCount).toBe(1);
      expect(result.current.hasSelection).toBe(true);

      // Toggle off
      act(() => {
        result.current.selectDocument('doc-1');
      });

      expect(result.current.selectedDocuments.has('doc-1')).toBe(false);
      expect(result.current.selectedCount).toBe(0);
    });

    it('selectAllDocuments selects all loaded documents', async () => {
      mockGet.mockResolvedValue(createBackendResponse(3));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      act(() => {
        result.current.selectAllDocuments();
      });

      expect(result.current.selectedCount).toBe(3);
      expect(result.current.isAllSelected).toBe(true);
    });

    it('clearSelection empties the selection set', async () => {
      mockGet.mockResolvedValue(createBackendResponse(2));

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      act(() => {
        result.current.selectAllDocuments();
      });

      expect(result.current.selectedCount).toBe(2);

      act(() => {
        result.current.clearSelection();
      });

      expect(result.current.selectedCount).toBe(0);
      expect(result.current.hasSelection).toBe(false);
    });
  });

  // =========================================================================
  // Auto-fetch on mount
  // =========================================================================

  describe('autoFetch', () => {
    it('automatically fetches documents when autoFetch is true (default)', async () => {
      mockGet.mockResolvedValue(createBackendResponse(1));

      renderHook(() => useDocuments());

      await waitFor(() => {
        expect(mockGet).toHaveBeenCalledWith(
          expect.stringContaining('/documents/?')
        );
      });
    });

    it('does not auto-fetch when autoFetch is false', () => {
      mockGet.mockResolvedValue(createBackendResponse(0));

      renderHook(() => useDocuments({ autoFetch: false }));

      expect(mockGet).not.toHaveBeenCalled();
    });
  });

  // =========================================================================
  // Data transformation
  // =========================================================================

  describe('document transformation', () => {
    it('normalizes processing_status to valid values', async () => {
      const response = {
        documents: [
          {
            id: 'doc-1',
            processing_status: 'UNKNOWN_STATUS',
            filename: 'test.pdf',
          },
        ],
        pagination: {
          page: 1,
          page_size: 20,
          total: 1,
          has_next: false,
          has_prev: false,
        },
      };
      mockGet.mockResolvedValue(response);

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      // Unknown status should default to 'queued'
      expect(result.current.documents[0].processing_status).toBe('queued');
    });

    it('normalizes file_type to valid values', async () => {
      const response = {
        documents: [
          {
            id: 'doc-1',
            document_type: 'UNKNOWN_TYPE',
            filename: 'test.xyz',
          },
        ],
        pagination: {
          page: 1,
          page_size: 20,
          total: 1,
          has_next: false,
          has_prev: false,
        },
      };
      mockGet.mockResolvedValue(response);

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      // Unknown file type should default to 'pdf'
      expect(result.current.documents[0].file_type).toBe('pdf');
    });

    it('uses title fallback when title is missing', async () => {
      const response = {
        documents: [
          {
            id: 'doc-1',
            filename: 'fallback-name.pdf',
            // no title
          },
        ],
        pagination: {
          page: 1,
          page_size: 20,
          total: 1,
          has_next: false,
          has_prev: false,
        },
      };
      mockGet.mockResolvedValue(response);

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      expect(result.current.documents[0].title).toBe('fallback-name.pdf');
    });
  });

  // =========================================================================
  // Polling (replaces the FE WebSocket realtime stack — R4-H1/H2/H3)
  // =========================================================================

  describe('polling for non-terminal documents', () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it('polls the list every 5s while a document is queued/processing', async () => {
      // Stays 'processing' across every poll in this test.
      mockGet.mockResolvedValue({
        documents: [
          {
            id: 'doc-1',
            filename: 'doc-1.pdf',
            processing_status: 'processing',
          },
        ],
        pagination: {
          page: 1,
          page_size: 20,
          total: 1,
          has_next: false,
          has_prev: false,
        },
      });

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      expect(result.current.documents[0]?.processing_status).toBe('processing');
      expect(result.current.isPolling).toBe(true);
      expect(mockGet).toHaveBeenCalledTimes(1);

      // Advance past one poll interval — a refetch should fire.
      await act(async () => {
        vi.advanceTimersByTime(5000);
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(mockGet).toHaveBeenCalledTimes(2);

      // Another interval — polling keeps going while still non-terminal.
      await act(async () => {
        vi.advanceTimersByTime(5000);
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(mockGet).toHaveBeenCalledTimes(3);
    });

    it('stops polling once every document reaches a terminal status', async () => {
      mockGet.mockResolvedValueOnce({
        documents: [
          {
            id: 'doc-1',
            filename: 'doc-1.pdf',
            processing_status: 'processing',
          },
        ],
        pagination: {
          page: 1,
          page_size: 20,
          total: 1,
          has_next: false,
          has_prev: false,
        },
      });

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      expect(result.current.isPolling).toBe(true);

      // The next poll comes back indexed — polling should stop.
      mockGet.mockResolvedValueOnce({
        documents: [
          {
            id: 'doc-1',
            filename: 'doc-1.pdf',
            processing_status: 'indexed',
          },
        ],
        pagination: {
          page: 1,
          page_size: 20,
          total: 1,
          has_next: false,
          has_prev: false,
        },
      });

      await act(async () => {
        vi.advanceTimersByTime(5000);
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(result.current.documents[0]?.processing_status).toBe('indexed');
      expect(result.current.isPolling).toBe(false);
      expect(mockGet).toHaveBeenCalledTimes(2);

      // No further polling — advancing time causes no additional calls.
      await act(async () => {
        vi.advanceTimersByTime(15000);
        await Promise.resolve();
      });

      expect(mockGet).toHaveBeenCalledTimes(2);
    });

    it('does not poll when all documents are already terminal', async () => {
      mockGet.mockResolvedValueOnce(createBackendResponse(2)); // all 'indexed'

      const { result } = renderHook(() => useDocuments({ autoFetch: false }));

      await act(async () => {
        await result.current.fetchDocuments();
      });

      expect(result.current.isPolling).toBe(false);

      await act(async () => {
        vi.advanceTimersByTime(20000);
        await Promise.resolve();
      });

      expect(mockGet).toHaveBeenCalledTimes(1);
    });
  });
});

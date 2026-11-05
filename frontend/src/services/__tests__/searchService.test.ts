import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mocked } from 'vitest';
import { APIErrorClass } from '@/types/api';
import { api } from '../api-client';
import { searchService } from '../searchService';

vi.mock('../api-client', () => ({
  api: {
    post: vi.fn(),
    get: vi.fn(),
    delete: vi.fn(),
    client: {
      post: vi.fn(),
    },
    createWebSocket: vi.fn(),
  },
}));

const mockApi = api as Mocked<typeof api>;

describe('searchService deterministic routing', () => {
  const originalResearchIds = process.env.NEXT_PUBLIC_RESEARCH_DOCUMENT_IDS;

  beforeEach(() => {
    vi.clearAllMocks();
    process.env.NEXT_PUBLIC_RESEARCH_DOCUMENT_IDS = originalResearchIds;
  });

  afterAll(() => {
    process.env.NEXT_PUBLIC_RESEARCH_DOCUMENT_IDS = originalResearchIds;
  });

  it('uses /search/hybrid with search_type hybrid by default', async () => {
    mockApi.post.mockResolvedValueOnce({
      search_id: 'search-1',
      query: 'prompt injection',
      results: [],
      search_time_ms: 25,
      total_results: 0,
    });

    const result = await searchService.search({ query: 'prompt injection' });

    expect(mockApi.post).toHaveBeenCalledWith(
      '/search/hybrid',
      expect.objectContaining({
        query: 'prompt injection',
        search_type: 'hybrid',
      }),
      undefined
    );
    expect(mockApi.post).toHaveBeenCalledTimes(1);
    expect(result.success).toBe(true);
    expect(result.data.query).toBe('prompt injection');
  });

  it('falls back to /search/ with search_type fulltext on 404 from hybrid', async () => {
    mockApi.post
      .mockRejectedValueOnce(
        new APIErrorClass({
          message: 'Not Found',
          status_code: 404,
          type: 'http_error',
          details: { detail: 'Not Found' },
        })
      )
      .mockResolvedValueOnce({
        search_id: 'search-1',
        query: 'prompt injection',
        results: [
          {
            document_id: 'doc-1',
            title: 'Doc 1',
            content_preview: 'preview content',
            relevance_score: 0.78,
            document_type: 'PDF',
          },
        ],
        search_time_ms: 25,
        total_results: 1,
      });

    const result = await searchService.search({ query: 'prompt injection' });

    expect(result.success).toBe(true);
    expect(result.data.query).toBe('prompt injection');
    expect(mockApi.post).toHaveBeenNthCalledWith(
      1,
      '/search/hybrid',
      expect.objectContaining({
        query: 'prompt injection',
        search_type: 'hybrid',
      }),
      undefined
    );
    expect(mockApi.post).toHaveBeenNthCalledWith(
      2,
      '/search/',
      expect.objectContaining({
        query: 'prompt injection',
        search_type: 'fulltext',
      }),
      undefined
    );
    expect(mockApi.post).toHaveBeenCalledTimes(2);
  });

  it('rethrows when the caller aborted instead of reporting a search failure', async () => {
    // The search page only suppresses the "search failed" bubble in its own
    // catch; returning {success:false} for an abort made a superseded search
    // render an error message.
    const controller = new AbortController();
    controller.abort();
    const abortError = new DOMException('aborted', 'AbortError');
    mockApi.post.mockRejectedValueOnce(abortError);

    await expect(
      searchService.search({ query: 'q' }, controller.signal)
    ).rejects.toBe(abortError);
    expect(mockApi.post).toHaveBeenCalledTimes(1);
  });

  it('rethrows an abort that lands during the 404 fallback request', async () => {
    const controller = new AbortController();
    mockApi.post
      .mockRejectedValueOnce(
        new APIErrorClass({
          message: 'Not Found',
          status_code: 404,
          type: 'http_error',
          details: { detail: 'Not Found' },
        })
      )
      .mockImplementationOnce(async () => {
        controller.abort();
        throw new DOMException('aborted', 'AbortError');
      });

    await expect(
      searchService.search({ query: 'q' }, controller.signal)
    ).rejects.toThrow('aborted');
    expect(mockApi.post).toHaveBeenCalledTimes(2);
  });

  it('does not fall back for non-404 hybrid failures and returns success false', async () => {
    mockApi.post.mockRejectedValueOnce(
      new APIErrorClass({
        message: 'Server Error',
        status_code: 500,
        type: 'http_error',
        details: { detail: 'Internal Server Error' },
      })
    );

    const result = await searchService.search({ query: 'prompt injection' });

    expect(mockApi.post).toHaveBeenCalledTimes(1);
    expect(mockApi.post).toHaveBeenNthCalledWith(
      1,
      '/search/hybrid',
      expect.objectContaining({
        query: 'prompt injection',
        search_type: 'hybrid',
      }),
      undefined
    );
    expect(result).toMatchObject({
      success: false,
    });
  });

  it('falls back on Axios-style 404 error response and preserves payload mapping shape', async () => {
    mockApi.post
      .mockRejectedValueOnce({
        message: 'Request failed with status code 404',
        response: {
          status: 404,
        },
      })
      .mockResolvedValueOnce({
        search_id: 'search-2',
        query: 'vector search',
        results: [
          {
            document_id: 'doc-2',
            title: 'Doc 2',
            content_preview: 'fallback preview',
            relevance_score: 0.55,
            document_type: 'txt',
          },
        ],
        search_time_ms: 33,
        total_results: 1,
      });

    const result = await searchService.search({ query: 'vector search' });

    expect(mockApi.post).toHaveBeenCalledTimes(2);
    expect(mockApi.post).toHaveBeenNthCalledWith(
      2,
      '/search/',
      expect.objectContaining({
        query: 'vector search',
        search_type: 'fulltext',
      }),
      undefined
    );
    expect(result.success).toBe(true);
    expect(result.data.query).toBe('vector search');
  });

  it('sends addToHistory payload as query params', async () => {
    mockApi.post.mockResolvedValueOnce({
      success: true,
      data: undefined,
    });

    await searchService.addToHistory('rag systems', 'search-2');

    expect(mockApi.post).toHaveBeenCalledWith(
      '/search/history?query=rag%20systems&result_id=search-2'
    );
  });

  it('builds a non-empty assistant answer text from search results', async () => {
    mockApi.post.mockResolvedValueOnce({
      search_id: 'search-3',
      query: 'hello',
      results: [
        {
          document_id: 'doc-1',
          title: 'Welcome Guide',
          content_preview: 'This is the first matching snippet.',
          relevance_score: 0.89,
          document_type: 'PDF',
        },
      ],
      search_time_ms: 12,
      total_results: 1,
    });

    const response = await searchService.search({ query: 'hello' });

    expect(response.success).toBe(true);
    expect(response.data.answer.text).not.toEqual('');
    expect(response.data.answer.text).toContain('Welcome Guide');
  });

  it('maps deterministic trace and quality fields from backend response', async () => {
    mockApi.post.mockResolvedValueOnce({
      search_id: 'search-3',
      query: 'deterministic quality check',
      results: [],
      search_time_ms: 18,
      total_results: 0,
      confidence: 0.64,
      coverage: 0.42,
      decision_trace_id: 'trace-xyz-123',
      deterministic_status: 'INSUFFICIENT_EVIDENCE',
      deterministic_message:
        'Insufficient cross-source evidence to produce a deterministic answer.',
      suggestions: ['Narrow query scope'],
    });

    const result = await searchService.search({
      query: 'deterministic quality check',
    });

    expect(result.success).toBe(true);
    expect(result.data.answer.confidence).toBe(0.64);
    expect(result.data.answer.coverage).toBe(0.42);
    expect(result.data.answer.decisionTraceId).toBe('trace-xyz-123');
    expect(result.data.deterministicStatus).toBe('INSUFFICIENT_EVIDENCE');
    expect(result.data.deterministicMessage).toContain(
      'Insufficient cross-source evidence'
    );
    expect(result.data.refinementSuggestions).toEqual(['Narrow query scope']);
  });

  it('sends selected document_ids filter to backend for research mode queries', async () => {
    mockApi.post.mockResolvedValueOnce({
      search_id: 'search-4',
      query: 'filtered search',
      results: [],
      search_time_ms: 15,
      total_results: 0,
    });

    await searchService.search({
      query: 'filtered search',
      filters: { document_ids: ['doc-a', 'doc-b'] },
    });

    expect(mockApi.post).toHaveBeenCalledWith(
      '/search/hybrid',
      expect.objectContaining({
        filters: expect.objectContaining({
          document_ids: ['doc-a', 'doc-b'],
        }),
      }),
      undefined
    );
  });

  it('uses NEXT_PUBLIC_RESEARCH_DOCUMENT_IDS as default source filter when request filter is missing', async () => {
    process.env.NEXT_PUBLIC_RESEARCH_DOCUMENT_IDS = 'doc-x, doc-y';
    mockApi.post.mockResolvedValueOnce({
      search_id: 'search-5',
      query: 'env filtered search',
      results: [],
      search_time_ms: 14,
      total_results: 0,
    });

    await searchService.search({ query: 'env filtered search' });

    expect(mockApi.post).toHaveBeenCalledWith(
      '/search/hybrid',
      expect.objectContaining({
        filters: expect.objectContaining({
          document_ids: ['doc-x', 'doc-y'],
        }),
      }),
      undefined
    );
  });
});

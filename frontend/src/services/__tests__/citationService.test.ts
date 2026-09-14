import { beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '@/services/api-client';
import { citationService } from '@/services/citationService';

vi.mock('@/services/api-client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

const rawCitation = {
  id: 'citation-1',
  message_id: 'message-1',
  document_id: 'document-1',
  external_reference_id: 'arxiv:1706.03762',
  document_title: 'Attention Is All You Need',
  document_type: 'paper',
  authors: null,
  year: 2017,
  venue: 'NeurIPS',
  doi: '10.5555/3295222.3295349',
  arxiv_id: '1706.03762',
  abstract: 'A transformer architecture.',
  snippet: 'The dominant sequence transduction models...',
  page_number: 1,
  chunk_id: 'chunk-1',
  chunk_index: 0,
  rerank_score: 0.97,
  score: 0.95,
  metadata_source: 'arxiv',
  needs_review: false,
  created_at: '2026-09-14T12:00:00Z',
  updated_at: '2026-09-14T12:05:00Z',
};

const normalizedCitation = {
  id: 'citation-1',
  messageId: 'message-1',
  documentId: 'document-1',
  externalReferenceId: 'arxiv:1706.03762',
  documentTitle: 'Attention Is All You Need',
  documentType: 'paper',
  authors: null,
  year: 2017,
  venue: 'NeurIPS',
  doi: '10.5555/3295222.3295349',
  arxivId: '1706.03762',
  abstract: 'A transformer architecture.',
  snippet: 'The dominant sequence transduction models...',
  pageNumber: 1,
  chunkId: 'chunk-1',
  chunkIndex: 0,
  rerankScore: 0.97,
  score: 0.95,
  metadataSource: 'arxiv',
  needsReview: false,
  createdAt: '2026-09-14T12:00:00Z',
  updatedAt: '2026-09-14T12:05:00Z',
};

describe('citationService response normalization', () => {
  beforeEach(() => vi.clearAllMocks());

  it('normalizes a created citation and preserves missing authors as null', async () => {
    vi.mocked(api.post).mockResolvedValue(rawCitation);

    const result = await citationService.createCitation({
      documentTitle: 'Attention Is All You Need',
    });

    expect(api.post).toHaveBeenCalledWith('/citations', {
      document_title: 'Attention Is All You Need',
    });
    expect(result).toEqual(normalizedCitation);
    expect(result.authors).toBeNull();
  });

  it('normalizes a citation detail response', async () => {
    vi.mocked(api.get).mockResolvedValue(rawCitation);

    const result = await citationService.getCitation('citation-1');

    expect(api.get).toHaveBeenCalledWith('/citations/citation-1');
    expect(result.documentTitle).toBe('Attention Is All You Need');
    expect(result).toEqual(normalizedCitation);
  });

  it('normalizes every citation in a list response', async () => {
    vi.mocked(api.get).mockResolvedValue({
      citations: [rawCitation],
      total: 1,
      skip: 0,
      limit: 25,
    });

    const result = await citationService.listCitations({
      messageId: 'message-1',
      limit: 25,
    });

    expect(api.get).toHaveBeenCalledWith(
      '/citations?message_id=message-1&limit=25'
    );
    expect(result).toEqual({
      citations: [normalizedCitation],
      total: 1,
      skip: 0,
      limit: 25,
    });
  });

  it('normalizes an extracted citation response', async () => {
    vi.mocked(api.post).mockResolvedValue(rawCitation);

    const result = await citationService.extractCitations(
      'document-1',
      'arxiv'
    );

    expect(result).toEqual(normalizedCitation);
  });

  it('normalizes a lookup citation response', async () => {
    vi.mocked(api.post).mockResolvedValue(rawCitation);

    const result = await citationService.lookupCitation({
      title: 'Attention Is All You Need',
    });

    expect(result).toEqual(normalizedCitation);
  });
});

import { describe, expect, it } from 'vitest';
import { toCitationCreate } from '@/hooks/chat/useChatStreaming';

describe('toCitationCreate', () => {
  it('maps a raw rag_context item onto CitationCreate', () => {
    expect(
      toCitationCreate({
        document_id: 'doc-1',
        title: 'Attention Is All You Need',
        content: 'The dominant sequence transduction models...',
        score: 0.92,
        source_position: 4,
        chunk_id: 'chunk-4',
        chunk_index: 7,
        page_number: 12,
      })
    ).toEqual({
      document_id: 'doc-1',
      document_title: 'Attention Is All You Need',
      snippet: 'The dominant sequence transduction models...',
      score: 0.92,
      source_position: 4,
      chunk_id: 'chunk-4',
      chunk_index: 7,
      page_number: 12,
    });
  });

  it('omits document_id when absent and keeps external references', () => {
    const result = toCitationCreate({
      external_reference_id: '1706.03762',
      title: 'arXiv paper',
      content: 'abstract text',
      score: 0.5,
    });
    expect(result.document_id).toBeUndefined();
    expect(result.external_reference_id).toBe('1706.03762');
  });

  it('caps the snippet at the backend column limit', () => {
    const result = toCitationCreate({
      document_id: 'doc-1',
      title: 't',
      content: 'x'.repeat(5000),
      score: 0.1,
    });
    expect(result.snippet!.length).toBe(2000);
  });

  it('drops non-numeric scores instead of sending garbage', () => {
    const result = toCitationCreate({
      document_id: 'doc-1',
      title: 't',
      content: 'c',
      score: 'high',
    });
    expect(result.score).toBeUndefined();
  });
});

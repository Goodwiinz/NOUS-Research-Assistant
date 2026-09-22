import { describe, expect, it } from 'vitest';

import { normalizeCitation } from '@/utils/citationNormalizer';
import { mapDbMessageToUI } from '@/hooks/useChatPersistence';
import { MessageRole, type ChatMessage } from '@/types/workspace';

describe('normalizeCitation provenance fidelity', () => {
  it('preserves canonical source position and chunk/page locators from snake_case reloads', () => {
    expect(
      normalizeCitation({
        document_id: 'doc-1',
        document_title: 'Reloaded paper',
        source_position: 4,
        chunk_id: 'chunk-7',
        chunk_index: 7,
        page_number: 12,
        snippet: 'Reloaded evidence',
        score: 0.91,
      })
    ).toMatchObject({
      documentId: 'doc-1',
      title: 'Reloaded paper',
      sourcePosition: 4,
      chunkId: 'chunk-7',
      chunkIndex: 7,
      pageNumber: 12,
      content: 'Reloaded evidence',
    });
  });

  it('preserves canonical source position and chunk/page locators from camelCase live snapshots', () => {
    expect(
      normalizeCitation({
        documentId: 'doc-2',
        title: 'Live paper',
        sourcePosition: 2,
        chunkId: 'chunk-live',
        chunkIndex: 3,
        pageNumber: 8,
        content: 'Live evidence',
        score: 0.8,
      })
    ).toMatchObject({
      sourcePosition: 2,
      chunkId: 'chunk-live',
      chunkIndex: 3,
      pageNumber: 8,
    });
  });
});

describe('legacy chat persistence citation adapter', () => {
  it('keeps persisted source positions and locators in its UI message view', () => {
    const message = {
      id: 'message-1',
      thread_id: 'thread-1',
      role: MessageRole.ASSISTANT,
      content: 'Answer [Doc 1]',
      created_at: '2026-09-12T12:00:00Z',
      updated_at: '2026-09-12T12:00:00Z',
      citations: [
        {
          id: 'citation-1',
          document_id: 'doc-1',
          document_title: 'Paper',
          source_position: 1,
          chunk_id: 'chunk-4',
          chunk_index: 4,
          page_number: 9,
          snippet: 'Evidence',
        },
      ],
      attachments: [],
    } as ChatMessage;

    expect(mapDbMessageToUI(message).citations?.[0]).toMatchObject({
      sourcePosition: 1,
      chunkId: 'chunk-4',
      chunkIndex: 4,
      pageNumber: 9,
    });
  });
});

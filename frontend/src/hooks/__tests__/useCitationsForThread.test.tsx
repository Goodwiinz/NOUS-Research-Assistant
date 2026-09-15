import { beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';

import type { UIConversation } from '@/hooks/useChatPersistence';
import { useCitationsForThread } from '@/hooks/useCitationsForThread';

const persistenceState = vi.hoisted(() => ({
  currentThreadId: null as string | null,
  conversations: [] as UIConversation[],
}));

vi.mock('@/hooks/useChatPersistence', () => ({
  useChatPersistence: () => persistenceState,
}));

type ThreadCitation = NonNullable<
  UIConversation['messages'][number]['citations']
>[number];

const citation = (
  documentId: string,
  title: string,
  score: number
): ThreadCitation => ({ documentId, title, score });

const conversation = (
  threadId: string,
  messages: UIConversation['messages']
): UIConversation => ({
  id: `conversation-${threadId}`,
  title: threadId,
  messages,
  createdAt: 1,
  updatedAt: 1,
  threadId,
  conversationId: 'conversation-1',
});

describe('useCitationsForThread', () => {
  beforeEach(() => {
    persistenceState.currentThreadId = null;
    persistenceState.conversations = [];
  });

  it('returns attached citations when an assistant message has no inline markers', () => {
    const attached = citation('doc-attached', 'Attached source', 0.8);
    persistenceState.currentThreadId = 'thread-current';
    persistenceState.conversations = [
      conversation('thread-current', [
        {
          id: 'message-1',
          role: 'assistant',
          content: 'An answer with backend-provided sources.',
          timestamp: 1,
          citations: [attached],
        },
      ]),
    ];

    const { result } = renderHook(() => useCitationsForThread());

    expect(result.current.allCitations).toEqual([attached]);
    expect(result.current.activeDocument).toEqual(attached);
  });

  it('keeps only valid inline-referenced citations when markers resolve', () => {
    const first = citation('doc-1', 'First source', 0.9);
    const second = citation('doc-2', 'Second source', 0.8);
    persistenceState.currentThreadId = 'thread-current';
    persistenceState.conversations = [
      conversation('thread-current', [
        {
          id: 'message-1',
          role: 'assistant',
          content: 'Only the second source is used [Doc 2].',
          timestamp: 1,
          citations: [first, second],
        },
      ]),
    ];

    const { result } = renderHook(() => useCitationsForThread());

    expect(result.current.allCitations).toEqual([second]);
  });

  it('falls back to attached citations when every inline marker is invalid', () => {
    const first = citation('doc-1', 'First source', 0.9);
    const second = citation('doc-2', 'Second source', 0.8);
    persistenceState.currentThreadId = 'thread-current';
    persistenceState.conversations = [
      conversation('thread-current', [
        {
          id: 'message-1',
          role: 'assistant',
          content: 'This stale marker cannot resolve [Doc 99].',
          timestamp: 1,
          citations: [first, second],
        },
      ]),
    ];

    const { result } = renderHook(() => useCitationsForThread());

    expect(result.current.allCitations).toEqual([first, second]);
  });

  it('deduplicates visible citations without leaking another thread', () => {
    const shared = citation('doc-shared', 'Shared source', 0.9);
    const currentOnly = citation('doc-current', 'Current source', 0.7);
    const otherThread = citation('doc-other', 'Other thread source', 1);
    persistenceState.currentThreadId = 'thread-current';
    persistenceState.conversations = [
      conversation('thread-other', [
        {
          id: 'message-other',
          role: 'assistant',
          content: 'Other thread answer.',
          timestamp: 1,
          citations: [otherThread],
        },
      ]),
      conversation('thread-current', [
        {
          id: 'message-1',
          role: 'assistant',
          content: 'First answer.',
          timestamp: 1,
          citations: [shared],
        },
        {
          id: 'message-2',
          role: 'assistant',
          content: 'Second answer.',
          timestamp: 2,
          citations: [shared, currentOnly],
        },
      ]),
    ];

    const { result } = renderHook(() => useCitationsForThread());

    expect(result.current.allCitations).toEqual([shared, currentOnly]);
    expect(result.current.allCitations).not.toContainEqual(otherThread);
  });
});

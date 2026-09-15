import { useMemo } from 'react';
import { type Citation, getVisibleCitations } from '@/utils/citationParser';
import { useChatPersistence } from './useChatPersistence';

export interface CitationItem {
  id?: string;
  documentId?: string;
  externalReferenceId?: string;
  title?: string;
  snippet?: string;
  score?: number;
  source?: string;
}

/**
 * Shared hook that extracts deduplicated citations from the current thread's conversations.
 * Returns allCitations (deduplicated list), activeDocument (highest score), and relatedResults (top 5 by score).
 */
export function useCitationsForThread() {
  const { conversations, currentThreadId } = useChatPersistence();

  const allCitations = useMemo((): CitationItem[] => {
    if (!currentThreadId) return [];

    const currentConv = conversations.find(
      (c) => c.threadId === currentThreadId
    );
    if (!currentConv) return [];

    const citations: CitationItem[] = [];
    const seenIds = new Set<string>();

    currentConv.messages.forEach((msg) => {
      if (msg.role === 'assistant' && msg.citations) {
        getVisibleCitations(msg.content, msg.citations as Citation[]).forEach(
          (cit) => {
            const citationItem: CitationItem = cit;
            const citId =
              citationItem.documentId ||
              citationItem.externalReferenceId ||
              citationItem.id;
            if (citId && !seenIds.has(citId)) {
              seenIds.add(citId);
              citations.push(citationItem);
            }
          }
        );
      }
    });

    return citations;
  }, [conversations, currentThreadId]);

  const activeDocument = useMemo((): CitationItem | null => {
    let highestScoreCitation: CitationItem | null = null;
    let highestScore = 0;

    for (const cit of allCitations) {
      if ((cit.score || 0) > highestScore) {
        highestScore = cit.score || 0;
        highestScoreCitation = cit;
      }
    }

    return highestScoreCitation;
  }, [allCitations]);

  const relatedResults = useMemo((): CitationItem[] => {
    return [...allCitations]
      .sort((a, b) => (b.score || 0) - (a.score || 0))
      .slice(0, 5);
  }, [allCitations]);

  return { allCitations, activeDocument, relatedResults };
}

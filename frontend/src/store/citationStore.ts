/**
 * Citation Store - Zustand state management for Research Assistant citations
 */

import { create } from 'zustand';
import { captureAccountSession } from '@/lib/account-session';
import { citationService } from '@/services/citationService';
import type { CitationResponse } from '@/types/research';

// Maximum number of citations to retain in memory
const MAX_CITATIONS = 500;

interface CitationState {
  // State
  citations: CitationResponse[];
  citationsByMessage: Record<string, CitationResponse[]>;
  loading: boolean;
  error: string | null;

  // Actions
  fetchCitationsForMessage: (messageId: string) => Promise<void>;
  addCitation: (citation: CitationResponse) => void;
  clearCitations: () => void;
  setError: (error: string | null) => void;
}

export const useCitationStore = create<CitationState>((set, get) => ({
  // Initial state
  citations: [],
  citationsByMessage: {},
  loading: false,
  error: null,

  // Fetch citations for a specific message
  fetchCitationsForMessage: async (messageId: string) => {
    const isCurrentAccount = captureAccountSession();
    set({ loading: true, error: null });

    try {
      const citations = await citationService.getCitationsForMessage(messageId);
      if (!isCurrentAccount()) return;

      set((state) => {
        // Dedup: only add citations not already present by id
        const existingIds = new Set(state.citations.map((c) => c.id));
        const newCitations = citations.filter((c) => !existingIds.has(c.id));
        let merged = [...state.citations, ...newCitations];

        // Trim oldest entries when exceeding the cap
        if (merged.length > MAX_CITATIONS) {
          merged = merged.slice(merged.length - MAX_CITATIONS);
        }

        return {
          citations: merged,
          citationsByMessage: {
            ...state.citationsByMessage,
            [messageId]: citations,
          },
          loading: false,
        };
      });
    } catch (error: any) {
      if (!isCurrentAccount()) return;
      console.error('[CitationStore] Failed to fetch citations:', error);
      set({
        error: error?.message || 'Failed to fetch citations',
        loading: false,
      });
    }
  },

  // Add a single citation to the store
  addCitation: (citation: CitationResponse) => {
    set((state) => {
      const messageId = citation.messageId;

      // Dedup: skip if already present by id
      if (state.citations.some((c) => c.id === citation.id)) {
        return {};
      }

      // Add to global citations array and enforce cap
      let updatedCitations = [...state.citations, citation];
      if (updatedCitations.length > MAX_CITATIONS) {
        updatedCitations = updatedCitations.slice(
          updatedCitations.length - MAX_CITATIONS
        );
      }

      // If no messageId, just update citations without modifying citationsByMessage
      if (!messageId) {
        return { citations: updatedCitations };
      }

      // Add to message-specific citations
      const messageCitations = [
        ...(state.citationsByMessage[messageId] || []),
        citation,
      ];

      return {
        citations: updatedCitations,
        citationsByMessage: {
          ...state.citationsByMessage,
          [messageId]: messageCitations,
        },
      };
    });
  },

  // Clear all citations
  clearCitations: () => {
    set({
      citations: [],
      citationsByMessage: {},
      loading: false,
      error: null,
    });
  },

  // Set error message
  setError: (error: string | null) => {
    set({ error });
  },
}));

export default useCitationStore;

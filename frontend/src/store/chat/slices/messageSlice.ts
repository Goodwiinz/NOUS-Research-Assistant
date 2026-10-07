/**
 * Message actions: load/refresh/paginate/send/delete, plus the coupled
 * `messages` / `messagePagination` / `messageFreshness` triple and the
 * terminal-reconciliation + stale-response guards (see recon
 * `chatfe.store.guards` — REQUEST-IDENTITY, EXPECTATION RECONCILIATION,
 * OPTIMISTIC-SUPERSEDES-REFRESH). Kept as one slice deliberately: these three
 * state pieces and the request-identity check must never drift apart.
 *
 * Split out of chat-store.ts (Task 5.4) with no behavior change.
 */
import { ChatMessage, ChatMessageUpdate } from '@/types/workspace';
import { workspaceService } from '@/services/workspaceService';
import { captureChatSession } from '../requestCoordinator';
import type { ChatSliceCreator, RefreshExpectation } from '../types';
import { removeItemFromRecord } from '../recordIndex';
import { MAX_CACHED_THREADS, INITIAL_MESSAGE_PAGE_SIZE } from '../initialState';
import {
  abortNewestPageRequest,
  deletedThreadIds,
  newestPageRequests,
  type NewestPageRequest,
} from '../requestCoordinator';

export interface MessageSlice {
  loadMessages: (threadId: string) => Promise<void>;
  markMessagesStale: (threadId: string) => void;
  refreshMessages: (
    threadId: string,
    expected?: RefreshExpectation
  ) => Promise<boolean>;
  loadOlderMessages: (threadId: string) => Promise<void>;
  sendMessage: (
    content: string,
    threadId?: string
  ) => Promise<ChatMessage | null>;
  addMessageToStore: (threadId: string, message: ChatMessage) => void;
  updateMessageFeedback: (
    id: string,
    data: ChatMessageUpdate
  ) => Promise<ChatMessage | null>;
  deleteMessage: (id: string) => Promise<boolean>;
  clearThread: (threadId: string) => void;
}

// Identity for in-flight older-page loads. Module-scope (outside Immer) for
// the same reason as requestCoordinator's newestPageRequests. A commit may
// only apply if the thread's cache survived (pagination still present), this
// request is still the acknowledged one (loadingOlder still true — a cache
// rebuilt by refreshMessages starts back at false), and no newer
// loadOlderMessages superseded it (token identity). Unique token objects
// (not counters): the latest request deletes its entry on settle, and object
// identity can't be recycled the way a reset counter can.
const olderPageRequestTokens = new Map<string, object>();

function compareMessageOrder(left: ChatMessage, right: ChatMessage): number {
  const timestampOrder = left.created_at.localeCompare(right.created_at);
  return timestampOrder !== 0
    ? timestampOrder
    : left.id.localeCompare(right.id);
}

function mergeNewestMessagePage(
  existing: ChatMessage[],
  canonicalNewestPage: ChatMessage[]
): { messages: ChatMessage[]; retainedOlderCount: number } {
  if (canonicalNewestPage.length === 0) {
    return { messages: [], retainedOlderCount: 0 };
  }

  const canonical = [...canonicalNewestPage].sort(compareMessageOrder);
  const boundary = canonical[0];
  const canonicalIds = new Set(canonical.map((message) => message.id));
  const retainedOlder = existing.filter(
    (message) =>
      !canonicalIds.has(message.id) &&
      compareMessageOrder(message, boundary) < 0
  );
  const byId = new Map<string, ChatMessage>();
  [...retainedOlder, ...canonical].forEach((message) => {
    byId.set(message.id, message);
  });

  return {
    messages: [...byId.values()].sort(compareMessageOrder),
    retainedOlderCount: retainedOlder.length,
  };
}

export const createMessageSlice: ChatSliceCreator<MessageSlice> = (
  set,
  get
) => ({
  loadMessages: async (threadId) => {
    await get().refreshMessages(threadId);
  },

  markMessagesStale: (threadId) => {
    // A new optimistic turn supersedes any pre-turn newest-page request.
    // If that older response committed, it could mark the pre-turn page
    // fresh and hide the optimistic overlay before terminal reconciliation.
    abortNewestPageRequest(threadId);
    set((state) => {
      state.messageFreshness[threadId] = 'stale';
      if (state.loadingThreadId === threadId) {
        state.isLoadingMessages = false;
        state.loadingThreadId = null;
      }
    });
  },

  refreshMessages: async (threadId, expected) => {
    // A stream can outlive its thread's deletion; its terminal reconcile must
    // not resurrect the deleted thread's cache and reverse-index entries.
    if (deletedThreadIds.has(threadId)) return false;
    const previous = newestPageRequests.get(threadId);
    previous?.controller.abort();
    const request: NewestPageRequest = {
      generation: (previous?.generation ?? 0) + 1,
      controller: new AbortController(),
    };
    newestPageRequests.set(threadId, request);

    const snapshot = get();
    const hasCachedPage =
      Object.prototype.hasOwnProperty.call(snapshot.messages, threadId) &&
      !!snapshot.messagePagination[threadId];
    set((state) => {
      state.messageFreshness[threadId] = 'refreshing';
      state.error = null;
      // A fresh attempt supersedes this thread's earlier failure record.
      if (state.messageLoadError?.threadId === threadId) {
        state.messageLoadError = null;
      }
      if (!hasCachedPage) {
        state.isLoadingMessages = true;
        state.loadingThreadId = threadId;
      }
    });

    try {
      const response = await workspaceService.listMessages(threadId, {
        limit: INITIAL_MESSAGE_PAGE_SIZE,
        order: 'desc',
        signal: request.controller.signal,
      });
      if (newestPageRequests.get(threadId) !== request) {
        return false;
      }
      if (!Array.isArray(response.messages)) {
        throw new Error('Invalid messages response');
      }

      const canonicalAscending = [...response.messages].reverse();
      const expectationIds = [
        expected?.persistedId,
        expected?.runtimeId,
      ].filter((value): value is string => !!value);
      const expectationMet =
        expectationIds.length === 0 ||
        response.messages.some(
          (message) =>
            expectationIds.includes(message.id) ||
            (!!message.client_message_id &&
              expectationIds.includes(message.client_message_id))
        );
      const evictedThreadIds: string[] = [];

      newestPageRequests.delete(threadId);
      set((state) => {
        // This thread's earlier failure (if any) is resolved by the success.
        if (state.messageLoadError?.threadId === threadId) {
          state.messageLoadError = null;
        }
        const existing = state.messages[threadId] || [];
        const existingPagination = state.messagePagination[threadId];
        const { messages, retainedOlderCount } = mergeNewestMessagePage(
          existing,
          canonicalAscending
        );

        for (const message of existing) {
          delete state.messageToThread[message.id];
        }
        state.messages[threadId] = messages;
        for (const message of messages) {
          state.messageToThread[message.id] = threadId;
        }
        state.messagePagination[threadId] = {
          hasMore:
            retainedOlderCount > 0 && existingPagination
              ? existingPagination.hasMore
              : messages.length > 0 && response.has_more,
          loadingOlder: false,
          loadedCount: messages.length,
        };
        state.messageFreshness[threadId] = expectationMet ? 'fresh' : 'stale';

        const threadKeys = Object.keys(state.messages);
        if (threadKeys.length > MAX_CACHED_THREADS) {
          const protectedThreadIds = new Set(
            [state.currentThreadId, threadId].filter((id): id is string => !!id)
          );
          const excess = threadKeys.length - MAX_CACHED_THREADS;
          evictedThreadIds.push(
            ...threadKeys
              .filter((key) => !protectedThreadIds.has(key))
              .slice(0, excess)
          );
          for (const key of evictedThreadIds) {
            for (const message of state.messages[key] || []) {
              delete state.messageToThread[message.id];
            }
            delete state.messages[key];
            delete state.messagePagination[key];
            delete state.messageFreshness[key];
          }
        }

        if (state.loadingThreadId === threadId) {
          state.isLoadingMessages = false;
          state.loadingThreadId = null;
        }
      });
      evictedThreadIds.forEach(abortNewestPageRequest);
      if (!expectationMet && expected?.diagnostic) {
        console.warn('[ChatReconciliationInvariant]', {
          threadId,
          terminalReason: expected.diagnostic.terminalReason,
          failureKind: 'expected-message-missing',
          freshness: 'stale',
          localCount: expected.diagnostic.localCount,
          storeCount: get().messages[threadId]?.length ?? 0,
          expectedPersistedId: expected.persistedId,
          expectedRuntimeId: expected.runtimeId,
          requestGeneration: request.generation,
          completedInBackground: expected.diagnostic.completedInBackground,
        });
      }
      return expectationMet;
    } catch (error) {
      if (newestPageRequests.get(threadId) !== request) {
        return false;
      }
      newestPageRequests.delete(threadId);
      const isAbort = error instanceof Error && error.name === 'AbortError';
      if (!isAbort) {
        if (expected?.diagnostic) {
          console.warn('[ChatReconciliationInvariant]', {
            threadId,
            terminalReason: expected.diagnostic.terminalReason,
            failureKind: 'refresh-failed',
            freshness: 'stale',
            localCount: expected.diagnostic.localCount,
            storeCount: get().messages[threadId]?.length ?? 0,
            expectedPersistedId: expected.persistedId,
            expectedRuntimeId: expected.runtimeId,
            requestGeneration: request.generation,
            completedInBackground: expected.diagnostic.completedInBackground,
            errorName: error instanceof Error ? error.name : 'unknown',
          });
        } else {
          console.error('[ChatStore] Error refreshing messages:', error);
        }
      }
      set((state) => {
        state.messageFreshness[threadId] = 'stale';
        if (!isAbort) {
          // Thread-scoped: a superseded/background failure must not masquerade
          // as the currently displayed thread's error via the global string.
          state.messageLoadError = { threadId, nonce: Date.now() };
        }
        if (state.loadingThreadId === threadId) {
          state.isLoadingMessages = false;
          state.loadingThreadId = null;
        }
      });
      return false;
    }
  },

  loadOlderMessages: async (threadId) => {
    const isCurrentSession = captureChatSession();
    if (deletedThreadIds.has(threadId)) return;
    const pagination = get().messagePagination[threadId];
    if (!pagination || !pagination.hasMore || pagination.loadingOlder) {
      return;
    }

    set((state) => {
      if (!state.messagePagination[threadId]) return;
      state.messagePagination[threadId].loadingOlder = true;
    });
    const requestToken = {};
    olderPageRequestTokens.set(threadId, requestToken);

    try {
      // Cursor pagination: ask for messages strictly OLDER than the oldest
      // one currently loaded (display index 0), newest-first. Offset-based
      // paging against an ascending list would walk toward NEWER messages
      // and scramble order — before_id is the only correct "load older".
      const oldestLoaded = get().messages[threadId]?.[0];
      if (!oldestLoaded) {
        return;
      }
      const response = await workspaceService.listMessages(threadId, {
        limit: 100,
        order: 'desc',
        before_id: oldestLoaded.id,
      });
      if (!isCurrentSession()) return;

      // Runtime validation: a malformed payload must not corrupt the
      // message list. Bail out (the finally block clears loadingOlder).
      if (!response.messages || !Array.isArray(response.messages)) {
        console.error(
          '[ChatStore] Invalid response.messages in loadOlderMessages'
        );
        return;
      }

      // desc response is newest→oldest; reverse to ascending so it slots in
      // front of the existing list in chronological order.
      const olderAscending = [...response.messages].reverse();

      set((state) => {
        // Commit-phase guard: the thread cache may have been invalidated
        // (clearThread/deleteThread/eviction) or rebuilt while this request
        // was in flight — committing then would resurrect a deleted cache or
        // splice a stale page into a fresh one.
        const currentPagination = state.messagePagination[threadId];
        if (
          !currentPagination ||
          !currentPagination.loadingOlder ||
          olderPageRequestTokens.get(threadId) !== requestToken
        ) {
          return;
        }
        const existing = state.messages[threadId] || [];
        // De-dup against already-loaded messages: overlapping pages (e.g.
        // a message inserted between fetches) must not produce duplicates.
        const existingIds = new Set(existing.map((m) => m.id));
        const newMessages = olderAscending.filter(
          (m) => !existingIds.has(m.id)
        );
        // Prepend the older batch at the front, preserving newest-at-the-end
        // display order.
        state.messages[threadId] = [...newMessages, ...existing];
        // Populate reverse index for new messages
        for (const msg of newMessages) {
          if (!state.messageToThread[msg.id]) {
            state.messageToThread[msg.id] = threadId;
          }
        }
        state.messagePagination[threadId] = {
          // Empty page (or none new) means we've reached the start — stop,
          // even if the server still reports has_more, to avoid re-fetching
          // the same cursor forever.
          hasMore: response.messages.length > 0 && response.has_more,
          loadingOlder: false,
          loadedCount: pagination.loadedCount + newMessages.length,
        };
      });
    } catch (error) {
      if (!isCurrentSession()) return;
      console.error('[ChatStore] Error loading older messages:', error);
      set((state) => {
        // Same guard as the commit: a superseded request failing against an
        // invalidated/rebuilt cache must not surface a global error for a
        // view that no longer owns it.
        const currentPagination = state.messagePagination[threadId];
        if (
          !currentPagination ||
          !currentPagination.loadingOlder ||
          olderPageRequestTokens.get(threadId) !== requestToken
        ) {
          return;
        }
        state.error = 'Failed to load older messages';
      });
    } finally {
      set((state) => {
        // Same guard as the commit: don't clear a flag that now belongs to a
        // newer request against a rebuilt cache.
        if (
          state.messagePagination[threadId] &&
          olderPageRequestTokens.get(threadId) === requestToken
        ) {
          state.messagePagination[threadId].loadingOlder = false;
        }
      });
      // Bound the map: the latest request removes its entry on settle.
      if (olderPageRequestTokens.get(threadId) === requestToken) {
        olderPageRequestTokens.delete(threadId);
      }
    }
  },

  sendMessage: async (content, threadId) => {
    const isCurrentSession = captureChatSession();
    const state = get();
    const targetThreadId = threadId || state.currentThreadId;

    if (!targetThreadId) {
      console.error('[ChatStore] No thread selected for sending message');
      return null;
    }

    set((state) => {
      state.isSendingMessage = true;
      state.error = null;
    });

    try {
      const message = await workspaceService.createMessage({
        thread_id: targetThreadId,
        content,
      });
      if (!isCurrentSession()) return null;

      set((state) => {
        if (!state.messages[targetThreadId]) {
          state.messages[targetThreadId] = [];
        }
        state.messages[targetThreadId].push(message);
        // Populate reverse index (GOO-86)
        state.messageToThread[message.id] = targetThreadId;
        state.isSendingMessage = false;
      });

      return message;
    } catch (error) {
      if (!isCurrentSession()) return null;
      console.error('[ChatStore] Error sending message:', error);
      set((state) => {
        state.error = 'Failed to send message';
        state.isSendingMessage = false;
      });
      return null;
    }
  },

  // Add message directly to store without API call
  // Used when page.tsx saves messages with citations through its own flow
  addMessageToStore: (threadId, message) => {
    set((state) => {
      if (!state.messages[threadId]) {
        state.messages[threadId] = [];
      }
      // Check if message already exists to prevent duplicates
      const existingIndex = state.messages[threadId].findIndex(
        (m) => m.id === message.id
      );
      if (existingIndex === -1) {
        state.messages[threadId].push(message);
        state.messageToThread[message.id] = threadId;
        console.log(
          '[ChatStore] Added message to store:',
          message.id,
          'with',
          message.citations?.length || 0,
          'citations'
        );
      } else {
        // Update existing message (e.g., when citations are added later)
        state.messages[threadId][existingIndex] = message;
        console.log(
          '[ChatStore] Updated message in store:',
          message.id,
          'with',
          message.citations?.length || 0,
          'citations'
        );
      }
    });
  },

  updateMessageFeedback: async (id, data) => {
    const isCurrentSession = captureChatSession();
    try {
      const message = await workspaceService.updateMessage(id, data);
      if (!isCurrentSession()) return null;
      set((state) => {
        const threadId = message.thread_id;
        const messages = state.messages[threadId] || [];
        const index = messages.findIndex((m) => m.id === id);
        if (index !== -1) {
          state.messages[threadId][index] = message;
        }
      });
      return message;
    } catch (error) {
      if (!isCurrentSession()) return null;
      console.error('[ChatStore] Error updating message feedback:', error);
      set((state) => {
        state.error = 'Failed to update message feedback';
      });
      return null;
    }
  },

  deleteMessage: async (id) => {
    const isCurrentSession = captureChatSession();
    try {
      await workspaceService.deleteMessage(id);
      if (!isCurrentSession()) return false;
      set((state) => {
        // Use O(1) reverse index lookup (GOO-86)
        removeItemFromRecord(state.messages, id, state.messageToThread);
      });
      return true;
    } catch (error) {
      if (!isCurrentSession()) return false;
      console.error('[ChatStore] Error deleting message:', error);
      set((state) => {
        state.error = 'Failed to delete message';
      });
      return false;
    }
  },

  clearThread: (threadId) => {
    abortNewestPageRequest(threadId);
    set((state) => {
      // Clean up reverse index entries for evicted messages
      for (const msg of state.messages[threadId] || []) {
        delete state.messageToThread[msg.id];
      }
      delete state.messages[threadId];
      delete state.messagePagination[threadId];
      delete state.messageFreshness[threadId];
    });
  },
});

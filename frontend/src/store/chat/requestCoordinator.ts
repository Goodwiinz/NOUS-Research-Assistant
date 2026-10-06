/**
 * Per-thread newest-page request coordination + the (deprecated) single
 * active streaming AbortController.
 *
 * Deliberately module-scope, outside Immer/Zustand state: AbortController is
 * mutable and must never be proxied or persisted (see chat-store-streaming
 * test: `expect('abortController' in useChatStore.getState()).toBe(false)`).
 *
 * Split out of chat-store.ts (Task 5.4) with no behavior change. Owned here
 * (not per-slice) because both the message slice (refreshMessages/
 * loadOlderMessages/clearThread) and the thread slice (deleteThread,
 * bulkDeleteThreads) must abort a thread's in-flight newest-page request.
 */

export interface NewestPageRequest {
  generation: number;
  controller: AbortController;
}

// Request coordination stays outside Immer state because AbortController is
// mutable and must never be proxied or persisted.
export const newestPageRequests = new Map<string, NewestPageRequest>();

export function abortNewestPageRequest(threadId: string): void {
  const request = newestPageRequests.get(threadId);
  if (!request) return;
  request.controller.abort();
  newestPageRequests.delete(threadId);
}

/**
 * Threads deleted this session. A stream that outlives its thread's deletion
 * still runs terminal reconciliation (refreshMessages) when it commits;
 * without this record that orphan request re-created the deleted thread's
 * message cache, pagination, freshness and reverse-index entries.
 */
export const deletedThreadIds = new Set<string>();

export function markThreadDeleted(threadId: string): void {
  deletedThreadIds.add(threadId);
}

export function abortAllNewestPageRequests(): void {
  for (const request of newestPageRequests.values()) {
    request.controller.abort();
  }
  newestPageRequests.clear();
  // Full store reset (logout, tests): the session-scoped deletion record goes
  // with it — a fresh session may legitimately reuse ids in fixtures.
  deletedThreadIds.clear();
}

// Module-level abort controller for the deprecated `streamMessage` v2 path
// (outside Immer state to avoid proxy issues).
export let activeAbortController: AbortController | null = null;

export function setActiveAbortController(
  controller: AbortController | null
): void {
  activeAbortController = controller;
}

// A reset ends the account's entire chat session, including reads and writes
// that cannot be cancelled at the transport layer. Capture before any await.
let chatSession = new AbortController();
const sessionResetListeners = new Set<() => void>();

export function captureChatSession(): () => boolean {
  const signal = chatSession.signal;
  return () => !signal.aborted;
}

export function onChatSessionReset(listener: () => void): () => void {
  sessionResetListeners.add(listener);
  return () => {
    sessionResetListeners.delete(listener);
  };
}

export function resetChatSession(): void {
  const previousSession = chatSession;
  chatSession = new AbortController();
  previousSession.abort();
  activeAbortController?.abort();
  activeAbortController = null;
  abortAllNewestPageRequests();
  for (const listener of sessionResetListeners) listener();
}

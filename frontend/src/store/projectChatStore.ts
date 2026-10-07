/**
 * Project-Chat Store - Zustand store for project-chat integration
 * Manages project-thread links with per-project state isolation
 *
 * Architecture:
 * - State keyed by projectId for O(1) lookup
 * - Immer middleware for clean mutation syntax
 * - No persistence (data fetched on-demand)
 * - Per-project loading/error states
 */

import { create } from 'zustand';
import { captureAccountSession } from '@/lib/account-session';
import { immer } from 'zustand/middleware/immer';
import { projectChatService } from '@/services/projectChatService';
import { useChatStore } from '@/store/chat-store';
import type {
  ProjectThread,
  StartChatFromProjectRequest,
  LinkThreadRequest,
  SaveThreadToNoteRequest,
  StartChatFromProjectResponse,
} from '@/types/project-chat';
import type { NoteResponse } from '@/types/research';

// ============================================================================
// State Interface
// ============================================================================

interface ProjectChatState {
  // Data - keyed by projectId for O(1) lookup
  linkedThreads: Record<string, ProjectThread[]>;

  // Loading states - per-project granularity
  loadingThreads: Record<string, boolean>;
  startingChat: Record<string, boolean>;
  linkingThread: Record<string, boolean>;
  unlinkingThread: Record<string, boolean>;
  savingToNote: Record<string, boolean>;

  // Errors - per-project isolation
  errors: Record<string, string | null>;

  // Actions
  fetchProjectThreads: (projectId: string) => Promise<void>;
  startChatFromProject: (
    projectId: string,
    request: StartChatFromProjectRequest
  ) => Promise<StartChatFromProjectResponse | null>;
  linkThreadToProject: (
    projectId: string,
    request: LinkThreadRequest
  ) => Promise<ProjectThread | null>;
  unlinkThreadFromProject: (
    projectId: string,
    threadId: string
  ) => Promise<void>;
  saveThreadToNote: (
    projectId: string,
    request: SaveThreadToNoteRequest
  ) => Promise<NoteResponse | null>;
  clearError: (projectId: string) => void;
  reset: () => void;
}

// ============================================================================
// Initial State
// ============================================================================

const initialState = {
  linkedThreads: {},
  loadingThreads: {},
  startingChat: {},
  linkingThread: {},
  unlinkingThread: {},
  savingToNote: {},
  errors: {},
};

// ============================================================================
// Store Implementation
// ============================================================================

// Monotonic token per project so a slow list response can't clobber the
// result of a newer fetch (or a just-completed link/unlink refresh).
const fetchThreadsSeq: Record<string, number> = {};

// Per-action, per-project identity tokens (RS-C4): two overlapping calls to
// the SAME mutation action for the SAME project (double-click, retry) must
// only let the most-recently-started call's settle-time write to the
// per-project flag/error state win — a stale call's catch/success writing
// startingChat/linkingThread/unlinkingThread/savingToNote/errors after a
// newer call already completed would clobber it. Four separate maps (one
// per action), not one map keyed `action:projectId` — avoids string-
// composition key bugs and keeps each action self-contained, matching the
// fetchThreadsSeq convention above. Data commits derived from the response
// (linkedThreads adds/removals) are NOT gated — they reflect genuine
// backend state regardless of call ordering; only the ephemeral per-project
// UI flags/errors are ownership-guarded.
const startingChatTokens = new Map<string, object>();
const linkingThreadTokens = new Map<string, object>();
const unlinkingThreadTokens = new Map<string, object>();
const savingToNoteTokens = new Map<string, object>();

// The chat store's thread→project binding mirror is THREAD-keyed and
// single-valued, and both link and unlink mutate it — so they share ONE
// token map keyed by thread id. Without it, a slow link(projA, threadX)
// resolving after a faster link(projB, threadX) (or after an unlink) would
// re-point the chat-rail binding at a stale project. Guards ONLY the
// setThreadProjectBinding mirror call; linkedThreads data commits stay
// unconditional (each backend result is individually valid).
const threadBindingTokens = new Map<string, object>();

export const useProjectChatStore = create<ProjectChatState>()(
  immer((set, get) => ({
    ...initialState,

    /**
     * Fetch all threads linked to a project
     */
    fetchProjectThreads: async (projectId: string) => {
      const isCurrentAccount = captureAccountSession();
      // Guard against invalid project IDs
      if (!projectId || projectId === 'undefined') {
        console.warn(
          '[ProjectChatStore] fetchProjectThreads called with invalid projectId:',
          projectId
        );
        return;
      }

      const seq = (fetchThreadsSeq[projectId] =
        (fetchThreadsSeq[projectId] ?? 0) + 1);

      set((state) => {
        state.loadingThreads[projectId] = true;
        state.errors[projectId] = null;
      });

      try {
        const response = await projectChatService.listProjectThreads(projectId);
        if (!isCurrentAccount()) return;
        if (fetchThreadsSeq[projectId] !== seq) return; // superseded by a newer fetch

        set((state) => {
          state.linkedThreads[projectId] = response.threads;
          state.loadingThreads[projectId] = false;
        });
      } catch (error: any) {
        if (!isCurrentAccount()) return;
        if (fetchThreadsSeq[projectId] !== seq) return;
        console.error('[ProjectChatStore] fetchProjectThreads failed:', error);
        set((state) => {
          state.errors[projectId] = error?.message || 'Failed to fetch threads';
          state.loadingThreads[projectId] = false;
        });
      }
    },

    /**
     * Start a new chat thread from a project with document context
     */
    startChatFromProject: async (
      projectId: string,
      request: StartChatFromProjectRequest
    ) => {
      const isCurrentAccount = captureAccountSession();
      // Guard against invalid project IDs
      if (!projectId || projectId === 'undefined') {
        console.warn(
          '[ProjectChatStore] startChatFromProject called with invalid projectId:',
          projectId
        );
        return null;
      }

      const requestToken = {};
      startingChatTokens.set(projectId, requestToken);

      set((state) => {
        state.startingChat[projectId] = true;
        state.errors[projectId] = null;
      });

      try {
        const response = await projectChatService.startChatFromProject(
          projectId,
          request
        );
        if (!isCurrentAccount()) return null;

        if (startingChatTokens.get(projectId) === requestToken) {
          set((state) => {
            state.startingChat[projectId] = false;
          });
        }
        // Refresh threads list unconditionally — an older-but-successful
        // call still created a real thread that must appear in the list.
        // fetchProjectThreads has its own supersession guard, so a stale
        // refresh can't clobber a newer one.
        await get().fetchProjectThreads(projectId);
        if (!isCurrentAccount()) return null;

        return response;
      } catch (error: any) {
        if (!isCurrentAccount()) return null;
        console.error('[ProjectChatStore] startChatFromProject failed:', error);
        if (startingChatTokens.get(projectId) === requestToken) {
          set((state) => {
            state.errors[projectId] = error?.message || 'Failed to start chat';
            state.startingChat[projectId] = false;
          });
        }
        return null;
      } finally {
        if (startingChatTokens.get(projectId) === requestToken) {
          startingChatTokens.delete(projectId);
        }
      }
    },

    /**
     * Link an existing thread to a project
     */
    linkThreadToProject: async (
      projectId: string,
      request: LinkThreadRequest
    ) => {
      const isCurrentAccount = captureAccountSession();
      // Guard against invalid project IDs
      if (!projectId || projectId === 'undefined') {
        console.warn(
          '[ProjectChatStore] linkThreadToProject called with invalid projectId:',
          projectId
        );
        return null;
      }

      const requestToken = {};
      linkingThreadTokens.set(projectId, requestToken);
      const bindingToken = {};
      threadBindingTokens.set(request.thread_id, bindingToken);

      set((state) => {
        state.linkingThread[projectId] = true;
        state.errors[projectId] = null;
      });

      try {
        const response = await projectChatService.linkThreadToProject(
          projectId,
          request
        );
        if (!isCurrentAccount()) return null;

        // Optimistic update - add to local state immediately. Re-linking is
        // idempotent on the backend (returns the existing link), so replace
        // any prior entry for this thread instead of duplicating it. This
        // reflects genuine backend state regardless of call ordering, so
        // it's committed unconditionally (unlike the ephemeral flag below).
        set((state) => {
          const existing = state.linkedThreads[projectId] ?? [];
          state.linkedThreads[projectId] = [
            response,
            ...existing.filter((t) => t.thread_id !== response.thread_id),
          ];
        });
        // The "linking in flight" flag belongs to whichever call is most
        // recent — a stale call clearing it could hide a still-in-flight
        // newer call's spinner.
        if (linkingThreadTokens.get(projectId) === requestToken) {
          set((state) => {
            state.linkingThread[projectId] = false;
          });
        }

        // Mirror into the chat store's thread row — the chat rail and the
        // agent page_context derive the binding from source_project_id, so a
        // link made from the project page must be visible there too. Only
        // the most recent link/unlink for this THREAD may write the mirror
        // (see threadBindingTokens).
        if (threadBindingTokens.get(request.thread_id) === bindingToken) {
          useChatStore
            .getState()
            .setThreadProjectBinding(response.thread_id, projectId);
        }

        return response;
      } catch (error: any) {
        if (!isCurrentAccount()) return null;
        // The backend link endpoint is idempotent (upsert) and never returns
        // 409, so re-linking an already-attached thread succeeds rather than
        // erroring. Any error reaching here is a real failure (400 same-
        // workspace, 404, 500, network) — record its message so the UI can
        // surface the actual cause instead of a generic fallback.
        console.error('[ProjectChatStore] linkThreadToProject failed:', error);
        if (linkingThreadTokens.get(projectId) === requestToken) {
          set((state) => {
            state.errors[projectId] = error?.message || 'Failed to link thread';
            state.linkingThread[projectId] = false;
          });
        }
        return null;
      } finally {
        if (linkingThreadTokens.get(projectId) === requestToken) {
          linkingThreadTokens.delete(projectId);
        }
        if (threadBindingTokens.get(request.thread_id) === bindingToken) {
          threadBindingTokens.delete(request.thread_id);
        }
      }
    },

    /**
     * Unlink a thread from a project
     */
    unlinkThreadFromProject: async (projectId: string, threadId: string) => {
      const isCurrentAccount = captureAccountSession();
      // Guard against invalid project IDs
      if (!projectId || projectId === 'undefined') {
        console.warn(
          '[ProjectChatStore] unlinkThreadFromProject called with invalid projectId:',
          projectId
        );
        return;
      }

      const requestToken = {};
      unlinkingThreadTokens.set(projectId, requestToken);
      const bindingToken = {};
      threadBindingTokens.set(threadId, bindingToken);

      set((state) => {
        state.unlinkingThread[projectId] = true;
        state.errors[projectId] = null;
      });

      try {
        await projectChatService.unlinkThreadFromProject(projectId, threadId);
        if (!isCurrentAccount()) return;

        // Optimistic update - remove from local state immediately. Reflects
        // genuine backend state regardless of call ordering, so it's
        // committed unconditionally (unlike the ephemeral flag below).
        set((state) => {
          const threads = state.linkedThreads[projectId];
          if (threads) {
            state.linkedThreads[projectId] = threads.filter(
              (t) => t.thread_id !== threadId
            );
          }
        });
        // The "unlinking in flight" flag belongs to whichever call is most
        // recent — a stale call clearing it could hide a still-in-flight
        // newer call's spinner.
        if (unlinkingThreadTokens.get(projectId) === requestToken) {
          set((state) => {
            state.unlinkingThread[projectId] = false;
          });
        }

        // Clear the chat store's copy too. Without this the chat rail keeps
        // showing the removed project and the agent keeps receiving its
        // project_id until a full reload. Only the most recent link/unlink
        // for this THREAD may write the mirror (see threadBindingTokens).
        if (threadBindingTokens.get(threadId) === bindingToken) {
          useChatStore.getState().setThreadProjectBinding(threadId, null);
        }
      } catch (error: any) {
        if (!isCurrentAccount()) return;
        console.error(
          '[ProjectChatStore] unlinkThreadFromProject failed:',
          error
        );
        if (unlinkingThreadTokens.get(projectId) === requestToken) {
          set((state) => {
            state.errors[projectId] =
              error?.message || 'Failed to unlink thread';
            state.unlinkingThread[projectId] = false;
          });
        }
      } finally {
        if (unlinkingThreadTokens.get(projectId) === requestToken) {
          unlinkingThreadTokens.delete(projectId);
        }
        if (threadBindingTokens.get(threadId) === bindingToken) {
          threadBindingTokens.delete(threadId);
        }
      }
    },

    /**
     * Save thread content to a project note
     */
    saveThreadToNote: async (
      projectId: string,
      request: SaveThreadToNoteRequest
    ) => {
      const isCurrentAccount = captureAccountSession();
      // Guard against invalid project IDs
      if (!projectId || projectId === 'undefined') {
        console.warn(
          '[ProjectChatStore] saveThreadToNote called with invalid projectId:',
          projectId
        );
        return null;
      }

      const requestToken = {};
      savingToNoteTokens.set(projectId, requestToken);

      set((state) => {
        state.savingToNote[projectId] = true;
        state.errors[projectId] = null;
      });

      try {
        const response = await projectChatService.saveThreadToNote(
          projectId,
          request
        );
        if (!isCurrentAccount()) return null;

        if (savingToNoteTokens.get(projectId) === requestToken) {
          set((state) => {
            state.savingToNote[projectId] = false;
          });
        }

        return response;
      } catch (error: any) {
        if (!isCurrentAccount()) return null;
        console.error('[ProjectChatStore] saveThreadToNote failed:', error);
        if (savingToNoteTokens.get(projectId) === requestToken) {
          set((state) => {
            state.errors[projectId] =
              error?.message || 'Failed to save thread to note';
            state.savingToNote[projectId] = false;
          });
        }
        return null;
      } finally {
        if (savingToNoteTokens.get(projectId) === requestToken) {
          savingToNoteTokens.delete(projectId);
        }
      }
    },

    /**
     * Clear error state for a project
     */
    clearError: (projectId: string) => {
      set((state) => {
        state.errors[projectId] = null;
      });
    },

    /**
     * Reset entire store to initial state
     */
    reset: () => {
      for (const key of Object.keys(fetchThreadsSeq))
        delete fetchThreadsSeq[key];
      startingChatTokens.clear();
      linkingThreadTokens.clear();
      unlinkingThreadTokens.clear();
      savingToNoteTokens.clear();
      threadBindingTokens.clear();
      set(initialState);
    },
  }))
);

// ============================================================================
// Selectors (Optional convenience exports)
// ============================================================================

/**
 * Get threads for a specific project
 */
export const selectProjectThreads =
  (projectId: string) => (state: ProjectChatState) =>
    state.linkedThreads[projectId] || [];

/**
 * Get loading state for a specific project
 */
export const selectProjectLoading =
  (projectId: string) => (state: ProjectChatState) =>
    state.loadingThreads[projectId] ||
    state.startingChat[projectId] ||
    state.linkingThread[projectId] ||
    state.unlinkingThread[projectId] ||
    state.savingToNote[projectId] ||
    false;

/**
 * Get error state for a specific project
 */
export const selectProjectError =
  (projectId: string) => (state: ProjectChatState) =>
    state.errors[projectId] || null;

export default useProjectChatStore;

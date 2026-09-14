/**
 * Selection + small UI actions: current workspace/conversation/thread,
 * thread-project binding, thread registration, and the misc UI toggles
 * (shortcuts dialog, copied-message id, sidebar collapse).
 *
 * Split out of chat-store.ts (Task 5.4) with no behavior change.
 */
import type { Thread } from '@/types/workspace';
import type { ChatSliceCreator, ChatState } from '../types';

export interface SelectionSlice {
  setCurrentWorkspace: (workspaceId: string | null) => Promise<void>;
  setCurrentConversation: (conversationId: string | null) => Promise<void>;
  setCurrentThread: (threadId: string | null) => void;
  setThreadProjectBinding: (
    threadId: string,
    projectId: string | null
  ) => boolean;
  registerThread: (thread: Thread) => void;
  setShortcutsDialogOpen: (open: boolean) => void;
  setCopiedMessageId: (id: string | null) => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
}

export const createSelectionSlice: ChatSliceCreator<SelectionSlice> = (
  set,
  get
) => ({
  setCurrentWorkspace: async (workspaceId) => {
    set((state) => {
      state.currentWorkspaceId = workspaceId;
      // Clear downstream selections when workspace changes
      state.currentConversationId = null;
      state.currentThreadId = null;
    });

    // Load conversations for new workspace
    if (workspaceId) {
      await get().loadConversations(workspaceId);
    }
  },

  setCurrentConversation: async (conversationId) => {
    set((state) => {
      state.currentConversationId = conversationId;
      // Clear thread selection when conversation changes
      state.currentThreadId = null;
    });

    // Load threads for new conversation
    if (conversationId) {
      await get().loadThreads(conversationId);
    }
  },

  setCurrentThread: (threadId) => {
    const snapshot = get();
    const hasCachedPage =
      !!threadId &&
      Object.prototype.hasOwnProperty.call(snapshot.messages, threadId) &&
      !!snapshot.messagePagination[threadId];
    const freshness = threadId
      ? snapshot.messageFreshness[threadId]
      : undefined;
    set((state: ChatState) => {
      state.currentThreadId = threadId;
      if (!threadId || hasCachedPage) {
        // No load follows for a new chat or a valid cached page. The epoch
        // bump above makes any prior response stale, so clear its loading
        // flags here rather than stranding them indefinitely.
        state.isLoadingMessages = false;
        state.loadingThreadId = null;
      }
    });

    // Cached pages remain visible. A stale cache refreshes in the
    // background; a fresh (or legacy unclassified) cache makes no request.
    if (threadId && !hasCachedPage) {
      get().loadMessages(threadId);
    } else if (threadId && freshness === 'stale') {
      void get().refreshMessages(threadId);
    }
  },

  // The project binding lives on the thread row (source_project_id), not
  // in the URL — mirror binding changes into the store copy so the rail
  // updates immediately and survives in-session thread switches. Reload
  // survival comes from the server row (the threads API returns
  // source_project_id). Bind passes a project id; pass null on unbind.
  setThreadProjectBinding: (threadId, projectId) => {
    let found = false;
    set((state) => {
      const conversationId = state.threadToConversation[threadId];
      const indexed = conversationId
        ? state.threads[conversationId]?.find((x) => x.id === threadId)
        : undefined;
      if (indexed) {
        indexed.source_project_id = projectId;
        found = true;
        return;
      }
      // Reverse index can lag a fresh thread; fall back to a full scan.
      for (const list of Object.values(state.threads)) {
        const t = list.find((x) => x.id === threadId);
        if (t) {
          t.source_project_id = projectId;
          found = true;
          break;
        }
      }
    });
    if (!found) {
      console.warn(
        '[ChatStore] setThreadProjectBinding: thread not in store; binding not cached locally',
        { threadId, projectId }
      );
    }
    return found;
  },

  // Register a thread fetched outside the store (the chat composer creates
  // via workspaceService directly; the chat page fetches thread pages and
  // deep-linked threads into its own local list) so bindings and lookups
  // work without waiting for the next loadThreads. Re-registering an
  // already-indexed thread REPLACES the row rather than skipping it — a
  // server row is the truth for source_project_id, and skipping left threads
  // outside the store's one page-1 load looking permanently unbound.
  registerThread: (thread) => {
    set((state) => {
      const list = (state.threads[thread.conversation_id] ??= []);
      const index = list.findIndex((t) => t.id === thread.id);
      if (index === -1) {
        list.unshift(thread);
      } else {
        list[index] = thread;
      }
      state.threadToConversation[thread.id] = thread.conversation_id;
    });
  },

  setShortcutsDialogOpen: (open) => {
    set((state) => {
      state.shortcutsDialogOpen = open;
    });
  },

  setCopiedMessageId: (id) => {
    set((state) => {
      state.copiedMessageId = id;
    });
  },

  setSidebarCollapsed: (collapsed) => {
    set((state) => {
      state.sidebarCollapsed = collapsed;
    });
  },
});

/**
 * Conversation CRUD actions.
 *
 * Split out of chat-store.ts (Task 5.4) with no behavior change.
 */
import {
  Conversation,
  ConversationCreate,
  ConversationUpdate,
} from '@/types/workspace';
import { workspaceService } from '@/services/workspaceService';
import { captureChatSession } from '../requestCoordinator';
import type { ChatSliceCreator } from '../types';
import { removeItemFromRecord } from '../recordIndex';
import {
  abortNewestPageRequest,
  markThreadDeleted,
} from '../requestCoordinator';
import { handleStaleDataRecovery } from './workspaceSlice';

export interface ConversationSlice {
  loadConversations: (workspaceId: string) => Promise<void>;
  createConversation: (
    data: ConversationCreate
  ) => Promise<Conversation | null>;
  registerConversation: (
    conversation: Conversation,
    workspaceId: string
  ) => void;
  updateConversation: (
    id: string,
    data: ConversationUpdate
  ) => Promise<Conversation | null>;
  deleteConversation: (id: string) => Promise<boolean>;
}

// Identity for in-flight loadConversations calls, keyed by workspace. Module
// scope (outside Immer) like requestCoordinator. A superseded request — same
// workspace re-loaded (A → B → A) — must not commit a stale conversation
// list, set a stale error, or fire stale-data recovery over the newer load's
// state. Unique token objects (not counters): the latest request deletes its
// entry on settle, and object identity can't be recycled the way a reset
// counter can, so a slow request from an earlier cycle can never match.
const loadConversationsRequestTokens = new Map<string, object>();

export const createConversationSlice: ChatSliceCreator<ConversationSlice> = (
  set,
  get
) => ({
  loadConversations: async (workspaceId) => {
    const isCurrentSession = captureChatSession();
    const requestToken = {};
    loadConversationsRequestTokens.set(workspaceId, requestToken);
    set((state) => {
      state.isLoadingConversations = true;
      state.error = null;
    });

    try {
      const response = await workspaceService.listConversations(workspaceId);
      if (!isCurrentSession()) return;
      if (loadConversationsRequestTokens.get(workspaceId) !== requestToken) {
        return; // superseded by a newer load — the newer request owns state
      }
      set((state) => {
        state.conversations[workspaceId] = response.conversations;
        // Rebuild (not just append to) the reverse index for this workspace:
        // conversations deleted on the server would otherwise leave stale
        // entries pointing at a list they're no longer in.
        const returnedIds = new Set(response.conversations.map((c) => c.id));
        for (const [convId, wsId] of Object.entries(
          state.conversationToWorkspace
        )) {
          if (wsId === workspaceId && !returnedIds.has(convId)) {
            delete state.conversationToWorkspace[convId];
          }
        }
        for (const conv of response.conversations) {
          state.conversationToWorkspace[conv.id] = workspaceId;
        }
        state.isLoadingConversations = false;
      });
    } catch (error) {
      if (!isCurrentSession()) return;
      if (loadConversationsRequestTokens.get(workspaceId) !== requestToken) {
        return; // superseded — no stale error, no stale-data recovery
      }
      console.error('[ChatStore] Error loading conversations:', error);

      // Handle 404 - workspace not found (stale data)
      const err = error as { response?: { status?: number } };
      if (err?.response?.status === 404) {
        // A late 404 for a workspace the user has already navigated away
        // from must not nuke the (valid) current selection — only recover
        // when the failed load still targets the current workspace.
        if (get().currentWorkspaceId !== workspaceId) {
          console.warn(
            '[ChatStore] Ignoring stale 404 for superseded workspace:',
            workspaceId
          );
          set((state) => {
            state.isLoadingConversations = false;
          });
          return;
        }
        console.warn(
          '[ChatStore] Workspace not found (404) - clearing stale data'
        );
        const result = handleStaleDataRecovery(get, set, 'loadConversations', {
          clearWorkspaces: true,
          loadingKey: 'isLoadingConversations',
        });
        if (result.shouldProceed) {
          result.triggerReinit();
        }
        return;
      }

      set((state) => {
        state.error = 'Failed to load conversations';
        state.isLoadingConversations = false;
      });
    } finally {
      // Bound the map: the latest request removes its entry on settle. Any
      // still-in-flight older request already fails the token check.
      if (loadConversationsRequestTokens.get(workspaceId) === requestToken) {
        loadConversationsRequestTokens.delete(workspaceId);
      }
    }
  },

  // Index a conversation the store did not create itself. A conversation
  // obtained from workspaceService.getOrCreateDefaultConversation (the deduped
  // bootstrap helper) never passes through createConversation, so without this
  // it is missing from `conversations[workspaceId]` and from the reverse index
  // — the sidebar renders no conversation and lookups by id miss.
  registerConversation: (conversation, workspaceId) => {
    set((state) => {
      const list = (state.conversations[workspaceId] ??= []);
      if (!list.some((c) => c.id === conversation.id)) {
        list.unshift(conversation);
      }
      state.conversationToWorkspace[conversation.id] = workspaceId;
    });
  },

  createConversation: async (data) => {
    const isCurrentSession = captureChatSession();
    try {
      const conversation = await workspaceService.createConversation(data);
      if (!isCurrentSession()) return null;
      set((state) => {
        const workspaceId = data.workspace_id;
        if (!state.conversations[workspaceId]) {
          state.conversations[workspaceId] = [];
        }
        state.conversations[workspaceId].unshift(conversation);
        // Populate reverse index (GOO-86)
        state.conversationToWorkspace[conversation.id] = workspaceId;
      });
      return conversation;
    } catch (error) {
      if (!isCurrentSession()) return null;
      console.error('[ChatStore] Error creating conversation:', error);

      // Handle 404 - workspace not found (stale data)
      const err = error as { response?: { status?: number } };
      if (err?.response?.status === 404) {
        console.warn(
          '[ChatStore] Workspace not found (404) while creating conversation - clearing stale data'
        );
        const result = handleStaleDataRecovery(get, set, 'createConversation', {
          clearWorkspaces: true,
        });
        if (result.shouldProceed) {
          result.triggerReinit();
        }
        return null;
      }

      set((state) => {
        state.error = 'Failed to create conversation';
      });
      return null;
    }
  },

  updateConversation: async (id, data) => {
    const isCurrentSession = captureChatSession();
    try {
      const conversation = await workspaceService.updateConversation(id, data);
      if (!isCurrentSession()) return null;
      set((state) => {
        const workspaceId = conversation.workspace_id;
        const convs = state.conversations[workspaceId] || [];
        const index = convs.findIndex((c) => c.id === id);
        if (index !== -1) {
          state.conversations[workspaceId][index] = conversation;
        }
      });
      return conversation;
    } catch (error) {
      if (!isCurrentSession()) return null;
      console.error('[ChatStore] Error updating conversation:', error);
      set((state) => {
        state.error = 'Failed to update conversation';
      });
      return null;
    }
  },

  deleteConversation: async (id) => {
    const isCurrentSession = captureChatSession();
    try {
      await workspaceService.deleteConversation(id);
      if (!isCurrentSession()) return false;
      // Cascade: the conversation's threads (and their message caches) are
      // unreachable once it's gone — deleting only the conversation row used
      // to orphan cached threads/messages/pagination/freshness and their
      // reverse-index entries, and left descendant requests in flight.
      const snapshot = get();
      const threadIds = new Set([
        ...(snapshot.threads[id] || []).map((t) => t.id),
        ...Object.keys(snapshot.threadToConversation).filter(
          (threadId) => snapshot.threadToConversation[threadId] === id
        ),
      ]);
      threadIds.forEach(markThreadDeleted);
      threadIds.forEach(abortNewestPageRequest);
      set((state) => {
        // Use O(1) reverse index lookup (GOO-86)
        removeItemFromRecord(
          state.conversations,
          id,
          state.conversationToWorkspace
        );
        for (const threadId of threadIds) {
          for (const message of state.messages[threadId] || []) {
            delete state.messageToThread[message.id];
          }
          delete state.messages[threadId];
          delete state.messagePagination[threadId];
          delete state.messageFreshness[threadId];
          delete state.threadToConversation[threadId];
          if (state.currentThreadId === threadId) {
            state.currentThreadId = null;
          }
        }
        delete state.threads[id];
        if (state.currentConversationId === id) {
          state.currentConversationId = null;
          state.currentThreadId = null;
        }
      });
      return true;
    } catch (error) {
      if (!isCurrentSession()) return false;
      console.error('[ChatStore] Error deleting conversation:', error);
      set((state) => {
        state.error = 'Failed to delete conversation';
      });
      return false;
    }
  },
});

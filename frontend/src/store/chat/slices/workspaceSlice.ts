/**
 * Workspace CRUD actions, plus the shared 404 stale-data-recovery helper
 * used by the conversation and thread slices (workspace is the root of the
 * workspace → conversation → thread hierarchy, so those slices depending on
 * this one mirrors the domain).
 *
 * Split out of chat-store.ts (Task 5.4) with no behavior change.
 */
import { Workspace, WorkspaceCreate, WorkspaceUpdate } from '@/types/workspace';
import { workspaceService } from '@/services/workspaceService';
import { captureChatSession } from '../requestCoordinator';
import type { ChatSliceCreator, ChatState, ChatActions } from '../types';
import { MAX_REINIT_RETRIES } from '../initialState';

export interface WorkspaceSlice {
  loadWorkspaces: () => Promise<void>;
  createWorkspace: (data: WorkspaceCreate) => Promise<Workspace | null>;
  updateWorkspace: (
    id: string,
    data: WorkspaceUpdate
  ) => Promise<Workspace | null>;
  deleteWorkspace: (id: string) => Promise<boolean>;
}

// Helper type for the recovery handler
type RecoveryResult =
  { shouldProceed: false } | { shouldProceed: true; triggerReinit: () => void };

/**
 * Helper to handle stale data recovery (404 errors) atomically.
 * Performs guard check and state update in a single set() call to prevent race conditions.
 *
 * @param get - Zustand get function
 * @param set - Zustand set function (immer-wrapped)
 * @param context - Context string for logging
 * @param options - Additional options for state clearing
 * @returns RecoveryResult with shouldProceed and optional reinit trigger
 */
export function handleStaleDataRecovery(
  get: () => ChatState & ChatActions,
  set: (fn: (state: ChatState) => void) => void,
  context: string,
  options: {
    clearWorkspaces?: boolean;
    loadingKey?:
      'isLoadingConversations' | 'isLoadingThreads' | 'isLoadingMessages';
  } = {}
): RecoveryResult {
  const isCurrentSession = captureChatSession();
  const { clearWorkspaces = true, loadingKey } = options;

  let shouldReinit = false;
  let maxRetriesReached = false;

  // Perform guard check AND state update atomically in a single set() call
  set((state) => {
    // Guard against concurrent/repeated reinitialization and infinite loops
    if (
      state.isReinitializing ||
      state.reinitRetryCount >= MAX_REINIT_RETRIES
    ) {
      console.warn(
        `[ChatStore] Skipping reinitialization for ${context} (already in progress or max retries reached)`
      );
      maxRetriesReached = state.reinitRetryCount >= MAX_REINIT_RETRIES;
      if (loadingKey) {
        state[loadingKey] = false;
      }
      if (maxRetriesReached) {
        state.error = 'Failed to initialize workspace after multiple attempts';
      }
      return;
    }

    // Clear stale data and set reinitialization flag atomically
    shouldReinit = true;
    state.currentWorkspaceId = null;
    state.currentConversationId = null;
    state.currentThreadId = null;
    if (clearWorkspaces) {
      state.workspaces = [];
    }
    if (loadingKey) {
      state[loadingKey] = false;
    }
    state.isReinitializing = true;
    state.reinitRetryCount += 1;
    state.error = null;
  });

  if (!shouldReinit) {
    return { shouldProceed: false };
  }

  // Return a trigger function that the caller can use to start reinitialization
  return {
    shouldProceed: true,
    triggerReinit: () => {
      if (!isCurrentSession()) return;
      // Use Promise-based approach instead of fire-and-forget setTimeout
      (async () => {
        try {
          await get().initializeDefaultWorkspace();
        } catch (err) {
          console.error(
            `[ChatStore] Reinitialization failed for ${context}:`,
            err
          );
        } finally {
          if (isCurrentSession()) {
            set((state) => {
              state.isReinitializing = false;
            });
          }
        }
      })();
    },
  };
}

export const createWorkspaceSlice: ChatSliceCreator<WorkspaceSlice> = (
  set
) => ({
  loadWorkspaces: async () => {
    const isCurrentSession = captureChatSession();
    set((state) => {
      state.isLoadingWorkspaces = true;
      state.error = null;
    });

    try {
      const workspaces = await workspaceService.listWorkspaces();
      if (!isCurrentSession()) return;
      set((state) => {
        state.workspaces = workspaces;
        state.isLoadingWorkspaces = false;
      });
    } catch (error) {
      if (!isCurrentSession()) return;
      console.error('[ChatStore] Error loading workspaces:', error);
      set((state) => {
        state.error = 'Failed to load workspaces';
        state.isLoadingWorkspaces = false;
      });
    }
  },

  createWorkspace: async (data) => {
    const isCurrentSession = captureChatSession();
    try {
      const workspace = await workspaceService.createWorkspace(data);
      if (!isCurrentSession()) return null;
      set((state) => {
        state.workspaces.unshift(workspace);
      });
      return workspace;
    } catch (error) {
      if (!isCurrentSession()) return null;
      console.error('[ChatStore] Error creating workspace:', error);
      set((state) => {
        state.error = 'Failed to create workspace';
      });
      return null;
    }
  },

  updateWorkspace: async (id, data) => {
    const isCurrentSession = captureChatSession();
    try {
      const workspace = await workspaceService.updateWorkspace(id, data);
      if (!isCurrentSession()) return null;
      set((state) => {
        const index = state.workspaces.findIndex((w) => w.id === id);
        if (index !== -1) {
          state.workspaces[index] = workspace;
        }
      });
      return workspace;
    } catch (error) {
      if (!isCurrentSession()) return null;
      console.error('[ChatStore] Error updating workspace:', error);
      set((state) => {
        state.error = 'Failed to update workspace';
      });
      return null;
    }
  },

  deleteWorkspace: async (id) => {
    const isCurrentSession = captureChatSession();
    try {
      await workspaceService.deleteWorkspace(id);
      if (!isCurrentSession()) return false;
      set((state) => {
        state.workspaces = state.workspaces.filter((w) => w.id !== id);
        if (state.currentWorkspaceId === id) {
          state.currentWorkspaceId = null;
          state.currentConversationId = null;
          state.currentThreadId = null;
        }
      });
      return true;
    } catch (error) {
      if (!isCurrentSession()) return false;
      console.error('[ChatStore] Error deleting workspace:', error);
      set((state) => {
        state.error = 'Failed to delete workspace';
      });
      return false;
    }
  },
});

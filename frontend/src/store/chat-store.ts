/**
 * Chat Store - Zustand store for NOUS thread-centric chat system
 * Manages workspace, conversation, thread, and message state with persistence
 *
 * This file is the store's STABLE public entry point (`@/store/chat-store`):
 * it creates the persisted store, combines the slices under `./chat/`, and
 * re-exports the selectors/types every existing caller already imports from
 * here. The implementation lives in per-domain slice modules (Task 5.4) —
 * see `frontend/src/store/chat/` — but nothing outside this file should
 * change: every existing `@/store/chat-store` import keeps working.
 */

import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import { immer } from 'zustand/middleware/immer';
import { enableMapSet } from 'immer';

// Enable Immer's MapSet plugin for Set/Map support in state
enableMapSet();

import { workspaceService } from '@/services/workspaceService';
import type { ChatStore } from './chat/types';
import { initialState } from './chat/initialState';
import {
  abortAllNewestPageRequests,
  setActiveAbortController,
} from './chat/requestCoordinator';
import { createSelectionSlice } from './chat/slices/selectionSlice';
import { createWorkspaceSlice } from './chat/slices/workspaceSlice';
import { createConversationSlice } from './chat/slices/conversationSlice';
import { createThreadSlice } from './chat/slices/threadSlice';
import { createMessageSlice } from './chat/slices/messageSlice';
import { createStreamingSlice } from './chat/slices/streamingSlice';

// ============================================================================
// Store
// ============================================================================

export const useChatStore = create<ChatStore>()(
  persist(
    immer((set, get, api) => ({
      ...initialState,
      ...createSelectionSlice(set, get, api),
      ...createWorkspaceSlice(set, get, api),
      ...createConversationSlice(set, get, api),
      ...createThreadSlice(set, get, api),
      ...createMessageSlice(set, get, api),
      ...createStreamingSlice(set, get, api),

      // ======================================================================
      // Utility Actions
      // ======================================================================
      // Cross-cutting over the whole combined store (reset touches every
      // slice's state; initializeDefaultWorkspace drives the selection
      // slice) — kept in the facade rather than owned by one slice.

      clearError: () => {
        set((state) => {
          state.error = null;
        });
      },

      reset: () => {
        abortAllNewestPageRequests();
        set(initialState);
      },

      initializeDefaultWorkspace: async () => {
        try {
          console.log('[ChatStore] Initializing default workspace...');
          const { currentWorkspaceId } = get();
          const workspace =
            await workspaceService.getOrCreateDefaultWorkspace();

          // Keep local workspace list in sync with bootstrap result.
          set((state) => {
            if (!state.workspaces.some((w) => w.id === workspace.id)) {
              state.workspaces.unshift(workspace);
            }
          });

          // Clear stale IDs if current workspace no longer exists.
          if (currentWorkspaceId && currentWorkspaceId !== workspace.id) {
            console.warn(
              '[ChatStore] Clearing stale workspace ID:',
              currentWorkspaceId
            );
            set((s) => {
              s.currentWorkspaceId = null;
              s.currentConversationId = null;
              s.currentThreadId = null;
            });
          }

          console.log(
            '[ChatStore] Using workspace:',
            workspace.id,
            workspace.name
          );

          // Set current workspace and wait for its conversation load so
          // bootstrap callers do not have to issue the same read again.
          await get().setCurrentWorkspace(workspace.id);

          // Reset retry counter on successful initialization
          set((s) => {
            s.reinitRetryCount = 0;
          });
        } catch (error) {
          console.error(
            '[ChatStore] Error initializing default workspace:',
            error
          );
          set((state) => {
            state.error = 'Failed to initialize workspace';
          });
          throw error;
        }
      },
    })),
    {
      name: 'chat-storage',
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        // Only persist these fields
        currentWorkspaceId: state.currentWorkspaceId,
        currentConversationId: state.currentConversationId,
        currentThreadId: state.currentThreadId,
        sidebarCollapsed: state.sidebarCollapsed,
      }),
      onRehydrateStorage: () => () => {
        // Reset streaming state on rehydration to prevent stale UI
        setActiveAbortController(null);
      },
    }
  )
);

// ============================================================================
// Types (compatibility re-exports)
// ============================================================================

export type { MessageFreshness, RefreshExpectation } from './chat/types';

// ============================================================================
// Selectors (compatibility re-exports)
// ============================================================================

export {
  selectCurrentWorkspace,
  selectCurrentConversation,
  selectCurrentThread,
  selectCurrentThreadProjectId,
  resolveBoundProjectId,
  selectCurrentMessages,
  selectConversationsForCurrentWorkspace,
  selectThreadsForCurrentConversation,
} from './chat/selectors';

export default useChatStore;

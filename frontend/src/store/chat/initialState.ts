/**
 * Chat store initial state and shared size/retry limits.
 *
 * Split out of chat-store.ts (Task 5.4) with no behavior change.
 */
import type { ChatState } from './types';

// Maximum retry attempts for reinitialization to prevent infinite loops
export const MAX_REINIT_RETRIES = 3;

// Maximum number of thread message caches to retain in memory.
// When exceeded, the oldest threads (by key insertion order) are evicted.
export const MAX_CACHED_THREADS = 50;

// A thread selection only needs the recent context visible in the viewport.
// Older messages remain available through explicit cursor pagination.
export const INITIAL_MESSAGE_PAGE_SIZE = 50;

export const initialState: ChatState = {
  currentWorkspaceId: null,
  currentConversationId: null,
  currentThreadId: null,
  workspaces: [],
  conversations: {},
  threads: {},
  messages: {},
  messageFreshness: {},
  // Reverse indexes for O(1) lookup
  conversationToWorkspace: {},
  threadToConversation: {},
  messageToThread: {},
  isLoadingWorkspaces: false,
  isLoadingConversations: false,
  isLoadingThreads: false,
  isLoadingMessages: false,
  loadingThreadId: null,
  isSendingMessage: false,
  messagePagination: {},
  isReinitializing: false,
  reinitRetryCount: 0,
  error: null,
  messageLoadError: null,
  shortcutsDialogOpen: false,
  copiedMessageId: null,
  sidebarCollapsed: false,
  selectedThreadIds: new Set<string>(),
  isSelectMode: false,
  // Streaming state
  isStreaming: false,
  streamingContent: '',
  streamingMessageId: null,
  streamingCitations: [],
  streamingDiagnosticsTraceId: null,
  isRetrievingRag: false,
  streamingSteps: [],
  streamingPlan: [],
  streamingPlanReasoning: '',
  streamingProgress: [],
  streamingReasoning: '',
  streamingElapsedMs: null,
  streamingPhase: null,
  streamingStatusDetail: null,
  streamingThreadId: null,
};

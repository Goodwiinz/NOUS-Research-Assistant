/**
 * Chat store types — state shape, action signatures, and the slice-creator
 * helper type shared by every slice module.
 *
 * Split out of chat-store.ts (Task 5.4) with no behavior change.
 */
import type { StateCreator } from 'zustand';
import {
  BulkThreadResponse,
  Workspace,
  WorkspaceCreate,
  WorkspaceUpdate,
  Conversation,
  ConversationCreate,
  ConversationUpdate,
  Thread,
  ThreadCreate,
  ThreadUpdate,
  ChatMessage,
  ChatMessageUpdate,
} from '@/types/workspace';
import type { ActivityStep } from '@/components/chat/shared/cloudMessageView';
import type {
  AgentProgressStep,
  AgentStreamPhase,
} from '@/services/agentStreamEvents';
import type { PlanStep } from '@/types/agent-chat';

export interface ChatState {
  // Current selections
  currentWorkspaceId: string | null;
  currentConversationId: string | null;
  currentThreadId: string | null;

  // Data
  workspaces: Workspace[];
  conversations: Record<string, Conversation[]>; // keyed by workspace_id
  threads: Record<string, Thread[]>; // keyed by conversation_id
  messages: Record<string, ChatMessage[]>; // keyed by thread_id
  messageFreshness: Record<string, MessageFreshness>;

  // Reverse indexes for O(1) parent lookup (GOO-86 performance fix)
  conversationToWorkspace: Record<string, string>; // conversation_id -> workspace_id
  threadToConversation: Record<string, string>; // thread_id -> conversation_id
  messageToThread: Record<string, string>; // message_id -> thread_id

  // Loading states
  isLoadingWorkspaces: boolean;
  isLoadingConversations: boolean;
  isLoadingThreads: boolean;
  isLoadingMessages: boolean;
  /**
   * The thread whose initial message page is in flight (null when none).
   * Written in lockstep with isLoadingMessages — consumers that must not
   * blank the UI for unrelated loads (send-path fetch of a just-created
   * thread, background refresh of a cached thread) key off this instead of
   * the coarse boolean (#1121 regressions).
   */
  loadingThreadId: string | null;
  isSendingMessage: boolean;

  // Pagination state per thread
  messagePagination: Record<
    string,
    { hasMore: boolean; loadingOlder: boolean; loadedCount: number }
  >;

  // Reinitialization guard for 404 recovery
  isReinitializing: boolean;
  reinitRetryCount: number;

  // Error states
  error: string | null;
  /**
   * Thread-scoped transcript-load failure. The global `error` string cannot
   * attribute a failure to the thread that produced it: a superseded thread's
   * request failing after the user switched away used to surface as a toast
   * on the WRONG (current) thread, and a background stale-cache refresh
   * failure (no loading-flag transition) never surfaced at all. The nonce
   * lets repeat failures of the same thread re-fire consumers.
   */
  messageLoadError: { threadId: string; nonce: number } | null;

  // UI states
  shortcutsDialogOpen: boolean;
  copiedMessageId: string | null;
  sidebarCollapsed: boolean;

  // Bulk selection state
  selectedThreadIds: Set<string>;
  isSelectMode: boolean;

  // Streaming state
  isStreaming: boolean;
  streamingContent: string;
  streamingMessageId: string | null;
  streamingCitations: Array<Record<string, unknown>>;
  streamingDiagnosticsTraceId: string | null;
  // True only while RAG retrieval is in flight (set at stream start, cleared on
  // the first token or rag_context event). Drives the composer's
  // "reading sources…" status phase.
  isRetrievingRag: boolean;
  /** Tool executions accumulated during the current streaming turn. */
  streamingSteps: ActivityStep[];
  /** Structured execution plan for the current streaming turn — lets the
   * transcript render the plan WHILE the turn streams instead of only after
   * commit. Mirrors the committed message's `plan` field shape exactly. */
  streamingPlan: PlanStep[];
  /** Planner-authored rationale for the current live plan. */
  streamingPlanReasoning: string;
  /** Display-safe server progress accumulated for the current turn. */
  streamingProgress: AgentProgressStep[];
  /** Bounded provider-authored reasoning summary for the live turn. */
  streamingReasoning: string;
  /** Elapsed time of the current turn as last reported by a `heartbeat`
   * frame (ms). null until the first heartbeat — a silent planner/LLM phase
   * is otherwise indistinguishable from a stalled run. */
  streamingElapsedMs: number | null;
  /** Truthful server-reported phase for the pre-first-token status pill. */
  streamingPhase: AgentStreamPhase | null;
  /** Specific server/tool activity shown beside the phase matrix. */
  streamingStatusDetail: string | null;
  // CX5: the workspace thread id that owns the CURRENT live stream (both the
  // main runStreamTurn path and the separate HITL confirm-resume path stamp
  // this). isStreaming etc. above stay global — single-flight streaming is
  // an invariant (see the ponytail comment in useChatStreaming's
  // runStreamTurn) — but a thread-scoped UI consumer can compare this against
  // its own activeThreadId to avoid rendering another thread's in-flight
  // turn. null when no stream is active.
  streamingThreadId: string | null;
}

export interface ChatActions {
  // Selection actions
  setCurrentWorkspace: (workspaceId: string | null) => Promise<void>;
  setCurrentConversation: (conversationId: string | null) => Promise<void>;
  setCurrentThread: (threadId: string | null) => void;
  setThreadProjectBinding: (
    threadId: string,
    projectId: string | null
  ) => boolean;
  registerThread: (thread: Thread) => void;

  // UI actions
  setShortcutsDialogOpen: (open: boolean) => void;
  setCopiedMessageId: (id: string | null) => void;
  setSidebarCollapsed: (collapsed: boolean) => void;

  // Workspace actions
  loadWorkspaces: () => Promise<void>;
  createWorkspace: (data: WorkspaceCreate) => Promise<Workspace | null>;
  updateWorkspace: (
    id: string,
    data: WorkspaceUpdate
  ) => Promise<Workspace | null>;
  deleteWorkspace: (id: string) => Promise<boolean>;

  // Conversation actions
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

  // Thread actions
  loadThreads: (conversationId: string) => Promise<void>;
  createThread: (data: ThreadCreate) => Promise<Thread | null>;
  updateThread: (id: string, data: ThreadUpdate) => Promise<Thread | null>;
  deleteThread: (id: string) => Promise<boolean>;
  resolveThread: (id: string) => Promise<Thread | null>;
  reopenThread: (id: string) => Promise<Thread | null>;

  // Bulk thread actions
  toggleSelectMode: () => void;
  toggleThreadSelection: (threadId: string) => void;
  selectAllThreads: (threadIds?: string[]) => void;
  clearSelection: () => void;
  bulkResolveThreads: () => Promise<BulkThreadResponse | null>;
  bulkArchiveThreads: () => Promise<BulkThreadResponse | null>;
  bulkSummarizeThreads: () => Promise<BulkThreadResponse | null>;
  bulkDeleteThreads: (ids?: string[]) => Promise<BulkThreadResponse | null>;

  // Message actions
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

  // Streaming actions
  streamMessage: (
    content: string,
    threadId?: string,
    useRag?: boolean
  ) => Promise<void>;
  stopStreaming: () => void;

  // Utility actions
  clearError: () => void;
  reset: () => void;
  initializeDefaultWorkspace: () => Promise<void>;
}

export type ChatStore = ChatState & ChatActions;

export type MessageFreshness = 'fresh' | 'stale' | 'refreshing';

export interface RefreshExpectation {
  persistedId?: string;
  runtimeId?: string;
  diagnostic?: {
    terminalReason: string;
    localCount: number;
    completedInBackground: boolean;
  };
}

/**
 * Shared typing for every slice creator. Slices are plain functions of
 * `(set, get, api)` combined with `...createXSlice(...a)` in chat-store.ts
 * (the standard Zustand "slices pattern") — this alias just pins the mutator
 * tuple (immer) so every slice sees the same, fully-typed `set`/`get` over
 * the whole combined store.
 */
export type ChatSliceCreator<T> = StateCreator<
  ChatStore,
  [['zustand/immer', never]],
  [],
  T
>;

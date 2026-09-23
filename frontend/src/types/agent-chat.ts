/**
 * Types for the Global Agent Chat system.
 * Extends chat-widget types with agent-specific capabilities.
 */

// ============================================================================
// Page Context Types
// ============================================================================

export type PageContextType =
  | 'project'
  | 'chat'
  | 'documents'
  | 'arxiv'
  | 'research'
  | 'analytics'
  | 'overview'
  | 'settings'
  | 'upload'
  | 'entities'
  | 'diagnostics'
  | 'unknown';

export interface PageContext {
  type: PageContextType;
  label: string;
  /** Project ID if on a project page */
  projectId?: string;
  /** Project name for display */
  projectName?: string;
  /** Additional context metadata */
  metadata?: Record<string, unknown>;
}

// ============================================================================
// Agent Message Types
// ============================================================================

export interface AgentCitation {
  documentId: string;
  documentTitle: string;
  snippet?: string;
  page?: number;
  score?: number;
}

export interface ToolExecution {
  id: string;
  toolName: string;
  toolDisplayName: string;
  args: Record<string, unknown>;
  /** 'cancelled': still running when the turn was stopped or superseded —
   * distinct from 'failed' (the tool itself errored). */
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  result?: unknown;
  error?: string;
  durationMs?: number;
}

export interface AgentMessage {
  id: string;
  role: 'user' | 'assistant' | 'tool';
  content: string;
  timestamp: Date;
  citations?: AgentCitation[];
  toolExecutions?: ToolExecution[];
  /** Agent execution plan snapshot for this turn, persisted for the message's lifetime */
  plan?: PlanStep[];
  isStreaming?: boolean;
  /** Whether this message represents an error */
  isError?: boolean;
  /** Backend thread message ID for persistence */
  backendMessageId?: string;
}

// ============================================================================
// Thread Types
// ============================================================================

export interface AgentThread {
  id: string;
  title: string;
  createdAt: Date;
  updatedAt: Date;
  messageCount: number;
  lastMessage?: string;
  pageContext?: PageContext;
  /** Linked project ID if started from project page */
  projectId?: string;
}

// ============================================================================
// UI State Types
// ============================================================================

export type AgentUIMode = 'closed' | 'panel' | 'sidebar';

export interface PendingConfirmation {
  threadId: string;
  assistantMessageId: string;
  /** Durable runs put their run id here; SSE-originated confirmations reuse
   * the thread id, which is what `/agent/stream/confirm` wants — but NOT what
   * the legacy `/agent/confirm/{job_id}` endpoint accepts. `origin` keeps the
   * two apart so a confirmation is never routed to an endpoint that cannot
   * resolve its id. */
  jobId: string;
  origin?: 'sse' | 'durable';
  tools: Array<{ name: string; args: Record<string, unknown> }>;
  message: string;
  waitTokenId?: string;
}

export interface PlanStep {
  step: number;
  description: string;
  tool: string;
  args_hint: Record<string, unknown>;
  depends_on: number[];
}

export interface AgentChatState {
  /** Current UI mode */
  uiMode: AgentUIMode;
  /** Active thread ID */
  activeThreadId: string | null;
  /** Thread list */
  threads: AgentThread[];
  /** Messages for active thread */
  messages: AgentMessage[];
  /** Whether assistant is streaming */
  isStreaming: boolean;
  /** Current input value */
  inputValue: string;
  /** Unread indicator */
  hasUnread: boolean;
  /** Current page context */
  pageContext: PageContext;
  /** Loading states */
  isLoadingThreads: boolean;
  isLoadingMessages: boolean;
  /** Set when the thread list or a thread's messages failed to load. Without
   * it a failed load rendered the empty state, which reads as "this thread is
   * gone" rather than "we could not reach the server". */
  threadsError: string | null;
  messagesError: string | null;
  /** Pending human-in-the-loop confirmation */
  pendingConfirmations: Record<string, PendingConfirmation>;
  /** Whether a confirmation action is in progress */
  isConfirming: boolean;
  /** Incremented when agent tools mutate project data (documents, notes, etc.) */
  projectDataVersion: number;
  /** Current execution plan from the planner node */
  currentPlan: PlanStep[] | null;
}

export interface AgentChatActions {
  // UI
  openPanel: () => void;
  openSidebar: () => void;
  close: () => void;
  toggle: () => void;
  // Input
  setInputValue: (value: string) => void;
  // Messages
  sendMessage: () => Promise<void>;
  clearMessages: () => void;
  stopGeneration: () => void;
  retryLastMessage: () => void;
  // Threads
  newThread: () => void;
  selectThread: (threadId: string) => void;
  loadThreads: () => Promise<void>;
  loadThreadMessages: (threadId: string) => Promise<void>;
  // Context
  setPageContext: (context: PageContext) => void;
  // Human-in-the-loop
  confirmAction: (threadId: string, confirmed: boolean) => Promise<void>;
}

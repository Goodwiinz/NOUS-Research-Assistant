import type { RuntimeAttachment } from '@nous/chat-runtime/types';
/**
 * TypeScript types for NOUS thread-centric chat system
 * Based on backend/src/schemas/chat.py
 *
 * Workspace/conversation/thread/chat-message shapes are adopted from the
 * generated OpenAPI contract (`types/api/workspace-contract.ts`) rather than
 * hand-duplicated. Most are direct re-exports; a few are narrowed or relaxed
 * — see the comment above each one for why. This keeps the public names
 * (`Workspace`, `Thread`, `ChatMessage`, ...) stable so existing store/
 * component imports from `@/types/workspace` don't need to change.
 */

import type { AgentProgressStep } from '@/services/agentStreamEvents';
import type { PlanStep } from './agent-chat';
import type {
  ApiBulkThreadResponse,
  ApiCollection,
  ApiCollectionCreate,
  ApiCollectionDetail,
  ApiCollectionListResponse,
  ApiCollectionUpdate,
  ApiConversation,
  ApiConversationCreate,
  ApiConversationListResponse,
  ApiConversationUpdate,
  ApiMessage,
  ApiMessageCreate,
  ApiMessageListResponse,
  ApiMessageUpdate,
  ApiThread,
  ApiThreadCreate,
  ApiThreadDetail,
  ApiThreadList,
  ApiThreadUpdate,
  ApiWorkspace,
  ApiWorkspaceCreate,
  ApiWorkspaceDetail,
  ApiWorkspaceMember,
  ApiWorkspaceMemberCreate,
  ApiWorkspaceMemberUpdate,
  ApiWorkspaceUpdate,
} from './api/workspace-contract';

// ============================================================================
// Enums
// ============================================================================

export enum WorkspaceRole {
  OWNER = 'owner',
  ADMIN = 'admin',
  EDITOR = 'editor',
  VIEWER = 'viewer',
}

export enum ThreadStatus {
  ACTIVE = 'active',
  RESOLVED = 'resolved',
  ARCHIVED = 'archived',
}

export enum MessageRole {
  USER = 'user',
  ASSISTANT = 'assistant',
  SYSTEM = 'system',
  TOOL = 'tool',
}

// ============================================================================
// Workspace Types (generated contract)
// ============================================================================

/**
 * openapi-typescript marks `is_public` as required because the backend
 * Pydantic field declares a default (`is_public: bool = False`) —
 * `defaultNonNullable` treats any defaulted field as always-present. The
 * client may still omit it and let the backend default apply (existing
 * callers do), so it's relaxed back to optional here.
 */
export type WorkspaceCreate = Omit<ApiWorkspaceCreate, 'is_public'> & {
  is_public?: ApiWorkspaceCreate['is_public'];
};
export type WorkspaceUpdate = ApiWorkspaceUpdate;
export type Workspace = ApiWorkspace;
export type WorkspaceDetail = ApiWorkspaceDetail;
export type WorkspaceMember = ApiWorkspaceMember;
export type WorkspaceMemberCreate = ApiWorkspaceMemberCreate;
export type WorkspaceMemberUpdate = ApiWorkspaceMemberUpdate;

// ============================================================================
// Conversation Types (generated contract)
// ============================================================================

export type ConversationCreate = ApiConversationCreate;
export type ConversationUpdate = ApiConversationUpdate;
export type Conversation = ApiConversation;
export type ConversationListResponse = ApiConversationListResponse;

// ============================================================================
// Thread Types (generated contract)
// ============================================================================

export type ThreadCreate = ApiThreadCreate;
export type ThreadUpdate = ApiThreadUpdate;
/** Project this thread is bound to (drives chat project context). Note the
 * create-side field is `project_id` (ThreadCreate); the read side reports it
 * under this different name, `source_project_id` — that's a genuine backend
 * asymmetry, not something normalized away here. */
export type Thread = ApiThread;

// `messages` narrowed to the view-model-aware `ChatMessage` (below) rather
// than the raw generated message shape, so `plan`/`tool_executions`/
// `token_usage`/`citations`/`attachments` keep their concrete frontend types.
export type ThreadDetail = Omit<ApiThreadDetail, 'messages'> & {
  messages: ChatMessage[];
};

export type ThreadListResponse = ApiThreadList;

// ============================================================================
// Bulk Thread Operations
// ============================================================================

export interface BulkThreadRequest {
  thread_ids: string[];
}

export interface BulkThreadResult {
  thread_id: string;
  success: boolean;
  error?: string;
  thread?: Thread;
}

export type BulkThreadResponse = ApiBulkThreadResponse;

// ============================================================================
// Chat Message Types
// ============================================================================

export interface Citation {
  id: string;
  document_id?: string; // Optional: may not have a database UUID
  external_reference_id?: string; // For non-database references (e.g., arXiv IDs)
  /** Stable one-based index used by persisted `[Doc N]` markers. */
  source_position?: number;
  chunk_index?: number;
  chunk_id?: string;
  snippet?: string;
  snippet_preview?: string;
  page_number?: number;
  score?: number;
  rerank_score?: number;
  document_title?: string;
  document_type?: string;
}

export type MessageAttachment = RuntimeAttachment;

// Citation input for creating messages with RAG sources
export interface CitationCreate {
  document_id?: string; // Optional: may not have a database UUID
  external_reference_id?: string; // For non-database references (e.g., arXiv IDs)
  source_position?: number;
  chunk_index?: number;
  chunk_id?: string;
  snippet?: string;
  snippet_preview?: string;
  page_number?: number;
  score?: number;
  rerank_score?: number;
  document_title?: string;
  document_type?: string;
}

/**
 * `role` is required in the generated type for the same reason as
 * `WorkspaceCreate.is_public` above (Pydantic default `role: MessageRole =
 * MessageRole.USER` β†’ `defaultNonNullable`); real callers omit it and let
 * the backend default apply, so it's relaxed back to optional.
 * `citations` keeps the generated (nullable-field) nested type — the
 * handwritten `CitationCreate[]` below is a narrower shape that's still
 * assignable into it, so no override is needed there.
 */
export type ChatMessageCreate = Omit<ApiMessageCreate, 'role'> & {
  role?: MessageRole;
};

export type ChatMessageUpdate = ApiMessageUpdate;

/**
 * `plan` / `tool_executions` / `token_usage` / `progress_steps` are persisted as loose JSONB on
 * the backend, so the generated response types them as an untyped
 * passthrough (`Record<string, unknown>[] | null`). The frontend view-model
 * needs the concrete shapes (`PlanStep[]`, `DbToolExecution[]`, ...), so
 * those fields β€” plus `role` (enum, not a plain string union) and
 * `citations`/`attachments` (concrete `Citation`/`MessageAttachment`, not
 * the generated nested response types) β€” are narrowed back here rather than
 * left as the raw generated shape.
 */
export type ChatMessage = Omit<
  ApiMessage,
  | 'role'
  | 'citations'
  | 'attachments'
  | 'plan'
  | 'tool_executions'
  | 'token_usage'
  | 'progress_steps'
> & {
  role: MessageRole;
  /** Agent tool executions for this turn (JSONB passthrough from the
   * backend: {id, tool_name, tool_display_name, args, status, result,
   * error, duration_ms}[]). Absent for legacy and non-agent rows. */
  tool_executions?: DbToolExecution[];
  /** Planner steps persisted for this turn (chat_messages.plan JSONB).
   * Absent for legacy rows, user rows, and turns without a plan. */
  plan?: PlanStep[];
  /** Planner's top-level rationale for `plan` (chat_messages.plan_reasoning).
   * Absent for legacy rows, user rows, and turns without a plan. */
  plan_reasoning?: string;
  /** Aggregated per-turn LLM token usage (chat_messages.token_usage JSONB).
   * Absent when the turn reported no usage. */
  token_usage?: { input_tokens: number; output_tokens: number };
  /** Display-safe progress emitted while producing this turn. */
  progress_steps?: AgentProgressStep[];
  citations: Citation[];
  attachments: MessageAttachment[];
};

/** Persisted agent tool-execution record (chat_messages.tool_executions). */
export interface DbToolExecution {
  id?: string;
  tool_name: string;
  tool_display_name?: string;
  args?: Record<string, unknown>;
  status?: string;
  result?: unknown;
  error?: string | null;
  duration_ms?: number | null;
}

export type ChatMessageListResponse = Omit<
  ApiMessageListResponse,
  'messages'
> & {
  messages: ChatMessage[];
};

// ============================================================================
// Collection Types (generated contract)
// ============================================================================

export type CollectionCreate = ApiCollectionCreate;
export type CollectionUpdate = ApiCollectionUpdate;
export type Collection = ApiCollection;
export type CollectionDetail = ApiCollectionDetail;
export type CollectionListResponse = ApiCollectionListResponse;

// ============================================================================
// Search Types
// ============================================================================

export interface WorkspaceSearchRequest {
  query: string;
  workspace_id: string;
  collection_ids?: string[];
  conversation_ids?: string[];
  date_from?: string;
  date_to?: string;
  search_type?: string;
  include_messages?: boolean;
  include_documents?: boolean;
  top_k?: number;
}

export interface WorkspaceSearchResult {
  result_type: 'message' | 'document' | 'thread';
  id: string;
  score: number;
  snippet: string;
  thread_id?: string;
  thread_title?: string;
  conversation_id?: string;
  conversation_title?: string;
  document_id?: string;
  document_title?: string;
  created_at: string;
}

export interface WorkspaceSearchResponse {
  results: WorkspaceSearchResult[];
  total: number;
  query: string;
  search_latency_ms: number;
}

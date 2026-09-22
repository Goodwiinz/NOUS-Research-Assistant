/**
 * Type aliases for the workspace/conversation/thread/chat-message HTTP
 * boundary, sourced directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`).
 *
 * These are the wire shapes exactly as the backend declares them — the
 * source of truth for `workspaceService.ts`. Frontend-only concepts
 * (enums, streaming frames, planner steps, optimistic ids, view-model
 * normalization) are NOT here; see `types/workspace.ts`, which derives its
 * public domain types from these aliases and layers narrow overrides where
 * the generated shape is too loose (JSONB passthrough columns) or too
 * strict (fields the client may omit despite the schema marking them
 * required — see the comment on `ApiWorkspaceCreate` usage in
 * `types/workspace.ts`).
 */
import type { components } from '@/types/generated/api';

// ============================================================================
// Workspace
// ============================================================================

export type ApiWorkspace = components['schemas']['WorkspaceResponse'];
export type ApiWorkspaceDetail =
  components['schemas']['WorkspaceDetailResponse'];
export type ApiWorkspaceCreate = components['schemas']['WorkspaceCreate'];
export type ApiWorkspaceUpdate = components['schemas']['WorkspaceUpdate'];
export type ApiWorkspaceMember =
  components['schemas']['WorkspaceMemberResponse'];
export type ApiWorkspaceMemberCreate =
  components['schemas']['WorkspaceMemberCreate'];
export type ApiWorkspaceMemberUpdate =
  components['schemas']['WorkspaceMemberUpdate'];

// ============================================================================
// Conversation
// ============================================================================

export type ApiConversation = components['schemas']['ConversationResponse'];
export type ApiConversationCreate = components['schemas']['ConversationCreate'];
export type ApiConversationUpdate = components['schemas']['ConversationUpdate'];
export type ApiConversationListResponse =
  components['schemas']['ConversationListResponse'];

// ============================================================================
// Thread
// ============================================================================

export type ApiThread = components['schemas']['ThreadResponse'];
export type ApiThreadDetail = components['schemas']['ThreadDetailResponse'];
export type ApiThreadCreate = components['schemas']['ThreadCreate'];
export type ApiThreadUpdate = components['schemas']['ThreadUpdate'];
// Qualified: the OpenAPI schema has two distinct `ThreadListResponse`
// definitions (chat threads vs. the agent-execute job endpoint). This is
// the chat one, used by `workspaceService.listThreads`.
export type ApiThreadList =
  components['schemas']['src__schemas__chat__ThreadListResponse'];
/** The workspace-wide endpoint deliberately reuses the existing chat thread
 * list response; this semantic alias keeps callers tied to that generated
 * contract without suggesting that the rows share one conversation. */
export type ApiWorkspaceThreadList = ApiThreadList;

// ============================================================================
// Chat message
// ============================================================================

export type ApiMessage = components['schemas']['ChatMessageResponse'];
export type ApiMessageCreate = components['schemas']['ChatMessageCreate'];
export type ApiMessageUpdate = components['schemas']['ChatMessageUpdate'];
export type ApiMessageListResponse =
  components['schemas']['ChatMessageListResponse'];

// ============================================================================
// Collection
// ============================================================================

export type ApiCollection = components['schemas']['CollectionResponse'];
export type ApiCollectionDetail =
  components['schemas']['CollectionDetailResponse'];
export type ApiCollectionCreate = components['schemas']['CollectionCreate'];
export type ApiCollectionUpdate = components['schemas']['CollectionUpdate'];
export type ApiCollectionListResponse =
  components['schemas']['CollectionListResponse'];

// ============================================================================
// Bulk thread operations
// ============================================================================

export type ApiBulkThreadResponse = components['schemas']['BulkThreadResponse'];

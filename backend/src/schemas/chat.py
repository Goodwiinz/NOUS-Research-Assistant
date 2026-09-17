"""
Pydantic schemas for Terminal Observatory thread-centric chat system
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, validator

# ============================================================================
# Enums
# ============================================================================


class WorkspaceRole(str, Enum):
    """Workspace member roles"""

    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class ThreadStatus(str, Enum):
    """Thread status"""

    ACTIVE = "active"
    RESOLVED = "resolved"
    ARCHIVED = "archived"


class MessageRole(str, Enum):
    """Message sender role"""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


# ============================================================================
# Base Schemas
# ============================================================================


class TimestampMixin(BaseModel):
    """Mixin for timestamp fields"""

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# Workspace Schemas
# ============================================================================


class WorkspaceBase(BaseModel):
    """Base workspace schema"""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    is_public: bool = False


class WorkspaceCreate(WorkspaceBase):
    """Create workspace request"""

    organization_id: Optional[UUID] = None


class WorkspaceUpdate(BaseModel):
    """Update workspace request"""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    is_public: Optional[bool] = None
    is_archived: Optional[bool] = None


class WorkspaceMemberBase(BaseModel):
    """Base workspace member schema"""

    user_id: UUID
    role: WorkspaceRole = WorkspaceRole.VIEWER


class WorkspaceMemberCreate(WorkspaceMemberBase):
    """Add member to workspace"""

    pass


class WorkspaceMemberUpdate(BaseModel):
    """Update member role"""

    role: WorkspaceRole


class WorkspaceMemberResponse(WorkspaceMemberBase, TimestampMixin):
    """Workspace member response"""

    id: UUID
    workspace_id: UUID
    joined_at: datetime
    invited_by_id: Optional[UUID] = None

    # Nested user info (populated by API)
    user_email: Optional[str] = None
    user_name: Optional[str] = None


class WorkspaceResponse(WorkspaceBase, TimestampMixin):
    """Workspace response"""

    id: UUID
    owner_id: UUID
    organization_id: Optional[UUID] = None
    is_archived: bool = False

    # Counts (populated by API)
    member_count: Optional[int] = None
    conversation_count: Optional[int] = None
    collection_count: Optional[int] = None


class WorkspaceDetailResponse(WorkspaceResponse):
    """Detailed workspace response with members"""

    members: List[WorkspaceMemberResponse] = []


# ============================================================================
# Conversation Schemas
# ============================================================================


class ConversationBase(BaseModel):
    """Base conversation schema"""

    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = None


class ConversationCreate(ConversationBase):
    """Create conversation request"""

    workspace_id: UUID


class ConversationUpdate(BaseModel):
    """Update conversation request"""

    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = None
    is_archived: Optional[bool] = None
    is_pinned: Optional[bool] = None


class ConversationResponse(ConversationBase, TimestampMixin):
    """Conversation response"""

    id: UUID
    workspace_id: UUID
    created_by_id: UUID
    is_archived: bool = False
    is_pinned: bool = False
    last_activity_at: datetime

    # Counts (populated by API)
    thread_count: Optional[int] = None
    message_count: Optional[int] = None


class ConversationListResponse(BaseModel):
    """Paginated conversation list"""

    conversations: List[ConversationResponse]
    total: int
    page: int
    limit: int
    has_more: bool


# ============================================================================
# Thread Schemas
# ============================================================================


class ThreadBase(BaseModel):
    """Base thread schema"""

    title: Optional[str] = Field(None, max_length=500)


class ThreadCreate(ThreadBase):
    """Create thread request"""

    conversation_id: UUID
    initial_message: Optional[str] = None  # Optional first message
    project_id: Optional[UUID] = Field(
        None,
        description="Optional project ID to auto-link this thread to a research project",
    )


class ThreadUpdate(BaseModel):
    """Update thread request"""

    title: Optional[str] = Field(None, max_length=500)
    summary: Optional[str] = None
    status: Optional[ThreadStatus] = None


class ThreadResponse(ThreadBase, TimestampMixin):
    """Thread response"""

    id: UUID
    conversation_id: UUID
    summary: Optional[str] = None
    last_message_preview: Optional[str] = None
    status: ThreadStatus = ThreadStatus.ACTIVE
    last_message_at: datetime
    message_count: int = 0
    token_count: int = 0
    created_by_id: Optional[UUID] = None
    source_project_id: Optional[UUID] = None


class ThreadDetailResponse(ThreadResponse):
    """Thread with messages"""

    messages: List["ChatMessageResponse"] = []


class ThreadListResponse(BaseModel):
    """Paginated thread list"""

    threads: List[ThreadResponse]
    total: int
    page: int
    limit: int
    has_more: bool


# ============================================================================
# Bulk Thread Operations
# ============================================================================


class BulkThreadRequest(BaseModel):
    """Bulk thread operation request"""

    thread_ids: List[UUID] = Field(..., min_length=1, max_length=100)


class BulkThreadResult(BaseModel):
    """Result for a single thread in bulk operation"""

    thread_id: UUID
    success: bool
    error: Optional[str] = None
    thread: Optional[ThreadResponse] = None


class BulkThreadResponse(BaseModel):
    """Bulk thread operation response"""

    total: int
    succeeded: int
    failed: int
    results: List[BulkThreadResult]


# ============================================================================
# Chat Message Schemas
# ============================================================================


class ChatMessageBase(BaseModel):
    """Base chat message schema"""

    content: str = Field(..., min_length=1)
    role: MessageRole = MessageRole.USER


class CitationCreate(BaseModel):
    """Citation input for creating messages with sources"""

    document_id: Optional[UUID] = None  # Optional: may not have a database UUID
    external_reference_id: Optional[str] = (
        None  # For non-UUID references (e.g., arXiv IDs)
    )
    chunk_index: Optional[int] = None
    chunk_id: Optional[str] = None
    snippet: Optional[str] = None
    snippet_preview: Optional[str] = None
    page_number: Optional[int] = None
    score: Optional[float] = None
    rerank_score: Optional[float] = None
    document_title: Optional[str] = None
    document_type: Optional[str] = None


class ChatMessageCreate(ChatMessageBase):
    """Create chat message request"""

    thread_id: UUID
    attachment_ids: Optional[List[UUID]] = None  # Document IDs to attach
    citations: Optional[List[CitationCreate]] = None  # Citations from RAG retrieval
    latency_ms: Optional[int] = None  # Client-measured response time (ms)
    stopped: Optional[bool] = None  # User stopped this response mid-stream
    # Idempotency key for user turns (audit B8-I2). UUID, matching the
    # chat_messages.client_message_id GUID column and the partial unique index
    # (thread_id, client_message_id) the agent path already upserts against.
    client_message_id: Optional[UUID] = None


class ChatMessageUpdate(BaseModel):
    """Update chat message (limited - mainly for feedback)"""

    feedback_rating: Optional[int] = Field(None, ge=1, le=5)
    feedback_text: Optional[str] = None


class CitationResponse(BaseModel):
    """Citation in a message"""

    id: UUID
    document_id: Optional[UUID] = None  # Optional: may not have a database reference
    external_reference_id: Optional[str] = (
        None  # For non-database references (e.g., arXiv IDs)
    )
    source_position: Optional[int] = None
    chunk_index: Optional[int] = None
    chunk_id: Optional[str] = None
    snippet: Optional[str] = None
    snippet_preview: Optional[str] = None
    page_number: Optional[int] = None
    score: Optional[float] = None
    rerank_score: Optional[float] = None

    # Document info (populated by API or stored directly for external refs)
    document_title: Optional[str] = None
    document_type: Optional[str] = None

    class Config:
        from_attributes = True


class MessageAttachmentResponse(BaseModel):
    """Attachment in a message"""

    id: UUID
    document_id: UUID
    display_name: Optional[str] = None
    thumbnail_url: Optional[str] = None

    # Document info (populated by API)
    document_title: Optional[str] = None
    document_type: Optional[str] = None
    mime_type: Optional[str] = None

    class Config:
        from_attributes = True


class ChatMessageResponse(ChatMessageBase, TimestampMixin):
    """Chat message response"""

    id: UUID
    thread_id: UUID
    # Stable client/runtime identity carried across optimistic rendering,
    # persistence, and reload. Legacy rows predate this field and remain null.
    client_message_id: Optional[UUID] = None
    user_id: Optional[UUID] = None
    token_count: int = 0
    latency_ms: Optional[int] = None
    # Time to first token, same origin as latency_ms — the difference is the
    # time spent writing. Null when the turn streamed no token, and on rows
    # written before the column existed.
    ttft_ms: Optional[int] = None
    stopped: Optional[bool] = None
    model_name: Optional[str] = None
    model_version: Optional[str] = None
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    feedback_rating: Optional[int] = None
    feedback_text: Optional[str] = None
    # Agent tool executions recorded for this turn (JSONB passthrough:
    # [{id, tool_name, tool_display_name, args, status, result, error,
    # duration_ms}, ...]). Null for legacy rows and non-agent messages.
    tool_executions: Optional[List[dict]] = None
    # Per-turn agent provenance (JSONB passthrough). plan: planner steps
    # [{step, description, tool, args_hint, depends_on}]; token_usage:
    # {input_tokens, output_tokens}. Null for legacy/non-agent rows.
    plan: Optional[List[dict]] = None
    # Planner's top-level rationale for `plan`. Null for legacy/non-agent rows.
    plan_reasoning: Optional[str] = None
    # Bounded provider-authored reasoning summary. Null for legacy/non-agent
    # rows and when the provider emitted no public summary.
    reasoning_summary: Optional[str] = None
    token_usage: Optional[dict] = None
    # Display-safe server-authored progress: [{phase, detail}].
    progress_steps: Optional[List[dict]] = None

    # Nested data
    citations: List[CitationResponse] = []
    attachments: List[MessageAttachmentResponse] = []


class ChatMessageListResponse(BaseModel):
    """Paginated message list"""

    messages: List[ChatMessageResponse]
    total: int
    page: int
    limit: int
    has_more: bool


# ============================================================================
# Collection Schemas
# ============================================================================


class CollectionBase(BaseModel):
    """Base collection schema"""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    color: Optional[str] = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    icon: Optional[str] = Field(None, max_length=50)


class CollectionCreate(CollectionBase):
    """Create collection request"""

    workspace_id: UUID
    document_ids: Optional[List[UUID]] = None  # Initial documents


class CollectionUpdate(BaseModel):
    """Update collection request"""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    color: Optional[str] = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    icon: Optional[str] = Field(None, max_length=50)


class CollectionDocumentAdd(BaseModel):
    """Add documents to collection"""

    document_ids: List[UUID]


class CollectionDocumentRemove(BaseModel):
    """Remove documents from collection"""

    document_ids: List[UUID]


class CollectionDocumentReorder(BaseModel):
    """Reorder documents in collection"""

    document_id: UUID
    new_position: int = Field(..., ge=0)


class CollectionResponse(CollectionBase, TimestampMixin):
    """Collection response"""

    id: UUID
    workspace_id: UUID
    document_count: int = 0


class CollectionDetailResponse(CollectionResponse):
    """Collection with documents"""

    documents: List[Dict[str, Any]] = []  # Document summaries


class CollectionListResponse(BaseModel):
    """Paginated collection list"""

    collections: List[CollectionResponse]
    total: int
    page: int
    limit: int
    has_more: bool


# ============================================================================
# Chat Completion Schemas (for AI interactions)
# ============================================================================


class ChatCompletionRequest(BaseModel):
    """Request for AI chat completion"""

    thread_id: UUID
    message: str = Field(..., min_length=1)

    # RAG options
    use_rag: bool = True
    collection_ids: Optional[List[UUID]] = None  # Limit to specific collections
    search_type: str = "hybrid"
    top_k: int = Field(default=5, ge=1, le=20)

    # Model options
    model: Optional[str] = None
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: Optional[int] = Field(None, ge=1, le=4096)

    # Streaming
    stream: bool = False


class ChatCompletionResponse(BaseModel):
    """Response from AI chat completion"""

    message: ChatMessageResponse
    usage: Dict[str, int] = {}  # Token usage stats

    # RAG info
    sources_used: int = 0
    search_latency_ms: Optional[int] = None
    generation_latency_ms: Optional[int] = None


class StreamingChatChunk(BaseModel):
    """Streaming chat response chunk"""

    chunk_type: str  # "content", "citation", "done", "error"
    content: Optional[str] = None
    citation: Optional[CitationResponse] = None
    message_id: Optional[UUID] = None
    error: Optional[str] = None


# ============================================================================
# Search Within Workspace Schemas
# ============================================================================


class WorkspaceSearchRequest(BaseModel):
    """Search within a workspace"""

    query: str = Field(..., min_length=1)
    workspace_id: UUID

    # Filters
    collection_ids: Optional[List[UUID]] = None
    conversation_ids: Optional[List[UUID]] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None

    # Search options
    search_type: str = "hybrid"
    include_messages: bool = True
    include_documents: bool = True
    top_k: int = Field(default=10, ge=1, le=50)


class WorkspaceSearchResult(BaseModel):
    """Search result item"""

    result_type: str  # "message", "document", "thread"
    id: UUID
    score: float
    snippet: str

    # Context
    thread_id: Optional[UUID] = None
    thread_title: Optional[str] = None
    conversation_id: Optional[UUID] = None
    conversation_title: Optional[str] = None
    document_id: Optional[UUID] = None
    document_title: Optional[str] = None

    created_at: datetime


class WorkspaceSearchResponse(BaseModel):
    """Search response"""

    results: List[WorkspaceSearchResult]
    total: int
    query: str
    search_latency_ms: int


# Forward references for nested models
ThreadDetailResponse.model_rebuild()

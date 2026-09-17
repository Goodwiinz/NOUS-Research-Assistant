"""
Response serializers shared across workspace resource routes.

Task 4.2 split ``backend/src/api/threads/workspaces.py`` by resource. These
converters are consumed by more than one resource group — e.g.
``_message_to_response`` backs thread, message, and thread-detail responses
— so they live in one shared module instead of being duplicated per resource
file.

Task 4.3 moved the correlated last-message-preview subquery itself (a query
concern) to ``services.threads.thread_service``, which now owns building
thread-list rows; ``THREAD_PREVIEW_MAX_CHARS`` is re-exported here only for
backward compatibility with a couple of tests that import it from this path.
"""

from typing import Optional

from src.models.chat_message import ChatMessage
from src.models.citation import Citation
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.message_attachment import MessageAttachment
from src.models.thread import Thread
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.chat import (
    ChatMessageResponse,
    CitationResponse,
    CollectionDetailResponse,
    CollectionResponse,
    ConversationResponse,
    MessageAttachmentResponse,
    ThreadDetailResponse,
    ThreadResponse,
    WorkspaceDetailResponse,
    WorkspaceMemberResponse,
    WorkspaceResponse,
)
from src.services.agent._pii_redact import redact_tool_executions
from src.services.threads.thread_service import THREAD_PREVIEW_MAX_CHARS

__all__ = [
    "THREAD_PREVIEW_MAX_CHARS",
    "_workspace_to_response",
    "_workspace_to_detail_response",
    "_member_to_response",
    "_conversation_to_response",
    "_thread_to_response",
    "_thread_to_detail_response",
    "_message_to_response",
    "_citation_to_response",
    "_attachment_to_response",
    "_collection_to_response",
    "_collection_to_detail_response",
]


def _workspace_to_response(workspace: Workspace) -> WorkspaceResponse:
    """Convert Workspace model to response schema"""
    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        is_public=workspace.is_public,
        is_archived=workspace.is_archived,
        owner_id=workspace.owner_id,
        organization_id=workspace.organization_id,
        member_count=len(workspace.members) if workspace.members else 0,
        conversation_count=(
            len(workspace.conversations) if workspace.conversations else 0
        ),
        collection_count=len(workspace.collections) if workspace.collections else 0,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


def _workspace_to_detail_response(workspace: Workspace) -> WorkspaceDetailResponse:
    """Convert Workspace model to detail response schema"""
    return WorkspaceDetailResponse(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        is_public=workspace.is_public,
        is_archived=workspace.is_archived,
        owner_id=workspace.owner_id,
        organization_id=workspace.organization_id,
        member_count=len(workspace.members) if workspace.members else 0,
        conversation_count=(
            len(workspace.conversations) if workspace.conversations else 0
        ),
        collection_count=len(workspace.collections) if workspace.collections else 0,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        members=[_member_to_response(m) for m in workspace.members if not m.is_deleted],
    )


def _member_to_response(member: WorkspaceMember) -> WorkspaceMemberResponse:
    """Convert WorkspaceMember model to response schema"""
    return WorkspaceMemberResponse(
        id=member.id,
        workspace_id=member.workspace_id,
        user_id=member.user_id,
        role=member.role.value if member.role else "viewer",
        joined_at=member.joined_at,
        invited_by_id=member.invited_by_id,
        created_at=member.created_at,
        updated_at=member.updated_at,
        user_email=member.user.email if member.user else None,
        user_name=member.user.full_name if member.user else None,
    )


def _conversation_to_response(
    conversation: Conversation,
    thread_count: int | None = None,
    message_count: int | None = None,
) -> ConversationResponse:
    """Convert Conversation model to response schema.

    thread_count/message_count may be supplied from a grouped COUNT/SUM so the
    list path need not selectinload every thread row per conversation just to
    size the sidebar badges. When omitted (single-conversation callers that
    already eager-load threads), they fall back to the loaded relationship.
    """
    return ConversationResponse(
        id=conversation.id,
        workspace_id=conversation.workspace_id,
        title=conversation.title,
        description=conversation.description,
        is_archived=conversation.is_archived,
        is_pinned=conversation.is_pinned,
        last_activity_at=conversation.last_activity_at,
        created_by_id=conversation.created_by_id,
        thread_count=(
            thread_count if thread_count is not None else conversation.thread_count
        ),
        message_count=(
            message_count
            if message_count is not None
            else (
                sum(t.message_count or 0 for t in conversation.threads)
                if conversation.threads
                else 0
            )
        ),
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _thread_to_response(
    thread: Thread, last_message_preview: Optional[str] = None
) -> ThreadResponse:
    """Convert Thread model to response schema"""
    return ThreadResponse(
        id=thread.id,
        conversation_id=thread.conversation_id,
        title=thread.title or thread.generate_title(),
        summary=thread.summary,
        last_message_preview=last_message_preview,
        status=thread.status.value if thread.status else "active",
        last_message_at=thread.last_message_at,
        message_count=thread.message_count or 0,
        token_count=thread.token_count or 0,
        created_by_id=thread.created_by_id,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        source_project_id=thread.source_project_id,
    )


def _thread_to_detail_response(
    thread: Thread, include_messages: bool = True
) -> ThreadDetailResponse:
    """Convert Thread model to detail response schema"""
    messages = []
    if include_messages and thread.messages:
        messages = [
            _message_to_response(m)
            for m in thread.messages
            # Edit-and-resend tombstone — twin of the loop in
            # ``api/threads/threads.py``; keep both in sync.
            if not m.is_deleted and m.superseded_by_message_id is None
        ]

    return ThreadDetailResponse(
        id=thread.id,
        conversation_id=thread.conversation_id,
        title=thread.title or thread.generate_title(),
        summary=thread.summary,
        status=thread.status.value if thread.status else "active",
        last_message_at=thread.last_message_at,
        message_count=thread.message_count or 0,
        token_count=thread.token_count or 0,
        created_by_id=thread.created_by_id,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        source_project_id=thread.source_project_id,
        messages=messages,
    )


def _message_to_response(message: ChatMessage) -> ChatMessageResponse:
    """Convert ChatMessage model to response schema"""
    return ChatMessageResponse(
        id=message.id,
        thread_id=message.thread_id,
        client_message_id=message.client_message_id,
        user_id=message.user_id,
        role=message.role.value if message.role else "user",
        content=message.content,
        token_count=message.token_count or 0,
        latency_ms=message.latency_ms,
        ttft_ms=message.ttft_ms,
        stopped=message.stopped,
        model_name=message.model_name,
        model_version=message.model_version,
        tool_name=message.tool_name,
        tool_call_id=message.tool_call_id,
        feedback_rating=message.feedback_rating,
        feedback_text=message.feedback_text,
        tool_executions=redact_tool_executions(message.tool_executions),
        plan=message.plan,
        plan_reasoning=message.plan_reasoning,
        reasoning_summary=message.reasoning_summary,
        token_usage=message.token_usage,
        progress_steps=message.progress_steps,
        citations=(
            [_citation_to_response(c) for c in message.citations]
            if message.citations
            else []
        ),
        attachments=(
            [_attachment_to_response(a) for a in message.attachments]
            if message.attachments
            else []
        ),
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


def _citation_to_response(citation: Citation) -> CitationResponse:
    """Convert Citation model to response schema"""
    return CitationResponse(
        id=citation.id,
        document_id=citation.document_id,
        external_reference_id=citation.external_reference_id,  # For arXiv IDs, etc.
        source_position=citation.source_position,
        chunk_index=citation.chunk_index,
        chunk_id=citation.chunk_id,
        snippet=citation.snippet,
        snippet_preview=(
            citation.snippet[:200] + "..."
            if citation.snippet and len(citation.snippet) > 200
            else citation.snippet
        ),
        page_number=citation.page_number,
        score=citation.score,
        rerank_score=citation.rerank_score,
        # Use stored title/type for external refs, or get from document relationship
        document_title=citation.document_title
        or (citation.document.title if citation.document else None),
        document_type=citation.document_type
        or (
            citation.document.document_type.value
            if citation.document and citation.document.document_type
            else None
        ),
    )


def _attachment_to_response(attachment: MessageAttachment) -> MessageAttachmentResponse:
    """Convert MessageAttachment model to response schema"""
    return MessageAttachmentResponse(
        id=attachment.id,
        document_id=attachment.document_id,
        display_name=attachment.display_name,
        thumbnail_url=attachment.thumbnail_url,
        document_title=attachment.document.title if attachment.document else None,
        document_type=(
            attachment.document.document_type.value
            if attachment.document and attachment.document.document_type
            else None
        ),
        mime_type=attachment.document.mime_type if attachment.document else None,
    )


def _collection_to_response(collection: Collection) -> CollectionResponse:
    """Convert Collection model to response schema"""
    document_count = getattr(collection, "_document_count", None)
    return CollectionResponse(
        id=collection.id,
        workspace_id=collection.workspace_id,
        name=collection.name,
        description=collection.description,
        color=collection.color,
        icon=collection.icon,
        document_count=(
            collection.document_count if document_count is None else document_count
        ),
        created_at=collection.created_at,
        updated_at=collection.updated_at,
    )


def _collection_to_detail_response(collection: Collection) -> CollectionDetailResponse:
    """Convert Collection model to detail response schema"""
    documents = []
    if collection.documents:
        for cd in collection.documents:
            if not cd.is_deleted and cd.document:
                documents.append(
                    {
                        "id": str(cd.document.id),
                        "title": cd.document.title,
                        "document_type": (
                            cd.document.document_type.value
                            if cd.document.document_type
                            else None
                        ),
                        "sort_order": cd.sort_order,
                    }
                )

    return CollectionDetailResponse(
        id=collection.id,
        workspace_id=collection.workspace_id,
        name=collection.name,
        description=collection.description,
        color=collection.color,
        icon=collection.icon,
        document_count=collection.document_count,
        created_at=collection.created_at,
        updated_at=collection.updated_at,
        documents=documents,
    )

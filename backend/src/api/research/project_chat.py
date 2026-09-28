"""
Project-Chat Integration API endpoints.

Enables linking research projects to chat threads for:
- Starting chat from project with document context
- Linking/unlinking existing threads
- Viewing project-related conversations
- Saving thread content to project notes
"""

from typing import List, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from structlog import get_logger

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models import (
    ChatMessage,
    Collection,
    CollectionDocument,
    Conversation,
    MessageRole,
    ProjectThread,
    ProjectThreadLinkType,
    Thread,
    User,
    Workspace,
)
from src.schemas.chat import ThreadCreate
from src.services.research_engine.project_access import (
    ResearchAction,
    accessible_research_workspace_ids,
    resolve_project,
)
from src.shared.research_schemas import (
    LinkThreadRequest,
    NoteCreate,
    NoteResponse,
    ProjectThreadListResponse,
    ProjectThreadResponse,
    SaveThreadToNoteRequest,
    StartChatFromProjectRequest,
    StartChatFromProjectResponse,
)

logger = get_logger()
router = APIRouter(prefix="/api/v1/projects", tags=["project-chat-integration"])


# =========================================================================
# Helper Functions
# =========================================================================


async def _get_project_with_auth(
    project_id: UUID,
    current_user: User,
    db: AsyncSession,
    action: ResearchAction = ResearchAction.VIEW,
) -> Collection:
    """Apply the canonical project authorization and lifecycle boundary."""
    return (await resolve_project(db, project_id, current_user.id, action)).collection


async def _get_thread_with_auth(
    thread_id: UUID,
    current_user: User,
    db: AsyncSession,
    for_update: bool = False,
) -> Thread:
    """Get thread with authorization check.

    Args:
        thread_id: Thread ID
        current_user: Authenticated user
        db: Database session
        for_update: Take a row lock (SELECT ... FOR UPDATE OF threads) on the
            thread row. Required for read-modify-write flows that mutate
            source_project_id / rag_document_scope, so concurrent writers
            serialize instead of racing on last-commit-wins. No-op on SQLite.
            ``of=Thread`` locks only the threads row, not the joined
            conversation/workspace rows.

    Raises:
        HTTPException: If not found or not authorized
    """
    workspace_ids = await accessible_research_workspace_ids(db, current_user.id)
    query = (
        select(Thread)
        .options(selectinload(Thread.conversation))
        .join(Conversation, Thread.conversation_id == Conversation.id)
        .join(Workspace, Conversation.workspace_id == Workspace.id)
        .where(
            and_(
                Thread.id == thread_id,
                Workspace.id.in_(workspace_ids),
                Workspace.is_deleted.is_(False),
                Conversation.is_deleted.is_(False),
                Thread.is_deleted == False,
            )
        )
    )
    if for_update:
        query = query.with_for_update(of=Thread)
    result = await db.execute(query)
    thread = result.scalar_one_or_none()

    if not thread:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thread not found or access denied",
        )

    return cast(Thread, thread)


async def _get_conversation_with_auth(
    conversation_id: UUID,
    current_user: User,
    db: AsyncSession,
) -> Conversation:
    """Return a live conversation from a research-accessible workspace."""
    workspace_ids = await accessible_research_workspace_ids(db, current_user.id)
    conversation = (
        await db.execute(
            select(Conversation)
            .join(Workspace, Conversation.workspace_id == Workspace.id)
            .where(
                Conversation.id == conversation_id,
                Conversation.is_deleted.is_(False),
                Workspace.id.in_(workspace_ids),
                Workspace.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found or access denied",
        )
    return cast(Conversation, conversation)


async def _get_project_document_scope(
    project_id: UUID,
    db: AsyncSession,
) -> List[str]:
    """Return non-deleted document IDs linked to a project."""
    from src.services.research.project_thread_service import get_project_document_scope

    return await get_project_document_scope(project_id, db)


# =========================================================================
# Start Chat from Project
# =========================================================================


@router.post("/{project_id}/chat/start", response_model=StartChatFromProjectResponse)
async def start_chat_from_project(
    project_id: UUID,
    request: StartChatFromProjectRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StartChatFromProjectResponse:
    """
    Start a new chat thread from a project with document context.

    Creates a new thread (or uses existing conversation) and automatically
    links it to the project. The thread's RAG context is scoped to the
    project's documents.

    Args:
        project_id: Project ID to start chat from
        request: Chat start request with initial message
        current_user: Authenticated user
        db: Database session

    Returns:
        Thread details with document scope
    """
    try:
        # Verify project access
        project = await _get_project_with_auth(
            project_id, current_user, db, ResearchAction.EDIT
        )

        # Get project's documents for RAG scope
        document_ids = await _get_project_document_scope(project_id, db)

        if not document_ids:
            logger.warning(
                "starting_chat_from_project_with_no_documents",
                project_id=str(project_id),
            )

        # Get or create conversation
        conversation_id = request.conversation_id
        if not conversation_id:
            # Create new conversation in project's workspace
            conversation = Conversation(
                workspace_id=project.workspace_id,
                title=f"Chat: {project.name}",
                created_by_id=current_user.id,
            )
            db.add(conversation)
            await db.flush()  # Get conversation ID
            conversation_id = conversation.id
        else:
            # Verify conversation exists and user has access
            conversation = await _get_conversation_with_auth(
                conversation_id, current_user, db
            )
            if conversation.workspace_id != project.workspace_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Conversation and project must be in the same workspace",
                )

        # Create thread
        thread = Thread(
            conversation_id=conversation_id,
            title=request.thread_title or f"Discussion: {project.name}",
            created_by_id=current_user.id,
        )
        db.add(thread)
        await db.flush()  # Get thread ID

        # Create initial message if provided
        if request.initial_message:
            message = ChatMessage(
                thread_id=thread.id,
                user_id=current_user.id,
                role=MessageRole.USER,
                content=request.initial_message,
            )
            db.add(message)
            thread.message_count = 1

        # Atomic link via the single source of truth (sets source_project_id,
        # rag_document_scope, and the join row in one unit of work).
        from src.services.research.project_thread_service import (
            attach_thread_to_project,
        )

        project_thread = await attach_thread_to_project(
            db,
            thread,
            project_id,
            link_type=ProjectThreadLinkType.AUTO.value,
            linked_by_id=current_user.id,
            context_note="Auto-linked when starting chat from project",
        )

        await db.commit()
        await db.refresh(thread)
        await db.refresh(project_thread)

        logger.info(
            "chat_started_from_project",
            project_id=str(project_id),
            thread_id=str(thread.id),
            document_count=len(document_ids),
            user_id=str(current_user.id),
        )

        return StartChatFromProjectResponse(
            thread_id=thread.id,
            conversation_id=conversation_id,
            project_thread_id=project_thread.id,
            # Pydantic coerces the stored strings to UUIDs (field is List[UUID])
            document_scope=(thread.rag_document_scope or {}).get("document_ids", []),
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(
            "start_chat_from_project_failed", error=str(e), project_id=str(project_id)
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start chat from project",
        )


# =========================================================================
# Link Existing Thread
# =========================================================================


@router.post("/{project_id}/chat/link", response_model=ProjectThreadResponse)
async def link_thread_to_project(
    project_id: UUID,
    request: LinkThreadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectThreadResponse:
    """
    Link an existing thread to a project.

    Args:
        project_id: Project ID to link to
        request: Thread link request
        current_user: Authenticated user
        db: Database session

    Returns:
        Created project-thread link
    """
    try:
        # Verify project access
        project = await _get_project_with_auth(
            project_id, current_user, db, ResearchAction.EDIT
        )

        # Verify thread access
        thread = await _get_thread_with_auth(request.thread_id, current_user, db)

        # Verify thread and project are in same workspace
        if thread.conversation.workspace_id != project.workspace_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Thread and project must be in the same workspace",
            )

        # Idempotent attach: writes source_project_id + the join row atomically
        # and reuses an existing link, so re-linking repairs a desync instead of
        # erroring (the old 409 left half-linked threads unfixable).
        from src.services.research.project_thread_service import (
            attach_thread_to_project,
        )

        project_thread = await attach_thread_to_project(
            db,
            thread,
            project_id,
            link_type=ProjectThreadLinkType.MANUAL.value,
            linked_by_id=current_user.id,
            context_note=request.context_note,
        )
        await db.commit()
        await db.refresh(project_thread)

        logger.info(
            "thread_linked_to_project",
            project_id=str(project_id),
            thread_id=str(request.thread_id),
            user_id=str(current_user.id),
        )

        return ProjectThreadResponse(
            id=project_thread.id,
            project_id=project_thread.project_id,
            thread_id=project_thread.thread_id,
            thread_title=thread.title or thread.generate_title(),
            conversation_id=thread.conversation_id,
            link_type=project_thread.link_type,
            linked_at=project_thread.linked_at,
            linked_by_id=project_thread.linked_by_id,
            context_note=project_thread.context_note,
            message_count=thread.message_count,
            last_message_at=thread.last_message_at,
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("link_thread_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to link thread",
        )


# =========================================================================
# List Project Threads
# =========================================================================


@router.get("/{project_id}/chat/threads", response_model=ProjectThreadListResponse)
async def list_project_threads(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectThreadListResponse:
    """
    List all threads linked to a project.

    Args:
        project_id: Project ID
        current_user: Authenticated user
        db: Database session

    Returns:
        List of linked threads with metadata
    """
    try:
        # Verify project access
        project = await _get_project_with_auth(project_id, current_user, db)

        # Get linked threads with thread details (exclude soft-deleted links).
        # The thread-level soft-delete check also lives in SQL (audit B8):
        # joining Thread here bounds the load to live links of live threads
        # and keeps `total` consistent with the returned rows, instead of
        # loading every link ever and dropping dead threads in Python.
        query = (
            select(ProjectThread)
            .join(Thread, ProjectThread.thread_id == Thread.id)
            .join(Conversation, Thread.conversation_id == Conversation.id)
            .join(Workspace, Conversation.workspace_id == Workspace.id)
            .options(selectinload(ProjectThread.thread))
            .where(
                and_(
                    ProjectThread.project_id == project_id,
                    ProjectThread.is_deleted == False,
                    Thread.is_deleted == False,  # noqa: E712
                    Conversation.is_deleted.is_(False),
                    Conversation.workspace_id == project.workspace_id,
                    Workspace.is_deleted.is_(False),
                )
            )
            .order_by(ProjectThread.linked_at.desc())
        )
        result = await db.execute(query)
        project_threads = result.scalars().all()

        threads_response = []
        for pt in project_threads:
            # Defensive: the inner join guarantees pt.thread exists, but a
            # stale session state should degrade to a skipped row, not a 500.
            if pt.thread:
                threads_response.append(
                    ProjectThreadResponse(
                        id=pt.id,
                        project_id=pt.project_id,
                        thread_id=pt.thread_id,
                        thread_title=pt.thread.title or pt.thread.generate_title(),
                        conversation_id=pt.thread.conversation_id,
                        link_type=pt.link_type,
                        linked_at=pt.linked_at,
                        linked_by_id=pt.linked_by_id,
                        context_note=pt.context_note,
                        message_count=pt.thread.message_count,
                        last_message_at=pt.thread.last_message_at,
                    )
                )

        return ProjectThreadListResponse(
            threads=threads_response,
            total=len(threads_response),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_project_threads_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list threads",
        )


# =========================================================================
# Unlink Thread
# =========================================================================


@router.delete(
    "/{project_id}/chat/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def unlink_thread_from_project(
    project_id: UUID,
    thread_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Unlink a thread from a project.

    Args:
        project_id: Project ID
        thread_id: Thread ID to unlink
        current_user: Authenticated user
        db: Database session
    """
    try:
        # Verify project access
        await _get_project_with_auth(project_id, current_user, db, ResearchAction.EDIT)

        # Find and delete the link. Exclude soft-deleted links: a second
        # DELETE on an already-unlinked thread must 404, not re-run the
        # scope reassignment below against a dead row (audit B7).
        query = select(ProjectThread).where(
            and_(
                ProjectThread.project_id == project_id,
                ProjectThread.thread_id == thread_id,
                ProjectThread.is_deleted == False,  # noqa: E712
            )
        )
        result = await db.execute(query)
        project_thread = result.scalar_one_or_none()

        if not project_thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Thread link not found",
            )

        # Lock the thread row BEFORE computing remaining_link: the read-
        # modify-write of source_project_id / rag_document_scope below is a
        # race window (audit B5) — two concurrent unlinks both read the same
        # baseline and last commit wins, leaving stale document_ids feeding
        # RAG scope. FOR UPDATE OF threads serializes per-thread unlinks.
        thread = await _get_thread_with_auth(
            thread_id, current_user, db, for_update=True
        )
        project_thread.soft_delete()

        # Reassign the scalar source whenever the unlinked project was the
        # scalar source, OR whenever the scalar is NULL but other links remain
        # (fixes desync where source_project_id was already cleared).
        scalar = getattr(thread, "source_project_id", None)
        # Only reassign the scalar source to another live project in the
        # authorized thread workspace. This excludes stale cross-workspace
        # legacy links while allowing active workspace members to unlink.
        remaining_query = (
            select(ProjectThread)
            .join(Collection, ProjectThread.project_id == Collection.id)
            .join(Workspace, Collection.workspace_id == Workspace.id)
            .where(
                and_(
                    ProjectThread.thread_id == thread_id,
                    ProjectThread.project_id != project_id,
                    ProjectThread.is_deleted == False,
                    Collection.workspace_id == thread.conversation.workspace_id,
                    Workspace.is_deleted.is_(False),
                    # The link row can be live while its project is soft
                    # deleted; without this, unlinking repoints
                    # thread.source_project_id at a dead project and every
                    # later agent turn on the thread fails to resolve it.
                    Collection.is_deleted.is_(False),
                )
            )
            .order_by(ProjectThread.linked_at.desc(), ProjectThread.id.desc())
        )
        remaining_result = await db.execute(remaining_query)
        remaining_link = remaining_result.scalars().first()

        if scalar == project_id or (scalar is None and remaining_link):
            # MERGE rather than overwrite rag_document_scope. The column is
            # overloaded to also carry the agent marker ({"source": "agent"}),
            # which list_agent_threads (execute.py) filters on via
            # rag_document_scope.contains(AGENT_THREAD_MARKER). Replacing the
            # whole dict — or nulling it — drops the marker, so an agent thread
            # that was bound to a project silently vanishes from
            # GET /api/v1/agent/threads after it is unlinked.
            if remaining_link:
                document_ids = await _get_project_document_scope(
                    remaining_link.project_id, db
                )
                thread.source_project_id = remaining_link.project_id
                thread.rag_document_scope = {
                    **(thread.rag_document_scope or {}),
                    "document_ids": document_ids,
                }
            else:
                thread.source_project_id = None
                # Drop only the project scope; keep any sibling keys (agent
                # marker) so the thread stays visible in the agent list.
                scope = dict(thread.rag_document_scope or {})
                scope.pop("document_ids", None)
                thread.rag_document_scope = scope or None

        await db.commit()

        logger.info(
            "thread_unlinked_from_project",
            project_id=str(project_id),
            thread_id=str(thread_id),
            user_id=str(current_user.id),
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("unlink_thread_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to unlink thread",
        )


# =========================================================================
# Save Thread to Note
# =========================================================================


@router.post("/{project_id}/chat/save-to-note", response_model=NoteResponse)
async def save_thread_to_note(
    project_id: UUID,
    request: SaveThreadToNoteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NoteResponse:
    """
    Save thread content to a project note.

    Args:
        project_id: Project ID
        request: Save to note request
        current_user: Authenticated user
        db: Database session

    Returns:
        Created note
    """
    try:
        # Verify project access
        await _get_project_with_auth(project_id, current_user, db, ResearchAction.EDIT)

        # Verify thread access and get messages
        thread = await _get_thread_with_auth(request.thread_id, current_user, db)

        # Get messages with eager-loaded citations to avoid lazy-load in async context
        message_query = (
            select(ChatMessage)
            .options(selectinload(ChatMessage.citations))  # Eager load citations
            .where(
                and_(
                    ChatMessage.thread_id == request.thread_id,
                    ChatMessage.is_deleted == False,
                    # Edit-and-resend: a note saved from a thread must capture
                    # what the user actually asked, not the turn they replaced.
                    ChatMessage.superseded_by_message_id.is_(None),
                )
            )
            .order_by(ChatMessage.created_at.asc())
        )
        message_result = await db.execute(message_query)
        messages = message_result.scalars().all()

        # Build note content from messages
        # Use thread title directly (avoid generate_title() which causes lazy-load issues)
        thread_title = thread.title or f"Thread {str(thread.id)[:8]}"
        content_parts = [f"# {thread_title}\n"]

        # Format date safely
        date_str = (
            thread.last_message_at.strftime("%Y-%m-%d")
            if thread.last_message_at
            else "Unknown date"
        )
        content_parts.append(f"*Saved from chat thread on {date_str}*\n\n")

        for msg in messages:
            role_label = msg.role.value.title()
            content_parts.append(f"## {role_label}\n\n{msg.content}\n\n")

            # Add citations if requested
            if request.include_citations and msg.citations:
                content_parts.append("**Citations:**\n")
                for citation in msg.citations:
                    if citation.document_title:
                        content_parts.append(f"- {citation.document_title}\n")
                content_parts.append("\n")

        note_content = "".join(content_parts)

        # Create note using existing notes API
        from src.models import ProjectNote

        note = ProjectNote(
            project_id=project_id,
            user_id=current_user.id,
            title=request.note_title,
            content=note_content,
            linked_document_ids=[],
            tags=["chat-thread"],
            is_pinned=False,
        )

        db.add(note)
        await db.commit()
        await db.refresh(note)

        logger.info(
            "thread_saved_to_note",
            project_id=str(project_id),
            thread_id=str(request.thread_id),
            note_id=str(note.id),
            user_id=str(current_user.id),
        )

        # Import here to avoid circular dependency
        from src.api.research.projects import _to_note_response

        return _to_note_response(note)

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("save_thread_to_note_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save thread to note",
        )

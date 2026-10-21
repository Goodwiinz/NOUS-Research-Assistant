"""Read the alphaXiv history shape from canonical, explicitly bound PDF chats.

Supplemental reasoning rows are deterministic views, never a second transcript.
Native branch selection remains the only writer for selection metadata.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.chat_message import ChatMessage, MessageRole
from src.models.conversation import Conversation
from src.models.document import Document, DocumentType
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace
from src.schemas.alphaxiv_history import (
    AlphaXivChatSummary,
    AlphaXivContextWindowUsage,
    AlphaXivHistoryMessage,
    AlphaXivSelectedMessage,
    AlphaXivSelection,
)
from src.services.compat.alphaxiv import CompatibilityError, EventProjection
from src.services.compat.alphaxiv_files import (
    authorize_files,
    document_content_type,
    file_url,
)
from src.services.compat.alphaxiv_stop import authorize_pdf_chat
from src.services.threads import message_branches, workspace_access

logger = logging.getLogger(__name__)
_MAX_RECENT_CHATS = 1000


def _utc(value: datetime) -> datetime:
    # SQLite's timestamp adapter drops timezone information; native historical
    # timestamps are UTC even when the database returns a naive datetime.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _reasoning_id(message_id: UUID) -> UUID:
    return uuid5(message_id, "alphaxiv:output_reasoning:0")


async def _fresh_actor(db: AsyncSession, actor_id: UUID) -> User:
    actor: User | None = (
        await db.execute(
            select(User)
            .join(Organization, User.organization_id == Organization.id)
            .where(
                User.id == actor_id,
                User.is_active.is_(True),
                User.is_deleted.is_(False),
                Organization.is_active.is_(True),
                Organization.is_deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if actor is None:
        raise CompatibilityError(404, "Document not found")
    return actor


async def _paper(db: AsyncSession, actor: User, paper_id: UUID) -> Document:
    paper = await workspace_access.get_accessible_document_or_none(
        db, paper_id, actor.id, actor.organization_id
    )
    if paper is None:
        raise CompatibilityError(404, "Document not found")
    await db.refresh(paper)
    if (
        paper.is_deleted
        or paper.organization_id != actor.organization_id
        or paper.document_type != DocumentType.PDF
    ):
        raise CompatibilityError(404, "Document not found")
    return paper


async def _bound_chat(
    db: AsyncSession, *, actor_id: UUID, chat_id: UUID, require_edit: bool = False
) -> tuple[Thread, Document]:
    actor = await authorize_pdf_chat(
        db, actor_id=actor_id, chat_id=chat_id, require_edit=require_edit
    )
    thread = await workspace_access.get_thread(
        db, chat_id, actor_id, include_messages=False
    )
    if (
        thread is None
        or not thread.message_branching
        or thread.branch_document_id is None
    ):
        raise CompatibilityError(404, "PDF chat not found")
    paper = await _paper(db, actor, thread.branch_document_id)
    return thread, paper


async def list_paper_chats(
    db: AsyncSession, *, actor_id: UUID, paper_id: UUID
) -> list[AlphaXivChatSummary]:
    """List explicitly bound chats in joined/owned workspaces, with no inference."""
    try:
        actor = await _fresh_actor(db, actor_id)
        await _paper(db, actor, paper_id)
        # Aggregate only this paper's bound threads. Grouping every thread in
        # chat_messages scaled with the whole table, not with the result.
        newest = (
            select(
                ChatMessage.thread_id,
                func.max(ChatMessage.created_at).label("newest_message"),
            )
            .join(Thread, Thread.id == ChatMessage.thread_id)
            .where(
                Thread.message_branching.is_(True),
                Thread.branch_document_id == paper_id,
                Thread.is_deleted.is_(False),
                ChatMessage.is_deleted.is_(False),
                ChatMessage.superseded_by_message_id.is_(None),
                ChatMessage.role.in_((MessageRole.USER, MessageRole.ASSISTANT)),
            )
            .group_by(ChatMessage.thread_id)
            .subquery()
        )
        rows = (
            await db.execute(
                select(Thread.id, Thread.title, newest.c.newest_message)
                .join(Conversation, Thread.conversation_id == Conversation.id)
                .join(Workspace, Conversation.workspace_id == Workspace.id)
                .join(newest, newest.c.thread_id == Thread.id)
                .where(
                    Thread.message_branching.is_(True),
                    Thread.branch_document_id == paper_id,
                    Thread.is_deleted.is_(False),
                    Conversation.is_deleted.is_(False),
                    workspace_access.member_or_owner_workspace_clause(actor_id),
                )
                .order_by(newest.c.newest_message.desc(), Thread.id.asc())
                .limit(_MAX_RECENT_CHATS + 1)
            )
        ).all()
        if len(rows) > _MAX_RECENT_CHATS:
            raise CompatibilityError(409, "The PDF chat list is too large")
        return [
            AlphaXivChatSummary(
                id=row.id,
                title=row.title,
                newest_message=_utc(row.newest_message),
            )
            for row in rows
        ]
    except SQLAlchemyError as exc:
        logger.exception("Could not read PDF chat list")
        raise CompatibilityError(
            503, "Chat history is temporarily unavailable"
        ) from exc


def _validate_tree(messages: list[ChatMessage]) -> None:
    """Never turn a missing/deleted ancestor into a new root or return a cycle."""
    by_id = {message.id: message for message in messages}
    visited: set[UUID] = set()
    for message in messages:
        if message.role not in (MessageRole.USER, MessageRole.ASSISTANT):
            raise CompatibilityError(409, "This history contains unsupported messages")
        if message.selected_at is None:
            raise CompatibilityError(409, "This history has incomplete branch metadata")
        path: set[UUID] = set()
        current = message
        while current.id not in visited:
            if current.id in path:
                raise CompatibilityError(
                    409, "This history has invalid branch metadata"
                )
            path.add(current.id)
            if current.parent_message_id is None:
                break
            parent = by_id.get(current.parent_message_id)
            if parent is None:
                raise CompatibilityError(409, "A history ancestor is unavailable")
            current = parent
        visited.update(path)


def _answer_text(message: ChatMessage, paper: Document) -> str:
    projection = EventProjection(str(paper.id))
    projection.project(
        "rag_context",
        {
            "contexts": [
                {
                    "source_position": citation.source_position,
                    "document_id": str(paper.id),
                    "page_number": citation.page_number,
                    "title": citation.document_title or paper.title,
                }
                for citation in message.citations
                if not citation.is_deleted and citation.document_id == paper.id
            ]
        },
    )
    events = projection.project("token", {"content": message.content})
    events += projection.project("done", {})
    return "".join(
        event["delta"] for event in events if event["type"] == "delta_output_text"
    )


def _project_message(
    message: ChatMessage, paper: Document, files: list[Document] | None = None
) -> list[AlphaXivHistoryMessage]:
    selected_at = _utc(message.selected_at)
    parent_id = message.parent_message_id
    blocks: list[AlphaXivHistoryMessage] = []
    for document in files or []:
        identifier = uuid5(message.id, f"alphaxiv:input_file:{document.id}")
        blocks.append(
            AlphaXivHistoryMessage(
                id=identifier,
                parent_message_id=parent_id,
                selected_at=selected_at,
                type="input_file",
                content=file_url(document.id),
                kind=document_content_type(document),
            )
        )
        parent_id = identifier
    if message.role == MessageRole.ASSISTANT and message.reasoning_summary:
        reasoning_id = _reasoning_id(message.id)
        blocks.append(
            AlphaXivHistoryMessage(
                id=reasoning_id,
                parent_message_id=parent_id,
                selected_at=selected_at,
                type="output_reasoning",
                content=message.reasoning_summary,
            )
        )
        parent_id = reasoning_id
    blocks.append(
        AlphaXivHistoryMessage(
            id=message.id,
            parent_message_id=parent_id,
            selected_at=selected_at,
            type="input_text" if message.role == MessageRole.USER else "output_text",
            content=(
                message.content
                if message.role == MessageRole.USER
                else _answer_text(message, paper)
            ),
        )
    )
    return blocks


async def get_chat_messages(
    db: AsyncSession, *, actor_id: UUID, chat_id: UUID
) -> list[AlphaXivHistoryMessage]:
    """Return the whole visible branch tree, or fail without a partial transcript."""
    try:
        _, paper = await _bound_chat(db, actor_id=actor_id, chat_id=chat_id)
        messages = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.thread_id == chat_id,
                        ChatMessage.is_deleted.is_(False),
                        ChatMessage.superseded_by_message_id.is_(None),
                    )
                    .options(
                        selectinload(ChatMessage.citations),
                        selectinload(ChatMessage.attachments),
                    )
                    # Native selection and the hosted reader both choose the
                    # first chronological row on equal selectedAt timestamps.
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                    .limit(message_branches.MAX_BRANCH_MESSAGES + 1)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        if len(messages) > message_branches.MAX_BRANCH_MESSAGES:
            raise CompatibilityError(409, "The PDF chat history is too large")
        _validate_tree(messages)
        file_ids = sorted(
            {
                attachment.document_id
                for message in messages
                for attachment in message.attachments
                if not attachment.is_deleted and attachment.document_id != paper.id
            },
            key=str,
        )
        documents = (
            await authorize_files(
                db, user=await _fresh_actor(db, actor_id), document_ids=file_ids
            )
            if file_ids
            else []
        )
        by_id = {document.id: document for document in documents}
        return [
            block
            for message in messages
            for block in _project_message(
                message,
                paper,
                [
                    by_id[attachment.document_id]
                    for attachment in sorted(
                        message.attachments, key=lambda item: str(item.id)
                    )
                    if not attachment.is_deleted and attachment.document_id in by_id
                ],
            )
        ]
    except SQLAlchemyError as exc:
        logger.exception("Could not read PDF chat messages")
        raise CompatibilityError(
            503, "Chat history is temporarily unavailable"
        ) from exc


async def select_chat_message(
    db: AsyncSession, *, actor_id: UUID, chat_id: UUID, message_id: UUID
) -> AlphaXivSelection:
    """Select an observed input_text branch via the native transaction owner."""
    try:
        await _bound_chat(db, actor_id=actor_id, chat_id=chat_id, require_edit=True)
        target = (
            await db.execute(
                select(ChatMessage.id).where(
                    ChatMessage.id == message_id,
                    ChatMessage.thread_id == chat_id,
                    ChatMessage.role == MessageRole.USER,
                    ChatMessage.is_deleted.is_(False),
                    ChatMessage.superseded_by_message_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise CompatibilityError(404, "Input message not found")
        selected = await message_branches.select_branch(
            db, actor_id=actor_id, thread_id=chat_id, message_id=message_id
        )
        return AlphaXivSelection(
            selections=[
                AlphaXivSelectedMessage(id=item_id, selected_at=_utc(selected_at))
                for item_id, selected_at in selected
            ]
        )
    except message_branches.BranchAccessError as exc:
        raise CompatibilityError(exc.status_code, str(exc)) from exc
    except SQLAlchemyError as exc:
        logger.exception("Could not select PDF chat branch")
        raise CompatibilityError(
            503, "Chat selection is temporarily unavailable"
        ) from exc


async def get_context_window_usage(
    db: AsyncSession, *, actor_id: UUID, chat_id: UUID
) -> AlphaXivContextWindowUsage | None:
    """The observed consumer accepts null; aggregate token totals are not usage."""
    try:
        await _bound_chat(db, actor_id=actor_id, chat_id=chat_id)
    except SQLAlchemyError as exc:
        logger.exception("Could not authorize PDF context usage")
        raise CompatibilityError(
            503, "Chat history is temporarily unavailable"
        ) from exc
    return None

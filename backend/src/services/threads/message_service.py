"""
Message read/update/delete persistence (Task 4.3 consolidation).

Message *creation* is out of scope here — ``ChatService.create_message`` was
already consolidated onto the one canonical implementation in #1051 (audit
finding C4: the flat ``POST /api/v2/messages`` is the only create-message
route any client calls; the two nested variants delegate to it and are
deprecated) and this module doesn't touch it.

This module consolidates the *other* three message concerns, which were each
duplicated:

- **List**: three prior implementations existed — the nested router
  (asc/offset only), the standalone router (its own hand-rolled desc-cursor
  with a +1-row sentinel, reimplemented rather than calling the service), and
  ``ChatService.list_messages`` (asc/desc + ``before_id`` + ``since``, but no
  ``last_message_preview``-style column projection). This is a strict
  capability union, not a conflict: the consolidated ``list_messages`` always
  supports every mode any caller used, so nothing here is flag-gated.
- **Get**: the standalone router's ``get_message_standalone`` re-fetched with
  *no* eager load of citations/attachments and relied on lazy loading, which
  raises ``MissingGreenlet`` under an async session for any message that
  actually has citations or attachments. Routing it through
  ``workspace_access.get_message`` (which already eager-loads them, needed by
  the live ``threads.py`` PATCH/GET-message callers) fixes that latent crash
  risk as a side effect of consolidating onto the one already-working
  implementation — not a separate behavior change.
- **Delete**: a genuine permission conflict. ``ChatService.delete_message``
  allows the message's *author* (or a workspace admin) to delete it; the
  standalone router requires *edit* rights and never checks authorship. See
  ``delete_message``'s ``require_author_or_admin`` flag.
"""

from datetime import datetime
from typing import List, Literal, Optional, Tuple
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from src.models.chat_message import ChatMessage
from src.models.citation import Citation
from src.models.document import Document
from src.models.message_attachment import MessageAttachment
from src.schemas.chat import ChatMessageUpdate
from src.services.threads import workspace_access


async def get_message(
    db: AsyncSession,
    message_id: UUID,
    user_id: UUID,
    *,
    thread_id: Optional[UUID] = None,
) -> Optional[ChatMessage]:
    """Fetch a message the caller can access, or ``None``."""
    return await workspace_access.get_message(
        db, message_id, user_id, thread_id=thread_id
    )


async def list_messages(
    db: AsyncSession,
    thread_id: UUID,
    user_id: UUID,
    *,
    workspace_id: Optional[UUID] = None,
    conversation_id: Optional[UUID] = None,
    limit: int = 100,
    offset: int = 0,
    before_id: Optional[UUID] = None,
    since: Optional[datetime] = None,
    order: Literal["asc", "desc"] = "asc",
) -> Optional[Tuple[List[ChatMessage], int, bool]]:
    """List messages in a thread.

    Returns ``None`` if the thread isn't found/accessible (distinct from a
    legitimately empty thread); otherwise ``(messages, total, has_more)``.
    ``workspace_id``/``conversation_id`` scope the access check for
    nested-route callers (the chain-validation the nested router previously
    did via ``_get_thread_or_404``); standalone callers pass neither.

    ``before_id`` filters to messages strictly earlier than the cursor by
    the stable ``(created_at, id)`` tuple — thread-scoped, so a foreign
    message id can't be used as a cross-thread pagination oracle. ``since``
    filters to ``created_at > since`` (delta-fetch). ``order`` controls both
    the sort direction and how ``has_more`` is computed: ``"asc"`` is
    offset/total-based (backward-compatible default); ``"desc"`` fetches
    ``limit + 1`` rows and trims, so a cursor page never needs the total.
    """
    thread = await workspace_access.get_thread(
        db,
        thread_id,
        user_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        include_messages=False,
    )
    if not thread:
        return None

    base_conditions = [
        ChatMessage.thread_id == thread_id,
        ChatMessage.is_deleted == False,  # noqa: E712
        # Edit-and-resend tombstones. In base_conditions so the page query and
        # the count stay in lockstep.
        ChatMessage.superseded_by_message_id.is_(None),
    ]

    if before_id is not None:
        before_stmt = select(ChatMessage).where(
            ChatMessage.id == before_id, ChatMessage.thread_id == thread_id
        )
        before_msg = (await db.execute(before_stmt)).scalars().first()
        if before_msg:
            base_conditions.append(
                or_(
                    ChatMessage.created_at < before_msg.created_at,
                    and_(
                        ChatMessage.created_at == before_msg.created_at,
                        ChatMessage.id < before_msg.id,
                    ),
                )
            )

    if since is not None:
        base_conditions.append(ChatMessage.created_at > since)

    total = (
        await db.execute(select(func.count(ChatMessage.id)).where(*base_conditions))
    ).scalar() or 0

    # load_only: message responses render only title/type/mime of a cited/
    # attached Document — never content_text/content_summary/search_vector
    # (the heavy extracted body). Keeps that off the wire on every paged
    # fetch; a straight perf win for every caller, ported from the router's
    # standalone/nested list endpoints (ChatService's original list_messages
    # loaded full Document rows).
    options = [
        selectinload(ChatMessage.citations)
        .selectinload(Citation.document)
        .load_only(Document.title, Document.document_type, Document.mime_type),
        selectinload(ChatMessage.attachments)
        .selectinload(MessageAttachment.document)
        .load_only(Document.title, Document.document_type, Document.mime_type),
    ]

    if order == "desc":
        stmt = (
            select(ChatMessage)
            .options(*options)
            .where(*base_conditions)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(limit + 1)
        )
        rows = list((await db.execute(stmt)).scalars().all())
        has_more = len(rows) > limit
        messages = rows[:limit]
    else:
        stmt = (
            select(ChatMessage)
            .options(*options)
            .where(*base_conditions)
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            .offset(offset)
            .limit(limit)
        )
        messages = list((await db.execute(stmt)).scalars().all())
        has_more = (offset + len(messages)) < total

    for message in messages:
        # The page query loads no ``thread`` relationship. Attach the one the
        # access funnel above already resolved (same identity-map object: no
        # extra query, no dirty state) so presenters can read the viewer's
        # workspace trust from the row — a lazy load there would raise
        # MissingGreenlet under AsyncSession and fail closed to the public
        # tool-trace projection even for the thread's owner.
        set_committed_value(message, "thread", thread)

    return messages, total, has_more


async def update_message_feedback(
    db: AsyncSession,
    message_id: UUID,
    data: ChatMessageUpdate,
    user_id: UUID,
    *,
    thread_id: Optional[UUID] = None,
    conversation_id: Optional[UUID] = None,
    workspace_id: Optional[UUID] = None,
) -> Optional[ChatMessage]:
    """Update message feedback. ``None`` if not found/accessible; raises
    ``PermissionError`` if found but the caller lacks edit rights (read
    access via the thread — member/public viewer — is not enough).

    ``thread_id``/``conversation_id``/``workspace_id``, when given, scope
    the lookup to that parent chain; standalone callers pass only
    ``thread_id`` (or none).
    """
    message = await workspace_access.get_message(
        db,
        message_id,
        user_id,
        thread_id=thread_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
    )
    if not message:
        return None
    if not message.thread.conversation.workspace.can_user_edit(str(user_id)):
        raise PermissionError("Insufficient permissions")

    if data.feedback_rating is not None:
        message.feedback_rating = data.feedback_rating
    if data.feedback_text is not None:
        message.feedback_text = data.feedback_text

    message.updated_at = datetime.utcnow()
    await db.flush()
    # No db.refresh(): every mutated field is a Python-side assignment already
    # reflecting final state, and a bare refresh() would expire the
    # citations/attachments eager-loaded above (untouched by this mutation)
    # — the presenter reads them right after, and expired-then-accessed
    # relationships MissingGreenlet under the async session.
    return message


async def delete_message(
    db: AsyncSession,
    message_id: UUID,
    user_id: UUID,
    *,
    thread_id: Optional[UUID] = None,
    require_author_or_admin: bool = False,
) -> Optional[bool]:
    """Soft-delete a message. ``None`` if not found/accessible; raises
    ``PermissionError`` if found but the caller lacks the required rights.

    ``require_author_or_admin=False`` (router-canonical default): requires
    edit rights, matching every other mutator in the router. ``True`` (old
    ``ChatService`` default, preserved for its own — currently uncalled —
    callers): the message's author may always delete it even as a plain
    editor/viewer; anyone else needs admin rights.
    """
    message = await workspace_access.get_message(
        db, message_id, user_id, thread_id=thread_id
    )
    if not message:
        return None

    if require_author_or_admin:
        is_author = str(message.user_id) == str(user_id)
        if not is_author and not message.thread.conversation.workspace.can_user_admin(
            str(user_id)
        ):
            raise PermissionError("Insufficient permissions")
    else:
        if not message.thread.conversation.workspace.can_user_edit(str(user_id)):
            raise PermissionError("Insufficient permissions")

    message.is_deleted = True
    message.updated_at = datetime.utcnow()
    message.thread.message_count = max((message.thread.message_count or 1) - 1, 0)
    await db.flush()
    return True

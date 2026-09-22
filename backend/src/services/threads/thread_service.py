"""
Thread persistence (Task 4.3 consolidation).

Canonical owner for thread CRUD — the router-inline copy in
``backend/src/api/threads/workspace_routes/threads.py`` (nested +
standalone) and ``ChatService``'s thread methods both implemented this
independently; this is now the one implementation.

PR 3 Task 3.2: these functions no longer own the request commit — they
``flush()`` and the route (or, for ChatService's own callers, the ChatService
method) issues the single ``await db.commit()`` at the end of the mutating
path. The old ``commit: bool`` flag on ``create_thread`` is gone (create is now
unconditionally flush-only, which is exactly what its live ``threads.py`` caller
already relied on — that route does further work, project auto-link + WS
broadcast, then commits once). ``update_thread``'s resolve-summary enqueue moved
to ``enqueue_after_commit`` so it fires on the caller's commit, not at flush.

Divergence flags (Task 4.3 amendment A2):

- ``trigger_resolve_summary`` (update, status -> RESOLVED): pre-4.3,
  ``ChatService.update_thread`` compared ``data.status`` (``schemas.chat.
  ThreadStatus``) directly against ``src.models.ThreadStatus.RESOLVED`` —
  two distinct Enum classes that never compare equal, so the summary task
  never actually fired through that path; the router-inline update
  endpoints never had this trigger at all. Every current caller therefore
  observes "never fires." This consolidation fixes the comparison (values
  now normalized before comparing), so the trigger *could* fire — but per
  the amendment, a genuine behavioral fix like this stays behind a flag
  defaulting to the old (never-fires) behavior. Default ``False`` for every
  caller (``ChatService.update_thread``, the nested and standalone
  router endpoints); no caller currently opts in, so live behavior is
  unchanged pending a separate product decision to enable it.

A capability gap, not a conflict (adding it changes nothing for existing
callers that don't ask for it):

- ``with_preview`` (list): the router-inline list endpoints compute a
  correlated last-message-preview subquery per thread that
  ``ChatService.list_threads`` never supported. Default ``False`` (bare
  thread list, unchanged signature for ``threads.py``'s live
  ``threads, total = await service.list_threads(...)`` call); ``True``
  additionally returns a ``{thread_id: preview}`` dict.
"""

import uuid as uuid_mod
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.chat_message import ChatMessage, MessageRole
from src.models.conversation import Conversation
from src.models.thread import Thread, ThreadStatus
from src.schemas.chat import ThreadCreate, ThreadUpdate
from src.services.threads import workspace_access
from src.tasks.enqueue import enqueue_after_commit

logger = structlog.get_logger(__name__)

# The router's rendered `last_message_preview` field truncates to this many
# characters. Owned here (the query that produces it); re-exported by
# ``workspace_routes/presenters.py`` for backward compatibility — a few
# characterization tests import it from that path.
THREAD_PREVIEW_MAX_CHARS = 240


def last_message_preview_expression() -> Any:
    """Latest non-deleted message excerpt for a thread-list row."""
    return (
        select(func.substr(ChatMessage.content, 1, THREAD_PREVIEW_MAX_CHARS))
        .where(
            ChatMessage.thread_id == Thread.id,
            ChatMessage.is_deleted == False,  # noqa: E712
            # Without this the thread list preview keeps showing the stale
            # content of a turn the user already edited away.
            ChatMessage.superseded_by_message_id.is_(None),
            ChatMessage.role.in_([MessageRole.USER, MessageRole.ASSISTANT]),
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(1)
        .correlate(Thread)
        .scalar_subquery()
        .label("last_message_preview")
    )


async def create_thread(
    db: AsyncSession,
    data: ThreadCreate,
    user_id: UUID,
    *,
    workspace_id: Optional[UUID] = None,
) -> Optional[Thread]:
    """Create a thread (+ optional initial message). ``None`` if the
    conversation isn't found/accessible; raises ``PermissionError`` if found
    but the caller lacks edit rights.

    ``workspace_id``, when given, scopes the parent lookup to that workspace
    (the nested-route chain check); standalone callers pass ``None``.
    """
    conversation = await workspace_access.get_conversation(
        db, data.conversation_id, user_id, workspace_id=workspace_id, load_threads=False
    )
    if not conversation:
        return None
    if not conversation.workspace.can_user_edit(str(user_id)):
        raise PermissionError("Insufficient permissions")

    thread_id = uuid_mod.uuid4()
    thread = Thread(
        id=thread_id,
        conversation_id=data.conversation_id,
        title=data.title,
        status=ThreadStatus.ACTIVE,
        created_by_id=user_id,
        last_message_at=datetime.utcnow(),
        message_count=0,
        token_count=0,
    )
    db.add(thread)

    if data.initial_message:
        initial_msg = ChatMessage.create_user_message(
            thread_id=str(thread_id), user_id=str(user_id), content=data.initial_message
        )
        db.add(initial_msg)
        thread.message_count = 1

    conversation.update_activity()

    # PR 3 Task 3.2: unconditionally flush (the old ``commit`` flag is gone).
    # The caller owns the request commit — the live ``threads.py`` route flushes
    # here, does further work (project auto-link, WS broadcast), then commits
    # once; the workspace_routes create handlers commit right after this call.
    await db.flush()
    await db.refresh(thread)

    logger.info("thread_created", thread_id=str(thread.id))
    return thread


async def list_threads(
    db: AsyncSession,
    conversation_id: UUID,
    user_id: UUID,
    *,
    workspace_id: Optional[UUID] = None,
    status_filter: Optional[ThreadStatus] = None,
    limit: int = 50,
    offset: int = 0,
    with_preview: bool = False,
) -> Optional[Tuple[List[Thread], int, Dict[UUID, Optional[str]]]]:
    """List threads in a conversation.

    ``workspace_id``, when given, scopes the parent lookup to that workspace
    (the nested-route chain check) — a conversation id that does not belong
    to the path's ``workspace_id`` returns ``None`` exactly as it did as a
    two-query chain; standalone callers pass ``None``.

    Returns ``None`` if the conversation isn't found/accessible (distinct
    from a legitimately empty list); otherwise ``(threads, total, previews)``
    — ``previews`` is empty unless ``with_preview=True``, in which case it
    maps thread id -> the latest non-deleted message excerpt (the router's
    rendered field).
    """
    conversation = await workspace_access.get_conversation(
        db, conversation_id, user_id, workspace_id=workspace_id, load_threads=False
    )
    if not conversation:
        return None

    base_conditions = [
        Thread.conversation_id == conversation_id,
        Thread.is_deleted == False,  # noqa: E712
    ]
    if status_filter:
        base_conditions.append(Thread.status == status_filter)

    total = (
        await db.execute(select(func.count(Thread.id)).where(*base_conditions))
    ).scalar() or 0

    previews: Dict[UUID, Optional[str]] = {}
    if with_preview:
        preview_expr = last_message_preview_expression()
        stmt = (
            select(Thread, preview_expr)
            .where(*base_conditions)
            .order_by(desc(Thread.last_message_at))
            .offset(offset)
            .limit(limit)
        )
        rows = (await db.execute(stmt)).all()
        threads = [row[0] for row in rows]
        previews = {row[0].id: row[1] for row in rows}
    else:
        stmt = (
            select(Thread)
            .where(*base_conditions)
            .order_by(desc(Thread.last_message_at))
            .offset(offset)
            .limit(limit)
        )
        threads = list((await db.execute(stmt)).scalars().all())

    return threads, total, previews


async def list_workspace_threads(
    db: AsyncSession,
    workspace_id: UUID,
    user_id: UUID,
    *,
    status_filter: Optional[ThreadStatus] = None,
    limit: int = 20,
    offset: int = 0,
) -> Optional[Tuple[List[Thread], int, Dict[UUID, Optional[str]]]]:
    """List one globally paginated thread feed for an accessible workspace."""
    workspace = await workspace_access.get_workspace(
        db,
        workspace_id,
        user_id,
        load_conversations=False,
        load_collections=False,
    )
    if workspace is None:
        return None

    conditions = [
        Conversation.workspace_id == workspace_id,
        Conversation.is_deleted == False,  # noqa: E712
        Thread.is_deleted == False,  # noqa: E712
    ]
    if status_filter is not None:
        conditions.append(Thread.status == status_filter)

    total = (
        await db.execute(
            select(func.count(Thread.id))
            .join(Conversation, Thread.conversation_id == Conversation.id)
            .where(*conditions)
        )
    ).scalar() or 0

    preview_expr = last_message_preview_expression()
    rows = (
        await db.execute(
            select(Thread, preview_expr)
            .join(Conversation, Thread.conversation_id == Conversation.id)
            .where(*conditions)
            .order_by(Thread.last_message_at.desc(), Thread.id.asc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    threads = [row[0] for row in rows]
    previews = {row[0].id: row[1] for row in rows}
    logger.info(
        "workspace_threads_listed",
        workspace_id=str(workspace_id),
        offset=offset,
        limit=limit,
        result_count=len(threads),
    )
    return threads, total, previews


async def update_thread(
    db: AsyncSession,
    thread_id: UUID,
    data: ThreadUpdate,
    user_id: UUID,
    *,
    conversation_id: Optional[UUID] = None,
    workspace_id: Optional[UUID] = None,
    trigger_resolve_summary: bool = False,
) -> Optional[Thread]:
    """Update a thread. ``None`` if not found/accessible; raises
    ``PermissionError`` if found but the caller lacks edit rights.

    ``conversation_id``/``workspace_id``, when given, scope the lookup to
    that parent chain (the nested-route chain check); standalone callers
    pass neither.

    ``trigger_resolve_summary`` (Task 4.3 amendment A2 divergence flag):
    gates the resolve -> AI-summary enqueue. Every current caller observed
    "never fires" pre-4.3 (see module docstring), so it defaults ``False``
    to preserve that behavior exactly; no caller passes ``True`` yet.
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
    if not thread.conversation.workspace.can_user_edit(str(user_id)):
        raise PermissionError("Insufficient permissions")

    if data.title is not None:
        thread.title = data.title
    if data.summary is not None:
        thread.summary = data.summary

    status_changing_to_resolved = (
        trigger_resolve_summary
        and data.status is not None
        and ThreadStatus(getattr(data.status, "value", data.status))
        == ThreadStatus.RESOLVED
        and thread.status != ThreadStatus.RESOLVED
    )
    if data.status is not None:
        thread.status = ThreadStatus(getattr(data.status, "value", data.status))

    thread.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(thread)

    if status_changing_to_resolved:
        try:
            from src.tasks.summarize_thread_task import summarize_thread_on_resolve_task

            # PR 3 Task 3.2: this leaf now flushes; the request commit is owned
            # by the route / ChatService. Register the enqueue on the session so
            # it fires on THAT commit (exactly once, dropped on rollback) rather
            # than at flush time — the enqueue_after_commit invariant. Gated
            # behind trigger_resolve_summary, which every current caller leaves
            # False, so this path is dead today; the helper keeps it correct for
            # whenever the flag is turned on.
            enqueue_after_commit(db, summarize_thread_on_resolve_task, str(thread_id))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "thread_resolve_summary_enqueue_failed",
                thread_id=str(thread_id),
                error=str(e),
            )

    return thread


async def delete_thread(
    db: AsyncSession,
    thread_id: UUID,
    user_id: UUID,
    *,
    conversation_id: Optional[UUID] = None,
    workspace_id: Optional[UUID] = None,
    stamp_deleted_at: bool = False,
) -> Optional[bool]:
    """Soft-delete a thread. ``None`` if not found/accessible; raises
    ``PermissionError`` if found but the caller lacks edit rights.

    ``conversation_id``/``workspace_id``, when given, scope the lookup to
    that parent chain; standalone callers pass neither.

    ``stamp_deleted_at=True`` (router-canonical) also sets ``deleted_at``;
    ``False`` (old ``ChatService`` default, preserved for its live
    ``threads.py`` caller) leaves it ``NULL``.
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
    if not thread.conversation.workspace.can_user_edit(str(user_id)):
        raise PermissionError("Insufficient permissions")

    thread.is_deleted = True
    if stamp_deleted_at:
        thread.deleted_at = datetime.utcnow()
    thread.updated_at = datetime.utcnow()
    await db.flush()

    logger.info("thread_deleted", thread_id=str(thread_id))
    return True

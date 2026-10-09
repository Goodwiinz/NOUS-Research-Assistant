"""
Canonical scope/access funnel for the workspace resource hierarchy
(workspace -> conversation -> thread -> message; workspace -> collection).

Task 4.3 consolidation. Before this module the same access chain existed in
two divergent places:

- ``backend/src/api/threads/workspace_routes/dependencies.py`` (router-inline,
  raised ``HTTPException(404)``, always eager-loaded the relationships every
  response needs).
- ``ChatService`` (returned ``None`` on failure, minimal eager-loading, plus
  explicit soft-deleted-*parent* cascade guards — a delete never cascades to
  children, so a child row's own ``is_deleted`` flag staying False doesn't
  mean it's still reachable).

Both encode the same net access predicate (soft-delete-aware; public OR
member OR owner) — this module is now the one implementation. Callers that
want the router's raise-404 behavior wrap these in
``workspace_routes/dependencies.py``; ``ChatService`` calls them directly
since its own callers already expect ``Optional[...]``.

Access-model note: access to a workspace (and everything nested under it) is
granted by explicit membership (a ``WorkspaceMember`` row), ownership, or
because the workspace is public — NOT by organization co-location. A
workspace's ``organization_id`` is metadata recorded at creation, not a
filter on who may join or view it (a public workspace is visible cross-org
by design; a member can be invited from any org). Tenant scoping for
*documents* attached under a workspace (collection contents, message
attachments) is a separate, real predicate, enforced here by
``get_accessible_document_or_none`` and, for message attachments, by
``filter_owned_document_ids`` below — which moved here from
``ChatService`` once the agent stream needed the same predicate on its own
session (see ``_persist_user_message``).
"""

import logging
from typing import List, Optional, Sequence
from uuid import UUID

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.chat_message import ChatMessage
from src.models.citation import Citation
from src.models.collection import Collection, CollectionDocument
from src.models.conversation import Conversation
from src.models.document import Document
from src.models.message_attachment import MessageAttachment
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember

logger = logging.getLogger(__name__)


def user_can_access_workspace(workspace: Workspace, user_id: Optional[UUID]) -> bool:
    """Canonical workspace access predicate.

    Not deleted, then public OR member OR owner. Callers that load
    ``workspace`` via a relationship (``conversation.workspace``,
    ``thread.conversation.workspace``, ...) carry no ``is_deleted`` filter
    from the query that fetched them, so this re-checks it explicitly — a
    soft-deleted workspace revokes access to everything nested under it even
    though ``delete_workspace`` never cascades to children rows.
    """
    if user_id is None or workspace.is_deleted:
        return False
    if workspace.is_public:
        return True
    if str(workspace.owner_id) == str(user_id):
        return True
    return workspace.is_member(str(user_id))


def member_or_owner_workspace_clause(user_id: UUID) -> ColumnElement[bool]:
    """SQL twin of ``user_can_access_workspace`` for listings: live, owned or joined.

    Public workspaces the caller never joined are deliberately left out — they
    stay readable by id through the getters below, but a "my threads" listing
    must not enumerate every public workspace. Soft-deleted memberships do not
    count, matching ``Workspace.is_member``.
    """
    joined = select(WorkspaceMember.workspace_id).where(
        WorkspaceMember.user_id == user_id,
        WorkspaceMember.is_deleted == False,  # noqa: E712
    )
    return (Workspace.is_deleted == False) & or_(  # noqa: E712
        Workspace.owner_id == user_id, Workspace.id.in_(joined)
    )


async def get_workspace(
    db: AsyncSession,
    workspace_id: UUID,
    user_id: UUID,
    *,
    load_conversations: bool = True,
    load_collections: bool = True,
    for_update: bool = False,
) -> Optional[Workspace]:
    """Fetch a workspace the caller can access, or ``None``.

    ``load_conversations``/``load_collections`` default ``True`` — the
    router's historical eager-load, needed to render member/conversation/
    collection counts on a workspace response. Pass ``False`` for a lean
    gate-only check: the pattern used internally before creating a child
    resource, where eager-loading the unbounded conversations collection on
    every permission check grows linearly with workspace age.
    """
    # members.user must ride along: both workspace-detail presenters read
    # member.user.email/full_name, and a lazy load on an AsyncSession raises
    # MissingGreenlet instead of querying.
    options = [selectinload(Workspace.members).selectinload(WorkspaceMember.user)]
    if load_conversations:
        options.append(selectinload(Workspace.conversations))
    if load_collections:
        options.append(selectinload(Workspace.collections))

    stmt = (
        select(Workspace)
        .options(*options)
        .where(
            Workspace.id == workspace_id,
            Workspace.is_deleted == False,  # noqa: E712
        )
    )
    if for_update:
        # Membership and lifecycle writers coordinate with research approvals.
        # Refresh the identity map and members after any lock wait.
        stmt = stmt.with_for_update(of=Workspace).execution_options(
            populate_existing=True
        )
    result = await db.execute(stmt)
    workspace: Optional[Workspace] = result.scalars().first()
    if not workspace:
        return None
    if not user_can_access_workspace(workspace, user_id):
        return None
    return workspace


async def get_conversation(
    db: AsyncSession,
    conversation_id: UUID,
    user_id: UUID,
    *,
    workspace_id: Optional[UUID] = None,
    load_threads: bool = True,
) -> Optional[Conversation]:
    """Fetch a conversation the caller can access, or ``None``.

    ``workspace_id``, when given, scopes the lookup to that parent — the
    nested-route chain check: a conversation id that does not belong to the
    path's ``workspace_id`` 404s exactly as it did as two separate queries.
    """
    conditions = [
        Conversation.id == conversation_id,
        Conversation.is_deleted == False,  # noqa: E712
    ]
    if workspace_id is not None:
        conditions.append(Conversation.workspace_id == workspace_id)

    options = [selectinload(Conversation.workspace).selectinload(Workspace.members)]
    if load_threads:
        options.append(selectinload(Conversation.threads))

    stmt = select(Conversation).options(*options).where(*conditions)
    result = await db.execute(stmt)
    conversation: Optional[Conversation] = result.scalars().first()
    if not conversation:
        return None

    # A soft-deleted parent workspace must revoke access to its
    # conversations: delete_workspace flags only its own row and never
    # cascades to child conversations/threads.
    if conversation.workspace.is_deleted:
        return None
    if not user_can_access_workspace(conversation.workspace, user_id):
        return None
    return conversation


async def get_thread(
    db: AsyncSession,
    thread_id: UUID,
    user_id: UUID,
    *,
    conversation_id: Optional[UUID] = None,
    workspace_id: Optional[UUID] = None,
    include_messages: bool = True,
) -> Optional[Thread]:
    """Fetch a thread the caller can access, or ``None``.

    ``conversation_id``/``workspace_id``, when given, scope the lookup to
    that parent chain (nested-route path validation) exactly as the
    two/three-query chain did.
    """
    conditions = [Thread.id == thread_id, Thread.is_deleted == False]  # noqa: E712
    if conversation_id is not None:
        conditions.append(Thread.conversation_id == conversation_id)

    options = [
        selectinload(Thread.conversation)
        .selectinload(Conversation.workspace)
        .selectinload(Workspace.members)
    ]
    if include_messages:
        options.append(selectinload(Thread.messages))

    stmt = select(Thread).options(*options).where(*conditions)
    result = await db.execute(stmt)
    thread: Optional[Thread] = result.scalars().first()
    if not thread:
        return None

    conversation = thread.conversation
    if workspace_id is not None and conversation.workspace_id != workspace_id:
        return None

    # Soft-deleted parent conversation/workspace revoke access to their
    # threads — neither delete cascades to children.
    if conversation.is_deleted or conversation.workspace.is_deleted:
        return None
    if not user_can_access_workspace(conversation.workspace, user_id):
        return None
    return thread


async def get_message(
    db: AsyncSession,
    message_id: UUID,
    user_id: UUID,
    *,
    thread_id: Optional[UUID] = None,
    conversation_id: Optional[UUID] = None,
    workspace_id: Optional[UUID] = None,
) -> Optional[ChatMessage]:
    """Fetch a message the caller can access, or ``None``.

    ``thread_id``/``conversation_id``/``workspace_id``, when given, scope
    the lookup to that parent chain (the nested-route chain check);
    standalone callers pass none of them.

    Eager-loads citations/attachments (+ their documents) unconditionally,
    matching ``ChatService.get_message``'s original options — a live
    caller (``threads.py`` PATCH/GET message) renders those through
    ``_format_message_response`` immediately after. The former standalone
    router variant (``get_message_standalone``) re-fetched with no eager
    load at all and relied on lazy loading, which raises ``MissingGreenlet``
    under an async session for any message that actually has citations or
    attachments; routing it through this shared funnel fixes that latent
    crash risk as a side effect of consolidating onto the one — already
    working — implementation, not as a separate behavior change.
    """
    conditions = [
        ChatMessage.id == message_id,
        ChatMessage.is_deleted == False,  # noqa: E712
        # Deliberately NOT filtered on ``superseded_by_message_id``: this is the
        # by-id fetch behind feedback/edit, which must still resolve a turn an
        # edit-and-resend has tombstoned.
    ]
    if thread_id is not None:
        conditions.append(ChatMessage.thread_id == thread_id)

    stmt = (
        select(ChatMessage)
        .options(
            selectinload(ChatMessage.citations).selectinload(Citation.document),
            selectinload(ChatMessage.attachments).selectinload(
                MessageAttachment.document
            ),
            selectinload(ChatMessage.thread)
            .selectinload(Thread.conversation)
            .selectinload(Conversation.workspace)
            .selectinload(Workspace.members),
        )
        .where(*conditions)
    )
    result = await db.execute(stmt)
    message: Optional[ChatMessage] = result.scalars().first()
    if not message:
        return None

    if (
        conversation_id is not None
        and message.thread.conversation_id != conversation_id
    ):
        return None
    if (
        workspace_id is not None
        and message.thread.conversation.workspace_id != workspace_id
    ):
        return None

    thread = message.thread
    # A soft-deleted thread — or its soft-deleted parent conversation/
    # workspace — must revoke access to its individual messages.
    if thread.is_deleted:
        return None
    if thread.conversation.is_deleted or thread.conversation.workspace.is_deleted:
        return None
    if not user_can_access_workspace(thread.conversation.workspace, user_id):
        return None
    return message


async def get_collection(
    db: AsyncSession,
    collection_id: UUID,
    user_id: UUID,
    *,
    workspace_id: Optional[UUID] = None,
    load_documents: bool = True,
) -> Optional[Collection]:
    """Fetch a collection the caller can access, or ``None``."""
    conditions = [
        Collection.id == collection_id,
        Collection.is_deleted == False,  # noqa: E712
    ]
    if workspace_id is not None:
        conditions.append(Collection.workspace_id == workspace_id)

    options = [selectinload(Collection.workspace).selectinload(Workspace.members)]
    if load_documents:
        options.append(
            selectinload(Collection.documents).selectinload(CollectionDocument.document)
        )

    stmt = select(Collection).options(*options).where(*conditions)
    result = await db.execute(stmt)
    collection: Optional[Collection] = result.scalars().first()
    if not collection:
        return None
    if collection.workspace.is_deleted:
        return None
    if not user_can_access_workspace(collection.workspace, user_id):
        return None
    return collection


async def get_accessible_document_or_none(
    db: AsyncSession,
    document_id: UUID,
    user_id: UUID,
    organization_id: Optional[UUID],
) -> Optional[Document]:
    """Return a document only if it belongs to the caller's organization.

    Collection attachment uses the same organization boundary as document
    reads. A former uploader without tenant scope has no document access.
    """
    if user_id is None or organization_id is None:
        return None
    filters = [
        Document.id == document_id,
        Document.is_deleted == False,  # noqa: E712
        Document.organization_id == organization_id,
    ]

    result = await db.execute(select(Document).where(*filters))
    document: Optional[Document] = result.scalars().first()
    return document


async def filter_owned_document_ids(
    db: AsyncSession, document_ids: Sequence[UUID], user_id: UUID
) -> List[UUID]:
    """Return only the document ids the caller's organization owns.

    Mirrors the documents-service access convention
    (``Document.organization_id == <caller org>`` + ``is_deleted == False``).
    The caller's org is resolved from ``user_id``. Ids that don't survive the
    filter (foreign-org, deleted, or nonexistent) are dropped and logged rather
    than raised, so a mixed batch still attaches the owned ones.

    An unscoped attach let a guessed foreign document UUID leak its title and
    mime type into a thread through the attachment response (IDOR), so both
    write paths — ``ChatService.create_message`` and the agent stream's
    ``_persist_user_message`` — funnel through here.
    """
    if not document_ids:
        return []

    org_result = await db.execute(
        select(User.organization_id).where(User.id == user_id)
    )
    organization_id = org_result.scalar_one_or_none()
    if organization_id is None:
        logger.warning(
            "User %s has no organization; dropping %d attachment id(s)",
            user_id,
            len(document_ids),
        )
        return []

    owned_result = await db.execute(
        select(Document.id).where(
            Document.id.in_(document_ids),
            Document.organization_id == organization_id,
            Document.is_deleted == False,  # noqa: E712
        )
    )
    owned_ids = list(owned_result.scalars().all())

    dropped = set(document_ids) - set(owned_ids)
    if dropped:
        logger.warning(
            "Dropped %d attachment id(s) not owned by org %s: %s",
            len(dropped),
            organization_id,
            sorted(str(d) for d in dropped),
        )
    return owned_ids

"""
Chat Service for Terminal Observatory thread-centric chat persistence.

Provides CRUD operations for workspaces, conversations, threads, and messages.

Task 4.3 consolidated the workspace/conversation/thread/message/collection
CRUD concerns that used to be duplicated between this class and the
``workspace_routes`` router-inline implementations into single owners under
``src/services/threads/{workspace,conversation,thread,message,collection}_service.py``
(+ the shared ``workspace_access`` funnel). Most methods below now delegate
to those modules, each passing the flag that reproduces this class's own
pre-4.3 behavior — see each method's docstring and
``docs/plans/2026-07-15-maintainability-foundation.md`` Task 4.3 (+ its
2026-07-16 amendment) for the specific divergence each flag preserves.

Out of scope for that consolidation, unchanged: ``create_message``,
``create_assistant_message``, ``_filter_owned_document_ids``,
``get_thread_context``, ``bulk_update_threads``, ``bulk_delete_threads``,
``bulk_summarize_threads``, ``search_conversations``, ``get_workspace_stats``.
"""

import logging
from datetime import datetime
from typing import List, Literal, Optional, Tuple
from uuid import UUID

from sqlalchemy import desc, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import (
    ChatMessage,
    Citation,
    Collection,
    Conversation,
    Document,
    MessageAttachment,
    MessageRole,
    Thread,
    ThreadStatus,
    User,
    Workspace,
)
from src.schemas.chat import (
    ChatMessageCreate,
    ChatMessageUpdate,
    CollectionCreate,
    CollectionUpdate,
    ConversationCreate,
    ConversationUpdate,
    ThreadCreate,
    ThreadUpdate,
    WorkspaceCreate,
    WorkspaceUpdate,
)
from src.services.threads import (
    collection_service,
    conversation_service,
    message_service,
    thread_service,
    workspace_access,
    workspace_service,
)

logger = logging.getLogger(__name__)


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so search terms match literally (R5-L8)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ChatService:
    """
    Service layer for chat persistence operations.

    Handles all CRUD operations for the thread-centric chat schema:
    - Workspaces (project containers)
    - Conversations (topic containers)
    - Threads (inquiry lines)
    - Messages (chat entries)
    - Collections (document groupings)
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    # =========================================================================
    # Workspace Operations
    # =========================================================================

    async def create_workspace(
        self, data: WorkspaceCreate, owner_id: UUID
    ) -> Workspace:
        """Create a new workspace.

        Delegates to ``workspace_service.create_workspace`` with
        ``enforce_org_match=True``. Pre-4.3, this class trusted
        ``data.organization_id`` verbatim with no cross-org guard even though
        the router-inline create endpoint has always enforced it — a
        pre-existing tenant gap on the ``conversations.py`` create endpoint
        this class backs, closed here rather than preserved via a flag (see
        ``workspace_service.create_workspace``'s docstring). Raises
        ``PermissionError`` on a cross-org request, same as the router-inline
        endpoint; the caller maps that to a 403.
        """
        org_result = await self.db.execute(
            select(User.organization_id).where(User.id == owner_id)
        )
        user_organization_id = org_result.scalar_one_or_none()

        workspace = await workspace_service.create_workspace(
            self.db, data, owner_id, user_organization_id, enforce_org_match=True
        )
        # PR 3 Task 3.2: leaf now flushes; this delegate owns the request commit
        # for its callers (the legacy conversations.py create endpoint issues no
        # commit of its own). Removal condition: delete when that caller owns it.
        await self.db.commit()
        logger.info(f"Created workspace: {workspace.id} - {workspace.name}")
        return workspace

    async def get_workspace(
        self, workspace_id: UUID, user_id: UUID, load_conversations: bool = False
    ) -> Optional[Workspace]:
        """Get workspace by ID if user has access.

        This is the access gate for every conversation/collection CRUD op, so
        by default it loads only what the check needs (owner/public/members).
        Eager-loading the unbounded conversations collection on every gate call
        grew linearly with workspace age; pass load_conversations=True only
        where the response actually renders conversation_count. Never loads
        collections — this class never needed that field.
        """
        return await workspace_access.get_workspace(
            self.db,
            workspace_id,
            user_id,
            load_conversations=load_conversations,
            load_collections=False,
        )

    async def list_workspaces(
        self,
        user_id: UUID,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Workspace], int]:
        """List workspaces accessible to user.

        Delegates with ``filter_deleted_memberships=True``. Pre-4.3, this
        class did not exclude a soft-deleted ``WorkspaceMember`` row, so a
        removed member still saw the workspace listed on the
        ``conversations.py`` list endpoint this class backs, even though the
        router-inline endpoint has always filtered it out — a pre-existing
        tenant gap closed here rather than preserved via a flag.
        """
        workspaces, total = await workspace_service.list_workspaces(
            self.db,
            user_id,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
            filter_deleted_memberships=True,
        )
        return workspaces, total

    async def update_workspace(
        self, workspace_id: UUID, data: WorkspaceUpdate, user_id: UUID
    ) -> Optional[Workspace]:
        """Update workspace. Returns ``None`` on not-found *or* insufficient
        permission, matching this class's pre-4.3 undifferentiated result.

        PR 3 Task 3.2: delegate-then-commit (leaf now flushes) — the legacy
        conversations.py update endpoint owns no commit of its own."""
        try:
            result = await workspace_service.update_workspace(
                self.db, workspace_id, data, user_id
            )
        except PermissionError:
            return None
        await self.db.commit()
        return result

    async def delete_workspace(self, workspace_id: UUID, user_id: UUID) -> bool:
        """Soft delete workspace.

        Delegates with ``stamp_deleted_at=False`` — this class's pre-4.3
        behavior never set ``deleted_at`` (the router-inline delete endpoint
        always has). Returns ``False`` on not-found *or* insufficient
        permission, matching the pre-4.3 undifferentiated result.
        """
        try:
            result = await workspace_service.delete_workspace(
                self.db, workspace_id, user_id, stamp_deleted_at=False
            )
        except PermissionError:
            return False
        # PR 3 Task 3.2: delegate-then-commit (leaf now flushes).
        await self.db.commit()
        if result:
            logger.info(f"Deleted workspace: {workspace_id}")
        return bool(result)

    def _user_can_access_workspace(self, workspace: Workspace, user_id: UUID) -> bool:
        """Check if user can access workspace. Delegates to the canonical
        predicate in ``workspace_access`` — kept here as a thin wrapper since
        it's still called internally by ``get_thread_context`` and other
        out-of-scope methods below."""
        return workspace_access.user_can_access_workspace(workspace, user_id)

    # =========================================================================
    # Conversation Operations
    # =========================================================================

    async def create_conversation(
        self, data: ConversationCreate, user_id: UUID
    ) -> Optional[Conversation]:
        """Create a new conversation in a workspace. Returns ``None`` on
        not-found *or* insufficient permission, matching this class's
        pre-4.3 undifferentiated result."""
        try:
            conversation = await conversation_service.create_conversation(
                self.db, data, user_id
            )
        except PermissionError:
            return None
        # PR 3 Task 3.2: delegate-then-commit (leaf now flushes) — the legacy
        # conversations.py create endpoint owns no commit of its own.
        await self.db.commit()
        if conversation:
            logger.info(
                f"Created conversation: {conversation.id} - {conversation.title}"
            )
        return conversation

    async def get_conversation(
        self, conversation_id: UUID, user_id: UUID
    ) -> Optional[Conversation]:
        """Get conversation by ID."""
        return await workspace_access.get_conversation(
            self.db, conversation_id, user_id, load_threads=True
        )

    async def list_conversations(
        self,
        workspace_id: UUID,
        user_id: UUID,
        include_archived: bool = False,
        search_query: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Conversation], int]:
        """List conversations in a workspace.

        Delegates with ``order_pinned_first=True`` — this class's pre-4.3
        ordering (pinned conversations float to the top). The router-inline
        list endpoints order by activity only and pass ``False``.
        """
        result = await conversation_service.list_conversations(
            self.db,
            workspace_id,
            user_id,
            include_archived=include_archived,
            search_query=search_query,
            limit=limit,
            offset=offset,
            order_pinned_first=True,
        )
        if result is None:
            return [], 0
        conversations, total, _counts = result
        return conversations, total

    async def update_conversation(
        self, conversation_id: UUID, data: ConversationUpdate, user_id: UUID
    ) -> Optional[Conversation]:
        """Update conversation. Returns ``None`` on not-found *or*
        insufficient permission, matching this class's pre-4.3
        undifferentiated result.

        PR 3 Task 3.2: delegate-then-commit (leaf now flushes)."""
        try:
            result = await conversation_service.update_conversation(
                self.db, conversation_id, data, user_id
            )
        except PermissionError:
            return None
        await self.db.commit()
        return result

    async def delete_conversation(self, conversation_id: UUID, user_id: UUID) -> bool:
        """Soft delete conversation.

        Delegates with ``stamp_deleted_at=False`` (this class never set
        ``deleted_at``) and ``require_admin=True`` — this class required
        *admin* rights to delete a conversation; the router-inline delete
        endpoints require only edit rights (matching every other mutator
        there) and pass ``require_admin=False``. Returns ``False`` on
        not-found *or* insufficient permission, matching the pre-4.3
        undifferentiated result.
        """
        try:
            result = await conversation_service.delete_conversation(
                self.db,
                conversation_id,
                user_id,
                stamp_deleted_at=False,
                require_admin=True,
            )
        except PermissionError:
            return False
        # PR 3 Task 3.2: delegate-then-commit (leaf now flushes).
        await self.db.commit()
        if result:
            logger.info(f"Deleted conversation: {conversation_id}")
        return bool(result)

    # =========================================================================
    # Thread Operations
    # =========================================================================

    async def create_thread(
        self, data: ThreadCreate, user_id: UUID
    ) -> Optional[Thread]:
        """Create a new thread in a conversation.

        Flush-only (the leaf's ``commit`` flag was removed in PR 3 Task 3.2;
        ``create_thread`` is now unconditionally flush-only). This class does
        NOT commit here — its live caller (``src/api/threads/threads.py``) does
        further work (project auto-link, WS broadcast) in the same request and
        then owns the single ``await db.commit()``. Adding a commit here would
        split that atomic unit and fire the WS broadcast against a not-yet-
        committed thread. Returns ``None`` on not-found *or* insufficient
        permission, matching the pre-4.3 undifferentiated result.
        """
        try:
            thread = await thread_service.create_thread(self.db, data, user_id)
        except PermissionError:
            return None
        if thread:
            logger.info(f"Created thread: {thread.id}")
        return thread

    async def get_thread(
        self, thread_id: UUID, user_id: UUID, include_messages: bool = False
    ) -> Optional[Thread]:
        """Get thread by ID."""
        return await workspace_access.get_thread(
            self.db, thread_id, user_id, include_messages=include_messages
        )

    async def list_threads(
        self,
        conversation_id: UUID,
        user_id: UUID,
        status_filter: Optional[ThreadStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Thread], int]:
        """List threads in a conversation."""
        result = await thread_service.list_threads(
            self.db,
            conversation_id,
            user_id,
            status_filter=status_filter,
            limit=limit,
            offset=offset,
            with_preview=False,
        )
        if result is None:
            return [], 0
        threads, total, _previews = result
        return threads, total

    async def update_thread(
        self, thread_id: UUID, data: ThreadUpdate, user_id: UUID
    ) -> Optional[Thread]:
        """Update thread. Returns ``None`` on not-found *or* insufficient
        permission, matching this class's pre-4.3 undifferentiated result.

        Delegates with ``trigger_resolve_summary`` left at its default
        (``False``) — this class's pre-4.3 resolve-trigger comparison
        compared mismatched Enum classes and never actually fired; see
        ``thread_service.update_thread``'s docstring (Task 4.3 amendment
        A2).

        PR 3 Task 3.2: delegate-then-commit (leaf now flushes) — the legacy
        threads.py PATCH endpoint issues no commit of its own. The leaf's
        resolve-summary enqueue (dead under the default flag) is registered via
        enqueue_after_commit, so it would fire on THIS commit."""
        try:
            result = await thread_service.update_thread(
                self.db, thread_id, data, user_id
            )
        except PermissionError:
            return None
        await self.db.commit()
        return result

    async def delete_thread(self, thread_id: UUID, user_id: UUID) -> bool:
        """Soft delete thread.

        Delegates with ``stamp_deleted_at=False`` — this class's pre-4.3
        behavior never set ``deleted_at`` (the router-inline delete
        endpoints always have). Returns ``False`` on not-found *or*
        insufficient permission, matching the pre-4.3 undifferentiated
        result.
        """
        try:
            result = await thread_service.delete_thread(
                self.db, thread_id, user_id, stamp_deleted_at=False
            )
        except PermissionError:
            return False
        # PR 3 Task 3.2: delegate-then-commit (leaf now flushes).
        await self.db.commit()
        if result:
            logger.info(f"Deleted thread: {thread_id}")
        return bool(result)

    # ========================================================================
    # Bulk Thread Operations
    # ========================================================================

    async def bulk_update_threads(
        self,
        thread_ids: List[UUID],
        data: "ThreadUpdate",
        user_id: UUID,
        atomic: bool = False,
    ) -> List[Tuple[UUID, bool, Optional[str], Optional["Thread"]]]:
        """Bulk update multiple threads with same data.

        Args:
            thread_ids: List of thread UUIDs to update
            data: ThreadUpdate data to apply
            user_id: User performing the operation
            atomic: If True, all succeed or all fail. If False, best-effort.

        Returns:
            List of (thread_id, success, error_msg, thread) tuples.
        """
        results = []

        if atomic:
            # Atomic mode: all succeed or all fail
            try:
                # Start a savepoint for atomic operation
                async with self.db.begin_nested():
                    updated_threads = []

                    for thread_id in thread_ids:
                        thread = await self.get_thread(thread_id, user_id)
                        if not thread:
                            raise ValueError(f"Thread {thread_id} not found")

                        if not thread.conversation.workspace.can_user_edit(
                            str(user_id)
                        ):
                            raise PermissionError(
                                f"No permission to edit thread {thread_id}"
                            )

                        # Apply updates without committing
                        if data.title is not None:
                            thread.title = data.title
                        if data.summary is not None:
                            thread.summary = data.summary

                        status_changing_to_resolved = (
                            data.status is not None
                            and data.status == ThreadStatus.RESOLVED
                            and thread.status != ThreadStatus.RESOLVED
                        )

                        if data.status is not None:
                            thread.status = data.status

                        thread.updated_at = datetime.utcnow()
                        updated_threads.append(
                            (thread_id, thread, status_changing_to_resolved)
                        )

                # Commit after savepoint context exits
                await self.db.commit()

                # Build results and trigger any post-update tasks
                for thread_id, thread, status_changing_to_resolved in updated_threads:
                    await self.db.refresh(thread)
                    results.append((thread_id, True, None, thread))

                    if status_changing_to_resolved:
                        try:
                            from src.tasks.summarize_thread_task import (
                                summarize_thread_on_resolve_task,
                            )

                            summarize_thread_on_resolve_task.delay(str(thread_id))
                        except Exception as e:
                            logger.warning(
                                f"Failed to queue resolution summary for thread {thread_id}: {e}"
                            )

            except Exception as e:
                # Rollback on any error
                await self.db.rollback()
                logger.error(f"Atomic bulk update failed: {e}")
                # Return all as failed
                for thread_id in thread_ids:
                    results.append(
                        (thread_id, False, f"Atomic operation failed: {str(e)}", None)
                    )
        else:
            # Best-effort mode: continue on errors
            for thread_id in thread_ids:
                try:
                    thread = await self.update_thread(thread_id, data, user_id)
                    if thread:
                        results.append((thread_id, True, None, thread))
                    else:
                        results.append(
                            (
                                thread_id,
                                False,
                                "Thread not found or insufficient permissions",
                                None,
                            )
                        )
                except Exception as e:
                    logger.error(f"Error updating thread {thread_id}: {e}")
                    results.append((thread_id, False, str(e), None))

        return results

    async def bulk_delete_threads(
        self, thread_ids: List[UUID], user_id: UUID, atomic: bool = False
    ) -> List[Tuple[UUID, bool, Optional[str]]]:
        """Bulk soft delete multiple threads.

        Args:
            thread_ids: List of thread UUIDs to delete
            user_id: User performing the operation
            atomic: If True, all succeed or all fail. If False, best-effort.

        Returns:
            List of (thread_id, success, error_msg) tuples.
        """
        results = []

        if atomic:
            # Atomic mode: all succeed or all fail
            try:
                # Start a savepoint for atomic operation
                async with self.db.begin_nested():
                    deleted_ids = []

                    for thread_id in thread_ids:
                        thread = await self.get_thread(thread_id, user_id)
                        if not thread:
                            raise ValueError(f"Thread {thread_id} not found")

                        if not thread.conversation.workspace.can_user_edit(
                            str(user_id)
                        ):
                            raise PermissionError(
                                f"No permission to delete thread {thread_id}"
                            )

                        # Apply soft delete without committing
                        thread.is_deleted = True
                        thread.updated_at = datetime.utcnow()
                        deleted_ids.append(thread_id)

                # Commit after savepoint context exits
                await self.db.commit()

                # Build results
                for thread_id in deleted_ids:
                    results.append((thread_id, True, None))
                    logger.info(f"Deleted thread: {thread_id}")

            except Exception as e:
                # Rollback on any error
                await self.db.rollback()
                logger.error(f"Atomic bulk delete failed: {e}")
                # Return all as failed
                for thread_id in thread_ids:
                    results.append(
                        (thread_id, False, f"Atomic operation failed: {str(e)}")
                    )
        else:
            # Best-effort mode: continue on errors
            for thread_id in thread_ids:
                try:
                    success = await self.delete_thread(thread_id, user_id)
                    if success:
                        results.append((thread_id, True, None))
                    else:
                        results.append(
                            (
                                thread_id,
                                False,
                                "Thread not found or insufficient permissions",
                            )
                        )
                except Exception as e:
                    logger.error(f"Error deleting thread {thread_id}: {e}")
                    results.append((thread_id, False, str(e)))

        return results

    async def bulk_summarize_threads(
        self, thread_ids: List[UUID], user_id: UUID
    ) -> List[Tuple[UUID, bool, Optional[str], Optional["Thread"]]]:
        """Bulk trigger AI summarization for multiple threads.

        Args:
            thread_ids: List of thread UUIDs to summarize
            user_id: User performing the operation

        Returns:
            List of (thread_id, success, error_msg, thread) tuples.
        """
        results = []

        for thread_id in thread_ids:
            try:
                thread = await self.get_thread(thread_id, user_id)
                if not thread:
                    results.append(
                        (
                            thread_id,
                            False,
                            "Thread not found or insufficient permissions",
                            None,
                        )
                    )
                    continue

                # Trigger async summarization task
                # We use force=True to ensure a fresh summary is generated for manual bulk requests
                try:
                    from src.tasks.summarize_thread_task import summarize_thread_task

                    summarize_thread_task.delay(str(thread_id), force=True)
                    results.append((thread_id, True, None, thread))
                except Exception as e:
                    logger.error(
                        f"Failed to queue summarization for thread {thread_id}: {e}"
                    )
                    results.append(
                        (thread_id, False, f"Failed to queue task: {str(e)}", thread)
                    )

            except Exception as e:
                logger.error(f"Error in bulk summarize for thread {thread_id}: {e}")
                results.append((thread_id, False, str(e), None))

        return results

    # =========================================================================
    # Message Operations
    # =========================================================================

    async def create_message(
        self, data: ChatMessageCreate, user_id: UUID
    ) -> Optional[ChatMessage]:
        """Create a new message in a thread"""
        # Verify thread access
        thread = await self.get_thread(data.thread_id, user_id)
        if not thread:
            return None

        # Check edit permission
        if not thread.conversation.workspace.can_user_edit(str(user_id)):
            return None

        # Count tokens for the message using fast estimation to avoid blocking
        # on large messages (tiktoken encoding can be slow for long content)
        from src.utils.token_counter import count_message_tokens

        message_token_count = count_message_tokens(
            data.content, data.role.value, estimate_only=True
        )

        # Compared by value on purpose: `data.role` is the *schemas* MessageRole
        # (a str enum) while `MessageRole` here is the *models* one (plain
        # Enum), so `==` between them is always False.
        is_user_turn = getattr(data.role, "value", data.role) == MessageRole.USER.value

        if data.client_message_id is not None and is_user_turn:
            # B8-I2: the client retries this POST blindly, so a
            # 5xx/timeout-after-commit used to duplicate the row and re-bump
            # the thread counters. Same ON CONFLICT upsert the agent path uses
            # against the partial index uq_chat_messages_thread_client_msg_user
            # — not a select-then-insert, which races two concurrent retries.
            inserted_id = (
                await self.db.execute(
                    pg_insert(ChatMessage)
                    .values(
                        thread_id=data.thread_id,
                        user_id=user_id,
                        role=data.role,
                        content=data.content,
                        token_count=message_token_count,
                        latency_ms=data.latency_ms,
                        stopped=bool(data.stopped),
                        client_message_id=data.client_message_id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["thread_id", "client_message_id"],
                        # Literal role: an enum bind parameter stops PostgreSQL
                        # inferring the partial index (see _persist_user_message).
                        index_where=text(
                            "client_message_id IS NOT NULL AND role = 'user'"
                        ),
                    )
                    .returning(ChatMessage.id)
                )
            ).scalar_one_or_none()
            if inserted_id is None:
                # A retry of a turn already stored: return that row untouched,
                # counters and attachments included.
                return (
                    await self.db.execute(
                        select(ChatMessage).where(
                            ChatMessage.thread_id == data.thread_id,
                            ChatMessage.client_message_id == data.client_message_id,
                            ChatMessage.role == MessageRole.USER,
                        )
                    )
                ).scalar_one_or_none()
            message = await self.db.get(ChatMessage, inserted_id)
            if message is None:  # pragma: no cover - inserted row must exist
                return None
        else:
            message = ChatMessage(
                thread_id=data.thread_id,
                user_id=user_id if data.role == MessageRole.USER else None,
                role=data.role,
                content=data.content,
                token_count=message_token_count,
                latency_ms=data.latency_ms,
                stopped=bool(data.stopped),
            )
            self.db.add(message)
        await self.db.flush()  # Flush to get message.id for citations/attachments

        # Handle attachments — only documents the caller's org owns may be
        # attached. An unscoped attach let a guessed foreign document UUID leak
        # its title/mime into this thread via the attachment response (IDOR).
        # Non-owned / deleted ids are silently dropped (logged), mirroring the
        # documents service's org-scoping convention.
        if data.attachment_ids:
            owned_ids = await self._filter_owned_document_ids(
                data.attachment_ids, user_id
            )
            for doc_id in owned_ids:
                attachment = MessageAttachment(
                    message_id=message.id, document_id=doc_id
                )
                self.db.add(attachment)

        # Handle citations (for assistant messages with RAG sources)
        if data.citations:
            for cit in data.citations:
                citation = Citation(
                    message_id=message.id,
                    document_id=cit.document_id,
                    external_reference_id=cit.external_reference_id,
                    chunk_index=cit.chunk_index,
                    chunk_id=cit.chunk_id,
                    snippet=cit.snippet,
                    page_number=cit.page_number,
                    score=cit.score,
                    rerank_score=cit.rerank_score,
                    document_title=cit.document_title,
                    document_type=cit.document_type,
                )
                self.db.add(citation)

        # Update thread stats atomically so concurrent writers cannot lose an
        # increment. The expression deliberately bypasses ORM synchronization;
        # this path does not inspect the in-memory counter after the commit.
        await self.db.execute(
            update(Thread)
            .where(Thread.id == data.thread_id)
            .values(
                message_count=func.coalesce(Thread.message_count, 0) + 1,
                token_count=func.coalesce(Thread.token_count, 0) + message_token_count,
                last_message_at=datetime.utcnow(),
            )
            .execution_options(synchronize_session=False)
        )

        # Update conversation activity
        thread.conversation.update_activity()

        await self.db.commit()
        await self.db.refresh(message)

        return message

    async def _filter_owned_document_ids(
        self, document_ids: List[UUID], user_id: UUID
    ) -> List[UUID]:
        """Return only the document ids the caller's organization owns.

        Thin delegate: the predicate lives in ``workspace_access`` so the
        agent stream's user-turn persist can apply the identical scoping on
        its own session.
        """
        return await workspace_access.filter_owned_document_ids(
            self.db, document_ids, user_id
        )

    async def create_assistant_message(
        self,
        thread_id: UUID,
        content: str,
        model_name: Optional[str] = None,
        token_count: int = 0,
        latency_ms: Optional[int] = None,
        citations: Optional[List[dict]] = None,
    ) -> Optional[ChatMessage]:
        """Create an assistant message with optional citations"""
        message = ChatMessage.create_assistant_message(
            thread_id=str(thread_id),
            content=content,
            model_name=model_name,
            token_count=token_count,
            latency_ms=latency_ms,
        )
        self.db.add(message)
        await self.db.flush()  # Flush to get message.id for citations

        # Add citations if provided
        if citations:
            for cit in citations:
                # Validate document_id is a valid UUID, otherwise set to None
                doc_id = cit.get("document_id")
                if doc_id:
                    try:
                        import uuid as _uuid

                        _uuid.UUID(str(doc_id))
                    except (ValueError, AttributeError):
                        logger.warning(
                            f"Invalid document_id '{doc_id}', setting to None"
                        )
                        doc_id = None

                try:
                    citation = Citation(
                        message_id=message.id,
                        document_id=doc_id,
                        external_reference_id=cit.get("external_reference_id"),
                        chunk_index=cit.get("chunk_index"),
                        chunk_id=cit.get("chunk_id"),
                        snippet=cit.get("snippet"),
                        page_number=cit.get("page_number"),
                        score=cit.get("score"),
                        rerank_score=cit.get("rerank_score"),
                        document_title=cit.get("document_title"),
                        document_type=cit.get("document_type"),
                    )
                    self.db.add(citation)
                except Exception as cit_exc:
                    logger.warning(f"Failed to create citation: {cit_exc}")

        # Update thread stats (eagerly load conversation to avoid lazy-load
        # MissingGreenlet errors in async context)
        stmt = (
            select(Thread)
            .options(selectinload(Thread.conversation))
            .where(Thread.id == thread_id)
        )
        result = await self.db.execute(stmt)
        thread = result.scalars().first()
        if thread:
            thread.conversation.update_activity()
            await self.db.execute(
                update(Thread)
                .where(Thread.id == thread_id)
                .values(
                    message_count=func.coalesce(Thread.message_count, 0) + 1,
                    token_count=func.coalesce(Thread.token_count, 0) + token_count,
                    last_message_at=datetime.utcnow(),
                )
                .execution_options(synchronize_session=False)
            )

        await self.db.commit()
        await self.db.refresh(message)
        if thread:
            # AsyncSession keeps objects live across commit; refresh the
            # deliberately unsynchronized SQL expression before the threshold.
            await self.db.refresh(thread)

        # Trigger async summarization if thread has enough messages
        if thread and thread.message_count >= 3:
            try:
                from src.services.threads.thread_summarization_service import (
                    enqueue_summarization,
                )

                enqueue_summarization(thread_id)
            except Exception as e:
                # Don't fail message creation if summarization queue fails
                logger.warning(
                    f"Failed to queue summarization for thread {thread_id}: {e}"
                )

        return message

    async def get_message(
        self, message_id: UUID, user_id: UUID
    ) -> Optional[ChatMessage]:
        """Get message by ID."""
        return await workspace_access.get_message(self.db, message_id, user_id)

    async def list_messages(
        self,
        thread_id: UUID,
        user_id: UUID,
        limit: int = 100,
        offset: int = 0,
        before_id: Optional[UUID] = None,
        since: Optional[datetime] = None,
        order: Literal["asc", "desc"] = "asc",
    ) -> Tuple[List[ChatMessage], int]:
        """List messages in a thread.

        ``since`` (optional) filters to messages with ``created_at > since``
        (strict). Used by clients (CLI, web) to delta-fetch only rows newer
        than their last-seen timestamp.

        ``order`` controls the sort direction by the stable
        ``(created_at, id)`` tuple: ``"asc"`` (oldest first, default for
        backward compatibility) or ``"desc"`` (newest first, used by the web
        client for newest-first pagination). ``before_id`` filtering works
        identically with either sort order.
        """
        result = await message_service.list_messages(
            self.db,
            thread_id,
            user_id,
            limit=limit,
            offset=offset,
            before_id=before_id,
            since=since,
            order=order,
        )
        if result is None:
            return [], 0
        messages, total, _has_more = result
        return messages, total

    async def update_message_feedback(
        self, message_id: UUID, data: ChatMessageUpdate, user_id: UUID
    ) -> Optional[ChatMessage]:
        """Update message feedback. Returns ``None`` on not-found *or*
        insufficient permission (get_message only checks read access;
        writing feedback requires edit rights), matching this class's
        pre-4.3 undifferentiated result.

        PR 3 Task 3.2: delegate-then-commit (leaf now flushes)."""
        try:
            result = await message_service.update_message_feedback(
                self.db, message_id, data, user_id
            )
        except PermissionError:
            return None
        # R5-M11: the route rejects cross-thread ids AFTER this method — the
        # old unconditional commit applied feedback then answered 404.
        # Flush-only here; the ROUTE commits after its thread-match check.
        if result is not None and getattr(result, "thread_id", None) is not None:
            await self.db.flush()
        return result

    async def delete_message(self, message_id: UUID, user_id: UUID) -> bool:
        """Soft delete message.

        Delegates with ``require_author_or_admin=True`` — this class's
        pre-4.3 permission rule: the message's author may always delete it,
        anyone else needs admin rights. The standalone router endpoint
        requires only edit rights and never checks authorship (passes
        ``require_author_or_admin=False``). Returns ``False`` on not-found
        *or* insufficient permission, matching the pre-4.3 undifferentiated
        result.
        """
        try:
            result = await message_service.delete_message(
                self.db, message_id, user_id, require_author_or_admin=True
            )
        except PermissionError:
            return False
        # PR 3 Task 3.2: delegate-then-commit (leaf now flushes).
        await self.db.commit()
        if result:
            logger.info(f"Deleted message: {message_id}")
        return bool(result)

    # =========================================================================
    # Collection Operations
    # =========================================================================

    async def create_collection(
        self, data: CollectionCreate, user_id: UUID
    ) -> Optional[Collection]:
        """Create a new collection in a workspace.

        Delegates to ``collection_service.create_collection``, which — unlike
        this class's old inline implementation — checks per-document
        organization ownership before attaching initial documents (this
        method has zero production callers, so there is no existing insecure
        behavior worth preserving via a flag; see
        ``collection_service``'s module docstring).

        PR 3 Task 3.2: the leaf now flushes, so this delegate owns the request
        commit (delegate-then-commit) — external behavior identical (persisted
        on return). Kept faithful even though this method is currently
        caller-less. Removal condition: delete the commit when/if this delegate
        is deleted or its (currently non-existent) callers own the commit.
        """
        try:
            collection = await collection_service.create_collection(
                self.db, data, user_id
            )
        except PermissionError:
            return None
        await self.db.commit()
        if collection:
            logger.info(f"Created collection: {collection.id} - {collection.name}")
        return collection

    async def get_collection(
        self, collection_id: UUID, user_id: UUID
    ) -> Optional[Collection]:
        """Get collection by ID."""
        return await workspace_access.get_collection(self.db, collection_id, user_id)

    async def list_collections(
        self, workspace_id: UUID, user_id: UUID, limit: int = 50, offset: int = 0
    ) -> Tuple[List[Collection], int]:
        """List collections in a workspace."""
        result = await collection_service.list_collections(
            self.db, workspace_id, user_id, limit=limit, offset=offset
        )
        return result if result is not None else ([], 0)

    async def add_documents_to_collection(
        self, collection_id: UUID, document_ids: List[UUID], user_id: UUID
    ) -> Optional[Collection]:
        """Add documents to a collection.

        Delegates to ``collection_service.add_documents_to_collection``,
        which — unlike this class's old inline implementation — checks
        per-document organization ownership before attaching (this method
        has zero production callers, so there is no existing insecure
        behavior worth preserving via a flag).

        PR 3 Task 3.2: delegate-then-commit (leaf now flushes); external
        behavior identical. Removal condition: delete the commit when this
        delegate is removed or its callers own the commit.
        """
        try:
            collection = await collection_service.add_documents_to_collection(
                self.db, collection_id, document_ids, user_id
            )
        except PermissionError:
            return None
        await self.db.commit()
        return collection

    async def remove_documents_from_collection(
        self, collection_id: UUID, document_ids: List[UUID], user_id: UUID
    ) -> Optional[Collection]:
        """Remove documents from a collection.

        Delegates to ``collection_service.remove_documents_from_collection``,
        which soft-deletes the ``CollectionDocument`` rows (this class's old
        implementation hard-deleted them; zero production callers, so there
        is no existing behavior worth preserving via a flag — soft-delete
        also matches every other delete in this schema).

        PR 3 Task 3.2: delegate-then-commit (leaf now flushes); external
        behavior identical. Removal condition: delete the commit when this
        delegate is removed or its callers own the commit.
        """
        try:
            collection = await collection_service.remove_documents_from_collection(
                self.db, collection_id, document_ids, user_id
            )
        except PermissionError:
            return None
        await self.db.commit()
        return collection

    # =========================================================================
    # Utility Methods
    # =========================================================================

    async def get_thread_context(
        self,
        thread_id: UUID,
        user_id: UUID,
        max_messages: Optional[int] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> dict:
        """
        Get thread messages formatted for LLM context.

        Returns messages in chronological order, limited by count or tokens,
        with metadata about context usage and truncation.

        Args:
            thread_id: Thread UUID
            user_id: User UUID for access control
            max_messages: Override default max messages (uses config if None)
            max_tokens: Override default max tokens (uses config if None)
            model: Model name for model-aware token limits

        Returns:
            Dictionary with messages and context metadata
        """
        from src.core.config import settings
        from src.utils.token_counter import count_tokens

        # Use config defaults if not specified
        effective_max_messages = max_messages or settings.THREAD_DEFAULT_MAX_MESSAGES
        effective_max_tokens = max_tokens or settings.THREAD_DEFAULT_MAX_TOKENS

        # If model specified, use model-aware token limit
        if model and max_tokens is None:
            model_key = model.lower()
            for key, limit in settings.MODEL_CONTEXT_LIMITS.items():
                if key in model_key:
                    # Use 75% of model limit for context (leave room for response)
                    effective_max_tokens = int(limit * 0.75)
                    break

        thread = await self.get_thread(thread_id, user_id, include_messages=True)
        if not thread:
            # R5-M10: signal "not found / no access" distinctly so the route
            # can 404; the old fake empty-context dict made that branch dead.
            return None

        messages = []
        total_tokens = 0
        all_message_count = len(
            [
                m
                for m in thread.messages
                if not m.is_deleted and m.superseded_by_message_id is None
            ]
        )
        truncated = False

        # Get messages in reverse order (newest first) for token limiting
        for msg in reversed(thread.messages):
            if msg.is_deleted:
                continue
            # Edit-and-resend tombstone — skip exactly like a soft delete, else
            # the model context carries the prompt the user replaced.
            if msg.superseded_by_message_id is not None:
                continue

            # Use accurate token counting
            msg_tokens = msg.token_count if msg.token_count else 0

            if total_tokens + msg_tokens > effective_max_tokens:
                truncated = True
                break

            messages.insert(0, msg.to_llm_format())
            total_tokens += msg_tokens

            if len(messages) >= effective_max_messages:
                truncated = True
                break

        # Calculate context usage ratio for warning
        usage_ratio = (
            total_tokens / effective_max_tokens if effective_max_tokens > 0 else 0
        )
        approaching_limit = usage_ratio >= settings.THREAD_CONTEXT_WARN_THRESHOLD

        return {
            "messages": messages,
            "metadata": {
                "truncated": truncated,
                "total_tokens": total_tokens,
                "max_tokens": effective_max_tokens,
                "message_count": len(messages),
                "total_messages": all_message_count,
                "usage_ratio": round(usage_ratio, 2),
                "approaching_limit": approaching_limit,
            },
        }

    async def search_conversations(
        self, workspace_id: UUID, user_id: UUID, query: str, limit: int = 20
    ) -> List[Conversation]:
        """Search conversations by title/description"""
        workspace = await self.get_workspace(workspace_id, user_id)
        if not workspace:
            return []

        # R5-L8: literal matching — %/_ no longer wildcard the scan
        search_pattern = f"%{_escape_like(query)}%"

        stmt = (
            select(Conversation)
            .where(
                Conversation.workspace_id == workspace_id,
                Conversation.is_deleted == False,
                or_(
                    Conversation.title.ilike(search_pattern, escape="\\"),
                    Conversation.description.ilike(search_pattern, escape="\\"),
                ),
            )
            .order_by(desc(Conversation.last_activity_at))
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        conversations = result.scalars().all()

        return conversations

    async def get_workspace_stats(
        self, workspace_id: UUID, user_id: UUID
    ) -> Optional[dict]:
        """Get workspace statistics"""
        workspace = await self.get_workspace(workspace_id, user_id)
        if not workspace:
            return None

        # Count conversations
        conv_count_stmt = select(func.count(Conversation.id)).where(
            Conversation.workspace_id == workspace_id, Conversation.is_deleted == False
        )
        conv_count_result = await self.db.execute(conv_count_stmt)
        conv_count = conv_count_result.scalar()

        # Count threads
        thread_count_stmt = (
            select(func.count(Thread.id))
            .join(Conversation)
            .where(
                Conversation.workspace_id == workspace_id, Thread.is_deleted == False
            )
        )
        thread_count_result = await self.db.execute(thread_count_stmt)
        thread_count = thread_count_result.scalar()

        # Count messages. Deliberately NOT filtered on
        # ``superseded_by_message_id``: this is analytics — an edit-and-resend
        # tombstones a turn for display, it does not un-happen it.
        msg_count_stmt = (
            select(func.count(ChatMessage.id))
            .join(Thread)
            .join(Conversation)
            .where(
                Conversation.workspace_id == workspace_id,
                ChatMessage.is_deleted == False,
            )
        )
        msg_count_result = await self.db.execute(msg_count_stmt)
        msg_count = msg_count_result.scalar()

        # Count collections
        coll_count_stmt = select(func.count(Collection.id)).where(
            Collection.workspace_id == workspace_id, Collection.is_deleted == False
        )
        coll_count_result = await self.db.execute(coll_count_stmt)
        coll_count = coll_count_result.scalar()

        return {
            "workspace_id": str(workspace_id),
            "conversation_count": conv_count,
            "thread_count": thread_count,
            "message_count": msg_count,
            "collection_count": coll_count,
            "member_count": len(workspace.members),
        }


def get_chat_service(db: AsyncSession) -> ChatService:
    """Dependency injection helper for ChatService"""
    return ChatService(db)

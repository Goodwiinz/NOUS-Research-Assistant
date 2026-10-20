"""Atomic selected-branch views for native chat reads; no transcript writes."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from src.models.chat_message import ChatMessage, MessageRole
from src.models.thread import Thread
from src.services.threads import message_branches, workspace_access


@dataclass
class MessagePageMetadata:
    message_branching: bool = False
    branch_document_id: UUID | None = None
    selected_message_id: UUID | None = None

    def response_fields(self) -> dict[str, Any]:
        return {
            "message_branching": self.message_branching,
            "branch_document_id": self.branch_document_id,
            "selected_message_id": self.selected_message_id,
        }


@dataclass
class ThreadRead:
    thread: Thread
    path: list[ChatMessage] | None = None
    siblings: dict[UUID | None, list[UUID]] = field(default_factory=dict)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def read_thread(
    db: AsyncSession,
    *,
    thread_id: UUID,
    actor_id: UUID,
    conversation_id: UUID | None = None,
    workspace_id: UUID | None = None,
) -> ThreadRead | None:
    # Authorize before locking: a caller without access must not be able to
    # hold, or queue on, another tenant's thread row.
    thread = await workspace_access.get_thread(
        db,
        thread_id,
        actor_id,
        include_messages=False,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        authorize_bound_document=True,
    )
    if thread is None:
        return None
    # Linear threads keep their lock-free reads. Branching is only enabled on
    # an empty thread (prepare_branch_submission) and never turned off, so a
    # read racing the first binding sees at most one single-path exchange,
    # which the linear view renders the same way.
    if thread.message_branching is not True:
        return ThreadRead(thread)
    # Binding, selection and canonical writers take FOR UPDATE on this row.
    # FOR SHARE excludes them through projection/paging, so a new head cannot
    # mix views, without serializing concurrent readers of the same thread.
    await db.execute(
        select(Thread)
        .where(Thread.id == thread_id)
        .with_for_update(of=Thread, read=True)
        .execution_options(populate_existing=True)
    )
    actor, thread = await message_branches.authorize_branch_thread(
        db, actor_id=actor_id, thread_id=thread_id, require_edit=False
    )
    await message_branches._authorize_document(db, actor, thread.branch_document_id)
    # Read bounded graph identities first. Unselected sibling bodies, citations
    # and attachments are never hydrated just to return a small message page.
    rows = list(
        (
            await db.scalars(
                select(ChatMessage)
                .options(
                    load_only(
                        ChatMessage.id,
                        ChatMessage.thread_id,
                        ChatMessage.parent_message_id,
                        ChatMessage.role,
                        ChatMessage.created_at,
                        ChatMessage.selected_at,
                    )
                )
                .where(
                    ChatMessage.thread_id == thread_id,
                    ChatMessage.is_deleted.is_(False),
                    ChatMessage.superseded_by_message_id.is_(None),
                )
                .limit(message_branches.MAX_BRANCH_MESSAGES + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(rows) > message_branches.MAX_BRANCH_MESSAGES:
        raise message_branches.BranchAccessError(
            409, "Chat history exceeds its supported size"
        )
    by_id = {row.id: row for row in rows}
    path = message_branches._path(by_id, thread.selected_message_id)
    siblings: dict[UUID | None, list[UUID]] = {}
    for row in sorted(rows, key=lambda item: (_aware(item.created_at), str(item.id))):
        if row.role == MessageRole.USER:
            siblings.setdefault(row.parent_message_id, []).append(row.id)
    return ThreadRead(thread, path, siblings)


def selected_page(
    view: ThreadRead,
    *,
    selected_message_id: UUID | None,
    before_id: UUID | None,
    since: datetime | None,
    offset: int,
    limit: int,
    order: str,
) -> tuple[list[UUID], int, bool]:
    assert view.path is not None
    head = view.thread.selected_message_id
    if (
        (before_id is not None or offset > 0 or since is not None)
        and selected_message_id is None
    ) or (selected_message_id is not None and selected_message_id != head):
        raise message_branches.BranchAccessError(
            409, "The selected branch changed; reload messages"
        )
    rows = view.path
    if before_id is not None:
        positions = {row.id: index for index, row in enumerate(rows)}
        if before_id not in positions:
            raise message_branches.BranchAccessError(
                409, "The message cursor is not on the selected branch"
            )
        rows = rows[: positions[before_id]]
    if since is not None:
        rows = [row for row in rows if _aware(row.created_at) > _aware(since)]
    total = len(rows)
    if order == "desc":
        rows = list(reversed(rows))
        offset = 0
    page = rows[offset : offset + limit]
    return [row.id for row in page], total, offset + len(page) < total

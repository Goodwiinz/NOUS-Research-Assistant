"""Canonical opt-in PDF branches; helpers join their caller's transaction.

Only select_branch owns a commit. Acceptance and assistant persistence remain
the sole transcript writers and call the other helpers inside their writes.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.agent_run import AgentRun
from src.models.chat_message import ChatMessage, MessageRole
from src.models.conversation import Conversation
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace
from src.services.threads import workspace_access
from src.shared.enums import JobStatus

MAX_BRANCH_MESSAGES = 2000


class BranchAccessError(ValueError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class BranchSubmission:
    thread: Thread
    parent_message_id: UUID | None
    actor_id: UUID
    organization_id: UUID


def _uuid(value: object) -> UUID:
    return UUID(str(value))


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def authorize_branch_thread(
    db: AsyncSession,
    *,
    actor_id: UUID,
    thread_id: UUID,
    require_edit: bool = True,
    lock: bool = False,
) -> tuple[User, Thread]:
    """Fresh caller, tenant and canonical ancestors, including after lock waits."""
    statement = select(Thread).where(Thread.id == thread_id)
    if lock:
        statement = statement.with_for_update(of=Thread)
    thread: Thread | None = (
        await db.execute(statement.execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if thread is None:
        raise BranchAccessError(404, "Chat not found")
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
        raise BranchAccessError(404, "Chat not found")
    await db.execute(
        select(Thread)
        .where(Thread.id == thread_id)
        .options(
            selectinload(Thread.conversation)
            .selectinload(Conversation.workspace)
            .selectinload(Workspace.members)
        )
        .execution_options(populate_existing=True)
    )
    accessible = await workspace_access.get_thread(
        db, thread_id, actor_id, include_messages=False
    )
    if accessible is None or (
        require_edit
        and not accessible.conversation.workspace.can_user_edit(str(actor_id))
    ):
        raise BranchAccessError(404, "Chat not found")
    return actor, accessible


async def _authorize_document(
    db: AsyncSession, actor: User, document_id: UUID | None
) -> None:
    if document_id is None or actor.organization_id is None:
        raise BranchAccessError(404, "Document not found")
    # Match canonical document authority using scalar columns, so a document
    # loaded before a concurrent revocation cannot supply cached readiness.
    document = (
        await db.execute(
            select(Document.document_type, Document.processing_status).where(
                Document.id == document_id,
                Document.organization_id == actor.organization_id,
                Document.is_deleted.is_(False),
            )
        )
    ).one_or_none()
    if document is None or document.document_type != DocumentType.PDF:
        raise BranchAccessError(404, "Document not found")
    if document.processing_status != ProcessingStatus.COMPLETED:
        raise BranchAccessError(409, "PDF processing is not complete")


async def authorize_branch_document_read(
    db: AsyncSession, *, actor_id: UUID, document_id: UUID | None
) -> None:
    """Fresh bound-document authority without recursively resolving a thread."""
    actor = (
        await db.scalars(
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
    ).one_or_none()
    if actor is None:
        raise BranchAccessError(404, "Chat not found")
    await _authorize_document(db, actor, document_id)


async def _messages(db: AsyncSession, thread_id: UUID) -> dict[UUID, ChatMessage]:
    rows = list(
        (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.thread_id == thread_id,
                    ChatMessage.is_deleted.is_(False),
                    ChatMessage.superseded_by_message_id.is_(None),
                )
                .limit(MAX_BRANCH_MESSAGES + 1)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    if len(rows) > MAX_BRANCH_MESSAGES:
        raise BranchAccessError(409, "Chat history exceeds its supported size")
    return {_uuid(row.id): row for row in rows}


def _path(
    messages: dict[UUID, ChatMessage], head_message_id: UUID | None
) -> list[ChatMessage]:
    result: list[ChatMessage] = []
    seen: set[UUID] = set()
    current = head_message_id
    while current is not None:
        if current in seen or current not in messages:
            raise BranchAccessError(404, "Message branch not found")
        seen.add(current)
        message = messages[current]
        result.append(message)
        current = (
            _uuid(message.parent_message_id) if message.parent_message_id else None
        )
    return list(reversed(result))


async def get_branch_path(
    db: AsyncSession, *, thread_id: UUID, head_message_id: UUID | None
) -> list[ChatMessage]:
    """Read visible ancestry after the caller authorizes the thread/document.

    Missing/deleted/tombstoned ancestors and cycles fail closed. A caller must
    never replace this failure with chronological or checkpoint history.
    """
    return _path(await _messages(db, thread_id), head_message_id)


async def _stamp_path(
    db: AsyncSession, thread: Thread, path: list[ChatMessage]
) -> list[tuple[UUID, datetime]]:
    latest: datetime | None = (
        await db.execute(
            select(func.max(ChatMessage.selected_at)).where(
                ChatMessage.thread_id == thread.id
            )
        )
    ).scalar_one_or_none()
    selected_at = datetime.now(timezone.utc)
    selected_at = selected_at.replace(
        microsecond=selected_at.microsecond // 1000 * 1000
    )
    if latest is not None:
        previous = _aware(latest)
        previous = previous.replace(microsecond=previous.microsecond // 1000 * 1000)
        # NOUS also preserves ordering for clients that convert to JavaScript
        # Date. The observed wire's raw-string ordering is handled separately
        # by the compatibility serializer's fixed UTC precision.
        selected_at = max(selected_at, previous + timedelta(milliseconds=1))
    for message in path:
        message.selected_at = selected_at
    thread.selected_message_id = path[-1].id if path else None
    await db.flush()
    return [(_uuid(message.id), selected_at) for message in path]


async def prepare_branch_submission(
    db: AsyncSession,
    *,
    actor_id: UUID,
    thread_id: UUID,
    document_id: UUID,
    parent_message_id: UUID | None,
) -> BranchSubmission:
    actor, thread = await authorize_branch_thread(
        db, actor_id=actor_id, thread_id=thread_id, lock=True
    )
    await _authorize_document(db, actor, document_id)
    if thread.message_branching:
        if thread.branch_document_id != document_id:
            raise BranchAccessError(409, "This chat belongs to another PDF")
    else:
        existing = await db.scalar(
            select(ChatMessage.id).where(ChatMessage.thread_id == thread_id).limit(1)
        )
        if existing is not None:
            raise BranchAccessError(409, "Start a new PDF chat to use message branches")
        thread.message_branching = True
        thread.branch_document_id = document_id
    if parent_message_id is not None:
        path = await get_branch_path(
            db, thread_id=thread_id, head_message_id=parent_message_id
        )
        if path[-1].role not in (MessageRole.USER, MessageRole.ASSISTANT):
            raise BranchAccessError(422, "A new question must follow a chat message")
    return BranchSubmission(
        thread, parent_message_id, _uuid(actor.id), _uuid(actor.organization_id)
    )


async def attach_user_branch(
    db: AsyncSession,
    binding: BranchSubmission,
    *,
    user_message_id: UUID,
    inserted: bool,
    content: str,
) -> None:
    messages = await _messages(db, _uuid(binding.thread.id))
    message = messages.get(user_message_id)
    if message is None or message.role != MessageRole.USER:
        raise BranchAccessError(404, "Message not found")
    if message.user_id != binding.actor_id or message.content != content:
        raise BranchAccessError(409, "Idempotency key belongs to another question")
    if not inserted and message.parent_message_id != binding.parent_message_id:
        raise BranchAccessError(409, "Idempotency key belongs to another branch")
    message.parent_message_id = binding.parent_message_id
    await _stamp_path(db, binding.thread, _path(messages, user_message_id))


async def reserve_branch_reply_capacity(
    db: AsyncSession, *, thread_id: UUID, client_message_id: UUID | None
) -> None:
    """Check question/reply capacity while acceptance holds the thread lock.

    Idempotent run replay must be resolved before this check. A deduplicated
    canonical question consumes no new slot, and an existing canonical reply
    likewise remains the sole answer rather than consuming another slot.
    """
    messages = await _messages(db, thread_id)
    existing_user = next(
        (
            message
            for message in messages.values()
            if client_message_id is not None
            and message.role == MessageRole.USER
            and str(message.client_message_id) == str(client_message_id)
        ),
        None,
    )
    existing_reply = existing_user is not None and any(
        message.role == MessageRole.ASSISTANT
        and message.parent_message_id == existing_user.id
        for message in messages.values()
    )
    required = int(existing_user is None) + int(not existing_reply)
    if len(messages) + required > MAX_BRANCH_MESSAGES:
        raise BranchAccessError(409, "Chat history has no room for another answer")


async def authorize_branch_resume(
    db: AsyncSession, *, actor_id: UUID, thread_id: UUID, run_id: str
) -> None:
    """Authorize the exact accepted branch before any checkpoint resume read."""
    actor, thread = await authorize_branch_thread(
        db, actor_id=actor_id, thread_id=thread_id
    )
    if not thread.message_branching:
        raise BranchAccessError(409, "Chat does not support message branches")
    await _authorize_document(db, actor, thread.branch_document_id)
    run: AgentRun | None = (
        await db.execute(
            select(AgentRun)
            .where(
                AgentRun.job_id == str(run_id),
                AgentRun.thread_id == thread_id,
                AgentRun.user_id == actor_id,
                AgentRun.organization_id == actor.organization_id,
                AgentRun.execution_provider == "nous",
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None or run.user_message_id is None:
        raise BranchAccessError(404, "Accepted question not found")
    path = await get_branch_path(
        db, thread_id=thread_id, head_message_id=_uuid(run.user_message_id)
    )
    if not path or path[-1].role != MessageRole.USER or path[-1].user_id != actor_id:
        raise BranchAccessError(404, "Accepted question not found")
    from src.services.agent.branch_history import load_branch_file_contexts

    try:
        await load_branch_file_contexts(
            db,
            thread_id=thread_id,
            message_ids=[message.id for message in path],
            actor_id=actor_id,
            organization_id=actor.organization_id,
            include_context=False,
        )
    except ValueError as exc:
        raise BranchAccessError(404, "A branch file is unavailable") from exc


async def prepare_assistant_branch(
    db: AsyncSession, *, thread_id: UUID, parent_message_id: UUID | None
) -> Thread | None:
    """Lock the branch and validate its accepted user; no transcript writes."""
    thread: Thread | None = (
        await db.execute(
            select(Thread)
            .where(Thread.id == thread_id)
            .with_for_update(of=Thread)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if thread is None or not thread.message_branching:
        return None
    if parent_message_id is None:
        raise BranchAccessError(409, "A PDF answer requires its accepted question")
    run: AgentRun | None = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.thread_id == thread_id,
                AgentRun.user_message_id == parent_message_id,
                AgentRun.execution_provider == "nous",
            )
        )
    ).scalar_one_or_none()
    if run is None or run.user_id is None:
        raise BranchAccessError(404, "Accepted question not found")
    actor, thread = await authorize_branch_thread(
        db, actor_id=_uuid(run.user_id), thread_id=thread_id
    )
    if actor.organization_id != run.organization_id:
        raise BranchAccessError(404, "Accepted question not found")
    await _authorize_document(db, actor, thread.branch_document_id)
    path = await get_branch_path(
        db, thread_id=thread_id, head_message_id=parent_message_id
    )
    if path[-1].role != MessageRole.USER or path[-1].user_id != actor.id:
        raise BranchAccessError(404, "Accepted question not found")
    return thread


async def link_assistant_branch(
    db: AsyncSession,
    thread: Thread,
    *,
    assistant_message_id: UUID,
    accepted_user_message_id: UUID,
) -> None:
    messages = await _messages(db, _uuid(thread.id))
    assistant = messages.get(assistant_message_id)
    if (
        assistant is None
        or assistant.role != MessageRole.ASSISTANT
        or assistant.parent_message_id != accepted_user_message_id
    ):
        raise BranchAccessError(409, "Answer belongs to another question")
    # A late idempotent replay may validate its row but cannot switch a newer
    # selected branch back to an earlier assistant.
    if thread.selected_message_id == accepted_user_message_id:
        await _stamp_path(db, thread, _path(messages, assistant_message_id))


async def select_branch(
    db: AsyncSession, *, actor_id: UUID, thread_id: UUID, message_id: UUID
) -> list[tuple[UUID, datetime]]:
    """Commit selection of an input variant and its previously selected tail."""
    try:
        actor, thread = await authorize_branch_thread(
            db, actor_id=actor_id, thread_id=thread_id, lock=True
        )
        if not thread.message_branching:
            raise BranchAccessError(409, "Chat does not support message branches")
        await _authorize_document(db, actor, thread.branch_document_id)
        active = await db.scalar(
            select(AgentRun.job_id)
            .where(
                AgentRun.thread_id == thread_id,
                AgentRun.status.in_(
                    [status.value for status in JobStatus if not status.is_terminal]
                ),
            )
            .limit(1)
        )
        if active is not None:
            raise BranchAccessError(
                409, "A response is already in progress for this thread"
            )
        messages = await _messages(db, thread_id)
        path = _path(messages, message_id)
        if path[-1].role != MessageRole.USER:
            raise BranchAccessError(422, "Select a user message")
        seen = {_uuid(message.id) for message in path}
        while True:
            children = [
                message
                for message in messages.values()
                if message.parent_message_id == path[-1].id
                and message.selected_at is not None
            ]
            if not children:
                break
            child = min(
                children,
                key=lambda message: (
                    -_aware(cast(datetime, message.selected_at)).timestamp(),
                    _aware(cast(datetime, message.created_at)),
                    str(message.id),
                ),
            )
            if _uuid(child.id) in seen:
                raise BranchAccessError(404, "Message branch not found")
            seen.add(_uuid(child.id))
            path.append(child)
        selected = await _stamp_path(db, thread, path)
        await db.commit()
        return selected
    except Exception:
        await db.rollback()
        raise

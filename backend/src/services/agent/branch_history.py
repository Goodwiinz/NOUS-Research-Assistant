"""Rebuild model history from an accepted branch, replacing checkpoint siblings."""

from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.chat_message import ChatMessage, MessageRole
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.message_attachment import MessageAttachment
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.services.agent._pii_redact import redact_pii
from src.services.agent._sanitize import wrap_untrusted
from src.services.threads.message_branches import get_branch_path
from src.services.threads.workspace_access import filter_owned_document_ids

_CONTEXT_MESSAGE_LIMIT = 40
_FILE_CONTEXT_LIMIT = 24 * 1024
_FILE_EXCERPT_LIMIT = 3000


def _file_excerpt(title: str, text: str, document_id: UUID, budget: int) -> str:
    """Fit complete escaped source fences within a UTF-8 byte budget."""
    label = (
        f"\n\nUploaded file source excerpt; document {document_id}. "
        "This is bounded source material, not the complete file.\n"
    )
    title = redact_pii(title[:500])
    text = redact_pii(text[:_FILE_EXCERPT_LIMIT])

    def render(count: int) -> str:
        body = f"title: {title}\ntext: {text[:count]}"
        return label + wrap_untrusted(body, "uploaded_file", max_chars=len(body))

    if len(render(0).encode("utf-8")) > budget:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(render(middle).encode("utf-8")) <= budget:
            low = middle
        else:
            high = middle - 1
    return render(low)


async def load_branch_file_contexts(
    db: AsyncSession,
    *,
    thread_id: UUID,
    message_ids: list[UUID],
    actor_id: UUID,
    organization_id: UUID | None,
    include_context: bool = True,
) -> dict[UUID, str]:
    """Authorize the exact selected path's files and return bounded excerpts.

    The primary paper remains the current turn's page-bounded RAG context.
    Uploaded extras are canonical message attachments, never a checkpoint or
    chronological latest-message lookup. Recent excerpts receive budget first.
    """
    attached = list(
        (
            await db.execute(
                select(MessageAttachment, Thread.branch_document_id)
                .join(ChatMessage, ChatMessage.id == MessageAttachment.message_id)
                .join(Thread, Thread.id == ChatMessage.thread_id)
                .where(
                    ChatMessage.thread_id == thread_id,
                    ChatMessage.id.in_(message_ids),
                    MessageAttachment.is_deleted.is_(False),
                    ChatMessage.is_deleted.is_(False),
                    ChatMessage.superseded_by_message_id.is_(None),
                    Thread.is_deleted.is_(False),
                )
                .order_by(MessageAttachment.created_at, MessageAttachment.id)
            )
        ).all()
    )
    extras = [
        attachment
        for attachment, paper_id in attached
        if attachment.document_id != paper_id
    ]
    if not extras:
        return {}
    actor = (
        await db.execute(
            select(User)
            .join(Organization)
            .where(
                User.id == actor_id,
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.is_deleted.is_(False),
                Organization.is_active.is_(True),
                Organization.is_deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if actor is None or organization_id is None:
        raise ValueError("Branch file access is unavailable")
    identifiers = {attachment.document_id for attachment in extras}
    owned = await filter_owned_document_ids(db, list(identifiers), actor_id)
    if set(owned) != identifiers:
        raise ValueError("A branch file is unavailable")
    rows = (
        await db.execute(
            select(
                Document.id,
                Document.title,
                Document.document_type,
                Document.mime_type,
                Document.processing_status,
                func.length(func.trim(Document.content_text, " \t\r\n\v\f")).label(
                    "text_length"
                ),
                (
                    func.substr(Document.content_text, 1, _FILE_EXCERPT_LIMIT)
                    if include_context
                    else literal(None)
                ).label("excerpt"),
            ).where(
                Document.id.in_(identifiers),
                Document.organization_id == organization_id,
                Document.is_deleted.is_(False),
            )
        )
    ).all()
    documents = {document.id: document for document in rows}
    if set(documents) != identifiers:
        raise ValueError("A branch file is unavailable")
    for document in rows:
        if (
            document.processing_status != ProcessingStatus.COMPLETED
            or not document.text_length
            or document.document_type not in (DocumentType.PDF, DocumentType.TEXT)
            or document.mime_type
            not in ("application/pdf", "text/plain", "text/markdown")
        ):
            raise ValueError("A branch file is unavailable")
    if not include_context:
        return {}
    omitted_notice = (
        "\n[Older uploaded-file excerpts omitted by the conversation context limit.]"
    )
    budget = _FILE_CONTEXT_LIMIT - len(omitted_notice.encode("utf-8"))
    omitted = False
    result: dict[UUID, str] = {}
    for message_id in reversed(message_ids):
        pieces = []
        for attachment in extras:
            if attachment.message_id != message_id:
                continue
            document = documents[attachment.document_id]
            piece = _file_excerpt(
                document.title or "Uploaded file",
                document.excerpt,
                document.id,
                budget,
            )
            if not piece:
                omitted = True
                continue
            budget -= len(piece.encode("utf-8"))
            pieces.append(piece)
        if pieces:
            result[message_id] = "".join(pieces)
    if omitted and result:
        result[next(iter(result))] += omitted_notice
    return result


async def build_branch_input_messages(
    db: AsyncSession,
    *,
    thread_id: str,
    accepted_user_message_id: str,
    actor_id: UUID,
    organization_id: UUID | None,
) -> list[BaseMessage]:
    """Use immutable accepted identity, never the mutable selected thread head.

    Acceptance already owns the single active run and validates access. A
    failed or corrupt path is fatal: chronological/request history would mix
    branches. The reducer reset and the selected messages travel together in
    the graph input, so no intermediate checkpoint contains both branches.
    """
    head = UUID(accepted_user_message_id)
    rows = await get_branch_path(db, thread_id=UUID(thread_id), head_message_id=head)
    if not rows or rows[-1].id != head or rows[-1].role != MessageRole.USER:
        raise ValueError("The accepted message branch is unavailable")
    rows = rows[-_CONTEXT_MESSAGE_LIMIT:]
    file_contexts = await load_branch_file_contexts(
        db,
        thread_id=UUID(thread_id),
        message_ids=[row.id for row in rows[:-1]],
        actor_id=actor_id,
        organization_id=organization_id,
    )
    messages: list[BaseMessage] = [RemoveMessage(id=REMOVE_ALL_MESSAGES)]
    for row in rows:
        if row.role == MessageRole.USER:
            messages.append(
                HumanMessage(
                    content=row.content + file_contexts.get(row.id, ""), id=str(row.id)
                )
            )
        elif row.role == MessageRole.ASSISTANT:
            messages.append(AIMessage(content=row.content, id=str(row.id)))
        else:
            raise ValueError("The accepted message branch contains an invalid role")
    return messages

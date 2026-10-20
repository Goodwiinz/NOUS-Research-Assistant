"""Durable alphaXiv live projection over the canonical NOUS run ledger.

The producer records only bounded presentation facts, before advancing its
native generator. Native lifecycle events supply the terminal result; this
module never reopens a terminal ledger or writes a second transcript. Dense
wire sequences are derived from the deterministic projection, not ledger seq.
"""

import hashlib
import json
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import AsyncSessionLocal
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.chat_message import MessageRole
from src.models.document import DocumentType, ProcessingStatus
from src.models.message_attachment import MessageAttachment
from src.models.user import User
from src.schemas.alphaxiv import AlphaXivChatFields
from src.services.agent._pii_redact import redact_pii
from src.services.agent.agent_run_service import get_latest_run_for_thread, get_run
from src.services.agent.chat_presentation_types import ChatPresentationStarted
from src.services.agent.run_event_store import append_event, read_events
from src.services.agent.run_event_types import TERMINAL_RUN_EVENTS, RunEventType
from src.services.agent.schemas import PageFilterResponse
from src.services.compat.alphaxiv import CompatibilityError, EventProjection
from src.services.compat.alphaxiv_files import (
    authorize_files,
    document_content_type,
    file_url,
    reference_id,
)
from src.services.compat.alphaxiv_stop import authorize_pdf_chat
from src.services.threads.workspace_access import (
    get_accessible_document_or_none,
    get_message,
)

_READ_PAGE = 500
_MAX_LEDGER_ROWS = 50000
_MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024


async def _owned_run(
    db: AsyncSession, *, actor_id: UUID, chat_id: UUID, run_id: UUID, write: bool
) -> tuple[User, AgentRun]:
    user = await authorize_pdf_chat(
        db, actor_id=actor_id, chat_id=chat_id, require_edit=write
    )
    run = await get_run(
        db,
        str(run_id),
        organization_id=user.organization_id,
        user_id=user.id,
        for_update=write,
    )
    if (
        run is None
        or str(run.thread_id) != str(chat_id)
        or run.execution_provider != "nous"
    ):
        raise CompatibilityError(404, "Run not found")
    return user, run


async def _read_ledger(
    db: AsyncSession, run: AgentRun, user: User
) -> list[AgentRunEvent]:
    rows: list[AgentRunEvent] = []
    after = 0
    while True:
        page = await read_events(
            db,
            str(run.job_id),
            organization_id=user.organization_id,
            user_id=user.id,
            after_seq=after,
            limit=_READ_PAGE,
        )
        if not page:
            return rows
        rows.extend(page)
        if len(rows) > _MAX_LEDGER_ROWS:
            raise CompatibilityError(503, "Live replay exceeds its supported size")
        after = int(page[-1].seq)
        if len(page) < _READ_PAGE:
            return rows


def _header(rows: list[AgentRunEvent]) -> dict[str, Any] | None:
    for row in rows:
        if row.event_type == RunEventType.CHAT_PRESENTATION_STARTED.value:
            metadata: dict[str, Any] = ChatPresentationStarted.model_validate(
                row.payload
            ).model_dump(mode="json")
            return metadata
    return None


async def _authorize_paper(db: AsyncSession, user: User, paper_id: UUID) -> None:
    paper = await get_accessible_document_or_none(
        db,
        paper_id,
        UUID(str(user.id)),
        UUID(str(user.organization_id)) if user.organization_id else None,
    )
    if paper is None or paper.document_type != DocumentType.PDF:
        raise CompatibilityError(404, "Document not found")
    if paper.processing_status != ProcessingStatus.COMPLETED:
        raise CompatibilityError(409, "PDF processing is not complete")


async def _user_content(
    db: AsyncSession, run: AgentRun, user: User, chat_id: UUID
) -> str:
    if run.user_message_id is None:
        raise CompatibilityError(404, "Message not found")
    message = await get_message(
        db, UUID(str(run.user_message_id)), UUID(str(user.id)), thread_id=chat_id
    )
    if (
        message is None
        or message.role != MessageRole.USER
        or not isinstance(message.content, str)
    ):
        raise CompatibilityError(404, "Message not found")
    return message.content


async def start_live_run(
    *, actor_id: UUID, chat_id: UUID, run_id: UUID, body: AlphaXivChatFields
) -> None:
    """Atomically bind one accepted run to a replayable presentation.

    The owned run row lock makes retries idempotent. Request content must
    match the already committed canonical user message; it is never copied
    into the ledger.
    """
    async with AsyncSessionLocal() as db:
        user, run = await _owned_run(
            db, actor_id=actor_id, chat_id=chat_id, run_id=run_id, write=True
        )
        await _authorize_paper(db, user, body.paper_version_id)
        file_ids = [reference_id(item.url) for item in body.files]
        await authorize_files(db, user=user, document_ids=file_ids)
        if file_ids:
            from sqlalchemy import select

            attached = set(
                (
                    await db.scalars(
                        select(MessageAttachment.document_id).where(
                            MessageAttachment.message_id == run.user_message_id,
                            MessageAttachment.is_deleted.is_(False),
                        )
                    )
                ).all()
            )
            if attached != {body.paper_version_id, *file_ids}:
                raise CompatibilityError(409, "The accepted files changed")
        content = await _user_content(db, run, user, chat_id)
        if content != body.message:
            raise CompatibilityError(409, "The accepted message changed")
        metadata = ChatPresentationStarted(
            paper_id=body.paper_version_id,
            user_content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            model=body.model,
            thinking=body.thinking,
            web_search="full" if body.web_search == "full" else "off",
            selection_page_range=body.selection_page_range,
            parent_message_id=body.parent_message_id,
            file_ids=file_ids,
        ).model_dump(mode="json")
        previous = _header(await _read_ledger(db, run, user))
        if previous is not None:
            if previous != metadata:
                raise CompatibilityError(409, "The live presentation changed")
            return
        await append_event(
            db,
            run_id=str(run_id),
            organization_id=user.organization_id,
            event_type=RunEventType.CHAT_PRESENTATION_STARTED,
            payload=metadata,
        )
        await db.commit()


def _presentation_frames(kind: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if kind in {"token", "reasoning_delta", "status"}:
        text = redact_pii(
            str(payload.get("detail" if kind == "status" else "content", ""))
        )
        if kind == "status":
            return [{"kind": kind, "text": text[:1000]}]
        return [
            {"kind": kind, "text": text[offset : offset + 1000]}
            for offset in range(0, len(text), 1000)
        ]
    if kind == "rag_context":
        frames: list[dict[str, Any]] = [{"kind": "context_reset"}]
        for item in payload.get("contexts", []):
            if not isinstance(item, dict):
                continue
            position, page = item.get("source_position"), item.get("page_number")
            if (
                type(position) is not int
                or position < 1
                or type(page) is not int
                or page < 1
            ):
                continue
            try:
                document_id = str(UUID(str(item.get("document_id"))))
            except ValueError:
                continue
            frames.append(
                {
                    "kind": "context_item",
                    "source_position": position,
                    "document_id": document_id,
                    "page_number": page,
                    "title": redact_pii(str(item.get("title") or f"Page {page}"))[:500],
                }
            )
        metadata = payload.get("page_filter")
        projected = (
            PageFilterResponse.model_validate(metadata).model_dump(mode="json")
            if metadata is not None
            else None
        )
        frames.append({"kind": "context_complete", "page_filter": projected})
        return frames
    if kind == "usage":
        return [
            {
                "kind": kind,
                "input_tokens": payload.get("input_tokens"),
                "output_tokens": payload.get("output_tokens"),
            }
        ]
    if kind in {"confirmation", "approval_required"}:
        return [{"kind": kind}]
    return []


async def record_live_frame(
    *, actor_id: UUID, chat_id: UUID, run_id: UUID, kind: str, payload: dict[str, Any]
) -> None:
    """Persist a complete native presentation frame before its next pull.

    Lifecycle terminal frames are deliberately ignored: native finalization
    has already committed, and replay derives the terminal from that fact.
    Each source snapshot is atomic even when split into bounded ledger rows.
    """
    frames = _presentation_frames(kind, payload)
    if not frames:
        return
    async with AsyncSessionLocal() as db:
        user, run = await _owned_run(
            db, actor_id=actor_id, chat_id=chat_id, run_id=run_id, write=True
        )
        first = await read_events(
            db,
            str(run_id),
            organization_id=user.organization_id,
            user_id=user.id,
            limit=_READ_PAGE,
        )
        metadata = _header(first)
        if metadata is None:
            raise CompatibilityError(409, "The live presentation was not started")
        await _authorize_paper(db, user, UUID(metadata["paper_id"]))
        for frame in frames:
            await append_event(
                db,
                run_id=str(run_id),
                organization_id=user.organization_id,
                event_type=RunEventType.CHAT_PRESENTATION_FRAME,
                payload={"frame": frame},
            )
        await db.commit()


def _project_events(
    rows: list[AgentRunEvent],
    metadata: dict[str, Any],
    content: str,
    files: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    projection = EventProjection(metadata["paper_id"])
    events: list[dict[str, Any]] = [
        {
            "type": "run_start",
            "paperVersionId": metadata["paper_id"],
            "userContent": content,
            "userFiles": files or [],
            "model": metadata["model"],
            "thinking": metadata.get("thinking"),
            "webSearch": metadata["web_search"],
            "parentMessageId": metadata.get("parent_message_id"),
            "selectionPageRange": metadata["selection_page_range"],
        }
    ]
    contexts: list[dict[str, Any]] = []
    for row in rows:
        if row.event_type == RunEventType.CHAT_PRESENTATION_FRAME.value:
            frame = dict(row.payload["frame"])
            kind = frame.pop("kind")
            if kind == "context_reset":
                contexts = []
            elif kind == "context_item":
                contexts.append(frame)
            elif kind == "context_complete":
                events.extend(
                    projection.project(
                        "rag_context",
                        {"contexts": contexts, "page_filter": frame.get("page_filter")},
                    )
                )
            elif kind in {"token", "reasoning_delta", "status"}:
                events.extend(
                    projection.project(
                        kind,
                        {"detail" if kind == "status" else "content": frame["text"]},
                    )
                )
            else:
                events.extend(projection.project(kind, frame))
        elif row.event_type in TERMINAL_RUN_EVENTS:
            if row.event_type == RunEventType.RUN_COMPLETED.value:
                events.extend(projection.project("done", {}))
            elif row.event_type == RunEventType.RUN_CANCELLED.value:
                events.extend(projection.project("cancelled", {}))
            else:
                events.extend(
                    projection.project("error", {"error": "The response failed"})
                )
    return events


async def read_live_snapshot(*, actor_id: UUID, chat_id: UUID) -> dict[str, Any] | None:
    """Read a fresh authorized snapshot. Redis and process memory are optional."""
    async with AsyncSessionLocal() as db:
        user = await authorize_pdf_chat(
            db, actor_id=actor_id, chat_id=chat_id, require_edit=False
        )
        run = await get_latest_run_for_thread(
            db,
            chat_id,
            organization_id=user.organization_id,
            user_id=user.id,
            by_creation=True,
        )
        if run is None or run.execution_provider != "nous":
            return None
        rows = await _read_ledger(db, run, user)
        metadata = _header(rows)
        if metadata is None:
            return None
        await _authorize_paper(db, user, UUID(metadata["paper_id"]))
        documents = await authorize_files(
            db,
            user=user,
            document_ids=[UUID(item) for item in metadata.get("file_ids", [])],
        )
        content = await _user_content(db, run, user, chat_id)
        if (
            hashlib.sha256(content.encode()).hexdigest()
            != metadata["user_content_sha256"]
        ):
            raise CompatibilityError(409, "The accepted message changed")
        events = _project_events(
            rows,
            metadata,
            content,
            [
                {
                    "contentType": document_content_type(item),
                    "url": file_url(item.id),
                }
                for item in documents
            ],
        )
        snapshot = {
            "runId": str(run.job_id),
            "seq": len(events),
            "events": events,
        }
        if len(json.dumps(snapshot, ensure_ascii=False).encode()) > _MAX_SNAPSHOT_BYTES:
            raise CompatibilityError(503, "Live replay exceeds its supported size")
        return snapshot


async def read_live_update(
    *, actor_id: UUID, chat_id: UUID, known_run_id: UUID | None, after_seq: int
) -> dict[str, Any]:
    if after_seq < 0:
        raise CompatibilityError(422, "Invalid live cursor")
    snapshot = await read_live_snapshot(actor_id=actor_id, chat_id=chat_id)
    if snapshot is None and known_run_id is None:
        # Still no presentation run (a new chat, or one last answered
        # natively): the client already holds this empty view.
        return {"type": "delta", "payloads": []}
    if (
        snapshot is None
        or snapshot["runId"] != str(known_run_id)
        or after_seq > len(snapshot["events"])
    ):
        return {"type": "replace", "payload": snapshot}
    events = cast(list[dict[str, Any]], snapshot["events"])
    return {
        "type": "delta",
        "payloads": [
            {"runId": snapshot["runId"], "seq": index + 1, "event": item}
            for index, item in enumerate(events)
            if index >= after_seq
        ],
    }

"""Map the observed alphaXiv v2 surface onto native NOUS PDF chat.

The native producer remains the sole writer. This module neither generates
answers nor inserts messages. Event names follow the shipped client inspected
in the alphaXiv investigation; unsupported execution options fail explicitly.
"""

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document import DocumentType, ProcessingStatus
from src.models.user import User
from src.schemas.alphaxiv import AlphaXivChatFields
from src.services.agent.agent_execution_service import (
    AgentThreadResolutionError,
    _resolve_thread,
)
from src.services.agent.reasoning import validate_reasoning_override
from src.services.agent.schemas import (
    SUPPORTED_MODELS,
    AgentExecuteRequest,
    AgentMessage,
    PageContextRequest,
)
from src.services.threads.workspace_access import get_accessible_document_or_none


class CompatibilityError(ValueError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


async def prepare_request(
    body: AlphaXivChatFields, db: AsyncSession, user: User
) -> AgentExecuteRequest:
    """Validate capability/access before starting the canonical producer."""
    unsupported = {
        "webSearch": body.web_search not in (None, "off", "full"),
        "filterModel": body.filter_model not in (None, "cohere"),
        "customFilter": body.custom_filter is not None,
        "overviewId": body.overview_id is not None,
        "assistantVariant": body.assistant_variant not in (None, "paper"),
    }
    for name, present in unsupported.items():
        if present:
            raise CompatibilityError(422, f"{name} is not supported by this adapter")
    # The observed preset selects NOUS's configured default deployment. Keep
    # body.model intact for the hosted-shaped presentation; no model equivalence
    # to alphaXiv's private "pro" routing is implied by this local mapping.
    native_model = "" if body.model == "pro" else body.model
    if native_model not in SUPPORTED_MODELS:
        raise CompatibilityError(422, "Unsupported NOUS model")

    try:
        reasoning_effort = validate_reasoning_override(
            body.thinking, model=native_model, document_only=body.web_search != "full"
        )
    except ValueError as exc:
        raise CompatibilityError(422, str(exc)) from exc

    document = await get_accessible_document_or_none(
        db,
        body.paper_version_id,
        UUID(str(user.id)),
        UUID(str(user.organization_id)) if user.organization_id else None,
    )
    if document is None:
        raise CompatibilityError(404, "Document not found")
    if document.document_type != DocumentType.PDF:
        raise CompatibilityError(422, "paperVersionId must identify a PDF document")
    if document.processing_status != ProcessingStatus.COMPLETED:
        raise CompatibilityError(409, "PDF processing is not complete")

    # Import here because the file adapter shares this module's public error.
    from src.services.compat.alphaxiv_files import normalize_files

    file_ids, normalized_files = await normalize_files(
        db, user=user, paper_id=body.paper_version_id, files=body.files
    )
    body.files = normalized_files

    native: AgentExecuteRequest = AgentExecuteRequest.model_validate(
        {
            "messages": [
                AgentMessage(
                    role="user", content=body.message, client_message_id=uuid4()
                )
            ],
            "model": native_model,
            "reasoning_effort": reasoning_effort,
            "page_context": PageContextRequest(type="document"),
            "thread_id": str(body.llm_chat_id) if body.llm_chat_id else None,
            "attachment_ids": [body.paper_version_id, *file_ids],
            "selection_page_range": body.selection_page_range,
            # Match native PDF-chat intent without changing other chat defaults.
            # A manual range is authoritative and never invokes the ranker.
            "page_filter_model": (
                None
                if body.selection_page_range is not None
                else body.filter_model or "cohere"
            ),
            "document_only": body.web_search != "full",
            "branch_document_id": body.paper_version_id,
            "parent_message_id": body.parent_message_id,
            "use_rag": True,
        }
    )
    if body.llm_chat_id is not None:
        try:
            thread, _ = await _resolve_thread(db, user, native, create_if_missing=False)
        except AgentThreadResolutionError as exc:
            raise CompatibilityError(404, "Chat not found") from exc
        if thread is None or str(thread.id) != str(body.llm_chat_id):
            raise CompatibilityError(404, "Chat not found")
    return native


def decode_native_frame(frame: str) -> tuple[str, dict[str, Any]]:
    """Native producer yields complete frames, never raw network chunks."""
    kind = ""
    data: list[str] = []
    for line in frame.splitlines():
        if line.startswith("event:"):
            kind = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    payload = json.loads("\n".join(data))
    if not kind or not isinstance(payload, dict):
        raise ValueError("Invalid native stream frame")
    return kind, payload


def encode_event(event: dict[str, Any]) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


@dataclass
class EventProjection:
    """Stateful wire projection; sparse source citations survive token splits."""

    paper_id: str
    terminal: bool = False
    next_index: int = 0
    last_kind: str = ""
    last_index: int = -1
    pending_text: str = ""
    contexts: dict[int, dict[str, Any]] = field(default_factory=dict)

    def _content(self, kind: str, delta: str) -> list[dict[str, Any]]:
        if not delta:
            return []
        if self.last_kind != kind:
            self.last_kind = kind
            self.last_index = self.next_index
            self.next_index += 1
        return [{"type": kind, "index": self.last_index, "delta": delta}]

    def _citation(self, match: re.Match[str]) -> str:
        context = self.contexts.get(int(match.group(1)))
        if context is None:
            return match.group(0)
        page = context.get("page_number")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            return match.group(0)
        paper = context.get("document_id")
        if paper != self.paper_id:
            return match.group(0)
        title = html.escape(str(context.get("title") or f"Page {page}"), quote=True)
        return (
            f'<alphaxiv-paper-citation paper="{html.escape(paper, quote=True)}" '
            f'page="{page}" title="{title}" />'
        )

    def _text(self, delta: str, *, flush: bool = False) -> list[dict[str, Any]]:
        value = self.pending_text + delta
        self.pending_text = ""
        if not flush:
            offset = value.rfind("[")
            tail = value[offset:] if offset >= 0 else ""
            if tail and (
                "[Doc ".startswith(tail) or re.fullmatch(r"\[(?:Doc )?\d{0,5}", tail)
            ):
                self.pending_text = tail
                value = value[:offset]
        return self._content(
            "delta_output_text",
            re.sub(r"\[(?:Doc )?(\d{1,5})\]", self._citation, value),
        )

    def project(self, kind: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if self.terminal:
            return []
        if kind == "token":
            return self._text(str(payload.get("content", "")))
        if kind == "reasoning_delta":
            return self._text("", flush=True) + self._content(
                "delta_output_reasoning", str(payload.get("content", ""))
            )
        if kind == "status":
            return [{"type": "assistant_status", "label": payload.get("detail", "")}]
        if kind == "rag_context":
            # Native events are complete snapshots, including an explicit []
            # when a later node clears the sources for this turn.
            self.contexts = {}
            for context in payload.get("contexts", []):
                if isinstance(context, dict):
                    position = context.get("source_position")
                    if (
                        isinstance(position, int)
                        and not isinstance(position, bool)
                        and position > 0
                    ):
                        self.contexts[position] = context
            metadata = payload.get("page_filter")
            if not isinstance(metadata, dict) or not isinstance(
                metadata.get("timing_ms"), dict
            ):
                # The observed debug renderer dereferences timingMs eagerly.
                # Do not invent measurements or emit a client-crashing shape.
                return []
            event = {
                "type": "paper_filter_debug",
                "acceptedPages": metadata.get("accepted_pages", []),
                "totalPages": metadata.get("total_pages"),
                "shortCircuited": metadata.get("short_circuited"),
                "filterModel": metadata.get("model"),
                "timingMs": metadata["timing_ms"],
                "acceptedPagePriorities": [
                    {"page": item["page"], "priority": item["score"]}
                    for item in metadata.get("priorities", [])
                ],
            }
            return [event]
        if kind == "usage":
            return [
                {
                    "type": "token_usage",
                    "inputTokens": payload.get("input_tokens"),
                    "outputTokens": payload.get("output_tokens"),
                }
            ]
        if kind in {"done", "error", "confirmation", "approval_required", "cancelled"}:
            events = self._text("", flush=True)
            self.terminal = True
            completed = kind == "done"
            if not completed and kind != "cancelled":
                message = (
                    payload.get("error", "The response failed")
                    if kind == "error"
                    else "This response requires approval in NOUS"
                )
                events.append({"type": "error", "error": message})
            events.append(
                {
                    "type": "run_end",
                    "kind": (
                        "completed"
                        if completed
                        else "aborted" if kind == "cancelled" else "error"
                    ),
                }
            )
            return events
        # Native traces/plans have no validated alphaXiv content equivalent.
        return []

    def finish(self) -> list[dict[str, Any]]:
        if self.terminal:
            return []
        return self.project("error", {"error": "The response ended before completion"})

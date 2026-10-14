"""alphaXiv-shaped v2 SSE transport with NOUS bearer authentication."""

import logging
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from src.api.agent import execute, streaming
from src.core.database import AsyncSessionLocal
from src.core.dependencies import get_current_user
from src.middleware.disconnect_signal import AGENT_DISCONNECT_EVENT
from src.models.user import User
from src.schemas.alphaxiv import AlphaXivChatRequest
from src.services.compat.alphaxiv import (
    CompatibilityError,
    EventProjection,
    decode_native_frame,
    encode_event,
    prepare_request,
)

router = APIRouter(tags=["alphaXiv compatibility"])
logger = logging.getLogger(__name__)


class _NativeRequest:
    """Keep disconnect visibility across the pre-header/body task handoff.

    Native streaming normally captures the executing task to cancel it on
    disconnect. Priming happens in the route task, while StreamingResponse
    can consume the remainder in a child task. Let ASGI own task cancellation
    here, and expose the shared disconnect signal through polling only.
    """

    def __init__(self, request: Request) -> None:
        self.request = request
        self.headers = request.headers
        self.state = SimpleNamespace(
            request_id=getattr(request.state, "request_id", None)
        )

    async def is_disconnected(self) -> bool:
        signal = getattr(self.request.state, AGENT_DISCONNECT_EVENT, None)
        if signal is not None:
            return bool(signal.is_set())
        return bool(await self.request.is_disconnected())


class _NativeStreamResponse(StreamingResponse):
    """Close a primed producer even when ASGI fails before the first body pull."""

    def __init__(self, *args: Any, source: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.source = source

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await streaming._run_interrupted_cleanup(self.source.aclose)


@router.post(
    "/assistant/v2/chat",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "alphaXiv v2 SSE over a durable NOUS PDF chat",
            "headers": {
                "llm-chat-id": {"schema": {"type": "string", "format": "uuid"}}
            },
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def chat_v2(
    body: AlphaXivChatRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    started_at = time.monotonic()
    await execute._enforce_rate_limit(
        execute._agent_rate_limiter, str(current_user.id), execute._AGENT_TURN_PREFIX
    )
    try:
        # The preflight uses a separate short-lived read session; do not pin a
        # database connection while StreamingResponse is serving the answer.
        async with AsyncSessionLocal() as db:
            native = await prepare_request(body, db, current_user)
    except CompatibilityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    # Use the native producer's own DB session for the long-lived stream.
    # No message is created by this transport; even acceptance belongs to it.
    source = streaming.stream_event_generator(
        native, _NativeRequest(request), current_user, request_started_at=started_at
    )
    try:
        first = await anext(source)
        kind, accepted = decode_native_frame(first)
        if kind != "status" or accepted.get("phase") != "accepted":
            category = accepted.get("category")
            status = {"conflict": 409, "invalid_request": 422}.get(str(category), 503)
            raise HTTPException(status, "The response could not be started")
        chat_id = str(UUID(str(accepted["thread_id"])))
        UUID(str(accepted["run_id"]))
        if body.llm_chat_id is not None and chat_id != str(body.llm_chat_id):
            raise HTTPException(409, "The chat identity changed")
    except (KeyError, ValueError, StopAsyncIteration) as exc:
        await streaming._run_interrupted_cleanup(source.aclose)
        raise HTTPException(409, "A durable NOUS chat workspace is required") from exc
    except BaseException:
        await streaming._run_interrupted_cleanup(source.aclose)
        raise

    projection = EventProjection(paper_id=str(body.paper_version_id))

    async def response_events() -> AsyncIterator[str]:
        try:
            yield encode_event(
                {
                    "type": "run_start",
                    "paperVersionId": str(body.paper_version_id),
                    "userContent": body.message,
                    "userFiles": [
                        item.model_dump(by_alias=True) for item in body.files
                    ],
                    "model": body.model,
                    "webSearch": body.web_search or "off",
                    "parentMessageId": (
                        str(body.parent_message_id) if body.parent_message_id else None
                    ),
                    "selectionPageRange": body.selection_page_range,
                }
            )
            for event in projection.project(kind, accepted):
                yield encode_event(event)
            async for frame in source:
                event_kind, payload = decode_native_frame(frame)
                if event_kind == "heartbeat":
                    # Preserve native idle protection without adding an
                    # unrecognized content event to the alphaXiv consumer.
                    yield ": keepalive\n\n"
                    continue
                for event in projection.project(event_kind, payload):
                    yield encode_event(event)
            for event in projection.finish():
                yield encode_event(event)
        except Exception:
            logger.exception("alphaXiv compatibility stream failed")
            # EOF is success to the observed v2 consumer. A projection or
            # transport-adapter failure must therefore be an explicit event.
            for event in projection.project(
                "error", {"error": "The response could not be completed"}
            ):
                yield encode_event(event)
        finally:
            await streaming._run_interrupted_cleanup(source.aclose)

    return _NativeStreamResponse(
        response_events(),
        source=source,
        media_type="text/event-stream",
        headers={**execute._SSE_HEADERS, "llm-chat-id": chat_id},
    )

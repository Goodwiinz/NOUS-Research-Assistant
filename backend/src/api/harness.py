"""Authenticated WSS bridge transport; delivery service owns transactions."""

import asyncio
import json
from datetime import timezone
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.integrations.auth import require_interactive_user
from src.core.database import AsyncSessionLocal, get_db
from src.core.dependencies import get_current_user
from src.core.websocket_auth import WebSocketAuthenticator, WebSocketAuthError
from src.models.integration_grant import IntegrationGrant
from src.models.user import User
from src.schemas.harness import BridgeEvent, NativeRequestDTO
from src.services.harness.approvals import (
    NativeDecision,
    NativeRequestConflict,
    get_native_request,
    ingest_native_request,
    ingest_native_response_ack,
    native_request_context,
    resolve_native_request,
)
from src.services.harness.delivery import (
    ingest_bridge_event,
    lease_commands,
    lease_runs,
)
from src.services.integrations.context import (
    IntegrationAccessDenied,
    resolve_integration_context,
)

router = APIRouter(prefix="/harness", tags=["harness"])


@router.get("/requests/{request_id}", response_model=NativeRequestDTO)
async def read_native_request(
    request_id: UUID,
    user: User = Depends(require_interactive_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    try:
        request = await get_native_request(db, request_id, user.id)
    except IntegrationAccessDenied as error:
        raise HTTPException(404, "Native request not found") from error
    return {
        "id": request["id"],
        "runId": request["run_id"],
        "method": request["method"],
        "target": request["target"],
        "targetHash": request["target_hash"],
        "expiresAt": request["expires_at"],
        "consumed": request["consumed"],
        "expired": request["expired"],
    }


@router.post("/requests/{request_id}/decision", status_code=204)
async def decide_native_request(
    request_id: UUID,
    decision: NativeDecision,
    user: User = Depends(require_interactive_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        display = await get_native_request(db, request_id, user.id)
        if (
            decision.target_hash is None
            or decision.target_hash != display["target_hash"]
        ):
            raise NativeRequestConflict("Displayed native target changed")
        context = await native_request_context(db, request_id, user.id)
        if context.organization_id != user.organization_id:
            raise IntegrationAccessDenied()
        await resolve_native_request(db, context, request_id, decision)
    except IntegrationAccessDenied as error:
        raise HTTPException(404, "Native request not found") from error
    except NativeRequestConflict as error:
        raise HTTPException(409, "Native request is no longer available") from error
    return Response(status_code=204)


@router.websocket("/connect")
async def connect(websocket: WebSocket) -> None:
    try:
        if websocket.url.scheme != "wss":
            raise IntegrationAccessDenied()
        identity = await WebSocketAuthenticator.authenticate(websocket)
        token = websocket.headers.get("x-nous-integration-grant", "")
        async with AsyncSessionLocal() as db:
            context = await resolve_integration_context(
                db, token, required_scope="harness:execute"
            )
            if str(context.user_id) != str(identity.get("sub")):
                raise IntegrationAccessDenied()
        await websocket.accept(
            subprotocol=WebSocketAuthenticator.get_subprotocol_response(websocket)
        )
        while True:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=45)
            if len(raw.encode()) > 16 * 1024:
                raise ValueError("frame too large")
            value = json.loads(raw)
            # Recheck access JWT and database grant on every action, including polls.
            identity = await WebSocketAuthenticator.authenticate(websocket)
            async with AsyncSessionLocal() as db:
                context = await resolve_integration_context(
                    db, token, required_scope="harness:execute"
                )
                if str(context.user_id) != str(identity.get("sub")):
                    raise IntegrationAccessDenied()
                if (
                    isinstance(value, dict)
                    and set(value) == {"poll", "deviceId"}
                    and value["poll"] is True
                ):
                    commands = await lease_commands(
                        db, context, UUID(value["deviceId"])
                    )
                    grant = await db.get(
                        IntegrationGrant, context.grant_id, populate_existing=True
                    )
                    assert grant is not None
                    lease = {
                        "deviceId": str(grant.device_id),
                        "runs": await lease_runs(db, context, UUID(value["deviceId"])),
                        "expiresAt": grant.expires_at.replace(
                            tzinfo=timezone.utc
                        ).isoformat(),
                    }
                    # One bounded command per frame; a device may own many
                    # workspaces but cannot force a multi-megabyte replay frame.
                    if not commands:
                        await websocket.send_json({"commands": [], "lease": lease})
                    for command in commands:
                        await websocket.send_json(
                            {
                                "commands": [command.model_dump(mode="json")],
                                "lease": lease,
                            }
                        )
                else:
                    event = BridgeEvent.model_validate(value)
                    if getattr(event.body, "kind", None) == "request":
                        request_id = await ingest_native_request(db, context, event)
                        seq = 0
                    elif getattr(event.body, "kind", None) == "command_ack":
                        request_id = event.body.approvalRecordId
                        seq = await ingest_native_response_ack(db, context, event)
                    else:
                        request_id = None
                        seq = await ingest_bridge_event(db, context, event)
                    await websocket.send_json(
                        {
                            "ack": {
                                "runId": str(event.runId),
                                "sourceId": event.sourceId,
                                "sourceSeq": event.sourceSeq,
                                "generation": event.generation,
                                "canonicalSeq": seq,
                                **(
                                    {"approvalRecordId": str(request_id)}
                                    if request_id
                                    else {}
                                ),
                            }
                        }
                    )
    except WebSocketDisconnect:
        return
    except (IntegrationAccessDenied, WebSocketAuthError):
        await websocket.close(code=4403, reason="Bridge authorization denied")
    except (ValueError, NativeRequestConflict, ValidationError, asyncio.TimeoutError):
        await websocket.close(
            code=4400, reason="Invalid bridge frame or expired connection"
        )

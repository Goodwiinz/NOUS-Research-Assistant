"""Authenticated WSS bridge transport; delivery service owns transactions."""

import asyncio
import json
from datetime import timezone
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from src.core.database import AsyncSessionLocal
from src.core.websocket_auth import WebSocketAuthenticator, WebSocketAuthError
from src.models.integration_grant import IntegrationGrant
from src.schemas.harness import BridgeEvent
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
                    seq = await ingest_bridge_event(db, context, event)
                    await websocket.send_json(
                        {
                            "ack": {
                                "runId": str(event.runId),
                                "sourceId": event.sourceId,
                                "sourceSeq": event.sourceSeq,
                                "generation": event.generation,
                                "canonicalSeq": seq,
                            }
                        }
                    )
    except WebSocketDisconnect:
        return
    except (IntegrationAccessDenied, WebSocketAuthError):
        await websocket.close(code=4403, reason="Bridge authorization denied")
    except (ValueError, ValidationError, asyncio.TimeoutError):
        await websocket.close(
            code=4400, reason="Invalid bridge frame or expired connection"
        )

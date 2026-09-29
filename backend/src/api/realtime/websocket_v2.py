"""
Enhanced WebSocket API endpoints for real-time communications
"""

import asyncio
import json
import logging
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.core.dependencies import get_current_user
from src.core.security import TokenData
from src.core.user_provisioning import ensure_user_and_org
from src.core.websocket_auth import WebSocketAuthenticator, WebSocketAuthError
from src.models.user import User, UserRole
from src.services.infrastructure.status_update_service import (
    Channel,
    UpdateFrequency,
    status_update_service,
)
from src.services.websocket.websocket_manager import (
    MessageType,
    Priority,
    WebSocketMessage,
    connection_manager,
)

logger = logging.getLogger(__name__)


async def _resolve_ws_organization_id(
    user_id: str,
    user_payload: Dict[str, Any],
    session_factory=AsyncSessionLocal,
) -> Optional[str]:
    """Resolve a WS connection's REAL organization_id from the DB.

    Supabase JWTs carry no org claim (it's None, then dropped by
    model_dump(exclude_none=True)), so the org MUST be looked up — never
    defaulted to a shared "default" bucket, which co-locates every tenant and
    leaks cross-tenant messages. Mirrors get_current_user (active-user filter +
    JIT-provision). Returns None on any miss/error so the caller fails closed.
    """
    stmt = (
        select(User)
        .options(selectinload(User.organization))
        .where(
            User.id == user_id,
            User.is_active == True,  # noqa: E712
            User.is_deleted == False,  # noqa: E712
        )
    )
    try:
        async with session_factory() as db:
            user = (await db.execute(stmt)).scalars().first()
            if user is None:
                # First login over WS — JIT-provision like get_current_user does.
                token_data = TokenData(
                    user_id=user_id,
                    email=user_payload.get("email"),
                    organization_id=user_payload.get("organization_id"),
                    role=user_payload.get("role", "USER"),
                )
                if await ensure_user_and_org(db, token_data):
                    await db.commit()
                    user = (await db.execute(stmt)).scalars().first()
            if user is not None and user.organization_id is not None:
                return str(user.organization_id)
    except Exception as e:  # DB failure must NOT silently grant shared access
        logger.error("WebSocket org resolution failed for user %s: %s", user_id, e)
    return None


async def _resolve_ws_role(
    user_id: str,
    session_factory=AsyncSessionLocal,
) -> str:
    """Resolve a WS connection's REAL role from the DB (source of truth).

    The JWT's ``role`` claim (Supabase ``app_metadata.role``) can go stale:
    a CLI token is long-lived (``CLI_TOKEN_EXPIRE_DAYS``, default 30) and
    isn't re-minted when an admin is demoted, so trusting the claim for
    admin-channel gating would let a demoted admin keep admin-channel access
    for up to 30 days. Mirrors ``_resolve_ws_organization_id``: DB lookup,
    fail closed (least-privileged role) on any miss/error.
    """
    stmt = select(User.role).where(
        User.id == user_id,
        User.is_active == True,  # noqa: E712
        User.is_deleted == False,  # noqa: E712
    )
    try:
        async with session_factory() as db:
            role = (await db.execute(stmt)).scalars().first()
            if role is not None:
                return role.value
    except Exception as e:  # DB failure must NOT silently grant admin access
        logger.error("WebSocket role resolution failed for user %s: %s", user_id, e)
    return UserRole.USER.value


async def _get_user_organization_id(
    user_id: str,
    session_factory=AsyncSessionLocal,
) -> Optional[str]:
    """Fetch a user's organization_id from the DB (I6 tenant scoping).

    Scopes ADMIN cross-user access on the HTTP inspection endpoints:
    ``role==ADMIN`` is a per-org role, not a platform superuser, so an admin
    may only act on users inside their own organization. Returns None when
    the user does not exist (caller maps that to 404) or on any DB error
    (fail closed).
    """
    stmt = select(User.organization_id).where(
        User.id == user_id,
        User.is_active == True,  # noqa: E712 - mirror get_current_user liveness filters
        User.is_deleted == False,  # noqa: E712
    )
    try:
        async with session_factory() as db:
            org_id = (await db.execute(stmt)).scalars().first()
            if org_id is not None:
                return str(org_id)
    except Exception as e:  # DB failure must NOT silently grant cross-tenant access
        logger.error("User organization lookup failed for %s: %s", user_id, e)
    return None


router = APIRouter(prefix="/api/v2/ws", tags=["websocket-v2"])


# Pydantic models for WebSocket operations
class ConnectionRequest(BaseModel):
    """WebSocket connection request"""

    channels: List[str] = Field(
        default_factory=list, description="Channels to subscribe to"
    )
    update_frequency: UpdateFrequency = Field(
        default=UpdateFrequency.NORMAL, description="Update frequency"
    )
    message_filter: Optional[Dict[str, Any]] = Field(
        default=None, description="Message filtering criteria"
    )
    client_info: Optional[Dict[str, Any]] = Field(
        default=None, description="Client information"
    )


class SubscriptionRequest(BaseModel):
    """Subscription request"""

    channel: str = Field(..., description="Channel to subscribe to")
    message_filter: Optional[Dict[str, Any]] = Field(
        default=None, description="Message filter for this channel"
    )


class BroadcastRequest(BaseModel):
    """Broadcast message request"""

    message_type: str = Field(..., description="Type of message")
    data: Dict[str, Any] = Field(..., description="Message data")
    channel: str = Field(..., description="Channel to broadcast to")
    priority: Priority = Field(default=Priority.NORMAL, description="Message priority")
    target_users: Optional[List[str]] = Field(
        default=None, description="Specific users to target"
    )
    target_organizations: Optional[List[str]] = Field(
        default=None, description="Specific organizations to target"
    )


class ConnectionStatusResponse(BaseModel):
    """Connection status response"""

    connection_id: str
    user_id: str
    organization_id: str
    connected_at: datetime
    last_heartbeat: datetime
    subscribed_channels: List[str]
    messages_sent: int
    messages_received: int


@router.websocket("/connect")
async def websocket_connect_v2_secure(
    websocket: WebSocket,
    channels: str = Query(
        "", description="Comma-separated list of channels to subscribe to"
    ),
    frequency: str = Query("normal", description="Update frequency"),
    client_info: str = Query("", description="JSON-encoded client information"),
):
    """
    Secure WebSocket connection endpoint with comprehensive features.

    SECURITY: Authentication is performed via headers, NOT URL parameters.
    A present-but-disallowed Origin header is rejected (CSWSH hardening).

    Authentication Methods (in priority order):
    1. Authorization header: `Authorization: Bearer <token>`
    2. Sec-WebSocket-Protocol: `auth, <token>` (browser workaround)

    Features:
    - Secure JWT authentication (no token in URL)
    - Channel subscription
    - Message filtering
    - Automatic reconnection support
    - Heartbeat/ping-pong mechanism
    - Connection metrics tracking
    - Graceful degradation

    Query Parameters:
    - channels: Comma-separated list of channels (e.g., "document_processing,job_status")
    - frequency: Update frequency - realtime, frequent, normal, periodic
    - client_info: JSON-encoded client information (browser, version, etc.)

    Connected channels:
    - document_processing: Real-time document processing updates
    - job_status: Background job status updates
    - system_status: System-wide status and metrics
    - user_notifications: User-specific notifications
    - quota_alerts: Storage and processing quota alerts
    - quality_metrics: Quality evaluation metrics
    - admin_alerts: Administrative alerts (admin users only)

    Message Format:
    ```json
    {
        "id": "uuid",
        "type": "message_type",
        "data": {...},
        "timestamp": "ISO8601",
        "priority": "low|normal|high|critical"
    }
    ```

    Client-to-Server Messages:
    - ping: Heartbeat request
    - subscribe: Subscribe to channel
    - unsubscribe: Unsubscribe from channel
    - status_update: Update connection status or message filter

    Server-to-Client Messages:
    - pong: Heartbeat response
    - document_processing: Document processing status updates
    - job_status: Job status updates
    - system_notification: System notifications and alerts
    - connection_status: Connection status updates
    """
    # Authenticate using secure methods (headers/protocol/cookie)
    try:
        user_payload = await WebSocketAuthenticator.authenticate(websocket)
    except WebSocketAuthError as e:
        logger.warning(f"WebSocket authentication failed: {e.message}")
        await websocket.close(code=e.code, reason=e.message)
        return

    # Get subprotocol for response if using protocol-based auth
    subprotocol = WebSocketAuthenticator.get_subprotocol_response(websocket)

    # Extract user info from authenticated payload
    user_id = user_payload.get("sub")

    if not user_id:
        logger.error("Authentication succeeded but user_id (sub) missing from payload")
        await websocket.close(
            code=4003, reason="Invalid token payload: missing user ID"
        )
        return

    # Resolve the connection's REAL organization from the DB. Never fall back to
    # a shared "default" bucket: that co-locates every tenant's connections and
    # leaks org-scoped / shared-channel messages across tenants. Fail closed.
    organization_id = await _resolve_ws_organization_id(user_id, user_payload)
    if not organization_id:
        logger.warning(
            "WebSocket rejected: no organization resolved for user %s", user_id
        )
        await websocket.close(code=4003, reason="No organization for user")
        return

    # Resolve the connection's REAL role from the DB (R4-M8). The JWT role
    # claim (app_metadata.role, or a long-lived CLI token's copy of it) can be
    # stale for up to CLI_TOKEN_EXPIRE_DAYS after a demotion — admin-channel
    # gating must not trust it.
    db_role = await _resolve_ws_role(user_id)

    # Parse client information
    client_info_dict = {}
    if client_info:
        try:
            client_info_dict = json.loads(client_info)
        except json.JSONDecodeError:
            logger.warning(f"Invalid client_info JSON: {client_info}")

    # Parse channels
    channel_list = (
        [ch.strip() for ch in channels.split(",") if ch.strip()] if channels else []
    )

    # Parse frequency
    try:
        update_frequency = UpdateFrequency(frequency)
    except ValueError:
        update_frequency = UpdateFrequency.NORMAL
        logger.warning(f"Invalid frequency '{frequency}', using normal")

    # Add client info and auth metadata
    client_info_dict.update(
        {
            "requested_channels": channel_list,
            "update_frequency": update_frequency.value,
            "connected_at": datetime.now(dt_timezone.utc).isoformat(),
            "auth_method": user_payload.get("_auth_method", "unknown"),
            # DB-sourced (not JWT-claimed) role, stored so the connection
            # manager can authorize mid-session channel subscriptions
            # (admin-only channels) the same way the connect-time gate below
            # does — see R4-M8.
            "role": db_role,
        }
    )

    # Establish connection using secure authenticated method. Stamp the
    # token's own exp (naive UTC, from TokenData — see security.verify_token)
    # onto the connection so the heartbeat monitor can close it once the
    # token expires (AU4), instead of trusting the connect-time check forever.
    connection_id = await connection_manager.connect_authenticated(
        websocket=websocket,
        user_id=user_id,
        organization_id=organization_id,
        client_info=client_info_dict,
        subprotocol=subprotocol,
        expires_at=user_payload.get("exp"),
    )

    if not connection_id:
        return  # Connection failed and was closed

    try:
        # Authorize each requested channel before subscribing. Admin-only
        # channels (system_status, admin_alerts) are rejected for non-admins;
        # unknown channels are dropped. Only the granted set is subscribed and
        # echoed back, so a client cannot receive events it isn't entitled to.
        user_role = db_role
        granted_channels = [
            ch for ch in channel_list if _can_subscribe_to_channel(ch, user_role)
        ]
        denied_channels = [ch for ch in channel_list if ch not in granted_channels]
        if denied_channels:
            logger.warning(
                "WebSocket subscription denied for user %s role=%s channels=%s",
                user_id,
                user_role,
                denied_channels,
            )
        channel_list = granted_channels

        # Subscribe to requested channels
        for channel in channel_list:
            await connection_manager.subscribe_to_channel(connection_id, channel)

        # Subscribe to status updates if requested
        if channel_list:
            await status_update_service.subscribe_to_updates(
                connection_id=connection_id,
                update_types=channel_list,
                frequency=update_frequency,
            )

        # Send initial connection status
        status_message = WebSocketMessage(
            type=MessageType.CONNECT,
            data={
                "connection_id": connection_id,
                "subscribed_channels": channel_list,
                "update_frequency": update_frequency.value,
                "server_capabilities": {
                    "channels": [c.value for c in Channel],
                    "message_types": [t.value for t in MessageType],
                    "priorities": [p.value for p in Priority],
                    "frequencies": [f.value for f in UpdateFrequency],
                },
                "recommended_settings": {
                    "heartbeat_interval": 30,
                    "reconnect_delay": 5,
                    "max_reconnect_attempts": 5,
                },
            },
            timestamp=datetime.now(dt_timezone.utc),
        )
        await connection_manager.send_message_to_connection(
            connection_id, status_message
        )

        # Main message loop
        while True:
            try:
                # Receive message from client
                raw_message = await websocket.receive_text()

                # Handle the message
                await connection_manager.handle_client_message(
                    connection_id, raw_message
                )

            except WebSocketDisconnect:
                logger.info(f"WebSocket client disconnected: {connection_id}")
                break
            except Exception as e:
                logger.error(f"Error handling WebSocket message: {e}")

                # Send error message if still connected
                error_message = WebSocketMessage(
                    type=MessageType.ERROR,
                    data={
                        "error": "Message processing failed",
                        "details": (
                            str(e) if settings.DEBUG else "Internal error occurred"
                        ),
                    },
                    timestamp=datetime.now(dt_timezone.utc),
                )
                await connection_manager.send_message_to_connection(
                    connection_id, error_message
                )

    except WebSocketDisconnect:
        logger.info(f"WebSocket connection closed by client: {connection_id}")
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")
    finally:
        # Cleanup
        await status_update_service.unsubscribe_from_updates(connection_id)
        await connection_manager.disconnect(connection_id, "Connection closed")


@router.get("/status", response_model=Dict[str, Any])
async def get_websocket_status(current_user: User = Depends(get_current_user)):
    """
    Get WebSocket service status and statistics

    Returns comprehensive statistics about WebSocket connections,
    including active connections, channel subscriptions, and system health.
    """
    # I6: these are platform-wide ops metrics (total connections, per-channel
    # subscribers, org/user counts, job throughput) — any authenticated user
    # could read other tenants' activity volume. Admin-only now.
    if not current_user.has_permission(UserRole.ADMIN):
        raise HTTPException(
            status_code=403,
            detail="Only administrators can view WebSocket status",
        )
    try:
        # Get connection manager statistics
        conn_stats = connection_manager.get_connection_stats()

        # Get system status from status update service
        system_status = await status_update_service.get_system_status()

        # Combine statistics
        status_data = {
            "websocket_service": {
                "status": "healthy",
                "timestamp": datetime.now(dt_timezone.utc).isoformat(),
                "version": "2.0.0",
                "features": {
                    "jwt_authentication": True,
                    "channel_subscription": True,
                    "message_filtering": True,
                    "heartbeat_monitoring": True,
                    "automatic_reconnection": True,
                    "message_batching": True,
                    "redis_clustering": connection_manager.redis_client is not None,
                },
            },
            "connections": conn_stats,
            "system_status": {
                "active_jobs": system_status.active_jobs,
                "queued_jobs": system_status.queued_jobs,
                "completed_jobs_today": system_status.completed_jobs_today,
                "failed_jobs_today": system_status.failed_jobs_today,
                "average_processing_time_seconds": system_status.average_processing_time_seconds,
                "active_connections": system_status.active_connections,
            },
            "channels": {
                channel.value: {
                    "description": _get_channel_description(channel),
                    "subscribers": len(
                        connection_manager.channel_subscribers.get(channel.value, set())
                    ),
                    "message_rate": (
                        "realtime"
                        if channel in [Channel.DOCUMENT_PROCESSING, Channel.JOB_STATUS]
                        else "periodic"
                    ),
                }
                for channel in Channel
            },
            "performance": {
                "max_connections": conn_stats["max_connections"],
                "current_usage_percentage": (
                    conn_stats["total_connections"] / conn_stats["max_connections"]
                )
                * 100,
                "redis_enabled": conn_stats["redis_enabled"],
                "batch_processing": True,
                "message_throttling": True,
            },
        }

        return status_data

    except Exception as e:
        logger.error(f"Error getting WebSocket status: {e}")
        raise HTTPException(status_code=500, detail="Failed to get WebSocket status")


@router.get("/connections/{user_id}")
async def get_user_connections(
    user_id: str, current_user: User = Depends(get_current_user)
):
    """
    Get active WebSocket connections for a user

    Only the user themselves or admin users can access this information.
    """
    try:
        # Verify authorization (user can only see their own connections unless
        # a same-org admin — role==ADMIN is a per-org role, not a platform
        # superuser, so an admin cannot inspect users of another tenant; I6).
        if str(current_user.id) != user_id:
            if not current_user.has_permission(UserRole.ADMIN):
                raise HTTPException(
                    status_code=403,
                    detail="Not authorized to view these connections",
                )
            target_org_id = await _get_user_organization_id(user_id)
            if target_org_id is None:
                raise HTTPException(status_code=404, detail="User not found")
            if (
                current_user.organization_id is None
                or str(current_user.organization_id) != target_org_id
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Not authorized to view these connections",
                )

        # Get user's connections
        connection_ids = connection_manager.get_user_connections(user_id)

        connections_info = []
        for conn_id in connection_ids:
            if conn_id in connection_manager.active_connections:
                conn_info = connection_manager.active_connections[conn_id]
                connections_info.append(
                    {
                        "connection_id": conn_id,
                        "connected_at": conn_info.connected_at.isoformat(),
                        "last_heartbeat": conn_info.last_heartbeat.isoformat(),
                        "subscribed_channels": list(conn_info.subscribed_channels),
                        "client_info": conn_info.client_info,
                    }
                )

        return {
            "user_id": user_id,
            "active_connections": len(connections_info),
            "connections": connections_info,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting user connections: {e}")
        raise HTTPException(status_code=500, detail="Failed to get user connections")


@router.post("/broadcast")
async def broadcast_message(
    request: BroadcastRequest, current_user: User = Depends(get_current_user)
):
    """
    Broadcast a message to WebSocket subscribers

    Requires admin role.
    """
    if current_user.role.value != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only administrators can broadcast messages",
        )

    # Tenant scope: role==ADMIN is a PER-ORG role, not a platform superuser, so
    # an org admin may only broadcast within their OWN org. A null-org principal
    # has no tenant to scope to — reject rather than let str(None) conflate every
    # null-org tenant into one broadcast target.
    if current_user.organization_id is None:
        raise HTTPException(
            status_code=403,
            detail="Cannot broadcast without an organization",
        )
    caller_org = str(current_user.organization_id)

    # An org admin cannot target another tenant's org explicitly.
    if request.target_organizations:
        for org_id in request.target_organizations:
            if str(org_id) != caller_org:
                raise HTTPException(
                    status_code=403,
                    detail="Cannot broadcast to another organization",
                )

    try:
        # Create WebSocket message — target_organization makes
        # should_receive_message gate delivery to the caller's own tenant on
        # every path (channel / user / organization).
        message = WebSocketMessage(
            type=MessageType(request.message_type),
            data=request.data,
            timestamp=datetime.now(dt_timezone.utc),
            priority=request.priority,
            target_channels=[request.channel],
            target_organization=caller_org,
        )

        # Broadcast based on targets
        if request.target_users:
            for user_id in request.target_users:
                await connection_manager.broadcast_to_user(user_id, message)
        elif request.target_organizations:
            for org_id in request.target_organizations:
                await connection_manager.broadcast_to_organization(org_id, message)
        else:
            await connection_manager.broadcast_to_channel(request.channel, message)

        return {
            "success": True,
            "message_id": message.message_id,
            "broadcast_to": {
                "channel": request.channel,
                "target_users": request.target_users,
                "target_organizations": request.target_organizations,
            },
            "timestamp": datetime.now(dt_timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.error(f"Error broadcasting message: {e}")
        raise HTTPException(status_code=500, detail="Failed to broadcast message")


@router.post("/test-connection")
async def test_websocket_connection(
    connection_id: str, current_user: User = Depends(get_current_user)
):
    """
    Send a test message to a specific WebSocket connection

    Used for debugging and connection testing.
    """
    try:
        # Check if connection exists and user has access
        if connection_id not in connection_manager.active_connections:
            raise HTTPException(status_code=404, detail="Connection not found")

        conn_info = connection_manager.active_connections[connection_id]
        # Same-org admin rule as /connections/{user_id} (I6): role==ADMIN is a
        # per-org role, so an admin may only message connections owned by
        # users of their own tenant.
        if str(conn_info.user_id) != str(current_user.id):
            if not current_user.has_permission(UserRole.ADMIN):
                raise HTTPException(
                    status_code=403,
                    detail="Not authorized to access this connection",
                )
            target_org_id = await _get_user_organization_id(str(conn_info.user_id))
            if target_org_id is None:
                raise HTTPException(status_code=404, detail="User not found")
            if (
                current_user.organization_id is None
                or str(current_user.organization_id) != target_org_id
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Not authorized to access this connection",
                )

        # Send test message
        test_message = WebSocketMessage(
            type=MessageType.SYSTEM_NOTIFICATION,
            data={
                "title": "Connection Test",
                "message": "This is a test message to verify your WebSocket connection is working correctly.",
                "type": "info",
                "timestamp": datetime.now(dt_timezone.utc).isoformat(),
            },
            timestamp=datetime.now(dt_timezone.utc),
        )

        success = await connection_manager.send_message_to_connection(
            connection_id, test_message
        )

        return {
            "success": success,
            "connection_id": connection_id,
            "message_id": test_message.message_id,
            "timestamp": datetime.now(dt_timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error testing WebSocket connection: {e}")
        raise HTTPException(status_code=500, detail="Failed to test connection")


@router.get("/channels")
async def get_available_channels(current_user: User = Depends(get_current_user)):
    """
    Get list of available WebSocket channels and their descriptions
    """
    return {
        "channels": [
            {
                "name": channel.value,
                "description": _get_channel_description(channel),
                "message_types": _get_channel_message_types(channel),
                "typical_update_frequency": _get_channel_frequency(channel),
                "required_permissions": _get_channel_permissions(channel),
            }
            for channel in Channel
        ]
    }


@router.get("/health")
async def websocket_health_check(current_user: User = Depends(get_current_user)):
    """
    WebSocket service health check endpoint

    Returns detailed health information for monitoring systems.
    """
    try:
        conn_stats = connection_manager.get_connection_stats()
        system_status = await status_update_service.get_system_status()

        # Calculate health status
        is_healthy = (
            conn_stats["total_connections"] < conn_stats["max_connections"]
            and system_status.active_jobs < 1000
            and system_status.error_rate_last_hour < 10.0  # Arbitrary threshold
        )

        return {
            "status": "healthy" if is_healthy else "degraded",
            "timestamp": datetime.now(dt_timezone.utc).isoformat(),
            "checks": {
                "connection_manager": "healthy" if connection_manager else "unhealthy",
                "status_update_service": (
                    "healthy" if status_update_service else "unhealthy"
                ),
                "redis_connection": (
                    "healthy" if connection_manager.redis_client else "disabled"
                ),
                "connection_load": (
                    "healthy"
                    if conn_stats["total_connections"]
                    < conn_stats["max_connections"] * 0.8
                    else "high"
                ),
            },
            "metrics": {
                "active_connections": conn_stats["total_connections"],
                "active_jobs": system_status.active_jobs,
                "failed_jobs_today": system_status.failed_jobs_today,
                "average_processing_time": system_status.average_processing_time_seconds,
            },
        }

    except Exception as e:
        logger.error(f"WebSocket health check failed: {e}")
        return {
            "status": "unhealthy",
            "timestamp": datetime.now(dt_timezone.utc).isoformat(),
            "error": str(e) if settings.DEBUG else "Health check failed",
        }


def _get_channel_description(channel: Channel) -> str:
    """Get description for a channel"""
    descriptions = {
        Channel.DOCUMENT_PROCESSING: "Real-time updates for document processing operations",
        Channel.JOB_STATUS: "Background job status and progress updates",
        Channel.SYSTEM_STATUS: "System-wide status and performance metrics",
        Channel.USER_NOTIFICATIONS: "User-specific notifications and alerts",
        Channel.QUOTA_ALERTS: "Storage and processing quota usage alerts",
        Channel.QUALITY_METRICS: "Quality evaluation metrics and results",
        Channel.ADMIN_ALERTS: "Administrative alerts and system notifications",
    }
    return descriptions.get(channel, "Unknown channel")


def _get_channel_message_types(channel: Channel) -> List[str]:
    """Get message types for a channel"""
    message_types = {
        Channel.DOCUMENT_PROCESSING: [MessageType.DOCUMENT_PROCESSING.value],
        Channel.JOB_STATUS: [MessageType.JOB_STATUS.value],
        Channel.SYSTEM_STATUS: [MessageType.SYSTEM_NOTIFICATION.value],
        Channel.USER_NOTIFICATIONS: [MessageType.SYSTEM_NOTIFICATION.value],
        Channel.QUOTA_ALERTS: [MessageType.SYSTEM_NOTIFICATION.value],
        Channel.QUALITY_METRICS: [MessageType.STATUS_UPDATE.value],
        Channel.ADMIN_ALERTS: [
            MessageType.SYSTEM_NOTIFICATION.value,
            MessageType.ERROR.value,
        ],
    }
    return message_types.get(channel, [MessageType.STATUS_UPDATE.value])


def _get_channel_frequency(channel: Channel) -> str:
    """Get typical update frequency for a channel"""
    frequencies = {
        Channel.DOCUMENT_PROCESSING: "realtime (100-500ms)",
        Channel.JOB_STATUS: "frequent (1-2s)",
        Channel.SYSTEM_STATUS: "periodic (30-60s)",
        Channel.USER_NOTIFICATIONS: "immediate",
        Channel.QUOTA_ALERTS: "immediate",
        Channel.QUALITY_METRICS: "normal (5-10s)",
        Channel.ADMIN_ALERTS: "immediate",
    }
    return frequencies.get(channel, "normal (5-10s)")


def _get_channel_permissions(channel: Channel) -> str:
    """Get required permissions for a channel"""
    permissions = {
        Channel.DOCUMENT_PROCESSING: "user",
        Channel.JOB_STATUS: "user",
        Channel.SYSTEM_STATUS: "admin",
        Channel.USER_NOTIFICATIONS: "user",
        Channel.QUOTA_ALERTS: "user",
        Channel.QUALITY_METRICS: "user",
        Channel.ADMIN_ALERTS: "admin",
    }
    return permissions.get(channel, "user")


def _can_subscribe_to_channel(channel_name: str, role: str) -> bool:
    """Whether a user with *role* may subscribe to *channel_name*.

    Admin-only channels (``system_status``, ``admin_alerts``) were previously
    subscribable by anyone — combined with channel broadcasts that meant a
    regular user could receive admin/system events. Unknown channel names are
    rejected (fail closed). Role match is case-insensitive (token claims use
    ``USER``/``ADMIN``; the permission map uses lowercase).
    """
    try:
        channel = Channel(channel_name)
    except ValueError:
        return False
    required = _get_channel_permissions(channel)
    if required == "admin":
        return str(role or "").lower() == "admin"
    return True

"""
Enhanced WebSocket connection manager for enterprise real-time communications
"""

import asyncio
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

import redis.asyncio as redis
from fastapi import HTTPException, WebSocket, WebSocketDisconnect, status
from jose import JWTError, jwt
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import get_async_session
from src.models.document import ProcessingStatus
from src.models.processing import JobStatus
from src.models.user import User, UserRole
from src.models.websocket_status import (
    ConnectionEvent,
    ConnectionStatus,
    Priority,
    StatusUpdate,
    UpdateType,
    WebSocketConnection,
)
from src.services.base import BaseService

logger = logging.getLogger(__name__)

# Inbound caps (audit I11). Client->server frames are tiny control messages
# (ping / subscribe / unsubscribe / status_update, well under 1 KiB); 64 KiB is
# far above any legit one and far below the server's 16 MiB ws_max_size, so an
# oversized frame is closed with 1009 before it is ever json.loads'd.
WS_MAX_INBOUND_MESSAGE_BYTES = 64 * 1024
# ponytail: per-connection fixed window, in-memory (connections are
# process-local). Allows a 2x burst at a window edge; switch to a token bucket
# (or a Redis counter keyed by user) if per-user fairness across workers matters.
WS_MAX_INBOUND_MESSAGES_PER_WINDOW = 60
WS_INBOUND_WINDOW_SECONDS = 10.0


class MessageType(Enum):
    """WebSocket message types"""

    CONNECT = "connect"
    DISCONNECT = "disconnect"
    PING = "ping"
    PONG = "pong"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    ERROR = "error"
    STATUS_UPDATE = "status_update"
    DOCUMENT_PROCESSING = "document_processing"
    JOB_STATUS = "job_status"
    SYSTEM_NOTIFICATION = "system_notification"
    # Thread activity events
    THREAD_CREATED = "thread_created"
    THREAD_UPDATED = "thread_updated"
    THREAD_DELETED = "thread_deleted"
    THREADS_BULK_UPDATED = "threads_bulk_updated"
    MESSAGE_CREATED = "message_created"
    MESSAGE_UPDATED = "message_updated"
    CONVERSATION_UPDATED = "conversation_updated"


@dataclass
class WebSocketMessage:
    """Structured WebSocket message"""

    type: MessageType
    data: Dict[str, Any]
    timestamp: datetime
    message_id: str = None
    priority: Priority = Priority.NORMAL
    target_channels: List[str] = None
    expires_at: Optional[datetime] = None
    # When set, only connections belonging to this organization may receive the
    # message — even if they are subscribed to the broadcast channel. Document
    # and job status updates set this so per-tenant payloads queued onto the
    # shared ``document_processing``/``job_status`` channels cannot fan out
    # across tenants. ``None`` = no org restriction (global/system messages).
    target_organization: Optional[str] = None

    def __post_init__(self):
        if self.message_id is None:
            self.message_id = str(uuid.uuid4())
        if self.target_channels is None:
            self.target_channels = []
        if isinstance(self.type, str):
            self.type = MessageType(self.type)
        if isinstance(self.priority, str):
            self.priority = Priority(self.priority)


@dataclass
class ConnectionInfo:
    """Connection metadata"""

    user_id: str
    organization_id: str
    connection_id: str
    websocket: WebSocket
    connected_at: datetime
    last_heartbeat: datetime
    subscribed_channels: Set[str]
    message_filter: Dict[str, Any] = None
    client_info: Dict[str, Any] = None
    # Authenticated token's expiry (naive UTC, matching TokenData.exp — see
    # security.py verify_token). AU4: without this, a connection authenticated
    # once at connect time keeps receiving its org's realtime pushes even
    # after the token backing it has expired, until the socket happens to
    # drop for an unrelated reason. None = no expiry enforced (back-compat
    # for callers that don't thread a token expiry through, e.g. the legacy
    # connect() path).
    expires_at: Optional[datetime] = None
    # Inbound rate window (audit I11): monotonic start + frames seen in it.
    inbound_window_start: float = 0.0
    inbound_count: int = 0

    def update_heartbeat(self):
        """Update connection heartbeat timestamp"""
        self.last_heartbeat = datetime.now(dt_timezone.utc)

    def is_subscribed_to_channel(self, channel: str) -> bool:
        """Check if connection is subscribed to channel"""
        return channel in self.subscribed_channels

    def should_receive_message(self, message: WebSocketMessage) -> bool:
        """Check if connection should receive this message"""
        # Tenant gate: a message addressed to a specific organization must
        # never reach a connection from another org, even via a shared
        # broadcast channel. Closes the cross-tenant document/job leak.
        if message.target_organization is not None and str(self.organization_id) != str(
            message.target_organization
        ):
            return False

        # Check channel subscription
        if message.target_channels and not any(
            self.is_subscribed_to_channel(ch) for ch in message.target_channels
        ):
            return False

        # Check message filter
        if self.message_filter and message.data:
            # Apply filtering logic based on connection's filter
            for key, filter_value in self.message_filter.items():
                if key in message.data:
                    message_value = message.data[key]
                    if isinstance(filter_value, list):
                        if message_value not in filter_value:
                            return False
                    elif filter_value != message_value:
                        return False

        return True


# Channels that require an admin role to subscribe to. Kept here (not just in
# the websocket_v2 connect handler) so the mid-session SUBSCRIBE command path
# enforces the same gate — otherwise a non-admin could connect with no channels
# and then subscribe to an admin channel at runtime.
_ADMIN_ONLY_CHANNELS = {"system_status", "admin_alerts"}


async def _resolve_current_db_role(user_id: str) -> str:
    """Re-resolve *user_id*'s role from the DB at admin-channel subscribe time.

    R4-M8: a connection's stored ``client_info["role"]`` is a DB-sourced
    snapshot taken at connect time, but a connection (and a long-lived CLI
    token backing it) can outlive a demotion by up to CLI_TOKEN_EXPIRE_DAYS
    (30d default). Re-checking the DB on every admin-channel subscribe closes
    that window instead of trusting the connect-time snapshot for the life of
    the socket. Fails closed (least-privileged role) on any miss/error.
    """
    stmt = select(User.role).where(
        User.id == user_id,
        User.is_active == True,  # noqa: E712
        User.is_deleted == False,  # noqa: E712
    )
    try:
        async with get_async_session() as db:
            role = (await db.execute(stmt)).scalars().first()
            if role is not None:
                return role.value
    except Exception as e:
        logger.error(
            "WebSocket mid-session role resolution failed for user %s: %s",
            user_id,
            e,
        )
    return UserRole.USER.value


class EnhancedConnectionManager(BaseService):
    """Enterprise-grade WebSocket connection manager with Redis clustering support"""

    _background_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _fire_and_forget(coro):
        task = asyncio.create_task(coro)
        EnhancedConnectionManager._background_tasks.add(task)
        task.add_done_callback(EnhancedConnectionManager._background_tasks.discard)
        return task

    def __init__(self):
        super().__init__()
        self.active_connections: Dict[str, ConnectionInfo] = {}
        self.user_connections: Dict[str, Set[str]] = defaultdict(set)
        self.organization_connections: Dict[str, Set[str]] = defaultdict(set)
        self.channel_subscribers: Dict[str, Set[str]] = defaultdict(set)

        # Unique id for THIS manager instance (one per worker process). Stamped
        # on every Redis broadcast so the listener can skip the copy of its own
        # publish (Redis fan-out delivers back to the publisher) WITHOUT dropping
        # sibling workers' messages. Must be a real value: a missing id made the
        # self-filter compare "unknown" == "unknown" and silently drop every
        # cross-worker broadcast (defeating fan-out at >1 gunicorn worker).
        self.instance_id = uuid.uuid4().hex

        # Redis for clustering and message broadcasting
        self.redis_client: Optional[redis.Redis] = None
        self.redis_pubsub: Optional[redis.PubSub] = None

        # Background tasks
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.cleanup_task: Optional[asyncio.Task] = None
        self.redis_listener_task: Optional[asyncio.Task] = None

        # Configuration
        self.heartbeat_interval = 30  # seconds
        self.connection_timeout = 300  # 5 minutes
        # Per-send timeout: a stuck/slow socket must not block the (concurrent)
        # fan-out to everyone else on this worker. On timeout the send is treated
        # as a failure and the connection is scheduled for disconnect.
        self.send_timeout = getattr(settings, "WS_SEND_TIMEOUT", 10)  # seconds
        self.max_connections = 10000
        # Per-user cap: the global cap alone lets one user (or a single leaked
        # token) open thousands of sockets and starve the global budget for
        # every other tenant. Bound each user_id to a sane number of concurrent
        # connections (multiple tabs/devices stay comfortably under it).
        self.max_connections_per_user = getattr(
            settings, "WS_MAX_CONNECTIONS_PER_USER", 100
        )
        self.message_queue = asyncio.Queue(maxsize=1000)

    async def initialize(self):
        """Initialize the connection manager"""
        await super().initialize()

        # Initialize Redis client for clustering
        try:
            self.redis_client = redis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                health_check_interval=30,
            )

            # Test Redis connection
            await self.redis_client.ping()
            logger.info("Redis connection established for WebSocket clustering")

            # Initialize pubsub for inter-cluster communication
            self.redis_pubsub = self.redis_client.pubsub()
            await self.redis_pubsub.subscribe("websocket_broadcast")

            # Start background tasks
            self.heartbeat_task = asyncio.create_task(self._heartbeat_monitor())
            self.cleanup_task = asyncio.create_task(self._cleanup_stale_connections())
            self.redis_listener_task = asyncio.create_task(
                self._redis_message_listener()
            )

        except Exception as e:
            logger.warning(f"Redis not available, running in single-instance mode: {e}")
            self.redis_client = None

        logger.info("Enhanced WebSocket Connection Manager initialized")

    async def shutdown(self):
        """Graceful shutdown"""
        logger.info("Shutting down WebSocket Connection Manager...")

        # Cancel background tasks
        tasks = [self.heartbeat_task, self.cleanup_task, self.redis_listener_task]
        for task in tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Close all connections
        for connection_info in list(self.active_connections.values()):
            try:
                await connection_info.websocket.close(code=status.WS_1001_GOING_AWAY)
            except Exception:
                pass

        # Close Redis connections
        if self.redis_pubsub:
            await self.redis_pubsub.close()
        if self.redis_client:
            await self.redis_client.close()

        await super().shutdown()
        logger.info("WebSocket Connection Manager shutdown complete")

    async def authenticate_websocket(
        self, websocket: WebSocket, token: str
    ) -> Optional[Dict[str, Any]]:
        """Authenticate WebSocket connection using JWT token.

        On auth failure, accepts the WebSocket before closing it to ensure
        proper protocol ordering (close frame requires an accepted connection).
        """

        async def _reject(code: int, reason: str) -> None:
            """Accept then immediately close to avoid connection leak."""
            try:
                await websocket.accept()
            except Exception:
                pass  # Already accepted or connection lost
            try:
                await websocket.close(code=code, reason=reason)
            except Exception:
                pass  # Best-effort close

        try:
            if not token:
                await _reject(
                    status.WS_1008_POLICY_VIOLATION,
                    "Authentication token required",
                )
                return None

            # Decode JWT token
            payload = jwt.decode(
                token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
            )

            user_id = payload.get("sub")
            organization_id = payload.get("organization_id")

            if not user_id or not organization_id:
                await _reject(status.WS_1008_POLICY_VIOLATION, "Invalid token payload")
                return None

            return {
                "user_id": user_id,
                "organization_id": organization_id,
                "token_payload": payload,
            }

        except JWTError as e:
            logger.warning(f"JWT authentication failed: {e}")
            await _reject(status.WS_1008_POLICY_VIOLATION, "Invalid or expired token")
            return None
        except Exception as e:
            logger.error(f"WebSocket authentication error: {e}")
            await _reject(status.WS_1011_INTERNAL_ERROR, "Authentication error")
            return None

    async def _connect_internal(
        self,
        websocket: WebSocket,
        user_id: str,
        organization_id: str,
        client_info: Dict[str, Any] = None,
        auth_method: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> str:
        """
        Internal helper that handles the common connection setup logic.

        This method is called after authentication and WebSocket accept have
        been handled by the public connect methods.

        Args:
            websocket: The already-accepted WebSocket connection
            user_id: Authenticated user's ID
            organization_id: User's organization ID
            client_info: Optional client metadata
            auth_method: Optional auth method label for welcome message
            expires_at: Authenticated token's expiry (naive UTC), if known

        Returns:
            Connection ID
        """
        # Generate connection ID
        connection_id = str(uuid.uuid4())

        # Create connection info
        connection_info = ConnectionInfo(
            user_id=user_id,
            organization_id=organization_id,
            connection_id=connection_id,
            websocket=websocket,
            connected_at=datetime.now(dt_timezone.utc),
            last_heartbeat=datetime.now(dt_timezone.utc),
            subscribed_channels=set(),
            message_filter=client_info.get("message_filter") if client_info else None,
            client_info=client_info or {},
            expires_at=expires_at,
        )

        # Store connection
        self.active_connections[connection_id] = connection_info
        self.user_connections[user_id].add(connection_id)
        self.organization_connections[organization_id].add(connection_id)

        # Log connection to database
        await self._log_connection_event(
            connection_id=connection_id,
            event_type="connect",
            user_id=user_id,
            organization_id=organization_id,
            client_info=client_info,
        )

        # Build welcome message data
        welcome_data = {
            "connection_id": connection_id,
            "user_id": user_id,
            "organization_id": organization_id,
            "server_time": datetime.now(dt_timezone.utc).isoformat(),
            "heartbeat_interval": self.heartbeat_interval,
        }
        if auth_method:
            welcome_data["auth_method"] = auth_method

        # Send welcome message
        welcome_message = WebSocketMessage(
            type=MessageType.CONNECT,
            data=welcome_data,
            timestamp=datetime.now(dt_timezone.utc),
        )

        await self.send_message_to_connection(connection_id, welcome_message)

        # Log connection established
        auth_label = f" ({auth_method})" if auth_method else ""
        logger.info(
            f"WebSocket connection established{auth_label}: {connection_id} for user {user_id}"
        )

        return connection_id

    def _user_at_capacity(self, user_id: str) -> bool:
        """True when this user already holds the max allowed concurrent sockets."""
        return (
            len(self.user_connections.get(user_id, ())) >= self.max_connections_per_user
        )

    async def _reject_connection(
        self, websocket: WebSocket, reason: str, subprotocol: Optional[str] = None
    ) -> None:
        """Accept then immediately close (WS_1013) so the client sees a clean
        'try again later' instead of a silently dropped handshake."""
        try:
            if subprotocol:
                await websocket.accept(subprotocol=subprotocol)
            else:
                await websocket.accept()
        except Exception:
            pass
        try:
            await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER, reason=reason)
        except Exception:
            pass

    async def connect(
        self, websocket: WebSocket, token: str, client_info: Dict[str, Any] = None
    ) -> Optional[str]:
        """Accept and manage new WebSocket connection with token authentication"""
        # Authenticate connection
        auth_result = await self.authenticate_websocket(websocket, token)
        if not auth_result:
            return None

        # Check connection limits — accept then close to avoid connection leak
        if len(self.active_connections) >= self.max_connections:
            await self._reject_connection(websocket, "Server at maximum capacity")
            return None

        # Per-user cap — one principal must not exhaust the global budget.
        if self._user_at_capacity(auth_result["user_id"]):
            logger.warning(
                "WebSocket per-user connection limit reached for user %s (limit=%s)",
                auth_result["user_id"],
                self.max_connections_per_user,
            )
            await self._reject_connection(
                websocket, "Per-user connection limit reached"
            )
            return None

        # Accept connection
        await websocket.accept()

        # Delegate to internal helper
        return await self._connect_internal(
            websocket=websocket,
            user_id=auth_result["user_id"],
            organization_id=auth_result["organization_id"],
            client_info=client_info,
            auth_method=None,
        )

    async def connect_authenticated(
        self,
        websocket: WebSocket,
        user_id: str,
        organization_id: str,
        client_info: Dict[str, Any] = None,
        subprotocol: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> Optional[str]:
        """
        Accept and manage a pre-authenticated WebSocket connection.

        This method is used when authentication has already been performed
        (e.g., via the WebSocketAuthenticator helper) to avoid token exposure
        in URL query parameters.

        Args:
            websocket: The WebSocket connection
            user_id: Authenticated user's ID
            organization_id: User's organization ID
            client_info: Optional client metadata
            subprotocol: Optional subprotocol to respond with
            expires_at: Authenticated token's expiry (naive UTC), if known —
                enforced by the heartbeat monitor (AU4)

        Returns:
            Connection ID if successful, None otherwise
        """
        # Check connection limits — accept then close to avoid connection leak
        if len(self.active_connections) >= self.max_connections:
            await self._reject_connection(
                websocket, "Server at maximum capacity", subprotocol=subprotocol
            )
            return None

        # Per-user cap — one principal must not exhaust the global budget.
        if self._user_at_capacity(user_id):
            logger.warning(
                "WebSocket per-user connection limit reached for user %s (limit=%s)",
                user_id,
                self.max_connections_per_user,
            )
            await self._reject_connection(
                websocket, "Per-user connection limit reached", subprotocol=subprotocol
            )
            return None

        # Accept connection with appropriate subprotocol
        if subprotocol:
            await websocket.accept(subprotocol=subprotocol)
        else:
            await websocket.accept()

        # Delegate to internal helper
        return await self._connect_internal(
            websocket=websocket,
            user_id=user_id,
            organization_id=organization_id,
            client_info=client_info,
            auth_method="secure",
            expires_at=expires_at,
        )

    async def disconnect(self, connection_id: str, reason: str = None):
        """Handle WebSocket disconnection"""
        if connection_id not in self.active_connections:
            return

        connection_info = self.active_connections[connection_id]

        # Remove from tracking
        del self.active_connections[connection_id]
        self.user_connections[connection_info.user_id].discard(connection_id)
        # ponytail: opportunistic eviction; move to TTL cache if key cardinality
        # ever matters. A defaultdict(set) never drops empty entries on its
        # own, so a user/org/channel that disconnects for good would sit in
        # these dicts forever.
        if not self.user_connections[connection_info.user_id]:
            del self.user_connections[connection_info.user_id]
        self.organization_connections[connection_info.organization_id].discard(
            connection_id
        )
        if not self.organization_connections[connection_info.organization_id]:
            del self.organization_connections[connection_info.organization_id]

        # Remove from channel subscriptions
        for channel in connection_info.subscribed_channels:
            self.channel_subscribers[channel].discard(connection_id)
            if not self.channel_subscribers[channel]:
                del self.channel_subscribers[channel]

        # Log disconnection to database
        await self._log_connection_event(
            connection_id=connection_id,
            event_type="disconnect",
            user_id=connection_info.user_id,
            organization_id=connection_info.organization_id,
            event_data={"reason": reason},
        )

        logger.info(
            f"WebSocket connection closed: {connection_id} - {reason or 'Unknown reason'}"
        )

    async def subscribe_to_channel(self, connection_id: str, channel: str) -> bool:
        """Subscribe connection to a channel"""
        if connection_id not in self.active_connections:
            return False

        connection_info = self.active_connections[connection_id]
        connection_info.subscribed_channels.add(channel)
        self.channel_subscribers[channel].add(connection_id)

        # Send confirmation
        confirmation_message = WebSocketMessage(
            type=MessageType.SUBSCRIBE,
            data={"channel": channel, "subscribed": True},
            timestamp=datetime.now(dt_timezone.utc),
        )

        await self.send_message_to_connection(connection_id, confirmation_message)
        return True

    async def unsubscribe_from_channel(self, connection_id: str, channel: str) -> bool:
        """Unsubscribe connection from a channel"""
        if connection_id not in self.active_connections:
            return False

        connection_info = self.active_connections[connection_id]
        connection_info.subscribed_channels.discard(channel)
        self.channel_subscribers[channel].discard(connection_id)
        if not self.channel_subscribers[channel]:
            del self.channel_subscribers[channel]

        # Send confirmation
        confirmation_message = WebSocketMessage(
            type=MessageType.UNSUBSCRIBE,
            data={"channel": channel, "subscribed": False},
            timestamp=datetime.now(dt_timezone.utc),
        )

        await self.send_message_to_connection(connection_id, confirmation_message)
        return True

    async def send_message_to_connection(
        self, connection_id: str, message: WebSocketMessage
    ) -> bool:
        """Send message to specific connection"""
        if connection_id not in self.active_connections:
            return False

        connection_info = self.active_connections[connection_id]

        try:
            # Prepare message payload
            payload = {
                "id": message.message_id,
                "type": message.type.value,
                "data": message.data,
                "timestamp": message.timestamp.isoformat(),
                "priority": message.priority.value,
            }

            # Send with a bounded timeout so one stuck/slow socket cannot block
            # the concurrent fan-out — on timeout it is handled like any send
            # failure and scheduled for disconnect below.
            await asyncio.wait_for(
                connection_info.websocket.send_json(payload),
                timeout=self.send_timeout,
            )

            # Record message metrics if we have a database record
            await self._record_message_metrics(
                connection_id, "sent", len(json.dumps(payload))
            )

            return True

        except Exception as e:
            logger.error(f"Failed to send message to {connection_id}: {e}")

            # Connection might be dead, schedule cleanup
            self._fire_and_forget(
                self.disconnect(connection_id, f"Send error: {str(e)}")
            )
            return False

    def _serialize_for_cluster(self, message: WebSocketMessage) -> Dict[str, Any]:
        """Explicit message shape for Redis fan-out. ``asdict`` would emit raw
        Enum/datetime that json.dumps can't encode; target_organization MUST
        round-trip so ``should_receive_message`` can still gate tenants on the
        receiving worker."""
        return {
            "type": message.type.value,
            "data": message.data,
            "timestamp": message.timestamp.isoformat(),
            "message_id": message.message_id,
            "priority": message.priority.value,
            "target_channels": message.target_channels,
            "target_organization": message.target_organization,
        }

    async def _deliver_local(self, connection_ids: Set[str], message: WebSocketMessage):
        """Deliver to the given locally-connected ids CONCURRENTLY, honouring the
        per-connection tenant/channel/filter gate. Concurrency plus the per-send
        timeout (in send_message_to_connection) stop one slow/stuck client from
        head-of-line-blocking delivery to everyone else on this worker. Snapshot
        the id set first — a failed send schedules a disconnect that mutates it.

        # ponytail: concurrent fan-out + per-send timeout. A full bounded
        # per-connection send queue is the heavier upgrade if a high-volume
        # channel ever needs true backpressure; this push channel does not.
        """
        targets = [
            cid
            for cid in list(connection_ids)
            if (conn := self.active_connections.get(cid))
            and conn.should_receive_message(message)
        ]
        if not targets:
            return
        # send_message_to_connection swallows its own exceptions (returns False),
        # so gather never propagates and needs no return_exceptions.
        await asyncio.gather(
            *(self.send_message_to_connection(cid, message) for cid in targets)
        )

    async def _publish_to_cluster(
        self, kind: str, target: str, message: WebSocketMessage
    ):
        """Publish to the OTHER worker processes / replicas so a push produced on
        one worker reaches sockets held by another. ``kind`` is
        channel|user|organization; ``target`` is the channel name / user id / org
        id. No-op in single-instance mode (no Redis)."""
        if not self.redis_client:
            return
        try:
            payload = {
                "kind": kind,
                "target": target,
                # Back-compat: pre-fix workers read a bare ``channel`` key.
                # Keep it so channel fan-out still crosses to not-yet-upgraded
                # pods during a rolling deploy.
                "channel": target,
                "message": self._serialize_for_cluster(message),
                "source_instance": self.instance_id,
            }
            await self.redis_client.publish("websocket_broadcast", json.dumps(payload))
        except Exception as e:
            logger.error(f"Failed to broadcast via Redis (kind={kind}): {e}")

    async def broadcast_to_channel(self, channel: str, message: WebSocketMessage):
        """Broadcast message to all subscribers of a channel (local + cluster)."""
        # Validate channel name to prevent injection
        if not re.match(r"^[a-zA-Z0-9_.\-]+$", channel):
            raise ValueError(f"Invalid channel name: {channel}")

        # Add channel to target channels if not already present
        if channel not in message.target_channels:
            message.target_channels.append(channel)

        await self._deliver_local(self.channel_subscribers.get(channel, set()), message)
        await self._publish_to_cluster("channel", channel, message)

    async def broadcast_to_user(self, user_id: str, message: WebSocketMessage):
        """Broadcast message to all connections for a user (local + cluster).

        Was local-only, so a targeted push produced on worker/replica A never
        reached the user's socket on worker B — ~half of pushes at two workers,
        and every push produced off the web process (Celery) reached nobody."""
        await self._deliver_local(self.user_connections.get(user_id, set()), message)
        await self._publish_to_cluster("user", user_id, message)

    async def broadcast_to_organization(
        self, organization_id: str, message: WebSocketMessage
    ):
        """Broadcast message to all connections in an organization (local + cluster)."""
        await self._deliver_local(
            self.organization_connections.get(organization_id, set()), message
        )
        await self._publish_to_cluster("organization", organization_id, message)

    async def handle_client_message(self, connection_id: str, raw_message: str):
        """Handle incoming message from client"""
        if connection_id not in self.active_connections:
            return

        connection_info = self.active_connections[connection_id]

        # Inbound caps (audit I11) — checked before any parsing. Every frame
        # (ping included) counts, which also bounds ping-flood -> PONG sends.
        now = time.monotonic()
        if now - connection_info.inbound_window_start >= WS_INBOUND_WINDOW_SECONDS:
            connection_info.inbound_window_start = now
            connection_info.inbound_count = 0
        connection_info.inbound_count += 1
        if connection_info.inbound_count > WS_MAX_INBOUND_MESSAGES_PER_WINDOW:
            await self.send_message_to_connection(
                connection_id,
                WebSocketMessage(
                    type=MessageType.ERROR,
                    data={"error": "rate_limited", "reason": "too_many_messages"},
                    timestamp=datetime.now(dt_timezone.utc),
                ),
            )
            await self._close_inbound_violation(
                connection_id, status.WS_1008_POLICY_VIOLATION, "Rate limit exceeded"
            )
            return
        # len(str) <= UTF-8 byte length, so the cheap char check short-circuits
        # huge frames without copying them; only <=64K-char frames get encoded.
        if (
            len(raw_message) > WS_MAX_INBOUND_MESSAGE_BYTES
            or len(raw_message.encode("utf-8")) > WS_MAX_INBOUND_MESSAGE_BYTES
        ):
            await self._close_inbound_violation(
                connection_id, status.WS_1009_MESSAGE_TOO_BIG, "Message too big"
            )
            return

        connection_info.update_heartbeat()

        try:
            # Parse message
            data = json.loads(raw_message)
            message_type = data.get("type")
            message_data = data.get("data", {})

            # Handle different message types
            if message_type == MessageType.PING.value:
                pong_message = WebSocketMessage(
                    type=MessageType.PONG,
                    data={"timestamp": datetime.now(dt_timezone.utc).isoformat()},
                    timestamp=datetime.now(dt_timezone.utc),
                )
                await self.send_message_to_connection(connection_id, pong_message)

            elif message_type == MessageType.SUBSCRIBE.value:
                channel = message_data.get("channel")
                if channel:
                    # Authorize admin-only channels before subscribing —
                    # mirrors the connect-time gate so a non-admin cannot
                    # escalate mid-session. For admin channels specifically,
                    # re-resolve the role from the DB rather than trusting the
                    # connect-time snapshot: a connection can outlive a
                    # demotion for the life of a long-lived CLI token (R4-M8).
                    conn = self.active_connections.get(connection_id)
                    if channel in _ADMIN_ONLY_CHANNELS:
                        role = (
                            await _resolve_current_db_role(conn.user_id)
                            if conn
                            else UserRole.USER.value
                        ).lower()
                    else:
                        role = str(
                            (conn.client_info or {}).get("role", "USER")
                            if conn
                            else "USER"
                        ).lower()
                    if channel in _ADMIN_ONLY_CHANNELS and role != "admin":
                        logger.warning(
                            "Denied mid-session subscribe to admin channel %s "
                            "for connection %s (role=%s)",
                            channel,
                            connection_id,
                            role,
                        )
                        # Tell the client explicitly — a silent skip is
                        # indistinguishable from a successful-but-quiet channel.
                        await self.send_message_to_connection(
                            connection_id,
                            WebSocketMessage(
                                type=MessageType.ERROR,
                                data={
                                    "error": "subscription_denied",
                                    "channel": channel,
                                    "reason": "insufficient_role",
                                },
                                timestamp=datetime.now(dt_timezone.utc),
                            ),
                        )
                    else:
                        await self.subscribe_to_channel(connection_id, channel)

            elif message_type == MessageType.UNSUBSCRIBE.value:
                channel = message_data.get("channel")
                if channel:
                    await self.unsubscribe_from_channel(connection_id, channel)

            elif message_type == MessageType.STATUS_UPDATE.value:
                # Update connection status or heartbeat
                await self._handle_status_update(connection_id, message_data)

            else:
                logger.warning(
                    f"Unknown message type: {message_type} from {connection_id}"
                )

        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from {connection_id}: {raw_message}")
        except Exception as e:
            logger.error(f"Error handling message from {connection_id}: {e}")

            error_message = WebSocketMessage(
                type=MessageType.ERROR,
                data={
                    "error": "Message processing failed",
                    "details": str(e) if settings.DEBUG else "Internal error",
                },
                timestamp=datetime.now(dt_timezone.utc),
            )
            await self.send_message_to_connection(connection_id, error_message)

    async def _close_inbound_violation(
        self, connection_id: str, code: int, reason: str
    ) -> None:
        connection_info = self.active_connections.get(connection_id)
        if connection_info is None:
            return
        try:
            await connection_info.websocket.close(code=code, reason=reason)
        except Exception:
            pass  # best-effort close, mirrors the token-expiry path
        await self.disconnect(connection_id, reason)

    async def _handle_status_update(
        self, connection_id: str, status_data: Dict[str, Any]
    ):
        """Handle status update from client"""
        connection_info = self.active_connections[connection_id]

        # Update heartbeat
        connection_info.update_heartbeat()

        # Update message filter if provided
        if "message_filter" in status_data:
            connection_info.message_filter = status_data["message_filter"]

        # Record status update in database
        await self._log_connection_event(
            connection_id=connection_id,
            event_type="status_update",
            user_id=connection_info.user_id,
            organization_id=connection_info.organization_id,
            event_data=status_data,
        )

    async def _heartbeat_monitor(self):
        """Monitor connection heartbeats and detect stale connections"""
        while True:
            try:
                await asyncio.sleep(self.heartbeat_interval)

                current_time = datetime.now(dt_timezone.utc)
                # Naive UTC "now" for expires_at comparisons. TokenData.exp is
                # built via datetime.utcfromtimestamp (naive — see
                # security.py verify_token / token_data.exp < datetime.utcnow()),
                # so this must stay naive too: comparing it against an
                # aware current_time would raise TypeError.
                token_now = datetime.utcnow()
                stale_connections = []
                expired_connections = []

                for connection_id, connection_info in self.active_connections.items():
                    # Check if connection is stale
                    time_since_heartbeat = (
                        current_time - connection_info.last_heartbeat
                    ).total_seconds()

                    if time_since_heartbeat > self.connection_timeout:
                        stale_connections.append(connection_id)
                    elif time_since_heartbeat > self.heartbeat_interval * 2:
                        # Send ping request
                        ping_message = WebSocketMessage(
                            type=MessageType.PING,
                            data={"timestamp": current_time.isoformat()},
                            timestamp=current_time,
                        )
                        await self.send_message_to_connection(
                            connection_id, ping_message
                        )

                    # AU4: a JWT verified once at connect time was never
                    # re-checked, so a revoked/expired session kept receiving
                    # its own org's realtime pushes until the socket happened
                    # to drop. Enforce the token's own exp here.
                    if (
                        connection_info.expires_at is not None
                        and connection_info.expires_at < token_now
                    ):
                        expired_connections.append(connection_id)

                # Clean up stale connections
                for connection_id in stale_connections:
                    await self.disconnect(
                        connection_id, "Connection timeout - no heartbeat"
                    )

                # Close connections whose authentication token has expired
                for connection_id in expired_connections:
                    connection_info = self.active_connections.get(connection_id)
                    if connection_info is None:
                        continue  # already cleaned up above (e.g. also stale)
                    try:
                        await connection_info.websocket.close(
                            code=4002, reason="Authentication token expired"
                        )
                    except Exception:
                        pass  # best-effort close, mirrors shutdown()
                    await self.disconnect(connection_id, "Authentication token expired")

                if stale_connections:
                    logger.info(
                        f"Cleaned up {len(stale_connections)} stale WebSocket connections"
                    )
                if expired_connections:
                    logger.info(
                        f"Closed {len(expired_connections)} WebSocket connections with expired tokens"
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in heartbeat monitor: {e}")
                await asyncio.sleep(5)  # Brief pause before retrying

    async def _cleanup_stale_connections(self):
        """Periodic cleanup of stale connections and expired data"""
        while True:
            try:
                await asyncio.sleep(300)  # Run every 5 minutes

                # Clean up database records
                async with get_async_session() as session:
                    try:
                        # Clean up expired status updates
                        expired_cutoff = datetime.utcnow() - timedelta(hours=24)

                        # This would require adding the delete method to the base model
                        # For now, we'll just log the cleanup need
                        logger.info("Database cleanup task completed")

                    except Exception as e:
                        logger.error(f"Database cleanup error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup task: {e}")
                await asyncio.sleep(30)  # Brief pause before retrying

    async def _dispatch_cluster_message(self, broadcast_data: Dict[str, Any]):
        """Deliver a Redis-forwarded broadcast to LOCAL subscribers only.

        Never calls ``broadcast_to_*`` — those re-publish to Redis, which would
        amplify every forwarded message into an infinite cross-worker storm now
        that ``instance_id`` is a real value. Skips the copy of our own publish
        (Redis fans out back to the sender). Reconstructs target_organization so
        the tenant gate still holds on the receiving worker.
        """
        # Skip the copy of our own publish.
        if broadcast_data.get("source_instance") == self.instance_id:
            return

        message_dict = broadcast_data["message"]
        message = WebSocketMessage(
            type=MessageType(message_dict["type"]),
            data=message_dict["data"],
            timestamp=datetime.fromisoformat(message_dict["timestamp"]),
            message_id=message_dict.get("message_id") or message_dict.get("id"),
            priority=Priority(message_dict["priority"]),
            target_channels=message_dict.get("target_channels", []),
            target_organization=message_dict.get("target_organization"),
        )

        kind = broadcast_data.get("kind", "channel")
        # ``target`` is the new field; fall back to the legacy ``channel`` key so
        # an in-flight message from a not-yet-upgraded worker still routes.
        target = broadcast_data.get("target") or broadcast_data.get("channel")
        if kind == "user":
            await self._deliver_local(self.user_connections.get(target, set()), message)
        elif kind == "organization":
            await self._deliver_local(
                self.organization_connections.get(target, set()), message
            )
        else:
            await self._deliver_local(
                self.channel_subscribers.get(target, set()), message
            )

    async def _redis_message_listener(self):
        """Listen for broadcast messages from other instances"""
        if not self.redis_client:
            return

        while True:
            try:
                message = await self.redis_pubsub.get_message(timeout=1.0)
                if message and message["type"] == "message":
                    try:
                        broadcast_data = json.loads(message["data"])
                        await self._dispatch_cluster_message(broadcast_data)
                    except (json.JSONDecodeError, KeyError, ValueError) as e:
                        logger.error(f"Invalid broadcast message format: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Redis message listener error: {e}")
                await asyncio.sleep(1)

    async def _log_connection_event(
        self,
        connection_id: str,
        event_type: str,
        user_id: str,
        organization_id: str,
        client_info: Dict = None,
        event_data: Dict = None,
    ):
        """Log connection event to database"""
        try:
            async with get_async_session() as session:
                # For now, just log the event - full database implementation
                # would require proper async session handling
                logger.debug(f"Connection event: {event_type} for {connection_id}")

        except Exception as e:
            logger.error(f"Failed to log connection event: {e}")

    async def _record_message_metrics(
        self, connection_id: str, direction: str, size_bytes: int
    ):
        """Record message metrics in database"""
        try:
            # For now, just log metrics - full implementation would update
            # the WebSocketConnection record in the database
            logger.debug(
                f"Message metrics: {direction} {size_bytes} bytes to {connection_id}"
            )

        except Exception as e:
            logger.error(f"Failed to record message metrics: {e}")

    def get_connection_stats(self) -> Dict[str, Any]:
        """Get connection statistics"""
        return {
            "total_connections": len(self.active_connections),
            "unique_users": len(self.user_connections),
            "unique_organizations": len(self.organization_connections),
            "channel_subscriptions": {
                channel: len(subscribers)
                for channel, subscribers in self.channel_subscribers.items()
            },
            "max_connections": self.max_connections,
            "max_connections_per_user": self.max_connections_per_user,
            "redis_enabled": self.redis_client is not None,
        }

    def get_user_connections(self, user_id: str) -> List[str]:
        """Get all connection IDs for a user"""
        return list(self.user_connections.get(user_id, set()))

    def get_organization_connections(self, organization_id: str) -> List[str]:
        """Get all connection IDs for an organization"""
        return list(self.organization_connections.get(organization_id, set()))


# Global connection manager instance
connection_manager = EnhancedConnectionManager()

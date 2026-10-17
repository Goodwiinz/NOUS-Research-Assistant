# WebSocket Services Documentation

> **Path check 2026-10-17: component file locations below were updated to the current tree, but the feature descriptions (Redis clustering, circuit breakers, batching) were not re-verified against the code. See `backend/src/websocket/websocket_api.py` and the services named below for the real behavior.**

## Overview

The Multimodal Enterprise RAG System includes a comprehensive WebSocket infrastructure that provides real-time updates for document processing, job status, system notifications, and more. This documentation covers the architecture, usage, and configuration of the WebSocket services.

## Architecture

### Core Components

#### 1. Enhanced Connection Manager (`backend/src/services/websocket/websocket_manager.py`)
- **Purpose**: Manages WebSocket connections with enterprise-grade features
- **Features**:
  - JWT authentication
  - Connection pooling and load balancing
  - Redis clustering support for multi-instance deployments
  - Heartbeat monitoring with automatic reconnection
  - Channel-based message routing
  - Connection metrics and monitoring
  - Circuit breaker patterns for fault tolerance

#### 2. Status Update Service (`backend/src/services/infrastructure/status_update_service.py`)
- **Purpose**: Provides real-time status updates with intelligent throttling
- **Features**:
  - Document processing progress tracking
  - Job status notifications
  - System-wide status broadcasting
  - Message batching for performance optimization
  - Priority-based message delivery
  - Subscription management

#### 3. Processing Integration (`backend/src/services/processing/processing_integration.py`)
- **Purpose**: Integrates WebSocket updates with document processing pipeline
- **Features**:
  - Real-time processing progress updates
  - Multi-stage processing status tracking
  - Error and warning notifications
  - Processing context management
  - Automatic status synchronization

#### 4. Error Handler (`backend/src/services/websocket/websocket_error_handler.py`)
- **Purpose**: Comprehensive error handling and recovery
- **Features**:
  - Automatic error classification and severity assessment
  - Multiple recovery strategies (reconnect, retry, fail-fast)
  - Circuit breaker patterns
  - Exponential backoff for reconnections
  - Error analytics and reporting

#### 5. Service Initializer (`backend/src/services/websocket/websocket_service_initializer.py`)
- **Purpose**: Orchestrates initialization and lifecycle of all WebSocket services
- **Features**:
  - Coordinated service startup/shutdown
  - Health monitoring
  - Service integration management

### Database Schema

The WebSocket services use the following database tables:

#### `websocket_connections`
- Tracks active WebSocket connections
- Stores connection metadata and metrics
- Supports connection cleanup and monitoring

#### `status_updates`
- Records all status update messages
- Enables message tracking and delivery confirmation
- Supports message history and analytics

#### `connection_events`
- Logs connection lifecycle events
- Provides audit trail for troubleshooting
- Supports performance analysis

#### `notification_templates`
- Stores reusable notification templates
- Supports localization and customization
- Enables dynamic notification generation

## API Reference

### WebSocket Endpoints

#### `/api/v2/ws/connect`
Enhanced WebSocket connection endpoint with comprehensive features.

**Parameters:**
- `token` (query, required): JWT authentication token
- `channels` (query, optional): Comma-separated list of channels
- `frequency` (query, optional): Update frequency (realtime, frequent, normal, periodic)
- `client_info` (query, optional): JSON-encoded client information

**Example Connection URL:**
```
ws://localhost:8000/api/v2/ws/connect?token=jwt_token&channels=document_processing,job_status&frequency=realtime
```

#### `/api/v2/ws/status`
Get WebSocket service statistics and health information.

**Response:**
```json
{
  "websocket_service": {
    "status": "healthy",
    "version": "2.0.0",
    "features": {...}
  },
  "connections": {
    "total_connections": 150,
    "unique_users": 45,
    "unique_organizations": 12
  },
  "channels": {
    "document_processing": {
      "subscribers": 25,
      "message_rate": "realtime"
    }
  }
}
```

#### `/api/v2/ws/health`
WebSocket service health check for monitoring systems.

#### `/api/v2/ws/channels`
Get available WebSocket channels and their descriptions.

### HTTP Endpoints

#### `/api/v2/ws/broadcast`
Broadcast a message to WebSocket subscribers.

**Request Body:**
```json
{
  "message_type": "system_notification",
  "data": {
    "title": "System Maintenance",
    "message": "Scheduled maintenance in 1 hour"
  },
  "channel": "system_status",
  "priority": "high"
}
```

#### `/api/v2/ws/connections/{user_id}`
Get active connections for a specific user (admin access required).

#### `/api/v2/ws/test-connection`
Send a test message to a specific WebSocket connection.

## Message Formats

### Standard Message Structure
```json
{
  "id": "uuid",
  "type": "message_type",
  "data": {...},
  "timestamp": "2024-01-01T12:00:00Z",
  "priority": "low|normal|high|critical"
}
```

### Document Processing Messages
```json
{
  "id": "uuid",
  "type": "document_processing",
  "data": {
    "document_id": "uuid",
    "title": "Document Title",
    "processing_status": "processing",
    "progress_percentage": 75.0,
    "current_step": "entity_extraction",
    "total_steps": 5,
    "estimated_remaining_seconds": 120,
    "warnings": [],
    "errors": []
  },
  "timestamp": "2024-01-01T12:00:00Z"
}
```

### Job Status Messages
```json
{
  "id": "uuid",
  "type": "job_status",
  "data": {
    "job_id": "uuid",
    "job_type": "document_ingestion",
    "status": "running",
    "progress_percentage": 60.0,
    "current_step": "embedding_generation",
    "started_at": "2024-01-01T12:00:00Z",
    "duration_seconds": 45
  },
  "timestamp": "2024-01-01T12:00:00Z"
}
```

### System Notifications
```json
{
  "id": "uuid",
  "type": "system_notification",
  "data": {
    "title": "System Alert",
    "message": "High system load detected",
    "type": "warning",
    "action_url": "/admin/dashboard"
  },
  "timestamp": "2024-01-01T12:00:00Z"
}
```

## Channels

### Available Channels

#### `document_processing`
- **Purpose**: Real-time document processing updates
- **Message Types**: `document_processing`
- **Update Frequency**: Realtime (100-500ms)
- **Permissions**: User
- **Example Use Cases**: File upload progress, text extraction status, embedding generation

#### `job_status`
- **Purpose**: Background job status updates
- **Message Types**: `job_status`
- **Update Frequency**: Frequent (1-2s)
- **Permissions**: User
- **Example Use Cases**: Batch processing jobs, system maintenance tasks

#### `system_status`
- **Purpose**: System-wide status and metrics
- **Message Types**: `system_notification`
- **Update Frequency**: Periodic (30-60s)
- **Permissions**: Admin
- **Example Use Cases**: System load alerts, resource usage warnings

#### `user_notifications`
- **Purpose**: User-specific notifications
- **Message Types**: `system_notification`
- **Update Frequency**: Immediate
- **Permissions**: User
- **Example Use Cases**: Task completion alerts, quota warnings

#### `quota_alerts`
- **Purpose**: Storage and processing quota alerts
- **Message Types**: `system_notification`
- **Update Frequency**: Immediate
- **Permissions**: User
- **Example Use Cases**: Storage limit warnings, processing quota exceeded

#### `quality_metrics`
- **Purpose**: Quality evaluation metrics
- **Message Types**: `status_update`
- **Update Frequency**: Normal (5-10s)
- **Permissions**: User
- **Example Use Cases**: RAG evaluation results, accuracy scores

#### `admin_alerts`
- **Purpose**: Administrative alerts
- **Message Types**: `system_notification`, `error`
- **Update Frequency**: Immediate
- **Permissions**: Admin
- **Example Use Cases**: Security alerts, system failures

## Client Implementation Guide

### JavaScript/TypeScript Client

```typescript
class WebSocketClient {
  private ws: WebSocket | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000;

  constructor(
    private token: string,
    private channels: string[] = [],
    private frequency: 'realtime' | 'frequent' | 'normal' | 'periodic' = 'normal'
  ) {}

  async connect(): Promise<void> {
    const url = new URL('ws://localhost:8000/api/v2/ws/connect');
    url.searchParams.set('token', this.token);
    url.searchParams.set('channels', this.channels.join(','));
    url.searchParams.set('frequency', this.frequency);

    this.ws = new WebSocket(url.toString());

    this.ws.onopen = () => {
      console.log('WebSocket connected');
      this.reconnectAttempts = 0;
    };

    this.ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      this.handleMessage(message);
    };

    this.ws.onclose = () => {
      console.log('WebSocket disconnected');
      this.handleReconnect();
    };

    this.ws.onerror = (error) => {
      console.error('WebSocket error:', error);
    };
  }

  private handleMessage(message: any): void {
    switch (message.type) {
      case 'document_processing':
        this.onDocumentProcessing(message.data);
        break;
      case 'job_status':
        this.onJobStatus(message.data);
        break;
      case 'system_notification':
        this.onSystemNotification(message.data);
        break;
      case 'ping':
        this.sendPong();
        break;
      default:
        console.log('Unknown message type:', message.type);
    }
  }

  private handleReconnect(): void {
    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++;
      const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1);

      setTimeout(() => {
        console.log(`Reconnecting... Attempt ${this.reconnectAttempts}`);
        this.connect();
      }, delay);
    }
  }

  private sendPong(): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({
        type: 'pong',
        timestamp: new Date().toISOString()
      }));
    }
  }

  // Event handlers
  onDocumentProcessing(data: any): void {
    // Handle document processing updates
  }

  onJobStatus(data: any): void {
    // Handle job status updates
  }

  onSystemNotification(data: any): void {
    // Handle system notifications
  }

  // Public methods
  subscribe(channel: string): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({
        type: 'subscribe',
        data: { channel }
      }));
    }
  }

  unsubscribe(channel: string): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({
        type: 'unsubscribe',
        data: { channel }
      }));
    }
  }

  sendPing(): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({
        type: 'ping',
        timestamp: new Date().toISOString()
      }));
    }
  }

  disconnect(): void {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}

// Usage example
const client = new WebSocketClient(
  'your-jwt-token',
  ['document_processing', 'job_status'],
  'realtime'
);

client.connect();

// Subscribe to additional channels
client.subscribe('user_notifications');
```

### Python Client

```python
import asyncio
import json
import websockets
from typing import List, Callable, Optional

class WebSocketClient:
    def __init__(
        self,
        token: str,
        channels: List[str] = None,
        frequency: str = 'normal',
        base_url: str = 'ws://localhost:8000'
    ):
        self.token = token
        self.channels = channels or []
        self.frequency = frequency
        self.base_url = base_url
        self.websocket = None
        self.message_handlers = {}

    async def connect(self):
        """Connect to WebSocket server"""
        url = f"{self.base_url}/api/v2/ws/connect"
        params = {
            'token': self.token,
            'channels': ','.join(self.channels),
            'frequency': self.frequency
        }

        query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        full_url = f"{url}?{query_string}"

        self.websocket = await websockets.connect(full_url)

        # Start message handling
        asyncio.create_task(self._message_loop())

    async def _message_loop(self):
        """Handle incoming messages"""
        try:
            async for message in self.websocket:
                data = json.loads(message)
                await self._handle_message(data)
        except websockets.exceptions.ConnectionClosed:
            print("WebSocket connection closed")
        except Exception as e:
            print(f"WebSocket error: {e}")

    async def _handle_message(self, message: dict):
        """Handle incoming message"""
        message_type = message.get('type')
        handler = self.message_handlers.get(message_type)

        if handler:
            await handler(message.get('data', {}))
        else:
            print(f"Unknown message type: {message_type}")

    def on_message(self, message_type: str, handler: Callable):
        """Register message handler"""
        self.message_handlers[message_type] = handler

    async def send_message(self, message_type: str, data: dict):
        """Send message to server"""
        if self.websocket:
            message = {
                'type': message_type,
                'data': data,
                'timestamp': datetime.now().isoformat()
            }
            await self.websocket.send(json.dumps(message))

    async def subscribe(self, channel: str):
        """Subscribe to channel"""
        await self.send_message('subscribe', {'channel': channel})

    async def unsubscribe(self, channel: str):
        """Unsubscribe from channel"""
        await self.send_message('unsubscribe', {'channel': channel})

    async def ping(self):
        """Send ping to server"""
        await self.send_message('ping', {})

    async def close(self):
        """Close WebSocket connection"""
        if self.websocket:
            await self.websocket.close()

# Usage example
async def main():
    client = WebSocketClient(
        token='your-jwt-token',
        channels=['document_processing', 'job_status'],
        frequency='realtime'
    )

    # Register message handlers
    @client.on_message('document_processing')
    async def handle_document_processing(data):
        print(f"Document processing update: {data}")

    @client.on_message('system_notification')
    async def handle_system_notification(data):
        print(f"System notification: {data}")

    # Connect and handle messages
    await client.connect()

    # Subscribe to additional channel
    await client.subscribe('user_notifications')

    # Keep running
    await asyncio.Future()  # Run forever

if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

### Environment Variables

```bash
# WebSocket Configuration
WS_HEARTBEAT_INTERVAL=30
WS_CONNECTION_TIMEOUT=300
WS_MAX_CONNECTIONS=10000
WS_BATCH_SIZE=50
WS_BATCH_TIMEOUT=2.0

# Redis Configuration (for clustering)
REDIS_URL=redis://localhost:6379
REDIS_WS_CHANNEL=websocket_broadcast

# Update Frequencies
WS_UPDATE_FREQUENCY_REALTIME=0.1
WS_UPDATE_FREQUENCY_FREQUENT=1.0
WS_UPDATE_FREQUENCY_NORMAL=5.0
WS_UPDATE_FREQUENCY_PERIODIC=30.0
```

### Service Configuration

The WebSocket services can be configured through the following settings:

#### Connection Manager Settings
```python
{
    "heartbeat_interval": 30,  # seconds
    "connection_timeout": 300,  # 5 minutes
    "max_connections": 10000,
    "redis_enabled": True,
    "clustering_enabled": True
}
```

#### Status Update Service Settings
```python
{
    "batch_size": 50,
    "batch_timeout": 2.0,  # seconds
    "max_cache_age": 300,  # 5 minutes
    "default_frequency": "normal"
}
```

#### Error Handler Settings
```python
{
    "max_error_history": 1000,
    "reconnection_backoff_base": 1.0,
    "reconnection_backoff_max": 30.0,
    "circuit_breaker_threshold": 5,
    "circuit_breaker_timeout": 60
}
```

## Deployment

### Single Instance Deployment

For development or small deployments:

1. Start the FastAPI application with WebSocket services
2. Configure local Redis for connection state
3. Monitor through the `/api/v2/ws/status` endpoint

### Multi-Instance Deployment

For production deployments:

1. Deploy Redis cluster for connection state sharing
2. Use load balancer with WebSocket support
3. Configure each instance with the same Redis cluster
4. Monitor all instances through health checks

#### Docker Compose Example

```yaml
version: '3.8'

services:
  backend:
    build: ./backend
    ports:
      - "8000:8000"
    environment:
      - REDIS_URL=redis://redis:6379
      - WS_REDIS_ENABLED=true
    depends_on:
      - redis

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    command: redis-server --appendonly yes

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
    depends_on:
      - backend
```

### Monitoring and Observability

#### Health Checks
- `/api/v2/ws/health` - WebSocket service health
- `/api/v2/ws/status` - Detailed status and metrics

#### Metrics
- Connection count and distribution
- Message throughput and latency
- Error rates and types
- Resource utilization

#### Logging
- Connection lifecycle events
- Error messages and stack traces
- Performance metrics
- Security events

## Troubleshooting

### Common Issues

#### 1. Connection Failures
- **Symptoms**: WebSocket connections failing to establish
- **Causes**: Invalid JWT tokens, network issues, service unavailability
- **Solutions**:
  - Verify JWT token validity
  - Check network connectivity
  - Monitor service health endpoints

#### 2. High Memory Usage
- **Symptoms**: Memory usage increasing over time
- **Causes**: Connection leaks, large message queues
- **Solutions**:
  - Monitor connection cleanup
  - Adjust batch sizes and timeouts
  - Check Redis memory usage

#### 3. Message Delivery Issues
- **Symptoms**: Messages not reaching clients
- **Causes**: Channel subscription issues, message filtering
- **Solutions**:
  - Verify channel subscriptions
  - Check message filters
  - Monitor message queues

#### 4. Performance Degradation
- **Symptoms**: Slow message delivery, high latency
- **Causes**: High connection load, inefficient batching
- **Solutions**:
  - Optimize batch sizes
  - Implement connection limits
  - Scale horizontally

### Debugging Tools

#### WebSocket Testing
```bash
# Test WebSocket connection with wscat
wscat -c "ws://localhost:8000/api/v2/ws/connect?token=your_token"

# Test with curl (HTTP endpoints)
curl http://localhost:8000/api/v2/ws/status
```

#### Redis Monitoring
```bash
# Monitor Redis keys
redis-cli --scan --pattern "ws:*"

# Check pub/sub channels
redis-cli pubsub channels
```

#### Log Analysis
```bash
# Filter WebSocket logs
grep "WebSocket" /var/log/app.log

# Monitor connection events
grep "ConnectionEvent" /var/log/app.log
```

## Security Considerations

### Authentication
- JWT token validation for all connections
- Token expiration handling
- User permission verification

### Authorization
- Channel-based access control
- User-specific message filtering
- Organization-level restrictions

### Data Protection
- Message encryption in transit
- Sensitive data filtering
- Audit logging for compliance

### Rate Limiting
- Connection rate limiting
- Message rate limiting per user
- DDoS protection measures

## Best Practices

### Client Implementation
1. Implement exponential backoff for reconnections
2. Handle different message types appropriately
3. Provide user feedback for connection status
4. Implement graceful degradation

### Server Configuration
1. Set appropriate connection limits
2. Configure Redis clustering for scalability
3. Implement proper monitoring and alerting
4. Regular performance testing and optimization

### Message Design
1. Keep messages small and efficient
2. Use appropriate message priorities
3. Implement message batching for high-frequency updates
4. Provide clear error messages and recovery guidance

### Error Handling
1. Implement comprehensive error logging
2. Provide meaningful error messages to clients
3. Use circuit breakers for fault tolerance
4. Monitor error rates and patterns
# Real-time Processing API Reference

## Overview

The Real-time Processing API provides RESTful endpoints for managing WebSocket connections, subscribing to document updates, and monitoring system metrics. All endpoints require JWT authentication.

## Base URL

```
http://localhost:8000/api/v2/realtime
```

## Authentication

All API endpoints require JWT authentication:

```http
Authorization: Bearer <your-jwt-token>
```

## Endpoints

### Document Status Subscription

#### Subscribe to Document Updates

Subscribe to real-time status updates for a specific document.

**Endpoint:** `POST /documents/status/subscribe`

**Request Body:**
```json
{
  "document_id": "uuid-string",
  "channels": ["document_processing"],
  "frequency": "realtime",
  "filters": {
    "status": ["processing", "completed"]
  }
}
```

**Response:**
```json
{
  "success": true,
  "message": "Subscribed to real-time updates for document uuid",
  "document_id": "uuid-string",
  "subscribed_connections": 3,
  "channels": ["document_processing"],
  "frequency": "realtime",
  "websocket_endpoint": "ws://localhost:8000/api/v2/ws/connect"
}
```

**Parameters:**
- `document_id` (string, required): Document ID to track
- `channels` (array, optional): WebSocket channels to subscribe to
- `frequency` (enum, optional): Update frequency - `realtime`, `frequent`, `normal`, `periodic`
- `filters` (object, optional): Message filtering criteria

#### Get Document Status

Get enhanced real-time status for a specific document with detailed processing information.

**Endpoint:** `GET /documents/{document_id}/status`

**Query Parameters:**
- `include_progress` (boolean, default: true): Include detailed progress information
- `include_stages` (boolean, default: true): Include processing stage details
- `include_jobs` (boolean, default: false): Include processing job information

**Response:**
```json
{
  "id": "uuid-string",
  "filename": "document.pdf",
  "title": "My Document",
  "document_type": "pdf",
  "file_size_bytes": 1048576,
  "processing_status": "processing",
  "overall_progress": 65.5,
  "current_stage": {
    "id": "stage-uuid",
    "name": "Embedding Generation",
    "description": "Generating vector embeddings",
    "status": "in_progress",
    "progress": 75.0,
    "started_at": "2025-11-21T10:30:00Z",
    "completed_at": null,
    "duration_seconds": null,
    "error": null,
    "metadata": {
      "job_type": "embedding_generation",
      "worker_id": "worker-123"
    }
  },
  "stages": [
    {
      "id": "stage-1",
      "name": "OCR Processing",
      "status": "completed",
      "progress": 100.0,
      "started_at": "2025-11-21T10:25:00Z",
      "completed_at": "2025-11-21T10:27:30Z",
      "duration_seconds": 150.0
    },
    {
      "id": "stage-2",
      "name": "Text Extraction",
      "status": "completed",
      "progress": 100.0,
      "started_at": "2025-11-21T10:27:30Z",
      "completed_at": "2025-11-21T10:29:00Z",
      "duration_seconds": 90.0
    }
  ],
  "upload_progress": 100.0,
  "processing_started_at": "2025-11-21T10:25:00Z",
  "processing_completed_at": null,
  "estimated_completion_time": "2025-11-21T10:35:00Z",
  "processing_error": null,
  "retry_count": 0,
  "can_retry": false,
  "actions": {
    "pause": false,
    "resume": false,
    "cancel": true,
    "retry": false,
    "download": false
  },
  "created_at": "2025-11-21T10:20:00Z",
  "updated_at": "2025-11-21T10:30:00Z",
  "websocket_subscribers": 3,
  "last_status_update": "2025-11-21T10:30:00Z",
  "update_frequency": "realtime",
  "is_realtime_enabled": true
}
```

#### Get Bulk Document Status

Get status for multiple documents in a single request. Optimized for dashboard views.

**Endpoint:** `POST /documents/bulk/status`

**Request Body:**
```json
{
  "document_ids": ["uuid-1", "uuid-2", "uuid-3"],
  "include_jobs": true,
  "include_stages": true,
  "group_by_status": false
}
```

**Response:**
```json
{
  "documents": {
    "uuid-1": {
      "id": "uuid-1",
      "filename": "doc1.pdf",
      "processing_status": "processing",
      "updated_at": "2025-11-21T10:30:00Z"
    },
    "uuid-2": {
      "id": "uuid-2",
      "filename": "doc2.pdf",
      "processing_status": "completed",
      "updated_at": "2025-11-21T10:25:00Z"
    }
  },
  "total_requested": 3,
  "found_count": 2,
  "missing_count": 1,
  "missing_document_ids": ["uuid-3"],
  "status_groups": {
    "processing": ["uuid-1"],
    "completed": ["uuid-2"]
  }
}
```

### System Monitoring

#### Get System Metrics

Get comprehensive system-wide processing metrics and WebSocket connection statistics.

**Endpoint:** `GET /system/metrics`

**Response:**
```json
{
  "active_connections": 127,
  "total_documents": 15420,
  "queued_documents": 23,
  "processing_documents": 45,
  "completed_documents_today": 342,
  "failed_documents_today": 8,
  "average_processing_time_seconds": 127.5,
  "system_load_percentage": 45.2,
  "websocket_connections_by_channel": {
    "document_processing": 89,
    "job_status": 25,
    "system_status": 38,
    "user_notifications": 67
  },
  "messages_sent_last_hour": 15234,
  "error_rate_last_hour": 2.1,
  "timestamp": "2025-11-21T10:30:00Z"
}
```

### Connection Management

#### Get Connection Status

Get real-time connection status for the current user, including active WebSocket connections and subscriptions.

**Endpoint:** `GET /connections/status`

**Response:**
```json
{
  "user_id": "user-uuid",
  "active_connections": 2,
  "total_subscriptions": 8,
  "connections": [
    {
      "connection_id": "conn-abc123",
      "connected_at": "2025-11-21T10:25:00Z",
      "last_heartbeat": "2025-11-21T10:30:00Z",
      "subscribed_channels": ["document_processing", "system_status"],
      "client_info": {
        "browser": "Chrome",
        "version": "119.0.0",
        "platform": "MacOS"
      },
      "message_filter": {
        "status": ["processing", "completed"]
      }
    }
  ],
  "websocket_endpoint": "ws://localhost:8000/api/v2/ws/connect",
  "available_channels": [
    {
      "name": "document_processing",
      "description": "Document Processing"
    },
    {
      "name": "job_status",
      "description": "Job Status"
    },
    {
      "name": "system_status",
      "description": "System Status"
    }
  ]
}
```

#### Manual Status Broadcast

Trigger a manual status broadcast for a document. Useful for testing or refreshing client status.

**Endpoint:** `POST /documents/{document_id}/broadcast-status`

**Response:**
```json
{
  "success": true,
  "message": "Status broadcast triggered for document uuid",
  "document_id": "uuid-string",
  "status": "processing",
  "timestamp": "2025-11-21T10:30:00Z"
}
```

## WebSocket Endpoints

### WebSocket Connection

#### Connect to WebSocket

**URL:** `ws://localhost:8000/api/v2/ws/connect`

**Query Parameters:**
- `token` (required): JWT authentication token
- `channels` (optional): Comma-separated list of channels to subscribe to
- `frequency` (optional): Update frequency - `realtime`, `frequent`, `normal`, `periodic`
- `client_info` (optional): JSON-encoded client information

**Example:**
```
ws://localhost:8000/api/v2/ws/connect?token=jwt-token&channels=document_processing,system_status&frequency=realtime
```

### WebSocket Messages

#### Client to Server Messages

**Subscribe to Channel**
```json
{
  "type": "subscribe",
  "data": {
    "channel": "document_processing"
  }
}
```

**Ping (Heartbeat)**
```json
{
  "type": "ping",
  "data": {
    "timestamp": "2025-11-21T10:30:00Z"
  }
}
```

#### Server to Client Messages

**Connection Established**
```json
{
  "id": "msg-uuid",
  "type": "connect",
  "data": {
    "connection_id": "conn-abc123",
    "user_id": "user-uuid",
    "server_capabilities": {
      "channels": ["document_processing", "job_status"],
      "message_types": ["document_processing", "system_notification"],
      "priorities": ["low", "normal", "high", "critical"]
    }
  },
  "timestamp": "2025-11-21T10:25:00Z"
}
```

**Document Processing Update**
```json
{
  "id": "msg-def456",
  "type": "document_processing",
  "data": {
    "document_id": "doc-uuid",
    "title": "My Document",
    "filename": "document.pdf",
    "processing_status": "processing",
    "progress_percentage": 65.5,
    "current_step": "Embedding Generation",
    "estimated_remaining_seconds": 120.0,
    "warnings": [],
    "errors": [],
    "updated_at": "2025-11-21T10:30:00Z"
  },
  "timestamp": "2025-11-21T10:30:00Z",
  "priority": "normal"
}
```

**Batch Status Updates**
```json
{
  "id": "msg-ghi789",
  "type": "status_update",
  "data": {
    "batch": true,
    "update_type": "document_processing",
    "updates": [
      {
        "id": "msg-1",
        "type": "document_processing",
        "data": { "document_id": "doc-1", "progress": 75.0 },
        "timestamp": "2025-11-21T10:30:00Z"
      },
      {
        "id": "msg-2",
        "type": "document_processing",
        "data": { "document_id": "doc-2", "status": "completed" },
        "timestamp": "2025-11-21T10:29:00Z"
      }
    ],
    "total_updates": 2
  },
  "timestamp": "2025-11-21T10:30:00Z"
}
```

## Data Models

### ProcessingStage

```typescript
interface ProcessingStage {
  id: string;
  name: string;
  description: string;
  progress: number;        // 0-100
  status: string;         // 'pending' | 'in_progress' | 'completed' | 'failed' | 'skipped'
  started_at?: string;
  completed_at?: string;
  duration_seconds?: number;
  error?: string;
  metadata?: Record<string, any>;
}
```

### DocumentProcessingState

```typescript
interface DocumentProcessingState {
  id: string;
  filename: string;
  fileType: 'pdf' | 'txt' | 'jpg' | 'png' | 'mp3' | 'mp4';
  overallProgress: number;     // 0-100
  currentStage: ProcessingStage;
  stages: ProcessingStage[];
  status: 'queued' | 'uploading' | 'processing' | 'completed' | 'failed' | 'paused' | 'cancelled';
  uploadProgress: number;     // 0-100
  metadata: {
    fileSize: number;
    pageCount?: number;
    duration?: number;
    uploadStartedAt: string;
    processingStartedAt?: string;
    completedAt?: string;
    estimatedTimeRemaining?: number;
  };
  error?: string;
  retryCount: number;
  canRetry: boolean;
  actions: {
    pause: boolean;
    resume: boolean;
    cancel: boolean;
    retry: boolean;
    download: boolean;
  };
}
```

### WebSocketMessage

```typescript
interface WebSocketMessage {
  id: string;
  type: string;
  data: any;
  timestamp: string;
  priority: 'low' | 'normal' | 'high' | 'critical';
  target_channels?: string[];
  expires_at?: string;
}
```

## Error Responses

All API endpoints return error responses in the following format:

```json
{
  "error": {
    "message": "Error description",
    "status_code": 400,
    "type": "validation_error"
  }
}
```

### Common Error Codes

- `400`: Bad Request - Invalid parameters or missing required fields
- `401`: Unauthorized - Invalid or missing JWT token
- `403`: Forbidden - Insufficient permissions
- `404`: Not Found - Resource not found
- `429`: Too Many Requests - Rate limit exceeded
- `500`: Internal Server Error - Server error occurred

## Rate Limiting

- **Default Rate Limit**: 100 requests per minute per user
- **WebSocket Connections**: 10 connections per user
- **Message Frequency**: Throttled based on subscription frequency

## Examples

### Example 1: Complete Document Tracking Flow

```bash
# 1. Upload document
curl -X POST http://localhost:8000/api/v1/files/upload \
  -H "Authorization: Bearer token" \
  -F "file=@document.pdf" \
  -F "title=My Document"

# 2. Subscribe to real-time updates
curl -X POST http://localhost:8000/api/v2/realtime/documents/status/subscribe \
  -H "Authorization: Bearer token" \
  -H "Content-Type: application/json" \
  -d '{"document_id": "doc-uuid", "frequency": "realtime"}'

# 3. Start processing
curl -X POST http://localhost:8000/api/v1/processing/documents/doc-uuid/process \
  -H "Authorization: Bearer token"

# 4. Monitor progress
curl -H "Authorization: Bearer token" \
  http://localhost:8000/api/v2/realtime/documents/doc-uuid/status
```

### Example 2: Bulk Status Monitoring

```bash
curl -X POST http://localhost:8000/api/v2/realtime/documents/bulk/status \
  -H "Authorization: Bearer token" \
  -H "Content-Type: application/json" \
  -d '{
    "document_ids": ["doc-1", "doc-2", "doc-3"],
    "include_jobs": true,
    "group_by_status": true
  }'
```

### Example 3: WebSocket Integration

```javascript
// Connect to WebSocket
const ws = new WebSocket(
  'ws://localhost:8000/api/v2/ws/connect?token=jwt-token&channels=document_processing'
);

ws.onopen = () => {
  // Subscribe to specific document
  ws.send(JSON.stringify({
    type: 'subscribe',
    data: {
      channel: 'document_processing',
      documentId: 'doc-uuid'
    }
  }));
};

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);

  if (message.type === 'document_processing') {
    const { document_id, progress, status } = message.data;
    console.log(`Document ${document_id}: ${status} (${progress}%)`);
  }
};
```

## Testing

### Unit Tests

```bash
# Test API endpoints
pytest tests/test_realtime_api.py

# Test WebSocket connections
pytest tests/test_websocket_connections.py

# Test status updates
pytest tests/test_status_broadcasts.py
```

### Integration Tests

```bash
# Test end-to-end flow
pytest tests/integration/test_realtime_document_flow.py

# Test connection scaling
pytest tests/performance/test_websocket_scaling.py
```

## Monitoring and Debugging

### Health Checks

```bash
# WebSocket service health
curl http://localhost:8000/api/v2/ws/health

# Real-time API health
curl http://localhost:8000/api/v2/realtime/documents/health
```

### Debug Endpoints

```bash
# Connection statistics
curl http://localhost:8000/api/v2/ws/status

# System metrics
curl http://localhost:8000/api/v2/realtime/system/metrics

# Service status
curl http://localhost:8000/api/v2/realtime/services/status
```

### Logging

Enable debug logging by setting environment variable:

```bash
export LOG_LEVEL=DEBUG
```

Log categories:
- `websocket_manager`: WebSocket connection management
- `status_update_service`: Status update broadcasting
- `document_realtime_service`: Document event tracking
- `realtime_api`: REST API endpoints

---

For additional information, refer to:
- [Real-time Processing Guide](../REALTIME_PROCESSING_GUIDE.md)
- [WebSocket Services Documentation](../websocket_services.md)
- [Quick Start Guide](../archive/guides/REALTIME_QUICK_START.md)
# Real-time Document Processing Guide

## Overview

The Multimodal Enterprise RAG System now includes a comprehensive real-time document processing status system that provides live updates for document processing progress through WebSocket connections. This guide covers the architecture, implementation, usage, and deployment of the real-time processing features.

## Table of Contents

- [Architecture](#architecture)
- [Frontend Implementation](#frontend-implementation)
- [Backend API](#backend-api)
- [WebSocket Integration](#websocket-integration)
- [Database Schema](#database-schema)
- [Usage Examples](#usage-examples)
- [Performance Considerations](#performance-considerations)
- [Troubleshooting](#troubleshooting)

## Architecture

### System Components

#### 1. WebSocket Connection Manager
- **Location**: `backend/src/services/websocket_manager.py`
- **Purpose**: Enterprise-grade WebSocket connection management
- **Capacity**: Supports 10,000+ concurrent connections
- **Features**:
  - JWT authentication and authorization
  - Redis clustering for multi-instance deployments
  - Automatic reconnection with exponential backoff
  - Heartbeat monitoring and connection health checks
  - Channel-based message routing
  - Connection metrics and performance monitoring

#### 2. Status Update Service
- **Location**: `backend/src/services/status_update_service.py`
- **Purpose**: Real-time status broadcasting with intelligent throttling
- **Features**:
  - Document processing progress tracking
  - Job status notifications
  - System-wide metrics broadcasting
  - Message batching and prioritization
  - Subscription management

#### 3. Document Real-time Service
- **Location**: `backend/src/services/document_realtime_service.py`
- **Purpose**: Document-specific event tracking and broadcasting
- **Features**:
  - Multi-stage processing progress monitoring
  - Event history and analytics
  - ETA calculations based on processing patterns
  - Error and warning notifications
  - Automatic cleanup of stale data

#### 4. Real-time Status API
- **Location**: `backend/src/api/realtime_document_status.py`
- **Purpose**: RESTful APIs for real-time document status management
- **Endpoints**:
  - Document subscription management
  - Enhanced status retrieval
  - Bulk status operations
  - System metrics and monitoring

### Frontend Architecture

#### 1. React Components
- **Location**: `frontend/app/components/realtime/`
- **Components**:
  - `RealtimeStatusDashboard.tsx` - Main dashboard view
  - `DocumentProgressVisualizer.tsx` - Interactive progress timeline
  - Connection status indicators and system metrics

#### 2. State Management
- **Location**: `frontend/src/store/realtime-store.ts`
- **Technology**: Zustand with TypeScript
- **Features**:
  - WebSocket connection state management
  - Document subscription handling
  - Real-time status updates
  - Connection metrics tracking

#### 3. WebSocket Client Service
- **Location**: `frontend/src/services/realtime-websocket-service.ts`
- **Features**:
  - Automatic connection management
  - Message buffering and batching
  - Reconnection logic
  - Performance monitoring

## Frontend Implementation

### TypeScript Types

The frontend uses comprehensive TypeScript definitions for type safety:

```typescript
// Core types for real-time processing
export interface DocumentProcessingState {
  id: string;
  filename: string;
  fileType: 'pdf' | 'txt' | 'jpg' | 'png' | 'mp3' | 'mp4';
  overallProgress: number;
  currentStage: ProcessingStage;
  stages: ProcessingStage[];
  status: 'queued' | 'uploading' | 'processing' | 'completed' | 'failed' | 'paused' | 'cancelled';
  // ... additional fields
}

// WebSocket message types
export interface WebSocketMessage {
  id: string;
  type: string;
  data: any;
  timestamp: string;
  priority: 'low' | 'normal' | 'high' | 'critical';
}
```

### React Components

#### RealtimeStatusDashboard
Main dashboard component that displays:
- Connection status indicators
- Active document processing queue
- System performance metrics
- Real-time notifications

```typescript
// Usage example
<RealtimeStatusDashboard
  showSystemMetrics={true}
  showConnectionStatus={true}
  refreshInterval={5000}
/>
```

#### DocumentProgressVisualizer
Interactive timeline component showing:
- Multi-stage processing progress
- Detailed step information
- Error states and retry options
- ETA calculations

```typescript
// Usage example
<DocumentProgressVisualizer
  document={processingDocument}
  showDetails={true}
  interactive={true}
  compact={false}
/>
```

### State Management with Zustand

```typescript
// Store usage
const {
  connection,
  documents,
  subscribeToDocument,
  connect
} = useRealtimeStore();

// Connect to WebSocket
await connect(token, {
  channels: [Channel.DOCUMENT_PROCESSING],
  frequency: UpdateFrequency.REALTIME
});
```

## Backend API

### Authentication

All real-time API endpoints require JWT authentication:

```bash
# Include JWT token in Authorization header
Authorization: Bearer <jwt_token>
```

### Core Endpoints

#### Document Status Subscription
```http
POST /api/v2/realtime/documents/status/subscribe
Content-Type: application/json

{
  "document_id": "uuid",
  "channels": ["document_processing"],
  "frequency": "realtime"
}
```

#### Get Enhanced Document Status
```http
GET /api/v2/realtime/documents/{document_id}/status?include_progress=true&include_stages=true
```

Response:
```json
{
  "id": "uuid",
  "filename": "document.pdf",
  "processing_status": "processing",
  "overall_progress": 45.5,
  "current_stage": {
    "id": "stage-uuid",
    "name": "OCR Processing",
    "status": "in_progress",
    "progress": 60.0
  },
  "stages": [...],
  "websocket_subscribers": 3,
  "is_realtime_enabled": true
}
```

#### Bulk Status Retrieval
```http
POST /api/v2/realtime/documents/bulk/status
Content-Type: application/json

{
  "document_ids": ["uuid1", "uuid2", "uuid3"],
  "include_jobs": true,
  "group_by_status": true
}
```

#### System Metrics
```http
GET /api/v2/realtime/system/metrics
```

Response:
```json
{
  "active_connections": 127,
  "total_documents": 15420,
  "queued_documents": 23,
  "processing_documents": 45,
  "average_processing_time_seconds": 127.5,
  "websocket_connections_by_channel": {
    "document_processing": 89,
    "system_status": 38
  }
}
```

## WebSocket Integration

### Connection Endpoint

```javascript
// WebSocket connection with authentication
const ws = new WebSocket(
  `ws://localhost:8000/api/v2/ws/connect?token=${jwt_token}&channels=document_processing&frequency=realtime`
);
```

### Message Types

#### Client to Server Messages

**Subscribe to Document Updates**
```json
{
  "type": "subscribe",
  "data": {
    "documentId": "uuid",
    "channels": ["document_processing"]
  }
}
```

**Heartbeat/Ping**
```json
{
  "type": "ping",
  "timestamp": "2025-11-21T10:30:00.000Z"
}
```

#### Server to Client Messages

**Document Processing Update**
```json
{
  "id": "msg-uuid",
  "type": "document_processing",
  "data": {
    "documentId": "uuid",
    "status": "processing",
    "progress": 65.5,
    "currentStage": "Embedding Generation",
    "event": {
      "event_type": "progress_update",
      "timestamp": "2025-11-21T10:30:00.000Z"
    }
  },
  "timestamp": "2025-11-21T10:30:00.000Z",
  "priority": "normal"
}
```

**Connection Status Update**
```json
{
  "id": "conn-uuid",
  "type": "connection_status",
  "data": {
    "status": "connected",
    "connection_id": "conn-uuid",
    "subscribed_channels": ["document_processing"]
  }
}
```

### Error Handling

The WebSocket service implements comprehensive error handling:

- **Connection Errors**: Automatic reconnection with exponential backoff
- **Authentication Errors**: Clear error messages and connection termination
- **Message Processing Errors**: Error logging and graceful degradation
- **Rate Limiting**: Message throttling to prevent overwhelming the system

## Database Schema

### Real-time Tracking Tables

#### WebSocket Connections
```sql
CREATE TABLE websocket_connections (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  connection_id VARCHAR(255) UNIQUE NOT NULL,
  user_id UUID NOT NULL,
  organization_id UUID NOT NULL,
  connected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  last_activity TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  last_heartbeat TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  client_ip INET,
  user_agent TEXT,
  subscription_channels TEXT[],
  is_active BOOLEAN DEFAULT TRUE,
  message_count_sent INTEGER DEFAULT 0,
  message_count_received INTEGER DEFAULT 0,
  bytes_sent BIGINT DEFAULT 0,
  bytes_received BIGINT DEFAULT 0
);
```

#### Status Updates
```sql
CREATE TABLE status_updates (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  update_id VARCHAR(255) UNIQUE NOT NULL,
  update_type VARCHAR(50) NOT NULL,
  title VARCHAR(255) NOT NULL,
  message TEXT,
  update_data JSONB,
  target_users UUID[],
  target_organizations UUID[],
  priority VARCHAR(20) DEFAULT 'normal',
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  expires_at TIMESTAMP WITH TIME ZONE
);
```

#### Connection Events
```sql
CREATE TABLE connection_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  connection_id VARCHAR(255) NOT NULL,
  event_type VARCHAR(50) NOT NULL,
  user_id UUID NOT NULL,
  organization_id UUID NOT NULL,
  event_data JSONB,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

### Materialized Views

#### Real-time Dashboard View
```sql
CREATE MATERIALIZED VIEW realtime_document_status AS
SELECT
  d.id,
  d.filename,
  d.title,
  d.processing_status,
  d.processing_started_at,
  d.processing_completed_at,
  COALESCE(d.processing_retry_count, 0) as retry_count,
  COUNT(wc.connection_id) as websocket_subscribers,
  d.updated_at as last_update
FROM documents d
LEFT JOIN websocket_connections wc ON d.uploaded_by_user_id = wc.user_id AND wc.is_active = true
WHERE d.is_deleted = false
GROUP BY d.id, d.filename, d.title, d.processing_status, d.processing_started_at, d.processing_completed_at, d.processing_retry_count, d.updated_at;
```

## Usage Examples

### Frontend Integration

#### React Component with Real-time Updates
```typescript
import React, { useEffect, useState } from 'react';
import { useRealtimeStore } from '@/store/realtime-store';
import { DocumentProgressVisualizer } from '@/components/realtime/DocumentProgressVisualizer';

const DocumentStatusComponent = ({ documentId }: { documentId: string }) => {
  const { documents, subscribeToDocument, connect } = useRealtimeStore();
  const [document, setDocument] = useState(null);

  useEffect(() => {
    // Connect to WebSocket
    const initConnection = async () => {
      await connect(process.env.REACT_APP_WS_TOKEN, {
        channels: ['document_processing'],
        frequency: 'realtime'
      });

      // Subscribe to document updates
      subscribeToDocument(documentId);
    };

    initConnection();
  }, [documentId]);

  useEffect(() => {
    // Get document from store
    const doc = documents.get(documentId);
    setDocument(doc || null);
  }, [documents, documentId]);

  if (!document) return <div>Loading...</div>;

  return (
    <DocumentProgressVisualizer
      document={document}
      showDetails={true}
      interactive={true}
    />
  );
};
```

#### WebSocket Service Usage
```typescript
import { realtimeWebSocketService } from '@/services/realtime-websocket-service';

// Connect and subscribe
const connectToRealtimeUpdates = async (token: string, documentId: string) => {
  try {
    await realtimeWebSocketService.initialize({
      url: 'ws://localhost:8000/api/v2/ws/connect',
      token,
      channels: ['document_processing'],
      frequency: 'realtime'
    });

    // Subscribe to document updates
    realtimeWebSocketService.subscribeToDocument(documentId);

    // Listen for messages
    realtimeWebSocketService.on('document_processing', (message) => {
      console.log('Document update:', message.data);
      // Update UI state
    });

  } catch (error) {
    console.error('Failed to connect:', error);
  }
};
```

### Backend Integration

#### Broadcasting Document Updates
```python
from src.services.document_realtime_service import document_realtime_service

# Update document progress
await document_realtime_service.update_document_progress(
    document_id="doc-uuid",
    current_step="Embedding Generation",
    progress_percentage=75.5,
    current_operation="Generating vector embeddings",
    step_details={
        "embeddings_count": 1250,
        "total_embeddings": 1650
    }
)

# Handle status change
await document_realtime_service.handle_document_status_change(
    document_id="doc-uuid",
    old_status=ProcessingStatus.PROCESSING,
    new_status=ProcessingStatus.COMPLETED
)
```

#### Custom Event Broadcasting
```python
from src.services.status_update_service import status_update_service

# Broadcast system notification
await status_update_service.broadcast_system_notification(
    title="Processing Complete",
    message="Document processing has completed successfully",
    notification_type="success",
    target_users=["user-uuid"]
)
```

## Performance Considerations

### Connection Scaling

- **Maximum Connections**: 10,000 concurrent WebSocket connections per instance
- **Redis Clustering**: Required for multi-instance deployments
- **Connection Pooling**: Implemented for database and Redis connections
- **Message Batching**: Automatic batching for high-frequency updates

### Memory Usage

- **Event History**: Limited to 1000 events per service
- **Connection Tracking**: Minimal memory footprint per connection
- **Progress Caching**: Automatic cleanup of stale progress data
- **Message Buffering**: Configurable buffer sizes for message batching

### Database Performance

- **Materialized Views**: Refreshed every 30 seconds for dashboard queries
- **Indexing**: Optimized indexes on document_id, user_id, and timestamp fields
- **Partitioning**: Tables partitioned by organization_id for large deployments
- **Connection Pooling**: Configurable connection pool sizes

### Network Optimization

- **Message Compression**: Optional compression for large message payloads
- **Throttling**: Intelligent message throttling based on update frequency
- **Priority Queues**: High-priority messages bypass throttling
- **Batch Processing**: Automatic batching for similar message types

## Troubleshooting

### Common Issues

#### WebSocket Connection Failures
**Problem**: Clients cannot connect to WebSocket endpoint
**Solution**:
1. Verify JWT token is valid and not expired
2. Check WebSocket endpoint URL: `ws://localhost:8000/api/v2/ws/connect`
3. Ensure WebSocket service is running: check `/api/v2/ws/status`
4. Verify authentication headers are properly formatted

#### No Real-time Updates
**Problem**: Connected clients are not receiving updates
**Solution**:
1. Check document subscription: `GET /api/v2/realtime/connections/status`
2. Verify document processing is active: check processing jobs
3. Check message filters: ensure channels are properly subscribed
4. Review browser console for WebSocket errors

#### High Memory Usage
**Problem**: Memory usage increases over time
**Solution**:
1. Check event history cleanup: configured for 1-hour retention
2. Verify connection cleanup: stale connections should auto-disconnect
3. Monitor Redis memory usage: check for connection leaks
4. Review database connection pooling settings

#### Performance Issues
**Problem**: Slow response times or high CPU usage
**Solution**:
1. Adjust message throttling settings
2. Optimize database queries with proper indexing
3. Check materialized view refresh intervals
4. Monitor batch sizes and timeout settings

### Debug Mode

Enable debug logging for detailed troubleshooting:

```python
# In backend configuration
LOG_LEVEL=DEBUG

# Enable WebSocket debugging
import logging
logging.getLogger('src.services.websocket_manager').setLevel(logging.DEBUG)
logging.getLogger('src.services.status_update_service').setLevel(logging.DEBUG)
```

### Monitoring Endpoints

#### Service Health Check
```bash
curl http://localhost:8000/api/v2/ws/health
```

#### Connection Statistics
```bash
curl http://localhost:8000/api/v2/ws/status
```

#### System Metrics
```bash
curl http://localhost:8000/api/v2/realtime/system/metrics
```

### Log Analysis

Check application logs for WebSocket-related events:

```bash
# WebSocket connection logs
docker-compose logs backend | grep "WebSocket"

# Real-time service logs
docker-compose logs backend | grep "realtime"

# Connection management logs
docker-compose logs backend | grep "connection_manager"
```

## Development and Testing

### Unit Testing

```bash
# Run WebSocket tests
pytest tests/specs/test_websocket_connections.py -v

# Test real-time processing
pytest tests/specs/test_realtime_processing.py -v

# Test status updates
pytest tests/specs/test_status_updates.py -v
```

### Integration Testing

```bash
# Test WebSocket integration
pytest tests/integration/test_websocket_integration.py -v

# Test end-to-end real-time flow
pytest tests/e2e/test_realtime_document_processing.py -v
```

### Performance Testing

Load testing for WebSocket connections:

```bash
# WebSocket load test
python tests/performance/websocket_load_test.py --connections=1000 --duration=300

# Message throughput test
python tests/performance/message_throughput_test.py --messages-per-second=1000
```

## Security Considerations

### Authentication
- JWT token validation for all WebSocket connections
- Token expiration handling with automatic reconnection
- User and organization authorization checks

### Data Protection
- Message content validation and sanitization
- Rate limiting per user and organization
- Connection monitoring and abuse detection

### Network Security
- TLS encryption for WebSocket connections in production
- Origin validation for WebSocket requests
- CORS configuration for cross-origin requests

## Deployment

### Environment Variables

```bash
# WebSocket Configuration
WS_MAX_CONNECTIONS=10000
WS_HEARTBEAT_INTERVAL=30
WS_CONNECTION_TIMEOUT=300

# Redis Configuration (for clustering)
REDIS_URL=redis://localhost:6379
REDIS_CLUSTER_ENABLED=false

# Real-time Processing
REALTIME_EVENT_HISTORY_LIMIT=1000
REALTIME_PROGRESS_UPDATE_INTERVAL=2.0
REALTIME_CLEANUP_INTERVAL=3600
```

### Docker Configuration

```yaml
# docker-compose.development.yml
services:
  backend:
    environment:
      - WS_MAX_CONNECTIONS=1000
      - REDIS_URL=redis://redis:6379
    depends_on:
      - redis
      - postgres
    ports:
      - "8000:8000"
```

### Production Deployment

1. **Load Balancing**: Use WebSocket-aware load balancer
2. **Redis Cluster**: Deploy Redis cluster for session sharing
3. **Monitoring**: Set up Prometheus and Grafana metrics
4. **Scaling**: Horizontal scaling with connection affinity

## API Reference

### WebSocket Channels

- **document_processing**: Document processing status updates
- **job_status**: Background job status notifications
- **system_status**: System-wide status and metrics
- **user_notifications**: User-specific notifications
- **admin_alerts**: Administrative alerts (admin users only)

### Message Priorities

- **critical**: System failures, security alerts
- **high**: Job failures, processing errors
- **normal**: Status updates, progress notifications
- **low**: General informational messages

### Update Frequencies

- **realtime**: 100-500ms intervals
- **frequent**: 1-2 seconds intervals
- **normal**: 5-10 seconds intervals
- **periodic**: 30-60 seconds intervals

---

This guide provides comprehensive documentation for implementing and using the real-time document processing features. For additional information, refer to the API documentation and source code comments.
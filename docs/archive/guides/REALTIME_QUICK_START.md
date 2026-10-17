# Real-time Document Processing Quick Start

This guide will help you get started with the real-time document processing features in minutes.

## Prerequisites

- Backend and frontend services running
- Valid JWT authentication token
- Modern web browser with WebSocket support

## Step 1: Connect to WebSocket

### JavaScript/TypeScript Client

```typescript
import { realtimeWebSocketService } from '@/services/realtime-websocket-service';

// Connect to WebSocket
await realtimeWebSocketService.initialize({
  url: 'ws://localhost:8000/api/v2/ws/connect',
  token: 'your-jwt-token-here',
  channels: ['document_processing'],
  frequency: 'realtime'
});
```

### Raw WebSocket Connection

```javascript
const ws = new WebSocket(
  `ws://localhost:8000/api/v2/ws/connect?token=your-jwt-token&channels=document_processing`
);

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  console.log('Received:', message);
};
```

## Step 2: Subscribe to Document Updates

### Subscribe to a Specific Document

```bash
curl -X POST http://localhost:8000/api/v2/realtime/documents/status/subscribe \
  -H "Authorization: Bearer your-jwt-token" \
  -H "Content-Type: application/json" \
  -d '{
    "document_id": "your-document-uuid",
    "channels": ["document_processing"],
    "frequency": "realtime"
  }'
```

### React Component Integration

```typescript
import { useRealtimeStore } from '@/store/realtime-store';

function DocumentStatus({ documentId }: { documentId: string }) {
  const { subscribeToDocument, documents } = useRealtimeStore();

  useEffect(() => {
    subscribeToDocument(documentId);
  }, [documentId]);

  const document = documents.get(documentId);

  return (
    <div>
      <h3>{document?.filename}</h3>
      <p>Status: {document?.status}</p>
      <p>Progress: {document?.overallProgress}%</p>
    </div>
  );
}
```

## Step 3: Use the React Components

### Real-time Dashboard

```typescript
import { RealtimeStatusDashboard } from '@/components/realtime/RealtimeStatusDashboard';

function App() {
  return (
    <RealtimeStatusDashboard
      showSystemMetrics={true}
      showConnectionStatus={true}
    />
  );
}
```

### Document Progress Visualizer

```typescript
import { DocumentProgressVisualizer } from '@/components/realtime/DocumentProgressVisualizer';

function DocumentProcessing({ document }: { document: any }) {
  return (
    <DocumentProgressVisualizer
      document={document}
      showDetails={true}
      interactive={true}
      compact={false}
    />
  );
}
```

## Step 4: Monitor Real-time Status

### Check WebSocket Service Status

```bash
curl http://localhost:8000/api/v2/ws/status
```

### Get System Metrics

```bash
curl http://localhost:8000/api/v2/realtime/system/metrics
```

### Check Your Connection Status

```bash
curl -H "Authorization: Bearer your-jwt-token" \
  http://localhost:8000/api/v2/realtime/connections/status
```

## Common Usage Patterns

### 1. Upload and Track Document

```typescript
async function uploadAndTrack(file: File) {
  // Upload document
  const uploadResponse = await fetch('/api/v1/files/upload', {
    method: 'POST',
    body: formData,
    headers: { 'Authorization': `Bearer ${token}` }
  });

  const { document_id } = await uploadResponse.json();

  // Subscribe to real-time updates
  subscribeToDocument(document_id);

  // Start processing
  await fetch(`/api/v1/processing/documents/${document_id}/process`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${token}` }
  });
}
```

### 2. Listen for Progress Updates

```typescript
import { realtimeWebSocketService } from '@/services/realtime-websocket-service';

// Listen for document updates
realtimeWebSocketService.on('document_processing', (message) => {
  const { documentId, status, progress } = message.data;

  switch (status) {
    case 'processing':
      console.log(`Document ${documentId} is ${progress}% complete`);
      break;
    case 'completed':
      console.log(`Document ${documentId} processing complete!`);
      break;
    case 'failed':
      console.error(`Document ${documentId} processing failed`);
      break;
  }
});
```

### 3. Handle Connection Events

```typescript
realtimeWebSocketService.on('connect', (connectionInfo) => {
  console.log('Connected:', connectionInfo.connection_id);
});

realtimeWebSocketService.on('disconnect', (reason) => {
  console.log('Disconnected:', reason);
});

realtimeWebSocketService.on('error', (error) => {
  console.error('WebSocket error:', error);
});
```

## Testing Your Integration

### 1. Test WebSocket Connection

Open browser developer tools and go to Network tab:

```javascript
// In browser console
const ws = new WebSocket('ws://localhost:8000/api/v2/ws/connect?token=your-jwt-token');
ws.onopen = () => console.log('Connected!');
ws.onmessage = (e) => console.log('Message:', JSON.parse(e.data));
```

### 2. Test API Endpoints

```bash
# Test document status
curl -H "Authorization: Bearer your-jwt-token" \
  http://localhost:8000/api/v2/realtime/documents/your-doc-uuid/status

# Test bulk status
curl -X POST http://localhost:8000/api/v2/realtime/documents/bulk/status \
  -H "Authorization: Bearer your-jwt-token" \
  -H "Content-Type: application/json" \
  -d '{"document_ids": ["uuid1", "uuid2"], "include_jobs": true}'
```

### 3. React Component Testing

```typescript
import { render, screen } from '@testing-library/react';
import { DocumentProgressVisualizer } from '@/components/realtime/DocumentProgressVisualizer';

test('shows document progress', () => {
  const mockDocument = {
    id: 'test-uuid',
    filename: 'test.pdf',
    status: 'processing',
    overallProgress: 50,
    stages: []
  };

  render(<DocumentProgressVisualizer document={mockDocument} />);

  expect(screen.getByText('test.pdf')).toBeInTheDocument();
  expect(screen.getByText('50%')).toBeInTheDocument();
});
```

## Troubleshooting

### Connection Issues

**Problem**: "WebSocket connection failed"
**Solution**:
- Check your JWT token is valid
- Verify backend is running on port 8000
- Ensure WebSocket endpoint is accessible

### No Updates Received

**Problem**: Connected but no status updates
**Solution**:
- Verify you subscribed to the correct document
- Check document is actually being processed
- Review browser console for errors

### Frontend Integration Issues

**Problem**: Components not updating
**Solution**:
- Verify Zustand store integration
- Check WebSocket message handling
- Ensure proper re-rendering with state changes

## Next Steps

1. **Implement Custom Components**: Build custom UI components using the real-time data
2. **Add Error Handling**: Implement robust error handling and user feedback
3. **Performance Optimization**: Tune update frequencies and batch sizes
4. **Testing**: Write comprehensive tests for your real-time features

## Additional Resources

- [Complete Real-time Processing Guide](REALTIME_PROCESSING_GUIDE.md)
- [WebSocket API Documentation](../../guides/websocket_services.md)
- [React Component Documentation](../frontend/react-components.md)
- [Backend API Reference](../api/realtime-api.md)

## Support

If you encounter issues:

1. Check the browser console for JavaScript errors
2. Review the backend logs: `docker-compose logs backend`
3. Verify WebSocket endpoint: `curl http://localhost:8000/api/v2/ws/status`
4. Check system metrics: `curl http://localhost:8000/api/v2/realtime/system/metrics`

Happy coding with real-time document processing! 🚀
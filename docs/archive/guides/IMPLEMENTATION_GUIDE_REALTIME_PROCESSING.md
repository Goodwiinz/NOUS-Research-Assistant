# Real-Time Document Processing Dashboard - Implementation Guide

## Overview

This guide provides step-by-step instructions for implementing the real-time document processing status display architecture. The system is built with React, TypeScript, Zustand for state management, and WebSocket integration for real-time updates.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Core Components](#core-components)
4. [State Management](#state-management)
5. [WebSocket Integration](#websocket-integration)
6. [Accessibility Implementation](#accessibility-implementation)
7. [Performance Optimization](#performance-optimization)
8. [Testing Strategy](#testing-strategy)
9. [Deployment](#deployment)
10. [Troubleshooting](#troubleshooting)

## Prerequisites

- Node.js 18+ and npm/yarn
- React 18+ with TypeScript
- Existing design system (Radix UI components)
- FastAPI backend with WebSocket support
- Zustand for state management

## Installation

### 1. Install Dependencies

```bash
# Core dependencies
npm install zustand @radix-ui/react-*
npm install @heroicons/react lucide-react
npm install react-hot-toast
npm install clsx tailwind-merge

# Development dependencies
npm install -D @storybook/react @storybook/test
npm install -D @testing-library/react @testing-library/jest-dom
npm install -D @testing-library/user-event axe-core jest-axe
npm install -D @types/react @types/react-dom
```

### 2. File Structure Setup

Create the following directory structure in your frontend application:

```
frontend/src/
├── components/
│   ├── realtime/
│   │   ├── RealtimeProcessingProvider.tsx
│   │   ├── RealtimeProcessingDashboard.tsx
│   │   └── RealtimeProcessingDashboard.stories.tsx
│   └── ui/
├── services/
│   ├── websocket.ts
│   └── enhancedWebSocket.ts
├── store/
│   └── realtimeProcessingStore.ts
├── types/
│   └── realtime-processing.ts
└── hooks/
    └── useRealtimeProcessing.ts
```

## Core Components

### 1. RealtimeProcessingProvider

The provider component manages WebSocket connections and provides context to child components.

**Usage:**
```tsx
import { RealtimeProcessingProvider } from '@/components/realtime/RealtimeProcessingProvider';

function App() {
  return (
    <RealtimeProcessingProvider>
      <RealtimeProcessingDashboard />
    </RealtimeProcessingProvider>
  );
}
```

**Key Features:**
- Automatic connection management
- Authentication handling
- Error boundary integration
- Message routing to store

### 2. RealtimeProcessingDashboard

Main dashboard component for displaying processing status.

**Usage:**
```tsx
import { RealtimeProcessingDashboard } from '@/components/realtime/RealtimeProcessingDashboard';

<RealtimeProcessingDashboard
  autoRefresh={true}
  refreshInterval={5}
  showControls={true}
  maxHeight="600px"
  enableSounds={true}
  theme="auto"
  compactView={false}
/>
```

**Props:**
- `autoRefresh`: Enable automatic refresh (default: true)
- `refreshInterval`: Update interval in seconds (default: 5)
- `showControls`: Show bulk action controls (default: true)
- `maxHeight`: Maximum height of document list (default: "600px")
- `enableSounds`: Enable sound notifications (default: true)
- `theme`: Theme preference (default: "auto")
- `compactView`: Use compact layout (default: false)

### 3. DocumentCard Component

Individual document display with progress tracking.

**Features:**
- Real-time progress updates
- Stage-wise processing visualization
- Action buttons (pause, resume, cancel, retry)
- Error display and retry mechanism
- Accessibility support

## State Management

### Zustand Store Structure

The store uses Zustand with immer middleware for immutable updates:

```typescript
interface RealtimeProcessingState {
  connection: WebSocketConnectionState;
  queue: ProcessingQueue;
  systemMetrics: SystemMetrics;
  notifications: NotificationItem[];
  ui: UIState;
  preferences: UserPreferences;
}
```

### Store Selectors

Use optimized selectors to prevent unnecessary re-renders:

```typescript
// Good: Selects only needed state slice
const documents = useRealtimeProcessingStore(state => state.queue.documents);

// Better: Memoized selector for derived state
const processingDocuments = useRealtimeProcessingStore(
  useCallback(state => state.queue.documents.filter(doc => doc.status === 'processing'), [])
);
```

### State Updates

All state updates are handled through actions:

```typescript
const { updateDocument, addNotification } = useRealtimeProcessingStore();

// Update document progress
updateDocument(documentId, {
  overallProgress: 75,
  currentStage: newStage,
  status: 'processing'
});

// Add notification
addNotification({
  type: 'success',
  title: 'Processing Complete',
  message: 'Document processed successfully',
  documentId,
  autoHide: true
});
```

## WebSocket Integration

### Basic WebSocket Setup

```typescript
import { initializeWebSocket } from '@/services/websocket';

// Initialize WebSocket connection
const wsManager = initializeWebSocket(
  'ws://localhost:8000/ws',
  token,
  organizationId
);

// Connect
await wsManager.connect();

// Listen for updates
wsManager.on('document_update', handleDocumentUpdate);
```

### Enhanced WebSocket Service

For production use, use the enhanced WebSocket service:

```typescript
import { initializeEnhancedWebSocket } from '@/services/enhancedWebSocket';

const enhancedWs = initializeEnhancedWebSocket({
  url: 'ws://localhost:8000/ws',
  token,
  organizationId,
  enableLatencyMonitoring: true,
  enableMessageBuffering: true,
  heartbeatInterval: 30000
});
```

### Message Format

**Document Update:**
```typescript
{
  type: 'document_update',
  payload: {
    documentId: 'doc-123',
    progress: 75,
    currentStage: {
      id: 'ocr-processing',
      name: 'OCR Processing',
      progress: 80,
      status: 'in_progress'
    },
    status: 'processing'
  },
  timestamp: '2024-01-15T10:30:00Z',
  documentId: 'doc-123'
}
```

**Queue Update:**
```typescript
{
  type: 'queue_update',
  payload: {
    summary: {
      total: 25,
      queued: 3,
      processing: 5,
      completed: 17,
      failed: 0
    },
    metrics: {
      averageProcessingTime: 45.2,
      throughput: 12.5,
      successRate: 98.4
    }
  },
  timestamp: '2024-01-15T10:30:00Z'
}
```

## Accessibility Implementation

### WCAG 2.1 AA Compliance

**Keyboard Navigation:**
- Tab order follows logical flow
- Focus indicators are visible
- Skip links provided for navigation

```tsx
const DocumentCard = ({ document, onSelect }) => (
  <div
    role="article"
    aria-label={`Document ${document.filename}, status: ${document.status}`}
    tabIndex={0}
    onKeyDown={handleKeyDown}
    onClick={onSelect}
  >
    {/* Content */}
  </div>
);
```

**Screen Reader Support:**
- ARIA labels for all interactive elements
- Live regions for status updates
- Semantic HTML structure

```tsx
<div
  role="progressbar"
  aria-valuenow={progress}
  aria-valuemin={0}
  aria-valuemax={100}
  aria-label={`Processing progress: ${progress}%`}
>
  {progress}%
</div>

<div aria-live="polite" aria-atomic="true" className="sr-only">
  Status: {status}, Current stage: {currentStage.name}
</div>
```

**Color Contrast:**
- Text contrast ratio ≥ 4.5:1
- Large text contrast ratio ≥ 3:1
- Interactive elements have visible focus states

## Performance Optimization

### React Optimization

**Memoization:**
```tsx
const DocumentCard = React.memo(({ document }) => {
  // Component implementation
}, (prevProps, nextProps) => {
  // Custom comparison logic
  return prevProps.document.id === nextProps.document.id &&
         prevProps.document.overallProgress === nextProps.document.overallProgress;
});
```

**Virtual Scrolling:**
```tsx
import { FixedSizeList as List } from 'react-window';

const DocumentList = ({ documents }) => {
  const Row = ({ index, style }) => (
    <div style={style}>
      <DocumentCard document={documents[index]} />
    </div>
  );

  return (
    <List
      height={600}
      itemCount={documents.length}
      itemSize={80}
    >
      {Row}
    </List>
  );
};
```

**Debounced Updates:**
```typescript
import { debounce } from 'lodash-es';

const debouncedUpdate = debounce((documentId, updates) => {
  updateDocument(documentId, updates);
}, 300);
```

### WebSocket Optimization

**Message Throttling:**
```typescript
class ThrottledWebSocket {
  private updateQueue = new Map<string, any>();
  private throttleTimer: NodeJS.Timeout;

  throttledUpdate(documentId: string, updates: any) {
    this.updateQueue.set(documentId, updates);

    if (!this.throttleTimer) {
      this.throttleTimer = setTimeout(() => {
        this.flushUpdates();
      }, 100); // Throttle to 10 updates per second
    }
  }

  private flushUpdates() {
    const updates = Array.from(this.updateQueue.entries());
    this.updateQueue.clear();
    this.throttleTimer = null;

    // Send batched updates
    updates.forEach(([id, update]) => {
      this.send('document_update', { documentId: id, ...update });
    });
  }
}
```

## Testing Strategy

### Unit Testing

**Component Tests:**
```typescript
import { render, screen, fireEvent } from '@testing-library/react';
import { RealtimeProcessingDashboard } from './RealtimeProcessingDashboard';

describe('RealtimeProcessingDashboard', () => {
  it('displays processing statistics', () => {
    render(<RealtimeProcessingDashboard />);

    expect(screen.getByText('Total Files')).toBeInTheDocument();
    expect(screen.getByText('Processing')).toBeInTheDocument();
    expect(screen.getByText('Completed')).toBeInTheDocument();
  });

  it('handles document selection', () => {
    render(<RealtimeProcessingDashboard />);

    const checkbox = screen.getByRole('checkbox');
    fireEvent.click(checkbox);

    expect(checkbox).toBeChecked();
  });
});
```

**Store Tests:**
```typescript
import { useRealtimeProcessingStore } from '../store/realtimeProcessingStore';

describe('RealtimeProcessingStore', () => {
  it('adds document to queue', () => {
    const { addDocument, getDocumentsByStatus } = useRealtimeProcessingStore.getState();

    const document = createMockDocument();
    addDocument(document);

    const processingDocs = getDocumentsByStatus('processing');
    expect(processingDocs).toContain(document);
  });
});
```

### Integration Testing

**WebSocket Integration:**
```typescript
import { renderHook, act } from '@testing-library/react';
import { useRealtimeProcessing } from '../hooks/useRealtimeProcessing';

describe('useRealtimeProcessing', () => {
  it('connects to WebSocket on mount', async () => {
    const { result } = renderHook(() => useRealtimeProcessing());

    expect(result.current.isConnected).toBe(false);

    await act(async () => {
      await result.current.connect();
    });

    expect(result.current.isConnected).toBe(true);
  });
});
```

### Accessibility Testing

**Axe Testing:**
```typescript
import { axe, toHaveNoViolations } from 'jest-axe';
import { render } from '@testing-library/react';
import { RealtimeProcessingDashboard } from './RealtimeProcessingDashboard';

expect.extend(toHaveNoViolations);

describe('Accessibility', () => {
  it('has no accessibility violations', async () => {
    const { container } = render(<RealtimeProcessingDashboard />);
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});
```

### E2E Testing with Playwright

```typescript
import { test, expect } from '@playwright/test';

test('real-time dashboard functionality', async ({ page }) => {
  await page.goto('/processing-dashboard');

  // Check connection status
  await expect(page.locator('[data-testid="connection-status"]')).toContainText('Connected');

  // Upload test document
  await page.setInputFiles('input[type="file"]', 'test-document.pdf');
  await page.click('[data-testid="upload-button"]');

  // Verify document appears in queue
  await expect(page.locator('[data-testid="document-card"]')).toContainText('test-document.pdf');

  // Wait for processing to complete
  await expect(page.locator('[data-testid="processing-status"]')).toContainText('Completed', { timeout: 30000 });
});
```

## Storybook Documentation

### Running Storybook

```bash
# Start Storybook development server
npm run storybook

# Build static Storybook
npm run build-storybook
```

### Component Stories

Each component includes comprehensive Storybook stories:

- **Default**: Normal operation with sample data
- **Empty**: Empty state handling
- **Loading**: Loading states and skeletons
- **Error**: Error states and recovery
- **Accessibility**: Accessibility testing with axe
- **Performance**: Performance monitoring
- **Responsive**: Mobile and tablet layouts

### Interactive Examples

Stories include interactive features for:
- Real-time progress simulation
- WebSocket connection testing
- State manipulation
- User interaction testing

## Deployment

### Production Build

```bash
# Build optimized production bundle
npm run build

# Run tests before deployment
npm run test:production

# Generate Storybook documentation
npm run build-storybook
```

### Environment Configuration

```typescript
// config/production.ts
export const config = {
  websocket: {
    url: process.env.WS_URL || 'wss://api.example.com/ws',
    reconnectInterval: 5000,
    maxReconnectionAttempts: 10,
    heartbeatInterval: 30000
  },
  performance: {
    messageQueueSize: 500,
    bufferSize: 50,
    enableLatencyMonitoring: true
  }
};
```

### Performance Monitoring

```typescript
// monitoring/performance.ts
export class PerformanceMonitor {
  static trackRenderTime(componentName: string) {
    return (WrappedComponent: React.ComponentType) => {
      return React.memo((props) => {
        const startTime = performance.now();

        return (
          <>
            <WrappedComponent {...props} />
            {process.env.NODE_ENV === 'development' && (
              <div className="fixed top-4 right-4 bg-black text-white p-2 rounded text-xs">
                {componentName}: {(performance.now() - startTime).toFixed(2)}ms
              </div>
            )}
          </>
        );
      });
    };
  }
}
```

## Troubleshooting

### Common Issues

**WebSocket Connection Failed:**
- Check if backend WebSocket server is running
- Verify authentication token is valid
- Ensure firewall allows WebSocket connections
- Check browser console for specific error messages

**Performance Issues:**
- Enable virtual scrolling for large document lists
- Implement message throttling
- Use React.memo to prevent unnecessary re-renders
- Monitor memory usage with React DevTools

**Accessibility Issues:**
- Run axe accessibility tests
- Verify keyboard navigation works
- Check color contrast ratios
- Test with screen readers

**State Management Issues:**
- Ensure proper Zustand store usage
- Avoid direct state mutations
- Use proper selector patterns
- Implement error boundaries

### Debug Mode

Enable debug logging for development:

```typescript
// debug/logging.ts
if (process.env.NODE_ENV === 'development') {
  const originalLog = console.log;
  console.log = (...args) => {
    if (args[0] === '[WebSocketManager]') {
      originalLog(...args);
    }
  };
}
```

### Performance Profiling

Use React DevTools Profiler to identify performance bottlenecks:

```typescript
// profiling/Profiler.tsx
import { Profiler } from 'react';

const ProfiledDashboard = ({ children }) => (
  <Profiler id="RealtimeDashboard" onRender={onRenderCallback}>
    {children}
  </Profiler>
);

function onRenderCallback(id, phase, actualDuration) {
  console.log(`${id} ${phase} took ${actualDuration}ms`);
}
```

## Best Practices

1. **Error Boundaries**: Wrap components in error boundaries
2. **Loading States**: Show skeleton loaders during data fetch
3. **Progressive Enhancement**: Ensure functionality without WebSocket
4. **Graceful Degradation**: Handle connection failures gracefully
5. **Security**: Sanitize all WebSocket messages
6. **Performance**: Optimize re-renders and memory usage
7. **Accessibility**: Test with keyboard and screen readers
8. **Testing**: Comprehensive test coverage
9. **Documentation**: Keep Storybook stories updated
10. **Monitoring**: Track performance metrics in production

## Support

For issues and questions:
- Check the troubleshooting section above
- Review component Storybook documentation
- Examine browser console for errors
- Verify backend WebSocket endpoint is accessible
- Test with different network conditions

This implementation provides a robust, accessible, and performant real-time document processing dashboard that integrates seamlessly with your existing React/TypeScript application architecture.
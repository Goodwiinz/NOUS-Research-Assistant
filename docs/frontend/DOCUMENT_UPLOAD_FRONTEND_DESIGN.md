> **Dated design (2026-10-17 review): an upfront component design, not a description of the shipped upload UI. The implementation lives in `frontend/src/components/upload/` and `frontend/src/services/uploadService.ts`; check the code before relying on this.**

# Document Upload Frontend Component Design

## Overview

React component architecture for document upload with automatic knowledge graph population, featuring drag-and-drop interface, real-time processing status, and seamless integration with the existing RAG system.

## Component Hierarchy

```
App
└── AppRouter
    └── AppLayout
        └── DashboardPage
            ├── DocumentUploadZone
            ├── DocumentLibrary
            ├── UploadProgressTracker
            └── ProcessingStatusModal
```

## Component Specifications

### 1. DocumentUploadZone Component

**File**: `frontend/src/components/documents/DocumentUploadZone.tsx`

**Purpose**: Main drag-and-drop interface for document uploads

**Props**:
```typescript
interface DocumentUploadZoneProps {
  onUploadStart: (files: FileList) => void;
  onUploadProgress: (progress: UploadProgress[]) => void;
  onUploadComplete: (documents: DocumentUploadResponse[]) => void;
  onUploadError: (error: UploadError) => void;
  maxFiles?: number;
  maxFileSizeMB?: number;
  acceptedFileTypes?: string[];
  disabled?: boolean;
}
```

**Features**:
- Drag and drop file upload zone
- Multiple file selection
- File type validation
- File size validation
- Upload progress visualization
- Error handling and retry
- Batch upload support

**State Management**:
```typescript
interface UploadState {
  isDragging: boolean;
  isUploading: boolean;
  uploadProgress: Record<string, number>;
  errors: UploadError[];
  selectedFiles: File[];
}
```

**UI Elements**:
- Drag zone with visual feedback
- File list with previews
- Progress bars for each file
- Error messages with retry options
- Upload/pause/cancel controls

### 2. DocumentLibrary Component

**File**: `frontend/src/components/documents/DocumentLibrary.tsx`

**Purpose**: Display and manage uploaded documents

**Props**:
```typescript
interface DocumentLibraryProps {
  documents: Document[];
  loading: boolean;
  onDocumentSelect: (document: Document) => void;
  onDocumentDelete: (documentId: string) => void;
  onDocumentDownload: (documentId: string) => void;
  onRetryProcessing: (documentId: string) => void;
  filters: DocumentFilters;
  onFiltersChange: (filters: DocumentFilters) => void;
}
```

**Features**:
- Grid and list view modes
- Search and filtering
- Sorting options
- Pagination
- Bulk operations
- Document preview
- Processing status indicators

**UI Elements**:
- View toggle (grid/list)
- Search bar
- Filter dropdowns
- Sort controls
- Document cards/rows
- Pagination controls
- Bulk action toolbar

### 3. UploadProgressTracker Component

**File**: `frontend/src/components/documents/UploadProgressTracker.tsx`

**Purpose**: Real-time tracking of upload and processing progress

**Props**:
```typescript
interface UploadProgressTrackerProps {
  uploadJobs: UploadJob[];
  processingJobs: ProcessingJob[];
  showDetails?: boolean;
  autoHide?: boolean;
}
```

**Features**:
- Real-time progress updates via WebSocket
- Processing stage visualization
- Error handling and retry
- Estimated time remaining
- Cancel/pause operations

**UI Elements**:
- Progress bars with stages
- Status icons
- Time estimates
- Action buttons
- Error display

### 4. ProcessingStatusModal Component

**File**: `frontend/src/components/documents/ProcessingStatusModal.tsx`

**Purpose**: Detailed processing status modal

**Props**:
```typescript
interface ProcessingStatusModalProps {
  document: Document;
  isOpen: boolean;
  onClose: () => void;
  onRetry: (jobTypes: string[]) => void;
  onCancel: () => void;
}
```

**Features**:
- Detailed job status
- Log viewer
- Entity extraction preview
- Knowledge graph preview
- Error details

### 5. DocumentPreview Component

**File**: `frontend/src/components/documents/DocumentPreview.tsx`

**Purpose**: Quick preview of document content

**Props**:
```typescript
interface DocumentPreviewProps {
  document: Document;
  previewData?: DocumentPreviewData;
  onExtractedEntityClick: (entity: ExtractedEntity) => void;
}
```

**Features**:
- PDF page preview
- Text snippet display
- Image thumbnails
- Audio/video player
- Entity highlighting

## State Management

### Zustand Store Structure

**File**: `frontend/src/stores/documentStore.ts`

```typescript
interface DocumentStore {
  // State
  documents: Document[];
  selectedDocuments: string[];
  uploadState: UploadState;
  processingJobs: Record<string, ProcessingJob>;
  filters: DocumentFilters;
  pagination: PaginationState;
  loading: boolean;
  error: string | null;

  // Upload Actions
  uploadFiles: (files: FileList) => Promise<void>;
  cancelUpload: (fileId: string) => void;
  retryUpload: (fileId: string) => void;
  updateUploadProgress: (fileId: string, progress: number) => void;

  // Document Actions
  fetchDocuments: (filters?: DocumentFilters) => Promise<void>;
  deleteDocument: (documentId: string) => Promise<void>;
  downloadDocument: (documentId: string) => Promise<void>;
  selectDocument: (documentId: string) => void;
  selectMultipleDocuments: (documentIds: string[]) => void;

  // Processing Actions
  retryProcessing: (documentId: string, jobTypes?: string[]) => Promise<void>;
  cancelProcessing: (documentId: string) => Promise<void>;
  updateProcessingStatus: (documentId: string, status: ProcessingStatus) => void;

  // Filter Actions
  setFilters: (filters: DocumentFilters) => void;
  clearFilters: () => void;
  setPagination: (page: number, size: number) => void;
}
```

### WebSocket Integration

**File**: `frontend/src/services/websocketService.ts`

```typescript
class WebSocketService {
  private ws: WebSocket | null = null;
  private subscriptions: Map<string, Set<Function>> = new Map();

  connect(): void;
  disconnect(): void;
  subscribe(topic: string, callback: Function): () => void;
  unsubscribe(topic: string, callback: Function): void;
  publish(topic: string, data: any): void;
}

// Usage for processing updates
wsService.subscribe(`documents/${documentId}/processing`, (update) => {
  documentStore.updateProcessingStatus(documentId, update);
});
```

## Data Models

### Core Types

```typescript
interface Document {
  id: string;
  title: string;
  filename: string;
  originalFilename: string;
  fileType: string;
  fileSizeBytes: number;
  status: DocumentStatus;
  uploadedAt: Date;
  uploadedBy: string;
  tags: string[];
  metadata: Record<string, any>;
  processingSummary?: ProcessingSummary;
  downloadUrl: string;
  previewUrl: string;
}

interface ProcessingJob {
  id: string;
  documentId: string;
  jobType: ProcessingJobType;
  status: ProcessingJobStatus;
  progressPercentage: number;
  startedAt?: Date;
  completedAt?: Date;
  estimatedRemainingSeconds?: number;
  errorMessage?: string;
  resultData?: Record<string, any>;
}

interface ExtractedEntity {
  id: string;
  entityText: string;
  entityType: string;
  confidenceScore: number;
  startPosition?: number;
  endPosition?: number;
  contextText?: string;
  neo4jNodeId?: string;
}

interface UploadProgress {
  fileId: string;
  fileName: string;
  progressPercentage: number;
  status: 'uploading' | 'processing' | 'completed' | 'error';
  errorMessage?: string;
}
```

### Enums

```typescript
enum DocumentStatus {
  UPLOADED = 'uploaded',
  PROCESSING = 'processing',
  PROCESSED = 'processed',
  FAILED = 'failed',
  ARCHIVED = 'archived'
}

enum ProcessingJobType {
  TEXT_EXTRACTION = 'text_extraction',
  ENTITY_EXTRACTION = 'entity_extraction',
  VECTOR_INDEXING = 'vector_indexing',
  KNOWLEDGE_GRAPH_POPULATION = 'knowledge_graph_population',
  MULTIMODAL_PROCESSING = 'multimodal_processing'
}

enum ProcessingJobStatus {
  PENDING = 'pending',
  RUNNING = 'running',
  COMPLETED = 'completed',
  FAILED = 'failed',
  CANCELLED = 'cancelled'
}
```

## API Client

**File**: `frontend/src/services/documentService.ts`

```typescript
class DocumentService {
  // Upload Operations
  async uploadDocuments(files: FileList, options?: UploadOptions): Promise<DocumentUploadResponse>;
  async getUploadStatus(batchId: string): Promise<UploadStatusResponse>;

  // Document Management
  async getDocuments(filters: DocumentFilters, pagination: PaginationParams): Promise<DocumentListResponse>;
  async getDocument(documentId: string): Promise<DocumentDetailResponse>;
  async deleteDocument(documentId: string): Promise<void>;
  async downloadDocument(documentId: string, version?: number): Promise<Blob>;

  // Processing Operations
  async getProcessingStatus(documentId: string): Promise<ProcessingStatusResponse>;
  async retryProcessing(documentId: string, options?: RetryOptions): Promise<void>;
  async cancelProcessing(documentId: string): Promise<void>;

  // Entity Operations
  async getDocumentEntities(documentId: string, options?: EntityQueryOptions): Promise<EntityListResponse>;
  async getDocumentRelationships(documentId: string): Promise<RelationshipListResponse>;

  // Batch Operations
  async createBatchProcessing(documentIds: string[], options: BatchProcessingOptions): Promise<BatchResponse>;
  async getBatchStatus(batchId: string): Promise<BatchStatusResponse>;
}
```

## Styling & Theming

### CSS Classes (Tailwind)

```css
/* Upload Zone */
.upload-zone {
  @apply border-2 border-dashed border-gray-300 rounded-lg p-8 text-center transition-colors;
}

.upload-zone.dragging {
  @apply border-blue-500 bg-blue-50;
}

.upload-zone.disabled {
  @apply border-gray-200 bg-gray-50 opacity-50;
}

/* Document Cards */
.document-card {
  @apply bg-white rounded-lg shadow-sm border border-gray-200 p-4 hover:shadow-md transition-shadow;
}

.document-card.processing {
  @apply border-blue-200 bg-blue-50;
}

.document-card.failed {
  @apply border-red-200 bg-red-50;
}

/* Progress Indicators */
.progress-bar {
  @apply w-full bg-gray-200 rounded-full h-2;
}

.progress-bar-fill {
  @apply bg-blue-600 h-2 rounded-full transition-all duration-300;
}

/* Status Badges */
.status-badge {
  @apply inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium;
}

.status-badge.uploaded {
  @apply bg-gray-100 text-gray-800;
}

.status-badge.processing {
  @apply bg-blue-100 text-blue-800;
}

.status-badge.processed {
  @apply bg-green-100 text-green-800;
}

.status-badge.failed {
  @apply bg-red-100 text-red-800;
}
```

## Accessibility Features

### WCAG 2.1 AA Compliance

1. **Keyboard Navigation**:
   - All interactive elements reachable via Tab
   - Clear focus indicators
   - Skip links for navigation

2. **Screen Reader Support**:
   - ARIA labels and descriptions
   - Live regions for status updates
   - Semantic HTML structure

3. **Visual Accessibility**:
   - High contrast colors
   - Resizeable text
   - Colorblind-friendly design

### ARIA Implementation

```typescript
// Upload Zone
<div
  role="button"
  aria-label="Upload documents"
  aria-describedby="upload-instructions"
  aria-busy={isUploading}
  tabIndex={0}
>

// Progress Bar
<div
  role="progressbar"
  aria-valuenow={progress}
  aria-valuemin={0}
  aria-valuemax={100}
  aria-label={`Upload progress: ${progress}%`}
>

// Status Updates
<div
  aria-live="polite"
  aria-atomic="true"
>
  {statusMessage}
</div>
```

## Performance Optimizations

### React Optimizations

1. **Component Memoization**:
   ```typescript
   const DocumentCard = React.memo(({ document }: DocumentCardProps) => {
     // Component implementation
   });
   ```

2. **Virtual Scrolling**:
   - For large document lists
   - Using react-window or react-virtualized

3. **Image Lazy Loading**:
   ```typescript
   <img
     src={previewUrl}
     loading="lazy"
     alt={`Preview of ${document.title}`}
   />
   ```

### Bundle Optimization

1. **Code Splitting**:
   ```typescript
   const DocumentLibrary = lazy(() => import('./DocumentLibrary'));
   const UploadZone = lazy(() => import('./UploadZone'));
   ```

2. **Dynamic Imports**:
   - Load heavy components on demand
   - Separate vendor bundles

## Testing Strategy

### Component Tests (Jest + React Testing Library)

```typescript
describe('DocumentUploadZone', () => {
  it('should handle file drop', async () => {
    const file = new File(['content'], 'test.pdf', { type: 'application/pdf' });

    render(<DocumentUploadZone onUploadStart={mockOnUploadStart} />);

    const dropZone = screen.getByLabelText(/upload documents/i);
    fireEvent.drop(dropZone, { dataTransfer: { files: [file] } });

    await waitFor(() => {
      expect(mockOnUploadStart).toHaveBeenCalledWith([file]);
    });
  });

  it('should show upload progress', () => {
    const progress = [{ fileId: '1', fileName: 'test.pdf', progressPercentage: 50, status: 'uploading' }];

    render(<UploadProgressTracker uploadJobs={progress} />);

    expect(screen.getByText('50%')).toBeInTheDocument();
  });
});
```

### Integration Tests

```typescript
describe('Document Upload Flow', () => {
  it('should upload and process documents end-to-end', async () => {
    // Mock API responses
    mockDocumentService.uploadDocuments.mockResolvedValue(mockUploadResponse);

    const { getByLabelText, getByText } = render(<DashboardPage />);

    // Upload file
    const fileInput = getByLabelText(/upload documents/i);
    const file = new File(['content'], 'test.pdf', { type: 'application/pdf' });

    fireEvent.change(fileInput, { target: { files: [file] } });

    // Verify upload started
    await waitFor(() => {
      expect(getByText('Uploading...')).toBeInTheDocument();
    });

    // Simulate processing complete
    act(() => {
      mockWebSocketService.publish('documents/123/processing', {
        status: 'completed',
        progressPercentage: 100
      });
    });

    // Verify completion
    await waitFor(() => {
      expect(getByText('Processing complete')).toBeInTheDocument();
    });
  });
});
```

This frontend design provides a comprehensive, accessible, and performant document upload interface that seamlessly integrates with the existing RAG system architecture.
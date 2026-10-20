# Document Upload Components

This directory contains the enhanced document upload system for the Multimodal Enterprise RAG System.

## Components Overview

### DocumentUploadWizard
A comprehensive step-by-step upload wizard with drag-and-drop functionality, file validation, and real-time progress tracking.

**Features:**
- Multi-step wizard interface (Upload → Review → Processing → Complete)
- Drag-and-drop file upload with visual feedback
- File type and size validation with detailed error messages
- Image preview functionality
- Real-time upload progress tracking
- Batch upload management
- Error handling and retry options
- Responsive design and accessibility compliance

### useDocumentUpload Hook
A powerful React hook for managing document uploads with queue management, progress tracking, and real-time status updates.

**Features:**
- Queue management with configurable concurrent uploads
- File validation with comprehensive error handling
- Real-time progress tracking and statistics
- WebSocket integration for live updates
- Automatic cleanup of completed items
- TypeScript support with full type safety
- Retry functionality for failed uploads
- Upload speed and time estimation

### Upload Service
A singleton service class that handles the actual upload operations, queue management, and API communication.

**Features:**
- Concurrent upload management
- Progress tracking with detailed metrics
- WebSocket integration for real-time updates
- Automatic retry logic
- File validation and quota management
- Statistics calculation and reporting
- Memory-efficient cleanup

## File Structure

```
upload/
├── DocumentUploadWizard.tsx     # Main upload wizard component
├── DocumentUploadWizard.stories.tsx  # Storybook stories
├── index.ts                     # Exports
└── README.md                    # This file
```

## Usage Examples

### Basic DocumentUploadWizard

```tsx
import React, { useState } from 'react';
import { DocumentUploadWizard } from '@/components/upload';

export default function UploadPage() {
  const [isWizardOpen, setIsWizardOpen] = useState(false);

  return (
    <div>
      <button onClick={() => setIsWizardOpen(true)}>
        Upload Documents
      </button>

      <DocumentUploadWizard
        isOpen={isWizardOpen}
        onClose={() => setIsWizardOpen(false)}
        onComplete={(documentIds) => {
          console.log('Uploaded documents:', documentIds);
        }}
        maxFiles={10}
        maxFileSize={50 * 1024 * 1024} // 50MB
        currentQuotaUsed={1024 * 1024 * 1024} // 1GB
        maxQuota={5 * 1024 * 1024 * 1024} // 5GB
      />
    </div>
  );
}
```

### Using the useDocumentUpload Hook

```tsx
import React from 'react';
import { useDocumentUpload } from '@/components/upload';

export default function CustomUploadComponent() {
  const {
    queueItems,
    stats,
    addToQueue,
    removeFromQueue,
    retryUpload,
    isUploading,
    overallProgress,
    formatFileSize,
  } = useDocumentUpload({
    maxConcurrentUploads: 3,
    maxFiles: 10,
    autoCleanup: true,
  });

  const handleFileSelect = (files: File[]) => {
    const result = addToQueue(files);
    if (result.errors.length > 0) {
      console.error('Validation errors:', result.errors);
    }
  };

  return (
    <div>
      {/* Custom upload UI */}
      <input
        type="file"
        multiple
        onChange={(e) => handleFileSelect(Array.from(e.target.files || []))}
      />

      {/* Progress display */}
      <div>Progress: {overallProgress}%</div>
      <div>Uploading: {isUploading ? 'Yes' : 'No'}</div>

      {/* Queue items */}
      {queueItems.map(item => (
        <div key={item.id}>
          <span>{item.file.name}</span>
          <span>{formatFileSize(item.file.size)}</span>
          <span>{item.status}</span>
          <button onClick={() => removeFromQueue(item.id)}>Remove</button>
        </div>
      ))}
    </div>
  );
}
```

## Configuration Options

### useDocumentUpload Hook Options

```tsx
interface UseDocumentUploadOptions {
  maxConcurrentUploads?: number;    // Default: 3
  maxFileSize?: number;            // Default: 50MB
  maxFiles?: number;               // Default: 10
  allowedTypes?: string[];         // Default: All supported types
  currentQuotaUsed?: number;       // Default: 0
  maxQuota?: number;               // Default: 5GB
  autoCleanup?: boolean;           // Default: true
  cleanupInterval?: number;        // Default: 30 minutes
}
```

### DocumentUploadWizard Props

```tsx
interface DocumentUploadWizardProps {
  isOpen: boolean;                 // Required
  onClose: () => void;             // Required
  onComplete?: (documentIds: string[]) => void;
  maxFiles?: number;               // Default: 10
  maxFileSize?: number;            // Default: 50MB
  currentQuotaUsed?: number;       // Default: 0
  maxQuota?: number;               // Default: 5GB
}
```

## File Validation

The system includes comprehensive file validation:

### Supported File Types
- **Documents**: PDF, TXT
- **Images**: JPG, JPEG, PNG
- **Audio**: MP3, WAV
- **Video**: MP4, MOV

### Validation Rules
- File size limits
- File type restrictions
- File name validation (no invalid characters)
- Storage quota limits
- Maximum file count per batch

### Error Types
- `FILE_TOO_LARGE`: File exceeds size limit
- `UNSUPPORTED_TYPE`: File type not supported
- `TOO_MANY_FILES`: Exceeds maximum file count
- `QUOTA_EXCEEDED`: Storage quota exceeded
- `INVALID_NAME`: Invalid file name

## WebSocket Integration

The components integrate with WebSocket connections for real-time updates:

### Events
- `upload_progress`: Upload progress updates
- `processing_update`: Processing status changes
- `job_completed`: Job completion notification
- `job_failed`: Job failure notification
- `session_completed`: Session completion

### Message Format
```tsx
interface UploadWebSocketMessage {
  type: string;
  job_id?: string;
  session_id?: string;
  timestamp: string;
  payload: {
    progress?: number;
    status?: UploadJob['status'];
    current_step?: string;
    error_message?: string;
    document_id?: string;
  };
}
```

## Testing

Storybook is not installed in this repo (there is no `storybook` script), so
`DocumentUploadWizard.stories.tsx` is not runnable as-is. The upload service is
covered by Vitest:

```bash
pnpm --dir frontend exec vitest run src/services/__tests__/uploadService.test.ts
```

## Performance Considerations

- **Memory Management**: Automatic cleanup of completed items
- **Concurrent Uploads**: Configurable limits to prevent overwhelming the server
- **Progress Tracking**: Efficient state management with minimal re-renders
- **File Validation**: Client-side validation to reduce server load
- **WebSocket Updates**: Efficient real-time communication

## Accessibility

- **WCAG 2.1 AA** compliance
- **Keyboard Navigation**: Full keyboard support
- **Screen Reader**: Proper ARIA labels and descriptions
- **Focus Management**: Logical focus flow
- **Color Contrast**: Sufficient contrast ratios
- **Error Announcements**: Screen reader compatible error messages

## Browser Support

- **Chrome/Chromium**: 90+
- **Firefox**: 88+
- **Safari**: 14+
- **Edge**: 90+

## Dependencies

- React 18+
- TypeScript 4.9+
- Tailwind CSS
- Heroicons
- React Dropzone

## Contributing

When contributing to these components:

1. Follow the existing code patterns and TypeScript conventions
2. Add comprehensive JSDoc comments
3. Include Storybook stories for new features
4. Test accessibility with keyboard and screen reader
5. Verify responsive design on different screen sizes
6. Add appropriate error handling and loading states
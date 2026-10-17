> **Archived 2026-10-17: quick reference for a past type-safety effort; generated API types and adapters are described in docs/engineering/api-contracts.md.**
> Historical record, not a live contract. Originally at `frontend/docs/TYPE_SAFETY_QUICK_REFERENCE.md`; relative links and paths below were not updated.

# Type Safety Quick Reference

## Quick Start

### 1. Import the Type-Safe API Client

```typescript
import { typeSafeApiClient } from '@/services/typeSafeApiClient';
import * as schemas from '@/types/schemas';
```

### 2. Make Type-Safe API Calls

```typescript
// Get documents with validation
const documents = await typeSafeApiClient.getDocuments({ page: 1 });

// Upload file with transformation
const response = await typeSafeApiClient.uploadDocument(file);

// Search with validation
const results = await typeSafeApiClient.search({ query: 'test' });
```

### 3. Validate Files Before Upload

```typescript
import { validateFile } from '@/lib/typeGuards';

const validation = validateFile(file);
if (!validation.isValid) {
  console.error(validation.errors);
  return;
}
```

## Common Use Cases

### File Upload Flow

```typescript
import { enhancedDocumentServiceV2 } from '@/services/enhancedDocumentService.v2';
import { extractErrorMessage } from '@/lib/typeGuards';

const handleUpload = async (file: File) => {
  // 1. Validate
  const validation = enhancedDocumentServiceV2.validateFile(file);
  if (!validation.isValid) {
    setError(validation.errors.join(', '));
    return;
  }

  // 2. Upload
  try {
    const response = await enhancedDocumentServiceV2.uploadDocument(file, {
      title: file.name,
      enable_quality_check: true,
    });

    // 3. Track progress
    pollJobStatus(response.job_id);
  } catch (error) {
    setError(extractErrorMessage(error));
  }
};
```

### Safe URL Parameters

```typescript
import { buildSearchParams } from '@/lib/typeGuards';

const params = buildSearchParams({
  page: 1,
  search: undefined,  // Safely omitted
  status: 'completed',
});
// Result: ?page=1&status=completed
```

### Type Guards

```typescript
import { isDocument, isAPIError } from '@/lib/typeGuards';

if (isDocument(data)) {
  // TypeScript knows data is Document type
  console.log(data.title);
}

if (isAPIError(error)) {
  // TypeScript knows error structure
  console.log(error.error.message);
}
```

### Custom Validation

```typescript
import { validate } from '@/lib/typeGuards';
import { z } from 'zod';

const CustomSchema = z.object({
  id: z.string().uuid(),
  name: z.string().min(1),
});

const result = validate(CustomSchema, data);
if (result.success) {
  const validated = result.data;
  // Use validated data
} else {
  console.error(result.error);
}
```

## API Methods

### Authentication

```typescript
// Login
const { access_token, user } = await typeSafeApiClient.login({
  email: 'user@example.com',
  password: 'password',
});

// Get current user
const user = await typeSafeApiClient.getCurrentUser();
```

### Documents

```typescript
// List documents
const { documents, pagination } = await typeSafeApiClient.getDocuments({
  page: 1,
  page_size: 20,
  status: 'indexed',
});

// Get single document
const document = await typeSafeApiClient.getDocument(documentId);

// Delete document
await typeSafeApiClient.deleteDocument(documentId);

// Get job status
const status = await typeSafeApiClient.getJobStatus(jobId);
```

### Search

```typescript
// Search documents
const results = await typeSafeApiClient.search({
  query: 'machine learning',
  limit: 10,
});

console.log(results.results); // SearchResultItem[]
console.log(results.search_time_ms);
```

### Knowledge Graph

```typescript
// Get graph data
const graph = await typeSafeApiClient.getGraphData({
  document_ids: [documentId],
});

console.log(graph.nodes); // GraphNode[]
console.log(graph.edges); // GraphEdge[]

// Get entity details
const entity = await typeSafeApiClient.getEntityDetails(entityId, {
  include_relationships: true,
});
```

### Analytics

```typescript
// Get query metrics
const metrics = await typeSafeApiClient.getQueryMetrics(queryId);
console.log(metrics.answer_relevancy);
console.log(metrics.faithfulness);

// Get dashboard data
const analytics = await typeSafeApiClient.getDashboardData({
  time_range: '24h',
});
```

## Type Definitions

### Key Types from Schemas

```typescript
import type {
  Document,
  DocumentListResponse,
  DocumentUploadResponse,
  UploadProgress,
  SearchResult,
  Entity,
  GraphData,
  User,
  LoginResponse,
} from '@/types/schemas';
```

### File Types

```typescript
type FileType = 'pdf' | 'txt' | 'jpg' | 'png' | 'mp3' | 'mp4' | 'docx';
type ProcessingStatus = 'queued' | 'processing' | 'indexed' | 'failed';
```

### Response Wrapper

```typescript
interface TypedResponse<T> {
  data: T;
  status: number;
  headers: Headers;
}
```

## Error Handling

### Extract Error Messages

```typescript
import { extractErrorMessage } from '@/lib/typeGuards';

try {
  await typeSafeApiClient.getDocument(id);
} catch (error) {
  const message = extractErrorMessage(error);
  showError(message);
}
```

### Check Error Types

```typescript
import { isAPIErrorResponse } from '@/lib/typeGuards';

if (isAPIErrorResponse(error)) {
  console.log(error.error.status_code);
  console.log(error.error.type);
  console.log(error.error.details);
}
```

## Validation Utilities

### File Validation

```typescript
import { validateFile, validateFiles } from '@/lib/typeGuards';

// Single file
const result = validateFile(file, 50 * 1024 * 1024); // 50MB max
console.log(result.isValid);
console.log(result.errors);
console.log(result.warnings);

// Multiple files
const { valid, invalid } = validateFiles(files);
```

### Value Validation

```typescript
import {
  isNonEmptyString,
  isValidNumber,
  isUUID,
  isISODateString,
} from '@/lib/typeGuards';

if (isNonEmptyString(value)) {
  // value is string
}

if (isValidNumber(value)) {
  // value is number (not NaN or Infinity)
}

if (isUUID(value)) {
  // value is valid UUID
}

if (isISODateString(value)) {
  // value is valid ISO date string
}
```

### Object Utilities

```typescript
import {
  removeNullish,
  filterNullish,
  typedEntries,
  typedKeys,
} from '@/lib/typeGuards';

// Remove null/undefined from object
const clean = removeNullish({ a: 1, b: null, c: undefined });
// Result: { a: 1 }

// Filter array
const filtered = filterNullish([1, null, 2, undefined, 3]);
// Result: [1, 2, 3]

// Type-safe object iteration
typedEntries(obj).forEach(([key, value]) => {
  // key and value are properly typed
});
```

## Configuration

### Skip Validation (for debugging)

```typescript
const data = await typeSafeApiClient.get(
  '/endpoint',
  schema,
  { skipValidation: true }
);
```

### Custom Retry Configuration

```typescript
const data = await typeSafeApiClient.get(
  '/endpoint',
  schema,
  { retries: 5, retryDelay: 2000 }
);
```

## Best Practices

### ✅ DO

```typescript
// Validate before using external data
const result = validate(schema, data);
if (result.success) {
  useData(result.data);
}

// Use type guards for runtime checks
if (isDocument(item)) {
  console.log(item.title);
}

// Filter undefined from params
const params = buildSearchParams({ page, search });

// Validate files before upload
const validation = validateFile(file);
if (validation.isValid) {
  upload(file);
}
```

### ❌ DON'T

```typescript
// Don't skip validation in production
const data = await api.get('/endpoint', schema, { skipValidation: true });

// Don't use type assertions without validation
const document = apiResponse as Document; // Unsafe!

// Don't include undefined in URL params
const params = new URLSearchParams({ page: '1', search: undefined }); // Bad!

// Don't trust external data without validation
const title = apiResponse.title; // No guarantee title exists
```

## Troubleshooting

### Validation Errors

```typescript
import { validate } from '@/lib/typeGuards';

const result = validate(schema, data);
if (!result.success) {
  console.error('Validation failed:');
  result.error.errors.forEach(err => {
    console.error(`${err.path.join('.')}: ${err.message}`);
  });
}
```

### Type Mismatch Between Backend and Frontend

```typescript
// Use transformation utilities
import { transformFileUploadResponse } from '@/lib/typeGuards';

const backendResponse = await fetch('/upload');
const frontendResponse = transformFileUploadResponse(backendResponse);
```

### Debugging Schema Issues

```typescript
import * as schemas from '@/types/schemas';

// Check what schema expects
console.log(schemas.DocumentSchema.shape);

// Parse with detailed errors
try {
  const doc = schemas.DocumentSchema.parse(data);
} catch (error) {
  if (error instanceof z.ZodError) {
    console.log(error.format());
  }
}
```

## Performance Tips

1. **Cache schemas** - Don't recreate schemas in loops
2. **Skip validation in dev** - Only if needed for performance testing
3. **Use type guards** - More efficient than full schema validation
4. **Validate once** - Don't re-validate the same data
5. **Use lazy validation** - Only validate when needed

## Testing

### Test Type Guards

```typescript
import { isDocument } from '@/lib/typeGuards';

describe('Type Guards', () => {
  it('validates correct document', () => {
    expect(isDocument(validDoc)).toBe(true);
  });

  it('rejects invalid document', () => {
    expect(isDocument(invalidDoc)).toBe(false);
  });
});
```

### Test API Calls

```typescript
import { typeSafeApiClient } from '@/services/typeSafeApiClient';

describe('API Client', () => {
  it('returns validated documents', async () => {
    const result = await typeSafeApiClient.getDocuments();
    expect(result.documents).toBeInstanceOf(Array);
    result.documents.forEach(doc => {
      expect(isDocument(doc)).toBe(true);
    });
  });
});
```

## Migration Checklist

- [ ] Replace `apiClient` with `typeSafeApiClient`
- [ ] Add file validation before uploads
- [ ] Use `buildSearchParams` for URL params
- [ ] Add error handling with `extractErrorMessage`
- [ ] Replace type assertions with type guards
- [ ] Add tests for validation logic
- [ ] Update imports to use schemas
- [ ] Document any custom transformations

## Resources

- Full Documentation: `/docs/TYPESCRIPT_IMPROVEMENTS.md`
- Type Schemas: `/src/types/schemas.ts`
- Type Guards: `/src/lib/typeGuards.ts`
- API Client: `/src/services/typeSafeApiClient.ts`
- Enhanced Service: `/src/services/enhancedDocumentService.v2.ts`

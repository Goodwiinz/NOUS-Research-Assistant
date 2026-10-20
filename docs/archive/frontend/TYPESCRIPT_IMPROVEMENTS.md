> **Archived 2026-10-17: past TypeScript type-safety proposal; current rules are in docs/engineering/frontend.md and docs/engineering/api-contracts.md.**
> Historical record, not a live contract. Originally at `frontend/docs/TYPESCRIPT_IMPROVEMENTS.md`; relative links and paths below were not updated.

# TypeScript Type Safety Improvements

## Overview

This document describes the comprehensive TypeScript improvements implemented to address type mismatches, add runtime validation, and improve type safety across the frontend application.

## Problems Addressed

### 1. Backend/Frontend Type Mismatches

**Problem:** Frontend expected `DocumentUploadResponse` with `upload_id`, but backend returns `FileUploadResponse` with `id`.

**Solution:**
- Created separate schemas for backend and frontend types
- Implemented transformation layer in API client
- Added runtime validation to catch schema mismatches early

```typescript
// Backend response schema
export const FileUploadResponseSchema = z.object({
  id: z.string().uuid(),
  job_id: z.string().uuid(),
  message: z.string(),
  // ...
});

// Frontend expected schema
export const DocumentUploadResponseSchema = z.object({
  upload_id: z.string().uuid(), // Transformed from backend 'id'
  document_id: z.string().uuid(),
  // ...
});
```

### 2. URLSearchParams Converting undefined to "undefined"

**Problem:** URLSearchParams converts `undefined` to the string `"undefined"`, causing API issues.

**Solution:** Created type-safe `buildSearchParams` utility that filters out `undefined` and `null` values:

```typescript
export function buildSearchParams(
  params: Record<string, string | number | boolean | undefined | null>
): URLSearchParams {
  const filtered: Record<string, string> = {};

  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null) {
      filtered[key] = String(value);
    }
  });

  return new URLSearchParams(filtered);
}
```

### 3. Lack of Runtime Type Validation

**Problem:** TypeScript types only exist at compile-time, allowing invalid runtime data to cause errors.

**Solution:** Implemented Zod schemas for all API responses with runtime validation:

```typescript
// Define schema
const DocumentSchema = z.object({
  id: z.string().uuid(),
  title: z.string(),
  file_type: z.enum(['pdf', 'txt', 'jpg', 'png', 'mp3', 'mp4']),
  // ...
});

// Validate at runtime
const result = DocumentSchema.safeParse(apiResponse);
if (!result.success) {
  // Handle validation error
}
```

### 4. Missing Type Guards

**Problem:** No type guards for runtime type checking, leading to potential runtime errors.

**Solution:** Created comprehensive type guard library:

```typescript
// Generic type guard creator
export function createTypeGuard<T>(schema: ZodSchema<T>) {
  return (data: unknown): data is T => {
    const result = schema.safeParse(data);
    return result.success;
  };
}

// Specific type guards
export const isDocument = createTypeGuard(DocumentSchema);
export const isUploadProgress = createTypeGuard(UploadProgressSchema);
```

### 5. Weak Error Handling

**Problem:** Inconsistent error types and poor error messages.

**Solution:** Created typed error extraction and handling:

```typescript
export function extractErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (isAPIError(error)) return error.error.message;
  if (typeof error === 'string') return error;
  return 'An unknown error occurred';
}
```

## New Files Created

### 1. `/src/types/schemas.ts`
Comprehensive Zod schemas for all API types with runtime validation.

**Key Features:**
- 30+ Zod schemas covering all API responses
- Type inference from schemas
- Validation for UUIDs, timestamps, enums, etc.
- Nested object validation
- Array validation with proper typing

**Usage:**
```typescript
import * as schemas from '@/types/schemas';
import { validate } from '@/lib/typeGuards';

const result = validate(schemas.DocumentSchema, apiResponse);
if (result.success) {
  const document: schemas.Document = result.data;
}
```

### 2. `/src/lib/typeGuards.ts`
Type guards and utility functions for runtime type safety.

**Key Features:**
- Generic type guard creator
- Specific type guards for all schemas
- URL parameter builders with type safety
- File validation utilities
- Object/array manipulation utilities
- Response transformation utilities

**Usage:**
```typescript
import { validateFile, buildSearchParams, isDocument } from '@/lib/typeGuards';

// Validate file
const validation = validateFile(file);
if (!validation.isValid) {
  console.error(validation.errors);
}

// Build search params safely
const params = buildSearchParams({ page: 1, search: undefined }); // Only includes page

// Type guard
if (isDocument(data)) {
  // TypeScript knows data is Document type
  console.log(data.title);
}
```

### 3. `/src/services/typeSafeApiClient.ts`
Type-safe API client with runtime validation.

**Key Features:**
- All methods require Zod schema for validation
- Automatic retry with exponential backoff
- Request/response validation
- Typed error handling
- Response transformation (backend → frontend)
- Built-in schemas for common operations

**Usage:**
```typescript
import { typeSafeApiClient } from '@/services/typeSafeApiClient';

// Type-safe and validated
const documents = await typeSafeApiClient.getDocuments({ page: 1 });
// documents is DocumentListResponse type, validated at runtime

// Custom validation
const data = await typeSafeApiClient.get(
  '/custom/endpoint',
  z.object({ id: z.string(), name: z.string() })
);
```

### 4. `/src/services/enhancedDocumentService.v2.ts`
Enhanced document service with full type safety.

**Key Features:**
- File validation before upload
- Type-safe upload methods
- Proper error handling
- Progress tracking support
- Batch upload support
- Quality assessment integration

**Usage:**
```typescript
import { enhancedDocumentServiceV2 } from '@/services/enhancedDocumentService.v2';

// Validate file first
const validation = enhancedDocumentServiceV2.validateFile(file);
if (!validation.isValid) {
  throw new Error(validation.errors.join(', '));
}

// Upload with type safety
const response = await enhancedDocumentServiceV2.uploadDocument(file, {
  title: 'My Document',
  tags: ['important', 'urgent'],
});

// response is DocumentUploadResponse type, fully validated
```

## Type Safety Features

### 1. Compile-Time Type Safety

All API methods are fully typed with TypeScript:

```typescript
// TypeScript knows the exact return type
const document: Document = await typeSafeApiClient.getDocument('uuid');

// TypeScript enforces correct parameters
await typeSafeApiClient.getDocuments({
  page: 1,              // ✓ Valid
  invalid_param: true   // ✗ TypeScript error
});
```

### 2. Runtime Type Validation

All responses are validated at runtime with Zod:

```typescript
// If API returns invalid data, Zod catches it
try {
  const documents = await typeSafeApiClient.getDocuments();
} catch (error) {
  // Error includes validation details
  if (error.message.includes('Invalid API response format')) {
    console.error('API contract violation detected');
  }
}
```

### 3. Type Transformation

Automatic transformation between backend and frontend types:

```typescript
// Backend returns: { id: '123', job_id: '456', ... }
// Frontend receives: { upload_id: '123', document_id: '123', job_id: '456', ... }

const response = await typeSafeApiClient.uploadDocument(file);
console.log(response.upload_id); // ✓ Works correctly
```

### 4. Null Safety

Proper handling of null/undefined values:

```typescript
// Only includes defined values
const params = buildSearchParams({
  page: 1,
  search: undefined,  // Excluded
  status: null,       // Excluded
});
// Result: ?page=1

// Type-safe optional chaining
const thumbnail = document.thumbnail_url?.trim() ?? '/default.png';
```

## Migration Guide

### Migrating Existing Code

#### Step 1: Update Imports

```typescript
// Old
import { apiClient } from '@/services/api';
import { DocumentUploadResponse } from '@/types';

// New
import { typeSafeApiClient } from '@/services/typeSafeApiClient';
import * as schemas from '@/types/schemas';
```

#### Step 2: Update API Calls

```typescript
// Old (no validation)
const documents = await apiClient.get('/documents');

// New (validated)
const documents = await typeSafeApiClient.getDocuments();
```

#### Step 3: Add File Validation

```typescript
// Old (no validation)
await uploadDocument(file);

// New (with validation)
const validation = validateFile(file);
if (!validation.isValid) {
  showError(validation.errors.join(', '));
  return;
}
await enhancedDocumentServiceV2.uploadDocument(file);
```

#### Step 4: Handle Type Transformations

```typescript
// Old (assumed correct structure)
const uploadId = response.upload_id || response.id;

// New (guaranteed structure)
const uploadId = response.upload_id; // Always present and correct
```

### Backward Compatibility

The existing API clients (`api.ts`, `apiClient.ts`, `enhancedDocumentService.ts`) remain unchanged and functional. The new type-safe clients can be adopted incrementally:

1. Keep existing code working
2. Migrate high-risk areas first (file uploads, authentication)
3. Gradually migrate remaining code
4. Remove old clients once migration is complete

## Testing Recommendations

### 1. Unit Tests for Type Guards

```typescript
describe('Type Guards', () => {
  it('should validate correct document structure', () => {
    const validDocument = {
      id: '123e4567-e89b-12d3-a456-426614174000',
      title: 'Test',
      file_type: 'pdf',
      // ...
    };
    expect(isDocument(validDocument)).toBe(true);
  });

  it('should reject invalid document structure', () => {
    const invalidDocument = { id: 'invalid-uuid' };
    expect(isDocument(invalidDocument)).toBe(false);
  });
});
```

### 2. Integration Tests for API Calls

```typescript
describe('TypeSafeApiClient', () => {
  it('should validate response structure', async () => {
    const documents = await typeSafeApiClient.getDocuments();
    expect(Array.isArray(documents.documents)).toBe(true);
    documents.documents.forEach(doc => {
      expect(isDocument(doc)).toBe(true);
    });
  });
});
```

### 3. Mock Invalid API Responses

```typescript
describe('Error Handling', () => {
  it('should catch schema validation errors', async () => {
    mockApi.getDocuments.mockResolvedValue({ invalid: 'data' });

    await expect(
      typeSafeApiClient.getDocuments()
    ).rejects.toThrow('Invalid API response format');
  });
});
```

## Performance Considerations

### 1. Schema Validation Overhead

Zod validation adds ~1-5ms per validation. For typical API responses, this is negligible compared to network latency (50-500ms).

### 2. Bundle Size

- Zod adds ~50KB (gzipped) to bundle
- Type guards add ~10KB
- Total overhead: ~60KB (acceptable for improved safety)

### 3. Optimization Tips

```typescript
// Cache schemas for repeated validation
const cachedSchema = schemas.DocumentSchema;

// Skip validation in development for performance testing
const response = await typeSafeApiClient.get(
  endpoint,
  schema,
  { skipValidation: process.env.NODE_ENV === 'development' }
);
```

## Common Patterns

### Pattern 1: Safe API Call

```typescript
try {
  const document = await typeSafeApiClient.getDocument(id);
  // document is guaranteed to match Document schema
  setDocument(document);
} catch (error) {
  const message = extractErrorMessage(error);
  showError(message);
}
```

### Pattern 2: File Upload with Validation

```typescript
const handleUpload = async (file: File) => {
  const validation = validateFile(file);

  if (!validation.isValid) {
    setErrors(validation.errors);
    return;
  }

  if (validation.warnings.length > 0) {
    showWarnings(validation.warnings);
  }

  try {
    const response = await enhancedDocumentServiceV2.uploadDocument(file, {
      title: file.name,
      enable_quality_check: true,
    });

    setUploadId(response.upload_id);
    startPollingProgress(response.job_id);
  } catch (error) {
    setError(extractErrorMessage(error));
  }
};
```

### Pattern 3: Type-Safe Search Parameters

```typescript
const fetchDocuments = async (filters: DocumentFilters) => {
  const params = buildSearchParams({
    page: filters.page,
    page_size: filters.pageSize,
    status: filters.status,
    search: filters.search, // Safely handles undefined
  });

  const documents = await typeSafeApiClient.getDocuments(
    Object.fromEntries(params)
  );

  return documents;
};
```

## Best Practices

1. **Always validate external data** - Use Zod schemas for all API responses
2. **Use type guards for runtime checks** - Don't rely on TypeScript types alone
3. **Filter undefined/null from URL params** - Use `buildSearchParams`
4. **Validate files before upload** - Catch errors early
5. **Handle errors with type safety** - Use `extractErrorMessage`
6. **Transform backend responses** - Map to frontend-expected structure
7. **Test validation logic** - Write tests for schemas and type guards
8. **Document type transformations** - Comment where backend ≠ frontend
9. **Use strict TypeScript config** - Enable all strictness options
10. **Keep schemas in sync with backend** - Regular API contract reviews

## Future Improvements

1. **Auto-generate schemas from OpenAPI/Swagger** - Reduce manual maintenance
2. **Add GraphQL support** - Type-safe GraphQL queries
3. **Implement request validation** - Validate outgoing data too
4. **Add performance monitoring** - Track validation overhead
5. **Create VS Code snippets** - Speed up common patterns
6. **Generate API documentation** - From Zod schemas
7. **Add contract testing** - Ensure backend/frontend alignment
8. **Implement caching** - Cache validated responses

## Resources

- [Zod Documentation](https://zod.dev/)
- [TypeScript Handbook](https://www.typescriptlang.org/docs/handbook/intro.html)
- [Type Guards in TypeScript](https://www.typescriptlang.org/docs/handbook/2/narrowing.html)
- [Runtime Validation Best Practices](https://blog.logrocket.com/comparing-schema-validation-libraries-zod-vs-yup/)

## Support

For questions or issues with type safety:
1. Check this documentation first
2. Review existing type guards in `/src/lib/typeGuards.ts`
3. Check schemas in `/src/types/schemas.ts`
4. Review test examples for patterns
5. Create issue with reproduction case

## Changelog

### Version 1.0.0 (2025-10-23)
- Initial implementation of Zod schemas
- Created type guard library
- Implemented type-safe API client
- Added file validation utilities
- Created enhanced document service v2
- Fixed backend/frontend type mismatches
- Added comprehensive documentation

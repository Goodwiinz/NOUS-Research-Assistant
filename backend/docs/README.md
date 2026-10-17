# RAG Analytics API Documentation

> **Stale / historical (2026-10-17):** It covers only part of the API and its paths and schemas may have drifted. The source of truth is the generated `backend/openapi.json` (also served at `/docs` when `DEBUG` is on). Current contracts: [`docs/engineering/`](../../docs/engineering/README.md).

This directory contains comprehensive documentation for the RAG Analytics API, including OpenAPI specifications, usage examples, and integration guides.

## 📚 Documentation Files

### 1. OpenAPI Specification
- **File**: `analytics_api.yaml`
- **Description**: Complete OpenAPI 3.0.2 specification for all analytics endpoints
- **Features**:
  - an analytics endpoint set that predates the current API (`backend/openapi.json` lists 46 `/api/v1/analytics` paths today; this YAML does not track it)
  - Comprehensive schema definitions
  - Request/response examples
  - Authentication and security definitions
  - Error handling documentation

### 2. Usage Examples
- **File**: `analytics_examples.md`
- **Description**: Detailed examples and integration patterns
- **Content**:
  - Authentication setup
  - cURL, Python, and JavaScript examples
  - Error handling patterns
  - SDK integration examples
  - Best practices

### 3. API Test Suite
- **File**: none. `backend/src/tests/test_api_documentation.py` no longer exists. The enforced contract check is `python scripts/ci/generate_openapi.py --check` against `backend/openapi.json`, plus the suites under `backend/tests/unit/api`.

## 🚀 Quick Start

### 1. Access Interactive Documentation
```bash
# Start the backend (local dev does not use Docker; see docs/engineering/README.md)
# then browse to the docs, which are served only when DEBUG is true

# Access Swagger UI
open http://localhost:8000/docs

# Access ReDoc
open http://localhost:8000/redoc
```

### 2. Download OpenAPI Specification
```bash
# Get the raw OpenAPI spec
curl http://localhost:8000/openapi.json > openapi.json
```

### 3. Test API Endpoints
```bash
# Example: Get quality metrics
curl -X GET "http://localhost:8000/api/v1/analytics/quality/metrics?organization_id=your-org-id" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

## 📋 API Endpoints Overview

### Quality Metrics
- `GET /api/v1/analytics/quality/metrics` - Get quality metrics
- `GET /api/v1/analytics/quality/recommendations` - Get quality recommendations
- `GET /api/v1/analytics/quality/dashboard` - Get quality dashboard

### User Behavior Analytics
- `GET /api/v1/analytics/behavior/sessions` - Get user sessions
- `GET /api/v1/analytics/behavior/events` - Get behavior events
- `GET /api/v1/analytics/behavior/dashboard` - Get behavior dashboard

### Performance Monitoring
- `GET /api/v1/analytics/performance/metrics` - Get performance metrics
- `GET /api/v1/analytics/performance/dashboard` - Get performance dashboard
- `GET /api/v1/analytics/performance/health` - Get system health

### Analytics Events
- `POST /api/v1/analytics/events` - Create analytics event
- `GET /api/v1/analytics/events` - Get analytics events

### Background Jobs
- `POST /api/v1/analytics/jobs` - Create analytics job
- `GET /api/v1/analytics/jobs` - Get analytics jobs
- `GET /api/v1/analytics/jobs/{job_id}` - Get job details
- `DELETE /api/v1/analytics/jobs/{job_id}` - Cancel job

### Recommendations
- `GET /api/v1/analytics/recommendations` - Get recommendations

## 🔐 Authentication

All API endpoints require JWT authentication:

```bash
# Get JWT token
curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email": "your-email@example.com", "password": "your-password"}'

# Use token in requests
curl -X GET "http://localhost:8000/api/v1/analytics/quality/metrics" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

## 📊 Response Format

All API responses follow this structure:

### Success Response
```json
{
  "data": [...],
  "pagination": {
    "page": 1,
    "size": 20,
    "total": 100,
    "pages": 5,
    "has_next": true,
    "has_prev": false
  },
  "summary": {...}
}
```

### Error Response
```json
{
  "error": {
    "code": "ANALYTICS_ACCESS_DENIED",
    "message": "Insufficient permissions for analytics access",
    "details": "User role 'standard' requires 'analytics:read' permission",
    "timestamp": "2024-01-15T10:30:00Z",
    "request_id": "req_123456"
  }
}
```

## 🛠️ SDK Integration

### Python SDK
```python
from rag_analytics_sdk import RAGAnalyticsSDK

# Initialize SDK
sdk = RAGAnalyticsSDK(
    base_url="http://localhost:8000",
    api_token="YOUR_JWT_TOKEN",
    organization_id="YOUR_ORG_ID"
)

# Get quality metrics
metrics = sdk.get_quality_metrics(
    start_date="2024-01-01T00:00:00Z",
    end_date="2024-01-31T23:59:59Z"
)
```

### JavaScript SDK
```javascript
const { RAGAnalyticsSDK } = require('rag-analytics-sdk');

// Initialize SDK
const sdk = new RAGAnalyticsSDK(
    'http://localhost:8000',
    'YOUR_JWT_TOKEN',
    'YOUR_ORG_ID'
);

// Get quality metrics
const metrics = await sdk.getQualityMetrics({
    start_date: '2024-01-01T00:00:00Z',
    end_date: '2024-01-31T23:59:59Z'
});
```

## 📈 Rate Limiting

API requests are rate-limited based on user role:
- **Admin**: 1000 requests per hour
- **Premium**: 500 requests per hour
- **Standard**: 100 requests per hour

Rate limit headers are included in responses:
- `X-RateLimit-Limit`: Maximum requests per hour
- `X-RateLimit-Remaining`: Remaining requests
- `X-RateLimit-Reset`: Reset time (Unix timestamp)

## 🔍 Pagination

List endpoints support pagination:
- `page`: Page number (default: 1)
- `size`: Items per page (default: 20, max: 100)
- `sort`: Sort field (default: created_at)
- `order`: Sort order (asc/desc, default: desc)

```bash
curl -X GET "http://localhost:8000/api/v1/analytics/quality/metrics?page=2&size=50&sort=overall_score&order=desc" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

## 🧪 Testing

Run the API documentation tests:

```bash
# Run all documentation tests
pytest src/tests/test_api_documentation.py -v

# Run specific test
pytest src/tests/test_api_documentation.py::TestAPIDocumentation::test_openapi_spec_generation -v
```

## 📝 Changelog

### v1.0.0 (2024-01-15)
- ✅ Complete OpenAPI 3.0.2 specification
- ✅ 43 analytics endpoints documented
- ✅ Comprehensive request/response examples
- ✅ Authentication and security documentation
- ✅ Error handling documentation
- ✅ SDK integration examples
- ✅ Comprehensive test coverage

## 🤝 Contributing

When adding new endpoints:

1. Update the OpenAPI specification in `analytics_api.yaml`
2. Add usage examples in `analytics_examples.md`
3. Create tests in `test_api_documentation.py`
4. Validate the documentation with `test_openapi_spec_generation`

## 📞 Support

For API documentation issues:
- Create an issue in the repository
- Email: analytics-support@rag-system.com
- Documentation: https://docs.rag-system.com/analytics
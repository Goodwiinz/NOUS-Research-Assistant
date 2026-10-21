# Scalability & Testing Implementation Guide

**Created**: 2026-01-19
**Status**: ✅ Complete
**Coverage**: Scalability patterns + Feature-driven testing infrastructure

---

## 📋 Table of Contents

1. [Overview](#overview)
2. [Scalability Patterns](#scalability-patterns)
3. [Testing Infrastructure](#testing-infrastructure)
4. [Quick Start](#quick-start)
5. [Usage Examples](#usage-examples)
6. [CI/CD Pipeline](#cicd-pipeline)
7. [Best Practices](#best-practices)

---

## Overview

This implementation provides production-ready scalability patterns and comprehensive testing infrastructure for feature-driven development.

### What Was Implemented

| Component | Location | Purpose |
|-----------|----------|---------|
| **Resilience Module** | `backend/src/core/resilience.py` | Retry, bulkhead, timeout patterns |
| **Caching Module** | `backend/src/core/caching.py` | Decorator-based caching with Redis |
| **Pool Monitoring** | `backend/src/core/pool_monitoring.py` | Connection pool health tracking |
| **Test Fixtures** | `backend/tests/conftest_enhanced.py` | Enhanced fixtures for feature tests |
| **Test Templates** | `backend/tests/templates/` | Reusable test patterns |
| **CI/CD Pipeline** | `.github/workflows/test-pipeline.yml` | Automated testing workflow |

---

## Scalability Patterns

### 1. Retry with Exponential Backoff

Handle transient failures automatically with configurable retry strategies.

```python
from src.core.resilience import retry, RetryStrategy

# Basic retry (3 attempts with exponential backoff)
@retry(max_attempts=3, base_delay=1.0)
async def call_external_api():
    response = await httpx.get("https://api.example.com/data")
    return response.json()

# Advanced retry with custom strategy
@retry(
    max_attempts=5,
    base_delay=2.0,
    max_delay=60.0,
    strategy=RetryStrategy.EXPONENTIAL,
    retryable_exceptions=(ConnectionError, TimeoutError),
    non_retryable_exceptions=(ValueError, KeyError),
)
async def fetch_with_validation():
    data = await fetch_data()
    validate(data)  # ValueError won't be retried
    return data
```

**Features**:
- ✅ Exponential, linear, or constant backoff
- ✅ Jitter to prevent thundering herd
- ✅ Configurable exception types
- ✅ Retry callback hooks
- ✅ Works with sync and async functions

### 2. Bulkhead Pattern (Resource Isolation)

Prevent cascade failures by limiting concurrent access to resources.

```python
from src.core.resilience import Bulkhead, with_bulkhead, get_or_create_bulkhead

# Method 1: Decorator (automatic bulkhead creation)
@with_bulkhead("search", max_concurrent=100)
async def search_documents(query: str):
    return await search_service.search(query)

# Method 2: Context manager (manual control)
search_bulkhead = get_or_create_bulkhead("search", max_concurrent=100, max_queue=50)

async def perform_search(query: str):
    async with search_bulkhead.acquire(timeout=30.0):
        return await expensive_search(query)

# Method 3: Pre-configured bulkheads
from src.core.resilience import setup_default_bulkheads

setup_default_bulkheads()  # Creates bulkheads for common operations
```

**Pre-configured Bulkheads**:
- `search` - 100 concurrent, 50 queue
- `document_processing` - 10 concurrent, 100 queue
- `external_api` - 20 concurrent, 20 queue
- `llm` - 5 concurrent, 50 queue (rate limit protection)
- `db_write` - 50 concurrent, 100 queue

**Monitoring**:
```python
from src.core.resilience import get_all_bulkhead_stats

stats = get_all_bulkhead_stats()
for name, stat in stats.items():
    print(f"{name}: {stat.current_concurrent}/{stat.max_concurrent}")
    print(f"  Utilization: {stat.current_concurrent/stat.max_concurrent:.1%}")
    print(f"  Rejected: {stat.total_rejected}")
```

### 3. Timeout Management

Enforce operation timeouts to prevent hanging requests.

```python
from src.core.resilience import with_timeout, TimeoutError

# Basic timeout
@with_timeout(30.0)
async def slow_operation():
    await asyncio.sleep(5.0)
    return "completed"

# With custom operation name for logging
@with_timeout(10.0, operation_name="external_api_call")
async def call_external():
    return await httpx.get("https://slow-api.com")

# Exception handling
try:
    result = await slow_operation()
except TimeoutError as e:
    logger.error(f"Operation {e.operation} timed out after {e.timeout}s")
```

### 4. Combined Resilience

Apply multiple patterns with a single decorator.

```python
from src.core.resilience import resilient

@resilient(
    retry_attempts=3,           # Retry on failure
    retry_delay=1.0,            # Base delay between retries
    timeout=30.0,               # Overall operation timeout
    bulkhead="external_api",    # Limit concurrent calls
    bulkhead_limit=20,          # Max 20 concurrent
    circuit_breaker="external_api"  # Circuit breaker protection
)
async def call_external_service(data):
    return await service.process(data)
```

**Benefits**:
- Single decorator combines all patterns
- Automatic integration with circuit breaker
- Coordinated failure handling
- Comprehensive metrics tracking

### 5. Caching with Redis

Reduce database load and improve response times with intelligent caching.

```python
from src.core.caching import cached, invalidate_cache, warm_cache

# Basic caching
@cached(ttl=600, namespace="search")
async def search_documents(query: str, limit: int = 10):
    return await expensive_search(query, limit)

# Custom key builder
@cached(
    ttl=3600,
    namespace="embeddings",
    key_builder=lambda text: f"emb:{hash(text)}"
)
async def get_embedding(text: str):
    return await embedding_service.encode(text)

# Skip cache conditionally
@cached(
    ttl=300,
    namespace="user_data",
    skip_cache_if=lambda user_id: user_id.startswith("admin")
)
async def get_user_profile(user_id: str):
    return await db.get_user(user_id)

# Cache invalidation
await invalidate_cache("search", user_id, "recent")  # Specific
await invalidate_namespace("search")  # All search cache

# Cache warming (pre-populate with common queries)
common_queries = [
    (("machine learning",), {}),
    (("deep learning",), {}),
    (("neural networks",), {}),
]
await warm_cache(search_documents, common_queries, namespace="search")
```

**Cache Statistics**:
```python
from src.core.caching import get_all_cache_stats

stats = get_all_cache_stats()
for namespace, stat in stats.items():
    print(f"{namespace}:")
    print(f"  Hit rate: {stat.hit_rate:.1%}")
    print(f"  Hits: {stat.hits}, Misses: {stat.misses}")
    print(f"  Errors: {stat.errors}")
```

### 6. Connection Pool Monitoring

Track database and Redis connection pool health.

```python
from src.core.pool_monitoring import pool_manager, PoolHealthStatus

# Register pools (do this at startup)
from src.core.database import engine
from src.core.config import redis_client

pool_manager.register_sqlalchemy("primary", engine, alert_callback=log_alert)
pool_manager.register_redis("cache", redis_client)

# Get metrics
metrics = pool_manager.get_sqlalchemy_metrics("primary")
print(f"Pool utilization: {metrics.utilization:.1%}")
print(f"Checked out: {metrics.checked_out}/{metrics.pool_size}")
print(f"Avg checkout time: {metrics.avg_checkout_time:.3f}s")

# Health check
health = pool_manager.health_check()
if health["overall_status"] == PoolHealthStatus.CRITICAL.value:
    logger.critical("Database pool exhausted!")

# Alert callback
def log_alert(alert):
    if alert.severity == "critical":
        logger.critical(f"Pool alert: {alert.message}")
        # Send to monitoring system
        send_to_datadog(alert)
```

**Metrics Tracked**:
- Pool size, checked out, overflow
- Connection lifecycle (connects, disconnects, invalidations)
- Checkout duration (avg, max)
- Utilization percentage
- Errors and timeouts

---

## Testing Infrastructure

### Test Organization

```
tests/
├── conftest.py              # Base fixtures (existing)
├── conftest_enhanced.py     # Enhanced fixtures for feature dev ✨ NEW
├── pytest.ini               # Configuration ✨ UPDATED
├── README.md                # Testing guide ✨ NEW
│
├── templates/               # ✨ NEW - Copy these for new features
│   ├── test_feature_template.py       # Full feature test suite
│   ├── test_api_template.py           # REST API endpoints
│   └── test_resilience_template.py    # Resilience patterns
│
├── unit/                    # Fast, isolated tests
├── integration/             # Tests with mocked services
├── resilience/              # Retry, circuit breaker, bulkhead
├── e2e/                     # Full system tests
└── performance/             # Load and benchmark tests
```

### Test Markers

| Marker | Description | Speed | External Deps |
|--------|-------------|-------|---------------|
| `@pytest.mark.unit` | Isolated logic | Fast (<1s) | None |
| `@pytest.mark.integration` | Service integration | Medium (1-10s) | Mocked |
| `@pytest.mark.resilience` | Resilience patterns | Fast | None |
| `@pytest.mark.e2e` | Full system | Slow (10s+) | Real services |
| `@pytest.mark.slow` | Long-running | Very slow | Depends |
| `@pytest.mark.scalability` | Performance tests | Medium | Depends |

### Available Fixtures

#### Database Fixtures

```python
def test_with_sync_db(db_session):
    """Synchronous database session."""
    user = User(email="test@example.com")
    db_session.add(user)
    db_session.commit()

async def test_with_async_db(async_db_session):
    """Async database session."""
    stmt = select(User).where(User.email == "test@example.com")
    result = await async_db_session.execute(stmt)
```

#### API Client Fixtures

```python
def test_sync_api(client, auth_headers):
    """Synchronous test client."""
    response = client.get("/api/v1/users", headers=auth_headers)
    assert response.status_code == 200

async def test_async_api(async_client, auth_headers):
    """Async test client."""
    response = await async_client.get("/api/v1/users", headers=auth_headers)
    assert response.status_code == 200
```

#### Mock External Services

```python
def test_with_mocks(mock_redis, mock_qdrant, mock_neo4j, mock_openai):
    """All external services mocked."""
    # mock_redis returns None for cache misses
    # mock_qdrant returns empty search results
    # mock_neo4j returns empty graph queries
    # mock_openai returns predictable responses
```

#### Resilience Testing

```python
def test_retry_behavior(mock_failing_service):
    """Test retry with controlled failures."""
    mock_call, get_count = mock_failing_service(fail_count=2)

    @retry(max_attempts=3)
    async def operation():
        return await mock_call()

    result = await operation()
    assert result["success"] is True
    assert get_count() == 3  # 2 failures + 1 success
```

#### Performance Tracking

```python
async def test_latency(perf_tracker):
    """Track operation latency."""
    for _ in range(100):
        async with perf_tracker.measure("search"):
            await search_documents("test query")

    stats = perf_tracker.get_stats("search")
    assert stats["avg"] < 0.1  # 100ms threshold
    assert stats["p95"] < 0.2  # 200ms p95
```

#### Test Data Factory

```python
def test_with_factory(factory):
    """Generate test data."""
    doc = factory.document(
        title="Machine Learning Basics",
        tags=["ml", "tutorial"],
        metadata={"pages": 50}
    )

    user = factory.user(
        email="researcher@example.com",
        role="researcher"
    )

    query = factory.search_query(
        query="machine learning",
        limit=20,
        filters={"tags": ["ml"]}
    )
```

---

## Quick Start

### Running Tests

```bash
# Activate virtual environment
source .venv/bin/activate
cd backend

# Run unit tests (fast)
pytest -m unit

# Run with coverage
pytest -m unit --cov=src --cov-report=html --cov-fail-under=60

# Run specific test category
pytest -m integration
pytest -m resilience
pytest -m scalability

# Parallel execution (faster)
pytest -m unit -n auto

# Verbose output
pytest -m unit -v --tb=long

# Run specific test file
pytest tests/unit/test_my_feature.py

# Run specific test
pytest tests/unit/test_my_feature.py::TestMyFeature::test_specific
```

### Creating Tests for New Features

**Step 1: Copy Template**
```bash
# For new feature/service
cp tests/templates/test_feature_template.py tests/unit/test_my_feature.py

# For new API endpoints
cp tests/templates/test_api_template.py tests/integration/test_my_api.py

# For resilience testing
cp tests/templates/test_resilience_template.py tests/resilience/test_my_service_resilience.py
```

**Step 2: Find & Replace**
```bash
# Replace FEATURE_NAME or RESOURCE_NAME
sed -i '' 's/FEATURE_NAME/my_feature/g' tests/unit/test_my_feature.py
```

**Step 3: Implement Tests**
```python
@pytest.mark.unit
class TestMyFeatureUnit:
    def test_validation(self):
        # Arrange
        input_data = {"key": "value"}

        # Act
        result = validate_input(input_data)

        # Assert
        assert result.is_valid
```

---

## Usage Examples

### Example 1: Scalable Search Service

```python
from src.core.resilience import resilient
from src.core.caching import cached

class SearchService:
    @resilient(
        retry_attempts=3,
        timeout=30.0,
        bulkhead="search",
        bulkhead_limit=100,
    )
    @cached(ttl=600, namespace="search")
    async def search(self, query: str, limit: int = 10):
        """
        Search with:
        - Automatic retry on failures
        - 30s timeout
        - Max 100 concurrent searches
        - 10min cache
        """
        results = await self._execute_search(query, limit)
        return results
```

### Example 2: External API Integration

```python
from src.core.resilience import retry, with_timeout
from src.core.circuit_breaker import with_circuit_breaker

class ExternalAPIClient:
    @with_circuit_breaker("external_api")
    @retry(max_attempts=3, base_delay=1.0)
    @with_timeout(10.0)
    async def fetch_data(self, endpoint: str):
        """
        External API call with:
        - Circuit breaker (fail fast when service is down)
        - Retry on transient errors
        - 10s timeout
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{self.base_url}/{endpoint}")
            response.raise_for_status()
            return response.json()
```

### Example 3: Cached LLM Responses

```python
from src.core.caching import cached, invalidate_cache

class LLMService:
    @cached(
        ttl=3600,  # 1 hour
        namespace="llm_responses",
        skip_cache_if=lambda prompt: len(prompt) > 10000  # Don't cache huge prompts
    )
    async def generate(self, prompt: str, model: str = "gpt-4"):
        """
        LLM generation with semantic caching.
        Same prompts return cached responses.
        """
        response = await self.client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content

    async def invalidate_user_cache(self, user_id: str):
        """Clear cache for specific user."""
        await invalidate_cache("llm_responses", user_id)
```

### Example 4: Test Suite for New Feature

```python
# tests/unit/test_document_processor.py
import pytest
from src.services.document_processor import DocumentProcessor

@pytest.mark.unit
class TestDocumentProcessorUnit:
    """Unit tests - fast, isolated."""

    def test_validation_rejects_empty_content(self):
        processor = DocumentProcessor()

        with pytest.raises(ValueError, match="content cannot be empty"):
            processor.validate_document({"title": "Test", "content": ""})

    @pytest.mark.parametrize("file_type,expected", [
        ("pdf", True),
        ("txt", True),
        ("mp3", True),
        ("unknown", False),
    ])
    def test_supported_file_types(self, file_type, expected):
        processor = DocumentProcessor()
        assert processor.is_supported(file_type) == expected


@pytest.mark.integration
class TestDocumentProcessorIntegration:
    """Integration tests - with mocked services."""

    @pytest.mark.asyncio
    async def test_process_with_embeddings(
        self,
        db_session,
        mock_embeddings,
        mock_qdrant
    ):
        processor = DocumentProcessor(
            db=db_session,
            embeddings=mock_embeddings,
            vector_store=mock_qdrant
        )

        result = await processor.process_document({
            "title": "Test Doc",
            "content": "Machine learning concepts"
        })

        assert result.status == "completed"
        mock_embeddings.embed_documents.assert_called_once()
        mock_qdrant.upsert.assert_called_once()


@pytest.mark.resilience
class TestDocumentProcessorResilience:
    """Resilience tests."""

    @pytest.mark.asyncio
    async def test_retries_on_vector_store_failure(
        self,
        mock_failing_service
    ):
        mock_upsert, get_count = mock_failing_service(fail_count=2)

        # Processor with retry enabled
        processor = DocumentProcessor()

        with patch.object(processor.vector_store, 'upsert', mock_upsert):
            await processor.process_document({"title": "Test", "content": "..."})

        assert get_count() == 3  # Succeeded after 2 retries
```

---

## CI/CD Pipeline

### GitHub Actions Workflow

The pipeline runs automatically on push/PR:

```
┌─────────────────────────────────────────────────────────────┐
│                    Lint & Type Check                         │
│  - ruff, black, isort, mypy                                  │
└──────────────────┬──────────────────────────────────────────┘
                   │
        ┌──────────┼──────────┬──────────────────┐
        │          │          │                  │
┌───────▼────┐ ┌──▼──────┐ ┌─▼─────────┐ ┌─────▼──────┐
│Unit Tests  │ │Frontend │ │Resilience │ │Security    │
│(fast)      │ │Tests    │ │Tests      │ │Scan        │
└───────┬────┘ └──┬──────┘ └─┬─────────┘ └────────────┘
        │         │          │
        └─────────┼──────────┘
                  │
        ┌─────────▼─────────┐
        │ Integration Tests │
        │(Postgres + Redis) │
        └─────────┬─────────┘
                  │
                  ├──► API Contract Tests
                  ├──► E2E Tests (main/develop only)
                  └──► Performance Tests (main only)
```

### Test Categories in CI

| Stage | Runs On | Duration | Coverage |
|-------|---------|----------|----------|
| Lint | All branches | <1 min | N/A |
| Unit | All branches | 2-5 min | 60% minimum |
| Integration | All branches | 5-10 min | 40% minimum |
| Resilience | All branches | 2-3 min | Core patterns |
| E2E | main, develop | 10-15 min | Critical paths |
| Performance | main only | 5-10 min | Benchmarks |

### Coverage Requirements

```ini
# pytest.ini
[pytest]
addopts = --cov=src --cov-report=html --cov-fail-under=60
```

View coverage report:
```bash
pytest --cov=src --cov-report=html
open htmlcov/index.html
```

---

## Best Practices

### Scalability

1. **Use Bulkheads for All External Services**
   ```python
   # ✅ Good - Limited concurrency
   @with_bulkhead("external_api", max_concurrent=20)
   async def call_api():
       pass

   # ❌ Bad - Unlimited concurrency
   async def call_api():
       pass
   ```

2. **Cache Expensive Operations**
   ```python
   # ✅ Good - Cache search results
   @cached(ttl=600, namespace="search")
   async def search(query):
       pass

   # ❌ Bad - No caching
   async def search(query):
       # Expensive operation repeated
       pass
   ```

3. **Always Set Timeouts**
   ```python
   # ✅ Good - Explicit timeout
   @with_timeout(30.0)
   async def operation():
       pass

   # ❌ Bad - Can hang forever
   async def operation():
       await potentially_slow_operation()
   ```

4. **Combine Patterns Wisely**
   ```python
   # ✅ Good - Complete protection
   @resilient(
       retry_attempts=3,
       timeout=30.0,
       bulkhead="api",
       circuit_breaker="external"
   )
   async def protected_call():
       pass
   ```

5. **Monitor Pool Health**
   ```python
   # ✅ Good - Regular health checks
   @app.get("/health/pools")
   async def pool_health():
       health = pool_manager.health_check()
       if health["overall_status"] == "critical":
           raise HTTPException(503, "Pool exhausted")
       return health
   ```

### Testing

1. **Start with Unit Tests**
   ```python
   # ✅ Good - Fast, focused
   @pytest.mark.unit
   def test_validation():
       assert validate_email("test@example.com") == True

   # ❌ Bad - Too slow for unit test
   @pytest.mark.unit
   async def test_full_flow():
       # Creates DB, calls APIs, etc.
       pass
   ```

2. **Use Appropriate Markers**
   ```python
   # ✅ Good - Correct categorization
   @pytest.mark.unit
   def test_logic():
       pass

   @pytest.mark.integration
   async def test_with_db(db_session):
       pass

   # ❌ Bad - Wrong marker
   @pytest.mark.unit
   async def test_with_db(db_session):  # Should be integration
       pass
   ```

3. **Test Resilience Patterns**
   ```python
   # ✅ Good - Test retry behavior
   @pytest.mark.resilience
   async def test_retry_succeeds(mock_failing_service):
       mock_call, count = mock_failing_service(fail_count=2)

       @retry(max_attempts=3)
       async def operation():
           return await mock_call()

       result = await operation()
       assert count() == 3  # Verify retries
   ```

4. **Use Test Templates**
   ```bash
   # ✅ Good - Use templates
   cp tests/templates/test_feature_template.py tests/unit/test_my_feature.py

   # ❌ Bad - Start from scratch
   # Writing everything manually
   ```

5. **Track Performance**
   ```python
   # ✅ Good - Measure latency
   @pytest.mark.scalability
   async def test_search_latency(perf_tracker):
       async with perf_tracker.measure("search"):
           await search_documents("test")

       stats = perf_tracker.get_stats("search")
       assert stats["avg"] < 0.1  # 100ms SLA
   ```

---

## Troubleshooting

### Import Errors

**Problem**: `ImportError: cannot import name 'X' from 'module'`

**Solution**:
```bash
# Ensure PYTHONPATH is set
export PYTHONPATH="${PYTHONPATH}:$(pwd)/backend"
pytest
```

### Async Tests Hang

**Problem**: Tests timeout on async operations

**Solution**:
```bash
# Add global timeout
pytest --timeout=30

# Or in pytest.ini
[pytest]
timeout = 300
```

### Flaky Tests

**Problem**: Tests pass/fail randomly

**Solution**:
```bash
# Run multiple times to identify
pytest --count=10 tests/path/to/flaky_test.py

# Check for:
# - Shared state between tests
# - Time-dependent logic
# - External service dependencies
```

### Database State Leaks

**Problem**: Test state bleeds between tests

**Solution**:
```python
# Use function-scoped fixtures
@pytest.fixture(scope="function")  # Not "module" or "session"
def db_session():
    # Creates fresh DB for each test
    pass
```

---

## Summary

✅ **Scalability Patterns Implemented**:
- Retry with exponential backoff
- Bulkhead (resource isolation)
- Timeout management
- Combined resilience decorator
- Redis caching with decorators
- Connection pool monitoring

✅ **Testing Infrastructure**:
- Enhanced fixtures for feature development
- Test templates (feature, API, resilience)
- Comprehensive markers and categorization
- Performance tracking utilities
- CI/CD pipeline with multiple stages

✅ **Production Ready**:
- Monitoring and alerting
- Health checks
- Statistics tracking
- Error handling
- Documentation

---

## Next Steps

1. **Integrate with Existing Services**
   ```python
   # Add to hybrid search service
   @resilient(retry_attempts=3, bulkhead="search", timeout=30)
   @cached(ttl=600, namespace="search")
   async def search(query):
       pass
   ```

2. **Add Pool Monitoring to Startup**
   ```python
   # In main.py lifespan
   @asynccontextmanager
   async def lifespan(app: FastAPI):
       pool_manager.register_sqlalchemy("primary", engine)
       pool_manager.register_redis("cache", redis_client)
       yield
       pool_manager.shutdown()
   ```

3. **Create Tests for Existing Features**
   ```bash
   # Use templates
   cp tests/templates/test_feature_template.py tests/unit/test_search.py
   cp tests/templates/test_api_template.py tests/integration/test_search_api.py
   ```

4. **Run Full Test Suite**
   ```bash
   pytest --cov=src --cov-report=html --cov-fail-under=60
   ```

---

**Implementation Date**: 2026-01-19
**Maintained By**: Backend Team
**Questions**: See `/docs/` or `backend/tests/README.md`

# Testing Infrastructure

This directory contains the testing infrastructure for feature-driven development.

## Quick Start

```bash
# Run all unit tests (fast)
pytest -m unit

# Run integration tests
pytest -m integration

# Run resilience tests
pytest -m resilience

# Run all tests with coverage
pytest --cov=src --cov-report=html

# Run tests in parallel
pytest -n auto

# Run specific test file
pytest tests/unit/test_my_feature.py

# Run with verbose output
pytest -v --tb=long
```

## Test Categories

| Marker | Description | Speed | External Deps |
|--------|-------------|-------|---------------|
| `unit` | Isolated unit tests | Fast (<1s) | None |
| `integration` | Service integration | Medium (1-10s) | Mocked |
| `e2e` | End-to-end tests | Slow (10s+) | Real services |
| `resilience` | Retry, circuit breaker, bulkhead | Fast | None |
| `scalability` | Caching, pooling, performance | Medium | Depends |
| `slow` | Long-running tests | Very slow | Depends |
| `smoke` | Quick validation | Very fast | None |
| `regression` | Previously fixed bugs | Fast | None |
| `feature` | Feature-specific tests | Varies | Depends |
| `chat` | Chat API and messaging tests | Varies | Depends |
| `database` | Database connection and operation tests | Varies | Database |
| `langsmith` | LangSmith evaluation tests | Slow | `LANGCHAIN_API_KEY` and LLM credentials |
| `golden` | Local golden-case agent regression | Slow | LLM credentials, no LangSmith dataset |
| `eval` | Slow evaluation benchmarks running the agent against a dataset | Very slow | LLM credentials |

`backend/pytest.ini` registers exactly these 14 markers and runs with `--strict-markers`, so an unregistered marker fails collection. Its `slow` description says such tests are skipped unless `--run-slow` is passed, but no `--run-slow` option is defined in any `conftest.py` under `backend/`; treat that flag as unverified.

## Directory Structure

Configuration is `backend/pytest.ini` (there is no `tests/pytest.ini`). `norecursedirs` skips `templates`, `standalone`, `performance` and `scaffolding`. Top level of `backend/tests/` (check with `ls`):

```
tests/
├── conftest.py, conftest_fastapi.py, fixtures.py   # shared fixtures
├── unit/            # fast isolated tests: agent, api, architecture, core, models, services, tasks, ...
├── api/             # API-level tests (see also unit/api)
├── integration/     # service integration tests
├── contract/        # API contract tests
├── security/        # security and tenancy tests
├── services/, agent/, core/, db/, evidence/, tasks/, eval/   # additional per-area suites
├── fixtures/        # shared test data (ai_responses.py, file_fixtures.py, migrations/, ...)
├── factories/       # test data factories
├── mocks/           # mock AI client and service doubles
├── templates/       # copy-paste templates (test_feature_template.py, test_api_template.py, test_resilience_template.py)
├── standalone/, scaffolding/, performance/, perf/      # excluded or manual suites
└── test_*.py        # root-level regression tests
```

Resilience tests have no dedicated directory; mark them with `@pytest.mark.resilience` wherever they live.

## Creating Tests for New Features

### 1. Copy the appropriate template

```bash
# For a new service/feature
cp tests/templates/test_feature_template.py tests/unit/test_my_feature.py

# For new API endpoints
cp tests/templates/test_api_template.py tests/integration/test_my_api.py

# For resilience testing
cp tests/templates/test_resilience_template.py tests/unit/test_my_service_resilience.py
```

### 2. Replace placeholder names

Find and replace `FEATURE_NAME` or `RESOURCE_NAME` with your actual feature/resource name.

### 3. Implement the tests

Follow the patterns in the template:

```python
@pytest.mark.unit
class TestMyFeatureUnit:
    """Unit tests - fast, isolated."""
    
    def test_something(self):
        # Arrange
        input_data = {...}
        
        # Act
        result = my_function(input_data)
        
        # Assert
        assert result == expected


@pytest.mark.integration
class TestMyFeatureIntegration:
    """Integration tests - may use services."""
    
    @pytest.mark.asyncio
    async def test_service_integration(self, db_session, mock_redis):
        # Test with real DB session but mocked Redis
        pass
```

## Available Fixtures

### Database Fixtures

```python
def test_with_db(db_session):
    """Sync database session."""
    db_session.add(MyModel(...))
    db_session.commit()

async def test_with_async_db(async_db_session):
    """Async database session."""
    await async_db_session.execute(...)
```

### API Client Fixtures

```python
def test_api(client, auth_headers):
    """Sync test client with auth."""
    response = client.get("/api/v1/resource", headers=auth_headers)

async def test_async_api(async_client, auth_headers):
    """Async test client."""
    response = await async_client.get("/api/v1/resource", headers=auth_headers)
```

### Mock Fixtures

```python
def test_with_mocks(mock_redis, mock_qdrant, mock_neo4j, mock_openai):
    """All external services mocked."""
    pass
```

### Resilience Fixtures

```python
def test_retry(reset_circuit_breakers, reset_bulkheads, mock_failing_service):
    """Resilience pattern testing."""
    mock_call, get_count = mock_failing_service(fail_count=2)
    # Test retry behavior
```

### Performance Fixtures

```python
async def test_performance(perf_tracker):
    """Track performance metrics."""
    async with perf_tracker.measure("operation"):
        await my_operation()
    
    stats = perf_tracker.get_stats("operation")
    assert stats["avg"] < 0.1  # 100ms threshold
```

### Test Data Factory

```python
def test_with_factory(factory):
    """Create test data."""
    doc = factory.document(title="Test", tags=["test"])
    user = factory.user(email="test@example.com")
    query = factory.search_query(query="test query")
```

## Running in CI/CD

The GitHub Actions workflow (`.github/workflows/test-pipeline.yml`) runs:

1. **Lint & Type Check** - Fast code quality checks
2. **Unit Tests** - Fast, isolated tests
3. **Integration Tests** - Tests with services (Postgres, Redis)
4. **Resilience Tests** - Retry, circuit breaker, bulkhead tests
5. **Frontend Tests** - TypeScript/React tests
6. **API Contract Tests** - OpenAPI schema validation
7. **E2E Tests** - Full system tests (main/develop only)
8. **Performance Tests** - Benchmarks (main branch only)
9. **Security Scan** - Bandit, Safety checks

## Best Practices

### 1. Test Naming

```python
def test_<what>_<condition>_<expected>():
    """
    test_create_document_with_valid_data_returns_201
    test_search_with_empty_query_returns_validation_error
    test_retry_after_transient_failure_succeeds
    """
```

### 2. Test Structure (AAA Pattern)

```python
def test_something():
    # Arrange - Set up test data
    input_data = {...}
    expected = {...}
    
    # Act - Execute the code under test
    result = function_under_test(input_data)
    
    # Assert - Verify the results
    assert result == expected
```

### 3. Use Parametrize for Multiple Cases

```python
@pytest.mark.parametrize("input,expected", [
    ("case1", "result1"),
    ("case2", "result2"),
    ("case3", "result3"),
])
def test_multiple_cases(input, expected):
    assert function(input) == expected
```

### 4. Mark Tests Appropriately

```python
@pytest.mark.unit
def test_fast_isolated():
    pass

@pytest.mark.integration
@pytest.mark.asyncio
async def test_with_services():
    pass

@pytest.mark.slow
def test_long_running():
    pass
```

### 5. Clean Up Resources

```python
@pytest.fixture
def resource():
    # Setup
    resource = create_resource()
    yield resource
    # Teardown
    resource.cleanup()
```

## Coverage Requirements

| Test Type | Minimum Coverage |
|-----------|-----------------|
| Unit Tests | 60% |
| Integration Tests | 40% |
| Combined | 70% |

Run coverage report:

```bash
pytest --cov=src --cov-report=html --cov-fail-under=60
open htmlcov/index.html
```

## Troubleshooting

### Tests hang on async operations

```bash
# Add timeout
pytest --timeout=30
```

### Database state bleeds between tests

```python
# Use function-scoped fixtures
@pytest.fixture(scope="function")
def db_session():
    ...
```

### Flaky tests

```bash
# Run test multiple times
pytest --count=5 tests/path/to/flaky_test.py
```

### Import errors

```bash
# Ensure PYTHONPATH is set
export PYTHONPATH="${PYTHONPATH}:$(pwd)/backend"
pytest
```

# Monitoring System Testing Guide

> **Stale (2026-10-17): this guide predates the current layout. Qdrant is fully removed and live retrieval is PostgreSQL full-text search, so statements about Qdrant or vector stores below are historical. It cites `frontend/package-lock.json` and other files that do not exist, and uses `npm`; the repository uses `pnpm` with a single root `pnpm-lock.yaml`.**

## Overview

This guide provides comprehensive documentation for the monitoring system test suite, covering integration tests, E2E tests, performance validation, and accessibility testing for the Multimodal Enterprise RAG System monitoring implementation.

## Table of Contents

1. [Test Architecture](#test-architecture)
2. [Prerequisites](#prerequisites)
3. [Running Tests](#running-tests)
4. [Test Categories](#test-categories)
5. [Test Configuration](#test-configuration)
6. [CI/CD Integration](#cicd-integration)
7. [Troubleshooting](#troubleshooting)
8. [Best Practices](#best-practices)

## Test Architecture

### Backend Tests

```
tests/
├── integration/
│   ├── test_monitoring_services_integration.py    # Core services integration
│   ├── test_websocket_monitoring_realtime.py      # WebSocket real-time tests
│   ├── test_database_monitoring_integration.py    # Database schema tests
│   └── test_monitoring_api_integration.py         # API endpoint tests
├── contract/
│   └── monitoring/                                # Contract tests
├── performance/
│   └── monitoring_load_tests.py                   # Load testing scripts
└── e2e/
    └── monitoring_e2e.spec.ts                     # End-to-end tests
```

### Frontend Tests

```
frontend/src/integration/
├── __tests__/
│   ├── monitoring-dashboard.test.tsx             # Dashboard component tests
│   ├── monitoring-store.test.ts                   # State management tests
│   └── monitoring-react-query.test.tsx           # React Query tests
└── e2e/
    └── monitoring/
        └── monitoring-user-journeys.spec.ts       # User journey tests
```

## Prerequisites

### System Requirements

- **Node.js**: >= 18.0.0
- **Python**: >= 3.9
- **Docker**: >= 20.10
- **Docker Compose**: >= 2.0
- **Memory**: >= 8GB RAM
- **Storage**: >= 10GB free space

### Dependencies

#### Backend

```bash
# Install Python dependencies
cd backend
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Install test dependencies
pip install pytest pytest-asyncio pytest-cov
pip install httpx websockets pytest-mock
pip install aioredis asyncpg psycopg2-binary
```

#### Frontend

```bash
# Install Node.js dependencies
cd frontend
npm install

# Install test dependencies
npm install --save-dev @playwright/test @testing-library/react
npm install --save-dev @testing-library/jest-dom @testing-library/user-event
npm install --save-dev msw @axe-core/playwright
```

### Docker Services

```bash
# Start required services
docker-compose -f docker-compose.test.yml up -d

# Verify services are running
docker-compose ps
```

## Running Tests

### Backend Tests

#### All Integration Tests

```bash
# Run all monitoring integration tests
cd tests
pytest integration/test_monitoring_services_integration.py -v

# Run with coverage
pytest integration/ --cov=src.monitoring --cov-report=html

# Run specific test class
pytest integration/test_monitoring_services_integration.py::TestMetricsServiceIntegration -v
```

#### WebSocket Tests

```bash
# Run WebSocket integration tests
pytest integration/test_websocket_monitoring_realtime.py -v

# Run with specific markers
pytest integration/test_websocket_monitoring_realtime.py -m "not performance" -v
```

#### Database Tests

```bash
# Run database integration tests
pytest integration/test_database_monitoring_integration.py -v

# Run with database-specific markers
pytest integration/test_database_monitoring_integration.py -m "database" -v
```

#### API Tests

```bash
# Run API integration tests
pytest integration/test_monitoring_api_integration.py -v

# Run OpenAPI compliance tests
pytest integration/test_monitoring_api_integration.py::TestOpenAPICompliance -v
```

### Frontend Tests

#### Component Tests

```bash
# Run all component tests
cd frontend
npm run test:integration

# Run specific test file
npm run test:integration -- monitoring-dashboard.test.tsx

# Run with coverage
npm run test:integration -- --coverage
```

#### E2E Tests

```bash
# Install Playwright browsers
npx playwright install

# Run all E2E tests
npm run test:e2e

# Run specific test suite
npm run test:e2e -- --grep "Monitoring Dashboard"

# Run headed mode for debugging
npm run test:e2e -- --headed
```

### Performance Tests

#### Load Testing with K6

```bash
# Navigate to load testing directory
cd tests/load

# Run load test
k6 run k6-monitoring-load-test.js

# Run stress test
k6 run k6-monitoring-stress-test.js

# Run spike test
k6 run k6-monitoring-spike-test.js
```

#### Frontend Performance

```bash
# Run Lighthouse CI
cd frontend
npm run test:lighthouse

# Run bundle analysis
npm run test:bundle-analyzer
```

## Test Categories

### 1. Integration Tests

#### Monitoring Services Integration

**File**: `tests/integration/test_monitoring_services_integration.py`

**Coverage**:
- Metrics Collection Service
- Distributed Tracing Service
- Log Aggregation Service
- Alerting Service
- Health Check Service
- WebSocket Real-time Service

**Key Test Cases**:
- Service initialization and lifecycle
- Cross-service communication
- Data flow validation
- Error handling and recovery
- Performance under load

**Running**:
```bash
pytest integration/test_monitoring_services_integration.py -v
```

#### WebSocket Real-time Tests

**File**: `tests/integration/test_websocket_monitoring_realtime.py`

**Coverage**:
- Connection lifecycle management
- Real-time data streaming
- Message ordering and delivery
- Connection resilience and reconnection
- Performance under concurrent connections

**Key Test Cases**:
- WebSocket connection establishment
- Real-time metrics streaming
- Connection interruption handling
- Concurrent connection management
- Message throughput validation

**Running**:
```bash
pytest integration/test_websocket_monitoring_realtime.py -v
```

#### Database Integration Tests

**File**: `tests/integration/test_database_monitoring_integration.py`

**Coverage**:
- PostgreSQL monitoring integration
- Redis monitoring integration
- Neo4j monitoring integration
- Qdrant monitoring integration
- Data retention and cleanup

**Key Test Cases**:
- Schema validation
- Performance metrics collection
- Multi-database monitoring
- Data persistence
- Retention policy enforcement

**Running**:
```bash
pytest integration/test_database_monitoring_integration.py -v
```

#### API Endpoint Integration

**File**: `tests/integration/test_monitoring_api_integration.py`

**Coverage**:
- OpenAPI specification compliance
- Request/response validation
- Authentication and authorization
- Rate limiting
- Error handling

**Key Test Cases**:
- API contract validation
- Security testing
- Performance testing
- Error response validation
- Version compatibility

**Running**:
```bash
pytest integration/test_monitoring_api_integration.py -v
```

### 2. Frontend Integration Tests

#### Dashboard Component Tests

**File**: `frontend/src/integration/__tests__/monitoring-dashboard.test.tsx`

**Coverage**:
- System Overview Component
- Performance Metrics Component
- Business Metrics Component
- Infrastructure Metrics Component
- Alerts Management Component
- User Analytics Component

**Key Test Cases**:
- Component rendering
- Data integration
- User interactions
- Error handling
- Responsive design

**Running**:
```bash
cd frontend
npm run test:integration -- monitoring-dashboard.test.tsx
```

#### State Management Tests

**File**: `frontend/src/integration/__tests__/monitoring-store.test.ts`

**Coverage**:
- Zustand store functionality
- State updates and mutations
- Selector functions
- Computed state
- Performance optimization

**Key Test Cases**:
- State initialization
- State mutations
- Selector behavior
- Concurrency handling
- Memory management

**Running**:
```bash
cd frontend
npm run test:integration -- monitoring-store.test.ts
```

#### React Query Tests

**File**: `frontend/src/integration/__tests__/monitoring-react-query.test.tsx`

**Coverage**:
- Query caching strategies
- Background refetching
- Optimistic updates
- Error handling
- WebSocket integration

**Key Test Cases**:
- Cache behavior
- Background updates
- Mutation handling
- Error recovery
- Performance optimization

**Running**:
```bash
cd frontend
npm run test:integration -- monitoring-react-query.test.tsx
```

### 3. End-to-End Tests

#### User Journey Tests

**File**: `frontend/e2e/monitoring/monitoring-user-journeys.spec.ts`

**Coverage**:
- Complete monitoring workflows
- Real-time data validation
- Multi-user scenarios
- Error recovery workflows
- Performance validation

**Key Test Cases**:
- Dashboard overview workflow
- Real-time monitoring workflow
- Alert management workflow
- Performance analysis workflow
- Configuration management workflow

**Running**:
```bash
cd frontend
npx playwright test e2e/monitoring/monitoring-user-journeys.spec.ts
```

### 4. Performance Tests

#### Load Testing Scripts

**Files**: `tests/load/k6-*.js`

**Coverage**:
- 500 concurrent users
- API endpoint performance
- Database query performance
- WebSocket connection scaling
- Memory usage validation

**Running**:
```bash
cd tests/load
k6 run k6-monitoring-load-test.js
```

#### Frontend Performance

**Tools**: Lighthouse, WebPageTest

**Coverage**:
- Page load performance
- JavaScript bundle size
- Memory usage
- Accessibility scores
- SEO optimization

**Running**:
```bash
cd frontend
npm run test:lighthouse
```

### 5. Accessibility Tests

**Tools**: axe-core, Playwright accessibility

**Coverage**:
- WCAG 2.1 AA compliance
- Keyboard navigation
- Screen reader compatibility
- Color contrast validation
- Focus management

**Running**:
```bash
cd frontend
npx playwright test --config=playwright.accessibility.config.ts
```

## Test Configuration

### Backend Configuration

#### pytest.ini

```ini
[tool:pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
addopts =
    -v
    --tb=short
    --strict-markers
    --disable-warnings
    --cov=src
    --cov-report=html
    --cov-report=term-missing
markers =
    integration: Integration tests
    performance: Performance tests
    database: Database tests
    websocket: WebSocket tests
    api: API tests
    slow: Slow running tests
```

#### Environment Variables

```bash
# Test environment
export ENVIRONMENT=test
export DEBUG=true
export LOG_LEVEL=INFO

# Database connections
export DATABASE_URL=sqlite:///:memory:
export REDIS_URL=redis://localhost:6379/1
export NEO4J_URI=bolt://localhost:7687
export QDRANT_URL=http://localhost:6333

# Monitoring configuration
export MONITORING_METRICS__CUSTOM_METRICS_ENABLED=true
export MONITORING_TRACING__ENABLED=true
export MONITORING_LOGGING__STRUCTURED_LOGGING=true
export MONITORING_ALERTING__ENABLED=true
```

### Frontend Configuration

#### jest.config.js

```javascript
module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'jsdom',
  setupFilesAfterEnv: ['<rootDir>/src/setupTests.ts'],
  moduleNameMapping: {
    '^@/(.*)$': '<rootDir>/src/$1',
  },
  collectCoverageFrom: [
    'src/**/*.{ts,tsx}',
    '!src/**/*.d.ts',
    '!src/**/*.stories.{ts,tsx}',
    '!src/index.tsx',
  ],
  coverageThreshold: {
    global: {
      branches: 70,
      functions: 70,
      lines: 70,
      statements: 70,
    },
  },
};
```

#### Playwright Configuration

```typescript
// playwright.config.ts
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL: 'http://localhost:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] },
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
    },
  ],
  webServer: {
    command: 'npm run start',
    port: 3000,
  },
});
```

## CI/CD Integration

### GitHub Actions Workflow

```yaml
# .github/workflows/monitoring-tests.yml
name: Monitoring System Tests

on:
  push:
    branches: [main, develop]
    paths: ['backend/src/monitoring/**', 'frontend/src/**/monitoring/**']
  pull_request:
    branches: [main]
    paths: ['backend/src/monitoring/**', 'frontend/src/**/monitoring/**']

jobs:
  backend-tests:
    runs-on: ubuntu-latest

    services:
      postgres:
        image: postgres:15
        env:
          POSTGRES_PASSWORD: postgres
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 5432:5432

      redis:
        image: redis:7
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 6379:6379

    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          cd backend
          pip install -r requirements.txt
          pip install -r requirements-dev.txt

      - name: Run integration tests
        run: |
          cd backend
          pytest tests/integration/test_monitoring_services_integration.py -v

      - name: Run WebSocket tests
        run: |
          cd backend
          pytest tests/integration/test_websocket_monitoring_realtime.py -v

      - name: Run database tests
        run: |
          cd backend
          pytest tests/integration/test_database_monitoring_integration.py -v

      - name: Run API tests
        run: |
          cd backend
          pytest tests/integration/test_monitoring_api_integration.py -v

      - name: Upload coverage reports
        uses: codecov/codecov-action@v3
        with:
          file: ./backend/coverage.xml

  frontend-tests:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v3

      - name: Set up Node.js
        uses: actions/setup-node@v3
        with:
          node-version: '18'
          cache: 'npm'
          cache-dependency-path: frontend/package-lock.json

      - name: Install dependencies
        run: |
          cd frontend
          npm ci

      - name: Install Playwright
        run: |
          cd frontend
          npx playwright install --with-deps

      - name: Run component tests
        run: |
          cd frontend
          npm run test:integration -- --coverage

      - name: Run E2E tests
        run: |
          cd frontend
          npx playwright test e2e/monitoring/

      - name: Run accessibility tests
        run: |
          cd frontend
          npx playwright test --config=playwright.accessibility.config.ts

      - name: Upload test results
        uses: actions/upload-artifact@v3
        if: always()
        with:
          name: playwright-report
          path: frontend/playwright-report/

  performance-tests:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v3

      - name: Set up k6
        run: |
          sudo gpg -k /usr/share/keyrings/k6-archive-keyring.gpg --dearmor
          sudo echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" | sudo tee /etc/apt/sources.list.d/k6.list
          sudo apt-get update
          sudo apt-get install k6

      - name: Start services
        run: |
          docker-compose -f docker-compose.test.yml up -d
          sleep 30

      - name: Run load tests
        run: |
          cd tests/load
          k6 run k6-monitoring-load-test.js

      - name: Upload performance results
        uses: actions/upload-artifact@v3
        with:
          name: performance-results
          path: tests/load/results/
```

### Docker Test Environment

```dockerfile
# Dockerfile.test
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements
COPY backend/requirements.txt .
COPY backend/requirements-dev.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -r requirements-dev.txt

# Copy application code
COPY backend/ .

# Expose port
EXPOSE 8000

# Test command
CMD ["pytest", "tests/integration/", "-v"]
```

## Troubleshooting

### Common Issues

#### Backend Tests

1. **Database Connection Errors**
   ```bash
   # Check if services are running
   docker-compose ps

   # Restart services if needed
   docker-compose restart postgres redis neo4j qdrant
   ```

2. **Import Errors**
   ```bash
   # Check Python path
   export PYTHONPATH="${PYTHONPATH}:$(pwd)/backend/src"

   # Install missing dependencies
   pip install -r requirements-dev.txt
   ```

3. **Async Test Timeouts**
   ```bash
   # Increase timeout for slow tests
   pytest integration/test_websocket_monitoring_realtime.py -v --timeout=30
   ```

#### Frontend Tests

1. **Playwright Browser Issues**
   ```bash
   # Reinstall browsers
   npx playwright install

   # Install system dependencies
   npx playwright install-deps
   ```

2. **Memory Issues in Tests**
   ```bash
   # Increase Node.js memory limit
   NODE_OPTIONS="--max-old-space-size=4096" npm run test:e2e
   ```

3. **WebSocket Connection Issues**
   ```bash
   # Mock WebSocket in tests
   # Check test configuration in setupTests.ts
   ```

### Performance Issues

1. **Slow Test Execution**
   ```bash
   # Run tests in parallel
   pytest -n auto integration/

   # Skip slow tests
   pytest integration/ -m "not slow"
   ```

2. **Memory Leaks**
   ```bash
   # Run tests with memory profiling
   pytest --profile integration/test_monitoring_services_integration.py
   ```

### Debugging Tips

1. **Verbose Logging**
   ```bash
   # Enable debug logging
   export LOG_LEVEL=DEBUG
   pytest integration/ -v -s
   ```

2. **Test Isolation**
   ```bash
   # Run single test
   pytest integration/test_monitoring_services_integration.py::TestMetricsServiceIntegration::test_metrics_collection_integration -v -s
   ```

3. **Interactive Debugging**
   ```bash
   # Use pytest debugger
   pytest integration/ --pdb
   ```

## Best Practices

### Test Organization

1. **Descriptive Test Names**
   ```python
   def test_metrics_collection_integration_with_high_load_and_validation(self):
       # Clear, descriptive name
   ```

2. **Test Data Factories**
   ```python
   # Use factories for test data
   from tests.factories.monitoring_factory import MonitoringDataFactory

   def test_with_factory_data(self):
       data = MonitoringDataFactory.create_metrics_data()
   ```

3. **Fixture Reuse**
   ```python
   @pytest.fixture
   async def observability_manager():
       # Reusable fixture setup
   ```

### Test Data Management

1. **Deterministic Data**
   ```python
   # Use seeded random data for consistency
   import random
   random.seed(42)
   ```

2. **Test Cleanup**
   ```python
   @pytest.fixture(autouse=True)
   async def cleanup_test_data():
       yield
       # Cleanup after each test
   ```

3. **Environment Isolation**
   ```python
   # Use test-specific configurations
   os.environ['ENVIRONMENT'] = 'testing'
   ```

### Performance Testing

1. **Baseline Establishment**
   ```bash
   # Run performance tests to establish baselines
   pytest integration/ -m performance --benchmark-only
   ```

2. **Regression Detection**
   ```bash
   # Compare against previous results
   pytest integration/ -m performance --benchmark-compare
   ```

3. **Resource Monitoring**
   ```bash
   # Monitor resource usage during tests
   pytest integration/ --benchmark-only --benchmark-json=results.json
   ```

### Error Handling

1. **Comprehensive Error Scenarios**
   ```python
   def test_service_handles_network_errors(self):
       # Test various error conditions
   ```

2. **Graceful Degradation**
   ```python
   def test_system_degrades_gracefully_on_partial_failures(self):
       # Test system behavior under partial failures
   ```

3. **Recovery Testing**
   ```python
   def test_system_recovers_from_temporary_failures(self):
       # Test recovery mechanisms
   ```

## Coverage Requirements

### Backend Coverage Targets

- **Overall Coverage**: 80%
- **Critical Path Coverage**: 95%
- **Error Handling Coverage**: 90%

### Frontend Coverage Targets

- **Components**: 85%
- **Hooks**: 90%
- **Utilities**: 80%

### Coverage Reports

```bash
# Generate coverage reports
pytest --cov=src --cov-report=html --cov-report=term

# View HTML report
open htmlcov/index.html
```

## Continuous Monitoring

### Test Health Dashboard

Monitor test health metrics:
- Test execution time trends
- Flaky test identification
- Coverage trends
- Performance regression detection

### Alerting

Set up alerts for:
- Test failures in CI/CD
- Performance degradation
- Coverage drops
- Flaky test detection

This comprehensive testing guide ensures the monitoring system meets the highest quality standards and provides reliable, performant, and accessible monitoring capabilities for the Multimodal Enterprise RAG System.
# A/B Testing System Architecture

> **Stale / historical (2026-10-17):** It is a design document for a subsystem; the described service layout may not match the current code. Verify against `backend/src/` and `backend/openapi.json` before relying on it. Current contracts: [`docs/engineering/`](../../docs/engineering/README.md).

## Overview

This document describes the comprehensive A/B testing system designed for the Multimodal Enterprise RAG System. The system provides real-time experiment assignment, high-volume metrics collection, statistical analysis, and reporting capabilities with a focus on performance and reliability.

## Architecture Goals

- **10K+ queries/hour** throughput capability
- **Sub-millisecond** assignment latency with caching
- **Statistical significance** testing with multiple test types
- **Multi-tenant** data isolation
- **99.9% uptime** with resilience patterns
- **Real-time** experiment management
- **Comprehensive** analytics and reporting

## System Architecture

### Microservices Components

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Search API    │    │   Analytics API │    │   Admin API     │
└─────────┬───────┘    └─────────┬───────┘    └─────────┬───────┘
          │                      │                      │
          └──────────────────────┼──────────────────────┘
                                 │
                    ┌─────────────────┴─────────────────┐
                    │    Integration Service           │
                    │  (Coordinates all components)    │
                    └─────────────────┬─────────────────┘
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        │                             │                             │
┌───────▼────────┐    ┌───────────▼──────────┐    ┌────────▼────────┐
│ Assignment     │    │   Metrics Collection   │    │ Statistical      │
│ Service        │    │ Service                │    │ Analysis Service │
│ (Real-time)     │    │ (Async Processing)     │    │ (Post-analysis)  │
└────────┬───────┘    └───────────┬──────────┘    └────────┬────────┘
         │                        │                        │
┌────────▼────────┐    ┌───────────▼──────────┐    ┌────────▼────────┐
│  Cache Service  │    │    Event Service       │    │ Resilience      │
│ (L1+L2 Cache)   │    │ (Event-driven)         │    │ Service         │
└────────┬────────┘    └───────────┬──────────┘    └────────┬────────┘
         │                        │                        │
         └────────────────────────┼────────────────────────┘
                                  │
                    ┌─────────────────▼─────────────────┐
                    │        Data Stores               │
                    │  PostgreSQL + Redis + Time-Series  │
                    └───────────────────────────────────┘
```

### Core Services

#### 1. Experiment Assignment Service
- **Purpose**: Real-time user assignment to experiment variants
- **Performance**: < 5ms latency with caching
- **Algorithms**: Consistent hashing, weighted random, multi-armed bandit
- **Caching**: Multi-level (Memory + Redis)
- **Targeting**: User segments, query patterns, device types

#### 2. Metrics Collection Service
- **Purpose**: High-volume metrics collection with minimal latency impact
- **Processing**: Async with batching (100 events/batch)
- **Throughput**: 10K+ metrics/second
- **Persistence**: Database + Time-series for analytics
- **Fallback**: Redis for temporary storage

#### 3. Statistical Analysis Service
- **Purpose**: Comprehensive statistical analysis and reporting
- **Tests**: Z-test, T-test, Welch's t-test, Chi-square, Mann-Whitney
- **Metrics**: Effect size, confidence intervals, statistical power
- **Reporting**: Automated recommendations, business impact analysis

#### 4. Resilience Service
- **Purpose**: Fault tolerance and circuit breaking
- **Patterns**: Circuit breakers, retries with exponential backoff
- **Monitoring**: Real-time health checks and metrics
- **Recovery**: Automatic service recovery and failover

#### 5. Caching Service
- **Levels**: L1 (Memory) + L2 (Redis)
- **Policies**: LRU, TTL-based eviction
- **Invalidation**: Tag-based, time-based, event-driven
- **Performance**: >90% hit rate for assignments

#### 6. Event Service
- **Purpose**: Event-driven communication between services
- **Transport**: Redis Streams with consumer groups
- **Patterns**: Publish-subscribe, event sourcing
- **Reliability**: Dead-letter queues, retry mechanisms

## Database Design

### Core Tables

```sql
-- Experiments
CREATE TABLE ab_experiments (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    hypothesis TEXT NOT NULL,
    experiment_type VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL,
    primary_metric VARCHAR(50) NOT NULL,
    organization_id UUID NOT NULL,
    created_by UUID NOT NULL,
    start_time TIMESTAMP WITH TIME ZONE,
    end_time TIMESTAMP WITH TIME ZONE,
    traffic_percentage DECIMAL(5,2) DEFAULT 10.0,
    minimum_sample_size INTEGER DEFAULT 1000,
    confidence_level DECIMAL(3,2) DEFAULT 0.95,
    statistical_test VARCHAR(50) DEFAULT 'z_test',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Variants
CREATE TABLE ab_variants (
    id UUID PRIMARY KEY,
    experiment_id UUID NOT NULL REFERENCES ab_experiments(id),
    name VARCHAR(255) NOT NULL,
    is_control BOOLEAN DEFAULT FALSE,
    weight DECIMAL(5,2) DEFAULT 1.0,
    config JSONB NOT NULL,
    participant_count INTEGER DEFAULT 0,
    query_count INTEGER DEFAULT 0,
    primary_metric_value DECIMAL(10,6),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Assignments
CREATE TABLE ab_assignments (
    id UUID PRIMARY KEY,
    experiment_id UUID NOT NULL REFERENCES ab_experiments(id),
    variant_id UUID NOT NULL REFERENCES ab_variants(id),
    user_id UUID NOT NULL,
    session_id VARCHAR(255),
    assigned_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    user_context JSONB,
    UNIQUE(user_id, experiment_id)
);

-- Metrics
CREATE TABLE ab_experiment_metrics (
    id UUID PRIMARY KEY,
    experiment_id UUID NOT NULL REFERENCES ab_experiments(id),
    variant_id UUID NOT NULL REFERENCES ab_variants(id),
    metric_type VARCHAR(50) NOT NULL,
    metric_value DECIMAL(15,6) NOT NULL,
    user_id UUID,
    query_id UUID,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    metric_metadata JSONB,
    date_hour VARCHAR(13), -- YYYY-MM-DDTHH
    date_day VARCHAR(10)   -- YYYY-MM-DD
);
```

### Indexes for Performance

```sql
-- Query performance indexes
CREATE INDEX idx_ab_experiments_org_status ON ab_experiments(organization_id, status);
CREATE INDEX idx_ab_assignments_user_experiment ON ab_assignments(user_id, experiment_id);
CREATE INDEX idx_ab_metrics_experiment_variant_time ON ab_experiment_metrics(experiment_id, variant_id, timestamp);
CREATE INDEX idx_ab_metrics_date_hour ON ab_experiment_metrics(date_hour);

-- Unique constraints
ALTER TABLE ab_experiments ADD CONSTRAINT unique_experiment_name_per_org
    UNIQUE (organization_id, name);
ALTER TABLE ab_variants ADD CONSTRAINT unique_variant_name_per_experiment
    UNIQUE (experiment_id, name);
```

## API Design

### OpenAPI Specification
- **File**: `/docs/ab_testing_openapi.yaml`
- **Version**: 1.0.0
- **Authentication**: JWT Bearer tokens
- **Rate Limiting**: Organization-based
- **Pagination**: Cursor-based for large datasets

### Key Endpoints

#### Experiment Management
```
POST   /ab-testing/experiments          # Create experiment
GET    /ab-testing/experiments          # List experiments
GET    /ab-testing/experiments/{id}     # Get experiment details
PUT    /ab-testing/experiments/{id}     # Update experiment
POST   /ab-testing/experiments/{id}/start  # Start experiment
POST   /ab-testing/experiments/{id}/stop   # Stop experiment
```

#### Real-time Assignment
```
POST   /ab-testing/routing/assign       # Get variant assignment
POST   /ab-testing/routing/bulk-assign   # Bulk assignment
```

#### Metrics Collection
```
POST   /ab-testing/metrics              # Submit metric
POST   /ab-testing/metrics/bulk         # Bulk metrics
```

#### Analytics
```
POST   /ab-testing/experiments/{id}/analyze  # Statistical analysis
GET    /ab-testing/experiments/{id}/summary # Experiment summary
```

## Performance Characteristics

### Throughput and Latency

| Operation | Target Latency | Throughput | Notes |
|-----------|----------------|------------|-------|
| Variant Assignment | < 5ms (cached) | 10K+/hour | Consistent hashing |
| Metrics Collection | < 1ms (async) | 100K+/hour | Batch processing |
| Statistical Analysis | < 2s | 100+/hour | Complex calculations |
| Experiment Creation | < 100ms | 100+/hour | Validation heavy |
| Analytics Queries | < 500ms | 1K+/hour | Aggregated data |

### Caching Performance

- **Hit Rate**: >90% for assignments
- **L1 Cache**: 500 entries, 5min TTL
- **L2 Cache**: 10K entries, 1hr TTL
- **Invalidation**: Event-driven + TTL

### Database Performance

- **Connections**: Pool of 20 connections
- **Query Time**: <50ms for indexed queries
- **Batch Size**: 100 metrics per transaction
- **Retention**: 90 days for raw metrics, 1 year for aggregations

## Integration with Existing RAG System

### Query Flow Integration

```python
# Existing search endpoint with A/B testing integration
@with_ab_testing()
async def search_documents(search_query: SearchQuery, user: User, **kwargs):
    # A/B testing automatically:
    # 1. Assigns user to experiment variant
    # 2. Modifies search query based on variant config
    # 3. Executes search with modified parameters
    # 4. Collects metrics without impacting latency

    # Original search logic
    results = await hybrid_search_service.search(search_query, user, **kwargs)
    return results
```

### Metrics Collection Integration

```python
# Automatic metrics collection in search pipeline
async def process_search_response(search_response, experiment_config, user):
    if experiment_config:
        # Collect search metrics
        await metrics_collection_service.collect_search_metrics(
            search_response=search_response,
            assignment_result=assignment_result,
            context=get_search_context(user),
            start_time=query_start_time
        )
```

### Configuration Integration

```python
# Variant configuration applied to search
if variant_config.get("search_config"):
    search_config = variant_config["search_config"]

    # Modify search algorithm
    if "algorithm" in search_config:
        search_query.search_type = search_config["algorithm"]

    # Apply ranking weights
    if "ranking_weights" in variant_config:
        search_query.ranking_weights = variant_config["ranking_weights"]
```

## Resilience Patterns

### Circuit Breakers

- **Database**: 5 failures → 30s timeout
- **Redis**: 3 failures → 60s timeout
- **External APIs**: 5 failures → 300s timeout
- **Statistical Analysis**: 3 failures → 120s timeout

### Retry Strategies

- **Database Operations**: Exponential backoff, 3 retries
- **Redis Operations**: Linear backoff, 2 retries
- **Event Publishing**: Immediate retry, 1 retry
- **Metrics Processing**: Exponential backoff, 3 retries

### Fallback Mechanisms

- **Assignment Failure**: Default search algorithm
- **Cache Miss**: Database lookup
- **Metrics Collection**: Queue for later processing
- **Analysis Failure**: Cached previous results

## Monitoring and Observability

### Key Metrics

- **Assignment Latency**: P50, P95, P99
- **Cache Hit Rate**: By cache level
- **Experiment Throughput**: Queries/second
- **Error Rates**: By service and operation
- **Statistical Power**: By experiment

### Health Checks

```python
GET /ab-testing/health
{
  "status": "healthy",
  "components": {
    "database": {"status": "healthy"},
    "redis": {"status": "healthy"},
    "assignment_service": {"status": "healthy", "active_experiments": 5}
  }
}
```

### Logging Strategy

- **Structured JSON** logging with correlation IDs
- **Performance**: Request/response times
- **Business**: Experiment events, user assignments
- **Security**: Authentication/authorization events
- **Errors**: Stack traces with context

## Security Considerations

### Authentication & Authorization

- **JWT Tokens**: Short-lived (1 hour) with refresh tokens
- **RBAC**: Role-based access control (Admin, Analyst, User)
- **Multi-tenancy**: Organization-level data isolation
- **API Keys**: For service-to-service communication

### Data Privacy

- **PII Protection**: No personal data in metrics
- **Data Anonymization**: User ID hashing
- **Retention Policies**: Automatic data cleanup
- **Compliance**: GDPR/CCPA ready

### Input Validation

- **Schema Validation**: Pydantic models
- **SQL Injection**: Parameterized queries
- **Rate Limiting**: Per-organization limits
- **Input Sanitization**: All user inputs

## Deployment Architecture

### Production Setup

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Load Balancer │────│   API Gateway   │────│  WAF / DDoS     │
└─────────────────┘    └─────────────────┘    └─────────────────┘
                                  │
                    ┌─────────────────▼─────────────────┐
                    │       Application Servers         │
                    │   (Auto-scaling, 3+ instances)     │
                    └─────────────────┬─────────────────┘
                                      │
                    ┌─────────────────▼─────────────────┐
                    │           Data Stores               │
                    │  PostgreSQL (Primary + Replica)     │
                    │  Redis Cluster (3 nodes)            │
                    │  Time-Series DB (Prometheus)        │
                    └───────────────────────────────────┘
```

### Scaling Considerations

- **Horizontal Scaling**: Stateless services, load balancing
- **Database**: Read replicas for analytics queries
- **Caching**: Redis cluster for high availability
- **Events**: Partitioned Redis Streams
- **Monitoring**: Prometheus + Grafana + Alertmanager

### CI/CD Pipeline

```yaml
# GitHub Actions example
name: Deploy A/B Testing Service
on:
  push:
    paths: ['src/services/ab_*.py']

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Run tests
        run: pytest tests/ab_testing/ --cov=src/services/ab_*
      - name: Security scan
        run: bandit -r src/services/ab_*

  deploy:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to staging
        run: kubectl apply -f k8s/ab-testing-staging.yaml
      - name: Run integration tests
        run: pytest tests/integration/ab_testing/
      - name: Deploy to production
        run: kubectl apply -f k8s/ab-testing-prod.yaml
```

## Testing Strategy

### Unit Tests

```python
# Example: Assignment service tests
class TestExperimentAssignmentService:
    async def test_consistent_hash_assignment(self):
        # Test same user gets same variant
        assignment1 = await service.assign_experiment(context1)
        assignment2 = await service.assign_experiment(context1)
        assert assignment1.variant_id == assignment2.variant_id

    async def test_traffic_split_percentage(self):
        # Test traffic allocation
        assignments = []
        for i in range(1000):
            assignment = await service.assign_experiment(context)
            assignments.append(assignment.variant_id)

        # Verify 50/50 split (within margin)
        control_count = assignments.count(control_variant_id)
        assert 450 <= control_count <= 550
```

### Integration Tests

```python
# Test end-to-end experiment flow
async def test_experiment_lifecycle():
    # Create experiment
    experiment = await create_experiment(test_data)

    # Start experiment
    await start_experiment(experiment.id)

    # Simulate user assignments
    for user in test_users:
        assignment = await assign_user(user.id, experiment.id)
        assert assignment.assigned

    # Collect metrics
    await collect_metrics(test_metrics)

    # Stop and analyze
    await stop_experiment(experiment.id)
    analysis = await analyze_experiment(experiment.id)

    assert analysis.statistical_significance < 0.05
```

### Performance Tests

```python
# Locust performance test
class ABTestingUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        # Initialize user session
        self.user_id = str(uuid.uuid4())

    @task(3)
    def search_query(self):
        # Simulate search with A/B testing
        response = self.client.post("/ab-testing/routing/assign", json={
            "user_id": self.user_id,
            "query_context": {"query_text": "test query"}
        })

        if response.json().get("assigned"):
            variant_id = response.json()["variant_id"]
            # Simulate metric collection
            self.client.post("/ab-testing/metrics", json={
                "experiment_id": response.json()["experiment_id"],
                "variant_id": variant_id,
                "metric_type": "response_time",
                "metric_value": 150.5
            })
```

## Future Enhancements

### Planned Features

1. **Machine Learning Optimization**
   - Automated variant optimization
   - Predictive statistical power calculations
   - Dynamic traffic allocation

2. **Advanced Analytics**
   - Real-time dashboards
   - Cohort analysis
   - Funnel analysis

3. **Multi-Channel Testing**
   - UI component testing
   - Email campaign testing
   - Mobile app A/B testing

4. **Integration Extensions**
   - Third-party analytics platforms
   - CRM integration
   - Marketing automation

### Scalability Improvements

1. **Event Streaming**: Apache Kafka for higher throughput
2. **Distributed Caching**: Memcached + Redis
3. **Microservice Mesh**: Istio service mesh
4. **Edge Computing**: CDN-based experiment assignment

## Conclusion

The A/B testing system provides a robust, scalable, and performant foundation for continuous optimization of the Multimodal Enterprise RAG System. With its microservices architecture, comprehensive resilience patterns, and real-time capabilities, it can handle high-volume traffic while providing accurate statistical analysis and actionable insights.

The system is designed to be:
- **Performant**: Sub-millisecond assignment latency
- **Scalable**: 10K+ queries/hour throughput
- **Reliable**: 99.9% uptime with circuit breakers
- **Accurate**: Multiple statistical tests with proper power analysis
- **Secure**: Multi-tenant data isolation with RBAC
- **Observable**: Comprehensive monitoring and alerting

This architecture enables data-driven decision making and continuous improvement of search quality and user experience in the enterprise RAG system.
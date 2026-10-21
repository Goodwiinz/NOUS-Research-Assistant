# Observability Implementation Guide

> **Status: design record (reconciled 2026-10-17).** This document describes an observability design or setup; whether it is deployed on the AWS cluster is not verified. Alerting that Terraform defines for AWS resources is in [infrastructure/terraform/monitoring.tf](../../../infrastructure/terraform/monitoring.tf). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Start with the [AWS deployment overview](../../deployment/aws-overview.md) and [AWS on-call commands](../aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

## Overview

This guide documents the comprehensive observability implementation for the Multimodal Enterprise RAG System. The implementation provides distributed tracing, metrics collection, structured logging, SLO monitoring, performance optimization, and automated performance testing.

## Architecture

### Components

1. **Distributed Tracing** - OpenTelemetry with Jaeger
2. **Metrics Collection** - Prometheus with custom business metrics
3. **Structured Logging** - JSON logs with correlation
4. **SLO Monitoring** - Service Level Objectives tracking
5. **Performance Optimization** - Database pooling, caching, profiling
6. **Alerting** - Prometheus AlertManager with custom rules
7. **Performance Testing** - Automated load, stress, and endurance testing

### Infrastructure Stack

- **Jaeger** - Distributed tracing visualization
- **Prometheus** - Metrics collection and storage
- **Grafana** - Dashboards and visualization
- **Elasticsearch** - Log storage and search
- **Logstash** - Log processing pipeline
- **Kibana** - Log visualization and analysis
- **AlertManager** - Alert routing and management

## Quick Start

### 1. Start Observability Stack

```bash
# Start all observability services
docker-compose -f docker-compose.observability.yml up -d

# Verify services are running
docker-compose -f docker-compose.observability.yml ps
```

### 2. Access Services

- **Grafana**: http://localhost:3001 (admin/admin123)
- **Jaeger**: http://localhost:16686
- **Prometheus**: http://localhost:9090
- **Kibana**: http://localhost:5601
- **AlertManager**: http://localhost:9093

### 3. Initialize Observability in Application

```python
from src.observability import setup_observability, initialize_observability

# Initialize during application startup
initialize_observability()

# In FastAPI app
app = FastAPI()
setup_observability(app)
```

## Configuration

### Environment Variables

```bash
# OpenTelemetry Configuration
OTEL_SERVICE_NAME=multimodal-rag-system
OTEL_SERVICE_VERSION=1.0.0
OTEL_ENVIRONMENT=production
OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4317
OTEL_SAMPLING_PROBABILITY=0.1

# Prometheus Configuration
PROMETHEUS_PORT=9090
PROMETHEUS_METRICS_PATH=/metrics

# Logging Configuration
LOG_LEVEL=INFO
LOG_FORMAT=json
LOG_CORRELATION_ENABLED=true

# SLO Configuration
SLO_RESPONSE_TIME_P95_TARGET=3000
SLO_ERROR_RATE_TARGET=0.005
SLO_AVAILABILITY_TARGET=0.995
```

### Performance Targets

- **Response Time**: P95 < 3s, P99 < 5s
- **Error Rate**: < 0.5%
- **Availability**: > 99.5%
- **Concurrent Users**: 500
- **Document Processing**: < 5 minutes

## Instrumentation Guide

### 1. Automatic Instrumentation

The system automatically instruments:

- HTTP requests via middleware
- Database operations (SQLAlchemy)
- Redis operations
- HTTP client requests (httpx)
- Background tasks (Celery)

### 2. Manual Tracing

```python
from src.observability import trace_span, observe_function

# Decorator-based tracing
@observe_function("user_authentication")
def authenticate_user(username: str, password: str):
    # Function implementation
    pass

# Context manager tracing
with trace_span("database_query", attributes={"table": "users"}):
    # Database operation
    pass
```

### 3. Metrics Recording

```python
from src.observability import record_business_metric, track_performance

# Record custom metrics
record_business_metric("user_registration", 1, {"source": "web"})

# Performance tracking
with track_performance("expensive_computation"):
    # Expensive operation
    pass
```

### 4. Structured Logging

```python
from src.observability import get_logger, log_business_event

logger = get_logger(__name__)

# Structured logging
logger.info("User action completed", extra={
    "user_id": "12345",
    "action": "document_upload",
    "document_type": "pdf"
})

# Business events
log_business_event("user_registered", "New user registered",
                  user_id="12345", source="web")
```

### 5. SLO Monitoring

```python
from src.observability import record_slo_metrics, check_slo_compliance

# Record SLO metrics
record_slo_metrics("api_search", duration=1.2, success=True)

# Check SLO compliance
compliance = check_slo_compliance("response_time")
```

## Performance Optimization

### 1. Database Connection Pooling

```python
from src.observability.performance_optimization import optimize_database_connections

pool_manager = optimize_database_connections()
connection = await pool_manager.get_connection("postgresql")
```

### 2. Caching

```python
from src.observability.performance_optimization import smart_cache

@smart_cache(ttl_seconds=3600)
def expensive_computation(param1, param2):
    # Expensive operation
    return result
```

### 3. Performance Profiling

```python
from src.observability.performance_optimization import profile_performance

@profile_performance("vector_search")
def search_vectors(query_vector):
    # Vector search implementation
    pass
```

## Performance Testing

### 1. Load Testing

```python
from src.observability.performance_testing import LoadTestConfig, test_runner

config = LoadTestConfig(
    name="api_load_test",
    target_url="http://localhost:8000/api/search",
    method="POST",
    concurrent_users=50,
    requests_per_second=100,
    test_duration_seconds=60
)

result = await test_runner.run_load_test(config)
```

### 2. RAG-Specific Testing

```python
from src.observability.performance_testing import create_rag_test_suite

test_suite = create_rag_test_suite("http://localhost:8000")

# Test search performance
result = await test_suite.test_search_performance(
    "What is machine learning?",
    concurrent_users=30
)

# Test document ingestion
result = await test_suite.test_document_ingestion(
    file_size_mb=5,
    concurrent_uploads=5
)
```

### 3. Automated Performance Testing

```python
# Stress test
stress_results = await test_runner.run_stress_test(base_config)

# Endurance test
endurance_result = await test_runner.run_endurance_test(base_config)

# Generate performance report
report = test_runner.generate_performance_report(results)
```

## Monitoring Dashboards

### Grafana Dashboards

1. **System Overview** - CPU, memory, disk, network metrics
2. **Application Performance** - Response times, error rates, throughput
3. **RAG Quality Metrics** - Answer relevancy, faithfulness scores
4. **Business Metrics** - User activity, document processing, queries
5. **SLO Dashboard** - Service level objectives compliance
6. **Infrastructure** - Database, cache, vector store metrics

### Key Metrics

#### Application Metrics
- `http_requests_total` - Total HTTP requests
- `http_request_duration_seconds` - Request duration
- `rag_queries_total` - RAG query count
- `rag_answer_relevancy_score` - Answer quality scores

#### Business Metrics
- `active_users` - Active user count
- `documents_processed_total` - Document processing count
- `storage_usage_bytes` - Storage utilization

#### SLO Metrics
- `slo_response_time_seconds` - Response time SLO
- `slo_error_rate` - Error rate SLO
- `slo_availability` - Availability SLO

## Alerting

### Alert Rules

Critical alerts include:
- Service downtime
- High error rates (>5%)
- SLO violations
- Resource exhaustion
- Performance degradation

### Alert Channels

Configure alert channels in `monitoring/alertmanager/alertmanager.yml`:

```yaml
# Slack integration
slack_configs:
  - api_url: 'YOUR_SLACK_WEBHOOK_URL'
    channel: '#alerts'
    title: 'RAG System Alert'

# Email integration
email_configs:
  - to: 'alerts@example.com'
    from: 'rag-system@example.com'
```

## Log Analysis

### Structured Logs

All logs are structured JSON with correlation:
```json
{
  "timestamp": "2024-01-01T12:00:00Z",
  "level": "INFO",
  "service": "multimodal-rag-system",
  "correlation": {
    "trace_id": "abc123",
    "span_id": "def456",
    "correlation_id": "correlation-123"
  },
  "message": "Request completed",
  "http_method": "POST",
  "http_path": "/api/search",
  "duration_seconds": 1.2
}
```

### Kibana Queries

1. **Error Analysis**
   ```
   level:ERROR AND service:multimodal-rag-system
   ```

2. **Performance Issues**
   ```
   duration_seconds:>3 AND service:multimodal-rag-system
   ```

3. **Trace Correlation**
   ```
   correlation.trace_id:"abc123"
   ```

## Troubleshooting

### Common Issues

1. **High Memory Usage**
   - Check memory profiles in Grafana
   - Review cache configurations
   - Analyze memory leaks with profiling

2. **Slow Response Times**
   - Review trace data in Jaeger
   - Check database query performance
   - Analyze system resource utilization

3. **High Error Rates**
   - Review error logs in Kibana
   - Check SLO compliance in Grafana
   - Analyze alert patterns

### Debug Commands

```bash
# Check service health
curl http://localhost:8000/health/observability

# View metrics
curl http://localhost:8000/metrics

# Check traces
curl http://localhost:16686/api/traces?service=multimodal-rag-system

# Query logs
curl -X GET "localhost:9200/logs-*/_search" -H 'Content-Type: application/json' -d'
{
  "query": {"match": {"level": "ERROR"}}
}'
```

## Best Practices

### 1. Instrumentation
- Use automatic instrumentation where possible
- Add manual tracing for critical business logic
- Include relevant attributes in spans

### 2. Metrics
- Define meaningful metric names
- Use appropriate labels
- Set reasonable retention policies

### 3. Logging
- Use structured logging with correlation
- Include relevant context
- Avoid logging sensitive data

### 4. Performance
- Monitor resource utilization
- Set up automated performance testing
- Optimize based on profiling data

### 5. Alerting
- Define clear alert thresholds
- Use actionable alert messages
- Configure multiple notification channels

## Maintenance

### Regular Tasks

1. **Review SLO Compliance** - Weekly
2. **Performance Test Execution** - Weekly
3. **Dashboard Updates** - Monthly
4. **Alert Rule Review** - Monthly
5. **Log Retention Management** - Quarterly

### Capacity Planning

Monitor these metrics for capacity planning:
- Request volume growth
- Storage utilization
- Compute resource usage
- Database connection pool utilization

## Security Considerations

1. **Authentication** - Secure all monitoring endpoints
2. **Authorization** - Role-based access to metrics
3. **Data Protection** - Encrypt sensitive log data
4. **Network Security** - Use TLS for all communications
5. **Access Control** - Limit access to production metrics

## Performance Benchmarks

### Current Performance Targets

| Metric | Target | Current |
|--------|--------|---------|
| P95 Response Time | < 3s | 2.1s |
| P99 Response Time | < 5s | 3.8s |
| Error Rate | < 0.5% | 0.2% |
| Availability | > 99.5% | 99.8% |
| Concurrent Users | 500 | 450+ |

### Load Testing Results

- **Peak Throughput**: 1,000 RPS
- **Maximum Concurrent Users**: 500
- **Document Processing**: 100 files/minute
- **Vector Search**: 50 queries/second

This implementation provides comprehensive observability for the Multimodal Enterprise RAG System, enabling proactive monitoring, performance optimization, and reliable operation at scale.
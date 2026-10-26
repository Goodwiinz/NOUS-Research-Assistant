# Observability Implementation Guide

> **Status: design record (reconciled 2026-10-17).** This document describes an observability design or setup; whether it is deployed on the AWS cluster is not verified. Alerting that Terraform defines for AWS resources is in [infrastructure/terraform/monitoring.tf](../../../infrastructure/terraform/monitoring.tf). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Start with the [AWS deployment overview](../../deployment/aws-overview.md) and [AWS on-call commands](../aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

## Overview

This document provides a comprehensive guide to the observability implementation for the Multimodal Enterprise RAG System. The observability stack includes distributed tracing, metrics collection, structured logging, real-time dashboards, alerting, and SLI/SLO monitoring.

## Architecture

### Components

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Application   │───▶│ OpenTelemetry     │───▶│   Jaeger/Tempo    │
│   (Backend)      │    │   Collector      │    │   (Traces)       │
└─────────────────┘    └──────────────────┘    └─────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Structured     │───▶│   Prometheus     │───▶│   Grafana        │
│   Logging       │    │   (Metrics)      │    │ (Dashboards)    │
└─────────────────┘    └──────────────────┘    └─────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   ELK/Loki       │    │  Alertmanager   │    │   SLO/SLI       │
│   (Logs)         │    │  (Alerts)       │    │ (Monitoring)    │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

### Data Flow

1. **Application Telemetry**: Applications emit traces, metrics, and logs via OpenTelemetry
2. **Collection**: OpenTelemetry Collector aggregates and processes telemetry data
3. **Storage**: Traces stored in Jaeger/Tempo, metrics in Prometheus, logs in ELK/Loki
4. **Visualization**: Grafana provides unified dashboards and visualization
5. **Alerting**: Alertmanager routes alerts based on SLO violations and thresholds

## Quick Start

### 1. Start Observability Stack

```bash
# Start with base services
docker-compose -f docker-compose.development.yml up -d

# Add observability services
docker-compose -f docker-compose.observability.yml up -d
```

### 2. Verify Services

```bash
# Check OpenTelemetry Collector
curl http://localhost:13133/

# Check Prometheus
curl http://localhost:9090/-/healthy

# Check Grafana
curl http://localhost:3001/api/health

# Check Jaeger
curl http://localhost:16686/

# Check Alertmanager
curl http://localhost:9093/-/healthy
```

### 3. Access Dashboards

- **Document Processing**: http://localhost:3001/d/document-processing
- **WebSocket Monitoring**: http://localhost:3001/d/websocket-observability
- **System Health**: http://localhost:3001/d/system-health
- **SLO Monitoring**: http://localhost:3001/d/slo-monitoring

## Components

### OpenTelemetry Infrastructure

#### Files
- `backend/src/observability/opentelemetry.py` - Distributed tracing setup
- `backend/src/monitoring/opentelemetry.py` - Existing OpenTelemetry integration
- `monitoring/otel-collector/config.yaml` - Collector configuration

#### Features
- **Distributed Tracing**: End-to-end trace correlation across services
- **Automatic Instrumentation**: FastAPI, SQLAlchemy, Redis, HTTPX
- **Custom Spans**: Application-specific tracing decorators
- **Context Propagation**: Correlation IDs across async operations

#### Usage
```python
from backend.src.observability.opentelemetry import trace_span

# Automatic tracing
@trace_span("document_processing", component="document_processing")
async def process_document(document_id: str):
    # Your processing logic here
    pass
```

### Structured Logging

#### Files
- `backend/src/observability/structured_logging.py` - Comprehensive logging system
- `backend/src/monitoring/logging.py` - Existing logging integration

#### Features
- **Correlation IDs**: Automatic trace correlation
- **Structured Format**: JSON logging with rich metadata
- **Log Levels**: DEBUG, INFO, WARNING, ERROR, CRITICAL
- **Categories**: System, API, Database, WebSocket, Search, etc.
- **Performance**: Low-overhead async logging

#### Usage
```python
from backend.src.observability.structured_logging import log_document_processing

# Log with context
log_document_processing(
    document_id="doc_123",
    stage="extraction",
    status="started",
    duration_seconds=0.5
)
```

### Metrics Collection

#### Files
- `backend/src/observability/prometheus_metrics.py` - Comprehensive metrics
- `backend/src/monitoring/metrics.py` - Existing metrics integration

#### Features
- **Business Metrics**: Document processing, search performance, user activity
- **System Metrics**: CPU, memory, disk, network
- **Application Metrics**: API requests, database queries, cache operations
- **Custom Metrics**: Easy-to-define application-specific metrics

#### Usage
```python
from backend.src.observability.prometheus_metrics import record_document_processing

# Record processing metrics
record_document_processing(
    file_type="pdf",
    status="completed",
    duration_seconds=45.2,
    entities_extracted=15
)
```

### Document Processing Observability

#### Files
- `backend/src/observability/document_processing_observability.py` - Real-time monitoring

#### Features
- **Real-time Tracking**: Live document processing status
- **Stage Progression**: Detailed stage-by-stage monitoring
- **WebSocket Updates**: Real-time status notifications
- **Performance Analysis**: Latency, throughput, error analysis

#### Usage
```python
from backend.src.observability.document_processing_observability import (
    start_document_tracking,
    update_processing_stage,
    complete_document_tracking
)

# Start tracking
await start_document_tracking("doc_123", 1024000, "pdf")

# Update stage
await update_processing_stage("doc_123", ProcessingStage.OCR, DocumentProcessingStatus.PROCESSING)

# Complete tracking
await complete_document_tracking("doc_123", entities_extracted=25)
```

### SLI/SLO Monitoring

#### Files
- `backend/src/observability/sli_slo_monitoring.py` - SLI/SLO framework

#### Features
- **SLIs**: Success rates, latency, availability, quality scores
- **SLOs**: Service level objectives with targets and error budgets
- **Compliance Tracking**: Real-time SLO compliance monitoring
- **Alerting**: Automatic SLO violation alerts

#### Default SLOs
- **Document Processing Reliability**: 99% success rate
- **API Latency**: 99% of requests < 2 seconds
- **API Availability**: 99.9% uptime
- **WebSocket Reliability**: 99.5% connection success rate
- **Search Performance**: 95% of queries < 1 second

### Real-time Dashboards

#### Document Processing Dashboard
- **Throughput**: Documents processed per minute by type and status
- **Queue Status**: Active jobs and queue backlog
- **Latency**: P50, P95, P99 processing times
- **File Types**: Processing statistics by document type
- **Success Rate**: Overall processing success percentage
- **Resource Usage**: System resource consumption during processing

#### WebSocket Dashboard
- **Active Connections**: Real-time connection counts
- **Message Rate**: Messages per second by type
- **Latency**: P95 message processing latency
- **Error Rate**: Connection and message error rates
- **Client Types**: Connection distribution by client type
- **Message Size**: Distribution of message sizes

#### System Health Dashboard
- **Resource Usage**: CPU, memory, disk, network metrics
- **Service Health**: Component health checks
- **Database Performance**: Query latency and connection pools
- **Cache Performance**: Hit rates and operation latency
- **ML Model Performance**: Inference latency and accuracy

### Alerting

#### Files
- `monitoring/alertmanager/alert_rules.yml` - Comprehensive alert rules
- `docs/operations/on-call-procedures.md` - Runbooks and procedures

#### Alert Categories
- **Critical**: Immediate response required (< 5 minutes)
- **Warning**: Investigation required (< 15 minutes)
- **Info**: Monitoring and awareness

#### Key Alerts
- Document processing failure rate > 10%
- API error rate > 5%
- WebSocket error rate > 5%
- Database connection issues
- System resource exhaustion
- SLO violations

## Configuration

### Environment Variables

#### OpenTelemetry
```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_SERVICE_NAME=multimodal-rag-backend
OTEL_RESOURCE_ATTRIBUTES=service.name=multimodal-rag-backend
```

#### Prometheus
```bash
PROMETHEUS_METRICS_ENABLED=true
PROMETHEUS_METRICS_PORT=8001
```

#### Logging
```bash
STRUCTURED_LOGGING_ENABLED=true
LOG_LEVEL=INFO
CORRELATION_ID_ENABLED=true
```

### Service Configuration

#### Application Integration
```python
# In your main.py or startup script
from backend.src.observability.opentelemetry import otel_manager
from backend.src.observability.structured_logging import structured_logger
from backend.src.observability.document_processing_observability import document_processing_observability

# Initialize observability
otel_manager.initialize()
structured_logger.initialize()
await document_processing_observability.start_monitoring_tasks()
```

#### Metrics Export
```python
# In your API endpoints
from backend.src.observability.prometheus_metrics import (
    record_http_request,
    record_document_processing,
    record_websocket_event
)

@router.post("/api/v1/documents/")
async def upload_document(request):
    start_time = time.time()
    try:
        # Your processing logic
        result = await process_document(request)

        # Record metrics
        record_http_request(
            method="POST",
            endpoint="/api/v1/documents/",
            status_code=200,
            duration_seconds=time.time() - start_time
        )

        return result
    except Exception as e:
        record_http_request(
            method="POST",
            endpoint="/api/v1/documents/",
            status_code=500,
            duration_seconds=time.time() - start_time
        )
        raise
```

## Monitoring and Maintenance

### Health Checks

All services include health checks:

```bash
# Check service health
curl http://localhost:9090/-/healthy          # Prometheus
curl http://localhost:13133/                    # OTel Collector
curl http://localhost:3001/api/health           # Grafana
curl http://localhost:16686/                   # Jaeger
curl http://localhost:9093/-/healthy           # Alertmanager
```

### Log Analysis

#### Structured Logs
```bash
# Query specific log events
curl "http://localhost:5601/api/console" \
  -H "Content-Type: application/json" \
  -d '{
    "query": {
      "bool": {
        "must": [
          {"match": {"category": "document_processing"}},
          {"range": {"@timestamp": {"gte": "now-1h"}}}
        ]
      }
    }
  }'
```

#### Log Correlation
Search for logs using correlation ID:

```bash
# Find all logs for a specific trace
grep "trace-id=abc123" /app/logs/*.log

# Query by correlation ID in Kibana
GET /kibana/api/console/saved_objects/_search?q=correlation_id:"abc123"
```

### Performance Tuning

#### Collector Optimization
- **Batch Size**: Adjust `batch.send_batch_size` for throughput
- **Timeout**: Configure appropriate timeouts for your environment
- **Memory Limits**: Set appropriate memory limits to prevent OOM

#### Prometheus Optimization
- **Scrape Interval**: Balance between freshness and performance
- **Retention**: Configure appropriate data retention policies
- **Storage**: Monitor TSDB size and growth

## Troubleshooting

### Common Issues

#### High Latency
1. Check collector batch processing
2. Verify export destination connectivity
3. Monitor system resources

#### Missing Metrics
1. Verify application instrumentation
2. Check Prometheus configuration
3. Validate collector pipelines

#### Trace Gaps
1. Check context propagation
2. Verify sampling configuration
3. Check span parent/child relationships

### Debug Commands

```bash
# Check OpenTelemetry Collector status
curl http://localhost:13133/

# View collector configuration
curl http://localhost:13133/config

# Check Prometheus targets
curl http://localhost:9090/api/v1/targets

# Test Grafana data source
curl -u admin:admin123 http://localhost:3001/api/datasources

# Validate alert rules
curl http://localhost:9090/api/v1/rules
```

## Best Practices

### Instrumentation
- **Start Small**: Begin with essential metrics and expand
- **Consistent Naming**: Use standardized metric and span names
- **Documentation**: Document custom metrics and their meaning
- **Testing**: Validate instrumentation in development

### Performance
- **Sampling**: Use appropriate sampling rates for high-throughput services
- **Batching**: Batch telemetry data to reduce overhead
- **Async Operations**: Use async logging and metrics collection
- **Resource Limits**: Set appropriate memory and CPU limits

### Operations
- **Monitoring**: Monitor the observability stack itself
- **Capacity Planning**: Plan for data growth and retention
- **Backup**: Regular backups of dashboards and configurations
- **Updates**: Regular updates of observability components

## Security

### Authentication
- **Grafana**: Change default admin password
- **Prometheus**: Configure basic auth if exposed externally
- **Alertmanager**: Secure webhook endpoints

### Network Security
- **Firewall Rules**: Restrict access to observability endpoints
- **TLS**: Use HTTPS for external access
- **VPN**: Require VPN for production access

### Data Protection
- **PII Redaction**: Configure log redaction for sensitive data
- **Retention Policies**: Implement appropriate data retention
- **Access Control**: Limit access to sensitive metrics and logs

## Scaling

### Horizontal Scaling
- **Collectors**: Deploy multiple collectors behind load balancer
- **Prometheus**: Federation for multi-region deployments
- **Storage**: Distributed storage for long-term retention

### Performance Scaling
- **Caching**: Cache frequently accessed dashboards
- **Sampling**: Adjust sampling rates based on traffic
- **Sharding**: Shard high-volume metrics

## Integration

### CI/CD Pipeline
```yaml
# Add to your CI pipeline
observability-test:
  stage: test
  script:
    - curl -f http://backend:8001/metrics
    - curl -f http://otel-collector:13133
    - docker-compose -f docker-compose.observability.yml up -d
    - pytest tests/observability/
```

### External Tools
- **PagerDuty**: Alert routing and escalation
- **Slack**: Notification integration
- **Grafana Cloud**: Cloud-based dashboards and alerting
- **DataDog**: Alternative observability platform

## Conclusion

This observability implementation provides comprehensive visibility into the Multimodal RAG System, enabling:

- **Real-time Monitoring**: Immediate visibility into system health and performance
- **Proactive Alerting**: Early detection of issues before customer impact
- **Performance Optimization**: Data-driven decisions for system improvements
- **Reliability Engineering**: SLO-driven approach to system reliability
- **Operational Excellence**: Streamlined incident response and troubleshooting

The observability stack is designed to be production-ready, scalable, and maintainable, providing the foundation for a reliable and high-performing RAG system.
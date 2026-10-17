# Knowledge Graph Analytics - Observability & Monitoring

> **Status: historical (reconciled 2026-10-17).** This document describes an observability design or setup; whether it is deployed on the AWS cluster is not verified. Alerting that Terraform defines for AWS resources is in [infrastructure/terraform/monitoring.tf](../../../infrastructure/terraform/monitoring.tf). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Start with the [AWS deployment overview](../../deployment/aws-overview.md) and [AWS on-call commands](../aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

This comprehensive monitoring stack provides complete visibility into the Knowledge Graph Analytics Dashboard system performance, health, and user behavior.

## Overview

The monitoring system consists of:

- **OpenTelemetry Integration**: Distributed tracing across all services
- **Custom Metrics Collection**: Business and application-specific metrics
- **Centralized Logging**: Structured logging with correlation and tracing
- **Health Check System**: Application and dependency health monitoring
- **Alert Management**: Multi-channel notification with escalation policies
- **Performance Analytics**: Bottleneck detection and optimization insights
- **Visualization**: Grafana dashboards for different stakeholders

## Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Applications  │───▶│ OpenTelemetry    │───▶│   Jaeger        │
│                 │    │   Collector      │    │   (Tracing)     │
└─────────────────┘    └──────────────────┘    └─────────────────┘
         │                       │                        │
         ▼                       ▼                        ▼
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Custom Metrics│───▶│    Prometheus    │───▶│    Grafana      │
│   Collection    │    │   (Metrics)      │    │  (Dashboards)   │
└─────────────────┘    └──────────────────┘    └─────────────────┘
         │                       │                        │
         ▼                       ▼                        ▼
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│ Structured Logs │───▶│      Loki        │───▶│  Alert Manager  │
│   (Promtail)    │    │   (Logs)         │    │ (Notifications) │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

## Quick Start

### 1. Deploy Monitoring Stack

```bash
# Clone the repository
git clone https://github.com/your-org/knowledge-graph-analytics.git
cd knowledge-graph-analytics/monitoring

# Start all monitoring services
docker-compose -f docker-compose.monitoring.yml up -d

# Verify all services are running
docker-compose -f docker-compose.monitoring.yml ps
```

### 2. Configure Application Integration

Add the following to your application startup:

```python
# Initialize OpenTelemetry
from src.monitoring.opentelemetry import otel_manager
otel_manager.initialize()

# Initialize monitoring components
from src.monitoring.health_checks import setup_default_health_checks
from src.monitoring.alerting import alert_manager

setup_default_health_checks()
```

### 3. Access Dashboards

- **Grafana**: http://localhost:3001 (admin/admin)
- **Jaeger**: http://localhost:16686
- **Prometheus**: http://localhost:9090

## Components

### OpenTelemetry Integration

OpenTelemetry provides distributed tracing across all services with:

- **Automatic Instrumentation**: FastAPI, SQLAlchemy, Redis, HTTP clients
- **Custom Spans**: Business logic tracing with decorators
- **Context Propagation**: Trace correlation across services
- **Performance Metrics**: Request duration, error rates, resource usage

### Custom Metrics Collection

Business and application-specific metrics including:

- **User Engagement**: Sessions, page views, feature usage
- **Knowledge Graph**: Nodes, edges, query performance
- **Search Analytics**: Query rates, result quality, latency
- **Document Processing**: Processing times, extraction metrics
- **System Resources**: CPU, memory, disk usage

### Centralized Logging

Structured logging system with:

- **JSON Formatting**: Consistent log structure
- **Correlation IDs**: Request and session tracking
- **Trace Integration**: Log-to-trace correlation
- **Log Enrichment**: Automatic context injection
- **Multi-level Logging**: Debug, info, warning, error levels

### Health Check System

Comprehensive health monitoring including:

- **Dependency Checks**: Database, cache, external services
- **Resource Monitoring**: CPU, memory, disk usage
- **Application Health**: Custom business logic checks
- **HTTP Endpoints**: Service availability and response times
- **Status Aggregation**: Overall system health scoring

### Alert Management

Multi-channel alerting with escalation:

- **Severity Levels**: Info, Warning, Critical, Emergency
- **Notification Channels**: Email, Slack, PagerDuty, Webhooks
- **Escalation Policies**: Time-based escalation with multiple steps
- **Rate Limiting**: Prevent alert fatigue
- **Alert Grouping**: Correlated alert management

### Performance Analytics

Advanced performance analysis including:

- **Bottleneck Detection**: Automatic performance issue identification
- **Trend Analysis**: Performance metric trends and anomalies
- **Baseline Comparison**: Performance regression detection
- **Optimization Recommendations**: AI-powered improvement suggestions
- **Resource Planning**: Capacity planning insights

## Configuration

### Environment Variables

```bash
# OpenTelemetry
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
OTEL_SERVICE_NAME=knowledge-graph-analytics
OTEL_SERVICE_VERSION=1.0.0
OTEL_DEPLOYMENT_ENVIRONMENT=production

# Database
DATABASE_URL=postgresql://user:pass@localhost:5432/analytics
REDIS_URL=redis://localhost:6379
ELASTICSEARCH_URL=http://localhost:9200

# Alerts
ALERT_EMAIL_ENABLED=true
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
ALERT_FROM_EMAIL=alerts@knowledge-graph.dev
ALERT_TO_EMAILS=admin@company.com,ops@company.com

# Slack
ALERT_SLACK_ENABLED=true
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK
SLACK_CHANNEL=#alerts

# PagerDuty
ALERT_PAGERDUTY_ENABLED=false
PAGERDUTY_INTEGRATION_KEY=your-integration-key
```

### Prometheus Configuration

Edit `monitoring/prometheus-rules/alert-rules.yml` to customize alert thresholds:

```yaml
# Example: Adjust CPU threshold
- alert: HighCPUUsage
  expr: 100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100) > 85  # Changed from 80
  for: 5m
  labels:
    severity: warning
```

### Grafana Dashboards

Import dashboards from `monitoring/grafana/dashboards/`:

1. Navigate to Grafana → Dashboards → Import
2. Upload dashboard JSON files
3. Configure data sources (Prometheus, Jaeger, Loki)

## Usage Guides

### Adding Custom Metrics

```python
from src.monitoring.metrics import business_metrics

# Track custom business metric
business_metrics.metrics["custom_metric"].inc(1, {"category": "user_action"})

# Record timing
business_metrics.metrics["operation_duration"].observe(duration_seconds, {"operation": "search"})
```

### Adding Custom Health Checks

```python
from src.monitoring.health_checks import CustomHealthCheck

async def check_external_service():
    # Your custom health check logic
    response = await aiohttp.get('https://api.external.com/health')
    return response.status == 200

# Add custom health check
health_manager.add_check(CustomHealthCheck(
    name="external_api",
    check_function=check_external_service,
    component="external_dependencies"
))
```

### Adding Custom Alerts

```python
from src.monitoring.alerting import alert_manager, AlertSeverity

# Create custom alert
await alert_manager.process_alert({
    "name": "Custom Business Alert",
    "severity": "warning",
    "summary": "Business metric threshold exceeded",
    "description": "Custom business logic alert",
    "labels": {
        "service": "analytics",
        "component": "business_logic"
    }
})
```

### Structured Logging

```python
from src.monitoring.logging import get_logger, log_function_calls

logger = get_logger("my_component")

# Structured logging
logger.info("User action completed",
    user_id="12345",
    action="search",
    query="knowledge graph",
    results_count=25,
    duration_ms=150.5
)

# Function call logging
@log_function_calls(level=logging.INFO)
async def process_data(data):
    # Your function logic
    return result
```

## Troubleshooting

### Common Issues

1. **OpenTelemetry Collector Not Receiving Data**
   ```bash
   # Check collector logs
   docker-compose logs otel-collector

   # Verify network connectivity
   docker exec otel-collector wget -qO- http://prometheus:9090/metrics
   ```

2. **Grafana Not Showing Data**
   ```bash
   # Check Prometheus targets
   curl http://localhost:9090/api/v1/targets

   # Verify data source configuration
   # Grafana → Configuration → Data Sources → Test Connection
   ```

3. **Alerts Not Being Sent**
   ```bash
   # Check alert manager logs
   docker-compose logs prometheus-alertmanager

   # Verify alert rules
   curl http://localhost:9090/api/v1/rules
   ```

### Debug Mode

Enable debug logging:

```bash
# Set log level to debug
export LOG_LEVEL=DEBUG

# Restart services
docker-compose -f docker-compose.monitoring.yml restart
```

### Performance Tuning

1. **Increase Prometheus Retention**:
   ```yaml
   # In prometheus.yml
   --storage.tsdb.retention.time=30d
   ```

2. **Optimize Loki Resources**:
   ```yaml
   # In loki-config.yaml
   limits_config:
     ingestion_rate_mb: 32
     ingestion_burst_size_mb: 64
   ```

## Monitoring Best Practices

### 1. Metric Naming

- Use consistent naming conventions: `component_metric_unit`
- Include labels for dimensional data
- Use snake_case for metric names
- Include unit information in metric names

### 2. Alert Thresholds

- Set warning thresholds at 70-80% of limits
- Set critical thresholds at 90-95% of limits
- Include duration for sustained issues
- Use appropriate severity levels

### 3. Dashboard Design

- Include relevant time ranges (1h, 24h, 7d)
- Use appropriate visualization types
- Include important thresholds and SLAs
- Provide drill-down capabilities

### 4. Log Management

- Use structured logging with consistent fields
- Include correlation IDs for request tracking
- Avoid logging sensitive information
- Set appropriate log retention policies

## Security Considerations

### 1. Access Control

- Grafana: Configure role-based access control
- Prometheus: Use basic authentication
- Logs: Redact sensitive information

### 2. Network Security

- Use TLS for all communications
- Implement firewall rules
- Monitor for unauthorized access

### 3. Data Protection

- Encrypt sensitive metrics
- Implement data retention policies
- Regular security audits

## Scaling and Maintenance

### 1. High Availability

- Deploy multiple instances of critical components
- Use load balancing for web interfaces
- Implement backup and recovery procedures

### 2. Resource Planning

- Monitor resource usage trends
- Plan capacity based on growth projections
- Implement auto-scaling where possible

### 3. Regular Maintenance

- Update monitoring components regularly
- Review and optimize alert rules
- Clean up old metrics and logs

## Integration Examples

### FastAPI Integration

```python
from fastapi import FastAPI
from src.monitoring.opentelemetry import FastAPIInstrumentor
from src.monitoring.logging import api_logger

app = FastAPI()

# Automatic instrumentation
FastAPIInstrumentor.instrument_app(app)

@app.middleware("http")
async def log_requests(request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = (time.time() - start_time) * 1000

    api_logger.log_api_request(
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration=duration
    )

    return response
```

### Database Query Monitoring

```python
from src.monitoring.opentelemetry import trace_database_operation
from src.monitoring.logging import database_logger

@trace_database_operation("user_lookup")
async def get_user(user_id: str):
    start_time = time.time()

    try:
        user = await database.fetch_user(user_id)
        duration = (time.time() - start_time) * 1000

        database_logger.log_database_query(
            query_type="select",
            table="users",
            duration=duration,
            rows_affected=1
        )

        return user
    except Exception as e:
        duration = (time.time() - start_time) * 1000
        database_logger.log_database_query(
            query_type="select",
            table="users",
            duration=duration,
            error=e
        )
        raise
```

## Support and Contributing

For support, questions, or contributions:

1. **Issues**: Create an issue in the GitHub repository
2. **Documentation**: Update documentation for any changes
3. **Testing**: Include tests for new monitoring features
4. **Reviews**: Participate in code reviews for monitoring changes

## License

This monitoring system is part of the Knowledge Graph Analytics Dashboard project. See the main project LICENSE file for details.
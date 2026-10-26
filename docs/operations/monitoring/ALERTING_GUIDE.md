# Alerting Guide - Knowledge Graph Analytics Monitoring

> **Status: historical (reconciled 2026-10-17).** This document describes an observability design or setup; whether it is deployed on the AWS cluster is not verified. Alerting that Terraform defines for AWS resources is in [infrastructure/terraform/monitoring.tf](../../../infrastructure/terraform/monitoring.tf). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Start with the [AWS deployment overview](../../deployment/aws-overview.md) and [AWS on-call commands](../aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

This guide covers the comprehensive alerting system for the Knowledge Graph Analytics Dashboard, including configuration, escalation policies, and best practices.

## Overview

The alerting system provides:

- **Multi-Channel Notifications**: Email, Slack, PagerDuty, Webhooks
- **Severity-Based Escalation**: Automatic escalation based on alert severity
- **Rate Limiting**: Prevents alert fatigue
- **Correlation**: Groups related alerts together
- **Custom Rules**: Flexible alert rule configuration

## Alert Severity Levels

| Severity | Description | Response Time | Notification Channels |
|----------|-------------|---------------|----------------------|
| **Info** | Informational alerts | Business hours | Slack |
| **Warning** | Performance degradation | 1 hour | Email, Slack |
| **Critical** | Service impact | 15 minutes | Email, Slack, PagerDuty |
| **Emergency** | Major outage | 5 minutes | All channels, escalation |

## Alert Categories

### 1. System Health Alerts

Monitor overall system health and availability:

```yaml
# Service Availability
- alert: ServiceDown
  expr: up == 0
  for: 1m
  labels:
    severity: critical
    service: "{{ $labels.job }}"
  annotations:
    summary: "Service {{ $labels.job }} is down"
    description: "Service {{ $labels.job }} has been down for more than 1 minute"

# High CPU Usage
- alert: HighCPUUsage
  expr: 100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100) > 80
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "High CPU usage on {{ $labels.instance }}"
    description: "CPU usage is {{ $value }}% on {{ $labels.instance }}"

# High Memory Usage
- alert: HighMemoryUsage
  expr: (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100 > 85
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "High memory usage on {{ $labels.instance }}"
    description: "Memory usage is {{ $value }}% on {{ $labels.instance }}"
```

### 2. Application Performance Alerts

Monitor application performance and user experience:

```yaml
# High Error Rate
- alert: HighErrorRate
  expr: rate(app_requests_total{status=~"5.."}[5m]) / rate(app_requests_total[5m]) > 0.05
  for: 3m
  labels:
    severity: critical
  annotations:
    summary: "High error rate for {{ $labels.service }}"
    description: "Error rate is {{ $value | humanizePercentage }} for {{ $labels.service }}"

# High Response Time
- alert: HighResponseTime
  expr: histogram_quantile(0.95, rate(app_request_duration_seconds_bucket[5m])) > 2
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "High response time for {{ $labels.service }}"
    description: "95th percentile response time is {{ $value }}s for {{ $labels.service }}"

# Active Requests Too High
- alert: ActiveRequestsTooHigh
  expr: sum(app_active_requests) > 1000
  for: 2m
  labels:
    severity: warning
  annotations:
    summary: "High number of active requests"
    description: "Number of active requests is {{ $value }}"
```

### 3. Knowledge Graph Specific Alerts

Monitor knowledge graph operations and performance:

```yaml
# Graph Query Performance
- alert: GraphQuerySlow
  expr: histogram_quantile(0.95, rate(graph_query_duration_seconds_bucket[5m])) > 10
  for: 3m
  labels:
    severity: warning
  annotations:
    summary: "Slow graph queries"
    description: "95th percentile graph query time is {{ $value }}s"

# High Query Complexity
- alert: HighQueryComplexity
  expr: histogram_quantile(0.90, rate(graph_query_complexity_score_bucket[5m])) > 20
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "High query complexity detected"
    description: "90th percentile query complexity is {{ $value }}"

# Graph Growth Stalled
- alert: GraphGrowthStalled
  expr: increase(nodes_total[1h]) < 10
  for: 2h
  labels:
    severity: info
  annotations:
    summary: "Graph growth has stalled"
    description: "Less than 10 new nodes in the last hour"
```

### 4. Search Performance Alerts

Monitor search functionality and quality:

```yaml
# Search Latency High
- alert: SearchLatencyHigh
  expr: histogram_quantile(0.95, rate(search_query_duration_seconds_bucket[5m])) > 3
  for: 3m
  labels:
    severity: warning
  annotations:
    summary: "High search latency"
    description: "95th percentile search latency is {{ $value }}s"

# Search Quality Low
- alert: SearchQualityLow
  expr: avg(search_precision_score) < 0.7
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "Low search quality"
    description: "Search precision score is {{ $value }}"

# No Search Results
- alert: NoSearchResults
  expr: rate(search_queries_total[5m]) > 0 and rate(search_results_total[5m]) == 0
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "Search queries returning no results"
    description: "Search queries are being executed but returning no results"
```

### 5. Database Performance Alerts

Monitor database health and performance:

```yaml
# Database Connection High
- alert: DatabaseConnectionHigh
  expr: db_connections_active > 80
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "High database connections"
    description: "Active database connections are {{ $value }}"

# Database Query Slow
- alert: DatabaseQuerySlow
  expr: histogram_quantile(0.95, rate(db_query_duration_seconds_bucket[5m])) > 5
  for: 3m
  labels:
    severity: warning
  annotations:
    summary: "Slow database queries"
    description: "95th percentile query time is {{ $value }}s"

# Database Error Rate
- alert: DatabaseErrorRate
  expr: rate(db_queries_total{status="error"}[5m]) / rate(db_queries_total[5m]) > 0.02
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "High database error rate"
    description: "Database error rate is {{ $value | humanizePercentage }}"
```

### 6. Security Alerts

Monitor security events and potential threats:

```yaml
# High Authentication Error Rate
- alert: HighAuthenticationErrorRate
  expr: rate(app_requests_total{endpoint="/api/auth",status=~"4.."}[5m]) > 10
  for: 2m
  labels:
    severity: warning
    annotations:
    summary: "High authentication error rate"
    description: "Authentication endpoint error rate is {{ $value }} req/min"

# Suspicious Activity
- alert: SuspiciousActivity
  expr: rate(app_requests_total{user_id!=""}[5m]) > 100
  for: 1m
  labels:
    severity: warning
  annotations:
    summary: "High activity from user {{ $labels.user_id }}"
    description: "User {{ $labels.user_id }} is making {{ $value }} requests per minute"

# Unauthorized Access
- alert: UnauthorizedAccess
  expr: rate(app_requests_total{status="401"}[5m]) > 5
  for: 1m
  labels:
    severity: critical
  annotations:
    summary: "High unauthorized access attempts"
    description: "Unauthorized access attempts are {{ $value }} req/min"
```

## Escalation Policies

### Critical Alert Escalation

```python
# Critical alerts escalate through multiple channels and levels
critical_escalation_policy = {
    "immediate": {
        "channels": ["slack", "email"],
        "message": "Critical system issue requires immediate attention"
    },
    "5_minutes": {
        "channels": ["pagerduty"],
        "message": "Critical alert not acknowledged within 5 minutes"
    },
    "15_minutes": {
        "channels": ["email"],
        "additional_recipients": ["management@company.com"],
        "message": "Critical escalation - management notification"
    }
}
```

### Warning Alert Escalation

```python
# Warning alerts have a simpler escalation path
warning_escalation_policy = {
    "immediate": {
        "channels": ["slack"],
        "message": "Warning alert - please investigate"
    },
    "10_minutes": {
        "channels": ["email"],
        "message": "Warning alert persists for 10 minutes"
    }
}
```

### Info Alert Handling

```python
# Info alerts are only sent to Slack and don't escalate
info_escalation_policy = {
    "immediate": {
        "channels": ["slack"],
        "message": "Informational alert for awareness"
    }
}
```

## Notification Channels Configuration

### 1. Email Configuration

```yaml
# .env file
ALERT_EMAIL_ENABLED=true
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
ALERT_FROM_EMAIL=alerts@knowledge-graph.dev
ALERT_TO_EMAILS=admin@company.com,ops@company.com,oncall@company.com

# Email templates
EMAIL_TEMPLATE_CRITICAL="""
Subject: [CRITICAL] Knowledge Graph Analytics Alert

{{ .Alerts.Summary }}

Service: {{ .Alerts.Labels.service }}
Severity: {{ .Alerts.Labels.severity }}
Time: {{ .Alerts.StartsAt }}

Description:
{{ .Alerts.Annotations.description }}

Runbook: {{ .Alerts.Annotations.runbook_url }}

Immediate action required. Please acknowledge this alert.
"""
```

### 2. Slack Configuration

```yaml
# .env file
ALERT_SLACK_ENABLED=true
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK
SLACK_CHANNEL=#alerts
SLACK_USERNAME=Knowledge Graph Alerts

# Slack message formatting
SLACK_TEMPLATE_CRITICAL={
    "color": "danger",
    "title": "🚨 CRITICAL: {{ .Alerts.Summary }}",
    "text": "{{ .Alerts.Annotations.description }}",
    "fields": [
        {"title": "Service", "value": "{{ .Alerts.Labels.service }}", "short": true},
        {"title": "Severity", "value": "{{ .Alerts.Labels.severity }}", "short": true},
        {"title": "Time", "value": "{{ .Alerts.StartsAt }}", "short": true}
    ],
    "actions": [
        {
            "type": "button",
            "text": "View Dashboard",
            "url": "https://grafana.company.com/d/knowledge-graph"
        },
        {
            "type": "button",
            "text": "Runbook",
            "url": "{{ .Alerts.Annotations.runbook_url }}"
        }
    ]
}
```

### 3. PagerDuty Configuration

```yaml
# .env file
ALERT_PAGERDUTY_ENABLED=true
PAGERDUTY_INTEGRATION_KEY=your-integration-key
PAGERDUTY_SERVICE_KEY=your-service-key

# PagerDuty payload format
PAGERDUTY_PAYLOAD={
    "routing_key": "{{ .Config.IntegrationKey }}",
    "event_action": "trigger",
    "payload": {
        "summary": "{{ .Alerts.Summary }}",
        "source": "{{ .Alerts.Labels.service }}",
        "severity": "{{ .Alerts.Labels.severity }}",
        "timestamp": "{{ .Alerts.StartsAt }}",
        "component": "{{ .Alerts.Labels.component }}",
        "class": "{{ .Alerts.Labels.alertname }}",
        "custom_details": {
            "description": "{{ .Alerts.Annotations.description }}",
            "runbook_url": "{{ .Alerts.Annotations.runbook_url }}"
        }
    }
}
```

### 4. Webhook Configuration

```yaml
# .env file
ALERT_WEBHOOK_ENABLED=true
ALERT_WEBHOOK_URL=https://api.company.com/alerts
ALERT_WEBHOOK_HEADERS={"Authorization": "Bearer your-token"}
ALERT_WEBHOOK_TIMEOUT=30

# Webhook payload
WEBHOOK_PAYLOAD={
    "alert_id": "{{ .Alerts.ID }}",
    "name": "{{ .Alerts.Labels.alertname }}",
    "severity": "{{ .Alerts.Labels.severity }}",
    "summary": "{{ .Alerts.Summary }}",
    "description": "{{ .Alerts.Annotations.description }}",
    "labels": {{ .Alerts.Labels | toJSON }},
    "annotations": {{ .Alerts.Annotations | toJSON }},
    "starts_at": "{{ .Alerts.StartsAt }}",
    "generator_url": "{{ .Alerts.GeneratorURL }}"
}
```

## Alert Management

### Creating Custom Alerts

```python
from src.monitoring.alerting import alert_manager, AlertSeverity

# Create custom business alert
await alert_manager.process_alert({
    "id": f"custom_alert_{int(time.time())}",
    "name": "Low User Engagement",
    "severity": "warning",
    "summary": "User engagement below threshold",
    "description": "User session rate is below normal levels",
    "labels": {
        "service": "analytics",
        "component": "business_metrics",
        "alertname": "low_user_engagement"
    },
    "annotations": {
        "runbook_url": "https://docs.company.com/runbooks/user-engagement",
        "threshold": "5 sessions/hour",
        "current_value": "2.3 sessions/hour"
    }
})
```

### Acknowledging Alerts

```python
# Acknowledge an alert to prevent escalation
alert_id = "alert_123456"
await alert_manager.acknowledge_alert(
    alert_id=alert_id,
    acknowledged_by="ops-team",
    comment="Investigating database connection issues"
)
```

### Silencing Alerts

```python
# Silence alerts for maintenance windows
await alert_manager.silence_alerts(
    matchers={"service": "api"},
    duration=timedelta(hours=2),
    created_by="ops-team",
    comment="Scheduled maintenance for API service"
)
```

### Alert Rules Management

```yaml
# Custom alert rules
# prometheus-rules/custom-rules.yml
groups:
  - name: business-metrics
    rules:
      - alert: LowSearchQuality
        expr: avg(search_precision_score) < 0.75
        for: 15m
        labels:
          severity: warning
          team: product
        annotations:
          summary: "Search quality below threshold"
          description: "Search precision score is {{ .value }}"
          runbook_url: "https://docs.company.com/runbooks/search-quality"

      - alert: HighDocumentProcessingTime
        expr: histogram_quantile(0.90, rate(document_processing_duration_seconds_bucket[5m])) > 300
        for: 10m
        labels:
          severity: warning
          team: engineering
        annotations:
          summary: "Slow document processing"
          description: "90th percentile processing time is {{ .value }}s"
```

## Best Practices

### 1. Alert Design

- **Meaningful Alerts**: Each alert should represent a specific problem with a clear solution
- **Actionable Information**: Include runbook links and specific troubleshooting steps
- **Appropriate Thresholds**: Set thresholds based on SLAs and user experience impact
- **Avoid Alert Fatigue**: Don't alert on minor fluctuations

### 2. Escalation Management

- **Clear Escalation Paths**: Define who should be notified and when
- **Time-Based Escalation**: Escalate if alerts aren't acknowledged within specified timeframes
- **Channel Diversity**: Use multiple notification channels for critical alerts
- **Documentation**: Maintain up-to-date runbooks and escalation procedures

### 3. Monitoring Coverage

- **Critical Path Coverage**: Monitor all critical user journeys and business processes
- **Dependency Monitoring**: Track external services and dependencies
- **Performance Baselines**: Monitor against established performance baselines
- **Capacity Planning**: Alert when approaching resource limits

### 4. Alert Quality

```yaml
# Good alert example
- alert: DatabaseConnectionPoolExhausted
  expr: db_connections_active / db_connections_max > 0.9
  for: 2m
  labels:
    severity: critical
    service: database
    team: backend
  annotations:
    summary: "Database connection pool nearly exhausted"
    description: |
      Database connection pool is {{ .value | humanizePercentage }} full.
      This may cause application slowdowns or failures.

      Immediate actions:
      1. Check for connection leaks in the application
      2. Increase pool size if this is expected load
      3. Investigate slow queries that may be holding connections

    runbook_url: "https://docs.company.com/runbooks/database-connection-pool"
    dashboard_url: "https://grafana.company.com/d/database-performance"
```

### 5. Alert Testing

```python
# Test alert rules
import asyncio
from src.monitoring.alerting import alert_manager

async def test_alert_flow():
    # Create test alert
    test_alert = {
        "name": "Test Alert",
        "severity": "warning",
        "summary": "This is a test alert",
        "description": "Testing alert flow and notifications",
        "labels": {"service": "test", "environment": "development"}
    }

    # Process alert
    result = await alert_manager.process_alert(test_alert)
    print(f"Alert processed: {result.id}")

    # Verify notification was sent
    notifications = alert_manager.get_notification_history()
    test_notifications = [n for n in notifications if n["alert_id"] == result.id]
    print(f"Notifications sent: {len(test_notifications)}")

# Run test
asyncio.run(test_alert_flow())
```

## Alert Metrics and KPIs

Monitor the alerting system itself:

```yaml
# Alerting system metrics
- alert: AlertManagerDown
  expr: up{job="alertmanager"} == 0
  for: 1m
  labels:
    severity: critical
  annotations:
    summary: "AlertManager is down"

- alert: TooManyAlerts
  expr: rate(alerts_total[5m]) > 10
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "High alert firing rate"

- alert: NotificationDeliveryFailed
  expr: rate(alertmanager_notifications_failed_total[5m]) > 0.1
  for: 2m
  labels:
    severity: warning
  annotations:
    summary: "Alert notification delivery failures"
```

## Troubleshooting

### Common Issues

1. **Alerts Not Firing**
   ```bash
   # Check Prometheus rules
   curl http://localhost:9090/api/v1/rules

   # Verify targets are up
   curl http://localhost:9090/api/v1/targets

   # Check alert manager configuration
   docker exec alertmanager amtool config routes test
   ```

2. **Notifications Not Sending**
   ```bash
   # Check alert manager logs
   docker logs alertmanager

   # Test notification configuration
   docker exec alertmanager amtool config routes test --receiver=web.hook
   ```

3. **False Positives**
   ```yaml
   # Adjust for evaluation time
   - alert: HighCPUUsage
     expr: cpu_usage > 80
     for: 10m  # Increase from 5m to 10m
   ```

### Debug Commands

```bash
# Test specific alert rule
curl -G 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=rate(app_requests_total{status=~"5.."}[5m]) / rate(app_requests_total[5m]) > 0.05'

# Check alert manager status
curl http://localhost:9093/api/v1/status

# Silence alerts for testing
curl -XPOST http://localhost:9093/api/v1/silences \
  -d '{"matchers":[{"name":"service","value":"test","isRegex":false}],"startsAt":"2024-01-01T00:00:00Z","endsAt":"2024-01-01T01:00:00Z","createdBy":"test","comment":"Test silencing"}'
```

This comprehensive alerting guide provides the foundation for effective monitoring and alerting of the Knowledge Graph Analytics Dashboard system.
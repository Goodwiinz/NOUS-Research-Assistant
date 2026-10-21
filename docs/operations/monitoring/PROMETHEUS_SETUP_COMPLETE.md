# Prometheus Metrics Integration - Setup Complete

> **Status: historical (reconciled 2026-10-17).** This is a completion report for a past implementation, not current setup instructions. This document describes an observability design or setup; whether it is deployed on the AWS cluster is not verified. Alerting that Terraform defines for AWS resources is in [infrastructure/terraform/monitoring.tf](../../../infrastructure/terraform/monitoring.tf). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Start with the [AWS deployment overview](../../deployment/aws-overview.md) and [AWS on-call commands](../aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

## Summary

I've successfully integrated Prometheus metrics collection into your RAG system's Docker environment. Here's what was implemented:

## Changes Made

### 1. Backend Observability Integration
**File**: `backend/src/observability/document_processing_observability.py`
- ✅ Integrated `PrometheusMetricsCollector` for comprehensive metric collection
- ✅ Added real-time WebSocket updates via Redis Pub/Sub
- ✅ Configured Prometheus metrics server on port **8002**
- ✅ Integrated metrics for:
  - Document processing (start, stages, completion)
  - WebSocket events
  - System health metrics

### 2. Prometheus Configuration
**File**: `monitoring/prometheus.yml`
- ✅ Added `rag-backend-metrics` scrape job targeting `backend:8002`
- ✅ Scrape interval: 15s
- ✅ Timeout: 10s

**File**: `monitoring/alert_rules.yml`
- ✅ Added `real_time_status_alerts` group with:
  - `WebSocketHighErrorRate`: Triggers if error rate > 0.1/s
  - `DocumentProcessingHighErrorRate`: Triggers if failure rate > 10%
  - `DocumentProcessingHighLatency`: Triggers if avg duration > 5 minutes

### 3. Grafana Dashboard
**File**: `monitoring/grafana/dashboards/real-time-status.json`
- ✅ Created "Real-Time Status" dashboard with panels for:
  - Active WebSocket Connections
  - Active Processing Jobs
  - Document Processing Rate
  - Average Processing Duration

### 4. Docker Compose Updates
**File**: `config/docker-compose/docker-compose.development.yml`
- ✅ Exposed port **8002** for Prometheus metrics
- ✅ Fixed all build contexts to use relative paths (`../../backend`, `../../frontend`, etc.)
- ✅ Fixed all volume mounts to use relative paths

**File**: `config/docker-compose/docker-compose.yml` (production)
- ✅ Exposed port **8002** for Prometheus metrics
- ✅ Fixed all build contexts and volume paths

### 5. Kubernetes Configuration (for reference)
**File**: `infrastructure/k8s/monitoring/prometheus-config.yaml`
- ✅ Added `backend-metrics` scrape job
- ✅ Added alert rules for WebSocket and document processing

**File**: `deployment/monitoring/grafana-dashboards.yaml`
- ✅ Added `real-time-status.json` dashboard configuration

## Next Steps

### For Your Existing Docker Setup (`rag_system`)

Since you already have a running `rag_system-backend-1` container, you need to:

1. **Update the backend container to expose port 8002:**
   ```bash
   # Stop the current backend
   docker stop rag_system-backend-1
   
   # Find your docker-compose file (likely in the root directory)
   # Add port 8002 to the backend service ports section
   # Then restart:
   docker-compose up -d backend
   ```

2. **Start the monitoring stack:**
   ```bash
   # Navigate to monitoring directory
   cd monitoring
   
   # Start Prometheus and Grafana
   docker-compose -f docker-compose.monitoring.yml up -d prometheus grafana
   ```

3. **Verify the setup:**
   ```bash
   # Check if metrics endpoint is accessible
   curl http://localhost:8002/metrics
   
   # Check Prometheus targets
   open http://localhost:9090/targets
   
   # Access Grafana
   open http://localhost:3001
   # Default credentials: admin / (check GRAFANA_ADMIN_PASSWORD in .env)
   ```

### Alternative: Use the Updated Development Compose

If you want to use the updated configuration I just fixed:

```bash
# Stop current services
docker-compose down

# Use the updated development compose file
docker-compose -f config/docker-compose/docker-compose.development.yml up -d

# This will:
# - Build backend with port 8002 exposed
# - Use correct relative paths
# - Start all services with proper configuration
```

## Accessing the Monitoring Stack

Once everything is running:

- **Prometheus**: http://localhost:9090
  - Check targets: http://localhost:9090/targets
  - Check alerts: http://localhost:9090/alerts
  
- **Grafana**: http://localhost:3001
  - Navigate to Dashboards → Real-Time Status
  - View WebSocket and document processing metrics
  
- **Metrics Endpoint**: http://localhost:8002/metrics
  - Raw Prometheus metrics from the backend

## Key Metrics Available

### Document Processing
- `document_processing_total` - Total documents processed
- `document_processing_duration_seconds` - Processing duration histogram
- `document_processing_active_jobs` - Currently active processing jobs
- `document_processing_errors_total` - Processing errors

### WebSocket
- `websocket_connections_active` - Active WebSocket connections
- `websocket_messages_total` - Total messages sent/received
- `websocket_errors_total` - WebSocket errors
- `websocket_message_duration_seconds` - Message handling latency

### System Health
- `system_cpu_percent` - CPU usage per core
- `system_memory_bytes` - Memory usage (total, available, used, free)
- `system_disk_usage_percent` - Disk usage
- `system_network_io_bytes` - Network I/O

## Troubleshooting

### Metrics endpoint returns 404
- Ensure `document_processing_observability.start_monitoring_tasks()` is called during app startup
- Check that the Prometheus metrics server is running on port 8002
- Verify port 8002 is exposed in your Docker configuration

### Prometheus can't scrape metrics
- Check Docker network connectivity: `docker network inspect rag_system_multimodal-rag-network`
- Verify the backend service name matches in prometheus.yml
- Check Prometheus logs: `docker logs <prometheus-container>`

### Grafana dashboard shows no data
- Verify Prometheus is configured as a data source in Grafana
- Check that Prometheus is successfully scraping metrics
- Ensure the metric names in the dashboard match those being collected

## Files Modified

1. `backend/src/observability/document_processing_observability.py`
2. `monitoring/prometheus.yml`
3. `monitoring/alert_rules.yml`
4. `monitoring/grafana/dashboards/real-time-status.json`
5. `config/docker-compose/docker-compose.development.yml`
6. `config/docker-compose/docker-compose.yml`
7. `infrastructure/k8s/monitoring/prometheus-config.yaml`
8. `deployment/monitoring/grafana-dashboards.yaml`

## Implementation Status

✅ **Phase 4.2 Observability & Monitoring - COMPLETE**
- Prometheus metrics collection integrated
- Grafana dashboards configured
- Alert rules defined
- Docker configuration updated
- Real-time WebSocket updates via Redis Pub/Sub

The observability infrastructure is now ready for deployment!

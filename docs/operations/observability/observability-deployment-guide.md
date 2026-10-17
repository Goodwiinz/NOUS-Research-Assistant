# Observability Stack Deployment Guide

> **Status: design record (reconciled 2026-10-17).** This document describes an observability design or setup; whether it is deployed on the AWS cluster is not verified. Alerting that Terraform defines for AWS resources is in [infrastructure/terraform/monitoring.tf](../../../infrastructure/terraform/monitoring.tf). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Qdrant is removed; live retrieval is PostgreSQL full-text. Start with the [AWS deployment overview](../../deployment/aws-overview.md) and [AWS on-call commands](../aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

## Overview

This guide provides step-by-step instructions for deploying the complete observability stack for the Multimodal Enterprise RAG System. The stack includes distributed tracing, metrics collection, log aggregation, visualization, and alerting.

## Prerequisites

### System Requirements
- **CPU**: 8+ cores recommended
- **Memory**: 32GB+ RAM recommended
- **Storage**: 500GB+ SSD storage recommended
- **Network**: 1Gbps+ network connection
- **Docker**: Version 20.10+
- **Docker Compose**: Version 2.0+

### Environment Variables
Create a `.env` file with the following variables:

```bash
# Database Configuration
DB_PASSWORD=your_secure_password
NEO4J_PASSWORD=your_neo4j_password
QDRANT_API_KEY=your_qdrant_key
REDIS_PASSWORD=your_redis_password

# Grafana Configuration
GRAFANA_PASSWORD=your_grafana_password

# SMTP Configuration (for alerts)
SMTP_HOST=smtp.yourcompany.com
SMTP_PORT=587
SMTP_USER=alerts@yourcompany.com
SMTP_PASSWORD=your_smtp_password
SMTP_FROM=alerts@yourcompany.com

# Slack Configuration (for alerts)
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK

# PagerDuty Configuration (for critical alerts)
PAGERDUTY_SERVICE_KEY=your_pagerduty_key

# Application Configuration
SECRET_KEY=your_secret_key
OPENAI_API_KEY=your_openai_key
ANTHROPIC_API_KEY=your_anthropic_key
ENVIRONMENT=production
LOG_LEVEL=INFO
DEBUG=false
```

## Quick Start

### 1. Clone Repository
```bash
git clone https://github.com/yourcompany/rag-system.git
cd rag-system
```

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env with your configuration
```

### 3. Deploy Observability Stack
```bash
# Create network if it doesn't exist
docker network create rag-network

# Deploy observability stack
docker-compose -f docker-compose.observability.yml up -d
```

### 4. Verify Deployment
```bash
# Check all services are running
docker-compose -f docker-compose.observability.yml ps

# Check service health
curl http://localhost:3001/api/health  # Grafana
curl http://localhost:9090/-/healthy  # Prometheus
curl http://localhost:16686/         # Jaeger
curl http://localhost:5601/api/status # Kibana
```

### 5. Access Services
- **Grafana**: http://localhost:3001 (admin/admin)
- **Prometheus**: http://localhost:9090
- **Jaeger**: http://localhost:16686
- **Kibana**: http://localhost:5601
- **Alertmanager**: http://localhost:9093

## Detailed Deployment

### Step 1: Network Configuration
```bash
# Create dedicated network for observability
docker network create \
  --driver bridge \
  --subnet=172.20.0.0/16 \
  rag-network
```

### Step 2: Storage Preparation
```bash
# Create directories for persistent storage
mkdir -p data/{prometheus,grafana,elasticsearch,tempo,alertmanager}
mkdir -p logs/{application,system,security}

# Set proper permissions
chmod 755 data logs
chmod 644 data/* logs/*
```

### Step 3: SSL/TLS Configuration (Optional but Recommended)
```bash
# Create SSL certificates
mkdir -p ssl

# Generate self-signed certificates (for development)
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout ssl/rag-system.key \
  -out ssl/rag-system.crt \
  -subj "/C=US/ST=State/L=City/O=Company/CN=rag-system.local"

# For production, use certificates from your CA
```

### Step 4: Deploy Core Services
```bash
# Deploy databases first
docker-compose up -d postgres redis neo4j qdrant

# Wait for databases to be ready (approximately 60 seconds)
sleep 60

# Deploy application services
docker-compose up -d backend frontend celery-worker celery-beat

# Deploy observability stack
docker-compose -f docker-compose.observability.yml up -d
```

### Step 5: Initialize Monitoring
```bash
# Create initial data
docker-compose exec backend python -c "
from src.core.init_db import init_database
init_database()
"

# Initialize monitoring configuration
docker-compose exec prometheus curl -X POST http://localhost:9090/-/reload
docker-compose exec grafana curl -X POST http://localhost:3000/api/admin/provisioning/dashboards/reload
```

## Service Configuration

### Prometheus Configuration
Edit `monitoring/prometheus.yml`:
```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - "alert_rules.yml"
  - "recording_rules.yml"

alerting:
  alertmanagers:
    - static_configs:
        - targets: ['alertmanager:9093']

scrape_configs:
  # Application metrics
  - job_name: 'rag-backend'
    static_configs:
      - targets: ['backend:8000']
    metrics_path: '/metrics'
    scrape_interval: 30s

  # Infrastructure metrics
  - job_name: 'node-exporter'
    static_configs:
      - targets: ['node-exporter:9100']
```

### Alertmanager Configuration
Edit `monitoring/alertmanager/alertmanager.yml`:
```yaml
global:
  smtp_smarthost: '${SMTP_HOST}:${SMTP_PORT}'
  smtp_from: '${SMTP_FROM}'
  smtp_auth_username: '${SMTP_USER}'
  smtp_auth_password: '${SMTP_PASSWORD}'

route:
  group_by: ['alertname', 'cluster', 'service']
  group_wait: 10s
  group_interval: 10s
  repeat_interval: 12h
  receiver: 'default'

receivers:
  - name: 'default'
    email_configs:
      - to: 'alerts@yourcompany.com'
        subject: '[RAG System] Alert: {{ .GroupLabels.alertname }}'
```

### Grafana Configuration
1. **Data Sources**: Configure Prometheus, Elasticsearch, and Jaeger
2. **Dashboards**: Import pre-built dashboards
3. **Users**: Set up user roles and permissions
4. **Alerting**: Configure notification channels

### Kibana Configuration
1. **Index Patterns**: Create patterns for `rag-system-logs-*`
2. **Visualizations**: Set up log analysis dashboards
3. **Saved Searches**: Create common search queries
4. **Watchers**: Configure log-based alerts

## Monitoring Configuration

### Custom Metrics
Add custom metrics to your application:

```python
from src.observability.metrics import record_histogram, increment_counter

# Record custom business metrics
record_histogram("rag_answer_relevancy_score", score, {
    "model": model_name,
    "query_type": query_type
})

increment_counter("documents_processed_total", {
    "file_type": file_type,
    "success": str(success)
})
```

### Custom Tracing
Add custom spans to your application:

```python
from src.observability.tracer import trace_span

with trace_span("custom_operation", attributes={
    "operation_type": "data_processing",
    "record_count": len(records)
}):
    # Your custom operation here
    process_data(records)
```

### Custom Logging
Add structured logging to your application:

```python
from src.observability.logging import get_logger

logger = get_logger(__name__)
logger.info("Business event occurred",
    event_type="user_action",
    user_id=user.id,
    action="document_upload",
    document_id=document.id
)
```

## Security Hardening

### Network Security
```bash
# Configure firewall rules
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP
ufw allow 443/tcp   # HTTPS
ufw allow 9090/tcp  # Prometheus (internal)
ufw allow 3001/tcp  # Grafana (restricted)
ufw enable
```

### Authentication and Authorization
```bash
# Configure Grafana authentication
# Edit grafana.ini
[auth.anonymous]
enabled = false

[auth.basic]
enabled = true

[auth.ldap]
enabled = true
config_file = /etc/grafana/ldap.toml
```

### SSL/TLS Configuration
```yaml
# Configure HTTPS for services
services:
  grafana:
    environment:
      - GF_SERVER_PROTOCOL=https
      - GF_SERVER_CERT_FILE=/etc/ssl/certs/rag-system.crt
      - GF_SERVER_CERT_KEY=/etc/ssl/private/rag-system.key
    volumes:
      - ./ssl:/etc/ssl
```

## Performance Tuning

### Prometheus Optimization
```yaml
# prometheus.yml
global:
  scrape_interval: 15s
  evaluation_interval: 15s
  external_labels:
    cluster: 'rag-production'
    replica: 'prometheus-1'

# Storage configuration
storage:
  tsdb:
    retention.time: 30d
    retention.size: 10GB
```

### Elasticsearch Optimization
```yaml
# elasticsearch.yml
cluster.name: rag-cluster
node.name: rag-es-node
network.host: 0.0.0.0

# Performance settings
indices.memory.index_buffer_size: 20%
indices.queries.cache.size: 5%
indices.fielddata.cache.size: 40%

# JVM settings
ES_JAVA_OPTS: "-Xms2g -Xmx2g -XX:+UseG1GC"
```

### Grafana Optimization
```ini
# grafana.ini
[database]
max_idle_conn = 2
max_open_conn = 0
conn_max_lifetime = 14400

[metrics]
enabled = true
interval_seconds = 15

[plugins]
allow_loading_unsigned_plugins = false
```

## Backup and Recovery

### Automated Backup Script
```bash
#!/bin/bash
# backup-observability.sh

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backups/observability"

# Create backup directory
mkdir -p $BACKUP_DIR

# Backup Prometheus data
docker exec rag-prometheus tar -czf - /prometheus > $BACKUP_DIR/prometheus-$DATE.tar.gz

# Backup Grafana data
docker exec rag-grafana tar -czf - /var/lib/grafana > $BACKUP_DIR/grafana-$DATE.tar.gz

# Backup Elasticsearch data
docker exec rag-elasticsearch tar -czf - /usr/share/elasticsearch/data > $BACKUP_DIR/elasticsearch-$DATE.tar.gz

# Backup Alertmanager data
docker exec rag-alertmanager tar -czf - /alertmanager > $BACKUP_DIR/alertmanager-$DATE.tar.gz

# Clean old backups (keep 7 days)
find $BACKUP_DIR -name "*.tar.gz" -mtime +7 -delete

echo "Backup completed: $DATE"
```

### Recovery Script
```bash
#!/bin/bash
# restore-observability.sh

BACKUP_FILE=$1
RESTORE_DIR="/tmp/restore"

if [ -z "$BACKUP_FILE" ]; then
    echo "Usage: $0 <backup_file>"
    exit 1
fi

# Create restore directory
mkdir -p $RESTORE_DIR

# Stop services
docker-compose -f docker-compose.observability.yml stop

# Extract backup
tar -xzf $BACKUP_FILE -C $RESTORE_DIR

# Restore data (adjust paths as needed)
# docker exec rag-prometheus tar -xzf $RESTORE_DIR/prometheus.tar.gz -C /

# Start services
docker-compose -f docker-compose.observability.yml start

echo "Restore completed from: $BACKUP_FILE"
```

## Monitoring the Observability Stack

### Health Checks
```bash
#!/bin/bash
# health-check.sh

# Check Prometheus
curl -f http://localhost:9090/-/healthy || echo "Prometheus is unhealthy"

# Check Grafana
curl -f http://localhost:3001/api/health || echo "Grafana is unhealthy"

# Check Elasticsearch
curl -f http://localhost:9200/_cluster/health || echo "Elasticsearch is unhealthy"

# Check Jaeger
curl -f http://localhost:16686/ || echo "Jaeger is unhealthy"
```

### Performance Monitoring
```bash
# Monitor resource usage
docker stats --no-stream

# Check disk usage
df -h

# Check network connections
netstat -tulpn | grep -E ':(9090|3001|5601|16686|9200)'
```

## Troubleshooting

### Common Issues

#### Services Not Starting
```bash
# Check logs
docker-compose -f docker-compose.observability.yml logs [service_name]

# Check resource availability
docker system df
docker system prune -f

# Restart services
docker-compose -f docker-compose.observability.yml restart
```

#### Data Not Appearing
```bash
# Check Prometheus targets
curl http://localhost:9090/api/v1/targets

# Check Elasticsearch indices
curl http://localhost:9200/_cat/indices

# Check Grafana data sources
curl -u admin:admin http://localhost:3001/api/datasources
```

#### Alerting Not Working
```bash
# Check Alertmanager configuration
curl http://localhost:9093/api/v1/status

# Test alert configuration
curl -X POST http://localhost:9093/api/v1/alerts
```

### Log Analysis
```bash
# Application logs
docker-compose logs -f backend

# System logs
journalctl -u docker -f

# Observability logs
docker-compose -f docker-compose.observability.yml logs -f prometheus
```

## Scaling and High Availability

### Prometheus Scaling
```yaml
# docker-compose.scale.yml
version: '3.8'
services:
  prometheus-1:
    extends:
      file: docker-compose.observability.yml
      service: prometheus
    container_name: rag-prometheus-1

  prometheus-2:
    extends:
      file: docker-compose.observability.yml
      service: prometheus
    container_name: rag-prometheus-2
    environment:
      - PROMETHEUS_PEER=prometheus-1:9090
```

### Elasticsearch Scaling
```yaml
# Multi-node Elasticsearch
services:
  elasticsearch-1:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.11.0
    environment:
      - cluster.name=rag-cluster
      - node.name=rag-es-node-1
      - discovery.seed_hosts=elasticsearch-2,elasticsearch-3
      - cluster.initial_master_nodes=rag-es-node-1,rag-es-node-2,rag-es-node-3

  elasticsearch-2:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.11.0
    environment:
      - cluster.name=rag-cluster
      - node.name=rag-es-node-2
      - discovery.seed_hosts=elasticsearch-1,elasticsearch-3
      - cluster.initial_master_nodes=rag-es-node-1,rag-es-node-2,rag-es-node-3

  elasticsearch-3:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.11.0
    environment:
      - cluster.name=rag-cluster
      - node.name=rag-es-node-3
      - discovery.seed_hosts=elasticsearch-1,elasticsearch-2
      - cluster.initial_master_nodes=rag-es-node-1,rag-es-node-2,rag-es-node-3
```

## Maintenance

### Regular Maintenance Tasks
```bash
# Weekly maintenance script
#!/bin/bash
# weekly-maintenance.sh

# Clean up old logs
find /var/log -name "*.log" -mtime +7 -delete

# Update Docker images
docker-compose -f docker-compose.observability.yml pull

# Restart services
docker-compose -f docker-compose.observability.yml restart

# Check disk space
df -h | grep -E "(9[0-9]%|100%)"

# Run backup
./backup-observability.sh
```

### Configuration Updates
```bash
# Reload Prometheus configuration
curl -X POST http://localhost:9090/-/reload

# Reload Alertmanager configuration
curl -X POST http://localhost:9093/-/reload

# Restart Grafana for configuration changes
docker-compose restart grafana
```

## Conclusion

This deployment guide provides comprehensive instructions for deploying and maintaining the observability stack. Regular maintenance and monitoring of the observability system itself ensures reliable operation and effective monitoring of your RAG system.

For production deployments, consider:
- High availability configurations
- Disaster recovery procedures
- Security hardening
- Performance optimization
- Regular audits and updates
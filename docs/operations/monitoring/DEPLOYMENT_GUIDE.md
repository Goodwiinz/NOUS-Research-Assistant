# Deployment Guide - Knowledge Graph Analytics Monitoring

> **Status: historical (reconciled 2026-10-17).** This document describes an observability design or setup; whether it is deployed on the AWS cluster is not verified. Alerting that Terraform defines for AWS resources is in [infrastructure/terraform/monitoring.tf](../../../infrastructure/terraform/monitoring.tf). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Start with the [AWS deployment overview](../../deployment/aws-overview.md) and [AWS on-call commands](../aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

This guide provides detailed instructions for deploying the monitoring stack in different environments.

## Prerequisites

### System Requirements

- **Docker**: 20.10+ and Docker Compose 2.0+
- **Memory**: Minimum 8GB RAM (16GB+ recommended)
- **Storage**: Minimum 50GB available disk space
- **Network**: Access to Docker Hub and internet connectivity
- **Ports**: Ensure the following ports are available:
  - Grafana: 3001
  - Prometheus: 9090
  - Jaeger: 16686
  - Loki: 3100
  - OpenTelemetry Collector: 4317, 4318

### Required Software

```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Verify installation
docker --version
docker-compose --version
```

## Environment Setup

### 1. Create Environment File

Create `.env` file in the monitoring directory:

```bash
# Environment
ENVIRONMENT=production
SERVICE_NAME=knowledge-graph-analytics
VERSION=1.0.0

# Timezone
TZ=UTC

# Grafana Configuration
GRAFANA_ADMIN_PASSWORD=your-secure-password
GRAFANA_SMTP_ENABLED=true
GRAFANA_SMTP_HOST=smtp.gmail.com:587
GRAFANA_SMTP_USER=your-email@gmail.com
GRAFANA_SMTP_PASSWORD=your-app-password
GRAFANA_SMTP_FROM_ADDRESS=alerts@knowledge-graph.dev

# Prometheus Configuration
PROMETHEUS_RETENTION=15d
PROMETHEUS_WEB_ENABLE_ADMIN_API=true

# Alert Configuration
ALERT_EMAIL_ENABLED=true
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
ALERT_FROM_EMAIL=alerts@knowledge-graph.dev
ALERT_TO_EMAILS=admin@company.com,ops@company.com

# Slack Configuration
ALERT_SLACK_ENABLED=true
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK
SLACK_CHANNEL=#alerts

# PagerDuty Configuration (Optional)
ALERT_PAGERDUTY_ENABLED=false
PAGERDUTY_INTEGRATION_KEY=your-integration-key
PAGERDUTY_SERVICE_KEY=your-service-key

# OpenTelemetry
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_SERVICE_NAME=knowledge-graph-analytics
OTEL_SERVICE_VERSION=1.0.0
```

### 2. Directory Structure

Create the following directory structure:

```
monitoring/
├── docker-compose.monitoring.yml
├── .env
├── grafana/
│   ├── provisioning/
│   │   ├── datasources/
│   │   └── dashboards/
│   └── dashboards/
├── prometheus-rules/
├── loki-config.yaml
├── promtail-config.yml
├── otel-collector-config.yaml
└── data/
    ├── grafana/
    ├── prometheus/
    ├── loki/
    └── redis/
```

## Deployment Options

### Option 1: Development Deployment

Quick setup for development and testing:

```bash
# Clone repository
git clone https://github.com/your-org/knowledge-graph-analytics.git
cd knowledge-graph-analytics/monitoring

# Copy environment template
cp .env.example .env

# Edit environment variables
nano .env

# Start services
docker-compose -f docker-compose.monitoring.yml up -d

# Verify deployment
docker-compose -f docker-compose.monitoring.yml ps
```

### Option 2: Production Deployment

Production-ready deployment with persistence and security:

```bash
# Create data directories with proper permissions
sudo mkdir -p /opt/knowledge-graph-monitoring/{grafana,prometheus,loki,redis}
sudo chown -R 472:472 /opt/knowledge-graph-monitoring/grafana
sudo chown -R 65534:65534 /opt/knowledge-graph-monitoring/prometheus
sudo chown -R 10001:10001 /opt/knowledge-graph-monitoring/loki

# Create production compose file
cat > docker-compose.production.yml << 'EOF'
version: '3.8'

services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.102.0
    command: ["--config=/etc/otel-collector-config.yaml"]
    volumes:
      - ./otel-collector-config.yaml:/etc/otel-collector-config.yaml:ro
    ports:
      - "4317:4317"
      - "4318:4318"
      - "8888:8888"
      - "8889:8889"
      - "13133:13133"
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: '0.5'
        reservations:
          memory: 256M
          cpus: '0.25'

  jaeger:
    image: jaegertracing/all-in-one:1.55
    ports:
      - "16686:16686"
      - "14250:14250"
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 1G
          cpus: '0.5'
        reservations:
          memory: 512M
          cpus: '0.25'

  prometheus:
    image: prom/prometheus:v2.53.0
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
      - '--storage.tsdb.retention.time=${PROMETHEUS_RETENTION:-15d}'
      - '--web.enable-lifecycle'
      - '--web.enable-admin-api'
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./prometheus-rules:/etc/prometheus/rules:ro
      - /opt/knowledge-graph-monitoring/prometheus:/prometheus
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 2G
          cpus: '1.0'
        reservations:
          memory: 1G
          cpus: '0.5'

  grafana:
    image: grafana/grafana-oss:11.1.0
    ports:
      - "3001:3000"
    environment:
      - GF_SECURITY_ADMIN_USER=admin
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}
      - GF_INSTALL_PLUGINS=grafana-piechart-panel,grafana-worldmap-panel
      - GF_SERVER_HTTP_PORT=3000
      - GF_DATABASE_PATH=/var/lib/grafana/grafana.db
    volumes:
      - /opt/knowledge-graph-monitoring/grafana:/var/lib/grafana
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
      - ./grafana/dashboards:/var/lib/grafana/dashboards:ro
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: '0.5'
        reservations:
          memory: 256M
          cpus: '0.25'

  loki:
    image: grafana/loki:2.9.10
    command: -config.file=/etc/loki/local-config.yaml
    ports:
      - "3100:3100"
    volumes:
      - ./loki-config.yaml:/etc/loki/local-config.yaml:ro
      - /opt/knowledge-graph-monitoring/loki:/loki
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 1G
          cpus: '0.5'
        reservations:
          memory: 512M
          cpus: '0.25'

  promtail:
    image: grafana/promtail:2.9.10
    command: -config.file=/etc/promtail/config.yml
    volumes:
      - ./promtail-config.yml:/etc/promtail/config.yml:ro
      - /var/log:/var/log:ro
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 256M
          cpus: '0.25'
        reservations:
          memory: 128M
          cpus: '0.1'

  redis:
    image: redis:7.2.5-alpine
    command: redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy allkeys-lru
    ports:
      - "6379:6379"
    volumes:
      - /opt/knowledge-graph-monitoring/redis:/data
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: '0.25'
        reservations:
          memory: 256M
          cpus: '0.1'

networks:
  default:
    driver: bridge
EOF

# Deploy production stack
docker-compose -f docker-compose.production.yml up -d
```

### Option 3: Kubernetes Deployment

For Kubernetes environments:

```yaml
# monitoring-namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: monitoring
---
# prometheus-configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: prometheus-config
  namespace: monitoring
data:
  prometheus.yml: |
    global:
      scrape_interval: 15s
      evaluation_interval: 15s

    rule_files:
      - "/etc/prometheus/rules/*.yml"

    scrape_configs:
      - job_name: 'prometheus'
        static_configs:
          - targets: ['localhost:9090']

      - job_name: 'knowledge-graph-analytics'
        kubernetes_sd_configs:
          - role: pod
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
            action: keep
            regex: true

---
# grafana-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: grafana
  namespace: monitoring
spec:
  replicas: 1
  selector:
    matchLabels:
      app: grafana
  template:
    metadata:
      labels:
        app: grafana
    spec:
      containers:
      - name: grafana
        image: grafana/grafana-oss:11.1.0
        ports:
        - containerPort: 3000
        env:
        - name: GF_SECURITY_ADMIN_PASSWORD
          valueFrom:
            secretKeyRef:
              name: grafana-secrets
              key: admin-password
        volumeMounts:
        - name: grafana-storage
          mountPath: /var/lib/grafana
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "500m"
      volumes:
      - name: grafana-storage
        persistentVolumeClaim:
          claimName: grafana-pvc

---
# grafana-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: grafana
  namespace: monitoring
spec:
  selector:
    app: grafana
  ports:
  - port: 3000
    targetPort: 3000
  type: LoadBalancer
```

Deploy to Kubernetes:

```bash
# Create namespace
kubectl apply -f monitoring-namespace.yaml

# Deploy Prometheus
kubectl apply -f prometheus-configmap.yaml
kubectl apply -f prometheus-deployment.yaml

# Deploy Grafana
kubectl apply -f grafana-deployment.yaml
kubectl apply -f grafana-service.yaml

# Deploy other components
kubectl apply -f jaeger-deployment.yaml
kubectl apply -f loki-deployment.yaml

# Verify deployment
kubectl get pods -n monitoring
kubectl get services -n monitoring
```

## Configuration

### 1. Prometheus Configuration

Customize `prometheus.yml`:

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s
  external_labels:
    cluster: 'knowledge-graph'
    replica: 'prometheus-1'

rule_files:
  - "/etc/prometheus/rules/*.yml"

alerting:
  alertmanagers:
    - static_configs:
        - targets:
          - alertmanager:9093

scrape_configs:
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']

  - job_name: 'knowledge-graph-api'
    static_configs:
      - targets: ['api:8000']
    metrics_path: /metrics
    scrape_interval: 15s

  - job_name: 'node-exporter'
    static_configs:
      - targets: ['node-exporter:9100']

  - job_name: 'cadvisor'
    static_configs:
      - targets: ['cadvisor:8080']
```

### 2. Grafana Data Sources

Create `grafana/provisioning/datasources/datasources.yml`:

```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true

  - name: Jaeger
    type: jaeger
    access: proxy
    url: http://jaeger:16686

  - name: Loki
    type: loki
    access: proxy
    url: http://loki:3100
```

### 3. Alert Manager Configuration

Create `alertmanager.yml`:

```yaml
global:
  smtp_smarthost: 'smtp.gmail.com:587'
  smtp_from: 'alerts@knowledge-graph.dev'
  smtp_auth_username: 'your-email@gmail.com'
  smtp_auth_password: 'your-app-password'

route:
  group_by: ['alertname']
  group_wait: 10s
  group_interval: 10s
  repeat_interval: 1h
  receiver: 'web.hook'

receivers:
  - name: 'web.hook'
    email_configs:
      - to: 'admin@company.com'
        subject: '[KGA Alert] {{ .GroupLabels.alertname }}'
        body: |
          {{ range .Alerts }}
          Alert: {{ .Annotations.summary }}
          Description: {{ .Annotations.description }}
          {{ end }}

    slack_configs:
      - api_url: 'https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK'
        channel: '#alerts'
        title: 'Knowledge Graph Analytics Alert'
        text: '{{ range .Alerts }}{{ .Annotations.summary }}{{ end }}'

inhibit_rules:
  - source_match:
      severity: 'critical'
    target_match:
      severity: 'warning'
    equal: ['alertname', 'instance']
```

## Security Configuration

### 1. SSL/TLS Setup

For production, configure SSL/TLS:

```yaml
# grafana-ssl.yaml
version: '3.8'

services:
  grafana:
    image: grafana/grafana-oss:11.1.0
    ports:
      - "443:3000"
    volumes:
      - ./ssl:/etc/ssl/certs:ro
    environment:
      - GF_SERVER_PROTOCOL=https
      - GF_SERVER_CERT_FILE=/etc/ssl/certs/grafana.crt
      - GF_SERVER_CERT_KEY=/etc/ssl/certs/grafana.key
```

### 2. Authentication

Configure authentication in Grafana:

```bash
# Enable LDAP authentication
GF_AUTH_LDAP_ENABLED=true
GF_AUTH_LDAP_CONFIG_FILE=/etc/grafana/ldap.toml

# Enable OAuth
GF_AUTH_GENERIC_OAUTH_ENABLED=true
GF_AUTH_GENERIC_OAUTH_CLIENT_ID=your-client-id
GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET=your-client-secret
```

### 3. Network Security

Configure firewall rules:

```bash
# Allow only necessary ports
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 3001/tcp  # Grafana
sudo ufw allow 9090/tcp  # Prometheus
sudo ufw enable
```

## Monitoring the Monitoring Stack

### Health Checks

```bash
# Check service health
curl http://localhost:3001/api/health  # Grafana
curl http://localhost:9090/-/healthy   # Prometheus
curl http://localhost:16686/          # Jaeger UI
```

### Log Monitoring

```bash
# View logs
docker-compose logs -f grafana
docker-compose logs -f prometheus
docker-compose logs -f otel-collector
```

### Metrics Collection

Verify metrics are being collected:

```bash
# Check Prometheus targets
curl http://localhost:9090/api/v1/targets | jq

# Check OpenTelemetry metrics
curl http://localhost:8889/metrics
```

## Backup and Recovery

### 1. Data Backup

```bash
#!/bin/bash
# backup-monitoring.sh

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backup/monitoring/$DATE"

mkdir -p $BACKUP_DIR

# Backup Prometheus data
docker exec prometheus tar czf - /prometheus | gzip > $BACKUP_DIR/prometheus.tar.gz

# Backup Grafana data
docker exec grafana tar czf - /var/lib/grafana | gzip > $BACKUP_DIR/grafana.tar.gz

# Backup Loki data
docker exec loki tar czf - /loki | gzip > $BACKUP_DIR/loki.tar.gz

# Backup configuration files
cp -r /opt/knowledge-graph-monitoring/*.yml $BACKUP_DIR/

echo "Backup completed: $BACKUP_DIR"
```

### 2. Automated Backup

Add to crontab:

```bash
# Daily backup at 2 AM
0 2 * * * /opt/knowledge-graph-monitoring/scripts/backup-monitoring.sh
```

### 3. Recovery

```bash
#!/bin/bash
# restore-monitoring.sh

BACKUP_DIR=$1

if [ -z "$BACKUP_DIR" ]; then
    echo "Usage: $0 <backup_directory>"
    exit 1
fi

# Stop services
docker-compose -f docker-compose.production.yml down

# Restore data
docker run --rm -v /opt/knowledge-graph-monitoring/prometheus:/data alpine tar xzf $BACKUP_DIR/prometheus.tar.gz -C /
docker run --rm -v /opt/knowledge-graph-monitoring/grafana:/data alpine tar xzf $BACKUP_DIR/grafana.tar.gz -C /
docker run --rm -v /opt/knowledge-graph-monitoring/loki:/data alpine tar xzf $BACKUP_DIR/loki.tar.gz -C /

# Start services
docker-compose -f docker-compose.production.yml up -d

echo "Restore completed from: $BACKUP_DIR"
```

## Performance Tuning

### 1. Prometheus Optimization

```yaml
# prometheus.yml
global:
  # Reduce scrape interval for high-cardinality metrics
  scrape_interval: 30s
  # Limit sample ingestion
  remote_write:
    - queue_config:
        max_samples_per_send: 1000
        max_shards: 200
```

### 2. Grafana Performance

```bash
# Increase memory limits
GF_SECURITY_ADMIN_PASSWORD=your-password
GF_CACHE_TTL=300
GF_CACHE_DATA_TTL=60
```

### 3. Loki Performance

```yaml
# loki-config.yaml
limits_config:
  ingestion_rate_mb: 64
  ingestion_burst_size_mb: 128
  max_query_parallelism: 32
```

## Troubleshooting

### Common Issues

1. **Services Not Starting**
   ```bash
   # Check logs
   docker-compose logs service-name

   # Check port conflicts
   netstat -tulpn | grep :3001
   ```

2. **High Memory Usage**
   ```bash
   # Monitor resource usage
   docker stats

   # Adjust resource limits
   # Update docker-compose.yml with appropriate limits
   ```

3. **Missing Data in Grafana**
   ```bash
   # Check Prometheus targets
   curl http://localhost:9090/api/v1/targets

   # Verify data source configuration
   # Grafana → Configuration → Data Sources
   ```

### Debug Commands

```bash
# Check container health
docker-compose ps

# View resource usage
docker stats --no-stream

# Check network connectivity
docker exec prometheus wget -qO- http://grafana:3000/api/health

# Verify metrics collection
curl http://localhost:9090/api/v1/query?query=up
```

## Maintenance

### Regular Tasks

1. **Weekly**: Review alert rules and dashboards
2. **Monthly**: Update monitoring components
3. **Quarterly**: Review retention policies and storage requirements
4. **Annually**: Complete security audit and access review

### Updates

```bash
# Update images
docker-compose pull

# Restart services with new images
docker-compose up -d

# Verify update
docker-compose ps
```

This deployment guide provides comprehensive instructions for deploying the monitoring stack in various environments with proper security, backup, and maintenance procedures.
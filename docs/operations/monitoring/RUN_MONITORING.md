# How to Run the Monitoring System

> **Status: historical (reconciled 2026-10-17).** This document describes an observability design or setup; whether it is deployed on the AWS cluster is not verified. Alerting that Terraform defines for AWS resources is in [infrastructure/terraform/monitoring.tf](../../../infrastructure/terraform/monitoring.tf). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Start with the [AWS deployment overview](../../deployment/aws-overview.md) and [AWS on-call commands](../aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

## Quick Start

### 1. Prerequisites

Ensure you have the following installed:
- Docker & Docker Compose
- kubectl (if using Kubernetes)
- Node.js 18+ (for frontend)
- Python 3.12+ (for backend)

### 2. Environment Setup

```bash
# Clone the repository
git clone <your-repo-url>
cd RAG_system

# Copy environment templates
cp .env.example .env.production
cp .env.example .env.local

# Configure your environment variables
nano .env.local
```

### 3. Start Core Services

```bash
# Start the main RAG system services
docker-compose up -d

# Wait for services to be ready (20-30 seconds)
sleep 30

# Initialize databases
./setup.sh
```

## Option 1: Docker Compose (Recommended for Development)

### Start Monitoring Stack

```bash
# Navigate to monitoring directory
cd monitoring

# Start the complete monitoring stack
docker-compose -f docker-compose.monitoring.yml up -d

# Check status
docker-compose -f docker-compose.monitoring.yml ps
```

### Access Monitoring Interfaces

Once running, you can access:

- **Grafana Dashboards**: http://localhost:3001 (admin/admin)
- **Prometheus**: http://localhost:9090
- **Jaeger Tracing**: http://localhost:16686
- **Kibana Logs**: http://localhost:5601
- **AlertManager**: http://localhost:9093

### Monitoring Stack Components

```yaml
# monitoring/docker-compose.monitoring.yml
services:
  prometheus:
    image: prom/prometheus:latest
    ports: ["9090:9090"]
    volumes: ["./prometheus.yml:/etc/prometheus/prometheus.yml"]

  grafana:
    image: grafana/grafana:latest
    ports: ["3001:3000"]
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin

  jaeger:
    image: jaegertracing/all-in-one:latest
    ports: ["16686:16686", "14268:14268"]

  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.11.0
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
    ports: ["9200:9200"]

  kibana:
    image: docker.elastic.co/kibana/kibana:8.11.0
    ports: ["5601:5601"]
    environment:
      - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
```

## Option 2: Kubernetes Deployment

### 1. Deploy Namespaces

```bash
# Apply namespace configurations
kubectl apply -f deployment/k8s/namespace/
```

### 2. Deploy Monitoring Stack

```bash
# Deploy monitoring to rag-monitoring namespace
kubectl apply -f deployment/k8s/monitoring/

# Check deployment status
kubectl get pods -n rag-monitoring
```

### 3. Access Services

```bash
# Port forward to local machine
kubectl port-forward -n rag-monitoring svc/grafana 3001:3000 &
kubectl port-forward -n rag-monitoring svc/prometheus 9090:9090 &
kubectl port-forward -n rag-monitoring svc/jaeger 16686:16686 &
kubectl port-forward -n rag-monitoring svc/kibana 5601:5601 &
```

## Option 3: Backend Only (Development)

### 1. Start Backend Services

```bash
cd backend

# Install dependencies
pip install -r requirements.txt

# Start the main application with monitoring
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

# In another terminal, start monitoring services
python -m src.monitoring.main
```

### 2. Start Frontend

```bash
cd frontend

# Install dependencies
npm install

# Start with monitoring enabled
npm run dev
```

## Configuration

### Environment Variables

Create `.env.local` with:

```bash
# Database Configuration
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=rag_production
POSTGRES_USER=rag_user
POSTGRES_PASSWORD=your_password

# Monitoring Configuration
PROMETHEUS_ENABLED=true
PROMETHEUS_PORT=9090
JAEGER_ENABLED=true
JAEGER_ENDPOINT=http://localhost:14268/api/traces

# OpenTelemetry
OTEL_ENABLED=true
OTEL_SERVICE_NAME=rag-system
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317

# Logging
LOG_LEVEL=INFO
STRUCTURED_LOGGING=true

# Performance
MAX_WORKERS=4
WORKER_CONNECTIONS=1000
```

### Prometheus Configuration

Edit `monitoring/prometheus.yml`:

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: 'rag-backend'
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: '/metrics'
    scrape_interval: 10s

  - job_name: 'rag-frontend'
    static_configs:
      - targets: ['localhost:3000']
    metrics_path: '/api/metrics'
    scrape_interval: 30s
```

### Grafana Dashboards

1. **Login to Grafana**: http://localhost:3001 (admin/admin)
2. **Add Prometheus Datasource**:
   - URL: http://prometheus:9090
   - Access: Browser
3. **Import Dashboards**:
   - System Overview
   - Performance Metrics
   - Business Metrics
   - Alert Management

## Using the Monitoring System

### 1. Viewing Metrics

**System Health Dashboard:**
- Overall system status
- Component health indicators
- Active user counts
- Error rates and response times

**Performance Dashboard:**
- Request latency (P50, P90, P95, P99)
- Throughput metrics
- Resource utilization (CPU, Memory, Disk)
- Database performance

### 2. Tracing

**Jaeger UI**: http://localhost:16686
- Search for traces by service name
- Filter by operation, duration, or tags
- View detailed trace spans
- Identify performance bottlenecks

### 3. Log Analysis

**Kibana**: http://localhost:5601
- Create index patterns for logs
- Build visualizations and dashboards
- Search and filter log entries
- Set up log alerts

### 4. Alerting

**AlertManager**: http://localhost:9093
- View active alerts
- Manage alert silences
- Configure notification channels
- Review alert history

## Production Deployment

### 1. Build and Deploy

```bash
# Build Docker images
docker build -t rag-system/backend:latest ./backend
docker build -t rag-system/frontend:latest ./frontend

# Deploy to Kubernetes
kubectl apply -f deployment/k8s/deployments/
kubectl apply -f deployment/k8s/services/
kubectl apply -f deployment/k8s/ingress/
```

### 2. Configure SSL/TLS

```bash
# Install cert-manager
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.0/cert-manager.yaml

# Create SSL certificate
kubectl apply -f deployment/k8s/certificates/
```

### 3. Setup Domain

Update your DNS to point to the load balancer IP:
- `rag.company.com` → Main application
- `grafana.rag.company.com` → Monitoring dashboards
- `api.rag.company.com` → API endpoints

## Troubleshooting

### Common Issues

**1. Services not starting:**
```bash
# Check logs
docker-compose logs monitoring
kubectl logs -n rag-monitoring deployment/prometheus

# Check port conflicts
netstat -tulpn | grep :9090
```

**2. Metrics not appearing:**
```bash
# Check Prometheus targets
curl http://localhost:9090/api/v1/targets

# Check service endpoints
curl http://localhost:8000/metrics
```

**3. Traces not showing:**
```bash
# Check Jaeger health
curl http://localhost:14269/

# Verify OpenTelemetry configuration
grep -r OTEL .env*
```

### Health Checks

```bash
# Backend health
curl http://localhost:8000/health

# Frontend health
curl http://localhost:3000/api/health

# Monitoring stack health
curl http://localhost:9090/-/healthy
curl http://localhost:3001/api/health
```

### Log Locations

- **Docker**: `docker-compose logs -f [service-name]`
- **Kubernetes**: `kubectl logs -n rag-system deployment/[deployment-name] -f`
- **Application**: `/var/log/rag-system/`

## Performance Tuning

### 1. Prometheus Optimization

```yaml
# prometheus.yml
global:
  scrape_interval: 30s  # Reduce frequency for better performance
  evaluation_interval: 30s

storage:
  tsdb:
    retention.time: 30d  # Adjust retention based on disk space
    wal.compression: true
```

### 2. Grafana Optimization

- Enable caching in dashboard settings
- Use simplified queries for high-traffic dashboards
- Set appropriate refresh intervals (30s-5m)

### 3. Resource Limits

```yaml
# deployment/k8s/deployments/
resources:
  requests:
    cpu: 500m
    memory: 1Gi
  limits:
    cpu: 2000m
    memory: 4Gi
```

## Next Steps

1. **Set up automated alerts** for critical metrics
2. **Configure backup strategies** for monitoring data
3. **Integrate with existing monitoring** tools
4. **Create custom dashboards** for business metrics
5. **Set up SLO monitoring** and error budgets

## Support

For issues or questions:
- Check the [troubleshooting guide](MONITORING_TROUBLESHOOTING.md)
- Review [architecture documentation](../observability/observability-architecture.md)
- Contact the DevOps team at devops@company.com
- Join the Slack channel: #rag-monitoring
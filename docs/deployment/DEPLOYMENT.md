# Multimodal Enterprise RAG System - Deployment Guide

> **Status: historical (reconciled 2026-10-17).** This guide was written for earlier deployment generations (Docker Compose, single-host or DigitalOcean/DOKS setups). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Qdrant is removed; live retrieval is PostgreSQL full-text. Start with the [AWS deployment overview](aws-overview.md) and [AWS on-call commands](../operations/aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

This comprehensive guide covers deploying the Multimodal Enterprise RAG System in production environments using modern DevOps practices.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [Environment Setup](#environment-setup)
4. [Local Development](#local-development)
5. [Docker Deployment](#docker-deployment)
6. [Kubernetes Deployment](#kubernetes-deployment)
7. [Terraform Infrastructure](#terraform-infrastructure)
8. [CI/CD Pipeline](#cicd-pipeline)
9. [Feature Flags](#feature-flags)
10. [Monitoring and Alerting](#monitoring-and-alerting)
11. [Security Configuration](#security-configuration)
12. [Troubleshooting](#troubleshooting)
13. [Maintenance](#maintenance)

## Architecture Overview

The Multimodal Enterprise RAG System consists of:

- **Frontend**: React/TypeScript application with Material-UI
- **Backend**: FastAPI Python application with multiple microservices
- **Databases**: PostgreSQL, Neo4j, Redis, Qdrant
- **Background Processing**: Celery workers
- **Monitoring**: Prometheus, Grafana, AlertManager
- **Infrastructure**: AWS EKS, RDS, ElastiCache, S3

## Prerequisites

### Required Tools
- Docker 20.10+
- Docker Compose 2.0+
- Kubernetes 1.28+
- Helm 3.12+
- Terraform 1.5+
- AWS CLI 2.0+
- kubectl 1.28+
- Node.js 18+
- Python 3.11+

### Required Accounts
- AWS account with appropriate permissions
- Docker Hub or GitHub Container Registry
- Domain name (for production)
- SSL certificates (for production)
- Monitoring service accounts (Prometheus, Grafana)
- Feature flags service (LaunchDarkly or similar)

## Environment Setup

### 1. Clone Repository
```bash
git clone https://github.com/your-org/multimodal-rag-system.git
cd multimodal-rag-system
```

### 2. Environment Variables
Copy the example environment file and configure your values:

```bash
cp .env.example .env
```

Configure the following critical variables:
- Database credentials
- API keys (OpenAI, Anthropic)
- SSL certificates
- Monitoring endpoints
- Feature flags configuration

### 3. Setup AWS Credentials
```bash
aws configure
AWS Access Key ID [None]: YOUR_ACCESS_KEY
AWS Secret Access Key [None]: YOUR_SECRET_KEY
Default region name [None]: us-west-2
Default output format [None]: json
```

## Local Development

### 1. Using Docker Compose
```bash
# Start all services
docker-compose up -d

# View logs
docker-compose logs -f

# Stop services
docker-compose down
```

### 2. Manual Setup
```bash
# Backend
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn src.main:app --reload

# Frontend
cd frontend
npm install
npm start
```

### 3. Database Initialization
```bash
# Initialize PostgreSQL
docker exec -it rag-postgres psql -U raguser -d ragdb

# Run migrations
cd backend
alembic upgrade head

# Create indexes
python scripts/create_indexes.py
```

## Docker Deployment

### 1. Build Images
```bash
# Build backend image
docker build -t rag-system/backend:latest .

# Build frontend image
cd frontend
docker build -t rag-system/frontend:latest .
```

### 2. Production Docker Compose
```bash
# Deploy to production
docker-compose -f docker-compose.production.yml up -d

# Scale services
docker-compose -f docker-compose.production.yml up -d --scale celery-worker=4
```

### 3. Health Checks
```bash
# Check service health
docker-compose -f docker-compose.production.yml ps

# Check logs
docker-compose -f docker-compose.production.yml logs -f backend
```

## Kubernetes Deployment

### 1. Prepare Kubernetes Cluster
```bash
# Using Terraform (recommended)
cd terraform
terraform init
terraform plan
terraform apply

# Or create manually with EKS
aws eks create-cluster --name rag-system --version 1.28 --role-arn <role-arn> --resources-vpc-config <vpc-config>
```

### 2. Install Dependencies
```bash
# Install NGINX Ingress Controller
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx --namespace ingress-nginx --create-namespace

# Install Cert-Manager
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.0/cert-manager.yaml

# Install Prometheus Operator
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack --namespace monitoring --create-namespace
```

### 3. Deploy Application
```bash
# Create namespaces
kubectl apply -f k8s/namespace.yaml

# Deploy secrets
kubectl apply -f k8s/secrets.yaml

# Deploy configmaps
kubectl apply -f k8s/configmap.yaml

# Deploy databases
kubectl apply -f k8s/manifests/databases.yaml

# Deploy application
kubectl apply -f k8s/manifests/backend-deployment.yaml
kubectl apply -f k8s/manifests/frontend-deployment.yaml
kubectl apply -f k8s/manifests/celery-deployment.yaml

# Wait for pods to be ready
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=rag-system --timeout=300s
```

### 4. Using Helm
```bash
# Deploy with Helm
helm install rag-system ./k8s/helm/rag-system \
  --namespace rag-system \
  --create-namespace \
  --values ./k8s/helm/rag-system/values.yaml \
  --set global.environment=production

# Upgrade deployment
helm upgrade rag-system ./k8s/helm/rag-system \
  --namespace rag-system \
  --values ./k8s/helm/rag-system/values-prod.yaml

# Rollback
helm rollback rag-system 1 --namespace rag-system
```

## Terraform Infrastructure

### 1. Initialize Terraform
```bash
cd terraform

# Initialize modules
terraform init

# Plan deployment
terraform plan -var-file="environments/staging/terraform.tfvars"

# Apply changes
terraform apply -var-file="environments/staging/terraform.tfvars"
```

### 2. Environment Configuration
```bash
# Staging
cd environments/staging
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with staging values

# Production
cd ../production
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with production values
```

### 3. Infrastructure Outputs
```bash
# Get cluster information
terraform output cluster_name
terraform output cluster_endpoint
terraform output vpc_id

# Configure kubectl
aws eks update-kubeconfig --name $(terraform output -raw cluster_name)
```

## CI/CD Pipeline

### 1. GitHub Actions Setup
```yaml
# .github/workflows/ci-cd.yml is automatically triggered on:
# - Push to main/develop branches
# - Pull requests
# - Releases

# Manual workflow dispatch available for:
# - Feature flag management
# - Production deployments
```

### 2. Quality Gates
- **Code Quality**: Linting, formatting, type checking
- **Security**: Vulnerability scanning, dependency checks
- **Testing**: Unit tests, integration tests, E2E tests
- **Performance**: Load testing, performance thresholds
- **Accessibility**: A11y compliance checks

### 3. Deployment Pipeline
```bash
# Development (automatic on push to develop)
1. Build and test
2. Deploy to staging
3. Run smoke tests
4. Notify team

# Production (on release)
1. Build and test
2. Deploy to production (blue-green)
3. Run smoke tests
4. Monitor health
5. Notify success/failure
```

## Feature Flags

### 1. LaunchDarkly Setup
```bash
# Install SDK
pip install launchdarkly-server-sdk  # Backend
npm install launchdarkly-react-client-sdk  # Frontend

# Configure environment variables
export LAUNCHDARKLY_SDK_KEY="your-sdk-key"
export LAUNCHDARKLY_PROJECT_KEY="your-project-key"
```

### 2. Feature Flag Management
```python
# Backend usage
from src.services.feature_flags import is_multimodal_processing_enabled

if is_multimodal_processing_enabled(user_context):
    # Enable multimodal processing
    process_multimodal_content(file)
```

```typescript
// Frontend usage
import { useFeatureFlag, FeatureFlag } from '../services/featureFlags';

function MultimodalProcessor() {
  const { enabled } = useFeatureFlag(FeatureFlag.MULTIMODAL_PROCESSING);

  if (!enabled) {
    return <div>Feature not available</div>;
  }

  return <MultimodalInterface />;
}
```

### 3. Feature Flag Operations
```bash
# Enable feature for testing
gh workflow run feature-flags.yml \
  --field action=enable \
  --field feature=multimodal-processing \
  --field environment=staging

# Gradual rollout
gh workflow run feature-flags.yml \
  --field action=rollout \
  --field feature=multimodal-processing \
  --field percentage=50 \
  --field environment=production
```

## Monitoring and Alerting

### 1. Prometheus Configuration
```yaml
# monitoring/prometheus/prometheus.yml
# Scrape targets configured for:
# - Application metrics
# - Database metrics
# - Infrastructure metrics
# - Custom business metrics
```

### 2. Grafana Dashboards
```bash
# Import dashboards
kubectl apply -f monitoring/grafana/dashboards/

# Available dashboards:
# - System Overview
# - Application Performance
# - Database Health
# - RAG Quality Metrics
# - Business Metrics
```

### 3. Alert Rules
```yaml
# monitoring/prometheus/rules/rag-system.yml
# Alerts configured for:
# - Service health
# - Performance degradation
# - Resource utilization
# - Security events
# - Business metric thresholds
```

### 4. AlertManager Configuration
```yaml
# monitoring/alertmanager/alertmanager.yml
# Notification channels:
# - Email alerts
# - Slack notifications
# - PagerDuty integration
# - Custom webhooks
```

## Security Configuration

### 1. SSL/TLS Setup
```bash
# Generate SSL certificates (Let's Encrypt)
certbot certonly --webroot -w /var/www/html -d rag.yourdomain.com

# Create Kubernetes secret
kubectl create secret tls rag-tls \
  --cert=/path/to/cert.pem \
  --key=/path/to/key.pem \
  --namespace rag-system
```

### 2. Network Policies
```yaml
# Restrict traffic between services
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: rag-system-netpol
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: rag-system
  policyTypes:
  - Ingress
  - Egress
```

### 3. Pod Security Policies
```yaml
# Security contexts for pods
securityContext:
  runAsNonRoot: true
  runAsUser: 1001
  fsGroup: 1001
  capabilities:
    drop:
    - ALL
```

### 4. Secrets Management
```bash
# Create secrets
kubectl create secret generic rag-secrets \
  --from-literal=db-password=your-password \
  --from-literal=secret-key=your-secret \
  --namespace rag-system

# Use with IRSA (IAM Roles for Service Accounts)
# Configure in Terraform modules/irsa/
```

## Troubleshooting

### 1. Common Issues

#### Service Not Starting
```bash
# Check pod status
kubectl get pods -n rag-system

# Check pod logs
kubectl logs -f deployment/backend -n rag-system

# Check events
kubectl get events -n rag-system --sort-by='.lastTimestamp'
```

#### Database Connection Issues
```bash
# Test database connectivity
kubectl exec -it deployment/backend -n rag-system -- python -c "
import psycopg2
conn = psycopg2.connect('postgresql://raguser:password@postgres:5432/ragdb')
print('Connection successful')
"

# Check database logs
kubectl logs -f deployment/postgres -n rag-system
```

#### High Resource Usage
```bash
# Check resource usage
kubectl top pods -n rag-system
kubectl top nodes

# Check HPA status
kubectl get hpa -n rag-system
kubectl describe hpa backend-hpa -n rag-system
```

### 2. Performance Issues
```bash
# Check response times
curl -w "@curl-format.txt" -o /dev/null -s https://rag.yourdomain.com/api/v1/health

# Monitor database queries
kubectl exec -it deployment/postgres -n rag-system -- psql -U raguser -d ragdb -c "
SELECT query, calls, total_time, mean_time
FROM pg_stat_statements
ORDER BY total_time DESC LIMIT 10;"
```

### 3. Memory Leaks
```bash
# Check memory usage
kubectl exec -it deployment/backend -n rag-system -- python -c "
import psutil
import os
process = psutil.Process(os.getpid())
print(f'Memory: {process.memory_info().rss / 1024 / 1024:.2f} MB')
"

# Monitor memory trends
kubectl logs -f deployment/backend -n rag-system | grep memory
```

## Maintenance

### 1. Regular Tasks

#### Daily
- Check alert status
- Review system metrics
- Verify backup completion
- Monitor error rates

#### Weekly
- Review and update dependencies
- Check disk usage
- Review security advisories
- Performance tuning

#### Monthly
- Update Kubernetes versions
- Review and rotate secrets
- Audit access controls
- Cost optimization review

### 2. Backup Procedures
```bash
# Database backup
kubectl exec -it deployment/postgres -n rag-system -- pg_dump ragdb > backup-$(date +%Y%m%d).sql

# Vector store backup
kubectl exec -it deployment/qdrant -n rag-system -- cp -r /qdrant/storage /backup/qdrant-$(date +%Y%m%d)

# Application configuration backup
kubectl get configmaps,secrets -n rag-system -o yaml > config-backup-$(date +%Y%m%d).yaml
```

### 3. Updates and Upgrades
```bash
# Application update
helm upgrade rag-system ./k8s/helm/rag-system \
  --namespace rag-system \
  --values ./k8s/helm/rag-system/values.yaml

# Kubernetes upgrade
# Update Terraform configuration
terraform apply -var-file="environments/production/terraform.tfvars"

# Database maintenance
kubectl exec -it deployment/postgres -n rag-system -- psql -U raguser -d ragdb -c "VACUUM ANALYZE;"
```

### 4. Disaster Recovery
```bash
# Restore from backup
kubectl exec -i deployment/postgres -n rag-system -- psql -U raguser -d ragdb < backup-20240101.sql

# Restore vector store
kubectl cp backup/qdrant-20240101 deployment/qdrant-ns-rag-system:/qdrant/storage/

# Recreate infrastructure
terraform apply -var-file="environments/production/terraform.tfvars" -replace="module.eks"
```

## Support and Escalation

### 1. Contact Information
- **DevOps Team**: devops@rag-system.com
- **Development Team**: dev@rag-system.com
- **On-call Rotation**: +1-555-RAG-SUPPORT

### 2. Escalation Levels
- **Level 1**: Basic troubleshooting, restart services
- **Level 2**: Debugging, configuration changes
- **Level 3**: Code issues, infrastructure changes
- **Level 4**: Security incidents, major outages

### 3. Documentation
- [API Documentation](https://docs.rag-system.com/api)
- [Architecture Guide](https://docs.rag-system.com/architecture)
- [Development Guide](https://docs.rag-system.com/development)
- [Troubleshooting Guide](https://docs.rag-system.com/troubleshooting)

---

This deployment guide provides comprehensive instructions for deploying and maintaining the Multimodal Enterprise RAG System. For specific issues or questions, please refer to the troubleshooting section or contact the support team.
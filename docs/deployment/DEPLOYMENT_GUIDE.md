# Deployment Guide - Multimodal Enterprise RAG System

> **Status: historical (reconciled 2026-10-17).** This guide was written for earlier deployment generations (Docker Compose, single-host or DigitalOcean/DOKS setups). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Qdrant is removed; live retrieval is PostgreSQL full-text. Start with the [AWS deployment overview](aws-overview.md) and [AWS on-call commands](../operations/aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

This guide provides comprehensive instructions for deploying and operating the Multimodal Enterprise RAG system in production environments.

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Environment Setup](#environment-setup)
4. [Deployment Methods](#deployment-methods)
5. [Monitoring and Observability](#monitoring-and-observability)
6. [Backup and Recovery](#backup-and-recovery)
7. [Security Considerations](#security-considerations)
8. [Troubleshooting](#troubleshooting)
9. [Maintenance Procedures](#maintenance-procedures)

## Overview

The Multimodal Enterprise RAG system supports multiple deployment strategies:

- **Development**: Local development with hot reload
- **Staging**: Production-like environment for testing
- **Production**: High-availability, scalable deployment
- **Security Testing**: Dedicated environment for security testing

### Architecture Components

- **Backend**: FastAPI application with Celery workers
- **Frontend**: React TypeScript application
- **Database**: PostgreSQL for relational data
- **Cache**: Redis for caching and session management
- **Knowledge Graph**: Neo4j for entity relationships
- **Vector Store**: Qdrant for semantic search
- **Monitoring**: Prometheus, Grafana, AlertManager
- **Logging**: ELK stack (Elasticsearch, Logstash, Kibana)

## Prerequisites

### System Requirements

#### Minimum Requirements
- **CPU**: 4 cores
- **Memory**: 8GB RAM
- **Storage**: 100GB SSD
- **Network**: 1Gbps

#### Production Requirements
- **CPU**: 8+ cores
- **Memory**: 32GB+ RAM
- **Storage**: 500GB+ SSD
- **Network**: 10Gbps

### Software Dependencies

- Docker 20.10+
- Docker Compose 2.0+
- Git 2.30+
- Node.js 18+ (for local development)
- Python 3.11+ (for local development)

### External Services

- OpenAI API key (for AI processing)
- Anthropic API key (for AI processing)
- Cloud storage account (AWS S3, Google Cloud Storage, or Azure Blob)
- Email service (SMTP) for notifications
- Slack webhook (optional) for notifications

## Environment Setup

### 1. Clone Repository

```bash
git clone https://github.com/your-org/multimodal-rag.git
cd multimodal-rag
```

### 2. Environment Configuration

Create environment-specific configuration files:

```bash
# Development
cp .env.example .env.development

# Staging
cp .env.example .env.staging

# Production
cp .env.example .env.production
```

### 3. Configure Environment Variables

Edit each environment file with appropriate values:

#### Required Variables
```bash
# Database
DATABASE_URL=postgresql://user:password@host:5432/dbname
REDIS_URL=redis://host:6379

# Neo4j
NEO4J_URI=bolt://host:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=password

# Qdrant
QDRANT_URL=http://host:6333
QDRANT_API_KEY=your-api-key

# Security
SECRET_KEY=your-secret-key-here

# AI Services
OPENAI_API_KEY=your-openai-key
ANTHROPIC_API_KEY=your-anthropic-key
```

#### Optional Variables
```bash
# Monitoring
SENTRY_DSN=your-sentry-dsn
NEW_RELIC_LICENSE_KEY=your-newrelic-key

# Notifications
SLACK_WEBHOOK_URL=your-slack-webhook
SMTP_SERVER=smtp.example.com
SMTP_USER=user@example.com
SMTP_PASSWORD=smtp-password

# Cloud Storage
CLOUD_STORAGE_ENABLED=true
CLOUD_STORAGE_PROVIDER=aws
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_S3_BUCKET=your-bucket-name
```

### 4. SSL/TLS Configuration

For production deployments, configure SSL certificates:

```bash
# Create SSL directory
mkdir -p nginx/ssl

# Copy your certificates
cp your-cert.pem nginx/ssl/
cp your-key.pem nginx/ssl/
```

## Deployment Methods

### Method 1: Automated Deployment Script

The deployment script provides a simple, automated way to deploy the system:

```bash
# Deploy to staging
./scripts/deploy.sh deploy

# Deploy to production
ENVIRONMENT=production ./scripts/deploy.sh deploy

# Blue-green deployment
./scripts/deploy.sh deploy-blue-green

# Rollback
./scripts/deploy.sh rollback
```

### Method 2: Docker Compose

#### Development Environment

```bash
# Start all services
docker-compose -f docker-compose.development.yml up -d

# View logs
docker-compose -f docker-compose.development.yml logs -f

# Stop services
docker-compose -f docker-compose.development.yml down
```

#### Staging Environment

```bash
# Start staging services
docker-compose -f docker-compose.staging.yml up -d

# With monitoring stack
docker-compose -f docker-compose.staging.yml --profile monitoring up -d
```

#### Production Environment

```bash
# Start production services
docker-compose -f docker-compose.production.yml up -d

# With full monitoring stack
docker-compose -f docker-compose.production.yml --profile monitoring up -d
```

### Method 3: CI/CD Pipeline

The system includes comprehensive GitHub Actions workflows:

#### Automated Pipeline Triggers
- Push to `develop` branch → Deploy to staging
- Push to `main` branch → Deploy to production
- Create release → Full production deployment

#### Pipeline Stages
1. **Code Quality**: Linting, formatting, type checking
2. **Security Scanning**: Vulnerability scanning, dependency checks
3. **Testing**: Unit tests, integration tests, E2E tests
4. **Build**: Docker image building and optimization
5. **Deploy**: Blue-green deployment with rollback capability
6. **Verification**: Smoke tests, health checks

#### Manual Deployment

```bash
# Create and push release tag
git tag v1.0.0
git push origin v1.0.0

# This will trigger the production deployment pipeline
```

## Monitoring and Observability

### Health Checks

The system exposes two unauthenticated probe endpoints:

- **Liveness Probe**: `GET /health`
- **Readiness Probe**: `GET /health/readiness`

Use authenticated service endpoints, application logs, and infrastructure
monitoring for component diagnosis. The public probes intentionally do not
expose dependency details.

### Metrics Collection

#### Prometheus Metrics
- Application metrics (requests, response times, error rates)
- Business metrics (document processing, search performance)
- System metrics (CPU, memory, disk usage)
- Database metrics (connections, query performance)

#### Grafana Dashboards
- **System Overview**: Overall system health and performance
- **Business Metrics**: Document processing and usage analytics
- **Application Performance**: Request rates, response times, error rates
- **Infrastructure**: Resource utilization and capacity planning

### Alerting

#### AlertManager Configuration
Alerts are configured for:

- **Critical Alerts** (immediate notification)
  - Service down
  - Database connection failure
  - High error rates (>5%)
  - Security incidents

- **Warning Alerts** (notification within 5 minutes)
  - High resource usage (>80%)
  - Degraded performance
  - Backup failures

#### Notification Channels
- **Slack**: Real-time alerts to designated channels
- **Email**: Detailed alert summaries
- **PagerDuty**: Critical alerts (configurable)

### Log Aggregation

#### ELK Stack Configuration
- **Elasticsearch**: Log storage and indexing
- **Logstash**: Log processing and enrichment
- **Kibana**: Log visualization and analysis

#### Log Formats
- **Structured JSON**: Consistent format for easy parsing
- **Correlation IDs**: Request tracing across services
- **Log Levels**: DEBUG, INFO, WARNING, ERROR, CRITICAL

## Backup and Recovery

### Automated Backups

#### Database Backups
- **Frequency**: Every 6 hours
- **Retention**: 30 days
- **Storage**: Local and cloud (S3/GCS/Azure)
- **Compression**: gzip compression

#### Vector Store Backups
- **Frequency**: Daily
- **Retention**: 90 days
- **Format**: Compressed JSON with metadata
- **Verification**: Automatic integrity checks

#### File Backups
- **Frequency**: Daily
- **Content**: Uploaded files, configurations
- **Retention**: 30 days
- **Encryption**: AES-256 encryption

### Manual Backup

```bash
# Database backup
./scripts/backup/backup_database.sh

# Vector store backup
python3 ./scripts/backup/backup_vector_store.py

# Full system backup
./scripts/backup/backup_full_system.sh
```

### Disaster Recovery

#### Recovery Procedures

1. **Assessment**: Determine scope of failure
2. **Isolation**: Isolate affected components
3. **Recovery**: Restore from backups
4. **Verification**: Validate system integrity
5. **Monitoring**: Enhanced monitoring post-recovery

#### Automated Recovery

```bash
# Full disaster recovery
python3 ./scripts/backup/disaster_recovery.py

# Component-specific recovery
python3 ./scripts/backup/disaster_recovery.py --components database vector_store

# Dry run (simulation)
python3 ./scripts/backup/disaster_recovery.py --dry-run
```

#### Recovery Time Objectives (RTO)
- **Database**: 2 hours
- **Vector Store**: 4 hours
- **Application**: 1 hour
- **Full System**: 6 hours

#### Recovery Point Objectives (RPO)
- **Database**: 15 minutes
- **Vector Store**: 24 hours
- **Files**: 24 hours

## Security Considerations

### Application Security

#### Authentication & Authorization
- JWT-based authentication
- Role-based access control (RBAC)
- Session management with Redis
- API key authentication for external services

#### Data Protection
- Encryption at rest (AES-256)
- Encryption in transit (TLS 1.3)
- Data masking for sensitive information
- GDPR compliance features

#### Input Validation
- File type validation
- Size limits and quotas
- Virus scanning with ClamAV
- SQL injection prevention

### Infrastructure Security

#### Network Security
- Docker network isolation
- Firewall configuration
- VPN access for administration
- Network segmentation

#### Container Security
- Non-root user execution
- Read-only filesystems where possible
- Resource limits and constraints
- Security scanning of images

#### Secrets Management
- Environment variables for configuration
- Encrypted secret storage
- Key rotation policies
- Audit logging

### Compliance

#### Security Standards
- SOC 2 Type II compliance
- ISO 27001 alignment
- PCI DSS considerations
- HIPAA compliance (if applicable)

#### Audit Trail
- Comprehensive logging
- Access logs
- Change tracking
- Tamper-evident logs

## Troubleshooting

### Common Issues

#### Service Startup Failures

```bash
# Check service status
docker-compose ps

# View service logs
docker-compose logs backend

# Check resource usage
docker stats

# Restart specific service
docker-compose restart backend
```

#### Database Connection Issues

```bash
# Test database connectivity
docker-compose exec backend python -c "
import asyncpg
import asyncio

async def test():
    conn = await asyncpg.connect('postgresql://postgres:postgres@postgres:5432/multimodal_rag')
    result = await conn.fetchval('SELECT 1')
    print('Database connection successful')

asyncio.run(test())
"
```

#### Performance Issues

```bash
# Check system resources
docker-compose exec backend top
docker-compose exec backend iostat -x 1

# Check database performance
docker-compose exec postgres psql -U postgres -d multimodal_rag -c "
SELECT query, calls, total_time, mean_time
FROM pg_stat_statements
ORDER BY total_time DESC
LIMIT 10;
"
```

#### Memory Issues

```bash
# Check memory usage
docker stats --no-stream

# Analyze memory leaks
docker-compose exec backend python -c "
import tracemalloc
tracemalloc.start()

# Your code here

snapshot = tracemalloc.take_snapshot()
top_stats = snapshot.statistics('lineno')
for stat in top_stats[:10]:
    print(stat)
"
```

### Debug Mode

Enable debug logging for troubleshooting:

```bash
# Set debug mode
export DEBUG=true
export LOG_LEVEL=DEBUG

# Restart with debug
docker-compose down
docker-compose up -d
```

### Health Check Failures

```bash
# Manual health check
curl -fsS http://localhost:8000/health

# Dependency readiness (database and Redis)
curl -fsS http://localhost:8000/health/readiness

# Authenticated component checks
curl -fsS -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/workers/health
curl -fsS -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/knowledge-graph/health

# Diagnose remaining failures from backend logs
docker-compose logs --tail=200 backend
```

## Maintenance Procedures

### Routine Maintenance

#### Daily Tasks
- Review system alerts
- Check backup status
- Monitor resource utilization
- Review security logs

#### Weekly Tasks
- Update security patches
- Review performance metrics
- Clean up old logs and temporary files
- Verify disaster recovery procedures

#### Monthly Tasks
- Security vulnerability scanning
- Capacity planning review
- Backup restoration testing
- Performance optimization

### Updates and Upgrades

#### Application Updates

```bash
# Update application code
git pull origin main

# Update Docker images
docker-compose pull

# Redeploy with zero downtime
./scripts/deploy.sh deploy-blue-green
```

#### System Updates

```bash
# Update Docker images
docker-compose pull

# Update system packages
sudo apt update && sudo apt upgrade

# Restart services
./scripts/deploy.sh deploy
```

#### Database Maintenance

```bash
# Database backup (before maintenance)
./scripts/backup/backup_database.sh

# Database maintenance
docker-compose exec postgres psql -U postgres -d multimodal_rag -c "
VACUUM ANALYZE;
REINDEX DATABASE multimodal_rag;
"
```

### Performance Tuning

#### Database Optimization
```sql
-- Index analysis
EXPLAIN ANALYZE SELECT * FROM documents WHERE content LIKE '%search%';

-- Create missing indexes
CREATE INDEX CONCURRENTLY idx_documents_content_gin ON documents USING gin(to_tsvector('english', content));

-- Update statistics
ANALYZE documents;
```

#### Cache Optimization
```bash
# Redis memory optimization
docker-compose exec redis redis-cli CONFIG SET maxmemory-policy allkeys-lru

# Clear cache if needed
docker-compose exec redis redis-cli FLUSHDB
```

#### Application Optimization
- Enable query result caching
- Optimize vector search parameters
- Tune Celery worker concurrency
- Adjust connection pool sizes

### Capacity Planning

#### Scaling Guidelines
- **CPU**: >70% utilization → scale up
- **Memory**: >80% utilization → scale up
- **Storage**: <20% free → scale up
- **Network**: >80% bandwidth → scale up

#### Monitoring Metrics
- Request throughput
- Response time percentiles
- Error rates
- Resource utilization
- Queue depths

## Emergency Procedures

### Incident Response

1. **Detection**
   - Alert notification
   - Initial assessment
   - Severity determination

2. **Response**
   - Incident team activation
   - Communication plan execution
   - Mitigation procedures

3. **Resolution**
   - Problem isolation
   - Fix implementation
   - Service restoration

4. **Post-Incident**
   - Root cause analysis
   - Process improvement
   - Documentation updates

### Escalation Procedures

#### Level 1 (L1) - Operations Team
- Monitoring and alerting
- Basic troubleshooting
- Service restarts

#### Level 2 (L2) - Engineering Team
- Advanced troubleshooting
- Code deployments
- Configuration changes

#### Level 3 (L3) - Architecture Team
- Complex problem resolution
- Architecture changes
- Performance optimization

### Communication Protocols

#### Internal Communication
- Slack channels for real-time updates
- Incident tracking system
- Status page updates

#### External Communication
- Customer notifications
- Stakeholder updates
- Post-incident reports

## Support and Contact Information

### Technical Support
- **Email**: support@yourcompany.com
- **Slack**: #support-multimodal-rag
- **PagerDuty**: Available for critical incidents

### Documentation
- **API Documentation**: https://docs.yourcompany.com/api
- **Architecture Guide**: https://docs.yourcompany.com/architecture
- **Runbooks**: https://docs.yourcompany.com/runbooks

### Training Resources
- **Operator Training**: Monthly sessions
- **Developer Onboarding**: Bi-weekly sessions
- **Security Training**: Quarterly sessions

---

For additional assistance or questions, contact the DevOps team at devops@yourcompany.com.

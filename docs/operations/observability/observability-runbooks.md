# Observability Runbooks

> **Status: design record (reconciled 2026-10-17).** This document describes an observability design or setup; whether it is deployed on the AWS cluster is not verified. Alerting that Terraform defines for AWS resources is in [infrastructure/terraform/monitoring.tf](../../../infrastructure/terraform/monitoring.tf). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Qdrant is removed; live retrieval is PostgreSQL full-text. Start with the [AWS deployment overview](../../deployment/aws-overview.md) and [AWS on-call commands](../aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

## Overview

This document contains operational runbooks for common scenarios and incidents in the RAG System observability stack. Each runbook provides step-by-step procedures for identifying, diagnosing, and resolving issues.

## Table of Contents

1. [General Troubleshooting](#general-troubleshooting)
2. [Performance Issues](#performance-issues)
3. [Quality Degradation](#quality-degradation)
4. [Infrastructure Problems](#infrastructure-problems)
5. [Security Incidents](#security-incidents)
6. [Data Issues](#data-issues)
7. [Maintenance Procedures](#maintenance-procedures)

---

## General Troubleshooting

### RB001: Service Unavailable

**Symptoms:**
- Services showing as down in monitoring dashboards
- HTTP 503 errors
- Health check failures

**Impact:**
- Complete service outage
- User inability to access the system

**Diagnosis:**
1. Check Grafana System Overview dashboard
2. Verify service health endpoints
3. Check container status
4. Review recent deployments

**Resolution Steps:**

#### Step 1: Verify Service Status
```bash
# Check container status
docker-compose ps

# Check individual service health
docker-compose exec backend curl -f http://localhost:8000/health
docker-compose exec frontend curl -f http://localhost:3000
```

#### Step 2: Check Logs
```bash
# Check application logs
docker-compose logs backend
docker-compose logs frontend

# Check system logs
docker-compose logs | grep ERROR
```

#### Step 3: Verify Dependencies
```bash
# Check database connectivity
docker-compose exec postgres pg_isready -U raguser -d ragdb

# Check Redis connectivity
docker-compose exec redis redis-cli ping

# Check Neo4j connectivity
docker-compose exec neo4j cypher-shell -u neo4j -p password "RETURN 1"
```

#### Step 4: Restart Services if Needed
```bash
# Restart specific service
docker-compose restart backend

# Restart all services
docker-compose restart
```

**Escalation Criteria:**
- Service doesn't recover after restart
- Multiple services affected
- Database connectivity issues

### RB002: High Error Rate

**Symptoms:**
- Increased 5xx HTTP status codes
- Application exceptions
- User complaints about errors

**Impact:**
- Degraded user experience
- Potential data corruption
- System instability

**Diagnosis:**
1. Check Error Rate dashboard in Grafana
2. Review application logs for error patterns
3. Check recent changes or deployments
4. Verify system resources

**Resolution Steps:**

#### Step 1: Identify Error Pattern
```sql
-- Check error patterns in Elasticsearch
GET rag-system-logs-*/_search
{
  "query": {
    "bool": {
      "must": [
        {"term": {"log_level": "ERROR"}},
        {"range": {"@timestamp": {"gte": "now-1h"}}}
      ]
    }
  },
  "aggs": {
    "error_types": {
      "terms": {"field": "rag_log.error_type"}
    }
  }
}
```

#### Step 2: Check Resource Utilization
```bash
# Check system resources
docker stats

# Check disk space
df -h

# Check memory usage
free -h
```

#### Step 3: Review Recent Changes
```bash
# Check recent deployments
git log --oneline -10

# Check recent configuration changes
git status
```

#### Step 4: Common Error Fixes
```bash
# If database connection errors:
docker-compose restart postgres

# If Redis connection errors:
docker-compose restart redis

# If memory issues:
docker-compose down
docker system prune -f
docker-compose up -d
```

**Escalation Criteria:**
- Error rate > 10% for more than 5 minutes
- Database corruption suspected
- Security-related errors

---

## Performance Issues

### RB003: High Latency

**Symptoms:**
- Slow response times
- User complaints about slowness
- Dashboard alerts for high p95 latency

**Impact:**
- Poor user experience
- Potential timeout errors
- Reduced system throughput

**Diagnosis:**
1. Check Response Time dashboard
2. Analyze trace data in Jaeger/Tempo
3. Review database query performance
4. Check system resource utilization

**Resolution Steps:**

#### Step 1: Identify Bottlenecks
```bash
# Check slow queries in PostgreSQL
docker-compose exec postgres psql -U raguser -d ragdb -c "
SELECT query, mean_time, calls, total_time
FROM pg_stat_statements
ORDER BY mean_time DESC
LIMIT 10;
"
```

#### Step 2: Analyze Traces
1. Open Jaeger UI: http://localhost:16686
2. Search for traces with high duration
3. Identify slow operations
4. Check span details for bottlenecks

#### Step 3: Check Resource Contention
```bash
# Check CPU usage
top

# Check I/O wait
iostat -x 1

# Check network latency
ping google.com
```

#### Step 4: Performance Tuning
```bash
# Optimize database connections
docker-compose exec backend curl -X POST http://localhost:8000/admin/db-optimize

# Clear cache if needed
docker-compose exec redis redis-cli FLUSHDB

# Restart services with proper resource limits
docker-compose down
docker-compose up -d --scale backend=2
```

**Escalation Criteria:**
- p95 latency > 5 seconds for more than 10 minutes
- Database queries consistently slow
- System resources exhausted

### RB004: Search Performance Degradation

**Symptoms:**
- Slow search responses
- Poor search results
- High vector search latency

**Impact:**
- Degraded search functionality
- User frustration
- Reduced system utility

**Diagnosis:**
1. Check Search Performance dashboard
2. Analyze vector search metrics
3. Review embedding service performance
4. Check Qdrant cluster health

**Resolution Steps:**

#### Step 1: Check Vector Database
```bash
# Check Qdrant cluster status
curl http://localhost:6333/cluster

# Check collection statistics
curl http://localhost:6333/collections/documents
```

#### Step 2: Optimize Search Configuration
```python
# Adjust search parameters in application
search_config = {
    "vector_size": 768,
    "search_limit": 10,
    "score_threshold": 0.7,
    "rerank": True
}
```

#### Step 3: Rebuild Index if Needed
```bash
# Trigger index rebuild
docker-compose exec backend python -c "
from src.services.vector_service import VectorService
vs = VectorService()
vs.rebuild_index()
"
```

**Escalation Criteria:**
- Search latency > 10 seconds
- Search returning no results
- Vector database corruption suspected

---

## Quality Degradation

### RB005: RAG Quality Metrics Low

**Symptoms:**
- Low answer relevancy scores
- Poor faithfulness metrics
- User complaints about answer quality

**Impact:**
- Reduced system effectiveness
- User dissatisfaction
- Potential compliance issues

**Diagnosis:**
1. Check RAG Quality dashboard
2. Analyze quality score trends
3. Review recent document additions
4. Check model performance

**Resolution Steps:**

#### Step 1: Identify Quality Issues
```sql
-- Check recent quality scores
SELECT
  AVG(answer_relevancy) as avg_relevancy,
  AVG(faithfulness) as avg_faithfulness,
  AVG(contextual_relevancy) as avg_contextual
FROM quality_metrics
WHERE created_at > NOW() - INTERVAL '1 hour';
```

#### Step 2: Review Data Quality
```bash
# Check document processing status
docker-compose exec backend python -c "
from src.models.document import Document
docs = Document.query.filter_by(status='processed').limit(10)
for doc in docs:
    print(f'Document {doc.id}: Quality={doc.quality_score}')
"
```

#### Step 3: Retrain Models if Needed
```bash
# Trigger model retraining
docker-compose exec backend python -c "
from src.services.model_service import ModelService
ms = ModelService()
ms.retrain_models()
"
```

#### Step 4: Adjust Quality Thresholds
```python
# Update quality thresholds
QUALITY_THRESHOLDS = {
    "answer_relevancy": 0.7,
    "faithfulness": 0.9,
    "contextual_relevancy": 0.7
}
```

**Escalation Criteria:**
- Quality scores below 50% for extended period
- Complete loss of quality metrics
- Model training failures

---

## Infrastructure Problems

### RB006: Database Issues

**Symptoms:**
- Database connection errors
- Slow query performance
- Database unavailability

**Impact:**
- Complete system outage
- Data inconsistency
- Loss of functionality

**Diagnosis:**
1. Check database connectivity
2. Review database logs
3. Analyze query performance
4. Check resource utilization

**Resolution Steps:**

#### Step 1: Verify Database Status
```bash
# Check PostgreSQL status
docker-compose exec postgres pg_isready -U raguser -d ragdb

# Check database size and usage
docker-compose exec postgres psql -U raguser -d ragdb -c "
SELECT pg_size_pretty(pg_database_size('ragdb'));
"
```

#### Step 2: Check for Locks
```sql
-- Check for long-running locks
SELECT
  pid,
  now() - pg_stat_activity.query_start AS duration,
  query,
  state
FROM pg_stat_activity
WHERE (now() - pg_stat_activity.query_start) > interval '5 minutes';
```

#### Step 3: Restart Database if Needed
```bash
# Graceful database restart
docker-compose restart postgres

# Check recovery progress
docker-compose logs postgres | tail -20
```

#### Step 4: Database Maintenance
```bash
# Run VACUUM and ANALYZE
docker-compose exec postgres psql -U raguser -d ragdb -c "VACUUM ANALYZE;"

# Update statistics
docker-compose exec postgres psql -U raguser -d ragdb -c "ANALYZE;"
```

**Escalation Criteria:**
- Database corruption suspected
- Data loss
- Recovery fails

### RB007: Vector Database Issues

**Symptoms:**
- Qdrant connection errors
- Vector search failures
- Index corruption

**Impact:**
- Search functionality unavailable
- Reduced system capabilities
- Data inconsistency

**Diagnosis:**
1. Check Qdrant service status
2. Verify cluster health
3. Review collection status
4. Check disk space

**Resolution Steps:**

#### Step 1: Check Qdrant Status
```bash
# Check service health
curl http://localhost:6333/health

# Check cluster status
curl http://localhost:6333/cluster

# Check collections
curl http://localhost:6333/collections
```

#### Step 2: Repair Collections if Needed
```bash
# Recreate corrupted collection
curl -X DELETE http://localhost:6333/collections/documents

# Re-create collection
curl -X PUT http://localhost:6333/collections/documents \
  -H 'Content-Type: application/json' \
  -d '{
    "vectors": {
      "size": 768,
      "distance": "Cosine"
    }
  }'
```

#### Step 3: Rebuild Index
```bash
# Trigger vector reindexing
docker-compose exec backend python -c "
from src.services.vector_service import VectorService
vs = VectorService()
vs.reindex_all_documents()
"
```

**Escalation Criteria:**
- Complete vector database failure
- Data corruption
- Index recovery fails

---

## Security Incidents

### RB008: Security Breach Detected

**Symptoms:**
- Suspicious authentication patterns
- Unauthorized access attempts
- Security alert triggers

**Impact:**
- Potential data breach
- System compromise
- Compliance violations

**Diagnosis:**
1. Review security logs
2. Check authentication patterns
3. Analyze access logs
4. Verify system integrity

**Resolution Steps:**

#### Step 1: Immediate Containment
```bash
# Block suspicious IPs
iptables -A INPUT -s <SUSPICIOUS_IP> -j DROP

# Disable compromised accounts
docker-compose exec backend python -c "
from src.services.auth_service import AuthService
auth = AuthService()
auth.disable_user('compromised_user_id')
"
```

#### Step 2: Investigate Breach
```bash
# Check authentication logs
grep "authentication" /app/logs/application.log | tail -50

# Check access patterns
grep "suspicious" /app/logs/security.log
```

#### Step 3: Security Hardening
```bash
# Force password reset
docker-compose exec backend python -c "
from src.services.auth_service import AuthService
auth = AuthService()
auth.force_password_reset_for_all_users()
"

# Update security policies
# Review and update firewall rules
# Audit user permissions
```

#### Step 4: Report and Document
- Document incident details
- Notify stakeholders
- File security report
- Plan prevention measures

**Escalation Criteria:**
- Confirmed data breach
- System compromise
- Regulatory reporting required

---

## Data Issues

### RB009: Data Corruption

**Symptoms:**
- Inconsistent query results
- Missing data
- Data validation failures

**Impact:**
- System unreliability
- User data loss
- Compliance issues

**Diagnosis:**
1. Check data consistency
2. Review recent data operations
3. Verify backup integrity
4. Analyze error logs

**Resolution Steps:**

#### Step 1: Identify Corrupted Data
```sql
-- Check for data inconsistencies
SELECT COUNT(*) FROM documents WHERE content IS NULL;
SELECT COUNT(*) FROM vectors WHERE embedding IS NULL;
```

#### Step 2: Restore from Backup
```bash
# Restore database from backup
docker-compose exec postgres psql -U raguser -d ragdb < backup.sql

# Restore vector data
docker-compose exec backend python -c "
from src.services.backup_service import BackupService
bs = BackupService()
bs.restore_vectors('backup_file.json')
"
```

#### Step 3: Rebuild Corrupted Data
```bash
# Re-process documents
docker-compose exec backend python -c "
from src.services.processing_service import ProcessingService
ps = ProcessingService()
ps.reprocess_failed_documents()
"

# Re-generate embeddings
docker-compose exec backend python -c "
from src.services.vector_service import VectorService
vs = VectorService()
vs.regenerate_missing_embeddings()
"
```

**Escalation Criteria:**
- Widespread data corruption
- Backup restoration fails
- Data loss exceeds acceptable limits

---

## Maintenance Procedures

### RB010: System Maintenance

**Purpose**: Perform routine system maintenance to ensure optimal performance and reliability.

**Frequency**: Weekly for basic maintenance, monthly for deep maintenance.

**Procedure**:

#### Basic Maintenance (Weekly)
```bash
# 1. Clean up old logs
find /app/logs -name "*.log" -mtime +7 -delete

# 2. Update system packages
apt-get update && apt-get upgrade -y

# 3. Check disk space
df -h

# 4. Review system logs
journalctl --since "7 days ago" --priority err

# 5. Backup configurations
tar -czf config-backup-$(date +%Y%m%d).tar.gz /etc/rag-system/
```

#### Deep Maintenance (Monthly)
```bash
# 1. Database maintenance
docker-compose exec postgres psql -U raguser -d ragdb -c "
VACUUM FULL;
ANALYZE;
REINDEX DATABASE ragdb;
"

# 2. Vector optimization
docker-compose exec backend python -c "
from src.services.vector_service import VectorService
vs = VectorService()
vs.optimize_index()
"

# 3. Clear old metrics
curl -X DELETE http://localhost:9090/api/v1/admin/tsdb/delete?match[]={__name__=~\".*\"}&start=2024-01-01T00:00:00Z&end=2024-02-01T00:00:00Z

# 4. Update SSL certificates
# Update as needed based on certificate provider

# 5. Security audit
# Run security scanning tools
# Check for vulnerabilities
# Update dependencies
```

### RB011: Backup and Recovery

**Purpose**: Ensure data backup and recovery procedures are working correctly.

**Frequency**: Daily for automated backups, quarterly for recovery testing.

**Procedure**:

#### Automated Backup (Daily)
```bash
# Database backup
docker-compose exec postgres pg_dump -U raguser ragdb > backup-$(date +%Y%m%d).sql

# Vector backup
docker-compose exec backend python -c "
from src.services.backup_service import BackupService
bs = BackupService()
bs.backup_vectors('vectors-$(date +%Y%m%d).json')
"

# Upload to cloud storage (if configured)
aws s3 cp backup-$(date +%Y%m%d).sql s3://rag-system-backups/
```

#### Recovery Testing (Quarterly)
```bash
# 1. Create test environment
docker-compose -f docker-compose.test.yml up -d

# 2. Restore backup to test environment
docker-compose exec postgres-test psql -U raguser ragdb < backup-$(date +%Y%m%d).sql

# 3. Verify data integrity
docker-compose exec backend-test python -c "
from src.services.validation_service import ValidationService
vs = ValidationService()
vs.validate_all_data()
"

# 4. Test functionality
docker-compose exec backend-test python -c "
from src.tests.integration_tests import run_integration_tests
run_integration_tests()
"

# 5. Cleanup test environment
docker-compose -f docker-compose.test.yml down -v
```

### RB012: Performance Tuning

**Purpose**: Optimize system performance based on monitoring data.

**Frequency**: Monthly or when performance issues are detected.

**Procedure**:

#### Database Optimization
```sql
-- Analyze slow queries
SELECT query, mean_time, calls
FROM pg_stat_statements
ORDER BY mean_time DESC
LIMIT 10;

-- Add indexes as needed
CREATE INDEX CONCURRENTLY idx_documents_created_at ON documents(created_at);

-- Update statistics
ANALYZE;
```

#### Application Optimization
```python
# Review and optimize code
# Profile memory usage
# Optimize database queries
# Implement caching strategies
```

#### Infrastructure Optimization
```bash
# Adjust resource limits
docker-compose up -d --scale backend=3

# Optimize network settings
# Tune kernel parameters
# Update load balancing configuration
```

## Post-Incident Review

After resolving any incident, conduct a post-incident review:

1. **Timeline Creation**: Document the incident timeline
2. **Root Cause Analysis**: Identify the underlying cause
3. **Impact Assessment**: Measure the business impact
4. **Lessons Learned**: Document key takeaways
5. **Action Items**: Create preventive measures
6. **Process Improvement**: Update runbooks and procedures

## Communication Templates

### Service Outage Notification
```
Subject: [OUTAGE] RAG System Service Degradation

Status: INVESTIGATING
Started: [Time]
Estimated Resolution: Unknown

Impact: Users may experience slow response times or errors when accessing the RAG system.

Next Update: [Time + 30 minutes]

Technical Team is actively investigating the issue.
```

### Resolution Notification
```
Subject: [RESOLVED] RAG System Service Restoration

Status: RESOLVED
Duration: [Duration]
Root Cause: [Brief description]

All services have been restored to normal operation.

We apologize for any inconvenience caused.
```

## Escalation Contacts

### Primary Escalation
- **On-call Engineer**: [Contact Information]
- **Engineering Lead**: [Contact Information]
- **DevOps Team**: [Contact Information]

### Secondary Escalation
- **CTO**: [Contact Information]
- **Security Team**: [Contact Information]
- **Legal/Compliance**: [Contact Information]

### External Contacts
- **Cloud Provider**: [Contact Information]
- **Security Consultant**: [Contact Information]
- **Legal Counsel**: [Contact Information]

## Conclusion

These runbooks provide comprehensive procedures for handling common operational scenarios. Regular review and updates ensure they remain effective and relevant to the evolving system landscape.

All incidents should be documented and used to improve both the system and the operational procedures. Continuous improvement is key to maintaining a reliable and efficient observability system.
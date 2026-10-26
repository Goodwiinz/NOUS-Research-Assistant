# Operations Runbook - Multimodal Enterprise RAG System

> **Status: superseded (reconciled 2026-10-17).** This runbook (a near-duplicate of [../deployment/OPERATIONS_RUNBOOK.md](../deployment/OPERATIONS_RUNBOOK.md)) targets an earlier deployment generation and its title, "Multimodal Enterprise RAG", is not the current product name. Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Qdrant is removed; live retrieval is PostgreSQL full-text. Start with the [AWS deployment overview](../deployment/aws-overview.md) and [AWS on-call commands](aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

This runbook provides step-by-step procedures for common operational tasks and incident response scenarios.

## Table of Contents

1. [Quick Reference](#quick-reference)
2. [Daily Operations](#daily-operations)
3. [Incident Response](#incident-response)
4. [Maintenance Procedures](#maintenance-procedures)
5. [Troubleshooting Scenarios](#troubleshooting-scenarios)
6. [Emergency Procedures](#emergency-procedures)

## Quick Reference

### Health Check Commands

```bash
# Liveness
curl -fsS http://localhost:8000/health

# Database and Redis readiness
curl -fsS http://localhost:8000/health/readiness

# Authenticated worker and knowledge-graph diagnostics
curl -fsS -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/workers/health
curl -fsS -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/knowledge-graph/health

# Service status
docker-compose ps

# Resource usage
docker stats --no-stream
```

### Log Commands

```bash
# Application logs
docker-compose logs -f backend

# All services logs
docker-compose logs -f

# Recent logs (last 100 lines)
docker-compose logs --tail=100 backend

# Error logs only
docker-compose logs backend | grep ERROR
```

### Service Management

```bash
# Restart specific service
docker-compose restart backend

# Stop and start service
docker-compose stop backend && docker-compose start backend

# Scale service
docker-compose up -d --scale celery-worker=4
```

### Monitoring Access

- **Grafana**: http://localhost:3001 (admin/admin)
- **Prometheus**: http://localhost:9090
- **Flower (Celery)**: http://localhost:5555

## Daily Operations

### Morning Checklist (8:00 AM)

#### 1. System Health Check
```bash
# Check overall system status
./scripts/health_check.sh

# Verify all services are running
docker-compose ps | grep -q "Up"

# Check resource utilization
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
```

#### 2. Backup Status
```bash
# Check recent backups
ls -la /backups/postgresql/ | tail -5
ls -la /backups/qdrant/ | tail -5

# Verify backup integrity
./scripts/verify_backups.sh
```

#### 3. Review Alerts
- Check Slack alerts channel
- Review email notifications
- Verify any critical alerts have been addressed

#### 4. Performance Review
- Check Grafana dashboards for anomalies
- Review response times and error rates
- Compare against baseline metrics

### Evening Checklist (6:00 PM)

#### 1. Daily Backup
```bash
# Ensure daily backup completed
./scripts/backup/backup_database.sh
./scripts/backup/backup_vector_store.py
```

#### 2. Log Review
```bash
# Check for errors in the last 24 hours
docker-compose logs --since=24h backend | grep -i error | tail -10

# Check for security events
docker-compose logs --since=24h backend | grep -i "unauthorized\|forbidden"
```

#### 3. Security Scan
```bash
# Run security vulnerability scan
./scripts/security_scan.sh
```

## Incident Response

### Incident Severity Levels

#### **SEV 0 - Critical**
- System outage affecting all users
- Data loss or security breach
- Revenue impact > $10,000/hour

#### **SEV 1 - High**
- Significant functionality degraded
- Major performance impact
- User experience severely affected

#### **SEV 2 - Medium**
- Partial functionality unavailable
- Moderate performance impact
- Subset of users affected

#### **SEV 3 - Low**
- Minor functionality issues
- Slight performance degradation
- Limited user impact

### Incident Response Procedure

#### 1. Detection (0-5 minutes)
```bash
# Verify incident
curl -f http://localhost:8000/health || echo "Health check failed"

# Check service status
docker-compose ps

# Check recent errors
docker-compose logs --since=10m backend | grep -i error
```

#### 2. Assessment (5-15 minutes)
```bash
# Determine scope
./scripts/assess_incident.sh

# Check dependencies
docker-compose exec backend python -c "
import asyncio
import asyncpg

async def check_db():
    try:
        conn = await asyncpg.connect('postgresql://postgres:postgres@postgres:5432/multimodal_rag')
        await conn.close()
        print('Database: OK')
    except Exception as e:
        print(f'Database: ERROR - {e}')

asyncio.run(check_db())
"
```

#### 3. Communication (15-30 minutes)
- Notify incident team via Slack
- Update status page
- Send customer notification if needed

#### 4. Resolution (Variable)
- Follow specific incident playbooks below
- Document all actions taken
- Verify resolution

#### 5. Post-Incident (1-2 hours)
- Write post-mortem report
- Identify root cause
- Create improvement actions

### Incident Playbooks

#### Service Outage

**Symptoms**: Health check failing, services not responding

**Immediate Actions**:
```bash
# Restart services
docker-compose restart

# If restart fails, rebuild
docker-compose down
docker-compose up -d --force-recreate

# Check resource constraints
docker system df
df -h
```

**Escalation**: If service not restored in 10 minutes, escalate to SEV 0

#### Database Connection Issues

**Symptoms**: Database connection errors, timeouts

**Diagnosis**:
```bash
# Test database connectivity
docker-compose exec postgres pg_isready

# Check connection count
docker-compose exec postgres psql -U postgres -d multimodal_rag -c "
SELECT count(*) FROM pg_stat_activity;
"

# Check database size
docker-compose exec postgres psql -U postgres -d multimodal_rag -c "
SELECT pg_size_pretty(pg_database_size('multimodal_rag'));
"
```

**Resolution**:
```bash
# Restart database
docker-compose restart postgres

# If connection pool exhausted, restart backend
docker-compose restart backend

# Check for long-running queries
docker-compose exec postgres psql -U postgres -d multimodal_rag -c "
SELECT query, now() - query_start AS duration
FROM pg_stat_activity
WHERE state = 'active'
ORDER BY duration DESC;
"
```

#### High Memory Usage

**Symptoms**: Memory alerts, OOM kills, slow performance

**Diagnosis**:
```bash
# Check memory usage
docker stats --no-stream | sort -k4 -hr

# Check system memory
free -h

# Find memory leaks
docker-compose exec backend python -c "
import tracemalloc
import gc

tracemalloc.start()
gc.collect()

snapshot = tracemalloc.take_snapshot()
top_stats = snapshot.statistics('lineno')
print('Top 10 memory consumers:')
for stat in top_stats[:10]:
    print(stat)
"
```

**Resolution**:
```bash
# Restart affected service
docker-compose restart backend

# Scale up resources if needed
docker-compose up -d --scale backend=2

# Clean up unused Docker resources
docker system prune -f
```

#### High CPU Usage

**Symptoms**: CPU alerts, slow response times

**Diagnosis**:
```bash
# Check CPU usage
docker stats --no-stream | sort -k3 -hr

# Check system processes
docker-compose exec backend top

# Find slow queries
docker-compose exec postgres psql -U postgres -d multimodal_rag -c "
SELECT query, calls, total_time, mean_time
FROM pg_stat_statements
ORDER BY mean_time DESC
LIMIT 10;
"
```

**Resolution**:
```bash
# Scale services
docker-compose up -d --scale celery-worker=4

# Optimize database queries
# (This may require code changes)

# Restart service if needed
docker-compose restart backend
```

#### Disk Space Issues

**Symptoms**: Disk space alerts, write failures

**Diagnosis**:
```bash
# Check disk usage
df -h

# Find large files
find /var/lib/docker -type f -size +1G -exec ls -lh {} \;

# Check Docker space usage
docker system df
```

**Resolution**:
```bash
# Clean up Docker
docker system prune -a -f

# Clean up old logs
docker-compose exec backend find /app/logs -name "*.log" -mtime +7 -delete

# Clean up old backups
find /backups -name "*.gz" -mtime +30 -delete
```

#### Security Incident

**Symptoms**: Security alerts, unauthorized access attempts

**Immediate Actions**:
```bash
# Check auth logs
docker-compose logs backend | grep -i "unauthorized\|forbidden" | tail -20

# Check for suspicious IPs
docker-compose logs backend | grep -E "ERROR.*[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" | tail -20

# Block suspicious IPs (if using firewall)
# This depends on your firewall configuration
```

**Escalation**: Immediately escalate to security team

## Maintenance Procedures

### Scheduled Maintenance

#### Weekly Maintenance (Sunday 2:00 AM)

```bash
#!/bin/bash
# weekly_maintenance.sh

set -euo pipefail

echo "Starting weekly maintenance..."

# Create backup
echo "Creating backup..."
./scripts/backup/backup_database.sh

# Update packages
echo "Updating packages..."
sudo apt update && sudo apt upgrade -y

# Clean up Docker
echo "Cleaning up Docker..."
docker system prune -f

# Restart services
echo "Restarting services..."
docker-compose restart

# Verify health
echo "Verifying health..."
sleep 30
curl -f http://localhost:8000/health || exit 1

echo "Weekly maintenance completed"
```

#### Monthly Maintenance (First Sunday of month)

```bash
#!/bin/bash
# monthly_maintenance.sh

set -euo pipefail

echo "Starting monthly maintenance..."

# Security updates
echo "Applying security updates..."
sudo apt update && sudo apt unattended-upgrade -y

# Database maintenance
echo "Performing database maintenance..."
docker-compose exec postgres psql -U postgres -d multimodal_rag -c "
VACUUM ANALYZE;
REINDEX DATABASE multimodal_rag;
"

# Update Docker images
echo "Updating Docker images..."
docker-compose pull

# Test disaster recovery
echo "Testing disaster recovery..."
./scripts/backup/disaster_recovery.py --dry-run

# Performance optimization
echo "Optimizing performance..."
# Add any performance tuning tasks

echo "Monthly maintenance completed"
```

### Rolling Updates

#### Application Update

```bash
#!/bin/bash
# rolling_update.sh

set -euo pipefail

NEW_VERSION=${1:-latest}
ENVIRONMENT=${2:-staging}

echo "Rolling update to $NEW_VERSION for $ENVIRONMENT..."

# Backup current version
echo "Creating backup..."
./scripts/backup/backup_database.sh

# Update one instance at a time
for service in backend frontend; do
    echo "Updating $service..."

    # Scale down to zero
    docker-compose -f docker-compose.$ENVIRONMENT.yml up -d --scale $service=0

    # Wait for service to stop
    sleep 10

    # Pull new image
    docker-compose -f docker-compose.$ENVIRONMENT.yml pull $service

    # Scale up with new image
    docker-compose -f docker-compose.$ENVIRONMENT.yml up -d --scale $service=1

    # Wait for health check
    sleep 30
    curl -f http://localhost:8000/health || {
        echo "Health check failed, rolling back..."
        docker-compose -f docker-compose.$ENVIRONMENT.yml rollback
        exit 1
    }

    echo "$service updated successfully"
done

echo "Rolling update completed"
```

## Troubleshooting Scenarios

### Performance Degradation

#### Scenario: Slow Response Times

**Investigation Steps**:
1. Check response time metrics in Grafana
2. Identify bottleneck (CPU, memory, I/O, network)
3. Check database query performance
4. Review recent deployments or changes

**Commands**:
```bash
# Check response times
curl -w "@curl-format.txt" -o /dev/null -s http://localhost:8000/api/documents

# Database performance
docker-compose exec postgres psql -U postgres -d multimodal_rag -c "
SELECT query, calls, total_time, mean_time, stddev_time
FROM pg_stat_statements
WHERE calls > 10
ORDER BY mean_time DESC
LIMIT 10;
"

# Check for blocking queries
docker-compose exec postgres psql -U postgres -d multimodal_rag -c "
SELECT blocked_locks.pid AS blocked_pid,
       blocked_activity.usename AS blocked_user,
       blocking_locks.pid AS blocking_pid,
       blocking_activity.usename AS blocking_user,
       blocked_activity.query AS blocked_statement,
       blocking_activity.query AS current_statement_in_blocking_process
FROM pg_catalog.pg_locks blocked_locks
JOIN pg_catalog.pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
JOIN pg_catalog.pg_locks blocking_locks ON blocking_locks.locktype = blocked_locks.locktype
JOIN pg_catalog.pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
WHERE NOT blocked_locks.granted;
"
```

### Data Issues

#### Scenario: Document Processing Failures

**Investigation Steps**:
1. Check Celery worker status
2. Review error logs in worker processes
3. Verify file accessibility and permissions
4. Check AI service availability

**Commands**:
```bash
# Check Celery status
docker-compose exec backend celery -A src.tasks.celery_app inspect active

# Check worker logs
docker-compose logs celery-worker | tail -50

# Check failed tasks
docker-compose exec backend python -c "
from celery.result import AsyncResult
from .tasks import process_document_task

# Check for failed tasks
# This would need to be adapted based on your task tracking
"

# Test AI services
curl -H "Authorization: Bearer $OPENAI_API_KEY" \
     https://api.openai.com/v1/models
```

### Integration Issues

#### Scenario: External Service Failures

**Investigation Steps**:
1. Test external API connectivity
2. Check API keys and credentials
3. Review rate limiting and quotas
4. Verify network connectivity

**Commands**:
```bash
# Test OpenAI API
curl -H "Authorization: Bearer $OPENAI_API_KEY" \
     https://api.openai.com/v1/models

# Test Anthropic API
curl -H "x-api-key: $ANTHROPIC_API_KEY" \
     https://api.anthropic.com/v1/messages

# Check network connectivity
ping api.openai.com
nslookup api.openai.com

# Check DNS resolution
dig api.openai.com
```

## Emergency Procedures

### Complete System Outage

#### Immediate Response (First 5 minutes)

```bash
#!/bin/bash
# emergency_response.sh

echo "=== EMERGENCY RESPONSE PROCEDURE ==="

# 1. Assess damage
echo "Assessing system status..."
docker-compose ps
docker stats --no-stream

# 2. Check health endpoints
echo "Checking health endpoints..."
for service in backend frontend; do
    if curl -f http://localhost:8000/health > /dev/null 2>&1; then
        echo "$service: HEALTHY"
    else
        echo "$service: UNHEALTHY"
    fi
done

# 3. Restart critical services
echo "Restarting critical services..."
docker-compose restart postgres redis neo4j qdrant

# 4. Wait for databases
echo "Waiting for databases to be ready..."
sleep 30

# 5. Restart application services
echo "Restarting application services..."
docker-compose restart backend celery-worker celery-beat frontend

# 6. Verify recovery
echo "Verifying recovery..."
sleep 30
curl -f http://localhost:8000/health && echo "System recovered" || echo "System still down"
```

#### Communication Protocol

1. **Internal**: Immediately notify on Slack #incidents
2. **External**: Update status page within 15 minutes
3. **Management**: Escalate to management team for SEV 0 incidents

### Data Corruption

#### Response Steps

```bash
#!/bin/bash
# data_corruption_response.sh

echo "=== DATA CORRUPTION RESPONSE ==="

# 1. Stop all writes
echo "Stopping all write operations..."
docker-compose stop backend celery-worker celery-beat

# 2. Assess damage
echo "Assessing data integrity..."
# Run data integrity checks

# 3. Restore from backup
echo "Restoring from backup..."
LATEST_BACKUP=$(ls -t /backups/postgresql/*multimodal_rag_backup*.sql.gz | head -1)
gunzip -c $LATEST_BACKUP | docker-compose exec -T postgres psql -U postgres -d multimodal_rag

# 4. Verify restoration
echo "Verifying restoration..."
docker-compose exec postgres psql -U postgres -d multimodal_rag -c "
SELECT count(*) FROM documents;
"

# 5. Restart services
echo "Restarting services..."
docker-compose start backend celery-worker celery-beat
```

### Security Breach

#### Immediate Response

```bash
#!/bin/bash
# security_breach_response.sh

echo "=== SECURITY BREACH RESPONSE ==="

# 1. Isolate affected systems
echo "Isolating affected systems..."
docker-compose stop backend

# 2. Preserve evidence
echo "Preserving evidence..."
docker logs backend > /tmp/security-incident-$(date +%Y%m%d_%H%M%S).log
cp /var/log/auth.log /tmp/auth-log-$(date +%Y%m%d_%H%M%S).log

# 3. Change credentials
echo "Changing credentials..."
# Rotate all API keys, passwords, and secrets

# 4. Scan for malware
echo "Scanning for malware..."
# Run security scanning tools

# 5. Notify security team
echo "Notifying security team..."
# Send emergency notification

# 6. Document everything
echo "Documenting incident..."
# Create incident report
```

### Escalation Contact List

#### Primary Contacts
- **DevOps Lead**: +1-555-0101
- **Engineering Manager**: +1-555-0102
- **Security Officer**: +1-555-0103

#### Secondary Contacts
- **CTO**: +1-555-0104
- **VP Engineering**: +1-555-0105

#### External Contacts
- **Managed Services Provider**: +1-555-0201
- **Cloud Provider Support**: Available 24/7
- **Security Consultant**: +1-555-0301

## Post-Incident Procedures

### Incident Report Template

```markdown
# Incident Report

## Summary
[Brief description of incident]

## Timeline
- **Start Time**: [YYYY-MM-DD HH:MM:SS UTC]
- **Detection Time**: [YYYY-MM-DD HH:MM:SS UTC]
- **Resolution Time**: [YYYY-MM-DD HH:MM:SS UTC]
- **Duration**: [Total time]

## Impact
- **Severity**: [SEV 0/1/2/3]
- **Affected Users**: [Number or percentage]
- **Services Affected**: [List of services]
- **Business Impact**: [Description]

## Root Cause
[Detailed analysis of what caused the incident]

## Resolution
[Step-by-step description of how it was resolved]

## Lessons Learned
[What we learned from this incident]

## Action Items
- [ ] [Action item 1] - [Owner] - [Due date]
- [ ] [Action item 2] - [Owner] - [Due date]
```

### Follow-up Tasks

1. **Root Cause Analysis** (Within 24 hours)
   - Detailed technical investigation
   - Identify contributing factors
   - Document findings

2. **Prevention Measures** (Within 1 week)
   - Implement fixes
   - Update monitoring
   - Improve procedures

3. **Team Review** (Within 1 week)
   - Review incident response
   - Identify improvements
   - Update runbooks

4. **Documentation Updates** (Within 2 weeks)
   - Update this runbook
   - Create new procedures
   - Train team members

---

**This runbook is a living document. Please update it with lessons learned from incidents and changes to the system.**

For questions or improvements to this runbook, contact the DevOps team.

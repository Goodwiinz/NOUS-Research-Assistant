# Operations Runbook - Multimodal Enterprise RAG System

> **Status: superseded (reconciled 2026-10-17).** This is a near-duplicate of [../operations/OPERATIONS_RUNBOOK.md](../operations/OPERATIONS_RUNBOOK.md) and targets an earlier deployment generation (namespace `rag-system`, Qdrant). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Qdrant is removed; live retrieval is PostgreSQL full-text. Start with the [AWS deployment overview](aws-overview.md) and [AWS on-call commands](../operations/aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

## 📋 Table of Contents

1. [System Overview](#system-overview)
2. [Daily Operations](#daily-operations)
3. [Monitoring Procedures](#monitoring-procedures)
4. [Incident Response](#incident-response)
5. [Maintenance Procedures](#maintenance-procedures)
6. [Scaling Operations](#scaling-operations)
7. [Backup and Recovery](#backup-and-recovery)
8. [Security Operations](#security-operations)
9. [Troubleshooting Guide](#troubleshooting-guide)
10. [Emergency Procedures](#emergency-procedures)

---

## System Overview

### Application Components

- **Frontend**: Next.js 15 application (TypeScript, Tailwind CSS)
- **Backend**: FastAPI Python services with multi-agent orchestration
- **Knowledge Graph**: Neo4j database for entity relationships
- **Vector/RAG Search**: DO Knowledge Base (GradientAI, `kbaas.do-ai.run`) behind `DO_KB_ENABLED` flag (off by default); effective retrieval falls back to PostgreSQL full-text search
- **Cache**: Redis — DO Managed Redis (external, not in-cluster)
- **Database**: PostgreSQL — Supabase managed (not in-cluster)
- **Processing**: Celery workers (backend tasks) + Trigger.dev cloud (durable orchestration); both deploy from `main` branch only
- **File Storage**: DO Spaces (object storage)
- **Secrets**: Infisical operator
- **Auth**: Supabase GoTrue (no self-hosted auth)

### Key Metrics to Monitor

- **Frontend Performance**: Core Web Vitals, page load times
- **Backend Performance**: API response times, error rates
- **Database Performance**: Query latency, connection counts
- **Search Performance**: Search response times, result relevance
- **Processing Queue**: Task completion rates, queue depth
- **Resource Usage**: CPU, memory, storage, network

---

## Daily Operations

### Morning Health Checks (9:00 AM)

```bash
# Check cluster status
kubectl get nodes
kubectl get pods -n rag-production

# Check application health
curl -f https://rag.yourdomain.com/api/health
curl -f https://api.rag.yourdomain.com/health

# Check database connectivity
kubectl exec -n rag-production deployment/neo4j -- cypher-shell -u neo4j -p $NEO4J_PASSWORD "RETURN 1"
# Redis is DO Managed — check via backend env or DO console; no in-cluster pod to exec into
# Qdrant is REMOVED from prod; vector search uses DO Knowledge Base (DO_KB_ENABLED) or PG fulltext

# Check resource usage
kubectl top nodes
kubectl top pods -n rag-production

# Review overnight logs
kubectl logs -n rag-production --since=24h -l app=multimodal-rag-frontend | grep ERROR
kubectl logs -n rag-production --since=24h -l app=multimodal-rag-backend | grep ERROR
```

### Daily Reports (10:00 AM)

```bash
# Generate daily performance report
./scripts/generate-daily-report.sh

# Check backup completion
# PostgreSQL: Supabase managed — verify via Supabase dashboard (Point-in-Time Recovery)
# Object storage: verify DO Spaces backups via DO console or doctl

# Review security alerts — use DO Monitoring / Sentry rather than AWS GuardDuty

# Check SSL certificate expiry
kubectl get certificates -n rag-production -o json | jq '.items[] | {name: .metadata.name, expiry: .status.notAfter}'
```

### End-of-Day Checks (5:00 PM)

```bash
# Review system performance
kubectl logs -n rag-production --since=12h -l app=multimodal-rag-backend | grep "SLOW_QUERY"

# Check processing queue status
kubectl exec -n rag-production deployment/celery-worker -- celery -A tasks inspect active

# Monitor disk usage
df -h
kubectl exec -n rag-production deployment/neo4j -- df -h /data
# Qdrant removed from prod; no Qdrant pod to check
```

---

## Monitoring Procedures

### Real-time Monitoring Dashboard Access

```bash
# Grafana Dashboard
kubectl port-forward -n monitoring svc/monitoring-grafana 3000:3000
# Access: http://localhost:3000

# Prometheus
kubectl port-forward -n monitoring svc/monitoring-prometheus 9090:9090
# Access: http://localhost:9090

# Application Metrics
# NOTE: /metrics endpoint may not be active — setup_observability() not confirmed wired in main.py.
# Use Sentry (frontend + backend errors) and LangSmith (agent traces, optional) as primary observability.
kubectl port-forward -n rag-production svc/multimodal-rag-backend 8000:8000
# Access: http://localhost:8000/metrics (verify endpoint is live before relying on it)
```

### Key Monitoring Dashboards

#### 1. System Overview Dashboard

- **CPU Usage**: Alert if >80% for >5 minutes
- **Memory Usage**: Alert if >85% for >5 minutes
- **Disk Usage**: Alert if >90%
- **Network I/O**: Monitor bandwidth utilization
- **Pod Status**: Track restarts and failures

#### 2. Application Performance Dashboard

- **API Response Time**: P95 <2s, P99 <5s
- **Error Rate**: Alert if >5%
- **Request Rate**: Monitor traffic patterns
- **Active Users**: Track concurrent usage
- **Search Performance**: Response times and result quality

#### 3. Database Performance Dashboard

- **Neo4j Query Performance**: Slow query identification
- **DO KB / PG Fulltext Search**: Search latency and throughput (Qdrant removed from prod)
- **Redis Performance**: Hit rate and memory usage (DO Managed Redis — monitor via DO dashboard)
- **PostgreSQL Performance**: Connection count and query times (Supabase managed — monitor via Supabase dashboard)

#### 4. Business Metrics Dashboard

- **Document Processing**: Daily upload and processing volumes
- **Search Analytics**: Query patterns and result relevance
- **User Engagement**: Session duration and feature usage
- **Quality Metrics**: RAG Triad scores and trends

### Alert Configuration

Critical Alerts (Page):

```yaml
groups:
  - name: critical-alerts
    rules:
      - alert: ApplicationDown
        expr: up{job="multimodal-rag-backend"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Multimodal RAG System is down"

      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) > 0.05
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "High error rate detected"

      - alert: DatabaseDown
        # Qdrant removed from prod; Redis and Postgres are external managed services
        expr: up{job=~"neo4j"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Database service is down"
```

Warning Alerts (Email):

```yaml
- alert: HighMemoryUsage
  expr: container_memory_usage_bytes / container_spec_memory_limit_bytes > 0.85
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "High memory usage detected"

- alert: SlowDatabaseQueries
  expr: histogram_quantile(0.95, rate(sql_query_duration_seconds_bucket[5m])) > 2
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Slow database queries detected"
```

---

## Incident Response

### Incident Severity Levels

#### **P0 - Critical** (Page immediately, 15min response)

- Complete system outage
- Data loss or corruption
- Security breach
- Performance degradation affecting all users

#### **P1 - High** (Page within 30min)

- Major feature failure
- Significant performance degradation
- Partial system outage
- High error rates

#### **P2 - Medium** (Email within 2h)

- Minor feature issues
- Moderate performance impact
- Non-critical bugs
- Documentation issues

#### **P3 - Low** (Ticket within 24h)

- Cosmetic issues
- Feature enhancements
- Documentation updates
- Minor optimizations

### Incident Response Workflow

#### 1. Incident Detection

```bash
# Monitor alert channels
# Slack: #incidents-multimodal-rag
# Email: ops-team@yourcompany.com
# PagerDuty: On-call engineer

# Automated monitoring
kubectl get events -n rag-production --sort-by='.lastTimestamp' | tail -10
```

#### 2. Initial Assessment (First 15 minutes)

```bash
# Verify scope of impact
curl -I https://rag.yourdomain.com
curl -I https://api.rag.yourdomain.com/health

# Check system status
kubectl get pods -n rag-production
kubectl get events -n rag-production --sort-by='.lastTimestamp'

# Review recent changes
kubectl rollout history deployment/multimodal-rag-frontend -n rag-production
kubectl rollout history deployment/multimodal-rag-backend -n rag-production
```

#### 3. Communication (First 30 minutes)

```bash
# Create incident Slack channel
# Update status page
# Send notification to stakeholders

# Template message:
"""
🚨 INVESTIGATING: Multimodal RAG System Issue

**Status**: Investigating
**Impact**: Users unable to access search functionality
**Started**: $(date)
**Next Update**: 30 minutes

Team is actively investigating the issue. We'll provide updates as soon as more information is available.
"""
```

#### 4. Resolution and Recovery

```bash
# Common resolution patterns:

# Restart failed services
kubectl rollout restart deployment/multimodal-rag-backend -n rag-production

# Scale up resources
kubectl scale deployment multimodal-rag-backend --replicas=5 -n rag-production

# Rollback to previous version
kubectl rollout undo deployment/multimodal-rag-backend -n rag-production

# Clear cache (Redis is DO Managed — connect via the DO-provided Redis URL, not in-cluster exec)
# kubectl exec will not work; connect via: redis-cli -u "$REDIS_URL" FLUSHALL
```

#### 5. Post-Incident Review

```bash
# Schedule post-mortem meeting within 24 hours
# Create incident report template
# Document timeline, impact, root cause, and prevention measures
# Update monitoring and alerting based on lessons learned
```

---

## Maintenance Procedures

### Weekly Maintenance (Sundays 2:00 AM - 4:00 AM)

#### 1. System Updates

```bash
# Check for security updates
npm audit audit --level high
pip-audit

# Update dependencies (test in staging first)
cd frontend && npm update
cd ../backend && pip install -r requirements.txt --upgrade

# Review and apply security patches
kubectl get pods -n rag-production -o json | jq '.items[].spec.containers[].image' | sort | uniq
```

#### 2. Performance Optimization

```bash
# Database maintenance
# PostgreSQL: Supabase managed — VACUUM runs automatically; trigger via Supabase dashboard if needed
kubectl exec -n rag-production deployment/neo4j -- cypher-shell -u neo4j -p $NEO4J_PASSWORD "CALL db.stats.retrieve('GRAPH COUNTS');"

# Clear old logs
kubectl logs -n rag-production --since=7d -l app=multimodal-rag-backend > /tmp/old-logs.log
# Archive old logs to DO Spaces (not AWS S3)
# s3cmd put /tmp/old-logs.log s3://multimodal-rag-logs/archives/
```

#### 3. Backup Verification

```bash
# Test backup restoration
./scripts/test-backup-restore.sh

# Verify backup integrity — check Supabase dashboard for PG backups; verify DO Spaces for Neo4j dumps

# Document backup status
./scripts/backup-status-report.sh
```

### Monthly Maintenance

#### 1. Security Audit

```bash
# Run security scanner
kubectl get pods -n rag-production -o json | kubectl score -

# Check for exposed secrets
kubectl get secrets -n rag-production -o yaml | grep -i "password\|key\|token"

# Review RBAC policies (no AWS IAM — infra is DOKS/DigitalOcean)
kubectl get clusterrolebindings,rolebindings -n rag-production
```

#### 2. Performance Review

```bash
# Generate performance report
./scripts/performance-analysis.sh

# Review resource utilization trends
kubectl top nodes --use-protocol-buffers | awk 'NR>1 {print $2, $3}' > /tmp/cpu-usage.log
kubectl top pods -n rag-production --use-protocol-buffers | awk 'NR>1 {print $2, $3}' > /tmp/memory-usage.log

# Plan capacity adjustments
./scripts/capacity-planning.sh
```

#### 3. Documentation Updates

```bash
# Update runbooks based on recent incidents
# Review and update architecture diagrams
# Update API documentation
# Verify contact information is current
```

---

## Scaling Operations

### Horizontal Scaling

#### Auto-scaling Configuration

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: multimodal-rag-backend-hpa
  namespace: rag-production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: multimodal-rag-backend
  minReplicas: 3
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80
```

#### Manual Scaling Operations

```bash
# Scale up for expected load
kubectl scale deployment multimodal-rag-backend --replicas=10 -n rag-production

# Scale down after peak period
kubectl scale deployment multimodal-rag-backend --replicas=3 -n rag-production

# Enable/disable auto-scaling
kubectl autoscale deployment multimodal-rag-backend \
    --cpu-percent=70 \
    --min=3 \
    --max=20 \
    -n rag-production

kubectl delete hpa multimodal-rag-backend-hpa -n rag-production
```

### Vertical Scaling

#### Resource Adjustment

```bash
# Monitor resource usage
kubectl top pods -n rag-production --use-protocol-buffers

# Update resource limits
kubectl patch deployment multimodal-rag-backend -n rag-production -p '{
  "spec": {
    "template": {
      "spec": {
        "containers": [{
          "name": "backend",
          "resources": {
            "limits": {
              "memory": "8Gi",
              "cpu": "4000m"
            },
            "requests": {
              "memory": "4Gi",
              "cpu": "2000m"
            }
          }
        }]
      }
    }
  }
}'
```

### Database Scaling

#### Neo4j Scaling

```bash
# Monitor Neo4j performance
kubectl exec -n rag-production deployment/neo4j -- cypher-shell -u neo4j -p $NEO4J_PASSWORD "CALL dbms.procedures() YIELD name WHERE name STARTS WITH 'dbms.list' RETURN name;"

# Add read replicas (cluster mode)
kubectl scale statefulset neo4j --replicas=3 -n rag-production

# Optimize configuration
kubectl exec -n rag-production deployment/neo4j -- cat /conf/neo4j.conf | grep -E "(memory|heap)"
```

#### Vector/RAG Search Scaling

**Qdrant is REMOVED from production.** Vector/RAG retrieval uses:

1. DO Knowledge Base (`kbaas.do-ai.run`) when `DO_KB_ENABLED=true` — scale via DO console
2. PostgreSQL full-text search (Supabase managed) as the effective fallback

There are no in-cluster Qdrant pods to scale or monitor.

---

## Backup and Recovery

### Automated Backup Configuration

#### Database Backups

```bash
# Neo4j backups (in-cluster)
kubectl exec -n rag-production deployment/neo4j -- neo4j-admin dump --database=neo4j --to=/backup/neo4j-$(date +%Y%m%d-%H%M%S).dump
# Upload to DO Spaces
doctl compute cdn flush  # or use s3cmd / rclone configured for DO Spaces
# e.g.: s3cmd put /backup/neo4j-*.dump s3://multimodal-rag-backups/neo4j/

# Qdrant: REMOVED from prod — no backups needed

# PostgreSQL: Supabase managed — backups handled by Supabase (Point-in-Time Recovery)
# To export manually: use Supabase dashboard > Database > Backups, or:
# pg_dump "postgresql://<supabase-connection-string>" | gzip > /tmp/postgres-$(date +%Y%m%d-%H%M%S).sql.gz
```

#### Application Data Backups

```bash
# DO Spaces synchronization (object storage — not AWS S3)
# Use s3cmd or rclone configured for DO Spaces endpoint (e.g. nyc3.digitaloceanspaces.com)
# s3cmd sync s3://multimodal-rag-documents s3://multimodal-rag-backups/documents/ --delete

# Configuration backups
kubectl get configmaps -n rag-production -o yaml > /tmp/configmaps-$(date +%Y%m%d).yaml
# NOTE: secrets are managed by Infisical operator — export via Infisical CLI, not kubectl secrets
kubectl get secrets -n rag-production -o yaml > /tmp/secrets-$(date +%Y%m%d).yaml
```

### Disaster Recovery Procedures

#### Scenario 1: Single Service Failure

```bash
# Identify failed service
kubectl get pods -n rag-production | grep -v Running

# Restart failed service
kubectl delete pod <failed-pod-name> -n rag-production

# Monitor recovery
kubectl get pods -w -n rag-production
```

#### Scenario 2: Database Corruption

```bash
# Stop application to prevent further damage
kubectl scale deployment multimodal-rag-backend --replicas=0 -n rag-production

# Restore from latest backup (DO Spaces)
# s3cmd get s3://multimodal-rag-backups/neo4j/latest.dump /tmp/neo4j-restore.dump
kubectl cp /tmp/neo4j-restore.dump neo4j-0:/tmp/

# Restore database
kubectl exec -n rag-production neo4j-0 -- neo4j-admin load --from=/tmp/neo4j-restore.dump --database=neo4j

# Restart application
kubectl scale deployment multimodal-rag-backend --replicas=3 -n rag-production
```

#### Scenario 3: Complete Cluster Recovery

```bash
# Provision new DOKS cluster via DO console or doctl (infrastructure is DOKS/ArgoCD, NOT EKS/Terraform)
doctl kubernetes cluster create multimodal-rag-dr --region nyc3 --node-pool "name=default;size=s-4vcpu-8gb;count=3"

# Configure kubectl
doctl kubernetes cluster kubeconfig save multimodal-rag-dr

# Restore databases from backups
./scripts/restore-all-databases.sh

# Deploy application via ArgoCD (primary) or Helm fallback
helm install rag-production ./helm/rag-production \
    --namespace rag-production \
    --create-namespace \
    --values values-dr.yaml

# Verify functionality
./scripts/health-check.sh
```

---

## Security Operations

### Daily Security Monitoring

```bash
# Check for security events — use Sentry (frontend + backend) and DO Monitoring alerts
# There is no AWS GuardDuty or CloudTrail; infra is DOKS on DigitalOcean

# Check for unusual activity
kubectl auth can-i --list --namespace rag-production
kubectl get events -n rag-production --field-selector type=Warning
```

### Security Patch Management

```bash
# Scan for vulnerabilities
trivy image --severity HIGH,CRITICAL your-registry/multimodal-rag-frontend:latest
trivy image --severity HIGH,CRITICAL your-registry/multimodal-rag-backend:latest

# Apply security patches
kubectl rollout status deployment/multimodal-rag-frontend -n rag-production
kubectl rollout status deployment/multimodal-rag-backend -n rag-production
```

### Access Control Management

```bash
# Review user access — auth is Supabase GoTrue (no self-hosted auth, no AWS IAM)
# Manage users via Supabase dashboard > Authentication > Users
kubectl get rolebindings -n rag-production

# Rotate secrets
./scripts/rotate-secrets.sh

# Update SSL certificates
kubectl get certificates -n rag-production
kubectl delete certificate <cert-name> -n rag-production  # Force renewal
```

---

## Troubleshooting Guide

### Common Issues and Solutions

#### 1. Frontend Not Loading

**Symptoms**: Blank page, 502 errors, slow loading
**Troubleshooting**:

```bash
# Check frontend pod status
kubectl get pods -n rag-production -l app=multimodal-rag-frontend

# Check frontend logs
kubectl logs -n rag-production -l app=multimodal-rag-frontend

# Check build process
kubectl exec -n rag-production deployment/multimodal-rag-frontend -- ls -la /app/.next

# Restart frontend
kubectl rollout restart deployment/multimodal-rag-frontend -n rag-production
```

#### 2. Backend API Errors

**Symptoms**: 500 errors, timeouts, authentication failures
**Troubleshooting**:

```bash
# Check backend pod status
kubectl get pods -n rag-production -l app=multimodal-rag-backend

# Check backend logs
kubectl logs -n rag-production -l app=multimodal-rag-backend --tail=100

# Check database connectivity (Supabase managed — use the DATABASE_URL from Infisical/env)
kubectl exec -n rag-production deployment/multimodal-rag-backend -- python -c "
import psycopg2, os
try:
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    print('PostgreSQL (Supabase): OK')
except Exception as e:
    print(f'PostgreSQL: {e}')
"

# Check environment variables (QDRANT_URL will be unset in prod — expected)
kubectl exec -n rag-production deployment/multimodal-rag-backend -- env | grep -E "(DATABASE|REDIS|NEO4J|DO_KB)"
```

#### 3. Search Performance Issues

**Symptoms**: Slow search results, timeouts, irrelevant results
**Troubleshooting**:

> **Note**: Qdrant is REMOVED from prod. Search goes through DO Knowledge Base (`DO_KB_ENABLED=true`)
> or falls back to PostgreSQL full-text (Supabase). There are no in-cluster Qdrant pods.

```bash
# Check DO KB flag
kubectl exec -n rag-production deployment/multimodal-rag-backend -- env | grep DO_KB

# Check search logs
kubectl logs -n rag-production -l app=multimodal-rag-backend | grep "SEARCH"

# Monitor search performance
curl -X POST http://api.rag.yourdomain.com/api/search \
    -H "Content-Type: application/json" \
    -d '{"query": "test", "debug": true}' \
    -w "Response time: %{time_total}s\n"

# If DO KB is returning empty results, check DO_KB_ENABLED and DO KB endpoint health
# in DO console (kbaas.do-ai.run). Fallback to PG fulltext is automatic when QdrantClient=None.
```

#### 4. Document Processing Failures

**Symptoms**: Stuck uploads, processing errors, missing documents
**Troubleshooting**:

```bash
# Check Celery worker status
kubectl exec -n rag-production deployment/celery-worker -- celery -A tasks inspect active

# Check processing logs
kubectl logs -n rag-production -l app=celery-worker

# Check file storage (DO Spaces — not AWS S3)
# doctl compute cdn list  or use DO console > Spaces

# Check processing queue
kubectl exec -n rag-production deployment/celery-worker -- celery -A tasks inspect reserved
```

#### 5. Memory Issues

**Symptoms**: OOMKilled events, slow performance
**Troubleshooting**:

```bash
# Check memory usage
kubectl top pods -n rag-production --use-protocol-buffers

# Check for OOM events
kubectl get events -n rag-production --field-selector type=Warning | grep OOM

# Increase memory limits
kubectl patch deployment multimodal-rag-backend -n rag-production -p '{
  "spec": {
    "template": {
      "spec": {
        "containers": [{
          "name": "backend",
          "resources": {
            "limits": {"memory": "8Gi"},
            "requests": {"memory": "4Gi"}
          }
        }]
      }
    }
  }
}'
```

---

## Emergency Procedures

### Complete System Outage

#### Immediate Actions (First 15 minutes)

```bash
# Declare incident
# Create Slack channel: #incident-multimodal-rag-$(date +%Y%m%d-%H%M%S)
# Page on-call engineer and team lead

# Assess scope
kubectl get pods -n rag-production
kubectl get nodes
curl -I https://rag.yourdomain.com
curl -I https://api.rag.yourdomain.com/health

# Check recent changes
kubectl rollout history deployment/multimodal-rag-frontend -n rag-production
kubectl rollout history deployment/multimodal-rag-backend -n rag-production
```

#### Recovery Actions

```bash
# Attempt service restart
kubectl rollout restart deployment/multimodal-rag-frontend -n rag-production
kubectl rollout restart deployment/multimodal-rag-backend -n rag-production

# Check infrastructure (DOKS — not AWS EC2/RDS)
doctl kubernetes cluster list
doctl compute droplet list --tag-name production
# Database: Supabase managed — check via Supabase dashboard; Redis: DO Managed — check via DO console

# If needed, rollback to previous version
kubectl rollout undo deployment/multimodal-rag-frontend -n rag-production
kubectl rollout undo deployment/multimodal-rag-backend -n rag-production
```

### Security Incident

#### Immediate Response

```bash
# Isolate affected systems
kubectl scale deployment multimodal-rag-backend --replicas=0 -n rag-production

# Enable audit logging
kubectl apply -f - <<EOF
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
- level: Metadata
EOF

# Preserve evidence
kubectl get events -n rag-production --all-namespaces > /tmp/incident-events.log
kubectl logs -n rag-production --all > /tmp/incident-logs.log

# Notify security team
# Document timeline of events
```

### Data Corruption Incident

#### Response Procedures

```bash
# Stop all writes to prevent further damage
kubectl scale deployment multimodal-rag-backend --replicas=0 -n rag-production

# Identify affected data
./scripts/assess-data-integrity.sh

# Restore from clean backup
./scripts/emergency-restore.sh

# Verify data integrity
./scripts/verify-data-integrity.sh

# Gradually restore service
kubectl scale deployment multimodal-rag-backend --replicas=1 -n rag-production
# Monitor for 15 minutes, then scale up gradually
```

---

## Contact Information

### Emergency Contacts

- **On-call Engineer**: +1-XXX-XXX-XXXX (PagerDuty)
- **DevOps Team Lead**: devops-lead@yourcompany.com
- **Security Team**: security@yourcompany.com
- **Management**: management@yourcompany.com

### Service Providers

- **DigitalOcean Support**: https://cloud.digitalocean.com/support
- **Supabase Support**: https://supabase.com/dashboard/support (Postgres + Auth)
- **Database Support**: neo4j-support@neo4j.com
- **Monitoring**: Sentry (https://sentry.io), kube-prometheus-stack (`monitoring` namespace)

### Documentation Links

- **Architecture Guide**: [Link to architecture documentation]
- **API Documentation**: [Link to API docs]
- **Runbook Repository**: [Link to runbook repo]
- **Status Page**: https://status.yourdomain.com

---

## Training and Documentation

### Operator Training Requirements

- Complete Kubernetes administration training
- Understanding of application architecture
- Familiarity with monitoring tools
- Incident response procedures

### Documentation Updates

- Review runbooks quarterly
- Update after major incidents
- Maintain change logs
- Cross-train team members

### Drills and Simulations

- Monthly fire drills
- Quarterly disaster recovery tests
- Annual security simulations
- Post-incident reviews and improvements

---

This operations runbook should be reviewed quarterly and updated as the system evolves. All team members should be familiar with emergency procedures and have access to necessary tools and credentials.

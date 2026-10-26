# On-Call Procedures and Runbooks

> **Status (reconciled 2026-10-17):** the Kubernetes commands below were
> updated for the live target: EKS `nous-dev-cluster`, namespace
> `multimodal-rag-system`, Helm release `nous-dev-aws`. For a verified command
> set start with [AWS on-call commands](aws-on-call.md). Qdrant is removed
> (retrieval is PostgreSQL full-text), PostgreSQL is RDS (not an in-cluster pod),
> and there is no `document-processor` deployment: document processing runs in
> the Celery worker. Items marked *not verified* (dashboard URLs, internal
> backend HTTP endpoints, the escalation chain, contact placeholders) are
> carried over from the original text and were not checked against the repo.

## Table of Contents
1. [On-Call Responsibilities](#on-call-responsibilities)
2. [Escalation Policy](#escalation-policy)
3. [Communication Channels](#communication-channels)
4. [Runbooks by Service](#runbooks-by-service)
   - [Document Processing](#document-processing-runbooks)
   - [WebSocket Services](#websocket-services-runbooks)
   - [API Services](#api-services-runbooks)
   - [Database Services](#database-services-runbooks)
   - [Search Services](#search-services-runbooks)
   - [ML/AI Services](#mlai-services-runbooks)
   - [System Resources](#system-resources-runbooks)
5. [Incident Management](#incident-management)
6. [Post-Incident Procedures](#post-incident-procedures)

## On-Call Responsibilities

### Primary Responsibilities
- **System Monitoring**: Continuously monitor all system dashboards and alert channels
- **Incident Response**: Acknowledge alerts within 5 minutes and begin investigation
- **Issue Resolution**: Work to resolve incidents according to SLA requirements
- **Communication**: Provide regular updates to stakeholders during major incidents
- **Documentation**: Document all actions taken during incident resolution
- **Handoff**: Provide comprehensive handoff to next on-call engineer

### Required Tools Access
- Grafana dashboards (monitoring.company.com)
- Alertmanager (alerts.company.com)
- Kubernetes cluster access
- Database access (RDS PostgreSQL, Neo4j; Redis is ElastiCache)
- Log aggregation platform (ELK/Splunk)
- Communication platforms (Slack, PagerDuty)

### Monitoring Requirements
- Monitor primary dashboard: [Document Processing Dashboard](https://grafana.company.com/d/document-processing)
- Monitor secondary dashboard: [WebSocket Dashboard](https://grafana.company.com/d/websocket-observability)
- Check SLO status: [SLO Dashboard](https://grafana.company.com/d/slo-monitoring)
- Review system health: [System Health Dashboard](https://grafana.company.com/d/system-health)

## Escalation Policy

### Alert Severity Levels
- **Critical**: Respond within 5 minutes, escalate after 15 minutes if no resolution
- **Warning**: Respond within 15 minutes, escalate after 1 hour if no resolution
- **Info**: Monitor and address during business hours

### Escalation Chain
1. **Primary On-Call Engineer** (immediate response)
2. **Secondary On-Call Engineer** (escalate after 15/60 minutes)
3. **Engineering Lead** (escalate after 30/120 minutes)
4. **Head of Engineering** (escalate for major incidents)
5. **CTO** (escalate for critical system-wide outages)

### Escalation Triggers
- No response to critical alerts within 5 minutes
- No resolution progress within timeout periods
- Multiple simultaneous critical alerts
- External customer impact reports
- System-wide degradation

### Escalation Communication
- **Slack**: `#incidents` channel for all escalations
- **PagerDuty**: Automatic escalation based on timeout
- **Phone**: Direct call for critical escalations
- **Email**: Incident summary sent to management team

## Communication Channels

### Internal Communication
- **Slack #incidents**: Primary incident coordination channel
- **Slack #engineering**: General engineering updates
- **Slack #alerts**: Automated alert notifications
- **PagerDuty**: On-call scheduling and notifications

### External Communication
- **Status Page**: status.company.com for customer-facing updates
- **Customer Support**: Direct communication for customer-impacting issues
- **Management Updates**: Executive status reports for major incidents

### Communication Templates

#### Initial Incident Alert
```
🚨 INCIDENT DECLARED

Service: [Service Name]
Severity: [Critical/Warning/Info]
Started: [Timestamp]
Description: [Brief description]

Investigation in progress.
Next update in: 15 minutes

#incident-[number]
```

#### Status Update Template
```
📊 INCIDENT UPDATE

Incident: #incident-[number]
Status: [Investigating/Mitigated/Resolved]
Started: [Timestamp]
Duration: [X minutes]

Update: [Detailed progress update]
Impact: [Current customer impact]
ETA: [Estimated resolution time]

Next update: [Time]
```

#### Resolution Template
```
✅ INCIDENT RESOLVED

Incident: #incident-[number]
Resolved: [Timestamp]
Duration: [X minutes]
Root Cause: [Brief root cause]
Resolution: [Brief resolution]

Post-mortem scheduled: [Date/Time]
#incident-[number]
```

## Runbooks by Service

> **Scaling caveat on AWS.** Argo CD `selfHeal` reverts manual `kubectl scale`,
> `set env` and `patch` on the backend and beat Deployments within minutes;
> only the Celery worker's replica count is excluded from drift
> (`ignoreDifferences` in `aws-dev.yaml`, because the worker is HPA-owned). A
> durable change is a values change in Git. Manual mutation of the live cluster
> needs explicit authorization.

### Document Processing Runbooks

#### 🚨 High Document Processing Failure Rate
**Trigger**: Document processing failure rate > 10%

**Impact**: Users cannot process documents, system functionality degraded

**Investigation Steps**:
1. **Check Current Status**
   ```bash
   # Check processing queue
   kubectl get pods -n multimodal-rag-system -l app.kubernetes.io/component=celery-worker

   # Check queue size
   curl http://backend:8000/api/v1/documents/queue/status
   ```

2. **Review Logs**
   ```bash
   # Check processor logs
   kubectl logs -n multimodal-rag-system -l app.kubernetes.io/component=celery-worker --tail=100

   # Check Celery worker status
   kubectl exec -it -n multimodal-rag-system deploy/nous-dev-aws-celery-worker -- celery -A src.tasks.celery_app inspect active
   ```

3. **Verify Dependencies**
   ```bash
   # Check database connectivity
   kubectl exec -it -n multimodal-rag-system deployment/nous-dev-aws-knowledge-graph-analytics-backend -- python -c "from src.database import get_db; print('DB OK')"

   # Check Neo4j pod
   kubectl get pods -n multimodal-rag-system | grep neo4j
   ```

**Common Issues and Solutions**:
- **Database Connection Pool Exhausted**:
  ```bash
  # Scale backend pods
  kubectl scale deployment/nous-dev-aws-knowledge-graph-analytics-backend --replicas=4 -n multimodal-rag-system

  # Check connection pool settings (counts only)
  kubectl exec -it -n multimodal-rag-system deployment/nous-dev-aws-knowledge-graph-analytics-backend -- sh -c 'env | grep -c ^DATABASE'  # counts only; never print secret values
  ```

- **Insufficient Worker Resources**:
  ```bash
  # Scale Celery workers
  kubectl scale deployment/nous-dev-aws-celery-worker --replicas=8 -n multimodal-rag-system

  # Check worker resource limits
  kubectl describe deployment/nous-dev-aws-celery-worker -n multimodal-rag-system
  ```

- **Search/retrieval issues**: Qdrant is removed. Live retrieval is PostgreSQL
  full-text on RDS, so check database health (below) rather than a vector store.

**Escalation Criteria**:
- Failure rate > 50% for more than 5 minutes
- Queue backlog > 100 documents
- Multiple components failing simultaneously

#### 🚨 Document Processing Queue Backlog
**Trigger**: Queue size > 50 documents

**Investigation Steps**:
1. **Check Queue Metrics**
   ```bash
   # Current queue size
   curl http://backend:8000/api/v1/documents/queue/metrics

   # Worker status
   kubectl get pods -n multimodal-rag-system -l app.kubernetes.io/component=celery-worker
   ```

2. **Analyze Processing Bottlenecks**
   ```bash
   # Check worker performance
   kubectl top pods -n multimodal-rag-system -l app.kubernetes.io/component=celery-worker

   # Check document processing metrics
   curl http://backend:8000/metrics | grep document_processing
   ```

3. **Review Recent Document Types**
   ```bash
   # Check what types of documents are queued
   curl http://backend:8000/api/v1/documents/queue/analysis
   ```

**Mitigation Strategies**:
- **Scale Workers**:
  ```bash
  # Temporary scale up
  kubectl scale deployment/nous-dev-aws-celery-worker --replicas=12 -n multimodal-rag-system
  ```

- **Prioritize Processing**:
  ```bash
  # Enable priority processing for critical documents
  curl -X POST http://backend:8000/api/v1/documents/queue/prioritize \
    -H "Content-Type: application/json" \
    -d '{"priority": "high", "max_age": "1h"}'
  ```

### WebSocket Services Runbooks

#### 🚨 High WebSocket Error Rate
**Trigger**: WebSocket error rate > 5%

**Impact**: Real-time updates not working, user experience degraded

**Investigation Steps**:
1. **Check Connection Metrics**
   ```bash
   # Current active connections
   curl http://backend:8000/api/v1/websocket/metrics

   # Connection errors by type
   curl http://backend:8000/metrics | grep websocket_errors
   ```

2. **Review WebSocket Server Status**
   ```bash
   # Check WebSocket endpoint health
   curl http://backend:8000/api/v1/websocket/health

   # Check backend pod status
   kubectl get pods -n multimodal-rag-system -l app.kubernetes.io/component=backend
   ```

3. **Analyze Error Patterns**
   ```bash
   # Check recent WebSocket logs
   kubectl logs -n multimodal-rag-system -l app.kubernetes.io/component=backend --tail=200 | grep websocket

   # Check for connection limits
   kubectl describe service nous-dev-aws-knowledge-graph-analytics-backend -n multimodal-rag-system
   ```

**Common Issues and Solutions**:
- **Connection Limit Exceeded**:
  ```bash
  # Check connection limits
  sysctl net.core.somaxconn
  sysctl net.ipv4.tcp_max_syn_backlog

  # Increase connection limits if needed
  kubectl patch deployment/nous-dev-aws-knowledge-graph-analytics-backend -n multimodal-rag-system -p '{"spec":{"template":{"spec":{"containers":[{"name":"backend","resources":{"limits":{"memory":"2Gi","cpu":"1000m"}}}]}}}'
  ```

- **Memory Pressure**:
  ```bash
  # Check pod memory usage
  kubectl top pods -n multimodal-rag-system -l app.kubernetes.io/component=backend

  # Scale backend if needed
  kubectl scale deployment/nous-dev-aws-knowledge-graph-analytics-backend --replicas=3 -n multimodal-rag-system
  ```

#### 🚨 WebSocket Connection Drop
**Trigger**: Sudden loss of >20 connections

**Investigation Steps**:
1. **Verify Infrastructure Health**
   ```bash
   # Check load balancer status
   kubectl get svc -n multimodal-rag-system

   # Check the AWS Load Balancer Controller (ALB ingress)
   kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller
   ```

2. **Review Recent Deployments**
   ```bash
   # Check recent changes
   kubectl rollout history deployment/nous-dev-aws-knowledge-graph-analytics-backend -n multimodal-rag-system

   # Check for recent configuration changes
   kubectl get events -n multimodal-rag-system --sort-by='.lastTimestamp' | tail -20
   ```

3. **Network Diagnostics**
   ```bash
   # Test WebSocket connectivity
   wscat -c ws://nous-dev-aws-knowledge-graph-analytics-backend.multimodal-rag-system.svc.cluster.local:80/ws

   # Check DNS resolution
   nslookup nous-dev-aws-knowledge-graph-analytics-backend.multimodal-rag-system.svc.cluster.local
   ```

### API Services Runbooks

#### 🚨 High API Error Rate
**Trigger**: API error rate > 5% (5xx responses)

**Impact**: Users cannot access system functionality

**Investigation Steps**:
1. **Check API Probes**
   ```bash
   # Liveness
   curl -fsS http://backend:8000/health

   # Database and Redis readiness
   curl -fsS http://backend:8000/health/readiness

   # Authenticated component diagnostics
   curl -fsS -H "Authorization: Bearer $TOKEN" http://backend:8000/api/v1/workers/health
   curl -fsS -H "Authorization: Bearer $TOKEN" http://backend:8000/api/v1/knowledge-graph/health
   ```

2. **Analyze Error Patterns**
   ```bash
   # Check 5xx error rates
   curl http://backend:8000/metrics | grep 'http_requests_total{status_code=~"5.."'

   # Check specific endpoints
   curl http://backend:8000/metrics | grep http_requests_total
   ```

3. **Review Application Logs**
   ```bash
   # Check recent error logs
   kubectl logs -n multimodal-rag-system -l app.kubernetes.io/component=backend --tail=100 | grep -i error

   # Check for memory issues
   kubectl logs -n multimodal-rag-system -l app.kubernetes.io/component=backend --tail=100 | grep -i memory
   ```

**Common Issues and Solutions**:
- **Database Connection Issues**:
  ```bash
  # Test database connectivity
  kubectl exec -it -n multimodal-rag-system deployment/nous-dev-aws-knowledge-graph-analytics-backend -- python -c "
  from sqlalchemy import create_engine
  engine = create_engine('<DATABASE_URL from the pod environment; never paste credentials>')
  engine.execute('SELECT 1')
  print('Database OK')
  "

  # Restart backend if needed
  kubectl rollout restart deployment/nous-dev-aws-knowledge-graph-analytics-backend -n multimodal-rag-system
  ```

- **Resource Exhaustion**:
  ```bash
  # Check resource usage
  kubectl top pods -n multimodal-rag-system -l app.kubernetes.io/component=backend

  # Scale horizontally
  kubectl scale deployment/nous-dev-aws-knowledge-graph-analytics-backend --replicas=4 -n multimodal-rag-system
  ```

### Database Services Runbooks

#### 🚨 Database High Error Rate
**Trigger**: Database error rate > 5%

**Investigation Steps**:
1. **Check Database Connectivity**
   ```bash
   # PostgreSQL is RDS (credentials in aws-database-credentials, never print them).
   # The backend readiness probe checks the database:
   kubectl exec -n multimodal-rag-system deployment/nous-dev-aws-knowledge-graph-analytics-backend -- curl -fsS localhost:8000/health/readiness

   # Neo4j (StatefulSet nous-dev-aws-knowledge-graph-analytics-neo4j)
   kubectl get pods -n multimodal-rag-system | grep neo4j
   ```

2. **Review Database Performance**
   ```bash
   # PostgreSQL slow queries
   # RDS is in private subnets: run psql from a pod or host that can reach it,
   # and never echo the connection URL.
   psql "$DATABASE_URL" -c "
   SELECT query, mean_time, calls, total_time
   FROM pg_stat_statements
   ORDER BY mean_time DESC
   LIMIT 10;
   "

   # Check connection pool status
   kubectl exec -it -n multimodal-rag-system deployment/nous-dev-aws-knowledge-graph-analytics-backend -- python -c "
  from src.database import engine
  print(f'Pool size: {engine.pool.size()}')
  print(f'Checked out: {engine.pool.checkedout()}')
  "
   ```

3. **Monitor Database Resources**
   ```bash
   # PostgreSQL metrics
   # RDS is in private subnets: run psql from a pod or host that can reach it,
   # and never echo the connection URL.
   psql "$DATABASE_URL" -c "
   SELECT
     datname,
     numbackends,
     xact_commit,
     xact_rollback,
     blks_read,
     blks_hit,
     tup_returned,
     tup_fetched
   FROM pg_stat_database
   WHERE datname = 'multimodal_rag_dev';
   "
   ```

**Common Solutions**:
- **Connection Pool Exhaustion**:
  ```bash
  # Increase pool size temporarily
  kubectl set env deployment/nous-dev-aws-knowledge-graph-analytics-backend -n multimodal-rag-system DATABASE_POOL_SIZE=20
  kubectl rollout restart deployment/nous-dev-aws-knowledge-graph-analytics-backend -n multimodal-rag-system
  ```

- **Query Performance Issues**:
  ```bash
  # Add indexes for slow queries
  # RDS is in private subnets: run psql from a pod or host that can reach it,
  # and never echo the connection URL.
  psql "$DATABASE_URL" -c "
  CREATE INDEX CONCURRENTLY idx_documents_created_at
  ON documents(created_at);
  "
  ```

## Incident Management

### Incident Classification

#### Severity Levels
- **SEV-0 (Critical)**: System-wide outage, complete service failure
  - Response time: 5 minutes
  - Resolution target: 1 hour
  - Executive notification: Immediate

- **SEV-1 (High)**: Major feature failure, significant customer impact
  - Response time: 15 minutes
  - Resolution target: 4 hours
  - Executive notification: Within 30 minutes

- **SEV-2 (Medium)**: Partial service degradation, limited customer impact
  - Response time: 1 hour
  - Resolution target: 24 hours
  - Executive notification: Daily summary

- **SEV-3 (Low)**: Minor issues, no customer impact
  - Response time: 4 hours
  - Resolution target: 72 hours
  - Executive notification: Weekly summary

### Incident Response Process

#### 1. Detection and Triage (0-5 minutes)
- Acknowledge all critical alerts
- Assess incident severity and impact
- Declare incident if severity ≥ SEV-1
- Establish communication channel

#### 2. Initial Response (5-15 minutes)
- Form incident response team
- Begin initial investigation
- Implement immediate mitigations
- Set up customer communications

#### 3. Investigation (15-60 minutes)
- Analyze logs and metrics
- Identify root cause candidates
- Test potential fixes
- Document all findings

#### 4. Resolution and Recovery (60+ minutes)
- Implement fix
- Monitor system recovery
- Verify service restoration
- Update all stakeholders

#### 5. Post-Incident (Post-resolution)
- Conduct blameless post-mortem
- Create action items
- Update documentation
- Improve monitoring/alerting

### Incident Command System Roles

- **Incident Commander**: Overall coordination and communication
- **Technical Lead**: Technical investigation and resolution
- **Communications Lead**: Internal and external communications
- **Customer Support Lead**: Customer impact assessment and support

## Post-Incident Procedures

### Post-Mortem Requirements

All SEV-0, SEV-1, and SEV-2 incidents require a post-mortem within 72 hours.

#### Post-Mortem Template
```
# Incident Post-Mortem: [Incident Title]

## Summary
- Date: [Date]
- Duration: [X hours/minutes]
- Severity: [SEV-X]
- Impact: [Customer/system impact]

## Timeline
- [Time]: Incident detected
- [Time]: Investigation started
- [Time]: Mitigation implemented
- [Time]: Incident resolved

## Root Cause Analysis
### What Happened
[Brief description of events]

### Why It Happened
[Root cause analysis]

### Contributing Factors
[Secondary factors]

## Impact Assessment
### Customer Impact
[Number of affected users, functionality impacted]

### Business Impact
[Revenue impact, SLA violations]

## Resolution and Recovery
### Immediate Actions
[Actions taken to resolve the incident]

### Permanent Fixes
[Long-term solutions implemented]

## Lessons Learned
### What Went Well
[Positive aspects of response]

### What Could Be Improved
[Areas for improvement]

## Action Items
- [ ] [Action item 1] - [Owner] - [Due date]
- [ ] [Action item 2] - [Owner] - [Due date]

## Prevention
### Monitoring Improvements
[New alerts, dashboards, or metrics]

### Process Improvements
[Changes to procedures or training]

### Infrastructure Changes
[System improvements to prevent recurrence]
```

### Knowledge Management

#### Documentation Updates
- Update relevant runbooks with new troubleshooting steps
- Add known failure patterns to knowledge base
- Update monitoring configurations
- Create training materials for new issues

#### Training and Awareness
- Conduct incident review sessions
- Share lessons learned with engineering team
- Update on-call training materials
- Schedule follow-up training if needed

#### Process Improvements
- Review and update escalation procedures
- Improve alerting thresholds based on incident data
- Enhance monitoring coverage
- Refine communication templates

### Continuous Improvement

#### Metrics Tracking
Track the following incident response metrics:
- **MTTR** (Mean Time to Resolution)
- **MTTA** (Mean Time to Acknowledge)
- **MTTD** (Mean Time to Detection)
- **Incident recurrence rate**
- **Customer satisfaction scores**

#### Regular Reviews
- **Weekly**: Incident summary review
- **Monthly**: SLO performance review
- **Quarterly**: Incident process review
- **Annually**: Major incident retrospective

## Contact Information

### Emergency Contacts
- **Primary On-Call**: [Phone number], [Slack handle]
- **Engineering Lead**: [Phone number], [Slack handle]
- **Head of Engineering**: [Phone number], [Slack handle]
- **CTO**: [Phone number], [Slack handle]

### Service Teams
- **Platform Team**: #platform-team
- **Application Team**: #application-team
- **Security Team**: #security-team
- **Customer Support**: #customer-support

### External Vendors
- **Cloud Provider**: [Contact information]
- **Database Support**: [Contact information]
- **Monitoring Provider**: [Contact information]

This document should be reviewed quarterly and updated as systems and procedures evolve.

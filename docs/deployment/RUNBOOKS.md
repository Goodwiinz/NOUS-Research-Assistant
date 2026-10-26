# Runbooks

> **Status: superseded (reconciled 2026-10-17).** These runbooks target an earlier deployment generation. Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Start with the [AWS deployment overview](aws-overview.md) and [AWS on-call commands](../operations/aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.
## Knowledge Graph Analytics Dashboard

### Table of Contents
1. [Emergency Procedures](#emergency-procedures)
2. [Incident Response](#incident-response)
3. [Maintenance Runbooks](#maintenance-runbooks)
4. [Troubleshooting Guides](#troubleshooting-guides)
5. [Performance Tuning](#performance-tuning)

---

## Emergency Procedures

### Service Outage Response

#### Severity 1: Complete Service Outage
**Response Time**: 15 minutes
**Escalation**: Immediately alert on-call engineer and DevOps lead

**Procedure**:
1. **Immediate Assessment (0-5 min)**
   ```bash
   # Check overall cluster health
   kubectl get nodes
   kubectl get pods -A

   # Check critical services
   kubectl get pods -n knowledge-graph-analytics
   kubectl get services -n knowledge-graph-analytics

   # Check ingress and load balancer
   kubectl get ingress -n knowledge-graph-analytics
   kubectl get svc -n knowledge-graph-analytics
   ```

2. **Identify Root Cause (5-10 min)**
   ```bash
   # Check pod logs for errors
   kubectl logs -n knowledge-graph-analytics -l app=backend --tail=100
   kubectl logs -n knowledge-graph-analytics -l app=frontend --tail=100

   # Check resource usage
   kubectl top nodes
   kubectl top pods -n knowledge-graph-analytics

   # Check database connectivity
   kubectl exec -n knowledge-graph-analytics deployment/postgres -- pg_isready
   ```

3. **Restore Service (10-15 min)**
   ```bash
   # Restart failed services
   kubectl rollout restart deployment/knowledge-graph-analytics-backend -n knowledge-graph-analytics
   kubectl rollout restart deployment/knowledge-graph-analytics-frontend -n knowledge-graph-analytics

   # Scale up if resource constrained
   kubectl scale deployment knowledge-graph-analytics-backend --replicas=5 -n knowledge-graph-analytics

   # Monitor recovery
   watch kubectl get pods -n knowledge-graph-analytics
   ```

#### Severity 2: Partial Service Degradation
**Response Time**: 30 minutes
**Escalation**: Alert on-call engineer

**Procedure**:
1. **Assess Impact**
   ```bash
   # Check which components are affected
   curl -I https://analytics.yourdomain.com
   curl -I https://api.yourdomain.com/health

   # Check specific service health
   kubectl get pods -n knowledge-graph-analytics -o wide
   ```

2. **Isolate Problem Area**
   ```bash
   # Test individual components
   kubectl exec -n knowledge-graph-analytics deployment/backend -- curl localhost:8000/health
   kubectl exec -n knowledge-graph-analytics deployment/postgres -- pg_isready
   ```

3. **Implement Fix**
   ```bash
   # Restart affected components
   kubectl rollout restart deployment/<affected-service> -n knowledge-graph-analytics

   # Monitor recovery
   kubectl rollout status deployment/<affected-service> -n knowledge-graph-analytics
   ```

### Database Emergency Procedures

#### PostgreSQL Database Recovery
```bash
# Check database status
kubectl exec -n knowledge-graph-analytics deployment/postgres -- pg_isready

# If database is down, restart pod
kubectl delete pod -l app=postgres -n knowledge-graph-analytics

# Check for corruption
kubectl exec -n knowledge-graph-analytics deployment/postgres -- psql -U raguser -d ragdb -c "SELECT datname FROM pg_database WHERE datistemplate = false;"

# If corruption detected, restore from backup
cd scripts
./backup-and-restore.sh restore <backup_id>
```

#### Redis Cache Recovery
```bash
# Check Redis status
kubectl exec -n knowledge-graph-analytics deployment/redis -- redis-cli ping

# Clear cache if corrupted
kubectl exec -n knowledge-graph-analytics deployment/redis -- redis-cli FLUSHALL

# Restart Redis service
kubectl rollout restart deployment/redis -n knowledge-graph-analytics
```

#### Neo4j Graph Database Recovery
```bash
# Check Neo4j status
kubectl exec -n knowledge-graph-analytics deployment/neo4j -- cypher-shell -u neo4j -p $NEO4J_PASSWORD "RETURN 1"

# Restart Neo4j if unresponsive
kubectl delete pod -l app=neo4j -n knowledge-graph-analytics

# Check for data corruption
kubectl exec -n knowledge-graph-analytics deployment/neo4j -- cypher-shell -u neo4j -p $NEO4J_PASSWORD "MATCH (n) RETURN count(n)"
```

---

## Incident Response

### Incident Classification

| Severity | Impact | Response Time | Resolution Target |
|----------|---------|---------------|-------------------|
| S1 - Critical | Complete service outage, revenue impact | 15 min | 1 hour |
| S2 - High | Major feature degradation, user impact | 30 min | 4 hours |
| S3 - Medium | Minor issues, limited user impact | 2 hours | 24 hours |
| S4 - Low | Cosmetic issues, no user impact | 24 hours | 1 week |

### Incident Response Process

#### 1. Detection
```bash
# Monitor alerts
# Check Grafana dashboards
# Review CloudWatch alerts
# Monitor error rates in logs
```

#### 2. Assessment
```bash
# Document impact scope
# Check affected users
# Assess business impact
# Determine severity level
```

#### 3. Communication
```bash
# Alert on-call team
# Update status page
# Notify stakeholders
# Provide regular updates
```

#### 4. Resolution
```bash
# Implement temporary fixes
# Restore services
# Monitor stability
# Plan permanent fixes
```

#### 5. Post-Incident
```bash
# Document root cause
# Create improvement tickets
# Update monitoring
# Conduct post-mortem
```

### Incident Command Structure

- **Incident Commander**: Overall coordination
- **Technical Lead**: Technical resolution
- **Communications Lead**: Stakeholder communication
- **Subject Matter Experts**: Component-specific expertise

---

## Maintenance Runbooks

### Database Maintenance

#### PostgreSQL Maintenance
```bash
# Connect to database
kubectl exec -it -n knowledge-graph-analytics deployment/postgres -- psql -U raguser -d ragdb

# Update statistics
ANALYZE;

# Reindex tables
REINDEX DATABASE ragdb;

# Check table sizes
SELECT schemaname,tablename,pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size FROM pg_tables WHERE schemaname = 'public' ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;

# Check slow queries
SELECT query, calls, total_time, mean_time FROM pg_stat_statements ORDER BY total_time DESC LIMIT 10;

# Vacuum and analyze (run during maintenance window)
VACUUM ANALYZE;
```

#### Redis Maintenance
```bash
# Check memory usage
kubectl exec -n knowledge-graph-analytics deployment/redis -- redis-cli info memory

# Check key distribution
kubectl exec -n knowledge-graph-analytics deployment/redis -- redis-cli info keyspace

# Clear expired keys
kubectl exec -n knowledge-graph-analytics deployment/redis -- redis-cli --scan --pattern "expired:*" | xargs kubectl exec -n knowledge-graph-analytics deployment/redis -- redis-cli del

# Monitor performance
kubectl exec -n knowledge-graph-analytics deployment/redis -- redis-cli monitor
```

### Application Maintenance

#### Backend Application Updates
```bash
# Update application image
helm upgrade knowledge-graph-analytics ./infrastructure/helm/knowledge-graph-analytics \
    --namespace knowledge-graph-analytics \
    --set backend.image.tag=v1.2.0

# Monitor rollout
kubectl rollout status deployment/knowledge-graph-analytics-backend -n knowledge-graph-analytics

# Check new version
kubectl exec -n knowledge-graph-analytics deployment/knowledge-graph-analytics-backend -- python -c "import src; print(src.__version__)"
```

#### Frontend Application Updates
```bash
# Update frontend image
helm upgrade knowledge-graph-analytics ./infrastructure/helm/knowledge-graph-analytics \
    --namespace knowledge-graph-analytics \
    --set frontend.image.tag=v1.2.0

# Monitor rollout
kubectl rollout status deployment/knowledge-graph-analytics-frontend -n knowledge-graph-analytics

# Clear browser cache if needed
curl -X POST "https://analytics.yourdomain.com/api/cache-clear"
```

### Security Maintenance

#### Certificate Rotation
```bash
# Check certificate expiration
kubectl get certificates -n knowledge-graph-analytics

# Force certificate renewal (if needed)
kubectl delete certificate ssl-certificate -n knowledge-graph-analytics

# Verify new certificate
kubectl describe certificate ssl-certificate -n knowledge-graph-analytics
```

#### Secret Rotation
```bash
# Generate new secrets
./scripts/setup-secrets.sh rotate

# Restart services to use new secrets
kubectl rollout restart deployment/knowledge-graph-analytics-backend -n knowledge-graph-analytics
kubectl rollout restart deployment/knowledge-graph-analytics-frontend -n knowledge-graph-analytics
```

---

## Troubleshooting Guides

### High CPU Usage

#### Identify High CPU Pods
```bash
# Check pod CPU usage
kubectl top pods -n knowledge-graph-analytics --sort-by=cpu

# Check pod details
kubectl describe pod <high-cpu-pod> -n knowledge-graph-analytics

# Check pod processes
kubectl exec -n knowledge-graph-analytics <high-cpu-pod> -- top
```

#### Resolve High CPU Issues
```bash
# Scale up deployment
kubectl scale deployment <deployment-name> --replicas=<higher-count> -n knowledge-graph-analytics

# Add resource limits if missing
kubectl patch deployment <deployment-name> -n knowledge-graph-analytics -p '{
  "spec": {
    "template": {
      "spec": {
        "containers": [{
          "name": "<container-name>",
          "resources": {
            "limits": {
              "cpu": "2000m"
            }
          }
        }]
      }
    }
  }
}'

# Restart deployment
kubectl rollout restart deployment <deployment-name> -n knowledge-graph-analytics
```

### High Memory Usage

#### Identify Memory Leaks
```bash
# Check pod memory usage
kubectl top pods -n knowledge-graph-analytics --sort-by=memory

# Check memory usage over time
kubectl exec -n knowledge-graph-analytics <pod-name> -- cat /sys/fs/cgroup/memory/memory.usage_in_bytes

# Check for memory leaks in application
kubectl exec -n knowledge-graph-analytics <pod-name> -- python -c "
import psutil
import os
process = psutil.Process(os.getpid())
print(f'Memory usage: {process.memory_info().rss / 1024 / 1024:.2f} MB')
print(f'Memory percent: {process.memory_percent()}%')
"
```

#### Resolve Memory Issues
```bash
# Increase memory limits
kubectl patch deployment <deployment-name> -n knowledge-graph-analytics -p '{
  "spec": {
    "template": {
      "spec": {
        "containers": [{
          "name": "<container-name>",
          "resources": {
            "limits": {
              "memory": "4Gi"
            }
          }
        }]
      }
    }
  }
}'

# Restart pod to clear memory
kubectl delete pod <pod-name> -n knowledge-graph-analytics

# Enable horizontal pod autoscaler
kubectl autoscale deployment <deployment-name> \
    --cpu-percent=70 \
    --min=<min-replicas> \
    --max=<max-replicas> \
    -n knowledge-graph-analytics
```

### Database Connection Issues

#### PostgreSQL Connection Problems
```bash
# Check database connectivity
kubectl exec -n knowledge-graph-analytics deployment/backend -- python -c "
import psycopg2
try:
    conn = psycopg2.connect('postgresql://raguser:password@postgres:5432/ragdb')
    print('Connection successful')
    conn.close()
except Exception as e:
    print(f'Connection failed: {e}')
"

# Check database logs
kubectl logs -n knowledge-graph-analytics deployment/postgres --tail=50

# Check connection count
kubectl exec -n knowledge-graph-analytics deployment/postgres -- psql -U raguser -d ragdb -c "SELECT count(*) FROM pg_stat_activity;"

# Restart database if needed
kubectl rollout restart deployment/postgres -n knowledge-graph-analytics
```

#### Redis Connection Problems
```bash
# Test Redis connectivity
kubectl exec -n knowledge-graph-analytics deployment/redis -- redis-cli ping

# Check Redis logs
kubectl logs -n knowledge-graph-analytics deployment/redis --tail=50

# Check connection count
kubectl exec -n knowledge-graph-analytics deployment/redis -- redis-cli info clients

# Restart Redis if needed
kubectl rollout restart deployment/redis -n knowledge-graph-analytics
```

### Application Errors

#### Backend API Errors
```bash
# Check backend logs
kubectl logs -n knowledge-graph-analytics deployment/knowledge-graph-analytics-backend --tail=100

# Check for specific error patterns
kubectl logs -n knowledge-graph-analytics deployment/knowledge-graph-analytics-backend | grep ERROR

# Check application health
kubectl exec -n knowledge-graph-analytics deployment/knowledge-graph-analytics-backend -- curl localhost:8000/health

# Restart backend service
kubectl rollout restart deployment/knowledge-graph-analytics-backend -n knowledge-graph-analytics
```

#### Frontend Loading Issues
```bash
# Check frontend logs
kubectl logs -n knowledge-graph-analytics deployment/knowledge-graph-analytics-frontend --tail=100

# Check Nginx status
kubectl exec -n knowledge-graph-analytics deployment/knowledge-graph-analytics-frontend -- curl localhost:3000

# Check ingress configuration
kubectl describe ingress -n knowledge-graph-analytics

# Restart frontend service
kubectl rollout restart deployment/knowledge-graph-analytics-frontend -n knowledge-graph-analytics
```

---

## Performance Tuning

### Database Performance

#### PostgreSQL Optimization
```sql
-- Check slow queries
SELECT query, calls, total_time, mean_time, rows
FROM pg_stat_statements
ORDER BY total_time DESC
LIMIT 10;

-- Check missing indexes
SELECT schemaname, tablename, attname, n_distinct, correlation
FROM pg_stats
WHERE schemaname = 'public'
ORDER BY tablename, attname;

-- Create recommended indexes
CREATE INDEX CONCURRENTLY idx_table_column ON public.table_name (column_name);

-- Update statistics
ANALYZE public.table_name;
```

#### Redis Optimization
```bash
# Check Redis performance metrics
kubectl exec -n knowledge-graph-analytics deployment/redis -- redis-cli info stats

# Optimize memory usage
kubectl exec -n knowledge-graph-analytics deployment/redis -- redis-cli config set maxmemory-policy allkeys-lru

# Enable persistence if not already enabled
kubectl exec -n knowledge-graph-analytics deployment/redis -- redis-cli config set save "900 1 300 10 60 10000"
```

### Application Performance

#### Backend Optimization
```bash
# Check application response times
kubectl exec -n knowledge-graph-analytics deployment/knowledge-graph-analytics-backend -- curl -w "@curl-format.txt" -o /dev/null -s "http://localhost:8000/api/health"

# Monitor Python memory usage
kubectl exec -n knowledge-graph-analytics deployment/knowledge-graph-analytics-backend -- python -c "
import tracemalloc
import gc
tracemalloc.start()
gc.collect()
snapshot = tracemalloc.take_snapshot()
top_stats = snapshot.statistics('lineno')
for stat in top_stats[:10]:
    print(stat)
"

# Enable application profiling if needed
kubectl patch deployment knowledge-graph-analytics-backend -n knowledge-graph-analytics -p '{
  "spec": {
    "template": {
      "spec": {
        "containers": [{
          "name": "backend",
          "env": [{
            "name": "PROFILING_ENABLED",
            "value": "true"
          }]
        }]
      }
    }
  }
}'
```

#### Frontend Optimization
```bash
# Check bundle size
kubectl exec -n knowledge-graph-analytics deployment/knowledge-graph-analytics-frontend -- du -sh /usr/share/nginx/html/

# Enable gzip compression
kubectl patch deployment knowledge-graph-analytics-frontend -n knowledge-graph-analytics -p '{
  "spec": {
    "template": {
      "spec": {
        "containers": [{
          "name": "frontend",
          "env": [{
            "name": "NGINX_GZIP",
            "value": "on"
          }]
        }]
      }
    }
  }
}'
```

### Infrastructure Scaling

#### Cluster Scaling
```bash
# Add nodes to cluster
aws eks update-nodegroup-config \
    --cluster-name knowledge-graph-analytics-cluster \
    --nodegroup-name standard-workers \
    --scaling-config minSize=3,maxSize=20,desiredSize=10

# Enable cluster autoscaler
kubectl apply -f https://github.com/kubernetes/autoscaler/releases/download/cluster-autoscaler-1.28.0/cluster-autoscaler.yaml
```

#### Resource Optimization
```bash
# Set resource requests and limits
kubectl patch deployment <deployment-name> -n knowledge-graph-analytics -p '{
  "spec": {
    "template": {
      "spec": {
        "containers": [{
          "name": "<container-name>",
          "resources": {
            "requests": {
              "memory": "512Mi",
              "cpu": "250m"
            },
            "limits": {
              "memory": "2Gi",
              "cpu": "1000m"
            }
          }
        }]
      }
    }
  }
}'
```

---

## Monitoring and Alerting

### Key Metrics to Monitor

#### Application Metrics
- Response time (p95, p99)
- Error rate
- Throughput (requests/second)
- Active users

#### Infrastructure Metrics
- CPU usage
- Memory usage
- Disk usage
- Network I/O

#### Database Metrics
- Connection count
- Query performance
- Index usage
- Transaction rate

### Alert Thresholds

#### Critical Alerts
- Error rate > 5%
- Response time p95 > 2 seconds
- CPU usage > 90%
- Memory usage > 90%
- Database connections > 80%

#### Warning Alerts
- Error rate > 1%
- Response time p95 > 1 second
- CPU usage > 70%
- Memory usage > 70%
- Database connections > 60%

### Escalation Procedures

#### Level 1: On-call Engineer
- Initial response
- Basic troubleshooting
- Document progress

#### Level 2: DevOps Team
- Complex issues
- Infrastructure problems
- Performance optimization

#### Level 3: Engineering Team
- Application bugs
- Architecture issues
- Feature development

These runbooks should be regularly reviewed and updated based on incident learnings and system changes.
# Monitoring System Troubleshooting Guide

> **Status: historical (reconciled 2026-10-17).** This document describes an observability design or setup; whether it is deployed on the AWS cluster is not verified. Alerting that Terraform defines for AWS resources is in [infrastructure/terraform/monitoring.tf](../../../infrastructure/terraform/monitoring.tf). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Start with the [AWS deployment overview](../../deployment/aws-overview.md) and [AWS on-call commands](../aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

## Quick Fixes for Common Issues

### 1. Docker Compose Warnings Fixed ✅

**Issue**: `WARN[0000] /Users/goodwiinz/development/RAG_system/rag/docker-compose.yml: the attribute 'version' is obsolete`

**Solution**: ✅ **FIXED** - Removed the obsolete `version: '3.8'` attribute from docker-compose.yml

**Issue**: `services.deploy.replicas: can't set container_name and celery-worker as container name must be unique`

**Solution**: ✅ **FIXED** - Removed `container_name` from celery-worker service since it uses `replicas: 2`

**Issue**: Duplicate `command` keys in monitoring docker-compose

**Solution**: ✅ **FIXED** - Consolidated Prometheus configuration into single command section

### 2. Environment Variable Warnings

**Issue**: `The "GRAFANA_ADMIN_USER" variable is not set`

**Solution**: Create environment file for monitoring:
```bash
cd monitoring
cp .env.example .env
# Edit .env with your values
```

## 3. Running the Monitoring System

### Option A: Complete System Startup (Recommended)
```bash
# This starts EVERYTHING including your RAG app and monitoring
./START_MONITORED_SYSTEM.sh
```

### Option B: Monitoring Only
```bash
# Just monitoring stack (if your app is already running)
./QUICK_START_MONITORING.sh
```

### Option C: Manual Start
```bash
# Start main RAG system
docker-compose up -d

# Start monitoring stack
cd monitoring
docker-compose -f docker-compose.monitoring.yml up -d
```

## 4. Validation

Run the validation script to check everything is working:
```bash
./validate_docker_compose.sh
```

This will check:
- ✅ Docker installation and status
- ✅ Docker Compose configuration validity
- ✅ Environment files
- ✅ Common configuration issues
- ✅ Disk space availability

## 5. Access URLs

Once running, access your monitoring at:

| Service | URL | Notes |
|---------|-----|-------|
| **Grafana Dashboards** | http://localhost:3001 | admin/admin (change password) |
| **Prometheus** | http://localhost:9090 | Metrics collection |
| **Jaeger Tracing** | http://localhost:16686 | Distributed traces |
| **Kibana Logs** | http://localhost:5601 | Log analysis |
| **AlertManager** | http://localhost:9093 | Alert management |
| **RAG Frontend** | http://localhost:3000 | Your application |
| **RAG Backend** | http://localhost:8000 | API endpoints |

## 6. Common Problems and Solutions

### Services not starting
```bash
# Check what's running
docker-compose ps
docker-compose -f monitoring/docker-compose.monitoring.yml ps

# Check logs
docker-compose logs [service-name]
docker-compose -f monitoring/docker-compose.monitoring.yml logs [service-name]

# Restart specific service
docker-compose restart [service-name]
```

### Port conflicts
```bash
# Check what's using ports
netstat -tulpn | grep :3000
netstat -tulpn | grep :9090

# Stop conflicting services and restart
./STOP_ALL_SERVICES.sh
./START_MONITORED_SYSTEM.sh
```

### Permission issues
```bash
# Fix Docker permissions
sudo usermod -aG docker $USER
# Log out and log back in

# Or run with sudo (not recommended)
sudo docker-compose up -d
```

### Disk space issues
```bash
# Check Docker disk usage
docker system df

# Clean up unused images/containers
docker system prune -a

# Check available space
df -h
```

## 7. Performance Optimization

### If monitoring is slow:
1. **Reduce Prometheus retention**:
   ```bash
   # Edit monitoring/.env
   PROMETHEUS_RETENTION=7d  # Reduce from 15d
   ```

2. **Increase memory limits**:
   ```bash
   # Edit monitoring/docker-compose.monitoring.yml
   deploy:
     resources:
       limits:
         memory: 4G  # Increase from 2G
   ```

3. **Optimize Grafana dashboards**:
   - Increase refresh intervals
   - Simplify queries
   - Enable caching

## 8. Monitoring Health Checks

### Check if services are healthy:
```bash
# Backend health
curl http://localhost:8000/health

# Frontend health
curl http://localhost:3000

# Prometheus health
curl http://localhost:9090/-/healthy

# Grafana health
curl http://localhost:3001/api/health
```

### Check metrics collection:
```bash
# View backend metrics
curl http://localhost:8000/metrics

# Check Prometheus targets
curl http://localhost:9090/api/v1/targets
```

## 9. Logs and Debugging

### View logs in real-time:
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f backend
docker-compose logs -f frontend

# Monitoring services
cd monitoring
docker-compose -f docker-compose.monitoring.yml logs -f grafana
docker-compose -f docker-compose.monitoring.yml logs -f prometheus
```

### Check application logs:
```bash
# Backend logs
tail -f logs/backend.log

# Frontend logs
tail -f logs/frontend.log
```

## 10. Security Considerations

### Change default passwords:
```bash
# Edit monitoring/.env
GRAFANA_ADMIN_PASSWORD=your-secure-password
REDIS_PASSWORD=your-redis-password
```

### Enable SSL in production:
- Configure reverse proxy (nginx/traefik)
- Obtain SSL certificates
- Update URLs to https://

## 11. Getting Help

If you encounter issues:

1. **Run validation**: `./validate_docker_compose.sh`
2. **Check logs**: Look at service logs for errors
3. **Consult documentation**: `docs/RUN_MONITORING.md`
4. **Check resources**: Ensure sufficient disk space and memory
5. **Restart services**: Try a clean restart with `./STOP_ALL_SERVICES.sh` then `./START_MONITORED_SYSTEM.sh`

## 12. Quick Reference Commands

```bash
# 🚀 Start everything
./START_MONITORED_SYSTEM.sh

# 🛑 Stop everything
./STOP_ALL_SERVICES.sh

# 📊 Start monitoring only
./QUICK_START_MONITORING.sh

# ✅ Validate configuration
./validate_docker_compose.sh

# 📋 Check status
docker-compose ps

# 📝 View logs
docker-compose logs -f

# 🔄 Restart service
docker-compose restart [service-name]
```

---

Your monitoring system is now ready to provide comprehensive observability for your Multimodal Enterprise RAG System! 🎉
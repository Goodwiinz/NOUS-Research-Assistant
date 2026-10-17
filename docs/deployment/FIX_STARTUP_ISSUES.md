# Fixing Startup Issues

> **Status: historical (reconciled 2026-10-17).** This guide was written for earlier deployment generations (Docker Compose, single-host or DigitalOcean/DOKS setups). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Qdrant is removed; live retrieval is PostgreSQL full-text. Start with the [AWS deployment overview](aws-overview.md) and [AWS on-call commands](../operations/aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

## Problems Identified

### Problem 1: Docker Volume Warning
```
WARN[0000] volume "rag_neo4j_data" already exists but was not created by Docker Compose. Use `external: true` to use an existing volume
```

### Problem 2: Database Connection Error
```
ERROR: could not translate host name "postgres" to address: Name or service not known
```

## Root Causes

1. **Docker Volume Issue**: Existing Neo4j volume wasn't created by Docker Compose
2. **Network Resolution**: Backend can't resolve "postgres" hostname (running outside Docker network)
3. **Missing Function**: `print_error` function not defined in startup script

## 🛠️ Solutions

### Solution 1: Fix Docker Compose Volumes

Add `external: true` for existing volumes in `docker-compose.yml`:

```yaml
volumes:
  rag_neo4j_data:
    external: true
  rag_postgres_data:
    external: true
  rag_redis_data:
    external: true
  rag_qdrant_data:
    external: true
```

### Solution 2: Fix Database Hostnames

Backend needs to use Docker network hostnames when running inside containers, but localhost when running outside.

### Solution 3: Fix Startup Script

Add missing `print_error` function to startup script.

## 🚀 Quick Fix Commands

### Option 1: Clean Restart (Recommended)
```bash
# Stop all services
docker-compose down -v

# Remove existing volumes (WARNING: This deletes data!)
docker volume rm rag_rag_neo4j_data rag_rag_postgres_data rag_rag_redis_data rag_rag_qdrant_data

# Start fresh
./START_MONITORED_SYSTEM.sh
```

### Option 2: Keep Data, Fix Configuration
```bash
# Fix docker-compose.yml volumes section
# Then restart services
docker-compose down
docker-compose up -d
```

### Option 3: Run Backend Locally (Development)
```bash
# Start databases with Docker
docker-compose up -d postgres neo4j qdrant redis

# Run backend locally (not in Docker)
cd backend
source .venv/bin/activate
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

# Update .env to use localhost
DATABASE_URL=postgresql://raguser:password@localhost:5432/ragdb
```

## 📋 Detailed Steps

### Step 1: Fix Docker Compose Volumes

Edit `docker-compose.yml` and add:

```yaml
volumes:
  rag_neo4j_data:
    external: true
  rag_postgres_data:
    external: true
  rag_redis_data:
    external: true
  rag_qdrant_data:
    external: true
```

### Step 2: Update Environment Variables

Edit `.env` to use correct hostnames:

```bash
# If backend runs in Docker
DATABASE_URL=postgresql://raguser:${DB_PASSWORD}@postgres:5432/ragdb
NEO4J_URI=bolt://neo4j:7687
QDRANT_URL=http://qdrant:6333
REDIS_URL=redis://redis:6379/0

# If backend runs locally
DATABASE_URL=postgresql://raguser:${DB_PASSWORD}@localhost:5432/ragdb
NEO4J_URI=bolt://localhost:7687
QDRANT_URL=http://localhost:6333
REDIS_URL=redis://localhost:6379/0
```

### Step 3: Fix Startup Script

Add missing functions to `START_MONITORED_SYSTEM.sh`:

```bash
# Add at the top of the script
print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}
```

## 🔍 Verification

### Check Docker Network
```bash
# Check network exists
docker network ls | grep rag_rag-network

# Check containers are in network
docker network inspect rag_rag-network
```

### Check Database Connectivity
```bash
# Test PostgreSQL connection
docker exec rag-postgres psql -U raguser -d ragdb -c "SELECT 1;"

# Test Neo4j connection
docker exec rag-neo4j cypher-shell -u neo4j -p password "RETURN 1;"

# Test Redis connection
docker exec rag-redis redis-cli ping

# Test Qdrant connection
curl http://localhost:6333/collections
```

### Check Backend Logs
```bash
# View backend logs
docker logs rag-backend

# Or if running locally
tail -f logs/backend.log
```

## 🎯 Recommended Approach

### For Development (Easiest)
```bash
# 1. Stop everything
docker-compose down

# 2. Start only databases
docker-compose up -d postgres neo4j qdrant redis

# 3. Run backend locally
cd backend
source .venv/bin/activate
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

# 4. Run frontend
cd frontend
npm run dev
```

### For Production
```bash
# 1. Fix docker-compose.yml volumes
# 2. Use external volumes
# 3. Everything runs in Docker containers
./START_MONITORED_SYSTEM.sh
```

The issues are typically caused by mixing local development with Docker container networking. Choose one approach and stick with it!
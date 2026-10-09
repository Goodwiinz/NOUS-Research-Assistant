# Document Upload Feature Deployment Guide

## Overview

This guide covers the deployment of the enhanced Document Upload with Automatic Knowledge Graph Population feature for the Multimodal Enterprise RAG System.

## Architecture Components

### Backend Services
- **FastAPI Application**: Document upload API endpoints
- **Enhanced Document Processing Service**: Multimodal processing pipeline
- **Knowledge Graph Service**: Neo4j integration
- **Vector Store Service**: Qdrant embeddings
- **File Storage Service**: MinIO integration
- **WebSocket Service**: Real-time progress updates

### Frontend Components
- **Enhanced Document Upload Zone**: Drag-and-drop interface
- **Progress Tracking Components**: Real-time status updates
- **Error Handling System**: Comprehensive error management
- **Quality Assessment UI**: Document quality metrics

### External Dependencies
- **Neo4j**: Knowledge graph storage
- **Qdrant**: Vector embeddings
- **MinIO**: File storage
- **Redis**: Caching and session management
- **PostgreSQL**: Metadata and job tracking

## Prerequisites

### System Requirements
- **CPU**: 4+ cores recommended
- **Memory**: 16GB+ RAM (8GB minimum)
- **Storage**: 100GB+ available space
- **Network**: Stable internet connection

### Software Dependencies
- Docker & Docker Compose
- Python 3.9+
- Node.js 18+
- PostgreSQL 14+
- Neo4j 5.x
- Qdrant 1.x
- MinIO

### ML Models (Optional but Recommended)
```bash
# spaCy English model
python -m spacy download en_core_web_sm

# Sentence Transformers
pip install sentence-transformers

# PyTorch (for embeddings)
pip install torch torchvision torchaudio
```

## Configuration

### Environment Variables

Create a `.env` file with the following variables:

```bash
# Application Settings
DEBUG=false
SECRET_KEY=your-secret-key-here
ALLOWED_HOSTS=localhost,127.0.0.1,your-domain.com

# Database Configuration
DATABASE_URL=postgresql://user:password@localhost:5432/rag_system
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-neo4j-password
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=your-qdrant-api-key
REDIS_URL=redis://localhost:6379

# File Storage (MinIO)
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=your-minio-access-key
MINIO_SECRET_KEY=your-minio-secret-key
MINIO_BUCKET_NAME=document-storage
MINIO_SECURE=false

# AI Services
OPENAI_API_KEY=your-openai-api-key
ANTHROPIC_API_KEY=your-anthropic-api-key

# Processing Settings
MAX_FILE_SIZE_MB=50
MAX_FILES_PER_UPLOAD=10
PROCESSING_TIMEOUT_SECONDS=600
ENABLE_OCR=true
ENABLE_ENTITY_EXTRACTION=true
ENABLE_EMBEDDING_GENERATION=true

# Security
ENABLE_VIRUS_SCANNING=true
ENABLE_CONTENT_VALIDATION=true
UPLOAD_RATE_LIMIT=100

# Monitoring
ENABLE_METRICS=true
SENTRY_DSN=your-sentry-dsn
LOG_LEVEL=INFO
```

### Database Configuration

Update the database configuration in `src/core/config.py`:

```python
class Settings:
    # Document upload settings
    MAX_FILE_SIZE_MB: int = 50
    MAX_FILES_PER_UPLOAD: int = 10
    PROCESSING_TIMEOUT_SECONDS: int = 600

    # Processing features
    ENABLE_OCR: bool = True
    ENABLE_ENTITY_EXTRACTION: bool = True
    ENABLE_EMBEDDING_GENERATION: bool = True
    ENABLE_QUALITY_ASSESSMENT: bool = True

    # Security settings
    ENABLE_VIRUS_SCANNING: bool = True
    ENABLE_CONTENT_VALIDATION: bool = True
    UPLOAD_RATE_LIMIT: int = 100
```

## Deployment Steps

### 1. Database Setup

#### PostgreSQL
```bash
# Apply database migrations
python -m alembic upgrade head

# Apply document upload schema
psql -h localhost -U postgres -d rag_system -f database/migrations/006_document_upload_schema.sql
```

#### Neo4j
```bash
# Connect to Neo4j and setup constraints
cypher-shell -u neo4j -p your-password
CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE;
CREATE CONSTRAINT document_id_unique IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE;
```

#### Qdrant
```bash
# Create collections for embeddings
curl -X PUT "http://localhost:6333/collections/document_embeddings" \
     -H "Content-Type: application/json" \
     -d '{
       "vectors": {
         "size": 384,
         "distance": "Cosine"
       }
     }'
```

### 2. Backend Deployment

#### Using Docker Compose
```yaml
# docker-compose.enhanced.yml
version: '3.8'

services:
  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - NEO4J_URI=${NEO4J_URI}
      - QDRANT_URL=${QDRANT_URL}
      - MINIO_ENDPOINT=${MINIO_ENDPOINT}
      - MINIO_ACCESS_KEY=${MINIO_ACCESS_KEY}
      - MINIO_SECRET_KEY=${MINIO_SECRET_KEY}
    ports:
      - "8000:8000"
    depends_on:
      - postgres
      - neo4j
      - qdrant
      - minio
      - redis
    volumes:
      - ./backend:/app
      - ./models:/app/models

  worker:
    build:
      context: ./backend
      dockerfile: Dockerfile.worker
    command: celery -A src.tasks.celery_app worker --loglevel=info
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - NEO4J_URI=${NEO4J_URI}
      - QDRANT_URL=${QDRANT_URL}
    depends_on:
      - postgres
      - redis
      - neo4j
      - qdrant
    volumes:
      - ./backend:/app
      - ./models:/app/models

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    ports:
      - "3000:3000"
    environment:
      - NEXT_PUBLIC_API_URL=http://localhost:8000
      - NEXT_PUBLIC_WS_URL=ws://localhost:8000
    depends_on:
      - backend

  postgres:
    image: postgres:14
    environment:
      - POSTGRES_DB=rag_system
      - POSTGRES_USER=postgres
      - POSTGRES_PASSWORD=postgres
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./database/migrations:/docker-entrypoint-initdb.d
    ports:
      - "5432:5432"

  neo4j:
    image: neo4j:5.12-community
    environment:
      - NEO4J_AUTH=neo4j/your-password
      - NEO4J_PLUGINS=["apoc"]
      - NEO4J_dbms_security_procedures_unrestricted=apoc.*
    volumes:
      - neo4j_data:/data
      - neo4j_logs:/logs
      - neo4j_import:/var/lib/neo4j/import
    ports:
      - "7474:7474"
      - "7687:7687"

  qdrant:
    image: qdrant/qdrant:v1.3.0
    ports:
      - "6333:6333"
    volumes:
      - qdrant_data:/qdrant/storage

  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      - MINIO_ROOT_USER=minioadmin
      - MINIO_ROOT_PASSWORD=minioadmin
    volumes:
      - minio_data:/data
    ports:
      - "9000:9000"
      - "9001:9001"

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  neo4j_data:
  neo4j_logs:
  neo4j_import:
  qdrant_data:
  minio_data:
  redis_data:
```

#### Deploy with Docker Compose
```bash
# Start all services
docker-compose -f docker-compose.enhanced.yml up -d

# Check service status
docker-compose -f docker-compose.enhanced.yml ps

# View logs
docker-compose -f docker-compose.enhanced.yml logs -f backend
```

### 3. Frontend Deployment

#### Build for Production
```bash
cd frontend

# Install dependencies
npm install

# Build for production
npm run build

# Start production server
npm start
```

#### Environment Configuration
Create `.env.production`:
```bash
NEXT_PUBLIC_API_URL=https://your-api-domain.com
NEXT_PUBLIC_WS_URL=wss://your-api-domain.com
NEXT_PUBLIC_MAX_FILE_SIZE_MB=50
NEXT_PUBLIC_SUPPORTED_FORMATS=pdf,txt,docx,jpg,png,mp3,wav,mp4,mov
```

### 4. Monitoring and Logging

#### Application Monitoring
```python
# Add to src/main.py
from prometheus_client import Counter, Histogram, generate_latest

# Metrics
UPLOAD_COUNTER = Counter('document_uploads_total', 'Total document uploads')
PROCESSING_DURATION = Histogram('document_processing_seconds', 'Document processing duration')
ERROR_COUNTER = Counter('document_errors_total', 'Total document processing errors')

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type="text/plain")
```

#### Health Checks
```python
# Add to src/api/health.py
@router.get("/health")
async def health_check():
    checks = {
        "database": await check_database(),
        "neo4j": await check_neo4j(),
        "qdrant": await check_qdrant(),
        "minio": await check_minio(),
        "redis": await check_redis()
    }

    all_healthy = all(checks.values())
    status_code = 200 if all_healthy else 503

    return JSONResponse(
        content={"status": "healthy" if all_healthy else "unhealthy", "checks": checks},
        status_code=status_code
    )
```

## Security Configuration

### 1. File Upload Security
```python
# Enforced in src/services/documents/file_service.py (FileService.validate_file);
# the former src/middleware/file_upload_security.py was dead code, deleted in I10.
MAX_FILE_SIZE_MB = 50
ALLOWED_MIME_TYPES = [
    "application/pdf",
    "text/plain",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/jpeg",
    "image/png",
    "audio/mpeg",
    "audio/wav",
    "video/mp4",
    "video/quicktime"
]
ENABLE_VIRUS_SCANNING = True
```

### 2. Rate Limiting
```python
# Configure in src/middleware/rate_limiting.py
UPLOAD_RATE_LIMIT = 100  # uploads per hour per user
API_RATE_LIMIT = 1000    # requests per hour per user
```

### 3. Authentication
```python
# JWT Configuration
JWT_SECRET_KEY = your-super-secret-jwt-key
JWT_ALGORITHM = HS256
JWT_EXPIRATION_HOURS = 24
```

## Performance Optimization

### 1. Database Optimization
```sql
-- Create optimized indexes
CREATE INDEX CONCURRENTLY idx_documents_processing_status
ON documents(status) WHERE status != 'deleted';

CREATE INDEX CONCURRENTLY idx_processing_jobs_priority_created
ON document_processing_jobs(priority DESC, created_at);

-- Partition large tables
CREATE TABLE processing_logs_2024 PARTITION OF processing_logs
FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
```

### 2. Caching Strategy
```python
# Redis caching configuration
CACHE_TTL_DOCUMENTS = 3600      # 1 hour
CACHE_TTL_ENTITIES = 7200       # 2 hours
CACHE_TTL_EMBEDDINGS = 86400    # 24 hours
```

### 3. Async Processing
```python
# Celery configuration
CELERY_BROKER_URL = "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = "redis://localhost:6379/0"
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"
```

## Testing

### 1. Run Integration Tests
```bash
# Install test dependencies
pip install -r requirements.test.txt

# Run all tests
pytest tests/integration/test_document_upload_pipeline.py -v

# Run with coverage
pytest tests/integration/test_document_upload_pipeline.py --cov=src --cov-report=html
```

### 2. Load Testing
```python
# Using Locust for load testing
from locust import HttpUser, task, between

class DocumentUploadUser(HttpUser):
    wait_time = between(1, 3)

    @task
    def upload_document(self):
        files = {
            "file": ("test.pdf", open("test.pdf", "rb"), "application/pdf")
        }
        data = {
            "title": "Load Test Document",
            "description": "Document for load testing"
        }

        self.client.post("/api/v2/documents/upload/single", files=files, data=data)
```

### 3. Performance Testing
```bash
# Run load test
locust -f tests/performance/locustfile.py --host=http://localhost:8000

# Run with 100 users for 10 minutes
locust -f tests/performance/locustfile.py --host=http://localhost:8000 \
       --users=100 --spawn-rate=10 --run-time=10m
```

## Maintenance

### 1. Database Maintenance
```bash
# Clean up old processing logs
DELETE FROM processing_logs WHERE created_at < NOW() - INTERVAL '30 days';

# Clean up failed jobs
DELETE FROM document_processing_jobs
WHERE status = 'failed' AND created_at < NOW() - INTERVAL '7 days';

# Update statistics
ANALYZE documents;
ANALYZE document_processing_jobs;
```

### 2. File Storage Cleanup
```bash
# Clean up orphaned files in MinIO
python scripts/cleanup_orphaned_files.py

# Monitor storage usage
docker exec minio_container mc du document-storage
```

### 3. Health Monitoring
```bash
# Setup monitoring alerts
# Example using Prometheus + Grafana
# Alert on high processing failure rate
# Alert on storage space usage > 80%
# Alert on high memory usage
```

## Troubleshooting

### Common Issues

#### 1. Upload Fails with File Size Error
```bash
# Check MAX_FILE_SIZE_MB setting
grep MAX_FILE_SIZE_MB .env

# Verify nginx configuration (if using reverse proxy)
# client_max_body_size 100M;
```

#### 2. Processing Jobs Stuck in Pending
```bash
# Check Celery worker status
docker-compose exec backend celery -A src.tasks.celery_app inspect active

# Restart worker if needed
docker-compose restart worker
```

#### 3. WebSocket Connection Issues
```bash
# Check WebSocket URL configuration
grep NEXT_PUBLIC_WS_URL frontend/.env.production

# Verify firewall allows WebSocket connections
# wss:// on port 443 or ws:// on port 8000
```

#### 4. Memory Issues with Large Files
```bash
# Monitor memory usage
docker stats

# Increase memory limits in docker-compose.yml
services:
  backend:
    deploy:
      resources:
        limits:
          memory: 4G
        reservations:
          memory: 2G
```

### Debug Commands
```bash
# View backend logs
docker-compose logs -f backend

# View worker logs
docker-compose logs -f worker

# Check database connections
docker-compose exec postgres psql -U postgres -d rag_system -c "SELECT count(*) FROM documents;"

# Check Neo4j connectivity
docker-compose exec neo4j cypher-shell -u neo4j -p your-password "MATCH (n) RETURN count(n);"
```

## Backup and Recovery

### 1. Database Backup
```bash
# PostgreSQL backup
docker-compose exec postgres pg_dump -U postgres rag_system > backup_$(date +%Y%m%d).sql

# Neo4j backup
docker-compose exec neo4j neo4j-admin backup --backup-dir=/backup --name=rag_backup

# Qdrant backup
curl -X POST "http://localhost:6333/collections/document_embeddings/snapshots"
```

### 2. File Storage Backup
```bash
# MinIO backup using mc
docker-compose exec minio mc mirror document-storage /backup/minio/

# Or use rsync
rsync -av /var/lib/minio/data/ backup/minio/
```

### 3. Recovery Procedure
```bash
# Restore PostgreSQL
docker-compose exec -T postgres psql -U postgres rag_system < backup_20241220.sql

# Restore Neo4j
docker-compose exec neo4j neo4j-admin restore --from-dir=/backup --name=rag_backup

# Restart services
docker-compose restart
```

## Scaling Considerations

### 1. Horizontal Scaling
```yaml
# Scale backend and worker services
services:
  backend:
    deploy:
      replicas: 3

  worker:
    deploy:
      replicas: 5
```

### 2. Database Scaling
```python
# Read replicas for read-heavy operations
DATABASE_READ_REPLICAS = [
    "postgresql://user:pass@replica1:5432/rag_system",
    "postgresql://user:pass@replica2:5432/rag_system"
]
```

### 3. CDN Integration
```nginx
# Serve static files via CDN
location /uploads/ {
    proxy_pass https://your-cdn-domain.com/;
}
```

This comprehensive deployment guide covers all aspects of deploying the enhanced document upload feature with proper configuration, monitoring, and maintenance procedures.
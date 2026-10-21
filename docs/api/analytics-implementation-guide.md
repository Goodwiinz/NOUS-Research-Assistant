# Analytics Dashboard Implementation Guide

> **Stale / historical (2026-10-17):** It covers only part of the API and its paths and schemas may have drifted. The source of truth is the generated `backend/openapi.json` (also served at `/docs` when `DEBUG` is on). Current contracts: [`docs/engineering/`](../engineering/README.md).

## Quick Start

This guide provides step-by-step instructions for implementing the Knowledge Graph Analytics Dashboard backend services.

## Prerequisites

### System Requirements
- Python 3.9+
- PostgreSQL 14+
- Redis 6+
- Neo4j 4.4+
- RabbitMQ 3.9+ or Redis Streams
- Node.js 16+ (for some tooling)

### Existing Dependencies
The system integrates with the existing Multimodal Enterprise RAG System components:
- Database schema from migration 007
- Existing authentication patterns
- Current service architecture
- Existing monitoring infrastructure

## Implementation Steps

### Step 1: Database Setup

The analytics schema is already defined in `database/migrations/007_knowledge_graph_analytics_dashboard.sql`.

To apply the migration:
```bash
# Run the migration
cd /path/to/project
psql -h localhost -U postgres -d multimodal_rag -f database/migrations/007_knowledge_graph_analytics_dashboard.sql

# Verify tables were created
psql -h localhost -U postgres -d multimodal_rag -c "\dt analytics_*"
```

Key tables created:
- `entity_analytics` - Entity metrics aggregations
- `relationship_analytics` - Relationship analytics
- `graph_metrics_analytics` - Computed graph metrics
- `document_analytics` - Document processing metrics
- `user_interaction_analytics` - User behavior analytics
- `dashboard_configurations` - Dashboard configurations
- `custom_analytics_reports` - Report definitions
- `analytics_cache` - Computation result cache

### Step 2: Core Service Implementation

#### 2.1 Analytics Service Structure
Create the service structure:
```bash
mkdir -p backend/src/services/analytics
cd backend/src/services/analytics

# Create service modules
touch __init__.py
touch realtime_service.py
touch graph_analytics_service.py
touch dashboard_service.py
touch report_service.py
touch alerting_service.py
touch cache_service.py
```

#### 2.2 Base Service Class
Create `backend/src/services/analytics/base_service.py`:
```python
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
import asyncio
import logging

from src.core.database import get_db_session
from src.core.config import settings
from src.services.cache_service import RedisConnection

logger = logging.getLogger(__name__)

class BaseAnalyticsService(ABC):
    """Base class for all analytics services"""

    def __init__(self):
        self.redis = RedisConnection()
        self.logger = logging.getLogger(self.__class__.__name__)

    async def get_db_session(self):
        """Get database session"""
        async with get_db_session() as session:
            yield session

    @abstractmethod
    async def initialize(self):
        """Initialize service-specific resources"""
        pass

    async def cleanup(self):
        """Cleanup service resources"""
        pass

    async def health_check(self) -> Dict[str, Any]:
        """Basic health check for the service"""
        try:
            # Test Redis connection
            await self.redis.ping()
            redis_status = "healthy"
        except Exception as e:
            redis_status = f"unhealthy: {e}"

        return {
            "service": self.__class__.__name__,
            "status": "healthy" if redis_status == "healthy" else "degraded",
            "redis": redis_status
        }
```

### Step 3: Real-time Analytics Service

#### 3.1 Service Implementation
Create `backend/src/services/analytics/realtime_service.py`:
```python
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from sqlalchemy import select, func
from src.models.analytics import (
    EntityAnalytics, RelationshipAnalytics,
    DocumentAnalytics, UserInteractionAnalytics
)
from .base_service import BaseAnalyticsService

class RealtimeAnalyticsService(BaseAnalyticsService):
    """Real-time analytics computation service"""

    def __init__(self):
        super().__init__()
        self.cache_ttl = 30  # 30 seconds

    async def initialize(self):
        """Initialize real-time analytics service"""
        self.logger.info("Real-time analytics service initialized")

    async def get_metrics(self, organization_id: str) -> Dict[str, Any]:
        """Get real-time metrics for organization"""

        # Check cache first
        cache_key = f"analytics:realtime:metrics:{organization_id}"
        cached_data = await self.redis.get(cache_key)

        if cached_data:
            return json.loads(cached_data)

        # Compute fresh metrics
        async with self.get_db_session() as session:
            metrics = await self._compute_metrics(session, organization_id)

        # Cache result
        await self.redis.setex(
            cache_key,
            self.cache_ttl,
            json.dumps(metrics)
        )

        return metrics

    async def _compute_metrics(self, session, organization_id: str) -> Dict[str, Any]:
        """Compute current metrics from database"""

        # Get latest entity analytics
        entity_query = select(EntityAnalytics).where(
            EntityAnalytics.organization_id == organization_id,
            EntityAnalytics.time_bucket >= datetime.utcnow() - timedelta(hours=1)
        ).order_by(EntityAnalytics.time_bucket.desc()).limit(1)

        entity_result = await session.execute(entity_query)
        entity_analytics = entity_result.scalar_one_or_none()

        # Get latest relationship analytics
        rel_query = select(RelationshipAnalytics).where(
            RelationshipAnalytics.organization_id == organization_id,
            RelationshipAnalytics.time_bucket >= datetime.utcnow() - timedelta(hours=1)
        ).order_by(RelationshipAnalytics.time_bucket.desc()).limit(1)

        rel_result = await session.execute(rel_query)
        rel_analytics = rel_result.scalar_one_or_none()

        # Get latest document analytics
        doc_query = select(DocumentAnalytics).where(
            DocumentAnalytics.organization_id == organization_id,
            DocumentAnalytics.time_bucket >= datetime.utcnow() - timedelta(hours=1)
        ).order_by(DocumentAnalytics.time_bucket.desc()).limit(1)

        doc_result = await session.execute(doc_query)
        doc_analytics = doc_result.scalar_one_or_none()

        # Get latest user interaction analytics
        user_query = select(UserInteractionAnalytics).where(
            UserInteractionAnalytics.organization_id == organization_id,
            UserInteractionAnalytics.time_bucket >= datetime.utcnow() - timedelta(hours=1)
        ).order_by(UserInteractionAnalytics.time_bucket.desc()).limit(1)

        user_result = await session.execute(user_query)
        user_analytics = user_result.scalar_one_or_none()

        return {
            "organization_id": organization_id,
            "timestamp": datetime.utcnow().isoformat(),
            "metrics": {
                "entities": {
                    "total": entity_analytics.total_entities if entity_analytics else 0,
                    "new_today": entity_analytics.new_entities if entity_analytics else 0,
                    "avg_confidence": float(entity_analytics.avg_confidence_score) if entity_analytics else 0.0
                },
                "relationships": {
                    "total": rel_analytics.total_relationships if rel_analytics else 0,
                    "new_today": rel_analytics.new_relationships if rel_analytics else 0,
                    "avg_confidence": float(rel_analytics.avg_confidence_score) if rel_analytics else 0.0
                },
                "documents": {
                    "total": doc_analytics.total_documents if doc_analytics else 0,
                    "processed_today": doc_analytics.processed_documents if doc_analytics else 0,
                    "processing_success_rate": float(doc_analytics.processing_success_rate) if doc_analytics else 0.0
                },
                "users": {
                    "active_today": user_analytics.active_users if user_analytics else 0,
                    "total_searches": user_analytics.total_searches if user_analytics else 0,
                    "avg_response_time_ms": user_analytics.avg_response_time_ms if user_analytics else 0
                }
            },
            "last_updated": datetime.utcnow().isoformat()
        }

    async def invalidate_cache(self, organization_id: str):
        """Invalidate cache for organization"""
        patterns = [
            f"analytics:realtime:metrics:{organization_id}",
            f"analytics:realtime:kpi:{organization_id}:*",
            f"analytics:realtime:trend:{organization_id}:*"
        ]

        for pattern in patterns:
            keys = await self.redis.keys(pattern)
            if keys:
                await self.redis.delete(*keys)
                self.logger.info(f"Invalidated {len(keys)} cache keys for {organization_id}")
```

#### 3.2 API Implementation
Create `backend/src/api/analytics_realtime.py`:
```python
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from typing import Optional

from src.core.auth import get_current_user
from src.services.analytics.realtime_service import RealtimeAnalyticsService
from src.middleware.rate_limiting import rate_limit_check

router = APIRouter()
realtime_service = RealtimeAnalyticsService()

@router.get("/realtime/metrics")
@rate_limit_check(limit=60, window=60)  # 60 requests per minute
async def get_realtime_metrics(
    organization_id: str = Query(...),
    refresh_interval: int = Query(5, ge=1, le=300),
    current_user: dict = Depends(get_current_user)
):
    """Get real-time dashboard metrics"""

    try:
        # Verify user has access to organization
        if not await _verify_org_access(current_user["user_id"], organization_id):
            raise HTTPException(status_code=403, detail="Organization access denied")

        metrics = await realtime_service.get_metrics(organization_id)

        return JSONResponse(
            content=metrics,
            headers={
                "Cache-Control": f"public, max-age={refresh_interval}",
                "X-Refresh-Interval": str(refresh_interval)
            }
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get metrics: {e}")

async def _verify_org_access(user_id: str, organization_id: str) -> bool:
    """Verify user has access to organization"""
    # Implementation would check user's organization access
    return True
```

### Step 4: Graph Analytics Service

#### 4.1 Neo4j Integration
Create `backend/src/services/analytics/graph_analytics_service.py`:
```python
import json
from typing import Dict, List, Optional, Any
import asyncio
from neo4j import GraphDatabase
from datetime import datetime

from .base_service import BaseAnalyticsService

class GraphAnalyticsService(BaseAnalyticsService):
    """Graph analytics computation service"""

    def __init__(self):
        super().__init__()
        self.neo4j_driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
        )
        self.cache_ttl = 3600  # 1 hour

    async def initialize(self):
        """Initialize graph analytics service"""
        self.logger.info("Graph analytics service initialized")

    async def compute_centrality(
        self,
        organization_id: str,
        algorithm: str,
        parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Compute centrality metrics"""

        # Check cache
        params_hash = self._hash_params(parameters)
        cache_key = f"analytics:graph:centrality:{organization_id}:{algorithm}:{params_hash}"

        cached_result = await self.redis.get(cache_key)
        if cached_result:
            return json.loads(cached_result)

        # Compute centrality
        with self.neo4j_driver.session() as session:
            if algorithm == "degree":
                result = await self._compute_degree_centrality(
                    session, organization_id, parameters
                )
            elif algorithm == "betweenness":
                result = await self._compute_betweenness_centrality(
                    session, organization_id, parameters
                )
            elif algorithm == "pagerank":
                result = await self._compute_pagerank(
                    session, organization_id, parameters
                )
            else:
                raise ValueError(f"Unsupported centrality algorithm: {algorithm}")

        # Cache result
        await self.redis.setex(cache_key, self.cache_ttl, json.dumps(result))

        return result

    async def _compute_degree_centrality(
        self,
        session,
        organization_id: str,
        parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Compute degree centrality"""

        entity_types = parameters.get("entity_types", [])
        limit = parameters.get("limit", 100)

        # Build query
        if entity_types:
            type_filter = f"AND n.entity_type IN {entity_types}"
        else:
            type_filter = ""

        query = f"""
        MATCH (n:Entity)
        WHERE n.organization_id = $org_id
        {type_filter}
        WITH n, size((n)--()) as degree
        RETURN n.entity_id as entity_id,
               n.name as entity_name,
               n.entity_type as entity_type,
               degree as centrality_score
        ORDER BY degree DESC
        LIMIT $limit
        """

        result = session.run(query, org_id=organization_id, limit=limit)

        centrality_scores = []
        for record in result:
            centrality_scores.append({
                "entity_id": record["entity_id"],
                "entity_name": record["entity_name"],
                "entity_type": record["entity_type"],
                "centrality_score": record["centrality_score"],
                "rank": len(centrality_scores) + 1
            })

        return {
            "algorithm": "degree",
            "computed_at": datetime.utcnow().isoformat(),
            "total_entities": len(centrality_scores),
            "results": centrality_scores,
            "parameters": parameters
        }

    def _hash_params(self, params: Dict[str, Any]) -> str:
        """Create hash of parameters for caching"""
        import hashlib
        params_str = json.dumps(params, sort_keys=True)
        return hashlib.md5(params_str.encode()).hexdigest()
```

### Step 5: Message Queue Setup

#### 5.1 Background Task Configuration
Create `backend/src/tasks/analytics_tasks.py`:
```python
from celery import Celery
from celery.schedules import crontab
from src.core.config import settings

# Configure Celery
celery_app = Celery(
    "analytics",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["src.tasks.analytics_tasks"]
)

# Configure Celery settings
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,  # 30 minutes
    task_soft_time_limit=25 * 60,  # 25 minutes
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
)

# Define periodic tasks
celery_app.conf.beat_schedule = {
    "update-realtime-metrics": {
        "task": "src.tasks.analytics_tasks.update_realtime_metrics",
        "schedule": 30.0,  # Every 30 seconds
    },
    "aggregate-hourly-analytics": {
        "task": "src.tasks.analytics_tasks.aggregate_hourly_analytics",
        "schedule": crontab(minute=5),  # At 5 minutes past every hour
    },
    "cleanup-expired-cache": {
        "task": "src.tasks.analytics_tasks.cleanup_expired_cache",
        "schedule": crontab(hour=2, minute=0),  # Daily at 2 AM
    },
}

@celery_app.task(bind=True)
def update_realtime_metrics(self):
    """Update real-time metrics for all organizations"""
    from src.services.analytics.realtime_service import RealtimeAnalyticsService
    from src.models.organizations import Organization

    service = RealtimeAnalyticsService()

    # Get all active organizations
    organizations = Organization.query.filter(Organization.is_active == True).all()

    for org in organizations:
        try:
            # Invalidate cache and update metrics
            await service.invalidate_cache(str(org.id))
            await service.get_metrics(str(org.id))

        except Exception as e:
            self.logger.error(f"Failed to update metrics for org {org.id}: {e}")

@celery_app.task(bind=True)
def compute_graph_analytics(self, organization_id: str, algorithm: str, parameters: dict):
    """Compute graph analytics for organization"""
    from src.services.analytics.graph_analytics_service import GraphAnalyticsService

    service = GraphAnalyticsService()

    try:
        result = await service.compute_centrality(
            organization_id, algorithm, parameters
        )

        return {
            "status": "success",
            "result": result
        }

    except Exception as e:
        self.logger.error(f"Graph analytics failed for {organization_id}: {e}")
        return {
            "status": "error",
            "error": str(e)
        }

@celery_app.task(bind=True)
def generate_report(self, report_id: str, execution_id: str):
    """Generate analytics report"""
    from src.services.analytics.report_service import ReportService

    service = ReportService()

    try:
        result = await service.execute_report(report_id, execution_id)
        return result

    except Exception as e:
        self.logger.error(f"Report generation failed for {report_id}: {e}")
        raise
```

### Step 6: API Integration

#### 6.1 Update Main Application
Update `backend/src/main.py` to include analytics routes:
```python
# Add analytics routers
from src.api.analytics_realtime import router as analytics_realtime_router
from src.api.analytics_graph import router as analytics_graph_router
from src.api.analytics_dashboard import router as analytics_dashboard_router
from src.api.analytics_reports import router as analytics_reports_router

# Include analytics routers
app.include_router(analytics_realtime_router, prefix="/api/v1/analytics")
app.include_router(analytics_graph_router, prefix="/api/v1/analytics")
app.include_router(analytics_dashboard_router, prefix="/api/v1/analytics")
app.include_router(analytics_reports_router, prefix="/api/v1/analytics")
```

#### 6.2 CORS and Middleware Configuration
Update middleware configuration for analytics:
```python
# Add analytics-specific CORS origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://analytics.company.com",
        "https://dashboard.company.com",
        "http://localhost:3000"  # Development
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "X-API-Key",
        "Content-Type",
        "X-Organization-ID",
        "X-WebSocket-Key"
    ],
)

# Add analytics rate limiting middleware
app.add_middleware(AnalyticsRateLimitMiddleware)
```

### Step 7: Configuration Updates

#### 7.1 Environment Variables
Add to `.env` file:
```env
# Analytics Configuration
ANALYTICS_ENABLED=true
ANALYTICS_CACHE_TTL=30
ANALYTICS_GRAPH_TIMEOUT=300
ANALYTICS_REPORT_TIMEOUT=600

# Celery Configuration
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2

# WebSocket Configuration
WEBSOCKET_ENABLED=true
WEBSOCKET_HEARTBEAT_INTERVAL=30
WEBSOCKET_MAX_CONNECTIONS=1000

# Performance Configuration
MAX_CONCURRENT_GRAPH_TASKS=5
MAX_CONCURRENT_REPORTS=3
ANALYTICS_BATCH_SIZE=1000
```

#### 7.2 Update Settings
Update `backend/src/core/config.py`:
```python
class Settings(BaseSettings):
    # ... existing settings ...

    # Analytics settings
    ANALYTICS_ENABLED: bool = True
    ANALYTICS_CACHE_TTL: int = 30
    ANALYTICS_GRAPH_TIMEOUT: int = 300
    ANALYTICS_REPORT_TIMEOUT: int = 600

    # Celery settings
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # WebSocket settings
    WEBSOCKET_ENABLED: bool = True
    WEBSOCKET_HEARTBEAT_INTERVAL: int = 30
    WEBSOCKET_MAX_CONNECTIONS: int = 1000
```

### Step 8: Testing Implementation

#### 8.1 Unit Tests
Create `backend/src/tests/test_analytics_realtime.py`:
```python
import pytest
from unittest.mock import AsyncMock, patch
from src.services.analytics.realtime_service import RealtimeAnalyticsService

@pytest.fixture
def realtime_service():
    return RealtimeAnalyticsService()

@pytest.mark.asyncio
async def test_get_metrics_cache_hit(realtime_service):
    """Test getting metrics from cache"""

    organization_id = "test-org"
    cached_data = {"metrics": {"entities": {"total": 100}}}

    with patch.object(realtime_service.redis, 'get', return_value=json.dumps(cached_data)):
        result = await realtime_service.get_metrics(organization_id)

        assert result == cached_data

@pytest.mark.asyncio
async def test_get_metrics_cache_miss(realtime_service):
    """Test computing fresh metrics when cache miss"""

    organization_id = "test-org"
    expected_metrics = {"metrics": {"entities": {"total": 100}}}

    with patch.object(realtime_service.redis, 'get', return_value=None):
        with patch.object(realtime_service, '_compute_metrics', return_value=expected_metrics):
            with patch.object(realtime_service.redis, 'setex'):
                result = await realtime_service.get_metrics(organization_id)

                assert result == expected_metrics
```

#### 8.2 Integration Tests
Create `backend/src/tests/test_analytics_integration.py`:
```python
import pytest
from httpx import AsyncClient
from src.main import app

@pytest.mark.asyncio
async def test_realtime_metrics_endpoint():
    """Test real-time metrics API endpoint"""

    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/analytics/realtime/metrics",
            params={"organization_id": "test-org"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "organization_id" in data
        assert "metrics" in data
        assert "timestamp" in data

@pytest.mark.asyncio
async def test_graph_analytics_endpoint():
    """Test graph analytics API endpoint"""

    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/analytics/graph/centrality",
            params={
                "organization_id": "test-org",
                "algorithm": "degree"
            },
            json={"limit": 10}
        )

        assert response.status_code == 200
        data = response.json()
        assert "algorithm" in data
        assert "results" in data
```

### Step 9: Monitoring Setup

#### 9.1 Health Checks
Create `backend/src/health/analytics_health.py`:
```python
from typing import Dict, Any
from src.services.analytics.realtime_service import RealtimeAnalyticsService
from src.services.analytics.graph_analytics_service import GraphAnalyticsService

async def get_analytics_health() -> Dict[str, Any]:
    """Get overall analytics system health"""

    services = {
        "realtime": RealtimeAnalyticsService(),
        "graph": GraphAnalyticsService()
    }

    health_results = {}
    overall_status = "healthy"

    for name, service in services.items():
        try:
            health = await service.health_check()
            health_results[name] = health

            if health["status"] != "healthy":
                overall_status = "degraded"

        except Exception as e:
            health_results[name] = {
                "service": name,
                "status": "unhealthy",
                "error": str(e)
            }
            overall_status = "unhealthy"

    return {
        "status": overall_status,
        "services": health_results,
        "timestamp": datetime.utcnow().isoformat()
    }
```

#### 9.2 Metrics Collection
Create `backend/src/middleware/analytics_metrics.py`:
```python
import time
from fastapi import Request, Response
from prometheus_client import Counter, Histogram, generate_latest

# Define metrics
analytics_requests_total = Counter(
    'analytics_requests_total',
    'Total analytics requests',
    ['method', 'endpoint', 'status']
)

analytics_request_duration = Histogram(
    'analytics_request_duration_seconds',
    'Analytics request duration',
    ['method', 'endpoint']
)

async def analytics_metrics_middleware(request: Request, call_next):
    """Middleware to collect analytics metrics"""

    start_time = time.time()

    # Process request
    response = await call_next(request)

    # Record metrics
    duration = time.time() - start_time

    analytics_requests_total.labels(
        method=request.method,
        endpoint=request.url.path,
        status=response.status_code
    ).inc()

    analytics_request_duration.labels(
        method=request.method,
        endpoint=request.url.path
    ).observe(duration)

    return response
```

### Step 10: Deployment

#### 10.1 Docker Configuration
Create `backend/Dockerfile.analytics`:
```dockerfile
FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Set environment variables
ENV PYTHONPATH=/app
ENV ANALYTICS_ENABLED=true

# Expose ports
EXPOSE 8000

# Start application
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

#### 10.2 Docker Compose
Add to `docker-compose.yml`:
```yaml
services:
  analytics-api:
    build:
      context: .
      dockerfile: Dockerfile.analytics
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql://postgres:postgres@postgres:5432/multimodal_rag
      - REDIS_URL=redis://redis:6379
      - NEO4J_URI=bolt://neo4j:7687
      - NEO4J_USER=neo4j
      - NEO4J_PASSWORD=neo4jpassword
    depends_on:
      - postgres
      - redis
      - neo4j
    volumes:
      - ./src:/app/src
      - ./requirements.txt:/app/requirements.txt

  analytics-worker:
    build:
      context: .
      dockerfile: Dockerfile.analytics
    command: celery -A src.tasks.analytics_tasks worker --loglevel=info
    environment:
      - DATABASE_URL=postgresql://postgres:postgres@postgres:5432/multimodal_rag
      - REDIS_URL=redis://redis:6379
      - CELERY_BROKER_URL=redis://redis:6379/1
      - CELERY_RESULT_BACKEND=redis://redis:6379/2
    depends_on:
      - redis
      - postgres
    volumes:
      - ./src:/app/src

  analytics-beat:
    build:
      context: .
      dockerfile: Dockerfile.analytics
    command: celery -A src.tasks.analytics_tasks beat --loglevel=info
    environment:
      - DATABASE_URL=postgresql://postgres:postgres@postgres:5432/multimodal_rag
      - REDIS_URL=redis://redis:6379
      - CELERY_BROKER_URL=redis://redis:6379/1
      - CELERY_RESULT_BACKEND=redis://redis:6379/2
    depends_on:
      - redis
      - postgres
    volumes:
      - ./src:/app/src
```

## Verification Checklist

### Pre-deployment Checks
- [ ] Database migration applied successfully
- [ ] All services start without errors
- [ ] Redis connection working
- [ ] Neo4j connection working
- [ ] Background tasks processing
- [ ] Health endpoints responding
- [ ] Basic API endpoints working

### Post-deployment Verification
- [ ] Real-time metrics updating
- [ ] Graph analytics computing
- [ ] Dashboard configurations saving
- [ ] Reports generating
- [ ] WebSocket connections working
- [ ] Cache invalidation working
- [ ] Error monitoring active

### Performance Validation
- [ ] API response times < 200ms
- [ ] Cache hit rates > 80%
- [ ] Background task processing normal
- [ ] Memory usage within limits
- [ ] CPU usage within limits

## Troubleshooting

### Common Issues

#### 1. Database Connection Errors
```bash
# Check database connection
psql -h localhost -U postgres -d multimodal_rag -c "SELECT 1;"

# Check migration status
psql -h localhost -U postgres -d multimodal_rag -c "\dt analytics_*"
```

#### 2. Redis Connection Issues
```bash
# Check Redis connection
redis-cli ping

# Check Redis keys
redis-cli keys "analytics:*"
```

#### 3. Neo4j Connection Issues
```bash
# Check Neo4j status
cypher-shell -u neo4j -p neo4jpassword "RETURN 1;"

# Check graph data
cypher-shell -u neo4j -p neo4jpassword "MATCH (n:Entity) RETURN count(n);"
```

#### 4. Background Task Issues
```bash
# Check Celery worker status
celery -A src.tasks.analytics_tasks inspect active

# Check Celery queues
celery -A src.tasks.analytics_tasks inspect reserved
```

### Log Analysis

#### Application Logs
```bash
# View analytics service logs
docker-compose logs -f analytics-api

# View worker logs
docker-compose logs -f analytics-worker

# View beat logs
docker-compose logs -f analytics-beat
```

#### Database Logs
```bash
# View PostgreSQL logs
docker-compose logs -f postgres

# View Redis logs
docker-compose logs -f redis

# View Neo4j logs
docker-compose logs -f neo4j
```

This implementation guide provides a comprehensive roadmap for implementing the Knowledge Graph Analytics Dashboard backend services. The guide covers all major components from database setup to deployment, with specific code examples and troubleshooting guidance.
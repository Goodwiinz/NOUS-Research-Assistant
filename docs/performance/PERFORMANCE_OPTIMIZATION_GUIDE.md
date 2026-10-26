# Comprehensive Performance Optimization Guide

> **Stale (2026-10-17): Qdrant is fully removed and live retrieval is PostgreSQL full-text search, so statements about Qdrant or vector stores below are historical. Measurements and file paths below describe the system when the document was written and are not current performance evidence.**

## Multimodal Enterprise RAG System

This guide provides detailed documentation for the performance optimization implementation across the entire RAG system, including monitoring, optimization strategies, and best practices.

## Table of Contents

1. [Performance Targets Overview](#performance-targets-overview)
2. [Architecture Overview](#architecture-overview)
3. [Database Optimizations](#database-optimizations)
4. [Backend API Optimizations](#backend-api-optimizations)
5. [Frontend Performance Optimizations](#frontend-performance-optimizations)
6. [Caching Infrastructure](#caching-infrastructure)
7. [Load Testing and Validation](#load-testing-and-validation)
8. [Monitoring and Alerting](#monitoring-and-alerting)
9. [Deployment Guide](#deployment-guide)
10. [Troubleshooting Runbook](#troubleshooting-runbook)

## Performance Targets Overview

### Primary Performance Requirements

| Metric | Target | Current Status | Measurement Method |
|--------|--------|----------------|-------------------|
| **Query Response Time (P95)** | < 2 seconds | ✅ Achieved | Prometheus metrics |
| **Query Response Time (P99)** | < 5 seconds | ✅ Achieved | Load testing |
| **File Processing Time** | < 5 minutes for <10MB files | ✅ Achieved | End-to-end testing |
| **Concurrent Users** | 50 users with <10% degradation | ✅ Achieved | Load testing |
| **System Uptime** | 99% availability | ✅ Achieved | Health checks |
| **Page Load Time** | < 3 seconds | ✅ Achieved | Frontend monitoring |
| **Graph Rendering** | < 1 second for 500 nodes | ✅ Achieved | Performance tests |
| **WebSocket Latency** | < 100ms | ✅ Achieved | Real-time monitoring |

### Secondary Performance Metrics

| Metric | Target | Acceptable Range |
|--------|--------|------------------|
| API Response Time (P50) | < 500ms | 300-600ms |
| Error Rate | < 1% | < 2% |
| Memory Usage | < 4GB | 2-6GB |
| CPU Usage | < 70% average | < 85% peak |
| Cache Hit Rate | > 80% | > 70% |
| Database Query Time | < 100ms average | < 200ms |

## Architecture Overview

### Performance-Optimized System Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Frontend      │    │  API Gateway    │    │   Backend       │
│   (Optimized)   │◄──►│   (Nginx)       │◄──►│   (FastAPI)     │
│                 │    │                 │    │                 │
│ • Bundle Split  │    │ • Load Balance  │    │ • Response      │
│ • Lazy Loading  │    │ • SSL Terminate│    │   Caching       │
│ • Image Opt     │    │ • Rate Limiting │    │ • Async Process │
└─────────────────┘    └─────────────────┘    └─────────────────┘
                                                         │
                    ┌─────────────────┐                 │
                    │   Monitoring    │◄────────────────┤
                    │   (Prometheus)  │                 │
                    │                 │                 │
                    │ • Metrics       │                 │
                    │ • Alerting      │                 │
                    │ • Dashboards    │                 │
                    └─────────────────┘                 │
                                                         │
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  Qdrant         │    │  PostgreSQL     │    │   Neo4j         │
│  (Vector DB)    │    │  (Relational)   │    │   (Graph DB)    │
│                 │    │                 │    │                 │
│ • HNSW Index    │    │ • Query Opt     │    │ • Graph Travers │
│ • Quantization  │    │ • Connection    │    │   al Opt        │
│ • Disk Storage  │    │   Pooling       │    │ • Full-text     │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         └───────────────────────┼───────────────────────┘
                                 │
                    ┌─────────────────┐
                    │     Redis       │
                    │   (Cache)       │
                    │                 │
                    │ • Multi-level   │
                    │ • Compression   │
                    │ • Persistence   │
                    └─────────────────┘
```

## Database Optimizations

### PostgreSQL Performance Optimization

#### Configuration Settings
```sql
-- Memory configuration (adjust based on available RAM)
shared_buffers = 256MB              -- 25% of RAM
effective_cache_size = 1GB          -- 50-75% of total RAM
work_mem = 4MB                      -- Per query operation
maintenance_work_mem = 64MB         -- For maintenance operations

-- Performance tuning
checkpoint_completion_target = 0.9  -- Smoother checkpoints
wal_buffers = 16MB                  -- WAL buffer size
default_statistics_target = 100     -- Better query planning
random_page_cost = 1.1              -- SSD optimization
effective_io_concurrency = 200      -- Concurrent I/O

-- Connection settings
max_connections = 200               -- Concurrent connections
shared_preload_libraries = 'pg_stat_statements'  -- Query monitoring
```

#### Index Optimization Strategy
```sql
-- Full-text search indexes
CREATE INDEX CONCURRENTLY idx_documents_title_gin
ON documents USING gin(to_tsvector('english', title));

CREATE INDEX CONCURRENTLY idx_documents_content_gin
ON documents USING gin(to_tsvector('english', content));

-- Composite indexes for common query patterns
CREATE INDEX CONCURRENTLY idx_documents_tenant_type_status
ON documents(tenant_id, document_type, processing_status);

-- Partial indexes for better performance
CREATE INDEX CONCURRENTLY idx_active_documents
ON documents(created_at DESC) WHERE processing_status = 'completed';
```

#### Query Optimization Examples
```sql
-- Optimized document search with pagination
EXPLAIN (ANALYZE, BUFFERS)
SELECT d.id, d.title, d.document_type
FROM documents d
WHERE d.tenant_id = $1
    AND d.processing_status = 'completed'
    AND to_tsvector('english', d.content) @@ to_tsquery('english', $2)
ORDER BY d.created_at DESC
LIMIT $3 OFFSET $4;

-- Materialized view for analytics
CREATE MATERIALIZED VIEW mv_document_stats AS
SELECT
    tenant_id,
    document_type,
    COUNT(*) as total_documents,
    SUM(file_size) as total_file_size,
    AVG(EXTRACT(EPOCH FROM (now() - created_at))) as avg_age_days
FROM documents
GROUP BY tenant_id, document_type;

-- Refresh strategy
CREATE OR REPLACE FUNCTION refresh_document_stats()
RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_document_stats;
END;
$$ LANGUAGE plpgsql;
```

### Neo4j Graph Database Optimization

#### Index Configuration
```cypher
-- Create indexes for frequently queried properties
CREATE INDEX document_id_index IF NOT EXISTS FOR (d:Document) ON (d.id);
CREATE INDEX entity_name_index IF NOT EXISTS FOR (e:Entity) ON (e.name);
CREATE INDEX entity_type_index IF NOT EXISTS FOR (e:Entity) ON (e.type);

-- Full-text search indexes
CALL db.index.fulltext.createNodeIndex(
    "documentFulltext",
    ["Document", "TextContent"],
    ["title", "content", "summary"]
);

CALL db.index.fulltext.createNodeIndex(
    "entityFulltext",
    ["Entity"],
    ["name", "description", "aliases"]
);
```

#### Query Optimization Patterns
```cypher
-- Efficient graph traversal with depth control
MATCH path = (d:Document {tenant_id: $tenant_id})-[:CONTAINS|MENTIONS*1..3]-(related)
WHERE d.id = $document_id
RETURN path
LIMIT 100;

-- Optimized full-text search
CALL db.index.fulltext.queryNodes("documentFulltext", $search_query) YIELD node, score
WHERE node.tenant_id = $tenant_id
RETURN node, score
ORDER BY score DESC
LIMIT $limit;

-- Efficient aggregation queries
MATCH (d:Document {tenant_id: $tenant_id})
OPTIONAL MATCH (d)-[:MENTIONS]->(e:Entity)
WITH d, count(e) as entity_count
RETURN d.document_type, avg(entity_count) as avg_entities_per_doc
ORDER BY avg_entities_per_doc DESC;
```

#### Memory Configuration
```properties
# Neo4j configuration for optimal performance
dbms.memory.heap.initial_size=2G
dbms.memory.heap.max__size=4G
dbms.memory.pagecache.size=2G

# Transaction settings
dbms.transaction.timeout=60s
dbms.transaction.concurrent.maximum=1000

# Query optimization
dbms.cypher.forbid_exhaustive_shortestpath=true
dbms.logs.query.threshold=1s
```

### Qdrant Vector Database Optimization

#### Collection Configuration
```python
# Optimized collection creation
from qdrant_client.models import (
    Distance, VectorParams, ScalarQuantization,
    ScalarQuantizationConfig, OptimizersConfigDiff,
    HnswConfigDiff
)

hnsw_config = HnswConfigDiff(
    m=16,                    # Number of edges per node
    ef_construct=100,       # Construction accuracy
    ef_search=64,           # Search accuracy
    full_scan_threshold=10000,
    max_indexing_threads=4
)

quantization_config = ScalarQuantization(
    scalar=ScalarQuantizationConfig(
        type=ScalarType.INT8,
        ram=True
    )
)

optimizer_config = OptimizersConfigDiff(
    deleted_threshold=0.2,
    vacuum_min_vector_number=1000,
    default_segment_number=2,
    max_segment_size=200000,
    max_optimization_threads=4
)
```

#### Search Optimization
```python
# Optimized search with performance monitoring
async def search_optimized(
    collection_name: str,
    query_vector: List[float],
    limit: int = 10,
    score_threshold: float = 0.7
) -> List[Dict]:
    start_time = time.time()

    search_result = client.search(
        collection_name=collection_name,
        query_vector=query_vector,
        limit=limit,
        score_threshold=score_threshold,
        search_params={
            'hnsw_ef': 64,  # Balance between speed and accuracy
            'exact': False   # Use approximate search
        },
        with_payload=True,
        with_vectors=False  # Don't return vectors unless needed
    )

    search_time = time.time() - start_time
    logger.info(f"Search completed in {search_time:.3f}s")

    return [hit.payload for hit in search_result]
```

### Redis Caching Optimization

#### Configuration Settings
```conf
# Redis performance configuration
maxmemory 1gb
maxmemory-policy allkeys-lru

# Memory optimization
hash-max-ziplist-entries 512
hash-max-ziplist-value 64
list-max-ziplist-size -2
set-max-intset-entries 512

# Persistence optimization
appendonly yes
appendfsync everysec
auto-aof-rewrite-percentage 100
auto-aof-rewrite-min-size 64mb

# Performance settings
tcp-keepalive 300
timeout 300
slowlog-log-slower-than 10000
latency-monitor-threshold 100
```

## Backend API Optimizations

### Response Caching Strategy

#### Multi-Level Caching Implementation
```python
# Smart caching with Redis backend
class SmartCache:
    def __init__(self, redis_client: redis.Redis, config: CacheConfig):
        self.redis_client = redis_client
        self.config = config
        self.local_cache: Dict[str, Dict] = {}

    async def get(self, key_parts: List[Any]) -> Optional[Any]:
        # Check local cache first
        key = self._generate_key(key_parts)
        if key in self.local_cache:
            entry = self.local_cache[key]
            if time.time() < entry['expires']:
                return entry['value']

        # Check Redis cache
        data = await self.redis_client.get(key)
        if data:
            value = self._deserialize_value(data)
            # Store in local cache
            self.local_cache[key] = {
                'value': value,
                'expires': time.time() + self.config.ttl
            }
            return value

        return None

# Cache decorator for API endpoints
@smart_cache(cache=smart_cache_instance, key_parts=['search'], ttl=300)
async def search_documents(query: str, filters: Dict) -> List[Document]:
    # Search implementation
    pass
```

#### API Response Optimization
```python
# Response compression and optimization
class APIPerformanceMiddleware:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        start_time = time.time()

        # Rate limiting check
        if not await self.rate_limiter.is_allowed(request):
            response = JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded"}
            )
            await self._send_response(response, send, scope, start_time)
            return

        # Cache check for GET requests
        if request.method == 'GET':
            cached_response = await self.response_cache.get_cached_response(request)
            if cached_response:
                await self._send_response(cached_response, send, scope, start_time)
                return

        # Process request
        response = await self._call_asgi(request, receive, send)

        # Cache successful responses
        if request.method == 'GET' and response.status_code == 200:
            await self.response_cache.cache_response(request, response)

        await self._send_response(response, send, scope, start_time)
```

### Async Processing Optimization

#### Concurrent Processing with Throttling
```python
class AsyncOptimizer:
    def __init__(self, max_workers: int = 4):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.semaphore = asyncio.Semaphore(max_workers * 2)

    async def batch_process(
        self,
        items: List[Any],
        processor: Callable,
        batch_size: int = 10,
        max_concurrent: int = 5
    ) -> List[Any]:
        """Process items in batches with concurrency control"""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_batch(batch):
            async with semaphore:
                if asyncio.iscoroutinefunction(processor):
                    return await processor(batch)
                else:
                    return await self.run_in_background(processor, batch)

        # Create and process batches
        batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
        tasks = [process_batch(batch) for batch in batches]
        return await asyncio.gather(*tasks, return_exceptions=True)
```

#### Database Connection Pooling
```python
# Optimized database configuration
DATABASE_CONFIG = {
    "pool_size": 20,           # Number of connections in pool
    "max_overflow": 30,        # Additional connections under load
    "pool_timeout": 30,        # Timeout for getting connection
    "pool_recycle": 3600,      # Recycle connections every hour
    "pool_pre_ping": True,     # Validate connections
}

# Connection monitoring
async def monitor_database_pool():
    while True:
        pool = engine.pool
        logger.info(f"Pool status: {pool.size()}/{pool.checkedout()} connections")
        await asyncio.sleep(60)
```

## Frontend Performance Optimizations

### Bundle Optimization Strategy

#### Code Splitting Implementation
```typescript
// Lazy loading with React Suspense
import { lazy, Suspense } from 'react';
import { CodeSplitOptimizer } from '../performance/optimization';

const LazyDocumentViewer = lazy(() =>
    CodeSplitOptimizer.loadComponent(
        () => import('../components/DocumentViewer'),
        'DocumentViewer'
    )
);

const LazyAnalytics = lazy(() =>
    CodeSplitOptimizer.loadComponent(
        () => import('../components/Analytics'),
        'Analytics'
    )
);

function App() {
    return (
        <Suspense fallback={<LoadingSpinner />}>
            <Routes>
                <Route path="/documents/:id" element={<LazyDocumentViewer />} />
                <Route path="/analytics" element={<LazyAnalytics />} />
            </Routes>
        </Suspense>
    );
}
```

#### Critical Resource Preloading
```typescript
// Preload critical resources
export function preloadCriticalResources(): void {
    // Preload critical CSS
    BundleOptimizer.loadCriticalCSS('/css/critical.css');

    // Preload critical fonts
    BundleOptimizer.loadFont('/fonts/inter-var.woff2', 'Inter var');

    // Preload critical images
    const criticalImages = [
        '/images/hero-bg.webp',
        '/images/logo.webp'
    ];
    ImageOptimizer.preloadImages(criticalImages);

    // Preload critical JavaScript
    BundleOptimizer.preloadResources([
        { url: '/js/vendor~main.js', type: 'script', priority: 'high' },
        { url: '/js/main.js', type: 'script', priority: 'high' }
    ]);
}
```

### Image Optimization

#### WebP Conversion and Lazy Loading
```typescript
// Optimized image component
import { useLazyLoad } from '../performance/optimization';

interface OptimizedImageProps {
    src: string;
    alt: string;
    width?: number;
    height?: number;
    priority?: boolean;
}

function OptimizedImage({
    src,
    alt,
    width,
    height,
    priority = false
}: OptimizedImageProps) {
    const { isVisible, elementRef } = useLazyLoad(0.1, '50px');
    const [optimizedSrc, setOptimizedSrc] = useState<string>('');

    useEffect(() => {
        if (priority || isVisible) {
            ImageOptimizer.loadImage(src, {
                width,
                height,
                quality: 0.8,
                format: 'webp'
            }).then(setOptimizedSrc);
        }
    }, [src, width, height, priority, isVisible]);

    return (
        <img
            ref={elementRef}
            src={optimizedSrc}
            alt={alt}
            loading={priority ? 'eager' : 'lazy'}
            style={{ opacity: optimizedSrc ? 1 : 0 }}
        />
    );
}
```

### Performance Monitoring

#### React Performance Hooks
```typescript
// Performance monitoring for components
import { usePerformanceMonitor } from '../performance/optimization';

function DocumentSearch() {
    const { startMeasure, endMeasure } = usePerformanceMonitor('DocumentSearch');

    const handleSearch = async (query: string) => {
        const measureId = startMeasure('search');

        try {
            const results = await searchDocuments(query);
            return results;
        } finally {
            endMeasure('search');
        }
    };

    return (
        <div>
            {/* Search UI */}
        </div>
    );
}
```

## Caching Infrastructure

### Multi-Level Caching Architecture

#### Cache Hierarchy
```
┌─────────────────┐
│   Browser       │ L1: Browser Cache
│   Cache         │ (Static assets, API responses)
└─────────────────┘
         │
┌─────────────────┐
│   CDN           │ L2: CDN Cache
│   (CloudFlare)  │ (Static content, edge caching)
└─────────────────┘
         │
┌─────────────────┐
│   Application   │ L3: Application Cache
│   Cache         │ (Response cache, computed results)
└─────────────────┘
         │
┌─────────────────┐
│   Redis         │ L4: Distributed Cache
│   (Redis)       │ (Session cache, shared data)
└─────────────────┘
         │
┌─────────────────┐
│   Database      │ L5: Database Cache
│   Cache         │ (Query results, materialized views)
└─────────────────┘
```

#### Cache Implementation Strategies
```python
# Cache configuration by data type
CACHE_STRATEGIES = {
    'user_sessions': {
        'ttl': 3600,           # 1 hour
        'level': 'redis',
        'strategy': 'lru'
    },
    'search_results': {
        'ttl': 300,            # 5 minutes
        'level': 'redis',
        'strategy': 'lfu'
    },
    'document_metadata': {
        'ttl': 1800,           # 30 minutes
        'level': 'redis',
        'strategy': 'lru'
    },
    'api_responses': {
        'ttl': 60,             # 1 minute
        'level': 'application',
        'strategy': 'lru'
    },
    'static_assets': {
        'ttl': 86400,          # 24 hours
        'level': 'cdn',
        'strategy': 'lru'
    }
}

# Cache invalidation strategies
class CacheInvalidation:
    def __init__(self, cache_client):
        self.cache = cache_client

    async def invalidate_user_cache(self, user_id: str):
        """Invalidate all cache entries for a user"""
        patterns = [
            f"user_sessions:{user_id}:*",
            f"search_results:{user_id}:*",
            f"api_responses:{user_id}:*"
        ]

        for pattern in patterns:
            await self.cache.clear_pattern(pattern)

    async def invalidate_document_cache(self, document_id: str):
        """Invalidate cache entries related to a document"""
        patterns = [
            f"document_metadata:{document_id}:*",
            f"search_results:*:document:{document_id}",
            f"api_responses:*:document:{document_id}"
        ]

        for pattern in patterns:
            await self.cache.clear_pattern(pattern)
```

## Load Testing and Validation

### Comprehensive Load Testing Strategy

#### Test Scenarios
```python
# Load test scenarios
class LoadTestScenarios:
    # Test configurations
    TEST_CONFIGS = {
        'light_load': {
            'concurrent_users': 10,
            'duration': 300,
            'ramp_up': 30
        },
        'moderate_load': {
            'concurrent_users': 25,
            'duration': 600,
            'ramp_up': 60
        },
        'heavy_load': {
            'concurrent_users': 50,
            'duration': 900,
            'ramp_up': 120
        },
        'stress_test': {
            'concurrent_users': 100,
            'duration': 300,
            'ramp_up': 60
        }
    }

    # Test scenarios
    SCENARIOS = {
        'document_upload': {
            'endpoint': '/api/v1/files/upload',
            'method': 'POST',
            'payload_size': '1MB',
            'expected_response_time': 5.0,
            'expected_success_rate': 0.95
        },
        'search_query': {
            'endpoint': '/api/v1/search',
            'method': 'GET',
            'payload_size': 'small',
            'expected_response_time': 2.0,
            'expected_success_rate': 0.99
        },
        'hybrid_search': {
            'endpoint': '/api/v1/search/hybrid',
            'method': 'POST',
            'payload_size': 'medium',
            'expected_response_time': 3.0,
            'expected_success_rate': 0.95
        },
        'knowledge_graph': {
            'endpoint': '/api/v1/knowledge-graph/entities/search',
            'method': 'GET',
            'payload_size': 'small',
            'expected_response_time': 1.5,
            'expected_success_rate': 0.98
        }
    }
```

#### Performance Validation
```python
# Automated performance validation
class PerformanceValidator:
    def __init__(self, targets: Dict[str, float]):
        self.targets = targets

    def validate_results(self, results: LoadTestResults) -> Dict[str, bool]:
        """Validate load test results against performance targets"""
        validation_results = {}

        # Response time validation
        validation_results['p95_response_time'] = (
            results.p95_response_time <= self.targets.get('p95_response_time', 2.0)
        )
        validation_results['p99_response_time'] = (
            results.p99_response_time <= self.targets.get('p99_response_time', 5.0)
        )

        # Error rate validation
        validation_results['error_rate'] = (
            results.error_rate <= self.targets.get('error_rate', 0.01)
        )

        # Throughput validation
        validation_results['throughput'] = (
            results.throughput >= self.targets.get('throughput', 100)
        )

        return validation_results

    def generate_report(self, results: List[LoadTestResults]) -> str:
        """Generate comprehensive performance report"""
        report = ["# Load Test Performance Report\n"]

        for result in results:
            validation = self.validate_results(result)

            report.append(f"## {result.test_name}\n")
            report.append(f"- **Total Requests**: {result.total_requests}")
            report.append(f"- **P95 Response Time**: {result.p95_response_time:.3f}s")
            report.append(f"- **P99 Response Time**: {result.p99_response_time:.3f}s")
            report.append(f"- **Error Rate**: {result.error_rate:.2%}")
            report.append(f"- **Throughput**: {result.throughput:.1f} req/s")

            report.append("### Validation Results\n")
            for metric, passed in validation.items():
                status = "✅ Pass" if passed else "❌ Fail"
                report.append(f"- **{metric}**: {status}")

            report.append("\n")

        return "".join(report)
```

## Monitoring and Alerting

### Comprehensive Monitoring Stack

#### Prometheus Metrics Configuration
```yaml
# Prometheus configuration
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - "performance_alerts.yml"

scrape_configs:
  - job_name: 'rag-backend'
    static_configs:
      - targets: ['backend:8000']
    metrics_path: '/metrics'
    scrape_interval: 10s

  - job_name: 'postgres'
    static_configs:
      - targets: ['postgres-exporter:9187']

  - job_name: 'redis'
    static_configs:
      - targets: ['redis-exporter:9121']

  - job_name: 'neo4j'
    static_configs:
      - targets: ['neo4j:2004']

  - job_name: 'qdrant'
    static_configs:
      - targets: ['qdrant:6333']

  - job_name: 'frontend'
    static_configs:
      - targets: ['frontend:3000']
    metrics_path: '/api/metrics'
```

#### Custom Metrics Implementation
```python
# Application metrics
from prometheus_client import Counter, Histogram, Gauge

# Request metrics
REQUEST_COUNT = Counter('http_requests_total', 'Total HTTP requests',
                       ['method', 'endpoint', 'status'])
REQUEST_DURATION = Histogram('http_request_duration_seconds', 'HTTP request duration',
                           ['method', 'endpoint'])
ACTIVE_REQUESTS = Gauge('concurrent_requests', 'Number of concurrent requests')

# Business metrics
SEARCH_QUERIES = Counter('search_queries_total', 'Total search queries',
                        ['query_type', 'success'])
DOCUMENT_UPLOADS = Counter('document_uploads_total', 'Total document uploads',
                         ['file_type', 'success'])
CACHE_OPERATIONS = Counter('cache_operations_total', 'Total cache operations',
                         ['operation', 'cache_level', 'result'])

# Performance metrics
MEMORY_USAGE = Gauge('memory_usage_bytes', 'Memory usage in bytes')
CPU_USAGE = Gauge('cpu_usage_percent', 'CPU usage percentage')
DATABASE_CONNECTIONS = Gauge('database_connections_active', 'Active database connections')
```

#### Alert Rules Configuration
```yaml
# Performance alerting rules
groups:
  - name: rag_performance_alerts
    rules:
      - alert: HighResponseTimeP95
        expr: histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])) > 2
        for: 2m
        labels:
          severity: warning
          service: rag-api
        annotations:
          summary: "High P95 response time detected"
          description: "P95 response time is {{ $value }}s, above 2s target"

      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]) * 100 > 5
        for: 1m
        labels:
          severity: critical
          service: rag-api
        annotations:
          summary: "High error rate detected"
          description: "Error rate is {{ $value }}%, above 5% threshold"

      - alert: HighMemoryUsage
        expr: memory_usage_bytes / (1024*1024*1024) > 4
        for: 5m
        labels:
          severity: warning
          service: rag-api
        annotations:
          summary: "High memory usage detected"
          description: "Memory usage is {{ $value }}GB, above 4GB threshold"
```

### Grafana Dashboard Configuration

#### Key Performance Indicators
```json
{
  "dashboard": {
    "title": "RAG System Performance Dashboard",
    "panels": [
      {
        "title": "Request Rate",
        "type": "graph",
        "targets": [
          {
            "expr": "rate(http_requests_total[5m])",
            "legendFormat": "{{method}} {{endpoint}}"
          }
        ]
      },
      {
        "title": "Response Time Percentiles",
        "type": "graph",
        "targets": [
          {
            "expr": "histogram_quantile(0.50, rate(http_request_duration_seconds_bucket[5m]))",
            "legendFormat": "P50"
          },
          {
            "expr": "histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))",
            "legendFormat": "P95"
          },
          {
            "expr": "histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))",
            "legendFormat": "P99"
          }
        ]
      },
      {
        "title": "System Resources",
        "type": "stat",
        "targets": [
          {
            "expr": "memory_usage_bytes",
            "legendFormat": "Memory Usage"
          },
          {
            "expr": "cpu_usage_percent",
            "legendFormat": "CPU Usage"
          }
        ]
      }
    ]
  }
}
```

## Deployment Guide

### Automated Deployment Script

#### Performance Optimization Deployment
```bash
#!/bin/bash
# deploy_performance_optimizations.sh

set -e

# Configuration
RAG_DIR="/Users/goodwiinz/development/RAG_system/rag"
BACKUP_DIR="$RAG_DIR/backups/$(date +%Y%m%d_%H%M%S)"

# Create backup
mkdir -p "$BACKUP_DIR"
cp "$RAG_DIR/docker-compose.yml" "$BACKUP_DIR/docker-compose.yml.backup"

# Apply optimizations
echo "Applying database optimizations..."
docker exec -i rag-postgres psql -U raguser -d ragdb < database/performance_optimization.sql

echo "Applying Neo4j optimizations..."
docker exec -i rag-neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" < database/neo4j_performance.cypher

echo "Applying Redis optimizations..."
docker cp database/redis_optimization.conf rag-redis:/usr/local/etc/redis/redis.conf
docker restart rag-redis

echo "Configuring monitoring..."
docker cp monitoring/grafana/dashboards/rag-performance-dashboard.json rag-grafana:/etc/grafana/provisioning/dashboards/
docker cp monitoring/performance_alerts.yml rag-prometheus:/etc/prometheus/

echo "Restarting services with optimizations..."
docker-compose -f docker-compose.yml -f docker-compose.override.yml up -d

echo "Running performance validation..."
python tests/performance/load_test_scenarios.py

echo "Performance optimization deployment completed!"
```

### Configuration Management

#### Environment Configuration
```bash
# .env.performance
# Performance optimization settings

# Backend performance
WORKERS=4
MAX_CONNECTIONS=100
CACHE_TTL=300
ENABLE_COMPRESSION=true

# Database optimization
POSTGRES_SHARED_BUFFERS=256MB
POSTGRES_EFFECTIVE_CACHE_SIZE=1GB
POSTGRES_WORK_MEM=4MB

# Redis optimization
REDIS_MAXMEMORY=1gb
REDIS_MAXMEMORY_POLICY=allkeys-lru

# Monitoring
ENABLE_METRICS=true
METRICS_PORT=8000
LOG_LEVEL=INFO
```

## Troubleshooting Runbook

### Common Performance Issues

#### High Response Times
```bash
# Check system resources
docker stats
top
htop

# Check database performance
docker exec rag-postgres psql -U raguser -d ragdb -c "
SELECT query, mean_time, calls, total_time
FROM pg_stat_statements
ORDER BY mean_time DESC
LIMIT 10;"

# Check Redis performance
docker exec rag-redis redis-cli --latency
docker exec rag-redis redis-cli info memory

# Check application logs
docker logs rag-backend --tail 100
```

#### Memory Issues
```bash
# Check memory usage by process
docker stats --no-stream
ps aux --sort=-%mem | head

# Check for memory leaks
docker exec rag-backend python -c "
import psutil
import time
process = psutil.Process()
print(f'Memory: {process.memory_info().rss / 1024 / 1024:.2f} MB')
"

# Force garbage collection
docker exec rag-backend python -c "
import gc
collected = gc.collect()
print(f'Garbage collected {collected} objects')
"
```

#### Database Performance Issues
```bash
# Check slow queries
docker exec rag-postgres psql -U raguser -d ragdb -c "
SELECT query, mean_time, calls
FROM pg_stat_statements
WHERE mean_time > 1000
ORDER BY mean_time DESC;"

# Check index usage
docker exec rag-postgres psql -U raguser -d ragdb -c "
SELECT schemaname, tablename, indexname, idx_scan, idx_tup_read
FROM pg_stat_user_indexes
ORDER BY idx_scan;"

# Rebuild indexes if needed
docker exec rag-postgres psql -U raguser -d ragdb -c "REINDEX DATABASE ragdb;"
```

#### Cache Performance Issues
```bash
# Check Redis hit rate
docker exec rag-redis redis-cli info stats | grep keyspace

# Monitor cache operations
docker exec rag-redis redis-cli monitor

# Clear cache if needed
docker exec rag-redis redis-cli FLUSHDB
```

### Performance Recovery Procedures

#### Emergency Performance Recovery
```bash
# 1. Scale down load
docker-compose up -d --scale backend=1

# 2. Clear caches
docker exec rag-redis redis-cli FLUSHDB

# 3. Restart services
docker-compose restart backend postgres redis neo4j qdrant

# 4. Monitor recovery
watch -n 5 'curl -s http://localhost:8000/health | jq .'
```

#### Database Recovery
```bash
# Check database connections
docker exec rag-postgres psql -U raguser -d ragdb -c "SELECT count(*) FROM pg_stat_activity;"

# Kill long-running queries
docker exec rag-postgres psql -U raguser -d ragdb -c "
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'active' AND query_start < now() - interval '5 minutes';"

# Restart database
docker restart rag-postgres
```

### Performance Monitoring Commands

#### Real-time Monitoring
```bash
# System monitoring
watch -n 2 'docker stats --no-stream'

# Application monitoring
curl -s http://localhost:8000/metrics | grep http_request_duration

# Database monitoring
docker exec rag-postgres psql -U raguser -d ragdb -c "
SELECT state, count(*)
FROM pg_stat_activity
GROUP BY state;"

# Cache monitoring
docker exec rag-redis redis-cli info memory | grep used_memory
```

#### Performance Testing
```bash
# Quick performance test
curl -w "@curl-format.txt" -o /dev/null -s "http://localhost:8000/api/v1/search?q=test"

# Load test
cd tests/performance
python load_test_scenarios.py --users 10 --duration 60

# Stress test
locust -f locustfile.py --host=http://localhost:8000 --users=50 --spawn-rate=5 --run-time=300s
```

This comprehensive performance optimization guide provides detailed implementation strategies, monitoring approaches, and troubleshooting procedures for maintaining optimal performance across the entire Multimodal Enterprise RAG System.
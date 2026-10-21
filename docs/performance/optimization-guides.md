# Performance Optimization Guides

> **Stale (2026-10-17): Qdrant is fully removed and live retrieval is PostgreSQL full-text search, so statements about Qdrant or vector stores below are historical. Measurements and file paths below describe the system when the document was written and are not current performance evidence.**

## Comprehensive Best Practices for the Multimodal Enterprise RAG System

This document provides comprehensive optimization guides and best practices for ensuring optimal performance of the Knowledge Graph Analytics Dashboard and the entire RAG system.

## Table of Contents
1. [System Overview](#system-overview)
2. [Performance Goals](#performance-goals)
3. [Database Optimization](#database-optimization)
4. [Backend Performance](#backend-performance)
5. [Frontend Optimization](#frontend-optimization)
6. [API Performance](#api-performance)
7. [Caching Strategies](#caching-strategies)
8. [Network Optimization](#network-optimization)
9. [Resource Optimization](#resource-optimization)
10. [Monitoring and Alerting](#monitoring-and-alerting)
11. [Performance Testing](#performance-testing)
12. [Troubleshooting Guide](#troubleshooting-guide)

## System Overview

The Multimodal Enterprise RAG System consists of several performance-critical components:

### Architecture Components
- **Frontend**: Next.js application with React components
- **Backend**: FastAPI services with async/await patterns
- **Databases**: PostgreSQL (analytics), Neo4j (knowledge graph), Qdrant (vector store), Redis (cache)
- **Search**: Elasticsearch for advanced search capabilities
- **Infrastructure**: Docker containers with resource limits

### Performance Characteristics
- **Target Response Time**: < 200ms for 95th percentile
- **Target Throughput**: 1000+ requests per second
- **Target Availability**: 99.9% uptime
- **Target Error Rate**: < 0.1%

## Performance Goals

### Primary Performance Indicators
| Metric | Target | Measurement Method |
|--------|--------|-------------------|
| Page Load Time | < 2 seconds | Web Vitals (LCP) |
| API Response Time | < 200ms (P95) | Server-side timing |
| Database Query Time | < 100ms (P95) | Query profiling |
| Cache Hit Ratio | > 90% | Cache metrics |
| Memory Usage | < 2GB per container | System monitoring |
| CPU Usage | < 70% average | System monitoring |

### Performance Budgets
```javascript
// Frontend Performance Budgets
const performanceBudgets = {
  javascript: 250,  // KB gzipped
  css: 50,         // KB gzipped
  images: 500,     // KB optimized
  fonts: 100,      // KB gzipped
  total: 1000      // KB total transfer size
};

// API Performance Budgets
const apiBudgets = {
  'GET /api/search': 150,      // ms
  'GET /api/analytics': 200,   // ms
  'POST /api/documents': 1000, // ms
  'GET /api/graph': 300,       // ms
  'default': 200               // ms
};
```

## Database Optimization

### PostgreSQL Optimization

#### Query Optimization
```sql
-- Enable query statistics
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Analyze slow queries
SELECT
  query,
  calls,
  total_exec_time,
  mean_exec_time,
  rows
FROM pg_stat_statements
WHERE mean_exec_time > 100
ORDER BY mean_exec_time DESC
LIMIT 10;

-- Missing indexes analysis
SELECT
  schemaname,
  tablename,
  attname,
  n_distinct,
  correlation
FROM pg_stats
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  AND n_distinct > 100;
```

#### Index Optimization
```sql
-- Create composite indexes for common query patterns
CREATE INDEX CONCURRENTLY idx_analytics_metrics_timestamp_user
ON analytics_metrics (timestamp DESC, user_id);

-- Create partial indexes for filtered queries
CREATE INDEX CONCURRENTLY idx_documents_active
ON documents (id) WHERE status = 'active';

-- Use covering indexes to avoid table lookups
CREATE INDEX CONCURRENTLY idx_search_results_covering
ON search_results (query_id, score, document_id, metadata);
```

#### Connection Pooling
```python
# Recommended connection pool settings
DATABASE_CONFIG = {
    "pool_size": 20,
    "max_overflow": 30,
    "pool_timeout": 30,
    "pool_recycle": 3600,
    "pool_pre_ping": True,
    "echo": False  # Disable in production
}
```

#### Configuration Optimization
```postgresql
# postgresql.conf optimization for RAG system
# Memory Configuration
shared_buffers = 256MB                    # 25% of RAM
effective_cache_size = 1GB                # 75% of RAM
work_mem = 4MB                           # Per query
maintenance_work_mem = 64MB              # Maintenance operations

# Query Planning
random_page_cost = 1.1                   # SSD optimization
effective_io_concurrency = 200            # SSD parallel I/O

# Checkpoint Configuration
checkpoint_completion_target = 0.9
wal_buffers = 16MB
default_statistics_target = 100
```

### Neo4j Optimization

#### Query Optimization
```cypher
// Use EXPLAIN to analyze query plans
EXPLAIN MATCH (u:User)-[:CREATED]->(d:Document)
WHERE d.created_at > datetime() - duration('P7D')
RETURN u, d;

// Use indexes for fast lookups
CREATE INDEX document_created_at FOR (d:Document) ON (d.created_at);
CREATE INDEX user_email FOR (u:User) ON (u.email);

// Optimize traversal queries
MATCH (u:User {id: $user_id})-[:HAS_ACCESS*1..3]->(d:Document)
WHERE d.type = $document_type
RETURN d LIMIT 100;
```

#### Memory Configuration
```properties
# neo4j.conf optimization
# Memory Settings
dbms.memory.heap.initial_size=512m
dbms.memory.heap.max_size=2G
dbms.memory.pagecache.size=1G

# Query Optimization
dbms.query_cache_size=1000
dbms.cypher.forbid_exhaustive_shortestpath=true
dbms.track_query_cpu_time=true

# Transaction Settings
dbms.transaction.timeout=60s
dbms.transaction.concurrent_maximum=1000
```

### Qdrant Optimization

#### Collection Optimization
```python
# Optimize collection settings for search performance
collection_config = {
    "vectors": {
        "size": 1536,
        "distance": "Cosine"
    },
    "optimization_config": {
        "default_segment_number": 2,
        "max_segment_size": 200000,
        "memmap_threshold": 50000,
        "indexing_threshold": 20000,
        "flush_interval_sec": 5,
        "max_optimization_threads": 1
    }
}
```

#### Search Optimization
```python
# Use batch search for multiple queries
async def batch_search(queries: List[str], limit: int = 10):
    search_tasks = [
        client.search(
            collection_name="documents",
            query_vector=embed_query(query),
            limit=limit,
            search_params={"exact": False, "hnsw_ef": 128}
        )
        for query in queries
    ]
    return await asyncio.gather(*search_tasks)
```

## Backend Performance

### Async/Await Optimization

#### Proper Async Patterns
```python
# BAD: Blocking operations in async functions
async def process_document_bad(document_id: str):
    time.sleep(1)  # Blocking!
    result = cpu_intensive_operation(data)
    return result

# GOOD: Non-blocking operations
async def process_document_good(document_id: str):
    await asyncio.sleep(1)  # Non-blocking
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, cpu_intensive_operation, data
    )
    return result
```

#### Concurrent Operations
```python
# Use asyncio.gather for concurrent operations
async def fetch_user_data(user_id: str):
    tasks = [
        fetch_user_profile(user_id),
        fetch_user_documents(user_id),
        fetch_user_analytics(user_id)
    ]
    profile, documents, analytics = await asyncio.gather(*tasks)
    return {
        "profile": profile,
        "documents": documents,
        "analytics": analytics
    }
```

### Memory Management

#### Memory Profiling
```python
# Monitor memory usage
import tracemalloc
import psutil

def profile_memory(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        process = psutil.Process()
        initial_memory = process.memory_info().rss

        tracemalloc.start()
        result = await func(*args, **kwargs)
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        final_memory = process.memory_info().rss
        memory_delta = final_memory - initial_memory

        logger.info(f"Memory usage: +{memory_delta:,} bytes, Peak: {peak:,} bytes")
        return result
    return wrapper
```

#### Object Pooling
```python
class DatabaseConnectionPool:
    def __init__(self, max_connections: int = 20):
        self.pool = asyncio.Queue(maxsize=max_connections)
        self.max_connections = max_connections
        self.created_connections = 0

    async def get_connection(self):
        try:
            return self.pool.get_nowait()
        except asyncio.QueueEmpty:
            if self.created_connections < self.max_connections:
                self.created_connections += 1
                return await self._create_connection()
            else:
                return await self.pool.get()

    async def return_connection(self, connection):
        try:
            self.pool.put_nowait(connection)
        except asyncio.QueueFull:
            await connection.close()
            self.created_connections -= 1
```

### Caching Implementation

#### Multi-Level Caching
```python
class MultiLevelCache:
    def __init__(self):
        self.l1_cache = {}  # In-memory cache
        self.l2_cache = redis.Redis()  # Redis cache
        self.l1_ttl = 60  # 1 minute
        self.l2_ttl = 3600  # 1 hour

    async def get(self, key: str):
        # L1 Cache
        if key in self.l1_cache:
            item, timestamp = self.l1_cache[key]
            if time.time() - timestamp < self.l1_ttl:
                return item

        # L2 Cache
        item = await self.l2_cache.get(key)
        if item:
            self.l1_cache[key] = (item, time.time())
            return json.loads(item)

        return None

    async def set(self, key: str, value: Any):
        # Set in both caches
        self.l1_cache[key] = (value, time.time())
        await self.l2_cache.setex(
            key,
            self.l2_ttl,
            json.dumps(value, default=str)
        )
```

## Frontend Optimization

### Code Splitting and Lazy Loading

#### Route-based Code Splitting
```typescript
// Dynamic imports for route components
const AnalyticsDashboard = dynamic(
  () => import('../components/analytics/AnalyticsDashboard'),
  {
    loading: () => <div>Loading dashboard...</div>,
    ssr: false
  }
);

const DocumentUpload = dynamic(
  () => import('../components/documents/DocumentUpload'),
  {
    loading: () => <div>Loading upload...</div>
  }
);
```

#### Component-level Lazy Loading
```typescript
// Lazy load heavy components
const GraphVisualization = lazy(() =>
  import('../components/graph/GraphVisualization')
);

// Use with Suspense
function App() {
  return (
    <Suspense fallback={<div>Loading graph...</div>}>
      <GraphVisualization />
    </Suspense>
  );
}
```

### Bundle Optimization

#### Webpack Configuration
```javascript
// next.config.js optimizations
module.exports = {
  webpack: (config, { isServer }) => {
    // Code splitting
    config.optimization.splitChunks = {
      chunks: 'all',
      cacheGroups: {
        vendor: {
          test: /[\\/]node_modules[\\/]/,
          name: 'vendors',
          chunks: 'all',
        },
        common: {
          name: 'common',
          minChunks: 2,
          chunks: 'all',
          enforce: true,
        },
      },
    };

    // Compression
    config.optimization.minimize = true;

    return config;
  },

  // Image optimization
  images: {
    formats: ['image/webp', 'image/avif'],
    deviceSizes: [640, 750, 828, 1080, 1200, 1920, 2048, 3840],
    imageSizes: [16, 32, 48, 64, 96, 128, 256, 384],
  },
};
```

### Performance Monitoring

#### Web Vitals Tracking
```typescript
// Track Core Web Vitals
import { getCLS, getFID, getFCP, getLCP, getTTFB } from 'web-vitals';

function sendToAnalytics(metric: any) {
  // Send to your analytics service
  gtag('event', metric.name, {
    value: metric.value,
    metric_id: metric.id,
    metric_value: metric.value,
    metric_delta: metric.delta,
  });
}

getCLS(sendToAnalytics);
getFID(sendToAnalytics);
getFCP(sendToAnalytics);
getLCP(sendToAnalytics);
getTTFB(sendToAnalytics);
```

## API Performance

### Response Optimization

#### Compression
```python
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

app = FastAPI()

# Enable GZIP compression
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Custom compression middleware
@app.middleware("http")
async def compression_middleware(request: Request, call_next):
    response = await call_next(request)

    # Compress large JSON responses
    if (response.headers.get("content-type") == "application/json" and
        len(response.body) > 1024):
        # Apply compression
        pass

    return response
```

#### Response Caching
```python
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend

# Configure caching
FastAPICache.init(RedisBackend(redis_client), prefix="fastapi-cache")

@app.get("/api/analytics/dashboard")
@cache(expire=60)  # Cache for 1 minute
async def get_analytics_dashboard():
    return await generate_analytics_data()
```

### Request Optimization

#### Batch Processing
```python
@app.post("/api/batch/search")
async def batch_search(requests: List[SearchRequest]):
    """Process multiple search requests efficiently"""
    # Collect all queries
    queries = [req.query for req in requests]

    # Batch embedding generation
    embeddings = await generate_embeddings_batch(queries)

    # Batch vector search
    results = await search_vectors_batch(embeddings)

    return results
```

#### Pagination
```python
@app.get("/api/documents")
async def get_documents(
    page: int = 1,
    size: int = 20,
    sort_by: str = "created_at",
    sort_order: str = "desc"
):
    """Efficiently paginate large datasets"""

    # Validate page size
    size = min(size, 100)  # Max 100 items per page

    # Use cursor-based pagination for better performance
    offset = (page - 1) * size

    query = select(Document).offset(offset).limit(size)

    # Add sorting
    if sort_order == "desc":
        query = query.order_by(desc(getattr(Document, sort_by)))
    else:
        query = query.order_by(asc(getattr(Document, sort_by)))

    return await database.execute(query)
```

## Caching Strategies

### Cache Hierarchy

#### Level 1: In-Memory Cache
```python
from functools import lru_cache
import asyncio

# LRU Cache for frequently accessed data
@lru_cache(maxsize=1000)
def get_user_permissions(user_id: str):
    # Cache user permissions for 5 minutes
    return fetch_permissions_from_db(user_id)

# Async LRU Cache wrapper
class AsyncLRUCache:
    def __init__(self, maxsize: int = 1000, ttl: int = 300):
        self.cache = {}
        self.maxsize = maxsize
        self.ttl = ttl

    async def get(self, key: str):
        if key in self.cache:
            item, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return item
            else:
                del self.cache[key]
        return None

    async def set(self, key: str, value: Any):
        if len(self.cache) >= self.maxsize:
            # Remove oldest item
            oldest_key = min(self.cache.keys(),
                           key=lambda k: self.cache[k][1])
            del self.cache[oldest_key]

        self.cache[key] = (value, time.time())
```

#### Level 2: Redis Cache
```python
import aioredis

class RedisCacheManager:
    def __init__(self):
        self.redis = None

    async def connect(self):
        self.redis = await aioredis.from_url("redis://localhost")

    async def get_json(self, key: str):
        data = await self.redis.get(key)
        return json.loads(data) if data else None

    async def set_json(self, key: str, value: Any, ttl: int = 3600):
        data = json.dumps(value, default=str)
        await self.redis.setex(key, ttl, data)

    async def invalidate_pattern(self, pattern: str):
        keys = await self.redis.keys(pattern)
        if keys:
            await self.redis.delete(*keys)
```

### Cache Invalidation

#### Smart Invalidation
```python
class CacheInvalidator:
    def __init__(self, cache_manager):
        self.cache = cache_manager
        self.dependencies = {}  # Track cache dependencies

    def invalidate_related(self, entity_type: str, entity_id: str):
        """Invalidate all cache entries related to an entity"""
        patterns = [
            f"{entity_type}:{entity_id}:*",
            f"search:{entity_type}:*",
            f"analytics:{entity_type}:*"
        ]

        for pattern in patterns:
            asyncio.create_task(self.cache.invalidate_pattern(pattern))

    def track_dependency(self, cache_key: str, dependencies: List[str]):
        """Track dependencies for cache entries"""
        self.dependencies[cache_key] = dependencies
```

## Network Optimization

### HTTP/2 and Compression

#### Server Configuration
```nginx
# Nginx configuration for HTTP/2
server {
    listen 443 ssl http2;

    # Enable Brotli compression
    brotli on;
    brotli_comp_level 6;
    brotli_types text/plain text/css application/json application/javascript text/xml application/xml application/xml+rss text/javascript;

    # Enable GZIP fallback
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml application/xml+rss text/javascript;

    # Connection keep-alive
    keepalive_timeout 65;
    keepalive_requests 100;
}
```

#### Client-Side Optimization
```typescript
// HTTP/2 connection reuse
const httpClient = {
  // Reuse connections for multiple requests
  async request(url: string, options: RequestInit) {
    // Use connection pooling
    return fetch(url, {
      ...options,
      keepalive: true,
    });
  },

  // Batch requests to reduce round trips
  async batchRequests(requests: Array<{url: string, options?: RequestInit}>) {
    const batchBody = requests.map(req => ({
      method: req.options?.method || 'GET',
      url: req.url,
      headers: req.options?.headers,
      body: req.options?.body
    }));

    return fetch('/api/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(batchBody)
    });
  }
};
```

### CDN Integration

#### Static Asset Optimization
```typescript
// CDN URL generation
function getCDNUrl(assetPath: string): string {
  const CDN_DOMAIN = process.env.NEXT_PUBLIC_CDN_DOMAIN;
  const VERSION = process.env.NEXT_PUBLIC_BUILD_VERSION;

  return `https://${CDN_DOMAIN}/assets/${VERSION}/${assetPath}`;
}

// Progressive image loading
export function OptimizedImage({
  src,
  alt,
  width,
  height,
  priority = false
}: ImageProps) {
  const [loaded, setLoaded] = useState(false);

  // Generate responsive image URLs
  const sources = [
    { srcSet: `${src}?w=640 640w, ${src}?w=750 750w, ${src}?w=828 828w` },
    { srcSet: `${src}?w=1080 1080w, ${src}?w=1200 1200w, ${src}?w=1920 1920w` }
  ];

  return (
    <picture>
      {sources.map(source => (
        <source key={source.srcSet} {...source} />
      ))}
      <img
        src={`${src}?w=640&format=webp`}
        alt={alt}
        width={width}
        height={height}
        loading={priority ? "eager" : "lazy"}
        onLoad={() => setLoaded(true)}
        className={`transition-opacity duration-300 ${
          loaded ? 'opacity-100' : 'opacity-0'
        }`}
      />
    </picture>
  );
}
```

## Resource Optimization

### Memory Management

#### Garbage Collection Optimization
```python
# Optimize garbage collection for RAG system
import gc
import weakref

class ResourceManager:
    def __init__(self):
        self.resources = weakref.WeakSet()

    def register_resource(self, resource):
        self.resources.add(resource)

    def cleanup(self):
        # Force garbage collection
        collected = gc.collect()
        logger.info(f"Garbage collected {collected} objects")

        # Clear resource references
        for resource in list(self.resources):
            if hasattr(resource, 'cleanup'):
                resource.cleanup()

# Configure GC thresholds
gc.set_threshold(700, 10, 10)  # Adjust for RAG system workload
```

#### Memory Profiling
```python
import memory_profiler
import psutil

@memory_profiler.profile
def memory_intensive_function():
    """Profile memory usage of intensive operations"""
    large_data = [i for i in range(1000000)]
    processed_data = [x * 2 for x in large_data]
    return processed_data

# Monitor memory usage in production
def monitor_memory():
    process = psutil.Process()
    memory_info = process.memory_info()

    logger.info(f"RSS: {memory_info.rss / 1024 / 1024:.2f} MB")
    logger.info(f"VMS: {memory_info.vms / 1024 / 1024:.2f} MB")
    logger.info(f"Percent: {process.memory_percent():.2f}%")

    # Alert if memory usage is high
    if process.memory_percent() > 80:
        logger.warning("High memory usage detected!")
```

### CPU Optimization

#### Parallel Processing
```python
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

# CPU-bound operations use ProcessPool
async def process_documents_parallel(documents: List[str]):
    """Process documents using multiple CPU cores"""
    with ProcessPoolExecutor(max_workers=multiprocessing.cpu_count()) as executor:
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(executor, process_single_document, doc)
            for doc in documents
        ]
        return await asyncio.gather(*tasks)

# I/O-bound operations use ThreadPool
async def fetch_urls_parallel(urls: List[str]):
    """Fetch multiple URLs concurrently"""
    with ThreadPoolExecutor(max_workers=50) as executor:
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(executor, fetch_url, url)
            for url in urls
        ]
        return await asyncio.gather(*tasks)
```

## Monitoring and Alerting

### Performance Metrics

#### Key Performance Indicators
```python
# Define performance KPIs
PERFORMANCE_KPI = {
    "response_time_p95": 200,      # ms
    "response_time_p99": 500,      # ms
    "error_rate": 0.001,           # 0.1%
    "throughput_rps": 1000,        # requests per second
    "cpu_usage": 0.70,             # 70%
    "memory_usage": 0.80,          # 80%
    "disk_io_wait": 10,            # ms
    "cache_hit_ratio": 0.90,       # 90%
    "db_connection_usage": 0.80,    # 80%
}

class PerformanceMonitor:
    def __init__(self):
        self.metrics = {}
        self.alerts = []

    def track_metric(self, name: str, value: float):
        """Track a performance metric"""
        self.metrics[name] = {
            "value": value,
            "timestamp": datetime.now(),
            "threshold": PERFORMANCE_KPI.get(name)
        }

        # Check for alerts
        if name in PERFORMANCE_KPI:
            threshold = PERFORMANCE_KPI[name]
            if self._is_violation(name, value, threshold):
                self._trigger_alert(name, value, threshold)

    def _is_violation(self, metric: str, value: float, threshold: float) -> bool:
        """Check if metric violates threshold"""
        if metric in ["response_time_p95", "response_time_p99", "error_rate",
                     "cpu_usage", "memory_usage", "disk_io_wait"]:
            return value > threshold
        else:
            return value < threshold
```

#### Real-time Dashboards
```python
# Grafana dashboard configuration
GRAFANA_DASHBOARD = {
    "dashboard": {
        "title": "RAG System Performance",
        "panels": [
            {
                "title": "Response Time",
                "type": "graph",
                "targets": [
                    {
                        "expr": "histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))",
                        "legendFormat": "95th percentile"
                    },
                    {
                        "expr": "histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))",
                        "legendFormat": "99th percentile"
                    }
                ]
            },
            {
                "title": "Error Rate",
                "type": "singlestat",
                "targets": [
                    {
                        "expr": "rate(http_requests_total{status=~\"5..\"}[5m]) / rate(http_requests_total[5m])",
                        "legendFormat": "Error Rate"
                    }
                ]
            },
            {
                "title": "System Resources",
                "type": "graph",
                "targets": [
                    {
                        "expr": "cpu_usage_percent",
                        "legendFormat": "CPU %"
                    },
                    {
                        "expr": "memory_usage_percent",
                        "legendFormat": "Memory %"
                    }
                ]
            }
        ]
    }
}
```

### Alerting Rules

#### Prometheus Alerting Rules
```yaml
# prometheus_rules.yml
groups:
  - name: rag_system_performance
    rules:
      - alert: HighResponseTime
        expr: histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])) > 0.2
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "High response time detected"
          description: "95th percentile response time is {{ $value }}s"

      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~\"5..\"}[5m]) / rate(http_requests_total[5m]) > 0.01
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "High error rate detected"
          description: "Error rate is {{ $value | humanizePercentage }}"

      - alert: HighMemoryUsage
        expr: memory_usage_percent > 0.85
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High memory usage detected"
          description: "Memory usage is {{ $value | humanizePercentage }}"
```

## Performance Testing

### Load Testing

#### K6 Load Testing Script
```javascript
// k6-load-test.js
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate } from 'k6/metrics';

const errorRate = new Rate('errors');

export let options = {
  stages: [
    { duration: '2m', target: 10 },   // Ramp up to 10 users
    { duration: '5m', target: 10 },   // Stay at 10 users
    { duration: '2m', target: 50 },   // Ramp up to 50 users
    { duration: '5m', target: 50 },   // Stay at 50 users
    { duration: '2m', target: 100 },  // Ramp up to 100 users
    { duration: '5m', target: 100 },  // Stay at 100 users
    { duration: '2m', target: 0 },    // Ramp down
  ],
  thresholds: {
    http_req_duration: ['p(95)<200'], // 95% of requests under 200ms
    http_req_failed: ['rate<0.01'],    // Error rate below 1%
    errors: ['rate<0.01'],             // Custom error rate below 1%
  },
};

const BASE_URL = 'http://localhost:8000';

export default function() {
  // Test search endpoint
  let searchResponse = http.post(`${BASE_URL}/api/search`, JSON.stringify({
    query: 'test query',
    limit: 10
  }), {
    headers: { 'Content-Type': 'application/json' },
  });

  let searchOk = check(searchResponse, {
    'search status is 200': (r) => r.status === 200,
    'search response time < 200ms': (r) => r.timings.duration < 200,
  });

  errorRate.add(!searchOk);

  // Test analytics endpoint
  let analyticsResponse = http.get(`${BASE_URL}/api/analytics/dashboard`, {
    headers: { 'Authorization': 'Bearer test-token' },
  });

  let analyticsOk = check(analyticsResponse, {
    'analytics status is 200': (r) => r.status === 200,
    'analytics response time < 500ms': (r) => r.timings.duration < 500,
  });

  errorRate.add(!analyticsOk);

  sleep(1);
}

export function handleSummary(data) {
  console.log('Performance test summary:');
  console.log(`  Requests: ${data.metrics.http_reqs.count}`);
  console.log(`  Avg response time: ${data.metrics.http_req_duration.avg}ms`);
  console.log(`  95th percentile: ${data.metrics.http_req_duration['p(95)']}ms`);
  console.log(`  Error rate: ${data.metrics.errors.rate * 100}%`);
}
```

#### Locust Performance Testing
```python
# locustfile.py
from locust import HttpUser, task, between
import random
import json

class RAGSystemUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        """Called when a user starts"""
        # Login and get token
        response = self.client.post("/api/auth/login", json={
            "email": "test@example.com",
            "password": "testpassword"
        })
        if response.status_code == 200:
            self.token = response.json()["access_token"]
        else:
            self.token = None

    @task(3)
    def search_documents(self):
        """Search for documents"""
        queries = [
            "machine learning algorithms",
            "data processing pipelines",
            "knowledge graph construction",
            "vector search optimization",
            "semantic similarity"
        ]

        query = random.choice(queries)
        response = self.client.post(
            "/api/search",
            json={"query": query, "limit": 10},
            headers={"Authorization": f"Bearer {self.token}"} if self.token else {}
        )

        if response.status_code != 200:
            response.failure("Search request failed")

    @task(2)
    def get_analytics_dashboard(self):
        """Get analytics dashboard"""
        response = self.client.get(
            "/api/analytics/dashboard",
            headers={"Authorization": f"Bearer {self.token}"} if self.token else {}
        )

        if response.status_code != 200:
            response.failure("Analytics request failed")

    @task(1)
    def get_knowledge_graph(self):
        """Get knowledge graph data"""
        response = self.client.get(
            "/api/graph/entities",
            headers={"Authorization": f"Bearer {self.token}"} if self.token else {}
        )

        if response.status_code != 200:
            response.failure("Graph request failed")

    @task(1)
    def upload_document(self):
        """Upload a document"""
        # Simulate file upload
        files = {
            "file": ("test.txt", "This is a test document content", "text/plain")
        }

        response = self.client.post(
            "/api/documents/upload",
            files=files,
            headers={"Authorization": f"Bearer {self.token}"} if self.token else {}
        )

        if response.status_code not in [200, 201]:
            response.failure("Upload request failed")
```

### Stress Testing

#### Database Stress Testing
```python
import asyncio
import asyncpg
import time
from concurrent.futures import ThreadPoolExecutor

async def stress_test_database():
    """Stress test database with concurrent queries"""

    # Connection pool
    pool = await asyncpg.create_pool(
        "postgresql://user:pass@localhost/ragdb",
        min_size=10,
        max_size=50
    )

    async def run_query(query_id: int):
        """Run a single test query"""
        async with pool.acquire() as conn:
            start_time = time.time()

            # Simulate complex analytics query
            result = await conn.fetch("""
                SELECT
                    d.id,
                    d.title,
                    COUNT(DISTINCT e.id) as entity_count,
                    COUNT(DISTINCT r.related_id) as relation_count
                FROM documents d
                LEFT JOIN entities e ON e.document_id = d.id
                LEFT JOIN relations r ON r.document_id = d.id
                WHERE d.created_at > NOW() - INTERVAL '7 days'
                GROUP BY d.id, d.title
                ORDER BY entity_count DESC, relation_count DESC
                LIMIT 100
            """)

            duration = time.time() - start_time
            return query_id, len(result), duration

    # Run concurrent queries
    tasks = [run_query(i) for i in range(100)]
    results = await asyncio.gather(*tasks)

    # Analyze results
    durations = [r[2] for r in results]
    row_counts = [r[1] for r in results]

    print(f"Average query time: {sum(durations) / len(durations):.3f}s")
    print(f"Max query time: {max(durations):.3f}s")
    print(f"Average rows returned: {sum(row_counts) / len(row_counts)}")

    await pool.close()
```

## Troubleshooting Guide

### Common Performance Issues

#### High Response Times
1. **Database Queries**
   ```sql
   -- Find slow queries
   SELECT query, mean_exec_time, calls
   FROM pg_stat_statements
   ORDER BY mean_exec_time DESC
   LIMIT 10;

   -- Check query plans
   EXPLAIN ANALYZE SELECT * FROM large_table WHERE condition;
   ```

2. **Memory Leaks**
   ```python
   # Monitor memory growth
   import tracemalloc
   tracemalloc.start()

   # Your code here

   # Print memory usage
   snapshot = tracemalloc.take_snapshot()
   top_stats = snapshot.statistics('lineno')
   for stat in top_stats[:10]:
       print(stat)
   ```

3. **Blocking Operations**
   ```python
   # Identify blocking calls
   import asyncio
   import cProfile

   def profile_async_function():
       profiler = cProfile.Profile()
       profiler.enable()

       # Run your async function
       asyncio.run(your_async_function())

       profiler.disable()
       profiler.print_stats(sort='cumulative')
   ```

#### High CPU Usage
1. **CPU Profiling**
   ```python
   import py-spy

   # Profile running process
   # py-spy top --pid <process_id>

   # Generate flame graph
   # py-spy record --pid <process_id> -o profile.svg
   ```

2. **Algorithm Optimization**
   ```python
   # Use efficient data structures
   from collections import defaultdict, deque

   # Vectorized operations with numpy
   import numpy as np

   # Avoid O(n²) operations
   def efficient_operation(data):
       # Use sets for O(1) lookups
       data_set = set(data)
       # Use dict comprehensions
       result = {item: process(item) for item in data_set}
       return result
   ```

#### High Memory Usage
1. **Memory Profiling**
   ```python
   from memory_profiler import profile

   @profile
   def memory_intensive_function():
       large_list = [i for i in range(1000000)]
       # Process data
       return large_list
   ```

2. **Generator Usage**
   ```python
   # Use generators for large datasets
   def process_large_file(filename):
       with open(filename) as f:
           for line in f:
               yield process_line(line)

   # Instead of loading all into memory
   # lines = [process_line(line) for line in open(filename)]
   ```

### Performance Debugging Tools

#### Frontend Tools
```javascript
// Performance API usage
function measurePageLoad() {
  const navigation = performance.getEntriesByType('navigation')[0];

  console.log('Page Load Metrics:');
  console.log(`  DOM Content Loaded: ${navigation.domContentLoadedEventEnd - navigation.domContentLoadedEventStart}ms`);
  console.log(`  Load Complete: ${navigation.loadEventEnd - navigation.loadEventStart}ms`);
  console.log(`  First Paint: ${navigation.responseStart - navigation.requestStart}ms`);
}

// User timing API
function measureCustomOperation(name, operation) {
  performance.mark(`${name}-start`);
  operation();
  performance.mark(`${name}-end`);
  performance.measure(name, `${name}-start`, `${name}-end`);

  const measures = performance.getEntriesByName(name);
  console.log(`${name}: ${measures[0].duration}ms`);
}
```

#### Backend Tools
```python
# Application profiling
import cProfile
import pstats

def profile_application():
    profiler = cProfile.Profile()
    profiler.enable()

    # Run your application code
    run_application()

    profiler.disable()

    # Save results
    stats = pstats.Stats(profiler)
    stats.sort_stats('cumulative')
    stats.print_stats(20)  # Top 20 functions

    # Save to file
    stats.dump_stats('profile.stats')

# Database query logging
import logging

# Configure query logging
logging.basicConfig()
logger = logging.getLogger('sqlalchemy.engine')
logger.setLevel(logging.INFO)
```

### Performance Optimization Checklist

#### Database Optimization
- [ ] Analyze slow queries with `EXPLAIN ANALYZE`
- [ ] Create appropriate indexes for query patterns
- [ ] Use connection pooling
- [ ] Optimize configuration parameters
- [ ] Monitor cache hit ratios
- [ ] Regular vacuum and analyze operations

#### Backend Optimization
- [ ] Profile function performance
- [ ] Use async/await properly
- [ ] Implement multi-level caching
- [ ] Optimize memory usage
- [ ] Use connection pooling for external services
- [ ] Monitor resource utilization

#### Frontend Optimization
- [ ] Implement code splitting
- [ ] Optimize bundle sizes
- [ ] Use lazy loading for images and components
- [ ] Monitor Core Web Vitals
- [ ] Optimize render performance
- [ ] Use service workers for caching

#### Infrastructure Optimization
- [ ] Configure load balancers properly
- [ ] Use CDN for static assets
- [ ] Enable HTTP/2 and compression
- [ ] Set up proper monitoring and alerting
- [ ] Configure auto-scaling policies
- [ ] Implement proper logging and tracing

This comprehensive optimization guide provides the foundation for maintaining optimal performance of the Knowledge Graph Analytics Dashboard and the entire RAG system. Regular monitoring, testing, and optimization based on these guidelines will ensure the system continues to meet performance requirements as it scales.
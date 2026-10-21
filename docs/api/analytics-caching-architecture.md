# Analytics Dashboard - Caching Architecture and Data Flow

> **Stale / historical (2026-10-17):** It covers only part of the API and its paths and schemas may have drifted. The source of truth is the generated `backend/openapi.json` (also served at `/docs` when `DEBUG` is on). Current contracts: [`docs/engineering/`](../engineering/README.md).

## 1. Multi-Level Caching Strategy

### Cache Hierarchy Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Client-Side Caching                               │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   Browser       │  │   Mobile App    │  │   Desktop App   │           │
│  │   Cache         │  │   Cache         │  │   Cache         │           │
│  │                 │  │                 │  │                 │           │
│  │ - HTTP Cache    │  │ - In-Memory     │  │ - Local Storage │           │
│  │ - Service Cache │  │ - Persistent    │  │ - IndexedDB     │           │
│  │ - IndexedDB     │  │   Cache         │  │ - File Cache    │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Edge/CDN Caching                                   │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   CloudFlare    │  │   AWS CloudFront│  │   Fastly        │           │
│  │   CDN           │  │   CDN           │  │   CDN           │           │
│  │                 │  │                 │  │                 │           │
│  │ - Static Assets │  │ - API Responses │  │ - Geo Distribution│           │
│  │ - API Caching   │  │ - Image Cache   │  │ - Edge Compute  │           │
│  │ - DDoS Protection│ │ - Compression   │  │ - Rate Limiting │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Application Layer Caching                            │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   Redis         │  │   Memcached     │  │   Application   │           │
│  │   Cluster       │  │   Cluster       │  │   Memory Cache  │           │
│  │                 │  │                 │  │                 │           │
│  │ - Session Store │  │ - Query Cache   │  │ - Computed      │           │
│  │ - Real-time     │  │ - Result Cache  │  │   Results       │           │
│  │   Data          │  │ - API Cache     │  │ - Configuration │           │
│  │ - Pub/Sub       │  │ - Temp Data     │  │   Cache         │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Database Layer Caching                               │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   PostgreSQL    │  │   Neo4j         │  │   Qdrant        │           │
│  │   Query Cache   │  │   Query Cache   │  │   Vector Cache  │           │
│  │                 │  │                 │  │                 │           │
│  │ - Query Plan    │  │ - Graph Traversal│ │ - Vector Index  │           │
│  │   Cache         │  │   Cache         │  │   Cache         │           │
│  │ - Materialized   │  │ - Node/Edge     │  │ - ANN Results   │           │
│  │   Views         │  │   Cache         │  │   Cache         │           │
│  │ - Connection    │  │ - Cypher Query  │  │ - Payload Cache │           │
│  │   Pooling       │  │   Cache         │  │                 │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 2. Cache Types and Use Cases

### 2.1 Real-time Analytics Cache
```python
class RealtimeAnalyticsCache:
    """Cache for real-time dashboard metrics"""

    CACHE_KEYS = {
        "metrics": "analytics:realtime:metrics:{org_id}",
        "kpi": "analytics:realtime:kpi:{org_id}:{metric_type}",
        "trend": "analytics:realtime:trend:{org_id}:{time_range}",
        "alerts": "analytics:realtime:alerts:{org_id}"
    }

    CACHE_TTL = {
        "metrics": 30,      # 30 seconds
        "kpi": 60,          # 1 minute
        "trend": 300,       # 5 minutes
        "alerts": 10        # 10 seconds
    }

    async def get_metrics(self, organization_id: str) -> dict:
        cache_key = self.CACHE_KEYS["metrics"].format(org_id=organization_id)
        cached_data = await self.redis.get(cache_key)

        if cached_data:
            return json.loads(cached_data)

        # Compute fresh metrics
        metrics = await self.compute_realtime_metrics(organization_id)
        await self.redis.setex(
            cache_key,
            self.CACHE_TTL["metrics"],
            json.dumps(metrics)
        )
        return metrics
```

### 2.2 Graph Analytics Cache
```python
class GraphAnalyticsCache:
    """Cache for expensive graph computations"""

    CACHE_KEYS = {
        "centrality": "analytics:graph:centrality:{org_id}:{algorithm}:{params_hash}",
        "clustering": "analytics:graph:clustering:{org_id}:{algorithm}:{params_hash}",
        "paths": "analytics:graph:paths:{org_id}:{source}:{target}:{algorithm}",
        "metrics": "analytics:graph:metrics:{org_id}:{snapshot_id}"
    }

    CACHE_TTL = {
        "centrality": 3600,     # 1 hour
        "clustering": 1800,     # 30 minutes
        "paths": 900,           # 15 minutes
        "metrics": 7200         # 2 hours
    }

    async def get_centrality_scores(
        self,
        organization_id: str,
        algorithm: str,
        params: dict
    ) -> Optional[List[dict]]:
        params_hash = self._hash_params(params)
        cache_key = self.CACHE_KEYS["centrality"].format(
            org_id=organization_id,
            algorithm=algorithm,
            params_hash=params_hash
        )

        cached_result = await self.redis.get(cache_key)
        if cached_result:
            # Update access statistics
            await self.redis.hincrby(
                f"analytics:graph:stats:{organization_id}",
                f"centrality:{algorithm}:hits",
                1
            )
            return json.loads(cached_result)

        return None
```

### 2.3 Dashboard Configuration Cache
```python
class DashboardConfigCache:
    """Cache for dashboard configurations and widget definitions"""

    CACHE_KEYS = {
        "config": "analytics:dashboard:config:{org_id}:{config_id}",
        "user_configs": "analytics:dashboard:user:{org_id}:{user_id}",
        "widgets": "analytics:widgets:definitions",
        "layout": "analytics:dashboard:layout:{org_id}:{config_id}"
    }

    CACHE_TTL = {
        "config": 1800,      # 30 minutes
        "user_configs": 600, # 10 minutes
        "widgets": 3600,     # 1 hour
        "layout": 1800       # 30 minutes
    }

    async def invalidate_user_dashboards(self, organization_id: str, user_id: str):
        """Invalidate all dashboard caches for a user"""
        patterns = [
            f"analytics:dashboard:user:{organization_id}:{user_id}",
            f"analytics:dashboard:config:{organization_id}:*"
        ]

        for pattern in patterns:
            keys = await self.redis.keys(pattern)
            if keys:
                await self.redis.delete(*keys)
```

### 2.4 Report Execution Cache
```python
class ReportExecutionCache:
    """Cache for report execution results and intermediate data"""

    CACHE_KEYS = {
        "execution": "analytics:report:execution:{execution_id}",
        "result": "analytics:report:result:{execution_id}",
        "intermediate": "analytics:report:intermediate:{report_id}:{stage}",
        "schedule": "analytics:report:schedule:{org_id}"
    }

    CACHE_TTL = {
        "execution": 86400,    # 24 hours
        "result": 604800,      # 7 days
        "intermediate": 3600,  # 1 hour
        "schedule": 300        # 5 minutes
    }

    async def cache_execution_result(
        self,
        execution_id: str,
        result_data: dict,
        ttl: int = None
    ):
        cache_key = self.CACHE_KEYS["result"].format(execution_id=execution_id)
        ttl = ttl or self.CACHE_TTL["result"]

        # Compress large results
        if len(json.dumps(result_data)) > 1024 * 1024:  # 1MB
            compressed_data = gzip.compress(json.dumps(result_data).encode())
            await self.redis.setex(cache_key, ttl, compressed_data)
            await self.redis.setex(f"{cache_key}:compressed", ttl, "true")
        else:
            await self.redis.setex(cache_key, ttl, json.dumps(result_data))
```

## 3. Cache Invalidation Strategies

### 3.1 Event-Driven Invalidation
```python
class CacheInvalidationService:
    """Manages cache invalidation based on data change events"""

    def __init__(self):
        self.redis = RedisConnection()
        self.event_bus = EventBus()

    async def setup_listeners(self):
        """Setup event listeners for cache invalidation"""

        # Entity changes invalidate real-time metrics
        self.event_bus.subscribe("entity.created", self.invalidate_entity_metrics)
        self.event_bus.subscribe("entity.updated", self.invalidate_entity_metrics)
        self.event_bus.subscribe("entity.deleted", self.invalidate_entity_metrics)

        # Relationship changes invalidate graph analytics
        self.event_bus.subscribe("relationship.created", self.invalidate_graph_analytics)
        self.event_bus.subscribe("relationship.updated", self.invalidate_graph_analytics)
        self.event_bus.subscribe("relationship.deleted", self.invalidate_graph_analytics)

        # Document changes invalidate document analytics
        self.event_bus.subscribe("document.processed", self.invalidate_document_analytics)
        self.event_bus.subscribe("document.deleted", self.invalidate_document_analytics)

        # Dashboard changes invalidate dashboard cache
        self.event_bus.subscribe("dashboard.updated", self.invalidate_dashboard_cache)
        self.event_bus.subscribe("dashboard.widget.updated", self.invalidate_dashboard_cache)

    async def invalidate_entity_metrics(self, event_data: dict):
        """Invalidate entity-related caches"""
        organization_id = event_data["organization_id"]

        patterns = [
            f"analytics:realtime:metrics:{organization_id}",
            f"analytics:realtime:kpi:{organization_id}:*",
            f"analytics:entity:trends:{organization_id}:*"
        ]

        await self._invalidate_patterns(patterns)

    async def invalidate_graph_analytics(self, event_data: dict):
        """Invalidate graph analytics caches"""
        organization_id = event_data["organization_id"]

        patterns = [
            f"analytics:graph:*:{organization_id}:*",
            f"analytics:realtime:trend:{organization_id}:*"
        ]

        await self._invalidate_patterns(patterns)

        # Also invalidate dashboard configs that use graph widgets
        await self._invalidate_graph_dashboards(organization_id)

    async def _invalidate_patterns(self, patterns: List[str]):
        """Invalidate cache keys matching patterns"""
        for pattern in patterns:
            keys = await self.redis.keys(pattern)
            if keys:
                await self.redis.delete(*keys)
                logger.info(f"Invalidated {len(keys)} cache keys for pattern: {pattern}")
```

### 3.2 Time-Based Invalidation
```python
class TimeBasedInvalidation:
    """Scheduled cache invalidation for predictable data updates"""

    async def setup_scheduled_tasks(self):
        """Setup scheduled cache cleanup and refresh tasks"""

        # Daily refresh of widget definitions
        scheduler.add_job(
            self.refresh_widget_definitions,
            trigger='cron',
            hour=2,
            minute=0
        )

        # Hourly refresh of real-time aggregations
        scheduler.add_job(
            self.refresh_realtime_aggregations,
            trigger='cron',
            minute=0
        )

        # Weekly cleanup of expired cache entries
        scheduler.add_job(
            self.cleanup_expired_cache,
            trigger='cron',
            day_of_week=0,
            hour=3,
            minute=0
        )
```

### 3.3 Manual Invalidation
```python
class ManualCacheInvalidation:
    """Manual cache invalidation for administrative operations"""

    async def invalidate_organization_cache(
        self,
        organization_id: str,
        cache_types: List[str] = None
    ):
        """Invalidate all cache for an organization or specific types"""

        if not cache_types:
            cache_types = ["realtime", "graph", "dashboard", "reports"]

        invalidation_map = {
            "realtime": [
                f"analytics:realtime:*:{organization_id}:*"
            ],
            "graph": [
                f"analytics:graph:*:{organization_id}:*"
            ],
            "dashboard": [
                f"analytics:dashboard:*:{organization_id}:*"
            ],
            "reports": [
                f"analytics:report:*:{organization_id}:*"
            ]
        }

        for cache_type in cache_types:
            patterns = invalidation_map.get(cache_type, [])
            await self._invalidate_patterns(patterns)
```

## 4. Cache Warming Strategies

### 4.1 Proactive Cache Warming
```python
class CacheWarmingService:
    """Proactive cache warming for frequently accessed data"""

    async def warm_popular_dashboards(self):
        """Warm cache for popular dashboards"""
        popular_configs = await self.db.query("""
            SELECT dc.id, dc.organization_id, COUNT(*) as access_count
            FROM dashboard_configurations dc
            JOIN user_dashboard_access uda ON dc.id = uda.dashboard_config_id
            WHERE uda.accessed_at >= NOW() - INTERVAL '7 days'
            GROUP BY dc.id, dc.organization_id
            ORDER BY access_count DESC
            LIMIT 50
        """)

        for config in popular_configs:
            await self._warm_dashboard_cache(config.id, config.organization_id)

    async def warm_realtime_metrics(self, organization_id: str):
        """Warm real-time metrics cache for an organization"""

        # Warm current metrics
        await self.compute_and_cache_metrics(organization_id)

        # Warm KPI trends for common time ranges
        time_ranges = ["1h", "24h", "7d", "30d"]
        for time_range in time_ranges:
            await self.compute_and_cache_kpi_trends(organization_id, time_range)

    async def warm_graph_analytics(self, organization_id: str):
        """Warm graph analytics cache"""

        # Warm centrality metrics for popular algorithms
        algorithms = ["degree", "betweenness", "pagerank"]
        for algorithm in algorithms:
            await self.compute_and_cache_centrality(organization_id, algorithm)

        # Warm clustering results
        await self.compute_and_cache_clustering(organization_id, "louvain")
```

### 4.2 Lazy Loading with Prefetch
```python
class LazyLoadingCache:
    """Lazy loading cache with intelligent prefetching"""

    async def get_with_prefetch(
        self,
        cache_key: str,
        data_fetcher: Callable,
        ttl: int,
        prefetch_keys: List[str] = None
    ):
        """Get data from cache with prefetching of related data"""

        # Try to get from cache
        cached_data = await self.redis.get(cache_key)
        if cached_data:
            data = json.loads(cached_data)

            # Prefetch related data asynchronously
            if prefetch_keys:
                asyncio.create_task(self._prefetch_related_data(prefetch_keys))

            return data

        # Cache miss - fetch and cache
        data = await data_fetcher()
        await self.redis.setex(cache_key, ttl, json.dumps(data))

        # Prefetch related data
        if prefetch_keys:
            await self._prefetch_related_data(prefetch_keys)

        return data

    async def _prefetch_related_data(self, prefetch_keys: List[str]):
        """Prefetch related cache keys"""
        for key in prefetch_keys:
            if not await self.redis.exists(key):
                # Trigger background computation for prefetch data
                await self.background_task_queue.enqueue(
                    "prefetch_cache_key",
                    cache_key=key
                )
```

## 5. Cache Monitoring and Analytics

### 5.1 Cache Performance Metrics
```python
class CacheMonitor:
    """Monitor cache performance and health"""

    async def collect_cache_metrics(self):
        """Collect comprehensive cache metrics"""

        metrics = {
            "redis": await self._get_redis_metrics(),
            "application": await self._get_application_cache_metrics(),
            "hit_rates": await self._calculate_hit_rates(),
            "memory_usage": await self._get_memory_usage(),
            "key_distribution": await self._get_key_distribution()
        }

        return metrics

    async def _get_redis_metrics(self):
        """Get Redis performance metrics"""
        info = await self.redis.info()

        return {
            "used_memory": info["used_memory"],
            "used_memory_human": info["used_memory_human"],
            "used_memory_peak": info["used_memory_peak"],
            "total_commands_processed": info["total_commands_processed"],
            "instantaneous_ops_per_sec": info["instantaneous_ops_per_sec"],
            "keyspace_hits": info["keyspace_hits"],
            "keyspace_misses": info["keyspace_misses"],
            "connected_clients": info["connected_clients"]
        }

    async def _calculate_hit_rates(self):
        """Calculate cache hit rates by category"""

        categories = ["realtime", "graph", "dashboard", "reports"]
        hit_rates = {}

        for category in categories:
            hits_key = f"analytics:stats:{category}:hits"
            misses_key = f"analytics:stats:{category}:misses"

            hits = int(await self.redis.get(hits_key) or 0)
            misses = int(await self.redis.get(misses_key) or 0)
            total = hits + misses

            hit_rates[category] = {
                "hit_rate": hits / total if total > 0 else 0,
                "total_requests": total,
                "hits": hits,
                "misses": misses
            }

        return hit_rates
```

### 5.2 Cache Health Checks
```python
class CacheHealthChecker:
    """Health checks for cache systems"""

    async def check_redis_health(self):
        """Check Redis cluster health"""
        try:
            # Test basic connectivity
            await self.redis.ping()

            # Test read/write operations
            test_key = "health_check_test"
            await self.redis.set(test_key, "test_value", ex=10)
            value = await self.redis.get(test_key)

            if value != "test_value":
                return {"status": "unhealthy", "reason": "read_write_failed"}

            # Check memory usage
            info = await self.redis.info("memory")
            memory_usage_percent = info["used_memory"] / info["maxmemory"] * 100

            if memory_usage_percent > 90:
                return {
                    "status": "warning",
                    "reason": "high_memory_usage",
                    "usage_percent": memory_usage_percent
                }

            return {"status": "healthy", "memory_usage_percent": memory_usage_percent}

        except Exception as e:
            return {"status": "unhealthy", "reason": str(e)}

    async def check_cache_consistency(self):
        """Check cache consistency across multiple cache stores"""

        # Sample random keys and compare across cache stores
        sample_keys = await self._get_sample_cache_keys(100)
        inconsistencies = []

        for key in sample_keys:
            redis_value = await self.redis.get(key)
            app_cache_value = await self.app_cache.get(key)

            if redis_value != app_cache_value:
                inconsistencies.append({
                    "key": key,
                    "redis_value": redis_value[:100],  # Truncate for logging
                    "app_cache_value": app_cache_value[:100] if app_cache_value else None
                })

        consistency_rate = (len(sample_keys) - len(inconsistencies)) / len(sample_keys)

        return {
            "consistency_rate": consistency_rate,
            "inconsistencies_count": len(inconsistencies),
            "sample_size": len(sample_keys),
            "inconsistencies": inconsistencies[:10]  # Return first 10 for analysis
        }
```

## 6. Cache Configuration and Optimization

### 6.1 Redis Configuration for Analytics
```yaml
# redis-analytics.conf
# Optimized for analytics workloads

# Memory management
maxmemory 8gb
maxmemory-policy allkeys-lru

# Persistence
save 900 1
save 300 10
save 60 10000

# Analytics-specific optimizations
hash-max-ziplist-entries 512
hash-max-ziplist-value 64
list-max-ziplist-size -2
set-max-intset-entries 512

# Network optimization
tcp-keepalive 300
timeout 0

# Slow log
slowlog-log-slower-than 10000
slowlog-max-len 128

# Client limits
maxclients 10000

# Memory optimization for large values
proto-max-bulk-len 512mb
```

### 6.2 Cache Configuration by Data Type
```python
CACHE_CONFIG = {
    "realtime_metrics": {
        "ttl": 30,
        "max_size": 1000,
        "compression": False,
        "serialization": "json"
    },
    "graph_analytics": {
        "ttl": 3600,
        "max_size": 100,
        "compression": True,
        "serialization": "json"
    },
    "dashboard_configs": {
        "ttl": 1800,
        "max_size": 5000,
        "compression": False,
        "serialization": "json"
    },
    "report_results": {
        "ttl": 604800,
        "max_size": 200,
        "compression": True,
        "serialization": "json"
    },
    "user_sessions": {
        "ttl": 1800,
        "max_size": 10000,
        "compression": False,
        "serialization": "json"
    }
}
```

## 7. Implementation Checklist

### Cache Implementation Tasks

- [ ] Redis cluster setup with high availability
- [ ] Multi-level cache implementation (client, edge, application, database)
- [ ] Cache key naming conventions and versioning
- [ ] Cache invalidation service with event-driven updates
- [ ] Cache warming service for popular data
- [ ] Cache monitoring and metrics collection
- [ ] Cache health checks and alerting
- [ ] Cache compression for large datasets
- [ ] Cache performance optimization
- [ ] Cache backup and disaster recovery
- [ ] Cache security (encryption, access control)
- [ ] Cache capacity planning and scaling

### Monitoring and Alerting

- [ ] Cache hit/miss ratio monitoring
- [ ] Memory usage alerts
- [ ] Cache response time monitoring
- [ ] Cache error rate monitoring
- [ ] Invalidated cache tracking
- [ ] Cache consistency checks
- [ ] Cache warming effectiveness metrics

This comprehensive caching architecture ensures optimal performance for the Knowledge Graph Analytics Dashboard while maintaining data consistency and providing real-time capabilities.
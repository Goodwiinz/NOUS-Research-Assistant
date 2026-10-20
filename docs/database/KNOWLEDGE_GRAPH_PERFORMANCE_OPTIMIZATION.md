# Knowledge Graph Performance Optimization Guide

> **Stale / historical (2026-10-17):** Qdrant is fully removed and live retrieval is PostgreSQL full-text search (`DO_KB_ENABLED` defaults to `False`; pgvector semantic search is flag-gated). Treat Qdrant, DO KB and vector-store statements below as historical. Current contracts: [`docs/engineering/`](../engineering/README.md).

## Overview

This document provides comprehensive performance optimization strategies for the corrected Knowledge Graph architecture, ensuring efficient backend-first processing that can handle enterprise-scale graphs with hundreds to thousands of nodes and relationships.

## Performance Targets

### Response Time Targets
- **Small Graphs** (< 100 nodes): < 500ms for centrality computation
- **Medium Graphs** (100-500 nodes): < 2s for centrality computation
- **Large Graphs** (500-1000 nodes): < 10s for centrality computation
- **Real-time Updates**: < 100ms for cache invalidation
- **Visualization Layout**: < 2s for force-directed layout
- **API Responses**: < 200ms for cached data, < 5s for computed data

### Throughput Targets
- **Concurrent Users**: 100+ simultaneous users
- **API Requests**: 1000+ requests/minute
- **Graph Computations**: 10+ concurrent centrality computations
- **Cache Hit Rate**: > 85% for frequently accessed data
- **Database Connections**: Efficient connection pooling

## 1. Neo4j Performance Optimization

### Query Optimization

#### Use Native Graph Algorithms
```cypher
// GOOD: Use Neo4j Graph Data Science library
CALL gds.pageRank.stream({
    nodeProjection: 'Entity',
    relationshipProjection: {
        RELATED_TO: {
            type: 'RELATED_TO',
            orientation: 'UNDIRECTED',
            properties: 'strength'
        }
    }
}) YIELD nodeId, score
RETURN gds.util.asNode(nodeId).name AS entityName, score
ORDER BY score DESC LIMIT 100;

// BAD: Manual centrality calculation in Cypher
MATCH (e:Entity)
OPTIONAL MATCH (e)-[r:RELATED_TO]-(other:Entity)
WITH e, count(r) AS degree
RETURN e.name, degree
ORDER BY degree DESC;
```

#### Optimize Query Patterns
```cypher
-- Use index hints for predictable performance
MATCH (e:Entity)
USING INDEX e:Entity(entity_type, organization_id)
WHERE e.entity_type = 'PERSON'
  AND e.organization_id = $orgId
  AND e.confidence > 0.8
RETURN e
LIMIT 100;

-- Use PERIODIC COMMIT for large operations
USING PERIODIC COMMIT 1000
LOAD CSV WITH HEADERS FROM 'file:///entities.csv' AS row
CREATE (e:Entity {
    id: row.id,
    name: row.name,
    entity_type: row.type,
    organization_id: row.org_id
});
```

#### Efficient Graph Traversals
```cypher
-- Limit traversal depth and use early termination
MATCH (start:Entity {id: $entityId})
CALL apoc.path.expandConfig(start, {
    relationshipFilter: 'RELATED_TO>',
    minLevel: 1,
    maxLevel: 3,
    limit: 100,
    filterStartNode: true,
    uniqueness: 'RELATIONSHIP_GLOBAL'
}) YIELD path
RETURN path
LIMIT 50;

-- Use relationship weight filtering
MATCH (e1:Entity)-[r:RELATED_TO]->(e2:Entity)
WHERE r.strength >= 0.5
  AND r.confidence >= 0.7
  AND e1.organization_id = $orgId
RETURN e1, r, e2;
```

### Index Strategy

#### Composite Indexes for Common Queries
```cypher
-- Multi-property indexes for filtered queries
CREATE INDEX entity_type_org_confidence IF NOT EXISTS
FOR (e:Entity) ON (e.entity_type, e.organization_id, e.confidence);

CREATE INDEX relationship_org_type_strength IF NOT EXISTS
FOR ()-[r:RELATED_TO]-() ON (r.organization_id, r.type, r.strength);

-- Full-text search for entity discovery
CREATE FULLTEXT INDEX entity_search_index IF NOT EXISTS
FOR (e:Entity) ON EACH [e.name, e.description, e.aliases];

-- Range indexes for temporal queries
CREATE INDEX entity_created_at_index IF NOT EXISTS
FOR (e:Entity) ON (e.created_at);

CREATE INDEX relationship_created_at_index IF NOT EXISTS
FOR ()-[r:RELATED_TO]-() ON (r.created_at);
```

#### Index Usage Monitoring
```cypher
-- Monitor index usage
CALL db.indexes() YIELD indexName, state, populationPercent, uniqueness, type
RETURN indexName, state, populationPercent, uniqueness, type;

-- Query plan analysis
EXPLAIN MATCH (e:Entity)
WHERE e.entity_type = 'PERSON' AND e.organization_id = $orgId
RETURN e;

PROFILE MATCH (e:Entity)
WHERE e.entity_type = 'PERSON' AND e.organization_id = $orgId
RETURN e;
```

### Memory Management

#### Configure Neo4j Memory Settings
```properties
# conf/neo4j.conf
server.memory.heap.initial_size=8G
server.memory.heap.max_size=8G
server.memory.pagecache.size=4G

# Graph Data Science library
gds.memory.max_heap_size=8G
gds.memory.pagecache.size=2G

# Transaction management
db.transaction.timeout=60s
db.transaction.concurrent.maximum=1000

# Connection pooling
server.bolt.thread_pool_min_size=5
server.bolt.thread_pool_max_size=400
```

#### Graph Projection Optimization
```cypher
-- Use in-memory graph projections for analytics
CALL gds.graph.project(
    'orgGraph',
    {
        Entity: {
            label: 'Entity',
            properties: ['confidence', 'entity_type']
        }
    },
    {
        RELATED_TO: {
            type: 'RELATED_TO',
            orientation: 'UNDIRECTED',
            properties: ['strength', 'confidence']
        }
    },
    {
        nodeProperties: ['confidence'],
        relationshipProperties: ['strength']
    }
);

-- Use graph catalog for persistent projections
CALL gds.graph.project.estimate('orgGraph', {
    Entity: {
        label: 'Entity',
        estimatedNodeCount: 10000
    }
}, {
    RELATED_TO: {
        type: 'RELATED_TO',
        estimatedRelationshipCount: 50000
    }
});
```

### Scaling Strategies

#### Read Replicas for Analytics
```bash
# Configure read replicas for analytics queries
# Primary database for writes
# Replicas for centrality computations and analytics
```

#### Connection Pooling
```python
# Python example with Bolt driver
from neo4j import GraphDatabase

class Neo4jPool:
    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(
            uri,
            auth=(user, password),
            max_connection_lifetime=3600,
            max_connection_pool_size=100,
            connection_acquisition_timeout=60
        )

    def get_session(self, database="neo4j"):
        return self.driver.session(database=database)
```

## 2. PostgreSQL Performance Optimization

### Partitioning Strategy

#### Time-based Partitioning for Analytics
```sql
-- Partition graph_analytics_snapshots by time
CREATE TABLE graph_analytics_snapshots (
    id UUID,
    organization_id UUID NOT NULL,
    snapshot_timestamp TIMESTAMPTZ DEFAULT NOW(),
    -- ... other columns
) PARTITION BY RANGE (snapshot_timestamp);

-- Create monthly partitions
CREATE TABLE graph_analytics_snapshots_2024_01
PARTITION OF graph_analytics_snapshots
FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');

CREATE TABLE graph_analytics_snapshots_2024_02
PARTITION OF graph_analytics_snapshots
FOR VALUES FROM ('2024-02-01') TO ('2024-03-01');
```

#### Organization-based Partitioning
```sql
-- Partition entity_analytics_cache by organization
CREATE TABLE entity_analytics_cache (
    id UUID,
    entity_id UUID NOT NULL,
    organization_id UUID NOT NULL,
    -- ... other columns
) PARTITION BY HASH (organization_id);

-- Create partitions
CREATE TABLE entity_analytics_cache_part_0
PARTITION OF entity_analytics_cache
FOR VALUES WITH (modulus 4, remainder 0);

CREATE TABLE entity_analytics_cache_part_1
PARTITION OF entity_analytics_cache
FOR VALUES WITH (modulus 4, remainder 1);
```

### Materialized Views for Analytics

#### Graph Statistics Aggregation
```sql
CREATE MATERIALIZED VIEW graph_organization_stats AS
SELECT
    o.id as organization_id,
    o.name as organization_name,

    -- Entity statistics
    COUNT(DISTINCT e.id) as total_entities,
    COUNT(DISTINCT CASE WHEN e.confidence > 0.8 THEN e.id END) as high_quality_entities,
    AVG(e.confidence) as avg_entity_confidence,

    -- Relationship statistics
    COUNT(DISTINCT er.id) as total_relationships,
    AVG(er.relationship_strength) as avg_relationship_strength,

    -- Computation statistics
    COUNT(DISTINCT gc.id) as total_computations,
    AVG(gc.computation_time_ms) as avg_computation_time_ms,

    -- Timestamp
    NOW() as computed_at

FROM organizations o
LEFT JOIN entities e ON o.id = e.organization_id AND e.is_deleted = false
LEFT JOIN entity_relationships er ON o.id = er.organization_id AND er.is_deleted = false
LEFT JOIN graph_computation_cache gc ON o.id = gc.organization_id
WHERE o.is_active = true
GROUP BY o.id, o.name;

-- Create unique index for refresh
CREATE UNIQUE INDEX idx_graph_org_stats_unique
ON graph_organization_stats (organization_id);

-- Refresh strategy
CREATE OR REPLACE FUNCTION refresh_graph_organization_stats()
RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY graph_organization_stats;
END;
$$ LANGUAGE plpgsql;

-- Schedule refresh every 15 minutes
```

#### Performance Metrics Aggregation
```sql
CREATE MATERIALIZED VIEW graph_performance_hourly AS
SELECT
    date_trunc('hour', timestamp) as hour_timestamp,
    organization_id,
    operation_type,
    algorithm_name,

    -- Performance metrics
    AVG(computation_time_ms) as avg_computation_time_ms,
    MIN(computation_time_ms) as min_computation_time_ms,
    MAX(computation_time_ms) as max_computation_time_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY computation_time_ms) as p95_computation_time_ms,

    -- Scale metrics
    AVG(entity_count) as avg_entity_count,
    MAX(entity_count) as max_entity_count,

    -- Quality metrics
    AVG(result_quality_score) as avg_quality_score,

    -- Sample count
    COUNT(*) as sample_count

FROM graph_performance_metrics
WHERE timestamp >= NOW() - INTERVAL '7 days'
GROUP BY date_trunc('hour', timestamp), organization_id, operation_type, algorithm_name;

CREATE INDEX idx_graph_perf_hourly_time_org
ON graph_performance_hourly (hour_timestamp DESC, organization_id);
```

### Query Optimization

#### Efficient Pagination
```sql
-- Use cursor-based pagination for large result sets
-- First page
SELECT e.id, e.name, e.entity_type, eac.pagerank_score
FROM entities e
JOIN entity_analytics_cache eac ON e.id = eac.entity_id
WHERE e.organization_id = $org_id
  AND e.entity_type = ANY($entity_types)
ORDER BY eac.pagerank_score DESC, e.id
LIMIT 50;

-- Next page (using last seen ID)
SELECT e.id, e.name, e.entity_type, eac.pagerank_score
FROM entities e
JOIN entity_analytics_cache eac ON e.id = eac.entity_id
WHERE e.organization_id = $org_id
  AND e.entity_type = ANY($entity_types)
  AND (eac.pagerank_score, e.id) < ($last_score, $last_id)
ORDER BY eac.pagerank_score DESC, e.id
LIMIT 50;
```

#### Optimized Joins with Indexes
```sql
-- Ensure proper index usage for joins
EXPLAIN (ANALYZE, BUFFERS)
SELECT e.name, er.relationship_type, e2.name as target_name
FROM entities e
JOIN entity_relationships er ON e.id = er.source_entity_id
JOIN entities e2 ON er.target_entity_id = e2.id
WHERE e.organization_id = $org_id
  AND er.strength > 0.5
ORDER BY er.strength DESC
LIMIT 100;

-- Create supporting indexes
CREATE INDEX idx_entity_relationships_source_strength
ON entity_relationships (source_entity_id, strength DESC)
WHERE organization_id IS NOT NULL;
```

### Connection Pooling and Load Balancing

#### PgBouncer Configuration
```ini
# pgbouncer.ini
[databases]
rag_graph_db = host=localhost port=5432 dbname=rag

[pgbouncer]
listen_port = 6432
listen_addr = 127.0.0.1
auth_type = md5
auth_file = /etc/pgbouncer/userlist.txt
logfile = /var/log/pgbouncer/pgbouncer.log
pidfile = /var/run/pgbouncer/pgbouncer.pid
admin_users = postgres
stats_users = stats, postgres

# Pool settings
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 20
min_pool_size = 5
reserve_pool_size = 5
reserve_pool_timeout = 5
max_db_connections = 100
max_user_connections = 100

# Timeouts
server_reset_query = DISCARD ALL
server_check_delay = 30
server_check_query = select 1
server_lifetime = 3600
server_idle_timeout = 600
```

#### Application Connection Management
```python
# SQLAlchemy connection pool configuration
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

engine = create_engine(
    "postgresql://user:pass@localhost:6432/rag",
    poolclass=QueuePool,
    pool_size=20,
    max_overflow=30,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_timeout=30,
    echo=False
)
```

## 3. Qdrant Vector Database Optimization

### Collection Configuration

#### Optimized Index Settings
```json
{
  "collection_name": "entity_embeddings_optimized",
  "vectors": {
    "size": 1536,
    "distance": "Cosine"
  },
  "hnsw_config": {
    "m": 64,
    "ef_construct": 256,
    "full_scan_threshold": 20000,
    "max_indexing_threads": 8,
    "on_disk": false
  },
  "quantization_config": {
    "scalar": {
      "type": "int8",
      "quantile": 0.99,
      "always_ram": false
    }
  },
  "optimizer_config": {
    "deleted_threshold": 0.3,
    "vacuum_min_vector_number": 1000,
    "default_segment_number": 4,
    "max_segment_size": 400000,
    "memmap_threshold": 100000,
    "indexing_threshold": 50000,
    "flush_interval_sec": 10,
    "max_optimization_threads": 4
  }
}
```

#### Payload Indexing Strategy
```python
# Create efficient payload indexes for filtering
from qdrant_client import QdrantClient

client = QdrantClient(url="http://localhost:6333")

# Create collection with optimized payload indexes
client.create_collection(
    collection_name="entity_embeddings",
    vectors_config={
        "size": 1536,
        "distance": "Cosine"
    },
    hnsw_config={
        "m": 64,
        "ef_construct": 256
    },
    quantization_config={
        "scalar": {
            "type": "int8",
            "quantile": 0.99
        }
    },
    optimizer_config={
        "default_segment_number": 4,
        "max_segment_size": 400000
    }
)

# Create payload indexes for filtering
client.create_payload_index(
    collection_name="entity_embeddings",
    field_name="organization_id",
    field_schema="keyword"
)

client.create_payload_index(
    collection_name="entity_embeddings",
    field_name="entity_type",
    field_schema="keyword"
)

client.create_payload_index(
    collection_name="entity_embeddings",
    field_name="confidence",
    field_schema="float"
)
```

### Batch Operations

#### Efficient Batch Insertion
```python
def batch_insert_entities(entities, batch_size=1000):
    """Efficiently insert large numbers of entity embeddings"""
    client = QdrantClient(url="http://localhost:6333")

    for i in range(0, len(entities), batch_size):
        batch = entities[i:i + batch_size]

        points = []
        for entity in batch:
            points.append(
                PointStruct(
                    id=entity["id"],
                    vector=entity["embedding"],
                    payload={
                        "entity_id": entity["entity_id"],
                        "entity_name": entity["name"],
                        "entity_type": entity["type"],
                        "organization_id": entity["org_id"],
                        "confidence": entity["confidence"],
                        "created_at": entity["created_at"]
                    }
                )
            )

        # Upsert batch
        client.upsert(
            collection_name="entity_embeddings",
            points=points,
            wait=False  # Don't wait for indexing
        )

        print(f"Inserted batch {i//batch_size + 1}/{(len(entities)-1)//batch_size + 1}")
```

#### Optimized Search with Filtering
```python
def search_similar_entities(query_vector, organization_id, entity_types=None, limit=10):
    """Optimized similarity search with pre-filtering"""
    client = QdrantClient(url="http://localhost:6333")

    # Build filter
    filter_condition = Filter(
        must=[
            FieldCondition(
                key="organization_id",
                match=MatchValue(value=organization_id)
            )
        ]
    )

    if entity_types:
        filter_condition.must.append(
            FieldCondition(
                key="entity_type",
                match=MatchAny(any=entity_types)
            )
        )

    # Perform search with filter
    results = client.search(
        collection_name="entity_embeddings",
        query_vector=query_vector,
        query_filter=filter_condition,
        limit=limit,
        with_payload=True,
        with_vectors=False,  # Don't return vectors unless needed
        score_threshold=0.7
    )

    return results
```

### Memory Management

#### Quantization for Memory Efficiency
```python
# Apply scalar quantization to reduce memory usage
client.update_collection(
    collection_name="entity_embeddings",
    optimizer_config={
        "default_segment_number": 4,
        "max_segment_size": 400000
    },
    quantization_config={
        "scalar": {
            "type": "int8",
            "quantile": 0.99,
            "always_ram": False  # Keep quantized data on disk
        }
    }
)

# Monitor memory usage
collection_info = client.get_collection("entity_embeddings")
print(f"Vectors count: {collection_info.vectors_count}")
print(f"Indexed vectors count: {collection_info.indexed_vectors_count}")
print(f"Points count: {collection_info.points_count}")
```

#### Shard Management for Scale
```python
# Create sharded collection for large datasets
client.create_collection(
    collection_name="large_entity_embeddings",
    vectors_config={"size": 1536, "distance": "Cosine"},
    shard_number=4,  # Split into 4 shards
    replication_factor=2  # Replicate each shard
)

# Move data between shards if needed
client.update_collection_clusters(
    collection_name="large_entity_embeddings",
    shard_transfers=[
        ShardTransfer(
            shard_id=0,
            to_shard_id=1,
            points=[1, 2, 3, 4, 5]  # Point IDs to move
        )
    ]
)
```

## 4. Redis Caching Optimization

### Memory Management

#### Eviction Policies and Memory Limits
```redis
# redis.conf optimized for graph caching
maxmemory 4gb
maxmemory-policy allkeys-lru

# Set appropriate TTL values for different cache types
# Entity centrality: 1 hour
# Graph layouts: 2 hours
# Shortest paths: 30 minutes
# Real-time stats: 5 minutes
```

#### Memory-Optimized Data Structures
```python
# Use Redis pipelines for batch operations
import redis

def cache_centrality_scores_batch(entity_scores, ttl=3600):
    """Cache multiple centrality scores efficiently"""
    r = redis.Redis(host='localhost', port=6379, db=0)

    pipe = r.pipeline()

    for entity_id, scores in entity_scores.items():
        for metric_type, score in scores.items():
            key = f"entity:centrality:{entity_id}:{metric_type}"
            pipe.setex(key, ttl, score)

    pipe.execute()

# Use compressed serialization for large objects
import json
import gzip
import base64

def cache_graph_layout(layout_data, graph_id, algorithm, ttl=7200):
    """Cache compressed graph layout data using JSON for security"""
    r = redis.Redis(host='localhost', port=6379, db=0)

    # Serialize to JSON and compress
    serialized = json.dumps(layout_data).encode('utf-8')
    compressed = gzip.compress(serialized)
    encoded = base64.b64encode(compressed).decode('utf-8')

    key = f"graph:layout:{graph_id}:{algorithm}"
    r.setex(key, ttl, encoded)

def get_cached_graph_layout(graph_id, algorithm):
    """Retrieve and decompress cached layout using JSON for security"""
    r = redis.Redis(host='localhost', port=6379, db=0)

    key = f"graph:layout:{graph_id}:{algorithm}"
    encoded = r.get(key)

    if encoded:
        compressed = base64.b64decode(encoded)
        serialized = gzip.decompress(compressed)
        return json.loads(serialized.decode('utf-8'))

    return None
```

### Cache Warming Strategies

#### Proactive Cache Population
```python
def warm_graph_cache(organization_id):
    """Proactively warm cache with frequently accessed data"""
    r = redis.Redis(host='localhost', port=6379, db=0)

    # Get top entities by importance
    top_entities = get_top_entities_by_importance(organization_id, limit=100)

    # Pre-compute and cache centrality metrics
    for entity in top_entities:
        cache_key = f"entity:centrality:{entity.id}:pagerank"
        if not r.exists(cache_key):
            # Trigger background computation
            trigger_centrality_computation(entity.id, 'pagerank', organization_id)

    # Pre-compute layout for important subgraphs
    central_entities = get_central_entities(organization_id, limit=20)
    for entity in central_entities:
        layout_key = f"graph:layout:{entity.id}:neighborhood:2"
        if not r.exists(layout_key):
            trigger_layout_computation(entity.id, depth=2, organization_id)

def schedule_cache_warming():
    """Schedule periodic cache warming"""
    import schedule
    import time

    # Warm cache every 15 minutes
    schedule.every(15).minutes.do(lambda: warm_graph_cache(get_active_organizations()))

    # Warm cache for new organizations
    schedule.every(5).minutes.do(warm_new_organization_cache)

    while True:
        schedule.run_pending()
        time.sleep(1)
```

### Real-time Cache Updates

#### Pub/Sub for Cache Invalidation
```python
import redis
import json

class GraphCacheManager:
    def __init__(self):
        self.redis_client = redis.Redis(host='localhost', port=6379, db=0)
        self.pubsub = self.redis_client.pubsub()

        # Subscribe to graph update channels
        self.pubsub.subscribe([
            'channel:graph:updates:*',
            'channel:entity:updated:*',
            'channel:relationship:updated:*'
        ])

    def listen_for_updates(self):
        """Listen for graph updates and invalidate cache"""
        for message in self.pubsub.listen():
            if message['type'] == 'message':
                data = json.loads(message['data'])

                if data['type'] == 'entity_updated':
                    self.invalidate_entity_cache(data['entity_id'], data['organization_id'])
                elif data['type'] == 'relationship_updated':
                    self.invalidate_relationship_cache(data['relationship_id'], data['organization_id'])
                elif data['type'] == 'graph_updated':
                    self.invalidate_organization_cache(data['organization_id'])

    def invalidate_entity_cache(self, entity_id, organization_id):
        """Invalidate all cache entries related to an entity"""
        pattern = f"entity:*:{entity_id}:*"
        keys = self.redis_client.keys(pattern)

        if keys:
            self.redis_client.delete(*keys)

        # Invalidate related caches
        patterns = [
            f"neighborhood:{entity_id}:*",
            f"similarity:entity:{entity_id}:*",
            f"paths:*:{entity_id}",
            f"paths:{entity_id}:*"
        ]

        for pattern in patterns:
            keys = self.redis_client.keys(pattern)
            if keys:
                self.redis_client.delete(*keys)
```

## 5. Application-Level Optimization

### Asynchronous Processing

#### Background Job Queue
```python
# Using Celery for graph computation tasks
from celery import Celery
from celery.result import AsyncResult

app = Celery('graph_tasks')

@app.task(bind=True)
def compute_centrality_task(self, organization_id, algorithm, parameters):
    """Background task for centrality computation"""
    try:
        # Update progress
        self.update_state(state='PROGRESS', meta={'current': 0, 'total': 100})

        # Computation logic
        result = compute_centrality_metrics(organization_id, algorithm, parameters)

        # Cache results
        cache_computation_results(organization_id, algorithm, result)

        return {'status': 'SUCCESS', 'result': result}

    except Exception as exc:
        self.update_state(state='FAILURE', meta={'error': str(exc)})
        raise

# API endpoint that queues computation
@app.post('/api/v1/knowledge-graph/analytics/centrality')
async def queue_centrality_computation(request: CentralityRequest):
    job = compute_centrality_task.delay(
        organization_id=request.organization_id,
        algorithm=request.algorithm_type.value,
        parameters=request.parameters
    )

    return {
        "job_id": job.id,
        "status": "queued",
        "estimated_time": estimate_computation_time(request)
    }
```

#### Result Streaming
```python
from fastapi.responses import StreamingResponse

@app.get('/api/v1/knowledge-graph/computation/{job_id}/stream')
async def stream_computation_progress(job_id: str):
    """Stream computation progress in real-time"""

    def generate_progress():
        result = AsyncResult(job_id)

        while not result.ready():
            if result.status == 'FAILURE':
                yield f"data: {json.dumps({'status': 'error', 'error': str(result.info)})}\n\n"
                break

            yield f"data: {json.dumps({'status': 'progress', 'progress': result.info.get('current', 0)})}\n\n"
            time.sleep(1)

        if result.successful():
            yield f"data: {json.dumps({'status': 'completed', 'result': result.result})}\n\n"

    return StreamingResponse(generate_progress(), media_type="text/plain")
```

### Efficient Data Transfer

#### Pagination and Field Selection
```python
from pydantic import BaseModel
from typing import List, Optional

class GraphEntityResponse(BaseModel):
    id: str
    name: str
    entity_type: str
    confidence: float
    # Only include analytics if requested
    centrality_metrics: Optional[Dict[str, float]] = None

@app.get('/api/v1/knowledge-graph/entities')
async def get_entities(
    organization_id: str,
    page: int = 1,
    page_size: int = 50,
    fields: Optional[str] = None,
    include_analytics: bool = False
):
    """Get paginated entities with optional field selection"""

    # Parse requested fields
    requested_fields = fields.split(',') if fields else None

    # Build query with field selection
    entities = await fetch_entities_paginated(
        organization_id=organization_id,
        page=page,
        page_size=page_size,
        fields=requested_fields,
        include_analytics=include_analytics
    )

    return {
        "entities": entities,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": await count_entities(organization_id)
        }
    }
```

#### Response Compression
```python
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

app = FastAPI()

# Add gzip compression for API responses
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Use efficient serialization formats
from fastapi import Response

@app.get('/api/v1/knowledge-graph/graph-data/{graph_id}')
async def get_graph_data_optimized(graph_id: str):
    """Return graph data in optimized format"""

    graph_data = get_graph_data(graph_id)

    # Use efficient JSON serialization
    response_data = json.dumps(graph_data, separators=(',', ':'))

    return Response(
        content=response_data,
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )
```

## 6. Monitoring and Performance Tuning

### Key Performance Indicators

#### Database Performance Metrics
```python
# Monitor Neo4j performance
def monitor_neo4j_performance():
    """Monitor Neo4j database performance"""

    metrics = {
        'query_time': [],
        'memory_usage': [],
        'connection_count': [],
        'cache_hit_rate': []
    }

    # Query performance
    start_time = time.time()
    result = execute_test_query()
    metrics['query_time'].append(time.time() - start_time)

    # Memory usage
    memory_info = neo4j_driver.execute_query(
        "CALL dbms.queryJmx('java.lang:type=Memory') YIELD attributes RETURN attributes"
    )
    metrics['memory_usage'].append(memory_info)

    return metrics

# Monitor PostgreSQL performance
def monitor_postgresql_performance():
    """Monitor PostgreSQL database performance"""

    with get_db_connection() as conn:
        # Slow query analysis
        slow_queries = conn.execute("""
            SELECT query, calls, total_time, mean_time, rows
            FROM pg_stat_statements
            WHERE mean_time > 100
            ORDER BY mean_time DESC
            LIMIT 10
        """).fetchall()

        # Index usage
        index_usage = conn.execute("""
            SELECT schemaname, tablename, indexname, idx_scan, idx_tup_read, idx_tup_fetch
            FROM pg_stat_user_indexes
            ORDER BY idx_scan DESC
            LIMIT 10
        """).fetchall()

        return {
            'slow_queries': slow_queries,
            'index_usage': index_usage
        }
```

#### Automated Performance Alerts
```python
from prometheus_client import Counter, Histogram, Gauge

# Define metrics
GRAPH_QUERY_DURATION = Histogram(
    'graph_query_duration_seconds',
    'Time spent executing graph queries',
    ['query_type', 'algorithm']
)

CACHE_HIT_RATE = Gauge(
    'cache_hit_rate',
    'Cache hit rate percentage',
    ['cache_type']
)

ACTIVE_GRAPH_COMPUTATIONS = Gauge(
    'active_graph_computations',
    'Number of active graph computations'
)

# Use metrics in code
@GRAPH_QUERY_DURATION.labels(query_type='centrality', algorithm='pagerank').time()
def compute_pagerank(organization_id):
    # Computation logic
    pass

def update_cache_metrics():
    """Update cache performance metrics"""
    cache_stats = get_redis_cache_stats()

    CACHE_HIT_RATE.labels(cache_type='entity_centrality').set(
        cache_stats['entity_centrality_hit_rate']
    )
    CACHE_HIT_RATE.labels(cache_type='graph_layout').set(
        cache_stats['graph_layout_hit_rate']
    )
```

### Performance Testing

#### Load Testing with Locust
```python
from locust import HttpUser, task, between

class KnowledgeGraphUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        """Initialize user session"""
        response = self.client.post("/api/v1/auth/login", json={
            "email": "test@example.com",
            "password": "password"
        })
        self.token = response.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    @task(3)
    def get_entity_centrality(self):
        """Test centrality computation endpoint"""
        self.client.get(
            "/api/v1/knowledge-graph/analytics/centrality/top",
            headers=self.headers,
            params={
                "metric": "pagerank",
                "limit": 50
            }
        )

    @task(2)
    def get_graph_visualization(self):
        """Test graph visualization endpoint"""
        self.client.get(
            "/api/v1/knowledge-graph/visualization/neighborhood/sample-entity-id",
            headers=self.headers,
            params={
                "depth": 2,
                "max_nodes": 100,
                "layout_algorithm": "force"
            }
        )

    @task(1)
    def compute_graph_insights(self):
        """Test insights computation"""
        self.client.post(
            "/api/v1/knowledge-graph/insights/compute",
            headers=self.headers,
            json={
                "categories": ["quality", "connectivity"],
                "organization_id": "sample-org-id"
            }
        )

# Run with: locust -f performance_test.py --host=http://localhost:8000
```

#### Benchmark Suite
```python
import asyncio
import time
from dataclasses import dataclass
from typing import List, Dict

@dataclass
class BenchmarkResult:
    operation: str
    entity_count: int
    relationship_count: int
    duration_ms: int
    memory_mb: int
    success: bool

class KnowledgeGraphBenchmark:
    def __init__(self):
        self.results: List[BenchmarkResult] = []

    async def benchmark_centrality_computation(self, entity_counts: List[int]):
        """Benchmark centrality computation at different scales"""

        for count in entity_counts:
            print(f"Testing centrality computation with {count} entities...")

            # Generate test data
            test_data = generate_test_graph(count, count * 3)

            # Run benchmark
            start_time = time.time()
            start_memory = get_memory_usage()

            try:
                result = await compute_centrality_batch(test_data, algorithm='pagerank')

                duration_ms = int((time.time() - start_time) * 1000)
                memory_used = get_memory_usage() - start_memory

                self.results.append(BenchmarkResult(
                    operation="pagerank_centrality",
                    entity_count=count,
                    relationship_count=count * 3,
                    duration_ms=duration_ms,
                    memory_mb=memory_used,
                    success=True
                ))

                print(f"  ✓ Completed in {duration_ms}ms, {memory_used}MB memory")

            except Exception as e:
                print(f"  ✗ Failed: {e}")
                self.results.append(BenchmarkResult(
                    operation="pagerank_centrality",
                    entity_count=count,
                    relationship_count=count * 3,
                    duration_ms=0,
                    memory_mb=0,
                    success=False
                ))

    def generate_report(self):
        """Generate performance benchmark report"""

        successful_results = [r for r in self.results if r.success]

        if not successful_results:
            return "No successful benchmarks"

        report = "# Knowledge Graph Performance Benchmark Report\n\n"

        # Performance summary
        report += "## Performance Summary\n\n"
        report += "| Entity Count | Avg Duration (ms) | Memory (MB) | Success Rate |\n"
        report += "|-------------|-------------------|-------------|-------------|\n"

        for count in sorted(set(r.entity_count for r in successful_results)):
            count_results = [r for r in successful_results if r.entity_count == count]
            avg_duration = sum(r.duration_ms for r in count_results) / len(count_results)
            avg_memory = sum(r.memory_mb for r in count_results) / len(count_results)
            success_rate = len(count_results) / len([r for r in self.results if r.entity_count == count]) * 100

            report += f"| {count:,} | {avg_duration:.1f} | {avg_memory:.1f} | {success_rate:.1f}% |\n"

        return report

# Run benchmarks
async def run_performance_benchmarks():
    benchmark = KnowledgeGraphBenchmark()

    # Test different scales
    await benchmark.benchmark_centrality_computation([100, 500, 1000, 2000])
    await benchmark.benchmark_layout_computation([50, 100, 200, 500])
    await benchmark.benchmark_pathfinding([100, 500, 1000])

    # Generate report
    report = benchmark.generate_report()
    with open("benchmark_report.md", "w") as f:
        f.write(report)

    print("Benchmark completed. Report saved to benchmark_report.md")
```

## Conclusion

This comprehensive performance optimization guide ensures that the corrected Knowledge Graph architecture can handle enterprise-scale operations efficiently while maintaining the backend-first processing approach that resolves the architectural violations identified in the original system. The strategies outlined here provide:

1. **Scalable database architecture** optimized for graph operations
2. **Efficient caching strategies** for frequently accessed data
3. **Asynchronous processing** for computationally intensive operations
4. **Comprehensive monitoring** for performance tracking
5. **Load testing capabilities** for capacity planning

By implementing these optimizations, the system can support the requirements for FR-006 (interactive knowledge graph visualization) and FR-007 (graph navigation) with proper backend processing while maintaining excellent user experience.
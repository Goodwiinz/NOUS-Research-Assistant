# Database Documentation - Multimodal Enterprise RAG System

This document provides comprehensive documentation for the database architecture of the Multimodal Enterprise RAG System.

## Overview

The system uses a polyglot persistence architecture with the following data stores:

1. **PostgreSQL** - Primary relational database for structured data (local: `rag-postgres-1` container, DB `multimodal_rag_dev`; AWS dev: RDS PostgreSQL; Supabase is auth/auxiliary, not the application SQL database)
2. **Neo4j** - Knowledge graph for entities and relationships (self-hosted in-cluster, `bolt://neo4j.gen-text.app`)
3. **DO Knowledge Base** - Optional, flag-gated (`DO_KB_ENABLED`, default `False`) retrieval via DigitalOcean GradientAI (`kbaas.do-ai.run`); manages its own embeddings. Implementation: `backend/src/services/do_kb/`. Controlled by `DO_KB_ENABLED` flag.
4. **Redis** - Cache and real-time data structures (prod: DO Managed Redis; dev: local container)

> **Amendment 2026-10-17:** live retrieval is PostgreSQL full-text search (`fulltext_search_service`, `keyword_search_service`). DO KB is off unless `DO_KB_ENABLED` is set; statements below that call it "the RAG retrieval store" describe the flag-on configuration only.

> **Qdrant removed**: Qdrant has been fully removed from the production stack (`config.py:173` deprecated, `values-production.yaml qdrant.enabled:false`, `QDRANT_URL` unset → `QdrantClient=None`). All Qdrant documentation below is superseded by DO Knowledge Base.

## Architecture Diagram

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   PostgreSQL    │    │     Neo4j       │    │   DO KB (RAG)   │    │      Redis      │
│  (Supabase/dev) │    │  (self-hosted)  │    │  (GradientAI)   │    │  (DO Managed)   │
│ • Users         │◄──►│ • Entities      │◄──►│ • RAG retrieval │◄──►│ • Cache         │
│ • Organizations │    │ • Relationships │    │ • Embeddings    │    │ • Sessions      │
│ • Documents     │    │ • Graph Traversal│    │ • Similarity    │    │ • Rate Limits   │
│ • Audit Logs    │    │ • Path Finding  │    │ • Document KB   │    │ • Pub/Sub       │
│ • Agent Memory* │    │ • Communities   │    │ (kbaas.do-ai.run│    │ • Metrics       │
└─────────────────┘    └─────────────────┘    └─────────────────┘    └─────────────────┘

* PostgreSQL pgvector (via LangGraph AsyncPostgresStore) provides agent memory semantic recall.
  RAG retrieval uses DO Knowledge Base, not pgvector.
```

## Database Connections

### Environment Variables

```bash
# PostgreSQL
# Dev: postgres:postgres@localhost:5432/multimodal_rag_dev (rag-postgres-1 container)
# Prod: Supabase managed (session-mode pooler) — see Supabase dashboard for connection string
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/multimodal_rag_dev"

# Neo4j (self-hosted in-cluster; prod: bolt://neo4j.gen-text.app)
export NEO4J_URI="bolt://localhost:7687"
export NEO4J_USER="neo4j"
export NEO4J_PASSWORD="neo4j_password_123"

# DO Knowledge Base (replaces Qdrant for RAG retrieval; off by default, on in eval CI)
export DO_KB_ENABLED="false"
export DO_KB_URL="https://kbaas.do-ai.run"
export DO_KB_API_KEY="<do_kb_api_key>"

# Redis
# Dev: local container. Prod: DO Managed Redis (external endpoint).
export REDIS_URL="redis://:redis_password_123@localhost:6379/0"
```

### Connection Details

| Service    | Dev Host / Port                      | Prod                                   | Notes                                 |
| ---------- | ------------------------------------ | -------------------------------------- | ------------------------------------- |
| PostgreSQL | localhost:5432, `multimodal_rag_dev` | Supabase managed (session-mode pooler) | pgvector used for agent memory        |
| Neo4j      | localhost:7687 (Bolt)                | bolt://neo4j.gen-text.app              | neo4j:5.26-community in-cluster       |
| DO KB      | kbaas.do-ai.run                      | kbaas.do-ai.run                        | RAG retrieval; manages own embeddings |
| Redis      | localhost:6379                       | DO Managed Redis (external)            | —                                     |

> **Qdrant removed**: `QDRANT_URL` is unset in production; `QdrantClient` initialises to `None`. Do not reference Qdrant collections or connection details for any new work.

## PostgreSQL Schema

### Core Tables

#### Organizations

- **Purpose**: Multi-tenant organization management
- **Key Fields**: id, name, slug, storage_tier, max_storage_gb, max_users
- **Features**: Row-level security, storage quotas

#### Users

- **Purpose**: User authentication and authorization
- **Key Fields**: id, email, password_hash, role, organization_id
- **Features**: Role-based access, login tracking

#### Documents

- **Purpose**: Multimodal document storage
- **Key Fields**: id, title, document_type, content_text, organization_id
- **Features**: Full-text search, versioning, metadata

#### Entities

- **Purpose**: Named entities extracted from documents
- **Key Fields**: id, name, entity_type, confidence_score, organization_id
- **Features**: Entity linking, canonicalization, aliases

#### Search Queries

- **Purpose**: Search analytics and optimization
- **Key Fields**: id, query_text, query_type, results_count, latency_ms
- **Features**: Query analysis, performance tracking

### Performance Optimizations

- **Indexes**: 69+ optimized indexes for common query patterns
- **Full-text Search**: trigram indexes for fuzzy matching
- **Partitioning**: Time-based partitioning for analytics tables
- **Row-Level Security**: Multi-tenant data isolation

### Views and Functions

```sql
-- Organization statistics
SELECT * FROM organization_stats;

-- User activity summary
SELECT * FROM user_activity_summary;

-- Entity importance calculation
SELECT * FROM calculate_entity_importance('entity_id');
```

## Neo4j Knowledge Graph

### Node Types

- **Entity**: People, organizations, locations, concepts
- **Document**: Documents with content metadata
- **User**: System users
- **Organization**: Multi-tenant organizations
- **MultimodalContent**: Images, tables, charts

### Relationship Types

- **RELATED_TO**: General entity relationships
- **EXTRACTED_FROM**: Document-entity extraction
- **PART_OF**: Hierarchical relationships
- **MEMBER_OF**: User-organization membership
- **UPLOADED**: User-document uploads

### Indexes and Constraints

```cypher
-- Unique constraints
CREATE CONSTRAINT entity_id_unique FOR (e:Entity) REQUIRE e.id IS UNIQUE;

-- Performance indexes
CREATE INDEX entity_name_index FOR (e:Entity) ON (e.name);
CREATE INDEX entity_type_index FOR (e:Entity) ON (e.type);

-- Full-text search
CREATE FULLTEXT INDEX entity_content_index FOR (e:Entity) ON EACH [e.name, e.description];
```

### Graph Algorithms

- **Path Finding**: Shortest paths between entities
- **Community Detection**: Entity clustering
- **Centrality Measures**: Importance scoring
- **Similarity**: Graph-based entity similarity

## DO Knowledge Base (RAG Retrieval)

> **Replaces Qdrant.** Qdrant has been fully removed. RAG retrieval is now handled by the DigitalOcean GradientAI Knowledge Base service (`kbaas.do-ai.run`). Implementation lives in `backend/src/services/do_kb/`. Controlled by the `DO_KB_ENABLED` feature flag (off by default in prod; enabled in eval CI).

### Key Characteristics

- **Embeddings**: Managed internally by the DO KB service — no client-side embedding step required.
- **Retrieval**: Semantic similarity search via DO KB API; results are returned as ranked document chunks.
- **Tenancy**: Queries are scoped per knowledge base ID; organization isolation is enforced at the KB provisioning level.
- **Storage**: DO Spaces (`rag-system-storage`, nyc3 region) is used for raw document object storage backing the KB.

### pgvector / PostgreSQL Agent Memory

pgvector (`vector` column type, `<=>` operator) is present in the PostgreSQL schema and is used exclusively by the **LangGraph `AsyncPostgresStore`** for agent memory semantic recall. It is **not** the RAG retrieval store — that role belongs to DO KB.

## Redis Cache System

### Data Structures

#### Cache Configuration

```redis
HSET cache_configs search_ttl 3600
HSET cache_configs document_ttl 7200
HSET cache_configs entity_ttl 1800
```

#### Rate Limiting

```redis
HSET rate_limits search_per_minute 60
HSET rate_limits upload_per_hour 100
HSET rate_limits api_requests_per_minute 1000
```

#### Session Management

```redis
HMSET session:user123 user_id 123 organization_id org456 ...
EXPIRE session:user123 3600
```

### Real-time Features

- **Pub/Sub**: Real-time updates and notifications
- **Metrics**: Performance counters and analytics
- **Locks**: Distributed locking for critical operations
- **Queues**: Job processing queues

## Database Initialization

### Quick Start

1. **Ensure Docker containers are running**:

   ```bash
   docker-compose ps
   ```

2. **Run database initialization**:

   ```bash
   chmod +x database/init/05_database_init.sh
   ./database/init/05_database_init.sh
   ```

3. **Verify health status**:
   ```bash
   python3 database/health_checks.py
   ```

### Manual Setup Steps

#### PostgreSQL

```bash
# Dev container: rag-postgres-1, DB multimodal_rag_dev
psql -h localhost -p 5432 -U postgres -d multimodal_rag_dev -f database/init/01_schema.sql

# Verify tables
psql -h localhost -p 5432 -U postgres -d multimodal_rag_dev -c "\dt"
```

#### Neo4j

```bash
# Run Cypher setup
cypher-shell -a bolt://localhost:7687 -u neo4j -p neo4j_password_123 -f database/init/02_neo4j_setup.cypher

# Verify nodes
cypher-shell -a bolt://localhost:7687 -u neo4j -p neo4j_password_123 "MATCH (n) RETURN count(n) as node_count"
```

#### DO Knowledge Base

DO KB is provisioned and managed via the DigitalOcean console or API. No local setup step is required; configure `DO_KB_ENABLED`, `DO_KB_URL`, and `DO_KB_API_KEY` environment variables and start the backend. See `backend/src/services/do_kb/` for the client implementation.

#### Redis

```bash
# Configure Redis
redis-cli -h localhost -p 6379 -a redis_password_123 CONFIG SET maxmemory 256mb
redis-cli -h localhost -p 6379 -a redis_password_123 CONFIG SET maxmemory-policy allkeys-lru
```

## Health Monitoring

### Health Check Script

```bash
python3 database/health_checks.py
```

### Manual Health Checks

#### PostgreSQL

```sql
-- Check connection
SELECT 1;

-- Check table counts
SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';

-- Check database size
SELECT pg_size_pretty(pg_database_size('ragdb'));
```

#### Neo4j

```cypher
-- Check connectivity
RETURN 1;

-- Check node counts
MATCH (n) RETURN labels(n) as type, count(n) as count;

-- Check relationship counts
MATCH ()-[r]->() RETURN type(r) as type, count(r) as count;
```

#### DO Knowledge Base

DO KB health is monitored via the DigitalOcean dashboard. The backend's `DO_KB_ENABLED` flag gates all KB interactions; if the flag is off, KB calls are skipped.

#### Redis

```bash
# Check connectivity
redis-cli -h localhost -p 6379 -a redis_password_123 ping

# Check memory usage
redis-cli -h localhost -p 6379 -a redis_password_123 info memory
```

## Performance Optimization

### PostgreSQL

- **Connection Pooling**: Use connection pools (10-20 connections)
- **Query Optimization**: Use EXPLAIN ANALYZE for slow queries
- **Indexing**: Composite indexes for complex queries
- **Vacuuming**: Regular autovacuum configuration

### Neo4j

- **Memory Configuration**: Adequate heap and page cache
- **Index Usage**: Query plan analysis
- **Query Optimization**: PROFILE command for query analysis
- **Batch Operations**: Use UNWIND for bulk operations

### DO Knowledge Base

- **Throughput**: Batch document ingestion via the DO KB API where supported
- **Flag gate**: Keep `DO_KB_ENABLED=false` in prod by default; enable only in eval CI to control costs

### Redis

- **Memory Management**: LRU eviction policy
- **Connection Pooling**: Reuse connections
- **Pipeline Commands**: Batch Redis operations
- **Key Expiration**: Set TTL for cache entries

## Security

### Authentication

- **PostgreSQL**: Role-based access with RLS policies
- **Neo4j**: User authentication with role-based permissions
- **DO Knowledge Base**: API key authentication (`DO_KB_API_KEY`)
- **Redis**: Password authentication

### Data Isolation

- **Multi-tenancy**: Row-level security in PostgreSQL
- **Organization Scoping**: All queries filtered by organization_id
- **Network Security**: Database network isolation
- **Audit Logging**: Complete audit trail

## Backup and Recovery

### PostgreSQL

```bash
# Backup
pg_dump -h localhost -p 5432 -U raguser ragdb > backup.sql

# Restore
psql -h localhost -p 5432 -U raguser ragdb < backup.sql
```

### Neo4j

```bash
# Backup
neo4j-admin database backup --database=neo4j --to-path=/backup

# Restore
neo4j-admin database restore --from-path=/backup --database=neo4j
```

### DO Knowledge Base

DO KB data is managed by DigitalOcean. Use the DO console or API snapshots for backup. Raw source documents are stored in DO Spaces (`rag-system-storage`, nyc3) and serve as the authoritative source for re-ingestion.

### Redis

```bash
# Backup
redis-cli -h localhost -p 6379 -a redis_password_123 BGSAVE

# Restore
redis-server --appendonly yes
```

## Troubleshooting

### Common Issues

#### PostgreSQL Connection Issues

- Check user permissions: `\du` in psql
- Verify database exists: `\l` in psql
- Check network connectivity: `telnet localhost 5432`

#### Neo4j Performance Issues

- Check memory usage: `CALL dbms.listConnections()`
- Monitor query performance: Use PROFILE keyword
- Verify index usage: `EXPLAIN` query plans

#### DO Knowledge Base Issues

- Verify `DO_KB_ENABLED=true` and `DO_KB_API_KEY` is set
- Check DO dashboard for KB service health at `kbaas.do-ai.run`
- Review backend logs in `backend/src/services/do_kb/` for client-side errors

#### Redis Memory Issues

- Monitor memory usage: `INFO memory`
- Check eviction policy: `CONFIG GET maxmemory-policy`
- Analyze key distribution: `INFO keyspace`

### Performance Tuning

#### PostgreSQL Configuration

```ini
# postgresql.conf
shared_buffers = 256MB
effective_cache_size = 1GB
work_mem = 4MB
maintenance_work_mem = 64MB
```

#### Neo4j Configuration

```properties
# neo4j.conf
dbms.memory.heap.initial_size=1G
dbms.memory.heap.max_size=2G
dbms.memory.pagecache.size=1G
```

#### Redis Configuration

```bash
# redis.conf
maxmemory 256mb
maxmemory-policy allkeys-lru
save 900 1
save 300 10
save 60 10000
```

## API Integration

### Connection Libraries

```python
# PostgreSQL (dev: asyncpg direct; prod: Supabase session-mode pooler via SQLAlchemy AsyncEngine)
import asyncpg
conn = await asyncpg.connect(DATABASE_URL)

# Neo4j
from neo4j import AsyncGraphDatabase
driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# DO Knowledge Base — see backend/src/services/do_kb/ for the client wrapper
# QdrantClient is NOT used; QDRANT_URL is unset in prod.

# Redis (dev: local; prod: DO Managed Redis external endpoint)
import redis.asyncio as redis
redis_client = redis.Redis.from_url(REDIS_URL)
```

### Example Query Patterns

```python
# Multi-database search (current architecture)
async def search_documents(query_text, organization_id):
    # 1. RAG retrieval via DO Knowledge Base (manages its own embeddings)
    #    See backend/src/services/do_kb/ for the client implementation.
    do_kb_results = await do_kb_client.search(query=query_text, limit=10)

    # 2. Get document details from PostgreSQL
    document_ids = [r["document_id"] for r in do_kb_results]
    documents = await postgres_conn.fetch(
        "SELECT * FROM documents WHERE id = ANY($1) AND organization_id = $2",
        document_ids, organization_id
    )

    # 3. Get related entities from Neo4j (circuit-breaker gated)
    entity_results = []
    for doc in documents:
        entities = await neo4j_session.run(
            "MATCH (d:Document {id: $doc_id})-[:EXTRACTED_FROM]-(e:Entity) RETURN e",
            doc_id=doc["id"]
        )
        entity_results.extend([record["e"] for record in entities])

    return {
        "documents": documents,
        "entities": entity_results,
        "search_metadata": {
            "query": query_text,
            "results_count": len(documents),
            "entities_found": len(entity_results)
        }
    }
```

## Monitoring and Analytics

### Key Metrics

#### PostgreSQL

- Connection count
- Query latency
- Database size
- Table sizes
- Index usage

#### Neo4j

- Node/relationship counts
- Query performance
- Memory usage
- Cache hit rates

#### DO Knowledge Base

- Query latency
- Retrieval result quality (eval CI)
- API error rates

#### Redis

- Memory usage
- Hit rates
- Connection count
- Operations per second

### Alerting

Set up alerts for:

- Database connection failures
- High query latency (>2 seconds)
- Low cache hit rates (<80%)
- Memory usage (>90%)

## Future Enhancements

### Scalability

- Read replicas for PostgreSQL
- Neo4j clustering
- DO KB multi-region provisioning
- Redis clustering

### Performance

- Materialized views
- Graph embeddings
- Advanced caching strategies
- Query optimization

### Features

- Real-time synchronization
- Advanced analytics
- Machine learning integration
- Automated backups

---

_Last updated: June 2026 — Qdrant removed; DO Knowledge Base (GradientAI) is the RAG retrieval store; PostgreSQL prod is Supabase managed; Redis prod is DO Managed Redis._

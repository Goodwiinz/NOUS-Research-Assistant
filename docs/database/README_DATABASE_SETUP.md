# Database Setup Guide - Multimodal Enterprise RAG System

## Quick Start

The NOUS platform uses a multi-database architecture: PostgreSQL (primary), Neo4j (knowledge graph), Redis (cache/sessions), and, optionally, DO Knowledge Base (off by default). Live retrieval is PostgreSQL full-text search.

### Prerequisites

- Docker and Docker Compose installed (for local dev)
- Python 3.11 (the version CI uses; see `PYTHON_VERSION` in `.github/workflows/test-pipeline.yml`)

### Current Status

| Service           | Status | Role                   | Connection                                    |
| ----------------- | ------ | ---------------------- | --------------------------------------------- |
| PostgreSQL        | Active | Primary DB             | Local: `rag-postgres-1`; AWS dev: RDS PostgreSQL |
| Neo4j             | Active | Knowledge graph        | `bolt://localhost:7687` (dev)                 |
| Redis             | Active | Cache / sessions       | Dev: local port 6379 / Prod: DO Managed Redis |
| DO Knowledge Base | Flag-off (`DO_KB_ENABLED=False` by default) | Optional retrieval backend | `DO_KB_*` env vars |

> **Note (2026-10-17):** Qdrant has been removed. DO Knowledge Base is an optional backend behind `DO_KB_ENABLED`, which defaults to `False` (`backend/src/core/config.py`). Live retrieval is PostgreSQL full-text search; pgvector semantic search is flag-gated (`SEMANTIC_SEARCH_ENABLED`, `DOCUMENT_EMBEDDINGS_ENABLED`, both default off). AWS application SQL uses RDS PostgreSQL; Supabase remains an auth/auxiliary service (see `docs/engineering/gotchas.md`). Production sections below that mention Supabase as the application database are historical.

## Database Files

### Core Schema Files

```
database/
├── init/
│   ├── 01_schema.sql              # PostgreSQL complete schema
│   ├── 02_neo4j_setup.cypher      # Neo4j knowledge graph setup
│   ├── 04_redis_config.lua        # Redis configuration script
│   └── 05_database_init.sh        # Complete initialization script
├── health_checks.py               # Comprehensive health monitoring
└── database_connections.md        # Connection information
```

### Documentation

```
├── DATABASE_DOCUMENTATION.md      # Complete database documentation
└── README_DATABASE_SETUP.md      # This file
```

## Database Configuration

### PostgreSQL (Primary Database)

- **Purpose**: Structured data, user management, audit logs
- **Tables**: 20+ optimized tables with full-text search
- **Features**: Row-level security, multi-tenant isolation
- **Dev connection**: `postgresql://postgres:postgres@localhost:5432/multimodal_rag_dev` (container `rag-postgres-1`)
- **Prod connection**: Supabase managed — use `SUPABASE_DB_URL` env var
- **Migrations**: Alembic (`alembic upgrade head`). Dev runs against local container; prod applies the same migrations pointed at Supabase URL.

### Neo4j (Knowledge Graph)

- **Purpose**: Entity relationships, graph traversal
- **Node Types**: Entities, Documents, Users, Organizations
- **Features**: Full-text search, graph algorithms
- **Image**: `neo4j:5.26-community`
- **Dev connection**: `bolt://localhost:7687`
- **Prod connection**: `bolt://neo4j.gen-text.app`

### DO Knowledge Base (Vector / RAG Retrieval)

> Qdrant has been removed. Vector search is now handled by DigitalOcean Knowledge Base (GradientAI).

- **Purpose**: Semantic search, document embeddings, RAG retrieval
- **Managed via**: `backend/src/services/do_kb/`
- **No local container needed** — fully managed service
- **Required env vars**:
  - `DO_API_TOKEN`
  - `DO_KB_PROJECT_ID`
  - `DO_KB_REGION`
  - `DO_KB_EMBEDDING_MODEL_UUID`

### Redis (Cache & Real-time)

- **Purpose**: Caching, sessions, rate limiting, pub/sub
- **Features**: Real-time updates, performance optimization
- **Dev connection**: `redis://localhost:6379` (local container)
- **Prod**: DO Managed Redis instance

### Object Storage (DO Spaces)

- **Purpose**: Document and asset storage
- **Provider**: DigitalOcean Spaces (S3-compatible)
- **Required env vars**:
  - `S3_ENDPOINT_URL=https://nyc3.digitaloceanspaces.com`
  - `S3_BUCKET_NAME=rag-system-storage`
  - `S3_REGION=nyc3`
  - `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`

## Running Database Setup

### Option 1: Automated Setup (Recommended)

```bash
# Run complete database initialization
chmod +x database/init/05_database_init.sh
./database/init/05_database_init.sh
```

### Option 2: Manual Setup

#### PostgreSQL Setup (Local Dev)

```bash
# Apply Alembic migrations against local dev container
alembic upgrade head

# Or initialize schema directly
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d multimodal_rag_dev -f database/init/01_schema.sql

# Verify setup
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d multimodal_rag_dev -c "\dt"
```

#### PostgreSQL Setup (Production — Supabase)

```bash
# Apply migrations to Supabase
DATABASE_URL="$SUPABASE_DB_URL" alembic upgrade head
```

#### Neo4j Setup

```bash
# Run Neo4j setup (requires cypher-shell)
cypher-shell -a bolt://localhost:7687 -u neo4j -p neo4j_password_123 -f database/init/02_neo4j_setup.cypher
```

#### Redis Setup (Local Dev)

```bash
# Verify Redis is running
redis-cli -h localhost -p 6379 ping

# Fix password configuration if needed
docker exec rag-redis-1 redis-cli CONFIG SET requirepass "redis_password_123"
docker exec rag-redis-1 redis-cli AUTH redis_password_123
```

#### DO Knowledge Base

No local setup required. Ensure `DO_KB_*` env vars are set. The service client in `backend/src/services/do_kb/` connects automatically.

## Database Health Checks

### Run Health Monitoring

```bash
# Install required packages first
pip3 install --break-system-packages asyncpg redis neo4j

# Run comprehensive health checks
python3 database/health_checks.py
```

### Manual Health Checks

#### PostgreSQL

```bash
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d multimodal_rag_dev -c "SELECT count(*) FROM information_schema.tables;"
```

#### Redis

```bash
redis-cli -h localhost -p 6379 ping
```

#### Neo4j

```bash
echo "RETURN 1;" | cypher-shell -a bolt://localhost:7687 -u neo4j -p neo4j_password_123
```

## Database Statistics

### Current State

- **PostgreSQL**: 20 tables, 69 indexes, multi-tenant security enabled
- **DO Knowledge Base**: Managed vector collections (documents, entities)
- **Neo4j**: Graph database with constraints and indexes
- **Redis**: Cache system with rate limiting and pub/sub

## Application Integration

### Environment Variables

```bash
# PostgreSQL
# Dev
DATABASE_URL="postgresql://postgres:postgres@localhost:5432/multimodal_rag_dev"
# Prod
SUPABASE_DB_URL="postgresql://..."   # Supabase connection string

# Neo4j
NEO4J_URI="bolt://localhost:7687"    # dev; prod: bolt://neo4j.gen-text.app
NEO4J_USER="neo4j"
NEO4J_PASSWORD="neo4j_password_123"

# Redis
REDIS_URL="redis://localhost:6379/0" # dev; prod: DO Managed Redis URL

# DO Knowledge Base (vector/RAG)
DO_API_TOKEN="..."
DO_KB_PROJECT_ID="..."
DO_KB_REGION="..."
DO_KB_EMBEDDING_MODEL_UUID="..."

# DO Spaces (object storage)
S3_ENDPOINT_URL="https://nyc3.digitaloceanspaces.com"
S3_BUCKET_NAME="rag-system-storage"
S3_REGION="nyc3"
S3_ACCESS_KEY_ID="..."
S3_SECRET_ACCESS_KEY="..."
```

### Connection Code Examples

```python
# PostgreSQL
import asyncpg
conn = await asyncpg.connect(os.getenv("DATABASE_URL"))

# Neo4j
from neo4j import AsyncGraphDatabase
driver = AsyncGraphDatabase.driver(
    os.getenv("NEO4J_URI"),
    auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD"))
)

# Redis
import redis.asyncio as redis
redis_client = redis.from_url(os.getenv("REDIS_URL"))

# DO Knowledge Base — use the service layer
from src.services.do_kb import DOKnowledgeBaseService
kb = DOKnowledgeBaseService()
```

## Performance Features

### PostgreSQL Optimizations

- **Full-text search**: Trigram indexes for fuzzy matching
- **Multi-tenant isolation**: Row-level security policies
- **Performance indexes**: 69+ optimized indexes
- **Materialized views**: Organization and user statistics

### Neo4j Features

- **Graph algorithms**: Path finding, centrality measures
- **Full-text search**: Entity content search
- **Constraints**: Data integrity and uniqueness

### DO Knowledge Base Features

- **Managed embeddings**: Configurable embedding model via `DO_KB_EMBEDDING_MODEL_UUID`
- **Semantic search**: Cosine similarity with metadata filtering
- **No infrastructure maintenance**: Fully managed by DigitalOcean

### Redis Features

- **Rate limiting**: User and API rate limiting
- **Session management**: User session tracking
- **Pub/Sub**: Real-time updates
- **Caching**: Multi-layer caching strategy

## Troubleshooting

### Common Issues and Solutions

#### PostgreSQL Connection Issues

```bash
# Check if dev container is running
docker ps | grep rag-postgres-1

# Check container logs
docker logs rag-postgres-1

# Connect directly
docker exec -it rag-postgres-1 psql -U postgres -d multimodal_rag_dev
```

#### Redis Authentication Issues

```bash
# Reset Redis password (dev)
docker exec rag-redis-1 redis-cli CONFIG SET requirepass "redis_password_123"

# Test connection
docker exec rag-redis-1 redis-cli AUTH redis_password_123
```

#### Neo4j Connection Issues

```bash
# Check Neo4j status
docker exec rag-neo4j-1 cypher-shell -u neo4j -p neo4j_password_123 "RETURN 1"

# Reset password if needed
docker exec rag-neo4j-1 cypher-shell -u neo4j "ALTER CURRENT USER SET PASSWORD FROM 'old_password' TO 'neo4j_password_123'"
```

#### DO Knowledge Base Issues

- Verify `DO_API_TOKEN`, `DO_KB_PROJECT_ID`, `DO_KB_REGION`, `DO_KB_EMBEDDING_MODEL_UUID` are set in `.env`
- Check `backend/src/services/do_kb/` service logs for connectivity errors

## Monitoring and Maintenance

### Database Monitoring

```bash
# PostgreSQL monitoring
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d multimodal_rag_dev -c "SELECT * FROM organization_stats;"

# Redis monitoring
redis-cli -h localhost -p 6379 info memory
```

### Regular Maintenance

1. **PostgreSQL**: Regular vacuuming and index rebuilding
2. **Neo4j**: Monitor memory usage and query performance
3. **DO Knowledge Base**: Monitor via DO console
4. **Redis**: Monitor memory usage and hit rates

## Next Steps

1. **Test Application Integration**: Connect your application to the databases
2. **Run Performance Tests**: Validate query performance meets requirements
3. **Set Up Monitoring**: Configure database monitoring and alerting
4. **Configure Backups**: Set up regular backup procedures for PostgreSQL and Neo4j
5. **Scale as Needed**: Upgrade DO managed service tiers as usage grows

## Additional Resources

- **Complete Documentation**: `DATABASE_DOCUMENTATION.md`
- **Health Monitoring**: `database/health_checks.py`
- **Connection Info**: `database_connections.md`
- **Schema Details**: `database/init/01_schema.sql`
- **DO KB Service**: `backend/src/services/do_kb/`

## Support

If you encounter issues:

1. Check container status: `docker-compose ps`
2. Review container logs: `docker logs rag-postgres-1`
3. Run health checks: `python3 database/health_checks.py`
4. Check this troubleshooting section

---

**Last Updated**: 2026-06-12

# NOUS — Data CODEMAP

> Database schemas (PostgreSQL, Neo4j, Redis), migrations, seeders.
> Partly refreshed 2026-10-17: Qdrant sections and nonexistent files were corrected. The migration table below is a 2026-04-26 snapshot of selected revisions and is not exhaustive; run `ls backend/alembic/versions` for the real chain.

## Data stores

| Store | Port | Purpose |
|---|---|---|
| PostgreSQL | 5432 | Primary relational store (RDS PostgreSQL on AWS; local container `rag-postgres-1`) — users, documents, threads, projects, analytics, full-text search (`documents.search_vector`); optional pgvector `document_embeddings` behind flags |
| Neo4j | 7687 | Knowledge graph — entities, relationships, citations |
| Redis / Valkey | 6379 | Cache, Celery broker, WebSocket pub/sub, rate limiting |

## PostgreSQL schema

Managed by **Alembic** (`backend/alembic/`). The canonical migration chain is in `backend/alembic/versions/`.

### Migration chain (selected key revisions)

| Revision | Description |
|---|---|
| `258df00ea837` | Initial schema — users, documents, threads |
| `a1b2c3...` | Thread-centric chat schema |
| `add_api_keys_table` | API key management |
| `add_search_analytics_table` | Search event logging |
| `b2c3d4e5...` | Full-text search for threads (pg_trgm) |
| `c8e2f1a9...` | External citation support |
| `d9f3g4h5...` | Extended citation metadata |
| `e0g4h5i6...` | Citation relationship table |
| `f1h5i6j7...` | Collections + research extensions |
| `f931599b...` | Enhanced document processing columns |
| `g2i6j7k8...` | Project notes |
| `h3j7k8l9...` | Generated drafts |
| `i4k8l9m0...` | Project-thread integration |

Run migrations: `cd backend && alembic upgrade head`  
Create new: `alembic revision --autogenerate -m "describe_change"`  
Roll back one: `alembic downgrade -1`

### Raw SQL migrations (`database/migrations/`)

Legacy SQL files — used for PostgreSQL features (views, triggers, partitioning) that Alembic doesn't autogenerate. Applied manually or via deploy scripts. These have numeric prefix collisions (`001_*`, `007_*`, `008_*`) — **do not rely on filename order; read each file's header comment for application context.**

Key files:

| File | Purpose |
|---|---|
| `006_document_upload_schema.sql` | Document storage columns |
| `007_analytics_schema.sql` | Analytics event tables |
| `009_realtime_status_optimizations.sql` | Realtime status indexes |
| `011_realtime_document_status_enhancements.sql` | Final realtime status schema |
| `012_ai_document_qa_system.sql` | QA system tables |

Also in `database/`:

| File | Purpose |
|---|---|
| `indexing_strategy.sql` | Index creation guidelines + DDL |
| `partitioning_strategy.sql` | Table partitioning (analytics events) |
| `performance_optimization.sql` | PG tuning queries |
| `monitoring_schema.sql` | Monitoring/metrics tables |
| `data_retention_policies.sql` | TTL + cleanup jobs |
| `triggers/` | PG trigger functions |
| `views/` | Materialized + regular views |
| `procedures/` | Stored procedures |

## Neo4j schema

Graph database for the knowledge graph. No migration tool — schema is enforced via:

- Constraints: `database/init_knowledge_graph_db.sql` (Cypher `CREATE CONSTRAINT`)
- Indexes: `database/neo4j_performance.cypher`
- Init script: `database/neo4j-init.cypher`

### Core node labels

| Label | Properties | Purpose |
|---|---|---|
| `Entity` | id, name, type, project_id | Extracted entities (person, org, concept) |
| `Document` | id, title, url, project_id | Ingested documents |
| `Concept` | id, name, description | Domain concepts |
| `Citation` | id, doi, title, authors | Academic citations |

### Core relationships

`MENTIONS`, `RELATES_TO`, `CITES`, `PART_OF`, `AUTHORED_BY`

Redis KG cache config: `database/redis_knowledge_graph_config.lua`

## Vector storage

Qdrant is fully removed (the leftover `database/qdrant_knowledge_graph_collections.json` is not used). Live retrieval is PostgreSQL full-text search. `DO_KB_ENABLED` defaults to `False`, and pgvector semantic search (`document_embeddings`, created only by migration `jd01_document_embeddings` when the `vector` extension is available) is gated by `SEMANTIC_SEARCH_ENABLED` / `DOCUMENT_EMBEDDINGS_ENABLED`, both default off. See `backend/src/services/search/README.md`.

## Redis / Valkey usage

| Key pattern | TTL | Purpose |
|---|---|---|
| `session:<user_id>` | 24h | Auth session cache |
| `rate:<ip>:<endpoint>` | 60s | Rate limiting counters |
| `ws:room:<thread_id>` | — | WebSocket pub/sub channel |
| `celery:*` | — | Celery task queue / results |
| `kg:cache:<query_hash>` | 5m | KG query result cache |

## Supabase and hosting specifics

- AWS application SQL uses RDS PostgreSQL via `DATABASE_URL`; `SUPABASE_DB_URL` is deliberately blank in the AWS overlay. Supabase remains an auth/auxiliary service with its own migrations in `supabase/migrations/`.
- Connection string for the agent checkpointer must be `postgresql://` (psycopg v3), not `postgresql+asyncpg://`.
- Document bytes live in S3-compatible object storage; `Document.storage_path` is the bare object key and `file_path` is `s3://bucket/key`.
- Alembic rules: exactly one head and revision ids of at most 32 characters, enforced by `scripts/ci/check_alembic.py` (see `backend/alembic/README.md`).

## Seeding / fixtures

Test fixtures: `data/datasets/` — 4 fixture files used in integration tests.  
There is no `backend/scripts/seed_dev_data.py`; `backend/src/core/init_db.py` provides the local bootstrap (`create_all`, default org/user).

## Adding a new table

1. Create Alembic migration: `cd backend && alembic revision --autogenerate -m "add_<table>"`.
2. Add SQLAlchemy model in `backend/src/models/<domain>.py`.
3. Add Pydantic schema in `backend/src/schemas/<domain>.py`.
4. If the table needs indexing strategy docs, add to `database/indexing/`.
5. If the table has a TTL/retention policy, add to `database/data_retention_policies.sql`.

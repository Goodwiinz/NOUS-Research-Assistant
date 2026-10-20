# Migration Strategy and Implementation Guide

> **Stale / historical (2026-10-17):** Qdrant is fully removed and live retrieval is PostgreSQL full-text search (`DO_KB_ENABLED` defaults to `False`; pgvector semantic search is flag-gated). Treat Qdrant, DO KB and vector-store statements below as historical. Current contracts: [`docs/engineering/`](../engineering/README.md).

## Multimodal Enterprise RAG System Analytics Platform

This document provides a comprehensive migration strategy for extending the existing Neo4j/Qdrant-based RAG system with the new PostgreSQL analytics platform.

## 1. Migration Strategy Overview

### 1.1 Migration Phases

**Phase 1: Infrastructure Setup (Week 1-2)**
- Provision PostgreSQL analytics database
- Configure connection pooling and performance tuning
- Set up monitoring and alerting
- Initialize schema with core tables

**Phase 2: Data Integration (Week 2-4)**
- Implement data synchronization from existing systems
- Set up change data capture (CDC) from Neo4j and Qdrant
- Migrate historical data where applicable
- Validate data integrity

**Phase 3: Application Integration (Week 3-5)**
- Update application code to emit analytics events
- Implement middleware for performance logging
- Add evaluation pipeline integration
- Test end-to-end data flow

**Phase 4: Analytics Implementation (Week 4-6)**
- Deploy analytics dashboards and reporting
- Implement A/B testing framework
- Set up automated monitoring and alerting
- Train teams on new analytics capabilities

### 1.2 Risk Mitigation

| Risk | Mitigation Strategy | Impact Level |
|------|-------------------|--------------|
| Data loss during migration | Full backups, incremental sync validation | High |
| Performance degradation on main system | Async processing, throttled migration | Medium |
| Schema changes requiring application updates | Backward compatibility, feature flags | Medium |
| Data consistency issues | Validation scripts, reconciliation jobs | High |
| Increased complexity in monitoring | Simplified observability, clear documentation | Low |

## 2. Database Setup and Configuration

### 2.1 PostgreSQL Analytics Database Configuration

```sql
-- Create dedicated analytics database
CREATE DATABASE multimodal_rag_analytics
    WITH
    OWNER = postgres
    ENCODING = 'UTF8'
    LC_COLLATE = 'en_US.UTF-8'
    LC_CTYPE = 'en_US.UTF-8'
    TABLESPACE = pg_default
    CONNECTION LIMIT = -1;

-- Connect to analytics database
\c multimodal_rag_analytics;

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "btree_gin";
CREATE EXTENSION IF NOT EXISTS "btree_gist";
CREATE EXTENSION IF NOT EXISTS "pg_partman";
CREATE EXTENSION IF NOT EXISTS "pg_cron";

-- Create schemas
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS raw_data;
CREATE SCHEMA IF NOT EXISTS aggregated;
CREATE SCHEMA IF NOT EXISTS archive;
CREATE SCHEMA IF NOT EXISTS staging;

-- Set default privileges
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT ALL ON TABLES TO analytics_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA raw_data GRANT ALL ON TABLES TO analytics_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA aggregated GRANT SELECT ON TABLES TO reporting_role;
```

### 2.2 Performance Configuration

```sql
-- Performance tuning for analytics workload
ALTER SYSTEM SET shared_buffers = '4GB';
ALTER SYSTEM SET effective_cache_size = '12GB';
ALTER SYSTEM SET maintenance_work_mem = '1GB';
ALTER SYSTEM SET checkpoint_completion_target = 0.9;
ALTER SYSTEM SET wal_buffers = '16MB';
ALTER SYSTEM SET default_statistics_target = 100;
ALTER SYSTEM SET random_page_cost = 1.1;
ALTER SYSTEM SET effective_io_concurrency = 200;

-- Configure pg_stat_statements for query monitoring
ALTER SYSTEM SET pg_stat_statements.track = 'all';
ALTER SYSTEM SET pg_stat_statements.max = 10000;

-- Configure partition management
ALTER SYSTEM SET cron.schedule = '0 2 * * *' AS $$SELECT partman.run_maintenance();$$;

-- Reload configuration
SELECT pg_reload_conf();
```

### 2.3 Connection Pooling with PgBouncer

```ini
# pgbouncer.ini
[databases]
multimodal_rag_analytics = host=localhost port=5432 dbname=multimodal_rag_analytics

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
max_client_conn = 2000
default_pool_size = 25
min_pool_size = 10
reserve_pool_size = 5
reserve_pool_timeout = 5
max_db_connections = 50
max_user_connections = 50

# Timeout settings
server_reset_query = DISCARD ALL
server_check_delay = 30
server_check_query = select 1
server_lifetime = 3600
server_idle_timeout = 600
```

## 3. Schema Migration Scripts

### 3.1 Core Schema Creation Script

```sql
-- File: 01_create_core_schema.sql
-- This script creates the core analytics schema

-- Create role hierarchy
CREATE ROLE analytics_role WITH LOGIN PASSWORD 'secure_password';
CREATE ROLE reporting_role WITH LOGIN PASSWORD 'secure_password';

GRANT analytics_role TO reporting_role;

-- Organizations and Users
CREATE TABLE IF NOT EXISTS analytics.organizations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    settings JSONB DEFAULT '{}',
    subscription_plan VARCHAR(50) DEFAULT 'basic',
    max_documents INTEGER DEFAULT 10000,
    max_users INTEGER DEFAULT 100,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS analytics.users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES analytics.organizations(id),
    email VARCHAR(255) NOT NULL,
    username VARCHAR(100) NOT NULL,
    full_name VARCHAR(255),
    role VARCHAR(50) DEFAULT 'user',
    preferences JSONB DEFAULT '{}',
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMPTZ,
    UNIQUE(organization_id, email),
    UNIQUE(organization_id, username)
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_organizations_slug ON analytics.organizations(slug);
CREATE INDEX IF NOT EXISTS idx_users_organization ON analytics.users(organization_id);
CREATE INDEX IF NOT EXISTS idx_users_email ON analytics.users(email);

-- Grant permissions
GRANT SELECT, INSERT, UPDATE ON analytics.organizations TO analytics_role;
GRANT SELECT, INSERT, UPDATE ON analytics.users TO analytics_role;
GRANT SELECT ON analytics.organizations TO reporting_role;
GRANT SELECT ON analytics.users TO reporting_role;

COMMIT;
```

### 3.2 Time-Series Tables with Partitioning

```sql
-- File: 02_create_partitioned_tables.sql
-- Creates partitioned tables for time-series data

-- Search queries table
CREATE TABLE IF NOT EXISTS analytics.search_queries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES analytics.users(id),
    session_id UUID,
    organization_id UUID NOT NULL REFERENCES analytics.organizations(id),
    query_text TEXT NOT NULL,
    query_type VARCHAR(50) DEFAULT 'hybrid',
    query_metadata JSONB DEFAULT '{}',
    result_count INTEGER DEFAULT 0,
    result_ids JSONB DEFAULT '[]',
    result_scores JSONB DEFAULT '[]',
    query_time_ms INTEGER NOT NULL,
    vector_search_time_ms INTEGER,
    graph_search_time_ms INTEGER,
    ranking_time_ms INTEGER,
    clicked_result_ids JSONB DEFAULT '[]',
    clicked_positions JSONB DEFAULT '[]',
    user_satisfaction_score INTEGER,
    experiment_id UUID,
    variant_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMPTZ
) PARTITION BY RANGE (created_at);

-- Create initial partitions
CREATE TABLE IF NOT EXISTS analytics.search_queries_y2024m01
    PARTITION OF analytics.search_queries
    FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');

CREATE TABLE IF NOT EXISTS analytics.search_queries_y2024m02
    PARTITION OF analytics.search_queries
    FOR VALUES FROM ('2024-02-01') TO ('2024-03-01');

-- Configure pg_partman for automatic partition management
SELECT partman.create_parent(
    p_parent_table => 'analytics.search_queries',
    p_control => 'created_at',
    p_type => 'native',
    p_interval => '1 month',
    p_premake => 3,
    p_automatic_maintenance => 'on'
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_search_queries_user ON analytics.search_queries (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_queries_org ON analytics.search_queries (organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_queries_type ON analytics.search_queries (query_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_queries_time ON analytics.search_queries (query_time_ms, created_at DESC);

-- Grant permissions
GRANT SELECT, INSERT, UPDATE ON analytics.search_queries TO analytics_role;
GRANT SELECT ON analytics.search_queries TO reporting_role;

COMMIT;
```

### 3.3 Evaluation and Metrics Tables

```sql
-- File: 03_create_evaluation_tables.sql
-- Creates tables for RAG evaluation and custom metrics

-- Evaluation runs table
CREATE TABLE IF NOT EXISTS analytics.evaluation_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    query_id UUID NOT NULL,
    organization_id UUID NOT NULL REFERENCES analytics.organizations(id),
    evaluation_type VARCHAR(50) DEFAULT 'rag_triad',
    answer_relevancy_score DECIMAL(5,4),
    answer_relevancy_confidence DECIMAL(5,4),
    answer_relevancy_threshold DECIMAL(5,4) DEFAULT 0.7000,
    faithfulness_score DECIMAL(5,4),
    faithfulness_confidence DECIMAL(5,4),
    faithfulness_threshold DECIMAL(5,4) DEFAULT 0.9000,
    contextual_relevancy_score DECIMAL(5,4),
    contextual_relevancy_confidence DECIMAL(5,4),
    contextual_relevancy_threshold DECIMAL(5,4) DEFAULT 0.7000,
    overall_score DECIMAL(5,4),
    meets_thresholds BOOLEAN,
    evaluator_model VARCHAR(100),
    evaluation_config JSONB DEFAULT '{}',
    evaluation_time_ms INTEGER,
    cost_tokens INTEGER,
    cost_usd DECIMAL(10,6),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMPTZ,
    FOREIGN KEY (query_id) REFERENCES analytics.search_queries(id) ON DELETE CASCADE
) PARTITION BY RANGE (created_at);

-- Configure partitions
SELECT partman.create_parent(
    p_parent_table => 'analytics.evaluation_runs',
    p_control => 'created_at',
    p_type => 'native',
    p_interval => '1 month',
    p_premake => 3,
    p_automatic_maintenance => 'on'
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_query ON analytics.evaluation_runs (query_id);
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_org ON analytics.evaluation_runs (organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_type ON analytics.evaluation_runs (evaluation_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_overall ON analytics.evaluation_runs (overall_score, created_at DESC);

-- Grant permissions
GRANT SELECT, INSERT, UPDATE ON analytics.evaluation_runs TO analytics_role;
GRANT SELECT ON analytics.evaluation_runs TO reporting_role;

COMMIT;
```

## 4. Data Integration and Synchronization

### 4.1 Change Data Capture (CDC) Setup

```python
# File: data_sync/cdc_config.py
import asyncio
import json
from typing import Dict, Any
from dataclasses import dataclass
from sqlalchemy import create_engine, text
from neo4j import GraphDatabase
import qdrant_client
from kafka import KafkaProducer, KafkaConsumer

@dataclass
class CDCConfig:
    """Configuration for change data capture"""

    # PostgreSQL connection
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_database: str = "multimodal_rag_analytics"
    pg_user: str = "analytics_role"
    pg_password: str = "secure_password"

    # Neo4j connection
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    # Qdrant connection
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333

    # Kafka configuration
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_prefix: str = "rag-analytics"

class DataSynchronizer:
    """Handles data synchronization between systems"""

    def __init__(self, config: CDCConfig):
        self.config = config
        self.pg_engine = create_engine(
            f"postgresql://{config.pg_user}:{config.pg_password}@{config.pg_host}:{config.pg_port}/{config.pg_database}",
            pool_size=20,
            max_overflow=30
        )
        self.neo4j_driver = GraphDatabase.driver(
            config.neo4j_uri,
            auth=(config.neo4j_user, config.neo4j_password)
        )
        self.qdrant_client = qdrant_client.QdrantClient(
            host=config.qdrant_host,
            port=config.qdrant_port
        )
        self.kafka_producer = KafkaProducer(
            bootstrap_servers=config.kafka_bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode('utf-8')
        )

    async def sync_neo4j_entities(self):
        """Sync entity data from Neo4j to PostgreSQL"""

        sync_query = """
        MATCH (e:Entity)
        WHERE e.last_updated >= timestamp() - duration({hours: 1})
        RETURN e
        """

        with self.neo4j_driver.session() as session:
            result = session.run(sync_query)

            for record in result:
                entity = record["e"]

                # Insert or update in PostgreSQL
                with self.pg_engine.connect() as conn:
                    conn.execute(text("""
                        INSERT INTO analytics.entities (entity_id, entity_type, properties, organization_id, created_at, updated_at)
                        VALUES (:entity_id, :entity_type, :properties, :organization_id, :created_at, :updated_at)
                        ON CONFLICT (entity_id) DO UPDATE SET
                            entity_type = EXCLUDED.entity_type,
                            properties = EXCLUDED.properties,
                            updated_at = EXCLUDED.updated_at
                    """), {
                        "entity_id": entity.element_id,
                        "entity_type": list(entity.labels)[0] if entity.labels else "unknown",
                        "properties": dict(entity),
                        "organization_id": entity.get("organization_id"),
                        "created_at": entity.get("created_at"),
                        "updated_at": entity.get("updated_at")
                    })

                    # Emit change event to Kafka
                    self.kafka_producer.send(
                        f"{self.config.kafka_topic_prefix}.entity_changes",
                        {
                            "action": "update",
                            "entity_id": entity.element_id,
                            "entity_type": list(entity.labels)[0] if entity.labels else "unknown",
                            "timestamp": datetime.utcnow().isoformat()
                        }
                    )

    async def sync_qdrant_collections(self):
        """Sync collection statistics from Qdrant to PostgreSQL"""

        collections = self.qdrant_client.get_collections().collections

        for collection in collections:
            collection_info = self.qdrant_client.get_collection(collection.name)

            with self.pg_engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO analytics.vector_collections (collection_name, vectors_count, segments_count,
                                                           index_status, config, organization_id, updated_at)
                    VALUES (:collection_name, :vectors_count, :segments_count,
                           :index_status, :config, :organization_id, :updated_at)
                    ON CONFLICT (collection_name) DO UPDATE SET
                        vectors_count = EXCLUDED.vectors_count,
                        segments_count = EXCLUDED.segments_count,
                        index_status = EXCLUDED.index_status,
                        config = EXCLUDED.config,
                        updated_at = EXCLUDED.updated_at
                """), {
                    "collection_name": collection.name,
                    "vectors_count": collection_info.points_count,
                    "segments_count": len(collection_info.segments),
                    "index_status": "ready" if collection_info.status.value == "green" else "building",
                    "config": collection_info.config.dict(),
                    "organization_id": collection.config.payload.get("organization_id"),
                    "updated_at": datetime.utcnow()
                })
```

### 4.2 Historical Data Migration

```python
# File: data_sync/migration.py
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
from sqlalchemy import create_engine, text
import pandas as pd

class HistoricalDataMigrator:
    """Migrates historical data from existing systems to analytics platform"""

    def __init__(self, pg_engine, neo4j_driver, batch_size: int = 1000):
        self.pg_engine = pg_engine
        self.neo4j_driver = neo4j_driver
        self.batch_size = batch_size
        self.logger = logging.getLogger(__name__)

    async def migrate_historical_queries(self, start_date: datetime, end_date: datetime):
        """Migrate historical search queries"""

        # This would read from existing logs or database
        # For this example, we'll assume we have a CSV with historical data

        historical_data = pd.read_csv("historical_search_queries.csv")

        # Filter by date range
        historical_data['created_at'] = pd.to_datetime(historical_data['created_at'])
        filtered_data = historical_data[
            (historical_data['created_at'] >= start_date) &
            (historical_data['created_at'] <= end_date)
        ]

        # Process in batches
        total_rows = len(filtered_data)
        processed_rows = 0

        for i in range(0, total_rows, self.batch_size):
            batch = filtered_data.iloc[i:i+self.batch_size]

            # Convert to dict records
            records = batch.to_dict('records')

            # Insert into analytics database
            with self.pg_engine.connect() as conn:
                for record in records:
                    conn.execute(text("""
                        INSERT INTO analytics.search_queries
                        (id, user_id, session_id, organization_id, query_text, query_type,
                         query_metadata, result_count, result_ids, result_scores,
                         query_time_ms, vector_search_time_ms, graph_search_time_ms,
                         ranking_time_ms, clicked_result_ids, clicked_positions,
                         user_satisfaction_score, created_at, updated_at)
                        VALUES (:id, :user_id, :session_id, :organization_id, :query_text, :query_type,
                               :query_metadata, :result_count, :result_ids, :result_scores,
                               :query_time_ms, :vector_search_time_ms, :graph_search_time_ms,
                               :ranking_time_ms, :clicked_result_ids, :clicked_positions,
                               :user_satisfaction_score, :created_at, :updated_at)
                        ON CONFLICT (id) DO NOTHING
                    """), record)

            processed_rows += len(batch)
            self.logger.info(f"Processed {processed_rows}/{total_rows} historical queries")

    async def migrate_historical_evaluations(self, start_date: datetime, end_date: datetime):
        """Migrate historical evaluation results"""

        # Similar approach for evaluation data
        pass

    async def validate_data_integrity(self):
        """Validate migrated data integrity"""

        validation_queries = [
            {
                "name": "Check query counts",
                "query": """
                    SELECT COUNT(*) as query_count,
                           COUNT(DISTINCT user_id) as unique_users,
                           COUNT(DISTINCT organization_id) as unique_orgs
                    FROM analytics.search_queries
                """
            },
            {
                "name": "Check evaluation coverage",
                "query": """
                    SELECT COUNT(*) as total_queries,
                           COUNT(e.id) as evaluated_queries,
                           ROUND(COUNT(e.id) * 100.0 / COUNT(sq.id), 2) as coverage_percentage
                    FROM analytics.search_queries sq
                    LEFT JOIN analytics.evaluation_runs e ON sq.id = e.query_id
                """
            },
            {
                "name": "Check data consistency",
                "query": """
                    SELECT COUNT(*) as orphaned_evaluations
                    FROM analytics.evaluation_runs e
                    LEFT JOIN analytics.search_queries sq ON e.query_id = sq.id
                    WHERE sq.id IS NULL
                """
            }
        ]

        with self.pg_engine.connect() as conn:
            for validation in validation_queries:
                result = conn.execute(text(validation["query"]))
                row = result.fetchone()
                self.logger.info(f"{validation['name']}: {dict(row._mapping)}")
```

## 5. Application Integration

### 5.1 Analytics Middleware

```python
# File: analytics/middleware.py
import time
import json
import uuid
from typing import Dict, Any, Optional
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import create_engine, text
from datetime import datetime

class AnalyticsMiddleware(BaseHTTPMiddleware):
    """Middleware to capture analytics data from HTTP requests"""

    def __init__(self, app, pg_engine, enabled: bool = True):
        super().__init__(app)
        self.pg_engine = pg_engine
        self.enabled = enabled

    async def dispatch(self, request: Request, call_next):
        if not self.enabled:
            return await call_next(request)

        start_time = time.time()

        # Extract analytics data from request
        analytics_data = {
            "request_id": str(uuid.uuid4()),
            "method": request.method,
            "path": request.url.path,
            "query_params": dict(request.query_params),
            "user_agent": request.headers.get("user-agent"),
            "ip_address": request.client.host,
            "timestamp": datetime.utcnow().isoformat()
        }

        # Extract user/organization info if available
        if hasattr(request.state, 'user_id'):
            analytics_data["user_id"] = request.state.user_id

        if hasattr(request.state, 'organization_id'):
            analytics_data["organization_id"] = request.state.organization_id

        if hasattr(request.state, 'session_id'):
            analytics_data["session_id"] = request.state.session_id

        try:
            response = await call_next(request)

            # Add response data
            analytics_data.update({
                "status_code": response.status_code,
                "response_time_ms": int((time.time() - start_time) * 1000),
                "response_size": response.headers.get("content-length")
            })

            # Store analytics data asynchronously
            await self.store_analytics_data(analytics_data)

            return response

        except Exception as e:
            # Log error analytics
            analytics_data.update({
                "error": str(e),
                "response_time_ms": int((time.time() - start_time) * 1000),
                "status_code": 500
            })

            await self.store_analytics_data(analytics_data)
            raise

    async def store_analytics_data(self, data: Dict[str, Any]):
        """Store analytics data in PostgreSQL"""

        try:
            with self.pg_engine.connect() as conn:
                # Store in performance logs table
                conn.execute(text("""
                    INSERT INTO analytics.performance_logs
                    (metric_name, metric_category, performance_level, organization_id,
                     value, unit, component, response_time_ms, timestamp, event_metadata)
                    VALUES (:metric_name, :metric_category, :performance_level, :organization_id,
                           :value, :unit, :component, :response_time_ms, :timestamp, :event_metadata)
                """), {
                    "metric_name": "api_response_time",
                    "metric_category": "api",
                    "performance_level": "good" if data["response_time_ms"] < 1000 else "fair",
                    "organization_id": data.get("organization_id"),
                    "value": data["response_time_ms"],
                    "unit": "ms",
                    "component": f"{data['method']} {data['path']}",
                    "response_time_ms": data["response_time_ms"],
                    "timestamp": data["timestamp"],
                    "event_metadata": json.dumps({
                        "request_id": data["request_id"],
                        "method": data["method"],
                        "path": data["path"],
                        "status_code": data["status_code"],
                        "user_agent": data["user_agent"]
                    })
                })

                # Store in request logs table
                conn.execute(text("""
                    INSERT INTO analytics.request_logs
                    (request_id, user_id, session_id, organization_id, method, path,
                     query_params, status_code, response_time_ms, ip_address,
                     user_agent, timestamp)
                    VALUES (:request_id, :user_id, :session_id, :organization_id, :method, :path,
                           :query_params, :status_code, :response_time_ms, :ip_address,
                           :user_agent, :timestamp)
                """), {
                    "request_id": data["request_id"],
                    "user_id": data.get("user_id"),
                    "session_id": data.get("session_id"),
                    "organization_id": data.get("organization_id"),
                    "method": data["method"],
                    "path": data["path"],
                    "query_params": json.dumps(data["query_params"]),
                    "status_code": data["status_code"],
                    "response_time_ms": data["response_time_ms"],
                    "ip_address": data["ip_address"],
                    "user_agent": data["user_agent"],
                    "timestamp": data["timestamp"]
                })

        except Exception as e:
            # Log error but don't fail the request
            print(f"Failed to store analytics data: {e}")
```

### 5.2 Search Analytics Integration

```python
# File: analytics/search_analytics.py
import time
import json
from typing import List, Dict, Any, Optional
from datetime import datetime
from sqlalchemy import create_engine, text

class SearchAnalyticsCollector:
    """Collects analytics data for search operations"""

    def __init__(self, pg_engine):
        self.pg_engine = pg_engine

    def track_search_query(self,
                          query_id: str,
                          user_id: Optional[str],
                          session_id: Optional[str],
                          organization_id: str,
                          query_text: str,
                          query_type: str,
                          query_metadata: Dict[str, Any],
                          results: List[Dict[str, Any]],
                          performance_metrics: Dict[str, int]):
        """Track a search query and its results"""

        try:
            with self.pg_engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO analytics.search_queries
                    (id, user_id, session_id, organization_id, query_text, query_type,
                     query_metadata, result_count, result_ids, result_scores,
                     query_time_ms, vector_search_time_ms, graph_search_time_ms,
                     ranking_time_ms, created_at)
                    VALUES (:id, :user_id, :session_id, :organization_id, :query_text, :query_type,
                           :query_metadata, :result_count, :result_ids, :result_scores,
                           :query_time_ms, :vector_search_time_ms, :graph_search_time_ms,
                           :ranking_time_ms, :created_at)
                """), {
                    "id": query_id,
                    "user_id": user_id,
                    "session_id": session_id,
                    "organization_id": organization_id,
                    "query_text": query_text,
                    "query_type": query_type,
                    "query_metadata": json.dumps(query_metadata),
                    "result_count": len(results),
                    "result_ids": json.dumps([r.get("id") for r in results]),
                    "result_scores": json.dumps([r.get("score") for r in results]),
                    "query_time_ms": performance_metrics.get("total_time_ms", 0),
                    "vector_search_time_ms": performance_metrics.get("vector_search_time_ms"),
                    "graph_search_time_ms": performance_metrics.get("graph_search_time_ms"),
                    "ranking_time_ms": performance_metrics.get("ranking_time_ms"),
                    "created_at": datetime.utcnow()
                })

        except Exception as e:
            print(f"Failed to track search query: {e}")

    def track_query_interaction(self,
                               query_id: str,
                               user_id: Optional[str],
                               interaction_type: str,
                               document_id: Optional[str],
                               position: Optional[int],
                               metadata: Dict[str, Any]):
        """Track user interactions with search results"""

        try:
            with self.pg_engine.connect() as conn:
                # Update the search query with interaction data
                if interaction_type == "click" and document_id and position is not None:
                    conn.execute(text("""
                        UPDATE analytics.search_queries
                        SET clicked_result_ids = COALESCE(clicked_result_ids, '[]'::jsonb) || :document_id::jsonb,
                            clicked_positions = COALESCE(clicked_positions, '[]'::jsonb) || :position::jsonb,
                            updated_at = :updated_at
                        WHERE id = :query_id
                    """), {
                        "query_id": query_id,
                        "document_id": json.dumps(document_id),
                        "position": json.dumps(position),
                        "updated_at": datetime.utcnow()
                    })

                # Store detailed interaction event
                conn.execute(text("""
                    INSERT INTO analytics.search_interactions
                    (query_id, user_id, interaction_type, document_id, position, metadata, timestamp)
                    VALUES (:query_id, :user_id, :interaction_type, :document_id, :position, :metadata, :timestamp)
                """), {
                    "query_id": query_id,
                    "user_id": user_id,
                    "interaction_type": interaction_type,
                    "document_id": document_id,
                    "position": position,
                    "metadata": json.dumps(metadata),
                    "timestamp": datetime.utcnow()
                })

        except Exception as e:
            print(f"Failed to track query interaction: {e}")
```

## 6. Monitoring and Alerting

### 6.1 Performance Monitoring Setup

```python
# File: monitoring/performance_monitor.py
import psutil
import time
from datetime import datetime, timedelta
from typing import Dict, Any
from sqlalchemy import create_engine, text

class PerformanceMonitor:
    """Monitors system performance and stores metrics"""

    def __init__(self, pg_engine, collection_interval: int = 60):
        self.pg_engine = pg_engine
        self.collection_interval = collection_interval
        self.running = False

    async def start_monitoring(self):
        """Start performance monitoring"""
        self.running = True

        while self.running:
            metrics = self.collect_system_metrics()
            self.store_metrics(metrics)
            await asyncio.sleep(self.collection_interval)

    def collect_system_metrics(self) -> Dict[str, Any]:
        """Collect system performance metrics"""

        # CPU metrics
        cpu_percent = psutil.cpu_percent(interval=1)
        cpu_count = psutil.cpu_count()

        # Memory metrics
        memory = psutil.virtual_memory()

        # Disk metrics
        disk = psutil.disk_usage('/')

        # Network metrics
        network = psutil.net_io_counters()

        return {
            "timestamp": datetime.utcnow(),
            "cpu_usage_percent": cpu_percent,
            "cpu_count": cpu_count,
            "memory_total_gb": memory.total / (1024**3),
            "memory_available_gb": memory.available / (1024**3),
            "memory_usage_percent": memory.percent,
            "disk_total_gb": disk.total / (1024**3),
            "disk_free_gb": disk.free / (1024**3),
            "disk_usage_percent": (disk.used / disk.total) * 100,
            "network_bytes_sent": network.bytes_sent,
            "network_bytes_recv": network.bytes_recv,
            "network_packets_sent": network.packets_sent,
            "network_packets_recv": network.packets_recv
        }

    def store_metrics(self, metrics: Dict[str, Any]):
        """Store performance metrics in database"""

        try:
            with self.pg_engine.connect() as conn:
                # CPU metric
                conn.execute(text("""
                    INSERT INTO analytics.performance_logs
                    (metric_name, metric_category, performance_level, value, unit,
                     cpu_usage_percent, timestamp, date_hour, date_day)
                    VALUES (:metric_name, :metric_category, :performance_level, :value, :unit,
                           :cpu_usage_percent, :timestamp, :date_hour, :date_day)
                """), {
                    "metric_name": "system_cpu_usage",
                    "metric_category": "system",
                    "performance_level": self._get_performance_level("cpu", metrics["cpu_usage_percent"]),
                    "value": metrics["cpu_usage_percent"],
                    "unit": "percent",
                    "cpu_usage_percent": metrics["cpu_usage_percent"],
                    "timestamp": metrics["timestamp"],
                    "date_hour": metrics["timestamp"].strftime("%Y-%m-%dT%H"),
                    "date_day": metrics["timestamp"].strftime("%Y-%m-%d")
                })

                # Memory metric
                conn.execute(text("""
                    INSERT INTO analytics.performance_logs
                    (metric_name, metric_category, performance_level, value, unit,
                     memory_usage_mb, memory_usage_percent, timestamp, date_hour, date_day)
                    VALUES (:metric_name, :metric_category, :performance_level, :value, :unit,
                           :memory_usage_mb, :memory_usage_percent, :timestamp, :date_hour, :date_day)
                """), {
                    "metric_name": "system_memory_usage",
                    "metric_category": "system",
                    "performance_level": self._get_performance_level("memory", metrics["memory_usage_percent"]),
                    "value": metrics["memory_usage_percent"],
                    "unit": "percent",
                    "memory_usage_mb": (metrics["memory_total_gb"] - metrics["memory_available_gb"]) * 1024,
                    "memory_usage_percent": metrics["memory_usage_percent"],
                    "timestamp": metrics["timestamp"],
                    "date_hour": metrics["timestamp"].strftime("%Y-%m-%dT%H"),
                    "date_day": metrics["timestamp"].strftime("%Y-%m-%d")
                })

                # Disk metric
                conn.execute(text("""
                    INSERT INTO analytics.performance_logs
                    (metric_name, metric_category, performance_level, value, unit,
                     disk_usage_gb, disk_usage_percent, timestamp, date_hour, date_day)
                    VALUES (:metric_name, :metric_category, :performance_level, :value, :unit,
                           :disk_usage_gb, :disk_usage_percent, :timestamp, :date_hour, :date_day)
                """), {
                    "metric_name": "system_disk_usage",
                    "metric_category": "system",
                    "performance_level": self._get_performance_level("disk", metrics["disk_usage_percent"]),
                    "value": metrics["disk_usage_percent"],
                    "unit": "percent",
                    "disk_usage_gb": metrics["disk_total_gb"] - metrics["disk_free_gb"],
                    "disk_usage_percent": metrics["disk_usage_percent"],
                    "timestamp": metrics["timestamp"],
                    "date_hour": metrics["timestamp"].strftime("%Y-%m-%dT%H"),
                    "date_day": metrics["timestamp"].strftime("%Y-%m-%d")
                })

        except Exception as e:
            print(f"Failed to store performance metrics: {e}")

    def _get_performance_level(self, metric_type: str, value: float) -> str:
        """Determine performance level based on metric value"""

        thresholds = {
            "cpu": {"critical": 90, "poor": 80, "fair": 70},
            "memory": {"critical": 95, "poor": 85, "fair": 75},
            "disk": {"critical": 95, "poor": 90, "fair": 80}
        }

        metric_thresholds = thresholds.get(metric_type, {})

        if value >= metric_thresholds.get("critical", 90):
            return "critical"
        elif value >= metric_thresholds.get("poor", 80):
            return "poor"
        elif value >= metric_thresholds.get("fair", 70):
            return "fair"
        else:
            return "good"
```

### 6.2 Alerting System

```python
# File: monitoring/alerting.py
import smtplib
from email.mime.text import MimeText
from typing import List, Dict, Any
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta

class AlertManager:
    """Manages alerts based on performance metrics"""

    def __init__(self, pg_engine, smtp_config: Dict[str, Any]):
        self.pg_engine = pg_engine
        self.smtp_config = smtp_config

    def check_and_send_alerts(self):
        """Check for conditions that should trigger alerts"""

        alerts = self.check_for_alerts()

        for alert in alerts:
            if self.should_send_alert(alert):
                self.send_alert(alert)
                self.mark_alert_as_sent(alert)

    def check_for_alerts(self) -> List[Dict[str, Any]]:
        """Check for conditions that should trigger alerts"""

        alerts = []

        with self.pg_engine.connect() as conn:
            # Check for critical performance issues
            critical_metrics = conn.execute(text("""
                SELECT metric_name, component, value, timestamp, AVG(value) OVER (
                    PARTITION BY metric_name, component
                    ORDER BY timestamp
                    RANGE BETWEEN INTERVAL '1 hour' PRECEDING AND CURRENT ROW
                ) as avg_value
                FROM analytics.performance_logs
                WHERE timestamp >= NOW() - INTERVAL '1 hour'
                  AND performance_level = 'critical'
                  AND alert_triggered = false
                GROUP BY metric_name, component, value, timestamp
            """)).fetchall()

            for metric in critical_metrics:
                alerts.append({
                    "type": "performance_critical",
                    "metric_name": metric.metric_name,
                    "component": metric.component,
                    "value": metric.value,
                    "avg_value": metric.avg_value,
                    "timestamp": metric.timestamp
                })

            # Check for error spikes
            error_spike = conn.execute(text("""
                SELECT component, COUNT(*) as error_count,
                       AVG(CASE WHEN alert_triggered THEN 1 ELSE 0 END) as historical_avg
                FROM analytics.error_logs
                WHERE occurred_at >= NOW() - INTERVAL '1 hour'
                GROUP BY component
                HAVING COUNT(*) > 10  -- More than 10 errors in last hour
                   AND COUNT(*) > COALESCE(
                       AVG(CASE WHEN alert_triggered THEN 1 ELSE 0 END) * 5, 5
                   )
            """)).fetchall()

            for spike in error_spike:
                alerts.append({
                    "type": "error_spike",
                    "component": spike.component,
                    "error_count": spike.error_count,
                    "historical_avg": spike.historical_avg
                })

            # Check for RAG evaluation failures
            evaluation_failures = conn.execute(text("""
                SELECT COUNT(*) as failure_count,
                       AVG(overall_score) as avg_score
                FROM analytics.evaluation_runs
                WHERE created_at >= NOW() - INTERVAL '1 hour'
                  AND meets_thresholds = false
            """)).fetchone()

            if evaluation_failures and evaluation_failures.failure_count > 5:
                alerts.append({
                    "type": "evaluation_failures",
                    "failure_count": evaluation_failures.failure_count,
                    "avg_score": evaluation_failures.avg_score
                })

        return alerts

    def should_send_alert(self, alert: Dict[str, Any]) -> bool:
        """Determine if an alert should be sent"""

        # Check if similar alert was sent recently
        with self.pg_engine.connect() as conn:
            recent_alert = conn.execute(text("""
                SELECT COUNT(*) as count
                FROM analytics.sent_alerts
                WHERE alert_type = :alert_type
                  AND component = :component
                  AND sent_at >= NOW() - INTERVAL '1 hour'
            """), {
                "alert_type": alert["type"],
                "component": alert.get("component", "system")
            }).fetchone()

            return recent_alert.count == 0

    def send_alert(self, alert: Dict[str, Any]):
        """Send alert notification"""

        subject = f"RAG System Alert: {alert['type'].replace('_', ' ').title()}"

        if alert["type"] == "performance_critical":
            message = f"""
            Critical performance issue detected:

            Component: {alert['component']}
            Metric: {alert['metric_name']}
            Current Value: {alert['value']}
            Average Value (1h): {alert['avg_value']}
            Timestamp: {alert['timestamp']}

            Please investigate immediately.
            """
        elif alert["type"] == "error_spike":
            message = f"""
            Error spike detected:

            Component: {alert['component']}
            Error Count (1h): {alert['error_count']}
            Historical Average: {alert['historical_avg']}

            Please investigate the cause of increased errors.
            """
        elif alert["type"] == "evaluation_failures":
            message = f"""
            RAG evaluation failures detected:

            Failed Evaluations (1h): {alert['failure_count']}
            Average Score: {alert['avg_score']}

            Quality metrics are below thresholds. Please review.
            """
        else:
            message = f"Alert: {alert}"

        # Send email
        try:
            msg = MimeText(message)
            msg['Subject'] = subject
            msg['From'] = self.smtp_config['from']
            msg['To'] = ', '.join(self.smtp_config['to'])

            with smtplib.SMTP(
                self.smtp_config['host'],
                self.smtp_config['port']
            ) as server:
                if self.smtp_config.get('use_tls'):
                    server.starttls()
                if self.smtp_config.get('username'):
                    server.login(
                        self.smtp_config['username'],
                        self.smtp_config['password']
                    )
                server.send_message(msg)

        except Exception as e:
            print(f"Failed to send alert email: {e}")

    def mark_alert_as_sent(self, alert: Dict[str, Any]):
        """Mark alert as sent to prevent duplicate notifications"""

        try:
            with self.pg_engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO analytics.sent_alerts
                    (alert_type, component, alert_data, sent_at)
                    VALUES (:alert_type, :component, :alert_data, :sent_at)
                """), {
                    "alert_type": alert["type"],
                    "component": alert.get("component", "system"),
                    "alert_data": json.dumps(alert),
                    "sent_at": datetime.utcnow()
                })

        except Exception as e:
            print(f"Failed to mark alert as sent: {e}")
```

This comprehensive migration strategy provides:

1. **Phased approach** with clear milestones and risk mitigation
2. **Database setup** with optimized PostgreSQL configuration
3. **Automated migration scripts** for schema creation and data transfer
4. **Change data capture** for real-time synchronization
5. **Application integration** with middleware and analytics collectors
6. **Monitoring and alerting** for proactive issue detection
7. **Data validation** to ensure migration integrity

The migration strategy ensures minimal disruption to the existing RAG system while adding powerful analytics capabilities for monitoring, evaluation, and optimization.
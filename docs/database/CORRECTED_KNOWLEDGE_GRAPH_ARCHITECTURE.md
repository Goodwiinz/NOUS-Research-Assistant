# Corrected Knowledge Graph Database Architecture

> **Stale / historical (2026-10-17):** Qdrant is fully removed and live retrieval is PostgreSQL full-text search (`DO_KB_ENABLED` defaults to `False`; pgvector semantic search is flag-gated). Treat Qdrant, DO KB and vector-store statements below as historical. Current contracts: [`docs/engineering/`](../engineering/README.md).

## Executive Summary

This document describes the corrected database architecture for the Knowledge Graph functionality that resolves the critical architectural violation where graph algorithms were incorrectly placed in frontend tasks. The new design implements proper backend-first processing with clear separation of concerns.

## Critical Issues Identified

### Current Architectural Violations
1. **Frontend Graph Processing**: Components like `GraphCentralityMetrics.tsx` and `GraphInsightsDashboard.tsx` implement complex graph algorithms (centrality, betweenness, PageRank) in JavaScript
2. **Backend API Limitations**: Current knowledge graph API lacks comprehensive algorithm endpoints
3. **Data Model Inconsistencies**: Missing proper integration between PostgreSQL metadata and Neo4j graph data
4. **Performance Issues**: Real-time graph calculations on frontend cause poor UX
5. **Scalability Problems**: Frontend cannot handle large-scale graph processing

### Architecture Requirements
- **FR-006**: Interactive knowledge graph visualization (backend processing, frontend visualization)
- **FR-007**: Graph navigation with detailed entity information (backend algorithms, frontend UI)
- Multi-tenant data isolation
- Real-time graph updates
- Performance for 500+ node graphs
- Comprehensive graph analytics backend

## Corrected Architecture Overview

### Database Responsibilities
- **Neo4j**: Graph structure storage and native graph algorithms
- **PostgreSQL**: Metadata, analytics results, user preferences, operation tracking
- **Qdrant**: Vector embeddings for semantic similarity
- **Redis**: Caching and real-time data

### Backend Services Architecture
```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Graph API     │    │ Analytics API   │    │ Visualization  │
│   Controller    │    │   Controller    │    │ API Controller  │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│ Graph Service   │    │Analytics Engine │    │ Layout Service  │
│ - CRUD Ops      │    │ - Centrality    │    │ - Force Layout  │
│ - Traversal     │    │ - Pathfinding   │    │ - Hierarchical  │
│ - Search        │    │ - Clustering    │    │ - Circular      │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Database Layer                               │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────┐  │
│  │    Neo4j    │ │ PostgreSQL  │ │   Qdrant    │ │  Redis  │  │
│  │   Graph     │ │  Metadata   │ │ Embeddings  │ │  Cache  │  │
│  └─────────────┘ └─────────────┘ └─────────────┘ └─────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## 1. Neo4j Graph Schema (Corrected)

### Enhanced Node Structure
```cypher
// Core entity nodes with comprehensive properties
CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE;
CREATE CONSTRAINT document_id_unique IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE;
CREATE CONSTRAINT organization_id_unique IF NOT EXISTS FOR (o:Organization) REQUIRE o.id IS UNIQUE;

// Entity nodes with full analytics support
(:Entity {
    id: String,                           // UUID
    name: String,                         // Display name
    canonical_name: String,               // Standardized name
    type: String,                         // Entity type
    subtypes: [String],                   // Multiple subtypes
    confidence: Float,                    // 0.0-1.0
    importance_score: Float,              // Calculated importance
    description: String,                  // Entity description
    aliases: [String],                    // Alternative names
    properties: Map,                      // Additional properties
    extraction_method: String,            // How entity was extracted
    extraction_context: String,           // Extraction context
    first_seen: DateTime,                 // First appearance
    last_seen: DateTime,                  // Last appearance
    created_at: DateTime,                 // Creation time
    updated_at: DateTime,                 // Last update
    source_document_ids: [String],        // Source documents
    organization_id: String,              // Multi-tenant isolation
    embeddings_version: String,           // Embedding model version
    graph_metrics: Map                    // Cached graph metrics
})

// Document nodes with multimodal support
(:Document {
    id: String,
    title: String,
    content_type: String,
    processing_status: String,
    entity_count: Integer,
    relationship_count: Integer,
    created_at: DateTime,
    organization_id: String
})

// Organization nodes for multi-tenancy
(:Organization {
    id: String,
    name: String,
    slug: String,
    created_at: DateTime
})
```

### Enhanced Relationship Structure
```cypher
// Comprehensive relationship structure
(:Entity)-[r:RELATED_TO {
    id: String,                           // UUID
    type: String,                         // Relationship type
    subtype: String,                      // Specific subtype
    strength: Float,                      // 0.0-1.0
    confidence: Float,                    // 0.0-1.0
    weight: Float,                        // Algorithm weight
    direction: String,                    // 'bidirectional', 'directed'
    temporal_data: Map,                   // Time-based information
    evidence: [String],                   // Supporting evidence
    context: String,                      // Relationship context
    extraction_method: String,            // Extraction method
    source_document_id: String,           // Source document
    created_at: DateTime,
    updated_at: DateTime,
    organization_id: String,
    analytics: Map                        // Pre-computed analytics
}]->(:Entity)

// Entity-document relationships
(:Entity)-[:EXTRACTED_FROM {
    confidence: Float,
    extraction_method: String,
    position_in_document: Integer,
    context_window: String,
    created_at: DateTime
}]->(:Document)

// Document-document relationships
(:Document)-[:SIMILAR_TO {
    similarity_score: Float,
    shared_entities: [String],
    shared_concepts: [String],
    calculation_method: String,
    created_at: DateTime
}]->(:Document)
```

### Graph Algorithm Support
```cypher
// Centrality and importance calculation properties
(:Entity {
    centrality_metrics: {
        degree: Float,
        betweenness: Float,
        closeness: Float,
        eigenvector: Float,
        pagerank: Float,
        clustering_coefficient: Float
    },
    last_centrality_update: DateTime,
    centrality_version: String
})

// Community and cluster information
(:Entity {
    community_id: String,
    community_role: String,               // 'hub', 'bridge', 'peripheral'
    cluster_id: String,
    cluster_confidence: Float,
    last_cluster_update: DateTime
})

// Path and navigation optimization
(:Entity)-[r:RELATED_TO {
    shortest_path_cache: Map,             // Cached shortest paths
    path_finding_weight: Float,           // Weight for path algorithms
    traversal_cost: Float,                // Cost for traversal algorithms
    last_path_update: DateTime
}]->(:Entity)
```

## 2. PostgreSQL Metadata Schema (Corrected)

### Graph Analytics Tracking
```sql
-- Graph computation results cache
CREATE TABLE graph_computation_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    computation_type VARCHAR(100) NOT NULL, -- 'centrality', 'betweenness', 'clustering'
    computation_version VARCHAR(50) NOT NULL,
    input_parameters JSONB NOT NULL,
    result_data JSONB NOT NULL,
    computation_time_ms INTEGER,
    node_count INTEGER,
    edge_count INTEGER,
    algorithm_used VARCHAR(100),
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(organization_id, computation_type, computation_version, md5(input_parameters::text))
);

-- Real-time graph analytics
CREATE TABLE graph_analytics_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    snapshot_timestamp TIMESTAMPTZ DEFAULT NOW(),

    -- Overall graph metrics
    total_nodes INTEGER,
    total_edges INTEGER,
    graph_density DECIMAL(10,8),
    average_degree DECIMAL(10,4),
    connected_components INTEGER,
    largest_component_size INTEGER,

    -- Quality metrics
    average_confidence DECIMAL(5,4),
    high_quality_nodes INTEGER,            -- confidence > 0.8
    low_quality_nodes INTEGER,             -- confidence < 0.5

    -- Entity type distribution
    entity_type_distribution JSONB,
    relationship_type_distribution JSONB,

    -- Performance metrics
    centrality_computation_time_ms INTEGER,
    clustering_computation_time_ms INTEGER,
    pathfinding_computation_time_ms INTEGER,

    -- Change tracking
    nodes_added_since_last_snapshot INTEGER,
    nodes_removed_since_last_snapshot INTEGER,
    edges_added_since_last_snapshot INTEGER,
    edges_removed_since_last_snapshot INTEGER
);

-- User-specific graph preferences and views
CREATE TABLE user_graph_preferences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    organization_id UUID NOT NULL REFERENCES organizations(id),

    -- Visualization preferences
    default_layout_algorithm VARCHAR(50) DEFAULT 'force',
    node_color_scheme VARCHAR(50) DEFAULT 'type_based',
    edge_color_scheme VARCHAR(50) DEFAULT 'type_based',
    show_labels BOOLEAN DEFAULT true,
    label_threshold INTEGER DEFAULT 50,    -- Show labels for graphs with < 50 nodes

    -- Filter preferences
    default_entity_types TEXT[],
    default_relationship_types TEXT[],
    confidence_threshold DECIMAL(5,4) DEFAULT 0.5,
    strength_threshold DECIMAL(5,4) DEFAULT 0.3,

    -- Analytics preferences
    preferred_centrality_metric VARCHAR(50) DEFAULT 'degree',
    show_community_clusters BOOLEAN DEFAULT true,
    show_isolated_nodes BOOLEAN DEFAULT false,

    -- Performance preferences
    max_nodes_for_realtime INTEGER DEFAULT 500,
    animation_enabled BOOLEAN DEFAULT true,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(user_id, organization_id)
);

-- Graph computation job tracking
CREATE TABLE graph_computation_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    user_id UUID REFERENCES users(id),

    job_type VARCHAR(100) NOT NULL,        -- 'centrality_analysis', 'clustering', 'pathfinding'
    algorithm_name VARCHAR(100) NOT NULL,  -- 'pagerank', 'louvain', 'dijkstra'
    input_parameters JSONB NOT NULL,

    status VARCHAR(50) DEFAULT 'pending',  -- 'pending', 'running', 'completed', 'failed'
    progress_percentage INTEGER DEFAULT 0,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    estimated_completion TIMESTAMPTZ,

    -- Resource usage
    cpu_time_ms INTEGER,
    memory_used_mb INTEGER,
    nodes_processed INTEGER,

    error_message TEXT,
    result_location VARCHAR(500),          -- S3 path or similar

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Entity-specific analytics cache
CREATE TABLE entity_analytics_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL,
    organization_id UUID NOT NULL REFERENCES organizations(id),

    -- Centrality metrics
    degree_centrality DECIMAL(15,8),
    betweenness_centrality DECIMAL(15,8),
    closeness_centrality DECIMAL(15,8),
    eigenvector_centrality DECIMAL(15,8),
    pagerank_score DECIMAL(15,8),

    -- Local structure
    clustering_coefficient DECIMAL(10,8),
    neighbor_count INTEGER,
    ego_network_density DECIMAL(10,8),

    -- Role in network
    is_bridge_node BOOLEAN,
    is_peripheral BOOLEAN,
    community_id VARCHAR(100),
    community_importance DECIMAL(5,4),

    -- Temporal metrics
    importance_trend JSONB,                -- Historical importance scores
    connection_velocity DECIMAL(10,4),     -- Rate of new connections

    -- Computation metadata
    computed_at TIMESTAMPTZ DEFAULT NOW(),
    algorithm_version VARCHAR(50),
    computation_time_ms INTEGER,

    UNIQUE(entity_id, organization_id)
);

-- Graph insights and recommendations
CREATE TABLE graph_insights (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),

    insight_type VARCHAR(100) NOT NULL,    -- 'centrality_anomaly', 'community_shift', 'quality_issue'
    severity VARCHAR(20) DEFAULT 'medium', -- 'low', 'medium', 'high', 'critical'
    title VARCHAR(500) NOT NULL,
    description TEXT NOT NULL,

    -- Quantitative metrics
    impact_score DECIMAL(5,4),             -- 0.0-1.0
    confidence DECIMAL(5,4),               -- 0.0-1.0

    -- Context
    affected_entity_ids UUID[],
    affected_relationship_ids UUID[],
    graph_snapshot_before JSONB,
    graph_snapshot_after JSONB,

    -- Recommendations
    recommended_actions JSONB,
    action_priority INTEGER,

    -- Metadata
    generated_by VARCHAR(100),             -- 'system', 'user', 'ml_model'
    ml_model_version VARCHAR(50),
    expires_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    acknowledged_by_user_id UUID REFERENCES users(id),
    acknowledged_at TIMESTAMPTZ,
    resolved BOOLEAN DEFAULT FALSE
);
```

### Enhanced Entity Metadata
```sql
-- Enhanced entities table with graph integration
ALTER TABLE entities ADD COLUMN IF NOT EXISTS
    graph_node_id VARCHAR(255),
    importance_score DECIMAL(5,4),
    centrality_metrics JSONB,
    community_info JSONB,
    graph_analytics_version VARCHAR(50),
    last_graph_update TIMESTAMPTZ;

-- Enhanced entity relationships table
ALTER TABLE entity_relationships ADD COLUMN IF NOT EXISTS
    graph_relationship_id VARCHAR(255),
    pathfinding_weight DECIMAL(5,4),
    graph_analytics JSONB,
    last_graph_update TIMESTAMPTZ;
```

## 3. Qdrant Collections (Corrected)

### Entity Embeddings Collection
```json
{
  "collection_name": "entity_embeddings",
  "vectors": {
    "size": 1536,
    "distance": "Cosine"
  },
  "payload_schema": {
    "entity_id": "keyword",
    "entity_name": "text",
    "entity_type": "keyword",
    "organization_id": "keyword",
    "confidence": "float",
    "extraction_method": "keyword",
    "document_context": "text",
    "created_at": "integer",
    "embedding_model": "keyword",
    "embedding_version": "keyword"
  }
}
```

### Relationship Embeddings Collection
```json
{
  "collection_name": "relationship_embeddings",
  "vectors": {
    "size": 768,
    "distance": "Cosine"
  },
  "payload_schema": {
    "relationship_id": "keyword",
    "source_entity_id": "keyword",
    "target_entity_id": "keyword",
    "relationship_type": "keyword",
    "organization_id": "keyword",
    "strength": "float",
    "confidence": "float",
    "context_embedding": "keyword",
    "created_at": "integer",
    "embedding_model": "keyword"
  }
}
```

### Graph Context Embeddings Collection
```json
{
  "collection_name": "graph_context_embeddings",
  "vectors": {
    "size": 1024,
    "distance": "Cosine"
  },
  "payload_schema": {
    "context_id": "keyword",
    "context_type": "keyword",              // "neighborhood", "path", "community"
    "entity_ids": "keyword",               // Array of entity IDs
    "organization_id": "keyword",
    "context_radius": "integer",            // For neighborhood contexts
    "path_length": "integer",               // For path contexts
    "community_id": "keyword",              // For community contexts
    "created_at": "integer",
    "embedding_model": "keyword"
  }
}
```

## 4. Redis Caching Strategy (Corrected)

### Graph Data Structures
```
# Entity centrality scores cache
entity:centrality:{entity_id}:{metric_type} -> Float
TTL: 3600 seconds (1 hour)

# Graph layout positions cache
graph:layout:{graph_id}:{algorithm} -> Hash {
  entity_id: {x: Float, y: Float, level: Integer}
}
TTL: 7200 seconds (2 hours)

# Shortest path cache
paths:shortest:{source_id}:{target_id} -> List[entity_ids]
TTL: 1800 seconds (30 minutes)

# Community detection results
communities:{organization_id}:{algorithm} -> Hash {
  community_id: [entity_ids]
}
TTL: 86400 seconds (24 hours)

# Real-time graph statistics
stats:graph:{organization_id}:realtime -> Hash {
  total_nodes: Integer,
  total_edges: Integer,
  avg_degree: Float,
  last_updated: timestamp
}
TTL: 300 seconds (5 minutes)

# User-specific view cache
view:graph:{user_id}:{view_hash} -> JSON {
  nodes: [...],
  edges: [...],
  layout: {...},
  filters: {...}
}
TTL: 1800 seconds (30 minutes)

# Computation job status
job:graph:{job_id} -> Hash {
  status: String,
  progress: Integer,
  result_location: String,
  error: String
}
TTL: 86400 seconds (24 hours)

# Entity neighborhood cache
neighborhood:{entity_id}:{depth}:{organization_id} -> JSON {
  entities: [...],
  relationships: [...],
  computed_at: timestamp
}
TTL: 3600 seconds (1 hour)

# Graph insights cache
insights:{organization_id}:{category} -> JSON {
  insights: [...],
  generated_at: timestamp,
  expires_at: timestamp
}
TTL: 21600 seconds (6 hours)
```

### Real-time Update Channels
```
# Pub/Sub channels for real-time updates
channel:graph:updates:{organization_id}
channel:entity:created:{organization_id}
channel:entity:updated:{organization_id}
channel:entity:deleted:{organization_id}
channel:relationship:created:{organization_id}
channel:relationship:updated:{organization_id}
channel:computation:completed:{organization_id}
```

## 5. Backend API Endpoints (Corrected)

### Graph Analytics API
```python
# Centrality computation endpoints
POST /api/v1/knowledge-graph/analytics/centrality
{
  "algorithm": "pagerank",  # degree, betweenness, closeness, eigenvector, pagerank
  "entity_types": ["person", "organization"],
  "filters": {...},
  "weights": {...}
}
-> {
  "computation_id": "uuid",
  "estimated_time": 45,
  "status": "queued"
}

GET /api/v1/knowledge-graph/analytics/centrality/{computation_id}
-> {
  "status": "completed",
  "results": [
    {
      "entity_id": "uuid",
      "entity_name": "John Doe",
      "centrality_score": 0.85,
      "rank": 1,
      "percentile": 99
    }
  ],
  "computation_time_ms": 1234
}

# Community detection
POST /api/v1/knowledge-graph/analytics/communities
{
  "algorithm": "louvain",  # louvain, label_propagation, leiden
  "resolution": 1.0,
  "min_community_size": 5
}
-> {
  "communities": [
    {
      "community_id": "c1",
      "entities": [...],
      "modularity_score": 0.72,
      "dominant_entity_type": "person"
    }
  ]
}

# Pathfinding algorithms
POST /api/v1/knowledge-graph/analytics/paths
{
  "source_entity_id": "uuid",
  "target_entity_id": "uuid",
  "algorithm": "dijkstra",  # dijkstra, bfs, astar
  "max_depth": 5,
  "weight_property": "strength"
}
-> {
  "paths": [
    {
      "path": [entity_ids],
      "total_weight": 0.85,
      "path_length": 3,
      "entities": [...],
      "relationships": [...]
    }
  ]
}

# Graph insights and recommendations
GET /api/v1/knowledge-graph/insights
{
  "categories": ["quality", "connectivity", "performance"],
  "severity": ["high", "medium"],
  "limit": 20
}
-> {
  "insights": [
    {
      "id": "uuid",
      "type": "quality_issue",
      "severity": "high",
      "title": "Low confidence entities detected",
      "description": "15 entities have confidence below 0.5",
      "affected_entities": [...],
      "recommendations": [...],
      "impact_score": 0.8
    }
  ]
}
```

### Visualization API
```python
# Layout computation
POST /api/v1/knowledge-graph/visualization/layout
{
  "entities": [entity_ids],
  "algorithm": "force",  # force, circular, hierarchical, grid
  "options": {
    "iterations": 1000,
    "gravity": 0.1,
    "link_distance": 100
  }
}
-> {
  "layout_id": "uuid",
  "positions": {
    "entity_id": {"x": 100, "y": 200, "level": 1}
  },
  "bounds": {"min_x": 0, "max_x": 800, "min_y": 0, "max_y": 600}
}

# Neighborhood visualization
GET /api/v1/knowledge-graph/visualization/neighborhood/{entity_id}
{
  "depth": 2,
  "max_nodes": 50,
  "include_layout": true,
  "layout_algorithm": "force"
}
-> {
  "central_entity": {...},
  "entities": [...],
  "relationships": [...],
  "layout": {...},
  "metadata": {
    "total_nodes": 45,
    "max_depth_reached": 2,
    "computation_time_ms": 234
  }
}

# Real-time graph updates
WebSocket /ws/knowledge-graph/updates/{organization_id}
-> {
  "type": "entity_created",
  "data": {...},
  "timestamp": "2024-01-01T12:00:00Z"
}
```

## 6. Performance Optimization Strategies

### Neo4j Optimizations
1. **Composite Indexes**: For multi-property queries
2. **Native Graph Algorithms**: Use Neo4j Graph Data Science library
3. **Lazy Loading**: Load graph neighborhoods on demand
4. **Result Caching**: Cache computation results in Redis
5. **Query Optimization**: Use EXPLAIN and PROFILE for query tuning

### PostgreSQL Optimizations
1. **Partitioning**: Partition large tables by organization_id
2. **Materialized Views**: For frequently accessed aggregations
3. **Connection Pooling**: PgBouncer for high concurrency
4. **Read Replicas**: For analytics queries
5. **Partial Indexes**: For common filter combinations

### Qdrant Optimizations
1. **Collection Sharding**: For large-scale embeddings
2. **Quantization**: Reduce memory usage for embeddings
3. **Batch Operations**: Group embedding updates
4. **Metadata Filtering**: Efficient payload-based filtering
5. **HNSW Index Optimization**: Tune HNSW parameters

### Redis Optimizations
1. **Memory Management**: Use appropriate TTL values
2. **Pipeline Operations**: Batch Redis commands
3. **Cluster Setup**: For high availability
4. **Compression**: Compress large cached objects
5. **Ephemeral Storage**: For temporary computation data

### Integration Patterns
1. **Event-Driven Updates**: Use message queues for async processing
2. **Circuit Breakers**: Handle database failures gracefully
3. **Retry Logic**: For transient failures
4. **Bulk Operations**: Batch graph updates
5. **Async Processing**: Background jobs for heavy computations

## 7. Migration Strategy

### Phase 1: Backend Infrastructure
1. Implement corrected database schemas
2. Create comprehensive API endpoints
3. Set up caching and optimization layers
4. Implement graph algorithm services

### Phase 2: Data Migration
1. Migrate existing graph data to new schemas
2. Compute and cache initial analytics
3. Validate data integrity and performance
4. Update API documentation

### Phase 3: Frontend Migration
1. Remove graph algorithm logic from frontend
2. Implement API integration for graph data
3. Update visualization components
4. Add real-time update handling

### Phase 4: Testing & Optimization
1. Performance testing with large graphs
2. Load testing for concurrent users
3. Optimization based on metrics
4. Documentation and training

## 8. Monitoring and Observability

### Key Metrics
- Graph computation performance (latency, throughput)
- Database query performance
- Cache hit rates
- Real-time update latency
- User interaction metrics

### Alerting
- Slow graph computations (>10s)
- High memory usage
- Database connection issues
- Cache failures
- API error rates

### Health Checks
- Database connectivity
- Algorithm service availability
- Cache performance
- API response times
- End-to-end functionality

This corrected architecture resolves the critical architectural violation by moving all graph processing to backend services while maintaining rich frontend visualization capabilities. The design ensures scalability, performance, and maintainability for enterprise-scale knowledge graph operations.
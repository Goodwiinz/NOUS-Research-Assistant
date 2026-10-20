# Comprehensive Data Model Architecture
## Multimodal Enterprise RAG System

This document provides a complete overview of the data model architecture for the Multimodal Enterprise RAG System, designed to support individual user accounts with private document storage, multimodal file processing, natural language querying with RAG, knowledge graph exploration, and performance evaluation metrics.

### Table of Contents
1. [Core Architecture Principles](#core-architecture-principles)
2. [Multi-Tenant Data Isolation](#multi-tenant-data-isolation)
3. [Entity Relationships](#entity-relationships)
4. [Database Storage Strategy](#database-storage-strategy)
5. [Data Flow Architecture](#data-flow-architecture)
6. [Performance Optimizations](#performance-optimizations)
7. [Security and Access Control](#security-and-access-control)
8. [Scaling Considerations](#scaling-considerations)

---

## Core Architecture Principles

### 1. Multi-Tenancy by Design
- **Organization-based isolation**: All data is scoped to organizations
- **User-level boundaries**: Individual users have private document storage
- **Row-level security**: Database queries automatically filter by organization/user

### 2. Evaluation-First Architecture
- **Metrics collection**: All operations generate comprehensive metrics
- **Quality tracking**: Built-in quality scores for documents, queries, and entities
- **Performance monitoring**: Real-time performance dashboards and alerting

### 3. Multimodal Content Support
- **Unified document model**: Single model supports text, images, audio, video
- **Content extraction**: Structured metadata for all modalities
- **Cross-modal relationships**: Links between different content types

### 4. Knowledge Graph Integration
- **Entity extraction**: Automatic identification of named entities
- **Relationship tracking**: Entity relationships with confidence scores
- **Graph analytics**: Centrality, community detection, and importance scoring

---

## Multi-Tenant Data Isolation

### Organization Scoping
All major entities include `organization_id` for tenant isolation:
```sql
-- Example: Document isolation
CREATE INDEX idx_documents_org_tenant ON enhanced_documents(organization_id);
CREATE INDEX idx_queries_org_tenant ON rag_queries(organization_id);
CREATE INDEX idx_entities_org_tenant ON knowledge_entities(organization_id);
```

### User-Level Privacy
- **Private document storage**: Users have isolated document collections
- **Personal query history**: 30-day retention with user-scoped access
- **Individual quotas**: Per-user storage and usage limits

### Data Access Patterns
```python
# Example: Query isolation in application layer
def get_user_documents(user_id: UUID, organization_id: UUID):
    return EnhancedDocument.query.filter(
        EnhancedDocument.organization_id == organization_id,
        EnhancedDocument.uploaded_by_user_id == user_id
    ).all()
```

---

## Entity Relationships

### Core Entity Diagram

```
Organization (1) -----> (N) User
    |                        |
    |                        +-----> (N) EnhancedDocument
    |                        |              |
    |                        |              +-----> (1) UserQuota
    |                        |              |
    |                        |              +-----> (N) ProcessingJob
    |                        |              |
    |                        |              +-----> (N) DocumentQualityMetrics
    |                        |
    |                        +-----> (N) RAGQuery
    |                        |              |
    |                        |              +-----> (N) RAGQualityMetrics
    |                        |
    |                        +-----> (N) KnowledgeEntity
    |                                       |
    |                                       +-----> (N) EntityRelationship
    |                                       |
    |                                       +-----> (N) EntityDocumentMention
    |
    +-----> (N) WebSocketConnection
    |              |
    |              +-----> (N) StatusUpdate
    |
    +-----> (N) EvaluationRun
                   |
                   +-----> (N) MetricMeasurement
                   |
                   +-----> (N) MetricAggregation
```

### Relationship Types

#### 1. Document-Entity Relationships
```sql
-- Documents contain entities
CREATE TABLE entity_document_mentions (
    entity_id UUID REFERENCES knowledge_entities(id),
    document_id UUID REFERENCES enhanced_documents(id),
    mention_text TEXT,
    mention_start INTEGER,
    mention_end INTEGER,
    confidence FLOAT
);
```

#### 2. User-Document Ownership
```sql
-- Users own and upload documents
CREATE TABLE enhanced_documents (
    id UUID PRIMARY KEY,
    organization_id UUID REFERENCES organizations(id),
    uploaded_by_user_id UUID REFERENCES users(id),
    owner_user_id UUID REFERENCES users(id),
    -- Document fields
);
```

#### 3. Query-Document References
```sql
-- Queries reference documents for answers
CREATE TABLE rag_queries (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    answer_sources JSON, -- Document references
    context_snippets JSON, -- Text contexts used
);
```

#### 4. Entity-Entity Relationships
```sql
-- Entities are related to each other
CREATE TABLE entity_relationships (
    source_entity_id UUID REFERENCES knowledge_entities(id),
    target_entity_id UUID REFERENCES knowledge_entities(id),
    relationship_type VARCHAR(50),
    confidence FLOAT,
    valid_from TIMESTAMP,
    valid_to TIMESTAMP
);
```

---

## Database Storage Strategy

### Primary Database: PostgreSQL
**Purpose**: Structured data, relationships, and full-text search
- **Documents**: Enhanced multimodal document metadata
- **Users**: Authentication, preferences, quotas
- **Queries**: RAG query history and answers
- **Entities**: Knowledge graph entities and relationships
- **Metrics**: Performance and quality metrics

### Vector Store: historical (Qdrant removed)
> Amendment 2026-10-17: Qdrant has been removed. Live retrieval is PostgreSQL full-text search; an optional pgvector `document_embeddings` table is gated by `SEMANTIC_SEARCH_ENABLED` / `DOCUMENT_EMBEDDINGS_ENABLED` (default off). The list below is the original design intent, not the running system.

**Purpose (original)**: Semantic similarity search
- **Document embeddings**: Multimodal content vectors
- **Query embeddings**: Natural language query vectors
- **Entity embeddings**: Entity semantic representations

### Graph Database: Neo4j
**Purpose**: Knowledge graph traversal and analytics
- **Entity nodes**: All extracted entities
- **Relationship edges**: Entity relationships
- **Graph algorithms**: Centrality, community detection, path finding

### Cache: Redis
**Purpose**: Real-time data and session management
- **User sessions**: Authentication and preferences
- **Query cache**: Frequently asked questions
- **Processing status**: Real-time job updates

### Storage: File System/Object Storage
**Purpose**: Original file storage
- **Document files**: Original uploaded files
- **Processed content**: Extracted images, audio, video
- **Backups**: Document and data backups

---

## Data Flow Architecture

### 1. Document Upload Pipeline
```
User Upload → File Validation → Storage → Metadata Creation → Processing Queue
    ↓
Background Processing → Text Extraction → Entity Extraction → Embedding Generation
    ↓
Indexing → Knowledge Graph Update → Quality Assessment → User Notification
```

### 2. Query Processing Pipeline
```
User Query → Query Analysis → Entity Recognition → Vector Search
    ↓
Hybrid Search (Vector + Graph + Keyword) → Context Assembly → RAG Generation
    ↓
Answer Generation → Quality Scoring → User Display → Feedback Collection
```

### 3. Entity Extraction Pipeline
```
Document Content → NLP Processing → Entity Extraction → Relationship Detection
    ↓
Confidence Scoring → Knowledge Graph Integration → Entity Resolution
    ↓
Quality Assessment → User Review → Knowledge Graph Update
```

### 4. Metrics Collection Pipeline
```
System Events → Metric Collection → Aggregation → Storage
    ↓
Real-time Dashboards → Alerting → Historical Analysis → Reporting
```

---

## Performance Optimizations

### 1. Indexing Strategy
**Primary Access Patterns:**
```sql
-- User document access
CREATE INDEX idx_documents_org_user ON enhanced_documents(organization_id, uploaded_by_user_id);

-- Full-text search
CREATE INDEX idx_documents_search ON enhanced_documents USING gin(search_vector);

-- Entity lookup
CREATE INDEX idx_entities_name_type ON knowledge_entities(canonical_name, entity_type);

-- Query performance
CREATE INDEX idx_queries_user_session ON rag_queries(user_id, session_id);
```

### 2. Partitioning Strategy
**Time-based Partitioning:**
```sql
-- Query retention (30 days)
CREATE TABLE rag_queries_y2024m01 PARTITION OF rag_queries
FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');

-- Access logs
CREATE TABLE document_access_logs_y2024w01 PARTITION OF document_access_logs
FOR VALUES FROM ('2024-01-01') TO ('2024-01-08');
```

### 3. Materialized Views
**Performance Critical Aggregations:**
```sql
-- User quota summary
CREATE MATERIALIZED VIEW user_quota_summary AS
SELECT u.id as user_id, u.organization_id,
       q.storage_used_bytes, q.storage_quota_bytes,
       q.document_count, q.document_quota
FROM users u
JOIN user_quotas q ON u.id = q.user_id;
```

### 4. Caching Strategy
**Multi-Level Caching:**
- **L1 Cache**: Application memory (Redis)
- **L2 Cache**: Database query cache
- **L3 Cache**: CDN for static content
- **L4 Cache**: Browser cache for UI assets

---

## Security and Access Control

### 1. Authentication & Authorization
```python
# Role-based access control
class UserRole(Enum):
    ADMIN = "admin"           # Full system access
    ANALYST = "analyst"       # Analytics and reporting
    CONTENT_MANAGER = "content_manager"  # Document management
    USER = "user"            # Basic user access

# Permission checking
def check_document_access(user: User, document: EnhancedDocument) -> bool:
    if document.uploaded_by_user_id == user.id:
        return True
    if document.is_public:
        return True
    if document.sharing_level == "organization" and user.organization_id == document.organization_id:
        return True
    return False
```

### 2. Data Encryption
```python
# Sensitive field encryption
class EncryptedUser(BaseModel):
    email = Column(EncryptedString(255))  # Encrypted email
    phone = Column(EncryptedString(20))   # Encrypted phone
    preferences = Column(EncryptedJSON)   # Encrypted preferences
```

### 3. Audit Logging
```sql
-- Comprehensive audit trail
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    organization_id UUID REFERENCES organizations(id),
    action VARCHAR(100),
    resource_type VARCHAR(50),
    resource_id UUID,
    old_values JSON,
    new_values JSON,
    ip_address INET,
    user_agent TEXT,
    timestamp TIMESTAMP DEFAULT NOW()
);
```

### 4. Data Retention Policies
```python
# Automated data cleanup
class DataRetentionPolicy:
    QUERY_RETENTION_DAYS = 30
    LOG_RETENTION_DAYS = 90
    METRICS_RETENTION_DAYS = 365

    def cleanup_expired_queries(self):
        expired_date = datetime.utcnow() - timedelta(days=self.QUERY_RETENTION_DAYS)
        RAGQuery.query.filter(RAGQuery.created_at < expired_date).delete()
```

---

## Scaling Considerations

### 1. Horizontal Scaling
**Database Replication:**
- **Primary**: Write operations
- **Read Replicas**: Analytics and reporting
- **Geographic Replicas**: Global user distribution

**Microservice Architecture:**
- **Document Service**: File processing and storage
- **Query Service**: RAG query processing
- **Entity Service**: Knowledge graph management
- **Metrics Service**: Performance monitoring

### 2. Vertical Scaling
**Resource Allocation:**
```python
# Processing job resource management
class ProcessingJob(BaseModel):
    cpu_cores = Column(Integer, default=2)
    memory_mb = Column(Integer, default=4096)
    gpu_required = Column(Boolean, default=False)

    def allocate_resources(self):
        return ResourcePool.allocate(
            cpu=self.cpu_cores,
            memory=self.memory_mb,
            gpu=self.gpu_required
        )
```

### 3. Load Balancing
**Query Distribution:**
```python
# Load-aware query routing
class QueryRouter:
    def route_query(self, query: RAGQuery):
        if query.query_type == QueryType.MULTIMODAL:
            return "high_performance_pool"
        elif query.complexity == "simple":
            return "standard_pool"
        else:
            return "balanced_pool"
```

### 4. Monitoring & Alerting
**Performance Metrics:**
```python
# Real-time monitoring
class PerformanceMonitor:
    def track_query_performance(self, query_id: UUID):
        metrics = {
            'duration_ms': query.total_duration_ms,
            'tokens_used': query.total_tokens_used,
            'quality_score': query.overall_quality_score
        }
        self.send_to_metrics_system(metrics)
        self.check_performance_thresholds(metrics)
```

---

## Key Design Decisions

### 1. Multi-Modal Document Model
**Decision**: Single unified document model with modality enumeration
**Rationale**: Simplifies queries, enables cross-modal search, maintains consistency

### 2. 30-Day Query Retention
**Decision**: Automatic query cleanup after 30 days
**Rationale**: Balances user privacy with system performance, complies with data regulations

### 3. Knowledge Graph Integration
**Decision**: Native entity and relationship models with Neo4j integration
**Rationale**: Enables complex relationship queries, supports graph analytics

### 4. Evaluation-First Architecture
**Decision**: Comprehensive metrics collection for all operations
**Rationale**: Enables continuous improvement, supports quality assurance

### 5. Real-Time Processing Updates
**Decision**: WebSocket-based status updates with job tracking
**Rationale**: Provides better user experience, enables monitoring

---

## Data Model Summary

### Core Entities
1. **Organization**: Multi-tenant container
2. **User**: Individual user accounts with authentication
3. **EnhancedDocument**: Multimodal document with rich metadata
4. **RAGQuery**: Natural language queries with answers
5. **KnowledgeEntity**: Named entities from documents
6. **EntityRelationship**: Relationships between entities
7. **UserQuota**: Storage and usage limits per user
8. **ProcessingJob**: Background task management
9. **EvaluationMetrics**: Performance and quality tracking

### Key Features
- ✅ **Multi-tenant isolation** with organization scoping
- ✅ **Individual user storage** with 5GB quotas
- ✅ **Multimodal file support** (PDF, TXT, JPG, PNG, MP3, MP4)
- ✅ **Natural language querying** with RAG answers
- ✅ **Knowledge graph exploration** with entities and relationships
- ✅ **Performance evaluation** with comprehensive metrics
- ✅ **Real-time status updates** via WebSocket
- ✅ **30-day query retention** with automatic cleanup
- ✅ **50MB file size limits** with validation
- ✅ **Comprehensive indexing** for performance

This data model architecture provides a solid foundation for building a scalable, performant, and feature-rich Multimodal Enterprise RAG System that meets all the specified requirements.
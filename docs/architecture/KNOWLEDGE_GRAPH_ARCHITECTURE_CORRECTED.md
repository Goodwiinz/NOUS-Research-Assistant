# Knowledge Graph Architecture - Corrected Implementation

> **Stale / historical (2026-10-17):** It describes a design from an earlier infrastructure generation (standalone microservices, Qdrant, multiple environments) that is not what runs today. Only `dev` is live and services run inside the single FastAPI backend. Check the code before relying on any statement here. Current contracts: [`docs/engineering/`](../engineering/README.md).

## Overview

This document describes the corrected Knowledge Graph architecture that resolves the critical architectural violation where graph algorithms were incorrectly placed in the frontend. The new architecture implements a clean separation of concerns with three specialized microservices, each handling specific aspects of knowledge graph functionality.

## Architecture Problem Statement

**Original Issue:**
- Tasks 3.1.1-3.3.5 (graph algorithms) were incorrectly placed in frontend components
- This violated the principle that heavy computational tasks should be handled by backend services
- Frontend was responsible for graph analytics, visualization layout computation, and complex algorithms

**Solution:**
- Complete separation of graph processing from frontend
- Three specialized backend microservices with clear responsibilities
- API-first design with comprehensive OpenAPI specifications
- Performance optimization for large-scale graph operations

## Corrected Architecture Overview

### Service Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           Frontend Application                                  │
│                      (React/Vue/Angular UI)                                    │
└─────────────────────────────────────────────────────────────────────────────────┘
                                             │
                                             │ HTTP/WebSocket
                                             ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        API Gateway / Load Balancer                             │
└─────────────────────────────────────────────────────────────────────────────────┘
                                             │
                    ┌─────────────────┼─────────────────┐
                    │                 │                 │
                    ▼                 ▼                 ▼
    ┌─────────────────────────┐ ┌─────────────────────────┐ ┌─────────────────────────┐
    │ Knowledge Graph Service │ │ Graph Analytics Service │ │ Graph Visualization API │
    │        (Port 8003)       │ │      (Port 8009)       │ │     (Port 8010)        │
    └─────────────────────────┘ └─────────────────────────┘ └─────────────────────────┘
                    │                 │                 │
                    └─────────────────┼─────────────────┘
                                      │
                                      ▼
    ┌─────────────────────────────────────────────────────────────────────────────┐
    │                         Shared Infrastructure                                 │
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                        │
    │  │   Neo4j    │  │ PostgreSQL  │  │   Redis     │                        │
    │  │   Graph    │  │   Metadata  │  │   Cache     │                        │
    │  └─────────────┘  └─────────────┘  └─────────────┘                        │
    └─────────────────────────────────────────────────────────────────────────────┘
```

## Service Specifications

### 1. Knowledge Graph Service (Port 8003)

**Responsibilities:**
- Entity extraction and relationship management
- CRUD operations for entities and relationships
- Multi-tenant security and data isolation
- Integration with document processing pipeline
- Real-time graph updates via WebSocket
- Batch entity and relationship operations

**Key Features:**
- **Entity Extraction**: AI-powered entity extraction from documents
- **Relationship Management**: Create, update, and delete relationships
- **Multi-tenancy**: Complete tenant isolation with RBAC
- **Real-time Updates**: WebSocket notifications for graph changes
- **Batch Operations**: Efficient bulk processing capabilities
- **Search & Discovery**: Advanced entity search and relationship traversal

**API Endpoints:**
```
POST   /entities                    # Create entity
GET    /entities/{id}               # Get entity
PUT    /entities/{id}               # Update entity
DELETE /entities/{id}               # Delete entity
POST   /entities/batch              # Batch operations
POST   /documents/{id}/extract      # Extract entities from document
GET    /entities/search             # Search entities
GET    /entities/{id}/relationships # Get entity relationships
WS     /ws/{tenant_id}              # WebSocket for real-time updates
```

**Performance Targets:**
- Entity CRUD: <200ms
- Batch processing: <10s for 1000 entities
- WebSocket latency: <100ms
- Concurrent users: 50+ with <10% degradation

### 2. Graph Analytics Service (Port 8009)

**Responsibilities:**
- **All graph algorithms** (moved from frontend)
- Background job processing for intensive computations
- Performance optimization for large-scale analysis
- Community detection and insights generation
- Caching of computed results
- Scheduled analytics jobs

**Key Features:**
- **Centrality Analysis**: PageRank, betweenness, closeness, degree centrality
- **Path Finding**: Dijkstra, BFS, shortest path algorithms
- **Community Detection**: Louvain, label propagation algorithms
- **Graph Insights**: Key entities, bridge entities, anomaly detection
- **Background Processing**: Celery-based job queue for long-running tasks
- **Scheduling**: Cron-based recurring analytics jobs

**API Endpoints:**
```
POST   /analytics/centrality         # Compute centrality metrics
POST   /analytics/paths              # Find shortest paths
POST   /analytics/communities        # Detect communities
POST   /analytics/insights           # Generate graph insights
POST   /analytics/jobs               # Submit background job
GET    /analytics/jobs/{id}          # Get job status
POST   /analytics/schedule           # Schedule recurring job
```

**Performance Targets:**
- Graph computation: <10s for 500+ nodes
- Background job processing: Async with progress tracking
- API response time: <2s for cached results
- Concurrent jobs: 10+ with resource management

### 3. Graph Visualization API Service (Port 8010)

**Responsibilities:**
- **Graph layout computation** (moved from frontend)
- Progressive loading for large graphs
- Interactive filtering and data preparation
- Performance optimization for frontend rendering
- Real-time data updates for visualization

**Key Features:**
- **Layout Algorithms**: Force-directed, circular, hierarchical, grid layouts
- **Progressive Loading**: Batch-based loading for large graphs
- **Interactive Filtering**: Dynamic filtering and subgraph extraction
- **Performance Optimization**: Adaptive algorithms based on graph size
- **Data Preparation**: Preprocessing for optimal visualization
- **Export Capabilities**: Multiple format exports (JSON, CSV, GEXF)

**API Endpoints:**
```
POST   /visualization/prepare         # Prepare graph data
GET    /visualization/{id}/neighborhood # Get entity neighborhood
POST   /visualization/progressive-load # Progressive loading
POST   /visualization/interactive-filter # Interactive filtering
POST   /layout/compute              # Compute layout only
GET    /layout/algorithms           # Get available layouts
```

**Performance Targets:**
- Layout computation: <5s for 1000+ nodes
- Progressive loading: <500ms per batch
- Interactive filtering: <1s response time
- Memory usage: Optimized for device capabilities

## Database Architecture

### Neo4j Graph Database
- **Primary graph storage** for entities and relationships
- **Graph Data Science Library** for advanced algorithms
- **Multi-tenant isolation** through tenant_id properties
- **Indexes and constraints** for optimal query performance

### PostgreSQL (Metadata & Analytics)
- **Entity metadata** and search indexes
- **Analytics results** and job tracking
- **User management** and authentication data
- **Tenant settings** and configuration
- **Performance metrics** and monitoring data

### Redis (Cache & Sessions)
- **Query result caching** with TTL optimization
- **Session management** for WebSocket connections
- **Background job queues** via Celery
- **Real-time updates** and pub/sub messaging

## Security Architecture

### Authentication & Authorization
- **JWT-based authentication** with refresh tokens
- **Role-based access control (RBAC)** with fine-grained permissions
- **Multi-tenant data isolation** at database and application level
- **API rate limiting** and request throttling
- **Audit logging** for compliance and monitoring

### Data Security
- **Input validation** and sanitization
- **SQL injection prevention** with parameterized queries
- **Tenant isolation** to prevent data leakage
- **Secure WebSocket connections** with token verification
- **Encrypted sensitive data** storage

## Performance Optimization

### Caching Strategy
- **Multi-level caching**: Application, database, and CDN
- **Adaptive TTL** based on graph size and complexity
- **Cache warming** for frequently accessed data
- **Intelligent cache invalidation** on data updates

### Graph Size Optimization
- **Automatic algorithm selection** based on graph size
- **Progressive loading** for large graphs
- **Memory management** with configurable limits
- **Performance monitoring** and auto-scaling

### Background Processing
- **Asynchronous job processing** for intensive operations
- **Queue management** with priority and retry logic
- **Resource allocation** and concurrent job limits
- **Progress tracking** and job status monitoring

## Monitoring & Observability

### Health Checks
- **Service health endpoints** for all microservices
- **Database connectivity monitoring**
- **Dependency health tracking**
- **Automated recovery** and alerting

### Metrics & Logging
- **Prometheus metrics** for performance monitoring
- **Structured logging** with correlation IDs
- **Distributed tracing** for request flow tracking
- **Custom dashboards** in Grafana

### Error Handling
- **Circuit breaker pattern** for external dependencies
- **Retry logic** with exponential backoff
- **Comprehensive error reporting** and alerting
- **Graceful degradation** for service failures

## Deployment Architecture

### Container Orchestration
- **Docker Compose** for development and testing
- **Kubernetes** ready for production deployment
- **Health checks** and readiness probes
- **Resource limits** and auto-scaling policies

### Environment Configuration
- **Environment-specific settings** via environment variables
- **Secret management** for sensitive data
- **Configuration validation** on startup
- **Hot reload** support for development

## API Documentation

All services provide comprehensive OpenAPI 3.0 specifications:
- **Interactive API docs** at `/docs` endpoint
- **ReDoc documentation** at `/redoc` endpoint
- **OpenAPI JSON** at `/openapi.json`
- **Code examples** for multiple languages

## Testing Strategy

### Unit Testing
- **Service-level unit tests** with pytest
- **Algorithm correctness** validation
- **Mock dependencies** for isolated testing
- **Coverage requirements**: >90%

### Integration Testing
- **API endpoint testing** with real databases
- **Cross-service communication** testing
- **Database integration** validation
- **WebSocket connection** testing

### Performance Testing
- **Load testing** with realistic data volumes
- **Algorithm performance** benchmarking
- **Memory usage** profiling
- **Concurrent user** simulation

## Migration Strategy

### From Frontend Algorithms to Backend
1. **Identify frontend graph algorithms** and their functionality
2. **Create equivalent backend APIs** with same or better performance
3. **Implement progressive migration** with feature flags
4. **Update frontend** to consume backend APIs
5. **Remove frontend code** for graph algorithms
6. **Validate performance** improvements

### Data Migration
- **Zero-downtime migration** strategy
- **Backward compatibility** during transition
- **Data validation** post-migration
- **Rollback procedures** if needed

## Conclusion

This corrected architecture resolves the critical architectural violation by:
- **Moving all graph processing to backend** services
- **Implementing proper separation of concerns**
- **Optimizing performance** for large-scale graph operations
- **Providing comprehensive APIs** for frontend consumption
- **Ensuring security** and multi-tenancy
- **Enabling scalability** and maintainability

The new architecture provides a solid foundation for enterprise-grade knowledge graph functionality while maintaining high performance and security standards.
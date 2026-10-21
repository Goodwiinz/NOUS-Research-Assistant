# Knowledge Graph Backend Architecture Summary

> **Stale / historical (2026-10-17):** It describes a design from an earlier infrastructure generation (standalone microservices, Qdrant, multiple environments) that is not what runs today. Only `dev` is live and services run inside the single FastAPI backend. Check the code before relying on any statement here. Current contracts: [`docs/engineering/`](../engineering/README.md).

## Critical Architecture Fix Completed

**PROBLEM SOLVED**: Tasks 3.1.1-3.3.5 incorrectly placed graph algorithms (centrality, pathfinding, clustering) in frontend components - a **CONSTITUTIONAL VIOLATION**.

**SOLUTION**: Complete backend-first architecture with ALL graph processing moved to dedicated backend services.

## Architecture Overview

### Service Layer (3 New Services)

**1. Knowledge Graph Service (Port 8003) - CORE**
- Entity CRUD operations and relationship management
- Graph traversal and basic search functionality
- Multi-tenant data isolation and security
- Real-time graph updates via WebSocket
- Integration with document processing pipeline

**2. Graph Analytics Service (Port 8009) - ALGORITHMS**
- ALL graph algorithm processing (centrality, pathfinding, clustering)
- Background job processing for intensive computations
- Analytics caching and result management
- Performance optimization for large-scale analysis
- Community detection and insights generation

**3. Graph Visualization API Service (Port 8010) - DATA PREPARATION**
- Graph layout computation (force-directed, hierarchical, circular)
- Performance optimization for frontend rendering
- Progressive loading for large graphs
- Interactive filtering and data preparation
- Export functionality in multiple formats

### Key API Contracts

**Core Graph APIs**:
- `GET /api/v1/entities` - Entity management
- `POST /api/v1/graph/subgraph` - Subgraph extraction
- `POST /api/v1/analytics/centrality` - Centrality computation
- `POST /api/v1/analytics/paths` - Pathfinding algorithms
- `POST /api/v1/analytics/communities` - Community detection
- `POST /api/v1/visualization/layout` - Layout computation

**Real-time APIs**:
- `WebSocket /ws/graph/updates/{organization_id}` - Live graph updates
- `WebSocket /ws/analytics/progress/{organization_id}` - Algorithm progress

### Frontend Integration Points

**FRONTEND RESPONSIBILITIES (ONLY)**:
1. Data visualization using D3.js or similar libraries
2. User interaction handling (zoom, pan, click, drag)
3. API consumption for graph data
4. Real-time update handling via WebSocket
5. UI state management and user preferences

**BACKEND RESPONSIBILITIES (ALL)**:
1. Graph algorithm execution
2. Data processing and computation
3. Layout calculation and optimization
4. Analytics and insights generation
5. Caching and performance optimization
6. Security and multi-tenant isolation

## Implementation Phases

### Phase 1: Core Infrastructure (Weeks 1-2)
1. Set up Knowledge Graph Service (Port 8003)
2. Implement basic entity and relationship CRUD
3. Configure Neo4j, PostgreSQL, Qdrant, Redis integration
4. Set up authentication and multi-tenant security
5. Implement basic WebSocket connections

### Phase 2: Analytics Engine (Weeks 3-4)
1. Deploy Graph Analytics Service (Port 8009)
2. Implement centrality algorithms (PageRank, betweenness, degree)
3. Add pathfinding algorithms (Dijkstra, BFS, A*)
4. Set up background job processing with Celery
5. Implement caching strategy for computation results

### Phase 3: Visualization Support (Weeks 5-6)
1. Deploy Graph Visualization API Service (Port 8010)
2. Implement layout algorithms (force-directed, hierarchical)
3. Add progressive loading for large graphs
4. Implement performance optimization strategies
5. Add export functionality

### Phase 4: Frontend Migration (Weeks 7-8)
1. Remove all graph algorithm code from frontend
2. Implement API integration for graph data
3. Add WebSocket handling for real-time updates
4. Update visualization components to use backend data
5. Performance testing and optimization

## Critical Technical Decisions

### 1. Database Architecture
- **Neo4j**: Native graph storage and algorithms
- **PostgreSQL**: Analytics results, user preferences, job tracking
- **Qdrant**: Vector embeddings for semantic similarity
- **Redis**: Caching, real-time data, message queuing

### 2. Algorithm Processing Strategy
- **Background Jobs**: Intensive algorithms run asynchronously
- **Caching**: Results cached to avoid recomputation
- **Progressive Loading**: Large graphs loaded in chunks
- **Performance Optimization**: Smart filtering and simplification

### 3. Security Architecture
- **JWT Authentication**: Centralized auth service integration
- **RBAC Authorization**: Fine-grained permissions for graph operations
- **Multi-tenant Isolation**: Complete data separation by organization
- **API Rate Limiting**: Prevent abuse and ensure fair usage

### 4. Performance Optimization
- **Multi-level Caching**: Service, Redis, and persistent layers
- **Query Optimization**: Neo4j query tuning and indexing
- **Layout Optimization**: Algorithm selection based on graph size
- **Progressive Enhancement**: Start simple, add complexity as needed

## Integration with Existing Services

### Document Management Integration
```python
# Automatic entity extraction when documents are processed
async def on_document_processed(event):
    document = await document_service.get_document(event.document_id)
    entities = await extract_entities(document)
    relationships = await extract_relationships(document)
    await graph_service.store_graph_data(entities, relationships)
    await analytics_service.trigger_background_computation(document.organization_id)
```

### Search Service Integration
```python
# Hybrid search combining vector, graph, and keyword search
async def hybrid_search(query):
    vector_results = await search_service.vector_search(query)
    graph_results = await graph_service.semantic_search(query)
    keyword_results = await search_service.keyword_search(query)
    return combine_and_rank_results(vector_results, graph_results, keyword_results)
```

### Real-time Communications Integration
```python
# Broadcast graph updates to connected clients
async def on_graph_update(update):
    await realtime_service.broadcast_to_organization(
        organization_id=update.organization_id,
        event_type='graph_update',
        data=update
    )
```

## Monitoring & Observability

### Key Metrics
- **Performance**: API response times, algorithm execution times
- **Business**: Daily active users, graph queries, algorithm executions
- **System**: Cache hit rates, error rates, resource utilization

### Alerting
- **Performance**: API latency >5s, algorithm execution >2min
- **Errors**: Error rate >5%, service downtime
- **Business**: Low user engagement, failed computations

## Success Criteria

### Technical Metrics
- **API Response Time**: <200ms (95th percentile)
- **Algorithm Execution**: <30s for 10K node graphs
- **System Availability**: >99.9% uptime
- **Cache Hit Rate**: >80% for frequently accessed data

### Business Metrics
- **User Adoption**: >80% of users using graph features
- **Query Success Rate**: >95% of graph queries successful
- **Performance Satisfaction**: >4.5/5 user rating
- **Scalability**: 10x growth handling without degradation

## Migration Checklist

### Frontend Cleanup
- [ ] Remove `GraphCentralityMetrics.tsx` component
- [ ] Remove `GraphInsightsDashboard.tsx` component
- [ ] Remove all JavaScript graph algorithm implementations
- [ ] Remove `PathfindingAlgorithms.ts` utilities
- [ ] Remove `ClusteringAnalysis.ts` utilities

### Backend Implementation
- [ ] Deploy Knowledge Graph Service (Port 8003)
- [ ] Deploy Graph Analytics Service (Port 8009)
- [ ] Deploy Graph Visualization API Service (Port 8010)
- [ ] Set up Redis caching layer
- [ ] Configure WebSocket endpoints
- [ ] Implement background job processing

### Integration
- [ ] Update frontend to consume graph APIs
- [ ] Implement WebSocket connection handling
- [ ] Add real-time update processing
- [ ] Update error handling for API calls
- [ ] Implement progressive loading for large graphs

### Testing
- [ ] Unit tests for all graph algorithms
- [ ] Integration tests for API endpoints
- [ ] Performance tests with large graphs
- [ ] Load tests for concurrent users
- [ ] End-to-end testing of complete workflows

This architecture completely resolves the critical architectural violation by ensuring ALL graph processing occurs on backend services while maintaining rich, performant frontend visualization capabilities.
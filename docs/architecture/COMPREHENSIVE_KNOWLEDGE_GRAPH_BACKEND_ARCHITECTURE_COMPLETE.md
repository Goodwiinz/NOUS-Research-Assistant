# Complete Knowledge Graph Backend Service Architecture

> **Stale / historical (2026-10-17):** It describes a design from an earlier infrastructure generation (standalone microservices, Qdrant, multiple environments) that is not what runs today. Only `dev` is live and services run inside the single FastAPI backend. Check the code before relying on any statement here. Current contracts: [`docs/engineering/`](../engineering/README.md).

## 3. Graph Visualization API Service (Port 8010) - FRONTEND DATA PREPARATION

### Service Responsibilities
- Data preparation and optimization for frontend visualization
- Graph layout algorithm computation (force-directed, hierarchical, circular)
- Performance optimization for large graph rendering
- Interactive graph data endpoints with filtering and pagination
- Real-time graph update data streaming

### API Endpoints
```yaml
openapi: 3.0.3
info:
  title: Graph Visualization API
  version: 1.0.0
  description: Graph data preparation and layout computation for frontend visualization

servers:
  - url: http://localhost:8010
    description: Development server

paths:
  # Layout Computation
  /api/v1/visualization/layout:
    post:
      summary: Compute graph layout coordinates
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/LayoutRequest'
      responses:
        '200':
          description: Layout computed successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/LayoutResponse'

  # Neighborhood Visualization
  /api/v1/visualization/neighborhood/{entity_id}:
    get:
      summary: Get entity neighborhood for visualization
      parameters:
        - name: entity_id
          in: path
          required: true
          schema:
            type: string
            format: uuid
        - name: depth
          in: query
          schema:
            type: integer
            default: 2
            minimum: 1
            maximum: 5
        - name: max_nodes
          in: query
          schema:
            type: integer
            default: 50
            maximum: 200
        - name: layout_algorithm
          in: query
          schema:
            type: string
            enum: [force, circular, hierarchical, grid, radial]
            default: force
        - name: include_layout
          in: query
          schema:
            type: boolean
            default: true
        - name: organization_id
          in: query
          required: true
          schema:
            type: string
            format: uuid
      responses:
        '200':
          description: Neighborhood visualization data
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/NeighborhoodVisualizationResponse'

  # Progressive Loading
  /api/v1/visualization/progressive:
    post:
      summary: Get graph data in progressive chunks
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ProgressiveLoadRequest'
      responses:
        '200':
          description: Progressive graph chunk
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ProgressiveLoadResponse'

components:
  schemas:
    LayoutRequest:
      type: object
      required:
        - algorithm
        - nodes
        - edges
        - organization_id
      properties:
        algorithm:
          type: string
          enum: [force, circular, hierarchical, grid, radial, tree]
        nodes:
          type: array
          items:
            type: object
            properties:
              id:
                type: string
              size:
                type: number
                default: 1
        edges:
          type: array
          items:
            type: object
            properties:
              source:
                type: string
              target:
                type: string
              weight:
                type: number
                default: 1
        options:
          type: object
          properties:
            iterations:
              type: integer
              default: 1000
            gravity:
              type: number
              default: 0.1
            link_distance:
              type: number
              default: 100
        organization_id:
          type: string
          format: uuid

    LayoutResponse:
      type: object
      properties:
        layout_id:
          type: string
          format: uuid
        algorithm:
          type: string
        computation_time_ms:
          type: integer
        positions:
          type: object
          additionalProperties:
            type: object
            properties:
              x:
                type: number
              y:
                type: number
              level:
                type: integer
        bounds:
          type: object
          properties:
            min_x:
              type: number
            max_x:
              type: number
            min_y:
              type: number
            max_y:
              type: number

    NeighborhoodVisualizationResponse:
      type: object
      properties:
        central_entity:
          $ref: '#/components/schemas/Entity'
        entities:
          type: array
          items:
            $ref: '#/components/schemas/VisualizationEntity'
        relationships:
          type: array
          items:
            $ref: '#/components/schemas/VisualizationRelationship'
        layout:
          $ref: '#/components/schemas/LayoutData'
        metadata:
          type: object
          properties:
            total_nodes:
              type: integer
            max_depth_reached:
              type: integer
            computation_time_ms:
              type: integer

    VisualizationEntity:
      allOf:
        - $ref: '#/components/schemas/Entity'
        - type: object
          properties:
            visualization_properties:
              type: object
              properties:
                size:
                  type: number
                color:
                  type: string
                opacity:
                  type: number
                  minimum: 0
                  maximum: 1
            layout_position:
              type: object
              properties:
                x:
                  type: number
                y:
                  type: number
                level:
                  type: integer
            degree:
              type: integer

    VisualizationRelationship:
      allOf:
        - $ref: '#/components/schemas/Relationship'
        - type: object
          properties:
            visualization_properties:
              type: object
              properties:
                width:
                  type: number
                color:
                  type: string
                style:
                  type: string
                  enum: [solid, dashed, dotted]
            strength_normalized:
              type: number
              minimum: 0
              maximum: 1

    LayoutData:
      type: object
      properties:
        algorithm:
          type: string
        positions:
          type: object
          additionalProperties:
            type: object
            properties:
              x:
                type: number
              y:
                type: number
        bounds:
          type: object
          properties:
            min_x:
              type: number
            max_x:
              type: number
            min_y:
              type: number
            max_y:
              type: number

    ProgressiveLoadRequest:
      type: object
      required:
        - organization_id
      properties:
        view_id:
          type: string
          format: uuid
        chunk_size:
          type: integer
          default: 50
          maximum: 200
        load_strategy:
          type: string
          enum: [breadth_first, depth_first, importance_first]
          default: importance_first
        center_entity_id:
          type: string
          format: uuid
        organization_id:
          type: string
          format: uuid

    ProgressiveLoadResponse:
      type: object
      properties:
        chunk_id:
          type: string
          format: uuid
        chunk_index:
          type: integer
        total_chunks:
          type: integer
        entities:
          type: array
          items:
            $ref: '#/components/schemas/VisualizationEntity'
        relationships:
          type: array
          items:
            $ref: '#/components/schemas/VisualizationRelationship'
        metadata:
          type: object
          properties:
            is_final_chunk:
              type: boolean
            remaining_entities:
              type: integer

  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT

security:
  - bearerAuth: []

tags:
  - name: layout
    description: Graph layout computation
  - name: neighborhood
    description: Entity neighborhood visualization
  - name: progressive
    description: Progressive graph loading
```

## 4. Authentication & Security Architecture

### JWT Token Structure
```json
{
  "header": {
    "alg": "HS256",
    "typ": "JWT"
  },
  "payload": {
    "sub": "user_uuid",
    "email": "user@example.com",
    "role": "user",
    "organization_id": "org_uuid",
    "permissions": [
      "read:graph",
      "read:graph:analytics",
      "write:graph:entities",
      "execute:graph:algorithms"
    ],
    "iat": 1640995200,
    "exp": 1641081600,
    "jti": "token_uuid"
  }
}
```

### Role-Based Access Control (RBAC)

**Graph-Specific Permissions**:
```
Knowledge Graph Access:
  - read:graph (view graph data)
  - read:graph:analytics (view graph analytics)
  - read:graph:insights (view insights and recommendations)
  - write:graph:entities (create/update entities)
  - delete:graph:entities (delete entities)
  - write:graph:relationships (create/update relationships)
  - delete:graph:relationships (delete relationships)
  - execute:graph:algorithms (run graph algorithms)
  - manage:graph:presets (manage visualization presets)
  - export:graph:data (export graph data)
  - read:graph:realtime (access real-time updates)
```

### Multi-Tenant Data Isolation

**Service-Level Isolation**:
```python
# Middleware for organization isolation
class OrganizationIsolationMiddleware:
    def process_request(self, request):
        token = self.extract_jwt(request)
        organization_id = token.get('organization_id')

        # Inject organization context into all database queries
        request.organization_context = {
            'organization_id': organization_id,
            'user_id': token.get('sub'),
            'role': token.get('role'),
            'permissions': token.get('permissions', [])
        }

        return request

# Neo4j query filtering
def apply_organization_filter(query, organization_id):
    return f"""
    {query}
    AND (entity.organization_id = $organization_id
         OR entity.is_public = true
         OR $user_role = 'admin')
    """
```

## 5. Caching & Performance Optimization Strategy

### Redis Caching Architecture
```yaml
caching_layers:
  # L1: In-memory service cache
  service_cache:
    ttl: 300  # 5 minutes
    size: 1GB
    patterns:
      - "graph:layout:*"
      - "entity:detail:*"
      - "analytics:centrality:*"

  # L2: Redis distributed cache
  redis_cache:
    ttl: 3600  # 1 hour
    size: 10GB
    patterns:
      - "graph:subgraph:*"
      - "graph:statistics:*"
      - "user:preferences:*"
      - "computation:results:*"

  # L3: Persistent results cache
  persistent_cache:
    ttl: 86400  # 24 hours
    size: 100GB
    patterns:
      - "analytics:heavy:*"
      - "layout:large_graphs:*"
      - "export:results:*"
```

### Performance Optimization Patterns

**Graph Processing Optimization**:
```python
class GraphPerformanceOptimizer:
    def __init__(self):
        self.caching_strategy = CachingStrategy()
        self.query_optimizer = QueryOptimizer()
        self.layout_optimizer = LayoutOptimizer()

    def optimize_graph_request(self, request):
        # 1. Cache key generation
        cache_key = self.generate_cache_key(request)

        # 2. Check cache first
        cached_result = self.caching_strategy.get(cache_key)
        if cached_result:
            return cached_result

        # 3. Optimize query based on size
        if request.node_count > 500:
            return self.handle_large_graph(request)
        elif request.node_count > 100:
            return self.handle_medium_graph(request)
        else:
            return self.handle_small_graph(request)

    def handle_large_graph(self, request):
        # Apply aggressive filtering
        filtered_request = self.apply_smart_filters(request)

        # Use simplified layout algorithm
        simplified_layout = self.layout_optimizer.compute_simplified_layout(
            filtered_request
        )

        # Implement progressive loading
        return self.create_progressive_response(
            filtered_request,
            simplified_layout
        )
```

## 6. WebSocket Real-time Communication

### WebSocket Event Protocols
```yaml
websocket_protocols:
  connection_management:
    connect:
      payload:
        token: "JWT Token"
        client_id: "Unique client identifier"
        organization_id: "Organization UUID"
      response:
        session_id: "WebSocket session ID"
        server_time: "2025-10-19T10:00:00Z"

    disconnect:
      payload:
        reason: "disconnect reason"

  graph_updates:
    entity_created:
      payload:
        type: "entity_created"
        entity_id: "UUID"
        entity_data: "Full entity object"
        timestamp: "ISO 8601 timestamp"

    entity_updated:
      payload:
        type: "entity_updated"
        entity_id: "UUID"
        changes: "Changed fields only"
        timestamp: "ISO 8601 timestamp"

    entity_deleted:
      payload:
        type: "entity_deleted"
        entity_id: "UUID"
        timestamp: "ISO 8601 timestamp"

    relationship_created:
      payload:
        type: "relationship_created"
        relationship_id: "UUID"
        relationship_data: "Full relationship object"
        timestamp: "ISO 8601 timestamp"

    analytics_updated:
      payload:
        type: "analytics_updated"
        graph_id: "UUID"
        analytics_type: "centrality, clustering, etc."
        results: "Analytics results"
        timestamp: "ISO 8601 timestamp"

  processing_status:
    job_started:
      payload:
        type: "job_started"
        job_id: "UUID"
        job_type: "computation type"
        estimated_duration: "seconds"

    job_progress:
      payload:
        type: "job_progress"
        job_id: "UUID"
        progress_percentage: "0-100"
        current_stage: "processing stage"

    job_completed:
      payload:
        type: "job_completed"
        job_id: "UUID"
        results_location: "URL or reference"
        computation_time: "milliseconds"
```

## 7. Deployment Configuration

### Docker Compose Services
```yaml
# docker-compose.knowledge-graph.yml
version: '3.8'

services:
  knowledge-graph-service:
    build:
      context: ./services/knowledge-graph
      dockerfile: Dockerfile
    ports:
      - "8003:8003"
    environment:
      - NEO4J_URI=bolt://neo4j:7687
      - NEO4J_USER=neo4j
      - NEO4J_PASSWORD=password
      - POSTGRES_URI=postgresql://postgres:password@postgres:5432/multimodal_rag
      - REDIS_URL=redis://redis:6379
      - QDRANT_URL=http://qdrant:6333
      - JWT_SECRET=${JWT_SECRET}
      - ENVIRONMENT=production
    depends_on:
      - neo4j
      - postgres
      - redis
      - qdrant
    volumes:
      - ./logs:/app/logs
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8003/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  graph-analytics-service:
    build:
      context: ./services/graph-analytics
      dockerfile: Dockerfile
    ports:
      - "8009:8009"
    environment:
      - NEO4J_URI=bolt://neo4j:7687
      - POSTGRES_URI=postgresql://postgres:password@postgres:5432/multimodal_rag
      - REDIS_URL=redis://redis:6379
      - CELERY_BROKER_URL=redis://redis:6379/0
      - CELERY_RESULT_BACKEND=redis://redis:6379/0
      - PYTHONPATH=/app
    depends_on:
      - neo4j
      - postgres
      - redis
    volumes:
      - ./logs:/app/logs
      - graph_computations:/app/computations
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8009/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  graph-visualization-service:
    build:
      context: ./services/graph-visualization
      dockerfile: Dockerfile
    ports:
      - "8010:8010"
    environment:
      - REDIS_URL=redis://redis:6379
      - KNOWLEDGE_GRAPH_URL=http://knowledge-graph-service:8003
      - GRAPH_ANALYTICS_URL=http://graph-analytics-service:8009
      - PYTHONPATH=/app
    depends_on:
      - knowledge-graph-service
      - graph-analytics-service
      - redis
    volumes:
      - ./logs:/app/logs
      - layout_cache:/app/layouts
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8010/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  # Background processing for intensive algorithms
  celery-worker:
    build:
      context: ./services/graph-analytics
      dockerfile: Dockerfile
    command: celery -A app.celery worker --loglevel=info --concurrency=4
    environment:
      - NEO4J_URI=bolt://neo4j:7687
      - POSTGRES_URI=postgresql://postgres:password@postgres:5432/multimodal_rag
      - REDIS_URL=redis://redis:6379
      - CELERY_BROKER_URL=redis://redis:6379/0
      - CELERY_RESULT_BACKEND=redis://redis:6379/0
    depends_on:
      - neo4j
      - postgres
      - redis
    volumes:
      - ./logs:/app/logs
      - graph_computations:/app/computations
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 4G
          cpus: '2.0'

volumes:
  graph_computations:
  layout_cache:
```

### Kubernetes Deployment
```yaml
# k8s/knowledge-graph-services.yml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: knowledge-graph-service
  namespace: rag-system
spec:
  replicas: 3
  selector:
    matchLabels:
      app: knowledge-graph-service
  template:
    metadata:
      labels:
        app: knowledge-graph-service
    spec:
      containers:
      - name: knowledge-graph-service
        image: rag-system/knowledge-graph-service:latest
        ports:
        - containerPort: 8003
        env:
        - name: NEO4J_URI
          valueFrom:
            secretKeyRef:
              name: database-credentials
              key: neo4j-uri
        - name: JWT_SECRET
          valueFrom:
            secretKeyRef:
              name: jwt-secret
              key: secret
        resources:
          requests:
            memory: "1Gi"
            cpu: "500m"
          limits:
            memory: "2Gi"
            cpu: "1000m"
        livenessProbe:
          httpGet:
            path: /health
            port: 8003
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready
            port: 8003
          initialDelaySeconds: 5
          periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: knowledge-graph-service
  namespace: rag-system
spec:
  selector:
    app: knowledge-graph-service
  ports:
  - port: 8003
    targetPort: 8003
  type: ClusterIP
```

## 8. Monitoring & Observability

### Prometheus Metrics Configuration
```yaml
# monitoring/prometheus-knowledge-graph.yml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'knowledge-graph-service'
    static_configs:
      - targets: ['knowledge-graph-service:8003']
    metrics_path: /metrics
    scrape_interval: 30s

  - job_name: 'graph-analytics-service'
    static_configs:
      - targets: ['graph-analytics-service:8009']
    metrics_path: /metrics
    scrape_interval: 30s

  - job_name: 'graph-visualization-service'
    static_configs:
      - targets: ['graph-visualization-service:8010']
    metrics_path: /metrics
    scrape_interval: 30s
```

### Key Performance Indicators
```yaml
kpi_definitions:
  system_performance:
    - name: graph_query_latency_p95
      description: 95th percentile query latency
      target: <2000ms
      alert_threshold: >5000ms

    - name: algorithm_computation_time
      description: Graph algorithm execution time
      target: <30s
      alert_threshold: >120s

    - name: cache_hit_rate
      description: Redis cache hit rate
      target: >80%
      alert_threshold: <60%

    - name: api_error_rate
      description: API error rate
      target: <1%
      alert_threshold: >5%

  business_metrics:
    - name: daily_active_users
      description: Users interacting with knowledge graph
      target: >100

    - name: graph_queries_per_day
      description: Daily graph query volume
      target: >1000

    - name: algorithm_executions_per_day
      description: Daily algorithm executions
      target: >100
```

## 9. Integration Patterns with Existing Services

### Service Communication Architecture
```python
class ServiceIntegrationRegistry:
    def __init__(self):
        self.services = {
            'document_management': ServiceClient('http://document-service:8001'),
            'search': ServiceClient('http://search-service:8002'),
            'processing_pipeline': ServiceClient('http://processing-service:8005'),
            'user_management': ServiceClient('http://user-service:8007'),
            'real_time': ServiceClient('http://realtime-service:8008'),
            'evaluation': ServiceClient('http://evaluation-service:8004')
        }

    async def process_document_for_graph(self, document_id: str):
        """Integrate with document processing pipeline"""
        # 1. Get document from document service
        document = await self.services['document_management'].get_document(document_id)

        # 2. Extract entities and relationships
        extraction_result = await self.extract_entities_from_document(document)

        # 3. Store in knowledge graph
        await self.store_extraction_results(extraction_result)

        # 4. Trigger analytics computation
        await self.trigger_background_analytics(document.organization_id)

        # 5. Notify real-time service
        await self.services['real_time'].broadcast_graph_update(
            organization_id=document.organization_id,
            update_type='document_processed',
            data=extraction_result
        )
```

### Event-Driven Integration
```yaml
event_streams:
  document_processed:
    source: processing_pipeline_service
    target: knowledge_graph_service
    schema:
      event_type: "document.processed"
      data:
        document_id: UUID
        organization_id: UUID
        extraction_results:
          entities: [Entity]
          relationships: [Relationship]
        processing_metadata:
          extraction_time_ms: Integer
          confidence_score: Float

  graph_analytics_computed:
    source: graph_analytics_service
    target: knowledge_graph_service
    schema:
      event_type: "analytics.computed"
      data:
        computation_id: UUID
        organization_id: UUID
        algorithm_type: String
        results: Object
        computation_time_ms: Integer

  user_graph_interaction:
    source: frontend
    target: knowledge_graph_service
    schema:
      event_type: "user.interaction"
      data:
        user_id: UUID
        organization_id: UUID
        interaction_type: String
        entity_ids: [UUID]
        query_params: Object
```

## 10. Error Handling & Resilience

### Comprehensive Error Response Format
```json
{
  "error": {
    "code": "GRAPH_ALGORITHM_FAILED",
    "message": "Centrality computation failed due to insufficient memory",
    "type": "computation_error",
    "severity": "error",
    "timestamp": "2025-10-19T10:00:00Z",
    "request_id": "req_123456789",
    "service": "graph-analytics-service",
    "retry_after": 60,
    "details": {
      "algorithm": "pagerank",
      "graph_size": {
        "nodes": 10000,
        "edges": 50000
      },
      "resource_usage": {
        "memory_mb": 8192,
        "cpu_cores": 4
      },
      "error_stage": "memory_allocation"
    },
    "suggestions": [
      "Try with a smaller subgraph",
      "Use a less memory-intensive algorithm",
      "Reduce the maximum depth parameter"
    ],
    "support_reference": "SUPPORT-GA-12345",
    "correlation_id": "corr_abcdef123456"
  }
}
```

### Circuit Breaker Configuration
```yaml
circuit_breakers:
  neo4j_connection:
    failure_threshold: 5
    recovery_timeout: 30
    expected_exception: [ConnectionError, TimeoutError]

  algorithm_computation:
    failure_threshold: 3
    recovery_timeout: 60
    expected_exception: [MemoryError, TimeoutError]

  external_service_calls:
    failure_threshold: 5
    recovery_timeout: 30
    expected_exception: [HTTPError, ConnectionError]
```

## Summary

This comprehensive backend service architecture completely resolves the critical architectural violation by:

1. **Moving ALL Graph Processing to Backend**: Every graph algorithm (centrality, pathfinding, clustering, analytics) is now executed exclusively on backend services.

2. **API-First Design**: Frontend consumes all graph data through well-defined REST APIs and WebSocket connections.

3. **Performance Optimization**: Specialized services for different aspects (core operations, analytics, visualization) with caching and optimization strategies.

4. **Scalability**: Designed to handle enterprise-scale graphs (500+ nodes) with <2s API response times.

5. **Security**: Complete multi-tenant isolation with RBAC and comprehensive authentication flows.

6. **Real-time Updates**: WebSocket protocols for live graph updates and processing status.

7. **Observability**: Comprehensive monitoring, metrics, and alerting for system health.

8. **Integration**: Seamless integration with existing services (document management, search, processing pipeline).

The architecture ensures that frontend components are limited to visualization and user interaction, while all complex graph processing, algorithm execution, and data manipulation occurs on backend services, resolving the constitutional architectural violation completely.
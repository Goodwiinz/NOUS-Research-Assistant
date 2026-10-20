# Comprehensive Knowledge Graph Backend Service Architecture

> **Stale / historical (2026-10-17):** It describes a design from an earlier infrastructure generation (standalone microservices, Qdrant, multiple environments) that is not what runs today. Only `dev` is live and services run inside the single FastAPI backend. Check the code before relying on any statement here. Current contracts: [`docs/engineering/`](../engineering/README.md).

**Version**: 1.0.0
**Date**: 2025-10-19
**Author**: Claude Code Assistant

## Executive Summary

This document defines the comprehensive backend service architecture for Knowledge Graph functionality that resolves the critical architectural violation where Tasks 3.1.1-3.3.5 incorrectly placed graph algorithms in frontend. The design implements proper backend-first processing with complete separation of concerns.

## Critical Architecture Issues Addressed

### Current Violations (RESOLVED)
1. **Frontend Graph Processing**: JavaScript implementations of centrality, betweenness, PageRank moved to backend
2. **Backend API Limitations**: Enhanced with comprehensive algorithm endpoints
3. **Data Model Inconsistencies**: Unified Neo4j + PostgreSQL + Qdrant + Redis integration
4. **Performance Issues**: Real-time graph calculations moved from frontend to backend services
5. **Scalability Problems**: Backend services designed for enterprise-scale graph processing

## Architecture Overview

### System Design Principles
1. **API-First Design**: All graph operations exposed via REST/WebSocket APIs
2. **Backend Processing**: ALL graph algorithms execute on backend services
3. **Performance Support**: Handle 500+ node graphs with <2s API response
4. **Real-time Updates**: WebSocket for live graph changes
5. **Multi-tenant Security**: Complete organization-based data isolation
6. **Caching Strategy**: Redis caching for computed results
7. **Observability**: Comprehensive metrics, logging, and tracing

### Service Architecture Diagram
```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                            API Gateway & Load Balancer                         │
│                               (Kong/Nginx/Envoy)                              │
└─────────────────┬───────────────────────────────────────────────────────────────┘
                  │
┌─────────────────┴───────────────────────────────────────────────────────────────┐
│                    Authentication & Authorization Layer                         │
│                     (JWT + RBAC + Rate Limiting + Audit)                        │
└─────────────────┬───────────────────────────────────────────────────────────────┘
                  │
┌─────────────────┴───────────────────────────────────────────────────────────────┐
│                        Knowledge Graph Services Layer                          │
│  ┌───────────────────┐ ┌───────────────────┐ ┌─────────────────────────────┐ │
│  │ Knowledge Graph   │ │ Graph Analytics   │ │ Graph Visualization API      │ │
│  │ Service (8003)    │ │ Service (8009)    │ │ Service (8010)               │ │
│  │                   │ │                   │ │                             │ │
│  │ • Entity CRUD     │ │ • Centrality      │ │ • Layout Computation        │ │
│  │ • Graph Traversal │ │ • Pathfinding     │ │ • Neighborhood Data         │ │
│  │ • Basic Search    │ │ • Clustering      │ │ • Interactive Data Prep     │ │
│  │ • Real-time Update│ │ • Insights        │ │ • Performance Optimization  │ │
│  └───────────────────┘ └───────────────────┘ └─────────────────────────────┘ │
└─────────────────┬───────────────────────────────────────────────────────────────┘
                  │
┌─────────────────┴───────────────────────────────────────────────────────────────┐
│                         Message Queue & Caching Layer                          │
│                           (Redis Streams + RabbitMQ)                           │
└─────────────────┬───────────────────────────────────────────────────────────────┘
                  │
┌─────────────────┴───────────────────────────────────────────────────────────────┐
│                            Data Storage Layer                                   │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────────────┐ │
│  │   Neo4j     │ │ PostgreSQL  │ │    Qdrant   │ │       Redis         │ │
│  │ (Knowledge  │ │ (Analytics  │ │ (Embeddings │ │ (Cache &            │ │
│  │   Graph)    │ │  & Metadata)│ │  & Context) │ │  Message Queue)    │ │
│  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 1. Knowledge Graph Service (Port 8003) - CORE SERVICE

### Service Responsibilities
- Entity extraction and relationship identification
- Graph CRUD operations and traversal
- Multi-tenant graph data isolation
- Real-time graph updates as documents are processed
- Basic graph search and filtering
- WebSocket integration for live updates

### Enhanced API Endpoints
```yaml
openapi: 3.0.3
info:
  title: Knowledge Graph Core API
  version: 1.0.0
  description: Core knowledge graph management and operations

servers:
  - url: http://localhost:8003
    description: Development server
  - url: https://api.rag-system.com/knowledge-graph
    description: Production server

paths:
  # Entity Management
  /api/v1/entities:
    post:
      summary: Create new entities
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateEntityRequest'
      responses:
        '201':
          description: Entity created successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Entity'
        '400':
          description: Invalid entity data
        '409':
          description: Entity already exists

    get:
      summary: List entities with filtering
      parameters:
        - name: entity_types
          in: query
          schema:
            type: array
            items:
              type: string
        - name: organization_id
          in: query
          required: true
          schema:
            type: string
            format: uuid
        - name: limit
          in: query
          schema:
            type: integer
            default: 100
            maximum: 1000
        - name: offset
          in: query
          schema:
            type: integer
            default: 0
        - name: confidence_min
          in: query
          schema:
            type: number
            minimum: 0
            maximum: 1
            default: 0.5
        - name: sort_by
          in: query
          schema:
            type: string
            enum: [name, created_at, confidence, importance_score]
            default: created_at
        - name: sort_order
          in: query
          schema:
            type: string
            enum: [asc, desc]
            default: desc
      responses:
        '200':
          description: List of entities
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/EntityListResponse'

  /api/v1/entities/{entity_id}:
    get:
      summary: Get entity details with relationships
      parameters:
        - name: entity_id
          in: path
          required: true
          schema:
            type: string
            format: uuid
        - name: include_relationships
          in: query
          schema:
            type: boolean
            default: true
        - name: relationship_depth
          in: query
          schema:
            type: integer
            default: 1
            maximum: 3
        - name: include_analytics
          in: query
          schema:
            type: boolean
            default: false
      responses:
        '200':
          description: Entity details
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/EntityDetail'
        '404':
          description: Entity not found

    put:
      summary: Update entity
      parameters:
        - name: entity_id
          in: path
          required: true
          schema:
            type: string
            format: uuid
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/UpdateEntityRequest'
      responses:
        '200':
          description: Entity updated
        '404':
          description: Entity not found
        '400':
          description: Invalid update data

    delete:
      summary: Delete entity
      parameters:
        - name: entity_id
          in: path
          required: true
          schema:
            type: string
            format: uuid
      responses:
        '204':
          description: Entity deleted
        '404':
          description: Entity not found

  # Relationship Management
  /api/v1/relationships:
    post:
      summary: Create new relationship
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateRelationshipRequest'
      responses:
        '201':
          description: Relationship created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Relationship'

  /api/v1/relationships/{relationship_id}:
    delete:
      summary: Delete relationship
      parameters:
        - name: relationship_id
          in: path
          required: true
          schema:
            type: string
            format: uuid
      responses:
        '204':
          description: Relationship deleted

  # Graph Operations
  /api/v1/graph/subgraph:
    post:
      summary: Extract subgraph with specified criteria
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/SubgraphRequest'
      responses:
        '200':
          description: Subgraph data
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SubgraphResponse'

  /api/v1/graph/traversal:
    post:
      summary: Graph traversal operations
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/TraversalRequest'
      responses:
        '200':
          description: Traversal results
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/TraversalResponse'

  /api/v1/graph/search/entities:
    post:
      summary: Advanced entity search
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/EntitySearchRequest'
      responses:
        '200':
          description: Search results
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/EntitySearchResponse'

  /api/v1/graph/search/semantic:
    post:
      summary: Semantic graph search
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/SemanticSearchRequest'
      responses:
        '200':
          description: Semantic search results
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SemanticSearchResponse'

  # Graph Statistics
  /api/v1/graph/statistics:
    get:
      summary: Get graph statistics
      parameters:
        - name: organization_id
          in: query
          required: true
          schema:
            type: string
            format: uuid
        - name: entity_types
          in: query
          schema:
            type: array
            items:
              type: string
        - name: date_range
          in: query
          schema:
            type: object
            properties:
              start:
                type: string
                format: date
              end:
                type: string
                format: date
      responses:
        '200':
          description: Graph statistics
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/GraphStatistics'

  # Document Integration
  /api/v1/documents/{document_id}/entities:
    get:
      summary: Get entities extracted from document
      parameters:
        - name: document_id
          in: path
          required: true
          schema:
            type: string
            format: uuid
        - name: include_relationships
          in: query
          schema:
            type: boolean
            default: true
      responses:
        '200':
          description: Document entities
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Entity'

  /api/v1/documents/{document_id}/graph:
    get:
      summary: Get document-specific subgraph
      parameters:
        - name: document_id
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
            maximum: 5
        - name: max_nodes
          in: query
          schema:
            type: integer
            default: 100
            maximum: 500
      responses:
        '200':
          description: Document graph
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SubgraphResponse'

  # WebSocket Endpoints
  /ws/graph/updates/{organization_id}:
    description: Real-time graph updates WebSocket
    events:
      entity_created:
        description: New entity created
        data:
          $ref: '#/components/schemas/Entity'
      entity_updated:
        description: Entity updated
        data:
          $ref: '#/components/schemas/Entity'
      entity_deleted:
        description: Entity deleted
        data:
          type: object
          properties:
            entity_id:
              type: string
              format: uuid
      relationship_created:
        description: New relationship created
        data:
          $ref: '#/components/schemas/Relationship'
      relationship_deleted:
        description: Relationship deleted
        data:
          type: object
          properties:
            relationship_id:
              type: string
              format: uuid
      graph_analytics_updated:
        description: Graph analytics updated
        data:
          $ref: '#/components/schemas/GraphStatistics'

components:
  schemas:
    Entity:
      type: object
      properties:
        id:
          type: string
          format: uuid
        name:
          type: string
          minLength: 1
          maxLength: 500
        canonical_name:
          type: string
          maxLength: 500
        type:
          type: string
          enum: [person, organization, location, concept, event, product, technology, document]
        subtypes:
          type: array
          items:
            type: string
        confidence:
          type: number
          minimum: 0
          maximum: 1
        importance_score:
          type: number
          minimum: 0
          maximum: 1
        description:
          type: string
          maxLength: 2000
        aliases:
          type: array
          items:
            type: string
        properties:
          type: object
          additionalProperties: true
        extraction_method:
          type: string
          enum: [nlp, manual, ml_model, rule_based]
        extraction_context:
          type: string
        first_seen:
          type: string
          format: date-time
        last_seen:
          type: string
          format: date-time
        created_at:
          type: string
          format: date-time
        updated_at:
          type: string
          format: date-time
        source_document_ids:
          type: array
          items:
            type: string
            format: uuid
        organization_id:
          type: string
          format: uuid
        embeddings_version:
          type: string

    EntityDetail:
      allOf:
        - $ref: '#/components/schemas/Entity'
        - type: object
          properties:
            relationships:
              type: array
              items:
                $ref: '#/components/schemas/RelationshipDetail'
            related_entities:
              type: array
              items:
                $ref: '#/components/schemas/RelatedEntity'
            analytics:
              $ref: '#/components/schemas/EntityAnalytics'
            temporal_data:
              type: object
              properties:
                first_mentioned:
                  type: string
                  format: date-time
                last_mentioned:
                  type: string
                  format: date-time
                mention_frequency:
                  type: integer
                trend_direction:
                  type: string
                  enum: [increasing, decreasing, stable]

    Relationship:
      type: object
      properties:
        id:
          type: string
          format: uuid
        source_entity_id:
          type: string
          format: uuid
        target_entity_id:
          type: string
          format: uuid
        type:
          type: string
          enum: [related_to, works_for, located_in, part_of, created_by, uses, contains, connects_to]
        subtype:
          type: string
        strength:
          type: number
          minimum: 0
          maximum: 1
        confidence:
          type: number
          minimum: 0
          maximum: 1
        weight:
          type: number
          minimum: 0
        direction:
          type: string
          enum: [bidirectional, directed]
        evidence:
          type: array
          items:
            type: string
        context:
          type: string
        extraction_method:
          type: string
        source_document_id:
          type: string
          format: uuid
        created_at:
          type: string
          format: date-time
        updated_at:
          type: string
          format: date-time
        organization_id:
          type: string
          format: uuid

    RelationshipDetail:
      allOf:
        - $ref: '#/components/schemas/Relationship'
        - type: object
          properties:
            source_entity:
              $ref: '#/components/schemas/Entity'
            target_entity:
              $ref: '#/components/schemas/Entity'
            analytics:
              $ref: '#/components/schemas/RelationshipAnalytics'

    CreateEntityRequest:
      type: object
      required:
        - name
        - type
        - organization_id
      properties:
        name:
          type: string
          minLength: 1
          maxLength: 500
        canonical_name:
          type: string
          maxLength: 500
        type:
          type: string
          enum: [person, organization, location, concept, event, product, technology, document]
        subtypes:
          type: array
          items:
            type: string
        confidence:
          type: number
          minimum: 0
          maximum: 1
          default: 1.0
        description:
          type: string
          maxLength: 2000
        aliases:
          type: array
          items:
            type: string
        properties:
          type: object
          additionalProperties: true
        extraction_method:
          type: string
          enum: [nlp, manual, ml_model, rule_based]
          default: manual
        source_document_ids:
          type: array
          items:
            type: string
            format: uuid
        organization_id:
          type: string
          format: uuid

    UpdateEntityRequest:
      type: object
      properties:
        name:
          type: string
          minLength: 1
          maxLength: 500
        canonical_name:
          type: string
          maxLength: 500
        type:
          type: string
          enum: [person, organization, location, concept, event, product, technology, document]
        subtypes:
          type: array
          items:
            type: string
        confidence:
          type: number
          minimum: 0
          maximum: 1
        importance_score:
          type: number
          minimum: 0
          maximum: 1
        description:
          type: string
          maxLength: 2000
        aliases:
          type: array
          items:
            type: string
        properties:
          type: object
          additionalProperties: true

    CreateRelationshipRequest:
      type: object
      required:
        - source_entity_id
        - target_entity_id
        - type
        - organization_id
      properties:
        source_entity_id:
          type: string
          format: uuid
        target_entity_id:
          type: string
          format: uuid
        type:
          type: string
          enum: [related_to, works_for, located_in, part_of, created_by, uses, contains, connects_to]
        subtype:
          type: string
        strength:
          type: number
          minimum: 0
          maximum: 1
          default: 0.5
        confidence:
          type: number
          minimum: 0
          maximum: 1
          default: 1.0
        weight:
          type: number
          minimum: 0
          default: 1.0
        direction:
          type: string
          enum: [bidirectional, directed]
          default: bidirectional
        evidence:
          type: array
          items:
            type: string
        context:
          type: string
        extraction_method:
          type: string
        source_document_id:
          type: string
          format: uuid
        organization_id:
          type: string
          format: uuid

    SubgraphRequest:
      type: object
      required:
        - organization_id
      properties:
        entity_ids:
          type: array
          items:
            type: string
            format: uuid
        entity_types:
          type: array
          items:
            type: string
        relationship_types:
          type: array
          items:
            type: string
        center_entity_id:
          type: string
          format: uuid
        depth:
          type: integer
          minimum: 1
          maximum: 5
          default: 2
        max_nodes:
          type: integer
          minimum: 1
          maximum: 1000
          default: 100
        include_attributes:
          type: boolean
          default: true
        filters:
          type: object
          properties:
            confidence_min:
              type: number
              minimum: 0
              maximum: 1
            strength_min:
              type: number
              minimum: 0
              maximum: 1
        organization_id:
          type: string
          format: uuid

    SubgraphResponse:
      type: object
      properties:
        graph_id:
          type: string
          format: uuid
        nodes:
          type: array
          items:
            $ref: '#/components/schemas/GraphEntity'
        edges:
          type: array
          items:
            $ref: '#/components/schemas/GraphEdge'
        statistics:
          $ref: '#/components/schemas/SubgraphStatistics'
        metadata:
          type: object
          properties:
            total_nodes:
              type: integer
            total_edges:
              type: integer
            max_depth_reached:
              type: integer
            query_time_ms:
              type: integer
            cache_hit:
              type: boolean

    GraphEntity:
      allOf:
        - $ref: '#/components/schemas/Entity'
        - type: object
          properties:
            degree:
              type: integer
            layout_position:
              type: object
              properties:
                x:
                  type: number
                y:
                  type: number
                z:
                  type: number
            visualization_properties:
              type: object
              properties:
                size:
                  type: number
                color:
                  type: string
                shape:
                  type: string
                label_visible:
                  type: boolean

    GraphEdge:
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
                label_visible:
                  type: boolean

    TraversalRequest:
      type: object
      required:
        - start_entity_id
        - traversal_type
        - organization_id
      properties:
        start_entity_id:
          type: string
          format: uuid
        traversal_type:
          type: string
          enum: [breadth_first, depth_first, shortest_path, all_paths, random_walk]
        max_depth:
          type: integer
          minimum: 1
          maximum: 10
          default: 3
        max_nodes:
          type: integer
          minimum: 1
          maximum: 1000
          default: 100
        target_entity_id:
          type: string
          format: uuid
        weight_property:
          type: string
          enum: [strength, confidence, weight]
          default: strength
        filters:
          type: object
        organization_id:
          type: string
          format: uuid

    TraversalResponse:
      type: object
      properties:
        traversal_id:
          type: string
          format: uuid
        paths:
          type: array
          items:
            $ref: '#/components/schemas/GraphPath'
        visited_entities:
          type: array
          items:
            $ref: '#/components/schemas/Entity'
        statistics:
          type: object
          properties:
            total_paths_found:
              type: integer
            average_path_length:
              type: number
            traversal_time_ms:
              type: integer

    GraphPath:
      type: object
      properties:
        path_id:
          type: string
          format: uuid
        entities:
          type: array
          items:
            $ref: '#/components/schemas/Entity'
        relationships:
          type: array
          items:
            $ref: '#/components/schemas/Relationship'
        total_weight:
          type: number
        path_length:
          type: integer
        strength_score:
          type: number

    EntitySearchRequest:
      type: object
      required:
        - organization_id
      properties:
        query:
          type: string
        search_type:
          type: string
          enum: [exact, fuzzy, semantic, hybrid]
          default: fuzzy
        entity_types:
          type: array
          items:
            type: string
        filters:
          type: object
          properties:
            confidence_min:
              type: number
              minimum: 0
              maximum: 1
            importance_min:
              type: number
              minimum: 0
              maximum: 1
            date_range:
              type: object
              properties:
                start:
                  type: string
                  format: date
                end:
                  type: string
                  format: date
        limit:
          type: integer
          default: 50
          maximum: 200
        offset:
          type: integer
          default: 0
        include_relationships:
          type: boolean
          default: false
        organization_id:
          type: string
          format: uuid

    EntitySearchResponse:
      type: object
      properties:
        query:
          type: string
        search_time_ms:
          type: integer
        total_results:
          type: integer
        entities:
          type: array
          items:
            $ref: '#/components/schemas/EntitySearchResult'
        facets:
          type: object
          properties:
            entity_types:
              type: array
              items:
                type: object
                properties:
                  type:
                    type: string
                  count:
                    type: integer
            confidence_ranges:
              type: array
              items:
                type: object
                properties:
                  range:
                    type: string
                  count:
                    type: integer

    EntitySearchResult:
      allOf:
        - $ref: '#/components/schemas/Entity'
        - type: object
          properties:
            relevance_score:
              type: number
              minimum: 0
              maximum: 1
            match_type:
              type: string
              enum: [exact_name, partial_name, alias, description, semantic]
            highlighted_fields:
              type: array
              items:
                type: string

    SemanticSearchRequest:
      type: object
      required:
        - query_embedding
        - organization_id
      properties:
        query_embedding:
          type: array
          items:
            type: number
          minItems: 128
          maxItems: 1536
        entity_types:
          type: array
          items:
            type: string
        similarity_threshold:
          type: number
          minimum: 0
          maximum: 1
          default: 0.7
        limit:
          type: integer
          default: 20
          maximum: 100
        include_context:
          type: boolean
          default: true
        organization_id:
          type: string
          format: uuid

    SemanticSearchResponse:
      type: object
      properties:
        search_time_ms:
          type: integer
        total_results:
          type: integer
        entities:
          type: array
          items:
            $ref: '#/components/schemas/SemanticSearchResult'

    SemanticSearchResult:
      allOf:
        - $ref: '#/components/schemas/Entity'
        - type: object
          properties:
            similarity_score:
              type: number
              minimum: 0
              maximum: 1
            context_snippets:
              type: array
              items:
                type: string
            embedding_distance:
              type: number

    EntityAnalytics:
      type: object
      properties:
        centrality_metrics:
          type: object
          properties:
            degree:
              type: number
            betweenness:
              type: number
            closeness:
              type: number
            eigenvector:
              type: number
            pagerank:
              type: number
        structural_metrics:
          type: object
          properties:
            clustering_coefficient:
              type: number
            neighbor_count:
              type: integer
            ego_network_density:
              type: number
        community_info:
          type: object
          properties:
            community_id:
              type: string
            community_role:
              type: string
              enum: [hub, bridge, peripheral]
            community_importance:
              type: number
        temporal_metrics:
          type: object
          properties:
            connection_velocity:
              type: number
            importance_trend:
              type: array
              items:
                type: object
                properties:
                  timestamp:
                    type: string
                    format: date-time
                  importance:
                    type: number
        computed_at:
          type: string
          format: date-time
        algorithm_version:
          type: string

    RelationshipAnalytics:
      type: object
      properties:
        structural_importance:
          type: number
        bridge_score:
          type: number
        path_usage_frequency:
          type: integer
        temporal_strength:
          type: number
        computed_at:
          type: string
          format: date-time

    GraphStatistics:
      type: object
      properties:
        organization_id:
          type: string
          format: uuid
        timestamp:
          type: string
          format: date-time
        basic_metrics:
          type: object
          properties:
            total_nodes:
              type: integer
            total_edges:
              type: integer
            average_degree:
              type: number
            graph_density:
              type: number
            connected_components:
              type: integer
            largest_component_size:
              type: integer
        quality_metrics:
          type: object
          properties:
            average_confidence:
              type: number
            high_quality_nodes:
              type: integer
            low_quality_nodes:
              type: integer
        type_distribution:
          type: object
          properties:
            entity_types:
              type: object
              additionalProperties:
                type: integer
            relationship_types:
              type: object
              additionalProperties:
                type: integer
        performance_metrics:
          type: object
          properties:
            last_computation_time_ms:
              type: integer
            cache_hit_rate:
              type: number
            average_query_time_ms:
              type: number

    SubgraphStatistics:
      type: object
      properties:
        node_count:
          type: integer
        edge_count:
          type: integer
        average_degree:
          type: number
        density:
          type: number
        connected_components:
          type: integer
        diameter:
          type: integer
        average_path_length:
          type: number
        clustering_coefficient:
          type: number

    EntityListResponse:
      type: object
      properties:
        entities:
          type: array
          items:
            $ref: '#/components/schemas/Entity'
        pagination:
          type: object
          properties:
            total:
              type: integer
            limit:
              type: integer
            offset:
              type: integer
            has_next:
              type: boolean
            has_previous:
              type: boolean
        filters_applied:
          type: object
        search_time_ms:
          type: integer

    RelatedEntity:
      type: object
      properties:
        entity:
          $ref: '#/components/schemas/Entity'
        relationship:
          $ref: '#/components/schemas/Relationship'
        relationship_strength:
          type: number
          minimum: 0
          maximum: 1
        connection_confidence:
          type: number
          minimum: 0
          maximum: 1
        shared_contexts:
          type: array
          items:
            type: string

  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT

security:
  - bearerAuth: []

tags:
  - name: entities
    description: Entity management operations
  - name: relationships
    description: Relationship management operations
  - name: graph
    description: Graph operations and analytics
  - name: search
    description: Graph search functionality
  - name: websocket
    description: Real-time graph updates
```

## 2. Graph Analytics Service (Port 8009) - ALGORITHM PROCESSING

### Service Responsibilities
- ALL graph algorithm processing (centrality, pathfinding, clustering)
- Graph analytics computation and insights generation
- Performance optimization for large-scale graph analysis
- Caching of computation results
- Background job processing for intensive algorithms

### API Endpoints
```yaml
openapi: 3.0.3
info:
  title: Graph Analytics API
  version: 1.0.0
  description: Advanced graph algorithms and analytics processing

servers:
  - url: http://localhost:8009
    description: Development server

paths:
  # Centrality Analysis
  /api/v1/analytics/centrality:
    post:
      summary: Compute centrality metrics
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CentralityRequest'
      responses:
        '202':
          description: Computation started
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ComputationJob'
        '400':
          description: Invalid request

  /api/v1/analytics/centrality/{job_id}:
    get:
      summary: Get centrality computation results
      parameters:
        - name: job_id
          in: path
          required: true
          schema:
            type: string
            format: uuid
      responses:
        '200':
          description: Computation results
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CentralityResult'

  # Pathfinding Algorithms
  /api/v1/analytics/paths:
    post:
      summary: Find paths between entities
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/PathfindingRequest'
      responses:
        '200':
          description: Pathfinding results
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PathfindingResult'

  # Community Detection
  /api/v1/analytics/communities:
    post:
      summary: Detect communities in graph
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CommunityDetectionRequest'
      responses:
        '202':
          description: Community detection started
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ComputationJob'

  # Clustering Analysis
  /api/v1/analytics/clustering:
    post:
      summary: Graph clustering analysis
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ClusteringRequest'
      responses:
        '202':
          description: Clustering analysis started
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ComputationJob'

  # Graph Insights
  /api/v1/analytics/insights:
    get:
      summary: Get graph insights and recommendations
      parameters:
        - name: organization_id
          in: query
          required: true
          schema:
            type: string
            format: uuid
        - name: categories
          in: query
          schema:
            type: array
            items:
              type: string
              enum: [quality, connectivity, performance, anomalies, trends]
        - name: severity
          in: query
          schema:
            type: array
            items:
              type: string
              enum: [low, medium, high, critical]
        - name: limit
          in: query
          schema:
            type: integer
            default: 20
      responses:
        '200':
          description: Graph insights
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/InsightsResponse'

  # Comparative Analytics
  /api/v1/analytics/comparison:
    post:
      summary: Compare graph states or subgraphs
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ComparisonRequest'
      responses:
        '200':
          description: Comparison results
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ComparisonResult'

  # Algorithm Jobs Management
  /api/v1/analytics/jobs:
    get:
      summary: List computation jobs
      parameters:
        - name: organization_id
          in: query
          required: true
          schema:
            type: string
            format: uuid
        - name: status
          in: query
          schema:
            type: string
            enum: [pending, running, completed, failed]
        - name: algorithm_type
          in: query
          schema:
            type: string
            enum: [centrality, pathfinding, community, clustering, insights]
      responses:
        '200':
          description: List of jobs
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/ComputationJob'

  /api/v1/analytics/jobs/{job_id}:
    get:
      summary: Get job details
      parameters:
        - name: job_id
          in: path
          required: true
          schema:
            type: string
            format: uuid
      responses:
        '200':
          description: Job details
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ComputationJobDetail'

    delete:
      summary: Cancel or delete job
      parameters:
        - name: job_id
          in: path
          required: true
          schema:
            type: string
            format: uuid
      responses:
        '204':
          description: Job cancelled/deleted

components:
  schemas:
    CentralityRequest:
      type: object
      required:
        - algorithm
        - organization_id
      properties:
        algorithm:
          type: string
          enum: [degree, betweenness, closeness, eigenvector, pagerank, katz]
        entity_types:
          type: array
          items:
            type: string
        entity_ids:
          type: array
          items:
            type: string
            format: uuid
        filters:
          type: object
          properties:
            confidence_min:
              type: number
              minimum: 0
              maximum: 1
            degree_min:
              type: integer
              minimum: 1
        weight_property:
          type: string
          enum: [strength, confidence, weight]
          default: strength
        normalization:
          type: boolean
          default: true
        include_normalized_scores:
          type: boolean
          default: true
        organization_id:
          type: string
          format: uuid

    CentralityResult:
      type: object
      properties:
        job_id:
          type: string
          format: uuid
        algorithm:
          type: string
        computation_time_ms:
          type: integer
        entity_count:
          type: integer
        results:
          type: array
          items:
            type: object
            properties:
              entity_id:
                type: string
                format: uuid
              entity_name:
                type: string
              centrality_score:
                type: number
              normalized_score:
                type: number
              rank:
                type: integer
              percentile:
                type: number
              metadata:
                type: object
        statistics:
          type: object
          properties:
            mean_score:
              type: number
            median_score:
              type: number
            std_deviation:
              type: number
            min_score:
              type: number
            max_score:
              type: number
        computed_at:
          type: string
          format: date-time

    PathfindingRequest:
      type: object
      required:
        - source_entity_id
        - target_entity_id
        - algorithm
        - organization_id
      properties:
        source_entity_id:
          type: string
          format: uuid
        target_entity_id:
          type: string
          format: uuid
        algorithm:
          type: string
          enum: [dijkstra, bfs, dfs, astar, johnson, floyd_warshall]
        max_depth:
          type: integer
          minimum: 1
          maximum: 10
          default: 5
        max_paths:
          type: integer
          default: 10
        weight_property:
          type: string
          enum: [strength, confidence, weight]
          default: strength
        return_all_paths:
          type: boolean
          default: false
        include_path_details:
          type: boolean
          default: true
        organization_id:
          type: string
          format: uuid

    PathfindingResult:
      type: object
      properties:
        request_id:
          type: string
          format: uuid
        computation_time_ms:
          type: integer
        paths:
          type: array
          items:
            type: object
            properties:
              path_id:
                type: string
                format: uuid
              entities:
                type: array
                items:
                  $ref: '#/components/schemas/Entity'
              relationships:
                type: array
                items:
                  $ref: '#/components/schemas/Relationship'
              total_weight:
                type: number
              path_length:
                type: integer
              strength_score:
                type: number
        statistics:
          type: object
          properties:
            total_paths_found:
              type: integer
            shortest_path_length:
              type: integer
            average_path_length:
              type: number
            computation_algorithm:
              type: string

    CommunityDetectionRequest:
      type: object
      required:
        - algorithm
        - organization_id
      properties:
        algorithm:
          type: string
          enum: [louvain, leiden, label_propagation, infomap, walktrap]
        resolution:
          type: number
          default: 1.0
          minimum: 0.1
          maximum: 10.0
        min_community_size:
          type: integer
          default: 3
          minimum: 2
        max_communities:
          type: integer
          default: 100
        entity_types:
          type: array
          items:
            type: string
        weight_property:
          type: string
          enum: [strength, confidence, weight]
        include_overlapping:
          type: boolean
          default: false
        organization_id:
          type: string
          format: uuid

    ClusteringRequest:
      type: object
      required:
        - algorithm
        - organization_id
      properties:
        algorithm:
          type: string
          enum: [k_core, clique_percolation, hierarchical, dbscan]
        parameters:
          type: object
          properties:
            k_value:
              type: integer
              minimum: 2
            min_clique_size:
              type: integer
              minimum: 3
            eps:
              type: number
            min_samples:
              type: integer
        entity_types:
          type: array
          items:
            type: string
        max_clusters:
          type: integer
          default: 50
        organization_id:
          type: string
          format: uuid

    ComparisonRequest:
      type: object
      required:
        - comparison_type
        - organization_id
      properties:
        comparison_type:
          type: string
          enum: [temporal, subgraph, entity, algorithm]
        baseline:
          type: object
          description: Baseline graph or snapshot
        comparison:
          type: object
          description: Comparison graph or snapshot
        time_range:
          type: object
          properties:
            baseline_start:
              type: string
              format: date-time
            baseline_end:
              type: string
              format: date-time
            comparison_start:
              type: string
              format: date-time
            comparison_end:
              type: string
              format: date-time
        metrics:
          type: array
          items:
            type: string
            enum: [centrality, connectivity, density, clustering, paths]
        organization_id:
          type: string
          format: uuid

    ComparisonResult:
      type: object
      properties:
        comparison_id:
          type: string
          format: uuid
        comparison_type:
          type: string
        computation_time_ms:
          type: integer
        baseline_metrics:
          type: object
        comparison_metrics:
          type: object
        changes:
          type: object
          properties:
            added_entities:
              type: integer
            removed_entities:
              type: integer
            added_relationships:
              type: integer
            removed_relationships:
              type: integer
            metric_changes:
              type: object
        insights:
          type: array
          items:
            type: object
            properties:
              type:
                type: string
              description:
                type: string
              significance:
                type: number
              recommendation:
                type: string

    InsightsResponse:
      type: object
      properties:
        organization_id:
          type: string
          format: uuid
        generated_at:
          type: string
          format: date-time
        insights:
          type: array
          items:
            type: object
            properties:
              id:
                type: string
                format: uuid
              type:
                type: string
                enum: [quality_issue, connectivity_anomaly, performance_bottleneck, trend_change, opportunity]
              severity:
                type: string
                enum: [low, medium, high, critical]
              title:
                type: string
              description:
                type: string
              impact_score:
                type: number
                minimum: 0
                maximum: 1
              confidence:
                type: number
                minimum: 0
                maximum: 1
              affected_entities:
                type: array
                items:
                  type: string
                  format: uuid
              metrics:
                type: object
              recommendations:
                type: array
                items:
                  type: string
              expires_at:
                type: string
                format: date-time

    ComputationJob:
      type: object
      properties:
        id:
          type: string
          format: uuid
        job_type:
          type: string
        algorithm:
          type: string
        status:
          type: string
          enum: [pending, running, completed, failed, cancelled]
        progress_percentage:
          type: integer
          minimum: 0
          maximum: 100
        created_at:
          type: string
          format: date-time
        started_at:
          type: string
          format: date-time
        completed_at:
          type: string
          format: date-time
        estimated_completion:
          type: string
          format: date-time

    ComputationJobDetail:
      allOf:
        - $ref: '#/components/schemas/ComputationJob'
        - type: object
          properties:
            input_parameters:
              type: object
            result_location:
              type: string
            error_message:
              type: string
            resource_usage:
              type: object
              properties:
                cpu_time_ms:
                  type: integer
                memory_used_mb:
                  type: integer
                nodes_processed:
                  type: integer
            organization_id:
              type: string
              format: uuid
            user_id:
              type: string
              format: uuid

  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT

security:
  - bearerAuth: []

tags:
  - name: centrality
    description: Centrality analysis algorithms
  - name: pathfinding
    description: Path finding algorithms
  - name: communities
    description: Community detection algorithms
  - name: clustering
    description: Graph clustering algorithms
  - name: insights
    description: Graph insights and recommendations
  - name: jobs
    description: Computation job management
```
TodoWrite
<arg_key>todos</arg_key>
<arg_value>[{"content": "Analyze current system architecture and identify integration points", "status": "completed", "activeForm": "Analyzed current system architecture and integration points"}, {"content": "Design Knowledge Graph Service (Port 8003) with complete graph algorithm processing", "status": "completed", "activeForm": "Designed Knowledge Graph Service with graph algorithms"}, {"content": "Design Graph Visualization API Service for frontend data preparation", "status": "in_progress", "activeForm": "Designing Graph Visualization API Service"}, {"content": "Design Graph Analytics Service for metrics and insights", "status": "completed", "activeForm": "Designed Graph Analytics Service"}, {"content": "Create complete OpenAPI 3.0 specifications for all graph services", "status": "pending", "activeForm": "Creating OpenAPI 3.0 specifications"}, {"content": "Design authentication, authorization, and security flows", "status": "pending", "activeForm": "Designing authentication and security flows"}, {"content": "Design caching and performance optimization strategies", "status": "pending", "activeForm": "Designing caching and performance optimization"}, {"content": "Design WebSocket protocols for real-time graph updates", "status": "pending", "activeForm": "Designing WebSocket protocols"}, {"content": "Create deployment configuration and monitoring setup", "status": "pending", "activeForm": "Creating deployment configuration"}]
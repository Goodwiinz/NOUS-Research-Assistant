# Comprehensive API Contracts - Multimodal Enterprise RAG System

> **Stale / historical (2026-10-17):** It covers only part of the API and its paths and schemas may have drifted. The source of truth is the generated `backend/openapi.json` (also served at `/docs` when `DEBUG` is on). Current contracts: [`docs/engineering/`](../engineering/README.md).

## Overview

This document provides complete API contracts for the Multimodal Enterprise RAG System UI, including REST endpoints, WebSocket specifications, authentication patterns, and comprehensive error handling. The API supports multi-tenant architecture with organization-based isolation, real-time updates, and comprehensive evaluation metrics.

## Table of Contents

1. [API Architecture](#api-architecture)
2. [Authentication & Authorization](#authentication--authorization)
3. [REST API Endpoints](#rest-api-endpoints)
4. [WebSocket API](#websocket-api)
5. [Error Handling](#error-handling)
6. [Rate Limiting & Quotas](#rate-limiting--quotas)
7. [OpenAPI 3.0 Specification](#openapi-30-specification)
8. [Examples & Usage](#examples--usage)

---

## API Architecture

### Base Configuration
- **Base URL**: `https://api.ragsystem.com/api/v1`
- **Protocol**: HTTPS only
- **Content-Type**: `application/json` (except file uploads)
- **API Version**: `v1` (URL versioned)
- **Authentication**: JWT Bearer tokens
- **Multi-tenancy**: Organization-based data isolation

### Response Format
All API responses follow a consistent format:

```json
{
  "success": true,
  "data": {},
  "message": "Operation completed successfully",
  "request_id": "uuid-string",
  "timestamp": "2024-01-01T12:00:00Z",
  "pagination": {
    "page": 1,
    "size": 20,
    "total": 100,
    "pages": 5
  }
}
```

---

## Authentication & Authorization

### JWT Token Structure
```json
{
  "iss": "https://api.ragsystem.com",
  "aud": "multimodal-rag-ui",
  "sub": "user-uuid",
  "org_id": "organization-uuid",
  "email": "user@example.com",
  "role": "user|admin|content_manager|analyst",
  "permissions": ["documents:read", "search:execute", "evaluation:view"],
  "exp": 1640995200,
  "iat": 1640991600,
  "session_id": "session-uuid"
}
```

### Authorization Patterns
- **User Access**: Can only access their own data within their organization
- **Admin Access**: Full organization-wide access with management capabilities
- **Content Manager**: Can manage documents and processing
- **Analyst**: Can view analytics and evaluation metrics

### Required Permissions by Endpoint

| Endpoint Pattern | Required Permissions |
|------------------|---------------------|
| `/auth/*` | None (public) |
| `/users/me` | `profile:read` |
| `/users/{id}` | `users:read` (admin/self) |
| `/documents/*` | `documents:read`/`documents:write`/`documents:delete` |
| `/search/*` | `search:execute` |
| `/knowledge-graph/*` | `graph:read` |
| `/evaluation/*` | `evaluation:view` |
| `/analytics/*` | `analytics:view` |

---

## REST API Endpoints

### 1. Authentication & User Management

#### POST /auth/login
Login with email and password.

**Request Body**:
```json
{
  "email": "user@example.com",
  "password": "securePassword123",
  "remember_me": false,
  "device_info": {
    "device_type": "web",
    "device_name": "Chrome on macOS",
    "user_agent": "Mozilla/5.0..."
  }
}
```

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "token_type": "Bearer",
    "expires_in": 3600,
    "user": {
      "id": "user-uuid",
      "email": "user@example.com",
      "first_name": "John",
      "last_name": "Doe",
      "role": "user",
      "organization": {
        "id": "org-uuid",
        "name": "Example Corp",
        "subscription_tier": "professional"
      }
    }
  }
}
```

#### POST /auth/refresh
Refresh access token using refresh token.

**Request Body**:
```json
{
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

#### POST /auth/logout
Logout and invalidate tokens.

**Request Headers**: `Authorization: Bearer <token>`

**Response**: 200 OK
```json
{
  "success": true,
  "message": "Logged out successfully"
}
```

#### GET /users/me
Get current user profile.

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "id": "user-uuid",
    "email": "user@example.com",
    "first_name": "John",
    "last_name": "Doe",
    "role": "user",
    "organization": {
      "id": "org-uuid",
      "name": "Example Corp",
      "subscription_tier": "professional"
    },
    "storage_quota": {
      "used_gb": 2.5,
      "limit_gb": 5.0,
      "document_count": 25,
      "document_limit": 100
    },
    "preferences": {
      "theme": "light",
      "language": "en",
      "notifications": {
        "email": true,
        "push": false
      }
    },
    "created_at": "2024-01-01T12:00:00Z",
    "last_login_at": "2024-01-15T10:30:00Z"
  }
}
```

#### PUT /users/me
Update current user profile.

**Request Body**:
```json
{
  "first_name": "John",
  "last_name": "Smith",
  "preferences": {
    "theme": "dark",
    "language": "en",
    "notifications": {
      "email": true,
      "push": true
    }
  }
}
```

### 2. Document Management

#### POST /documents/upload
Upload one or multiple documents for processing.

**Request**: `multipart/form-data`
```
files: File[] (required)
titles: string[] (optional)
tags: string[][] (optional)
descriptions: string[] (optional)
batch_id: string (optional)
process_immediately: boolean (default: true)
```

**Response**: 202 Accepted
```json
{
  "success": true,
  "data": {
    "batch_id": "batch-uuid",
    "documents": [
      {
        "id": "doc-uuid-1",
        "title": "Research Paper.pdf",
        "filename": "research_paper.pdf",
        "file_type": "pdf",
        "file_size_bytes": 2048576,
        "status": "uploaded",
        "processing_status": "pending",
        "uploaded_at": "2024-01-01T12:00:00Z"
      }
    ],
    "processing_jobs": [
      {
        "job_id": "job-uuid-1",
        "job_type": "text_extraction",
        "document_id": "doc-uuid-1",
        "status": "pending"
      }
    ]
  },
  "message": "Documents uploaded successfully and processing started"
}
```

#### GET /documents
List documents with filtering and pagination.

**Query Parameters**:
```
page: int = 1
size: int = 20
search: string = ""
status: string[] = ["uploaded", "processing", "indexed", "failed"]
document_type: string[] = ["pdf", "text", "image", "audio", "video"]
tags: string[] = []
sort_by: string = "uploaded_at"
sort_order: string = "desc"
date_from: string = ""
date_to: string = ""
```

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "documents": [
      {
        "id": "doc-uuid",
        "title": "Research Paper",
        "filename": "research.pdf",
        "document_type": "pdf",
        "primary_modality": "text",
        "file_size_bytes": 1024000,
        "status": "indexed",
        "processing_status": "completed",
        "quality_score": 0.85,
        "tags": ["research", "ai"],
        "uploaded_at": "2024-01-01T12:00:00Z",
        "processing_summary": {
          "entities_extracted": 15,
          "relationships_found": 8,
          "processing_time_seconds": 45
        },
        "usage_metrics": {
          "view_count": 12,
          "last_accessed_at": "2024-01-15T10:30:00Z"
        }
      }
    ],
    "pagination": {
      "page": 1,
      "size": 20,
      "total": 150,
      "pages": 8
    }
  }
}
```

#### GET /documents/{document_id}
Get detailed document information.

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "id": "doc-uuid",
    "title": "Research Paper on AI",
    "filename": "research_ai.pdf",
    "original_filename": "research_ai_v2.pdf",
    "document_type": "pdf",
    "primary_modality": "text",
    "file_size_bytes": 2048576,
    "mime_type": "application/pdf",
    "status": "indexed",
    "quality_score": 0.92,
    "extraction_confidence": 0.88,
    "content_text": "Extracted text content...",
    "content_summary": "AI-generated summary...",
    "tags": ["research", "artificial intelligence", "machine learning"],
    "categories": ["academic", "technology"],
    "extracted_metadata": {
      "author": "Dr. Jane Smith",
      "created_date": "2024-01-01",
      "page_count": 15,
      "language": "en"
    },
    "processing_jobs": [
      {
        "job_id": "job-uuid-1",
        "job_type": "text_extraction",
        "status": "completed",
        "progress_percentage": 100.0,
        "started_at": "2024-01-01T12:01:00Z",
        "completed_at": "2024-01-01T12:02:00Z",
        "result_data": {
          "pages_processed": 15,
          "text_length": 5000,
          "ocr_confidence": 0.95
        }
      },
      {
        "job_id": "job-uuid-2",
        "job_type": "entity_extraction",
        "status": "completed",
        "progress_percentage": 100.0,
        "started_at": "2024-01-01T12:02:00Z",
        "completed_at": "2024-01-01T12:03:00Z",
        "result_data": {
          "entities_found": 15,
          "relationships_found": 8,
          "confidence_avg": 0.87
        }
      }
    ],
    "usage_metrics": {
      "view_count": 25,
      "download_count": 3,
      "last_accessed_at": "2024-01-15T10:30:00Z",
      "search_appearances": 18
    },
    "download_url": "/api/v1/documents/doc-uuid/download",
    "preview_url": "/api/v1/documents/doc-uuid/preview",
    "created_at": "2024-01-01T12:00:00Z",
    "updated_at": "2024-01-01T12:05:00Z"
  }
}
```

#### DELETE /documents/{document_id}
Delete a document and all associated data.

**Response**: 204 No Content

#### GET /documents/{document_id}/processing-status
Get real-time processing status.

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "document_id": "doc-uuid",
    "overall_status": "processing",
    "progress_percentage": 65.0,
    "estimated_remaining_seconds": 30,
    "current_step": "entity_extraction",
    "current_step_message": "Extracting entities from page 8 of 15",
    "jobs": [
      {
        "job_id": "job-uuid-1",
        "job_type": "text_extraction",
        "status": "completed",
        "progress_percentage": 100.0,
        "started_at": "2024-01-01T12:01:00Z",
        "completed_at": "2024-01-01T12:02:00Z",
        "result_summary": "Extracted 5000 characters of text from 15 pages"
      },
      {
        "job_id": "job-uuid-2",
        "job_type": "entity_extraction",
        "status": "running",
        "progress_percentage": 60.0,
        "started_at": "2024-01-01T12:02:00Z",
        "estimated_remaining_seconds": 20,
        "current_operation": "Processing page 8 of 15"
      },
      {
        "job_id": "job-uuid-3",
        "job_type": "knowledge_graph_population",
        "status": "pending",
        "progress_percentage": 0.0,
        "queue_position": 2
      }
    ],
    "error_log": [],
    "last_updated": "2024-01-01T12:02:30Z"
  }
}
```

#### POST /documents/{document_id}/retry-processing
Retry failed processing jobs.

**Request Body**:
```json
{
  "job_types": ["entity_extraction", "knowledge_graph_population"],
  "force_retry": false,
  "priority": 5
}
```

#### POST /documents/{document_id}/cancel-processing
Cancel ongoing processing.

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "message": "Processing cancelled successfully",
    "cancelled_jobs": ["entity_extraction", "knowledge_graph_population"],
    "refunded_quota": {
      "processing_time_seconds": 45,
      "cost_usd": 0.0023
    }
  }
}
```

### 3. Search and RAG Queries

#### POST /search/execute
Execute a RAG query with hybrid search.

**Request Body**:
```json
{
  "query_text": "What are the latest developments in artificial intelligence?",
  "query_type": "hybrid",
  "filters": {
    "document_types": ["pdf", "text"],
    "tags": ["research", "ai"],
    "date_range": {
      "from": "2024-01-01",
      "to": "2024-12-31"
    },
    "quality_score_min": 0.7
  },
  "search_parameters": {
    "max_results": 10,
    "include_content_snippets": true,
    "rerank": true,
    "enable_semantic_search": true,
    "enable_graph_search": true,
    "enable_keyword_search": true
  },
  "context": {
    "session_id": "session-uuid",
    "previous_queries": ["machine learning trends"],
    "user_intent": "research"
  }
}
```

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "query_id": "query-uuid",
    "query_text": "What are the latest developments in artificial intelligence?",
    "answer": {
      "text": "Based on the documents analyzed, recent developments in AI include advanced language models, improved computer vision capabilities, and breakthrough applications in healthcare and autonomous systems...",
      "confidence_score": 0.87,
      "sources_cited": 5,
      "token_count": 156,
      "generation_time_ms": 850
    },
    "sources": [
      {
        "document_id": "doc-uuid-1",
        "title": "AI Research 2024",
        "filename": "ai_research_2024.pdf",
        "relevance_score": 0.92,
        "snippet": "Recent breakthroughs in artificial intelligence have focused on large language models...",
        "context_window": {
          "before": "The field of AI has evolved rapidly...",
          "after": "...these developments have implications for various industries."
        },
        "matched_entities": ["artificial intelligence", "language models"],
        "page_numbers": [3, 4],
        "highlight_spans": [
          {
            "start": 150,
            "end": 200,
            "text": "large language models"
          }
        ]
      }
    ],
    "search_metrics": {
      "total_time_ms": 1250,
      "vector_search_time_ms": 450,
      "graph_search_time_ms": 320,
      "keyword_search_time_ms": 180,
      "reranking_time_ms": 300,
      "cache_hit": false,
      "total_results_found": 25,
      "results_returned": 10
    },
    "query_analysis": {
      "intent": "research",
      "complexity": "medium",
      "entities_detected": ["artificial intelligence", "developments"],
      "language": "en"
    }
  }
}
```

#### GET /search/history
Get user's search history with pagination.

**Query Parameters**:
```
page: int = 1
size: int = 20
date_from: string = ""
date_to: string = ""
query_type: string = ""
min_quality_score: float = 0.0
```

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "queries": [
      {
        "id": "query-uuid",
        "query_text": "What are the latest developments in AI?",
        "query_type": "hybrid",
        "answer_preview": "Based on the documents analyzed, recent developments include...",
        "total_results": 25,
        "total_time_ms": 1250,
        "user_satisfaction_score": 4,
        "created_at": "2024-01-15T10:30:00Z",
        "evaluation_metrics": {
          "answer_relevancy": 0.87,
          "faithfulness": 0.92,
          "contextual_relevancy": 0.79
        }
      }
    ],
    "pagination": {
      "page": 1,
      "size": 20,
      "total": 150,
      "pages": 8
    }
  }
}
```

#### GET /search/{query_id}
Get detailed query results and analysis.

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "id": "query-uuid",
    "query_text": "What are the latest developments in artificial intelligence?",
    "query_type": "hybrid",
    "answer": {
      "text": "Full answer text...",
      "confidence_score": 0.87,
      "sources_cited": 5,
      "token_count": 156,
      "generation_time_ms": 850,
      "model_used": "gpt-4-turbo"
    },
    "sources": [
      {
        "document_id": "doc-uuid",
        "title": "AI Research 2024",
        "relevance_score": 0.92,
        "confidence_score": 0.88,
        "snippet": "Recent breakthroughs...",
        "context_before": "The field of AI...",
        "context_after": "...these developments...",
        "page_numbers": [3, 4],
        "highlight_spans": [...],
        "matched_modalities": ["text"],
        "was_clicked": true,
        "clicked_at": "2024-01-15T10:35:00Z"
      }
    ],
    "evaluation_metrics": {
      "rag_triad": {
        "answer_relevancy": {
          "score": 0.87,
          "confidence": 0.92,
          "explanation": "The answer directly addresses the question about AI developments..."
        },
        "faithfulness": {
          "score": 0.92,
          "confidence": 0.88,
          "explanation": "All claims in the answer are supported by the provided sources...",
          "violations": []
        },
        "contextual_relevancy": {
          "score": 0.79,
          "confidence": 0.85,
          "explanation": "The retrieved contexts are relevant to the query..."
        }
      },
      "overall_score": 0.86,
      "meets_thresholds": true
    },
    "performance_metrics": {
      "total_time_ms": 1250,
      "vector_search_time_ms": 450,
      "graph_search_time_ms": 320,
      "keyword_search_time_ms": 180,
      "reranking_time_ms": 300,
      "cache_hit": false
    },
    "user_interaction": {
      "clicked_results": [1, 3],
      "dwell_time_ms": 45000,
      "user_feedback": {
        "rating": 4,
        "comment": "Very comprehensive answer",
        "was_helpful": true
      }
    },
    "created_at": "2024-01-15T10:30:00Z"
  }
}
```

#### POST /search/{query_id}/feedback
Submit feedback for a search query.

**Request Body**:
```json
{
  "rating": 4,
  "was_helpful": true,
  "comment": "Very comprehensive answer with good sources",
  "clicked_results": [1, 3],
  "dwell_time_ms": 45000,
  "improvement_suggestions": "Could include more recent sources"
}
```

### 4. Knowledge Graph

#### GET /knowledge-graph/entities
Get entities with filtering and pagination.

**Query Parameters**:
```
page: int = 1
size: int = 50
entity_type: string = ""
search: string = ""
min_confidence: float = 0.0
document_id: string = ""
```

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "entities": [
      {
        "id": "entity-uuid",
        "entity_name": "Artificial Intelligence",
        "canonical_name": "Artificial Intelligence",
        "entity_type": "CONCEPT",
        "confidence_score": 0.95,
        "aliases": ["AI", "Machine Intelligence"],
        "properties": {
          "definition": "The simulation of human intelligence...",
          "category": "Technology"
        },
        "description": "AI refers to the simulation of human intelligence in machines...",
        "document_count": 25,
        "relationship_count": 48,
        "extraction_method": "spacy_ner",
        "created_at": "2024-01-01T12:00:00Z"
      }
    ],
    "pagination": {
      "page": 1,
      "size": 50,
      "total": 1500,
      "pages": 30
    }
  }
}
```

#### GET /knowledge-graph/entities/{entity_id}
Get detailed entity information.

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "id": "entity-uuid",
    "entity_name": "Artificial Intelligence",
    "canonical_name": "Artificial Intelligence",
    "entity_type": "CONCEPT",
    "confidence_score": 0.95,
    "aliases": ["AI", "Machine Intelligence"],
    "properties": {
      "definition": "The simulation of human intelligence...",
      "category": "Technology",
      "subcategories": ["Machine Learning", "Deep Learning"]
    },
    "description": "Comprehensive description...",
    "extraction_details": {
      "method": "spacy_ner",
      "model_version": "3.4.1",
      "extraction_confidence": 0.95,
      "document_sources": 25
    },
    "relationships": [
      {
        "id": "rel-uuid-1",
        "target_entity": {
          "id": "entity-uuid-2",
          "entity_name": "Machine Learning",
          "entity_type": "CONCEPT"
        },
        "relationship_type": "INCLUDES",
        "confidence": 0.92,
        "bidirectional": true,
        "source_context": "AI includes machine learning as a subset...",
        "source_document_id": "doc-uuid",
        "created_at": "2024-01-01T12:00:00Z"
      }
    ],
    "related_documents": [
      {
        "id": "doc-uuid",
        "title": "AI Research Overview",
        "relevance_score": 0.89,
        "mention_count": 8,
        "first_mention_position": 150
      }
    ],
    "graph_metrics": {
      "degree_centrality": 0.75,
      "betweenness_centrality": 0.62,
      "pagerank_score": 0.84,
      "cluster_coefficient": 0.43
    },
    "created_at": "2024-01-01T12:00:00Z",
    "updated_at": "2024-01-10T15:30:00Z"
  }
}
```

#### GET /knowledge-graph/relationships
Get entity relationships with filtering.

**Query Parameters**:
```
page: int = 1
size: int = 50
source_entity_id: string = ""
target_entity_id: string = ""
relationship_type: string = ""
min_confidence: float = 0.0
```

#### GET /knowledge-graph/explore
Explore knowledge graph with cypher queries.

**Request Body**:
```json
{
  "query": "MATCH (e1:Entity {name: $entity_name})-[r:RELATED_TO]-(e2:Entity) RETURN e1, r, e2 LIMIT 10",
  "parameters": {
    "entity_name": "Artificial Intelligence"
  },
  "include_context": true
}
```

#### GET /knowledge-graph/analytics
Get knowledge graph analytics and insights.

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "overview": {
      "total_entities": 15420,
      "total_relationships": 48350,
      "entity_types": {
        "PERSON": 5250,
        "ORGANIZATION": 3150,
        "CONCEPT": 4200,
        "LOCATION": 1820,
        "DATE": 1000
      },
      "relationship_types": {
        "WORKS_FOR": 8450,
        "LOCATED_IN": 3200,
        "RELATED_TO": 15600,
        "PART_OF": 8900,
        "CREATED_BY": 3200
      }
    },
    "growth_metrics": {
      "entities_added_last_30_days": 1250,
      "relationships_added_last_30_days": 3850,
      "avg_entities_per_document": 28.5,
      "avg_relationships_per_document": 45.2
    },
    "quality_metrics": {
      "avg_entity_confidence": 0.87,
      "avg_relationship_confidence": 0.82,
      "high_quality_entities_percentage": 73.5,
      "validated_relationships_percentage": 68.2
    },
    "network_analysis": {
      "largest_connected_component_size": 12450,
      "avg_clustering_coefficient": 0.42,
      "network_density": 0.0002,
      "avg_path_length": 4.8
    }
  }
}
```

### 5. Evaluation Metrics

#### GET /evaluation/metrics
Get RAG evaluation metrics with filters.

**Query Parameters**:
```
page: int = 1
size: int = 50
date_from: string = ""
date_to: string = ""
metric_type: string = ""
min_score: float = 0.0
```

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "evaluations": [
      {
        "id": "eval-uuid",
        "query_id": "query-uuid",
        "query_text": "What are the latest developments in AI?",
        "rag_triad": {
          "answer_relevancy": {
            "score": 0.87,
            "confidence": 0.92,
            "explanation": "The answer directly addresses the user's question..."
          },
          "faithfulness": {
            "score": 0.92,
            "confidence": 0.88,
            "explanation": "All claims are well-supported by the provided sources...",
            "violations": []
          },
          "contextual_relevancy": {
            "score": 0.79,
            "confidence": 0.85,
            "explanation": "The retrieved contexts are highly relevant..."
          }
        },
        "overall_score": 0.86,
        "meets_thresholds": true,
        "evaluation_metadata": {
          "model": "gpt-4-turbo",
          "version": "v2.1",
          "evaluation_time_ms": 850,
          "tokens_used": 245,
          "cost_usd": 0.0042
        },
        "created_at": "2024-01-15T10:30:00Z"
      }
    ],
    "pagination": {
      "page": 1,
      "size": 50,
      "total": 1250,
      "pages": 25
    }
  }
}
```

#### GET /evaluation/dashboard
Get evaluation dashboard with aggregated metrics.

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "summary": {
      "total_evaluations": 1250,
      "avg_overall_score": 0.82,
      "thresholds_met_percentage": 78.5,
      "evaluation_period": {
        "from": "2024-01-01",
        "to": "2024-01-31"
      }
    },
    "rag_triad_metrics": {
      "answer_relevancy": {
        "avg_score": 0.81,
        "min_score": 0.45,
        "max_score": 0.98,
        "threshold_met_percentage": 75.2,
        "trend": "improving"
      },
      "faithfulness": {
        "avg_score": 0.88,
        "min_score": 0.62,
        "max_score": 1.0,
        "threshold_met_percentage": 89.3,
        "trend": "stable"
      },
      "contextual_relevancy": {
        "avg_score": 0.76,
        "min_score": 0.38,
        "max_score": 0.96,
        "threshold_met_percentage": 71.1,
        "trend": "improving"
      }
    },
    "performance_trends": [
      {
        "date": "2024-01-01",
        "answer_relevancy": 0.78,
        "faithfulness": 0.86,
        "contextual_relevancy": 0.72,
        "overall_score": 0.79
      }
    ],
    "quality_distribution": {
      "excellent": 245,    // 0.9+
      "good": 520,        // 0.7-0.9
      "fair": 380,        // 0.5-0.7
      "poor": 105         // <0.5
    },
    "benchmark_comparisons": {
      "previous_period": {
        "avg_score_change": 0.03,
        "thresholds_met_change": 2.1
      },
      "industry_benchmark": {
        "avg_score_difference": 0.05,
        "percentile_rank": 75
      }
    }
  }
}
```

#### POST /evaluation/benchmark
Run evaluation benchmark on queries.

**Request Body**:
```json
{
  "query_ids": ["query-uuid-1", "query-uuid-2"],
  "evaluation_config": {
    "models": ["gpt-4-turbo", "claude-3-opus"],
    "metrics": ["answer_relevancy", "faithfulness", "contextual_relevancy"],
    "include_explanations": true,
    "batch_size": 10
  },
  "reference_answers": {
    "query-uuid-1": "Reference answer for comparison..."
  }
}
```

### 6. Analytics and Reporting

#### GET /analytics/performance
Get system performance analytics.

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "query_performance": {
      "total_queries": 15420,
      "avg_response_time_ms": 1250,
      "p95_response_time_ms": 2100,
      "success_rate": 0.987,
      "cache_hit_rate": 0.34,
      "daily_breakdown": [
        {
          "date": "2024-01-15",
          "query_count": 520,
          "avg_response_time_ms": 1180,
          "success_rate": 0.991
        }
      ]
    },
    "document_processing": {
      "total_documents": 3250,
      "processing_success_rate": 0.964,
      "avg_processing_time_seconds": 45,
      "queue_depth": 12,
      "processing_by_type": {
        "pdf": 1850,
        "text": 920,
        "image": 280,
        "audio": 120,
        "video": 80
      }
    },
    "system_health": {
      "uptime_percentage": 99.92,
      "error_rate": 0.013,
      "active_sessions": 45,
      "resource_utilization": {
        "cpu_usage_percent": 42.5,
        "memory_usage_percent": 67.8,
        "disk_usage_percent": 34.2
      }
    },
    "user_engagement": {
      "active_users_30_days": 125,
      "avg_queries_per_user": 23.4,
      "user_satisfaction_avg": 4.2,
      "session_duration_avg_minutes": 18.5
    }
  }
}
```

#### GET /analytics/usage
Get usage analytics and statistics.

**Query Parameters**:
```
period: string = "30d" // 7d, 30d, 90d, 1y
metric: string = "" // queries, documents, users, storage
granularity: string = "daily" // hourly, daily, weekly, monthly
```

### 7. File Management

#### GET /documents/{document_id}/download
Download original document file.

**Query Parameters**:
```
version: int = 1
```

**Response**: 200 OK (file stream with appropriate headers)

#### GET /documents/{document_id}/preview
Get document preview.

**Response**: 200 OK
```json
{
  "success": true,
  "data": {
    "preview_type": "text",
    "content": "This is the first 500 characters of the document content...",
    "page_count": 15,
    "preview_image_url": "/api/v1/documents/doc-uuid/preview-image",
    "metadata": {
      "language": "en",
      "encoding": "utf-8",
      "word_count": 5000
    }
  }
}
```

#### GET /documents/{document_id}/preview-image
Get document preview as image (for PDFs, images).

**Response**: 200 OK (image stream)

---

## WebSocket API

### Connection Endpoint
```
wss://api.ragsystem.com/ws
```

### Connection Parameters
- **Authentication**: JWT token sent as query parameter: `?token=eyJhbGciOi...`
- **Organization**: Extracted from token claims
- **Session ID**: Generated for connection tracking

### Message Format
All WebSocket messages follow this format:
```json
{
  "type": "message_type",
  "message_id": "uuid-string",
  "timestamp": "2024-01-01T12:00:00Z",
  "data": {}
}
```

### Event Types

#### 1. Connection Events

##### Client: Connect
```json
{
  "type": "connect",
  "message_id": "uuid-string",
  "data": {
    "client_version": "1.0.0",
    "capabilities": ["document_updates", "processing_status", "notifications"]
  }
}
```

##### Server: Connected
```json
{
  "type": "connected",
  "message_id": "uuid-string",
  "data": {
    "connection_id": "conn-uuid",
    "session_id": "session-uuid",
    "server_version": "1.0.0",
    "subscriptions": []
  }
}
```

#### 2. Document Processing Updates

##### Server: Processing Status Update
```json
{
  "type": "processing_update",
  "message_id": "uuid-string",
  "data": {
    "document_id": "doc-uuid",
    "job_id": "job-uuid",
    "job_type": "entity_extraction",
    "status": "running",
    "progress_percentage": 65.0,
    "current_step": "Extracting entities from page 8 of 15",
    "estimated_remaining_seconds": 20,
    "document_title": "Research Paper.pdf"
  }
}
```

##### Server: Processing Completed
```json
{
  "type": "processing_completed",
  "message_id": "uuid-string",
  "data": {
    "document_id": "doc-uuid",
    "overall_status": "completed",
    "processing_summary": {
      "total_time_seconds": 45,
      "entities_extracted": 15,
      "relationships_found": 8,
      "quality_score": 0.92
    },
    "document_title": "Research Paper.pdf"
  }
}
```

##### Server: Processing Failed
```json
{
  "type": "processing_failed",
  "message_id": "uuid-string",
  "data": {
    "document_id": "doc-uuid",
    "job_id": "job-uuid",
    "job_type": "entity_extraction",
    "error_message": "Entity extraction service unavailable",
    "error_code": "SERVICE_UNAVAILABLE",
    "retry_available": true,
    "retry_after_seconds": 300
  }
}
```

#### 3. Search Updates

##### Server: Search Results Ready
```json
{
  "type": "search_results",
  "message_id": "uuid-string",
  "data": {
    "query_id": "query-uuid",
    "query_text": "What are the latest developments in AI?",
    "status": "completed",
    "results_preview": {
      "answer_preview": "Based on the documents analyzed, recent developments...",
      "sources_count": 5,
      "confidence_score": 0.87
    },
    "total_time_ms": 1250
  }
}
```

#### 4. System Notifications

##### Server: Notification
```json
{
  "type": "notification",
  "message_id": "uuid-string",
  "data": {
    "id": "notif-uuid",
    "type": "info",
    "title": "Document Processing Complete",
    "message": "Your document 'Research Paper.pdf' has been processed successfully.",
    "data": {
      "document_id": "doc-uuid",
      "action_url": "/documents/doc-uuid"
    },
    "priority": "normal",
    "expires_at": "2024-01-02T12:00:00Z"
  }
}
```

##### Server: System Status
```json
{
  "type": "system_status",
  "message_id": "uuid-string",
  "data": {
    "status": "degraded",
    "message": "Some processing services are experiencing delays",
    "affected_services": ["entity_extraction", "knowledge_graph"],
    "estimated_recovery": "2024-01-01T13:00:00Z"
  }
}
```

#### 5. Client Commands

##### Client: Subscribe
```json
{
  "type": "subscribe",
  "message_id": "uuid-string",
  "data": {
    "subscription_type": "document_updates",
    "filters": {
      "document_ids": ["doc-uuid-1", "doc-uuid-2"]
    }
  }
}
```

##### Client: Unsubscribe
```json
{
  "type": "unsubscribe",
  "message_id": "uuid-string",
  "data": {
    "subscription_type": "document_updates",
    "filters": {
      "document_ids": ["doc-uuid-1"]
    }
  }
}
```

##### Client: Ping
```json
{
  "type": "ping",
  "message_id": "uuid-string",
  "data": {
    "timestamp": "2024-01-01T12:00:00Z"
  }
}
```

##### Server: Pong
```json
{
  "type": "pong",
  "message_id": "uuid-string",
  "data": {
    "timestamp": "2024-01-01T12:00:00Z",
    "server_timestamp": "2024-01-01T12:00:00Z"
  }
}
```

### Subscription Types

1. **document_updates**: Updates for specific documents
2. **processing_status**: All processing status updates
3. **notifications**: System and user notifications
4. **search_results**: Search result notifications
5. **system_status**: System health and maintenance updates

---

## Error Handling

### Standard Error Response Format
```json
{
  "success": false,
  "error": {
    "type": "validation_error",
    "code": "INVALID_REQUEST",
    "message": "Invalid request parameters",
    "details": {
      "field": "query_text",
      "issue": "Query text is required and must be at least 3 characters"
    },
    "request_id": "req-uuid",
    "timestamp": "2024-01-01T12:00:00Z"
  }
}
```

### Error Categories

#### 1. Client Errors (4xx)

| HTTP Status | Error Code | Description |
|-------------|------------|-------------|
| 400 | BAD_REQUEST | Invalid request format or parameters |
| 401 | UNAUTHORIZED | Invalid or missing authentication |
| 403 | FORBIDDEN | Insufficient permissions |
| 404 | NOT_FOUND | Resource not found |
| 409 | CONFLICT | Resource conflict (duplicate, etc.) |
| 413 | PAYLOAD_TOO_LARGE | File size exceeds limit |
| 415 | UNSUPPORTED_MEDIA_TYPE | Unsupported file format |
| 422 | VALIDATION_ERROR | Request validation failed |
| 429 | RATE_LIMIT_EXCEEDED | Rate limit exceeded |

#### 2. Server Errors (5xx)

| HTTP Status | Error Code | Description |
|-------------|------------|-------------|
| 500 | INTERNAL_ERROR | Unexpected server error |
| 502 | BAD_GATEWAY | External service unavailable |
| 503 | SERVICE_UNAVAILABLE | Service temporarily unavailable |
| 504 | GATEWAY_TIMEOUT | Request timeout |
| 507 | INSUFFICIENT_STORAGE | Storage quota exceeded |

### Common Error Scenarios

#### Authentication Errors
```json
{
  "success": false,
  "error": {
    "type": "authentication_error",
    "code": "TOKEN_EXPIRED",
    "message": "Authentication token has expired",
    "details": {
      "expired_at": "2024-01-01T11:00:00Z",
      "refresh_required": true
    }
  }
}
```

#### Validation Errors
```json
{
  "success": false,
  "error": {
    "type": "validation_error",
    "code": "INVALID_PARAMETERS",
    "message": "Request validation failed",
    "details": {
      "errors": [
        {
          "field": "query_text",
          "message": "Query text must be between 3 and 1000 characters",
          "current_value": ""
        },
        {
          "field": "max_results",
          "message": "max_results must be between 1 and 100",
          "current_value": 150
        }
      ]
    }
  }
}
```

#### Quota Errors
```json
{
  "success": false,
  "error": {
    "type": "quota_error",
    "code": "STORAGE_QUOTA_EXCEEDED",
    "message": "Storage quota exceeded",
    "details": {
      "current_usage_gb": 4.8,
      "quota_limit_gb": 5.0,
      "available_gb": 0.2,
      "upgrade_required": false
    },
    "retry_after": 86400
  }
}
```

#### Processing Errors
```json
{
  "success": false,
  "error": {
    "type": "processing_error",
    "code": "DOCUMENT_PROCESSING_FAILED",
    "message": "Document processing failed",
    "details": {
      "document_id": "doc-uuid",
      "job_type": "entity_extraction",
      "error_message": "Entity extraction service temporarily unavailable",
      "retry_available": true,
      "retry_after_seconds": 300,
      "retry_count": 1,
      "max_retries": 3
    }
  }
}
```

### Error Recovery Strategies

1. **Retry Logic**: For 5xx errors with `retry-able` flag
2. **Exponential Backoff**: For rate limiting and service unavailable
3. **Graceful Degradation**: For non-critical feature failures
4. **User Feedback**: For validation and input errors
5. **Automatic Refresh**: For expired authentication tokens

---

## Rate Limiting & Quotas

### Rate Limiting Rules

| Endpoint | User Limit | Organization Limit | IP Limit |
|----------|------------|-------------------|----------|
| /auth/login | 10/minute | 100/minute | 20/minute |
| /search/execute | 100/minute | 1000/minute | 200/minute |
| /documents/upload | 20/hour | 200/hour | 50/hour |
| /documents/* | 1000/hour | 10000/hour | 2000/hour |
| /knowledge-graph/* | 500/minute | 5000/minute | 1000/minute |

### Quota Enforcement

#### Storage Quotas
```json
{
  "user_quota": {
    "storage_gb": 5.0,
    "document_count": 100,
    "max_file_size_mb": 50,
    "daily_upload_limit_mb": 500
  },
  "organization_quota": {
    "storage_gb": 100.0,
    "document_count": 5000,
    "max_file_size_mb": 100,
    "daily_upload_limit_mb": 5000
  }
}
```

#### Rate Limit Headers
All API responses include rate limiting headers:
```
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 987
X-RateLimit-Reset: 1640995200
X-RateLimit-Retry-After: 60
X-Quota-Storage-Used: 2.5GB
X-Quota-Storage-Limit: 5.0GB
```

### Quota exceeded Response
```json
{
  "success": false,
  "error": {
    "type": "quota_error",
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "Rate limit exceeded",
    "details": {
      "limit": 1000,
      "window": "hour",
      "reset_at": "2024-01-01T13:00:00Z",
      "retry_after": 1800
    }
  }
}
```

---

## OpenAPI 3.0 Specification

### Complete OpenAPI Spec

```yaml
openapi: 3.0.3
info:
  title: Multimodal Enterprise RAG System API
  description: Comprehensive API for multimodal document processing, RAG queries, and knowledge graph exploration
  version: 1.0.0
  contact:
    name: API Support
    email: api-support@ragsystem.com
  license:
    name: MIT
    url: https://opensource.org/licenses/MIT

servers:
  - url: https://api.ragsystem.com/api/v1
    description: Production server
  - url: https://staging-api.ragsystem.com/api/v1
    description: Staging server
  - url: https://dev-api.ragsystem.com/api/v1
    description: Development server

security:
  - BearerAuth: []

paths:
  # Authentication endpoints
  /auth/login:
    post:
      tags:
        - Authentication
      summary: User login
      description: Authenticate user with email and password
      operationId: login
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/LoginRequest'
      responses:
        '200':
          description: Login successful
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/LoginResponse'
        '401':
          $ref: '#/components/responses/UnauthorizedError'
        '422':
          $ref: '#/components/responses/ValidationError'

  /auth/refresh:
    post:
      tags:
        - Authentication
      summary: Refresh access token
      description: Refresh access token using refresh token
      operationId: refreshToken
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/RefreshTokenRequest'
      responses:
        '200':
          description: Token refreshed successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/RefreshTokenResponse'
        '401':
          $ref: '#/components/responses/UnauthorizedError'

  /auth/logout:
    post:
      tags:
        - Authentication
      summary: User logout
      description: Logout and invalidate tokens
      operationId: logout
      security:
        - BearerAuth: []
      responses:
        '200':
          description: Logout successful
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SuccessResponse'

  # User management endpoints
  /users/me:
    get:
      tags:
        - Users
      summary: Get current user profile
      description: Retrieve current user's profile information
      operationId: getCurrentUser
      security:
        - BearerAuth: []
      responses:
        '200':
          description: User profile retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/UserProfileResponse'
        '401':
          $ref: '#/components/responses/UnauthorizedError'

    put:
      tags:
        - Users
      summary: Update current user profile
      description: Update current user's profile information
      operationId: updateCurrentUser
      security:
        - BearerAuth: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/UpdateUserRequest'
      responses:
        '200':
          description: User profile updated successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/UserProfileResponse'
        '422':
          $ref: '#/components/responses/ValidationError'

  # Document management endpoints
  /documents/upload:
    post:
      tags:
        - Documents
      summary: Upload documents
      description: Upload one or multiple documents for processing
      operationId: uploadDocuments
      security:
        - BearerAuth: []
      requestBody:
        required: true
        content:
          multipart/form-data:
            schema:
              $ref: '#/components/schemas/DocumentUploadRequest'
      responses:
        '202':
          description: Documents uploaded successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/DocumentUploadResponse'
        '413':
          $ref: '#/components/responses/PayloadTooLargeError'
        '422':
          $ref: '#/components/responses/ValidationError'

  /documents:
    get:
      tags:
        - Documents
      summary: List documents
      description: List documents with filtering and pagination
      operationId: listDocuments
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/PageParam'
        - $ref: '#/components/parameters/SizeParam'
        - $ref: '#/components/parameters/SearchParam'
        - $ref: '#/components/parameters/StatusFilterParam'
        - $ref: '#/components/parameters/DocumentTypeFilterParam'
        - $ref: '#/components/parameters/TagsFilterParam'
        - $ref: '#/components/parameters/SortByParam'
        - $ref: '#/components/parameters/SortOrderParam'
        - $ref: '#/components/parameters/DateFromParam'
        - $ref: '#/components/parameters/DateToParam'
      responses:
        '200':
          description: Documents retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/DocumentListResponse'

  /documents/{document_id}:
    get:
      tags:
        - Documents
      summary: Get document details
      description: Get detailed document information
      operationId: getDocument
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/DocumentIdParam'
      responses:
        '200':
          description: Document details retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/DocumentResponse'
        '404':
          $ref: '#/components/responses/NotFoundError'

    delete:
      tags:
        - Documents
      summary: Delete document
      description: Delete a document and all associated data
      operationId: deleteDocument
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/DocumentIdParam'
      responses:
        '204':
          description: Document deleted successfully
        '404':
          $ref: '#/components/responses/NotFoundError'

  /documents/{document_id}/processing-status:
    get:
      tags:
        - Documents
      summary: Get processing status
      description: Get real-time processing status for a document
      operationId: getProcessingStatus
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/DocumentIdParam'
      responses:
        '200':
          description: Processing status retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ProcessingStatusResponse'
        '404':
          $ref: '#/components/responses/NotFoundError'

  /documents/{document_id}/retry-processing:
    post:
      tags:
        - Documents
      summary: Retry processing
      description: Retry failed processing jobs for a document
      operationId: retryProcessing
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/DocumentIdParam'
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/RetryProcessingRequest'
      responses:
        '202':
          description: Processing retry initiated
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/RetryProcessingResponse'

  /documents/{document_id}/cancel-processing:
    post:
      tags:
        - Documents
      summary: Cancel processing
      description: Cancel ongoing processing for a document
      operationId: cancelProcessing
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/DocumentIdParam'
      responses:
        '200':
          description: Processing cancelled successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CancelProcessingResponse'

  # Search endpoints
  /search/execute:
    post:
      tags:
        - Search
      summary: Execute search
      description: Execute a RAG query with hybrid search
      operationId: executeSearch
      security:
        - BearerAuth: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/SearchRequest'
      responses:
        '200':
          description: Search executed successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SearchResponse'
        '422':
          $ref: '#/components/responses/ValidationError'

  /search/history:
    get:
      tags:
        - Search
      summary: Get search history
      description: Get user's search history with pagination
      operationId: getSearchHistory
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/PageParam'
        - $ref: '#/components/parameters/SizeParam'
        - $ref: '#/components/parameters/DateFromParam'
        - $ref: '#/components/parameters/DateToParam'
        - $ref: '#/components/parameters/QueryTypeParam'
        - $ref: '#/components/parameters/MinQualityScoreParam'
      responses:
        '200':
          description: Search history retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SearchHistoryResponse'

  /search/{query_id}:
    get:
      tags:
        - Search
      summary: Get query details
      description: Get detailed query results and analysis
      operationId: getQueryDetails
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/QueryIdParam'
      responses:
        '200':
          description: Query details retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/QueryDetailsResponse'
        '404':
          $ref: '#/components/responses/NotFoundError'

  /search/{query_id}/feedback:
    post:
      tags:
        - Search
      summary: Submit feedback
      description: Submit feedback for a search query
      operationId: submitFeedback
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/QueryIdParam'
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/FeedbackRequest'
      responses:
        '200':
          description: Feedback submitted successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SuccessResponse'

  # Knowledge graph endpoints
  /knowledge-graph/entities:
    get:
      tags:
        - Knowledge Graph
      summary: List entities
      description: Get entities with filtering and pagination
      operationId: listEntities
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/PageParam'
        - $ref: '#/components/parameters/SizeParam'
        - $ref: '#/components/parameters/EntityTypeParam'
        - $ref: '#/components/parameters/SearchParam'
        - $ref: '#/components/parameters/MinConfidenceParam'
        - $ref: '#/components/parameters/DocumentIdParam'
      responses:
        '200':
          description: Entities retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/EntityListResponse'

  /knowledge-graph/entities/{entity_id}:
    get:
      tags:
        - Knowledge Graph
      summary: Get entity details
      description: Get detailed entity information
      operationId: getEntityDetails
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/EntityIdParam'
      responses:
        '200':
          description: Entity details retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/EntityResponse'
        '404':
          $ref: '#/components/responses/NotFoundError'

  /knowledge-graph/relationships:
    get:
      tags:
        - Knowledge Graph
      summary: List relationships
      description: Get entity relationships with filtering
      operationId: listRelationships
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/PageParam'
        - $ref: '#/components/parameters/SizeParam'
        - $ref: '#/components/parameters/SourceEntityIdParam'
        - $ref: '#/components/parameters/TargetEntityIdParam'
        - $ref: '#/components/parameters/RelationshipTypeParam'
        - $ref: '#/components/parameters/MinConfidenceParam'
      responses:
        '200':
          description: Relationships retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/RelationshipListResponse'

  /knowledge-graph/explore:
    get:
      tags:
        - Knowledge Graph
      summary: Explore knowledge graph
      description: Explore knowledge graph with cypher queries
      operationId: exploreKnowledgeGraph
      security:
        - BearerAuth: []
      parameters:
        - name: query
          in: query
          required: true
          schema:
            type: string
        - name: parameters
          in: query
          schema:
            type: string
        - name: include_context
          in: query
          schema:
            type: boolean
            default: false
      responses:
        '200':
          description: Graph exploration results
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/GraphExplorationResponse'

  /knowledge-graph/analytics:
    get:
      tags:
        - Knowledge Graph
      summary: Get graph analytics
      description: Get knowledge graph analytics and insights
      operationId: getGraphAnalytics
      security:
        - BearerAuth: []
      responses:
        '200':
          description: Graph analytics retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/GraphAnalyticsResponse'

  # Evaluation endpoints
  /evaluation/metrics:
    get:
      tags:
        - Evaluation
      summary: Get evaluation metrics
      description: Get RAG evaluation metrics with filters
      operationId: getEvaluationMetrics
      security:
        - BearerAuth: []
      parameters:
        - $ref: '#/components/parameters/PageParam'
        - $ref: '#/components/parameters/SizeParam'
        - $ref: '#/components/parameters/DateFromParam'
        - $ref: '#/components/parameters/DateToParam'
        - $ref: '#/components/parameters/MetricTypeParam'
        - $ref: '#/components/parameters/MinScoreParam'
      responses:
        '200':
          description: Evaluation metrics retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/EvaluationMetricsResponse'

  /evaluation/dashboard:
    get:
      tags:
        - Evaluation
      summary: Get evaluation dashboard
      description: Get evaluation dashboard with aggregated metrics
      operationId: getEvaluationDashboard
      security:
        - BearerAuth: []
      responses:
        '200':
          description: Evaluation dashboard retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/EvaluationDashboardResponse'

  /evaluation/benchmark:
    post:
      tags:
        - Evaluation
      summary: Run evaluation benchmark
      description: Run evaluation benchmark on queries
      operationId: runBenchmark
      security:
        - BearerAuth: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/BenchmarkRequest'
      responses:
        '202':
          description: Benchmark started
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/BenchmarkResponse'

  # Analytics endpoints
  /analytics/performance:
    get:
      tags:
        - Analytics
      summary: Get performance analytics
      description: Get system performance analytics
      operationId: getPerformanceAnalytics
      security:
        - BearerAuth: []
      responses:
        '200':
          description: Performance analytics retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PerformanceAnalyticsResponse'

  /analytics/usage:
    get:
      tags:
        - Analytics
      summary: Get usage analytics
      description: Get usage analytics and statistics
      operationId: getUsageAnalytics
      security:
        - BearerAuth: []
      parameters:
        - name: period
          in: query
          schema:
            type: string
            enum: [7d, 30d, 90d, 1y]
            default: 30d
        - name: metric
          in: query
          schema:
            type: string
            enum: [queries, documents, users, storage]
        - name: granularity
          in: query
          schema:
            type: string
            enum: [hourly, daily, weekly, monthly]
            default: daily
      responses:
        '200':
          description: Usage analytics retrieved successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/UsageAnalyticsResponse'

components:
  securitySchemes:
    BearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT

  parameters:
    PageParam:
      name: page
      in: query
      description: Page number for pagination
      schema:
        type: integer
        minimum: 1
        default: 1

    SizeParam:
      name: size
      in: query
      description: Number of items per page
      schema:
        type: integer
        minimum: 1
        maximum: 100
        default: 20

    SearchParam:
      name: search
      in: query
      description: Search term for filtering
      schema:
        type: string

    StatusFilterParam:
      name: status
      in: query
      description: Filter by status
      schema:
        type: array
        items:
          type: string
          enum: [uploaded, processing, indexed, failed]

    DocumentTypeFilterParam:
      name: document_type
      in: query
      description: Filter by document type
      schema:
        type: array
        items:
          type: string
          enum: [pdf, text, image, audio, video, spreadsheet, presentation]

    TagsFilterParam:
      name: tags
      in: query
      description: Filter by tags
      schema:
        type: array
        items:
          type: string

    SortByParam:
      name: sort_by
      in: query
      description: Field to sort by
      schema:
        type: string
        default: created_at

    SortOrderParam:
      name: sort_order
      in: query
      description: Sort order
      schema:
        type: string
        enum: [asc, desc]
        default: desc

    DateFromParam:
      name: date_from
      in: query
      description: Filter by date from (ISO 8601)
      schema:
        type: string
        format: date-time

    DateToParam:
      name: date_to
      in: query
      description: Filter by date to (ISO 8601)
      schema:
        type: string
        format: date-time

    DocumentIdParam:
      name: document_id
      in: path
      required: true
      description: Document ID
      schema:
        type: string
        format: uuid

    QueryIdParam:
      name: query_id
      in: path
      required: true
      description: Query ID
      schema:
        type: string
        format: uuid

    EntityIdParam:
      name: entity_id
      in: path
      required: true
      description: Entity ID
      schema:
        type: string
        format: uuid

    EntityTypeParam:
      name: entity_type
      in: query
      description: Filter by entity type
      schema:
        type: string

    SourceEntityIdParam:
      name: source_entity_id
      in: query
      description: Filter by source entity ID
      schema:
        type: string
        format: uuid

    TargetEntityIdParam:
      name: target_entity_id
      in: query
      description: Filter by target entity ID
      schema:
        type: string
        format: uuid

    RelationshipTypeParam:
      name: relationship_type
      in: query
      description: Filter by relationship type
      schema:
        type: string

    MinConfidenceParam:
      name: min_confidence
      in: query
      description: Minimum confidence score
      schema:
        type: number
        minimum: 0
        maximum: 1
        default: 0

    QueryTypeParam:
      name: query_type
      in: query
      description: Filter by query type
      schema:
        type: string
        enum: [semantic, keyword, hybrid, graph, multimodal]

    MinQualityScoreParam:
      name: min_quality_score
      in: query
      description: Minimum quality score
      schema:
        type: number
        minimum: 0
        maximum: 1
        default: 0

    MetricTypeParam:
      name: metric_type
      in: query
      description: Filter by metric type
      schema:
        type: string
        enum: [answer_relevancy, faithfulness, contextual_relevancy, overall]

    MinScoreParam:
      name: min_score
      in: query
      description: Minimum score
      schema:
        type: number
        minimum: 0
        maximum: 1
        default: 0

  schemas:
    # Request schemas
    LoginRequest:
      type: object
      required:
        - email
        - password
      properties:
        email:
          type: string
          format: email
        password:
          type: string
          minLength: 8
        remember_me:
          type: boolean
          default: false
        device_info:
          type: object
          properties:
            device_type:
              type: string
            device_name:
              type: string
            user_agent:
              type: string

    RefreshTokenRequest:
      type: object
      required:
        - refresh_token
      properties:
        refresh_token:
          type: string

    UpdateUserRequest:
      type: object
      properties:
        first_name:
          type: string
        last_name:
          type: string
        preferences:
          type: object
          properties:
            theme:
              type: string
              enum: [light, dark]
            language:
              type: string
            notifications:
              type: object
              properties:
                email:
                  type: boolean
                push:
                  type: boolean

    DocumentUploadRequest:
      type: object
      required:
        - files
      properties:
        files:
          type: array
          items:
            type: string
            format: binary
        titles:
          type: array
          items:
            type: string
        tags:
          type: array
          items:
            type: array
            items:
              type: string
        descriptions:
          type: array
          items:
            type: string
        batch_id:
          type: string
          format: uuid
        process_immediately:
          type: boolean
          default: true

    RetryProcessingRequest:
      type: object
      properties:
        job_types:
          type: array
          items:
            type: string
        force_retry:
          type: boolean
          default: false
        priority:
          type: integer
          minimum: 1
          maximum: 10
          default: 5

    SearchRequest:
      type: object
      required:
        - query_text
      properties:
        query_text:
          type: string
          minLength: 3
          maxLength: 1000
        query_type:
          type: string
          enum: [semantic, keyword, hybrid, graph, multimodal]
          default: hybrid
        filters:
          type: object
          properties:
            document_types:
              type: array
              items:
                type: string
            tags:
              type: array
              items:
                type: string
            date_range:
              type: object
              properties:
                from:
                  type: string
                  format: date
                to:
                  type: string
                  format: date
            quality_score_min:
              type: number
              minimum: 0
              maximum: 1
        search_parameters:
          type: object
          properties:
            max_results:
              type: integer
              minimum: 1
              maximum: 100
              default: 10
            include_content_snippets:
              type: boolean
              default: true
            rerank:
              type: boolean
              default: true
            enable_semantic_search:
              type: boolean
              default: true
            enable_graph_search:
              type: boolean
              default: true
            enable_keyword_search:
              type: boolean
              default: true
        context:
          type: object
          properties:
            session_id:
              type: string
              format: uuid
            previous_queries:
              type: array
              items:
                type: string
            user_intent:
              type: string

    FeedbackRequest:
      type: object
      properties:
        rating:
          type: integer
          minimum: 1
          maximum: 5
        was_helpful:
          type: boolean
        comment:
          type: string
          maxLength: 1000
        clicked_results:
          type: array
          items:
            type: integer
        dwell_time_ms:
          type: integer
          minimum: 0
        improvement_suggestions:
          type: string
          maxLength: 500

    BenchmarkRequest:
      type: object
      required:
        - query_ids
      properties:
        query_ids:
          type: array
          items:
            type: string
            format: uuid
        evaluation_config:
          type: object
          properties:
            models:
              type: array
              items:
                type: string
            metrics:
              type: array
              items:
                type: string
                enum: [answer_relevancy, faithfulness, contextual_relevancy]
            include_explanations:
              type: boolean
              default: true
            batch_size:
              type: integer
              minimum: 1
              maximum: 50
              default: 10
        reference_answers:
          type: object
          additionalProperties:
            type: string

    # Response schemas
    SuccessResponse:
      type: object
      properties:
        success:
          type: boolean
          example: true
        message:
          type: string
        request_id:
          type: string
          format: uuid
        timestamp:
          type: string
          format: date-time

    LoginResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                access_token:
                  type: string
                refresh_token:
                  type: string
                token_type:
                  type: string
                  example: Bearer
                expires_in:
                  type: integer
                user:
                  $ref: '#/components/schemas/UserProfile'

    RefreshTokenResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                access_token:
                  type: string
                expires_in:
                  type: integer

    UserProfile:
      type: object
      properties:
        id:
          type: string
          format: uuid
        email:
          type: string
          format: email
        first_name:
          type: string
        last_name:
          type: string
        role:
          type: string
          enum: [admin, user, content_manager, analyst]
        organization:
          type: object
          properties:
            id:
              type: string
              format: uuid
            name:
              type: string
            subscription_tier:
              type: string
              enum: [starter, professional, enterprise]

    UserProfileResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              allOf:
                - $ref: '#/components/schemas/UserProfile'
                - type: object
                  properties:
                    storage_quota:
                      type: object
                      properties:
                        used_gb:
                          type: number
                        limit_gb:
                          type: number
                        document_count:
                          type: integer
                        document_limit:
                          type: integer
                    preferences:
                      type: object
                    created_at:
                      type: string
                      format: date-time
                    last_login_at:
                      type: string
                      format: date-time

    DocumentUploadResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                batch_id:
                  type: string
                  format: uuid
                documents:
                  type: array
                  items:
                    $ref: '#/components/schemas/DocumentSummary'
                processing_jobs:
                  type: array
                  items:
                    $ref: '#/components/schemas/ProcessingJobSummary'

    DocumentSummary:
      type: object
      properties:
        id:
          type: string
          format: uuid
        title:
          type: string
        filename:
          type: string
        document_type:
          type: string
          enum: [pdf, text, image, audio, video, spreadsheet, presentation]
        file_size_bytes:
          type: integer
        status:
          type: string
          enum: [uploaded, processing, indexed, failed]
        processing_status:
          type: string
        uploaded_at:
          type: string
          format: date-time

    DocumentListResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                documents:
                  type: array
                  items:
                    allOf:
                      - $ref: '#/components/schemas/DocumentSummary'
                      - type: object
                        properties:
                          primary_modality:
                            type: string
                          quality_score:
                            type: number
                          tags:
                            type: array
                            items:
                              type: string
                          processing_summary:
                            type: object
                          usage_metrics:
                            type: object
                pagination:
                  $ref: '#/components/schemas/Pagination'

    DocumentResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              allOf:
                - $ref: '#/components/schemas/DocumentSummary'
                - type: object
                  properties:
                    original_filename:
                      type: string
                    mime_type:
                      type: string
                    quality_score:
                      type: number
                    extraction_confidence:
                      type: number
                    content_text:
                      type: string
                    content_summary:
                      type: string
                    categories:
                      type: array
                      items:
                        type: string
                    extracted_metadata:
                      type: object
                    processing_jobs:
                      type: array
                      items:
                        $ref: '#/components/schemas/ProcessingJob'
                    usage_metrics:
                      type: object
                    download_url:
                      type: string
                    preview_url:
                      type: string
                    created_at:
                      type: string
                      format: date-time
                    updated_at:
                      type: string
                      format: date-time

    ProcessingJob:
      type: object
      properties:
        job_id:
          type: string
          format: uuid
        job_type:
          type: string
        status:
          type: string
          enum: [pending, running, completed, failed, cancelled]
        progress_percentage:
          type: number
          minimum: 0
          maximum: 100
        started_at:
          type: string
          format: date-time
        completed_at:
          type: string
          format: date-time
        result_data:
          type: object
        error_message:
          type: string

    ProcessingStatusResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                document_id:
                  type: string
                  format: uuid
                overall_status:
                  type: string
                progress_percentage:
                  type: number
                estimated_remaining_seconds:
                  type: integer
                current_step:
                  type: string
                current_step_message:
                  type: string
                jobs:
                  type: array
                  items:
                    $ref: '#/components/schemas/ProcessingJob'
                error_log:
                  type: array
                  items:
                    type: string
                last_updated:
                  type: string
                  format: date-time

    RetryProcessingResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                message:
                  type: string
                jobs_retried:
                  type: array
                  items:
                    type: string
                retry_id:
                  type: string
                  format: uuid

    CancelProcessingResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                message:
                  type: string
                cancelled_jobs:
                  type: array
                  items:
                    type: string
                refunded_quota:
                  type: object

    SearchResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                query_id:
                  type: string
                  format: uuid
                query_text:
                  type: string
                answer:
                  type: object
                  properties:
                    text:
                      type: string
                    confidence_score:
                      type: number
                    sources_cited:
                      type: integer
                    token_count:
                      type: integer
                    generation_time_ms:
                      type: integer
                sources:
                  type: array
                  items:
                    $ref: '#/components/schemas/SearchSource'
                search_metrics:
                  type: object
                query_analysis:
                  type: object

    SearchSource:
      type: object
      properties:
        document_id:
          type: string
          format: uuid
        title:
          type: string
        filename:
          type: string
        relevance_score:
          type: number
        snippet:
          type: string
        context_window:
          type: object
        matched_entities:
          type: array
          items:
            type: string
        page_numbers:
          type: array
          items:
            type: integer
        highlight_spans:
          type: array
          items:
            type: object

    SearchHistoryResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                queries:
                  type: array
                  items:
                    $ref: '#/components/schemas/QuerySummary'
                pagination:
                  $ref: '#/components/schemas/Pagination'

    QuerySummary:
      type: object
      properties:
        id:
          type: string
          format: uuid
        query_text:
          type: string
        query_type:
          type: string
        answer_preview:
          type: string
        total_results:
          type: integer
        total_time_ms:
          type: integer
        user_satisfaction_score:
          type: integer
        created_at:
          type: string
          format: date-time
        evaluation_metrics:
          type: object

    QueryDetailsResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              allOf:
                - $ref: '#/components/schemas/SearchResponse'
                - type: object
                  properties:
                    evaluation_metrics:
                      type: object
                      properties:
                        rag_triad:
                          type: object
                        overall_score:
                          type: number
                        meets_thresholds:
                          type: boolean
                    performance_metrics:
                      type: object
                    user_interaction:
                      type: object
                    created_at:
                      type: string
                      format: date-time

    EntityListResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                entities:
                  type: array
                  items:
                    $ref: '#/components/schemas/EntitySummary'
                pagination:
                  $ref: '#/components/schemas/Pagination'

    EntitySummary:
      type: object
      properties:
        id:
          type: string
          format: uuid
        entity_name:
          type: string
        canonical_name:
          type: string
        entity_type:
          type: string
        confidence_score:
          type: number
        aliases:
          type: array
          items:
            type: string
        properties:
          type: object
        description:
          type: string
        document_count:
          type: integer
        relationship_count:
          type: integer
        extraction_method:
          type: string
        created_at:
          type: string
          format: date-time

    EntityResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              allOf:
                - $ref: '#/components/schemas/EntitySummary'
                - type: object
                  properties:
                    extraction_details:
                      type: object
                    relationships:
                      type: array
                      items:
                        $ref: '#/components/schemas/Relationship'
                    related_documents:
                      type: array
                      items:
                        type: object
                    graph_metrics:
                      type: object
                    updated_at:
                      type: string
                      format: date-time

    Relationship:
      type: object
      properties:
        id:
          type: string
          format: uuid
        target_entity:
          type: object
          properties:
            id:
              type: string
              format: uuid
            entity_name:
              type: string
            entity_type:
              type: string
        relationship_type:
          type: string
        confidence:
          type: number
        bidirectional:
          type: boolean
        source_context:
          type: string
        source_document_id:
          type: string
          format: uuid
        created_at:
          type: string
          format: date-time

    RelationshipListResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                relationships:
                  type: array
                  items:
                    $ref: '#/components/schemas/Relationship'
                pagination:
                  $ref: '#/components/schemas/Pagination'

    GraphExplorationResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                nodes:
                  type: array
                  items:
                    type: object
                edges:
                  type: array
                  items:
                    type: object
                context:
                  type: object
                execution_time_ms:
                  type: integer

    GraphAnalyticsResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                overview:
                  type: object
                growth_metrics:
                  type: object
                quality_metrics:
                  type: object
                network_analysis:
                  type: object

    EvaluationMetricsResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                evaluations:
                  type: array
                  items:
                    $ref: '#/components/schemas/Evaluation'
                pagination:
                  $ref: '#/components/schemas/Pagination'

    Evaluation:
      type: object
      properties:
        id:
          type: string
          format: uuid
        query_id:
          type: string
          format: uuid
        query_text:
          type: string
        rag_triad:
          type: object
          properties:
            answer_relevancy:
              $ref: '#/components/schemas/MetricScore'
            faithfulness:
              $ref: '#/components/schemas/MetricScore'
            contextual_relevancy:
              $ref: '#/components/schemas/MetricScore'
        overall_score:
          type: number
        meets_thresholds:
          type: boolean
        evaluation_metadata:
          type: object
        created_at:
          type: string
          format: date-time

    MetricScore:
      type: object
      properties:
        score:
          type: number
        confidence:
          type: number
        explanation:
          type: string

    EvaluationDashboardResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                summary:
                  type: object
                rag_triad_metrics:
                  type: object
                performance_trends:
                  type: array
                  items:
                    type: object
                quality_distribution:
                  type: object
                benchmark_comparisons:
                  type: object

    BenchmarkResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                benchmark_id:
                  type: string
                  format: uuid
                status:
                  type: string
                total_queries:
                  type: integer
                estimated_completion:
                  type: string
                  format: date-time

    PerformanceAnalyticsResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                query_performance:
                  type: object
                document_processing:
                  type: object
                system_health:
                  type: object
                user_engagement:
                  type: object

    UsageAnalyticsResponse:
      allOf:
        - $ref: '#/components/schemas/SuccessResponse'
        - type: object
          properties:
            data:
              type: object
              properties:
                metrics:
                  type: array
                  items:
                    type: object
                summary:
                  type: object

    # Common schemas
    Pagination:
      type: object
      properties:
        page:
          type: integer
        size:
          type: integer
        total:
          type: integer
        pages:
          type: integer

    ProcessingJobSummary:
      type: object
      properties:
        job_id:
          type: string
          format: uuid
        job_type:
          type: string
        document_id:
          type: string
          format: uuid
        status:
          type: string

    # Error schemas
    Error:
      type: object
      properties:
        type:
          type: string
        code:
          type: string
        message:
          type: string
        details:
          type: object
        request_id:
          type: string
          format: uuid
        timestamp:
          type: string
          format: date-time

    ErrorResponse:
      type: object
      properties:
        success:
          type: boolean
          example: false
        error:
          $ref: '#/components/schemas/Error'

  responses:
    UnauthorizedError:
      description: Authentication failed
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorResponse'
          example:
            success: false
            error:
              type: authentication_error
              code: UNAUTHORIZED
              message: Invalid or missing authentication token

    ForbiddenError:
      description: Insufficient permissions
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorResponse'

    NotFoundError:
      description: Resource not found
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorResponse'

    ValidationError:
      description: Request validation failed
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorResponse'

    PayloadTooLargeError:
      description: File size exceeds limit
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorResponse'

    RateLimitError:
      description: Rate limit exceeded
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorResponse'

tags:
  - name: Authentication
    description: Authentication and authorization endpoints
  - name: Users
    description: User management endpoints
  - name: Documents
    description: Document upload and management endpoints
  - name: Search
    description: Search and RAG query endpoints
  - name: Knowledge Graph
    description: Knowledge graph exploration endpoints
  - name: Evaluation
    description: RAG evaluation and metrics endpoints
  - name: Analytics
    description: System analytics and reporting endpoints
```

---

## Examples & Usage

### 1. Authentication Flow

```javascript
// Login
const loginResponse = await fetch('/api/v1/auth/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    email: 'user@example.com',
    password: 'securePassword123',
    remember_me: false
  })
});

const { data } = await loginResponse.json();
const token = data.access_token;

// Use token for authenticated requests
const profileResponse = await fetch('/api/v1/users/me', {
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  }
});
```

### 2. Document Upload with Progress Tracking

```javascript
// Upload document
const formData = new FormData();
formData.append('files', fileInput.files[0]);
formData.append('tags', JSON.stringify(['research', 'ai']));
formData.append('process_immediately', 'true');

const uploadResponse = await fetch('/api/v1/documents/upload', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`
  },
  body: formData
});

const { data } = await uploadResponse.json();
const documentId = data.documents[0].id;

// Setup WebSocket for processing updates
const ws = new WebSocket(`wss://api.ragsystem.com/ws?token=${token}`);

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.type === 'processing_update' &&
      message.data.document_id === documentId) {
    updateProgressBar(message.data.progress_percentage);
  }
};
```

### 3. Search with Evaluation

```javascript
// Execute search
const searchResponse = await fetch('/api/v1/search/execute', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    query_text: 'What are the latest developments in artificial intelligence?',
    query_type: 'hybrid',
    filters: {
      document_types: ['pdf', 'text'],
      quality_score_min: 0.7
    },
    search_parameters: {
      max_results: 10,
      rerank: true
    }
  })
});

const { data } = await searchResponse.json();
console.log('Answer:', data.answer.text);
console.log('Sources:', data.sources);

// Submit feedback
await fetch(`/api/v1/search/${data.query_id}/feedback`, {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    rating: 4,
    was_helpful: true,
    comment: 'Very comprehensive answer'
  })
});
```

### 4. Knowledge Graph Exploration

```javascript
// Get entity details
const entityResponse = await fetch('/api/v1/knowledge-graph/entities/entity-uuid', {
  headers: {
    'Authorization': `Bearer ${token}`
  }
});

const { data: entity } = await entityResponse.json();
console.log('Entity:', entity.entity_name);
console.log('Relationships:', entity.relationships);

// Explore graph with cypher query
const explorationResponse = await fetch('/api/v1/knowledge-graph/explore', {
  method: 'GET',
  headers: {
    'Authorization': `Bearer ${token}`
  },
  body: JSON.stringify({
    query: 'MATCH (e:Entity {name: $entity_name})-[r:RELATED_TO]-(related:Entity) RETURN e, r, related LIMIT 10',
    parameters: { entity_name: 'Artificial Intelligence' }
  })
});
```

### 5. Error Handling

```javascript
async function apiCall(url, options = {}) {
  try {
    const response = await fetch(url, {
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
        ...options.headers
      },
      ...options
    });

    const data = await response.json();

    if (!response.ok) {
      // Handle different error types
      switch (response.status) {
        case 401:
          // Token expired - refresh
          await refreshToken();
          return apiCall(url, options); // Retry with new token
        case 429:
          // Rate limited - wait and retry
          const retryAfter = data.error.retry_after || 60;
          await new Promise(resolve => setTimeout(resolve, retryAfter * 1000));
          return apiCall(url, options); // Retry after delay
        default:
          throw new Error(data.error.message || 'API call failed');
      }
    }

    return data;
  } catch (error) {
    console.error('API call failed:', error);
    throw error;
  }
}
```

### 6. Batch Operations

```javascript
// Batch document upload
const files = [file1, file2, file3];
const uploadPromises = files.map(file => {
  const formData = new FormData();
  formData.append('files', file);
  return fetch('/api/v1/documents/upload', {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${token}` },
    body: formData
  });
});

const results = await Promise.all(uploadPromises);
const documentIds = results.map(r => r.json().data.documents[0].id);

// Batch processing retry
await fetch('/api/v1/documents/batch-process', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    document_ids: documentIds,
    job_types: ['entity_extraction'],
    priority: 5
  })
});
```

---

## Conclusion

This comprehensive API specification provides a complete foundation for the Multimodal Enterprise RAG System UI, covering:

1. **Complete REST API** with all endpoints for document management, search, knowledge graph, evaluation, and analytics
2. **WebSocket API** for real-time updates and notifications
3. **Comprehensive authentication** with JWT tokens and multi-tenant support
4. **Detailed error handling** with specific error codes and recovery strategies
5. **Rate limiting and quotas** with proper enforcement mechanisms
6. **OpenAPI 3.0 specification** for API documentation and code generation
7. **Practical examples** demonstrating common usage patterns

The API is designed to be:
- **Scalable**: Supports multi-tenant architecture with horizontal scaling
- **Secure**: Implements proper authentication, authorization, and data isolation
- **Reliable**: Includes comprehensive error handling and retry mechanisms
- **Performant**: Optimized for real-time interactions and batch processing
- **Observable**: Provides detailed metrics and monitoring capabilities
- **Developer-friendly**: Well-documented with clear examples and consistent patterns

This API specification serves as the foundation for building a robust, scalable, and user-friendly multimodal RAG system interface that can handle enterprise requirements while maintaining excellent user experience.
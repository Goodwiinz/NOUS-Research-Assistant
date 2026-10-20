# Comprehensive API Documentation - Multimodal Enterprise RAG System

> **Stale / historical (2026-10-17):** It covers only part of the API and its paths and schemas may have drifted. The source of truth is the generated `backend/openapi.json` (also served at `/docs` when `DEBUG` is on). Current contracts: [`docs/engineering/`](../engineering/README.md).

## 📋 Table of Contents

1. [API Overview](#api-overview)
2. [Authentication](#authentication)
3. [Document Management](#document-management)
4. [Search Endpoints](#search-endpoints)
5. [Knowledge Graph](#knowledge-graph)
6. [Analytics & Monitoring](#analytics--monitoring)
7. [User Management](#user-management)
8. [Processing & Workflows](#processing--workflows)
9. [Agent Chat (LangGraph)](#agent-chat-langgraph)
10. [Error Handling](#error-handling)
11. [Rate Limiting](#rate-limiting)
12. [API Versioning](#api-versioning)
13. [SDKs & Libraries](#sdks--libraries)

---

## API Overview

### Base URLs

- **Production**: `https://api.rag.yourdomain.com/api/v1`
- **Staging**: `https://staging-api.rag.yourdomain.com/api/v1`
- **Development**: `http://localhost:8000/api/v1`

### Supported Formats

- **Request Format**: JSON
- **Response Format**: JSON
- **File Upload**: Multipart/form-data

### Authentication

- **Method**: JWT Bearer Token
- **Header**: `Authorization: Bearer <token>`
- **Token Expiry**: 24 hours (refreshable)

### Rate Limiting

- **Standard**: 100 requests/minute
- **Premium**: 1000 requests/minute
- **Enterprise**: Unlimited

---

## Authentication

### Register User

```http
POST /api/v1/auth/register
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "SecurePass123!",
  "first_name": "John",
  "last_name": "Doe",
  "organization_name": "Acme Corp"
}
```

**Response (201)**:

```json
{
  "success": true,
  "data": {
    "user": {
      "id": "uuid",
      "email": "user@example.com",
      "first_name": "John",
      "last_name": "Doe",
      "organization": {
        "id": "uuid",
        "name": "Acme Corp"
      },
      "role": "USER",
      "created_at": "2024-01-01T00:00:00Z"
    },
    "tokens": {
      "access": "jwt_token_here",
      "refresh": "refresh_token_here",
      "expires_in": 86400
    }
  }
}
```

### Login User

```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "SecurePass123!"
}
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "user": {
      "id": "uuid",
      "email": "user@example.com",
      "first_name": "John",
      "last_name": "Doe",
      "organization": {
        "id": "uuid",
        "name": "Acme Corp"
      },
      "role": "USER"
    },
    "tokens": {
      "access": "jwt_token_here",
      "refresh": "refresh_token_here",
      "expires_in": 86400
    }
  }
}
```

### Refresh Token

```http
POST /api/v1/auth/refresh
Authorization: Bearer <refresh_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "access": "new_jwt_token_here",
    "expires_in": 86400
  }
}
```

### Logout User

```http
POST /api/v1/auth/logout
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "message": "Successfully logged out"
}
```

---

## Document Management

### Upload Document

```http
POST /api/v1/documents/upload
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file: <file_data>
title: "Document Title"
description: "Document description"
tags: ["tag1", "tag2"]
metadata: {
  "author": "John Doe",
  "department": "Engineering"
}
```

**Response (201)**:

```json
{
  "success": true,
  "data": {
    "document": {
      "id": "uuid",
      "filename": "document.pdf",
      "title": "Document Title",
      "description": "Document description",
      "file_type": "pdf",
      "file_size": 1024000,
      "status": "processing",
      "tags": ["tag1", "tag2"],
      "metadata": {
        "author": "John Doe",
        "department": "Engineering"
      },
      "organization_id": "uuid",
      "uploaded_by": "uuid",
      "created_at": "2024-01-01T00:00:00Z",
      "updated_at": "2024-01-01T00:00:00Z"
    },
    "processing_job": {
      "id": "uuid",
      "status": "queued",
      "estimated_duration": 30
    }
  }
}
```

### Get Documents

```http
GET /api/v1/documents?page=1&limit=20&search=keyword&file_type=pdf&tag=tag1
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "documents": [
      {
        "id": "uuid",
        "filename": "document.pdf",
        "title": "Document Title",
        "description": "Document description",
        "file_type": "pdf",
        "file_size": 1024000,
        "status": "processed",
        "tags": ["tag1", "tag2"],
        "metadata": {
          "author": "John Doe",
          "department": "Engineering"
        },
        "processing_status": {
          "stage": "completed",
          "progress": 100,
          "entities_extracted": 15,
          "pages_processed": 10
        },
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z"
      }
    ],
    "pagination": {
      "page": 1,
      "limit": 20,
      "total": 100,
      "pages": 5
    },
    "filters": {
      "file_types": ["pdf", "docx", "txt"],
      "tags": ["tag1", "tag2", "tag3"]
    }
  }
}
```

### Get Document Details

```http
GET /api/v1/documents/{document_id}
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "document": {
      "id": "uuid",
      "filename": "document.pdf",
      "title": "Document Title",
      "description": "Document description",
      "file_type": "pdf",
      "file_size": 1024000,
      "status": "processed",
      "tags": ["tag1", "tag2"],
      "metadata": {
        "author": "John Doe",
        "department": "Engineering"
      },
      "content": {
        "text": "Extracted text content...",
        "entities": [
          {
            "text": "John Doe",
            "type": "PERSON",
            "confidence": 0.95,
            "start_pos": 10,
            "end_pos": 18
          }
        ],
        "pages": 10,
        "language": "en"
      },
      "processing_history": [
        {
          "stage": "ocr",
          "status": "completed",
          "duration": 5.2,
          "timestamp": "2024-01-01T00:00:00Z"
        },
        {
          "stage": "entity_extraction",
          "status": "completed",
          "duration": 2.1,
          "timestamp": "2024-01-01T00:00:00Z"
        }
      ],
      "created_at": "2024-01-01T00:00:00Z",
      "updated_at": "2024-01-01T00:00:00Z"
    }
  }
}
```

### Update Document

```http
PUT /api/v1/documents/{document_id}
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "title": "Updated Title",
  "description": "Updated description",
  "tags": ["new_tag"],
  "metadata": {
    "author": "Jane Doe",
    "department": "Marketing"
  }
}
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "document": {
      "id": "uuid",
      "title": "Updated Title",
      "description": "Updated description",
      "tags": ["new_tag"],
      "metadata": {
        "author": "Jane Doe",
        "department": "Marketing"
      },
      "updated_at": "2024-01-01T00:00:00Z"
    }
  }
}
```

### Delete Document

```http
DELETE /api/v1/documents/{document_id}
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "message": "Document deleted successfully"
}
```

### Get Processing Status

```http
GET /api/v1/documents/{document_id}/processing-status
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "processing_job": {
      "id": "uuid",
      "document_id": "uuid",
      "status": "processing",
      "current_stage": "entity_extraction",
      "progress": 65,
      "stages": [
        {
          "name": "upload",
          "status": "completed",
          "duration": 0.5,
          "timestamp": "2024-01-01T00:00:00Z"
        },
        {
          "name": "ocr",
          "status": "completed",
          "duration": 5.2,
          "timestamp": "2024-01-01T00:00:00Z"
        },
        {
          "name": "entity_extraction",
          "status": "processing",
          "progress": 30,
          "timestamp": "2024-01-01T00:00:00Z"
        },
        {
          "name": "vector_embedding",
          "status": "pending",
          "timestamp": null
        }
      ],
      "estimated_completion": "2024-01-01T00:01:00Z",
      "created_at": "2024-01-01T00:00:00Z"
    }
  }
}
```

---

## Search Endpoints

### Hybrid Search

```http
POST /api/v1/search/hybrid
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "query": "What are the latest developments in AI?",
  "filters": {
    "file_types": ["pdf", "docx"],
    "tags": ["technology", "ai"],
    "date_range": {
      "start": "2024-01-01",
      "end": "2024-12-31"
    },
    "authors": ["John Doe", "Jane Smith"]
  },
  "options": {
    "top_k": 10,
    "include_metadata": true,
    "search_mode": "hybrid",
    "rerank": true,
    "include_highlights": true
  }
}
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "query": "What are the latest developments in AI?",
    "results": [
      {
        "id": "uuid",
        "document_id": "uuid",
        "title": "AI Developments in 2024",
        "content": "The latest developments in AI include...",
        "score": 0.95,
        "highlights": [
          "latest developments in <em>AI</em> include",
          "breakthrough in <em>machine learning</em>"
        ],
        "metadata": {
          "file_type": "pdf",
          "author": "John Doe",
          "created_at": "2024-01-01T00:00:00Z",
          "page_number": 3,
          "section": "Introduction"
        },
        "search_breakdown": {
          "vector_score": 0.92,
          "graph_score": 0.88,
          "keyword_score": 0.95,
          "final_score": 0.95
        },
        "entities": [
          {
            "text": "AI",
            "type": "TECHNOLOGY",
            "confidence": 0.98
          }
        ]
      }
    ],
    "aggregations": {
      "file_types": {
        "pdf": 6,
        "docx": 3,
        "txt": 1
      },
      "authors": {
        "John Doe": 4,
        "Jane Smith": 3,
        "Bob Johnson": 3
      },
      "tags": {
        "technology": 7,
        "ai": 8,
        "innovation": 2
      }
    },
    "performance": {
      "total_time_ms": 245,
      "vector_search_time_ms": 120,
      "graph_search_time_ms": 80,
      "reranking_time_ms": 45
    },
    "total_results": 10,
    "query_id": "uuid"
  }
}
```

### Vector Search

```http
POST /api/v1/search/vector
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "query": "artificial intelligence applications",
  "filters": {
    "modalities": ["text", "image"],
    "similarity_threshold": 0.7
  },
  "options": {
    "top_k": 5,
    "include_vectors": false
  }
}
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "results": [
      {
        "id": "uuid",
        "document_id": "uuid",
        "content": "Artificial intelligence applications...",
        "similarity_score": 0.92,
        "metadata": {
          "modality": "text",
          "embedding_model": "text-embedding-ada-002"
        }
      }
    ],
    "query_embedding": [0.1, 0.2, 0.3, ...],
    "performance": {
      "search_time_ms": 45
    }
  }
}
```

### Graph Search

```http
POST /api/v1/search/graph
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "query": "John Doe's projects",
  "search_type": "entity_traversal",
  "options": {
    "max_depth": 3,
    "include_relationships": true,
    "entity_types": ["PERSON", "PROJECT", "ORGANIZATION"]
  }
}
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "entities": [
      {
        "id": "uuid",
        "name": "John Doe",
        "type": "PERSON",
        "properties": {
          "role": "Engineer",
          "department": "Engineering"
        },
        "relationships": [
          {
            "type": "WORKS_ON",
            "target": {
              "id": "uuid",
              "name": "Project Alpha",
              "type": "PROJECT"
            },
            "properties": {
              "role": "Lead Developer",
              "start_date": "2024-01-01"
            }
          }
        ]
      }
    ],
    "paths": [
      {
        "entities": ["John Doe", "Project Alpha", "Acme Corp"],
        "relationships": ["WORKS_ON", "BELONGS_TO"],
        "path_length": 2
      }
    ],
    "performance": {
      "graph_traversal_time_ms": 80
    }
  }
}
```

### Keyword Search

```http
POST /api/v1/search/keyword
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "query": "machine learning algorithms",
  "options": {
    "fuzzy_search": true,
    "phrase_search": false,
    "highlight_results": true
  }
}
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "results": [
      {
        "id": "uuid",
        "document_id": "uuid",
        "content": "Machine learning algorithms are...",
        "score": 0.88,
        "highlights": ["<em>Machine learning</em> <em>algorithms</em> are"],
        "metadata": {
          "file_type": "pdf",
          "page_number": 5
        }
      }
    ],
    "query_analysis": {
      "tokens": ["machine", "learning", "algorithms"],
      "expanded_terms": ["machine learning", "deep learning", "neural networks"]
    }
  }
}
```

### Search Suggestions

```http
GET /api/v1/search/suggestions?q=machine&limit=5
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "suggestions": [
      {
        "text": "machine learning",
        "type": "phrase",
        "frequency": 150
      },
      {
        "text": "machine learning algorithms",
        "type": "phrase",
        "frequency": 45
      },
      {
        "text": "machine vision",
        "type": "phrase",
        "frequency": 23
      }
    ]
  }
}
```

---

## Knowledge Graph

### Get Entities

```http
GET /api/v1/graph/entities?type=PERSON&limit=20&offset=0
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "entities": [
      {
        "id": "uuid",
        "name": "John Doe",
        "type": "PERSON",
        "properties": {
          "role": "Engineer",
          "department": "Engineering",
          "email": "john.doe@company.com"
        },
        "relationships_count": 15,
        "documents_count": 8,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z"
      }
    ],
    "pagination": {
      "limit": 20,
      "offset": 0,
      "total": 150
    },
    "entity_types": ["PERSON", "ORGANIZATION", "PROJECT", "TECHNOLOGY"]
  }
}
```

### Get Entity Details

```http
GET /api/v1/graph/entities/{entity_id}?include_relationships=true&include_documents=true
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "entity": {
      "id": "uuid",
      "name": "John Doe",
      "type": "PERSON",
      "properties": {
        "role": "Engineer",
        "department": "Engineering",
        "email": "john.doe@company.com",
        "join_date": "2020-01-01"
      },
      "relationships": [
        {
          "id": "uuid",
          "type": "WORKS_ON",
          "target_entity": {
            "id": "uuid",
            "name": "Project Alpha",
            "type": "PROJECT"
          },
          "properties": {
            "role": "Lead Developer",
            "start_date": "2024-01-01"
          },
          "strength": 0.95,
          "document_sources": ["doc1", "doc2"]
        }
      ],
      "related_documents": [
        {
          "id": "uuid",
          "title": "Project Requirements",
          "relevance_score": 0.88,
          "mentions": 3
        }
      ],
      "analytics": {
        "centrality_metrics": {
          "betweenness": 0.75,
          "closeness": 0.82,
          "degree": 15
        },
        "temporal_trends": [
          {
            "date": "2024-01-01",
            "mention_count": 5
          }
        ]
      }
    }
  }
}
```

### Get Relationships

```http
GET /api/v1/graph/relationships?source_type=PERSON&target_type=PROJECT&relationship_type=WORKS_ON
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "relationships": [
      {
        "id": "uuid",
        "source_entity": {
          "id": "uuid",
          "name": "John Doe",
          "type": "PERSON"
        },
        "target_entity": {
          "id": "uuid",
          "name": "Project Alpha",
          "type": "PROJECT"
        },
        "relationship_type": "WORKS_ON",
        "properties": {
          "role": "Lead Developer",
          "start_date": "2024-01-01"
        },
        "strength": 0.95,
        "document_sources": ["doc1", "doc2"],
        "created_at": "2024-01-01T00:00:00Z"
      }
    ],
    "relationship_types": ["WORKS_ON", "BELONGS_TO", "MANAGES", "RELATED_TO"],
    "pagination": {
      "limit": 20,
      "offset": 0,
      "total": 50
    }
  }
}
```

### Graph Traversal

```http
POST /api/v1/graph/traverse
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "start_entity_id": "uuid",
  "max_depth": 3,
  "relationship_types": ["WORKS_ON", "MANAGES"],
  "entity_types": ["PERSON", "PROJECT"],
  "options": {
    "include_cycles": false,
    "max_paths": 100
  }
}
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "paths": [
      {
        "path_id": "uuid",
        "entities": [
          {
            "id": "uuid",
            "name": "John Doe",
            "type": "PERSON"
          },
          {
            "id": "uuid",
            "name": "Project Alpha",
            "type": "PROJECT"
          },
          {
            "id": "uuid",
            "name": "Acme Corp",
            "type": "ORGANIZATION"
          }
        ],
        "relationships": [
          {
            "type": "WORKS_ON",
            "source": "John Doe",
            "target": "Project Alpha"
          },
          {
            "type": "BELONGS_TO",
            "source": "Project Alpha",
            "target": "Acme Corp"
          }
        ],
        "path_length": 2,
        "strength_score": 0.88
      }
    ],
    "analytics": {
      "total_paths": 15,
      "average_path_length": 2.3,
      "strongest_connection": 0.95
    }
  }
}
```

### Graph Analytics

```http
GET /api/v1/graph/analytics?metric=centrality&entity_type=PERSON&limit=10
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "metric": "centrality",
    "entity_type": "PERSON",
    "results": [
      {
        "entity": {
          "id": "uuid",
          "name": "John Doe",
          "type": "PERSON"
        },
        "scores": {
          "betweenness": 0.85,
          "closeness": 0.92,
          "degree": 25,
          "eigenvector": 0.78
        },
        "rank": 1
      }
    ],
    "statistics": {
      "total_entities": 150,
      "average_degree": 8.5,
      "network_density": 0.12
    }
  }
}
```

---

## Analytics & Monitoring

### Get System Metrics

```http
GET /api/v1/analytics/metrics?start_time=2024-01-01&end_time=2024-01-02&granularity=hour
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "metrics": {
      "search_performance": [
        {
          "timestamp": "2024-01-01T00:00:00Z",
          "avg_response_time_ms": 245,
          "search_count": 1250,
          "success_rate": 0.99,
          "avg_relevance_score": 0.82
        }
      ],
      "document_processing": [
        {
          "timestamp": "2024-01-01T00:00:00Z",
          "documents_processed": 45,
          "avg_processing_time_s": 30.5,
          "success_rate": 0.98,
          "queue_depth": 12
        }
      ],
      "system_resources": [
        {
          "timestamp": "2024-01-01T00:00:00Z",
          "cpu_usage": 0.65,
          "memory_usage": 0.78,
          "disk_usage": 0.45,
          "active_connections": 150
        }
      ]
    },
    "period": {
      "start_time": "2024-01-01T00:00:00Z",
      "end_time": "2024-01-02T00:00:00Z",
      "granularity": "hour"
    }
  }
}
```

### Get Quality Metrics

```http
GET /api/v1/analytics/quality?metric=rag_triad&start_time=2024-01-01&end_time=2024-01-02
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "rag_triad_metrics": {
      "answer_relevancy": {
        "average": 0.85,
        "threshold": 0.7,
        "distribution": [
          { "range": "0.8-1.0", "count": 120 },
          { "range": "0.6-0.8", "count": 25 },
          { "range": "0.4-0.6", "count": 5 }
        ]
      },
      "faithfulness": {
        "average": 0.92,
        "threshold": 0.9,
        "distribution": [
          { "range": "0.9-1.0", "count": 135 },
          { "range": "0.7-0.9", "count": 15 }
        ]
      },
      "contextual_relevancy": {
        "average": 0.78,
        "threshold": 0.7,
        "distribution": [
          { "range": "0.8-1.0", "count": 110 },
          { "range": "0.6-0.8", "count": 35 },
          { "range": "0.4-0.6", "count": 5 }
        ]
      }
    },
    "overall_quality_score": 0.85,
    "evaluations_count": 150,
    "period": {
      "start_time": "2024-01-01T00:00:00Z",
      "end_time": "2024-01-02T00:00:00Z"
    }
  }
}
```

### Get Usage Analytics

```http
GET /api/v1/analytics/usage?dimension=users&start_time=2024-01-01&end_time=2024-01-02
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "user_analytics": {
      "active_users": 125,
      "new_users": 8,
      "top_users": [
        {
          "user_id": "uuid",
          "name": "John Doe",
          "search_count": 45,
          "document_count": 12,
          "session_duration_hours": 3.5
        }
      ],
      "user_retention": {
        "day_1": 0.85,
        "day_7": 0.65,
        "day_30": 0.35
      }
    },
    "feature_usage": {
      "search": 1250,
      "document_upload": 45,
      "graph_exploration": 85,
      "analytics_view": 35
    },
    "popular_queries": [
      {
        "query": "machine learning",
        "count": 25,
        "avg_relevance_score": 0.88
      }
    ]
  }
}
```

---

## User Management

### Get Current User Profile

```http
GET /api/v1/users/profile
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "user": {
      "id": "uuid",
      "email": "user@example.com",
      "first_name": "John",
      "last_name": "Doe",
      "role": "USER",
      "organization": {
        "id": "uuid",
        "name": "Acme Corp"
      },
      "preferences": {
        "theme": "light",
        "language": "en",
        "notifications": {
          "email": true,
          "push": false
        }
      },
      "usage_stats": {
        "search_count": 150,
        "document_count": 25,
        "last_login": "2024-01-01T00:00:00Z"
      },
      "created_at": "2024-01-01T00:00:00Z",
      "updated_at": "2024-01-01T00:00:00Z"
    }
  }
}
```

### Update User Profile

```http
PUT /api/v1/users/profile
Authorization: Bearer <access_token>
Content-Type: application/json

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

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "user": {
      "id": "uuid",
      "first_name": "John",
      "last_name": "Smith",
      "preferences": {
        "theme": "dark",
        "language": "en",
        "notifications": {
          "email": true,
          "push": true
        }
      },
      "updated_at": "2024-01-01T00:00:00Z"
    }
  }
}
```

### Change Password

```http
POST /api/v1/users/change-password
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "current_password": "OldPass123!",
  "new_password": "NewPass456!"
}
```

**Response (200)**:

```json
{
  "success": true,
  "message": "Password changed successfully"
}
```

---

## Processing & Workflows

### Get Processing Jobs

```http
GET /api/v1/processing/jobs?status=processing&page=1&limit=20
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "jobs": [
      {
        "id": "uuid",
        "document_id": "uuid",
        "status": "processing",
        "current_stage": "entity_extraction",
        "progress": 65,
        "estimated_completion": "2024-01-01T00:01:00Z",
        "stages": [
          {
            "name": "upload",
            "status": "completed",
            "duration": 0.5
          },
          {
            "name": "ocr",
            "status": "completed",
            "duration": 5.2
          },
          {
            "name": "entity_extraction",
            "status": "processing",
            "progress": 30
          }
        ],
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z"
      }
    ],
    "pagination": {
      "page": 1,
      "limit": 20,
      "total": 50
    }
  }
}
```

### Retry Processing Job

```http
POST /api/v1/processing/jobs/{job_id}/retry
Authorization: Bearer <access_token>
```

**Response (200)**:

```json
{
  "success": true,
  "data": {
    "job": {
      "id": "uuid",
      "status": "queued",
      "retry_count": 1,
      "max_retries": 3
    }
  }
}
```

---

## Error Handling

### Error Response Format

```json
{
  "success": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid input data",
    "details": [
      {
        "field": "email",
        "message": "Invalid email format"
      }
    ],
    "request_id": "uuid",
    "timestamp": "2024-01-01T00:00:00Z"
  }
}
```

### Common Error Codes

| Code                  | HTTP Status | Description                     |
| --------------------- | ----------- | ------------------------------- |
| `VALIDATION_ERROR`    | 400         | Invalid input data              |
| `UNAUTHORIZED`        | 401         | Authentication required         |
| `FORBIDDEN`           | 403         | Insufficient permissions        |
| `NOT_FOUND`           | 404         | Resource not found              |
| `CONFLICT`            | 409         | Resource conflict               |
| `RATE_LIMITED`        | 429         | Too many requests               |
| `INTERNAL_ERROR`      | 500         | Internal server error           |
| `SERVICE_UNAVAILABLE` | 503         | Service temporarily unavailable |
| `PROCESSING_ERROR`    | 422         | Document processing failed      |
| `SEARCH_ERROR`        | 422         | Search operation failed         |
| `STORAGE_ERROR`       | 507         | Insufficient storage            |

---

## Rate Limiting

### Rate Limit Headers

```http
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 95
X-RateLimit-Reset: 1640995200
X-RateLimit-Retry-After: 60
```

### Rate Limit Response

```json
{
  "success": false,
  "error": {
    "code": "RATE_LIMITED",
    "message": "Rate limit exceeded",
    "details": {
      "limit": 100,
      "window": 60,
      "retry_after": 60
    }
  }
}
```

---

## API Versioning

### Version Strategy

- **URL Versioning**: `/api/v1/`, `/api/v2/`
- **Backward Compatibility**: Maintained for at least 2 versions
- **Deprecation Notice**: 6 months before removal
- **Version Headers**: `API-Version: v1`

### Version Response Headers

```http
API-Version: v1
API-Version-Supported: v1,v2
API-Version-Deprecated: v1
API-Version-Sunset: 2024-06-01T00:00:00Z
```

---

## SDKs & Libraries

### JavaScript/TypeScript SDK

```bash
npm install @multimodal-rag/sdk
```

```typescript
import { MultimodalRAGClient } from "@multimodal-rag/sdk";

const client = new MultimodalRAGClient({
  baseURL: "https://api.rag.yourdomain.com/api/v1",
  apiKey: "your-api-key",
});

// Search documents
const results = await client.search.hybrid({
  query: "What are the latest developments in AI?",
  options: { topK: 10 },
});

// Upload document
const document = await client.documents.upload(file, {
  title: "Document Title",
  tags: ["technology", "ai"],
});
```

### Python SDK

```bash
pip install multimodal-rag-sdk
```

```python
from multimodal_rag import MultimodalRAGClient

client = MultimodalRAGClient(
    base_url='https://api.rag.yourdomain.com/api/v1',
    api_key='your-api-key'
)

# Search documents
results = client.search.hybrid(
    query='What are the latest developments in AI?',
    options={'top_k': 10}
)

# Upload document
document = client.documents.upload(
    file=open('document.pdf', 'rb'),
    title='Document Title',
    tags=['technology', 'ai']
)
```

### cURL Examples

```bash
# Search documents
curl -X POST https://api.rag.yourdomain.com/api/v1/search/hybrid \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What are the latest developments in AI?",
    "options": {"top_k": 10}
  }'

# Upload document
curl -X POST https://api.rag.yourdomain.com/api/v1/documents/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "file=@document.pdf" \
  -F "title=Document Title" \
  -F "tags=technology,ai"
```

---

## Webhooks

### Configure Webhook

```http
POST /api/v1/webhooks
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "url": "https://yourapp.com/webhooks/rag-events",
  "events": ["document.processed", "search.completed"],
  "secret": "webhook_secret",
  "active": true
}
```

### Webhook Event Payload

```json
{
  "event": "document.processed",
  "data": {
    "document_id": "uuid",
    "status": "completed",
    "processing_time": 30.5
  },
  "timestamp": "2024-01-01T00:00:00Z",
  "signature": "sha256=abc123..."
}
```

---

## Support & Documentation

### API Documentation

- **Interactive Docs**: https://api.rag.yourdomain.com/docs
- **OpenAPI Spec**: https://api.rag.yourdomain.com/openapi.json
- **Postman Collection**: Available for download

### Support Channels

- **API Support**: api-support@yourcompany.com
- **Developer Discord**: https://discord.gg/multimodal-rag
- **GitHub Issues**: https://github.com/yourorg/multimodal-rag/issues

### Additional Resources

- **SDK Documentation**: Link to SDK docs
- **Tutorials**: Link to tutorials
- **Best Practices**: Link to best practices guide
- **Change Log**: Link to API changelog

---

## Agent Chat (LangGraph)

The Agent Chat API provides an AI research assistant powered by a LangGraph StateGraph with intent-based routing to specialized subgraphs (research, writing, data analysis). All endpoints require authentication.

### Execute Agent (Async Job)

```http
POST /api/v1/agent/execute
Authorization: Bearer <token>
Content-Type: application/json

{
  "messages": [
    { "role": "user", "content": "Find papers about transformer attention mechanisms" }
  ],
  "page_context": {
    "type": "project",
    "project_id": "uuid",
    "project_name": "My Research"
  },
  "model": "gpt-4o",
  "use_rag": true,
  "max_context_docs": 5,
  "thread_id": "optional-uuid"
}
```

**Constraints:**

- `role`: `"user"` or `"assistant"` only
- `model`: `"gpt-4o"` or `"gpt-4o-mini"`
- `content`: max 32,000 characters
- `messages`: max 50

**Response (202):**

```json
{
  "job_id": "uuid",
  "status": "running"
}
```

### Poll Job Status

```http
GET /api/v1/agent/jobs/{job_id}
Authorization: Bearer <token>
```

**Response (200):**

```json
{
  "status": "completed",
  "result": {
    "message": { "role": "assistant", "content": "Based on your documents..." },
    "model": "gpt-4o",
    "usage": {},
    "finish_reason": "stop",
    "timestamp": "2026-03-16T20:00:00Z",
    "rag_enabled": true,
    "retrieved_contexts": [
      {
        "document_id": "uuid",
        "title": "Document Title",
        "content": "Excerpt...",
        "score": 0.87
      }
    ],
    "tool_executions": [
      {
        "id": "uuid",
        "tool_name": "search_arxiv",
        "status": "completed",
        "args": { "query": "transformers" },
        "result": "Found 5 papers...",
        "duration_ms": 1200
      }
    ],
    "thread_id": "uuid",
    "conversation_id": "uuid"
  },
  "confirmation": null
}
```

Statuses: `running`, `awaiting_confirmation`, `completed`, `failed`

When `awaiting_confirmation`, the `confirmation` field contains:

```json
{
  "tools": ["ingest_arxiv_papers"],
  "message": "The agent wants to ingest 3 arXiv papers. Proceed?"
}
```

### Confirm Agent Action (Human-in-the-Loop)

```http
POST /api/v1/agent/confirm/{job_id}
Authorization: Bearer <token>
Content-Type: application/json

{
  "confirmed": true
}
```

**Response (202):**

```json
{
  "status": "running"
}
```

### Stream Agent Response (SSE)

```http
POST /api/v1/agent/stream
Authorization: Bearer <token>
Content-Type: application/json

{
  "messages": [{ "role": "user", "content": "Summarize this project's documents" }],
  "page_context": { "type": "project", "project_id": "uuid" },
  "model": "gpt-4o",
  "use_rag": true
}
```

**Response:** Server-Sent Events stream (5-minute timeout)

Event types: `token`, `tool_start`, `tool_end`, `rag_context`, `done`, `error`

### List Agent Threads

```http
GET /api/v1/agent/threads
Authorization: Bearer <token>
```

**Response (200):**

```json
{
  "threads": [
    {
      "id": "uuid",
      "title": "Find papers about transformers",
      "created_at": "2026-03-16T20:00:00Z",
      "updated_at": "2026-03-16T20:05:00Z",
      "message_count": 4
    }
  ],
  "total": 1
}
```

### Get Thread Messages

```http
GET /api/v1/agent/threads/{thread_id}/messages
Authorization: Bearer <token>
```

**Response (200):**

```json
{
  "messages": [
    { "id": "uuid", "role": "user", "content": "...", "created_at": "..." },
    { "id": "uuid", "role": "assistant", "content": "...", "created_at": "...", "citations": [...], "tool_executions": [...] }
  ],
  "total": 2
}
```

### Graph Visualization

```http
GET /api/v1/agent/graph/mermaid
GET /api/v1/agent/graph/trace/{thread_id}
```

Returns Mermaid diagram strings for graph structure and execution traces.

### Agent Health Check

```http
GET /api/v1/agent/health
```

---

This API documentation provides comprehensive coverage of all available endpoints in the Multimodal Enterprise RAG System. For specific implementation details and examples, refer to the provided SDKs and sample code repositories.

# Analytics Dashboard API Specification

> **Stale / historical (2026-10-17):** It covers only part of the API and its paths and schemas may have drifted. The source of truth is the generated `backend/openapi.json` (also served at `/docs` when `DEBUG` is on). Current contracts: [`docs/engineering/`](../engineering/README.md).

## Overview

This document provides the comprehensive API specification for the Knowledge Graph Analytics Dashboard in the Multimodal Enterprise RAG System. The API supports real-time metrics, historical analysis, custom reporting, and dashboard configuration.

## Base URL

```
Production: https://api.ragsystem.com/v1/analytics
Development: http://localhost:8000/api/v1/analytics
```

## Authentication

All API requests require authentication using Bearer tokens:

```
Authorization: Bearer <jwt_token>
```

## Response Format

All API responses follow a consistent structure:

```json
{
  "success": true,
  "data": {},
  "message": "Operation completed successfully",
  "timestamp": "2024-01-01T12:00:00Z",
  "request_id": "uuid-v4"
}
```

Error responses:

```json
{
  "success": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid request parameters",
    "details": {}
  },
  "timestamp": "2024-01-01T12:00:00Z",
  "request_id": "uuid-v4"
}
```

## API Endpoints

### 1. Real-Time Dashboard Metrics

#### Get Real-Time Dashboard Metrics

**Endpoint**: `GET /dashboard/metrics`

**Description**: Retrieves current real-time metrics for the organization dashboard

**Parameters**:
- `organization_id` (query, UUID, required): Organization ID
- `metrics` (query, string[], optional): Specific metrics to retrieve

**Response**:
```json
{
  "success": true,
  "data": {
    "organization_id": "uuid",
    "organization_name": "Acme Corp",
    "total_entities": 15420,
    "new_entities_today": 127,
    "avg_entity_confidence": 0.87,
    "total_relationships": 45320,
    "new_relationships_today": 389,
    "total_documents": 3250,
    "documents_processed_today": 45,
    "avg_document_quality": 0.92,
    "processing_success_rate": 0.96,
    "active_users_today": 23,
    "total_searches_today": 1450,
    "avg_search_response_time": 1250,
    "system_health_score": 0.94,
    "active_alerts_count": 2,
    "total_storage_used": 125.7,
    "storage_growth_rate": 0.023,
    "last_updated": "2024-01-01T12:00:00Z"
  }
}
```

**Example Request**:
```bash
curl -X GET \
  "https://api.ragsystem.com/v1/analytics/dashboard/metrics?organization_id=uuid&metrics=total_entities,new_entities_today,avg_entity_confidence" \
  -H "Authorization: Bearer <token>"
```

### 2. Entity Analytics

#### Get Entity Trends

**Endpoint**: `GET /entities/trends`

**Description**: Retrieves historical trends for entity analytics

**Parameters**:
- `organization_id` (query, UUID, required): Organization ID
- `time_range` (query, string, required): Time range (7d, 30d, 90d, 1y)
- `bucket_type` (query, string, optional): Time bucket type (hour, day, week, month)
- `entity_types` (query, string[], optional): Filter by entity types

**Response**:
```json
{
  "success": true,
  "data": {
    "time_range": "30d",
    "bucket_type": "day",
    "trends": [
      {
        "time_bucket": "2024-01-01T00:00:00Z",
        "total_entities": 15000,
        "new_entities": 45,
        "entity_growth_rate": 0.003,
        "entity_type_counts": {
          "person": 5000,
          "organization": 2500,
          "location": 3000,
          "concept": 4500
        },
        "avg_confidence_score": 0.86,
        "high_quality_entities": 12000,
        "entities_processed": 50,
        "processing_success_rate": 0.94,
        "entities_per_document": 4.6
      }
    ],
    "summary": {
      "total_new_entities": 1270,
      "avg_growth_rate": 0.023,
      "most_common_type": "person",
      "avg_confidence": 0.87
    }
  }
}
```

#### Get Entity Type Distribution

**Endpoint**: `GET /entities/distribution`

**Description**: Retrieves current distribution of entities by type and other dimensions

**Parameters**:
- `organization_id` (query, UUID, required): Organization ID
- `dimension` (query, string, optional): Distribution dimension (type, confidence, modality)

**Response**:
```json
{
  "success": true,
  "data": {
    "distribution_type": "type",
    "distribution": [
      {
        "category": "person",
        "count": 5000,
        "percentage": 32.5,
        "avg_confidence": 0.89
      },
      {
        "category": "organization",
        "count": 2500,
        "percentage": 16.2,
        "avg_confidence": 0.91
      },
      {
        "category": "location",
        "count": 3000,
        "percentage": 19.5,
        "avg_confidence": 0.85
      },
      {
        "category": "concept",
        "count": 4500,
        "percentage": 29.2,
        "avg_confidence": 0.83
      }
    ],
    "total_entities": 15000,
    "last_calculated": "2024-01-01T12:00:00Z"
  }
}
```

### 3. Relationship Analytics

#### Get Relationship Analysis

**Endpoint**: `GET /relationships/analysis`

**Description**: Retrieves comprehensive relationship analytics and connectivity patterns

**Parameters**:
- `organization_id` (query, UUID, required): Organization ID
- `time_range` (query, string, required): Time range
- `relationship_types` (query, string[], optional): Filter by relationship types

**Response**:
```json
{
  "success": true,
  "data": {
    "time_range": "30d",
    "total_relationships": 45320,
    "new_relationships": 389,
    "relationship_growth_rate": 0.0086,
    "relationship_type_counts": {
      "works_for": 12000,
      "located_in": 8500,
      "related_to": 15000,
      "part_of": 6000,
      "member_of": 3820
    },
    "relationship_strength_distribution": {
      "high": 13600,  // >0.8
      "medium": 25000, // 0.5-0.8
      "low": 6720     // <0.5
    },
    "avg_relationship_strength": 0.67,
    "avg_connections_per_entity": 2.95,
    "hub_entities": 45,  // >50 connections
    "isolated_entities": 125,
    "bidirectional_relationships": 28500,
    "bidirectional_percentage": 0.63,
    "connectivity_trends": [
      {
        "time_bucket": "2024-01-01T00:00:00Z",
        "avg_connections_per_entity": 2.9,
        "hub_entities": 43,
        "bidirectional_percentage": 0.62
      }
    ]
  }
}
```

### 4. Graph Metrics

#### Get Graph Metrics

**Endpoint**: `GET /graph/metrics`

**Description**: Retrieves comprehensive graph analytics and topology metrics

**Parameters**:
- `organization_id` (query, UUID, required): Organization ID
- `include_centralities` (query, boolean, optional): Include centrality calculations
- `include_communities` (query, boolean, optional): Include community detection results

**Response**:
```json
{
  "success": true,
  "data": {
    "computed_at": "2024-01-01T12:00:00Z",
    "computation_version": "v1.0",
    "computation_time_ms": 15420,
    "top_entities_by_centrality": [
      {
        "entity_id": "uuid",
        "entity_name": "John Doe",
        "entity_type": "person",
        "centrality_score": 0.089,
        "connections_count": 156
      },
      {
        "entity_id": "uuid",
        "entity_name": "Acme Corporation",
        "entity_type": "organization",
        "centrality_score": 0.076,
        "connections_count": 142
      }
    ],
    "top_entities_by_betweenness": [
      {
        "entity_id": "uuid",
        "entity_name": "Jane Smith",
        "entity_type": "person",
        "betweenness_score": 0.124,
        "bridge_connections": 89
      }
    ],
    "graph_density": 0.0032,
    "average_path_length": 4.2,
    "clustering_coefficient": 0.34,
    "modularity_score": 0.67,
    "number_of_communities": 23,
    "average_community_size": 652,
    "largest_community_size": 1850,
    "community_distribution": {
      "small": 18,    // <100 members
      "medium": 4,    // 100-500 members
      "large": 1      // >500 members
    },
    "number_of_components": 3,
    "giant_component_size": 14200,
    "giant_component_percentage": 0.92,
    "entity_type_connectivity": {
      "person-organization": 0.45,
      "person-location": 0.32,
      "organization-location": 0.23
    }
  }
}
```

### 5. Document Analytics

#### Get Document Performance Metrics

**Endpoint**: `GET /documents/performance`

**Description**: Retrieves document processing performance and quality metrics

**Parameters**:
- `organization_id` (query, UUID, required): Organization ID
- `time_range` (query, string, required): Time range
- `document_types` (query, string[], optional): Filter by document types

**Response**:
```json
{
  "success": true,
  "data": {
    "time_range": "30d",
    "total_documents": 3250,
    "new_documents": 145,
    "processed_documents": 3200,
    "processing_success_rate": 0.96,
    "document_type_counts": {
      "pdf": 1800,
      "text": 750,
      "image": 450,
      "audio": 150,
      "video": 100
    },
    "modality_counts": {
      "text": 3000,
      "image": 600,
      "audio": 180,
      "video": 120
    },
    "avg_quality_score": 0.92,
    "high_quality_documents": 2850,
    "low_quality_documents": 125,
    "avg_processing_time_ms": 8500,
    "processing_bottlenecks": [
      {
        "step": "ocr_processing",
        "failure_rate": 0.04,
        "avg_time_ms": 3000
      },
      {
        "step": "entity_extraction",
        "failure_rate": 0.02,
        "avg_time_ms": 2000
      }
    ],
    "avg_entities_extracted": 8.5,
    "avg_concepts_extracted": 3.2,
    "extraction_success_rates": {
      "entity_extraction": 0.94,
      "concept_extraction": 0.89,
      "relationship_extraction": 0.87
    },
    "total_storage_gb": 125.7,
    "avg_document_size_mb": 38.7,
    "storage_growth_rate": 0.023,
    "total_views": 15420,
    "total_downloads": 3200,
    "avg_views_per_document": 4.7,
    "performance_trends": [
      {
        "time_bucket": "2024-01-01T00:00:00Z",
        "processing_success_rate": 0.95,
        "avg_processing_time_ms": 8200,
        "avg_quality_score": 0.91
      }
    ]
  }
}
```

### 6. User Interaction Analytics

#### Get User Engagement Metrics

**Endpoint**: `GET /users/engagement`

**Description**: Retrieves user activity and engagement analytics

**Parameters**:
- `organization_id` (query, UUID, required): Organization ID
- `time_range` (query, string, required): Time range
- `user_roles` (query, string[], optional): Filter by user roles

**Response**:
```json
{
  "success": true,
  "data": {
    "time_range": "30d",
    "active_users": 45,
    "new_users": 8,
    "total_sessions": 1250,
    "avg_session_duration_seconds": 1250,
    "total_searches": 14500,
    "unique_search_queries": 3200,
    "avg_search_time_ms": 1250,
    "search_success_rate": 0.87,
    "query_type_distribution": {
      "semantic": 5800,      // 40%
      "keyword": 4350,       // 30%
      "hybrid": 3625,        // 25%
      "graph": 725           // 5%
    },
    "query_complexity_distribution": {
      "simple": 8700,        // 60%
      "medium": 4350,        // 30%
      "complex": 1450        // 10%
    },
    "avg_user_satisfaction_score": 4.2,
    "feedback_submission_rate": 0.15,
    "complaint_rate": 0.02,
    "feature_usage": {
      "search": 14500,
      "document_viewer": 8900,
      "entity_browser": 3400,
      "graph_explorer": 2100,
      "reports": 1250
    },
    "advanced_feature_adoption_rate": 0.34,
    "avg_response_time_ms": 1200,
    "error_rate": 0.018,
    "timeout_rate": 0.005,
    "user_role_distribution": {
      "admin": 5,
      "analyst": 15,
      "user": 25
    },
    "user_experience_level_distribution": {
      "novice": 15,
      "intermediate": 25,
      "expert": 5
    },
    "engagement_trends": [
      {
        "time_bucket": "2024-01-01T00:00:00Z",
        "active_users": 42,
        "avg_satisfaction_score": 4.1,
        "avg_response_time_ms": 1180
      }
    ]
  }
}
```

### 7. Dashboard Configuration

#### Get Dashboard Configuration

**Endpoint**: `GET /dashboard/configuration`

**Description**: Retrieves dashboard configuration for a user or organization

**Parameters**:
- `organization_id` (query, UUID, required): Organization ID
- `user_id` (query, UUID, optional): User ID (if not provided, returns org default)
- `config_name` (query, string, optional): Specific configuration name

**Response**:
```json
{
  "success": true,
  "data": {
    "id": "uuid",
    "config_name": "Default Analytics Dashboard",
    "config_type": "user",
    "is_default": true,
    "is_active": true,
    "layout": {
      "grid": {
        "columns": 12,
        "rows": 8,
        "gap": 16
      },
      "widgets": [
        {
          "id": "widget-1",
          "type": "metric_card",
          "position": {"x": 0, "y": 0, "w": 3, "h": 2},
          "config": {
            "title": "Total Entities",
            "metric": "total_entities",
            "format": "number"
          }
        }
      ]
    },
    "widgets": [
      {
        "widget_name": "System Health Score",
        "widget_type": "metric_card",
        "data_source": "real_time_dashboard_metrics",
        "config": {
          "title": "System Health",
          "metric": "system_health_score",
          "format": "percentage",
          "thresholds": {"good": 0.9, "warning": 0.7, "critical": 0.5}
        }
      }
    ],
    "time_range_default": "7d",
    "filters": {
      "entity_types": ["person", "organization"],
      "quality_threshold": 0.7
    },
    "auto_refresh_enabled": true,
    "auto_refresh_interval_seconds": 300,
    "created_at": "2024-01-01T10:00:00Z",
    "updated_at": "2024-01-01T12:00:00Z"
  }
}
```

#### Update Dashboard Configuration

**Endpoint**: `PUT /dashboard/configuration`

**Description**: Updates dashboard configuration

**Request Body**:
```json
{
  "config_name": "Custom Analytics Dashboard",
  "layout": {
    "grid": {"columns": 12, "rows": 8},
    "widgets": [...]
  },
  "widgets": [...],
  "time_range_default": "30d",
  "filters": {...},
  "auto_refresh_enabled": true,
  "auto_refresh_interval_seconds": 600
}
```

**Response**:
```json
{
  "success": true,
  "data": {
    "id": "uuid",
    "config_name": "Custom Analytics Dashboard",
    "updated_at": "2024-01-01T12:30:00Z"
  }
}
```

### 8. Widget Definitions

#### Get Widget Definitions

**Endpoint**: `GET /widgets/definitions`

**Description**: Retrieves available widget definitions for dashboard configuration

**Parameters**:
- `category` (query, string, optional): Filter by widget category
- `type` (query, string, optional): Filter by widget type
- `user_role` (query, string, optional): Filter by user role permissions

**Response**:
```json
{
  "success": true,
  "data": {
    "widgets": [
      {
        "id": "uuid",
        "widget_name": "Entity Count Trend",
        "widget_type": "line_chart",
        "widget_category": "entity",
        "data_source": "entity_analytics",
        "chart_type": "line",
        "default_config": {
          "title": "Entity Growth Trend",
          "x_axis": "time_bucket",
          "y_axis": "total_entities",
          "color_scheme": "blue"
        },
        "required_permissions": ["analytics.view"],
        "cache_duration_seconds": 300,
        "description": "Shows the growth of entities over time",
        "is_active": true
      }
    ]
  }
}
```

### 9. Custom Reports

#### Generate Custom Report

**Endpoint**: `POST /reports/generate`

**Description**: Generates a custom analytics report

**Request Body**:
```json
{
  "report_name": "Monthly Analytics Summary",
  "report_definition": {
    "time_range": "30d",
    "sections": [
      {
        "title": "Entity Analytics",
        "widgets": [
          {
            "type": "chart",
            "data_source": "entity_analytics",
            "chart_config": {
              "type": "line",
              "metrics": ["total_entities", "new_entities"]
            }
          }
        ]
      }
    ]
  },
  "output_formats": ["pdf", "csv"],
  "delivery_methods": ["download", "email"]
}
```

**Response**:
```json
{
  "success": true,
  "data": {
    "execution_id": "uuid",
    "report_name": "Monthly Analytics Summary",
    "status": "running",
    "estimated_completion": "2024-01-01T12:05:00Z",
    "download_urls": []
  }
}
```

#### Get Report Execution Status

**Endpoint**: `GET /reports/execution/{execution_id}`

**Response**:
```json
{
  "success": true,
  "data": {
    "execution_id": "uuid",
    "report_name": "Monthly Analytics Summary",
    "status": "completed",
    "execution_time_ms": 15420,
    "result_file_paths": [
      "/reports/monthly_summary_20240101.pdf",
      "/reports/monthly_summary_20240101.csv"
    ],
    "download_urls": [
      "https://api.ragsystem.com/v1/analytics/reports/download/uuid/pdf",
      "https://api.ragsystem.com/v1/analytics/reports/download/uuid/csv"
    ],
    "started_at": "2024-01-01T12:00:00Z",
    "completed_at": "2024-01-01T12:15:00Z"
  }
}
```

#### Schedule Recurring Report

**Endpoint**: `POST /reports/schedule`

**Request Body**:
```json
{
  "report_name": "Weekly Analytics Report",
  "schedule_config": {
    "cron_expression": "0 9 * * 1", // Every Monday at 9 AM
    "timezone": "UTC"
  },
  "recipients": [
    {
      "type": "email",
      "address": "admin@company.com"
    }
  ],
  "auto_generate": true,
  "report_definition": {...}
}
```

### 10. Alerts and Notifications

#### Get Analytics Alerts

**Endpoint**: `GET /alerts`

**Parameters**:
- `organization_id` (query, UUID, required): Organization ID
- `status` (query, string, optional): Filter by status (active, triggered, acknowledged, resolved)
- `severity` (query, string, optional): Filter by severity
- `limit` (query, integer, optional): Maximum number of results (default: 50)

**Response**:
```json
{
  "success": true,
  "data": {
    "alerts": [
      {
        "id": "uuid",
        "alert_name": "Processing Success Rate Drop",
        "alert_type": "metric_threshold",
        "severity": "warning",
        "status": "active",
        "last_triggered_at": "2024-01-01T11:30:00Z",
        "trigger_count": 2,
        "alert_details": {
          "metric": "processing_success_rate",
          "current_value": 0.78,
          "threshold": 0.8
        },
        "created_at": "2024-01-01T11:00:00Z"
      }
    ],
    "total_count": 5,
    "has_more": true
  }
}
```

#### Acknowledge Alert

**Endpoint**: `POST /alerts/{alert_id}/acknowledge`

**Request Body**:
```json
{
  "acknowledgment_notes": "Investigating the processing issues with the document pipeline team."
}
```

### 11. Data Export

#### Export Analytics Data

**Endpoint**: `POST /export/data`

**Request Body**:
```json
{
  "data_type": "entity_analytics",
  "time_range": {
    "start": "2024-01-01T00:00:00Z",
    "end": "2024-01-31T23:59:59Z"
  },
  "format": "csv",
  "filters": {
    "entity_types": ["person", "organization"]
  },
  "include_metadata": true
}
```

**Response**:
```json
{
  "success": true,
  "data": {
    "export_id": "uuid",
    "status": "processing",
    "estimated_completion": "2024-01-01T12:02:00Z",
    "download_url": null
  }
}
```

#### Get Export Status

**Endpoint**: `GET /export/status/{export_id}`

**Response**:
```json
{
  "success": true,
  "data": {
    "export_id": "uuid",
    "status": "completed",
    "file_size_bytes": 2048576,
    "download_url": "https://api.ragsystem.com/v1/analytics/export/download/uuid",
    "expires_at": "2024-01-08T12:00:00Z",
    "created_at": "2024-01-01T12:00:00Z",
    "completed_at": "2024-01-01T12:01:30Z"
  }
}
```

## WebSocket Events

### Real-Time Analytics Updates

**Connection**: `wss://api.ragsystem.com/v1/analytics/realtime`

**Authentication**: Include JWT token in connection URL or as message header

**Event Types**:

#### Dashboard Metrics Update
```json
{
  "event_type": "dashboard_metrics_updated",
  "organization_id": "uuid",
  "data": {
    "total_entities": 15420,
    "new_entities_today": 127,
    "system_health_score": 0.94
  },
  "timestamp": "2024-01-01T12:00:00Z"
}
```

#### Analytics Alert Triggered
```json
{
  "event_type": "alert_triggered",
  "organization_id": "uuid",
  "data": {
    "alert_id": "uuid",
    "alert_name": "Processing Success Rate Drop",
    "severity": "warning",
    "current_value": 0.78,
    "threshold": 0.8
  },
  "timestamp": "2024-01-01T12:00:00Z"
}
```

#### Report Generation Completed
```json
{
  "event_type": "report_completed",
  "organization_id": "uuid",
  "data": {
    "execution_id": "uuid",
    "report_name": "Monthly Analytics Summary",
    "status": "completed",
    "download_urls": ["https://..."]
  },
  "timestamp": "2024-01-01T12:15:00Z"
}
```

## Error Codes

| Error Code | Description | HTTP Status |
|------------|-------------|-------------|
| VALIDATION_ERROR | Invalid request parameters | 400 |
| UNAUTHORIZED | Missing or invalid authentication | 401 |
| FORBIDDEN | Insufficient permissions | 403 |
| NOT_FOUND | Resource not found | 404 |
| RATE_LIMITED | Too many requests | 429 |
| INTERNAL_ERROR | Server error | 500 |
| SERVICE_UNAVAILABLE | Analytics service temporarily unavailable | 503 |

## Rate Limiting

- **Standard endpoints**: 100 requests per minute per organization
- **Report generation**: 10 requests per hour per organization
- **Data export**: 5 requests per hour per organization
- **WebSocket connections**: 10 concurrent connections per user

## Pagination

For list endpoints, use the following parameters:

- `page` (integer): Page number (default: 1)
- `limit` (integer): Items per page (default: 20, max: 100)
- `sort_by` (string): Field to sort by
- `sort_order` (string): Sort order (asc, desc)

**Response**:
```json
{
  "success": true,
  "data": {
    "items": [...],
    "pagination": {
      "page": 1,
      "limit": 20,
      "total_count": 150,
      "total_pages": 8,
      "has_next": true,
      "has_previous": false
    }
  }
}
```

## SDK Examples

### JavaScript/TypeScript

```typescript
import { AnalyticsClient } from '@ragsystem/analytics-sdk';

const client = new AnalyticsClient({
  baseURL: 'https://api.ragsystem.com/v1/analytics',
  authToken: 'your-jwt-token'
});

// Get dashboard metrics
const metrics = await client.dashboard.getMetrics({
  organizationId: 'uuid'
});

// Get entity trends
const trends = await client.entities.getTrends({
  organizationId: 'uuid',
  timeRange: '30d',
  bucketType: 'day'
});

// Setup real-time updates
client.realtime.connect({
  organizationId: 'uuid',
  onEvent: (event) => {
    console.log('Analytics update:', event);
  }
});
```

### Python

```python
from ragsystem_analytics import AnalyticsClient

client = AnalyticsClient(
    base_url='https://api.ragsystem.com/v1/analytics',
    auth_token='your-jwt-token'
)

# Get dashboard metrics
metrics = client.dashboard.get_metrics(
    organization_id='uuid'
)

# Generate report
report = client.reports.generate(
    organization_id='uuid',
    report_name='Monthly Summary',
    report_definition={
        'time_range': '30d',
        'sections': [...]
    }
)
```

This comprehensive API specification provides all the endpoints needed for a fully functional Knowledge Graph Analytics Dashboard with real-time capabilities, custom reporting, and extensive data export options.
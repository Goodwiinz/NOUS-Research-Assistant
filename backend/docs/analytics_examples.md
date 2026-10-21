# RAG Analytics API Examples

> **Stale / historical (2026-10-17):** It is a design document for a subsystem; the described service layout may not match the current code. Verify against `backend/src/` and `backend/openapi.json` before relying on it. Current contracts: [`docs/engineering/`](../../docs/engineering/README.md).

This document provides comprehensive examples for using the RAG Analytics API endpoints, including sample requests, responses, and integration patterns.

## Table of Contents

1. [Authentication Setup](#authentication-setup)
2. [Quality Metrics Examples](#quality-metrics-examples)
3. [User Behavior Analytics Examples](#user-behavior-analytics-examples)
4. [Performance Monitoring Examples](#performance-monitoring-examples)
5. [Analytics Events Examples](#analytics-events-examples)
6. [Background Jobs Examples](#background-jobs-examples)
7. [Recommendations Examples](#recommendations-examples)
8. [Error Handling Examples](#error-handling-examples)
9. [SDK Integration Examples](#sdk-integration-examples)

## Authentication Setup

### Getting Your JWT Token

```bash
# Login to get JWT token
curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "your-email@example.com",
    "password": "your-password"
  }'

# Response
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

### Using the Token

```bash
# Include the token in subsequent requests
curl -X GET "http://localhost:8000/api/v1/analytics/quality/metrics" \
  -H "Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
```

## Quality Metrics Examples

### Get Quality Metrics for Documents

```bash
# Basic request
curl -X GET "http://localhost:8000/api/v1/analytics/quality/metrics?organization_id=550e8400-e29b-41d4-a716-446655440000" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

```python
import requests

# Python example
headers = {"Authorization": "Bearer YOUR_JWT_TOKEN"}
params = {
    "organization_id": "550e8400-e29b-41d4-a716-446655440000",
    "start_date": "2024-01-01T00:00:00Z",
    "end_date": "2024-01-31T23:59:59Z",
    "document_type": "pdf",
    "min_quality_score": 0.75,
    "page": 1,
    "size": 20
}

response = requests.get(
    "http://localhost:8000/api/v1/analytics/quality/metrics",
    headers=headers,
    params=params
)

data = response.json()
print(f"Found {data['pagination']['total']} quality metrics")
print(f"Average quality score: {data['summary']['avg_quality_score']}")
```

```javascript
// JavaScript/Node.js example
const axios = require('axios');

const getQualityMetrics = async () => {
  try {
    const response = await axios.get('/api/v1/analytics/quality/metrics', {
      headers: {
        'Authorization': `Bearer ${process.env.ANALYTICS_TOKEN}`
      },
      params: {
        organization_id: '550e8400-e29b-41d4-a716-446655440000',
        start_date: '2024-01-01T00:00:00Z',
        end_date: '2024-01-31T23:59:59Z',
        document_type: 'pdf',
        min_quality_score: 0.75
      }
    });

    const data = response.data;
    console.log(`Found ${data.pagination.total} quality metrics`);
    console.log(`Average quality score: ${data.summary.avg_quality_score}`);

    return data;
  } catch (error) {
    console.error('Error fetching quality metrics:', error.response?.data);
    throw error;
  }
};
```

### Sample Response

```json
{
  "data": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "document_id": "doc123",
      "organization_id": "550e8400-e29b-41d4-a716-446655440000",
      "overall_score": 0.85,
      "content_relevance": 0.90,
      "information_density": 0.75,
      "readability_score": 0.88,
      "technical_accuracy": 0.82,
      "source_credibility": 0.91,
      "recency_score": 0.70,
      "completeness_score": 0.89,
      "structured_data_score": 0.86,
      "metadata_quality_score": 0.92,
      "extraction_quality": 0.84,
      "language_quality": 0.89,
      "processing_errors": [],
      "quality_flags": ["high_relevance", "credible_source"],
      "recommendations": ["Add more recent references", "Improve technical diagrams"],
      "quality_tier": "high",
      "assessment_version": "v2.1",
      "assessment_metadata": {
        "model_version": "quality-v2.1",
        "confidence_score": 0.92,
        "processing_time_ms": 1500
      },
      "created_at": "2024-01-15T10:30:00Z",
      "updated_at": "2024-01-15T10:30:00Z"
    }
  ],
  "pagination": {
    "page": 1,
    "size": 20,
    "total": 156,
    "pages": 8,
    "has_next": true,
    "has_prev": false
  },
  "summary": {
    "avg_quality_score": 0.78,
    "quality_distribution": {"high": 45, "medium": 89, "low": 22},
    "total_documents": 156
  }
}
```

### Get Quality Recommendations

```bash
curl -X GET "http://localhost:8000/api/v1/analytics/quality/recommendations?organization_id=550e8400-e29b-41d4-a716-446655440000&recommendation_type=content" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

### Get Quality Dashboard Data

```bash
curl -X GET "http://localhost:8000/api/v1/analytics/quality/dashboard?organization_id=550e8400-e29b-41d4-a716-446655440000&time_range=30d" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

## User Behavior Analytics Examples

### Get User Sessions

```bash
curl -X GET "http://localhost:8000/api/v1/analytics/behavior/sessions?organization_id=550e8400-e29b-41d4-a716-446655440000&start_date=2024-01-01T00:00:00Z&end_date=2024-01-31T23:59:59Z&min_engagement_score=50" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

```python
# Python example for user sessions analysis
import requests
import pandas as pd

def analyze_user_engagement(organization_id, start_date, end_date):
    headers = {"Authorization": "Bearer YOUR_JWT_TOKEN"}
    params = {
        "organization_id": organization_id,
        "start_date": start_date,
        "end_date": end_date,
        "min_engagement_score": 0,
        "size": 100
    }

    all_sessions = []
    page = 1

    while True:
        params["page"] = page
        response = requests.get(
            "http://localhost:8000/api/v1/analytics/behavior/sessions",
            headers=headers,
            params=params
        )

        data = response.json()
        all_sessions.extend(data["data"])

        if not data["pagination"]["has_next"]:
            break
        page += 1

    # Convert to DataFrame for analysis
    df = pd.DataFrame(all_sessions)

    # Calculate insights
    avg_session_duration = df["duration_seconds"].mean()
    avg_engagement_score = df["engagement_score"].mean()
    total_sessions = len(df)

    # Device type distribution
    device_distribution = df["device_type"].value_counts()

    # Top engaged users
    top_users = df.nlargest(10, "engagement_score")[["user_id", "engagement_score", "duration_seconds"]]

    return {
        "avg_session_duration": avg_session_duration,
        "avg_engagement_score": avg_engagement_score,
        "total_sessions": total_sessions,
        "device_distribution": device_distribution.to_dict(),
        "top_users": top_users.to_dict("records")
    }

# Usage
insights = analyze_user_engagement(
    "550e8400-e29b-41d4-a716-446655440000",
    "2024-01-01T00:00:00Z",
    "2024-01-31T23:59:59Z"
)
print(insights)
```

### Get Behavior Events

```bash
# Get all search events
curl -X GET "http://localhost:8000/api/v1/analytics/behavior/events?organization_id=550e8400-e29b-41d4-a716-446655440000&event_type=search&size=100" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"

# Get events for a specific session
curl -X GET "http://localhost:8000/api/v1/analytics/behavior/events?organization_id=550e8400-e29b-41d4-a716-446655440000&session_id=550e8400-e29b-41d4-a716-446655440001" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

### Get Behavior Dashboard

```bash
curl -X GET "http://localhost:8000/api/v1/analytics/behavior/dashboard?organization_id=550e8400-e29b-41d4-a716-446655440000&time_range=7d" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

## Performance Monitoring Examples

### Get Performance Metrics

```bash
# Get all performance metrics
curl -X GET "http://localhost:8000/api/v1/analytics/performance/metrics?organization_id=550e8400-e29b-41d4-a716-446655440000" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"

# Filter by component and performance level
curl -X GET "http://localhost:8000/api/v1/analytics/performance/metrics?organization_id=550e8400-e29b-41d4-a716-446655440000&component=api&performance_level=poor" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

```python
# Python example for performance monitoring
import requests
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

def get_performance_trends(organization_id, hours=24):
    headers = {"Authorization": "Bearer YOUR_JWT_TOKEN"}

    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=hours)

    params = {
        "organization_id": organization_id,
        "start_time": start_time.isoformat() + "Z",
        "end_time": end_time.isoformat() + "Z",
        "size": 1000
    }

    response = requests.get(
        "http://localhost:8000/api/v1/analytics/performance/metrics",
        headers=headers,
        params=params
    )

    data = response.json()
    metrics = data["data"]

    # Filter for response time metrics
    response_times = [
        (m["timestamp"], m["value"])
        for m in metrics
        if m["metric_name"] == "response_time"
    ]

    # Create visualization
    if response_times:
        times, values = zip(*response_times)
        plt.figure(figsize=(12, 6))
        plt.plot(times, values)
        plt.title("API Response Time Trends")
        plt.xlabel("Time")
        plt.ylabel("Response Time (ms)")
        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.show()

    return metrics

# Usage
metrics = get_performance_trends("550e8400-e29b-41d4-a716-446655440000", 24)
print(f"Retrieved {len(metrics)} performance metrics")
```

### Get Performance Dashboard

```bash
curl -X GET "http://localhost:8000/api/v1/analytics/performance/dashboard?organization_id=550e8400-e29b-41d4-a716-446655440000&time_range=1h" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

### Get System Health

```bash
curl -X GET "http://localhost:8000/api/v1/analytics/performance/health?organization_id=550e8400-e29b-41d4-a716-446655440000" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

## Analytics Events Examples

### Create Analytics Event

```bash
# Create a search event
curl -X POST "http://localhost:8000/api/v1/analytics/events" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "search",
    "event_name": "document_search",
    "event_data": {
      "query": "machine learning algorithms",
      "results_count": 25,
      "search_time_ms": 150,
      "filters_used": ["category:technical", "date:recent"],
      "ranking_algorithm": "bm25",
      "query_language": "en"
    },
    "user_id": "550e8400-e29b-41d4-a716-446655440001",
    "session_id": "550e8400-e29b-41d4-a716-446655440002",
    "organization_id": "550e8400-e29b-41d4-a716-446655440000",
    "severity": "low",
    "source": "web_ui",
    "tags": ["search", "technical", "bm25"]
  }'
```

```python
# Python example for creating various event types
import requests
import json
import time
from datetime import datetime

class AnalyticsTracker:
    def __init__(self, api_token, organization_id):
        self.api_token = api_token
        self.organization_id = organization_id
        self.base_url = "http://localhost:8000/api/v1/analytics"
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }

    def track_search(self, user_id, session_id, query, results_count, search_time_ms, **kwargs):
        """Track a search event"""
        event_data = {
            "query": query,
            "results_count": results_count,
            "search_time_ms": search_time_ms,
            **kwargs
        }

        return self._create_event(
            event_type="search",
            event_name="document_search",
            event_data=event_data,
            user_id=user_id,
            session_id=session_id
        )

    def track_document_view(self, user_id, session_id, document_id, title, **kwargs):
        """Track a document view event"""
        event_data = {
            "document_id": document_id,
            "document_title": title,
            **kwargs
        }

        return self._create_event(
            event_type="view",
            event_name="document_view",
            event_data=event_data,
            user_id=user_id,
            session_id=session_id,
            document_id=document_id
        )

    def track_download(self, user_id, session_id, document_id, format, **kwargs):
        """Track a document download event"""
        event_data = {
            "document_id": document_id,
            "format": format,
            **kwargs
        }

        return self._create_event(
            event_type="download",
            event_name="document_download",
            event_data=event_data,
            user_id=user_id,
            session_id=session_id,
            document_id=document_id
        )

    def track_performance_metric(self, metric_name, value, component, **kwargs):
        """Track a performance metric"""
        event_data = {
            "metric_name": metric_name,
            "value": value,
            "component": component,
            **kwargs
        }

        return self._create_event(
            event_type="performance",
            event_name="metric_collection",
            event_data=event_data,
            severity="low"
        )

    def _create_event(self, **event_data):
        """Create an analytics event"""
        base_data = {
            "organization_id": self.organization_id,
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }

        payload = {**base_data, **event_data}

        response = requests.post(
            f"{self.base_url}/events",
            headers=self.headers,
            json=payload
        )

        if response.status_code == 201:
            return response.json()
        else:
            raise Exception(f"Failed to create event: {response.text}")

# Usage example
tracker = AnalyticsTracker(
    api_token="YOUR_JWT_TOKEN",
    organization_id="550e8400-e29b-41d4-a716-446655440000"
)

# Track a user session
user_id = "550e8400-e29b-41d4-a716-446655440001"
session_id = "550e8400-e29b-41d4-a716-446655440002"

try:
    # Track search
    tracker.track_search(
        user_id=user_id,
        session_id=session_id,
        query="machine learning algorithms",
        results_count=25,
        search_time_ms=150,
        filters_used=["category:technical", "date:recent"],
        ranking_algorithm="bm25"
    )

    # Track document view
    tracker.track_document_view(
        user_id=user_id,
        session_id=session_id,
        document_id="doc123",
        title="Introduction to Machine Learning",
        view_duration_seconds=300,
        scroll_percentage=75
    )

    # Track performance
    tracker.track_performance_metric(
        metric_name="response_time",
        value=150,
        component="api",
        endpoint="/api/v1/search",
        method="GET"
    )

    print("Events tracked successfully!")

except Exception as e:
    print(f"Error tracking events: {e}")
```

### Get Analytics Events

```bash
# Get events with filtering
curl -X GET "http://localhost:8000/api/v1/analytics/events?organization_id=550e8400-e29b-41d4-a716-446655440000&event_type=search&severity=medium&start_time=2024-01-01T00:00:00Z&size=50" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

## Background Jobs Examples

### Create Analytics Job

```bash
# Create a data aggregation job
curl -X POST "http://localhost:8000/api/v1/analytics/jobs" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "job_type": "data_aggregation",
    "job_name": "Daily quality metrics aggregation",
    "description": "Aggregate quality metrics for the last 24 hours",
    "parameters": {
      "metric_type": "average",
      "time_granularity": "day",
      "start_date": "2024-01-01T00:00:00Z",
      "end_date": "2024-01-31T23:59:59Z",
      "data_source": "quality_metrics",
      "filters": {
        "document_type": "pdf",
        "min_quality_score": 0.5
      }
    },
    "config": {
      "priority": "high",
      "max_retries": 3,
      "timeout_seconds": 3600,
      "queue_name": "analytics_high_priority"
    },
    "organization_id": "550e8400-e29b-41d4-a716-446655440000"
  }'
```

```python
# Python example for managing analytics jobs
import requests
import time
from datetime import datetime

class AnalyticsJobManager:
    def __init__(self, api_token, organization_id):
        self.api_token = api_token
        self.organization_id = organization_id
        self.base_url = "http://localhost:8000/api/v1/analytics"
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }

    def create_aggregation_job(self, metric_type, time_granularity, start_date, end_date,
                             data_source, priority="normal", **kwargs):
        """Create a data aggregation job"""
        job_data = {
            "job_type": "data_aggregation",
            "job_name": f"{time_granularity.title()} {metric_type} aggregation",
            "description": f"Aggregate {metric_type} metrics with {time_granularity} granularity",
            "parameters": {
                "metric_type": metric_type,
                "time_granularity": time_granularity,
                "start_date": start_date,
                "end_date": end_date,
                "data_source": data_source,
                **kwargs
            },
            "config": {
                "priority": priority,
                "max_retries": 3,
                "timeout_seconds": 3600
            },
            "organization_id": self.organization_id
        }

        return self._create_job(job_data)

    def create_daily_summary_job(self, target_date=None):
        """Create a daily summary job"""
        if not target_date:
            target_date = datetime.utcnow().date().isoformat()

        job_data = {
            "job_type": "daily_summary",
            "job_name": f"Daily summary - {target_date}",
            "description": "Generate daily analytics summary",
            "parameters": {
                "target_date": target_date,
                "include_quality_metrics": True,
                "include_user_behavior": True,
                "include_performance": True
            },
            "config": {
                "priority": "normal",
                "max_retries": 2,
                "timeout_seconds": 1800
            },
            "organization_id": self.organization_id
        }

        return self._create_job(job_data)

    def create_weekly_report_job(self, week_start=None):
        """Create a weekly report job"""
        if not week_start:
            week_start = datetime.utcnow().date().isoformat()

        job_data = {
            "job_type": "weekly_report",
            "job_name": f"Weekly report - {week_start}",
            "description": "Generate comprehensive weekly analytics report",
            "parameters": {
                "week_start": week_start,
                "include_visualizations": True,
                "include_recommendations": True,
                "email_recipients": ["admin@example.com"]
            },
            "config": {
                "priority": "normal",
                "max_retries": 2,
                "timeout_seconds": 3600
            },
            "organization_id": self.organization_id
        }

        return self._create_job(job_data)

    def _create_job(self, job_data):
        """Create an analytics job"""
        response = requests.post(
            f"{self.base_url}/jobs",
            headers=self.headers,
            json=job_data
        )

        if response.status_code == 201:
            return response.json()
        else:
            raise Exception(f"Failed to create job: {response.text}")

    def get_job_status(self, job_id):
        """Get job status and details"""
        response = requests.get(
            f"{self.base_url}/jobs/{job_id}",
            headers=self.headers
        )

        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Failed to get job status: {response.text}")

    def wait_for_job_completion(self, job_id, timeout_seconds=300, poll_interval=5):
        """Wait for job completion and return result"""
        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            job = self.get_job_status(job_id)
            status = job["status"]

            if status in ["completed", "failed", "cancelled"]:
                return job

            print(f"Job {job_id} status: {status} - {job['progress_percentage']}%")
            time.sleep(poll_interval)

        raise TimeoutError(f"Job {job_id} did not complete within {timeout_seconds} seconds")

    def cancel_job(self, job_id):
        """Cancel a running job"""
        response = requests.delete(
            f"{self.base_url}/jobs/{job_id}",
            headers=self.headers
        )

        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Failed to cancel job: {response.text}")

# Usage example
job_manager = AnalyticsJobManager(
    api_token="YOUR_JWT_TOKEN",
    organization_id="550e8400-e29b-41d4-a716-446655440000"
)

try:
    # Create aggregation job
    job = job_manager.create_aggregation_job(
        metric_type="average",
        time_granularity="day",
        start_date="2024-01-01T00:00:00Z",
        end_date="2024-01-31T23:59:59Z",
        data_source="quality_metrics",
        priority="high"
    )

    job_id = job["id"]
    print(f"Created job: {job_id}")

    # Wait for completion
    result = job_manager.wait_for_job_completion(job_id, timeout_seconds=600)
    print(f"Job completed with status: {result['status']}")

    if result["status"] == "completed":
        print(f"Job result: {result['result']}")
    else:
        print(f"Job failed: {result['error_message']}")

except Exception as e:
    print(f"Error managing job: {e}")
```

### Get Analytics Jobs

```bash
# Get all jobs with filtering
curl -X GET "http://localhost:8000/api/v1/analytics/jobs?organization_id=550e8400-e29b-41d4-a716-446655440000&job_type=data_aggregation&status=completed&page=1&size=20" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"

# Get specific job details
curl -X GET "http://localhost:8000/api/v1/analytics/jobs/550e8400-e29b-41d4-a716-446655440000" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

### Cancel Analytics Job

```bash
curl -X DELETE "http://localhost:8000/api/v1/analytics/jobs/550e8400-e29b-41d4-a716-446655440000" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

## Recommendations Examples

### Get Analytics Recommendations

```bash
# Get all recommendations
curl -X GET "http://localhost:8000/api/v1/analytics/recommendations?organization_id=550e8400-e29b-41d4-a716-446655440000" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"

# Filter by type and priority
curl -X GET "http://localhost:8000/api/v1/analytics/recommendations?organization_id=550e8400-e29b-41d4-a716-446655440000&recommendation_type=quality_improvement&priority=high" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

```python
# Python example for processing recommendations
import requests
from datetime import datetime

class RecommendationProcessor:
    def __init__(self, api_token, organization_id):
        self.api_token = api_token
        self.organization_id = organization_id
        self.base_url = "http://localhost:8000/api/v1/analytics"
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }

    def get_recommendations(self, recommendation_type=None, priority=None, status=None):
        """Get recommendations with optional filtering"""
        params = {"organization_id": self.organization_id}

        if recommendation_type:
            params["recommendation_type"] = recommendation_type
        if priority:
            params["priority"] = priority
        if status:
            params["status"] = status

        response = requests.get(
            f"{self.base_url}/recommendations",
            headers=self.headers,
            params=params
        )

        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Failed to get recommendations: {response.text}")

    def prioritize_recommendations(self, recommendations):
        """Prioritize recommendations based on impact and effort"""
        prioritized = []

        for rec in recommendations["data"]:
            # Calculate priority score
            impact_score = rec.get("impact_score", 0)
            confidence_score = rec.get("confidence_score", 0)
            effort_factor = {"low": 1.0, "medium": 0.7, "high": 0.4}.get(rec.get("effort_estimate", "medium"), 0.7)

            priority_score = (impact_score * confidence_score * effort_factor) * 100
            rec["priority_score"] = priority_score

            prioritized.append(rec)

        # Sort by priority score (descending)
        prioritized.sort(key=lambda x: x["priority_score"], reverse=True)

        return prioritized

    def generate_action_plan(self, recommendations, max_items=10):
        """Generate an actionable plan from recommendations"""
        prioritized = self.prioritize_recommendations(recommendations)
        top_recommendations = prioritized[:max_items]

        action_plan = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "total_recommendations": len(recommendations["data"]),
            "prioritized_count": len(top_recommendations),
            "actions": []
        }

        for i, rec in enumerate(top_recommendations, 1):
            action = {
                "rank": i,
                "recommendation_id": rec["id"],
                "title": rec["title"],
                "description": rec["description"],
                "type": rec["recommendation_type"],
                "priority": rec["priority"],
                "priority_score": rec["priority_score"],
                "impact_score": rec["impact_score"],
                "confidence_score": rec["confidence_score"],
                "effort_estimate": rec["effort_estimate"],
                "auto_applicable": rec["auto_applicable"],
                "actionable_steps": rec["actionable_steps"],
                "estimated_timeline": self._estimate_timeline(rec),
                "resource_requirements": self._estimate_resources(rec)
            }

            action_plan["actions"].append(action)

        return action_plan

    def _estimate_timeline(self, recommendation):
        """Estimate implementation timeline"""
        effort = recommendation.get("effort_estimate", "medium")
        timelines = {
            "low": "1-2 days",
            "medium": "1-2 weeks",
            "high": "2-4 weeks"
        }
        return timelines.get(effort, "1-2 weeks")

    def _estimate_resources(self, recommendation):
        """Estimate resource requirements"""
        effort = recommendation.get("effort_estimate", "medium")
        rec_type = recommendation.get("recommendation_type", "")

        resources = {"person_hours": 0, "skills": [], "tools": []}

        if effort == "low":
            resources["person_hours"] = 8
            resources["skills"] = ["analytics"]
        elif effort == "medium":
            resources["person_hours"] = 40
            if "quality" in rec_type:
                resources["skills"] = ["analytics", "content_review"]
            elif "performance" in rec_type:
                resources["skills"] = ["analytics", "system_admin"]
            else:
                resources["skills"] = ["analytics"]
        else:  # high
            resources["person_hours"] = 120
            resources["skills"] = ["analytics", "development", "system_admin"]
            resources["tools"] = ["development_environment", "testing_tools"]

        return resources

# Usage example
processor = RecommendationProcessor(
    api_token="YOUR_JWT_TOKEN",
    organization_id="550e8400-e29b-41d4-a716-446655440000"
)

try:
    # Get quality improvement recommendations
    recommendations = processor.get_recommendations(
        recommendation_type="quality_improvement",
        priority="high"
    )

    # Generate action plan
    action_plan = processor.generate_action_plan(recommendations)

    print(f"Generated action plan with {len(action_plan['actions'])} prioritized actions")

    # Print top 3 actions
    for action in action_plan["actions"][:3]:
        print(f"\n{action['rank']}. {action['title']}")
        print(f"   Priority Score: {action['priority_score']:.1f}")
        print(f"   Timeline: {action['estimated_timeline']}")
        print(f"   Effort: {action['effort_estimate']}")
        print(f"   Steps: {', '.join(action['actionable_steps'][:2])}...")

except Exception as e:
    print(f"Error processing recommendations: {e}")
```

## Error Handling Examples

### Common Error Responses

```json
// Authentication Error (401)
{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "Authentication required",
    "details": "Please provide a valid JWT token",
    "timestamp": "2024-01-15T10:30:00Z",
    "request_id": "req_123456"
  }
}

// Authorization Error (403)
{
  "error": {
    "code": "ANALYTICS_ACCESS_DENIED",
    "message": "Insufficient permissions for analytics access",
    "details": "User role 'standard' requires 'analytics:read' permission",
    "timestamp": "2024-01-15T10:30:00Z",
    "request_id": "req_123456"
  }
}

// Validation Error (422)
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed",
    "details": "Invalid UUID format for organization_id",
    "timestamp": "2024-01-15T10:30:00Z",
    "request_id": "req_123456"
  }
}

// Not Found Error (404)
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "The requested resource was not found",
    "details": "Analytics job with ID '123' does not exist",
    "timestamp": "2024-01-15T10:30:00Z",
    "request_id": "req_123456"
  }
}

// Rate Limit Error (429)
{
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "Too many requests",
    "details": "Rate limit of 100 requests per hour exceeded. Please wait 45 minutes.",
    "timestamp": "2024-01-15T10:30:00Z",
    "request_id": "req_123456"
  }
}
```

### Error Handling in Code

```python
import requests
from requests.exceptions import RequestException
import time
import json

class AnalyticsAPIError(Exception):
    """Custom exception for Analytics API errors"""
    def __init__(self, response, message=None):
        self.response = response
        self.status_code = response.status_code
        self.error_data = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}

        if message:
            super().__init__(message)
        else:
            error_message = self.error_data.get('error', {}).get('message', f'HTTP {self.status_code}')
            super().__init__(error_message)

class AnalyticsAPIClient:
    def __init__(self, base_url, api_token):
        self.base_url = base_url.rstrip('/')
        self.api_token = api_token
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json'
        })

    def _make_request(self, method, endpoint, **kwargs):
        """Make request with error handling and retries"""
        url = f"{self.base_url}{endpoint}"
        max_retries = 3
        retry_delay = 1

        for attempt in range(max_retries):
            try:
                response = self.session.request(method, url, **kwargs)

                # Handle successful responses
                if 200 <= response.status_code < 300:
                    return response.json() if response.headers.get('content-type', '').startswith('application/json') else response.text

                # Handle client errors (4xx) - don't retry
                elif 400 <= response.status_code < 500:
                    if response.status_code == 429:  # Rate limit
                        retry_after = int(response.headers.get('Retry-After', 60))
                        print(f"Rate limited. Waiting {retry_after} seconds...")
                        time.sleep(retry_after)
                        continue

                    raise AnalyticsAPIError(response)

                # Handle server errors (5xx) - retry
                elif response.status_code >= 500:
                    if attempt < max_retries - 1:
                        print(f"Server error {response.status_code}. Retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                    else:
                        raise AnalyticsAPIError(response)

            except RequestException as e:
                if attempt < max_retries - 1:
                    print(f"Request error: {e}. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                else:
                    raise AnalyticsAPIError(response, f"Request failed: {e}")

    def get_quality_metrics(self, organization_id, **params):
        """Get quality metrics with error handling"""
        try:
            params['organization_id'] = organization_id
            return self._make_request('GET', '/api/v1/analytics/quality/metrics', params=params)
        except AnalyticsAPIError as e:
            if e.status_code == 403:
                print("Access denied. Check your analytics permissions.")
            elif e.status_code == 422:
                print(f"Validation error: {e.error_data.get('error', {}).get('details')}")
            else:
                print(f"Error fetching quality metrics: {e}")
            raise

    def create_analytics_event(self, event_data):
        """Create analytics event with error handling"""
        try:
            return self._make_request('POST', '/api/v1/analytics/events', json=event_data)
        except AnalyticsAPIError as e:
            if e.status_code == 422:
                print(f"Invalid event data: {e.error_data.get('error', {}).get('details')}")
            else:
                print(f"Error creating event: {e}")
            raise

# Usage example
client = AnalyticsAPIClient(
    base_url="http://localhost:8000",
    api_token="YOUR_JWT_TOKEN"
)

try:
    # Get quality metrics
    metrics = client.get_quality_metrics(
        organization_id="550e8400-e29b-41d4-a716-446655440000",
        start_date="2024-01-01T00:00:00Z",
        end_date="2024-01-31T23:59:59Z"
    )

    print(f"Retrieved {len(metrics['data'])} quality metrics")

    # Create event
    event_data = {
        "event_type": "search",
        "event_name": "document_search",
        "event_data": {
            "query": "test query",
            "results_count": 10
        },
        "organization_id": "550e8400-e29b-41d4-a716-446655440000"
    }

    event = client.create_analytics_event(event_data)
    print(f"Created event: {event['id']}")

except AnalyticsAPIError as e:
    print(f"Analytics API error: {e}")
    print(f"Request ID: {e.error_data.get('error', {}).get('request_id')}")
except Exception as e:
    print(f"Unexpected error: {e}")
```

## SDK Integration Examples

### Python SDK

```python
# rag_analytics_sdk.py
import requests
import json
from typing import Dict, List, Optional, Any
from datetime import datetime
import time

class RAGAnalyticsSDK:
    """Official RAG Analytics SDK for Python"""

    def __init__(self, base_url: str, api_token: str, organization_id: str):
        self.base_url = base_url.rstrip('/')
        self.api_token = api_token
        self.organization_id = organization_id
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json'
        })

    # Quality Metrics
    def get_quality_metrics(self, **filters) -> Dict[str, Any]:
        """Get quality metrics"""
        filters['organization_id'] = self.organization_id
        response = self.session.get(f"{self.base_url}/api/v1/analytics/quality/metrics", params=filters)
        response.raise_for_status()
        return response.json()

    def get_quality_recommendations(self, **filters) -> Dict[str, Any]:
        """Get quality recommendations"""
        filters['organization_id'] = self.organization_id
        response = self.session.get(f"{self.base_url}/api/v1/analytics/quality/recommendations", params=filters)
        response.raise_for_status()
        return response.json()

    def get_quality_dashboard(self, time_range: str = "30d") -> Dict[str, Any]:
        """Get quality dashboard data"""
        params = {"organization_id": self.organization_id, "time_range": time_range}
        response = self.session.get(f"{self.base_url}/api/v1/analytics/quality/dashboard", params=params)
        response.raise_for_status()
        return response.json()

    # User Behavior Analytics
    def get_user_sessions(self, **filters) -> Dict[str, Any]:
        """Get user sessions"""
        filters['organization_id'] = self.organization_id
        response = self.session.get(f"{self.base_url}/api/v1/analytics/behavior/sessions", params=filters)
        response.raise_for_status()
        return response.json()

    def get_behavior_events(self, **filters) -> Dict[str, Any]:
        """Get behavior events"""
        filters['organization_id'] = self.organization_id
        response = self.session.get(f"{self.base_url}/api/v1/analytics/behavior/events", params=filters)
        response.raise_for_status()
        return response.json()

    def get_behavior_dashboard(self, time_range: str = "7d") -> Dict[str, Any]:
        """Get behavior dashboard data"""
        params = {"organization_id": self.organization_id, "time_range": time_range}
        response = self.session.get(f"{self.base_url}/api/v1/analytics/behavior/dashboard", params=params)
        response.raise_for_status()
        return response.json()

    # Performance Monitoring
    def get_performance_metrics(self, **filters) -> Dict[str, Any]:
        """Get performance metrics"""
        filters['organization_id'] = self.organization_id
        response = self.session.get(f"{self.base_url}/api/v1/analytics/performance/metrics", params=filters)
        response.raise_for_status()
        return response.json()

    def get_system_health(self) -> Dict[str, Any]:
        """Get system health status"""
        params = {"organization_id": self.organization_id}
        response = self.session.get(f"{self.base_url}/api/v1/analytics/performance/health", params=params)
        response.raise_for_status()
        return response.json()

    # Events
    def create_event(self, event_type: str, event_name: str, event_data: Dict[str, Any],
                    user_id: Optional[str] = None, session_id: Optional[str] = None,
                    document_id: Optional[str] = None, severity: str = "low") -> Dict[str, Any]:
        """Create analytics event"""
        payload = {
            "event_type": event_type,
            "event_name": event_name,
            "event_data": event_data,
            "organization_id": self.organization_id,
            "severity": severity
        }

        if user_id:
            payload["user_id"] = user_id
        if session_id:
            payload["session_id"] = session_id
        if document_id:
            payload["document_id"] = document_id

        response = self.session.post(f"{self.base_url}/api/v1/analytics/events", json=payload)
        response.raise_for_status()
        return response.json()

    # Jobs
    def create_job(self, job_type: str, job_name: str, parameters: Dict[str, Any],
                  config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Create analytics job"""
        payload = {
            "job_type": job_type,
            "job_name": job_name,
            "parameters": parameters,
            "organization_id": self.organization_id,
            "config": config or {}
        }

        response = self.session.post(f"{self.base_url}/api/v1/analytics/jobs", json=payload)
        response.raise_for_status()
        return response.json()

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """Get job details"""
        response = self.session.get(f"{self.base_url}/api/v1/analytics/jobs/{job_id}")
        response.raise_for_status()
        return response.json()

    def wait_for_job(self, job_id: str, timeout: int = 300, poll_interval: int = 5) -> Dict[str, Any]:
        """Wait for job completion"""
        start_time = time.time()

        while time.time() - start_time < timeout:
            job = self.get_job(job_id)
            if job["status"] in ["completed", "failed", "cancelled"]:
                return job
            time.sleep(poll_interval)

        raise TimeoutError(f"Job {job_id} did not complete within {timeout} seconds")

    # Recommendations
    def get_recommendations(self, **filters) -> Dict[str, Any]:
        """Get recommendations"""
        filters['organization_id'] = self.organization_id
        response = self.session.get(f"{self.base_url}/api/v1/analytics/recommendations", params=filters)
        response.raise_for_status()
        return response.json()

# Usage example
def main():
    # Initialize SDK
    sdk = RAGAnalyticsSDK(
        base_url="http://localhost:8000",
        api_token="YOUR_JWT_TOKEN",
        organization_id="550e8400-e29b-41d4-a716-446655440000"
    )

    try:
        # Get quality metrics
        metrics = sdk.get_quality_metrics(
            start_date="2024-01-01T00:00:00Z",
            end_date="2024-01-31T23:59:59Z"
        )
        print(f"Retrieved {len(metrics['data'])} quality metrics")

        # Create events
        sdk.create_event(
            event_type="search",
            event_name="document_search",
            event_data={"query": "test", "results_count": 10},
            user_id="user123",
            session_id="session456"
        )

        # Create and wait for job
        job = sdk.create_job(
            job_type="data_aggregation",
            job_name="Daily aggregation",
            parameters={
                "metric_type": "average",
                "time_granularity": "day",
                "data_source": "quality_metrics"
            }
        )

        result = sdk.wait_for_job(job["id"], timeout=60)
        print(f"Job completed: {result['status']}")

        # Get recommendations
        recommendations = sdk.get_recommendations(priority="high")
        print(f"Found {len(recommendations['data'])} high-priority recommendations")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
```

### JavaScript/Node.js SDK

```javascript
// rag-analytics-sdk.js
const axios = require('axios');

class RAGAnalyticsSDK {
    constructor(baseURL, apiToken, organizationId) {
        this.baseURL = baseURL.replace(/\/$/, '');
        this.apiToken = apiToken;
        this.organizationId = organizationId;

        this.client = axios.create({
            baseURL: this.baseURL,
            headers: {
                'Authorization': `Bearer ${apiToken}`,
                'Content-Type': 'application/json'
            }
        });
    }

    // Quality Metrics
    async getQualityMetrics(filters = {}) {
        const response = await this.client.get('/api/v1/analytics/quality/metrics', {
            params: { organization_id: this.organizationId, ...filters }
        });
        return response.data;
    }

    async getQualityRecommendations(filters = {}) {
        const response = await this.client.get('/api/v1/analytics/quality/recommendations', {
            params: { organization_id: this.organizationId, ...filters }
        });
        return response.data;
    }

    async getQualityDashboard(timeRange = '30d') {
        const response = await this.client.get('/api/v1/analytics/quality/dashboard', {
            params: { organization_id: this.organizationId, time_range: timeRange }
        });
        return response.data;
    }

    // User Behavior Analytics
    async getUserSessions(filters = {}) {
        const response = await this.client.get('/api/v1/analytics/behavior/sessions', {
            params: { organization_id: this.organizationId, ...filters }
        });
        return response.data;
    }

    async getBehaviorEvents(filters = {}) {
        const response = await this.client.get('/api/v1/analytics/behavior/events', {
            params: { organization_id: this.organizationId, ...filters }
        });
        return response.data;
    }

    async getBehaviorDashboard(timeRange = '7d') {
        const response = await this.client.get('/api/v1/analytics/behavior/dashboard', {
            params: { organization_id: this.organizationId, time_range: timeRange }
        });
        return response.data;
    }

    // Performance Monitoring
    async getPerformanceMetrics(filters = {}) {
        const response = await this.client.get('/api/v1/analytics/performance/metrics', {
            params: { organization_id: this.organizationId, ...filters }
        });
        return response.data;
    }

    async getSystemHealth() {
        const response = await this.client.get('/api/v1/analytics/performance/health', {
            params: { organization_id: this.organizationId }
        });
        return response.data;
    }

    // Events
    async createEvent(eventType, eventName, eventData, options = {}) {
        const payload = {
            event_type: eventType,
            event_name: eventName,
            event_data: eventData,
            organization_id: this.organizationId,
            severity: options.severity || 'low',
            ...options
        };

        const response = await this.client.post('/api/v1/analytics/events', payload);
        return response.data;
    }

    // Jobs
    async createJob(jobType, jobName, parameters, config = {}) {
        const payload = {
            job_type: jobType,
            job_name: jobName,
            parameters,
            organization_id: this.organizationId,
            config
        };

        const response = await this.client.post('/api/v1/analytics/jobs', payload);
        return response.data;
    }

    async getJob(jobId) {
        const response = await this.client.get(`/api/v1/analytics/jobs/${jobId}`);
        return response.data;
    }

    async waitForJob(jobId, timeout = 300000, pollInterval = 5000) {
        const startTime = Date.now();

        return new Promise((resolve, reject) => {
            const poll = async () => {
                try {
                    const job = await this.getJob(jobId);

                    if (['completed', 'failed', 'cancelled'].includes(job.status)) {
                        return resolve(job);
                    }

                    if (Date.now() - startTime > timeout) {
                        return reject(new Error(`Job ${jobId} did not complete within ${timeout}ms`));
                    }

                    setTimeout(poll, pollInterval);
                } catch (error) {
                    reject(error);
                }
            };

            poll();
        });
    }

    // Recommendations
    async getRecommendations(filters = {}) {
        const response = await this.client.get('/api/v1/analytics/recommendations', {
            params: { organization_id: this.organizationId, ...filters }
        });
        return response.data;
    }
}

module.exports = RAGAnalyticsSDK;

// Usage example
async function main() {
    const sdk = new RAGAnalyticsSDK(
        'http://localhost:8000',
        'YOUR_JWT_TOKEN',
        '550e8400-e29b-41d4-a716-446655440000'
    );

    try {
        // Get quality metrics
        const metrics = await sdk.getQualityMetrics({
            start_date: '2024-01-01T00:00:00Z',
            end_date: '2024-01-31T23:59:59Z'
        });
        console.log(`Retrieved ${metrics.data.length} quality metrics`);

        // Create event
        const event = await sdk.createEvent(
            'search',
            'document_search',
            { query: 'test', results_count: 10 },
            { user_id: 'user123', session_id: 'session456' }
        );
        console.log(`Created event: ${event.id}`);

        // Create and wait for job
        const job = await sdk.createJob(
            'data_aggregation',
            'Daily aggregation',
            {
                metric_type: 'average',
                time_granularity: 'day',
                data_source: 'quality_metrics'
            }
        );

        const result = await sdk.waitForJob(job.id, 60000);
        console.log(`Job completed: ${result.status}`);

        // Get recommendations
        const recommendations = await sdk.getRecommendations({ priority: 'high' });
        console.log(`Found ${recommendations.data.length} high-priority recommendations`);

    } catch (error) {
        console.error('Error:', error.response?.data || error.message);
    }
}

if (require.main === module) {
    main();
}
```

This comprehensive documentation provides:

1. **Complete API Examples** - Real-world usage examples for all endpoints
2. **Multiple Language Support** - Python, JavaScript, and cURL examples
3. **Error Handling** - Comprehensive error handling patterns
4. **SDK Integration** - Ready-to-use SDK classes for Python and JavaScript
5. **Best Practices** - Pagination, retries, authentication, and security
6. **Advanced Use Cases** - Job management, batch processing, and automation
7. **Sample Code** - Production-ready code snippets

The documentation follows the OpenAPI specification and provides practical, copy-pasteable examples that developers can immediately use in their applications.
# Workers Monitoring API

## Overview

The Workers Monitoring API provides real-time visibility into the Celery background worker infrastructure. This API allows you to monitor worker health, task execution, and queue status.

## Endpoints

### 1. Get Worker Status

**Endpoint:** `GET /api/v1/workers/status`

**Authentication:** Required (any authenticated user)

**Description:** Get comprehensive status information about all Celery workers.

**Response:**
```json
{
  "active_workers": 1,
  "total_workers": 1,
  "active_tasks": 0,
  "pending_tasks": 0,
  "registered_tasks": 6,
  "worker_details": [
    {
      "hostname": "celery@1ca5e9bb5dac",
      "status": "online",
      "active_tasks": 0,
      "pending_tasks": 0,
      "total_processed": 0,
      "pool": {
        "implementation": "celery.concurrency.prefork:TaskPool",
        "max-concurrency": 4,
        "processes": [21, 22, 23, 24]
      },
      "queues": [],
      "registered_tasks": 6
    }
  ]
}
```

**Fields:**
- `active_workers`: Number of workers currently online and responding
- `total_workers`: Total number of workers configured
- `active_tasks`: Number of tasks currently being processed
- `pending_tasks`: Number of tasks waiting in queues
- `registered_tasks`: Number of unique task types registered
- `worker_details`: Detailed information about each worker

### 2. Get Worker Health

**Endpoint:** `GET /api/v1/workers/health`

**Authentication:** Required (any authenticated user)

**Description:** Get overall health status of the worker infrastructure.

**Response:**
```json
{
  "healthy": true,
  "workers_online": 1,
  "issues": [],
  "timestamp": "2025-10-14T22:24:26.183882"
}
```

**Fields:**
- `healthy`: Boolean indicating if the worker infrastructure is healthy
- `workers_online`: Number of workers currently online
- `issues`: List of any detected issues (empty if healthy)
- `timestamp`: ISO 8601 timestamp of the health check

### 3. Get Queue Status

**Endpoint:** `GET /api/v1/workers/queues`

**Authentication:** Required (admin only)

**Description:** Get statistics about task queues.

**Response:**
```json
[
  {
    "queue_name": "document_processing",
    "pending_messages": 0,
    "active_consumers": 1
  },
  {
    "queue_name": "text_processing",
    "pending_messages": 0,
    "active_consumers": 1
  },
  {
    "queue_name": "vector_processing",
    "pending_messages": 0,
    "active_consumers": 1
  }
]
```

### 4. Ping Workers

**Endpoint:** `POST /api/v1/workers/ping`

**Authentication:** Required (admin only)

**Description:** Ping all workers to check connectivity.

**Response:**
```json
{
  "message": "1 workers responded",
  "workers": [
    {
      "worker": "celery@1ca5e9bb5dac",
      "response": {"ok": "pong"},
      "timestamp": "2025-10-14T22:24:26.183882"
    }
  ],
  "total": 1
}
```

### 5. Get Registered Tasks

**Endpoint:** `GET /api/v1/workers/registered-tasks`

**Authentication:** Required (admin only)

**Description:** Get list of all registered tasks across all workers.

**Response:**
```json
{
  "total_tasks": 6,
  "tasks": [
    "src.tasks.processing_tasks.process_document",
    "src.tasks.processing_tasks.extract_text",
    "src.tasks.processing_tasks.generate_embeddings",
    "src.tasks.processing_tasks.extract_entities",
    "src.tasks.processing_tasks.build_knowledge_graph",
    "src.tasks.processing_tasks.index_document"
  ],
  "workers": 1
}
```

### 6. Shutdown Worker

**Endpoint:** `POST /api/v1/workers/shutdown/{worker_name}`

**Authentication:** Required (admin only)

**Description:** Shutdown a specific worker.

**Warning:** ⚠️ This will terminate the worker process!

**Response:**
```json
{
  "message": "Shutdown command sent to celery@1ca5e9bb5dac",
  "worker": "celery@1ca5e9bb5dac"
}
```

## Usage Examples

### Python Example

```python
import requests

BASE_URL = "http://localhost:8000"

# Login to get token
login_response = requests.post(
    f"{BASE_URL}/api/v1/auth/login",
    json={"email": "user@example.com", "password": "password"}
)
token = login_response.json()["access_token"]

headers = {"Authorization": f"Bearer {token}"}

# Get worker status
workers_response = requests.get(
    f"{BASE_URL}/api/v1/workers/status",
    headers=headers
)
workers_data = workers_response.json()

print(f"Active Workers: {workers_data['active_workers']}")
print(f"Active Tasks: {workers_data['active_tasks']}")
print(f"Pending Tasks: {workers_data['pending_tasks']}")

# Check worker health
health_response = requests.get(
    f"{BASE_URL}/api/v1/workers/health",
    headers=headers
)
health_data = health_response.json()

if health_data['healthy']:
    print("✅ Workers are healthy")
else:
    print(f"❌ Worker issues: {health_data['issues']}")
```

### cURL Example

```bash
# Login and get token
TOKEN=$(curl -s -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"password"}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

# Get worker status
curl -X GET "http://localhost:8000/api/v1/workers/status" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool

# Get worker health
curl -X GET "http://localhost:8000/api/v1/workers/health" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

## Integration with Notebook

You can use these endpoints in the Jupyter notebook to monitor background processing:

```python
# In your notebook
import requests

BASE_URL = "http://localhost:8000"

# Assuming you already have auth_token from login
headers = {"Authorization": f"Bearer {auth_token}"}

# Get worker status
response = requests.get(f"{BASE_URL}/api/v1/workers/status", headers=headers)
worker_data = response.json()

print(f"✅ Active Workers: {worker_data['active_workers']}")
print(f"⚙️ Active Tasks: {worker_data['active_tasks']}")
print(f"📋 Pending Tasks: {worker_data['pending_tasks']}")

# Check health
health_response = requests.get(f"{BASE_URL}/api/v1/workers/health", headers=headers)
health_data = health_response.json()

if health_data['healthy']:
    print("✅ Background workers operational")
else:
    print(f"❌ Worker issues detected: {health_data['issues']}")
```

## Monitoring Dashboard

The workers API integrates with the notebook's background processing test section. Update cell 19 to use the new endpoint:

```python
# Test background processing and job status
print("⚙️ **Testing Background Processing...**")

# Get worker status
try:
    worker_response = tester.session.get(f"{BASE_URL}/api/v1/workers/status", timeout=10)
    if worker_response.status_code == 200:
        worker_data = worker_response.json()
        print("✅ Background Workers Active")
        print(f"- Active Workers: {worker_data.get('active_workers', 'Unknown')}")
        print(f"- Active Tasks: {worker_data.get('active_tasks', 'Unknown')}")
        print(f"- Pending Tasks: {worker_data.get('pending_tasks', 'Unknown')}")
        print(f"- Registered Tasks: {worker_data.get('registered_tasks', 'Unknown')}")

        # Show worker details
        for worker in worker_data.get('worker_details', []):
            print(f"\n  Worker: {worker['hostname']}")
            print(f"    Status: {worker['status']}")
            print(f"    Concurrency: {worker.get('pool', {}).get('max-concurrency', 'Unknown')}")
            print(f"    Tasks Processed: {worker['total_processed']}")
    else:
        print(f"⚠️ Worker status returned: {worker_response.status_code}")
except Exception as e:
    print(f"❌ Worker status error: {str(e)}")
```

## Troubleshooting

### No Workers Responding

If you see `active_workers: 0`:

1. **Check if Celery is running:**
   ```bash
   docker-compose ps celery
   ```

2. **Check Celery logs:**
   ```bash
   docker-compose logs -f celery
   ```

3. **Restart Celery:**
   ```bash
   docker-compose restart celery
   ```

### Worker Issues Detected

If `healthy: false` with issues:

1. **Check the specific issue:**
   ```json
   {
     "healthy": false,
     "workers_online": 0,
     "issues": ["No workers are currently online"]
   }
   ```

2. **Investigate worker configuration:**
   - Check `backend/celery_worker.py`
   - Verify Redis connection
   - Check worker queues configuration

### High Pending Tasks

If `pending_tasks` is high:

1. **Scale workers:**
   ```bash
   docker-compose up -d --scale celery=3
   ```

2. **Check task failures:**
   ```bash
   # Get failed jobs
   curl -X GET "http://localhost:8000/api/v1/processing/jobs?status=failed" \
     -H "Authorization: Bearer $TOKEN"
   ```

## Architecture

### Worker Configuration

Workers are configured in [backend/celery_worker.py](../../backend/celery_worker.py):

- **Concurrency:** 4 worker processes
- **Queues:**
  - `document_processing`: Document upload and parsing
  - `text_processing`: Text extraction and analysis
  - `vector_processing`: Embedding generation
  - `entity_processing`: Entity extraction
  - `graph_processing`: Knowledge graph building

### Task Types

Registered tasks include:
1. `process_document` - Main document processing pipeline
2. `extract_text` - Text extraction from documents
3. `generate_embeddings` - Vector embedding generation
4. `extract_entities` - Entity recognition
5. `build_knowledge_graph` - Graph construction
6. `index_document` - Search index updates

## Files Created/Modified

1. **New File:** [backend/src/api/infrastructure/workers.py](../../backend/src/api/infrastructure/workers.py) - Workers monitoring API
2. **Modified:** [backend/src/main.py](../../backend/src/main.py) - Added workers router
3. **Test Script:** [test_workers_endpoint.sh](../../scripts/test_workers_endpoint.sh) - Endpoint verification
4. **Documentation:** [WORKERS_MONITORING.md](WORKERS_MONITORING.md) - This file

## Benefits

✅ **Real-time Monitoring:** See worker status and task execution in real-time
✅ **Health Checks:** Automated detection of worker issues
✅ **Performance Metrics:** Track task throughput and processing times
✅ **Admin Tools:** Ping, shutdown, and manage workers
✅ **Integration:** Works seamlessly with existing notebook demos

## Next Steps

1. **Add to Notebook:** Update cell 19 to use the new `/api/v1/workers/status` endpoint
2. **Dashboard:** Create a visual dashboard for worker metrics
3. **Alerts:** Set up notifications when workers become unhealthy
4. **Metrics:** Integrate with Prometheus/Grafana for long-term monitoring

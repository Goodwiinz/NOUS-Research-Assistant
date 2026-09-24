# knowledge-graph-analytics Helm chart

Backend (FastAPI) + frontend (Next.js) + celery + Neo4j for the NOUS Multimodal Intelligence Platform.

Three environment value files:

| File | Cluster | Namespace | URL |
|------|---------|-----------|-----|
| `values-dev.yaml` | `do-nyc3-rag-system-cluster` | `rag-dev` | `dev-app.goodwiinz.tech` |
| `values-staging.yaml` | same | `rag-staging` | staging |
| `values-production.yaml` | same | `rag-production` | `app.goodwiinz.tech` |

## Externally managed secrets

The chart references these k8s Secrets via `secretKeyRef`. They are **created out of band** (manual `kubectl create secret`, or whatever external secret operator you wire up) — no plaintext values live in this repo.

| Secret name | Required keys | Source of truth |
|---|---|---|
| `database-credentials` | `DATABASE_URL`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | DigitalOcean Managed Postgres |
| `app-secrets` | `JWT_SECRET`, `SECRET_KEY`, etc. | generated per env |
| `redis-credentials` | `REDIS_URL` | DO Managed Valkey |
| `spaces-credentials` | `S3_ACCESS_KEY`, `S3_SECRET_KEY` | DO Spaces |
| `supabase-credentials` | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` | Supabase project |
| `azure-openai-credentials` | `AZURE_OPENAI_CHAT_ENDPOINT`, `AZURE_OPENAI_CHAT_API_KEY`, `AZURE_OPENAI_CHAT_DEPLOYMENT_NAME`, `AZURE_OPENAI_CHAT_API_VERSION` | Azure portal → Cognitive Services resource |
| `neo4j-credentials` | `NEO4J_USER`, `NEO4J_PASSWORD` | Neo4j cluster (in-cluster StatefulSet) |

### Updating a secret

```sh
NS=rag-dev   # or rag-staging / rag-production

# 1. show what's there now
kubectl get secret -n $NS azure-openai-credentials \
  -o jsonpath='{.data.AZURE_OPENAI_CHAT_ENDPOINT}' | base64 -d

# 2. patch a single key (preserves others)
kubectl patch secret -n $NS azure-openai-credentials --type='json' \
  -p='[{"op":"replace","path":"/data/AZURE_OPENAI_CHAT_ENDPOINT","value":"'"$(echo -n https://your-resource.cognitiveservices.azure.com | base64)"'"}]'

# 3. roll the backend so the new env is picked up
kubectl rollout restart deploy/nous-${NS#rag-}-knowledge-graph-analytics-backend -n $NS
kubectl rollout status   deploy/nous-${NS#rag-}-knowledge-graph-analytics-backend -n $NS
```

### Verification

```sh
POD=$(kubectl get pod -n $NS -l app.kubernetes.io/component=backend -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n $NS $POD -- python -c "import socket,os,urllib.parse as u; \
  print(socket.gethostbyname(u.urlparse(os.environ['AZURE_OPENAI_CHAT_ENDPOINT']).hostname))"
```
A printed IP = endpoint resolves; `socket.gaierror` = wrong hostname or DNS issue.

## Database migrations

The backend deployment ships with an `initContainer` named `run-migrations` that executes `alembic upgrade heads` against the same `DATABASE_URL` the app uses. It is gated by a per-env values flag:

```yaml
backend:
  initContainers:
    runMigrations: true   # dev + staging
    runMigrations: false  # production
```

Production explicitly opts out so schema changes are reviewed and applied via `kubectl exec ... alembic upgrade heads` after a snapshot. Dev/staging auto-apply on every rollout to prevent drift like the `chat_messages.tool_executions` UndefinedColumnError this flag was added in response to.

To run migrations manually:
```sh
kubectl exec -n $NS $POD -- alembic current
kubectl exec -n $NS $POD -- alembic upgrade heads --sql > /tmp/pending.sql   # dry-run
kubectl exec -n $NS $POD -- alembic upgrade heads
```

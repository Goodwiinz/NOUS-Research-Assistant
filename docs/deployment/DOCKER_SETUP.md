# Docker Setup for Multimodal Enterprise RAG System

> **Status: historical (reconciled 2026-10-17).** This guide was written for earlier deployment generations (Docker Compose, single-host or DigitalOcean/DOKS setups). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Qdrant is removed; live retrieval is PostgreSQL full-text. Start with the [AWS deployment overview](aws-overview.md) and [AWS on-call commands](../operations/aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

This guide covers the Docker configuration for running the RAG system in local development. **Production runs on DOKS (DigitalOcean Kubernetes) + ArgoCD with Supabase-managed Postgres, DO Managed Redis, DO Spaces for storage, and the frontend on Vercel — not Docker Compose.**

## 🚀 Quick Start

### 1. Environment Configuration

Choose your configuration template:

```bash
# For local development with optional Azure OpenAI
cp .env.example .env

# For Azure OpenAI focused setup
cp .env.azure .env
```

### 2. Update Configuration

Edit your `.env` file with your actual values:

```bash
# Required for Azure OpenAI
AZURE_OPENAI_API_KEY=your_api_key_here
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME=text-embedding-ada-002
```

### 3. Start the System

Use the startup script for easy configuration:

```bash
# Development mode (local services)
./start.sh dev

# Azure OpenAI mode
./start.sh azure

# Azure OpenAI mode (local simulation only)
./start.sh cloud
```

## 📋 Configuration Options

### Development Environment (`docker-compose.development.yml`)

**Features:**

- Local PostgreSQL (`rag-postgres-1`), Redis, Neo4j
- Hot-reload for backend development
- Debug logging enabled
- Development tools (Adminer, Redis Commander)
- Note: Qdrant is **not used** — vector retrieval uses PG fulltext; Qdrant is removed from production

**Use Case:** Local development and testing

### Azure OpenAI Environment (`docker-compose.azure.yml`)

**Features:**

- Azure OpenAI integration
- Local simulation only — not the live production path
- Optimized performance settings

**Use Case:** Local simulation with Azure OpenAI; production runs on DOKS/ArgoCD

## 🔧 Environment Variables

### Core Configuration

| Variable      | Default       | Description            |
| ------------- | ------------- | ---------------------- |
| `ENVIRONMENT` | `development` | Environment mode       |
| `SECRET_KEY`  | -             | Application secret key |
| `DEBUG`       | `true`        | Enable debug mode      |
| `LOG_LEVEL`   | `DEBUG`       | Logging level          |

### Database Configuration

| Variable       | Default                | Description                  |
| -------------- | ---------------------- | ---------------------------- |
| `DATABASE_URL` | -                      | PostgreSQL connection string |
| `REDIS_URL`    | `redis://redis:6379/0` | Redis connection string      |
| `NEO4J_URI`    | `bolt://neo4j:7687`    | Neo4j connection URI         |

### Vector / RAG Configuration

> Qdrant is removed from production. Effective retrieval uses PostgreSQL fulltext. `DO_KB_ENABLED` enables the DO Knowledge Base (GradientAI) — off by default.

| Variable        | Default | Description                                   |
| --------------- | ------- | --------------------------------------------- |
| `DO_KB_ENABLED` | `false` | Enable DO Knowledge Base (GradientAI) for RAG |

### Object Storage

> Production uses DO Spaces (`STORAGE_BACKEND=s3`, bucket `rag-system-storage`, region `nyc3`). MinIO is a dev-only compose service — it is **not** wired to `STORAGE_BACKEND` in production.

| Variable          | Default | Description                       |
| ----------------- | ------- | --------------------------------- |
| `STORAGE_BACKEND` | `local` | Set to `s3` for DO Spaces in prod |
| `AWS_S3_BUCKET`   | -       | DO Spaces bucket name             |
| `AWS_S3_REGION`   | -       | DO Spaces region (e.g. `nyc3`)    |

### Azure OpenAI Configuration

| Variable                                 | Required             | Description               |
| ---------------------------------------- | -------------------- | ------------------------- |
| `AZURE_OPENAI_API_KEY`                   | Yes                  | Azure OpenAI API key      |
| `AZURE_OPENAI_ENDPOINT`                  | Yes                  | Azure OpenAI endpoint URL |
| `AZURE_OPENAI_API_VERSION`               | `2024-02-15-preview` | API version               |
| `AZURE_OPENAI_DEPLOYMENT_NAME`           | -                    | Chat deployment name      |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME` | Yes                  | Embedding deployment name |
| `AZURE_OPENAI_CHAT_DEPLOYMENT_NAME`      | -                    | Chat deployment name      |

### Embedding Configuration

| Variable             | Default                                  | Description     |
| -------------------- | ---------------------------------------- | --------------- |
| `EMBEDDING_PROVIDER` | `sentence_transformers`                  | Provider to use |
| `EMBEDDING_MODEL`    | `sentence-transformers/all-MiniLM-L6-v2` | Model name      |

## 🗂️ Service URLs

After starting the system, access services at:

| Service         | URL                        | Description              |
| --------------- | -------------------------- | ------------------------ |
| Frontend        | http://localhost:3000      | React application        |
| Backend API     | http://localhost:8000      | FastAPI server           |
| API Docs        | http://localhost:8000/docs | Swagger documentation    |
| Neo4j Browser   | http://localhost:7474      | Graph database interface |
| Redis Commander | http://localhost:8081      | Redis management UI      |
| Flower          | http://localhost:5555      | Celery task monitoring   |

## 🛠️ Management Commands

### Using the Startup Script

```bash
# Start different configurations
./start.sh dev          # Development mode
./start.sh azure        # Azure OpenAI mode
./start.sh cloud        # Cloud mode

# Stop services
./start.sh stop

# View logs
./start.sh logs         # All services
./start.sh logs backend # Specific service

# Check status
./start.sh status

# Test Azure OpenAI
./start.sh test-azure
```

### Manual Docker Commands

```bash
# Start services
docker-compose -f docker-compose.development.yml up -d

# Stop services
docker-compose -f docker-compose.development.yml down

# View logs
docker-compose -f docker-compose.development.yml logs -f

# Execute commands in container
docker-compose -f docker-compose.development.yml exec backend bash
```

## 🔍 Troubleshooting

### Common Issues

1. **Port Conflicts**

   ```bash
   # Check what's using ports
   lsof -i :3000
   lsof -i :8000

   # Kill processes if needed
   kill -9 <PID>
   ```

2. **Azure OpenAI Connection Issues**

   ```bash
   # Test configuration
   ./start.sh test-azure

   # Check environment variables
   grep AZURE_OPENAI .env
   ```

3. **Service Health Issues**

   ```bash
   # Check all services
   ./start.sh status

   # Check specific service logs
   ./start.sh logs backend
   ```

4. **Permission Issues**
   ```bash
   # Fix file permissions
   sudo chown -R $USER:$USER ./uploads
   chmod +x start.sh
   ```

### Debug Mode

For detailed debugging, enable debug logging:

```bash
# In .env file
DEBUG=true
LOG_LEVEL=DEBUG

# Or set via environment
export DEBUG=true
export LOG_LEVEL=DEBUG
```

### Performance Tuning

For production deployments, adjust these settings:

```bash
# In .env file
MAX_CONCURRENT_JOBS=10
WORKER_CONCURRENCY=8
ENABLE_CACHING=true
CACHE_TTL_SECONDS=3600
```

## 🏗️ Architecture

### Service Dependencies

```
Frontend (Next.js — Vercel in prod, localhost:3000 in dev)
    ↓
Backend (FastAPI — DOKS/ArgoCD in prod, localhost:8000 in dev)
    ↓
┌─────────────────┬─────────────────┬─────────────────┐
│   PostgreSQL    │      Redis      │      Neo4j      │
│  (Supabase in   │ (DO Managed in  │                 │
│  prod; local    │ prod; local     │  Knowledge Graph│
│  container dev) │ container dev)  │                 │
└─────────────────┴─────────────────┴─────────────────┘
    ↓
Azure OpenAI (Optional)
```

> **Production storage**: DO Spaces (S3-compatible). MinIO container is dev-only.

### Network Configuration

All services communicate via the `multimodal-rag-network` Docker network:

- **Frontend**: Port 3000
- **Backend**: Port 8000
- **PostgreSQL**: Port 5432
- **Redis**: Port 6379
- **Neo4j**: Ports 7474 (HTTP), 7687 (Bolt)
- Note: Qdrant is removed; no Qdrant ports in use

## 🔐 Security Considerations

1. **Environment Variables**: Never commit `.env` files with real credentials
2. **Network Isolation**: Services communicate within isolated Docker network
3. **Resource Limits**: Configure memory and CPU limits for production
4. **HTTPS**: Use reverse proxy with SSL for production deployments

## 📦 Deployment Options

### Development

- Use `docker-compose.development.yml`
- Hot-reload enabled
- Debug logging
- Local volumes mounted

### Production (live)

> **Not Docker Compose.** Production runs on DOKS (DigitalOcean Kubernetes) managed by ArgoCD.
>
> - **Frontend**: Vercel
> - **Database**: Supabase managed Postgres
> - **Cache/Queue**: DO Managed Redis
> - **Object storage**: DO Spaces (`rag-system-storage`, nyc3, `STORAGE_BACKEND=s3`)
> - **Auth**: Supabase hosted GoTrue

### Local Simulation with Azure OpenAI

- Use `docker-compose.azure.yml`
- Useful for testing Azure OpenAI integration locally
- Uses local Postgres/Redis containers (not the production managed services)

## 🧪 Testing

### Test Azure OpenAI Integration

```bash
# Run comprehensive tests
./start.sh test-azure

# Or manually
cd backend
python test_azure_openai.py
```

### Test Health Endpoints

```bash
# Backend health
curl http://localhost:8000/health

# Service status
curl http://localhost:8000/api/health/services
```

## 📚 Additional Resources

- [Azure OpenAI Documentation](https://learn.microsoft.com/en-us/azure/cognitive-services/openai/)
- [Docker Compose Reference](https://docs.docker.com/compose/reference/)
- [DigitalOcean Spaces (S3-compatible)](https://docs.digitalocean.com/products/spaces/)
- [Supabase Auth](https://supabase.com/docs/guides/auth)

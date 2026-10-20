# RAG System - Project Structure

> **Stale / historical (2026-10-17):** It describes a design from an earlier infrastructure generation (standalone microservices, Qdrant, multiple environments) that is not what runs today. Only `dev` is live and services run inside the single FastAPI backend. Check the code before relying on any statement here. Current contracts: [`docs/engineering/`](../engineering/README.md).

**Multimodal Enterprise RAG System** - A comprehensive Retrieval-Augmented Generation platform for processing text, images, audio, and video with knowledge graph integration.

## Root Directory

```
RAG_system/
├── backend/                    # FastAPI backend application
├── frontend/                   # Next.js frontend application
├── config/                     # Configuration files
├── database/                   # Database schemas and migrations
├── docs/                       # Project documentation
├── infrastructure/             # Terraform & Kubernetes configs
├── k8s/                        # Kubernetes manifests
├── monitoring/                 # Prometheus, Grafana configs
├── scripts/                    # Utility scripts
├── tests/                      # Integration tests
├── notebooks/                  # Jupyter notebooks for analysis
├── docker-compose.*.yml        # Docker compose configurations
├── .env                        # Environment variables
└── README.md                   # Project readme
```

---

## Backend (`/backend`)

FastAPI-based backend with multimodal processing, knowledge graph, and hybrid search capabilities.

```
backend/
├── src/                        # Main application source
│   ├── main.py                 # FastAPI application entry point
│   ├── __init__.py             # Package initialization
│   │
│   ├── api/                    # REST API endpoints
│   │   ├── auth.py             # Authentication endpoints
│   │   ├── chat.py             # Chat/LLM interaction
│   │   ├── documents.py        # Document management
│   │   ├── search.py           # Search endpoints
│   │   ├── knowledge_graph.py  # Knowledge graph queries
│   │   ├── arxiv.py            # ArXiv paper integration
│   │   ├── websocket.py        # WebSocket connections
│   │   ├── websocket_v2.py     # Enhanced WebSocket API
│   │   ├── quality_metrics.py  # RAG quality metrics
│   │   └── analytics/          # Analytics endpoints
│   │
│   ├── services/               # Business logic layer
│   │   ├── azure_openai_service.py     # Azure OpenAI integration
│   │   ├── embedding_service.py        # Vector embeddings
│   │   ├── knowledge_graph_service.py  # Neo4j operations
│   │   ├── vector_search_service.py    # Qdrant vector search
│   │   ├── hybrid_search_service.py    # Combined search
│   │   ├── bm25_service.py             # BM25 keyword search
│   │   ├── cohere_rerank_service.py    # Cohere reranking
│   │   ├── arxiv_service.py            # ArXiv API integration
│   │   ├── entity_extraction_service.py # Entity extraction
│   │   ├── document_upload_service.py  # File upload handling
│   │   ├── websocket_manager.py        # WebSocket management
│   │   ├── agents/                     # AI agent services
│   │   ├── ingestion/                  # Document ingestion
│   │   ├── search/                     # Search services
│   │   └── analytics/                  # Analytics services
│   │
│   ├── core/                   # Core utilities
│   │   └── config.py           # Application configuration
│   │
│   ├── database/               # Database connections
│   │   ├── session.py          # SQLAlchemy session
│   │   └── models.py           # ORM models
│   │
│   ├── auth/                   # Authentication & authorization
│   │   ├── jwt_handler.py      # JWT token handling
│   │   └── dependencies.py     # Auth dependencies
│   │
│   ├── tasks/                  # Celery async tasks
│   │   ├── celery_app.py       # Celery configuration
│   │   └── processing_tasks.py # Document processing tasks
│   │
│   ├── websocket/              # WebSocket infrastructure
│   │   └── connection_manager.py
│   │
│   ├── evaluation/             # RAG evaluation (DeepEval)
│   │   └── deepeval_runner.py
│   │
│   ├── monitoring/             # Prometheus metrics
│   ├── observability/          # OpenTelemetry integration
│   ├── security/               # Security utilities
│   └── middleware/             # FastAPI middleware
│
├── tests/                      # Unit tests
├── migrations/                 # Database migrations
├── alembic/                    # Alembic migration configs
├── requirements.txt            # Python dependencies
├── Dockerfile                  # Production Dockerfile
├── Dockerfile.simple           # Development Dockerfile
└── Dockerfile.worker           # Celery worker Dockerfile
```

---

## Frontend (`/frontend`)

Next.js 14 application with App Router, TypeScript, and Tailwind CSS. Uses "Terminal Observatory" dark theme design.

```
frontend/
├── app/                        # Next.js App Router pages
│   ├── layout.tsx              # Root layout
│   ├── page.tsx                # Home page
│   ├── globals.css             # Global styles
│   ├── providers.tsx           # Context providers
│   │
│   ├── dashboard/              # Dashboard page
│   ├── chat/                   # Chat interface (LLM interaction)
│   ├── documents/              # Document management
│   ├── search/                 # Search interface
│   ├── arxiv/                  # ArXiv paper management
│   ├── entities/               # Knowledge graph entities
│   ├── analytics/              # Analytics dashboard
│   ├── login/                  # Login page
│   ├── register/               # Registration page
│   ├── api/                    # API routes (Next.js)
│   └── components/             # Page-specific components
│
├── src/                        # Source code
│   ├── components/             # Reusable React components
│   │   ├── layout/             # Layout components (Sidebar, Header)
│   │   ├── ui/                 # shadcn/ui components
│   │   ├── arxiv/              # ArXiv-specific components
│   │   ├── chat/               # Chat components
│   │   └── realtime/           # Real-time status components
│   │
│   ├── services/               # API client services
│   │   ├── api.ts              # Base API client
│   │   ├── auth.ts             # Authentication service
│   │   ├── documents.ts        # Document service
│   │   └── realtime-websocket-service.ts
│   │
│   ├── stores/                 # State management (Zustand)
│   │   ├── auth-store.ts       # Authentication state
│   │   └── realtime-store.ts   # Real-time state
│   │
│   ├── types/                  # TypeScript type definitions
│   │   ├── api.ts              # API types
│   │   ├── document.ts         # Document types
│   │   └── realtime-processing.ts
│   │
│   ├── hooks/                  # Custom React hooks
│   ├── contexts/               # React contexts
│   ├── utils/                  # Utility functions
│   └── styles/                 # Additional styles
│
├── lib/                        # Library utilities
├── public/                     # Static assets
├── e2e/                        # Playwright E2E tests
├── tests/                      # Unit/integration tests
│
├── next.config.js              # Next.js configuration
├── tailwind.config.ts          # Tailwind CSS configuration
├── tsconfig.json               # TypeScript configuration
├── package.json                # NPM dependencies
├── Dockerfile                  # Development Dockerfile
└── Dockerfile.production       # Production Dockerfile
```

---

## Configuration (`/config`)

```
config/
├── docker-compose/
│   └── docker-compose.development.yml  # Development compose
│
└── security/                   # Security configurations
```

---

## Documentation (`/docs`)

```
docs/
├── README.md                   # Documentation index
│
├── architecture/               # System architecture
│   └── FRONTEND_ARCHITECTURE_REPORT.md
│
├── guides/                     # Setup & usage guides
│   ├── CLAUDE_CODE_SHADCN_SETUP.md
│   ├── CURSOR_SHADCN_SETUP.md
│   ├── GPT4o_MINI_SETUP_COMPLETE.md
│   └── download_and_extract_guide.md
│
├── database/                   # Database documentation
├── security/                   # Security documentation
├── testing/                    # Testing guides
├── operations/                 # Operations & runbooks
├── deployment/                 # Deployment guides
├── api/                        # API documentation
├── monitoring/                 # Monitoring setup
├── fixes/                      # Bug fix documentation
└── reports/                    # Generated reports
```

---

## Infrastructure

### Docker Compose Files

```
docker-compose.development.yml  # Development environment (symlink to config/)
docker-compose.production.yml   # Production environment
docker-compose.azure.yml        # Azure-specific configuration
```

### Kubernetes (`/k8s`)

```
k8s/
├── deployments/                # Kubernetes deployments
├── services/                   # Kubernetes services
├── configmaps/                 # ConfigMaps
└── secrets/                    # Secret templates
```

### Monitoring (`/monitoring`)

```
monitoring/
├── prometheus/                 # Prometheus configuration
├── grafana/                    # Grafana dashboards
└── alertmanager/               # Alert configurations
```

---

## Database Schema

### PostgreSQL
- `users` - User accounts
- `documents` - Document metadata
- `document_chunks` - Document text chunks
- `conversations` - Chat conversations
- `messages` - Chat messages

### Neo4j (Knowledge Graph)
- Entity nodes: `Concept`, `Person`, `Organization`, `Technology`, `Paper`
- Relationship types: `RELATES_TO`, `CITES`, `AUTHORED_BY`, `BELONGS_TO`

### Qdrant (Vector Store)
- Collections for document embeddings
- Semantic similarity search

### Redis
- Session caching
- Rate limiting
- Celery task queue

---

## Key Services

| Service | Port | Description |
|---------|------|-------------|
| Backend API | 8000 | FastAPI REST API |
| Frontend | 3000 | Next.js web application |
| PostgreSQL | 5432 | Relational database |
| Neo4j | 7474/7687 | Knowledge graph database |
| Qdrant | 6333/6334 | Vector database |
| Redis | 6379 | Caching & queues |
| Flower | 5555 | Celery monitoring |
| Adminer | 8080 | Database admin UI |
| MinIO | 9000/9001 | S3-compatible storage |

---

## Theme Constants (Terminal Observatory)

```typescript
// Color palette used throughout the frontend
PHOSPHOR_GREEN = '#00ff9f'    // Primary accent
AMBER = '#ffb700'             // Warning/highlight
CYAN = '#00d4ff'              // Info/secondary
BACKGROUND = '#0a0a0f'        // Dark background
```

---

## Quick Start Commands

```bash
# Start all services
docker-compose -f docker-compose.development.yml up -d

# Start backend only
docker-compose -f docker-compose.development.yml up backend

# Start frontend (local development)
pnpm --dir frontend dev:offline

# Run tests
pytest tests/ --cov=src

# View logs
docker-compose -f docker-compose.development.yml logs -f backend
```

---

## Environment Variables

Key environment variables (see `.env.example` for full list):

```bash
# Database
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/multimodal_rag_dev
REDIS_URL=redis://localhost:6379
NEO4J_URI=bolt://localhost:7687

# Azure OpenAI
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_ENDPOINT=https://your-endpoint.cognitiveservices.azure.com/
AZURE_OPENAI_CHAT_DEPLOYMENT_NAME=gpt-4o-mini
AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME=text-embedding-3-small

# Cohere Reranking
COHERE_RERANK_ENDPOINT=your-endpoint
COHERE_RERANK_API_KEY=your-key

# Application
SECRET_KEY=your-secret-key
ENVIRONMENT=development
```

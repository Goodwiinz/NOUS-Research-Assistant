# Knowledge Graph System Startup Guide

> **Stale / historical (2026-10-17):** Qdrant is fully removed and live retrieval is PostgreSQL full-text search (`DO_KB_ENABLED` defaults to `False`; pgvector semantic search is flag-gated). Treat Qdrant, DO KB and vector-store statements below as historical. Current contracts: [`docs/engineering/`](../engineering/README.md).

## Overview

This guide provides comprehensive instructions for starting the corrected Knowledge Graph Architecture for the Multimodal Enterprise RAG System. The system consists of three microservices that handle entity extraction, graph analytics, and visualization computations in the backend, resolving the architectural violation where graph algorithms were incorrectly placed in the frontend.

## Architecture Overview

### Microservices Architecture
- **Knowledge Graph Service (Port 8003)**: Entity extraction, CRUD operations, and graph data management
- **Graph Analytics Service (Port 8009)**: All graph algorithms (centrality, community detection, pathfinding)
- **Graph Visualization Service (Port 8010)**: Data preparation and layout computation for visualization

### Database Stack
- **PostgreSQL (Port 5433)**: Metadata, analytics, and caching with corrected schema
- **Neo4j (Port 7687)**: Graph database with GDS library for graph algorithms
- **Qdrant (Port 6333)**: Vector embeddings for semantic similarity
- **Redis (Port 6380)**: Caching and real-time updates

## Prerequisites

### System Requirements
- **RAM**: Minimum 8GB, Recommended 16GB+
- **CPU**: Minimum 4 cores, Recommended 8+ cores
- **Storage**: Minimum 50GB free space
- **Docker**: Version 20.10+
- **Docker Compose**: Version 2.0+

### Required Software
- Docker and Docker Compose
- Git
- curl (for API testing)
- jq (for JSON processing)

## Quick Start

### 1. Environment Setup

```bash
# Clone the repository (if not already done)
git clone <repository-url>
cd rag

# Set working directory
cd /Users/goodwiinz/development/RAG_system/rag
```

### 2. Start All Services

```bash
# Start graph services with corrected architecture
./scripts/deploy-graph-services.sh deploy
```

This command will:
- Check prerequisites
- Set up environment variables
- Build and deploy all services
- Wait for services to be healthy
- Run comprehensive health checks
- Display deployment information

### 3. Initialize Databases

After services are running, initialize the databases:

```bash
# Initialize PostgreSQL with corrected schema
docker-compose -f docker-compose.graph-services-corrected.yml exec -T postgres-graph psql -U rag_user -d rag_graph -f /docker-entrypoint-initdb.d/01-schema.sql

# Initialize Neo4j with graph structure
docker-compose -f docker-compose.graph-services-corrected.yml exec -T neo4j-graph cypher-shell -u neo4j -p ragpassword2024 -f /docker-entrypoint-initdb.d/init.cypher

# Initialize Qdrant collections
./scripts/init-qdrant-collections.sh init

# Initialize Redis caching
./scripts/init-redis-graph.sh init
```

### 4. Verify Installation

```bash
# Run comprehensive health checks
./scripts/deploy-graph-services.sh health

# Test API endpoints
curl http://localhost:8003/health
curl http://localhost:8009/health
curl http://localhost:8010/health
```

## Detailed Startup Instructions

### Step 1: Environment Configuration

#### Create Environment File
```bash
# Create .env file if it doesn't exist
cat > .env << EOF
# Environment Variables for Graph Services
JWT_SECRET_KEY=your-super-secret-jwt-key-change-in-production-$(date +%s)
DEBUG=false
LOG_LEVEL=INFO

# Flower credentials (optional)
FLOWER_USER=admin
FLOWER_PASSWORD=admin-$(date +%s)

# Database credentials
NEO4J_PASSWORD=ragpassword2024
POSTGRES_PASSWORD=rag_password2024

# Performance settings
MAX_GRAPH_SIZE_FOR_CENTRALITY=10000
MAX_GRAPH_SIZE_FOR_COMMUNITIES=50000
MAX_CONCURRENT_JOBS=10
CACHE_TTL_SECONDS=3600
EOF
```

#### Create Required Directories
```bash
mkdir -p logs
mkdir -p nginx
mkdir -p monitoring
mkdir -p analytics-schedule
```

### Step 2: Database Initialization

#### PostgreSQL Setup
The corrected PostgreSQL schema includes:

**Core Tables:**
- `graph_computation_cache`: Algorithm result caching
- `graph_analytics_snapshots`: Real-time graph metrics
- `user_graph_preferences`: User visualization preferences
- `graph_computation_jobs`: Background job tracking
- `entity_analytics_cache`: Pre-computed entity metrics
- `graph_insights`: AI-generated graph insights
- `graph_performance_metrics`: Performance monitoring

**Enhanced Tables:**
- `entities`: Added graph analytics columns
- `entity_relationships`: Added graph-specific properties

Run the initialization:
```bash
# Apply migrations and create schema
psql -h localhost -p 5433 -U rag_user -d rag_graph -f database/init_knowledge_graph_db.sql
```

#### Neo4j Setup
Neo4j includes:

**Constraints & Indexes:**
- Entity and document uniqueness constraints
- Performance indexes for common queries
- Full-text search indexes
- Composite indexes for query patterns

**Graph Projections:**
- Entity graph for centrality analysis
- Document graph for similarity analysis
- Relationship projections for pathfinding

Run the initialization:
```bash
# Apply Cypher initialization
cat database/neo4j-init.cypher | docker-compose -f docker-compose.graph-services-corrected.yml exec -T neo4j-graph cypher-shell -u neo4j -p ragpassword2024
```

#### Qdrant Vector Store Setup
Qdrant collections include:

**Collections:**
- `entity_embeddings`: 1536-dimensional entity vectors
- `relationship_embeddings`: 768-dimensional relationship vectors
- `graph_context_embeddings`: 1024-dimensional context vectors
- `entity_semantic_embeddings`: 2048-dimensional semantic vectors

Run the initialization:
```bash
# Initialize Qdrant collections
./scripts/init-qdrant-collections.sh init --url http://localhost:6333
```

#### Redis Caching Setup
Redis includes:

**Cache Patterns:**
- Entity centrality scores: `entity:centrality:{entity_id}:{metric_type}`
- Graph layouts: `graph:layout:{graph_id}:{algorithm}`
- Shortest paths: `paths:shortest:{source_id}:{target_id}`
- Community results: `communities:{organization_id}:{algorithm}`
- Real-time statistics: `stats:graph:{organization_id}:realtime`
- User views: `view:graph:{user_id}:{view_hash}`
- Job status: `job:graph:{job_id}`

Run the initialization:
```bash
# Initialize Redis caching
./scripts/init-redis-graph.sh init --host localhost --port 6380
```

### Step 3: Service Startup

#### Deploy Graph Services
```bash
# Deploy all services with health checks
./scripts/deploy-graph-services.sh deploy
```

Expected output:
```
[2024-01-XX XX:XX:XX] Starting deployment of corrected Knowledge Graph Services Architecture...
[SUCCESS] Prerequisites check passed
[SUCCESS] Environment setup completed
[SUCCESS] Services deployed successfully
[SUCCESS] All services are healthy!
[SUCCESS] All health checks passed!
```

#### Service Endpoints
After deployment, you should have access to:

**API Services:**
- Knowledge Graph Service: http://localhost:8003
- Graph Analytics Service: http://localhost:8009
- Graph Visualization Service: http://localhost:8010

**API Documentation:**
- Knowledge Graph: http://localhost:8003/docs
- Graph Analytics: http://localhost:8009/docs
- Graph Visualization: http://localhost:8010/docs

**Databases:**
- Neo4j Browser: http://localhost:7474 (neo4j/ragpassword2024)
- PostgreSQL: localhost:5433 (rag_user/rag_password2024)
- Qdrant: http://localhost:6333
- Redis: localhost:6380

### Step 4: Verification and Testing

#### Health Checks
```bash
# Check all services
./scripts/deploy-graph-services.sh health

# Check individual services
curl -f http://localhost:8003/health && echo "✅ Knowledge Graph Service healthy"
curl -f http://localhost:8009/health && echo "✅ Graph Analytics Service healthy"
curl -f http://localhost:8010/health && echo "✅ Graph Visualization Service healthy"
```

#### Database Connectivity
```bash
# Test PostgreSQL
docker-compose -f docker-compose.graph-services-corrected.yml exec postgres-graph psql -U rag_user -d rag_graph -c "SELECT version();"

# Test Neo4j
docker-compose -f docker-compose.graph-services-corrected.yml exec neo4j-graph cypher-shell -u neo4j -p ragpassword2024 "RETURN 1;"

# Test Redis
docker-compose -f docker-compose.graph-services-corrected.yml exec redis-graph redis-cli ping

# Test Qdrant
curl -f http://localhost:6333/health
```

#### Integration Testing
```bash
# Run integration tests (if available)
./scripts/test-integration.sh

# Test basic graph operations
curl -X POST http://localhost:8003/api/entities \
  -H "Content-Type: application/json" \
  -d '{"name": "Test Entity", "type": "Person", "organization_id": "test-org"}'
```

## Service Management

### Monitoring Services

#### View Logs
```bash
# View all service logs
./scripts/deploy-graph-services.sh logs

# View specific service logs
./scripts/deploy-graph-services.sh logs knowledge-graph
./scripts/deploy-graph-services.sh logs graph-analytics
./scripts/deploy-graph-services.sh logs graph-visualization
```

#### Check Service Status
```bash
# Check status of all services
./scripts/deploy-graph-services.sh status

# Check Docker container status
docker-compose -f docker-compose.graph-services-corrected.yml ps
```

#### Resource Monitoring
```bash
# Monitor resource usage
docker stats

# Monitor specific services
docker stats rag-knowledge-graph-service rag-graph-analytics-service rag-graph-visualization-service
```

### Maintenance Operations

#### Restart Services
```bash
# Restart all services
./scripts/deploy-graph-services.sh restart

# Restart specific service
docker-compose -f docker-compose.graph-services-corrected.yml restart knowledge-graph-service
```

#### Stop Services
```bash
# Stop all services
./scripts/deploy-graph-services.sh stop

# Stop and remove volumes
./scripts/deploy-graph-services.sh cleanup
```

#### Database Maintenance
```bash
# Clean up expired cache entries
psql -h localhost -p 5433 -U rag_user -d rag_graph -c "SELECT cleanup_expired_cache();"

# Update materialized views
psql -h localhost -p 5433 -U rag_user -d rag_graph -c "SELECT refresh_entity_importance_summary();"

# Optimize Neo4j
docker-compose -f docker-compose.graph-services-corrected.yml exec neo4j-graph cypher-shell -u neo4j -p ragpassword2024 "CALL db.stats.retrieve('GRAPH COUNTS');"

# Clean Redis expired keys
docker-compose -f docker-compose.graph-services-corrected.yml exec redis-graph redis-cli --scan --pattern "*:expires:*" | xargs docker-compose -f docker-compose.graph-services-corrected.yml exec -T redis-graph redis-cli del
```

## Troubleshooting

### Common Issues

#### Services Not Starting
```bash
# Check Docker and Docker Compose
docker --version
docker-compose --version

# Check port conflicts
netstat -an | grep :8003
netstat -an | grep :8009
netstat -an | grep :8010

# Check logs for errors
./scripts/deploy-graph-services.sh logs
```

#### Database Connection Issues
```bash
# Check database health
./scripts/deploy-graph-services.sh health

# Test individual databases
docker-compose -f docker-compose.graph-services-corrected.yml exec postgres-graph pg_isready -U rag_user
docker-compose -f docker-compose.graph-services-corrected.yml exec neo4j-graph cypher-shell -u neo4j -p ragpassword2024 "RETURN 1;"
docker-compose -f docker-compose.graph-services-corrected.yml exec redis-graph redis-cli ping
curl -f http://localhost:6333/health
```

#### Memory Issues
```bash
# Check memory usage
free -h
docker stats

# Check Redis memory usage
docker-compose -f docker-compose.graph-services-corrected.yml exec redis-graph redis-cli info memory

# Check PostgreSQL memory
docker-compose -f docker-compose.graph-services-corrected.yml exec postgres-graph psql -U rag_user -d rag_graph -c "SELECT pg_size_pretty(pg_database_size('rag_graph'));"
```

#### Performance Issues
```bash
# Check slow queries in PostgreSQL
docker-compose -f docker-compose.graph-services-corrected.yml exec postgres-graph psql -U rag_user -d rag_graph -c "SELECT query, mean_time, calls FROM pg_stat_statements ORDER BY mean_time DESC LIMIT 10;"

# Check Redis slow log
docker-compose -f docker-compose.graph-services-corrected.yml exec redis-graph redis-cli slowlog get 10

# Check Neo4j query performance
docker-compose -f docker-compose.graph-services-corrected.yml exec neo4j-graph cypher-shell -u neo4j -p ragpassword2024 "CALL dbms.listQueries();"
```

### Error Resolution

#### Migration Errors
```bash
# Check PostgreSQL migration status
psql -h localhost -p 5433 -U rag_user -d rag_graph -c "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1;"

# Re-run migrations if needed
psql -h localhost -p 5433 -U rag_user -d rag_graph -f database/005_correct_knowledge_graph_architecture.sql
```

#### Neo4j Constraint Errors
```bash
# Check Neo4j constraints
docker-compose -f docker-compose.graph-services-corrected.yml exec neo4j-graph cypher-shell -u neo4j -p ragpassword2024 "SHOW CONSTRAINTS;"

# Recreate constraints if needed
docker-compose -f docker-compose.graph-services-corrected.yml exec neo4j-graph cypher-shell -u neo4j -p ragpassword2024 -f database/neo4j-init.cypher
```

#### Qdrant Collection Errors
```bash
# Recreate Qdrant collections
./scripts/init-qdrant-collections.sh cleanup
./scripts/init-qdrant-collections.sh init

# Check collection status
curl -s http://localhost:6333/collections | jq
```

## Development Workflow

### Local Development Setup

#### 1. Start Services
```bash
# Start in development mode
DEBUG=true LOG_LEVEL=DEBUG ./scripts/deploy-graph-services.sh deploy
```

#### 2. Run Tests
```bash
# Run integration tests
./scripts/test-integration.sh

# Run performance tests
./scripts/test-performance.sh
```

#### 3. API Development
```bash
# Test API endpoints
curl -X GET http://localhost:8003/api/entities
curl -X GET http://localhost:8009/api/analytics/centrality
curl -X GET http://localhost:8010/api/visualization/layout
```

### Production Deployment

#### 1. Environment Configuration
```bash
# Production environment file
cat > .env.production << EOF
# Production Environment Variables
JWT_SECRET_KEY=your-production-secret-key
DEBUG=false
LOG_LEVEL=WARNING

# Database credentials (use strong passwords)
NEO4J_PASSWORD=your-neo4j-password
POSTGRES_PASSWORD=your-postgres-password

# Performance tuning
MAX_GRAPH_SIZE_FOR_CENTRALITY=50000
MAX_GRAPH_SIZE_FOR_COMMUNITIES=100000
MAX_CONCURRENT_JOBS=20
CACHE_TTL_SECONDS=7200
EOF
```

#### 2. Production Deployment
```bash
# Deploy with production configuration
env_file=.env.production ./scripts/deploy-graph-services.sh deploy

# Enable monitoring profile
docker-compose -f docker-compose.graph-services-corrected.yml --profile monitoring up -d
```

#### 3. Production Monitoring
```bash
# Access monitoring tools
# Prometheus: http://localhost:9091
# Flower (Celery monitoring): http://localhost:5555
```

## API Reference

### Knowledge Graph Service (Port 8003)

#### Entity Management
```bash
# Create entity
POST /api/entities
{
  "name": "Entity Name",
  "type": "Person",
  "organization_id": "org-id",
  "confidence": 0.9
}

# Get entity
GET /api/entities/{entity_id}

# Update entity
PUT /api/entities/{entity_id}

# Delete entity
DELETE /api/entities/{entity_id}

# Search entities
GET /api/entities/search?q=query&type=Person&limit=10
```

#### Relationship Management
```bash
# Create relationship
POST /api/relationships
{
  "source_entity_id": "source-id",
  "target_entity_id": "target-id",
  "relationship_type": "RELATED_TO",
  "confidence": 0.8
}

# Get relationships
GET /api/relationships?entity_id={entity_id}
```

### Graph Analytics Service (Port 8009)

#### Centrality Analysis
```bash
# Compute centrality
POST /api/analytics/centrality
{
  "entity_ids": ["id1", "id2"],
  "metrics": ["degree", "betweenness", "pagerank"],
  "organization_id": "org-id"
}

# Get cached centrality
GET /api/analytics/centrality/{entity_id}
```

#### Community Detection
```bash
# Run community detection
POST /api/analytics/communities
{
  "algorithm": "louvain",
  "organization_id": "org-id",
  "parameters": {"resolution": 1.0}
}

# Get communities
GET /api/analytics/communities/{community_id}
```

#### Path Finding
```bash
# Find shortest path
POST /api/analytics/paths
{
  "source_entity_id": "source-id",
  "target_entity_id": "target-id",
  "algorithm": "dijkstra",
  "max_depth": 5
}
```

### Graph Visualization Service (Port 8010)

#### Layout Computation
```bash
# Compute graph layout
POST /api/visualization/layout
{
  "entity_ids": ["id1", "id2"],
  "algorithm": "force",
  "parameters": {"iterations": 100},
  "organization_id": "org-id"
}

# Get cached layout
GET /api/visualization/layout/{graph_id}
```

#### Graph Data Export
```bash
# Export graph for visualization
GET /api/visualization/export?organization_id=org-id&format=json
```

## Performance Optimization

### Database Optimization

#### PostgreSQL
```sql
-- Update statistics
ANALYZE;

-- Reindex tables
REINDEX DATABASE rag_graph;

-- Check query performance
EXPLAIN ANALYZE SELECT * FROM entities WHERE organization_id = 'org-id';
```

#### Neo4j
```cypher
// Update statistics
CALL db.stats.retrieve('GRAPH COUNTS');

// Warm up caches
CALL apoc.warmup.run();

// Create graph projections for algorithms
CALL gds.graph.project('entityGraph', 'Entity', {
  MENTIONS: {orientation: 'UNDIRECTED'},
  RELATES_TO: {orientation: 'UNDIRECTED'}
});
```

#### Redis
```bash
# Monitor memory usage
redis-cli info memory

# Optimize memory settings
redis-cli config set maxmemory-policy "allkeys-lru"

# Monitor slow operations
redis-cli slowlog get 10
```

### Application Optimization

#### Caching Strategy
```bash
# Configure cache TTL settings
export CACHE_TTL_CENTRALITY=3600
export CACHE_TTL_LAYOUT=1800
export CACHE_TTL_COMMUNITIES=86400
```

#### Concurrent Processing
```bash
# Configure worker pools
export MAX_CONCURRENT_JOBS=20
export CELERY_WORKER_CONCURRENCY=8
```

## Security Considerations

### Authentication
- JWT-based authentication for all services
- Organization-based multi-tenancy
- Row-level security in PostgreSQL

### Network Security
- Services communicate within Docker network
- External access through reverse proxy (nginx)
- SSL/TLS encryption in production

### Data Protection
- Sensitive data encrypted at rest
- API rate limiting
- Input validation and sanitization

## Backup and Recovery

### Database Backups

#### PostgreSQL
```bash
# Create backup
docker-compose -f docker-compose.graph-services-corrected.yml exec postgres-graph pg_dump -U rag_user rag_graph > backup_$(date +%Y%m%d).sql

# Restore backup
docker-compose -f docker-compose.graph-services-corrected.yml exec -T postgres-graph psql -U rag_user rag_graph < backup_20240101.sql
```

#### Neo4j
```bash
# Create backup
docker-compose -f docker-compose.graph-services-corrected.yml exec neo4j-graph neo4j-admin database backup --database=neo4j --to-path=/backup

# Restore backup
docker-compose -f docker-compose.graph-services-corrected.yml exec neo4j-graph neo4j-admin database restore --from-path=/backup --database=neo4j
```

#### Redis
```bash
# Create backup
docker-compose -f docker-compose.graph-services-corrected.yml exec redis-graph redis-cli BGSAVE

# Copy backup file
docker cp rag-redis-graph:/data/dump.rdb ./redis_backup_$(date +%Y%m%d).rdb
```

### Disaster Recovery

#### Service Recovery
```bash
# Full system recovery
./scripts/deploy-graph-services.sh cleanup
./scripts/deploy-graph-services.sh deploy

# Restore databases from backups
# (Follow backup procedures above)
```

## Monitoring and Alerting

### Health Monitoring

#### Service Health
```bash
# Continuous health monitoring
watch -n 30 './scripts/deploy-graph-services.sh health'
```

#### Database Health
```bash
# PostgreSQL health
docker-compose -f docker-compose.graph-services-corrected.yml exec postgres-graph psql -U rag_user -d rag_graph -c "SELECT * FROM get_database_health_stats();"

# Neo4j health
curl -s http://localhost:7474/db/manage/server/jmx/domain/org.neo4j
```

### Performance Monitoring

#### Metrics Collection
```bash
# Application metrics
curl -s http://localhost:8009/metrics
curl -s http://localhost:8010/metrics

# Database metrics
curl -s http://localhost:9091/api/v1/query?query=pg_stat_database
```

#### Alerting Setup
Configure Prometheus alerting rules for:
- Service downtime
- High memory usage
- Slow query performance
- Cache hit ratio thresholds

## Support and Maintenance

### Regular Maintenance Tasks

#### Daily
- Check service health
- Monitor resource usage
- Review error logs

#### Weekly
- Clean up expired cache entries
- Update statistics
- Review performance metrics

#### Monthly
- Apply security updates
- Optimize database indexes
- Review backup procedures

### Getting Help

#### Documentation
- API documentation: http://localhost:{port}/docs
- System architecture: `/docs/architecture/`
- Database schema: `/docs/database/`

#### Troubleshooting
1. Check service logs: `./scripts/deploy-graph-services.sh logs`
2. Run health checks: `./scripts/deploy-graph-services.sh health`
3. Review this guide
4. Check system resources

#### Support Contact
- For technical issues: Create GitHub issue
- For security issues: Follow responsible disclosure policy

---

## Summary

This guide provides complete instructions for starting and managing the corrected Knowledge Graph architecture. The system is now properly designed with backend-first processing, resolving the architectural violation where graph algorithms were incorrectly placed in the frontend.

**Key Features:**
- ✅ Three microservices with clear responsibilities
- ✅ Comprehensive database stack (PostgreSQL, Neo4j, Qdrant, Redis)
- ✅ Automated deployment and health monitoring
- ✅ Performance optimization and caching
- ✅ Security and multi-tenancy support
- ✅ Comprehensive testing and validation

The system is now ready for production use with proper separation of concerns and scalable architecture.
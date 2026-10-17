# Knowledge Graph Quick Start Guide

> **Stale / historical (2026-10-17):** Qdrant is fully removed and live retrieval is PostgreSQL full-text search (`DO_KB_ENABLED` defaults to `False`; pgvector semantic search is flag-gated). Treat Qdrant, DO KB and vector-store statements below as historical. Current contracts: [`docs/engineering/`](../engineering/README.md).

## TL;DR

Your RAG system now automatically extracts entities from uploaded documents and stores them in Neo4j! 🎉

## For New Documents

**Nothing to do!** Just upload files via the frontend and entities will be automatically extracted.

## For Existing Documents

Run the backfill script to process documents already in your database:

```bash
# See what would be processed (dry run)
docker exec rag-backend-1 python /app/examples/backfill_knowledge_graph.py --dry-run

# Process all existing documents
docker exec rag-backend-1 python /app/examples/backfill_knowledge_graph.py

# Process first 10 documents as a test
docker exec rag-backend-1 python /app/examples/backfill_knowledge_graph.py --limit 10
```

## View Your Knowledge Graph

1. Open Neo4j Browser: http://localhost:7474
2. Login: `neo4j` / `neo4jpassword`
3. Run a query:

```cypher
// See all entities
MATCH (n:Entity) RETURN n LIMIT 25

// Count entities by type
MATCH (e:Entity)
RETURN e.type, count(e) as count
ORDER BY count DESC

// Find entities from your documents
MATCH (e:Entity)
WHERE e.source_document_id IS NOT NULL
RETURN e.name, e.type, e.metadata.document_title
LIMIT 50
```

## What Gets Extracted

From a document like:
> "Dr. Jane Smith works at TechCorp in San Francisco..."

You get:
- **Dr. Jane Smith** (PERSON)
- **TechCorp** (ORGANIZATION)
- **San Francisco** (LOCATION)

All automatically linked to the source document!

## Command Cheatsheet

```bash
# Backfill existing documents
docker exec rag-backend-1 python /app/examples/backfill_knowledge_graph.py

# Test with sample data
docker exec rag-backend-1 python /app/examples/store_knowledge_graph_data.py

# Verify entities in Neo4j
curl -u neo4j:neo4jpassword http://localhost:7474/db/neo4j/tx/commit \
  -H "Content-Type: application/json" \
  -d '{"statements":[{"statement":"MATCH (e:Entity) RETURN count(e)"}]}'
```

## API Endpoints

```bash
# Search entities
GET /knowledge-graph/entities/search?query=TechCorp

# Get entity details
GET /knowledge-graph/entities/{entity_id}

# Find related entities
GET /knowledge-graph/entities/{entity_id}/related?max_depth=2
```

## Troubleshooting

**No entities appearing?**
1. Check backend is running: `docker ps | grep backend`
2. Check Neo4j is running: `docker ps | grep neo4j`
3. View backend logs: `docker logs rag-backend-1 --tail 50`
4. Check document has text content

**Need help?**
- See [Full Documentation](./AUTO_KNOWLEDGE_GRAPH_INTEGRATION.md)
- Check [Knowledge Graph Guide](./KNOWLEDGE_GRAPH_GUIDE.md)

## That's It!

Your knowledge graph is ready to use. Upload documents and watch the graph grow! 📈

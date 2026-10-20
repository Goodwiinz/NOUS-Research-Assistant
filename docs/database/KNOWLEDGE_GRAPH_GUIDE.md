# Knowledge Graph Storage Guide

> **Stale / historical (2026-10-17):** Qdrant is fully removed and live retrieval is PostgreSQL full-text search (`DO_KB_ENABLED` defaults to `False`; pgvector semantic search is flag-gated). Treat Qdrant, DO KB and vector-store statements below as historical. Current contracts: [`docs/engineering/`](../engineering/README.md).

This guide explains how to store and query knowledge graph data in your RAG system using Neo4j.

## Table of Contents
- [Overview](#overview)
- [Data Models](#data-models)
- [Storage Methods](#storage-methods)
- [API Endpoints](#api-endpoints)
- [Example Usage](#example-usage)
- [Querying the Graph](#querying-the-graph)
- [Best Practices](#best-practices)

## Overview

The knowledge graph system stores entities (nodes) and relationships (edges) extracted from documents. This enables:
- Semantic search and discovery
- Relationship-based retrieval
- Entity recognition and linking
- Knowledge discovery and inference

### Architecture
- **Database**: Neo4j 5.15 Community Edition
- **Connection**: Bolt protocol on port 7687
- **Browser UI**: http://localhost:7474
- **Credentials**: neo4j / neo4jpassword

## Data Models

### Entity Types

Entities are the nodes in your knowledge graph. Available types:

| Type | Description | Example |
|------|-------------|---------|
| `PERSON` | Individual people | "Alice Johnson", "John Doe" |
| `ORGANIZATION` | Companies, institutions | "AI Research Lab", "MIT" |
| `LOCATION` | Places, addresses | "San Francisco", "New York" |
| `PRODUCT` | Products, services | "iPhone", "AWS Lambda" |
| `EVENT` | Events, occurrences | "Conference 2024", "Product Launch" |
| `DATE` | Dates, time references | "2024-01-15", "Q4 2023" |
| `FINANCIAL` | Financial terms | "$1M", "Q4 Revenue" |
| `EMAIL` | Email addresses | "user@example.com" |
| `PHONE` | Phone numbers | "+1-555-0123" |
| `URL` | Web addresses | "https://example.com" |
| `JOB_TITLE` | Job roles | "CEO", "Data Scientist" |
| `CONCEPT` | Abstract concepts | "Machine Learning", "Cloud Computing" |
| `OTHER` | Other types | Any custom entity type |

### Relationship Types

Relationships connect entities. Available types:

| Type | Description | Example |
|------|-------------|---------|
| `WORKS_FOR` | Employment relationship | Person → Organization |
| `KNOWS` | Social connection | Person → Person |
| `RELATED_TO` | Generic relation | Entity → Entity |
| `LOCATED_IN` | Physical location | Organization → Location |
| `PART_OF` | Hierarchical relation | Department → Organization |
| `MENTIONED_IN` | Document reference | Entity → Document |
| `APPEARS_WITH` | Co-occurrence | Entity → Entity |
| `CREATED_BY` | Authorship | Product → Person |
| `OWNS` | Ownership | Person → Product |
| `MANAGES` | Management | Person → Organization |
| `COLLABORATES_WITH` | Partnership | Organization → Organization |
| `REPORTS_TO` | Reporting structure | Person → Person |
| `MEMBER_OF` | Membership | Person → Organization |
| `ATTENDED` | Event participation | Person → Event |
| `SPOKE_AT` | Speaking engagement | Person → Event |
| `PUBLISHED_BY` | Publishing | Document → Organization |
| `CITED` | Citation | Document → Document |
| `REFERENCES` | Reference | Document → Entity |
| `CUSTOM` | Custom relationship | Any custom type |

### Extraction Methods

Track how entities were identified:

- `SPACY_NER`: Named Entity Recognition using spaCy
- `PATTERN_MATCHING`: Regular expression patterns
- `MANUAL`: Manually created
- `LLM_EXTRACTION`: Extracted using LLM
- `RULE_BASED`: Rule-based extraction
- `HYBRID`: Combination of methods

## Storage Methods

### Method 1: Using the API (Recommended for Frontend)

```python
import requests

BASE_URL = "http://localhost:8000"
headers = {
    "Authorization": f"Bearer {your_token}",
    "Content-Type": "application/json"
}

# Create an entity
entity_data = {
    "name": "John Doe",
    "entity_type": "PERSON",
    "confidence_score": 0.95,
    "extraction_method": "manual",
    "metadata": {
        "title": "Software Engineer",
        "company": "Tech Corp"
    }
}

response = requests.post(
    f"{BASE_URL}/knowledge-graph/entities",
    json=entity_data,
    headers=headers
)
entity = response.json()
```

### Method 2: Using KnowledgeGraphService (Backend)

```python
from src.services.knowledge_graph_service import knowledge_graph_service
from src.models.graph import (
    CreateEntityRequest, CreateRelationshipRequest,
    EntityType, RelationshipType, ExtractionMethod
)

# Create an entity
entity_request = CreateEntityRequest(
    name="Alice Johnson",
    entity_type=EntityType.PERSON,
    confidence_score=0.95,
    extraction_method=ExtractionMethod.MANUAL,
    metadata={
        "title": "Data Scientist",
        "email": "alice@example.com"
    }
)
entity = knowledge_graph_service.create_entity(entity_request)

# Create a relationship
relationship_request = CreateRelationshipRequest(
    source_entity_id=entity1_id,
    target_entity_id=entity2_id,
    relationship_type=RelationshipType.WORKS_FOR,
    strength=1.0,
    confidence_score=0.95,
    context="Alice works at AI Research Lab",
    evidence=["Employment record", "LinkedIn profile"],
    metadata={"start_date": "2020-01-15"}
)
relationship = knowledge_graph_service.create_relationship(relationship_request)
```

### Method 3: Batch Operations

```python
# Batch create entities
entities_data = [
    {
        "name": "Bob Smith",
        "entity_type": "PERSON",
        "confidence_score": 0.92,
        "extraction_method": "spacy_ner",
        "metadata": {"role": "Manager"}
    },
    {
        "name": "TechStart Inc",
        "entity_type": "ORGANIZATION",
        "confidence_score": 0.94,
        "extraction_method": "spacy_ner",
        "metadata": {"industry": "SaaS"}
    }
]

for entity_data in entities_data:
    entity_request = CreateEntityRequest(**entity_data)
    entity = knowledge_graph_service.create_entity(entity_request)
```

## API Endpoints

### Entity Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/knowledge-graph/entities` | Create a new entity |
| GET | `/knowledge-graph/entities/{id}` | Get entity by ID |
| PUT | `/knowledge-graph/entities/{id}` | Update entity |
| DELETE | `/knowledge-graph/entities/{id}` | Delete entity |
| GET | `/knowledge-graph/entities/search?query={q}` | Search entities |
| GET | `/knowledge-graph/entities/{id}/relationships` | Get entity relationships |
| GET | `/knowledge-graph/entities/{id}/related` | Find related entities |

### Relationship Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/knowledge-graph/relationships` | Create relationship |
| GET | `/knowledge-graph/relationships/{id}` | Get relationship by ID |
| DELETE | `/knowledge-graph/relationships/{id}` | Delete relationship |

### Analytics Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/knowledge-graph/analytics` | Get graph analytics |
| GET | `/knowledge-graph/health` | Get graph health status |
| GET | `/knowledge-graph/visualization` | Get visualization data |

## Example Usage

See the complete working example in:
```
/backend/examples/store_knowledge_graph_data.py
```

Run it with:
```bash
docker exec rag-backend-1 python /app/examples/store_knowledge_graph_data.py
```

### Quick Start Example

```python
from src.services.knowledge_graph_service import knowledge_graph_service
from src.models.graph import *

# Create entities
person = knowledge_graph_service.create_entity(CreateEntityRequest(
    name="Sarah Williams",
    entity_type=EntityType.PERSON,
    confidence_score=0.95,
    extraction_method=ExtractionMethod.MANUAL,
    metadata={"role": "CEO"}
))

company = knowledge_graph_service.create_entity(CreateEntityRequest(
    name="InnovateTech",
    entity_type=EntityType.ORGANIZATION,
    confidence_score=0.98,
    extraction_method=ExtractionMethod.MANUAL,
    metadata={"industry": "Technology"}
))

# Create relationship
rel = knowledge_graph_service.create_relationship(CreateRelationshipRequest(
    source_entity_id=person.id,
    target_entity_id=company.id,
    relationship_type=RelationshipType.WORKS_FOR,
    strength=1.0,
    confidence_score=0.95,
    metadata={"start_date": "2020-01-01"}
))
```

## Querying the Graph

### Using Cypher (Neo4j Browser)

Access Neo4j Browser at http://localhost:7474 and run Cypher queries:

```cypher
// View all entities
MATCH (e:Entity) RETURN e LIMIT 25

// Find all people
MATCH (e:Entity)
WHERE e.type = 'PERSON'
RETURN e.name, e.metadata

// Find relationships
MATCH (a:Entity)-[r]->(b:Entity)
RETURN a.name, type(r), b.name
LIMIT 20

// Find who works where
MATCH (p:Entity {type: 'PERSON'})-[r:RELATED_TO]->(o:Entity {type: 'ORGANIZATION'})
WHERE r.type = 'WORKS_FOR'
RETURN p.name as person, o.name as organization

// Find related entities within 2 hops
MATCH path = (start:Entity {name: 'Alice Johnson'})-[*1..2]-(end:Entity)
RETURN path
LIMIT 50
```

### Using Python

```python
with knowledge_graph_service.get_session() as session:
    # Get all entities
    result = session.run("MATCH (e:Entity) RETURN e.name as name, e.type as type")
    entities = list(result)

    # Find connected entities
    result = session.run("""
        MATCH (a:Entity {name: $name})-[r]-(b:Entity)
        RETURN b.name as connected, type(r) as relationship_type
    """, name="Alice Johnson")
    connections = list(result)
```

### Using the API

```python
import requests

# Search entities
response = requests.get(
    "http://localhost:8000/knowledge-graph/entities/search",
    params={"query": "Alice", "limit": 10},
    headers=headers
)
entities = response.json()

# Find related entities
response = requests.get(
    f"http://localhost:8000/knowledge-graph/entities/{entity_id}/related",
    params={"max_depth": 2, "limit": 20},
    headers=headers
)
related = response.json()
```

## Best Practices

### 1. Always Provide Metadata

Metadata enriches entities and makes them more useful:

```python
metadata = {
    "source_document": "report_2024.pdf",
    "page_number": 5,
    "extraction_date": "2024-01-15",
    "verified": True
}
```

**Important**: Always provide at least one key-value pair in metadata. Neo4j doesn't accept empty dictionaries `{}`.

### 2. Use Appropriate Confidence Scores

- Manual entries: 0.95-1.0
- LLM extraction: 0.80-0.95
- Pattern matching: 0.70-0.85
- Rule-based: 0.60-0.80

### 3. Provide Evidence for Relationships

```python
evidence = [
    "LinkedIn profile shows employment",
    "Company website lists as team member",
    "Email signature confirms role"
]
```

### 4. Use Context for Debugging

Store the surrounding text where the entity/relationship was found:

```python
context = "Sarah Williams, CEO of InnovateTech, announced..."
```

### 5. Set Appropriate Relationship Strength

- Direct, confirmed relationships: 1.0
- Inferred relationships: 0.5-0.8
- Weak associations: 0.1-0.4

### 6. Handle Duplicates

Before creating entities, check if they already exist:

```python
# Search for existing entity
existing = knowledge_graph_service.search_entities(
    query=entity_name,
    entity_types=[EntityType.PERSON],
    limit=1
)

if existing:
    # Update existing entity
    entity = knowledge_graph_service.update_entity(existing[0].id, updates)
else:
    # Create new entity
    entity = knowledge_graph_service.create_entity(request)
```

### 7. Use Transactions for Batch Operations

When creating multiple related entities and relationships, ensure atomicity.

### 8. Regular Maintenance

- Monitor graph size and performance
- Remove orphaned nodes
- Merge duplicate entities
- Update confidence scores based on new evidence

## Visualization

Access the Neo4j Browser at http://localhost:7474 to visualize your graph:

1. Login with credentials: neo4j / neo4jpassword
2. Run queries to explore your data
3. Use the graph visualization to understand relationships
4. Export results as JSON, CSV, or images

## Troubleshooting

### Common Issues

**Problem**: Empty metadata error
```
Property values can only be of primitive types or arrays thereof. Encountered: Map{}.
```
**Solution**: Always provide at least one key-value pair in metadata dictionaries.

**Problem**: Entity not found
**Solution**: Verify the entity ID and check if the entity was actually created.

**Problem**: Connection timeout
**Solution**: Check if Neo4j is running: `docker ps | grep neo4j`

**Problem**: Duplicate entities
**Solution**: Implement deduplication logic before creating entities.

## Next Steps

1. **Integrate with Document Processing**: Automatically extract entities from uploaded documents
2. **Build Search Features**: Use the graph for semantic search and recommendations
3. **Create Analytics Dashboards**: Visualize entity relationships and patterns
4. **Implement Caching**: Cache frequently accessed entities for better performance
5. **Add Monitoring**: Track graph growth and query performance

## Resources

- [Neo4j Documentation](https://neo4j.com/docs/)
- [Cypher Query Language](https://neo4j.com/docs/cypher-manual/current/)
- [Graph Data Modeling](https://neo4j.com/developer/data-modeling/)
- [Example Script](./backend/examples/store_knowledge_graph_data.py)

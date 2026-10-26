# Automatic Knowledge Graph Integration for Document Uploads

> **Stale / historical (2026-10-17):** Qdrant is fully removed and live retrieval is PostgreSQL full-text search (`DO_KB_ENABLED` defaults to `False`; pgvector semantic search is flag-gated). Treat Qdrant, DO KB and vector-store statements below as historical. Current contracts: [`docs/engineering/`](../engineering/README.md).

## Overview

This document describes the automatic knowledge graph extraction feature that has been integrated into the document upload pipeline. When you upload a document via the frontend, the system will automatically extract entities and store them in the Neo4j knowledge graph.

## What Was Implemented

### 1. Integration into Document Processing Pipeline

**File**: `backend/src/services/multimodal_processing_service.py`

A new processing step called "Knowledge Graph Storage" has been added to the document processing pipeline. This step runs automatically after entity extraction.

**Pipeline Order**:
1. Text Extraction
2. Entity Extraction
3. **Knowledge Graph Storage** ← NEW!
4. Embedding Generation
5. AI Analysis (optional)
6. Quality Assessment (optional)

### 2. New Method: `store_entities_in_knowledge_graph`

This async method:
- Extracts entities from the document text using spaCy NER
- Maps entity types to knowledge graph entity types
- Creates entity nodes in Neo4j with metadata
- Links entities back to the source document
- Returns statistics about entities stored

**Features**:
- Automatic entity type mapping (PERSON, ORGANIZATION, LOCATION, etc.)
- Confidence scores preserved
- Document context included
- Source document tracking
- Error handling and logging

### 3. Entity Metadata

Each entity stored in the knowledge graph includes:
```python
{
    "document_id": str,           # Source document ID
    "document_title": str,        # Source document title
    "extraction_date": str,       # ISO timestamp
    "source": "document_processing"
}
```

### 4. Configuration

The knowledge graph storage step is marked as `required=False`, meaning:
- If it fails, the document processing continues
- Errors are logged but don't block the pipeline
- Perfect for gradual rollout and testing

## How It Works

### Frontend Upload Flow

1. **User uploads document** via frontend
2. **Backend receives file** at `/api/v2/documents/upload/single`
3. **File is validated** and scanned for security
4. **Document record created** in PostgreSQL
5. **Processing job queued** for background processing
6. **Pipeline executes**:
   - Extract text from document
   - Extract entities using spaCy
   - **Store entities in Neo4j** ← Automatic!
   - Generate embeddings
   - Perform AI analysis
   - Assess quality

### Backend Processing

```python
# Simplified flow
document = create_document_record(uploaded_file)
job = create_processing_job(document)

# Background task
processing_results = await processing_service.process_document(document, job)

# Results include:
{
    "entity_extraction": {
        "entity_count": 15,
        "entities": [...]
    },
    "knowledge_graph_storage": {  # NEW!
        "entities_stored": 12,
        "relationships_stored": 0,
        "processing_time": 0.5,
        "errors": []
    }
}
```

## Entity Type Mapping

The system maps spaCy entity types to knowledge graph types:

| spaCy Type | Knowledge Graph Type |
|------------|---------------------|
| PERSON | PERSON |
| ORG | ORGANIZATION |
| GPE | LOCATION |
| LOC | LOCATION |
| PRODUCT | PRODUCT |
| EVENT | CONCEPT |
| DATE | DATE |
| MONEY | NUMBER |
| ... | ... |

## Testing

### Test Script

A test script has been created: `backend/examples/test_auto_knowledge_graph.py`

**Run it with**:
```bash
docker exec rag-backend-1 python /app/examples/test_auto_knowledge_graph.py
```

**What it does**:
1. Creates a test document with sample text containing entities
2. Processes the document through the pipeline
3. Verifies entities are stored in Neo4j
4. Displays statistics and sample entities

### Manual Testing via Frontend

1. Upload a document through the frontend
2. Wait for processing to complete
3. Check Neo4j Browser at http://localhost:7474
4. Query for entities from your document:

```cypher
// Find entities from a specific document
MATCH (e:Entity)
WHERE e.source_document_id = 'YOUR-DOCUMENT-ID'
RETURN e

// View all recently added entities
MATCH (e:Entity)
WHERE e.metadata IS NOT NULL
AND e.metadata CONTAINS 'document_processing'
RETURN e.name, e.type, e.confidence_score
ORDER BY e.created_at DESC
LIMIT 20
```

## Current Status

### ✅ Completed

- ✅ Integration method created
- ✅ Added to processing pipeline
- ✅ Entity type mapping implemented
- ✅ Metadata enrichment
- ✅ Error handling
- ✅ Test script created
- ✅ Documentation written

### 🔧 Needs Debugging

- **Entity extraction service initialization** - There are two different EntityExtractionService classes causing conflicts
- **Method signatures** - Need to ensure correct service is used with correct parameters
- **Async/sync compatibility** - Some methods need async wrappers

### 📋 TODO

1. **Debug and fix entity extraction**:
   - Resolve EntityExtractionService conflicts
   - Ensure proper initialization
   - Test end-to-end flow

2. **Add relationship extraction**:
   - Extract relationships between entities
   - Store relationships in knowledge graph
   - Link related entities

3. **Implement deduplication**:
   - Check for existing entities before creating
   - Merge duplicate entities
   - Update confidence scores

4. **Add configuration options**:
   - Enable/disable per organization
   - Configure entity types to extract
   - Set confidence thresholds

5. **Monitor and optimize**:
   - Track extraction performance
   - Monitor graph growth
   - Optimize queries

## Configuration

### Enable/Disable

The feature is currently enabled by default for all document uploads. To disable it:

**Option 1**: Remove from pipeline

```python
# In multimodal_processing_service.py
common_steps = [
    ProcessingStep("Text Extraction", self.extract_text_content),
    ProcessingStep("Entity Extraction", self.extract_entities),
    # ProcessingStep("Knowledge Graph Storage", self.store_entities_in_knowledge_graph, required=False),  # Comment out
    ProcessingStep("Embedding Generation", self.generate_embeddings),
    ...
]
```

**Option 2**: Add configuration flag

```python
# In document_upload.py
config={
    "max_retries": 3,
    "timeout_seconds": 600,
    "enable_ocr": True,
    "enable_entity_extraction": True,
    "enable_knowledge_graph": False,  # Add this
    "enable_embedding_generation": True
}
```

## Troubleshooting

### No entities being stored

**Check**:
1. Is Neo4j running? `docker ps | grep neo4j`
2. Can backend connect to Neo4j? Check logs
3. Are entities being extracted? Check entity_extraction step results
4. Any errors in knowledge_graph_storage results?

### Duplicate entities

**Solution**: Implement deduplication logic before creating entities:

```python
# Search for existing entity
existing = knowledge_graph_service.search_entities(
    query=entity_name,
    entity_types=[entity_type],
    limit=1
)

if existing:
    # Update existing entity
    knowledge_graph_service.update_entity(existing[0].id, {...})
else:
    # Create new entity
    knowledge_graph_service.create_entity(entity_request)
```

### Low extraction quality

**Improve**:
1. Use better NLP models (upgrade spaCy model)
2. Implement LLM-based extraction
3. Add custom extraction rules
4. Fine-tune confidence thresholds

## API Endpoints

### Query Extracted Entities

```bash
# Get all entities from a document
GET /knowledge-graph/entities/search?query=<document_id>

# Get entity relationships
GET /knowledge-graph/entities/{entity_id}/relationships

# Find related entities
GET /knowledge-graph/entities/{entity_id}/related?max_depth=2
```

## Examples

### Sample Text with Entities

```text
Dr. Jane Smith is the Chief Technology Officer at TechCorp Solutions,
a leading artificial intelligence company based in San Francisco, California.
```

**Extracted Entities**:
- Dr. Jane Smith (PERSON) - confidence: 0.95
- TechCorp Solutions (ORGANIZATION) - confidence: 0.92
- San Francisco (LOCATION) - confidence: 0.98
- California (LOCATION) - confidence: 0.98

### Knowledge Graph Query

```cypher
// Find who works where
MATCH (p:Entity {type: 'PERSON'})-[r:WORKS_FOR]->(o:Entity {type: 'ORGANIZATION'})
RETURN p.name as person, o.name as company

// Find organizations in a location
MATCH (o:Entity {type: 'ORGANIZATION'})-[r:LOCATED_IN]->(l:Entity {type: 'LOCATION'})
RETURN o.name as organization, l.name as location
```

## Performance

### Expected Processing Times

- Small text file (< 1KB): 0.1-0.5s
- Medium document (< 100KB): 0.5-2s
- Large document (> 1MB): 2-10s

### Optimization Tips

1. **Batch processing**: Store multiple entities in one transaction
2. **Caching**: Cache entity lookups for deduplication
3. **Async processing**: Already implemented
4. **Index tuning**: Ensure Neo4j indexes are optimized

## Processing Existing Documents

### Backfill Script

For documents that were uploaded **before** this feature was implemented, use the backfill script to extract entities retroactively.

**Script**: `backend/examples/backfill_knowledge_graph.py`

### Usage

```bash
# Dry run - see what would be processed
docker exec rag-backend-1 python /app/examples/backfill_knowledge_graph.py --dry-run

# Process all existing documents
docker exec rag-backend-1 python /app/examples/backfill_knowledge_graph.py

# Process in smaller batches (default is 10)
docker exec rag-backend-1 python /app/examples/backfill_knowledge_graph.py --batch-size 5

# Process only first 50 documents
docker exec rag-backend-1 python /app/examples/backfill_knowledge_graph.py --limit 50

# Process specific documents by ID
docker exec rag-backend-1 python /app/examples/backfill_knowledge_graph.py --document-ids doc-id-1 doc-id-2

# Reprocess documents that already have entities
docker exec rag-backend-1 python /app/examples/backfill_knowledge_graph.py --reprocess-all
```

### Features

**Smart Processing**:
- Automatically skips documents that already have entities in the knowledge graph
- Processes documents in batches to manage memory
- Commits after each batch for safety
- Detailed progress reporting
- Error handling with statistics

**What Gets Processed**:
- ✅ Documents with text content
- ✅ Non-deleted documents
- ✅ Documents without existing entities (unless `--reprocess-all`)
- ❌ Documents without text content
- ❌ Deleted documents

**Output Example**:
```
======================================================================
Knowledge Graph Backfill - Processing Existing Documents
======================================================================

Found 0 documents already processed
Found 45 documents to process

Processing 45 documents in batches of 10

Batch 1/5 (10 documents)
----------------------------------------------------------------------
✓ Research Paper on AI Ethics                          | Entities:  12
✓ Q2 Financial Report                                  | Entities:   8
✓ Customer Meeting Notes                               | Entities:  15
✓ Product Specification Document                       | Entities:   6
...

======================================================================
Backfill Complete!
======================================================================

Statistics:
  Documents processed:       45
  Documents skipped:          0
  Entities extracted:       387
  Entities stored:          387
  Errors:                     0
  Processing time:         23.45s
  Avg time per document:    0.52s
  Avg entities per doc:     8.6

Verify in Neo4j:
  1. Open http://localhost:7474
  2. Run: MATCH (e:Entity) WHERE e.metadata.backfilled = true RETURN count(e)
```

### Metadata for Backfilled Entities

Entities extracted during backfill are marked with special metadata:

```python
{
    "document_id": str,
    "document_title": str,
    "document_type": str,
    "extraction_date": str,
    "source": "backfill_processing",  # vs "document_processing"
    "backfilled": True                # Special flag
}
```

### Query Backfilled Entities

```cypher
// Find all backfilled entities
MATCH (e:Entity)
WHERE e.metadata.backfilled = true
RETURN e.name, e.type, e.source_document_id
LIMIT 50

// Count backfilled vs real-time entities
MATCH (e:Entity)
RETURN
  e.metadata.source as source,
  count(e) as count
ORDER BY count DESC

// Find documents with backfilled entities
MATCH (e:Entity)
WHERE e.metadata.backfilled = true
RETURN DISTINCT e.source_document_id, e.metadata.document_title
```

### Best Practices

1. **Start with dry run** to see what will be processed
2. **Use limits** for large databases (test with --limit 10 first)
3. **Monitor performance** - adjust batch size based on results
4. **Run during off-peak** hours for large backlogs
5. **Verify results** in Neo4j Browser after completion

### Scheduling Regular Backfills

For periodic processing of missed documents, create a cron job:

```bash
# Add to crontab
0 2 * * * docker exec rag-backend-1 python /app/examples/backfill_knowledge_graph.py --limit 100
```

This runs daily at 2 AM and processes up to 100 documents.

## Next Steps

1. **Process existing documents** using the backfill script
2. **Test the integration** with real document uploads
3. **Fix any remaining bugs** in entity extraction
4. **Add relationship extraction** for richer graph
5. **Implement deduplication** to avoid duplicate entities
6. **Create frontend visualization** to show extracted entities
7. **Add analytics** to track extraction quality

## Resources

- [Knowledge Graph Guide](./KNOWLEDGE_GRAPH_GUIDE.md)
- [Example Scripts](./backend/examples/)
- [Neo4j Browser](http://localhost:7474)
- [API Documentation](http://localhost:8000/docs)

## Support

For issues or questions:
1. Check the logs: `docker logs rag-backend-1`
2. Review Neo4j Browser for data verification
3. Run the test script for debugging
4. Check processing job status in database

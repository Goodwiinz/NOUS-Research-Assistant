# ArXiv Download and Local Extraction Workflow

> **Unverified 2026-10-17: the ingest route in the current backend is `POST /api/v1/arxiv/ingest` (see `backend/src/api/arxiv/core.py`), not `/ingest/papers`, and request bodies may have drifted. Check `backend/openapi.json` before using any curl example below.**

## Overview
This workflow downloads ArXiv papers first, then extracts features from the local PDF files. This provides better extraction results with meaningful relationships in the knowledge graph.

## Step 1: Download ArXiv Papers

### Option A: Using the Frontend UI
1. Go to your frontend application
2. In the "ArXiv Paper Management" section:
   - Select categories (e.g., cs.AI, cs.CV, cs.LG)
   - Set days back (e.g., 1 for recent papers)
   - Set max papers (e.g., 10-20)
   - Enable "Download PDFs"
3. Click "Track and Ingest Papers"
4. Wait for download to complete

### Option B: Direct API Call
```bash
curl -X POST http://localhost:8000/api/v1/arxiv/ingest/papers \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "categories": ["cs.AI", "cs.CV", "cs.LG"],
    "days_back": 1,
    "max_papers": 10,
    "download_pdfs": true,
    "update_database": true
  }'
```

## Step 2: Extract from Local PDFs

### Using the Frontend UI
1. After download is complete, go to the "Extract Local Features" section
2. Configure extraction options:
   - ✅ Extract Entities
   - ✅ Extract Topics
   - ✅ Extract Keyphrases
   - ✅ Extract Summaries
   - ✅ Update Knowledge Graph
   - Set "Process full content" to true for better results
3. Click "Extract Features from Local PDFs"
4. Wait for extraction to complete

### Check Results
After extraction, you should see:
- Topics extracted from the actual paper content
- Keyphrases that are relevant to the paper
- Relationships in the Neo4j browser at `http://localhost:7474/browser/`

## Step 3: Verify in Neo4j Browser

1. Open `http://localhost:7474/browser/`
2. Run this query to see recent additions:
```cypher
MATCH (n:Entity:DOCUMENT)
WHERE n.source = 'local_arxiv'
RETURN n.name as title, n.paper_id as paper_id, n.created_at as created
ORDER BY n.created_at DESC
LIMIT 10
```

3. Run this query to see relationships:
```cypher
MATCH (doc:Entity:DOCUMENT)-[r:RELATED_TO]->(concept:Entity:CONCEPT)
WHERE doc.source = 'local_arxiv'
RETURN doc.name as paper, concept.name as topic, type(r) as relation
LIMIT 20
```

## Tips for Better Results

1. **Download First**: Always download papers before extracting
2. **Full Content**: Enable "Process full content" for better topic extraction
3. **Check Downloads**: Ensure papers are downloaded to the uploads directory
4. **Monitor Logs**: Watch backend logs for any errors during extraction

## Troubleshooting

### If no topics are extracted:
- Check if the PDF was downloaded successfully
- Verify "Process full content" is enabled
- Look for errors in backend logs

### If relationships are missing:
- Ensure "Update Knowledge Graph" is checked
- Check backend logs for KG update messages
- Verify Neo4j connection is working

### If papers aren't downloading:
- Check ArXiv rate limits (you may need to wait)
- Verify internet connection
- Check backend logs for download errors

## Why This Works Better

1. **Full Text Access**: Local PDF extraction can read the entire paper content
2. **Better Entity Recognition**: Processes full text with proper NLP
3. **Meaningful Relationships**: Creates actual relationships between papers and topics
4. **Persistent Storage**: Downloaded papers stay available for re-extraction

## Automation Script

You can also use this Python script to automate the process:

```python
import asyncio
import requests
import time

# Configuration
BASE_URL = "http://localhost:8000/api/v1"
TOKEN = "YOUR_AUTH_TOKEN"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

async def download_and_extract():
    # Step 1: Download papers
    print("Downloading papers...")
    download_data = {
        "categories": ["cs.AI", "cs.CV"],
        "days_back": 1,
        "max_papers": 5,
        "download_pdfs": True,
        "update_database": True
    }

    response = requests.post(
        f"{BASE_URL}/arxiv/ingest/papers",
        json=download_data,
        headers=headers
    )

    if response.status_code == 200:
        result = response.json()
        print(f"Downloaded {result.get('downloaded_count', 0)} papers")
    else:
        print(f"Download failed: {response.text}")
        return

    # Step 2: Wait a bit for files to be processed
    print("Waiting 5 seconds...")
    time.sleep(5)

    # Step 3: Extract from local PDFs
    print("Extracting from local PDFs...")
    extract_data = {
        "paper_ids": None,  # Process all
        "extract_entities": True,
        "extract_topics": True,
        "extract_keyphrases": True,
        "extract_summaries": True,
        "process_full_content": True,
        "update_knowledge_graph": True
    }

    response = requests.post(
        f"{BASE_URL}/arxiv/local/extract-local-features",
        json=extract_data,
        headers=headers
    )

    if response.status_code == 200:
        result = response.json()
        print(f"Extracted features from {result.get('processed_count', 0)} papers")
    else:
        print(f"Extraction failed: {response.text}")

# Run the workflow
asyncio.run(download_and_extract())
```

This workflow ensures you get the best extraction results with proper relationships in your knowledge graph!
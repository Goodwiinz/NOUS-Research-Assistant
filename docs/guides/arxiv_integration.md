# ArXiv Integration for RAG System

This guide describes how to use the arXiv dataset integration with your multimodal RAG system for testing and evaluation.

## Overview

The arXiv integration provides:
- **Bulk ingestion** of arXiv papers (metadata + PDFs)
- **Category-based filtering** and search
- **Evaluation dataset generation** for RAG testing
- **Specialized search capabilities** for academic papers
- **Performance metrics** tracking for arXiv content

## Quick Start

### 1. Using the CLI Tool

The arxiv_cli.py script provides easy command-line access to arXiv functionality:

```bash
# Search for papers
python scripts/arxiv_cli.py search "machine learning" --max-results 10 --categories cs.LG cs.AI

# Ingest papers into the system
python scripts/arxiv_cli.py ingest --query "transformer architecture" --max-results 50 --download-pdfs

# Create evaluation dataset
python scripts/arxiv_cli.py dataset --query "attention mechanisms" --num-papers 20 --questions-per-paper 5

# Get statistics
python scripts/arxiv_cli.py stats --days 30 --categories cs.LG

# Download PDFs for specific papers
python scripts/arxiv_cli.py download 2301.07041 2017.08447
```

### 2. Using the API

The API endpoints provide programmatic access to arXiv functionality:

```python
import requests

# Login to get token
response = requests.post("http://localhost:8000/api/v1/auth/login",
                        json={"email": "test@example.com", "password": "SecurePass123!"})
token = response.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# Search papers
search_data = {
    "query": "deep learning",
    "max_results": 20,
    "categories": ["cs.LG", "cs.AI"]
}
response = requests.post("http://localhost:8000/api/v1/arxiv/search",
                        json=search_data, headers=headers)
papers = response.json()

# Ingest papers
ingest_data = {
    "paper_ids": ["2301.07041", "2017.08447"],
    "download_pdfs": True,
    "extract_content": True
}
response = requests.post("http://localhost:8000/api/v1/arxiv/ingest",
                        json=ingest_data, headers=headers)

# Get categories
response = requests.get("http://localhost:8000/api/v1/arxiv/categories", headers=headers)
categories = response.json()
```

## Features

### 1. Paper Search

Search arXiv papers with various filters:

```python
# Basic search
papers = await arxiv_service.search_papers(
    query="attention mechanism",
    max_results=100
)

# Advanced search with filters
papers = await arxiv_service.search_papers(
    query="transformer architecture",
    categories=["cs.LG", "cs.CL"],
    date_from=datetime(2023, 1, 1),
    sort_by="submittedDate"
)
```

### 2. PDF Processing

Download and process full PDF content:

```python
# Download PDF
pdf_content = await arxiv_service.download_paper_pdf("2301.07041")

# Extract content
extracted = await arxiv_service.extract_pdf_content(pdf_content)
print(f"Extracted {extracted['num_pages']} pages")
```

### 3. Evaluation Dataset Generation

Create structured evaluation datasets:

```python
# Generate dataset with questions
dataset = await arxiv_service.create_evaluation_dataset(
    papers=papers,
    num_questions=5,
    difficulty_levels=['easy', 'medium', 'hard']
)

# Dataset structure
print(f"Created {len(dataset['test_cases'])} test cases")
for test_case in dataset['test_cases']:
    print(f"Paper: {test_case['paper_title']}")
    for question in test_case['questions']:
        print(f"  Q: {question['question']}")
        print(f"  Difficulty: {question['difficulty']}")
```

### 4. Specialized Search

Use arXiv-specific search features:

```python
# Search within arXiv papers
results = await arxiv_search_service.search_papers(
    query="BERT",
    filters={
        'categories': ['cs.CL'],
        'date_from': '2023-01-01',
        'boost_categories': ['cs.LG']
    },
    boost_recent=True,
    sort_by="relevance"
)

# Find similar papers
similar = await arxiv_search_service.get_similar_papers("2301.07041")

# Get papers by author
author_papers = await arxiv_search_service.get_author_papers(
    "Geoffrey Hinton",
    include_coauthors=True
)

# Get trending papers
trending = await arxiv_search_service.get_trending_papers(
    category='cs.LG',
    days=7
)
```

## Category Taxonomy

ArXiv uses a hierarchical category system:

### Computer Science (cs)
- **AI**: Artificial Intelligence
- **CL**: Computation and Language (NLP)
- **CV**: Computer Vision
- **LG**: Machine Learning
- **NE**: Neural and Evolutionary Computing
- **RO**: Robotics
- **CR**: Cryptography and Security

### Mathematics (math)
- **OC**: Optimization and Control
- **ST**: Statistics Theory
- **PR**: Probability

### Statistics (stat)
- **ML**: Machine Learning
- **ME**: Methodology
- **TH**: Theory

### Physics
- **quant-ph**: Quantum Physics
- **cond-mat**: Condensed Matter
- **astro-ph**: Astrophysics

## Evaluation Metrics

The system tracks multiple RAG evaluation metrics:

### Retrieval Metrics
- **Precision@k**: Fraction of retrieved items that are relevant
- **Mean Reciprocal Rank (MRR)**: Average reciprocal rank of first relevant item
- **Recall**: Fraction of relevant items retrieved

### Answer Quality Metrics
- **Answer Relevancy**: How well the answer addresses the query (>70% threshold)
- **Faithfulness**: How grounded the answer is in retrieved context (>90% threshold)
- **Contextual Precision**: Fraction of retrieved contexts that are relevant (>70% threshold)

### Performance Metrics
- **Response Time**: Time to generate answer (<2000ms target)
- **Throughput**: Queries per second
- **Availability**: System uptime

## Running Evaluation

### 1. Create Evaluation Dataset

```bash
python scripts/arxiv_cli.py dataset \
    --query "machine learning transformers" \
    --num-papers 50 \
    --questions-per-paper 5 \
    --output datasets/evaluation_dataset.json
```

### 2. Run RAG Evaluation

```bash
python scripts/evaluate_arxiv_rag.py \
    --dataset datasets/evaluation_dataset.json \
    --max-cases 100 \
    --output evaluation_results.json \
    --api-url http://localhost:8000
```

### 3. Analyze Results

The evaluation produces:

```
📊 EVALUATION SUMMARY
============================================================

Total Questions: 250
Overall Pass Rate: 82.4%

⏱️  Response Time:
  Average: 1245ms
  Median: 1120ms
  <2s Pass Rate: 94.8%

🎯 Retrieval Quality:
  Precision@1: 0.65
  Precision@3: 0.78
  Precision@5: 0.84
  MRR: 0.723

💬 Answer Quality:
  Relevancy: 0.76
  Faithfulness: 0.89
  Contextual Precision: 0.72

🏆 Overall Performance:
  Score: 84.2%
  Rating: GOOD ⭐⭐⭐⭐
```

## Best Practices

### 1. Testing Strategy

1. **Start Small**: Begin with 10-20 papers for initial testing
2. **Focus Categories**: Limit to specific categories for domain testing
3. **Varied Queries**: Include different query types and difficulties
4. **Ground Truth**: Use papers you know well for manual validation

### 2. Performance Optimization

1. **Batch Processing**: Process papers in batches to manage memory
2. **Async Downloads**: Use concurrent downloads for PDFs
3. **Caching**: Cache processed content to avoid reprocessing
4. **Incremental**: Add papers incrementally rather than all at once

### 3. Quality Assurance

1. **Validation**: Check that paper IDs match between search and ingestion
2. **Content Quality**: Verify PDF extraction worked correctly
3. **Category Mapping**: Ensure categories are properly indexed
4. **Metadata**: Check all metadata fields are captured

## Troubleshooting

### Common Issues

1. **PDF Download Fails**:
   - Check internet connectivity
   - Verify paper ID is correct
   - Some papers may not have PDFs available

2. **Slow Ingestion**:
   - Reduce batch size
   - Skip PDF downloads if not needed
   - Check system resources

3. **Poor Search Results**:
   - Verify categories are indexed
   - Check embedding model is working
   - Review query formulation

4. **Evaluation Fails**:
   - Ensure login credentials are correct
   - Check API is accessible
   - Verify dataset format is valid

### Debug Commands

```bash
# Check arXiv service health
curl http://localhost:8000/api/v1/arxiv/categories

# Test search
python scripts/arxiv_cli.py search "test query" --max-results 1

# Check downloaded files
ls -la data/arxiv/

# Run with debug logging
export LOG_LEVEL=DEBUG
python scripts/arxiv_cli.py search "machine learning"
```

## Advanced Usage

### Custom Category Filtering

```python
# Filter by multiple category patterns
categories = ['cs.LG', 'cs.AI', 'stat.ML']

# Filter by category group
cs_categories = [cat for cat in all_categories if cat.startswith('cs.')]

# Custom category weights
weights = {
    'cs.AI': 1.5,  # Boost AI papers
    'cs.LG': 1.3,  # Boost ML papers
    'cs.IR': 0.8   # Downgrade IR papers
}
```

### Bulk Operations

```python
# Bulk ingest from file
paper_ids = []
with open('paper_ids.txt') as f:
    paper_ids = [line.strip() for line in f]

await arxiv_service.ingest_papers(
    papers=[{'id': pid} for pid in paper_ids],
    download_pdfs=False,  # Skip PDFs for speed
    batch_size=20
)
```

### Custom Question Generation

```python
# Custom question templates
templates = [
    "What methodology does {title} propose?",
    "How does {title} compare to previous work in {categories[0]}?",
    "What are the limitations of {title}?",
    "What datasets were used in {title}?"
]

# Generate using LLM (example)
questions = await llm_generate_questions(paper_abstract, templates)
```

## Integration with Existing Systems

### 1. Add to Existing Pipeline

```python
# In your ingestion pipeline
async def ingest_arxiv_papers(query: str):
    async with ArXivIngestionService() as service:
        papers = await service.search_papers(query, max_results=100)
        documents = await service.ingest_papers(papers)

        # Add to your vector store
        for doc in documents:
            await vector_store.add(document=doc)
```

### 2. Enhance Search UI

```javascript
// Frontend category filter
const arxivCategories = {
    "Computer Science": ["cs.AI", "cs.LG", "cs.CV", "cs.CL"],
    "Mathematics": ["math.OC", "math.ST", "math.PR"],
    "Physics": ["quant-ph", "cond-mat", "astro-ph"]
};

// Add to search request
searchParams.categories = selectedCategories;
```

### 3. Monitoring Integration

```python
# Add to your metrics collection
metrics = {
    'arxiv_papers_ingested': counter,
    'arxiv_search_latency': histogram,
    'arxiv_categories_distribution': gauge
}
```

## References

- [ArXiv API Documentation](https://arxiv.org/help/api)
- [ArXiv Category Taxonomy](https://arxiv.org/category_taxonomy)
- [RAG Evaluation Best Practices](https://docs.example.com/rag-evaluation)
- Performance Benchmarks (the former `benchmarks/README.md` no longer exists)
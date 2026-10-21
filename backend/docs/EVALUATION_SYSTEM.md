# RAG Evaluation System Documentation

> **Stale / historical (2026-10-17):** It is a design document for a subsystem; the described service layout may not match the current code. Verify against `backend/src/` and `backend/openapi.json` before relying on it. Current contracts: [`docs/engineering/`](../../docs/engineering/README.md).

## Overview

The RAG Evaluation System provides comprehensive evaluation capabilities for the Multimodal Enterprise RAG System, focusing on RAG Triad metrics calculation and evaluation workflows. This system enables organizations to measure, monitor, and improve the quality of their RAG implementations.

## Key Features

### RAG Triad Metrics
- **Answer Relevancy**: Measures how relevant generated answers are to the original queries
- **Faithfulness**: Evaluates whether answers are supported by the retrieved context
- **Contextual Relevancy**: Assesses the relevance of retrieved context to the query
- **Hallucination Rate**: Detects fabricated information not supported by context

### Evaluation Workflows
- **Batch Evaluation**: Process multiple queries simultaneously
- **Real-time Evaluation**: Evaluate individual query-answer pairs instantly
- **Dataset-based Evaluation**: Use predefined Q&A datasets for systematic testing
- **Comparative Analysis**: Compare different configurations or system versions

### Integration Points
- Hybrid search service for end-to-end evaluation
- Vector search and knowledge graph services
- Quality metrics service for comprehensive monitoring
- Celery tasks for asynchronous processing

## Architecture

### Core Components

#### 1. Database Models (`src/models/evaluation.py`)
- `EvaluationJob`: Main evaluation job tracking
- `EvaluationMetric`: Individual metric results
- `EvaluationDataset`: Test question and answer datasets
- `EvaluationThreshold`: Configurable quality thresholds
- `EvaluationComparison`: Comparison between evaluations
- `EvaluationReport`: Generated evaluation reports

#### 2. Evaluation Service (`src/services/evaluation/rag_evaluation_service.py`)
- Core RAG Triad metrics calculation
- LLM-based evaluation scoring
- Report generation
- Statistical analysis and comparisons

#### 3. Celery Tasks (`src/tasks/evaluation_tasks.py`)
- Asynchronous evaluation processing
- Background job management
- Report generation tasks
- Cleanup and maintenance tasks

#### 4. API Endpoints (`src/api/evaluation.py`)
- RESTful API for evaluation management
- Real-time evaluation endpoints
- Job status monitoring
- Report retrieval and analytics

## Usage Examples

### Creating a Batch Evaluation Job

```python
import requests

# Create batch evaluation
request = {
    "name": "Search Quality Evaluation",
    "description": "Evaluate current search performance",
    "queries": [
        "What is machine learning?",
        "How does RAG work?",
        "What are the benefits of vector search?"
    ],
    "search_type": "hybrid",
    "search_limit": 5
}

response = requests.post(
    "http://localhost:8000/api/v1/evaluation/jobs/batch",
    json=request,
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)

job_id = response.json()["job_id"]
print(f"Created evaluation job: {job_id}")
```

### Real-time Evaluation

```python
# Evaluate a single query-answer pair
request = {
    "query": "What is the capital of France?",
    "generated_answer": "Paris is the capital and largest city of France.",
    "retrieved_context": [
        "Paris is the capital city of France.",
        "France is located in Western Europe."
    ],
    "reference_answer": "Paris is the capital of France."
}

response = requests.post(
    "http://localhost:8000/api/v1/evaluation/real-time",
    json=request,
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)

print(f"Task ID: {response.json()['task_id']}")
```

### Getting Evaluation Results

```python
# Get job status and results
response = requests.get(
    f"http://localhost:8000/api/v1/evaluation/jobs/{job_id}",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)

results = response.json()
print(f"Overall Score: {results['overall_score']}")
print(f"Success Rate: {results['success_rate']}%")
```

### Generating Reports

```python
# Generate summary report
response = requests.post(
    f"http://localhost:8000/api/v1/evaluation/jobs/{job_id}/reports/summary",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)

print(f"Report generation started: {response.json()['status']}")
```

## RAG Triad Metrics Explained

### Answer Relevancy
Measures how relevant the generated answer is to the original query.

**Scoring**: 0.0 (completely irrelevant) to 1.0 (perfectly relevant)

**Evaluation Method**: LLM judgment comparing query and answer relevance

**Typical Threshold**: > 0.7

### Faithfulness
Assesses whether the answer is supported by and faithful to the retrieved context.

**Scoring**: 0.0 (completely unsupported) to 1.0 (fully supported)

**Evaluation Method**: LLM verification of answer claims against context

**Typical Threshold**: > 0.9

### Contextual Relevancy
Evaluates how relevant the retrieved context is to answering the query.

**Scoring**: 0.0 (completely irrelevant) to 1.0 (highly relevant)

**Evaluation Method**: LLM assessment of context-query alignment

**Typical Threshold**: > 0.7

### Hallucination Rate
Detects fabricated information not supported by the retrieved context.

**Scoring**: 0.0 (no hallucinations) to 1.0 (severe hallucinations)

**Evaluation Method**: LLM identification of unsupported claims

**Typical Threshold**: < 0.1

## Configuration

### Environment Variables

```bash
# LLM API Keys (required for metric calculation)
OPENAI_API_KEY=your_openai_api_key
ANTHROPIC_API_KEY=your_anthropic_api_key

# Database Configuration
DATABASE_URL=postgresql://user:pass@localhost/rag_db

# Redis Configuration (for Celery)
REDIS_URL=redis://localhost:6379/0

# External Services
NEO4J_URI=bolt://localhost:7687
QDRANT_URL=http://localhost:6333
```

### Metric Thresholds

Configure organization-specific thresholds in the database:

```sql
INSERT INTO evaluation_thresholds (
    metric_type, threshold_min, organization_id, is_enabled
) VALUES (
    'rag_triad_answer_relevancy', 0.7, 'org-id', true
);
```

## Performance Considerations

### LLM API Usage
- Each evaluation requires multiple LLM API calls
- Consider rate limits and costs for large-scale evaluations
- Use batch processing for efficiency

### Asynchronous Processing
- All evaluation jobs run asynchronously via Celery
- Monitor task queues for performance
- Configure worker count based on workload

### Database Optimization
- Index evaluation tables for fast queries
- Archive old evaluation data regularly
- Use pagination for large result sets

## Monitoring and Analytics

### Key Metrics to Monitor
- Average evaluation scores by metric type
- Threshold violation rates
- Evaluation job completion times
- LLM API usage and costs

### Available Endpoints
- `/api/v1/evaluation/metrics/summary` - Organization metrics summary
- `/api/v1/evaluation/health` - Service health check
- `/api/v1/evaluation/jobs` - Job listing and status

## Troubleshooting

### Common Issues

1. **LLM API Errors**
   - Check API key configuration
   - Verify rate limits and quotas
   - Monitor costs for large evaluations

2. **Slow Performance**
   - Increase Celery worker count
   - Optimize database queries
   - Consider caching strategies

3. **Memory Issues**
   - Process evaluations in smaller batches
   - Monitor database connection pools
   - Clean up old evaluation data

### Debug Mode

Enable debug logging:

```python
import logging
logging.getLogger('src.services.evaluation.rag_evaluation_service').setLevel(logging.DEBUG)
```

## Best Practices

### Evaluation Design
- Use diverse query sets for comprehensive testing
- Include reference answers when possible
- Test across different domains and query types

### Threshold Configuration
- Set realistic initial thresholds
- Adjust based on system performance
- Monitor threshold violations over time

### Continuous Improvement
- Regular evaluation scheduling
- A/B testing of system changes
- Trend analysis of metric performance

## Future Enhancements

### Planned Features
- Custom metric definitions
- Advanced statistical analysis
- Evaluation template system
- Multi-language support
- Integration with more LLM providers

### Extensions
- User feedback integration
- Automated optimization suggestions
- Real-time quality monitoring
- Integration with CI/CD pipelines

## API Reference

### Evaluation Jobs
- `POST /api/v1/evaluation/jobs` - Create evaluation job
- `GET /api/v1/evaluation/jobs` - List evaluation jobs
- `GET /api/v1/evaluation/jobs/{job_id}` - Get job details
- `DELETE /api/v1/evaluation/jobs/{job_id}` - Delete job

### Evaluation Metrics
- `GET /api/v1/evaluation/jobs/{job_id}/metrics` - Get job metrics
- `GET /api/v1/evaluation/metrics/summary` - Get organization metrics

### Reports
- `POST /api/v1/evaluation/jobs/{job_id}/reports/{type}` - Generate report
- `GET /api/v1/evaluation/reports` - List reports
- `GET /api/v1/evaluation/reports/{report_id}` - Get report details

### Comparisons
- `POST /api/v1/evaluation/comparisons` - Create comparison
- `GET /api/v1/evaluation/comparisons` - List comparisons

### Real-time Evaluation
- `POST /api/v1/evaluation/real-time` - Evaluate single query-answer pair

## Support and Contributing

For issues, questions, or contributions:
1. Check existing documentation and examples
2. Review system logs for error details
3. Test with the integration script provided
4. Follow the established code patterns and conventions
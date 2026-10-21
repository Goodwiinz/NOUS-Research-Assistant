# Dataset Usage Guide for RAG System

This guide provides comprehensive instructions for using the example datasets mentioned in the project requirements: **DocVQA**, **PubLayNet**, and **LAION-400M**.

## Overview

The RAG system supports multimodal document processing and can integrate various datasets for testing and evaluation. This guide shows you how to:

1. **Download and prepare datasets** from the official sources
2. **Process and convert datasets** into the RAG system format
3. **Upload datasets** to the RAG system using our API
4. **Evaluate system performance** on different dataset types
5. **Generate reports and visualizations** of the results

## Prerequisites

- RAG system running (`docker-compose up -d`)
- Python 3.8+ with required dependencies
- Sufficient disk space for datasets (recommended: 10GB+)
- Internet connection for downloading datasets

## Quick Start

```bash
# Run the complete pipeline (integration + upload + evaluation)
python scripts/run_complete_pipeline.py

# Or run individual phases
python scripts/run_complete_pipeline.py --phase integration
python scripts/run_complete_pipeline.py --phase upload
python scripts/run_complete_pipeline.py --phase evaluation
```

## Dataset Details

### 1. DocVQA (Document Visual Question Answering)

**Source**: https://docvqa.github.io/

**Description**: Dataset for question answering on document images, containing questions about various types of documents including forms, invoices, reports, and tables.

**Key Features**:
- 50,000+ question-answer pairs
- Multiple document types (forms, invoices, reports, tables)
- OCR-extracted text and layout information
- Bounding box coordinates for referenced regions

**Use Cases**:
- Testing document understanding capabilities
- Evaluating question-answering on structured documents
- Benchmarking OCR and text extraction performance

**Integration Script**:
```python
from dataset_integration import DatasetIntegrator

integrator = DatasetIntegrator()
integrator.integrate_dataset("docvqa")
```

### 2. PubLayNet (PubMed Central Layout Analysis)

**Source**: https://github.com/ibm-aur-nlp/PubLayNet

**Description**: Large-scale dataset for document layout analysis, containing 360,000+ PubMed Central article pages with annotated layout elements.

**Key Features**:
- 5 layout categories: text, title, list, table, figure
- High-quality annotations with bounding boxes
- Scientific document layouts
- 600+ DPI page images

**Use Cases**:
- Training and testing layout analysis models
- Document structure understanding
- Scientific document processing
- Information extraction from research papers

**Integration Script**:
```python
from dataset_integration import DatasetIntegrator

integrator = DatasetIntegrator()
integrator.integrate_dataset("publaynet")
```

### 3. LAION-400M (Large-scale Image-Text Dataset)

**Source**: https://laion.ai/blog/laion-400-open-dataset/

**Description**: Dataset containing 400 million image-text pairs crawled from the web, with CLIP-computed similarity scores.

**Key Features**:
- 400+ million image-text pairs
- CLIP similarity scores for quality filtering
- Multiple languages (predominantly English)
- Alt-text and caption-based descriptions

**Use Cases**:
- Training multimodal models
- Image-text retrieval evaluation
- Visual question answering
- Cross-modal understanding

**⚠️ Important Note**: The full LAION-400M dataset is extremely large (~10TB). Our integration uses a representative sample (100 items) for demonstration purposes.

**Integration Script**:
```python
from dataset_integration import DatasetIntegrator

integrator = DatasetIntegrator()
integrator.integrate_dataset("laion")
```

## Step-by-Step Guide

### Step 1: Environment Setup

```bash
# Ensure RAG system is running
docker-compose up -d

# Wait for services to be ready (20-30 seconds)
docker-compose ps

# Check system health
curl http://localhost:8000/health
```

### Step 2: Dataset Integration (Download + Processing)

```bash
# Download and process all datasets
python scripts/dataset_integration.py

# Or process specific datasets
python scripts/dataset_integration.py << EOF
1  # DocVQA
EOF
```

**What happens during integration**:
1. **Download**: Fetches datasets from official sources or creates samples
2. **Extract**: Processes dataset formats (JSON, images, annotations)
3. **Transform**: Converts to RAG system-compatible format
4. **Validate**: Ensures data quality and completeness

**Expected Output**:
```
🚀 Starting Dataset Integration for RAG System
============================================================
Available datasets:
1. docvqa - Document Visual Question Answering dataset
2. publaynet - PubMed Central layout analysis dataset
3. laion - Large-scale image-text dataset (sample version)
4. All datasets

Downloading docvqa_sample.json... Progress: 100.0%
Downloaded docvqa_sample.json successfully
Processed 50 DocVQA items

✅ SUCCESS: docvqa
  Description: Document Visual Question Answering dataset
  Target count: 100
```

### Step 3: Dataset Upload to RAG System

```bash
# Upload processed datasets to RAG system
python scripts/dataset_upload_api.py
```

**What happens during upload**:
1. **Authentication**: Logs into RAG system API
2. **Batch Processing**: Uploads documents in configurable batches
3. **Metadata Enrichment**: Adds source, tags, and processing metadata
4. **Error Handling**: Retries failed uploads with exponential backoff
5. **Progress Tracking**: Provides real-time upload statistics

**Expected Output**:
```
📤 Dataset Upload API Client
========================================

🔐 Authenticating...
✅ Authentication successful

📤 Uploading datasets...
Uploading batch 1/5 for docvqa
Uploading batch 2/5 for docvqa
...
Upload completed: 50/50 successful (100.0%)

Dataset Upload Report
==================================================
Overall Statistics:
  Total Datasets: 3
  Total Documents: 150
  Successful Uploads: 150
  Failed Uploads: 0
  Overall Success Rate: 100.0%

DOCVQA:
  Status: ✅ SUCCESS
  Documents: 50/50
  Success Rate: 100.0%
  Total Time: 12.45s
  Avg Time/Doc: 0.249s
```

### Step 4: System Evaluation

```bash
# Run comprehensive evaluation
python scripts/evaluation_framework.py
```

**What happens during evaluation**:
1. **Query Generation**: Creates test queries from dataset content
2. **Query Execution**: Runs queries against RAG system
3. **Metric Calculation**: Computes RAG Triad metrics (Answer Relevancy, Faithfulness, Context Relevancy)
4. **Performance Analysis**: Measures response times and success rates
5. **Visualization**: Creates charts and plots for performance analysis

**Evaluation Metrics**:
- **Answer Relevancy** (>70% threshold): How relevant the answer is to the query
- **Faithfulness** (>90% threshold): Factual consistency with retrieved context
- **Context Relevancy** (>70% threshold): Quality of retrieved context
- **Accuracy** (>80% threshold): Exact match for factual queries
- **Response Time** (<2000ms threshold): Query processing speed

**Expected Output**:
```
🔍 RAG System Evaluation Framework
============================================================

🔐 Authenticating with RAG system...
✅ Authentication successful

📊 Running evaluations on all datasets...
Evaluated 50/50 queries
Completed evaluation for docvqa

RAG System Evaluation Summary Report
============================================================
Overall Statistics:
  Datasets Evaluated: 3
  Total Queries: 150
  Successful Queries: 145
  Overall Success Rate: 96.7%

Performance by Dataset:
----------------------------------------

DOCVQA:
  Queries: 48/50
  Avg Response Time: 1245.67ms
  Answer Relevancy: 0.856 ✅
  Faithfulness: 0.923 ✅
  Context Relevancy: 0.789 ✅
  Accuracy: 0.820 ✅
  Response Time: 1245.670ms ✅
```

### Step 5: Complete Pipeline (All-in-One)

```bash
# Run everything: integration → upload → evaluation
python scripts/run_complete_pipeline.py

# Or specify API URL if different
python scripts/run_complete_pipeline.py --api-url http://localhost:8000
```

**Complete Pipeline Output**:
```
🚀 Starting Complete Dataset Pipeline
================================================================================

🔍 Phase 0: Checking System Health
✅ RAG system is healthy

📥 Phase 1: Dataset Integration
🚀 Starting Dataset Integration for RAG System
============================================================
...

📤 Phase 2: Dataset Upload
📤 Starting Dataset Upload Phase
============================================================
...

🔍 Phase 3: Evaluation
🔍 Starting Evaluation Phase
============================================================
...

🎉 COMPLETE PIPELINE FINISHED
================================================================================
Total Duration: 0:05:23.456789
Final Report: pipeline/final_report.txt
JSON Results: pipeline/results.json
================================================================================
```

## Generated Files and Reports

After running the pipeline, you'll find these files:

### Reports
- `pipeline/final_report.txt` - Comprehensive summary of all phases
- `pipeline/integration_report.txt` - Dataset integration details
- `pipeline/upload_report.txt` - Upload statistics and results
- `pipeline/evaluation_summary.txt` - Performance evaluation summary
- `pipeline/results.json` - Machine-readable results in JSON format

### Evaluations
- `evaluation/reports/{dataset}_evaluation_report.txt` - Per-dataset evaluation
- `evaluation/plots/{dataset}_*.png` - Performance visualizations
- `evaluation/summary_report.txt` - Overall evaluation summary

### Logs
- `pipeline.log` - Detailed execution log with timestamps

## Custom Dataset Integration

To integrate your own datasets:

1. **Prepare dataset in JSON format**:
```json
{
  "data": [
    {
      "title": "Document Title",
      "content": "Document content...",
      "metadata": {
        "source": "custom",
        "tags": ["tag1", "tag2"],
        "custom_field": "value"
      }
    }
  ]
}
```

2. **Use the integration script**:
```python
from dataset_integration import DatasetIntegrator

# Add custom dataset configuration
integrator = DatasetIntegrator()
integrator.configs["custom"] = DatasetConfig(
    name="Custom Dataset",
    source_url="file:///path/to/dataset",
    dataset_type="custom",
    file_format="json",
    processing_func="process_custom",
    target_count=1000,
    description="Custom dataset for specific domain"
)
```

3. **Upload and evaluate** using the same pipeline

## Troubleshooting

### Common Issues

1. **RAG System Not Running**:
   ```bash
   docker-compose up -d
   # Wait 20-30 seconds for services to start
   curl http://localhost:8000/health
   ```

2. **Authentication Failures**:
   - Check if user exists: `curl http://localhost:8000/auth/login -d '{"username":"admin","password":"admin123"}'`
   - Create admin user if needed

3. **Memory Issues with Large Datasets**:
   - Reduce batch size in `UploadConfig`
   - Increase Docker memory limits
   - Process datasets in smaller chunks

4. **Download Failures**:
   - Check internet connection
   - Verify dataset URLs are accessible
   - Use smaller sample datasets for testing

5. **Evaluation Timeouts**:
   - Increase timeout in `EvaluationRunner`
   - Reduce number of test queries
   - Check system resources

### Performance Optimization

1. **Parallel Processing**:
   ```python
   config = UploadConfig(
       batch_size=20,  # Increase batch size
       max_concurrent_uploads=10  # More parallel uploads
   )
   ```

2. **Caching**:
   - Enable Redis caching for frequently accessed documents
   - Use vector store caching for similarity search

3. **Resource Allocation**:
   ```bash
   # Allocate more memory to Docker
   docker-compose up -d --scale qdrant=1 --scale neo4j=1
   ```

## API Reference

### Dataset Integration API

```python
from dataset_integration import DatasetIntegrator

integrator = DatasetIntegrator(api_base_url="http://localhost:8000")

# Integrate specific dataset
result = integrator.integrate_dataset("docvqa")

# Integrate all datasets
results = integrator.integrate_all_datasets()
```

### Upload API

```python
from dataset_upload_api import DatasetUploader, UploadConfig

config = UploadConfig(batch_size=10, max_retries=3)

async with DatasetUploader(config=config) as uploader:
    await uploader.authenticate()
    result = await uploader.upload_dataset(
        Path("datasets/downloads/docvqa_sample.json"),
        "docvqa"
    )
```

### Evaluation API

```python
from evaluation_framework import EvaluationRunner

runner = EvaluationRunner(api_base_url="http://localhost:8000")
results = runner.run_all_evaluations()
```

## Next Steps

1. **Experiment with Different Datasets**: Try other multimodal datasets
2. **Fine-tune Evaluation Metrics**: Adjust thresholds based on your use case
3. **Scale Up**: Use larger dataset subsets for production testing
4. **Custom Processing**: Implement domain-specific data processing
5. **Continuous Evaluation**: Set up automated evaluation pipelines

## Support

For issues and questions:
- Check the execution logs in `pipeline.log`
- Review generated reports for detailed error messages
- Ensure all prerequisites are met before running the pipeline
- Start with smaller datasets to test the system before scaling up

---

**Note**: This implementation uses sample datasets for demonstration. For production use, download the full datasets from the official sources and adjust the processing logic accordingly.
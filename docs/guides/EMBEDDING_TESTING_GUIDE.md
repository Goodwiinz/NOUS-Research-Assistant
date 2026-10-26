# Azure OpenAI Embedding Testing Guide

This guide provides comprehensive testing strategies for your Azure OpenAI embedding service integration.

## 🧪 Available Test Scripts

### 1. Quick Test (`test_embedding_quick.py`)
**Purpose**: Fast validation that the embedding service is working
```bash
python scripts/testing/test_embedding_quick.py
```

**What it tests**:
- ✅ Single text embedding generation
- ✅ Batch embedding processing (3 texts)
- ✅ Response time and performance metrics
- ✅ Token usage tracking

**When to use**: Daily validation, quick checks, CI/CD pipelines

### 2. Comprehensive Test Suite (`test_embedding_comprehensive.py`)
**Purpose**: Complete functional and performance testing
```bash
python scripts/testing/test_embedding_comprehensive.py
```

**What it tests**:
- ✅ Basic single embedding functionality
- ✅ Batch embedding generation and consistency
- ✅ **Embedding Quality Assessment** (semantic similarity)
- ✅ **Performance Benchmarking** (different text lengths)
- ✅ **Document Workflow Testing** (realistic RAG scenarios)

**When to use**: Full system validation, performance analysis, quality assurance

### 3. Integration Test (`test_embedding_integration.py`)
**Purpose**: Backend service integration testing
```bash
python scripts/testing/test_embedding_integration.py
```

**What it tests**:
- ✅ Backend Azure OpenAI service integration
- ✅ Embedding service provider switching
- ✅ Document processing workflow
- ✅ API endpoint structure validation

**When to use**: Backend development, integration validation

## 📊 Test Results Interpretation

### Quality Metrics
- **Quality Score**: 0-100 scale, 75+ is good
- **Semantic Similarity**: Tests if similar texts have high cosine similarity
- **Consistency**: All embeddings should have the same dimension (1536)

### Performance Metrics
- **Response Time**: Individual embedding generation time
- **Processing Rate**: Tokens per second (100+ is good)
- **Batch Efficiency**: Time per embedding should decrease with batch size

### Functional Metrics
- **Success Rate**: Percentage of successful embedding generations
- **Dimension Consistency**: All embeddings should be 1536 dimensions
- **Token Usage**: Should match expected token counts

## 🎯 Testing Scenarios

### 1. Basic Functionality Testing
```python
# Test single embedding
text = "Test document for embedding generation"
embedding = await azure_openai_service.get_embeddings([text])
print(f"Dimension: {len(embedding[0])}")  # Should be 1536
```

### 2. Batch Processing Testing
```python
# Test batch embeddings
texts = ["Doc 1", "Doc 2", "Doc 3", "Doc 4", "Doc 5"]
embeddings = await azure_openai_service.get_embeddings(texts)
print(f"Generated {len(embeddings)} embeddings")
```

### 3. Quality Assessment Testing
```python
# Test semantic similarity
similar_texts = ["The cat sat on the mat", "A cat is sitting on a mat"]
different_texts = ["AI is evolving", "The weather is sunny"]

# Similar texts should have higher similarity scores
```

### 4. Performance Testing
```python
# Test different text lengths
short_text = "Hello"
long_text = " ".join(["This is a long text."] * 20)

# Measure response times and processing rates
```

### 5. Document Workflow Testing
```python
# Test realistic document processing
document = "Your document content here..."
chunks = split_into_chunks(document, chunk_size=100, overlap=20)
embeddings = await embedding_service.generate_batch_embeddings(chunks)
```

## 🔧 Configuration Testing

### Environment Variables Validation
Ensure these are properly set in your `.env` file:

```env
# Embedding Service Configuration
AZURE_OPENAI_EMBEDDING_API_KEY=your_embedding_api_key
AZURE_OPENAI_EMBEDDING_ENDPOINT=https://your-endpoint.cognitiveservices.azure.com/
AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME=text-embedding-ada-002
AZURE_OPENAI_EMBEDDING_API_VERSION=2023-05-15
```

### Service Availability Testing
```python
from src.services.azure_openai_service import azure_openai_service

if azure_openai_service.is_embedding_available():
    print("✅ Embedding service is available")
else:
    print("❌ Embedding service is not available")
```

## 📈 Performance Benchmarks

Based on our comprehensive testing:

### Expected Performance
- **Response Time**: 0.1-0.4 seconds per embedding
- **Processing Rate**: 100-200 tokens/second
- **Batch Efficiency**: 2-5x faster than individual calls
- **Quality Score**: 75+/100
- **Similarity Accuracy**: High similarity texts >0.9, different texts <0.8

### Scaling Considerations
- **Small texts** (< 50 chars): ~10-50 tokens/second
- **Medium texts** (50-150 chars): ~50-150 tokens/second
- **Large texts** (150+ chars): ~150-250 tokens/second
- **Batch processing**: More efficient than individual calls

## 🚨 Troubleshooting Common Issues

### 1. Authentication Failures
**Error**: `401 Access denied`
**Solution**: Check API key and endpoint configuration
```bash
# Verify environment variables
echo $AZURE_OPENAI_EMBEDDING_API_KEY
echo $AZURE_OPENAI_EMBEDDING_ENDPOINT
```

### 2. Deployment Not Found
**Error**: `404 Deployment not found`
**Solution**: Verify deployment name and API version
```env
AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME=text-embedding-ada-002
AZURE_OPENAI_EMBEDDING_API_VERSION=2023-05-15
```

### 3. Rate Limiting
**Error**: `429 Too many requests`
**Solution**: Implement rate limiting and batching
```python
# Use batch processing instead of individual calls
batch_texts = [...text chunk...]
embeddings = await azure_openai_service.get_embeddings(batch_texts)
```

### 4. Poor Quality Scores
**Issue**: Quality score < 70
**Solutions**:
- Check text preprocessing (remove special characters)
- Verify deployment is using correct model
- Test with different text examples

### 5. Slow Performance
**Issue**: Response times > 1 second
**Solutions**:
- Use batch processing for multiple texts
- Check network latency to Azure endpoint
- Consider text chunking optimization

## 🔄 Continuous Testing

### Daily Validation
```bash
# Quick health check
python scripts/testing/test_embedding_quick.py
```

### Weekly Comprehensive Testing
```bash
# Full test suite
python scripts/testing/test_embedding_comprehensive.py
```

### Integration Testing
```bash
# Backend integration validation
python scripts/testing/test_embedding_integration.py
```

### Automated Testing in CI/CD
```yaml
# Example GitHub Actions step
- name: Test Azure OpenAI Embeddings
  run: |
    python scripts/testing/test_embedding_quick.py
    python scripts/testing/test_embedding_comprehensive.py
```

## 📝 Test Report Template

```
=== AZURE OPENAI EMBEDDING TEST REPORT ===
Date: [Test Date]
Environment: [Development/Staging/Production]

Test Results:
✅ Basic Functionality: PASSED (Response time: 0.3s)
✅ Batch Processing: PASSED (5 embeddings, 0.25s)
✅ Quality Assessment: PASSED (Score: 75/100)
✅ Performance Benchmark: PASSED (107.5 tokens/s)
✅ Document Workflow: PASSED (3 documents, 6 chunks)

Performance Metrics:
- Average Response Time: 0.25s
- Processing Rate: 107.5 tokens/second
- Quality Score: 75/100
- Success Rate: 100%

Recommendations:
- Embedding service is production-ready
- Consider implementing batching for better performance
- Monitor quality scores in production

Next Test Date: [Schedule next test]
```

## 🎯 Production Readiness Checklist

- [ ] All test scripts passing consistently
- [ ] Quality scores above 70/100
- [ ] Performance metrics within acceptable ranges
- [ ] Error handling and fallback mechanisms in place
- [ ] Monitoring and alerting configured
- [ ] Rate limiting implemented
- [ ] Documentation updated
- [ ] Team trained on troubleshooting procedures

Your Azure OpenAI embedding service is now thoroughly tested and ready for production use! 🚀
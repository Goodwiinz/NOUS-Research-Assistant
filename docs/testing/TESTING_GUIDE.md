# Comprehensive Testing Guide for Multimodal Enterprise RAG Platform

> **Stale (2026-10-17): this guide predates the current test layout and qdrant is fully removed and live retrieval is PostgreSQL full-text search, so statements about Qdrant or vector stores below are historical. Many test paths it cites (for example `src/tests/...`) do not exist, and frontend commands use `npm`; the repository uses `pnpm`. For current gates and commands use the [testing contract](../engineering/testing.md).**

## Overview

This guide provides detailed instructions for testing all features of your multimodal RAG platform, including unit tests, integration tests, end-to-end tests, and manual testing procedures.

## 🏗️ Platform Architecture

Your RAG platform consists of:

### Core Components
- **Backend API** (FastAPI, Python) - Port 8000
- **Frontend** (React, TypeScript) - Port 3000
- **PostgreSQL** - Primary database (Port 5432)
- **Redis** - Caching and session storage (Port 6379)
- **Neo4j** - Knowledge graph (Ports 7474/7687)
- **Qdrant** - Vector database (Ports 6333/6334)
- **Celery** - Background task processing

### Implemented Features
1. **T3 Analytics System** - Quality metrics, performance dashboards, user behavior analytics
2. **T4 Enterprise Security** - Multi-tenancy, RBAC, audit logging, encryption
3. **Multimodal Document Processing** - Text, image, audio, video ingestion
4. **Hybrid Search** - Vector + semantic + knowledge graph search
5. **Real-time Quality Evaluation** - Automated quality metrics and recommendations

---

## 🚀 Quick Start Testing

### 1. Start the Platform

```bash
# Start all services
docker-compose up -d

# Check service status
docker-compose ps

# View logs
docker-compose logs -f backend
```

### 2. Verify Services are Running

```bash
# Test backend API
curl http://localhost:8000/health

# Test frontend
open http://localhost:3000

# Test database connections
docker-compose exec backend python -c "from src.core.database import get_db; print('Database OK')"
```

### 3. Run Automated Tests

```bash
# Run all tests
docker-compose exec backend python -m pytest src/tests/ -v

# Run specific test categories
docker-compose exec backend python -m pytest src/tests/unit/ -v
docker-compose exec backend python -m pytest src/tests/integration/ -v
docker-compose exec backend python -m pytest src/tests/test_encryption_service.py -v
```

---

## 🧪 Automated Testing

### Unit Tests

Unit tests focus on individual components and functions.

#### Running Unit Tests

```bash
# Run all unit tests
docker-compose exec backend python -m pytest src/tests/unit/ -v --tb=short

# Run tests with coverage
docker-compose exec backend python -m pytest src/tests/unit/ --cov=src --cov-report=html

# Run specific test file
docker-compose exec backend python -m pytest src/tests/unit/test_user_service.py -v
```

#### Key Unit Test Categories

1. **Authentication & Authorization**
   ```bash
   docker-compose exec backend python -m pytest src/tests/unit/test_auth.py -v
   ```

2. **Encryption Service**
   ```bash
   docker-compose exec backend python -m pytest src/tests/test_encryption_service.py -v
   ```

3. **Analytics Services**
   ```bash
   docker-compose exec backend python -m pytest src/tests/unit/test_analytics_services.py -v
   ```

4. **Document Processing**
   ```bash
   docker-compose exec backend python -m pytest src/tests/unit/test_processing_service.py -v
   ```

### Integration Tests

Integration tests verify that different components work together correctly.

#### Running Integration Tests

```bash
# Run all integration tests
docker-compose exec backend python -m pytest src/tests/integration/ -v

# Run API integration tests
docker-compose exec backend python -m pytest src/tests/integration/test_api_integration.py -v
```

#### Key Integration Test Categories

1. **Database Integration**
   ```bash
   docker-compose exec backend python -m pytest src/tests/integration/test_database_integration.py -v
   ```

2. **External Service Integration**
   ```bash
   docker-compose exec backend python -m pytest src/tests/integration/test_external_services.py -v
   ```

3. **Cache Integration**
   ```bash
   docker-compose exec backend python -m pytest src/tests/integration/test_cache_integration.py -v
   ```

### Performance Tests

```bash
# Run performance tests
docker-compose exec backend python -m pytest src/tests/performance/ -v

# Load testing with locust (if installed)
docker-compose exec backend locust -f src/tests/performance/locustfile.py
```

---

## 🔧 Manual Testing Procedures

### 1. Authentication & User Management

#### Test User Registration
```bash
# Test API endpoint
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "SecurePass123!",
    "first_name": "Test",
    "last_name": "User",
    "organization_name": "Test Org"
  }'
```

#### Test User Login
```bash
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "SecurePass123!"
  }'
```

#### Test Role-Based Access Control (RBAC)

1. **Admin User Tests**:
   - Can access all endpoints
   - Can manage users and organizations
   - Can view audit logs

2. **Content Manager Tests**:
   - Can upload and manage documents
   - Can view analytics
   - Cannot manage users

3. **Regular User Tests**:
   - Can search documents
   - Can upload own documents
   - Limited access to analytics

### 2. Document Upload & Processing

#### Supported File Types
- **Text**: .txt, .pdf, .doc, .docx
- **Images**: .jpg, .jpeg, .png, .gif, .bmp
- **Audio**: .mp3, .wav, .flac, .aac
- **Video**: .mp4, .avi, .mov, .wmv

#### Test Document Upload

```bash
# Upload a PDF file
curl -X POST http://localhost:8000/api/documents/upload \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -F "file=@test_document.pdf" \
  -F "title=Test Document" \
  -F "description=This is a test document"
```

#### Test Processing Status

```bash
# Check processing status
curl -X GET http://localhost:8000/api/processing/status/{document_id} \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

#### Test Multimodal Processing

1. **PDF with OCR**: Upload a scanned PDF and verify text extraction
2. **Image Analysis**: Upload images and verify object/text detection
3. **Audio Transcription**: Upload audio files and verify transcription
4. **Video Processing**: Upload videos and verify transcription + frame analysis

### 3. Search Functionality

#### Basic Text Search
```bash
curl -X POST http://localhost:8000/api/search/hybrid \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "machine learning algorithms",
    "limit": 10,
    "filters": {
      "file_types": ["pdf", "txt"],
      "date_range": {
        "start": "2024-01-01",
        "end": "2024-12-31"
      }
    }
  }'
```

#### Cross-Modal Search
```bash
# Search for content mentioned in videos but found in text documents
curl -X POST http://localhost:8000/api/search/cross-modal \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "quarterly financial results",
    "source_modalities": ["video"],
    "target_modalities": ["text", "pdf"],
    "limit": 5
  }'
```

#### Knowledge Graph Search
```bash
curl -X POST http://localhost:8000/api/search/knowledge-graph \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "entity": "Artificial Intelligence",
    "relationship_types": ["mentions", "relates_to", "examples_of"],
    "depth": 2,
    "limit": 20
  }'
```

### 4. Analytics & Quality Metrics

#### Test Quality Metrics Dashboard
```bash
# Get quality metrics for a search
curl -X GET http://localhost:8000/api/analytics/quality/search/{search_id} \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

#### Test Performance Dashboard
```bash
# Get system performance metrics
curl -X GET http://localhost:8000/api/analytics/performance/dashboard \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

#### Test User Behavior Analytics
```bash
# Get user behavior insights
curl -X GET http://localhost:8000/api/analytics/user-behavior/insights \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

### 5. Security Features

#### Test Encryption Service
```bash
# Encrypt user profile data
curl -X POST http://localhost:8000/api/encryption/profiles/user \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "USER_UUID",
    "profile_data": {
      "first_name": "John",
      "last_name": "Doe",
      "email_personal": "john@example.com",
      "phone_mobile": "+1234567890"
    }
  }'
```

#### Test Audit Logging
```bash
# Get audit logs
curl -X GET http://localhost:8000/api/compliance/audit/logs \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -G -d "limit=50" -d "operation_type=DOCUMENT_ACCESS"
```

#### Test Multi-Tenancy
1. Create users in different organizations
2. Verify data isolation between organizations
3. Test cross-organization access restrictions

### 6. Real-time Features

#### Test WebSocket Connections
```javascript
// Connect to WebSocket for real-time updates
const ws = new WebSocket('ws://localhost:8000/ws/updates');

ws.onmessage = function(event) {
    const data = JSON.parse(event.data);
    console.log('Real-time update:', data);
};
```

#### Test Live Processing Updates
1. Upload a large document
2. Monitor processing progress via WebSocket
3. Verify real-time status updates

---

## 📊 Test Data Setup

### Create Test Documents

```bash
# Create test directory
mkdir -p test_documents

# Download sample documents
wget -O test_documents/sample.pdf https://arxiv.org/pdf/2301.07041.pdf
wget -O test_documents/sample.txt https://www.gutenberg.org/files/11/11-0.txt

# Create test images (requires ImageMagick)
convert -size 800x600 xc:blue test_documents/test_image.jpg
convert -size 800x600 -pointsize 72 -fill white -gravity center \
  -annotate +0+0 "Test Document" test_documents/test_image_with_text.jpg

# Create test audio (requires ffmpeg)
ffmpeg -f lavfi -i anoise=white=0.1 -t 10 test_documents/test_audio.wav

# Create test video (requires ffmpeg)
ffmpeg -f lavfi -i testsrc=duration=10:size=320x240:rate=30 \
  -f lavfi -i anoise=white=0.1 -t 10 test_documents/test_video.mp4
```

### Database Test Data

```bash
# Populate with test data
docker-compose exec backend python -c "
from src.core.database import get_db
from src.models.user import User, Organization
from src.models.document import Document
import uuid
from datetime import datetime

db = next(get_db())

# Create test organization
org = Organization(
    name='Test Organization',
    storage_tier='free',
    storage_limit_bytes=1000000000  # 1GB
)
db.add(org)
db.commit()

# Create test users
user = User(
    email='test@example.com',
    password_hash='hashed_password',
    first_name='Test',
    last_name='User',
    organization_id=org.id,
    role='user'
)
db.add(user)
db.commit()

print('Test data created successfully')
"
```

---

## 🔍 Frontend Testing

### Manual Frontend Tests

1. **User Interface Tests**:
   - Login/logout flow
   - Document upload interface
   - Search interface
   - Analytics dashboards
   - User profile management

2. **Responsive Design Tests**:
   - Desktop (1920x1080)
   - Tablet (768x1024)
   - Mobile (375x667)

3. **Browser Compatibility**:
   - Chrome (latest)
   - Firefox (latest)
   - Safari (latest)
   - Edge (latest)

### Automated Frontend Tests

```bash
# Install frontend dependencies
cd frontend && npm install

# Run unit tests
npm test

# Run E2E tests (if Cypress is installed)
npm run test:e2e

# Run accessibility tests
npm run test:a11y
```

---

## 📈 Performance Testing

### Load Testing

```bash
# Install locust
pip install locust

# Create load test script (locustfile.py)
cat > locustfile.py << 'EOF'
from locust import HttpUser, task, between

class RAGUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        # Login and get token
        response = self.client.post("/api/auth/login", json={
            "email": "test@example.com",
            "password": "SecurePass123!"
        })
        if response.status_code == 200:
            self.token = response.json()["access_token"]
            self.headers = {"Authorization": f"Bearer {self.token}"}

    @task(3)
    def search_documents(self):
        self.client.post("/api/search/hybrid",
                        headers=self.headers,
                        json={"query": "test search", "limit": 10})

    @task(1)
    def get_analytics(self):
        self.client.get("/api/analytics/performance/dashboard",
                       headers=self.headers)

    @task(1)
    def upload_document(self):
        # Test with small text file
        files = {"file": ("test.txt", "Test content for upload", "text/plain")}
        data = {"title": "Test Upload", "description": "Performance test"}
        self.client.post("/api/documents/upload",
                        headers=self.headers,
                        files=files, data=data)
EOF

# Run load test
locust -f locustfile.py --host http://localhost:8000
```

### Stress Testing

```bash
# Test concurrent users
docker-compose exec backend python -c "
import asyncio
import aiohttp
import json

async def stress_test():
    async with aiohttp.ClientSession() as session:
        tasks = []
        for i in range(100):
            task = asyncio.create_task(test_search(session, i))
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)
        success_count = sum(1 for r in results if not isinstance(r, Exception))
        print(f'Successful requests: {success_count}/100')

async def test_search(session, user_id):
    async with session.post('http://localhost:8000/api/search/hybrid',
                           json={'query': f'test query {user_id}', 'limit': 5}) as response:
        return response.status

asyncio.run(stress_test())
"
```

---

## 🔒 Security Testing

### Authentication Security Tests

```bash
# Test SQL injection in login
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "admin'; DROP TABLE users; --",
    "password": "password"
  }'

# Test rate limiting
for i in {1..100}; do
  curl -X POST http://localhost:8000/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"email": "test@example.com", "password": "wrong"}' &
done
wait

# Test XSS in search
curl -X POST http://localhost:8000/api/search/hybrid \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "<script>alert(1)</script>", "limit": 10}'
```

### Data Encryption Tests

```bash
# Verify encrypted data in database
docker-compose exec postgres psql -U postgres -d multimodal_rag -c "
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('encrypted_user_profiles', 'encrypted_organization_profiles');
"

# Test field encryption
docker-compose exec backend python -c "
from src.core.encryption import get_field_encryption
field_enc = get_field_encryption()

# Test encryption/decryption
original = 'john.doe@example.com'
encrypted = field_enc.encrypt_field(original, 'email')
decrypted = field_enc.decrypt_field(encrypted, 'email')

print(f'Original: {original}')
print(f'Encrypted: {encrypted}')
print(f'Decrypted: {decrypted}')
print(f'Success: {original == decrypted}')
"
```

---

## 🐛 Debugging & Troubleshooting

### Common Issues and Solutions

#### 1. Database Connection Issues
```bash
# Check PostgreSQL status
docker-compose exec postgres pg_isready

# View database logs
docker-compose logs postgres

# Reset database (WARNING: Deletes all data)
docker-compose down -v
docker-compose up -d postgres
```

#### 2. Redis Connection Issues
```bash
# Check Redis status
docker-compose exec redis redis-cli ping

# View Redis logs
docker-compose logs redis

# Clear Redis cache
docker-compose exec redis redis-cli FLUSHALL
```

#### 3. Processing Job Failures
```bash
# Check Celery worker status
docker-compose exec celery-worker celery -A src.tasks.processing_tasks inspect active

# View worker logs
docker-compose logs celery-worker

# Retry failed jobs
docker-compose exec backend python -c "
from src.core.database import get_db
from src.models.processing import ProcessingJob

db = next(get_db())
failed_jobs = db.query(ProcessingJob).filter(ProcessingJob.status == 'failed').all()

for job in failed_jobs:
    job.status = 'pending'
    job.retry_count = 0
    job.error_message = None

db.commit()
print(f'Reset {len(failed_jobs)} failed jobs')
"
```

#### 4. Search Performance Issues
```bash
# Check Qdrant status
curl http://localhost:6333/health

# Rebuild vector index
docker-compose exec backend python -c "
from src.services.vector_service import VectorService
vs = VectorService()
vs.rebuild_index()
print('Vector index rebuilt')
"
```

### Debug Mode

```bash
# Run backend in debug mode
docker-compose -f docker-compose.yml -f docker-compose.debug.yml up backend

# Enable detailed logging
docker-compose exec backend python -c "
import logging
logging.basicConfig(level=logging.DEBUG)
print('Debug logging enabled')
"
```

---

## 📋 Testing Checklist

### Pre-Deployment Checklist

- [ ] All unit tests pass
- [ ] All integration tests pass
- [ ] Security tests pass
- [ ] Performance tests meet requirements
- [ ] Frontend tests pass on all browsers
- [ ] Mobile responsive design verified
- [ ] Accessibility tests pass
- [ ] Documentation is up to date
- [ ] Environment variables configured
- [ ] Database migrations applied
- [ ] SSL certificates configured
- [ ] Monitoring and alerting configured

### Feature-Specific Checklists

#### Authentication & Authorization
- [ ] User registration works
- [ ] Email verification works
- [ ] Password reset works
- [ ] JWT token handling works
- [ ] Role-based permissions work
- [ ] Session management works
- [ ] Rate limiting works
- [ ] Account lockout works

#### Document Processing
- [ ] File upload works for all supported types
- [ ] File size limits enforced
- [ ] Virus scanning works
- [ ] OCR processing works
- [ ] Text extraction works
- [ ] Metadata extraction works
- [ ] Background processing works
- [ ] Error handling works

#### Search Functionality
- [ ] Text search works
- [ ] Vector search works
- [ ] Hybrid search works
- [ ] Cross-modal search works
- [ ] Knowledge graph search works
- [ ] Search ranking works
- [ ] Search filters work
- [ ] Search analytics work

#### Security Features
- [ ] Data encryption works
- [ ] Field-level encryption works
- [ ] Key rotation works
- [ ] Audit logging works
- [ ] Multi-tenancy works
- [ ] RBAC works
- [ ] Data masking works
- [ ] Compliance features work

#### Analytics & Monitoring
- [ ] Quality metrics work
- [ ] Performance dashboards work
- [ ] User behavior tracking works
- [ ] Real-time updates work
- [ ] Export functionality works
- [ ] Alerting works
- [ ] Data retention works

---

## 🚀 Continuous Testing

### CI/CD Pipeline

```yaml
# .github/workflows/test.yml
name: Test Suite

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest

    services:
      postgres:
        image: postgres:15
        env:
          POSTGRES_PASSWORD: postgres
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

      redis:
        image: redis:7
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

    steps:
    - uses: actions/checkout@v3

    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'

    - name: Install dependencies
      run: |
        cd backend
        pip install -r requirements.txt
        pip install pytest pytest-cov

    - name: Run unit tests
      run: |
        cd backend
        pytest src/tests/unit/ -v --cov=src --cov-report=xml

    - name: Run integration tests
      run: |
        cd backend
        pytest src/tests/integration/ -v

    - name: Run security tests
      run: |
        cd backend
        pytest src/tests/security/ -v

    - name: Upload coverage
      uses: codecov/codecov-action@v3
      with:
        file: backend/coverage.xml
```

### Automated Testing Schedule

```bash
# Run full test suite daily
0 2 * * * cd /path/to/project && docker-compose -f docker-compose.test.yml up --abort-on-container-exit

# Run performance tests weekly
0 3 * * 0 cd /path/to/project && docker-compose -f docker-compose.perf.yml up --abort-on-container-exit

# Run security scans on PR
# Configured in GitHub Actions
```

---

This comprehensive testing guide should help you thoroughly test all aspects of your multimodal RAG platform. Start with the basic functionality tests and gradually move to more complex scenarios and performance testing.
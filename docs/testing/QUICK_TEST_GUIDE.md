# Quick Video Processing Test Guide

> **Stale (2026-10-17): an undated quick-test note written for an earlier environment. Commands may no longer work. For current gates and commands use the [testing contract](../engineering/testing.md).**

## ✅ Current Status

All systems are **OPERATIONAL**:
- ✅ Backend API running on port 8000
- ✅ All Docker containers healthy
- ✅ Video processing service ready
- ✅ Database connections established

## 🎥 Test Video Processing in 3 Steps

### Step 1: Register a User

```bash
curl -X POST "http://localhost:8000/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "TestPassword123!",
    "full_name": "Test User",
    "organization_name": "Test Org"
  }'
```

### Step 2: Login and Get Token

```bash
TOKEN=$(curl -s -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=test@example.com&password=TestPassword123!" | \
  python3 -c 'import sys, json; print(json.load(sys.stdin)["access_token"])')

echo "Token: $TOKEN"
```

### Step 3: Upload and Process Video

```bash
# Upload video
curl -X POST "http://localhost:8000/api/v1/files/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/your/video.mp4" \
  -F "title=My Test Video" \
  -F "description=Testing the video processing pipeline" | python3 -m json.tool

# Get the FILE_ID from the response above, then check status:
FILE_ID="your-file-id-here"

# Monitor processing
curl "http://localhost:8000/api/v1/files/$FILE_ID" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

## 🚀 Using Test Scripts

### Option 1: Shell Script (Automated)
```bash
cd /Users/goodwiinz/development/RAG_system/rag
./test_video_api.sh /path/to/video.mp4
```

### Option 2: Python Script (Detailed)
```bash
cd /Users/goodwiinz/development/RAG_system/rag/backend
pip3 install requests  # Only needed once
python3 test_video_integration.py /path/to/video.mp4
```

## 📊 View API Documentation

Open in your browser:
- http://localhost:8000/docs (Swagger UI)
- http://localhost:8000/redoc (ReDoc)

## 🐛 Troubleshooting

### Check if services are running:
```bash
docker-compose ps
```

### View backend logs:
```bash
docker logs rag-backend-1 -f
```

### View celery worker logs:
```bash
docker logs rag-celery-worker-1 -f
```

### Restart services:
```bash
docker-compose restart backend celery-worker
```

## 📝 Supported Video Formats

- MP4 (.mp4)
- AVI (.avi)
- MOV (.mov)
- MKV (.mkv)
- WebM (.webm)
- And more!

## 🎯 What Gets Processed

When you upload a video, the system will:
1. ✅ Extract metadata (duration, resolution, codecs)
2. ✅ Analyze video content (quality, type, category)
3. ✅ Extract audio and transcribe speech (Whisper)
4. ✅ Extract keyframes for visual analysis
5. ✅ Generate searchable content
6. ✅ Create quality score

## 📖 Full Documentation

See [VIDEO_PROCESSING_TEST_RESULTS.md](./VIDEO_PROCESSING_TEST_RESULTS.md) for complete details.

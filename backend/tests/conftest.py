"""
Unified Pytest Configuration for Backend Tests

This file provides comprehensive test fixtures and configuration for:
- Database mocking with SQLite
- External service mocking (Qdrant, Neo4j, Redis, Celery)
- AI client mocking
- Authentication fixtures
- Performance tracking

Usage:
    All fixtures are automatically available to tests via pytest's fixture discovery.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator, Generator, List, Optional
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

# Add backend to path for imports
backend_dir = Path(__file__).parent.parent
tests_dir = Path(__file__).parent
sys.path.insert(0, str(backend_dir))
sys.path.insert(
    0, str(tests_dir.parent)
)  # Add parent of tests for 'tests.mocks' imports

# Set test environment variables before importing application code
os.environ.setdefault("ENVIRONMENT", "testing")
os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test_db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
# Required by create_cli_token() at /cli-auth/approve. The empty default
# refuses to mint, so tests that exercise that path need a real value here.
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32-characters")

# ============================================================================
# Test Auth Helper — Supabase-compatible JWT generation
# ============================================================================


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a Supabase-compatible HS256 JWT for testing.

    Replaces the removed src.core.security.create_access_token.
    Accepts the same {sub, email, organization_id, role} dict for backward compat.
    """
    from jose import jwt as jose_jwt

    if expires_delta is None:
        expires_delta = timedelta(hours=1)

    sub = data.get("sub", data.get("user_id", "test-user-id"))
    email = data.get("email", "test@test.com")
    role = data.get("role", "USER")

    payload = {
        "sub": sub,
        "email": email,
        "exp": datetime.utcnow() + expires_delta,
        "iat": datetime.utcnow(),
        "iss": "supabase",
        "role": "authenticated",
        "app_metadata": {"role": role},
    }

    secret = os.environ.get(
        "SUPABASE_JWT_SECRET", "super-secret-jwt-token-with-at-least-32-characters-long"
    )
    return jose_jwt.encode(payload, secret, algorithm="HS256")


# ============================================================================
# Pytest Configuration
# ============================================================================


def pytest_configure(config):
    """Configure custom pytest markers."""
    # Test type markers
    config.addinivalue_line("markers", "unit: Unit tests (fast, no external deps)")
    config.addinivalue_line(
        "markers", "integration: Integration tests (may use containers)"
    )
    config.addinivalue_line("markers", "e2e: End-to-end tests")
    config.addinivalue_line("markers", "performance: Performance tests")
    config.addinivalue_line(
        "markers",
        "resilience: Resilience pattern tests (retry, circuit breaker, bulkhead)",
    )
    config.addinivalue_line(
        "markers", "scalability: Scalability tests (caching, pooling)"
    )
    config.addinivalue_line(
        "markers", "regression: Regression tests for previously fixed bugs"
    )
    config.addinivalue_line("markers", "smoke: Quick smoke tests for basic validation")
    config.addinivalue_line("markers", "deepeval: DeepEval LLM-judge evaluation tests")
    config.addinivalue_line(
        "markers", "langsmith: LangSmith evaluation tests - require LANGCHAIN_API_KEY"
    )
    config.addinivalue_line(
        "markers", "eval: Slow evaluation benchmarks that run agent against dataset"
    )

    # Feature/domain markers
    config.addinivalue_line("markers", "ai: AI-specific tests (mocked or real)")
    config.addinivalue_line("markers", "slow: Tests that take longer to run")
    config.addinivalue_line("markers", "auth: Authentication tests")
    config.addinivalue_line("markers", "search: Search functionality tests")
    config.addinivalue_line("markers", "documents: Document processing tests")

    # External dependency markers (for testcontainers)
    config.addinivalue_line(
        "markers",
        "requires_postgres: Tests requiring real PostgreSQL (via testcontainers)",
    )
    config.addinivalue_line(
        "markers", "requires_redis: Tests requiring real Redis (via testcontainers)"
    )
    config.addinivalue_line(
        "markers", "requires_neo4j: Tests requiring real Neo4j (via testcontainers)"
    )


def pytest_addoption(parser):
    """Add custom pytest options."""
    parser.addoption(
        "--run-scaffolding",
        action="store_true",
        default=False,
        help="Run scaffolding tests (normally skipped)",
    )


def pytest_collection_modifyitems(config, items):
    """Automatically add markers based on file/class names."""
    skip_template = pytest.mark.skip(reason="Template test - not meant to be run")
    skip_standalone = pytest.mark.skip(
        reason="Standalone test - requires external services"
    )
    skip_performance = pytest.mark.skip(
        reason="Performance test - run separately with proper infra"
    )
    skip_scaffolding = pytest.mark.skip(
        reason="Scaffolding test - needs implementation fixes"
    )

    # Check if user wants to run scaffolding tests
    run_scaffolding = config.getoption("--run-scaffolding", default=False)
    run_performance = os.getenv("DAILY_BRIEF_PERF_CAPTURE") == "1"

    for item in items:
        # Add markers based on test file names
        test_path = str(item.fspath)

        # Search security suites exercise full endpoint behavior and externalized
        # security controls; treat them as integration tests so unit jobs stay stable.
        if "/tests/security/search_security/" in test_path or test_path.endswith(
            "/tests/security/test_search_security.py"
        ):
            item.add_marker(pytest.mark.integration)

        # Skip template, standalone, performance, and scaffolding tests if they somehow got collected
        if "/templates/" in test_path:
            item.add_marker(skip_template)
            continue
        if "/standalone/" in test_path:
            item.add_marker(skip_standalone)
            continue
        if "/performance/" in test_path:
            item.add_marker(pytest.mark.performance)
            if not run_performance:
                item.add_marker(skip_performance)
            continue
        if "/scaffolding/" in test_path and not run_scaffolding:
            item.add_marker(skip_scaffolding)
            continue

        if "test_auth" in test_path:
            item.add_marker(pytest.mark.auth)
        elif "test_search" in test_path:
            item.add_marker(pytest.mark.search)
        elif "test_document" in test_path:
            item.add_marker(pytest.mark.documents)

        # Stage markers come from the suite DIRECTORY, not a path substring.
        # Bare-substring matching tagged files like tests/unit/services/
        # test_agent_integration.py as `integration`, so the unit CI lane
        # (-m "unit or not (integration or e2e or slow)") silently skipped
        # them and no lane ever ran them.
        if "/tests/integration/" in test_path:
            item.add_marker(pytest.mark.integration)
        elif "/tests/performance/" in test_path:
            item.add_marker(pytest.mark.performance)
        elif "/tests/e2e/" in test_path:
            item.add_marker(pytest.mark.e2e)
        elif "/tests/unit/" in test_path:
            item.add_marker(pytest.mark.unit)
        elif "/tests/resilience/" in test_path:
            item.add_marker(pytest.mark.resilience)


# ============================================================================
# Event Loop Fixtures
# ============================================================================


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ============================================================================
# Database Fixtures
# ============================================================================


@pytest.fixture(scope="function")
def mock_db_engine():
    """Create an in-memory SQLite engine for testing."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    yield engine
    engine.dispose()


@pytest.fixture(scope="function")
def mock_db_session(mock_db_engine):
    """Create a database session for testing."""
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=mock_db_engine)
    session = SessionLocal()

    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ============================================================================
# External Service Mocks
# ============================================================================


@pytest.fixture
def mock_qdrant_client():
    """Mock Qdrant client for vector search tests."""
    import numpy as np

    client = Mock()
    client.upsert = Mock(return_value=Mock())
    client.search = Mock(return_value=[])
    client.create_collection = Mock(return_value=Mock())
    client.get_collection = Mock(return_value=Mock())
    client.delete_collection = Mock(return_value=Mock())
    client.count = Mock(return_value=Mock(count=0))
    client.scroll = Mock(return_value=([], None))

    return client


@pytest.fixture
def mock_neo4j_driver():
    """Mock Neo4j driver for knowledge graph tests."""
    driver = Mock()
    session = Mock()
    session.run = Mock(return_value=Mock())
    session.close = Mock(return_value=None)
    driver.session = Mock(return_value=session)
    driver.verify_connectivity = Mock(return_value=None)
    driver.close = Mock(return_value=None)

    return driver


@pytest.fixture
def mock_redis_client():
    """Mock Redis client for caching tests."""
    client = Mock()
    client.get = Mock(return_value=None)
    client.set = Mock(return_value=True)
    client.delete = Mock(return_value=True)
    client.exists = Mock(return_value=False)
    client.ping = Mock(return_value=True)
    client.expire = Mock(return_value=True)

    return client


@pytest.fixture
def mock_celery_app():
    """Mock Celery app for background task testing."""
    app = Mock()
    app.send_task = Mock(return_value=Mock(id="test-task-id"))
    app.control = Mock()
    app.control.inspect = Mock()
    app.AsyncResult = Mock(return_value=Mock(state="PENDING", result=None))

    return app


# ============================================================================
# Embedding Service Mocks
# ============================================================================


@pytest.fixture
def mock_embeddings_service():
    """Mock embedding service for testing."""
    import numpy as np

    service = Mock()

    def mock_embed_query(text: str) -> List[float]:
        """Generate deterministic embeddings based on text hash."""
        np.random.seed(hash(text) % 2**32)
        return np.random.rand(384).tolist()

    service.embed_query = Mock(side_effect=mock_embed_query)
    service.embed_documents = Mock(
        side_effect=lambda texts: [mock_embed_query(text) for text in texts]
    )
    service.embed = AsyncMock(
        side_effect=lambda texts: [mock_embed_query(text) for text in texts]
    )

    return service


# ============================================================================
# Authentication Fixtures
# ============================================================================


@pytest.fixture
def mock_user():
    """Create a mock regular user."""
    user = Mock()
    user.id = "test-user-id-12345"
    user.email = "test@example.com"
    user.first_name = "Test"
    user.last_name = "User"
    user.role = Mock(value="user")
    user.organization_id = "test-org-id"
    user.is_active = True
    user.created_at = datetime.utcnow()
    user.updated_at = datetime.utcnow()

    return user


@pytest.fixture
def mock_admin_user():
    """Create a mock admin user."""
    user = Mock()
    user.id = "test-admin-id-12345"
    user.email = "admin@example.com"
    user.first_name = "Admin"
    user.last_name = "User"
    user.role = Mock(value="admin")
    user.organization_id = "test-org-id"
    user.is_active = True
    user.created_at = datetime.utcnow()
    user.updated_at = datetime.utcnow()

    return user


@pytest.fixture
def mock_auth_headers(mock_user):
    """Generate mock JWT authentication headers."""
    import jwt

    payload = {
        "sub": mock_user.id,
        "email": mock_user.email,
        "role": mock_user.role.value,
        "exp": datetime.utcnow() + timedelta(hours=1),
        "iat": datetime.utcnow(),
    }

    token = jwt.encode(payload, "test-secret-key-for-testing", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


# ============================================================================
# Sample Data Fixtures
# ============================================================================


@pytest.fixture
def sample_documents_data():
    """Sample document data for testing."""
    return [
        {
            "id": "doc-1",
            "title": "Introduction to Machine Learning",
            "content": "Machine learning is a subset of artificial intelligence that enables systems to learn and improve from experience without being explicitly programmed.",
            "document_type": "pdf",
            "tags": ["machine learning", "AI", "algorithms"],
            "metadata": {"pages": 50, "author": "Test Author"},
            "created_at": "2024-01-01T00:00:00Z",
        },
        {
            "id": "doc-2",
            "title": "Natural Language Processing Fundamentals",
            "content": "NLP is a branch of artificial intelligence that helps computers understand, interpret and manipulate human language.",
            "document_type": "pdf",
            "tags": ["NLP", "linguistics", "text processing"],
            "metadata": {"pages": 75, "author": "Test Author 2"},
            "created_at": "2024-01-02T00:00:00Z",
        },
        {
            "id": "doc-3",
            "title": "Computer Vision and Image Recognition",
            "content": "Computer vision is an AI field that trains computers to interpret and understand the visual world from digital images or videos.",
            "document_type": "video",
            "tags": ["computer vision", "image processing", "deep learning"],
            "metadata": {"duration": 3600, "author": "Test Author 3"},
            "created_at": "2024-01-03T00:00:00Z",
        },
    ]


@pytest.fixture
def sample_search_queries():
    """Sample search queries for testing."""
    return [
        "machine learning algorithms",
        "natural language processing",
        "computer vision applications",
        "deep neural networks",
        "artificial intelligence ethics",
        "data preprocessing techniques",
        "model evaluation metrics",
        "feature engineering methods",
    ]


@pytest.fixture
def sample_search_results():
    """Sample search results for testing."""
    return [
        {
            "id": "result-1",
            "document_id": "doc-1",
            "title": "Introduction to Machine Learning",
            "content_snippet": "Machine learning is a subset of artificial intelligence...",
            "score": 0.95,
            "metadata": {"source": "vector", "model": "embedding-model-v1"},
        },
        {
            "id": "result-2",
            "document_id": "doc-2",
            "title": "Natural Language Processing Fundamentals",
            "content_snippet": "NLP is a branch of artificial intelligence...",
            "score": 0.87,
            "metadata": {"source": "fulltext", "index": "documents_index"},
        },
    ]


# ============================================================================
# Performance Tracking
# ============================================================================


@pytest.fixture
def performance_tracker():
    """Track performance metrics for tests."""
    import time

    class PerformanceTracker:
        def __init__(self):
            self.metrics = {}
            self.start_times = {}

        def start_timer(self, name: str):
            self.start_times[name] = time.time()

        def end_timer(self, name: str) -> float:
            if name in self.start_times:
                duration = time.time() - self.start_times[name]
                if name not in self.metrics:
                    self.metrics[name] = []
                self.metrics[name].append(duration)
                return duration
            return 0.0

        def get_average(self, name: str) -> float:
            if name in self.metrics and self.metrics[name]:
                return sum(self.metrics[name]) / len(self.metrics[name])
            return 0.0

        def get_summary(self) -> dict:
            summary = {}
            for name, times in self.metrics.items():
                if times:
                    summary[name] = {
                        "count": len(times),
                        "total": sum(times),
                        "average": sum(times) / len(times),
                        "min": min(times),
                        "max": max(times),
                    }
            return summary

    tracker = PerformanceTracker()
    yield tracker

    # Print summary if metrics were recorded
    if tracker.metrics:
        print(f"\n Performance Summary:")
        for name, stats in tracker.get_summary().items():
            print(f"   {name}: {stats['average']:.3f}s avg ({stats['count']} runs)")


# ============================================================================
# Logging Configuration
# ============================================================================


@pytest.fixture(autouse=True)
def configure_test_logging():
    """Configure logging for tests to reduce noise."""
    import logging

    # Reduce noise from common loggers
    for logger_name in ["uvicorn", "fastapi", "sqlalchemy", "httpx", "qdrant", "neo4j"]:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    yield


# ============================================================================
# FastAPI Test Client Fixtures
# ============================================================================


@pytest.fixture
def test_app():
    """Create FastAPI test application."""
    try:
        from src.main import app

        # Clear any existing dependency overrides
        app.dependency_overrides = {}
        return app
    except ImportError:
        pytest.skip("FastAPI app not available")


@pytest.fixture
def test_client(test_app):
    """Create synchronous test client for FastAPI application."""
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    @asynccontextmanager
    async def _no_lifespan(_app):
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(test_app) as client:
            yield client
    finally:
        test_app.router.lifespan_context = original_lifespan


@pytest.fixture
async def async_test_client(test_app) -> AsyncGenerator:
    """Create async test client for FastAPI application."""
    from httpx import AsyncClient

    async with AsyncClient(app=test_app, base_url="http://test") as client:
        yield client


# ============================================================================
# Temporary File Fixtures
# ============================================================================


@pytest.fixture
def temp_upload_dir():
    """Create a temporary upload directory."""
    import shutil
    import tempfile

    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def sample_text_file(temp_upload_dir):
    """Create a sample text file for testing."""
    file_path = os.path.join(temp_upload_dir, "sample.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("""This is a sample text file for testing document processing.

It contains multiple paragraphs and various content types including:
- Regular text
- Some numbers like 123 and 4567
- Email addresses: test@example.com
- URLs: https://www.example.com
- Company names: Acme Corporation, Global Tech Inc

The quick brown fox jumps over the lazy dog.
""")
    return file_path


# ============================================================================
# Import Service Fixtures
# ============================================================================
# Import service-specific fixtures for automatic discovery
try:
    from tests.fixtures.service_fixtures import *
except ImportError:
    pass  # Service fixtures may not be available in all test contexts

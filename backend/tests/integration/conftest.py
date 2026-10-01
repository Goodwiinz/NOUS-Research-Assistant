"""
Integration Test Configuration

Sets up environment variables BEFORE importing the application to ensure
tests use SQLite in-memory database instead of PostgreSQL. This prevents the
'database does not exist' error when running integration tests without a
PostgreSQL server running.

This module also provides the shared fixtures for the integration suite:
  - Auto-mocking of external services (Redis, Qdrant, Neo4j) and Celery.
  - Optional testcontainers-backed PostgreSQL/Redis fixtures, gated behind
    @pytest.mark.requires_postgres / @pytest.mark.requires_redis.
  - Async SQLite database + HTTP client fixtures plus the model fixtures used
    by the Project-Chat integration tests (organizations, users, workspaces,
    projects, conversations, threads, messages, and project-thread links).

NOTE: This file was previously two concatenated module bodies (a second
docstring and a duplicate import block appeared mid-file). It has been
consolidated into a single coherent module: one docstring, imports hoisted
and deduplicated, fixtures grouped in logical order. Fixture behavior is
unchanged. The environment-variable bootstrap below is intentionally left
as-is.
"""

import os
import sys
from pathlib import Path

# Add backend to path
backend_dir = Path(__file__).parent.parent.parent
sys.path.insert(0, str(backend_dir))

# CRITICAL: Force test environment variables BEFORE any application imports.
# Do not use setdefault here - local shells may already export production/dev URLs.
os.environ["ENVIRONMENT"] = "testing"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ASYNC_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["DEBUG"] = "true"
os.environ["LOG_LEVEL"] = "WARNING"

# Mock external service URLs (tests will mock these services)
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["NEO4J_URI"] = "bolt://localhost:7687"

# Mock API keys for testing
os.environ["SECRET_KEY"] = "test-secret-key-for-integration-tests"
os.environ["OPENAI_API_KEY"] = "test-openai-key"
os.environ["ANTHROPIC_API_KEY"] = "test-anthropic-key"

from unittest.mock import AsyncMock, Mock, patch

import pytest

# Initialize encryption with a test key so encrypted model fields work
os.environ.setdefault(
    "ENCRYPTION_MASTER_KEY",
    # 32 zero-bytes base64 — obviously a test fixture, never valid for production
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
)
try:
    from src.core.encryption import (
        EncryptionKeyType,
        get_key_manager,
        initialize_encryption,
    )

    initialize_encryption()
    key_manager = get_key_manager()
    if key_manager.get_active_key(EncryptionKeyType.DATA) is None:
        key_manager.generate_key(EncryptionKeyType.DATA)
except Exception:
    pass  # Encryption module may not be available in all test envs


# ----------------------------------------------------------------------------
# Imports hoisted from the (formerly duplicated) second module body.
#
# The src.* imports MUST stay below the environment-variable bootstrap above so
# the application picks up the SQLite test configuration. `pytest_asyncio` is a
# hard module-level import here (the second body already required it); it backs
# all of the async fixtures below.
# ----------------------------------------------------------------------------

from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import get_db
from src.main import app
from src.models import (
    Base,
    ChatMessage,
    Collection,
    Conversation,
    MessageRole,
    Organization,
    ProjectThread,
    ProjectThreadLinkType,
    StorageTier,
    Thread,
    User,
    UserRole,
    Workspace,
    WorkspaceRole,
)

# ============================================================================
# External Service Auto-Mocking
# ============================================================================


@pytest.fixture(autouse=True)
def mock_external_services(request):
    """
    Auto-mock external services for integration tests that don't use testcontainers.

    Tests marked with @pytest.mark.requires_postgres or @pytest.mark.requires_redis
    will NOT have those services mocked - they use real containers instead.
    """
    # Check if test uses real containers
    markers = {m.name for m in request.node.iter_markers()}
    uses_postgres = "requires_postgres" in markers
    uses_redis = "requires_redis" in markers

    # Build context managers for services we need to mock
    patches = []

    if not uses_redis:
        patches.append(patch("redis.Redis"))
        patches.append(patch("redis.asyncio.Redis"))

    patches.append(patch("qdrant_client.QdrantClient"))
    patches.append(patch("neo4j.GraphDatabase.driver"))

    # Apply patches
    mocks = [p.start() for p in patches]

    # Configure mocks
    mock_idx = 0
    if not uses_redis:
        # Redis mock
        mock_redis = mocks[mock_idx]
        mock_redis_instance = Mock()
        mock_redis_instance.ping.return_value = True
        mock_redis_instance.get.return_value = None
        mock_redis_instance.set.return_value = True
        mock_redis.return_value = mock_redis_instance
        mock_redis.from_url.return_value = mock_redis_instance
        mock_idx += 1

        # Async Redis mock
        mock_async_redis = mocks[mock_idx]
        mock_async_redis_instance = AsyncMock()
        mock_async_redis_instance.ping.return_value = True
        mock_async_redis_instance.get.return_value = None
        mock_async_redis_instance.set.return_value = True
        mock_async_redis.return_value = mock_async_redis_instance
        mock_async_redis.from_url.return_value = mock_async_redis_instance
        mock_idx += 1

    # Qdrant mock
    mock_qdrant = mocks[mock_idx]
    mock_qdrant_instance = Mock()
    mock_qdrant_instance.get_collections.return_value = Mock(collections=[])
    mock_qdrant.return_value = mock_qdrant_instance
    mock_idx += 1

    # Neo4j mock
    mock_neo4j = mocks[mock_idx]
    mock_neo4j_driver = Mock()
    mock_neo4j_driver.verify_connectivity.return_value = None
    mock_neo4j.return_value = mock_neo4j_driver

    yield

    # Stop all patches
    for p in patches:
        p.stop()


@pytest.fixture
def mock_celery_task():
    """Mock Celery task.delay() to prevent actual task queuing."""
    with patch("celery.Celery.send_task") as mock_send:
        mock_send.return_value = Mock(id="test-task-id")
        yield mock_send


# ============================================================================
# Testcontainers Integration (Optional)
# ============================================================================
# These fixtures require testcontainers to be installed:
#   pip install testcontainers[postgres,redis]
#
# They are useful for integration tests that need real database behavior.
# Use with @pytest.mark.requires_postgres or @pytest.mark.requires_redis

try:
    from testcontainers.postgres import PostgresContainer
    from testcontainers.redis import RedisContainer

    TESTCONTAINERS_AVAILABLE = True
except ImportError:
    TESTCONTAINERS_AVAILABLE = False
    PostgresContainer = None
    RedisContainer = None


@pytest.fixture(scope="session")
def postgres_container():
    """
    Spin up PostgreSQL container for integration tests.

    This fixture provides a real PostgreSQL database for tests that need
    actual database behavior (transactions, constraints, etc.).

    Usage:
        @pytest.mark.requires_postgres
        def test_with_real_db(postgres_container):
            connection_url = postgres_container["url"]
            # Use connection_url for database operations

    Requires: pip install testcontainers[postgres]
    """
    if not TESTCONTAINERS_AVAILABLE:
        pytest.skip(
            "testcontainers not installed - run: pip install testcontainers[postgres]"
        )

    with PostgresContainer("postgres:15-alpine") as postgres:
        yield {
            "url": postgres.get_connection_url(),
            "host": postgres.get_container_host_ip(),
            "port": postgres.get_exposed_port(5432),
            "user": postgres.username,
            "password": postgres.password,
            "database": postgres.dbname,
        }


@pytest.fixture(scope="session")
def redis_container():
    """
    Spin up Redis container for integration tests.

    This fixture provides a real Redis instance for tests that need
    actual caching behavior, pub/sub, or Redis data structures.

    Usage:
        @pytest.mark.requires_redis
        def test_with_real_redis(redis_container):
            redis_url = redis_container["url"]
            # Use redis_url for cache operations

    Requires: pip install testcontainers[redis]
    """
    if not TESTCONTAINERS_AVAILABLE:
        pytest.skip(
            "testcontainers not installed - run: pip install testcontainers[redis]"
        )

    with RedisContainer("redis:7-alpine") as redis:
        host = redis.get_container_host_ip()
        port = redis.get_exposed_port(6379)
        yield {
            "url": f"redis://{host}:{port}/0",
            "host": host,
            "port": port,
        }


@pytest.fixture(scope="function")
def postgres_session(postgres_container):
    """
    Create a SQLAlchemy session connected to the PostgreSQL container.

    This fixture creates tables and provides a session for database tests.
    Tables are dropped after each test for isolation.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(postgres_container["url"])

    # Import and create all tables
    # IMPORTANT: Import all models to ensure SQLAlchemy can resolve relationships
    try:
        # Import ab_testing models to resolve User -> Experiment relationship
        from src.models import ab_testing  # noqa: F401
        from src.models import Base

        Base.metadata.create_all(engine)
    except ImportError:
        pass  # Models may not be available

    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    try:
        yield session
    finally:
        session.rollback()
        session.close()
        # Clean up tables after test
        try:
            from src.models import ab_testing  # noqa: F401
            from src.models import Base

            Base.metadata.drop_all(engine)
        except ImportError:
            pass
        engine.dispose()


@pytest.fixture(scope="function")
def redis_client(redis_container):
    """
    Create a Redis client connected to the Redis container.

    The client is connected to a fresh Redis instance for each test session.
    """
    import redis

    client = redis.Redis.from_url(redis_container["url"])

    try:
        yield client
    finally:
        # Clean up all keys after test
        client.flushdb()
        client.close()


# NOTE: `async_redis_client` was originally wrapped in a
# `try: import pytest_asyncio ... except ImportError` guard. That guard is now
# redundant because `pytest_asyncio` is imported unconditionally at module level
# above (the second module body already required it), so the fixture is defined
# directly. Its body is unchanged.
@pytest_asyncio.fixture(scope="function")
async def async_redis_client(redis_container):
    """
    Create an async Redis client connected to the Redis container.

    Use for testing async cache operations.
    """
    import redis.asyncio as aioredis

    client = aioredis.from_url(
        redis_container["url"],
        encoding="utf-8",
        decode_responses=True,
    )

    try:
        yield client
    finally:
        await client.flushdb()
        await client.close()


# ============================================================================
# Project-Chat Integration Fixtures
# ============================================================================
# Database + HTTP client fixtures and the model fixtures used by the
# Project-Chat integration tests (test_project_chat_api.py, test_citations_api.py).

try:
    import greenlet  # noqa: F401

    GREENLET_AVAILABLE = True
except ImportError:
    GREENLET_AVAILABLE = False


# ----------------------------------------------------------------------------
# Database Setup
# ----------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function")
async def test_db():
    """Create test database with all tables."""
    if not GREENLET_AVAILABLE:
        pytest.skip("greenlet is required for async SQLAlchemy integration fixtures")

    # Use in-memory SQLite for tests
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Create async session
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        yield session

    # Cleanup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest.fixture(scope="function")
def tenant_gate_headers(test_db, test_user, monkeypatch):
    """Authenticate requests through the REAL MultiTenancyMiddleware (PR #1722).

    The fail-closed tenancy gate verifies the Bearer JWT and resolves the user
    in its OWN ``AsyncSessionLocal`` session, independent of the ``get_db``
    override. Point that session factory at this test's in-memory engine (the
    one holding ``test_user``) and return a real signed token for
    ``test_user``. The middleware logic itself is untouched: an unknown,
    inactive or unauthenticated caller still gets 401, a lookup failure 500.
    """
    from jose import jwt

    from src.core.config import settings
    from src.middleware import multi_tenancy

    # Real offline HS256 verification, same pattern as
    # tests/unit/middleware/test_multi_tenancy_merge_contract.py::signed_token.
    secret = "integration-tenant-gate-signing-key-not-for-production"
    monkeypatch.setattr(settings, "SUPABASE_JWT_SECRET", secret)
    monkeypatch.setattr(settings, "SUPABASE_JWT_ISSUER", "")
    monkeypatch.setattr(
        multi_tenancy,
        "AsyncSessionLocal",
        sessionmaker(test_db.bind, class_=AsyncSession, expire_on_commit=False),
    )
    token = jwt.encode(
        {"sub": str(test_user.id), "aud": "authenticated", "app_metadata": {}},
        secret,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture(scope="function")
async def async_client(test_db, test_user, tenant_gate_headers):
    """Create async HTTP client with test database override, authenticated
    as ``test_user`` through the tenancy gate."""

    async def override_get_db():
        yield test_db

    async def override_get_current_user():
        return test_user

    from src.core.dependencies import get_current_user as core_get_current_user
    from src.services.security.user_management import (
        get_current_user as user_mgmt_get_current_user,
    )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[core_get_current_user] = override_get_current_user
    app.dependency_overrides[user_mgmt_get_current_user] = override_get_current_user

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://localhost", headers=tenant_gate_headers
    ) as client:
        yield client

    app.dependency_overrides.clear()


# ----------------------------------------------------------------------------
# Model Fixtures
# ----------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function")
async def test_organization(test_db: AsyncSession):
    """Create a test organization required by User foreign keys."""
    organization = Organization(
        id=uuid4(),
        name="Test Organization",
        storage_tier=StorageTier.FREE,
        storage_limit_bytes=Organization.get_default_storage_limit(StorageTier.FREE),
        is_active=True,
    )
    test_db.add(organization)
    await test_db.commit()
    await test_db.refresh(organization)
    return organization


@pytest_asyncio.fixture(scope="function")
async def test_user(test_db: AsyncSession, test_organization: Organization):
    """Create a test user."""
    user = User(
        id=uuid4(),
        email="test@example.com",
        password_hash="hashed_password_123",
        first_name="Test",
        last_name="User",
        role=UserRole.USER,
        is_active=True,
        organization_id=test_organization.id,
    )
    test_db.add(user)
    await test_db.commit()
    await test_db.refresh(user)
    return user


@pytest_asyncio.fixture(scope="function")
async def other_user(test_db: AsyncSession, test_organization: Organization):
    """Create another test user for isolation tests."""
    user = User(
        id=uuid4(),
        email="other@example.com",
        password_hash="hashed_password_456",
        first_name="Other",
        last_name="User",
        role=UserRole.USER,
        is_active=True,
        organization_id=test_organization.id,
    )
    test_db.add(user)
    await test_db.commit()
    await test_db.refresh(user)
    return user


@pytest_asyncio.fixture(scope="function")
async def test_workspace(test_db: AsyncSession, test_user: User):
    """Create a test workspace."""
    workspace = Workspace(
        id=uuid4(),
        name="Test Workspace",
        owner_id=test_user.id,
        organization_id=test_user.organization_id,
    )
    test_db.add(workspace)
    await test_db.commit()
    await test_db.refresh(workspace)
    return workspace


@pytest_asyncio.fixture(scope="function")
async def other_workspace(test_db: AsyncSession, other_user: User):
    """Create workspace for another user (isolation tests)."""
    workspace = Workspace(
        id=uuid4(),
        name="Other Workspace",
        owner_id=other_user.id,
        organization_id=other_user.organization_id,
    )
    test_db.add(workspace)
    await test_db.commit()
    await test_db.refresh(workspace)
    return workspace


@pytest_asyncio.fixture(scope="function")
async def test_project(test_db: AsyncSession, test_workspace: Workspace):
    """Create a test project (Collection)."""
    project = Collection(
        id=uuid4(),
        name="Test Research Project",
        description="A test project for integration testing",
        workspace_id=test_workspace.id,
    )
    test_db.add(project)
    await test_db.commit()
    await test_db.refresh(project)
    return project


@pytest_asyncio.fixture(scope="function")
async def test_conversation(
    test_db: AsyncSession, test_workspace: Workspace, test_user: User
):
    """Create a test conversation."""
    conversation = Conversation(
        id=uuid4(),
        title="Test Conversation",
        workspace_id=test_workspace.id,
        created_by_id=test_user.id,
    )
    test_db.add(conversation)
    await test_db.commit()
    await test_db.refresh(conversation)
    return conversation


@pytest_asyncio.fixture(scope="function")
async def test_thread(
    test_db: AsyncSession, test_conversation: Conversation, test_user: User
):
    """Create a test thread."""
    thread = Thread(
        id=uuid4(),
        title="Test Thread",
        conversation_id=test_conversation.id,
        created_by_id=test_user.id,
        message_count=0,
    )
    test_db.add(thread)
    await test_db.commit()
    await test_db.refresh(thread)
    return thread


@pytest_asyncio.fixture(scope="function")
async def test_project_thread(
    test_db: AsyncSession,
    test_project: Collection,
    test_thread: Thread,
    test_user: User,
):
    """Create a test project-thread link."""
    project_thread = ProjectThread(
        id=uuid4(),
        project_id=test_project.id,
        thread_id=test_thread.id,
        link_type=ProjectThreadLinkType.MANUAL.value,
        linked_by_id=test_user.id,
        context_note="Test link",
    )
    test_db.add(project_thread)
    await test_db.commit()
    await test_db.refresh(project_thread)
    return project_thread


@pytest_asyncio.fixture(scope="function")
async def test_message(test_db: AsyncSession, test_thread: Thread, test_user: User):
    """Create a test message."""
    message = ChatMessage(
        id=uuid4(),
        thread_id=test_thread.id,
        user_id=test_user.id,
        role=MessageRole.USER,
        content="Test message content",
    )
    test_db.add(message)

    # Update thread message count
    test_thread.message_count += 1

    await test_db.commit()
    await test_db.refresh(message)
    return message

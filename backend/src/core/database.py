"""
Database configuration and connection management
"""

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Import base model from models
from src.models.base import Base
from src.models.organization import Organization, StorageTier
from src.models.user import User, UserRole

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """Read an integer env var, falling back to ``default`` on any parse error.

    Previously these values used raw ``int(os.getenv(name, str(default)))``
    which raises ``ValueError`` at module import time if the env is set to
    an empty string, ``"true"``, or any other non-numeric value — crashing
    FastAPI startup with a cryptic traceback before loggers are initialized.
    This helper degrades gracefully: it logs the bad value once and keeps
    the service bootable on the hard-coded default.
    """

    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.error(
            "Invalid integer env %s=%r; falling back to default %d",
            name,
            raw,
            default,
        )
        return default


# Database URL resolution: prefer SUPABASE_DB_URL if set
_supabase_db_url = os.getenv("SUPABASE_DB_URL", "")
DATABASE_URL = _supabase_db_url or os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/multimodal_rag_dev",
)

# Async variant — asyncpg uses ssl= instead of sslmode=
if "asyncpg" not in DATABASE_URL:
    ASYNC_DATABASE_URL = (
        DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
        .replace("?sslmode=require", "?ssl=require")
        .replace("&sslmode=require", "&ssl=require")
    )
else:
    ASYNC_DATABASE_URL = DATABASE_URL

# Seed user passwords - MUST be set in production via environment variables
# In development, uses defaults for convenience (warning will be shown)
SEED_ADMIN_PASSWORD = os.getenv("SEED_ADMIN_PASSWORD", "")
SEED_DEMO_PASSWORD = os.getenv("SEED_DEMO_PASSWORD", "")
SEED_LAB_ADMIN_PASSWORD = os.getenv("SEED_LAB_ADMIN_PASSWORD", "")

# Hosts considered "genuinely local" for the startup create_all gate. Anything
# else (DO Managed PG, RDS, a future post-Supabase production DB) must be owned
# by Alembic — create_all against a managed DB races migrations and permanently
# drifts the schema (see main.py lifespan + PR #925/#931 history).
LOCAL_DB_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "host.docker.internal",
        "postgres",  # docker-compose service name
        "db",  # common compose alias
    }
)


def should_run_create_all(
    environment: str,
    supabase_db_url: str,
    db_host: str | None,
    is_sqlite: bool,
    force: bool,
) -> bool:
    """Decide whether startup may run Base.metadata.create_all.

    True only for a genuinely local dev database:
    - ENVIRONMENT must be exactly "development" (deployed clusters set
      dev/staging/production), AND
    - no SUPABASE_DB_URL (every managed cluster sets it), AND
    - the resolved engine host is local (or the DB is SQLite), OR the operator
      explicitly forced it with RUN_CREATE_ALL=1.

    The host check closes the residual gap where ENVIRONMENT is unset (defaults
    to "development") and a managed DATABASE_URL is used without
    SUPABASE_DB_URL — previously that combination still ran create_all.
    """
    if environment != "development":
        return False
    if supabase_db_url:
        return False
    if force:
        return True
    if is_sqlite:
        return True
    return (db_host or "") in LOCAL_DB_HOSTS


# Check if using SQLite (for testing) - SQLite doesn't support pool options
_is_sqlite = DATABASE_URL.startswith("sqlite")

# Create engine with appropriate settings
if _is_sqlite:
    # SQLite configuration for testing
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=os.getenv("ENVIRONMENT") == "development",
    )
else:
    # PostgreSQL configuration for production/development.
    # The sync engine is NOT background-only: request-path consumers include the
    # RBAC permission dependency (middleware/rbac.py), the evidence,
    # knowledge-graph, user-behavior and compliance routers, plus Celery tasks.
    # A 1+1 pool exhausts under two concurrent guarded requests ("QueuePool
    # limit of size 1 overflow 1"), stalling threadpool threads for
    # pool_timeout=30s and restarting the backend. Defaults sized for local
    # dev (no pooler cap); deployed values override per-component to fit the
    # Supabase session-mode pooler budget (values-dev.yaml).
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=_env_int("DB_SYNC_POOL_SIZE", 3),
        max_overflow=_env_int("DB_SYNC_MAX_OVERFLOW", 2),
        pool_timeout=30,
        echo=os.getenv("ENVIRONMENT") == "development",
    )

# Create async engine for async operations
if _is_sqlite:
    # SQLite async configuration for testing
    # Note: aiosqlite is required for async SQLite support
    async_engine = create_async_engine(
        (
            ASYNC_DATABASE_URL
            if not ASYNC_DATABASE_URL.startswith("sqlite")
            else "sqlite+aiosqlite:///:memory:"
        ),
        echo=os.getenv("ENVIRONMENT") == "development",
    )
else:
    # PostgreSQL async configuration
    # pool_pre_ping MUST stay enabled. Supabase's pooler drops idle server-side
    # connections well before ``pool_recycle`` expires them, and QueuePool is
    # FIFO by default, so a checkout preferentially hands back the *stalest*
    # connection. Without the liveness ping the first statement on that
    # connection (the transaction's BEGIN) raises
    # ``asyncpg.ConnectionDoesNotExistError``; on the agent stream path that
    # aborts finalize_submission after the answer was already produced, leaving
    # the run non-terminal so ``uq_agent_runs_active_thread`` rejects the user's
    # next turn until the stale-run sweeper marks a successful turn "failed"
    # (Sentry JAVASCRIPT-NEXTJS-4W).
    #
    # It was flipped to False in 77dfe97e citing MissingGreenlet, but that was
    # speculative: MissingGreenlet in this codebase comes from ORM lazy-loads,
    # never from the pool ping, which SQLAlchemy 2.x runs inside the greenlet
    # context via the async adapter. Verified against SQLAlchemy 2.0.51 /
    # asyncpg 0.31 — see tests/unit/core/test_database_helpers.py.
    #
    # Pool sizing: Supabase's session-mode pooler caps each client at pool_size
    # slots (Nano defaults to 15, Small to 25). Defaults below target the Small
    # tier (15 + 5 = 20 max) and remain env-overridable. Tune downward when
    # running on Nano or alongside celery workers.
    #
    # Statement cache: Supabase session-mode supports prepared statements, so
    # asyncpg's statement cache is enabled. Disable (size=0) only when routing
    # through PgBouncer in transaction mode.
    _stmt_cache = _env_int("DB_ASYNC_STMT_CACHE_SIZE", 100)
    async_engine = create_async_engine(
        ASYNC_DATABASE_URL,
        pool_size=_env_int("DB_ASYNC_POOL_SIZE", 15),
        max_overflow=_env_int("DB_ASYNC_MAX_OVERFLOW", 5),
        pool_timeout=_env_int("DB_ASYNC_POOL_TIMEOUT", 30),
        pool_recycle=_env_int("DB_ASYNC_POOL_RECYCLE", 1500),
        pool_pre_ping=True,  # See the note above — must not be disabled.
        connect_args={
            "prepared_statement_cache_size": _stmt_cache,
            "statement_cache_size": _stmt_cache,
        },
        echo=os.getenv("ENVIRONMENT") == "development",
    )

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False
)


def get_db_sync() -> Session:
    """Get database session (synchronous)"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_db() -> AsyncSession:
    """Get a request-scoped database session (asynchronous).

    FastAPI closes it after the response (including a streamed body) is sent.
    """
    async with AsyncSessionLocal() as session:
        yield session


@asynccontextmanager
async def get_async_session() -> AsyncSession:
    """Get async database session"""
    async with AsyncSessionLocal() as session:
        yield session


# Alias for async session (for backward compatibility with async with usage)
get_db_session = get_async_session


def create_tables():
    """Create all database tables"""
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created successfully")


async def create_tables_async():
    """Create all database tables using async engine"""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created successfully (async)")


def drop_tables():
    """Drop all database tables (use with caution)"""
    Base.metadata.drop_all(bind=engine)
    logger.warning("All database tables dropped")


def init_database():
    """Initialize database with seed data"""
    db = SessionLocal()
    try:
        # Check if already initialized
        if db.query(Organization).first():
            print("Database already initialized")
            return

        # Create default organization
        default_org = Organization(
            name="Default Organization",
            storage_tier=StorageTier.FREE,
            storage_limit_bytes=Organization.get_default_storage_limit(
                StorageTier.FREE
            ),
            is_active=True,
        )
        db.add(default_org)
        db.flush()
        print(f"Created default organization: {default_org.name}")

        # Create admin user
        admin_user = User(
            email="admin@multimodal-rag.com",
            first_name="System",
            last_name="Administrator",
            role=UserRole.ADMIN,
            organization_id=default_org.id,
            is_active=True,
        )
        # Use environment variable or generate secure random password
        admin_password = SEED_ADMIN_PASSWORD or f"dev-admin-{uuid.uuid4().hex[:8]}"
        admin_user.set_password(admin_password)
        db.add(admin_user)
        db.flush()
        print(f"Created admin user: {admin_user.email}")

        # Create demo user
        demo_user = User(
            email="demo@multimodal-rag.com",
            first_name="Demo",
            last_name="User",
            role=UserRole.USER,
            organization_id=default_org.id,
            is_active=True,
        )
        # Use environment variable or generate secure random password
        demo_password = SEED_DEMO_PASSWORD or f"dev-demo-{uuid.uuid4().hex[:8]}"
        demo_user.set_password(demo_password)
        db.add(demo_user)
        db.flush()
        print(f"Created demo user: {demo_user.email}")

        # Create additional demo organization
        demo_org = Organization(
            name="Demo Research Lab",
            storage_tier=StorageTier.PROFESSIONAL,
            storage_limit_bytes=Organization.get_default_storage_limit(
                StorageTier.PROFESSIONAL
            ),
            is_active=True,
        )
        db.add(demo_org)
        db.flush()
        print(f"Created demo organization: {demo_org.name}")

        # Create demo organization admin
        lab_admin = User(
            email="lab-admin@multimodal-rag.com",
            first_name="Lab",
            last_name="Administrator",
            role=UserRole.CONTENT_MANAGER,  # Using available role instead of ORG_ADMIN
            organization_id=demo_org.id,
            is_active=True,
        )
        # Use environment variable or generate secure random password
        lab_password = SEED_LAB_ADMIN_PASSWORD or f"dev-lab-{uuid.uuid4().hex[:8]}"
        lab_admin.set_password(lab_password)
        db.add(lab_admin)
        db.flush()
        print(f"Created lab admin user: {lab_admin.email}")

        # Commit all changes
        db.commit()
        print("\n✅ Database initialized successfully!")
        # Show generated passwords for development
        if not SEED_ADMIN_PASSWORD:
            print(f"   ├── Password: {admin_password} (auto-generated)")
            print("   │   ⚠️  Set SEED_ADMIN_PASSWORD env var for consistent password")
        else:
            print("   ├── Password: (set via SEED_ADMIN_PASSWORD env var)")
        print("   └── Role: System Administrator")
        print()
        print("   Demo User:")
        print("   ├── Email: demo@multimodal-rag.com")
        if not SEED_DEMO_PASSWORD:
            print(f"   ├── Password: {demo_password} (auto-generated)")
            print("   │   ⚠️  Set SEED_DEMO_PASSWORD env var for consistent password")
        else:
            print("   ├── Password: (set via SEED_DEMO_PASSWORD env var)")
        print("   └── Role: Regular User")
        print()
        print("   Lab Admin:")
        print("   ├── Email: lab-admin@multimodal-rag.com")
        if not SEED_LAB_ADMIN_PASSWORD:
            print(f"   ├── Password: {lab_password} (auto-generated)")
            print(
                "   │   ⚠️  Set SEED_LAB_ADMIN_PASSWORD env var for consistent password"
            )
        else:
            print("   ├── Password: (set via SEED_LAB_ADMIN_PASSWORD env var)")
        print("   └── Role: Organization Admin")
        print()
        if not all([SEED_ADMIN_PASSWORD, SEED_DEMO_PASSWORD, SEED_LAB_ADMIN_PASSWORD]):
            print(
                "⚠️  IMPORTANT: Set SEED_*_PASSWORD environment variables in production!"
            )

    except SQLAlchemyError as e:
        db.rollback()
        print(f"❌ Error initializing database: {e}")
        raise
    finally:
        db.close()


def get_database_info():
    """Get information about database state"""
    db = SessionLocal()
    try:
        info = {}

        # Get table counts
        info["users_count"] = db.query(User).count()
        info["organizations_count"] = db.query(Organization).count()

        # Get database size (PostgreSQL specific)
        try:
            result = db.execute(
                text("SELECT pg_size_pretty(pg_database_size('multimodal_rag'))")
            )
            info["database_size"] = result.scalar()
        except (Exception,) as e:
            logger.debug(f"Could not get database size: {e}")
            info["database_size"] = "Unknown"

        return info
    except Exception as e:
        print(f"Error getting database info: {e}")
        return {}
    finally:
        db.close()


# Database health check
def check_database_health() -> bool:
    """Check if database is healthy"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return False


# Query performance monitoring
SLOW_QUERY_THRESHOLD = float(os.getenv("SLOW_QUERY_THRESHOLD", "1.0"))


@event.listens_for(Engine, "before_cursor_execute")
def receive_before_cursor_execute(
    conn, cursor, statement, parameters, context, executemany
):
    """Record query start time for performance monitoring"""
    context._query_start_time = time.time()


@event.listens_for(Engine, "after_cursor_execute")
def receive_after_cursor_execute(
    conn, cursor, statement, parameters, context, executemany
):
    """Log slow queries for performance analysis"""
    total = time.time() - context._query_start_time
    if total > SLOW_QUERY_THRESHOLD:
        logger.warning(f"Slow query ({total:.2f}s): {statement[:200]}...")

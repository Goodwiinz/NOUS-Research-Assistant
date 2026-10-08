"""Two-account data-isolation fixtures (GOO-347).

The access matrix these fixtures exercise lives in
``docs/engineering/data-isolation-matrix.md``. Read it before adding a case.

Seed shape (every id is ``uuid5(NS, name)`` and every private string is a
``CANARY[name]`` constant, so a failing assertion names exactly which
account's data leaked and a rerun reproduces the same ids):

- ``A``  — organization ``org-a``. The caller under test.
- ``B``  — organization ``org-b``. Owns the private data A must never see.
- ``C``  — organization ``org-b`` (B's colleague). Same-org nonmember of B's
  private workspace; gets invited as a member by the revocation tests.

Data owned by B (org-b): a private document + public document (each with a
citation and a downloadable local file), a private workspace with
conversation/thread/message, and a public workspace with the same chain.
Data owned by A (org-a): one private document + citation and a private
workspace chain, for the positive own-data cases.

Every test runs against a fresh in-memory SQLite database (``test_db`` from
``tests/integration/conftest.py``), so cleanup is ``drop_all`` and is
idempotent by construction. No real users, organizations or deployed
databases are touched.

The ``pg_*`` fixtures (GOO-399) seed the same accounts into a throwaway
schema of a disposable PostgreSQL database for the full-text search paths
SQLite cannot run. They skip unless ``TWO_ACCOUNT_PG_TEST_DATABASE_URL`` is set.

Authentication goes through the real ``MultiTenancyMiddleware`` and the real
``get_current_user`` dependency with an HS256 token per account — nothing
overrides the user resolution, so a client really is the account it claims.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterator, Optional

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import create_engine, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from src.core import database
from src.core.database import get_db, get_db_sync
from src.main import app
from src.models import (
    Base,
    ChatMessage,
    Citation,
    Conversation,
    Document,
    MessageRole,
    Organization,
    StorageTier,
    Thread,
    User,
    UserRole,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)
from src.models.document import DocumentType, ProcessingStatus

NS = uuid.UUID("5e1f0a3c-0b5d-4c6e-9f1a-2a7d3c4b5e60")
_SIGNING_KEY = "two-account-isolation-signing-key-not-for-production"


def sid(name: str) -> uuid.UUID:
    """Reproducible id for a seeded row."""
    return uuid.uuid5(NS, name)


def canary(name: str) -> str:
    """Synthetic marker that must only ever reach its owner's clients."""
    return f"ISO-CANARY-{name.upper()}-{sid(name).hex[:8]}"


Clients = Callable[[str], AsyncClient]


async def _add(db: AsyncSession, *rows: Any) -> None:
    db.add_all(rows)
    await db.commit()


def _org(name: str) -> Organization:
    return Organization(
        id=sid(f"org-{name}"),
        name=f"Isolation org {name}",
        storage_tier=StorageTier.FREE,
        storage_limit_bytes=Organization.get_default_storage_limit(StorageTier.FREE),
        is_active=True,
    )


def _user(name: str, org: Optional[str]) -> User:
    return User(
        id=sid(f"user-{name}"),
        email=f"isolation-{name}@example.invalid",
        password_hash="unused",
        first_name=name.upper(),
        last_name="Isolation",
        role=UserRole.USER,
        is_active=True,
        organization_id=sid(f"org-{org}") if org else None,
    )


def _document(key: str, owner: str, org: str, tmp: Path, *, public: bool) -> Document:
    body = canary(f"{key}-content")
    path = tmp / f"{key}.txt"
    path.write_text(body)
    return Document(
        id=sid(key),
        title=canary(f"{key}-title"),
        filename=f"{key}.txt",
        file_path=str(path),
        file_size_bytes=len(body),
        mime_type="text/plain",
        document_type=DocumentType.TEXT,
        storage_backend="local",
        processing_status=ProcessingStatus.COMPLETED,
        content_text=body,
        is_public=public,
        uploaded_by_user_id=sid(f"user-{owner}"),
        organization_id=sid(f"org-{org}"),
    )


def _citation(key: str, document_key: str) -> Citation:
    return Citation(
        id=sid(key),
        document_id=sid(document_key),
        document_title=canary(f"{key}-title"),
        snippet=canary(f"{key}-quote"),
        metadata_source="manual",
    )


def _chat_chain(key: str, owner: str, org: str, *, public: bool) -> list:
    """Workspace -> conversation -> thread -> message, all owned by ``owner``.

    The owner also gets an OWNER membership row, as ``create_workspace`` does.
    """
    uid = sid(f"user-{owner}")
    return [
        Workspace(
            id=sid(f"{key}-ws"),
            name=canary(f"{key}-ws"),
            owner_id=uid,
            organization_id=sid(f"org-{org}"),
            is_public=public,
        ),
        WorkspaceMember(
            id=sid(f"member-{key}-{owner}"),
            workspace_id=sid(f"{key}-ws"),
            user_id=uid,
            role=WorkspaceRole.OWNER,
        ),
        Conversation(
            id=sid(f"{key}-conv"),
            title=canary(f"{key}-conv"),
            workspace_id=sid(f"{key}-ws"),
            created_by_id=uid,
        ),
        Thread(
            id=sid(f"{key}-thread"),
            title=canary(f"{key}-thread"),
            conversation_id=sid(f"{key}-conv"),
            created_by_id=uid,
            message_count=1,
        ),
        ChatMessage(
            id=sid(f"{key}-msg"),
            thread_id=sid(f"{key}-thread"),
            user_id=uid,
            role=MessageRole.USER,
            content=canary(f"{key}-msg"),
        ),
    ]


async def seed_accounts(db: AsyncSession, tmp_path: Path) -> None:
    """Seed A/B/C plus their documents, citations and chat chains.

    Shared by the SQLite ``seed`` fixture and the PostgreSQL ``pg_seed``.
    """
    await _add(db, _org("a"), _org("b"))
    users = {"A": _user("a", "a"), "B": _user("b", "b"), "C": _user("c", "b")}
    await _add(db, *users.values())
    await _add(
        db,
        _document("a-doc", "a", "a", tmp_path, public=False),
        _document("b-doc", "b", "b", tmp_path, public=False),
        _document("b-pub-doc", "b", "b", tmp_path, public=True),
        # Uploaded by A while A belonged to org-b; A has since moved to org-a.
        _document("a-old-org-doc", "a", "b", tmp_path, public=False),
    )
    await _add(
        db,
        _citation("a-cit", "a-doc"),
        _citation("b-cit", "b-doc"),
        _citation("b-pub-cit", "b-pub-doc"),
        _citation("a-old-org-cit", "a-old-org-doc"),
    )
    for rows in (
        _chat_chain("a", "a", "a", public=False),
        _chat_chain("b", "b", "b", public=False),
        _chat_chain("b-pub", "b", "b", public=True),
    ):
        for row in rows:  # parent before child: SQLite enforces nothing, PG does
            await _add(db, row)


@pytest_asyncio.fixture
async def seed(test_db: AsyncSession, tmp_path: Path) -> None:
    """Seed A/B/C plus their documents, citations and chat chains."""
    await seed_accounts(test_db, tmp_path)


async def add_member(db: AsyncSession, workspace_key: str, who: str) -> None:
    """Invite ``who`` into ``workspace_key`` as an editor (DB-level setup)."""
    await _add(
        db,
        WorkspaceMember(
            id=sid(f"member-{workspace_key}-{who}"),
            workspace_id=sid(f"{workspace_key}-ws"),
            user_id=sid(f"user-{who.lower()}"),
            role=WorkspaceRole.EDITOR,
        ),
    )


def _token(user_id: uuid.UUID) -> str:
    return str(
        jwt.encode(
            {
                "sub": str(user_id),
                "aud": "authenticated",
                "app_metadata": {},
                "exp": int(time.time()) + 3600,
            },
            _SIGNING_KEY,
            algorithm="HS256",
        )
    )


@asynccontextmanager
async def _authenticated_clients(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    sync_factory: Optional[sessionmaker[Session]] = None,
) -> AsyncIterator[Clients]:
    """Real-token clients; only the session factories point at the test DB.

    A fresh session per request, like production: sharing one session across
    requests would serve stale identity-map rows (e.g. a removed membership)
    and hide or fake an access decision. ``sync_factory`` also backs
    ``get_db_sync`` and ``database.SessionLocal`` for the routes and services
    that open their own synchronous session (the PostgreSQL search paths).
    """
    from src.core.config import settings
    from src.middleware import multi_tenancy

    monkeypatch.setattr(settings, "SUPABASE_JWT_SECRET", _SIGNING_KEY)
    monkeypatch.setattr(settings, "SUPABASE_JWT_ISSUER", "")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(multi_tenancy, "AsyncSessionLocal", session_factory)
    app.dependency_overrides[get_db] = override_get_db
    if sync_factory is not None:
        factory = sync_factory

        def override_get_db_sync() -> Iterator[Session]:
            with factory() as session:
                yield session

        monkeypatch.setattr(database, "SessionLocal", factory)
        app.dependency_overrides[get_db_sync] = override_get_db_sync
    opened: Dict[str, AsyncClient] = {}

    def _client(who: str) -> AsyncClient:
        if who not in opened:
            opened[who] = AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://localhost",
                headers={
                    "Authorization": f"Bearer {_token(sid(f'user-{who.lower()}'))}"
                },
            )
        return opened[who]

    try:
        yield _client
    finally:
        for c in opened.values():
            await c.aclose()
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def clients(
    test_db: AsyncSession, seed: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Clients]:
    """``clients("A")`` -> an HTTP client authenticated as account A.

    Only ``get_db`` is overridden (to share the in-memory engine). Token
    verification, user lookup and the tenancy middleware are the real code.
    """
    factory = async_sessionmaker(test_db.bind, expire_on_commit=False)
    async with _authenticated_clients(factory, monkeypatch) as client:
        yield client


# --- PostgreSQL variant (GOO-399) ------------------------------------------
#
# Document, thread and message search are PostgreSQL full-text (tsvector,
# ts_rank_cd, ts_headline), which SQLite cannot run. These fixtures seed the
# same accounts into a throwaway schema of the disposable database named by
# TWO_ACCOUNT_PG_TEST_DATABASE_URL and skip when it is unset. CI sets it in
# the Integration Tests job and rejects a run in which these tests skip.

PG_ENV = "TWO_ACCOUNT_PG_TEST_DATABASE_URL"
PG_DOCUMENT_KEYS = ("a-doc", "a-doc-2", "b-doc", "b-pub-doc", "a-old-org-doc")
_THREAD_FTS_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "b2c3d4e5f6g7_add_fulltext_search_for_threads.py"
)
# Supabase's default. Pinned so plainto_tsquery() stems like the 'english'
# search_vector builders whatever locale the server was initialised with.
_PG_TEXT_SEARCH_CONFIG = "pg_catalog.english"


def pg_url(driver: str) -> str:
    """The disposable database URL for ``driver``; skip when it is unset."""
    dsn = os.getenv(PG_ENV, "")
    if not dsn:
        pytest.skip(f"{PG_ENV} is not configured")
    for prefix in (
        "postgresql+asyncpg://",
        "postgresql+psycopg2://",
        "postgresql+psycopg://",
        "postgresql://",
        "postgres://",
    ):
        if dsn.startswith(prefix):
            return f"postgresql+{driver}://{dsn[len(prefix):]}"
    raise ValueError(f"{PG_ENV} must be a PostgreSQL URL")


@dataclass(frozen=True)
class PgEngines:
    sync: Engine
    async_: AsyncEngine


def _pg_sync_engine(schema: str) -> Engine:
    return create_engine(
        pg_url("psycopg2"),
        poolclass=NullPool,
        connect_args={
            "options": (
                f"-csearch_path={schema} "
                f"-cdefault_text_search_config={_PG_TEXT_SEARCH_CONFIG}"
            )
        },
    )


def _apply_thread_fulltext_migration(engine: Engine) -> None:
    """Run the production migration that adds thread/message ``search_vector``.

    The ORM models declare neither those columns nor their insert triggers;
    this Alembic revision does. Running its ``upgrade()`` keeps the schema in
    step with production instead of a hand-copied DDL.
    """
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location(
        "two_account_thread_fts_migration", _THREAD_FTS_MIGRATION
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()


@pytest.fixture(scope="module")
def pg_schema() -> Iterator[str]:
    """One schema per module: every table plus the thread FTS migration."""
    url = pg_url("psycopg2")
    schema = f"two_account_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url, poolclass=NullPool)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = _pg_sync_engine(schema)
    try:
        Base.metadata.create_all(engine)
        _apply_thread_fulltext_migration(engine)
        yield schema
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


@pytest_asyncio.fixture
async def pg_engines(pg_schema: str) -> AsyncIterator[PgEngines]:
    """Empty every table, then hand out sync and async engines on the schema."""
    sync = _pg_sync_engine(pg_schema)
    tables = ", ".join(f'"{name}"' for name in sorted(Base.metadata.tables))
    with sync.begin() as connection:
        connection.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    async_ = create_async_engine(
        pg_url("asyncpg"),
        poolclass=NullPool,
        connect_args={
            "server_settings": {
                "search_path": pg_schema,
                "default_text_search_config": _PG_TEXT_SEARCH_CONFIG,
            }
        },
    )
    try:
        yield PgEngines(sync=sync, async_=async_)
    finally:
        await async_.dispose()
        sync.dispose()


@pytest_asyncio.fixture
async def pg_db(pg_engines: PgEngines) -> AsyncIterator[AsyncSession]:
    """PostgreSQL counterpart of ``test_db``, for seeding and soft deletes."""
    factory = async_sessionmaker(pg_engines.async_, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def pg_seed(pg_db: AsyncSession, tmp_path: Path) -> None:
    """The shared seed plus a second org-a document (so A's pages are real)
    and ``D``, an active user with no organization.

    Document ``search_vector`` is built by the production ingest helper;
    thread and message vectors come from the migration's insert triggers.
    """
    from src.services.search.fulltext_search_service import fulltext_search_service

    await seed_accounts(pg_db, tmp_path)
    await _add(pg_db, _document("a-doc-2", "a", "a", tmp_path, public=False))
    await _add(pg_db, _user("d", None))
    await fulltext_search_service.async_update_document_search_vectors(
        [sid(key) for key in PG_DOCUMENT_KEYS], pg_db
    )
    await pg_db.commit()


@pytest_asyncio.fixture
async def pg_clients(
    pg_engines: PgEngines, pg_seed: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Clients]:
    """``pg_clients("A")``: ``clients`` on the PostgreSQL schema. Also
    overrides ``get_db_sync``, which the search routes use."""
    async with _authenticated_clients(
        async_sessionmaker(pg_engines.async_, expire_on_commit=False),
        monkeypatch,
        sync_factory=sessionmaker(pg_engines.sync, autocommit=False, autoflush=False),
    ) as client:
        yield client


def assert_no_canary(body: bytes | str, *keys: str) -> None:
    """Fail with the leaked key's name, never the full response."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    leaked = [k for k in keys if canary(k) in text]
    assert not leaked, f"leaked canaries: {leaked}"


B_PRIVATE_CHAT_KEYS = ("b-ws", "b-conv", "b-thread", "b-msg")


async def soft_delete(db: AsyncSession, model: Any, key: str) -> None:
    """Soft-delete one seeded row with a plain UPDATE (no identity-map state)."""
    await db.execute(update(model).where(model.id == sid(key)).values(is_deleted=True))
    await db.commit()

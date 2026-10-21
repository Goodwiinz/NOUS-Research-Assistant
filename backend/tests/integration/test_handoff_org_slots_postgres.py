"""GOO-423: native migration replay, tenant chains and downgrade preservation.

Uses a disposable PostgreSQL database and the real Alembic chain. The service
receives explicit synthetic contexts; this is not HTTP or deployed acceptance.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import (
    Insert,
    column,
    create_engine,
    delete,
    insert,
    select,
    table,
    text,
    update,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from src.models import (
    Collection,
    Conversation,
    Organization,
    Thread,
    User,
    UserRole,
    Workspace,
)
from src.models.integration_grant import IntegrationGrant
from src.models.integration_handoff import IntegrationHandoff
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_handoff import HandoffCreate, HandoffDTO
from src.services.integrations.handoffs import read_latest, save
from tests.integration.two_account.conftest import pg_url

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]
BACKEND = Path(__file__).resolve().parents[2]
HEAD = "ho04_handoff_org_slots"
PARENT = "jd01_document_embeddings"


def _admin(statement: str) -> None:
    engine = create_engine(
        pg_url("psycopg2"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(statement)
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def scratch_database() -> Iterator[str]:
    # Handoff keys need PostgreSQL, not pgvector. The stock CI image must run it.
    name = f"handoff_{uuid4().hex[:12]}"
    _admin(f'CREATE DATABASE "{name}"')
    try:
        engine = create_engine(
            make_url(pg_url("psycopg2")).set(database=name), poolclass=NullPool
        )
        try:
            result = _migrate(engine, "upgrade", "head")
            assert result.returncode == 0, result.stderr[-2000:]
        finally:
            engine.dispose()
        yield name
    finally:
        _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="module")
def sync_engine(scratch_database: str) -> Iterator[Engine]:
    engine = create_engine(
        make_url(pg_url("psycopg2")).set(database=scratch_database), poolclass=NullPool
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest_asyncio.fixture
async def async_engine(scratch_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        make_url(pg_url("asyncpg")).set(database=scratch_database), poolclass=NullPool
    )
    try:
        yield engine
    finally:
        await engine.dispose()


def _same_record(actual: HandoffDTO | None, expected: HandoffDTO) -> None:
    assert actual is not None
    assert actual == expected


def _migrate(
    engine: Engine, command: str, target: str
) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "SUPABASE_DB_URL"}
    env["DATABASE_URL"] = engine.url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


# The test migrates to HEAD, which is older than later revisions that add
# columns to integration_grants (ac01's binding_kind), so a grant is written
# through only the columns that exist at HEAD rather than the current model.
_GRANT_COLUMNS = (
    "id",
    "user_id",
    "organization_id",
    "project_id",
    "thread_id",
    "scopes",
    "token_hash",
    "expires_at",
    "consented_at",
    "created_at",
    "updated_at",
    "is_deleted",
)
_GRANTS = table(
    IntegrationGrant.__tablename__,
    *(column(name, IntegrationGrant.__table__.c[name].type) for name in _GRANT_COLUMNS),
)


def _grant(ids: dict[str, UUID], organization: str, suffix: str) -> Insert:
    now = datetime.now(timezone.utc)
    return insert(_GRANTS).values(
        id=ids[f"grant-{suffix}"],
        user_id=ids["user"],
        organization_id=ids[organization],
        project_id=ids["project"],
        thread_id=ids["thread"],
        scopes=["handoff:read", "handoff:write"],
        token_hash=hashlib.sha256(str(ids[f"grant-{suffix}"]).encode()).hexdigest(),
        expires_at=now + timedelta(hours=1),
        consented_at=now,
        created_at=now,
        updated_at=now,
        is_deleted=False,
    )


def _seed(engine: Engine) -> dict[str, UUID]:
    ids = {
        name: uuid4()
        for name in [
            "org-a",
            "org-b",
            "user",
            "workspace",
            "project",
            "conversation",
            "thread",
            "grant-a",
            "grant-b",
            "handoff",
        ]
    }
    with Session(engine) as db:
        db.add_all(
            [
                Organization(id=ids[o], name=o, storage_limit_bytes=10**6)
                for o in ["org-a", "org-b"]
            ]
        )
        db.flush()
        db.add(
            User(
                id=ids["user"],
                organization_id=ids["org-a"],
                email=f'{ids["user"]}@example.test',
                password_hash="unused",
                first_name="Test",
                last_name="User",
                role=UserRole.USER,
                is_active=True,
            )
        )
        db.flush()
        db.add(
            Workspace(
                id=ids["workspace"],
                owner_id=ids["user"],
                organization_id=None,
                name="Legacy workspace",
            )
        )
        db.flush()
        db.add(
            Collection(id=ids["project"], workspace_id=ids["workspace"], name="Project")
        )
        db.flush()
        db.add(
            Conversation(
                id=ids["conversation"],
                workspace_id=ids["workspace"],
                created_by_id=ids["user"],
                title="Conversation",
            )
        )
        db.flush()
        db.add(
            Thread(
                id=ids["thread"],
                conversation_id=ids["conversation"],
                source_project_id=ids["project"],
                created_by_id=ids["user"],
            )
        )
        db.flush()
        db.execute(_grant(ids, "org-a", "a"))
        db.commit()
    return ids


def _context(
    ids: dict[str, UUID], organization: str, suffix: str
) -> IntegrationContext:
    return IntegrationContext(
        user_id=ids["user"],
        organization_id=ids[organization],
        project_id=ids["project"],
        thread_id=ids["thread"],
        grant_id=ids[f"grant-{suffix}"],
        consent_id=None,
    )


def _payload(ids: dict[str, UUID], goal: str) -> HandoffCreate:
    return HandoffCreate(
        handoff_id=ids["handoff"],
        expected_parent_version=None,
        goal=goal,
        decisions=[],
        remaining=[],
        results=[],
        harness_name="codex",
    )


async def test_org_scoped_slots_preserve_history_and_refuse_lossy_downgrade(
    sync_engine: Engine, async_engine: AsyncEngine
) -> None:
    before = _migrate(sync_engine, "downgrade", PARENT)
    assert before.returncode == 0, before.stderr[-2000:]
    ids = _seed(sync_engine)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    async with sessions() as db:
        saved = await save(
            db, _context(ids, "org-a", "a"), _payload(ids, "Old org history")
        )
        # Compare persisted records before and after DDL, including timestamps.
        previous = await read_latest(db, _context(ids, "org-a", "a"))
        assert previous is not None and previous.id == saved.id
    upgrade = _migrate(sync_engine, "upgrade", HEAD)
    assert upgrade.returncode == 0, upgrade.stderr[-2000:]
    async with sessions() as db:
        _same_record(await read_latest(db, _context(ids, "org-a", "a")), previous)
        await db.execute(
            update(User)
            .where(User.id == ids["user"])
            .values(organization_id=ids["org-b"])
        )
        await db.execute(_grant(ids, "org-b", "b"))
        await db.commit()
        context = _context(ids, "org-b", "b")
        assert await read_latest(db, context) is None
        saved = await save(db, context, _payload(ids, "New org history"))
        current = await read_latest(db, context)
        assert current is not None and current.id == saved.id
        assert current.version == 1 and current.id != previous.id
        _same_record(await save(db, context, _payload(ids, "New org history")), current)
        assert await read_latest(db, _context(ids, "org-a", "a")) is None

        # The database still refuses either kind of duplicate inside one org.
        for version, retry_id in [(1, uuid4()), (2, ids["handoff"])]:
            with pytest.raises(IntegrityError) as duplicate:
                await db.execute(
                    insert(IntegrationHandoff).values(
                        organization_id=ids["org-b"],
                        project_id=ids["project"],
                        thread_id=ids["thread"],
                        version=version,
                        handoff_id=retry_id,
                        goal="Duplicate",
                        decisions=[],
                        remaining=[],
                        results=[],
                        harness_name="codex",
                        grant_id=ids["grant-b"],
                        created_by_user_id=ids["user"],
                    )
                )
            assert "uq_integration_handoffs_org_thread_project_" in str(
                duplicate.value.orig
            )
            await db.rollback()

    # Both organizations occupy v1. Refusal leaves rows, keys and revision intact.
    refusal = _migrate(sync_engine, "downgrade", PARENT)
    assert (
        refusal.returncode != 0
        and "Cannot restore organization-agnostic handoff keys" in refusal.stderr
    )
    async with sessions() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        rows = (await db.scalars(select(IntegrationHandoff))).all()
        assert {row.goal for row in rows} == {"Old org history", "New org history"}
        constraints = (
            await db.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='integration_handoffs'::regclass AND contype='u'"
                )
            )
        ).all()
        assert len(constraints) == 2
        assert all(
            "organization_id, thread_id, project_id" in definition
            for _, definition in constraints
        )
        # Now only the retry id collides across organizations: that also refuses.
        await db.execute(
            update(IntegrationHandoff)
            .where(IntegrationHandoff.id == current.id)
            .values(version=2)
        )
        await db.commit()
    second = _migrate(sync_engine, "downgrade", PARENT)
    assert (
        second.returncode != 0
        and "Cannot restore organization-agnostic handoff keys" in second.stderr
    )
    async with sessions() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        assert (
            await db.scalar(
                select(IntegrationHandoff.goal).where(
                    IntegrationHandoff.id == previous.id
                )
            )
            == "Old org history"
        )
        await db.execute(
            delete(IntegrationHandoff).where(IntegrationHandoff.id == current.id)
        )
        await db.commit()
    clean = _migrate(sync_engine, "downgrade", PARENT)
    assert clean.returncode == 0, clean.stderr[-2000:]
    again = _migrate(sync_engine, "upgrade", HEAD)
    assert again.returncode == 0, again.stderr[-2000:]
    async with sessions() as db:
        assert (
            await db.scalar(
                select(IntegrationHandoff.goal).where(
                    IntegrationHandoff.id == previous.id
                )
            )
            == "Old org history"
        )

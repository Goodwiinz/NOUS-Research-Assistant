"""Real PostgreSQL evidence that one lapsed grant resumes once.

Run with ORCHESTRATION_TEST_DATABASE_URL configured, PYTHONPATH=backend:
python -m pytest -o addopts='' -q
backend/tests/unit/services/integrations/test_grant_resume_postgres.py

Without the URL the test is skipped; a skip is NOT RUN, never PASS.

Both resumes are held until each has passed its checks, so they reach the
compare-and-set together. Mutation verified 2026-10-13 (local PostgreSQL
14): removing ``IntegrationGrant.revoked_at.is_(None)`` from the update in
``services/integrations/context.py::_replace_grant`` lets both resumes
commit a successor, and the test fails with two issued grants. Restoring it
makes the test pass.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.bridge_device import BridgeDevice
from src.models.collection import Collection
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_context import (
    DeviceCreate,
    GrantRequestCreate,
    IssuedGrant,
)
from src.services.integrations import context as service

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]
USER, ORG, WORKSPACE, PROJECT = uuid4(), uuid4(), uuid4(), uuid4()
OWNER = SimpleNamespace(id=USER, organization_id=ORG)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "grant_resume_" + uuid4().hex
    admin = create_async_engine(dsn)
    async with admin.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        dsn, connect_args={"server_settings": {"search_path": schema}}
    )
    models: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
        BridgeDevice,
        IntegrationGrantRequest,
        IntegrationGrant,
    ]
    try:
        async with engine.begin() as conn:
            # Nullable chat/run associations keep their real FK targets.
            await conn.exec_driver_sql("CREATE TABLE threads (id uuid PRIMARY KEY)")
            await conn.exec_driver_sql(
                "CREATE TABLE agent_runs (job_id varchar(36) PRIMARY KEY)"
            )
            await conn.run_sync(
                Base.metadata.create_all, tables=[model.__table__ for model in models]
            )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            db.add(Organization(id=ORG, name="org", storage_limit_bytes=1000000))
            await db.flush()
            await db.execute(
                text(
                    """INSERT INTO users (id, created_at, updated_at, is_deleted,
                email, password_hash, first_name, last_name, role, is_active,
                organization_id, login_count) VALUES (:id, now(), now(), false,
                'owner@example.test', 'unused', 'Test', 'User', 'USER', true, :org, 0)"""
                ),
                {"id": USER, "org": ORG},
            )
            db.add(Workspace(id=WORKSPACE, name="w", owner_id=USER, organization_id=ORG))
            await db.flush()
            db.add(Collection(id=PROJECT, name="p", workspace_id=WORKSPACE))
            await db.commit()
        yield sessions
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


async def _lapsed_pairing(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[Any, IssuedGrant]:
    async with sessions() as db:
        device = await service.register_device(db, OWNER, DeviceCreate(label="L"))
        request = await service.create_request(
            db,
            OWNER,
            GrantRequestCreate(
                project_id=PROJECT,
                device_id=device.id,
                scopes={"harness:execute", "tools:read"},
            ),
        )
        await service.decide_request(db, OWNER, request.id, True)
        issued = await service.exchange_request(db, OWNER, request.id)
        await db.execute(
            update(IntegrationGrant)
            .where(IntegrationGrant.id == issued.grant_id)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
        )
        await db.commit()
        return request.id, issued


async def test_concurrent_resumes_of_one_grant_issue_one_successor(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    consent, issued = await _lapsed_pairing(factory)
    # Hold both resumes after their checks so they reach the update together.
    barrier = asyncio.Barrier(2)
    checked = service.validate_binding

    async def validate_then_wait(*args: Any, **kwargs: Any) -> None:
        await checked(*args, **kwargs)
        await asyncio.wait_for(barrier.wait(), timeout=10)

    monkeypatch.setattr(service, "validate_binding", validate_then_wait)

    async def resume() -> IssuedGrant:
        async with factory() as db:
            return await service.resume_grant(
                db, OWNER, issued.grant_id, issued.token
            )

    results = await asyncio.wait_for(
        asyncio.gather(resume(), resume(), return_exceptions=True), timeout=30
    )
    monkeypatch.setattr(service, "validate_binding", checked)
    won = [r for r in results if isinstance(r, IssuedGrant)]
    lost = [r for r in results if isinstance(r, BaseException)]
    assert len(won) == 1, results
    assert len(lost) == 1 and isinstance(lost[0], service.IntegrationConflict), lost

    async with factory() as db:
        live = await db.scalar(
            select(func.count())
            .select_from(IntegrationGrant)
            .where(
                IntegrationGrant.request_id == consent,
                IntegrationGrant.revoked_at.is_(None),
            )
        )
        assert live == 1
        assert await db.scalar(select(func.count()).select_from(IntegrationGrant)) == 2
        context = await service.resolve_integration_context(
            db, won[0].token, required_scope="harness:execute"
        )
        assert context.consent_id == consent

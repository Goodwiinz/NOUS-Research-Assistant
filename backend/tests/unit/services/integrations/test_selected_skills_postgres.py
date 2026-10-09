"""Real PostgreSQL migration and serialization evidence for selected skills.

Run with ORCHESTRATION_TEST_DATABASE_URL configured, PYTHONPATH=backend:
python -m pytest -o addopts='' -q <this file>

Mutation verified 2026-10-09: removing the conditional FOR UPDATE in
selected_context._owned_consent makes the concurrent-load test fail with
"load escaped consent lock". Restoring it makes the test pass. The held
consent row also serializes browser replacement against skill loading.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.models.agent_runtime_snapshot import AgentRuntimeSnapshot
from src.models.base import Base
from src.models.bridge_device import BridgeDevice
from src.models.collection import Collection
from src.models.integration_context_selection import IntegrationContextSelection
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.project_memory import ProjectMemory
from src.models.project_skill import (
    ProjectSkill,
    ProjectSkillVersion,
    ProjectSkillVersionScan,
)
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.services.integrations import selected_context as service
from tests.unit.services.integrations.test_selected_context import (
    CONSENT,
    GRANT,
    KEEP,
    ORG,
    PROJECT,
    USER,
    WORKSPACE,
    _grant_context,
    _user,
)
from tests.unit.services.integrations.test_selected_skills import skill

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.fixture
async def pg(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "selected_skills_" + uuid4().hex
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
        ProjectMemory,
        IntegrationGrantRequest,
        IntegrationGrant,
        AgentRuntimeSnapshot,
        IntegrationContextSelection,
        ProjectSkill,
        ProjectSkillVersion,
        ProjectSkillVersionScan,
    ]
    try:
        async with engine.begin() as conn:
            # Nullable native run/thread associations retain their real FK types.
            await conn.exec_driver_sql("CREATE TABLE threads (id uuid PRIMARY KEY)")
            await conn.exec_driver_sql(
                "CREATE TABLE agent_runs (job_id varchar(36) PRIMARY KEY)"
            )
            await conn.run_sync(
                Base.metadata.create_all, tables=[model.__table__ for model in models]
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        device = uuid4()
        async with factory() as db:
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
            for row in (
                Workspace(id=WORKSPACE, name="w", owner_id=USER, organization_id=ORG),
                Collection(id=PROJECT, name="p", workspace_id=WORKSPACE),
                BridgeDevice(
                    id=device, user_id=USER, organization_id=ORG, label="test"
                ),
                ProjectMemory(
                    id=KEEP, user_id=USER, project_id=PROJECT, content="Cite in APA"
                ),
                IntegrationGrantRequest(
                    id=CONSENT,
                    user_id=USER,
                    organization_id=ORG,
                    project_id=PROJECT,
                    device_id=device,
                    scopes=["context:read"],
                    status="consumed",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                ),
                IntegrationGrant(
                    id=GRANT,
                    user_id=USER,
                    organization_id=ORG,
                    project_id=PROJECT,
                    device_id=device,
                    request_id=CONSENT,
                    scopes=["context:read"],
                    token_hash="a" * 64,
                    consented_at=datetime.now(timezone.utc),
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                ),
            ):
                db.add(row)
                await db.flush()
            await db.commit()
        monkeypatch.setattr(settings, "PROJECT_SKILL_CATALOG_ENABLED", True)
        monkeypatch.setattr(settings, "PROJECT_SKILL_RUNTIME_ENABLED", True)
        yield engine, factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


def migration(connection: Any, direction: str) -> None:
    path = (
        Path(__file__).resolve().parents[4]
        / "alembic/versions/ic02_selected_skill_snapshot.py"
    )
    spec = importlib.util.spec_from_file_location("selected_skills_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def test_migration_roundtrip_preserves_memory_selection_and_fk(pg: Any) -> None:
    engine, factory = pg
    async with factory() as db:
        await service.save_selection(db, await _user(db), CONSENT, [KEEP])
    async with engine.begin() as conn:
        await conn.run_sync(migration, "downgrade")
        columns = await conn.run_sync(
            lambda c: {
                v["name"]
                for v in inspect(c).get_columns("integration_context_selections")
            }
        )
        assert "runtime_snapshot_id" not in columns
        await conn.run_sync(migration, "upgrade")
    async with factory() as db:
        selection = await db.scalar(select(IntegrationContextSelection))
        assert selection.memory_ids == [str(KEEP)] and selection.skill_version_ids == []
        chosen = await skill(db)
        await service.save_selection(
            db, await _user(db), CONSENT, [KEEP], skill_version_ids=[chosen]
        )
        await db.execute(delete(AgentRuntimeSnapshot))
        await db.commit()
        await db.refresh(selection)
        assert selection.runtime_snapshot_id is None and selection.memory_ids == [
            str(KEEP)
        ]
        assert (await service.read_selected_context(db, _grant_context())).is_error


async def test_concurrent_loads_wait_for_consent_and_preserve_three_receipts(
    pg: Any,
) -> None:
    _, factory = pg
    async with factory() as db:
        ids = [await skill(db, f"rubric-{index}") for index in range(4)]
        await service.save_selection(
            db, await _user(db), CONSENT, [], skill_version_ids=ids
        )
        for index in range(2):
            assert not (
                await service.load_selected_skill(
                    db, _grant_context(), f"rubric-{index}"
                )
            ).is_error
    async with factory() as holder, factory() as first, factory() as second:
        await holder.scalar(
            select(IntegrationGrantRequest)
            .where(IntegrationGrantRequest.id == CONSENT)
            .with_for_update()
        )
        tasks = [
            asyncio.create_task(
                service.load_selected_skill(db, _grant_context(), f"rubric-{index}")
            )
            for db, index in ((first, 2), (second, 3))
        ]
        try:
            await asyncio.sleep(0.2)
            assert not any(task.done() for task in tasks), "load escaped consent lock"
            await holder.rollback()
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            assert sum(not result.is_error for result in results) == 1
            assert {
                result.content[0]["error"] for result in results if result.is_error
            } == {"project_skill_load_limit"}
        finally:
            await holder.rollback()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    async with factory() as db:
        snapshot = await db.scalar(select(AgentRuntimeSnapshot))
        assert len(snapshot.loaded_skill_versions) == 3

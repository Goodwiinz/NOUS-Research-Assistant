r"""Real-Postgres evidence that ``publish_version`` commits its rows in FK order.

``ArtifactReference`` and ``ArtifactLifecycleOutbox`` carry foreign keys to
``artifact_versions`` but declare no ORM relationship, so SQLAlchemy's unit of
work has no dependency to order their INSERTs after the version row. On
PostgreSQL the flush then failed with a foreign-key violation, and the
``IntegrityError`` handler reported it as ``ArtifactConflict`` (HTTP 409
"Artifact publication conflict") for every publication. SQLite does not
enforce foreign keys by default, so the unit suite in
``tests/unit/services/artifacts`` never saw it.

Mutation check (``docs/engineering/testing.md``): move the ``db.add(reference)``
and the outbox ``db.add`` back above the first ``await db.flush()`` in
``publish_version`` and this test fails with ``ArtifactConflict``.

Requires ``ORCHESTRATION_TEST_DATABASE_URL``; skipped (NOT RUN) otherwise.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any, cast

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models.agent_run import AgentRun
from src.models.agent_runtime_snapshot import AgentRuntimeSnapshot
from src.models.artifact import (
    Artifact,
    ArtifactLifecycleOutbox,
    ArtifactReference,
    ArtifactUpload,
    ArtifactVersion,
)
from src.models.base import Base
from src.models.chat_message import ChatMessage
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.artifact import (
    ArtifactProvenance,
    PublishVersionRequest,
    ReserveArtifactUploadRequest,
)
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts import service
from src.services.artifacts.service import publish_version, reserve_upload, store_upload
from src.services.artifacts.storage import MemoryArtifactStorage

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

_MODELS: list[Any] = [
    Organization,
    User,
    Workspace,
    WorkspaceMember,
    Collection,
    Conversation,
    Thread,
    ChatMessage,
    AgentRun,
    AgentRuntimeSnapshot,
    Artifact,
    ArtifactVersion,
    ArtifactUpload,
    ArtifactReference,
    ArtifactLifecycleOutbox,
]

CONTENT = b"harness live proof\n"


def _async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    return dsn


async def test_publish_version_inserts_version_before_its_dependents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    storage = MemoryArtifactStorage()
    monkeypatch.setattr(service, "get_artifact_storage", lambda: storage)
    schema = "artifact_publish_" + uuid.uuid4().hex
    admin = create_async_engine(_async_dsn(dsn))
    async with admin.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        _async_dsn(dsn), connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as connection:
            tables = [cast(Any, model).__table__ for model in _MODELS]
            await connection.run_sync(Base.metadata.create_all, tables=tables)
        user, org, workspace, project = (uuid.uuid4() for _ in range(4))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            await db.execute(
                text("""INSERT INTO organizations (id, created_at, updated_at,
                    is_deleted, name, storage_tier, storage_used_bytes,
                    storage_limit_bytes, is_active) VALUES (:id, now(), now(),
                    false, 'org', 'FREE', 0, 1000000, true)"""),
                {"id": org},
            )
            await db.execute(
                text("""INSERT INTO users (id, created_at, updated_at, is_deleted,
                    email, password_hash, first_name, last_name, role, is_active,
                    organization_id, login_count) VALUES (:id, now(), now(), false,
                    :email, 'unused', 'x', 'y', 'USER', true, :org, 0)"""),
                {"id": user, "email": f"{user}@example.test", "org": org},
            )
            # One flush per parent level: no relationships, so FK order is manual.
            for row in (
                Workspace(id=workspace, name="w", owner_id=user, organization_id=org),
                Collection(id=project, workspace_id=workspace, name="p"),
            ):
                db.add(row)
                await db.flush()
            await db.commit()

            # Standalone (Codex Desktop over MCP) publication: no run, no thread.
            context = IntegrationContext(
                user_id=user,
                organization_id=org,
                project_id=project,
                thread_id=None,
                grant_id=uuid.uuid4(),
            )
            reserve = ReserveArtifactUploadRequest(
                publication_id=uuid.uuid4(),
                byte_size=len(CONTENT),
                mime_type="text/markdown",
                sha256=hashlib.sha256(CONTENT).hexdigest(),
            )
            upload = await reserve_upload(db, context, reserve)
            await store_upload(db, context, upload.upload_id, CONTENT)
            request = PublishVersionRequest(
                publication_id=reserve.publication_id,
                upload_id=upload.upload_id,
                title="proof.md",
                provenance=ArtifactProvenance(producer="harness"),
            )
            first = await publish_version(db, context, request)
            # Identical retry inside the same grant returns the same version.
            second = await publish_version(db, context, request)
            assert second.version_id == first.version_id

            versions = await db.scalar(
                select(func.count()).select_from(ArtifactVersion)
            )
            references = await db.scalar(
                select(func.count()).select_from(ArtifactReference)
            )
            outbox = await db.scalar(
                select(func.count()).select_from(ArtifactLifecycleOutbox)
            )
            assert (versions, references, outbox) == (1, 1, 1)
            artifact = await db.get(Artifact, first.artifact_id)
            assert artifact is not None
            assert artifact.current_version_id == first.version_id
            claimed = await db.get(ArtifactUpload, upload.upload_id)
            assert claimed is not None and claimed.version_id == first.version_id
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()

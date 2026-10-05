"""GET /artifacts/projects/{id}: transport mapping over a real SQLite session."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.artifact import Artifact, ArtifactVersion
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole

pytestmark = pytest.mark.unit
OWNER, MEMBER, OUTSIDER = uuid4(), uuid4(), uuid4()
ORG, OTHER_ORG, WORKSPACE, PROJECT = (uuid4() for _ in range(4))
ARTIFACT, VERSION = uuid4(), uuid4()


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'route.db'}")
    tables: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
        Conversation,
        Thread,
        Artifact,
        ArtifactVersion,
    ]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        await db.execute(
            insert(Organization).values(
                [
                    dict(id=ORG, name="Test", storage_limit_bytes=1000000),
                    dict(id=OTHER_ORG, name="Other", storage_limit_bytes=1000000),
                ]
            )
        )
        for user_id, org in ((OWNER, ORG), (MEMBER, ORG), (OUTSIDER, OTHER_ORG)):
            await db.execute(
                text(
                    "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
                ),
                {"id": str(user_id), "org": str(org), "email": f"{user_id}@x.test"},
            )
        await db.execute(
            insert(Workspace).values(
                id=WORKSPACE, name="W", owner_id=OWNER, organization_id=ORG
            )
        )
        await db.execute(
            insert(Collection).values(id=PROJECT, name="P", workspace_id=WORKSPACE)
        )
        await db.execute(
            insert(WorkspaceMember).values(
                workspace_id=WORKSPACE, user_id=MEMBER, role=WorkspaceRole.VIEWER
            )
        )
        await db.execute(
            insert(ArtifactVersion).values(
                id=VERSION,
                artifact_id=ARTIFACT,
                upload_id=uuid4(),
                title="report.md",
                mime_type="text/markdown",
                byte_size=7,
                sha256="a" * 64,
                storage_key="private/key",
                producer="harness",
                provenance={"producer": "harness"},
                created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            )
        )
        await db.execute(
            insert(Artifact).values(
                id=ARTIFACT,
                organization_id=ORG,
                project_id=PROJECT,
                owner_id=OWNER,
                title="report.md",
                current_version_id=VERSION,
            )
        )
        await db.commit()
        yield db
    await engine.dispose()


@pytest.fixture
def app(session: AsyncSession) -> FastAPI:
    from src.api import artifacts

    async def get_session() -> AsyncIterator[AsyncSession]:
        yield session

    application = FastAPI()
    application.include_router(artifacts.router, prefix="/api/v1")
    application.dependency_overrides[get_db] = get_session
    return application


def _as(app: FastAPI, user_id: UUID, org_id: UUID | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id, organization_id=org_id
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _get(client: TestClient, project: UUID = PROJECT) -> Any:
    return client.get(f"/api/v1/artifacts/projects/{project}")


def test_member_lists_current_versions_without_storage_keys(
    app: FastAPI, client: TestClient
) -> None:
    _as(app, MEMBER, ORG)
    response = _get(client)
    assert response.status_code == 200
    (row,) = response.json()
    assert row["artifact_id"] == str(ARTIFACT)
    assert row["current_version"]["version_id"] == str(VERSION)
    assert row["current_version"]["sha256"] == "a" * 64
    assert row["thread_id"] is None
    assert "storage_key" not in response.text


def test_outsider_gets_a_stable_404(app: FastAPI, client: TestClient) -> None:
    _as(app, OUTSIDER, OTHER_ORG)
    response = _get(client)
    assert response.status_code == 404
    assert response.json() == {"detail": "Artifact not found"}


def test_unknown_project_is_404(app: FastAPI, client: TestClient) -> None:
    _as(app, MEMBER, ORG)
    assert _get(client, uuid4()).status_code == 404


@pytest.mark.parametrize("model", [Collection, Workspace])
async def test_deleted_ancestor_is_404(
    app: FastAPI, session: AsyncSession, model: Any
) -> None:
    await session.execute(update(model).values(is_deleted=True))
    await session.commit()
    _as(app, MEMBER, ORG)
    with TestClient(app) as client:
        assert _get(client).status_code == 404


def test_user_without_organization_is_403(app: FastAPI, client: TestClient) -> None:
    _as(app, MEMBER, None)
    assert _get(client).status_code == 403

"""Unit tests for Research Engine API endpoints.

Tests cover:
- POST /api/v1/research-engine/projects — Create project (201)
- GET /api/v1/research-engine/projects — List projects (200)
- POST /api/v1/research-engine/projects without name — 422
- GET /api/v1/research-engine/blueprints/templates — List templates (200, >= 3)
- POST /api/v1/research-engine/blueprints/{blueprint_id}/runs — Start run (201)
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.services.research_engine.project_access import ResearchAction


@pytest.fixture(autouse=True)
def _daily_brief_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """GOO-338: the flag defaults off; these tests exercise the enabled feature."""
    monkeypatch.setattr("src.core.config.settings.DAILY_RESEARCH_BRIEF_ENABLED", True)


# ============================================================================
# Helpers
# ============================================================================


def _make_mock_user():
    """Create a mock user for auth override."""
    user = Mock()
    user.id = uuid.uuid4()
    user.email = "researcher@example.com"
    user.is_active = True
    return user


def _make_mock_project(**overrides):
    """Create a mock ResearchProject ORM object."""
    now = datetime.now(timezone.utc)
    project = Mock()
    project.id = overrides.get("id", uuid.uuid4())
    project.name = overrides.get("name", "Test Project")
    project.description = overrides.get("description", "A test project")
    project.owner_id = overrides.get("owner_id", uuid.uuid4())
    project.collection_id = overrides.get("collection_id", None)
    project.status = overrides.get("status", "active")
    project.settings = overrides.get("settings", {})
    project.is_deleted = overrides.get("is_deleted", False)
    project.created_at = overrides.get("created_at", now)
    project.updated_at = overrides.get("updated_at", now)
    return project


def _make_mock_blueprint(**overrides):
    """Create a mock ResearchBlueprint ORM object."""
    now = datetime.now(timezone.utc)
    bp = Mock()
    bp.id = overrides.get("id", uuid.uuid4())
    bp.project_id = overrides.get("project_id", uuid.uuid4())
    bp.name = overrides.get("name", "Test Blueprint")
    bp.template_source = overrides.get("template_source", None)
    bp.version = overrides.get("version", 1)
    bp.steps = overrides.get("steps", [])
    bp.parameters = overrides.get("parameters", {})
    bp.is_immutable = overrides.get("is_immutable", False)
    bp.created_at = overrides.get("created_at", now)
    bp.updated_at = overrides.get("updated_at", now)
    return bp


def _make_mock_run(**overrides):
    """Create a mock ResearchRun ORM object."""
    now = datetime.now(timezone.utc)
    run = Mock()
    run.id = overrides.get("id", uuid.uuid4())
    run.blueprint_id = overrides.get("blueprint_id", uuid.uuid4())
    run.blueprint_version = overrides.get("blueprint_version", 1)
    run.status = overrides.get("status", "pending")
    run.started_at = overrides.get("started_at", None)
    run.completed_at = overrides.get("completed_at", None)
    run.total_tokens = overrides.get("total_tokens", 0)
    run.protocol_version_id = overrides.get("protocol_version_id", uuid.uuid4())
    run.effective_plan_hash = overrides.get("effective_plan_hash", "a" * 64)
    run.conformance_status = overrides.get("conformance_status", "plan_verified")
    run.created_at = overrides.get("created_at", now)
    run.updated_at = overrides.get("updated_at", now)
    return run


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_current_user():
    return _make_mock_user()


@pytest.fixture
def mock_db():
    """Return a mock async session."""
    session = AsyncMock()
    session.expire_all = Mock()
    return session


@pytest.fixture
def client(test_app, test_auth_headers, mock_current_user, mock_db):
    """Create a test client with auth and db overrides."""
    from contextlib import asynccontextmanager

    test_app.dependency_overrides[get_current_user] = lambda: mock_current_user
    test_app.dependency_overrides[get_db] = lambda: mock_db

    @asynccontextmanager
    async def _no_lifespan(_app):
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(test_app, headers=test_auth_headers) as c:
            yield c
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


# ============================================================================
# POST /api/v1/research-engine/projects — Create project
# ============================================================================


class TestCreateProject:
    """Tests for POST /api/v1/research-engine/projects."""

    @patch("src.api.research_engine.projects.select")
    def test_create_project_returns_201(
        self, mock_select, client, mock_db, mock_current_user
    ):
        """Creating a project should return 201 with project data."""
        collection_id = uuid.uuid4()
        project = _make_mock_project(
            owner_id=mock_current_user.id, collection_id=collection_id
        )
        context = SimpleNamespace(
            collection=SimpleNamespace(
                id=collection_id,
                name="My Research",
                description="Test description",
                research_status="active",
                created_at=project.created_at,
                updated_at=project.updated_at,
            ),
            workspace=Mock(),
            engine=None,
            organization_id=uuid.uuid4(),
            effective_roles=frozenset(),
            workspace_role=None,
        )

        # Mock db.add, db.commit, db.refresh to set id
        async def fake_refresh(obj):
            obj.id = project.id
            obj.created_at = project.created_at
            obj.updated_at = project.updated_at
            obj.status = "active"
            obj.settings = {}

        mock_db.add = Mock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock(side_effect=fake_refresh)
        result = Mock()
        result.scalars.return_value.first.return_value = None
        result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=result)

        with patch(
            "src.api.research_engine.projects.resolve_project",
            AsyncMock(return_value=context),
        ):
            response = client.post(
                "/api/v1/research-engine/projects",
                json={"collection_id": str(collection_id), "name": "ignored"},
            )

        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "My Research"
        assert "id" in body

    def test_create_project_without_name_returns_422(self, client):
        """Creating a project without a name should return 422."""
        response = client.post(
            "/api/v1/research-engine/projects",
            json={"description": "Missing name"},
        )

        assert response.status_code == 422


# ============================================================================
# GET /api/v1/research-engine/projects — List projects
# ============================================================================


class TestListProjects:
    """Tests for GET /api/v1/research-engine/projects."""

    def test_list_projects_returns_200(self, client, mock_db, mock_current_user):
        """Listing projects should return 200 with a list."""
        collection_id = uuid.uuid4()
        project = _make_mock_project(
            owner_id=mock_current_user.id, collection_id=collection_id
        )
        context = SimpleNamespace(engine=project)

        # Mock the db query chain
        mock_result = AsyncMock()
        mock_scalars = Mock()
        mock_scalars.all = Mock(return_value=[collection_id])
        mock_result.scalars = Mock(return_value=mock_scalars)
        mock_db.execute = AsyncMock(return_value=mock_result)

        response_model = SimpleNamespace(
            model_dump=lambda: {},
        )
        with (
            patch(
                "src.api.research_engine.projects.resolve_project",
                AsyncMock(return_value=context),
            ),
            patch(
                "src.api.research_engine.projects._project_response",
                AsyncMock(
                    return_value={
                        "id": collection_id,
                        "project_id": collection_id,
                        "collection_id": collection_id,
                        "research_engine_project_id": project.id,
                        "blueprint_id": None,
                        "name": project.name,
                        "description": project.description,
                        "status": project.status,
                        "settings": {},
                        "created_at": project.created_at,
                        "updated_at": project.updated_at,
                    }
                ),
            ),
        ):
            response = client.get("/api/v1/research-engine/projects")

        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) >= 1


class TestLinkProject:
    """Tests for explicit legacy engine-project linking."""

    def test_same_org_admin_cannot_claim_another_users_legacy_project(
        self, client, mock_db, mock_current_user
    ):
        collection_id = uuid.uuid4()
        project = _make_mock_project(owner_id=uuid.uuid4(), collection_id=None)
        context = SimpleNamespace(
            collection=SimpleNamespace(id=collection_id),
            workspace=Mock(),
            engine=None,
            organization_id=uuid.uuid4(),
            effective_roles=frozenset(),
            workspace_role=None,
        )
        collection_lock = Mock()
        project_lookup = Mock()
        project_lookup.scalar_one_or_none.return_value = project
        mock_db.execute = AsyncMock(side_effect=[collection_lock, project_lookup])

        with patch(
            "src.api.research_engine.projects.resolve_project",
            AsyncMock(return_value=context),
        ):
            response = client.patch(
                f"/api/v1/research-engine/projects/{project.id}/collection",
                json={"collection_id": str(collection_id)},
            )

        assert response.status_code == 404
        assert project.collection_id is None
        mock_db.commit.assert_not_awaited()


class TestLegacyProjectAccess:
    """Legacy engine IDs follow canonical ownership once mapped."""

    @pytest.mark.asyncio
    async def test_mapped_project_uses_current_canonical_access(
        self, mock_db, mock_current_user
    ):
        from src.services.research_engine.project_access import require_legacy_project

        collection_id = uuid.uuid4()
        project = _make_mock_project(owner_id=uuid.uuid4(), collection_id=collection_id)
        lookup = Mock()
        lookup.scalars.return_value.first.return_value = project
        mock_db.execute.return_value = lookup
        context = SimpleNamespace(engine=project)

        with patch(
            "src.services.research_engine.project_access.resolve_project",
            new=AsyncMock(return_value=context),
        ) as resolve:
            result = await require_legacy_project(
                mock_db, project.id, mock_current_user.id
            )

        assert result is project
        resolve.assert_awaited_once_with(
            mock_db,
            collection_id,
            mock_current_user.id,
            ResearchAction.VIEW,
            require_engine=True,
        )

    @pytest.mark.asyncio
    async def test_mapped_project_rejects_revoked_stale_engine_owner(
        self, mock_db, mock_current_user
    ):
        from src.services.research_engine.project_access import require_legacy_project

        project = _make_mock_project(
            owner_id=mock_current_user.id, collection_id=uuid.uuid4()
        )
        lookup = Mock()
        lookup.scalars.return_value.first.return_value = project
        mock_db.execute.return_value = lookup

        with patch(
            "src.services.research_engine.project_access.resolve_project",
            new=AsyncMock(
                side_effect=HTTPException(status_code=404, detail="Project not found")
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await require_legacy_project(mock_db, project.id, mock_current_user.id)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_unresolved_project_remains_original_owner_only(
        self, mock_db, mock_current_user
    ):
        from src.services.research_engine.project_access import require_legacy_project

        project = _make_mock_project(owner_id=mock_current_user.id, collection_id=None)
        lookup = Mock()
        lookup.scalars.return_value.first.return_value = project
        mock_db.execute.return_value = lookup

        with patch(
            "src.services.research_engine.project_access.resolve_project",
            new=AsyncMock(),
        ) as resolve:
            result = await require_legacy_project(
                mock_db, project.id, mock_current_user.id
            )

        assert result is project
        resolve.assert_not_awaited()


# ============================================================================
# GET /api/v1/research-engine/blueprints/templates — List templates
# ============================================================================


class TestListTemplates:
    """Tests for GET /api/v1/research-engine/blueprints/templates."""

    def test_list_templates_returns_200_with_items(self, client):
        """Listing templates should return 200 with at least 3 items."""
        response = client.get("/api/v1/research-engine/blueprints/templates")

        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) >= 3
        # Each template should have slug, name, description, step_count
        for template in body:
            assert "slug" in template
            assert "name" in template


# ============================================================================
# POST /api/v1/research-engine/blueprints/{blueprint_id}/runs — Start run
# ============================================================================


class TestStartRun:
    """Tests for POST /api/v1/research-engine/blueprints/{blueprint_id}/runs."""

    def test_start_run_returns_201(self, client, mock_db, mock_current_user):
        """Starting a run should return 201 Created."""
        blueprint_id = uuid.uuid4()
        blueprint = _make_mock_blueprint(id=blueprint_id, version=1)
        run = _make_mock_run(blueprint_id=blueprint_id, blueprint_version=1)

        mock_db.add = Mock()
        mock_db.commit = AsyncMock()

        async def fake_refresh(obj):
            obj.id = run.id
            obj.blueprint_id = run.blueprint_id
            obj.blueprint_version = run.blueprint_version
            obj.status = "pending"
            obj.started_at = None
            obj.completed_at = None
            obj.total_tokens = 0
            obj.created_at = run.created_at
            obj.updated_at = run.updated_at

        mock_db.refresh = AsyncMock(side_effect=fake_refresh)

        context = SimpleNamespace(
            engine=SimpleNamespace(id=blueprint.project_id),
            collection=SimpleNamespace(id=uuid.uuid4()),
        )
        protocol_version_id = uuid.uuid4()
        run.protocol_version_id = protocol_version_id
        with (
            patch(
                "src.api.research_engine.runs.require_blueprint",
                AsyncMock(return_value=blueprint),
            ),
            patch(
                "src.api.research_engine.runs.resolve_engine_project_context",
                AsyncMock(return_value=context),
            ),
            patch(
                "src.api.research_engine.runs.create_approved_run",
                AsyncMock(return_value=run),
            ),
        ):
            response = client.post(
                f"/api/v1/research-engine/blueprints/{blueprint_id}/runs",
                json={"protocol_version_id": str(protocol_version_id)},
            )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "pending"
        assert body["protocol_version_id"] == str(protocol_version_id)
        assert "id" in body

    def test_disabled_daily_brief_cannot_start_from_existing_blueprint(
        self, client, mock_current_user, monkeypatch
    ):
        monkeypatch.setattr(
            "src.core.config.settings.DAILY_RESEARCH_BRIEF_ENABLED", False
        )
        blueprint_id = uuid.uuid4()
        blueprint = _make_mock_blueprint(
            id=blueprint_id,
            template_source="daily_research_brief",
            parameters={},
        )
        create_approved_run = AsyncMock()
        with (
            patch(
                "src.api.research_engine.runs.require_blueprint",
                AsyncMock(return_value=blueprint),
            ),
            patch(
                "src.api.research_engine.runs.create_approved_run",
                create_approved_run,
            ),
        ):
            response = client.post(
                f"/api/v1/research-engine/blueprints/{blueprint_id}/runs",
                json={"protocol_version_id": str(uuid.uuid4())},
            )

        assert response.status_code == 404
        assert blueprint.is_immutable is False
        create_approved_run.assert_not_awaited()

    def test_daily_brief_requires_scope_confirmation(
        self,
        client,
    ):
        blueprint_id = uuid.uuid4()
        blueprint = _make_mock_blueprint(
            id=blueprint_id,
            version=1,
            template_source="daily_research_brief",
            parameters={
                "contract_version": 1,
                "research_question": "What changed in grounded generation?",
                "inclusion_criteria": ["Peer-reviewed empirical work"],
                "exclusion_criteria": [],
                "providers": ["openalex", "crossref"],
                "limit_per_provider": 25,
                "notes": "",
            },
        )
        create_approved_run = AsyncMock()
        with (
            patch(
                "src.api.research_engine.runs.require_blueprint",
                AsyncMock(return_value=blueprint),
            ),
            patch(
                "src.api.research_engine.runs.create_approved_run",
                create_approved_run,
            ),
        ):
            response = client.post(
                f"/api/v1/research-engine/blueprints/{blueprint_id}/runs",
                json={"protocol_version_id": str(uuid.uuid4())},
            )

        assert response.status_code == 422
        assert (
            response.json()["error"]["message"]
            == "Daily Brief scope confirmation is required"
        )
        create_approved_run.assert_not_awaited()

    def test_daily_brief_stores_confirmed_effective_scope_with_server_metadata(
        self,
        client,
        mock_db,
        mock_current_user,
    ):
        blueprint_id = uuid.uuid4()
        blueprint = _make_mock_blueprint(
            id=blueprint_id,
            version=1,
            template_source="daily_research_brief",
            parameters={
                "contract_version": 1,
                "research_question": "What changed in grounded generation?",
                "inclusion_criteria": ["Peer-reviewed empirical work"],
                "exclusion_criteria": ["Editorials"],
                "providers": ["pubmed"],
                "limit_per_provider": 10,
                "notes": "Last 24 hours.",
            },
        )
        run = _make_mock_run(blueprint_id=blueprint_id, blueprint_version=1)
        protocol_version_id = uuid.uuid4()
        run.protocol_version_id = protocol_version_id
        confirmed_scope = {
            "research_question": "What changed in grounded generation?",
            "inclusion_criteria": ["Peer-reviewed empirical work"],
            "exclusion_criteria": ["Editorials"],
            "providers": ["pubmed"],
            "limit_per_provider": 10,
            "notes": "Last 24 hours.",
        }
        context = SimpleNamespace(
            engine=SimpleNamespace(id=blueprint.project_id),
            collection=SimpleNamespace(id=uuid.uuid4()),
        )
        create_approved_run = AsyncMock(return_value=run)
        with (
            patch(
                "src.api.research_engine.runs.require_blueprint",
                AsyncMock(return_value=blueprint),
            ),
            patch(
                "src.api.research_engine.runs.resolve_engine_project_context",
                AsyncMock(return_value=context),
            ),
            patch(
                "src.api.research_engine.runs.create_approved_run",
                create_approved_run,
            ),
        ):
            response = client.post(
                f"/api/v1/research-engine/blueprints/{blueprint_id}/runs",
                json={
                    "protocol_version_id": str(protocol_version_id),
                    "scope_confirmation": {**confirmed_scope, "confirmed": True},
                },
            )

        assert response.status_code == 201, response.text
        request = create_approved_run.await_args.args[3]
        assert request.protocol_version_id == protocol_version_id
        assert request.parameters_override == {}
        manifest = create_approved_run.await_args.kwargs["manifest_metadata"]
        confirmation = manifest["scope_confirmation"]
        assert confirmation["contract_version"] == 1
        for key, value in confirmed_scope.items():
            assert confirmation[key] == value
        assert confirmation["confirmed"] is True
        assert confirmation["confirmed_by"] == str(mock_current_user.id)
        assert len(confirmation["configuration_hash"]) == 64
        assert datetime.fromisoformat(confirmation["confirmed_at"]).tzinfo is not None
        assert [entry["id"] for entry in manifest["provider_manifest"]] == ["pubmed"]

    def test_daily_brief_rejects_confirmation_that_differs_from_effective_scope(
        self,
        client,
    ):
        blueprint_id = uuid.uuid4()
        blueprint = _make_mock_blueprint(
            id=blueprint_id,
            version=1,
            template_source="daily_research_brief",
            parameters={
                "contract_version": 1,
                "research_question": "What changed in grounded generation?",
                "inclusion_criteria": ["Peer-reviewed empirical work"],
                "exclusion_criteria": [],
                "providers": ["openalex"],
                "limit_per_provider": 10,
                "notes": "",
            },
        )
        submitted = {
            "research_question": "What changed in grounded generation?",
            "inclusion_criteria": ["Peer-reviewed empirical work"],
            "exclusion_criteria": [],
            "providers": ["openalex"],
            "limit_per_provider": 10,
            "notes": "",
        }
        create_approved_run = AsyncMock()
        with (
            patch(
                "src.api.research_engine.runs.require_blueprint",
                AsyncMock(return_value=blueprint),
            ),
            patch(
                "src.api.research_engine.runs.create_approved_run",
                create_approved_run,
            ),
        ):
            response = client.post(
                f"/api/v1/research-engine/blueprints/{blueprint_id}/runs",
                json={
                    "protocol_version_id": str(uuid.uuid4()),
                    "scope_confirmation": {
                        **submitted,
                        "limit_per_provider": 11,
                        "confirmed": True,
                    },
                },
            )

        assert response.status_code == 422
        assert response.json()["error"]["message"] == (
            "Scope confirmation does not match effective Daily Brief parameters"
        )
        create_approved_run.assert_not_awaited()

    @pytest.mark.parametrize("request_options", [{}, {"json": {}}])
    def test_start_run_without_approved_protocol_returns_409(
        self, client, mock_db, mock_current_user, request_options
    ):
        blueprint_id = uuid.uuid4()
        blueprint = _make_mock_blueprint(id=blueprint_id)
        context = SimpleNamespace(
            engine=SimpleNamespace(id=blueprint.project_id),
            collection=SimpleNamespace(id=uuid.uuid4()),
        )
        with (
            patch(
                "src.api.research_engine.runs.require_blueprint",
                AsyncMock(return_value=blueprint),
            ),
            patch(
                "src.api.research_engine.runs.resolve_engine_project_context",
                AsyncMock(return_value=context),
            ),
            patch(
                "src.api.research_engine.runs.create_approved_run",
                AsyncMock(
                    side_effect=HTTPException(
                        status_code=409, detail="approved_protocol_required"
                    )
                ),
            ),
        ):
            response = client.post(
                f"/api/v1/research-engine/blueprints/{blueprint_id}/runs",
                **request_options,
            )

        assert response.status_code == 409
        assert "approved_protocol_required" in response.text

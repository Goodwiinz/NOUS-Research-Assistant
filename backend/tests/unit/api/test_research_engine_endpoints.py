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
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user

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
    return session


@pytest.fixture
def client(test_app, mock_current_user, mock_db):
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
        with TestClient(test_app) as c:
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
        project = _make_mock_project(owner_id=mock_current_user.id)

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
        count_result = Mock()
        count_result.scalar.return_value = 0
        mock_db.execute = AsyncMock(return_value=count_result)

        response = client.post(
            "/api/v1/research-engine/projects",
            json={"name": "My Research", "description": "Test description"},
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

    @patch("src.api.research_engine.projects.select")
    def test_list_projects_returns_200(
        self, mock_select, client, mock_db, mock_current_user
    ):
        """Listing projects should return 200 with a list."""
        project = _make_mock_project(owner_id=mock_current_user.id)

        # Mock the db query chain
        mock_result = AsyncMock()
        mock_scalars = Mock()
        mock_scalars.all = Mock(return_value=[project])
        mock_result.scalars = Mock(return_value=mock_scalars)
        mock_db.execute = AsyncMock(return_value=mock_result)

        # Mock select chain
        mock_query = MagicMock()
        mock_select.return_value = mock_query
        mock_query.where = MagicMock(return_value=mock_query)
        mock_query.order_by = MagicMock(return_value=mock_query)

        response = client.get("/api/v1/research-engine/projects")

        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) >= 1


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

    @patch("src.api.research_engine.runs.select")
    def test_start_run_returns_201(
        self, mock_select, client, mock_db, mock_current_user
    ):
        """Starting a run should return 201 Created."""
        blueprint_id = uuid.uuid4()
        blueprint = _make_mock_blueprint(id=blueprint_id, version=1)
        run = _make_mock_run(blueprint_id=blueprint_id, blueprint_version=1)

        # Mock select for blueprint lookup
        mock_result = AsyncMock()
        mock_scalars = Mock()
        mock_scalars.first = Mock(return_value=blueprint)
        mock_result.scalars = Mock(return_value=mock_scalars)
        mock_db.execute = AsyncMock(return_value=mock_result)

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

        response = client.post(
            f"/api/v1/research-engine/blueprints/{blueprint_id}/runs",
            json={},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "pending"
        assert "id" in body
        created_run = mock_db.add.call_args.args[0]
        assert created_run.reproducibility_manifest == {"parameters_override": {}}

    @patch("src.api.research_engine.runs.select")
    def test_daily_brief_requires_scope_confirmation(
        self, mock_select, client, mock_db, mock_current_user
    ):
        blueprint_id = uuid.uuid4()
        blueprint = _make_mock_blueprint(
            id=blueprint_id,
            version=1,
            template_source="daily_research_brief",
            parameters={
                "contract_version": 1,
                "research_question": "",
                "inclusion_criteria": [],
                "exclusion_criteria": [],
                "providers": ["openalex", "crossref"],
                "limit_per_provider": 25,
                "notes": "",
            },
        )
        result = Mock()
        scalars = Mock()
        scalars.first.return_value = blueprint
        result.scalars.return_value = scalars
        mock_db.execute = AsyncMock(return_value=result)

        response = client.post(
            f"/api/v1/research-engine/blueprints/{blueprint_id}/runs",
            json={"parameters_override": {}},
        )

        assert response.status_code == 422
        assert (
            response.json()["error"]["message"]
            == "Daily Brief scope confirmation is required"
        )
        mock_db.add.assert_not_called()

    @patch("src.api.research_engine.runs.select")
    def test_daily_brief_stores_confirmed_effective_scope_with_server_metadata(
        self, mock_select, client, mock_db, mock_current_user
    ):
        blueprint_id = uuid.uuid4()
        blueprint = _make_mock_blueprint(
            id=blueprint_id,
            version=1,
            template_source="daily_research_brief",
            parameters={
                "contract_version": 1,
                "research_question": "",
                "inclusion_criteria": [],
                "exclusion_criteria": [],
                "providers": ["openalex", "crossref"],
                "limit_per_provider": 25,
                "notes": "",
            },
        )
        run = _make_mock_run(blueprint_id=blueprint_id, blueprint_version=1)
        result = Mock()
        scalars = Mock()
        scalars.first.return_value = blueprint
        result.scalars.return_value = scalars
        mock_db.execute = AsyncMock(return_value=result)
        mock_db.add = Mock()
        mock_db.commit = AsyncMock()

        async def fake_refresh(obj):
            obj.id = run.id
            obj.created_at = run.created_at
            obj.updated_at = run.updated_at

        mock_db.refresh = AsyncMock(side_effect=fake_refresh)
        overrides = {
            "research_question": "What changed in grounded generation?",
            "inclusion_criteria": ["Peer-reviewed empirical work"],
            "exclusion_criteria": ["Editorials"],
            "providers": ["pubmed"],
            "limit_per_provider": 10,
            "notes": "Last 24 hours.",
        }

        response = client.post(
            f"/api/v1/research-engine/blueprints/{blueprint_id}/runs",
            json={
                "parameters_override": overrides,
                "scope_confirmation": {**overrides, "confirmed": True},
            },
        )

        assert response.status_code == 201, response.text
        created_run = mock_db.add.call_args.args[0]
        manifest = created_run.reproducibility_manifest
        assert manifest["parameters_override"] == overrides
        confirmation = manifest["scope_confirmation"]
        assert confirmation["contract_version"] == 1
        for key, value in overrides.items():
            assert confirmation[key] == value
        assert confirmation["confirmed"] is True
        assert confirmation["confirmed_by"] == str(mock_current_user.id)
        assert len(confirmation["configuration_hash"]) == 64
        assert datetime.fromisoformat(confirmation["confirmed_at"]).tzinfo is not None

    @patch("src.api.research_engine.runs.select")
    def test_daily_brief_rejects_confirmation_that_differs_from_effective_scope(
        self, mock_select, client, mock_db, mock_current_user
    ):
        blueprint_id = uuid.uuid4()
        blueprint = _make_mock_blueprint(
            id=blueprint_id,
            version=1,
            template_source="daily_research_brief",
            parameters={
                "contract_version": 1,
                "research_question": "",
                "inclusion_criteria": [],
                "exclusion_criteria": [],
                "providers": ["openalex"],
                "limit_per_provider": 25,
                "notes": "",
            },
        )
        result = Mock()
        scalars = Mock()
        scalars.first.return_value = blueprint
        result.scalars.return_value = scalars
        mock_db.execute = AsyncMock(return_value=result)
        overrides = {
            "research_question": "What changed in grounded generation?",
            "inclusion_criteria": ["Peer-reviewed empirical work"],
            "exclusion_criteria": [],
            "providers": ["openalex"],
            "limit_per_provider": 10,
            "notes": "",
        }

        response = client.post(
            f"/api/v1/research-engine/blueprints/{blueprint_id}/runs",
            json={
                "parameters_override": overrides,
                "scope_confirmation": {
                    **overrides,
                    "limit_per_provider": 11,
                    "confirmed": True,
                },
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["message"] == (
            "Scope confirmation does not match effective Daily Brief parameters"
        )
        mock_db.add.assert_not_called()

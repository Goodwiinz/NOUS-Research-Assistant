"""API tests for server-owned blueprint template expansion."""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.services.research_engine.blueprints.loader import BlueprintLoader


@pytest.fixture(autouse=True)
def _daily_brief_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """GOO-338: the flag defaults off; these tests exercise the enabled feature."""
    monkeypatch.setattr("src.core.config.settings.DAILY_RESEARCH_BRIEF_ENABLED", True)


@dataclass
class TemplateAPI:
    client: TestClient
    project: Any
    engine_project: Any
    created: list[Any]
    actor: dict[str, Any]
    collection_lookup: AsyncMock


@pytest.fixture
def template_api(
    test_app: FastAPI,
    test_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TemplateAPI]:
    user = Mock(
        id=uuid.uuid4(),
        email="researcher@example.com",
        is_active=True,
    )
    organization_id = uuid.uuid4()
    workspace = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=user.id,
        organization_id=organization_id,
        members=[],
        is_deleted=False,
        is_archived=False,
    )
    project = SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        workspace=workspace,
        name="Canonical research project",
        description=None,
        research_status="active",
        is_deleted=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    engine_project = Mock(
        id=uuid.uuid4(),
        collection_id=project.id,
        owner_id=user.id,
        settings={},
        is_deleted=False,
    )
    result = Mock()
    scalars = Mock()
    scalars.first.return_value = engine_project
    scalars.all.return_value = []
    result.scalars.return_value = scalars
    result.scalar_one_or_none.return_value = organization_id
    result.first.return_value = (project, workspace)

    db = AsyncMock()
    db.execute.return_value = result
    db.commit = AsyncMock()
    created: list[object] = []
    db.add = Mock(side_effect=created.append)

    async def refresh(blueprint: Any) -> None:
        blueprint.id = getattr(blueprint, "id", None) or uuid.uuid4()
        blueprint.created_at = datetime.now(timezone.utc)
        blueprint.updated_at = datetime.now(timezone.utc)

    db.refresh = AsyncMock(side_effect=refresh)
    actor = {"current": user}
    test_app.dependency_overrides[get_current_user] = lambda: actor["current"]
    test_app.dependency_overrides[get_db] = lambda: db

    async def get_collection(
        _db: Any,
        project_id: uuid.UUID,
        _user_id: uuid.UUID,
        *,
        load_documents: bool,
    ) -> Any:
        assert load_documents is False
        return project if project_id == project.id else None

    collection_lookup = AsyncMock(side_effect=get_collection)
    monkeypatch.setattr(
        "src.services.research_engine.project_access.workspace_access.get_collection",
        collection_lookup,
    )

    @asynccontextmanager
    async def no_lifespan(_app: Any) -> AsyncIterator[None]:
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = no_lifespan
    try:
        with TestClient(test_app, headers=test_auth_headers) as client:
            yield TemplateAPI(
                client=client,
                project=project,
                engine_project=engine_project,
                created=created,
                actor=actor,
                collection_lookup=collection_lookup,
            )
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


def test_template_detail_route_returns_validated_full_template_before_uuid_route(
    template_api: TemplateAPI,
) -> None:
    client = template_api.client

    response = client.get(
        "/api/v1/research-engine/blueprints/templates/daily_research_brief"
    )
    listed = client.get("/api/v1/research-engine/blueprints/templates")

    assert response.status_code == 200
    assert "daily_research_brief" in {item["slug"] for item in listed.json()}
    body = response.json()
    assert body["slug"] == "daily_research_brief"
    assert body["contract_version"] == 1
    assert body["template_source"] == "daily_research_brief"
    assert [step["type"] for step in body["steps"]] == [
        "search",
        "screen",
        "extract",
        "synthesize",
        "verify",
        "export",
    ]
    assert body["parameters"]["providers"] == ["openalex", "crossref"]
    assert body["constraints"]["limit_per_provider"] == {"min": 1, "max": 50}
    assert body["coverage"] == {"exhaustive": False}


def test_unknown_template_detail_returns_404(template_api: TemplateAPI) -> None:
    client = template_api.client
    response = client.get("/api/v1/research-engine/blueprints/templates/unknown")
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Blueprint template not found"


def test_disabled_daily_template_is_hidden_and_cannot_be_instantiated(
    template_api: TemplateAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = template_api.client
    project = template_api.project
    created = template_api.created
    monkeypatch.setattr("src.core.config.settings.DAILY_RESEARCH_BRIEF_ENABLED", False)

    listed = client.get("/api/v1/research-engine/blueprints/templates")
    detail = client.get(
        "/api/v1/research-engine/blueprints/templates/daily_research_brief"
    )
    created_response = client.post(
        f"/api/v1/research-engine/blueprints/projects/{project.id}",
        json={
            "name": "Disabled Daily Brief",
            "template_source": "daily_research_brief",
            "steps": [{"type": "search", "name": "Attempted bypass"}],
        },
    )

    assert "daily_research_brief" not in {item["slug"] for item in listed.json()}
    assert detail.status_code == 404
    assert created_response.status_code == 404
    assert created == []

    custom_response = client.post(
        f"/api/v1/research-engine/blueprints/projects/{project.id}",
        json={
            "name": "Custom Search Still Available",
            "steps": [{"type": "search", "name": "Custom search"}],
            "parameters": {"query": "bounded"},
        },
    )
    assert custom_response.status_code == 201, custom_response.text
    assert created[-1].template_source is None


def test_known_template_is_expanded_and_persisted_from_server_owned_steps(
    template_api: TemplateAPI,
) -> None:
    client = template_api.client
    project = template_api.project
    created = template_api.created

    response = client.post(
        f"/api/v1/research-engine/blueprints/projects/{project.id}",
        json={
            "name": "My Daily Brief",
            "template_source": "daily_research_brief",
            "parameters": {
                "providers": ["pubmed"],
                "limit_per_provider": 12,
            },
        },
    )

    assert response.status_code == 201, response.text
    saved = created[-1]
    assert saved.template_source == "daily_research_brief"
    assert [step["type"] for step in saved.steps] == [
        "search",
        "screen",
        "extract",
        "synthesize",
        "verify",
        "export",
    ]
    assert saved.steps[1]["parameters"]["review_gate"] == "screening"
    assert saved.steps[2]["parameters"]["review_gate"] == "extraction"
    assert saved.steps[5]["parameters"]["review_gate"] == "final"
    assert saved.parameters["contract_version"] == 1
    assert saved.parameters["providers"] == ["pubmed"]
    assert saved.parameters["limit_per_provider"] == 12
    assert saved.project_id == template_api.engine_project.id
    assert response.json()["project_id"] == str(project.id)
    assert response.json()["research_engine_project_id"] == str(
        template_api.engine_project.id
    )
    template_api.collection_lookup.assert_awaited_once()


def test_foreign_user_cannot_create_blueprint_for_canonical_project(
    template_api: TemplateAPI,
) -> None:
    template_api.actor["current"] = Mock(
        id=uuid.uuid4(),
        email="other-researcher@example.com",
        is_active=True,
    )

    response = template_api.client.post(
        f"/api/v1/research-engine/blueprints/projects/{template_api.project.id}",
        json={
            "name": "Foreign project blueprint",
            "steps": [{"type": "search", "name": "Unauthorized search"}],
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Project not found"
    assert template_api.created == []
    template_api.collection_lookup.assert_awaited_once()


@pytest.mark.parametrize("providers", [[{}], [[]], [1], [True]])
def test_known_template_rejects_non_string_provider_elements(
    template_api: TemplateAPI, providers: list[Any]
) -> None:
    client = template_api.client
    project = template_api.project
    created = template_api.created

    response = client.post(
        f"/api/v1/research-engine/blueprints/projects/{project.id}",
        json={
            "name": "Invalid Daily Brief",
            "template_source": "daily_research_brief",
            "parameters": {"providers": providers},
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Daily Brief providers are invalid"
    assert created == []


@pytest.mark.parametrize("tampering", ["reorder", "remove_gate", "extra_stage"])
def test_known_template_rejects_client_topology_changes(
    template_api: TemplateAPI, tampering: str
) -> None:
    client = template_api.client
    project = template_api.project
    template = BlueprintLoader(daily_research_brief_enabled=True).load_template(
        "daily_research_brief"
    )
    steps = copy.deepcopy(template["steps"])
    if tampering == "reorder":
        steps[0], steps[1] = steps[1], steps[0]
    elif tampering == "remove_gate":
        steps[1]["parameters"].pop("review_gate")
    else:
        steps.append(copy.deepcopy(steps[0]))

    response = client.post(
        f"/api/v1/research-engine/blueprints/projects/{project.id}",
        json={
            "name": "Tampered Daily Brief",
            "template_source": "daily_research_brief",
            "steps": steps,
            "parameters": template["parameters"],
        },
    )

    assert response.status_code == 422
    assert (
        response.json()["error"]["message"]
        == "Template topology does not match server contract"
    )


def test_custom_topology_cannot_persist_an_untrusted_template_source(
    template_api: TemplateAPI,
) -> None:
    client = template_api.client
    project = template_api.project
    created = template_api.created

    response = client.post(
        f"/api/v1/research-engine/blueprints/projects/{project.id}",
        json={
            "name": "Custom Search",
            "template_source": "client_supplied_template",
            "steps": [{"type": "search", "name": "Custom search"}],
            "parameters": {"query": "bounded"},
        },
    )

    assert response.status_code == 201, response.text
    assert created[-1].template_source is None
    assert response.json()["template_source"] is None


def test_persisted_template_steps_do_not_follow_later_loader_mutation(
    template_api: TemplateAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = template_api.client
    project = template_api.project
    created = template_api.created
    template = BlueprintLoader(daily_research_brief_enabled=True).load_template(
        "daily_research_brief"
    )
    mutable_template = copy.deepcopy(template)
    monkeypatch.setattr(
        "src.api.research_engine.blueprints.BlueprintLoader.load_template",
        lambda _self, _slug: mutable_template,
    )

    response = client.post(
        f"/api/v1/research-engine/blueprints/projects/{project.id}",
        json={
            "name": "Stable Daily Brief",
            "template_source": "daily_research_brief",
            "parameters": {},
        },
    )
    assert response.status_code == 201, response.text
    saved_steps = created[-1].steps

    mutable_template["steps"][0]["name"] = "Changed later"

    assert saved_steps[0]["name"] == "Bounded Provider Search"


# ---------------------------------------------------------------------------
# GOO-335: every Daily Brief route goes through the canonical access funnel.
# ---------------------------------------------------------------------------
#
# Unlike ``template_api`` above, these tests run the real ``project_access``
# and ``workspace_access`` code. ``_AccessSession`` answers each statement the
# funnel issues from one in-memory scope and emulates only the SQL predicates
# (``is_deleted``, archived) that a mock cannot evaluate; every Python-level
# guard runs for real. Export and pending-review VIEW routing is pinned by
# test_research_engine_exports.py / test_research_engine_reviews.py (GOO-404),
# and the PostgreSQL ACL proof lives in
# tests/integration/test_research_run_review_export_access_postgres.py.
#
# Mutation checks (GOO-335), each run with the focused command
#   pytest -q backend/tests/unit/api/test_research_template_security.py -k funnel
# and restored with ``git checkout -- <file>``:
#   M1 project_access.resolve_project ``if workspace_role is None`` removed ->
#      public-workspace foreign-owner cases return 201/200 instead of 404.
#   M2 workspace_access.get_collection ``user_can_access_workspace`` check
#      removed -> private foreign-owner cases still 404 (M1 backstops it), so
#      the private cases alone do not pin get_collection; M1 + M2 together
#      turn them green-to-red.
#   M3 resolve_project lifecycle ``lifecycle[0].is_deleted`` checks removed ->
#      the during-request soft-delete case returns 409 "Project is not
#      writable" (existence leak) instead of 404.
#   M4 resolve_project ``if collection is None`` removed -> soft-deleted
#      project cases raise instead of returning 404.
#   M5 start_run / resume_run / create_blueprint action changed from EDIT to
#      VIEW -> the action-matrix test fails on the recorded action.

from src.models.collection import Collection  # noqa: E402
from src.models.research_blueprint import ResearchBlueprint  # noqa: E402
from src.models.research_project import ResearchProject  # noqa: E402
from src.models.research_project_role import (  # noqa: E402
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.research_run import ResearchRun  # noqa: E402
from src.models.user import User  # noqa: E402
from src.models.workspace import Workspace, WorkspaceRole  # noqa: E402
from src.services.research_engine import project_access  # noqa: E402
from src.services.research_engine.project_access import ResearchAction  # noqa: E402
from src.services.research_engine.scope import (  # noqa: E402
    DAILY_BRIEF_SCOPE_FIELDS,
    resolve_effective_daily_brief_parameters,
)

_RUNS_MODULE = "src.api.research_engine.runs"


class _FunnelWorkspace(SimpleNamespace):
    def is_member(self, user_id: str) -> bool:
        return any(
            not member.is_deleted and str(member.user_id) == str(user_id)
            for member in self.members
        )


@dataclass
class _FunnelScope:
    owner: Any
    stranger: Any
    organization_id: uuid.UUID
    workspace: _FunnelWorkspace
    collection: Any
    engine: Any
    blueprint: Any
    run: Any
    roles: dict[uuid.UUID, list[ResearchProjectRole]]
    stale_collection_read: bool = False
    # Per-user organization overrides; anyone absent shares the workspace org.
    user_orgs: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)


class _Result:
    def __init__(
        self, *, first: Any = None, scalar: Any = None, rows: Any = None
    ) -> None:
        self._first = first
        self._scalar = scalar
        self._rows = rows or []

    def first(self) -> Any:
        return self._first

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _Transaction:
    async def __aenter__(self) -> "_Transaction":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _AccessSession:
    """Answers the canonical access funnel's statements from one scope."""

    def __init__(self, scope: _FunnelScope) -> None:
        self.scope = scope
        self.added: list[Any] = []
        self.commits = 0

    @staticmethod
    def _bound_uuids(statement: Any) -> set[uuid.UUID]:
        return {
            value
            for value in statement.compile().params.values()
            if isinstance(value, uuid.UUID)
        }

    def _live(self) -> bool:
        scope = self.scope
        return not (scope.collection.is_deleted or scope.workspace.is_deleted)

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _Result:
        scope = self.scope
        shape = tuple(
            (description.get("entity"), description.get("name"))
            for description in statement.column_descriptions
        )
        if shape == ((Collection, "Collection"),):
            # workspace_access.get_collection: ``Collection.is_deleted == False``
            # unless the test models a read that raced the soft-delete.
            visible = scope.stale_collection_read or not scope.collection.is_deleted
            return _Result(first=scope.collection if visible else None)
        if shape == ((Workspace, "id"),):
            return _Result()  # Workspace SHARE lock
        if shape == ((Collection, "id"),):
            # lock_active_project: live, non-archived Collection + Workspace.
            writable = (
                self._live()
                and scope.collection.research_status != "archived"
                and not scope.workspace.is_archived
            )
            return _Result(scalar=scope.collection.id if writable else None)
        if shape == ((Collection, "Collection"), (Workspace, "Workspace")):
            return _Result(first=(scope.collection, scope.workspace))
        if shape == ((User, "organization_id"),):
            (user_id,) = self._bound_uuids(statement)
            return _Result(scalar=scope.user_orgs.get(user_id, scope.organization_id))
        if shape == ((ResearchProjectRoleAssignment, "role"),):
            roles = [
                role
                for user_id in self._bound_uuids(statement)
                for role in scope.roles.get(user_id, [])
            ]
            return _Result(rows=roles)
        if shape == ((ResearchProject, "ResearchProject"),):
            return _Result(first=None if scope.engine.is_deleted else scope.engine)
        if shape == ((ResearchBlueprint, "ResearchBlueprint"),):
            return _Result(first=scope.blueprint)
        if shape == ((ResearchRun, "ResearchRun"), (ResearchBlueprint, "project_id")):
            return _Result(first=(scope.run, scope.engine.id))
        raise AssertionError(f"unexpected statement: {statement}")

    async def get(self, model: Any, ident: Any) -> Any:
        assert model is ResearchBlueprint and ident == self.scope.blueprint.id
        return self.scope.blueprint

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def refresh(self, instance: Any) -> None:
        instance.id = getattr(instance, "id", None) or uuid.uuid4()
        instance.created_at = datetime.now(timezone.utc)
        instance.updated_at = datetime.now(timezone.utc)

    def in_transaction(self) -> bool:
        return False

    def begin(self) -> _Transaction:
        return _Transaction()


def _daily_brief_parameters() -> dict[str, Any]:
    template = BlueprintLoader(daily_research_brief_enabled=True).load_template(
        "daily_research_brief"
    )
    return {
        **copy.deepcopy(template["parameters"]),
        "research_question": "Does the controlled treatment reduce score?",
        "inclusion_criteria": ["Reports the measured score"],
        "exclusion_criteria": ["No outcome data"],
        "providers": ["openalex"],
        "limit_per_provider": 2,
        "notes": "",
    }


def _scope_confirmation(parameters: dict[str, Any]) -> dict[str, Any]:
    effective = resolve_effective_daily_brief_parameters(parameters, {})
    return {
        **{field: effective[field] for field in DAILY_BRIEF_SCOPE_FIELDS},
        "confirmed": True,
    }


@dataclass
class FunnelAPI:
    client: TestClient
    scope: _FunnelScope
    session: _AccessSession
    actor: dict[str, Any]
    actions: list[ResearchAction]
    create_run: AsyncMock
    lifecycle: Mock
    load_run: AsyncMock
    export: AsyncMock


@pytest.fixture
def funnel_api(
    test_app: FastAPI,
    test_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FunnelAPI]:
    owner = Mock(id=uuid.uuid4(), email="owner@example.com", is_active=True)
    stranger = Mock(id=uuid.uuid4(), email="stranger@example.com", is_active=True)
    organization_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    workspace = _FunnelWorkspace(
        id=uuid.uuid4(),
        owner_id=owner.id,
        organization_id=organization_id,
        members=[],
        is_public=False,
        is_deleted=False,
        is_archived=False,
    )
    collection = SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        workspace=workspace,
        name="Daily Brief project",
        description=None,
        research_status="active",
        is_deleted=False,
        created_at=now,
        updated_at=now,
    )
    engine = SimpleNamespace(
        id=uuid.uuid4(),
        collection_id=collection.id,
        owner_id=owner.id,
        settings={},
        is_deleted=False,
    )
    parameters = _daily_brief_parameters()
    blueprint = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=engine.id,
        template_source="daily_research_brief",
        parameters=parameters,
        steps=[],
        version=1,
        is_deleted=False,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        blueprint_id=blueprint.id,
        blueprint_version=1,
        status="paused",
        reproducibility_manifest={},
        total_tokens=0,
        created_at=now,
        updated_at=now,
    )
    scope = _FunnelScope(
        owner=owner,
        stranger=stranger,
        organization_id=organization_id,
        workspace=workspace,
        collection=collection,
        engine=engine,
        blueprint=blueprint,
        run=run,
        roles={owner.id: [ResearchProjectRole.REVIEWER]},
    )
    session = _AccessSession(scope)
    actor = {"current": owner}
    test_app.dependency_overrides[get_current_user] = lambda: actor["current"]
    test_app.dependency_overrides[get_db] = lambda: session

    actions: list[ResearchAction] = []
    real_resolve_project = project_access.resolve_project

    async def recording_resolve_project(
        db: Any,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        action: ResearchAction = ResearchAction.VIEW,
        **kwargs: Any,
    ) -> Any:
        actions.append(action)
        return await real_resolve_project(db, project_id, user_id, action, **kwargs)

    # Every require_* helper reaches resolve_project through module globals.
    monkeypatch.setattr(project_access, "resolve_project", recording_resolve_project)
    create_run = AsyncMock(return_value=run)
    monkeypatch.setattr(f"{_RUNS_MODULE}.create_approved_run", create_run)
    monkeypatch.setattr(f"{_RUNS_MODULE}._require_run_conformance", AsyncMock())
    lifecycle = Mock()
    lifecycle.return_value.authorize_resume = AsyncMock()
    monkeypatch.setattr(f"{_RUNS_MODULE}.ResearchRunLifecycleService", lifecycle)
    # The review service's first post-authorization read; nothing is pending.
    load_run = AsyncMock(
        return_value=SimpleNamespace(status="running", reproducibility_manifest={})
    )
    monkeypatch.setattr(
        "src.services.research_engine.review_service.ResearchReviewService._load_run",
        load_run,
    )
    export = AsyncMock(
        return_value=SimpleNamespace(
            content=b"# Daily Brief",
            media_type="text/markdown",
            filename="daily-brief.md",
        )
    )
    exporter = Mock()
    exporter.return_value.export = export
    monkeypatch.setattr(f"{_RUNS_MODULE}.ExportService", exporter)

    @asynccontextmanager
    async def no_lifespan(_app: Any) -> AsyncIterator[None]:
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = no_lifespan
    try:
        with TestClient(test_app, headers=test_auth_headers) as client:
            yield FunnelAPI(
                client=client,
                scope=scope,
                session=session,
                actor=actor,
                actions=actions,
                create_run=create_run,
                lifecycle=lifecycle,
                load_run=load_run,
                export=export,
            )
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


def _call_route(api: FunnelAPI, route: str) -> Any:
    scope = api.scope
    client = api.client
    if route == "create":
        return client.post(
            f"/api/v1/research-engine/blueprints/projects/{scope.collection.id}",
            json={
                "name": "Daily Brief",
                "template_source": "daily_research_brief",
                "parameters": {"providers": ["openalex"], "limit_per_provider": 2},
            },
        )
    if route == "start":
        return client.post(
            f"/api/v1/research-engine/blueprints/{scope.blueprint.id}/runs",
            json={
                "scope_confirmation": _scope_confirmation(scope.blueprint.parameters)
            },
        )
    if route == "resume":
        return client.post(f"/api/v1/research-engine/runs/{scope.run.id}/resume")
    if route == "export":
        return client.get(f"/api/v1/research-engine/runs/{scope.run.id}/export")
    if route == "stream":
        return client.get(f"/api/v1/research-engine/runs/{scope.run.id}/stream")
    if route == "review":
        return client.post(
            f"/api/v1/research-engine/runs/{scope.run.id}/reviews/1",
            json={
                "review_kind": "screening",
                "output_hash": "a" * 64,
                "decision": "approve",
                "decision_payload": {
                    "items": [
                        {
                            "source_id": "source-a",
                            "part_id": "p0001",
                            "decision": "include",
                        }
                    ]
                },
            },
        )
    raise AssertionError(route)


_DAILY_BRIEF_ROUTES = ("create", "start", "resume", "stream", "review", "export")
_ROUTE_ACTIONS = {"review": ResearchAction.REVIEW, "export": ResearchAction.VIEW}


@pytest.mark.parametrize(
    ("route", "status_code", "expected_actions"),
    [
        ("create", 201, [ResearchAction.EDIT]),
        ("start", 201, [ResearchAction.EDIT, ResearchAction.EDIT]),
        ("resume", 200, [ResearchAction.EDIT, ResearchAction.EDIT]),
        ("export", 200, [ResearchAction.VIEW]),
    ],
)
def test_daily_brief_funnel_authorizes_owner_with_route_action(
    funnel_api: FunnelAPI,
    route: str,
    status_code: int,
    expected_actions: list[ResearchAction],
) -> None:
    response = _call_route(funnel_api, route)

    assert response.status_code == status_code, response.text
    assert funnel_api.actions == expected_actions
    if route == "create":
        assert funnel_api.session.added[-1].template_source == "daily_research_brief"
        assert response.json()["project_id"] == str(funnel_api.scope.collection.id)
    if route == "start":
        context = funnel_api.create_run.await_args.args[1]
        assert context.collection is funnel_api.scope.collection
        manifest = funnel_api.create_run.await_args.kwargs["manifest_metadata"]
        assert manifest["scope_confirmation"]["confirmed_by"] == str(
            funnel_api.scope.owner.id
        )
    if route == "resume":
        funnel_api.lifecycle.return_value.authorize_resume.assert_awaited_once()
    if route == "export":
        funnel_api.export.assert_awaited_once()
        assert response.content == b"# Daily Brief"


def test_daily_brief_funnel_authorizes_review_with_review_action(
    funnel_api: FunnelAPI,
) -> None:
    response = _call_route(funnel_api, "review")

    # Access passed; the service then refuses because no gate is pending.
    assert funnel_api.actions == [ResearchAction.REVIEW]
    funnel_api.load_run.assert_awaited_once()
    assert response.status_code == 409, response.text


def test_daily_brief_funnel_review_requires_reviewer_role(
    funnel_api: FunnelAPI,
) -> None:
    funnel_api.scope.roles = {}

    response = _call_route(funnel_api, "review")

    assert response.status_code == 403, response.text
    assert response.json()["error"]["message"] == "reviewer role required"
    assert funnel_api.actions == [ResearchAction.REVIEW]
    funnel_api.load_run.assert_not_awaited()


def _assert_hidden(api: FunnelAPI, response: Any) -> None:
    assert response.status_code == 404, response.text
    assert response.json()["error"]["message"] == "Project not found"
    assert api.session.added == []
    assert api.session.commits == 0
    api.create_run.assert_not_awaited()
    api.lifecycle.return_value.authorize_resume.assert_not_awaited()
    api.load_run.assert_not_awaited()
    api.export.assert_not_awaited()


@pytest.mark.parametrize("is_public", [False, True], ids=["private", "public"])
@pytest.mark.parametrize("route", _DAILY_BRIEF_ROUTES)
def test_daily_brief_funnel_denies_foreign_owner(
    funnel_api: FunnelAPI, route: str, is_public: bool
) -> None:
    # A public workspace lets get_collection return the row; only the
    # membership check in resolve_project keeps artifacts private.
    funnel_api.scope.workspace.is_public = is_public
    funnel_api.actor["current"] = funnel_api.scope.stranger

    response = _call_route(funnel_api, route)

    _assert_hidden(funnel_api, response)
    assert funnel_api.actions == [_ROUTE_ACTIONS.get(route, ResearchAction.EDIT)]


# ``racing`` excludes export: VIEW takes no lifecycle lock, so a read ordered
# before the soft-delete commit may serve that snapshot (linearizable).
@pytest.mark.parametrize(
    ("route", "racing"),
    [
        pytest.param(route, racing, id=f"{route}-{'racing' if racing else 'deleted'}")
        for route in _DAILY_BRIEF_ROUTES
        for racing in (False, True)
        if not (racing and route == "export")
    ],
)
def test_daily_brief_funnel_hides_soft_deleted_project(
    funnel_api: FunnelAPI, route: str, racing: bool
) -> None:
    funnel_api.scope.collection.is_deleted = True
    # ``racing``: the access read saw the row before the soft-delete committed;
    # the locked lifecycle re-read must still answer 404, never 409.
    funnel_api.scope.stale_collection_read = racing

    response = _call_route(funnel_api, route)

    _assert_hidden(funnel_api, response)


def _add_member(api: FunnelAPI, role: WorkspaceRole) -> None:
    api.scope.workspace.members.append(
        SimpleNamespace(user_id=api.scope.stranger.id, role=role, is_deleted=False)
    )
    api.actor["current"] = api.scope.stranger


@pytest.mark.parametrize("route", ("create", "start", "resume"))
def test_daily_brief_funnel_hides_mutations_from_viewer_member(
    funnel_api: FunnelAPI, route: str
) -> None:
    # A VIEWER can read the project but EDIT routes must not reveal it.
    _add_member(funnel_api, WorkspaceRole.VIEWER)

    response = _call_route(funnel_api, route)

    _assert_hidden(funnel_api, response)
    assert funnel_api.actions == [ResearchAction.EDIT]


def test_daily_brief_funnel_lets_viewer_member_export(funnel_api: FunnelAPI) -> None:
    _add_member(funnel_api, WorkspaceRole.VIEWER)

    response = _call_route(funnel_api, "export")

    assert response.status_code == 200, response.text
    assert funnel_api.actions == [ResearchAction.VIEW]


@pytest.mark.parametrize("route", _DAILY_BRIEF_ROUTES)
def test_daily_brief_funnel_hides_project_from_cross_org_member(
    funnel_api: FunnelAPI, route: str
) -> None:
    # Membership alone is not enough: the caller's organization must match.
    _add_member(funnel_api, WorkspaceRole.EDITOR)
    funnel_api.scope.roles = {
        funnel_api.scope.stranger.id: [ResearchProjectRole.REVIEWER]
    }
    funnel_api.scope.user_orgs[funnel_api.scope.stranger.id] = uuid.uuid4()

    response = _call_route(funnel_api, route)

    _assert_hidden(funnel_api, response)
    assert funnel_api.actions == [_ROUTE_ACTIONS.get(route, ResearchAction.EDIT)]

"""API tests for server-owned blueprint template expansion."""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.services.research_engine.blueprints.loader import BlueprintLoader

TemplateAPI = tuple[TestClient, Any, list[Any]]


@pytest.fixture
def daily_brief_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.core.config.settings.DAILY_RESEARCH_BRIEF_ENABLED", True)


@pytest.fixture
def template_api(test_app: FastAPI, daily_brief_enabled: None) -> Iterator[TemplateAPI]:
    user = Mock(
        id=uuid.uuid4(),
        email="researcher@example.com",
        is_active=True,
    )
    project = Mock(
        id=uuid.uuid4(),
        owner_id=user.id,
        is_deleted=False,
    )
    result = Mock()
    scalars = Mock()
    scalars.first.return_value = project
    result.scalars.return_value = scalars

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
    test_app.dependency_overrides[get_current_user] = lambda: user
    test_app.dependency_overrides[get_db] = lambda: db

    @asynccontextmanager
    async def no_lifespan(_app: Any) -> AsyncIterator[None]:
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = no_lifespan
    try:
        with TestClient(test_app) as client:
            yield client, project, created
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


def test_template_detail_route_returns_validated_full_template_before_uuid_route(
    template_api: TemplateAPI,
) -> None:
    client, _project, _created = template_api

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
    client, _project, _created = template_api
    response = client.get("/api/v1/research-engine/blueprints/templates/unknown")
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Blueprint template not found"


def test_disabled_daily_template_is_hidden_and_cannot_be_instantiated(
    template_api: TemplateAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, project, created = template_api
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
    client, project, created = template_api

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


@pytest.mark.parametrize("providers", [[{}], [[]], [1], [True]])
def test_known_template_rejects_non_string_provider_elements(
    template_api: TemplateAPI, providers: list[Any]
) -> None:
    client, project, created = template_api

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
    client, project, _created = template_api
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
    client, project, created = template_api

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
    client, project, created = template_api
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

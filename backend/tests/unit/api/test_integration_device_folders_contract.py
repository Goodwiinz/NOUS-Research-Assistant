"""Contract pins for the device folder routes.

`GET`/`POST /integrations/devices/{device_id}/workspaces` predate account
connections and are read by the bridge and the chat composer through the
generated types. Their `project_id` must stay a required string: a nullable
one is an ERR-level break for `oasdiff` (`response-property-became-optional`,
`response-property-list-of-types-widened`). Account folders, which belong to
no project, have their own routes and models instead.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.unit

WORKSPACES = "/integrations/devices/{device_id}/workspaces"
ACCOUNT_WORKSPACES = "/integrations/devices/{device_id}/account-workspaces"
REQUIRED_UUID = {"type": "string", "format": "uuid", "title": "Project Id"}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    from src.api.integrations import router

    app = FastAPI()
    app.include_router(router)
    return dict(app.openapi())


def _schema(spec: dict[str, Any], name: str) -> dict[str, Any]:
    return dict(spec["components"]["schemas"][name])


def _response(spec: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    operation = spec["paths"][path][method]
    return dict(operation["responses"]["200"]["content"]["application/json"]["schema"])


def _request(spec: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    operation = spec["paths"][path][method]
    return dict(operation["requestBody"]["content"]["application/json"]["schema"])


@pytest.mark.parametrize("name", ["WorkspaceBindingDTO", "WorkspaceBindingCreate"])
def test_project_folder_models_keep_a_required_string_project_id(
    spec: dict[str, Any], name: str
) -> None:
    schema = _schema(spec, name)
    assert schema["properties"]["project_id"] == REQUIRED_UUID
    assert "project_id" in schema["required"]


def test_the_workspaces_routes_keep_their_project_folder_models(
    spec: dict[str, Any],
) -> None:
    assert _response(spec, WORKSPACES, "get") == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/WorkspaceBindingDTO"},
        "title": "Response Get Workspace Bindings Integrations Devices  Device Id  Workspaces Get",
    }
    assert _response(spec, WORKSPACES, "post") == {
        "$ref": "#/components/schemas/WorkspaceBindingDTO"
    }
    assert _request(spec, WORKSPACES, "post") == {
        "$ref": "#/components/schemas/WorkspaceBindingCreate"
    }


def test_account_folders_have_their_own_routes_without_a_project_id(
    spec: dict[str, Any],
) -> None:
    assert _response(spec, ACCOUNT_WORKSPACES, "get")["items"] == {
        "$ref": "#/components/schemas/AccountWorkspaceBindingDTO"
    }
    assert _response(spec, ACCOUNT_WORKSPACES, "post") == {
        "$ref": "#/components/schemas/AccountWorkspaceBindingDTO"
    }
    assert _request(spec, ACCOUNT_WORKSPACES, "post") == {
        "$ref": "#/components/schemas/AccountWorkspaceBindingCreate"
    }
    for name in ("AccountWorkspaceBindingDTO", "AccountWorkspaceBindingCreate"):
        schema = _schema(spec, name)
        assert set(schema["properties"]) == {"label", "workspace_id"}
        assert set(schema["required"]) == {"label", "workspace_id"}

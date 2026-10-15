"""Account connections: schemas, grants, approvals, devices and folders.

The database tests run on SQLite always and on PostgreSQL when
ORCHESTRATION_TEST_DATABASE_URL is set (docs/testing/harness-live-proof.md Gate
0). Without the URL the PostgreSQL half is skipped, which is NOT RUN, never PASS.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.schemas.integration_connections import ConnectionConsent
from src.schemas.integration_context import GrantRequestCreate, IntegrationContext
from src.schemas.tool_actions import ActionActor

pytestmark = pytest.mark.unit
SCHEMA_IDS = {
    name: uuid4() for name in ("user", "org", "project", "workspace", "device")
}


def _ctx(**values: object) -> IntegrationContext:
    return IntegrationContext(
        user_id=SCHEMA_IDS["user"],
        organization_id=SCHEMA_IDS["org"],
        grant_id=uuid4(),
        **values,
    )


def test_context_kind_is_derived_for_project_and_workspace_never_for_account() -> None:
    assert _ctx(project_id=SCHEMA_IDS["project"]).binding_kind == "project"
    assert _ctx(workspace_id=SCHEMA_IDS["workspace"]).binding_kind == "workspace"
    assert _ctx(binding_kind="account").binding_kind == "account"
    # Both NULL means "bound to nothing" unless the kind says account.
    with pytest.raises(
        ValidationError, match="exactly one of project_id or workspace_id"
    ):
        _ctx()
    with pytest.raises(ValidationError):
        _ctx(project_id=SCHEMA_IDS["project"], workspace_id=SCHEMA_IDS["workspace"])
    for wrong in (
        {"binding_kind": "account", "project_id": SCHEMA_IDS["project"]},
        {"binding_kind": "account", "workspace_id": SCHEMA_IDS["workspace"]},
        {"binding_kind": "project", "workspace_id": SCHEMA_IDS["workspace"]},
        {"binding_kind": "workspace", "project_id": SCHEMA_IDS["project"]},
    ):
        with pytest.raises(ValidationError, match="binding does not match"):
            _ctx(**wrong)


def test_grant_request_names_exactly_one_binding_and_no_chat_for_an_account() -> None:
    device, scopes = SCHEMA_IDS["device"], {"tools:read"}
    account = GrantRequestCreate(account=True, device_id=device, scopes=scopes)
    assert account.binding_kind == "account"
    assert (
        GrantRequestCreate(
            project_id=SCHEMA_IDS["project"], device_id=device, scopes=scopes
        ).binding_kind
        == "project"
    )
    assert (
        GrantRequestCreate(
            workspace_id=SCHEMA_IDS["workspace"], device_id=device, scopes=scopes
        ).binding_kind
        == "workspace"
    )
    with pytest.raises(
        ValidationError, match="exactly one of project_id or workspace_id"
    ):
        GrantRequestCreate(device_id=device, scopes=scopes)
    for extra in (
        {"project_id": SCHEMA_IDS["project"]},
        {"workspace_id": SCHEMA_IDS["workspace"]},
    ):
        with pytest.raises(ValidationError, match="account excludes"):
            GrantRequestCreate(account=True, device_id=device, scopes=scopes, **extra)
    with pytest.raises(ValidationError, match="cannot be bound to a chat"):
        GrantRequestCreate(
            account=True, device_id=device, scopes=scopes, thread_id=uuid4()
        )


def test_a_connection_consent_may_be_the_whole_account() -> None:
    consent = ConnectionConsent(
        request_id=uuid4(),
        kind="account",
        project_id=None,
        project_label=None,
        workspace_id=None,
        workspace_label=None,
        thread_id=None,
        thread_label=None,
        scopes=["harness:execute", "tools:read"],
        status="consumed",
    )
    assert consent.kind == "account"


def test_action_actor_may_name_its_kind() -> None:
    actor = ActionActor(
        user_id=SCHEMA_IDS["user"],
        organization_id=SCHEMA_IDS["org"],
        project_id=None,
        binding_kind="account",
    )
    assert actor.binding_kind == "account"
    legacy = ActionActor(
        user_id=SCHEMA_IDS["user"],
        organization_id=SCHEMA_IDS["org"],
        project_id=uuid4(),
    )
    assert legacy.binding_kind is None  # derived where it is used (tool_actions)

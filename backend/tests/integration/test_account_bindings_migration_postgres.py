"""ac01_account_bindings on real PostgreSQL: backfill, CHECKs and downgrade.

Run with ORCHESTRATION_TEST_DATABASE_URL set (docs/testing/harness-live-proof.md
Gate 0). Without it every test skips, which is NOT RUN, never PASS.

The schema is built from the models (post-ac01), then taken to the pre-ac01
shape by running this revision's downgrade on empty tables. Pre-ac01 rows are
inserted with raw SQL because the models already name binding_kind.
"""

from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, create_engine, insert, text
from sqlalchemy.exc import IntegrityError

from src.models.base import Base
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.collection import Collection
from src.models.harness_session import HarnessSession
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.tool_action import IntegrationToolAction
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]
VERSIONS = Path(__file__).parents[2] / "alembic" / "versions"
USER, ORG, WORKSPACE, PROJECT, DEVICE = (uuid4() for _ in range(5))
SOON = datetime.now(timezone.utc) + timedelta(hours=1)
_MODELS: list[Any] = [
    Organization,
    User,
    Workspace,
    WorkspaceMember,
    Collection,
    BridgeDevice,
    WorkspaceBinding,
    IntegrationGrantRequest,
    IntegrationGrant,
    IntegrationToolAction,
    HarnessSession,
]


def _revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ac01_account_bindings", VERSIONS / "ac01_account_bindings.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(connection: Connection, direction: str) -> None:
    module = _revision()
    setattr(module, "op", Operations(MigrationContext.configure(connection)))
    getattr(module, direction)()


def _raw(connection: Connection, table: str, **values: Any) -> UUID:
    """Insert one row the way a pre-ac01 writer would (no binding_kind)."""
    row_id: UUID = values.pop("id", uuid4())
    now = datetime.now(timezone.utc)
    columns = {
        "id": row_id,
        "created_at": now,
        "updated_at": now,
        "is_deleted": False,
        **values,
    }
    for key, value in columns.items():
        if isinstance(value, (list, dict)):
            columns[key] = json.dumps(value)
        elif isinstance(value, UUID):
            columns[key] = str(value)
    names = ", ".join(columns)
    params = ", ".join(f":{name}" for name in columns)
    connection.execute(
        text(f"INSERT INTO {table} ({names}) VALUES ({params})"), columns
    )
    return row_id


@pytest.fixture
def pre_ac01() -> Iterator[Connection]:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured (NOT RUN)")
    schema = "ac01_" + uuid4().hex
    admin = create_engine(dsn)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(dsn, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE threads (id uuid PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE agent_runs (job_id varchar(36) PRIMARY KEY)"
            )
            Base.metadata.create_all(
                connection, tables=[model.__table__ for model in _MODELS]
            )
            _run(connection, "downgrade")  # empty tables: to the pre-ac01 shape
            connection.execute(
                insert(Organization).values(id=ORG, name="o", storage_limit_bytes=1)
            )
            connection.execute(
                text(
                    "INSERT INTO users (id, organization_id, email, password_hash, "
                    "first_name, last_name, role, is_active, login_count, "
                    "created_at, updated_at, is_deleted) VALUES (:id, :org, "
                    "'m@example.test', 'unused', 'T', 'U', 'USER', true, 0, now(), "
                    "now(), false)"
                ),
                {"id": str(USER), "org": str(ORG)},
            )
            connection.execute(
                insert(Workspace).values(
                    id=WORKSPACE, name="w", owner_id=USER, organization_id=ORG
                )
            )
            connection.execute(
                insert(Collection).values(id=PROJECT, name="p", workspace_id=WORKSPACE)
            )
            connection.execute(
                insert(BridgeDevice).values(
                    id=DEVICE, user_id=USER, organization_id=ORG, label="laptop"
                )
            )
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def _request(connection: Connection, **binding: Any) -> UUID:
    return _raw(
        connection,
        "integration_grant_requests",
        user_id=USER,
        organization_id=ORG,
        device_id=DEVICE,
        scopes=["tools:read"],
        status="consumed",
        expires_at=SOON,
        **binding,
    )


def _grant(connection: Connection, request_id: UUID, **binding: Any) -> UUID:
    return _raw(
        connection,
        "integration_grants",
        user_id=USER,
        organization_id=ORG,
        device_id=DEVICE,
        request_id=request_id,
        scopes=["tools:read"],
        token_hash=uuid4().hex * 2,
        expires_at=SOON,
        consented_at=SOON,
        **binding,
    )


def _action(connection: Connection, **binding: Any) -> UUID:
    return _raw(
        connection,
        "integration_tool_actions",
        organization_id=ORG,
        user_id=USER,
        invocation_id=uuid4(),
        tool_name="rename_folder",
        arguments={},
        argument_hash="z" * 64,
        state="awaiting_approval",
        **binding,
    )


def _kinds(connection: Connection, table: str) -> dict[UUID, str]:
    rows = connection.execute(text(f"SELECT id, binding_kind FROM {table}")).all()
    return {UUID(str(row_id)): kind for row_id, kind in rows}


def _constraints(connection: Connection, table: str) -> set[str]:
    rows = connection.execute(
        text(
            "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE t.relname = :table AND n.nspname = current_schema() "
            "AND c.contype = 'c'"
        ),
        {"table": table},
    ).all()
    return {name for (name,) in rows}


def _refused(connection: Connection, table: str, **values: Any) -> None:
    with pytest.raises(IntegrityError, match="binding_kind"):
        with connection.begin_nested():
            _raw(connection, table, **values)


def test_upgrade_backfills_kinds_and_enforces_one_check_per_kind(
    pre_ac01: Connection,
) -> None:
    connection = pre_ac01
    with connection.begin():
        project_request = _request(connection, project_id=PROJECT)
        workspace_request = _request(connection, workspace_id=WORKSPACE)
        project_grant = _grant(connection, project_request, project_id=PROJECT)
        workspace_grant = _grant(connection, workspace_request, workspace_id=WORKSPACE)
        project_action = _action(connection, project_id=PROJECT)
        workspace_action = _action(
            connection, project_id=PROJECT, workspace_id=WORKSPACE
        )
        _raw(
            connection,
            "bridge_workspace_bindings",
            device_id=DEVICE,
            workspace_id=uuid4(),
            project_id=PROJECT,
            label="repo",
        )
        _run(connection, "upgrade")
        assert _kinds(connection, "integration_grant_requests") == {
            project_request: "project",
            workspace_request: "workspace",
        }
        assert _kinds(connection, "integration_grants") == {
            project_grant: "project",
            workspace_grant: "workspace",
        }
        assert _kinds(connection, "integration_tool_actions") == {
            project_action: "project",
            workspace_action: "workspace",
        }
        for table in ("integration_grant_requests", "integration_grants"):
            assert _constraints(connection, table) >= {f"ck_{table}_binding_kind"}
            assert f"ck_{table}_one_binding" not in _constraints(connection, table)
        # Account rows are accepted, and only in their own shape.
        account = _request(connection, binding_kind="account")
        _grant(connection, account, binding_kind="account")
        _grant(connection, account, binding_kind="project", project_id=PROJECT)
        _action(connection, binding_kind="account", project_id=PROJECT)
        _action(connection, binding_kind="account", target_workspace_id=WORKSPACE)
        _raw(
            connection,
            "bridge_workspace_bindings",
            device_id=DEVICE,
            workspace_id=uuid4(),
            project_id=None,
            label="account folder",
        )
        _refused(
            connection,
            "integration_grant_requests",
            user_id=USER,
            organization_id=ORG,
            device_id=DEVICE,
            scopes=["tools:read"],
            status="pending",
            expires_at=SOON,
            binding_kind="account",
            project_id=PROJECT,
        )
        _refused(
            connection,
            "integration_grant_requests",
            user_id=USER,
            organization_id=ORG,
            device_id=DEVICE,
            scopes=["tools:read"],
            status="pending",
            expires_at=SOON,
            binding_kind="project",
        )
        bad_actions: tuple[dict[str, Any], ...] = (
            {"binding_kind": "account"},
            {
                "binding_kind": "account",
                "project_id": PROJECT,
                "target_workspace_id": WORKSPACE,
            },
            {
                "binding_kind": "project",
                "project_id": PROJECT,
                "target_workspace_id": WORKSPACE,
            },
        )
        for binding in bad_actions:
            _refused(
                connection,
                "integration_tool_actions",
                organization_id=ORG,
                user_id=USER,
                invocation_id=uuid4(),
                tool_name="rename_folder",
                arguments="{}",
                argument_hash="z" * 64,
                state="awaiting_approval",
                **binding,
            )


def test_downgrade_removes_account_rows_restores_the_old_rules_and_round_trips(
    pre_ac01: Connection,
) -> None:
    connection = pre_ac01
    with connection.begin():
        _run(connection, "upgrade")
        project_request = _request(
            connection, project_id=PROJECT, binding_kind="project"
        )
        project_grant = _grant(
            connection, project_request, project_id=PROJECT, binding_kind="project"
        )
        project_action = _action(connection, project_id=PROJECT, binding_kind="project")
        workspace_request = _request(
            connection, workspace_id=WORKSPACE, binding_kind="workspace"
        )
        workspace_grant = _grant(
            connection,
            workspace_request,
            workspace_id=WORKSPACE,
            binding_kind="workspace",
        )
        workspace_action = _action(
            connection, workspace_id=WORKSPACE, binding_kind="workspace"
        )
        account = _request(connection, binding_kind="account")
        _grant(connection, account, binding_kind="account")
        _grant(connection, account, binding_kind="project", project_id=PROJECT)
        _action(connection, binding_kind="account", project_id=PROJECT)
        _raw(
            connection,
            "bridge_workspace_bindings",
            device_id=DEVICE,
            workspace_id=uuid4(),
            project_id=None,
            label="account folder",
        )
        _run(connection, "downgrade")
        ids = lambda table: {  # noqa: E731
            UUID(str(row_id))
            for (row_id,) in connection.execute(text(f"SELECT id FROM {table}")).all()
        }
        assert ids("integration_grant_requests") == {
            project_request,
            workspace_request,
        }
        assert ids("integration_grants") == {project_grant, workspace_grant}
        assert ids("integration_tool_actions") == {project_action, workspace_action}
        assert (
            connection.execute(
                text("SELECT count(*) FROM bridge_workspace_bindings")
            ).scalar_one()
            == 0
        )
        nullable = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns WHERE "
                "table_schema = current_schema() AND "
                "table_name = 'bridge_workspace_bindings' AND column_name = 'project_id'"
            )
        ).scalar_one()
        assert nullable == "NO"
        assert "ck_integration_tool_actions_some_binding" in _constraints(
            connection, "integration_tool_actions"
        )
        for table in ("integration_grant_requests", "integration_grants"):
            assert f"ck_{table}_one_binding" in _constraints(connection, table)
        _run(connection, "upgrade")  # the round trip applies again
        assert _kinds(connection, "integration_grants") == {
            project_grant: "project",
            workspace_grant: "workspace",
        }


@pytest.mark.parametrize(
    "case",
    ["account-folder-with-a-project-grant", "project-folder-with-an-account-grant"],
)
def test_downgrade_refuses_while_a_harness_session_cites_account_state(
    pre_ac01: Connection, case: str
) -> None:
    """Each guard clause is proved alone: the session cites exactly one thing
    the downgrade would delete (an account folder, or a project grant minted
    under an account request), never both."""
    connection = pre_ac01
    with connection.begin():
        _run(connection, "upgrade")
    folder = uuid4()
    with connection.begin():
        if case == "account-folder-with-a-project-grant":
            request = _request(connection, project_id=PROJECT, binding_kind="project")
            grant = _grant(
                connection, request, project_id=PROJECT, binding_kind="project"
            )
            folder_project: UUID | None = None
        else:
            request = _request(connection, binding_kind="account")
            grant = _grant(
                connection, request, project_id=PROJECT, binding_kind="project"
            )
            folder_project = PROJECT
        _raw(
            connection,
            "bridge_workspace_bindings",
            device_id=DEVICE,
            workspace_id=folder,
            project_id=folder_project,
            label="folder",
        )
        run_id = str(uuid4())
        connection.execute(
            text("INSERT INTO agent_runs (job_id) VALUES (:job)"), {"job": run_id}
        )
        connection.execute(
            text(
                "INSERT INTO harness_sessions (id, run_id, grant_id, device_id, "
                "workspace_id) VALUES (:id, :run, :grant, :device, :folder)"
            ),
            {
                "id": str(uuid4()),
                "run": run_id,
                "grant": str(grant),
                "device": str(DEVICE),
                "folder": str(folder),
            },
        )
    with pytest.raises(RuntimeError, match="harness session"):
        with connection.begin():
            _run(connection, "downgrade")
    # Transactional DDL: nothing changed.
    assert "ck_integration_grants_binding_kind" in _constraints(
        connection, "integration_grants"
    )

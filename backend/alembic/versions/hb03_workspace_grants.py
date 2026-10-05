"""Workspace-scoped integration grants (Plan 07, slice 1).

A grant and its consent request bind either one Collection (``project_id``) or
every live Collection of one workspace (``workspace_id``); a CHECK constraint
keeps exactly one of the two set. On an action ``project_id`` is the target
Collection and ``workspace_id`` the binding of the authorising grant, so a
workspace-grant action aimed at a Collection sets both: its CHECK only requires
at least one.

Revision ID: hb03_workspace_grants
Revises: hb05_grant_request_thread
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "hb03_workspace_grants"
down_revision = "hb05_grant_request_thread"
branch_labels = None
depends_on = None

# Upgrade order; downgrade reverses it. A grant cites its consent request
# through request_id, so requests are parents.
TABLES = (
    "integration_grant_requests",
    "integration_grants",
    "integration_tool_actions",
)
ACTIONS = "integration_tool_actions"


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column(
                "workspace_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("workspaces.id"),
                nullable=True,
            ),
        )
        op.alter_column(table, "project_id", nullable=True)
        if table == ACTIONS:
            op.create_check_constraint(
                f"ck_{table}_some_binding",
                table,
                "project_id IS NOT NULL OR workspace_id IS NOT NULL",
            )
        else:
            op.create_check_constraint(
                f"ck_{table}_one_binding",
                table,
                "(project_id IS NULL) <> (workspace_id IS NULL)",
            )
        op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])


def downgrade() -> None:
    # A row without a project cannot satisfy NOT NULL, so workspace-bound
    # requests and grants and workspace-level actions are deleted. An action a
    # workspace grant aimed at one Collection keeps its project_id and survives.
    # Children go before the parents they cite (grants hold request_id).
    for table in reversed(TABLES):
        op.drop_index(f"ix_{table}_workspace_id", table_name=table)
        name = "some_binding" if table == ACTIONS else "one_binding"
        op.drop_constraint(f"ck_{table}_{name}", table, type_="check")
        op.execute(f"DELETE FROM {table} WHERE project_id IS NULL")
        op.alter_column(table, "project_id", nullable=False)
        op.drop_column(table, "workspace_id")

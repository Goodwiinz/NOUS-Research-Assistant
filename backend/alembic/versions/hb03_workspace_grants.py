"""Workspace-scoped integration grants (Plan 07, slice 1).

A grant, its consent request and the actions it authorises bind either one
Collection (``project_id``) or every live Collection of one workspace
(``workspace_id``); a CHECK constraint keeps exactly one of the two set.

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

# Parents first: a grant cites its consent request through request_id.
TABLES = (
    "integration_grant_requests",
    "integration_grants",
    "integration_tool_actions",
)


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
        op.create_check_constraint(
            f"ck_{table}_one_binding",
            table,
            "(project_id IS NULL) <> (workspace_id IS NULL)",
        )
        op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])


def downgrade() -> None:
    # A workspace-bound row has no project to satisfy NOT NULL, so it cannot
    # survive the downgrade. Children go before the parents they cite.
    for table in reversed(TABLES):
        op.drop_index(f"ix_{table}_workspace_id", table_name=table)
        op.drop_constraint(f"ck_{table}_one_binding", table, type_="check")
        op.execute(f"DELETE FROM {table} WHERE project_id IS NULL")
        op.alter_column(table, "project_id", nullable=False)
        op.drop_column(table, "workspace_id")

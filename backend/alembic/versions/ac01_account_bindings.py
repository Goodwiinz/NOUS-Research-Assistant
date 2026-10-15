"""Account-wide harness connections: a third binding kind.

A consent request and a grant now carry ``binding_kind``. ``project`` binds
one Collection and ``workspace`` binds one workspace, as before. ``account``
binds neither: it covers every live, unarchived Collection of the user's
organization workspaces that they own or belong to, evaluated per statement
(``services/integrations/context.account_scope_condition``). One CHECK per
kind replaces the old XOR, so "both NULL" is accepted only for an
``account`` row, never by accident.

An action row records its authorising grant's kind. ``target_workspace_id``
holds the workspace ``create_folder`` creates a folder in under an account
grant, because ``workspace_id`` on that table means "the grant's workspace".
A NULL ``bridge_workspace_bindings.project_id`` is an account folder.

Downgrade deletes account approvals, account grants and the project grants
minted under an account approval, account actions and account folders. It
refuses while a harness session cites any of them: those runs belong to their
users and are not deleted by a migration.

After a downgrade, project-kind actions created under a project grant that was
minted from an account consent keep ``consent_id`` and ``grant_id`` values that
point at deleted rows (neither column is a foreign key). Old code reads the
missing consent as a refusal.

Revision ID: ac01_account_bindings
Revises: aw03_artifact_completion
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ac01_account_bindings"
down_revision = "aw03_artifact_completion"
branch_labels = None
depends_on = None

REQUESTS = "integration_grant_requests"
GRANTS = "integration_grants"
ACTIONS = "integration_tool_actions"
BINDINGS = "bridge_workspace_bindings"
TARGET_INDEX = "ix_integration_tool_actions_target_workspace_id"

KIND_CHECK = (
    "(binding_kind = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL)"
    " OR (binding_kind = 'workspace' AND project_id IS NULL AND workspace_id IS NOT NULL)"
    " OR (binding_kind = 'account' AND project_id IS NULL AND workspace_id IS NULL)"
)
ACTION_KIND_CHECK = (
    "(binding_kind = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL"
    " AND target_workspace_id IS NULL)"
    " OR (binding_kind = 'workspace' AND workspace_id IS NOT NULL"
    " AND target_workspace_id IS NULL)"
    " OR (binding_kind = 'account' AND workspace_id IS NULL"
    " AND (project_id IS NULL) <> (target_workspace_id IS NULL))"
)
ONE_BINDING = "(project_id IS NULL) <> (workspace_id IS NULL)"
SOME_BINDING = "project_id IS NOT NULL OR workspace_id IS NOT NULL"
DOOMED_GRANTS = (
    f"binding_kind = 'account' OR request_id IN "
    f"(SELECT id FROM {REQUESTS} WHERE binding_kind = 'account')"
)
ACCOUNT_RUNS = (
    f"SELECT 1 FROM harness_sessions WHERE grant_id IN "
    f"(SELECT id FROM {GRANTS} WHERE {DOOMED_GRANTS}) "
    f"OR (device_id, workspace_id) IN "
    f"(SELECT device_id, workspace_id FROM {BINDINGS} WHERE project_id IS NULL) "
    f"LIMIT 1"
)


def upgrade() -> None:
    for table in (REQUESTS, GRANTS, ACTIONS):
        # The default exists only to backfill and is dropped below. The two
        # request/grant tables derive their kind in the model (project or
        # workspace); integration_tool_actions has no model default, so every
        # writer passes binding_kind. An account writer passes 'account'.
        op.add_column(
            table,
            sa.Column(
                "binding_kind",
                sa.String(16),
                nullable=False,
                server_default="project",
            ),
        )
        op.execute(
            f"UPDATE {table} SET binding_kind = 'workspace' "
            f"WHERE workspace_id IS NOT NULL"
        )
        op.alter_column(table, "binding_kind", server_default=None)
    op.add_column(
        ACTIONS,
        sa.Column(
            "target_workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=True,
        ),
    )
    op.create_index(TARGET_INDEX, ACTIONS, ["target_workspace_id"])
    for table in (REQUESTS, GRANTS):
        op.drop_constraint(f"ck_{table}_one_binding", table, type_="check")
        op.create_check_constraint(f"ck_{table}_binding_kind", table, KIND_CHECK)
    op.drop_constraint(f"ck_{ACTIONS}_some_binding", ACTIONS, type_="check")
    op.create_check_constraint(f"ck_{ACTIONS}_binding_kind", ACTIONS, ACTION_KIND_CHECK)
    op.alter_column(BINDINGS, "project_id", nullable=True)


def downgrade() -> None:
    # op.get_context().as_sql, not alembic.context.is_offline_mode(): the
    # PostgreSQL test drives this module through a bare Operations object.
    if not op.get_context().as_sql:
        if op.get_bind().execute(sa.text(ACCOUNT_RUNS)).first() is not None:
            raise RuntimeError(
                "ac01_account_bindings: a harness session cites an account "
                "grant or an account folder. Remove those Local Codex runs by "
                "hand before downgrading; this migration does not delete them."
            )
    # Children before the parents they cite: folders, actions, grants (which
    # hold request_id), then the requests themselves.
    op.execute(f"DELETE FROM {BINDINGS} WHERE project_id IS NULL")
    op.alter_column(BINDINGS, "project_id", nullable=False)
    op.drop_constraint(f"ck_{ACTIONS}_binding_kind", ACTIONS, type_="check")
    op.execute(f"DELETE FROM {ACTIONS} WHERE binding_kind = 'account'")
    op.drop_index(TARGET_INDEX, table_name=ACTIONS)
    op.drop_column(ACTIONS, "target_workspace_id")
    op.drop_column(ACTIONS, "binding_kind")
    op.create_check_constraint(f"ck_{ACTIONS}_some_binding", ACTIONS, SOME_BINDING)
    op.drop_constraint(f"ck_{GRANTS}_binding_kind", GRANTS, type_="check")
    op.execute(f"DELETE FROM {GRANTS} WHERE {DOOMED_GRANTS}")
    op.drop_column(GRANTS, "binding_kind")
    op.create_check_constraint(f"ck_{GRANTS}_one_binding", GRANTS, ONE_BINDING)
    op.drop_constraint(f"ck_{REQUESTS}_binding_kind", REQUESTS, type_="check")
    op.execute(f"DELETE FROM {REQUESTS} WHERE binding_kind = 'account'")
    op.drop_column(REQUESTS, "binding_kind")
    op.create_check_constraint(f"ck_{REQUESTS}_one_binding", REQUESTS, ONE_BINDING)

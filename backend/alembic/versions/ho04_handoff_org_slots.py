"""Scope handoff version and retry-id slots by organization (GOO-423).

Revision ID: ho04_handoff_org_slots
Revises: jd01_document_embeddings

The service already reads and resolves conflicts within an organization.
Replace both unique keys to match that namespace without changing any rows.
Downgrade refuses cross-organization collisions before touching either key;
it never deletes or rewrites a tenant's history to restore the old namespace.
"""

from alembic import op  # type: ignore[attr-defined]

revision = "ho04_handoff_org_slots"
down_revision = "jd01_document_embeddings"
branch_labels = None
depends_on = None

_TABLE = "integration_handoffs"
_KEYS = (
    (
        "version",
        "uq_integration_handoffs_thread_project_version",
        "uq_integration_handoffs_org_thread_project_version",
    ),
    (
        "handoff_id",
        "uq_integration_handoffs_thread_project_handoff",
        "uq_integration_handoffs_org_thread_project_handoff",
    ),
)


def upgrade() -> None:
    for column, old_name, new_name in _KEYS:
        op.drop_constraint(old_name, _TABLE, type_="unique")
        op.create_unique_constraint(
            new_name, _TABLE, ["organization_id", "thread_id", "project_id", column]
        )


def downgrade() -> None:
    # PostgreSQL DO also renders offline; do not execute a Python-side query
    # against Alembic's mock connection during --sql generation.
    op.execute("""
        DO $handoff_slots$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM integration_handoffs
                GROUP BY thread_id, project_id, version HAVING count(*) > 1
            ) OR EXISTS (
                SELECT 1 FROM integration_handoffs
                GROUP BY thread_id, project_id, handoff_id HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Cannot restore organization-agnostic handoff keys while organization chains share slots'
                    USING ERRCODE = '23505';
            END IF;
        END;
        $handoff_slots$;
        """)
    for column, old_name, new_name in _KEYS:
        op.drop_constraint(new_name, _TABLE, type_="unique")
        op.create_unique_constraint(
            old_name, _TABLE, ["thread_id", "project_id", column]
        )

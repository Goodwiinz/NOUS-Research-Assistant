"""Give frozen created_at/assigned_at columns a real DEFAULT now().

Revision ID: cd01_created_at_default_now
Revises: pb01_pdf_message_branches

These columns were declared ``server_default="now()"`` (a Python str). SQLAlchemy
renders a str as a quoted literal, ``DEFAULT 'now()'``, and PostgreSQL folds that
into a constant timestamp when the table is created (r6h3_model_baseline builds
these tables from the models). Every row that relied on the database default
therefore got the table's creation time instead of its insert time -- including
encryption_audit_logs. The models now declare ``func.now()``; this sets the
matching ``DEFAULT now()`` on databases built before that.

``ALTER COLUMN ... SET DEFAULT`` only changes the catalog (no table rewrite, no
existing row touched), so the brief ACCESS EXCLUSIVE lock is safe online. Rows
already written keep their frozen value: the real insert times were never
recorded and cannot be recovered.

Some of these tables exist only where r6h3's ``create_all`` created them, so each
column is altered only if present.

Downgrade is intentionally a no-op: restoring ``DEFAULT 'now()'`` would only
re-freeze the default at a new constant.
"""

from __future__ import annotations

import alembic.op as op
import sqlalchemy as sa

revision = "cd01_created_at_default_now"
down_revision = "pb01_pdf_message_branches"
branch_labels = None
depends_on = None

FROZEN_DEFAULT_COLUMNS = (
    ("encrypted_user_profiles", "created_at"),
    ("encrypted_organization_profiles", "created_at"),
    ("encryption_audit_logs", "created_at"),
    ("entity_relationships", "created_at"),
    ("entity_categories", "assigned_at"),
    ("entity_synonyms", "created_at"),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table, column in FROZEN_DEFAULT_COLUMNS:
        if not inspector.has_table(table):
            continue
        if column not in {c["name"] for c in inspector.get_columns(table)}:
            continue
        op.alter_column(table, column, server_default=sa.text("now()"))


def downgrade() -> None:
    # See module docstring: putting back DEFAULT 'now()' would freeze it again.
    pass

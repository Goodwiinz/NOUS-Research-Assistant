"""Widen users.first_name / users.last_name to TEXT for encrypted values

Revision ID: b7c4e1d9a2f6
Revises: merge_daily_harness_20260928
Create Date: 2026-09-29 00:00:00.000000

PR #175 (bded25e35) switched ``User.first_name`` / ``User.last_name`` to
``encrypted_string(...)``, whose column impl is ``TEXT``, but shipped no
migration. The live columns stayed ``VARCHAR(100)``. The stored ciphertext is a
JSON envelope (``{"encrypted_data": ..., "nonce": ..., "version": ...}``) of
several hundred characters, so every ``users`` INSERT fails with
``value too long for type character varying(100)`` once
``ENCRYPTION_MASTER_KEY`` is set, which breaks JIT provisioning of new users.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b7c4e1d9a2f6"
down_revision = "merge_daily_harness_20260928"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in ("first_name", "last_name"):
        op.alter_column(
            "users",
            column,
            type_=sa.Text(),
            existing_type=sa.String(length=100),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Encrypted values exceed VARCHAR(100); narrowing would fail or truncate
    # ciphertext and make the rows undecryptable.
    raise RuntimeError("Irreversible: encrypted values exceed VARCHAR(100)")

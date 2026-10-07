"""Index owner-side device and consent revocation lookups.

Revision ID: ir01_revocation_indexes
Revises: ic01_integration_context
"""

from alembic import op

revision = "ir01_revocation_indexes"
down_revision = "ic01_integration_context"
branch_labels = None
depends_on = None

_INDEXES = (
    ("integration_grants", "device_id"),
    ("integration_grants", "request_id"),
    ("integration_grant_requests", "device_id"),
)


def upgrade() -> None:
    for table, column in _INDEXES:
        op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    for table, column in reversed(_INDEXES):
        op.drop_index(f"ix_{table}_{column}", table_name=table)

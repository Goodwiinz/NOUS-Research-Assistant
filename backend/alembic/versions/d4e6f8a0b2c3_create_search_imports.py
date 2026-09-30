"""Create search import receipt/record tables (GOO-300).

Revision ID: d4e6f8a0b2c3
Revises: c9d2e4f6a8b1
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "d4e6f8a0b2c3"
down_revision = "c9d2e4f6a8b1"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)


def _deny_data_api(table: str) -> None:
    # Copied from c9d2e4f6a8b1: resolve through the connection's search_path.
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    for role in _POSTGREST_ROLES:
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    EXECUTE 'REVOKE ALL ON TABLE "{table}" FROM {role}';
                END IF;
            END
            $$
            """)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _collection_fk() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["collection_id"], ["collections.id"], ondelete="RESTRICT"
    )


def upgrade() -> None:
    op.create_table(
        "research_import_receipts",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("collection_id", _UUID, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("dedup_key", sa.String(length=160), nullable=False),
        sa.Column("lineage_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("previous_receipt_id", _UUID, nullable=True),
        sa.Column("declared", postgresql.JSONB(), nullable=False),
        sa.Column("observed", postgresql.JSONB(), nullable=False),
        sa.Column("parsed_count", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", _UUID, nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "kind IN ('file_import','citation_chase')",
            name="ck_research_import_receipt_kind",
        ),
        sa.CheckConstraint("version >= 1", name="ck_research_import_receipt_version"),
        sa.CheckConstraint(
            "accepted_count + rejected_count = parsed_count",
            name="ck_research_import_receipt_counts",
        ),
        _collection_fk(),
        sa.ForeignKeyConstraint(
            ["previous_receipt_id"],
            ["research_import_receipts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "collection_id", "dedup_key", name="uq_research_import_receipt_dedup"
        ),
        sa.UniqueConstraint(
            "collection_id",
            "lineage_key",
            "version",
            name="uq_research_import_receipt_version",
        ),
    )
    op.create_table(
        "research_import_records",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("collection_id", _UUID, nullable=False),
        sa.Column("receipt_id", _UUID, nullable=False),
        sa.Column("record_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.Column("parsed", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("rejection_reason", sa.String(length=64), nullable=True),
        sa.Column("report_id", _UUID, nullable=True),
        sa.Column("match_method", sa.String(length=32), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "status IN ('accepted','rejected')",
            name="ck_research_import_record_status",
        ),
        sa.CheckConstraint(
            "(status = 'rejected') = (rejection_reason IS NOT NULL)",
            name="ck_research_import_record_rejection",
        ),
        sa.CheckConstraint(
            "status = 'accepted' OR report_id IS NULL",
            name="ck_research_import_record_report",
        ),
        _collection_fk(),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["research_import_receipts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["report_id"], ["research_reports.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "receipt_id", "record_index", name="uq_research_import_record_index"
        ),
    )
    for table, name, columns in _INDEXES:
        op.create_index(name, table, columns)
    for table in _TABLES:
        _deny_data_api(table)


_TABLES = ("research_import_receipts", "research_import_records")
_INDEXES = (
    (
        "research_import_receipts",
        "idx_research_import_receipt_collection",
        ["collection_id"],
    ),
    (
        "research_import_records",
        "idx_research_import_record_collection",
        ["collection_id"],
    ),
    ("research_import_records", "idx_research_import_record_report", ["report_id"]),
)


def downgrade() -> None:
    for table, name, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
    for table in reversed(_TABLES):
        op.drop_table(table)

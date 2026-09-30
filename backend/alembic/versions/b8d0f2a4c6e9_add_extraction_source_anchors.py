"""Source anchors on extraction observations and accepted values (GOO-305).

All columns are nullable and there is no backfill: rows written before this
revision keep NULL, meaning "pre-anchor". Searching old citations for offsets
would invent provenance. Downgrade drops the columns.

Revision ID: b8d0f2a4c6e9
Revises: a3c5e7f9b1d4
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b8d0f2a4c6e9"
down_revision = "a3c5e7f9b1d4"
branch_labels = None
depends_on = None

_OBSERVATIONS = "extraction_observations"
_ACCEPTED = "extraction_accepted_values"
# Frozen copies of the model's check constraints (never import src here).
_OBSERVATION_CHECKS = {
    "ck_extraction_observation_anchor_status": (
        "anchor_status IS NULL OR anchor_status IN "
        "('verified','ambiguous','unverified','location_unavailable')"
    ),
    "ck_extraction_observation_anchor_pair": (
        "(anchor_start_char IS NULL) = (anchor_end_char IS NULL)"
    ),
    "ck_extraction_observation_anchor_located": (
        "anchor_start_char IS NULL OR anchor_status IN ('verified','ambiguous')"
    ),
    "ck_extraction_observation_anchor_verified": (
        "anchor_status IS NULL OR anchor_status <> 'verified'"
        " OR (anchor_start_char IS NOT NULL AND text_sha256 IS NOT NULL)"
    ),
    "ck_extraction_observation_anchor_missing": (
        "missingness IS NULL OR anchor_status IS NULL"
    ),
}
_ACCEPTED_CHECKS = {
    "ck_extraction_accepted_anchor_resolution": (
        "anchor_resolution IS NULL OR anchor_resolution IN "
        "('verified','disambiguated','accepted_unverified','not_applicable')"
    ),
    "ck_extraction_accepted_anchor_disambiguated": (
        "anchor_resolution IS NULL OR anchor_resolution <> 'disambiguated'"
        " OR anchor_start_char IS NOT NULL"
    ),
}
_Columns = tuple[tuple[str, sa.types.TypeEngine[Any]], ...]
_OBSERVATION_COLUMNS: _Columns = (
    ("anchor_status", sa.String(24)),
    ("anchor_start_char", sa.Integer()),
    ("anchor_end_char", sa.Integer()),
    ("anchor_page", sa.Integer()),
    ("anchor_occurrences", postgresql.JSONB()),
    ("occurrences_in_text", sa.Integer()),
    ("text_sha256", sa.String(64)),
    ("inspected_coverage", postgresql.JSONB()),
    ("text_length", sa.Integer()),
)
_ACCEPTED_COLUMNS: _Columns = (
    ("anchor_resolution", sa.String(24)),
    ("anchor_start_char", sa.Integer()),
    ("anchor_end_char", sa.Integer()),
    ("text_sha256", sa.String(64)),
)


def upgrade() -> None:
    for name, kind in _OBSERVATION_COLUMNS:
        op.add_column(_OBSERVATIONS, sa.Column(name, kind, nullable=True))
    for name, check in _OBSERVATION_CHECKS.items():
        op.create_check_constraint(name, _OBSERVATIONS, check)
    op.add_column(
        _ACCEPTED,
        sa.Column("anchor_observation_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_extraction_accepted_anchor_observation",
        _ACCEPTED,
        _OBSERVATIONS,
        ["anchor_observation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    for name, kind in _ACCEPTED_COLUMNS:
        op.add_column(_ACCEPTED, sa.Column(name, kind, nullable=True))
    for name, check in _ACCEPTED_CHECKS.items():
        op.create_check_constraint(name, _ACCEPTED, check)


def downgrade() -> None:
    for name in _ACCEPTED_CHECKS:
        op.drop_constraint(name, _ACCEPTED, type_="check")
    op.drop_constraint(
        "fk_extraction_accepted_anchor_observation", _ACCEPTED, type_="foreignkey"
    )
    for name, _ in reversed(_ACCEPTED_COLUMNS):
        op.drop_column(_ACCEPTED, name)
    op.drop_column(_ACCEPTED, "anchor_observation_id")
    for name in _OBSERVATION_CHECKS:
        op.drop_constraint(name, _OBSERVATIONS, type_="check")
    for name, _ in reversed(_OBSERVATION_COLUMNS):
        op.drop_column(_OBSERVATIONS, name)

"""Version extraction forms; add observations and accepted values (GOO-304).

Every existing matrix (soft-deleted ones included) gets exactly one
``legacy_unversioned`` v1 attributed to nobody: its ``columns`` in order, then
the sorted distinct live ``extraction_cells.column_name`` values missing from
them (orphans of past column edits), all typed ``text``. Cells are NOT copied
into observations - that would invent an extractor and a source version. They
stay frozen in ``extraction_cells``, addressable through the stable field id.

Offline (``--sql``) runs create the tables but cannot read existing matrices,
so the backfill is skipped there; run this revision online.

Downgrade drops the three tables. ``extraction_cells`` and
``extraction_matrices`` are never touched, so data written after the upgrade
(versions, observations, accepted values) is lost on downgrade.

Revision ID: a3c5e7f9b1d4
Revises: f2a4c6e8b0d3
"""

import hashlib
import json
from uuid import UUID, uuid4, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a3c5e7f9b1d4"
down_revision = "f2a4c6e8b0d3"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_VERSIONS = "extraction_form_versions"
_OBSERVATIONS = "extraction_observations"
_ACCEPTED = "extraction_accepted_values"
# Frozen copies of src.services.research.extraction_rules (never import src here).
_FIELD_NAMESPACE = UUID("ffb3dc50-fe01-4981-93f4-b1d1f49683c5")
_MISSINGNESS_CHECK = (
    "missingness IS NULL OR missingness IN ('not_reported','not_applicable',"
    "'unavailable_text','extraction_error','unresolved_disagreement')"
)
_XOR = "(value IS NULL) <> (missingness IS NULL)"


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


def _uuid(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, _UUID, nullable=nullable)


def _fk(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete="RESTRICT")


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _content_hash(fields: list[dict]) -> str:
    # Frozen copy of extraction_rules.form_hash + contracts.canonical_json_bytes.
    body = {"provenance": "legacy_unversioned", "protocol_version_id": None}
    canonical = json.dumps(
        {**body, "fields": fields},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _legacy_fields(matrix_id: UUID, columns: list, orphans: list[str]) -> list[dict]:
    fields: list[dict] = []
    seen: set[str] = set()
    named = [(c.get("name"), c.get("description")) for c in columns]
    for name, description in named + [(name, None) for name in orphans]:
        if not isinstance(name, str) or name in seen:
            continue
        seen.add(name)
        fields.append(
            {
                "field_id": str(uuid5(_FIELD_NAMESPACE, f"{matrix_id}:{name}")),
                "name": name,
                "description": description,
                "type": "text",
                "unit": None,
                "timepoint": None,
                "categories": None,
            }
        )
    return fields


def _backfill() -> None:
    if op.get_context().as_sql:
        return
    bind = op.get_bind()
    orphans: dict[UUID, list[str]] = {}
    for matrix_id, name in bind.execute(sa.text("""
            SELECT DISTINCT matrix_id, column_name FROM extraction_cells
            WHERE is_deleted IS NOT TRUE ORDER BY matrix_id, column_name
            """)):
        orphans.setdefault(UUID(str(matrix_id)), []).append(name)
    rows = []
    for raw_id, raw_columns, created_at in bind.execute(
        sa.text("SELECT id, columns, created_at FROM extraction_matrices")
    ):
        matrix_id = UUID(str(raw_id))
        if isinstance(raw_columns, str):
            raw_columns = json.loads(raw_columns)
        columns = [c for c in raw_columns or [] if isinstance(c, dict)]
        listed = {c.get("name") for c in columns}
        extra = [n for n in orphans.get(matrix_id, []) if n not in listed]
        fields = _legacy_fields(matrix_id, columns, extra)
        rows.append(
            {
                "id": uuid4(),
                "matrix_id": matrix_id,
                "version_no": 1,
                "provenance": "legacy_unversioned",
                "fields": fields,
                "protocol_version_id": None,
                "content_hash": _content_hash(fields),
                "created_by_id": None,
                "created_at": created_at,
            }
        )
    if not rows:
        return
    table = sa.table(
        _VERSIONS,
        sa.column("id", _UUID),
        sa.column("matrix_id", _UUID),
        sa.column("version_no", sa.Integer),
        sa.column("provenance", sa.String),
        sa.column("fields", postgresql.JSONB),
        sa.column("protocol_version_id", _UUID),
        sa.column("content_hash", sa.String),
        sa.column("created_by_id", _UUID),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(table, rows)


def upgrade() -> None:
    op.create_table(
        _VERSIONS,
        _uuid("id"),
        _uuid("matrix_id"),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("provenance", sa.String(length=24), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        _uuid("protocol_version_id", nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        _uuid("created_by_id", nullable=True),
        _created_at(),
        _fk("matrix_id", "extraction_matrices"),
        _fk("protocol_version_id", "research_protocol_versions"),
        _fk("created_by_id", "users"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "matrix_id", "version_no", name="uq_extraction_form_version_no"
        ),
        sa.CheckConstraint(
            "provenance IN ('legacy_unversioned','authored')",
            name="ck_extraction_form_version_provenance",
        ),
        sa.CheckConstraint(
            "(provenance = 'legacy_unversioned') = (created_by_id IS NULL)",
            name="ck_extraction_form_version_author",
        ),
        sa.CheckConstraint(
            "provenance <> 'legacy_unversioned' OR version_no = 1",
            name="ck_extraction_form_version_legacy_first",
        ),
        sa.CheckConstraint("version_no >= 1", name="ck_extraction_form_version_no"),
    )
    op.create_table(
        _OBSERVATIONS,
        _uuid("id"),
        _uuid("form_version_id"),
        _uuid("field_id"),
        _uuid("document_id"),
        sa.Column("kind", sa.String(length=16), nullable=False),
        _uuid("actor_user_id"),
        sa.Column("extractor_run_id", sa.String(length=64), nullable=True),
        sa.Column("extractor_model", sa.String(length=100), nullable=True),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        sa.Column("missingness", sa.String(length=32), nullable=True),
        sa.Column("validation_state", sa.String(length=16), nullable=False),
        sa.Column("citation", sa.Text(), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        _created_at(),
        _fk("form_version_id", _VERSIONS),
        _fk("document_id", "documents"),
        _fk("actor_user_id", "users"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('machine','human')", name="ck_extraction_observation_kind"
        ),
        sa.CheckConstraint(
            "(kind = 'machine') = (extractor_run_id IS NOT NULL)"
            " AND (kind = 'machine') = (extractor_model IS NOT NULL)",
            name="ck_extraction_observation_extractor",
        ),
        sa.CheckConstraint(
            _MISSINGNESS_CHECK, name="ck_extraction_observation_missing"
        ),
        sa.CheckConstraint(_XOR, name="ck_extraction_observation_xor"),
        sa.CheckConstraint(
            "validation_state IN ('valid','invalid')",
            name="ck_extraction_observation_validation",
        ),
    )
    op.create_index(
        "idx_extraction_observation_cell",
        _OBSERVATIONS,
        ["document_id", "field_id", "created_at"],
    )
    op.create_table(
        _ACCEPTED,
        _uuid("id"),
        _uuid("form_version_id"),
        _uuid("field_id"),
        _uuid("document_id"),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        sa.Column("missingness", sa.String(length=32), nullable=True),
        sa.Column("observation_ids", postgresql.JSONB(), nullable=False),
        _uuid("accepted_by_id"),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        _uuid("supersedes_accepted_value_id", nullable=True),
        _created_at(),
        _fk("form_version_id", _VERSIONS),
        _fk("document_id", "documents"),
        _fk("accepted_by_id", "users"),
        _fk("supersedes_accepted_value_id", _ACCEPTED),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_MISSINGNESS_CHECK, name="ck_extraction_accepted_missing"),
        sa.CheckConstraint(
            "missingness IS NULL OR missingness <> 'extraction_error'",
            name="ck_extraction_accepted_not_error",
        ),
        sa.CheckConstraint(_XOR, name="ck_extraction_accepted_xor"),
        sa.CheckConstraint(
            "jsonb_typeof(observation_ids) = 'array'"
            " AND jsonb_array_length(observation_ids) BETWEEN 1 AND 20",
            name="ck_extraction_accepted_citations",
        ),
        sa.UniqueConstraint(
            "supersedes_accepted_value_id", name="uq_extraction_accepted_supersedes"
        ),
    )
    op.create_index(
        "uq_extraction_accepted_initial",
        _ACCEPTED,
        ["document_id", "field_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_accepted_value_id IS NULL"),
    )
    for table in (_VERSIONS, _OBSERVATIONS, _ACCEPTED):
        _deny_data_api(table)
    _backfill()


def downgrade() -> None:
    # The indexes go with the tables; legacy tables are untouched.
    op.drop_table(_ACCEPTED)
    op.drop_table(_OBSERVATIONS)
    op.drop_table(_VERSIONS)

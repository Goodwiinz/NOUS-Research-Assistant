"""Add version-bound research protocols and run conformance.

Revision ID: b4d6f8021a3c
Revises: a3c5e7f901b2
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "b4d6f8021a3c"
down_revision = "a3c5e7f901b2"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")


def _deny_data_api(table: str) -> None:
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


def upgrade() -> None:
    op.create_table(
        "research_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["collections.id"], ondelete="RESTRICT"
        ),
    )
    op.create_table(
        "research_question_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("question_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("hypothesis", sa.Text()),
        sa.Column("scope", sa.Text()),
        sa.Column("framework", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("author_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["question_id"], ["research_questions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["parent_version_id"],
            ["research_question_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "question_id", "version", name="uq_research_question_version"
        ),
    )
    op.create_foreign_key(
        "fk_research_question_current_version",
        "research_questions",
        "research_question_versions",
        ["current_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "research_protocols",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("current_draft_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("current_approved_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["collections.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("collection_id", "name", name="uq_research_protocol_name"),
    )
    op.create_table(
        "research_protocol_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("protocol_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("question_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("blueprint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_plan", postgresql.JSONB(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column(
            "change_kind", sa.String(32), nullable=False, server_default="initial"
        ),
        sa.Column("amendment_reason", sa.Text()),
        sa.Column("author_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["protocol_id"], ["research_protocols.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["parent_version_id"],
            ["research_protocol_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["question_version_id"],
            ["research_question_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["blueprint_id"], ["research_blueprints.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('draft','approved','superseded')",
            name="ck_research_protocol_version_status",
        ),
        sa.CheckConstraint(
            "change_kind IN ('initial','amendment')",
            name="ck_research_protocol_version_change_kind",
        ),
        sa.UniqueConstraint(
            "protocol_id", "version", name="uq_research_protocol_version"
        ),
    )
    op.create_foreign_key(
        "fk_research_protocol_current_draft",
        "research_protocols",
        "research_protocol_versions",
        ["current_draft_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_research_protocol_current_approved",
        "research_protocols",
        "research_protocol_versions",
        ["current_approved_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "protocol_registration_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("protocol_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("external_identifier", sa.String(255)),
        sa.Column("url", sa.String(2048)),
        sa.Column("receipt", postgresql.JSONB()),
        sa.Column("protocol_version_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("recorded_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["collections.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["protocol_version_id"],
            ["research_protocol_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('registered','failed')", name="ck_protocol_registration_status"
        ),
        sa.UniqueConstraint(
            "protocol_version_id",
            "idempotency_key",
            name="uq_protocol_registration_idempotency",
        ),
    )
    op.create_table(
        "protocol_deviations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("protocol_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("output_reference", postgresql.UUID(as_uuid=True)),
        sa.Column("observed_difference", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["collections.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["protocol_version_id"],
            ["research_protocol_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["output_reference"], ["research_steps.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
    )
    op.add_column(
        "research_runs",
        sa.Column("protocol_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "research_runs", sa.Column("effective_plan_hash", sa.String(64), nullable=True)
    )
    op.add_column(
        "research_runs",
        sa.Column(
            "conformance_status",
            sa.String(32),
            nullable=False,
            server_default="legacy_unbound",
        ),
    )
    op.create_foreign_key(
        "fk_research_runs_protocol_version",
        "research_runs",
        "research_protocol_versions",
        ["protocol_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_research_run_conformance",
        "research_runs",
        "conformance_status IN ('plan_verified','conformant','deviated','legacy_unbound')",
    )
    op.execute("""
        CREATE FUNCTION reject_research_version_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'research versions are immutable' USING ERRCODE = '23514';
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER research_question_versions_no_update_delete
          BEFORE UPDATE OR DELETE ON research_question_versions
          FOR EACH ROW EXECUTE FUNCTION reject_research_version_mutation()
    """)
    op.execute("""
        CREATE FUNCTION guard_research_protocol_version_content() RETURNS trigger AS $$
        BEGIN
          IF ROW(OLD.id,OLD.protocol_id,OLD.version,OLD.parent_version_id,OLD.question_version_id,OLD.blueprint_id,OLD.execution_plan,OLD.snapshot,OLD.content_hash,OLD.change_kind,OLD.amendment_reason,OLD.author_user_id,OLD.created_at)
             IS DISTINCT FROM
             ROW(NEW.id,NEW.protocol_id,NEW.version,NEW.parent_version_id,NEW.question_version_id,NEW.blueprint_id,NEW.execution_plan,NEW.snapshot,NEW.content_hash,NEW.change_kind,NEW.amendment_reason,NEW.author_user_id,NEW.created_at) THEN
            RAISE EXCEPTION 'research protocol version content is immutable' USING ERRCODE = '23514';
          END IF;
          IF OLD.status = 'draft' AND NEW.status = 'approved' THEN
            IF NEW.approved_by_user_id IS NULL OR NEW.approved_by_user_id = NEW.author_user_id
               OR NEW.approved_at IS NULL OR NEW.superseded_at IS NOT NULL THEN
              RAISE EXCEPTION 'invalid protocol approval metadata' USING ERRCODE = '23514';
            END IF;
          ELSIF OLD.status = 'approved' AND NEW.status = 'superseded' THEN
            IF NEW.approved_by_user_id IS DISTINCT FROM OLD.approved_by_user_id
               OR NEW.approved_at IS DISTINCT FROM OLD.approved_at
               OR NEW.superseded_at IS NULL THEN
              RAISE EXCEPTION 'invalid protocol supersession metadata' USING ERRCODE = '23514';
            END IF;
          ELSIF NEW.status IS DISTINCT FROM OLD.status
             OR NEW.approved_by_user_id IS DISTINCT FROM OLD.approved_by_user_id
             OR NEW.approved_at IS DISTINCT FROM OLD.approved_at
             OR NEW.superseded_at IS DISTINCT FROM OLD.superseded_at THEN
            RAISE EXCEPTION 'invalid protocol state transition' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER research_protocol_versions_content_guard
          BEFORE UPDATE ON research_protocol_versions
          FOR EACH ROW EXECUTE FUNCTION guard_research_protocol_version_content()
    """)
    op.execute("""
        CREATE TRIGGER research_protocol_versions_no_delete
          BEFORE DELETE ON research_protocol_versions
          FOR EACH ROW EXECUTE FUNCTION reject_research_version_mutation()
    """)
    op.execute("""
        CREATE TRIGGER protocol_registrations_no_update_delete
          BEFORE UPDATE OR DELETE ON protocol_registration_operations
          FOR EACH ROW EXECUTE FUNCTION reject_research_version_mutation()
    """)
    op.execute("""
        CREATE TRIGGER protocol_deviations_no_update_delete
          BEFORE UPDATE OR DELETE ON protocol_deviations
          FOR EACH ROW EXECUTE FUNCTION reject_research_version_mutation()
    """)
    for table in (
        "research_questions",
        "research_question_versions",
        "research_protocols",
        "research_protocol_versions",
        "protocol_registration_operations",
        "protocol_deviations",
    ):
        _deny_data_api(table)


def downgrade() -> None:
    op.drop_constraint("ck_research_run_conformance", "research_runs", type_="check")
    op.drop_constraint(
        "fk_research_runs_protocol_version", "research_runs", type_="foreignkey"
    )
    op.drop_column("research_runs", "conformance_status")
    op.drop_column("research_runs", "effective_plan_hash")
    op.drop_column("research_runs", "protocol_version_id")
    op.execute(
        "DROP TRIGGER IF EXISTS research_protocol_versions_no_delete ON research_protocol_versions"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS research_protocol_versions_content_guard ON research_protocol_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_research_protocol_version_content()")
    op.execute(
        "DROP TRIGGER IF EXISTS research_question_versions_no_update_delete ON research_question_versions"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS protocol_registrations_no_update_delete ON protocol_registration_operations"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS protocol_deviations_no_update_delete ON protocol_deviations"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_research_version_mutation()")
    op.drop_table("protocol_deviations")
    op.drop_table("protocol_registration_operations")
    op.drop_constraint(
        "fk_research_protocol_current_approved",
        "research_protocols",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_research_protocol_current_draft", "research_protocols", type_="foreignkey"
    )
    op.drop_table("research_protocol_versions")
    op.drop_table("research_protocols")
    op.drop_constraint(
        "fk_research_question_current_version", "research_questions", type_="foreignkey"
    )
    op.drop_table("research_question_versions")
    op.drop_table("research_questions")

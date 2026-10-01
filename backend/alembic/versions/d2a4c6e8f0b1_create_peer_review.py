"""Create insert-only peer-review rounds, reviewers, comments, responses and
decisions (GOO-314).

All five tables get GOO-309's ``prevent_research_insert_only_mutation()``
trigger (SQLSTATE 55000 on UPDATE or DELETE) and the data-API lockout. All DDL
is a frozen copy of ``src.models.peer_review`` (never import src here). No
data statements.

``ck_peer_review_responses_change`` / ``_rationale`` make a response either a
change (a retained revision plus its diff hash) or an attributed no-change
rationale. ``UNIQUE(supersedes_*)`` plus the partial unique initial indexes
are the concurrency backstop: a chain can never fork. Independent of
``draft_reviews`` (machine citation review).

Downgrade refuses while any round exists (evidence is never dropped
silently), then drops the triggers and tables. The trigger function stays
(``e2a4c6b8d0f1`` owns it).

Revision ID: d2a4c6e8f0b1
Revises: c0f2a4b6d8e9
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "d2a4c6e8f0b1"
down_revision = "c0f2a4b6d8e9"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_TABLES = (
    "peer_review_rounds",
    "peer_review_reviewers",
    "peer_review_comments",
    "peer_review_responses",
    "peer_review_decisions",
)
_ANCHOR_SHAPE = (
    "(start_char IS NULL) = (quote IS NULL)"
    " AND (start_char IS NULL) = (end_char IS NULL)"
    " AND (start_char IS NULL) = (quote_sha256 IS NULL)"
    " AND (start_char IS NULL OR (0 <= start_char AND start_char < end_char))"
)
_CHANGE_SHAPE = (
    "(kind = 'change') = (revised_draft_id IS NOT NULL"
    " AND diff_sha256 IS NOT NULL AND rationale IS NULL)"
)
_NO_CHANGE_RATIONALE = (
    "kind = 'change' OR (rationale IS NOT NULL AND length(trim(rationale)) > 0)"
)
_DECISION_ROLE = (
    "(kind = 'assigned' AND actor_role = 'editor')"
    " OR (kind IN ('resolved','reopened') AND actor_role = 'adjudicator')"
)
_DECISION_SHAPE = (
    "(kind <> 'assigned' OR assignee_id IS NOT NULL)"
    " AND (kind <> 'resolved' OR response_id IS NOT NULL)"
)


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


def _now(name: str) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        "peer_review_rounds",
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("draft_id"),
        sa.Column("draft_content_hash", sa.String(64), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("received_at", sa.Date(), nullable=True),
        _uuid("created_by_id"),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("draft_id", "generated_drafts"),
        _fk("created_by_id", "users"),
    )
    op.create_index(
        "idx_peer_review_rounds_collection", "peer_review_rounds", ["collection_id"]
    )
    op.create_table(
        "peer_review_reviewers",
        _uuid("id"),
        _uuid("round_id"),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        _uuid("created_by_id"),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
        _fk("round_id", "peer_review_rounds"),
        _fk("created_by_id", "users"),
        sa.UniqueConstraint("round_id", "label", name="uq_peer_review_reviewers_label"),
        sa.UniqueConstraint("id", "round_id", name="uq_peer_review_reviewers_scope"),
    )
    op.create_table(
        "peer_review_comments",
        _uuid("id"),
        _uuid("round_id"),
        _uuid("reviewer_id"),
        sa.Column("number", sa.SmallInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        _uuid("draft_id"),
        sa.Column("draft_content_hash", sa.String(64), nullable=False),
        sa.Column("start_char", sa.Integer(), nullable=True),
        sa.Column("end_char", sa.Integer(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("quote_sha256", sa.String(64), nullable=True),
        _uuid("supersedes_comment_id", nullable=True),
        _uuid("author_id"),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["reviewer_id", "round_id"],
            ["peer_review_reviewers.id", "peer_review_reviewers.round_id"],
            ondelete="RESTRICT",
            name="fk_peer_review_comments_reviewer",
        ),
        _fk("draft_id", "generated_drafts"),
        _fk("supersedes_comment_id", "peer_review_comments"),
        _fk("author_id", "users"),
        sa.UniqueConstraint(
            "supersedes_comment_id", name="uq_peer_review_comments_supersedes"
        ),
        sa.CheckConstraint("number >= 1", name="ck_peer_review_comments_number"),
        sa.CheckConstraint(_ANCHOR_SHAPE, name="ck_peer_review_comments_anchor"),
    )
    op.create_index(
        "uq_peer_review_comments_initial",
        "peer_review_comments",
        ["round_id", "number"],
        unique=True,
        postgresql_where=sa.text("supersedes_comment_id IS NULL"),
    )
    op.create_index(
        "idx_peer_review_comments_round", "peer_review_comments", ["round_id"]
    )
    op.create_table(
        "peer_review_responses",
        _uuid("id"),
        _uuid("comment_root_id"),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        _uuid("revised_draft_id", nullable=True),
        sa.Column("revised_content_hash", sa.String(64), nullable=True),
        _uuid("base_draft_id", nullable=True),
        sa.Column("diff_sha256", sa.String(64), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "evidence_claim_version_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        _uuid("supersedes_response_id", nullable=True),
        _uuid("author_id"),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
        _fk("comment_root_id", "peer_review_comments"),
        _fk("revised_draft_id", "generated_drafts"),
        _fk("base_draft_id", "generated_drafts"),
        _fk("supersedes_response_id", "peer_review_responses"),
        _fk("author_id", "users"),
        sa.UniqueConstraint(
            "supersedes_response_id", name="uq_peer_review_responses_supersedes"
        ),
        sa.CheckConstraint(
            "kind IN ('change','no_change')", name="ck_peer_review_responses_kind"
        ),
        sa.CheckConstraint(_CHANGE_SHAPE, name="ck_peer_review_responses_change"),
        sa.CheckConstraint(
            _NO_CHANGE_RATIONALE, name="ck_peer_review_responses_rationale"
        ),
    )
    op.create_index(
        "uq_peer_review_responses_initial",
        "peer_review_responses",
        ["comment_root_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_response_id IS NULL"),
    )
    op.create_table(
        "peer_review_decisions",
        _uuid("id"),
        _uuid("comment_root_id"),
        sa.Column("kind", sa.String(16), nullable=False),
        _uuid("assignee_id", nullable=True),
        _uuid("response_id", nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        _uuid("actor_id"),
        sa.Column("actor_role", sa.String(16), nullable=False),
        _uuid("supersedes_decision_id", nullable=True),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
        _fk("comment_root_id", "peer_review_comments"),
        _fk("assignee_id", "users"),
        _fk("response_id", "peer_review_responses"),
        _fk("actor_id", "users"),
        _fk("supersedes_decision_id", "peer_review_decisions"),
        sa.UniqueConstraint(
            "supersedes_decision_id", name="uq_peer_review_decisions_supersedes"
        ),
        sa.CheckConstraint(
            "kind IN ('assigned','resolved','reopened')",
            name="ck_peer_review_decisions_kind",
        ),
        sa.CheckConstraint(_DECISION_ROLE, name="ck_peer_review_decisions_role"),
        sa.CheckConstraint(_DECISION_SHAPE, name="ck_peer_review_decisions_shape"),
    )
    op.execute("""
        CREATE UNIQUE INDEX uq_peer_review_decisions_initial
        ON peer_review_decisions (comment_root_id, (kind = 'assigned'))
        WHERE supersedes_decision_id IS NULL
        """)
    for table in _TABLES:
        _deny_data_api(table)
        op.execute(f"""
            CREATE TRIGGER trg_{table}_insert_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION prevent_research_insert_only_mutation()
            """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM peer_review_rounds")).first() is not None:
        raise RuntimeError("peer-review rounds exist; refusing to drop their evidence")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_insert_only ON {table}")
        op.drop_table(table)

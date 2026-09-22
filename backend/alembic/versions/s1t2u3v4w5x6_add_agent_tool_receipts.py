"""add agent_tool_receipts

One write-once row per executed side-effecting tool call, so a turn resumed
from the pre-``tool_node`` checkpoint cannot re-run ``create_project`` /
``create_project_note`` / ``create_draft`` / ``ingest_arxiv_papers`` /
``execute_code`` (audit B8-I1).

to_regclass/IF NOT EXISTS guarded: bootstrap-provisioned databases already
have the table via ``Base.metadata.create_all``.

Revision ID: s1t2u3v4w5x6
Revises: r7_drop_research_evidence
Create Date: 2026-09-05
"""

from alembic import op  # type: ignore[attr-defined]

revision = "s1t2u3v4w5x6"
down_revision = "r7_drop_research_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_tool_receipts (
            tool_call_id varchar(128) PRIMARY KEY,
            thread_id varchar(64),
            tool_name varchar(64) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_agent_tool_receipts_thread_id
        ON agent_tool_receipts (thread_id)
        """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF to_regclass('public.agent_tool_receipts') IS NOT NULL THEN
                DROP TABLE agent_tool_receipts;
            END IF;
        END $$
        """)

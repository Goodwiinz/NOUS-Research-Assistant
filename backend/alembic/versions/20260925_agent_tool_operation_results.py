"""Add scoped durable agent tool operation claims and results.

Revision ID: agent_ops_20260925
Revises: u3v4w5x6y7z8
Create Date: 2026-09-26
"""

from alembic import op

revision = "agent_ops_20260925"
down_revision = "u3v4w5x6y7z8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_tool_operations (
            operation_id varchar(64) PRIMARY KEY,
            organization_id uuid,
            user_id uuid NOT NULL,
            thread_id varchar(128) NOT NULL,
            turn_id varchar(128) NOT NULL,
            tool_call_id varchar(128) NOT NULL,
            tool_name varchar(64) NOT NULL,
            args_hash varchar(64) NOT NULL,
            state varchar(16) NOT NULL
                CHECK (state IN ('claimed', 'dispatched', 'completed', 'unknown')),
            owner_token uuid NOT NULL,
            result jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_agent_tool_operations_thread_turn
        ON agent_tool_operations (thread_id, turn_id)
        """)
    # These records are backend orchestration state. Browser roles receive no
    # direct visibility; the backend's owner/service role remains unchanged.
    op.execute("ALTER TABLE agent_tool_operations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_tool_receipts ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                EXECUTE 'REVOKE ALL ON TABLE agent_tool_operations FROM anon';
                EXECUTE 'REVOKE ALL ON TABLE agent_tool_receipts FROM anon';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                EXECUTE 'REVOKE ALL ON TABLE agent_tool_operations FROM authenticated';
                EXECUTE 'REVOKE ALL ON TABLE agent_tool_receipts FROM authenticated';
            END IF;
        END
        $$
        """)


def downgrade() -> None:
    # Legacy receipt hardening intentionally survives downgrade.
    op.execute("DROP TABLE IF EXISTS agent_tool_operations")

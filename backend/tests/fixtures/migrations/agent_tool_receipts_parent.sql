-- Affected-schema parent fixture for Task 2 migration verification.
-- Provenance: backend/alembic/versions/s1t2u3v4w5x6_add_agent_tool_receipts.py
-- Observed parent: u3v4w5x6y7z8. This is not a reconstructed full database.
-- The fixture intentionally has no agent_tool_operations table.
CREATE TABLE agent_tool_receipts (
    tool_call_id varchar(128) PRIMARY KEY,
    thread_id varchar(64),
    tool_name varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_agent_tool_receipts_thread_id
    ON agent_tool_receipts (thread_id);

INSERT INTO agent_tool_receipts (tool_call_id, thread_id, tool_name)
VALUES
    ('legacy_call_with_thread', 'thread_fixture_1', 'create_project'),
    ('legacy_call_without_thread', NULL, 'create_project_note');

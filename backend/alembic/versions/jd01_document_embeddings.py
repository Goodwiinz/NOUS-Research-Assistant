"""Create document_embeddings for the pgvector semantic channel (GOO-435).

Revision ID: jd01_document_embeddings
Revises: aw03_artifact_completion
Create Date: 2026-10-13

Plan 07 slice 5a. One table, one row per document, kept apart from
``documents`` so the hot documents table and its ORM stay untouched:

- ``document_id`` primary key and FK to ``documents`` ON DELETE CASCADE;
- ``organization_id`` NOT NULL, FK to ``organizations`` ON DELETE CASCADE,
  indexed (a copy of the document's organization, so every semantic query can
  filter it on this table too);
- ``embedding vector(1024)`` NOT NULL (1024 = ``settings.COHERE_EMBED_DIMENSIONS``,
  Cohere embed-v-4-0 on Azure; a provider that answers with another length is
  skipped by the service, never stored);
- ``model`` and ``content_hash`` (the service re-embeds only when the hash or
  the model changed) and ``created_at`` / ``updated_at``;
- an HNSW index on ``embedding vector_cosine_ops`` for ``embedding <=> :query``;
- the data-API lockout every internal table gets (RLS on, ``anon`` and
  ``authenticated`` revoked; copied from ``c2f4b6d8e0a1``).

DDL is a frozen copy of ``src.models.document_embedding`` (never import src
here).

pgvector availability. ``CREATE EXTENSION IF NOT EXISTS vector`` runs when the
server ships the extension (RDS PostgreSQL 16 does; the migration role needs
``rds_superuser`` or the right to create extensions). The revision never fails
the chain over pgvector, because nothing reads or writes the table unless
``DOCUMENT_EMBEDDINGS_ENABLED`` / ``SEMANTIC_SEARCH_ENABLED`` are turned on,
and both default OFF. A failed revision would instead fail the migration Job,
which blocks the whole AWS dev rollout (``docs/engineering/gotchas.md``). It
skips itself with a WARNING, creating nothing, in two cases:

- the server does not ship ``vector`` at all (the stock ``postgres:15``
  images that CI's blocking integration lane runs ``alembic upgrade head`` on,
  or a developer's plain local database);
- the DDL itself is refused: the role may not create the extension
  (``insufficient_privilege``), or the installed pgvector predates 0.5 and has
  no ``hnsw`` access method (``undefined_object``), or the extension's files
  are missing (``undefined_file``, ``feature_not_supported``). The nested
  block rolls back everything it did, so a refused run leaves no half-made
  table or extension behind.

To create the table afterwards, fix the cause, then apply this revision again.
Everything in it is idempotent:

- while ``jd01_document_embeddings`` is still the head:
  ``alembic downgrade aw03_artifact_completion && alembic upgrade
  jd01_document_embeddings``;
- once a later revision exists, never downgrade (that undoes the later
  revisions too). Replay only this revision's SQL instead:
  ``alembic upgrade aw03_artifact_completion:jd01_document_embeddings --sql``
  piped to ``psql``. Its version-table ``UPDATE`` matches no row, so the
  recorded head stays where it is.

Do either before enabling either flag.

Downgrade drops the table and its indexes and leaves the extension installed
(other objects may use it).
"""

from alembic import op  # type: ignore[attr-defined]

revision = "jd01_document_embeddings"
down_revision = "aw03_artifact_completion"
branch_labels = None
depends_on = None

_UPGRADE = """
DO $jd01$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_available_extensions WHERE name = 'vector'
    ) THEN
        RAISE WARNING 'jd01_document_embeddings: the pgvector extension is not available on this server; document_embeddings was NOT created. Install pgvector and apply this revision again (the recovery steps are in its docstring; while it is the head: alembic downgrade aw03_artifact_completion && alembic upgrade jd01_document_embeddings) before enabling DOCUMENT_EMBEDDINGS_ENABLED or SEMANTIC_SEARCH_ENABLED.';
        RETURN;
    END IF;

    -- A refused statement rolls this whole block back and warns instead of
    -- failing the chain (and with it the migration Job and the rollout).
    BEGIN
        CREATE EXTENSION IF NOT EXISTS vector;

        CREATE TABLE IF NOT EXISTS document_embeddings (
            document_id uuid NOT NULL PRIMARY KEY
                REFERENCES documents (id) ON DELETE CASCADE,
            organization_id uuid NOT NULL
                REFERENCES organizations (id) ON DELETE CASCADE,
            embedding vector(1024) NOT NULL,
            model varchar(255) NOT NULL,
            content_hash varchar(64) NOT NULL,
            created_at timestamp with time zone NOT NULL DEFAULT now(),
            updated_at timestamp with time zone NOT NULL DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS ix_document_embeddings_organization_id
            ON document_embeddings (organization_id);

        CREATE INDEX IF NOT EXISTS ix_document_embeddings_embedding_hnsw
            ON document_embeddings USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);

        ALTER TABLE document_embeddings ENABLE ROW LEVEL SECURITY;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE document_embeddings FROM anon;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE document_embeddings FROM authenticated;
        END IF;
    EXCEPTION
        WHEN insufficient_privilege OR feature_not_supported
            OR undefined_object OR undefined_file THEN
            RAISE WARNING 'jd01_document_embeddings: pgvector DDL was refused (SQLSTATE %: %); document_embeddings was NOT created and nothing of this revision was kept. The migration role needs the right to CREATE EXTENSION vector, and pgvector must be 0.5 or later (hnsw). Fix that and apply this revision again (the recovery steps are in its docstring; while it is the head: alembic downgrade aw03_artifact_completion && alembic upgrade jd01_document_embeddings) before enabling DOCUMENT_EMBEDDINGS_ENABLED or SEMANTIC_SEARCH_ENABLED.', SQLSTATE, SQLERRM;
    END;
END
$jd01$
"""


def upgrade() -> None:
    op.execute(_UPGRADE)


def downgrade() -> None:
    # The indexes, constraints and grants go with the table; the extension stays.
    op.execute("DROP TABLE IF EXISTS document_embeddings")

"""Create document_embeddings for the pgvector semantic channel (GOO-435).

Revision ID: jd01_document_embeddings
Revises: ic02_selected_skill_snapshot
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
``rds_superuser`` or the right to create extensions). Where it does not (the
stock ``postgres:15`` images that CI's blocking integration lane runs
``alembic upgrade head`` on, or a developer's plain local database) the whole
revision is skipped with a WARNING instead of failing the chain, because
nothing reads or writes the table unless ``DOCUMENT_EMBEDDINGS_ENABLED`` /
``SEMANTIC_SEARCH_ENABLED`` are turned on, and both default OFF. On such a
server, install pgvector and re-run the revision (``alembic downgrade -1 &&
alembic upgrade head``) before enabling either flag.

Downgrade drops the table and its indexes and leaves the extension installed
(other objects may use it).
"""

from alembic import op  # type: ignore[attr-defined]

revision = "jd01_document_embeddings"
down_revision = "ic02_selected_skill_snapshot"
branch_labels = None
depends_on = None

_UPGRADE = """
DO $jd01$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_available_extensions WHERE name = 'vector'
    ) THEN
        RAISE WARNING 'jd01_document_embeddings: the pgvector extension is not available on this server; document_embeddings was NOT created. Install pgvector and re-run this revision (alembic downgrade -1 and alembic upgrade head) before enabling DOCUMENT_EMBEDDINGS_ENABLED or SEMANTIC_SEARCH_ENABLED.';
        RETURN;
    END IF;

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
END
$jd01$
"""


def upgrade() -> None:
    op.execute(_UPGRADE)


def downgrade() -> None:
    # The indexes, constraints and grants go with the table; the extension stays.
    op.execute("DROP TABLE IF EXISTS document_embeddings")

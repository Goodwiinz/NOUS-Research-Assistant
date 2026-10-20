"""Document embeddings for the pgvector semantic search channel (GOO-435).

One row per document, kept apart from ``documents`` so the hot documents table
and its ORM stay untouched. The vector is the document's title plus abstract
or summary plus the head of its text (see ``document_embedding_service``),
embedded through ``EmbeddingService``.

``DOCUMENT_EMBEDDING_DIMENSIONS`` is the column width, which equals
``settings.COHERE_EMBED_DIMENSIONS`` (Cohere embed-v-4-0 on Azure). A provider
that answers with another length is skipped, never stored.

``organization_id`` copies the document's organization so every semantic query
can filter it on this table as well as on ``documents`` (defense in depth for
the tenant rule in ``docs/engineering/gotchas.md``).

Migration-owned, deliberately NOT on ``Base.metadata``. The table needs the
``vector`` extension, which stock PostgreSQL (CI's ``postgres:15`` services,
a plain local database) does not ship. ``Base.metadata.create_all`` runs in
``r6h3_model_baseline`` (mid-chain, before any extension exists), in the
integration-test fixtures and in the local-dev bootstrap, so a ``VECTOR``
column on ``Base`` would make every one of them fail with
``type "vector" does not exist``. The model therefore lives on its own
declarative base, and only ``jd01_document_embeddings`` creates the table (and
only where pgvector is available). The foreign keys to ``documents`` and
``organizations`` are in that migration, not here, because this metadata does
not contain those tables. Nothing reads or writes the table unless
``DOCUMENT_EMBEDDINGS_ENABLED`` / ``SEMANTIC_SEARCH_ENABLED`` are on.
"""

from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, Index, String, func
from sqlalchemy.orm import declarative_base

from .base import GUID

# Width of ``document_embeddings.embedding``; keep in step with the migration.
DOCUMENT_EMBEDDING_DIMENSIONS = 1024

# Tables that exist only through an Alembic revision. ``alembic/env.py`` leaves
# them out of autogenerate comparison, since they are not in ``Base.metadata``.
MIGRATION_OWNED_TABLES = frozenset({"document_embeddings"})

# A separate registry and MetaData: see the module docstring.
EmbeddingBase = declarative_base()


class DocumentEmbedding(EmbeddingBase):  # type: ignore[valid-type, misc]
    """The semantic embedding of one document (``document_id`` is the key)."""

    __tablename__ = "document_embeddings"

    # FK -> documents.id ON DELETE CASCADE (declared in jd01).
    document_id: Any = Column(GUID(), primary_key=True)
    # FK -> organizations.id ON DELETE CASCADE (declared in jd01).
    organization_id: Any = Column(GUID(), nullable=False, index=True)
    embedding = Column(Vector(DOCUMENT_EMBEDDING_DIMENSIONS), nullable=False)
    # Embedding provider and model that produced the vector ("cohere:embed-v-4-0").
    model = Column(String(255), nullable=False)
    # sha256 of the exact text that was embedded; an unchanged hash (and model)
    # means no provider call.
    content_hash = Column(String(64), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # Cosine distance, matching ``embedding <=> :query`` in
        # ``semantic_search_service``. HNSW params are pgvector's defaults.
        Index(
            "ix_document_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<DocumentEmbedding(document_id={self.document_id}, model={self.model})>"
        )

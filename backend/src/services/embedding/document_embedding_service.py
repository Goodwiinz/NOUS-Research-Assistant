"""Document embeddings for the pgvector semantic channel (GOO-435, Plan 07 S5a).

``DocumentEmbeddingService.embed_document`` writes one ``document_embeddings``
row per document: the vector of its title plus abstract or summary plus the
head of its text, produced by the existing ``EmbeddingService`` (provider
priority cohere > azure_openai > sentence_transformers) and upserted by
``document_id``.

Rules this module enforces:

- The column is ``vector(1024)`` (``DOCUMENT_EMBEDDING_DIMENSIONS``, equal to
  ``settings.COHERE_EMBED_DIMENSIONS``). A vector of any other length, and any
  vector from the sentence-transformers provider (another model, another
  space), is logged and skipped, never stored.
- Nothing runs unless ``settings.DOCUMENT_EMBEDDINGS_ENABLED`` is on.
- Embedding must never fail document processing: ``embed_document`` returns an
  ``EmbedOutcome`` and does not raise; a failed attempt rolls the session back
  and logs. The backfill task retries later.
- The text is hashed; an unchanged hash with an unchanged model is not
  re-embedded.
- Each call that writes owns one transaction boundary: it commits the session
  it is given (callers pass a session of their own).

The query side (``semantic_search_service``) embeds queries through
``DocumentEmbeddingService.embed_text`` so both sides share the provider and
dimension rules.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.document import Document
from src.models.document_embedding import (
    DOCUMENT_EMBEDDING_DIMENSIONS,
    DocumentEmbedding,
)
from src.models.vector import BatchEmbeddingRequest, EmbeddingRequest

logger = logging.getLogger(__name__)

# Characters of ``Document.content_text`` that join the title and abstract in
# the embedded text.
TEXT_HEAD_CHARS = 4000

# Providers whose vectors may be stored. sentence_transformers (also what a
# response with no provider means) is a different model and vector space.
_ACCEPTED_PROVIDERS = frozenset({"cohere", "azure_openai"})


class EmbedOutcome(str, Enum):
    """What ``embed_document`` did; it never raises."""

    EMBEDDED = "embedded"  # a row was written or replaced
    UNCHANGED = "unchanged"  # same text and model; provider not called
    DISABLED = "disabled"  # DOCUMENT_EMBEDDINGS_ENABLED is off
    SKIPPED = "skipped"  # nothing to embed, or the vector is not storable
    FAILED = "failed"  # provider or database error (logged, rolled back)


class EmbeddingRejected(Exception):
    """The provider answered, but not with a vector this column can hold."""


@dataclass(frozen=True)
class TextEmbedding:
    """A storable vector and the provider/model label that produced it."""

    vector: list[float]
    model: str


def _normalise(value: str) -> str:
    return " ".join(value.split())


def build_embedding_text(document: Document) -> str:
    """Title, then abstract or summary, then the head of the text.

    The abstract (``content_summary``, else the metadata ``abstract`` /
    ``description``) is left out when the text head already contains it, as it
    does for arXiv documents whose text starts with their abstract.
    """
    raw_metadata = document.document_metadata
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    title = _normalise(str(document.title or ""))
    head = str(document.content_text or "")[:TEXT_HEAD_CHARS].strip()
    abstract = _normalise(
        str(
            document.content_summary
            or metadata.get("abstract")
            or metadata.get("description")
            or ""
        )
    )
    parts = [title]
    # Metadata descriptions are cut with a trailing "..."; match on a prefix.
    probe = abstract.rstrip(". ")[:120]
    if abstract and (not probe or probe not in _normalise(head)):
        parts.append(abstract)
    if head:
        parts.append(head)
    return "\n\n".join(part for part in parts if part)


def _model_label(provider: str) -> str:
    if provider == "cohere":
        return f"cohere:{settings.COHERE_EMBED_MODEL}"
    from src.services.infrastructure.azure_openai_service import azure_openai_service

    return f"{provider}:{azure_openai_service.get_embedding_deployment()}"


class DocumentEmbeddingService:
    """Embeds documents (and search queries) for the ``document_embeddings`` column."""

    def __init__(self, embedder: Any = None) -> None:
        # ``EmbeddingService`` is imported lazily: it pulls in torch and
        # sentence-transformers when installed.
        self._embedder = embedder

    @property
    def embedder(self) -> Any:
        if self._embedder is None:
            from src.services.embedding.embedding_service import embedding_service

            self._embedder = embedding_service
        return self._embedder

    # -- provider ---------------------------------------------------------

    def _active_provider(self) -> str:
        # ``get_model_info`` runs the embedder's lazy provider resolution
        # (cohere > azure_openai > sentence_transformers).
        self.embedder.get_model_info()
        return str(self.embedder.embedding_provider)

    async def embed_text(self, text: str, *, for_query: bool) -> TextEmbedding:
        """Embed ``text`` through ``EmbeddingService`` into a storable vector.

        ``for_query`` picks the query form (Cohere ``search_query``) over the
        document form (``search_document``). Raises ``EmbeddingRejected`` when
        the provider is not storable or the vector has the wrong length, and
        lets provider errors propagate.
        """
        provider = self._active_provider()
        if provider not in _ACCEPTED_PROVIDERS:
            raise EmbeddingRejected(f"provider {provider!r} is not storable")
        # ``provider=`` is passed explicitly: EmbeddingService reads it from
        # the request and only falls back to its own choice when the attribute
        # is missing, not when it is None.
        if for_query:
            single = await self.embedder.generate_embedding(
                EmbeddingRequest(text=text, provider=provider)
            )
            vector, served_by = single.embedding, single.provider
        else:
            batch = await self.embedder.generate_batch_embeddings(
                BatchEmbeddingRequest(texts=[text], provider=provider)
            )
            first = batch.embeddings[0] if batch.embeddings else None
            if first is None:
                raise RuntimeError("the embedding provider returned no vector")
            vector, served_by = first, batch.provider
        if served_by not in _ACCEPTED_PROVIDERS:
            raise EmbeddingRejected(f"answered by provider {served_by!r}")
        if len(vector) != DOCUMENT_EMBEDDING_DIMENSIONS:
            raise EmbeddingRejected(
                f"{served_by} returned {len(vector)} dimensions, "
                f"the column holds {DOCUMENT_EMBEDDING_DIMENSIONS}"
            )
        if not all(math.isfinite(value) for value in vector):
            raise EmbeddingRejected("the vector has a non-finite component")
        return TextEmbedding(vector=list(vector), model=_model_label(str(served_by)))

    # -- documents --------------------------------------------------------

    async def embed_document(
        self, session: AsyncSession, document: Document
    ) -> EmbedOutcome:
        """Embed ``document`` and upsert its ``document_embeddings`` row.

        Commits ``session`` when it writes. Never raises: a provider or
        database error rolls the session back, is logged with ids only, and
        returns ``FAILED``.
        """
        if not settings.DOCUMENT_EMBEDDINGS_ENABLED:
            return EmbedOutcome.DISABLED
        # Scalars first: after a rollback the ORM object is expired, and an
        # async session cannot lazy-load it.
        document_id = document.id
        organization_id = document.organization_id
        try:
            return await self._embed_document(session, document)
        except EmbeddingRejected as rejected:
            await self._rollback(session)
            logger.warning(
                "document embedding skipped: document_id=%s organization_id=%s "
                "reason=%s",
                document_id,
                organization_id,
                rejected,
            )
            return EmbedOutcome.SKIPPED
        except Exception:  # noqa: BLE001 - embedding must never fail the caller
            await self._rollback(session)
            logger.warning(
                "document embedding failed: document_id=%s organization_id=%s",
                document_id,
                organization_id,
                exc_info=True,
            )
            return EmbedOutcome.FAILED

    async def _embed_document(
        self, session: AsyncSession, document: Document
    ) -> EmbedOutcome:
        if document.is_deleted:
            return EmbedOutcome.SKIPPED
        text = build_embedding_text(document)
        if not text.strip():
            return EmbedOutcome.SKIPPED
        document_id = document.id
        organization_id = document.organization_id
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        provider = self._active_provider()
        if provider in _ACCEPTED_PROVIDERS:
            current = (
                await session.execute(
                    select(DocumentEmbedding.content_hash, DocumentEmbedding.model)
                    .where(DocumentEmbedding.document_id == document_id)
                    .limit(1)
                )
            ).first()
            if current is not None and current == (
                content_hash,
                _model_label(provider),
            ):
                return EmbedOutcome.UNCHANGED

        embedded = await self.embed_text(text, for_query=False)

        statement = pg_insert(DocumentEmbedding).values(
            document_id=document_id,
            organization_id=organization_id,
            embedding=embedded.vector,
            model=embedded.model,
            content_hash=content_hash,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[DocumentEmbedding.document_id],
                set_={
                    "organization_id": statement.excluded.organization_id,
                    "embedding": statement.excluded.embedding,
                    "model": statement.excluded.model,
                    "content_hash": statement.excluded.content_hash,
                    "updated_at": func.now(),
                },
            )
        )
        await session.commit()
        return EmbedOutcome.EMBEDDED

    @staticmethod
    async def _rollback(session: AsyncSession) -> None:
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 - the original error is what matters
            logger.debug("rollback after a failed embedding also failed", exc_info=True)


document_embedding_service = DocumentEmbeddingService()

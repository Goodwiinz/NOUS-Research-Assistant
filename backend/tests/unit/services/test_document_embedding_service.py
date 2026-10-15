"""DocumentEmbeddingService (GOO-435 S5a): what gets stored, skipped, or refused.

The session and the embedder are fakes, so these run without PostgreSQL or a
provider; ``tests/integration/test_semantic_search_postgres.py`` runs the same
code against pgvector.
"""

from __future__ import annotations

import hashlib
import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from src.core.config import settings
from src.models.document_embedding import DOCUMENT_EMBEDDING_DIMENSIONS
from src.models.vector import BatchEmbeddingResponse, EmbeddingResponse
from src.services.embedding.document_embedding_service import (
    TEXT_HEAD_CHARS,
    DocumentEmbeddingService,
    EmbedOutcome,
    build_embedding_text,
)

pytestmark = pytest.mark.unit

DIM = DOCUMENT_EMBEDDING_DIMENSIONS


class FakeEmbedder:
    """Stands in for ``EmbeddingService``: records calls, answers as configured."""

    def __init__(
        self,
        *,
        provider: str = "cohere",
        vector: list[float] | None = None,
        served_by: str | None = None,
    ) -> None:
        self.embedding_provider = provider
        self.vector = vector if vector is not None else [0.5] * DIM
        self.served_by = served_by or provider
        self.fail: Exception | None = None
        self.batch_requests: list[Any] = []
        self.single_requests: list[Any] = []
        self.initialised = 0

    def get_model_info(self) -> dict[str, Any]:
        self.initialised += 1
        return {}

    async def generate_batch_embeddings(self, request: Any) -> BatchEmbeddingResponse:
        self.batch_requests.append(request)
        if self.fail is not None:
            raise self.fail
        return BatchEmbeddingResponse(
            embeddings=[self.vector],
            model="embed-v-4-0",
            dimension=len(self.vector),
            processing_time=0.0,
            failed_count=0,
            provider=self.served_by,
        )

    async def generate_embedding(self, request: Any) -> EmbeddingResponse:
        self.single_requests.append(request)
        if self.fail is not None:
            raise self.fail
        return EmbeddingResponse(
            embedding=self.vector,
            model="embed-v-4-0",
            dimension=len(self.vector),
            processing_time=0.0,
            provider=self.served_by,
        )


class FakeSession:
    """Records statements; ``existing`` is what the lookup SELECT returns."""

    def __init__(self, existing: tuple[str, str] | None = None) -> None:
        self.existing = existing
        self.statements: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_on_write: Exception | None = None

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        if getattr(statement, "is_insert", False):
            if self.fail_on_write is not None:
                raise self.fail_on_write
            return SimpleNamespace()
        return SimpleNamespace(first=lambda: self.existing)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    @property
    def writes(self) -> list[Any]:
        return [s for s in self.statements if getattr(s, "is_insert", False)]


def _document(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "title": "Graph neural networks",
        "content_summary": None,
        "content_text": "We study message passing on graphs.",
        "document_metadata": {},
        "is_deleted": False,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture(autouse=True)
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DOCUMENT_EMBEDDINGS_ENABLED", True)
    monkeypatch.setattr(settings, "COHERE_EMBED_MODEL", "embed-v-4-0")


def _service(embedder: FakeEmbedder) -> DocumentEmbeddingService:
    return DocumentEmbeddingService(embedder=embedder)


# --- flag, skip, failure -------------------------------------------------------


async def test_flag_off_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DOCUMENT_EMBEDDINGS_ENABLED", False)
    embedder, session = FakeEmbedder(), FakeSession()

    outcome = await _service(embedder).embed_document(session, _document())  # type: ignore[arg-type]

    assert outcome is EmbedOutcome.DISABLED
    assert embedder.initialised == 0 and not embedder.batch_requests
    assert session.statements == [] and session.commits == 0


async def test_unchanged_text_and_model_is_not_re_embedded() -> None:
    document = _document()
    text = build_embedding_text(document)  # type: ignore[arg-type]
    stored = (hashlib.sha256(text.encode()).hexdigest(), "cohere:embed-v-4-0")
    embedder, session = FakeEmbedder(), FakeSession(existing=stored)

    outcome = await _service(embedder).embed_document(session, document)  # type: ignore[arg-type]

    assert outcome is EmbedOutcome.UNCHANGED
    assert not embedder.batch_requests  # no provider call
    assert session.writes == [] and session.commits == 0


@pytest.mark.parametrize(
    "stored",
    [
        ("0" * 64, "cohere:embed-v-4-0"),  # the text changed
        (None, "cohere:another-model"),  # the model changed (hash filled in below)
    ],
    ids=["text-changed", "model-changed"],
)
async def test_changed_text_or_model_is_re_embedded_and_upserted(
    stored: tuple[str | None, str],
) -> None:
    document = _document()
    text_hash = hashlib.sha256(build_embedding_text(document).encode()).hexdigest()  # type: ignore[arg-type]
    existing = (stored[0] or text_hash, stored[1])
    embedder, session = FakeEmbedder(), FakeSession(existing=existing)

    outcome = await _service(embedder).embed_document(session, document)  # type: ignore[arg-type]

    assert outcome is EmbedOutcome.EMBEDDED
    assert len(embedder.batch_requests) == 1
    assert session.commits == 1 and session.rollbacks == 0
    (write,) = session.writes
    sql = str(write.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (document_id) DO UPDATE" in sql
    params = write.compile(dialect=postgresql.dialect()).params
    assert params["document_id"] == document.id
    # The organization is copied from the document row, never taken elsewhere.
    assert params["organization_id"] == document.organization_id
    assert params["content_hash"] == text_hash
    assert params["model"] == "cohere:embed-v-4-0"


async def test_new_document_is_written_once_in_one_transaction() -> None:
    embedder, session = FakeEmbedder(), FakeSession(existing=None)

    outcome = await _service(embedder).embed_document(session, _document())  # type: ignore[arg-type]

    assert outcome is EmbedOutcome.EMBEDDED
    assert len(session.writes) == 1 and session.commits == 1


async def test_provider_is_passed_explicitly_to_the_embedder() -> None:
    """``EmbeddingService`` falls back to sentence-transformers when the request
    carries ``provider=None``; the request must name the active provider."""
    embedder, session = FakeEmbedder(), FakeSession()

    await _service(embedder).embed_document(session, _document())  # type: ignore[arg-type]

    assert embedder.initialised >= 1  # lazy provider resolution ran first
    assert embedder.batch_requests[0].provider == "cohere"


@pytest.mark.parametrize("length", [384, 1536, DIM - 1, DIM + 1])
async def test_wrong_dimension_is_logged_and_skipped(
    length: int, caplog: pytest.LogCaptureFixture
) -> None:
    embedder = FakeEmbedder(vector=[0.1] * length)
    session = FakeSession()

    with caplog.at_level(logging.WARNING):
        outcome = await _service(embedder).embed_document(session, _document())  # type: ignore[arg-type]

    assert outcome is EmbedOutcome.SKIPPED
    assert session.writes == [] and session.commits == 0
    assert any(str(length) in record.getMessage() for record in caplog.records)


async def test_sentence_transformers_vectors_are_never_stored() -> None:
    """Even at the right length: another model, another vector space."""
    embedder = FakeEmbedder(provider="sentence_transformers", vector=[0.1] * DIM)
    session = FakeSession()

    outcome = await _service(embedder).embed_document(session, _document())  # type: ignore[arg-type]

    assert outcome is EmbedOutcome.SKIPPED
    assert not embedder.batch_requests  # not even asked
    assert session.writes == [] and session.commits == 0


async def test_an_answer_from_a_different_provider_is_refused() -> None:
    """EmbeddingService falls back to another provider on error; its vector is
    from another space, so it is refused even when the length fits."""
    embedder = FakeEmbedder(provider="cohere", served_by="sentence_transformers")
    session = FakeSession()

    outcome = await _service(embedder).embed_document(session, _document())  # type: ignore[arg-type]

    assert outcome is EmbedOutcome.SKIPPED
    assert session.writes == []


async def test_non_finite_vectors_are_refused() -> None:
    embedder = FakeEmbedder(vector=[float("nan")] + [0.1] * (DIM - 1))
    session = FakeSession()

    outcome = await _service(embedder).embed_document(session, _document())  # type: ignore[arg-type]

    assert outcome is EmbedOutcome.SKIPPED and session.writes == []


async def test_provider_failure_does_not_raise() -> None:
    embedder = FakeEmbedder()
    embedder.fail = RuntimeError("cohere is down")
    session = FakeSession()

    outcome = await _service(embedder).embed_document(session, _document())  # type: ignore[arg-type]

    assert outcome is EmbedOutcome.FAILED
    assert session.rollbacks == 1 and session.commits == 0 and session.writes == []


async def test_database_failure_does_not_raise_and_rolls_back() -> None:
    embedder, session = FakeEmbedder(), FakeSession()
    session.fail_on_write = RuntimeError(
        'relation "document_embeddings" does not exist'
    )

    outcome = await _service(embedder).embed_document(session, _document())  # type: ignore[arg-type]

    assert outcome is EmbedOutcome.FAILED
    assert session.rollbacks == 1 and session.commits == 0


async def test_failure_log_names_ids_not_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    document = _document(content_text="CONFIDENTIAL BODY")
    embedder = FakeEmbedder()
    embedder.fail = RuntimeError("boom")

    with caplog.at_level(logging.WARNING):
        await _service(embedder).embed_document(FakeSession(), document)  # type: ignore[arg-type]

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert str(document.id) in logged and str(document.organization_id) in logged
    assert "CONFIDENTIAL" not in logged


@pytest.mark.parametrize(
    "document",
    [
        _document(title="", content_text="", content_summary=None),
        _document(is_deleted=True),
    ],
    ids=["nothing-to-embed", "soft-deleted"],
)
async def test_documents_with_nothing_to_embed_are_skipped(
    document: SimpleNamespace,
) -> None:
    embedder, session = FakeEmbedder(), FakeSession()

    outcome = await _service(embedder).embed_document(session, document)  # type: ignore[arg-type]

    assert outcome is EmbedOutcome.SKIPPED
    assert not embedder.batch_requests and session.writes == []


# --- query side ----------------------------------------------------------------


async def test_query_embedding_uses_the_query_form_and_validates_length() -> None:
    embedder = FakeEmbedder()
    service = _service(embedder)

    embedded = await service.embed_text("graph networks", for_query=True)

    assert embedder.single_requests[0].provider == "cohere"
    assert not embedder.batch_requests
    assert len(embedded.vector) == DIM and embedded.model == "cohere:embed-v-4-0"


# --- text ----------------------------------------------------------------------


def test_text_is_title_then_abstract_then_the_head_of_the_text() -> None:
    document = _document(
        content_summary="A short summary.",
        content_text="x" * (TEXT_HEAD_CHARS + 500),
    )

    text = build_embedding_text(document)  # type: ignore[arg-type]

    assert text.startswith("Graph neural networks\n\nA short summary.\n\n")
    assert (
        len(text)
        == len("Graph neural networks\n\nA short summary.\n\n") + TEXT_HEAD_CHARS
    )


def test_an_abstract_the_text_already_starts_with_is_not_repeated() -> None:
    abstract = "We prove that message passing is expressive enough to decide it."
    document = _document(
        content_text=f"# Abstract\n\n{abstract}\n\n# Authors\n\nA. Author",
        document_metadata={"description": abstract[:40] + "..."},
    )

    text = build_embedding_text(document)  # type: ignore[arg-type]

    assert text.count("message passing is expressive") == 1


def test_metadata_description_stands_in_for_a_missing_summary() -> None:
    document = _document(
        content_text="body", document_metadata={"description": "From metadata."}
    )

    assert "From metadata." in build_embedding_text(document)  # type: ignore[arg-type]


# --- the real EmbeddingService, Cohere stubbed -------------------------------------


class _StubCohere:
    """Replaces ``cohere_embed_service``: enabled, records ``input_type``."""

    def __init__(self) -> None:
        self.is_enabled = True
        self.input_types: list[str] = []

    async def embed_texts(
        self, texts: list[str], input_type: str = "search_document"
    ) -> list[list[float]]:
        self.input_types.append(input_type)
        return [[0.25] * DIM for _ in texts]

    async def embed_text_single(
        self, text: str, input_type: str = "search_query"
    ) -> list[float]:
        return (await self.embed_texts([text], input_type))[0]


@pytest.fixture
def real_embedder(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _StubCohere]:
    """A real ``EmbeddingService`` whose model loading and Cohere are stubbed."""
    from src.services.embedding import cohere_embed_service as cohere_module
    from src.services.embedding.embedding_service import EmbeddingService

    stub = _StubCohere()
    monkeypatch.setattr(cohere_module, "cohere_embed_service", stub)
    monkeypatch.setattr(EmbeddingService, "_load_model", lambda self: None)
    monkeypatch.setattr(settings, "COHERE_EMBED_DIMENSIONS", DIM)
    return EmbeddingService(), stub


async def test_the_real_embedding_service_routes_documents_to_cohere(
    real_embedder: tuple[Any, _StubCohere],
) -> None:
    """Without ``provider=`` on the request EmbeddingService would answer from
    sentence-transformers (the attribute exists, so its fallback never runs)."""
    embedder, cohere = real_embedder
    session = FakeSession()

    outcome = await DocumentEmbeddingService(embedder=embedder).embed_document(
        session,  # type: ignore[arg-type]
        _document(),  # type: ignore[arg-type]
    )

    assert outcome is EmbedOutcome.EMBEDDED
    assert embedder.embedding_provider == "cohere"
    assert cohere.input_types == ["search_document"]
    params = session.writes[0].compile(dialect=postgresql.dialect()).params
    assert len(params["embedding"]) == DIM and params["model"] == "cohere:embed-v-4-0"


async def test_the_real_embedding_service_routes_queries_to_cohere(
    real_embedder: tuple[Any, _StubCohere],
) -> None:
    embedder, cohere = real_embedder

    embedded = await DocumentEmbeddingService(embedder=embedder).embed_text(
        "graph networks", for_query=True
    )

    assert len(embedded.vector) == DIM
    assert cohere.input_types == ["search_query"]

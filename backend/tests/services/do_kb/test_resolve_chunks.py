"""Tests for resolve_and_filter_chunks shared helper.

Verifies: storage-key → Document resolution, suffix matching, project-scope
filtering via CollectionDocument, and the two distinct empty-result paths
(_nodes_rag.py returns None, tools_impl.py returns empty list).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from src.services.do_kb.models import Chunk
from src.services.do_kb.resolve import resolve_and_filter_chunks


def _make_chunk(
    text: str = "some text",
    score: float = 0.9,
    document_id: str | None = "file.pdf",
    metadata: dict | None = None,
) -> Chunk:
    return Chunk(
        text=text,
        score=score,
        document_id=document_id,
        metadata=metadata or {},
    )


def _mock_session_with_docs(
    doc_rows: list[tuple],
    membership_doc_ids: list[UUID] | None = None,
) -> AsyncMock:
    """Build a mock AsyncSession.

    *doc_rows* — list of (doc_id, storage_path, title) tuples returned by
    the Document query.
    *membership_doc_ids* — if not None, the CollectionDocument query returns
    these as the in-project set.
    """
    session = AsyncMock()
    call_count = 0

    async def _execute_side_effect(stmt):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Document resolution query
            return doc_rows
        # CollectionDocument membership query
        if membership_doc_ids is not None:
            return [(did,) for did in membership_doc_ids]
        return []

    session.execute = AsyncMock(side_effect=_execute_side_effect)
    return session


# -------------------------------------------------------------------------
# Test 1: No chunks → empty result, no DB call
# -------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_chunks_returns_empty():
    """With an empty chunk list, the helper should return empty title_by_key
    and unmodified (empty) chunks_to_emit, without executing any queries."""
    session = AsyncMock()
    title_by_key, chunks_to_emit = await resolve_and_filter_chunks(
        chunks=[],
        org_id=uuid4(),
        session=session,
        project_id=None,
    )
    assert title_by_key == {}
    assert chunks_to_emit == []
    session.execute.assert_not_called()


# -------------------------------------------------------------------------
# Test 2: Exact-match resolution (storage_path == storage_key)
# -------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_exact_match_resolution():
    """When storage_path exactly matches a chunk's document_id, the doc is
    resolved and included in title_by_key."""
    doc_id = uuid4()
    chunks = [_make_chunk(document_id="report.pdf")]

    session = _mock_session_with_docs(
        doc_rows=[(doc_id, "report.pdf", "My Report")],
    )

    title_by_key, chunks_to_emit = await resolve_and_filter_chunks(
        chunks=chunks,
        org_id=uuid4(),
        session=session,
        project_id=None,
    )

    assert "report.pdf" in title_by_key
    assert title_by_key["report.pdf"] == (str(doc_id), "My Report")
    assert len(chunks_to_emit) == 1


# -------------------------------------------------------------------------
# Test 3: Suffix-match resolution (storage_path ends with /key)
# -------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_suffix_match_resolution():
    """When storage_path is a full canonical key like documents/org/file.pdf,
    but the chunk's document_id is just the leaf 'file.pdf', the suffix
    match should resolve it."""
    doc_id = uuid4()
    chunks = [_make_chunk(document_id="file.pdf")]

    # The DB returns the full canonical path; exact-match fails, but leaf
    # extraction succeeds.
    session = _mock_session_with_docs(
        doc_rows=[(doc_id, "documents/org-1/file.pdf", "Full Path Doc")],
    )

    title_by_key, chunks_to_emit = await resolve_and_filter_chunks(
        chunks=chunks,
        org_id=uuid4(),
        session=session,
        project_id=None,
    )

    assert "file.pdf" in title_by_key
    assert title_by_key["file.pdf"] == (str(doc_id), "Full Path Doc")


# -------------------------------------------------------------------------
# Test 3b: canonical-text-mirror resolution ("<doc_uuid>.txt" → Document.id)
# -------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_canonical_text_key_uuid_stem_resolution():
    """DO KB reports item_name as the basename of the canonical text mirror
    ``documents/{org}/{doc}.txt`` → ``"<doc_uuid>.txt"``. The stem IS the
    Document.id, so it resolves even though storage_path is the original
    ``.pdf`` upload key (which never suffix-matches a ``.txt`` key)."""
    doc_id = uuid4()
    chunks = [_make_chunk(document_id=f"{doc_id}.txt")]

    # storage_path is the ORIGINAL upload key — a .pdf that cannot suffix-match
    # the .txt KB key. Resolution must come from the UUID stem instead.
    session = _mock_session_with_docs(
        doc_rows=[
            (doc_id, f"documents/org-1/{doc_id}/1700000000_abc.pdf", "Self-RAG"),
        ],
    )

    title_by_key, chunks_to_emit = await resolve_and_filter_chunks(
        chunks=chunks,
        org_id=uuid4(),
        session=session,
        project_id=None,
    )

    assert f"{doc_id}.txt" in title_by_key
    assert title_by_key[f"{doc_id}.txt"] == (str(doc_id), "Self-RAG")
    assert len(chunks_to_emit) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_allowed_document_ids_drop_stronger_provider_distractor():
    target_id = UUID("11111111-1111-4111-8111-111111111111")
    distractor_id = UUID("22222222-2222-4222-8222-222222222222")
    chunks = [
        _make_chunk(
            document_id=f"{distractor_id}.txt",
            text="stronger but unrelated",
            score=0.99,
        ),
        _make_chunk(
            document_id=f"{target_id}.txt",
            text="target evidence",
            score=0.51,
        ),
    ]
    session = _mock_session_with_docs(
        doc_rows=[
            (target_id, "target.pdf", "Attention Is All You Need"),
            (distractor_id, "distractor.pdf", "Unrelated Paper"),
        ]
    )

    title_by_key, chunks_to_emit = await resolve_and_filter_chunks(
        chunks=chunks,
        org_id=uuid4(),
        session=session,
        allowed_document_ids={target_id},
    )

    assert [chunk.text for chunk in chunks_to_emit] == ["target evidence"]
    assert title_by_key[f"{target_id}.txt"] == (
        str(target_id),
        "Attention Is All You Need",
    )
    assert f"{distractor_id}.txt" not in title_by_key


# -------------------------------------------------------------------------
# Test 4: Project scoping filters out non-member docs
# -------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_project_scope_filters_chunks():
    """When project_id is set, only chunks whose resolved document belongs
    to the project should survive."""
    doc_a = uuid4()
    doc_b = uuid4()
    chunks = [
        _make_chunk(document_id="a.pdf", text="kept"),
        _make_chunk(document_id="b.pdf", text="dropped"),
    ]

    session = _mock_session_with_docs(
        doc_rows=[
            (doc_a, "a.pdf", "Doc A"),
            (doc_b, "b.pdf", "Doc B"),
        ],
        membership_doc_ids=[doc_a],  # Only doc_a in project
    )

    project_id = str(uuid4())
    title_by_key, chunks_to_emit = await resolve_and_filter_chunks(
        chunks=chunks,
        org_id=uuid4(),
        session=session,
        project_id=project_id,
    )

    assert len(chunks_to_emit) == 1
    assert chunks_to_emit[0].text == "kept"


# -------------------------------------------------------------------------
# Test 5: Unresolvable chunks under project scope → empty
# -------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_unresolvable_chunks_with_project_returns_empty():
    """When project_id is set but NO chunks resolve to known documents,
    title_by_key is empty and chunks_to_emit is empty — callers can
    detect this to trigger fallback."""
    chunks = [_make_chunk(document_id="unknown.pdf")]

    session = _mock_session_with_docs(doc_rows=[])

    project_id = str(uuid4())
    title_by_key, chunks_to_emit = await resolve_and_filter_chunks(
        chunks=chunks,
        org_id=uuid4(),
        session=session,
        project_id=project_id,
    )

    assert title_by_key == {}
    assert chunks_to_emit == []


# -------------------------------------------------------------------------
# Test 6: All chunks filtered out by project membership → empty
# -------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_chunks_filtered_returns_empty():
    """Chunks resolve to known docs but NONE belong to the active project.
    chunks_to_emit should be empty so callers can detect and trigger fallback."""
    doc_id = uuid4()
    chunks = [_make_chunk(document_id="out.pdf")]

    session = _mock_session_with_docs(
        doc_rows=[(doc_id, "out.pdf", "Outside Doc")],
        membership_doc_ids=[],  # No docs in project
    )

    project_id = str(uuid4())
    title_by_key, chunks_to_emit = await resolve_and_filter_chunks(
        chunks=chunks,
        org_id=uuid4(),
        session=session,
        project_id=project_id,
    )

    assert len(chunks_to_emit) == 0


# -------------------------------------------------------------------------
# Test 7: Invalid project_id UUID with resolvable docs → empty (security)
# -------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_project_uuid_with_resolvable_docs_returns_empty():
    """When project_id is set but not a valid UUID AND chunks resolve to known
    documents, the helper must return empty to prevent cross-project leakage."""
    doc_id = uuid4()
    chunks = [_make_chunk(document_id="secret.pdf")]

    session = _mock_session_with_docs(
        doc_rows=[(doc_id, "secret.pdf", "Secret Doc")],
    )

    title_by_key, chunks_to_emit = await resolve_and_filter_chunks(
        chunks=chunks,
        org_id=uuid4(),
        session=session,
        project_id="not-a-uuid",
    )

    assert chunks_to_emit == []

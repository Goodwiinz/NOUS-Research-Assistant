"""search_documents returns titles into a model-visible ToolMessage; titles
are user-supplied and must pass redact_pii (audit 2026-08-07, gap 1)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

MARKER = "gho_000000000000000000000000000000000000"


def _fake_doc(
    title: str,
    *,
    document_id: str = "22222222-2222-2222-2222-222222222222",
    arxiv_id: str | None = None,
    is_indexed: bool = False,
    search_vector: Any = None,
    processing_status: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=document_id,
        title=title,
        filename=f"{arxiv_id or 'document'}.pdf",
        document_type=None,
        processing_status=processing_status,
        created_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        arxiv_id=None,
        document_metadata={"arxiv_id": arxiv_id} if arxiv_id else {},
        is_indexed=is_indexed,
        is_embedded=is_indexed,
        search_vector=search_vector,
    )


async def test_search_documents_redacts_titles() -> None:
    from src.services.agent.tools_impl import _tool_search_documents

    scalars = MagicMock()
    scalars.all.return_value = [_fake_doc(f"creds {MARKER}")]
    result_proxy = MagicMock()
    result_proxy.scalars.return_value = scalars
    db = SimpleNamespace(execute=AsyncMock(return_value=result_proxy))
    current_user = SimpleNamespace(organization_id="org-1")

    result = await _tool_search_documents(
        {"query": "creds"}, db, cast(Any, current_user)
    )

    assert "error" not in result
    assert MARKER not in result["documents"][0]["title"]
    assert "<token>" in result["documents"][0]["title"]


async def test_search_documents_groups_legacy_rows_by_exact_arxiv_revision() -> None:
    from src.services.agent.tools_impl import _tool_search_documents

    ids = [
        "8f7f0000-0000-0000-0000-000000000001",
        "9bac0000-0000-0000-0000-000000000002",
        "b6e80000-0000-0000-0000-000000000003",
    ]
    docs = [
        _fake_doc(
            "Same paper",
            document_id=document_id,
            arxiv_id="2507.23334v2",
        )
        for document_id in ids
    ]
    scalars = MagicMock()
    scalars.all.return_value = docs
    result_proxy = MagicMock()
    result_proxy.scalars.return_value = scalars
    db = SimpleNamespace(execute=AsyncMock(return_value=result_proxy))
    current_user = SimpleNamespace(organization_id="org-1")

    result = await _tool_search_documents(
        {"query": "Same paper"}, db, cast(Any, current_user)
    )

    assert result["database_match_count"] == 3
    assert result["total"] == 1
    grouped = result["documents"][0]
    assert grouped["arxiv_id"] == "2507.23334v2"
    assert grouped["matching_document_ids"] == ids
    assert grouped["indexed_document_ids"] == []
    assert grouped["retrievable_document_ids"] == []
    assert grouped["duplicate_count"] == 3
    assert grouped["source_match"] == "database_title_or_filename"
    assert grouped["is_indexed"] is False
    assert grouped["retrievable"] is False
    assert grouped["source_url"] == "https://arxiv.org/abs/2507.23334v2"
    assert "not full-text retrievable" in result["warning"]


async def test_search_documents_does_not_merge_different_revisions() -> None:
    from src.services.agent.tools_impl import _tool_search_documents

    docs = [
        _fake_doc(
            "Paper v1",
            document_id="11111111-1111-1111-1111-111111111111",
            arxiv_id="2507.23334v1",
        ),
        _fake_doc(
            "Paper v2",
            document_id="22222222-2222-2222-2222-222222222222",
            arxiv_id="2507.23334v2",
        ),
    ]
    scalars = MagicMock()
    scalars.all.return_value = docs
    result_proxy = MagicMock()
    result_proxy.scalars.return_value = scalars
    db = SimpleNamespace(execute=AsyncMock(return_value=result_proxy))
    current_user = SimpleNamespace(organization_id="org-1")

    result = await _tool_search_documents(
        {"query": "Paper"}, db, cast(Any, current_user)
    )

    assert result["total"] == 2
    assert {doc["arxiv_id"] for doc in result["documents"]} == {
        "2507.23334v1",
        "2507.23334v2",
    }


async def test_search_documents_selects_the_retrievable_duplicate() -> None:
    """arXiv persistence builds search_vector without setting is_indexed."""
    from src.models.document import ProcessingStatus
    from src.services.agent.tools_impl import _tool_search_documents

    newer_id = "33333333-3333-3333-3333-333333333333"
    retrievable_id = "22222222-2222-2222-2222-222222222222"
    docs = [
        _fake_doc(
            "Same paper",
            document_id=newer_id,
            arxiv_id="2507.23334v2",
            processing_status=ProcessingStatus.COMPLETED,
        ),
        _fake_doc(
            "Same paper",
            document_id=retrievable_id,
            arxiv_id="2507.23334v2",
            processing_status=ProcessingStatus.COMPLETED,
            search_vector=object(),
            is_indexed=False,
        ),
    ]
    scalars = MagicMock()
    scalars.all.return_value = docs
    result_proxy = MagicMock()
    result_proxy.scalars.return_value = scalars
    db = SimpleNamespace(execute=AsyncMock(return_value=result_proxy))
    current_user = SimpleNamespace(organization_id="org-1")

    result = await _tool_search_documents(
        {"query": "Same paper"}, db, cast(Any, current_user)
    )

    grouped = result["documents"][0]
    assert grouped["matching_document_ids"] == [newer_id, retrievable_id]
    assert grouped["id"] == retrievable_id
    assert grouped["retrievable_document_ids"] == [retrievable_id]
    assert grouped["retrievable"] is True
    assert grouped["is_indexed"] is False

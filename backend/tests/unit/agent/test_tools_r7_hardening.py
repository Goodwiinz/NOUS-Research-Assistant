"""Audit R7-B — tool-surface hardening.

R7-M4  every tool result is size-bounded at the ``execute_tool`` choke point.
R7-L8  the title fallback in ``_resolve_document_id`` never guesses.
R7-L9  a DESTRUCTIVE tool never takes the CONTEXT_FREE fast path.
R7-L10 ``_tool_execute_code`` fails closed without a thread_id.
R7-L11 arXiv ids are validated before they reach the PDF URL builder.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.agent import tools_impl
from src.services.agent.tools_impl import _MAX_TOOL_RESULT_BYTES, execute_tool

# ---------------------------------------------------------------------------
# R7-M4 — result size cap
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_huge_tool_result_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_dispatch(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {
            "status": "success",
            "stdout": "A" * (50 * 1024 * 1024),
            "stderr": "B" * 4096,
            "exit_code": 0,
        }

    monkeypatch.setattr(tools_impl, "_dispatch_tool", _fake_dispatch)

    result = await execute_tool(
        "execute_code", {}, db=MagicMock(), current_user=MagicMock()
    )

    assert result["truncated"] is True
    assert len(json.dumps(result, default=str)) <= _MAX_TOOL_RESULT_BYTES
    assert "truncated" in result["stdout"]
    assert result["exit_code"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_small_tool_result_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"status": "success", "stdout": "hello", "exit_code": 0}

    async def _fake_dispatch(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return dict(payload)

    monkeypatch.setattr(tools_impl, "_dispatch_tool", _fake_dispatch)

    result = await execute_tool(
        "execute_code", {}, db=MagicMock(), current_user=MagicMock()
    )

    assert result == payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_many_medium_fields_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_dispatch(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {
            "results": [{"title": "T" * 5000, "url": "u" * 5000} for _ in range(20)]
        }

    monkeypatch.setattr(tools_impl, "_dispatch_tool", _fake_dispatch)

    result = await execute_tool(
        "search_external_database", {}, db=MagicMock(), current_user=MagicMock()
    )

    assert result["truncated"] is True
    assert len(json.dumps(result, default=str)) <= _MAX_TOOL_RESULT_BYTES
    assert len(result["results"]) == 20


# ---------------------------------------------------------------------------
# R7-L9 — destructive tools are never CONTEXT_FREE
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_tool_is_both_context_free_and_destructive() -> None:
    from src.services.agent.tool_registry import ToolPolicyTag
    from src.services.agent.tools import TOOL_REGISTRY

    offenders = [
        d.name
        for d in TOOL_REGISTRY.descriptors
        if ToolPolicyTag.CONTEXT_FREE in d.policy_tags
        and ToolPolicyTag.DESTRUCTIVE in d.policy_tags
    ]
    assert offenders == [], offenders


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forget_memory_requires_a_resolved_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _boom(**kwargs: Any) -> Dict[str, Any]:
        nonlocal called
        called = True
        return {"status": "completed"}

    monkeypatch.setattr(tools_impl, "_tool_forget_memory", _boom)

    result = await tools_impl._dispatch_tool(
        "forget_memory",
        {"query": "everything"},
        user_id="00000000-0000-0000-0000-000000000001",
        db=MagicMock(),
        current_user=None,
    )

    assert "error" in result
    assert called is False


# ---------------------------------------------------------------------------
# R7-L10 — execute_code needs a thread id
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_code_rejects_empty_thread_id() -> None:
    result = await tools_impl._tool_execute_code(
        {"code": "print(1)"}, thread_id="", current_user=MagicMock()
    )
    assert "error" in result
    assert "thread" in result["error"].lower()


# ---------------------------------------------------------------------------
# R7-L11 — arXiv id grammar
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "paper_id",
    ["2401.12345", "2401.1234", "2303.15563v2", "math/0309136", "math.GT/0309136v1"],
)
def test_valid_arxiv_ids_accepted(paper_id: str) -> None:
    from src.services.agent.tools import _reject_invalid_arxiv_ids

    assert _reject_invalid_arxiv_ids([paper_id]) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "paper_id",
    [
        "../../robots.txt?x=",
        "2401.12345/../../etc/passwd",
        "https://evil.example/x.pdf",
        "",
        "24.1",
        "2401.12345 2401.12346",
    ],
)
def test_invalid_arxiv_ids_rejected(paper_id: str) -> None:
    from src.services.agent.tools import _reject_invalid_arxiv_ids

    rejected = _reject_invalid_arxiv_ids([paper_id])
    assert rejected is not None and "error" in rejected


def _streaming_service(tmp_path: Path, chunks: List[bytes]) -> Any:
    """An ArXivIngestionService whose response body arrives in *chunks*."""
    from src.services.arxiv import arxiv_service as svc

    class _Content:
        async def iter_chunked(self, n: int) -> AsyncIterator[bytes]:
            for chunk in chunks:
                yield chunk

    class _Response:
        content = _Content()

        def raise_for_status(self) -> None:
            return None

        async def __aenter__(self) -> "_Response":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    session = MagicMock()
    session.get = MagicMock(return_value=_Response())

    service: Any = svc.ArXivIngestionService.__new__(svc.ArXivIngestionService)
    service.session = session
    service.download_dir = tmp_path
    service.MAX_RETRIES = 1
    return service


@pytest.mark.unit
@pytest.mark.asyncio
async def test_arxiv_pdf_download_drains_every_chunk(tmp_path: Path) -> None:
    # A single .read(n) on a streamed response yields only the bytes that have
    # arrived so far, so the cache used to end up holding a partial PDF.
    chunks = [b"%PDF-1.4\n", b"body-" * 100, b"%%EOF\n"]
    service = _streaming_service(tmp_path, chunks)

    content = await service.download_paper_pdf("2401.12345")

    assert content == b"".join(chunks)
    assert (tmp_path / "2401.12345.pdf").read_bytes() == b"".join(chunks)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_arxiv_pdf_download_caps_read_size(tmp_path: Path) -> None:
    from src.services.arxiv import arxiv_service as svc

    chunk = b"x" * (64 * 1024)
    over = (svc._MAX_PDF_BYTES // len(chunk)) + 2
    service = _streaming_service(tmp_path, [chunk] * over)

    with pytest.raises(svc.IngestionError):
        await service.download_paper_pdf("2401.12345")
    assert not (tmp_path / "2401.12345.pdf").exists()


# ---------------------------------------------------------------------------
# R7-L8 — title fallback must not guess
# ---------------------------------------------------------------------------


def _db_returning(docs: List[Any]) -> Any:
    """A db whose title query returns *docs*, recording the SQL it was given."""
    db = MagicMock()
    db.title_sql = ""

    async def _execute(stmt: Any) -> Any:
        db.title_sql = str(stmt)
        result = MagicMock()
        result.scalars.return_value.all.return_value = docs
        result.scalar_one_or_none.return_value = None
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return db


def _doc(title: str) -> Any:
    doc = MagicMock()
    doc.title = title
    return doc


@pytest.mark.unit
@pytest.mark.asyncio
async def test_title_fallback_exact_single_match_resolves() -> None:
    from src.services.agent.tool_helpers import _resolve_document_id

    doc = _doc("Attention Is All You Need")
    user = MagicMock()
    user.organization_id = "org-1"
    db = _db_returning([doc])

    got = await _resolve_document_id("attention is all you need", db, user)
    assert got is doc
    assert "lower(documents.title) =" in db.title_sql.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_title_fallback_ambiguous_match_returns_none() -> None:
    from src.services.agent.tool_helpers import _resolve_document_id

    user = MagicMock()
    user.organization_id = "org-1"
    docs = [_doc("Report 2024"), _doc("Report 2024")]

    assert await _resolve_document_id("Report 2024", _db_returning(docs), user) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_title_fallback_no_exact_match_returns_none() -> None:
    # "transformers" is only a substring of any real title, so the exact query
    # matches nothing and the caller gets a clean miss.
    from src.services.agent.tool_helpers import _resolve_document_id

    user = MagicMock()
    user.organization_id = "org-1"

    assert await _resolve_document_id("transformers", _db_returning([]), user) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_title_fallback_exact_match_not_shadowed_by_longer_title() -> None:
    # Audit follow-up: the old substring query returned both "Report" and
    # "Annual Report", and the "exactly one row" rule then rejected the
    # unambiguous exact match. An equality query never sees "Annual Report".
    from src.services.agent.tool_helpers import _resolve_document_id

    user = MagicMock()
    user.organization_id = "org-1"
    doc = _doc("Report")
    db = _db_returning([doc])

    got = await _resolve_document_id("Report", db, user)

    assert got is doc
    sql = db.title_sql.lower()
    assert "lower(documents.title) =" in sql
    assert "documents.title ilike" not in sql


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_arxiv_impl_rejects_bad_id_before_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Production dispatch is _nodes_tools -> execute_tool -> _tool_ingest_arxiv
    # and never runs the LangChain wrapper where the check used to live.
    import src.services.arxiv.arxiv_service as arxiv_service

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("arXiv service must not be reached")

    monkeypatch.setattr(arxiv_service, "ArXivIngestionService", _boom)

    result = await tools_impl._tool_ingest_arxiv(
        {"paper_ids": ["../../robots.txt?x="]},
        user_id="u-1",
        db=MagicMock(),
        current_user=MagicMock(),
    )

    assert "error" in result
    assert "arxiv paper id" in result["error"].lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bytes_leaf_is_capped_not_just_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # execute_code can return a multi-MB PNG; the oversize is then dominated by
    # a non-string leaf, which string truncation alone never touches.
    async def _fake_dispatch(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "success", "png": b"\x89PNG" * (5 * 1024 * 1024 // 4)}

    monkeypatch.setattr(tools_impl, "_dispatch_tool", _fake_dispatch)

    result = await execute_tool(
        "execute_code", {}, db=MagicMock(), current_user=MagicMock()
    )

    assert result["truncated"] is True
    assert len(json.dumps(result, default=str)) <= _MAX_TOOL_RESULT_BYTES
    assert result["status"] == "success"


@pytest.mark.unit
def test_cap_tool_result_always_fits_the_cap() -> None:
    # A long tail of small leaves: no single string is oversized, so the
    # water-fill leaves the payload over the cap on its own.
    payload: Dict[str, Any] = {
        f"k{i}": {"n": i, "blob": b"z" * 4096} for i in range(200)
    }

    capped = tools_impl._cap_tool_result(payload)

    assert capped["truncated"] is True
    assert len(json.dumps(capped, default=str)) <= _MAX_TOOL_RESULT_BYTES


@pytest.mark.unit
def test_cap_log_joins_and_caps_list_output() -> None:
    # e2b-code-interpreter 2.7.0 returns logs.stdout as List[str]; the old
    # isinstance(str) guard let the list through the cap and into a str field.
    from src.services.sandbox.e2b_sandbox_manager import MAX_LOG_CHARS, _cap_log

    assert _cap_log(["a\n", "b\n"]) == "a\nb\n"

    capped = _cap_log(["x" * 1024] * 32)
    assert isinstance(capped, str)
    assert len(capped) < MAX_LOG_CHARS + 64
    assert "truncated" in capped

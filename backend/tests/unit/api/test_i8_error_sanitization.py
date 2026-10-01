"""Audit I8 regression guards: no raw exception text in client-facing details.

Two layers:

1. Behavior tests — for each audit-named endpoint family (realtime document
   status, thread/message search, health readiness) a mocked dependency raises
   ``RuntimeError(LEAK_MARKER)`` and the response must keep the original
   status code, return the generic message, and never echo the secret.

2. Source tripwire — scans ``backend/src/api`` and ``backend/src/health`` for
   ``detail=...`` / ``"detail": ...`` constructions that interpolate ``str(e)``,
   ``str(exc)``, ``{e}``, or ``{exc}`` into a client-facing payload. Log-only
   uses of exception text are fine and must not trip the scan.
"""

import ast
import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.threads import thread_search as thread_search_module
from src.health import endpoints as ep

pytestmark = pytest.mark.unit

LEAK_MARKER = "i8-leak-marker-internal"


def _raising_async(exc: Exception) -> AsyncMock:
    mock = AsyncMock(side_effect=exc)
    return mock


# ---------------------------------------------------------------------------
# Layer 2: source tripwire (must hold for the whole api/ + health/ trees)
# ---------------------------------------------------------------------------

BACKEND_ROOT = Path(__file__).resolve().parents[3]
SCAN_DIRS = [BACKEND_ROOT / "src" / "api", BACKEND_ROOT / "src" / "health"]

# detail=<string literal> / detail=str(e) / ("detail": <literal or str(e)>)
# Kwarg forms require call context ("(" or "," before detail=) so internal
# variables that merely happen to be named `detail` (e.g. `detail = str(e)`
# used for message sniffing) do not false-positive.
_KW = r"(?:\(|,)\s*detail\s*=\s*"
LEAK_PATTERNS = [
    # direct str() call:  detail=str(e) / detail=str(exc) / detail=( str(e) )
    re.compile(_KW + r"\(?\s*str\(\s*(?:e|exc)\s*\)"),
    # f-string interpolation:  detail=f"... {str(e)}" / "... {e}" / "... {exc}"
    re.compile(_KW + r'\(?\s*f?"[^"]*?(?:str\(e\)|str\(exc\)|\{e\}|\{exc\})'),
    # dict content (JSONResponse etc.):  "detail": str(e) / "detail": f"...{e}"
    re.compile(
        r"[\"']detail[\"']\s*:\s*\(?\s*"
        r'(?:str\(\s*(?:e|exc)\s*\)|f?"[^"]*?(?:str\(e\)|str\(exc\)|\{e\}|\{exc\}))'
    ),
]


def _iter_python_files() -> Iterator[Path]:
    for directory in SCAN_DIRS:
        yield from sorted(directory.rglob("*.py"))


_EXC_NAMES = {"e", "exc"}


def _embeds_exception_text(node: ast.AST) -> bool:
    """True if ``node`` contains ``str(e)``/``str(exc)`` or an f-string ``{e}``."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "str"
            and any(isinstance(a, ast.Name) and a.id in _EXC_NAMES for a in sub.args)
        ):
            return True
        if (
            isinstance(sub, ast.FormattedValue)
            and isinstance(sub.value, ast.Name)
            and sub.value.id in _EXC_NAMES
        ):
            return True
    return False


def _scan_source(text: str) -> list[int]:
    """Return 1-based line numbers of leaking detail constructions in ``text``."""
    lines = set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if any(pattern.search(code) for pattern in LEAK_PATTERNS):
            lines.add(line_no)
    # The regexes are line-based; the AST catches multiline calls and dictionaries
    # even when the detail key and exception-bearing value are on separate lines.
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "detail" and _embeds_exception_text(kw.value):
                    lines.add(kw.value.lineno)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "detail"
                    and _embeds_exception_text(value)
                ):
                    lines.add(value.lineno)
    return sorted(lines)


def _scan_hits() -> list[str]:
    hits = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        source_lines = text.splitlines()
        for line_no in _scan_source(text):
            hits.append(
                f"{path.relative_to(BACKEND_ROOT)}:{line_no}: "
                f"{source_lines[line_no - 1].strip()}"
            )
    return hits


_MULTILINE_LEAK = """
try:
    pass
except Exception as exc:
    raise HTTPException(
        status_code=500,
        detail=str(exc),
    )
"""

_MULTILINE_FSTRING_LEAK = """
try:
    pass
except Exception as e:
    raise HTTPException(
        status_code=500,
        detail=f"failed: {e}",
    )
"""

_LOCAL_DETAIL_VARIABLE = """
try:
    pass
except Exception as e:
    detail = str(e)
    if "duplicate" in detail:
        raise HTTPException(status_code=409, detail="Already exists")
"""


def test_tripwire_catches_multiline_detail_kwargs() -> None:
    # Codex P2 on #1733: a kwarg on its own line must still be detected.
    assert _scan_source(_MULTILINE_LEAK) == [7]
    assert _scan_source(_MULTILINE_FSTRING_LEAK) == [7]


def test_tripwire_ignores_local_detail_variable() -> None:
    assert _scan_source(_LOCAL_DETAIL_VARIABLE) == []


@pytest.mark.parametrize("value", ["str(exc)", 'f"failed: {exc}"'])
def test_tripwire_catches_multiline_detail_dict_values(value: str) -> None:
    text = f"""
try:
    pass
except Exception as exc:
    return JSONResponse(content={{
        "detail":
            {value},
    }})
"""
    assert _scan_source(text) == [7]


def test_tripwire_ignores_commented_client_details() -> None:
    text = """
# return JSONResponse(content={"detail": str(exc)})
# raise HTTPException(status_code=500, detail=f"failed: {e}")
pass
"""
    assert _scan_source(text) == []


def test_no_raw_exception_text_in_client_details() -> None:
    hits = _scan_hits()
    assert not hits, (
        "client-facing detail= values must not embed raw exception text "
        f"({len(hits)} sites):\n" + "\n".join(hits)
    )


# ---------------------------------------------------------------------------
# Layer 1: realtime document-status family
# ---------------------------------------------------------------------------


def _rt_deps() -> tuple[MagicMock, MagicMock, MagicMock]:
    user = MagicMock(id="00000000-0000-0000-0000-000000000001")
    org = MagicMock(id="00000000-0000-0000-0000-000000000002")
    session = MagicMock()
    session.execute = _raising_async(RuntimeError(LEAK_MARKER))
    return user, org, session


async def test_realtime_subscribe_leak_free() -> None:
    from src.api.realtime import realtime_document_status as rds

    user, org, session = _rt_deps()
    subscription = rds.RealtimeStatusSubscription(document_id="doc-1")

    with pytest.raises(HTTPException) as exc:
        await rds.subscribe_document_status_updates(
            subscription=subscription,
            current_user=user,
            organization=org,
            session=session,
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to subscribe to document updates"
    assert LEAK_MARKER not in str(exc.value.detail)


async def test_realtime_get_status_leak_free() -> None:
    from src.api.realtime import realtime_document_status as rds

    user, org, session = _rt_deps()

    with pytest.raises(HTTPException) as exc:
        await rds.get_realtime_document_status(
            document_id="doc-1",
            current_user=user,
            organization=org,
            session=session,
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to get document status"
    assert LEAK_MARKER not in str(exc.value.detail)


async def test_realtime_bulk_status_leak_free() -> None:
    from src.api.realtime import realtime_document_status as rds

    user, org, session = _rt_deps()
    request = rds.BulkStatusRequest(document_ids=["doc-1"])

    with pytest.raises(HTTPException) as exc:
        await rds.get_bulk_realtime_status(
            request=request, current_user=user, organization=org, session=session
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to get bulk status"
    assert LEAK_MARKER not in str(exc.value.detail)


async def test_realtime_system_metrics_leak_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.realtime import realtime_document_status as rds

    user, org, _ = _rt_deps()
    monkeypatch.setattr(
        rds.status_update_service,
        "get_system_status",
        _raising_async(RuntimeError(LEAK_MARKER)),
    )

    with pytest.raises(HTTPException) as exc:
        await rds.get_realtime_system_metrics(
            current_user=user, organization=org, session=MagicMock()
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to get system metrics"
    assert LEAK_MARKER not in str(exc.value.detail)


async def test_realtime_broadcast_leak_free() -> None:
    from src.api.realtime import realtime_document_status as rds

    user, org, session = _rt_deps()

    with pytest.raises(HTTPException) as exc:
        await rds.trigger_document_status_broadcast(
            document_id="doc-1", current_user=user, organization=org, session=session
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to trigger status broadcast"
    assert LEAK_MARKER not in str(exc.value.detail)


async def test_realtime_connection_status_leak_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.realtime import realtime_document_status as rds

    user, _, _ = _rt_deps()
    monkeypatch.setattr(
        rds.connection_manager,
        "get_user_connections",
        MagicMock(side_effect=RuntimeError(LEAK_MARKER)),
    )

    with pytest.raises(HTTPException) as exc:
        await rds.get_realtime_connection_status(current_user=user)

    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to get connection status"
    assert LEAK_MARKER not in str(exc.value.detail)


# ---------------------------------------------------------------------------
# Layer 1: thread / message search family
# ---------------------------------------------------------------------------


def _search_deps() -> tuple[MagicMock, MagicMock]:
    user = MagicMock(id="00000000-0000-0000-0000-000000000001")
    db = MagicMock()
    return user, db


def _patch_service(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    monkeypatch.setattr(
        thread_search_module.thread_message_search_service,
        method,
        MagicMock(side_effect=RuntimeError(LEAK_MARKER)),
    )


def test_thread_search_post_leak_free(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.services.threads.thread_message_search_service import ThreadSearchRequest

    user, db = _search_deps()
    _patch_service(monkeypatch, "search_threads")

    with pytest.raises(HTTPException) as exc:
        thread_search_module.search_threads(
            request=ThreadSearchRequest(query="x"), current_user=user, db=db
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Search failed"
    assert LEAK_MARKER not in str(exc.value.detail)


def test_thread_search_get_leak_free(monkeypatch: pytest.MonkeyPatch) -> None:
    user, db = _search_deps()
    _patch_service(monkeypatch, "search_threads")

    with pytest.raises(HTTPException) as exc:
        thread_search_module.search_threads_get(
            query="x",
            conversation_id=None,
            workspace_id=None,
            status_filter=None,
            date_from=None,
            date_to=None,
            min_message_count=None,
            sort_order="relevance",
            limit=20,
            offset=0,
            current_user=user,
            db=db,
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Search failed"
    assert LEAK_MARKER not in str(exc.value.detail)


def test_message_search_post_leak_free(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.services.threads.thread_message_search_service import MessageSearchRequest

    user, db = _search_deps()
    _patch_service(monkeypatch, "search_messages")

    with pytest.raises(HTTPException) as exc:
        thread_search_module.search_messages(
            request=MessageSearchRequest(query="x"), current_user=user, db=db
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Search failed"
    assert LEAK_MARKER not in str(exc.value.detail)


def test_message_search_get_leak_free(monkeypatch: pytest.MonkeyPatch) -> None:
    user, db = _search_deps()
    _patch_service(monkeypatch, "search_messages")

    with pytest.raises(HTTPException) as exc:
        thread_search_module.search_messages_get(
            query="x",
            thread_id=None,
            conversation_id=None,
            workspace_id=None,
            user_id=None,
            roles=None,
            date_from=None,
            date_to=None,
            has_citations=None,
            sort_order="relevance",
            limit=20,
            offset=0,
            current_user=user,
            db=db,
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Search failed"
    assert LEAK_MARKER not in str(exc.value.detail)


def test_combined_search_leak_free(monkeypatch: pytest.MonkeyPatch) -> None:
    user, db = _search_deps()
    _patch_service(monkeypatch, "combined_search")

    with pytest.raises(HTTPException) as exc:
        thread_search_module.combined_search(query="x", current_user=user, db=db)

    assert exc.value.status_code == 500
    assert exc.value.detail == "Search failed"
    assert LEAK_MARKER not in str(exc.value.detail)


# ---------------------------------------------------------------------------
# Layer 1: health readiness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_readiness_state() -> Iterator[None]:
    ep._readiness_cached_result = None
    ep._readiness_last_check_time = 0.0
    ep._readiness_lock = None
    ep._llm_config_ready = True
    yield
    ep._readiness_cached_result = None
    ep._readiness_last_check_time = 0.0
    ep._readiness_lock = None
    ep._llm_config_ready = True


async def test_readiness_leak_free(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RaisingChecker:
        async def check_database(self) -> None:
            raise RuntimeError(LEAK_MARKER)

        async def check_redis(self) -> None:  # pragma: no cover - never reached
            raise AssertionError("redis should not be reached")

    monkeypatch.setattr(ep, "get_health_checker", lambda: _RaisingChecker())

    with pytest.raises(HTTPException) as exc:
        await ep.readiness_probe()

    assert exc.value.status_code == 503
    assert exc.value.detail == "Readiness check failed"
    assert LEAK_MARKER not in str(exc.value.detail)


# ---------------------------------------------------------------------------
# Layer 1: JSON error fields in evidence health and local ArXiv extraction
# ---------------------------------------------------------------------------


def test_evidence_health_json_error_leak_free(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from src.api.evidence.router import cache_service, router
    from src.core.dependencies import get_current_user

    monkeypatch.setattr(
        cache_service,
        "_ensure_connected",
        MagicMock(side_effect=RuntimeError(LEAK_MARKER)),
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/evidence")
    app.dependency_overrides[get_current_user] = lambda: MagicMock()

    with TestClient(app) as client:
        response = client.get("/api/v1/evidence/health")

    assert response.status_code == 200
    assert response.json() == {"status": "unhealthy", "error": "Health check failed"}
    assert LEAK_MARKER not in response.text
    assert any(
        record.exc_info and str(record.exc_info[1]) == LEAK_MARKER
        for record in caplog.records
    )


def test_arxiv_extraction_json_error_leak_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.api.arxiv import arxiv_local
    from src.core.dependencies import get_current_user

    (tmp_path / "paper.pdf").write_bytes(b"unused PDF content")
    monkeypatch.setattr(arxiv_local, "ARXIV_DATA_PATH", tmp_path)
    monkeypatch.setattr(
        arxiv_local,
        "_extract_topics_from_filename",
        _raising_async(RuntimeError(LEAK_MARKER)),
    )
    app = FastAPI()
    app.include_router(arxiv_local.router, prefix="/api/v1/arxiv/local")
    app.dependency_overrides[get_current_user] = lambda: MagicMock()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/arxiv/local/extract-local-features",
            json={
                "process_full_content": False,
                "extract_topics": True,
                "extract_keyphrases": False,
                "extract_summaries": False,
                "update_knowledge_graph": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["total_files_found"] == 1
    assert payload["processed_count"] == 0
    assert payload["results"] == [
        {
            "paper_id": "paper",
            "filename": "paper.pdf",
            "extraction_status": "failed",
            "error": "Failed to process PDF",
        }
    ]
    assert LEAK_MARKER not in response.text
    assert any(
        record.exc_info and str(record.exc_info[1]) == LEAK_MARKER
        for record in caplog.records
    )

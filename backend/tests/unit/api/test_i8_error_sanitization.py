"""Audit I8 regression guards: no raw exception text in client-facing details.

Two layers:

1. Behavior tests — for each audit-named endpoint family (realtime document
   status, thread/message search, health readiness) a mocked dependency raises
   ``RuntimeError("S3CR3T-internal")`` and the response must keep the original
   status code, return the generic message, and never echo the secret.

2. Source tripwire — scans ``backend/src/api`` and ``backend/src/health`` for
   ``detail=...`` / ``"detail": ...`` constructions that interpolate ``str(e)``,
   ``str(exc)``, ``{e}``, or ``{exc}`` into a client-facing payload. Log-only
   uses of exception text are fine and must not trip the scan.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.api.threads import thread_search as thread_search_module
from src.health import endpoints as ep

pytestmark = pytest.mark.unit

SECRET = "S3CR3T-internal"


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


def _iter_python_files():
    for directory in SCAN_DIRS:
        yield from sorted(directory.rglob("*.py"))


def _scan_hits():
    hits = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern in LEAK_PATTERNS:
                if pattern.search(line):
                    hits.append(f"{path.relative_to(BACKEND_ROOT)}:{line_no}: {line.strip()}")
                    break
    return hits


def test_no_raw_exception_text_in_client_details():
    hits = _scan_hits()
    assert not hits, (
        "client-facing detail= values must not embed raw exception text "
        f"({len(hits)} sites):\n" + "\n".join(hits)
    )


# ---------------------------------------------------------------------------
# Layer 1: realtime document-status family
# ---------------------------------------------------------------------------


def _rt_deps():
    user = MagicMock(id="00000000-0000-0000-0000-000000000001")
    org = MagicMock(id="00000000-0000-0000-0000-000000000002")
    session = MagicMock()
    session.execute = _raising_async(RuntimeError(SECRET))
    return user, org, session


async def test_realtime_subscribe_leak_free():
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
    assert SECRET not in str(exc.value.detail)


async def test_realtime_get_status_leak_free():
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
    assert SECRET not in str(exc.value.detail)


async def test_realtime_bulk_status_leak_free():
    from src.api.realtime import realtime_document_status as rds

    user, org, session = _rt_deps()
    request = rds.BulkStatusRequest(document_ids=["doc-1"])

    with pytest.raises(HTTPException) as exc:
        await rds.get_bulk_realtime_status(
            request=request, current_user=user, organization=org, session=session
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to get bulk status"
    assert SECRET not in str(exc.value.detail)


async def test_realtime_system_metrics_leak_free(monkeypatch):
    from src.api.realtime import realtime_document_status as rds

    user, org, _ = _rt_deps()
    monkeypatch.setattr(
        rds.status_update_service,
        "get_system_status",
        _raising_async(RuntimeError(SECRET)),
    )

    with pytest.raises(HTTPException) as exc:
        await rds.get_realtime_system_metrics(
            current_user=user, organization=org, session=MagicMock()
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to get system metrics"
    assert SECRET not in str(exc.value.detail)


async def test_realtime_broadcast_leak_free():
    from src.api.realtime import realtime_document_status as rds

    user, org, session = _rt_deps()

    with pytest.raises(HTTPException) as exc:
        await rds.trigger_document_status_broadcast(
            document_id="doc-1", current_user=user, organization=org, session=session
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to trigger status broadcast"
    assert SECRET not in str(exc.value.detail)


async def test_realtime_connection_status_leak_free(monkeypatch):
    from src.api.realtime import realtime_document_status as rds

    user, _, _ = _rt_deps()
    monkeypatch.setattr(
        rds.connection_manager,
        "get_user_connections",
        MagicMock(side_effect=RuntimeError(SECRET)),
    )

    with pytest.raises(HTTPException) as exc:
        await rds.get_realtime_connection_status(current_user=user)

    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to get connection status"
    assert SECRET not in str(exc.value.detail)


# ---------------------------------------------------------------------------
# Layer 1: thread / message search family
# ---------------------------------------------------------------------------


def _search_deps():
    user = MagicMock(id="00000000-0000-0000-0000-000000000001")
    db = MagicMock()
    return user, db


def _patch_service(monkeypatch, method: str) -> None:
    monkeypatch.setattr(
        thread_search_module.thread_message_search_service,
        method,
        MagicMock(side_effect=RuntimeError(SECRET)),
    )


def test_thread_search_post_leak_free(monkeypatch):
    from src.services.threads.thread_message_search_service import ThreadSearchRequest

    user, db = _search_deps()
    _patch_service(monkeypatch, "search_threads")

    with pytest.raises(HTTPException) as exc:
        thread_search_module.search_threads(
            request=ThreadSearchRequest(query="x"), current_user=user, db=db
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Search failed"
    assert SECRET not in str(exc.value.detail)


def test_thread_search_get_leak_free(monkeypatch):
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
    assert SECRET not in str(exc.value.detail)


def test_message_search_post_leak_free(monkeypatch):
    from src.services.threads.thread_message_search_service import MessageSearchRequest

    user, db = _search_deps()
    _patch_service(monkeypatch, "search_messages")

    with pytest.raises(HTTPException) as exc:
        thread_search_module.search_messages(
            request=MessageSearchRequest(query="x"), current_user=user, db=db
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Search failed"
    assert SECRET not in str(exc.value.detail)


def test_message_search_get_leak_free(monkeypatch):
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
    assert SECRET not in str(exc.value.detail)


def test_combined_search_leak_free(monkeypatch):
    user, db = _search_deps()
    _patch_service(monkeypatch, "combined_search")

    with pytest.raises(HTTPException) as exc:
        thread_search_module.combined_search(query="x", current_user=user, db=db)

    assert exc.value.status_code == 500
    assert exc.value.detail == "Search failed"
    assert SECRET not in str(exc.value.detail)


# ---------------------------------------------------------------------------
# Layer 1: health readiness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_readiness_state():
    ep._readiness_cached_result = None
    ep._readiness_last_check_time = 0.0
    ep._readiness_lock = None
    ep._llm_config_ready = True
    yield
    ep._readiness_cached_result = None
    ep._readiness_last_check_time = 0.0
    ep._readiness_lock = None
    ep._llm_config_ready = True


async def test_readiness_leak_free(monkeypatch):
    class _RaisingChecker:
        async def check_database(self):
            raise RuntimeError(SECRET)

        async def check_redis(self):  # pragma: no cover - never reached
            raise AssertionError("redis should not be reached")

    monkeypatch.setattr(ep, "get_health_checker", lambda: _RaisingChecker())

    with pytest.raises(HTTPException) as exc:
        await ep.readiness_probe()

    assert exc.value.status_code == 503
    assert exc.value.detail == "Readiness check failed"
    assert SECRET not in str(exc.value.detail)

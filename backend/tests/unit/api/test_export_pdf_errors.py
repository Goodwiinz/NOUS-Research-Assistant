"""Route-level contract tests for safe PDF export failures."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.api.research import export as export_api
from src.models.user import User
from src.services.research.export_service import ExportService, PDFExportConversionError
from src.shared.export_schemas import BatchExportRequest, ExportFormat, ExportOptions

pytestmark = pytest.mark.unit


def _thread_export() -> MagicMock:
    thread = MagicMock()
    thread.title = "Export test"
    thread.id = "thread-1"
    return thread


def _current_user() -> User:
    # These route tests exercise only current_user.id; no database-backed user
    # behavior is needed for the exporter error contract.
    return cast(User, SimpleNamespace(id="user-1"))


def _service_with_missing_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> ExportService:
    monkeypatch.setitem(sys.modules, "weasyprint", None)

    service = ExportService(AsyncMock())
    monkeypatch.setattr(
        service, "_load_thread", AsyncMock(return_value=_thread_export())
    )
    return service


@pytest.mark.asyncio
async def test_single_pdf_renderer_unavailable_is_a_safe_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_missing_renderer(monkeypatch)
    monkeypatch.setattr(export_api, "get_export_service", lambda _db: service)

    with pytest.raises(HTTPException) as raised:
        await export_api.export_thread(
            thread_id="thread-1",
            format=ExportFormat.PDF,
            include_system_messages=False,
            include_citations=True,
            include_metadata=True,
            include_feedback=False,
            db=AsyncMock(),
            current_user=_current_user(),
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == "PDF export unavailable"


@pytest.mark.asyncio
async def test_stream_pdf_renderer_unavailable_is_a_safe_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_missing_renderer(monkeypatch)
    monkeypatch.setattr(export_api, "get_export_service", lambda _db: service)

    with pytest.raises(HTTPException) as raised:
        await export_api.export_thread_stream(
            thread_id="thread-1",
            format=ExportFormat.PDF,
            include_citations=True,
            db=AsyncMock(),
            current_user=_current_user(),
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == "PDF export unavailable"


@pytest.mark.asyncio
async def test_batch_pdf_renderer_unavailable_is_a_safe_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_missing_renderer(monkeypatch)
    monkeypatch.setattr(export_api, "get_export_service", lambda _db: service)

    with pytest.raises(HTTPException) as raised:
        await export_api.export_batch(
            request=BatchExportRequest(
                thread_ids=["thread-1"],
                format=ExportFormat.PDF,
                options=ExportOptions(),
                as_zip=True,
            ),
            db=AsyncMock(),
            current_user=_current_user(),
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == "PDF export unavailable"


@pytest.mark.asyncio
async def test_single_pdf_success_uses_server_filename_and_mime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MagicMock()
    service.export_thread = AsyncMock(
        return_value=(b"%PDF-server", "server-title_thread-1.pdf", "application/pdf")
    )
    monkeypatch.setattr(export_api, "get_export_service", lambda _db: service)

    response = await export_api.export_thread(
        thread_id="thread-1",
        format=ExportFormat.PDF,
        include_system_messages=False,
        include_citations=True,
        include_metadata=True,
        include_feedback=False,
        db=AsyncMock(),
        current_user=_current_user(),
    )

    assert response.body.startswith(b"%PDF-")
    assert response.media_type == "application/pdf"
    assert response.headers["content-disposition"].endswith(
        'filename="server-title_thread-1.pdf"'
    )


@pytest.mark.asyncio
async def test_single_pdf_conversion_failure_is_safe_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MagicMock()
    service.export_thread = AsyncMock(side_effect=PDFExportConversionError())
    monkeypatch.setattr(export_api, "get_export_service", lambda _db: service)

    with pytest.raises(HTTPException) as raised:
        await export_api.export_thread(
            thread_id="thread-1",
            format=ExportFormat.PDF,
            include_system_messages=False,
            include_citations=True,
            include_metadata=True,
            include_feedback=False,
            db=AsyncMock(),
            current_user=_current_user(),
        )

    assert raised.value.status_code == 500
    assert raised.value.detail == "PDF export failed"

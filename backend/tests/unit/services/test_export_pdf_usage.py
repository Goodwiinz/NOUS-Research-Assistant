"""Behavior tests for truthful PDF and provider-usage thread exports."""

from __future__ import annotations

import builtins
import json
import sys
import threading
from datetime import datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.research.export_service import (
    ExportService,
    HTMLFormatter,
    JSONFormatter,
    MarkdownFormatter,
    PDFFormatter,
)
from src.shared.export_schemas import (
    ExportFormat,
    ExportOptions,
    MessageExport,
    ProviderTokenUsage,
    ThreadExport,
)

pytestmark = pytest.mark.unit

_STAMP = datetime(2026, 9, 17, 12, 0, 0)


def _thread_export(*, token_count: int = 0) -> ThreadExport:
    return ThreadExport(
        id="thread-1",
        title="Export test",
        summary=None,
        status="active",
        created_at=_STAMP,
        updated_at=_STAMP,
        last_message_at=_STAMP,
        message_count=1,
        token_count=token_count,
        conversation_id="conversation-1",
        messages=[
            MessageExport(
                id="message-1",
                role="assistant",
                content="A rendered answer.",
                created_at=_STAMP,
            )
        ],
        export_format=ExportFormat.MARKDOWN,
    )


def _fake_weasyprint(
    *,
    output: bytes = b"%PDF-fake",
    fetch_resource: bool = False,
    error: BaseException | None = None,
) -> ModuleType:
    module = ModuleType("weasyprint")
    urls = ModuleType("weasyprint.urls")

    class FakeFatalURLFetchingError(BaseException):
        pass

    class FakeURLFetcher:
        def fetch(self, url: str, headers: object = None) -> object:
            raise AssertionError(f"unexpected resource fetch: {url}")

        def __call__(self, url: str) -> object:
            return self.fetch(url)

    urls.FatalURLFetchingError = FakeFatalURLFetchingError  # type: ignore[attr-defined]
    urls.URLFetcher = FakeURLFetcher  # type: ignore[attr-defined]
    module.urls = urls  # type: ignore[attr-defined]

    class FakeHTML:
        def __init__(self, *, string: str, **kwargs: object) -> None:
            self.string = string
            self.fetcher = kwargs.get("url_fetcher")

        def write_pdf(self) -> bytes:
            if fetch_resource:
                assert self.fetcher is not None
                self.fetcher.fetch("file:///etc/passwd")
            if error is not None:
                raise error
            return output

    module.HTML = FakeHTML  # type: ignore[attr-defined]
    return module


def test_pdf_formatter_returns_pdf_signature_and_pdf_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "weasyprint", _fake_weasyprint())

    formatter = PDFFormatter()
    content = formatter.format(_thread_export(), ExportOptions())

    assert content.startswith(b"%PDF-")
    assert formatter.content_type == "application/pdf"
    assert formatter.file_extension == "pdf"


def test_pdf_formatter_fails_explicitly_when_renderer_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "weasyprint", None)

    formatter = PDFFormatter()

    with pytest.raises(RuntimeError, match="PDF export unavailable"):
        formatter.format(_thread_export(), ExportOptions())
    assert formatter.content_type == "application/pdf"
    assert formatter.file_extension == "pdf"


def test_pdf_formatter_hides_converter_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "weasyprint",
        _fake_weasyprint(error=RuntimeError("native renderer detail")),
    )

    formatter = PDFFormatter()

    with pytest.raises(RuntimeError) as raised:
        formatter.format(_thread_export(), ExportOptions())

    assert str(raised.value) == "PDF export failed"
    assert "native renderer detail" not in str(raised.value)


def test_pdf_formatter_rejects_non_pdf_renderer_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "weasyprint",
        _fake_weasyprint(output=b"<!DOCTYPE html>"),
    )

    with pytest.raises(RuntimeError, match="PDF export failed"):
        PDFFormatter().format(_thread_export(), ExportOptions())


def test_pdf_formatter_rejects_user_controlled_resource_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "weasyprint",
        _fake_weasyprint(fetch_resource=True),
    )

    formatter = PDFFormatter()

    with pytest.raises(RuntimeError, match="PDF export failed"):
        formatter.format(_thread_export(), ExportOptions())


def test_pdf_formatter_reports_missing_resource_policy_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("weasyprint")

    class FakeHTML:
        def __init__(self, **_: object) -> None:
            pass

    module.HTML = FakeHTML  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weasyprint", module)

    with pytest.raises(RuntimeError, match="PDF export unavailable"):
        PDFFormatter().format(_thread_export(), ExportOptions())


def test_native_import_failure_does_not_disable_non_pdf_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def fail_weasyprint(name: str, *args: object, **kwargs: object):
        if name == "weasyprint":
            raise OSError("libpango is unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_weasyprint)

    service = ExportService(AsyncMock())

    assert isinstance(service._formatters[ExportFormat.MARKDOWN], MarkdownFormatter)
    assert isinstance(service._formatters[ExportFormat.HTML], HTMLFormatter)
    assert isinstance(service._formatters[ExportFormat.JSON], JSONFormatter)


@pytest.mark.asyncio
async def test_pdf_formatting_runs_in_a_worker_thread(
) -> None:
    service = ExportService(AsyncMock())
    service._load_thread = AsyncMock(return_value=_thread_export())
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    class ThreadRecordingFormatter:
        file_extension = "pdf"
        content_type = "application/pdf"

        def format(
            self, thread: ThreadExport, options: ExportOptions
        ) -> bytes:
            worker_threads.append(threading.get_ident())
            return b"%PDF-worker"

    service._formatters[ExportFormat.PDF] = ThreadRecordingFormatter()

    content, filename, content_type = await service.export_thread(
        "thread-1", "user-1", ExportFormat.PDF, ExportOptions()
    )

    assert content.startswith(b"%PDF-")
    assert filename.endswith(".pdf")
    assert content_type == "application/pdf"
    assert worker_threads
    assert worker_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_batch_pdf_renderer_failure_is_not_packaged_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    service = ExportService(AsyncMock())
    service._load_thread = AsyncMock(return_value=_thread_export())

    with pytest.raises(RuntimeError, match="PDF export unavailable"):
        await service.export_batch(
            ["thread-1"], "user-1", ExportFormat.PDF, ExportOptions()
        )


@pytest.mark.asyncio
async def test_load_thread_exports_persisted_provider_usage_without_legacy_backfill() -> None:
    message = MagicMock()
    message.id = "message-1"
    message.is_deleted = False
    message.superseded_by_message_id = None
    message.role = SimpleNamespace(value="assistant")
    message.content = "A rendered answer."
    message.created_at = _STAMP
    message.model_name = "gpt-5.6-luna"
    message.token_count = 0
    message.token_usage = {"input_tokens": 42, "output_tokens": 7}
    message.latency_ms = 123
    message.feedback_rating = None
    message.feedback_text = None
    message.citations = []
    message.has_attachments = False

    thread = MagicMock()
    thread.id = "thread-1"
    thread.is_deleted = False
    thread.created_by_id = "user-1"
    thread.title = "Export test"
    thread.summary = None
    thread.status = SimpleNamespace(value="active")
    thread.created_at = _STAMP
    thread.updated_at = _STAMP
    thread.last_message_at = _STAMP
    thread.message_count = 1
    thread.token_count = 0
    thread.conversation_id = "conversation-1"
    thread.messages = [message]
    thread.conversation.is_deleted = False
    thread.conversation.created_by_id = "user-1"
    thread.conversation.workspace.is_deleted = False

    db = AsyncMock()
    result = MagicMock()
    result.unique.return_value.scalar_one_or_none.return_value = thread
    db.execute = AsyncMock(return_value=result)

    exported = await ExportService(db)._load_thread(
        "thread-1", "user-1", ExportOptions()
    )

    assert exported is not None
    assert exported.token_count == 0
    assert exported.messages[0].provider_usage is not None
    assert exported.messages[0].provider_usage.model_dump() == {
        "input_tokens": 42,
        "output_tokens": 7,
    }


def test_human_readable_export_does_not_print_unknown_zero_total() -> None:
    markdown = MarkdownFormatter().format(
        _thread_export(token_count=0), ExportOptions()
    ).decode("utf-8")

    assert "**Tokens**: 0" not in markdown
    assert "Legacy token count" not in markdown


def test_human_readable_export_labels_partial_provider_usage_as_unknown() -> None:
    thread = _thread_export()
    thread.messages[0].provider_usage = ProviderTokenUsage(input_tokens=42)

    markdown = MarkdownFormatter().format(thread, ExportOptions()).decode("utf-8")

    assert "**Provider usage**: 42 input tokens; unknown output tokens" in markdown
    assert "output tokens; 0" not in markdown


def test_json_export_contains_provider_usage_when_present() -> None:
    thread = _thread_export()
    thread.messages[0].provider_usage = ProviderTokenUsage(
        input_tokens=42, output_tokens=7
    )

    exported = json.loads(JSONFormatter().format(thread, ExportOptions()))

    assert exported["messages"][0]["provider_usage"] == {
        "input_tokens": 42,
        "output_tokens": 7,
    }


@pytest.mark.asyncio
async def test_export_thread_stamps_requested_format_in_json_metadata() -> None:
    service = ExportService(AsyncMock())
    service._load_thread = AsyncMock(return_value=_thread_export())

    content, filename, content_type = await service.export_thread(
        "thread-1", "user-1", ExportFormat.JSON, ExportOptions()
    )

    exported = json.loads(content)
    assert exported["export_format"] == "json"
    assert filename.endswith(".json")
    assert content_type.startswith("application/json")

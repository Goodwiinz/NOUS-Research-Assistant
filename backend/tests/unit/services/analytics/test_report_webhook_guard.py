"""_deliver_webhook must refuse non-public destinations before any request (GOO-409 / F1)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest

from src.services.analytics.report_service import ReportGenerationService
from src.utils.outbound_url_guard import UnsafeDestinationError


@pytest.mark.asyncio
async def test_deliver_webhook_refuses_private_url(tmp_path: Path) -> None:
    service = ReportGenerationService()
    report = SimpleNamespace(id="r1", name="n", title="t", output_format="pdf")
    file_path = tmp_path / "r.pdf"
    file_path.write_bytes(b"x")

    with patch(
        "src.services.analytics.report_service.aiohttp.ClientSession"
    ) as session_cls:
        with pytest.raises(UnsafeDestinationError):
            await service._deliver_webhook(
                report,  # type: ignore[arg-type]
                file_path,
                {"webhook": {"url": "https://169.254.169.254/x"}},
            )
    session_cls.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_webhook_upload_uses_multipart_without_redirects(
    tmp_path: Path,
) -> None:
    service = ReportGenerationService()
    report = SimpleNamespace(id="r1", name="n", title="t", output_format="pdf")
    file_path = tmp_path / "r.pdf"
    file_path.write_bytes(b"%PDF-test-report")
    posted: list[aiohttp.FormData] = []

    class Response:
        status = 200

        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

    # Keep the real ClientSession.post boundary: unsupported request keywords
    # (including requests-style files=) must fail before any network access.
    async def request(
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        data: aiohttp.FormData,
        allow_redirects: bool,
    ) -> Any:
        assert method == "POST"
        assert url == "https://hooks.example.com/report"
        assert allow_redirects is False
        posted.append(data)
        return Response()

    with (
        patch("src.services.analytics.report_service.assert_public_https_url"),
        patch.object(aiohttp.ClientSession, "_request", request),
    ):
        await service._deliver_webhook(
            report,  # type: ignore[arg-type]
            file_path,
            {
                "webhook": {
                    "url": "https://hooks.example.com/report",
                    "include_file": True,
                }
            },
        )

    class Writer:
        def __init__(self) -> None:
            self.parts: list[bytes] = []

        async def write(self, data: bytes) -> None:
            self.parts.append(data)

    assert len(posted) == 1
    writer = Writer()
    await posted[0]().write(writer)
    body = b"".join(writer.parts)
    assert b'name="file"' in body
    assert b'filename="r.pdf"' in body
    assert b"%PDF-test-report" in body
    assert b'name="report_id"\r\n\r\nr1' in body

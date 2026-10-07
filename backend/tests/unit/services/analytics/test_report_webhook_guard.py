"""_deliver_webhook must refuse non-public destinations before any request (GOO-409 / F1)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

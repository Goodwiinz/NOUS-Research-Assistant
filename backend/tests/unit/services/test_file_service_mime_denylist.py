"""Audit I10: active-content uploads are rejected by sniffed MIME, the local
download path carries the same hardening as the object-storage path, and the
dead ``file_upload_security`` module stays deleted."""

import asyncio
import io
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from src.api.documents import files as files_api
from src.services.documents.file_service import FileService, FileValidationError

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[3]
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _validate(sniffed: str, content: bytes = PNG_BYTES) -> dict[str, Any]:
    service = FileService.__new__(FileService)
    upload = SimpleNamespace(
        filename="x.png", size=len(content), file=io.BytesIO(content)
    )
    org = SimpleNamespace(max_file_size_bytes=10**6, can_upload_file=lambda _n: True)
    with patch(
        "src.services.documents.file_service.magic.from_buffer", return_value=sniffed
    ):
        return service.validate_file(cast(Any, upload), cast(Any, None), cast(Any, org))


@pytest.mark.parametrize("sniffed", ["text/html", "image/svg+xml"])
def test_png_named_active_content_is_rejected(sniffed: str) -> None:
    with pytest.raises(FileValidationError, match=re.escape(sniffed)):
        _validate(sniffed, b"<html><script>alert(1)</script></html>")


def test_genuine_png_still_passes() -> None:
    assert _validate("image/png")["mime_type"] == "image/png"


class _DB:
    def __init__(self, document: Any) -> None:
        self._document = document

    async def execute(self, _stmt: Any) -> Any:
        doc = self._document
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: doc))


def test_local_download_is_hardened(tmp_path: Path) -> None:
    stored = tmp_path / "blob"
    stored.write_bytes(b"<html></html>")
    document = SimpleNamespace(
        storage_backend="local",
        storage_path=None,
        file_path=str(stored),
        filename="évil.html",
        mime_type="text/html",
    )
    response = asyncio.run(
        files_api.download_file(
            uuid.uuid4(),
            cast(Any, None),
            cast(Any, SimpleNamespace(id=uuid.uuid4())),
            cast(Any, _DB(document)),
        )
    )
    assert response.media_type == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''%C3%A9vil.html"
    )


def test_dead_upload_security_module_stays_deleted() -> None:
    src = BACKEND_ROOT / "src"
    assert not (src / "middleware" / "file_upload_security.py").exists()
    offenders = [
        str(p)
        for p in src.rglob("*.py")
        if "file_upload_security" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == []

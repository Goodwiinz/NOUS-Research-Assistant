"""Safe public error handling for the file upload endpoint."""

import io
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from src.api.documents import files as files_module
from src.services.documents.file_service import FileStorageError, FileValidationError

pytestmark = pytest.mark.unit


def _upload() -> SimpleNamespace:
    return SimpleNamespace(
        filename="sample.txt",
        size=7,
        file=io.BytesIO(b"sample"),
    )


def _user() -> SimpleNamespace:
    organization = _organization()
    return SimpleNamespace(
        id="user-id",
        email="user@example.com",
        role=SimpleNamespace(value="USER"),
        is_active=True,
        organization=organization,
        can_upload_documents=lambda: True,
    )


def _organization() -> SimpleNamespace:
    return SimpleNamespace(
        id="organization-id",
        name="Test organization",
        storage_available_gb=10.0,
        can_upload_file=lambda _size: True,
    )


@pytest.mark.asyncio
async def test_upload_storage_failure_uses_safe_public_detail() -> None:
    user = _user()
    service = SimpleNamespace(
        upload_file=AsyncMock(
            side_effect=FileStorageError(
                "provider credentials leaked in this internal exception"
            )
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        await files_module.upload_file(
            file=cast(Any, _upload()),
            title="Sample",
            description=None,
            tags=None,
            is_public=False,
            processing_priority="normal",
            enable_quality_check=True,
            custom_metadata=None,
            current_user=cast(Any, user),
            db=MagicMock(),
            file_service=cast(Any, service),
        )

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc_info.value.detail == "File upload failed"
    assert "provider credentials" not in exc_info.value.detail


@pytest.mark.asyncio
async def test_upload_validation_error_is_genericized() -> None:
    """I8: FileValidation text stays server-side; client gets a stable message."""
    user = _user()
    service = SimpleNamespace(
        upload_file=AsyncMock(
            side_effect=FileValidationError("File extension '.zip' is not allowed")
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        await files_module.upload_file(
            file=cast(Any, _upload()),
            title="Sample",
            description=None,
            tags=None,
            is_public=False,
            processing_priority="normal",
            enable_quality_check=True,
            custom_metadata=None,
            current_user=cast(Any, user),
            db=MagicMock(),
            file_service=cast(Any, service),
        )

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc_info.value.detail == "File validation failed"
    assert ".zip" not in exc_info.value.detail


@pytest.mark.asyncio
async def test_upload_preserves_safe_http_error() -> None:
    user = _user()
    service = SimpleNamespace(
        upload_file=AsyncMock(
            side_effect=HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Identical file already exists in this organization",
            )
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        await files_module.upload_file(
            file=cast(Any, _upload()),
            title="Sample",
            description=None,
            tags=None,
            is_public=False,
            processing_priority="normal",
            enable_quality_check=True,
            custom_metadata=None,
            current_user=cast(Any, user),
            db=MagicMock(),
            file_service=cast(Any, service),
        )

    assert exc_info.value.status_code == status.HTTP_409_CONFLICT
    assert exc_info.value.detail == "Identical file already exists in this organization"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("public_detail", "expected"),
    [
        ("i8-arbitrary-private-detail", "File validation failed"),
        ("File type is not allowed", "File type is not allowed"),
        (
            "File exceeds the maximum allowed size",
            "File exceeds the maximum allowed size",
        ),
        ("Insufficient storage quota", "Insufficient storage quota"),
        (
            "Archive uploads are not supported; extract and upload the files",
            "Archive uploads are not supported; extract and upload the files",
        ),
    ],
)
async def test_upload_validation_error_returns_curated_public_detail(
    public_detail: str, expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Codex P2 on #1733: actionable validation reasons survive via public_detail."""
    user = _user()
    service = SimpleNamespace(
        upload_file=AsyncMock(
            side_effect=FileValidationError("private", public_detail=public_detail)
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        await files_module.upload_file(
            file=cast(Any, _upload()),
            title="Sample",
            description=None,
            tags=None,
            is_public=False,
            processing_priority="normal",
            enable_quality_check=True,
            custom_metadata=None,
            current_user=cast(Any, user),
            db=MagicMock(),
            file_service=cast(Any, service),
        )

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc_info.value.detail == expected
    assert "private" not in exc_info.value.detail
    assert any(
        record.exc_info and str(record.exc_info[1]) == "private"
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("filename", "size", "expected"),
    [
        ("big.txt", 10_000, "File exceeds the maximum allowed size"),
        (
            "bundle.zip",
            7,
            "Archive uploads are not supported; extract and upload the files",
        ),
        ("tool.exe", 7, "File type is not allowed"),
    ],
)
def test_validate_file_sets_curated_public_detail(
    filename: str, size: int, expected: str
) -> None:
    from src.services.documents.file_service import FileService

    upload = SimpleNamespace(filename=filename, size=size, file=io.BytesIO(b"x"))
    org = SimpleNamespace(
        max_file_size_bytes=1_000,
        storage_available_gb=10.0,
        can_upload_file=lambda _size: True,
    )

    with pytest.raises(FileValidationError) as exc_info:
        FileService.validate_file(
            cast(Any, MagicMock()), cast(Any, upload), cast(Any, None), cast(Any, org)
        )

    assert exc_info.value.public_detail == expected


def test_validate_file_quota_sets_curated_public_detail() -> None:
    from src.services.documents.file_service import FileService

    upload = SimpleNamespace(filename="a.txt", size=7, file=io.BytesIO(b"x"))
    org = SimpleNamespace(
        max_file_size_bytes=1_000,
        storage_available_gb=0.0,
        can_upload_file=lambda _size: False,
    )

    with pytest.raises(FileValidationError) as exc_info:
        FileService.validate_file(
            cast(Any, MagicMock()), cast(Any, upload), cast(Any, None), cast(Any, org)
        )

    assert exc_info.value.public_detail == "Insufficient storage quota"

"""Safe public error handling for the file upload endpoint."""

import io
from types import SimpleNamespace
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
async def test_upload_storage_failure_uses_safe_public_detail(monkeypatch):
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
            file=_upload(),
            title="Sample",
            description=None,
            tags=None,
            is_public=False,
            processing_priority="normal",
            enable_quality_check=True,
            custom_metadata=None,
            current_user=user,
            db=MagicMock(),
            file_service=service,
        )

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc_info.value.detail == "File upload failed"
    assert "provider credentials" not in exc_info.value.detail


@pytest.mark.asyncio
async def test_upload_preserves_safe_validation_error(monkeypatch):
    user = _user()
    service = SimpleNamespace(
        upload_file=AsyncMock(
            side_effect=FileValidationError("File extension '.zip' is not allowed")
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        await files_module.upload_file(
            file=_upload(),
            title="Sample",
            description=None,
            tags=None,
            is_public=False,
            processing_priority="normal",
            enable_quality_check=True,
            custom_metadata=None,
            current_user=user,
            db=MagicMock(),
            file_service=service,
        )

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc_info.value.detail == "File extension '.zip' is not allowed"


@pytest.mark.asyncio
async def test_upload_preserves_safe_http_error(monkeypatch):
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
            file=_upload(),
            title="Sample",
            description=None,
            tags=None,
            is_public=False,
            processing_priority="normal",
            enable_quality_check=True,
            custom_metadata=None,
            current_user=user,
            db=MagicMock(),
            file_service=service,
        )

    assert exc_info.value.status_code == status.HTTP_409_CONFLICT
    assert exc_info.value.detail == "Identical file already exists in this organization"

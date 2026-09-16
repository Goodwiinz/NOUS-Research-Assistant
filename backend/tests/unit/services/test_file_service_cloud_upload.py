"""Regression coverage for lazy cloud upload helper initialization."""

import io
import sys
import tempfile
import types
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.core.config import settings
from src.models.document import DocumentType
from src.services.documents.file_service import (
    FileService,
    FileStorageError,
    FileValidationError,
)

pytestmark = pytest.mark.unit


class _ChunkedUpload:
    def __init__(self, content: bytes, filename: str = "sample.txt"):
        self.filename = filename
        self.size = len(content)
        self.file = io.BytesIO(content)
        self._stream = io.BytesIO(content)

    async def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    async def seek(self, offset: int) -> None:
        self._stream.seek(offset)


class _UploadDB:
    def __init__(self):
        self.commit_calls = 0
        self.rollback_calls = 0

    def add(self, _obj) -> None:
        pass

    def query(self, *_args, **_kwargs):
        raise RuntimeError("query is intentionally unavailable in this unit test")

    async def commit(self) -> None:
        self.commit_calls += 1

    async def refresh(self, _obj) -> None:
        pass

    async def execute(self, _statement):
        return SimpleNamespace(rowcount=1)

    async def rollback(self) -> None:
        self.rollback_calls += 1


class _S3Recorder:
    instances = []

    def __init__(self):
        self.uploads = []
        self.__class__.instances.append(self)

    def upload_fileobj(self, fileobj, key, content_type):
        self.uploads.append((fileobj.read(), key, content_type))
        return key


class _SupabaseRecorder:
    instances = []

    def __init__(self):
        self.uploads = []
        self.__class__.instances.append(self)

    def upload_fileobj(self, fileobj, bucket, key, content_type):
        self.uploads.append((fileobj.read(), bucket, key, content_type))
        return f"{bucket}/{key}"


def _make_service(monkeypatch, tmp_path, backend: str) -> FileService:
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "STORAGE_BACKEND", backend)
    monkeypatch.setattr(settings, "SUPABASE_STORAGE_ENABLED", False)

    service = FileService(_UploadDB())
    service.validate_file = lambda _file, _user, _org: {
        "mime_type": "text/plain",
        "file_size": 18,
        "document_type": DocumentType.TEXT,
    }
    return service


def _patch_supabase_helper(monkeypatch, helper_cls) -> None:
    # The repository's root ``supabase/`` migration directory is a namespace
    # package on the test import path, so provide the third-party symbols that
    # ``src.core.supabase_client`` imports before patching its helper class.
    supabase_package = types.ModuleType("supabase")
    supabase_package.Client = object
    supabase_package.create_client = MagicMock()
    monkeypatch.setitem(sys.modules, "supabase", supabase_package)
    monkeypatch.setattr("src.core.supabase_client.StorageHelper", helper_cls)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["s3", "supabase"])
async def test_cloud_upload_lazily_constructs_helper_and_streams_bytes(
    monkeypatch, tmp_path, backend
):
    payload = b"bytes must arrive intact"
    db = _UploadDB()
    service = _make_service(monkeypatch, tmp_path, backend)
    service.db = db
    user = SimpleNamespace(id=uuid.uuid4())
    organization = SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(
        "src.tasks.processing_tasks.process_document_ingestion", MagicMock()
    )

    if backend == "s3":
        _S3Recorder.instances = []
        monkeypatch.setattr("src.core.s3_client.S3StorageHelper", _S3Recorder)
    else:
        _SupabaseRecorder.instances = []
        _patch_supabase_helper(monkeypatch, _SupabaseRecorder)

    assert service._s3_helper is None
    assert service._storage_helper is None

    document = await service.upload_file(
        _ChunkedUpload(payload), "Sample", user, organization
    )

    recorder_cls = _S3Recorder if backend == "s3" else _SupabaseRecorder
    assert len(recorder_cls.instances) == 1
    helper = recorder_cls.instances[0]
    assert helper.uploads[0][0] == payload
    assert (
        service.s3_helper is helper
        if backend == "s3"
        else service.storage_helper is helper
    )
    assert document.storage_backend == backend
    assert not list(tmp_path.glob("upload_*.bin"))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["s3", "supabase"])
async def test_cloud_helper_failure_rolls_back_and_removes_spool(
    monkeypatch, tmp_path, backend
):
    payload = b"bytes that fail"
    db = _UploadDB()
    service = _make_service(monkeypatch, tmp_path, backend)
    service.db = db
    user = SimpleNamespace(id=uuid.uuid4())
    organization = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    if backend == "s3":
        _S3Recorder.instances = []

        class FailingS3(_S3Recorder):
            def upload_fileobj(self, fileobj, key, content_type):
                self.uploads.append((fileobj.read(), key, content_type))
                raise RuntimeError("provider secret must stay private")

        monkeypatch.setattr("src.core.s3_client.S3StorageHelper", FailingS3)
        recorder_cls = FailingS3
    else:
        _SupabaseRecorder.instances = []

        class FailingSupabase(_SupabaseRecorder):
            def upload_fileobj(self, fileobj, bucket, key, content_type):
                self.uploads.append((fileobj.read(), bucket, key, content_type))
                raise RuntimeError("provider secret must stay private")

        _patch_supabase_helper(monkeypatch, FailingSupabase)
        recorder_cls = FailingSupabase

    with pytest.raises(FileStorageError):
        await service.upload_file(_ChunkedUpload(payload), "Sample", user, organization)

    assert len(recorder_cls.instances) == 1
    assert recorder_cls.instances[0].uploads[0][0] == payload
    assert db.rollback_calls == 1
    assert not list(tmp_path.glob("upload_*.bin"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "safe_error",
    [
        FileValidationError("File extension '.zip' is not allowed"),
        HTTPException(status_code=409, detail="Identical file already exists"),
    ],
)
async def test_safe_upload_errors_survive_service_cleanup(
    monkeypatch, tmp_path, safe_error
):
    service = _make_service(monkeypatch, tmp_path, "s3")
    service.validate_file = lambda *_args: (_ for _ in ()).throw(safe_error)

    with pytest.raises(type(safe_error)) as exc_info:
        await service.upload_file(
            _ChunkedUpload(b"safe error"),
            "Sample",
            SimpleNamespace(id=uuid.uuid4()),
            SimpleNamespace(id=uuid.uuid4()),
        )

    assert str(exc_info.value) == str(safe_error)
    assert service.db.rollback_calls == 1

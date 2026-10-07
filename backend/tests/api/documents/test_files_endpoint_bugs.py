"""Regressions for three files.py endpoint bugs (ingestion hunt, verified).

1. reprocess_file reset status to PENDING but created no ProcessingJob and
   enqueued no task — the document sat PENDING forever while the API reported
   success. It must create a job and register a post-commit enqueue (via
   enqueue_after_commit) like documents.py does.
2. cancel_upload compared JobStatus (a plain PyEnum) to lowercase strings, so
   `status not in ["pending","running"]` was always True and every cancel
   early-returned; the real cancel block was dead.
3. list_files compared the ProcessingStatus enum column to the raw frontend
   vocabulary the same router emits ("indexed"/"queued") — LookupError → 500.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.documents import files as files_mod
from src.models.processing import JobStatus
from src.services.documents.file_service import FileService, UploadCancellation
from src.shared.pagination import PaginationParams

pytestmark = pytest.mark.unit


def _result(value):
    r = MagicMock()
    r.scalars.return_value.first.return_value = value
    return r


def _user(admin=False):
    u = MagicMock()
    u.id = "user-1"
    u.has_permission.return_value = admin
    return u


def test_reprocess_creates_job_and_enqueues():
    document = MagicMock()
    document.id = uuid.uuid4()
    document.uploaded_by_user_id = "user-1"
    document.file_path = "s3://b/k.pdf"
    document.document_type.value = "pdf"
    document.mime_type = "application/pdf"

    db = MagicMock()
    selected = MagicMock()
    selected.scalar_one_or_none.return_value = document
    inactive = MagicMock()
    inactive.first.return_value = None
    db.execute = AsyncMock(side_effect=[selected, inactive])
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    org = MagicMock()
    org.id = uuid.uuid4()

    with (
        patch("src.tasks.processing_tasks.process_document_ingestion") as task,
        patch("src.tasks.enqueue.enqueue_after_commit") as enqueue,
    ):
        resp = asyncio.run(
            files_mod.reprocess_file(
                str(document.id),
                current_user=_user(),
                organization=org,
                db=db,
            )
        )

    # A job was added and a post-commit enqueue registered for the ingestion
    # task — not a bare status reset, and not a pre-commit .delay().
    db.add.assert_called_once()
    enqueue.assert_called_once()
    enq_args = enqueue.call_args.args
    assert enq_args[0] is db and enq_args[1] is task
    db.commit.assert_awaited_once()
    assert "job_id" in resp


@pytest.mark.parametrize("surface", ["file", "document"])
@pytest.mark.parametrize("missing", [True, False])
def test_reprocess_preserves_public_error_details(surface, missing):
    document = MagicMock()
    document.id = uuid.uuid4()
    document.uploaded_by_user_id = "another-owner"
    selected = MagicMock()
    selected.scalar_one_or_none.return_value = None if missing else document
    db = MagicMock()
    db.execute = AsyncMock(return_value=selected)
    org = MagicMock(id=uuid.uuid4())
    user = _user()

    async def invoke():
        if surface == "file":
            return await files_mod.reprocess_file(
                document.id, current_user=user, organization=org, db=db
            )
        service = FileService.__new__(FileService)
        service.db = db
        return await service.lock_document_for_reprocessing(document.id, org.id, user)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(invoke())
    assert exc.value.status_code == (404 if missing else 403)
    expected = (
        f"{surface.capitalize()} not found"
        if missing
        else f"Can only reprocess your own {surface}s or require admin role"
    )
    assert exc.value.detail == expected
    db.add.assert_not_called()
    db.flush.assert_not_called()
    db.commit.assert_not_called()


def test_cancel_uses_enum_not_string_and_cancels():
    upload_id = str(uuid.uuid4())
    job_id = uuid.uuid4()
    service = MagicMock()
    service.cancel_upload_job = AsyncMock(
        return_value=UploadCancellation(job_id, JobStatus.CANCELLED, True)
    )
    db = MagicMock()

    resp = asyncio.run(
        files_mod.cancel_upload(
            upload_id, current_user=_user(), db=db, file_service=service
        )
    )
    # Transport preserves the service's committed cancellation result.
    assert resp["job_id"] == job_id
    assert "cancelled successfully" in resp["message"].lower()


def test_cancel_rejects_finished_job_with_enum_value():
    upload_id = str(uuid.uuid4())
    service = MagicMock()
    service.cancel_upload_job = AsyncMock(
        return_value=UploadCancellation(uuid.uuid4(), JobStatus.COMPLETED, False)
    )
    db = MagicMock()

    resp = asyncio.run(
        files_mod.cancel_upload(
            upload_id, current_user=_user(), db=db, file_service=service
        )
    )
    assert resp["job_status"] == "completed"
    assert "cannot cancel" in resp["message"].lower()


def test_list_files_maps_frontend_status_vocabulary():
    org = MagicMock()
    org.id = uuid.uuid4()
    db = MagicMock()
    count_res = MagicMock()
    count_res.scalar.return_value = 0
    list_res = MagicMock()
    list_res.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[count_res, list_res])

    # "indexed" is what the upload response emits; raw-comparing it to the enum
    # column used to raise LookupError → 500. Now it maps and returns cleanly.
    resp = asyncio.run(
        files_mod.list_files(
            pagination=PaginationParams(page=1, size=20),
            document_type=None,
            processing_status="indexed",
            search=None,
            current_user=_user(),
            organization=org,
            db=db,
        )
    )
    assert resp.total == 0


def test_list_files_rejects_unknown_status():
    org = MagicMock()
    org.id = uuid.uuid4()
    db = MagicMock()
    db.execute = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            files_mod.list_files(
                pagination=PaginationParams(page=1, size=20),
                document_type=None,
                processing_status="bogus",
                search=None,
                current_user=_user(),
                organization=org,
                db=db,
            )
        )
    assert exc.value.status_code == 400

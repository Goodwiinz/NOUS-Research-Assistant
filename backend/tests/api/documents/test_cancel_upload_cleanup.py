"""Regression: cancelling a pending upload must release storage + quota.

cancel_upload previously did a bare `document.soft_delete()` + commit for a
pending/processing document. But the object was already uploaded to storage at
PENDING time, so that left the object orphaned AND never decremented the org's
storage usage — an upload-then-cancel loop leaked both. The fix routes through
file_service.delete_file(), which deletes the physical object, reverts the
quota, soft-deletes, and commits.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.documents import files as files_mod


def _result(value):
    r = MagicMock()
    r.scalars.return_value.first.return_value = value
    return r


@pytest.mark.unit
def test_cancel_pending_upload_routes_through_delete_file():
    upload_id = str(uuid.uuid4())
    user = MagicMock()
    user.id = "user-1"

    document = MagicMock()
    document.id = upload_id
    document.processing_status.value = "pending"

    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(document))

    file_service = MagicMock()
    file_service.cancel_upload_job = AsyncMock(return_value=None)
    file_service.delete_file = AsyncMock(return_value=True)

    resp = asyncio.run(
        files_mod.cancel_upload(
            upload_id, current_user=user, db=db, file_service=file_service
        )
    )

    # Must release the object + quota via delete_file, not a bare soft_delete.
    file_service.delete_file.assert_awaited_once_with(document, user)
    document.soft_delete.assert_not_called()
    assert resp["document_id"] == upload_id
    assert "cancelled" in resp["message"].lower()

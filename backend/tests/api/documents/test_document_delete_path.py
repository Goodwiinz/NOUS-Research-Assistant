"""DB-audit regressions: delete ordering, atomic quota.

- delete_document must commit the soft-delete BEFORE removing the storage
  object (mirrors FileService.delete_file's fix; the endpoint used to bypass it).
- Organization storage accounting must be a single server-side UPDATE, not a
  Python read-modify-write (lost updates under concurrency).

Split out of test_delete_ordering_and_dedup_race.py (PR #1453): the dedup-race
test that lived alongside these exercised the since-deleted EnhancedFileService
upload path, not delete_document/storage_usage_update, so it did not survive
the cluster deletion. These four remain the only regression coverage for the
live delete_document endpoint's commit-before-physical-delete ordering.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.api.documents import documents as documents_mod
from src.models.organization import Organization

pytestmark = pytest.mark.unit


def _result(value):
    r = MagicMock()
    r.scalars.return_value.first.return_value = value
    return r


def _doc():
    document = MagicMock()
    document.id = uuid.uuid4()
    document.organization_id = uuid.uuid4()
    document.file_size_bytes = 100
    document.do_kb_data_source_uuid = None
    document.uploaded_by_user_id = "user-1"
    return document


def _user_org(document):
    user = MagicMock()
    user.id = "user-1"
    user.has_permission.return_value = True
    org = MagicMock()
    org.id = document.organization_id
    return user, org


def _delete(document, db, file_service):
    user, org = _user_org(document)
    return documents_mod.delete_document(
        document_id=str(document.id),
        cascade=True,
        current_user=user,
        organization=org,
        db=db,
        file_service=file_service,
    )


def test_commit_happens_before_physical_delete():
    document = _doc()
    order = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(document))
    file_service = MagicMock()
    # The commit happens inside soft_delete_documents.
    file_service.soft_delete_documents = AsyncMock(
        side_effect=lambda *a, **k: order.append("commit") or [document]
    )
    file_service.delete_physical_file = MagicMock(
        side_effect=lambda d: order.append("physical")
    )

    resp = asyncio.run(_delete(document, db, file_service))
    assert resp["document_id"] == str(document.id)
    assert order == ["commit", "physical"]


def test_commit_failure_leaves_object_untouched():
    document = _doc()
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(document))
    file_service = MagicMock()
    file_service.soft_delete_documents = AsyncMock(
        side_effect=RuntimeError("pool gone")
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_delete(document, db, file_service))
    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to delete document"  # no raw error text
    # The rolled-back row is still live, so its object must NOT be deleted.
    file_service.delete_physical_file.assert_not_called()


def test_physical_delete_failure_does_not_fail_the_delete():
    document = _doc()
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(document))
    file_service = MagicMock()
    file_service.soft_delete_documents = AsyncMock(return_value=[document])
    file_service.delete_physical_file = MagicMock(side_effect=RuntimeError("s3 down"))

    resp = asyncio.run(_delete(document, db, file_service))
    # Orphan object, not a failed delete: the committed soft-delete stands.
    assert resp["document_id"] == str(document.id)


def test_storage_usage_update_is_server_side_atomic():
    stmt = Organization.storage_usage_update(uuid.uuid4(), -100)
    sql = str(stmt.compile(compile_kwargs={"literal_binds": False})).lower()
    # Arithmetic + clamp happen inside the UPDATE, not in Python.
    assert "greatest" in sql
    assert "storage_used_bytes +" in sql.replace(
        "organizations.storage_used_bytes", "storage_used_bytes"
    )

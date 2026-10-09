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
from src.core import database
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


def test_graph_session_acquisition_failure_keeps_committed_delete_successful(
    monkeypatch, caplog
):
    document = _doc()
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(document))
    file_service = MagicMock()
    file_service.soft_delete_documents = AsyncMock(return_value=[document])
    monkeypatch.setattr(
        database,
        "AsyncSessionLocal",
        MagicMock(side_effect=OSError("pool unavailable")),
    )

    response = asyncio.run(_delete(document, db, file_service))

    assert response["document_id"] == str(document.id)
    assert "graph cleanup failed" in caplog.text


@pytest.mark.parametrize("failure_at", ["construct", "enter", "cleanup", "exit"])
def test_bulk_graph_cleanup_continues_after_session_failure(
    monkeypatch, caplog, failure_at
):
    first_session = MagicMock()
    first_session.__aenter__ = AsyncMock(return_value=first_session)
    first_session.__aexit__ = AsyncMock(return_value=False)
    error = OSError("pool unavailable")
    if failure_at == "enter":
        first_session.__aenter__.side_effect = error
    if failure_at == "exit":
        first_session.__aexit__.side_effect = error
    second_session = MagicMock()
    second_session.__aenter__ = AsyncMock(return_value=second_session)
    second_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        database,
        "AsyncSessionLocal",
        MagicMock(
            side_effect=[
                error if failure_at == "construct" else first_session,
                second_session,
            ]
        ),
    )
    cleanup = AsyncMock(side_effect=error if failure_at == "cleanup" else None)
    second_cleanup = AsyncMock()
    monkeypatch.setattr(
        documents_mod,
        "FileService",
        MagicMock(
            side_effect=lambda session: MagicMock(
                cleanup_deleted_document_graph=(
                    cleanup if session is first_session else second_cleanup
                )
            )
        ),
    )

    asyncio.run(
        documents_mod._cleanup_document_graphs_background(["first", "second"], "org")
    )

    second_cleanup.assert_awaited_once_with("second", "org")
    assert "graph cleanup failed" in caplog.text

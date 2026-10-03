"""delete_file must commit the soft-delete BEFORE removing the storage object.

Deleting the physical object first meant a commit failure rolled the DB row back
while the object was already irreversibly gone — a live row pointing at a missing
file. Now the DB is the source of truth: commit first, then best-effort physical
delete (a storage-delete failure must NOT roll back the committed soft-delete).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.services.documents.file_service import FileService

pytestmark = pytest.mark.unit


def _svc():
    svc = object.__new__(FileService)  # skip heavy __init__
    svc.db = AsyncMock()
    return svc


def _doc(owner_id="u1"):
    doc = MagicMock()
    doc.uploaded_by_user_id = owner_id
    doc.file_size_bytes = 100
    doc.organization = MagicMock()
    return doc


def _owner():
    user = MagicMock()
    user.id = "u1"
    user.has_permission = MagicMock(return_value=False)
    return user


@pytest.mark.asyncio
async def test_commits_before_physical_delete():
    svc = _svc()
    # Satellite (DO KB / Neo4j) cleanup is exercised in its own test — stub it
    # here so these ordering tests stay focused on the DB→object sequence.
    svc._cleanup_satellites_on_delete = AsyncMock()
    order = []
    doc, user = _doc(), _owner()
    # soft_delete_documents commits; its real behaviour is proven against
    # Postgres in tests/integration/test_document_deletion_postgres.py.
    svc.soft_delete_documents = AsyncMock(
        side_effect=lambda *a, **k: order.append("commit") or [doc]
    )
    svc.delete_physical_file = MagicMock(side_effect=lambda d: order.append("physical"))

    assert await svc.delete_file(doc, user) is True
    # commit (DB source of truth) happens BEFORE the irreversible object delete.
    assert order == ["commit", "physical"]


@pytest.mark.asyncio
async def test_physical_delete_failure_does_not_rollback():
    svc = _svc()
    svc._cleanup_satellites_on_delete = AsyncMock()
    svc.db.rollback = AsyncMock()
    svc.delete_physical_file = MagicMock(side_effect=RuntimeError("s3 down"))

    doc, user = _doc(), _owner()
    svc.soft_delete_documents = AsyncMock(return_value=[doc])
    # A storage-delete failure after the commit leaves an orphan, not a data-loss
    # rollback: the call still succeeds and the committed soft-delete stands.
    assert await svc.delete_file(doc, user) is True
    svc.soft_delete_documents.assert_awaited_once()
    svc.db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_cascades_entity_and_satellite_cleanup():
    """delete_file must reap the document's Entity/ProcessingJob rows (the
    cascading soft_delete_documents) and its DO KB + Neo4j satellites
    (post-commit) — otherwise this second delete surface orphans them (unlike
    documents.delete_document)."""
    svc = _svc()
    svc.delete_physical_file = MagicMock()
    svc._cleanup_satellites_on_delete = AsyncMock()

    doc, user = _doc(), _owner()
    svc.soft_delete_documents = AsyncMock(return_value=[doc])
    assert await svc.delete_file(doc, user) is True

    svc.soft_delete_documents.assert_awaited_once_with(
        doc.organization_id, [doc.id], user=user
    )
    # Satellite cleanup fired after the commit, exactly once.
    svc._cleanup_satellites_on_delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_already_deleted_is_404_without_cleanup():
    """A concurrent delete already won and released the quota: refuse, and
    leave the storage object and satellites to the winner."""
    svc = _svc()
    svc.delete_physical_file = MagicMock()
    svc._cleanup_satellites_on_delete = AsyncMock()
    svc.soft_delete_documents = AsyncMock(return_value=[])

    with pytest.raises(HTTPException) as exc:
        await svc.delete_file(_doc(), _owner())
    assert exc.value.status_code == 404
    svc.delete_physical_file.assert_not_called()
    svc._cleanup_satellites_on_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_denied_propagates_403():
    svc = _svc()
    svc.delete_physical_file = MagicMock()
    doc = _doc(owner_id="someone-else")
    user = _owner()  # not owner, not admin
    with pytest.raises(HTTPException) as exc:
        await svc.delete_file(doc, user)
    assert exc.value.status_code == 403
    svc.delete_physical_file.assert_not_called()
    svc.db.commit.assert_not_awaited()

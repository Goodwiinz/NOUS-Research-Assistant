"""R4-L2 regression: ``bulk_delete_documents`` counted duplicate ids in
``request.document_ids`` multiple times toward ``successful``/
``total_processed``, over-reporting how many documents were actually
deleted. Requested ids are now deduped before the lookup + counts.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks

from src.api.documents import documents as documents_mod
from src.api.documents.documents import BulkDocumentRequest
from src.models.user import UserRole

pytestmark = pytest.mark.unit


def _admin_user() -> MagicMock:
    u = MagicMock()
    u.has_permission.return_value = True
    return u


def _org() -> MagicMock:
    o = MagicMock()
    o.id = uuid.uuid4()
    return o


def test_duplicate_ids_counted_once() -> None:
    dup_id = str(uuid.uuid4())
    missing_id = str(uuid.uuid4())

    doc = MagicMock()
    doc.id = uuid.UUID(dup_id)
    doc.file_size_bytes = 100
    doc.do_kb_data_source_uuid = None
    doc.soft_delete = MagicMock()

    db = MagicMock()

    file_service = MagicMock()
    file_service.soft_delete_documents = AsyncMock(return_value=[doc])
    file_service.delete_physical_file = MagicMock()

    request = BulkDocumentRequest(document_ids=[dup_id, dup_id, missing_id])

    resp = asyncio.run(
        documents_mod.bulk_delete_documents(
            request,
            cascade=False,
            background_tasks=BackgroundTasks(),
            current_user=_admin_user(),
            organization=_org(),
            db=db,
            file_service=file_service,
        )
    )

    # The service sees each id once, so it cannot lock or bill a row twice.
    assert file_service.soft_delete_documents.await_args.args[1] == [
        dup_id,
        missing_id,
    ]
    # The duplicate must appear exactly once in `successful`, not twice.
    assert resp.successful == [dup_id]
    assert resp.success_count == 1
    # 2 unique ids requested (dup collapses to one), one found + one missing.
    assert resp.total_processed == 2
    assert resp.failed == [{"document_id": missing_id, "error": "Document not found"}]
    assert resp.failure_count == 1

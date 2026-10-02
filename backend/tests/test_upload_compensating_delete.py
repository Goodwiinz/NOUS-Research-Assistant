"""FileService.upload_file must compensate on a post-PUT failure.

Regression guard for the upload compensating-delete gap (upload/storage/quota
hunt, 2026-07-09): the live POST /api/v1/files/upload path PUTs the object to
storage, then commits Document / quota / ProcessingJob in separate transactions
with a bare `except: rollback; raise`. A failure between the PUT and the last
step orphaned the object, stranded a PENDING row, and drifted org storage quota.

The fix reverses the DB (soft-delete row + revert quota + drop the stray job) and
then deletes the object — but only if the DB reversal actually committed, so a
correlated second failure leaves a *sweepable orphan object*, never a live row
pointing at a deleted file. The object is deleted by captured primitives, never
the (possibly expired) ORM instance.

Pure unit test — no DB, no storage; the session and storage helper are faked.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.models.document import Document, DocumentType
from src.models.organization import Organization
from src.models.processing import ProcessingJob
from src.services.documents.file_service import FileService, FileStorageError


class _FakeUpload:
    def __init__(self, content: bytes, filename: str = "test.pdf"):
        self.filename = filename
        self._content = content
        self.file = MagicMock()  # .seek(0) is a no-op

    async def read(self) -> bytes:
        return self._content


class _FakeDB:
    """Async session stub; each commit in `fail_commits` (1-based) raises."""

    def __init__(self, fail_commits=None):
        self.fail_commits = set(fail_commits or ())
        self.commit_calls = 0
        self.rollbacks = 0
        self.deleted = []

    def add(self, _obj):  # sync in the SQLAlchemy async API
        pass

    async def commit(self):
        self.commit_calls += 1
        if self.commit_calls in self.fail_commits:
            raise RuntimeError("simulated DB commit failure")

    async def refresh(self, _obj):
        pass

    async def execute(self, _stmt):
        # rowcount for UPDATEs; first() → None = no duplicate (GOO-333 dup check)
        return SimpleNamespace(rowcount=1, first=lambda: None)

    async def rollback(self):
        self.rollbacks += 1

    async def delete(self, obj):
        self.deleted.append(obj)


def _make_service(fake_db) -> FileService:
    svc = FileService.__new__(FileService)  # bypass __init__ filesystem side effects
    svc.db = fake_db
    svc._storage_backend = "s3"
    svc._s3_helper = MagicMock()  # s3_helper.upload_file is a no-op mock
    svc._storage_helper = None
    svc._delete_stored_object = MagicMock()  # compensation deletes via primitives
    svc.validate_file = lambda file, user, org: {
        "mime_type": "application/pdf",
        "file_size": 1234,
        "document_type": DocumentType.PDF,
    }
    return svc


@pytest.fixture
def quota_deltas(monkeypatch):
    """Record every delta passed to Organization.storage_usage_update."""
    deltas = []

    def _record(org_id, delta):
        deltas.append(delta)
        return ("storage_quota_claim", str(org_id), delta)  # sentinel stmt

    monkeypatch.setattr(Organization, "storage_quota_claim", staticmethod(_record))

    def _record_usage(org_id, delta):
        deltas.append(delta)
        return ("storage_usage_update", str(org_id), delta)

    monkeypatch.setattr(
        Organization, "storage_usage_update", staticmethod(_record_usage)
    )
    return deltas


@pytest.fixture
def soft_deleted(monkeypatch):
    """Record documents that were soft-deleted (and actually flip is_deleted)."""
    seen = []
    original = Document.soft_delete

    def _spy(self):
        seen.append(self)
        original(self)

    monkeypatch.setattr(Document, "soft_delete", _spy)
    return seen


def _org_user():
    return MagicMock(id=uuid.uuid4()), MagicMock(id=uuid.uuid4())


@pytest.mark.asyncio
async def test_late_failure_fully_compensates(quota_deltas, soft_deleted):
    """Object + Document + quota committed, then ProcessingJob commit fails →
    object deleted, row soft-deleted, quota reverted (net zero)."""
    db = _FakeDB(fail_commits={3})  # 1=Document, 2=quota, 3=ProcessingJob
    svc = _make_service(db)
    org, user = _org_user()

    with pytest.raises(FileStorageError):
        await svc.upload_file(_FakeUpload(b"hello world"), "Doc", user, org)

    svc._delete_stored_object.assert_called_once()  # object cleaned up
    assert len(soft_deleted) == 1 and soft_deleted[0].is_deleted is True
    assert quota_deltas == [1234, -1234]  # applied then reverted → no drift


@pytest.mark.asyncio
async def test_first_commit_failure_deletes_object_only(quota_deltas, soft_deleted):
    """Object committed, then the first (Document) commit fails → object still
    deleted; quota never applied and there is no committed row to soft-delete."""
    db = _FakeDB(fail_commits={1})
    svc = _make_service(db)
    org, user = _org_user()

    with pytest.raises(FileStorageError):
        await svc.upload_file(_FakeUpload(b"hello world"), "Doc", user, org)

    svc._delete_stored_object.assert_called_once()  # no orphaned object
    assert soft_deleted == []  # nothing committed to soft-delete
    assert quota_deltas == []  # neither forward nor revert


@pytest.mark.asyncio
async def test_enqueue_failure_cleans_object_row_quota_and_job(
    quota_deltas, soft_deleted, monkeypatch
):
    """All commits succeed, then the Celery enqueue (.delay) raises → object
    deleted, row soft-deleted, quota reverted, and the committed ProcessingJob
    dropped so it can't run against the soft-deleted document."""
    mock_task = MagicMock()
    mock_task.delay.side_effect = RuntimeError("broker unreachable")
    monkeypatch.setattr(
        "src.tasks.processing_tasks.process_document_ingestion", mock_task
    )

    db = _FakeDB(fail_commits=set())  # all commits succeed; .delay fails
    svc = _make_service(db)
    org, user = _org_user()

    with pytest.raises(FileStorageError):
        await svc.upload_file(_FakeUpload(b"hello world"), "Doc", user, org)

    svc._delete_stored_object.assert_called_once()
    assert len(soft_deleted) == 1 and soft_deleted[0].is_deleted is True
    assert quota_deltas == [1234, -1234]
    assert any(isinstance(o, ProcessingJob) for o in db.deleted)  # stray job dropped


@pytest.mark.asyncio
async def test_correlated_failure_preserves_object_as_orphan(quota_deltas):
    """A real commit failure AND a failing compensation commit → the row stays
    live, so the object is preserved as a sweepable orphan (never a live row
    pointing at a deleted file)."""
    db = _FakeDB(
        fail_commits={3, 4}
    )  # job commit fails, then compensation commit fails
    svc = _make_service(db)
    org, user = _org_user()

    with pytest.raises(FileStorageError):
        await svc.upload_file(_FakeUpload(b"hello world"), "Doc", user, org)

    # reversal did not commit → object must NOT be deleted (would orphan a live row)
    svc._delete_stored_object.assert_not_called()

"""The PUT /files/{id} metadata write, extracted into file_metadata_service.

Plan 07 Slice 3, review amendment 5: the router used to look the document up,
check owner-or-admin and commit inline. The service keeps the router's
organization predicate and ownership rule, and owns one transaction boundary:
``commit=True`` (HTTP callers) commits, or rolls back on failure;
``commit=False`` (the integration action path, which commits the effect
together with its receipt) only flushes into the caller's transaction.

Users are seeded with raw SQL: first_name/last_name are encrypted columns and
these tests do not initialize field encryption.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.models.document import Document, DocumentType
from src.models.organization import Organization
from src.models.user import User
from src.services.documents.file_metadata_service import (
    FileMetadataPermissionError,
    FileMetadataUpdateError,
    update_file_metadata,
)

pytestmark = pytest.mark.unit

ORG, FOREIGN_ORG = uuid4(), uuid4()
OWNER, COLLEAGUE, ADMIN, FOREIGN_ADMIN = (uuid4() for _ in range(4))
DOC, DELETED_DOC, FOREIGN_DOC = (uuid4() for _ in range(3))
ORIGINAL_TITLE = "Original title"
ORIGINAL_TAGS = ["seed"]

_INSERT_USER = text(
    "INSERT INTO users (id, organization_id, email, password_hash, first_name,"
    " last_name, role, is_active, login_count, created_at, updated_at,"
    " is_deleted) VALUES (:id, :org, :email, 'unused', 'Test', 'User', :role,"
    " 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
)


def _document(
    document_id: UUID, organization_id: UUID, uploader: UUID, *, is_deleted: bool
) -> Document:
    return Document(
        id=document_id,
        organization_id=organization_id,
        uploaded_by_user_id=uploader,
        title=ORIGINAL_TITLE,
        filename="paper.pdf",
        file_path="/unused/paper.pdf",
        file_size_bytes=100,
        mime_type="application/pdf",
        document_type=DocumentType.PDF,
        tags=list(ORIGINAL_TAGS),
        is_deleted=is_deleted,
    )


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    # A file database, not :memory:, so an observer session on its own
    # connection sees committed state only.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'files.db'}")
    tables: list[Any] = [Organization, User, Document]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        for org_id in (ORG, FOREIGN_ORG):
            session.add(
                Organization(id=org_id, name=str(org_id), storage_limit_bytes=10**6)
            )
        await session.flush()
        for user_id, org_id, role in (
            (OWNER, ORG, "USER"),
            (COLLEAGUE, ORG, "USER"),
            (ADMIN, ORG, "ADMIN"),
            (FOREIGN_ADMIN, FOREIGN_ORG, "ADMIN"),
        ):
            await session.execute(
                _INSERT_USER,
                {
                    "id": str(user_id),
                    "org": str(org_id),
                    "email": f"{user_id}@example.test",
                    "role": role,
                },
            )
        session.add_all(
            [
                _document(DOC, ORG, OWNER, is_deleted=False),
                _document(DELETED_DOC, ORG, OWNER, is_deleted=True),
                _document(FOREIGN_DOC, FOREIGN_ORG, FOREIGN_ADMIN, is_deleted=False),
            ]
        )
        await session.commit()
    yield engine
    await engine.dispose()


@pytest.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


async def _user(db: AsyncSession, user_id: UUID) -> User:
    user = await db.get(User, user_id)
    assert isinstance(user, User)
    return user


async def _committed(engine: AsyncEngine, document_id: UUID = DOC) -> Document:
    """Read through a separate session: only committed state is visible."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as observer:
        document = await observer.get(Document, document_id)
        assert isinstance(document, Document)
        return document


async def test_owner_title_update_is_committed(
    db: AsyncSession, engine: AsyncEngine
) -> None:
    updated = await update_file_metadata(
        db, DOC, await _user(db, OWNER), title="Renamed"
    )

    assert updated is not None
    assert (updated.id, updated.title) == (DOC, "Renamed")
    stored = await _committed(engine)
    assert (stored.title, stored.tags, stored.is_public) == (
        "Renamed",
        ORIGINAL_TAGS,
        False,
    )


async def test_owner_tags_update_is_committed(
    db: AsyncSession, engine: AsyncEngine
) -> None:
    updated = await update_file_metadata(
        db, DOC, await _user(db, OWNER), tags=["graphs", "survey"]
    )

    assert updated is not None and updated.tags == ["graphs", "survey"]
    stored = await _committed(engine)
    assert (stored.title, stored.tags) == (ORIGINAL_TITLE, ["graphs", "survey"])


async def test_is_public_update_is_committed(
    db: AsyncSession, engine: AsyncEngine
) -> None:
    # PUT /files/{id} also takes is_public; the service keeps that behaviour.
    await update_file_metadata(db, DOC, await _user(db, OWNER), is_public=True)

    assert (await _committed(engine)).is_public is True


@pytest.mark.parametrize(
    "document_id",
    [uuid4(), DELETED_DOC, FOREIGN_DOC],
    ids=["missing", "soft-deleted", "foreign-org"],
)
async def test_missing_deleted_or_foreign_document_is_not_found(
    db: AsyncSession, engine: AsyncEngine, document_id: UUID
) -> None:
    # An admin caller: the role must not widen the organization predicate.
    result = await update_file_metadata(
        db, document_id, await _user(db, ADMIN), title="Renamed"
    )

    assert result is None
    for existing in (DELETED_DOC, FOREIGN_DOC):
        assert (await _committed(engine, existing)).title == ORIGINAL_TITLE


async def test_admin_of_another_organization_cannot_reach_the_document(
    db: AsyncSession, engine: AsyncEngine
) -> None:
    result = await update_file_metadata(
        db, DOC, await _user(db, FOREIGN_ADMIN), title="Hijacked"
    )

    assert result is None
    assert (await _committed(engine)).title == ORIGINAL_TITLE


async def test_colleague_without_admin_role_is_refused_before_any_change(
    db: AsyncSession, engine: AsyncEngine
) -> None:
    with pytest.raises(FileMetadataPermissionError):
        await update_file_metadata(db, DOC, await _user(db, COLLEAGUE), title="Renamed")

    # A PermissionError, which the integration action path maps to a stable
    # public error.
    assert issubclass(FileMetadataPermissionError, PermissionError)
    assert not db.dirty
    assert (await _committed(engine)).title == ORIGINAL_TITLE


async def test_admin_may_edit_a_colleagues_document(
    db: AsyncSession, engine: AsyncEngine
) -> None:
    updated = await update_file_metadata(
        db, DOC, await _user(db, ADMIN), title="Edited by an admin"
    )

    assert updated is not None
    assert (await _committed(engine)).title == "Edited by an admin"


async def test_commit_false_leaves_the_session_uncommitted(
    db: AsyncSession, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await _user(db, OWNER)
    real_commit, real_rollback = db.commit, db.rollback
    calls: list[str] = []

    async def commit() -> None:
        calls.append("commit")
        await real_commit()

    async def rollback() -> None:
        calls.append("rollback")
        await real_rollback()

    monkeypatch.setattr(db, "commit", commit)
    monkeypatch.setattr(db, "rollback", rollback)

    updated = await update_file_metadata(
        db, DOC, owner, title="Pending", tags=["draft"], commit=False
    )

    assert updated is not None and (updated.title, updated.tags) == (
        "Pending",
        ["draft"],
    )
    assert calls == []
    assert db.in_transaction()
    # Flushed into this session's transaction, invisible to everyone else.
    assert (await _committed(engine)).title == ORIGINAL_TITLE
    await real_rollback()
    reloaded = await db.get(Document, DOC, populate_existing=True)
    assert reloaded is not None
    assert (reloaded.title, reloaded.tags) == (ORIGINAL_TITLE, ORIGINAL_TAGS)


async def test_commit_false_write_lands_with_the_callers_commit(
    db: AsyncSession, engine: AsyncEngine
) -> None:
    await update_file_metadata(
        db, DOC, await _user(db, OWNER), title="Joined", commit=False
    )
    await db.commit()

    assert (await _committed(engine)).title == "Joined"


async def test_failed_commit_rolls_back_and_raises_a_stable_error(
    db: AsyncSession, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await _user(db, OWNER)
    real_flush = db.flush

    async def failing_commit() -> None:
        await real_flush()  # the UPDATE reaches the transaction...
        raise RuntimeError("driver detail: password=hunter2")  # ...then fails

    monkeypatch.setattr(db, "commit", failing_commit)

    with pytest.raises(FileMetadataUpdateError) as caught:
        await update_file_metadata(db, DOC, owner, title="Never stored")

    assert "hunter2" not in str(caught.value)
    assert isinstance(caught.value.__cause__, RuntimeError)
    # The service owned this transaction, so it rolled it back: the flushed
    # UPDATE is gone even from this session's own view.
    assert not db.in_transaction()
    reloaded = await db.get(Document, DOC, populate_existing=True)
    assert reloaded is not None and reloaded.title == ORIGINAL_TITLE
    assert (await _committed(engine)).title == ORIGINAL_TITLE


async def test_commit_false_failure_leaves_the_rollback_to_the_caller(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await _user(db, OWNER)
    real_rollback = db.rollback
    rollbacks: list[str] = []

    async def failing_flush(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("driver detail")

    async def rollback() -> None:
        rollbacks.append("rollback")
        await real_rollback()

    monkeypatch.setattr(db, "flush", failing_flush)
    monkeypatch.setattr(db, "rollback", rollback)

    with pytest.raises(FileMetadataUpdateError):
        await update_file_metadata(db, DOC, owner, title="Pending", commit=False)

    assert rollbacks == []
    assert db.in_transaction()

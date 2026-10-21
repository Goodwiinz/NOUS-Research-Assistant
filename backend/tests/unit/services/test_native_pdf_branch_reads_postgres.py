"""Native read/selection serialization against isolated PostgreSQL schemas."""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.citation import Citation
from src.models.thread import Thread
from src.services.threads import message_branches, message_service
from src.services.threads.branch_reads import MessagePageMetadata
from tests.unit.services.test_message_branches_postgres import BranchHarness
from tests.unit.services.test_message_branches_postgres import (
    branch_harness as branch_harness,
)
from tests.unit.services.test_message_branches_postgres import (
    postgres_schema as postgres_schema,
)

pytestmark = pytest.mark.integration


async def test_branch_page_holds_selection_lock_until_snapshot_is_complete(
    branch_harness: BranchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = branch_harness
    async with harness.engine.begin() as connection:
        await connection.run_sync(Citation.__table__.create)
    first = await harness.accept()
    first_answer = await harness.answer(first)
    await harness.finish(first)
    second = await harness.accept()
    second_answer = await harness.answer(second)
    await harness.finish(second)

    reader_locked, release_reader, writer_attempted = (
        asyncio.Event() for _ in range(3)
    )
    authorize = message_branches.authorize_branch_thread
    reader_db: AsyncSession | None = None

    async def barrier(db: AsyncSession, **kwargs: Any) -> Any:
        if db is reader_db:
            reader_locked.set()
            await release_reader.wait()
        else:
            writer_attempted.set()
        return await authorize(db, **kwargs)

    monkeypatch.setattr(message_branches, "authorize_branch_thread", barrier)

    async def read() -> None:
        nonlocal reader_db
        async with harness.factory() as db:
            reader_db = db
            metadata = MessagePageMetadata()
            result = await message_service.list_messages(
                db,
                harness.chat,
                harness.actor,
                limit=1,
                order="desc",
                branch_metadata=metadata,
            )
            assert result is not None
            assert [row.id for row in result[0]] == [second_answer]
            assert metadata.selected_message_id == second_answer

    async def switch() -> None:
        async with harness.factory() as db:
            await message_branches.select_branch(
                db,
                actor_id=harness.actor,
                thread_id=harness.chat,
                message_id=UUID(str(first.user_message_id)),
            )

    reader = asyncio.create_task(read())
    writer: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(reader_locked.wait(), timeout=5)
        writer = asyncio.create_task(switch())
        await asyncio.wait_for(writer_attempted.wait(), timeout=5)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(writer), timeout=0.2)
        release_reader.set()
        await asyncio.wait_for(reader, timeout=5)
        await asyncio.wait_for(writer, timeout=5)
    finally:
        release_reader.set()
        await asyncio.gather(
            reader, *([writer] if writer else []), return_exceptions=True
        )

    async with harness.factory() as db:
        with pytest.raises(
            message_branches.BranchAccessError, match="selected branch changed"
        ):
            await message_service.list_messages(
                db,
                harness.chat,
                harness.actor,
                selected_message_id=second_answer,
            )
        fresh = await message_service.list_messages(db, harness.chat, harness.actor)
        assert fresh is not None
        assert [row.id for row in fresh[0]] == [
            UUID(str(first.user_message_id)),
            first_answer,
        ]


async def _row_lock_available(
    harness: BranchHarness, thread_id: UUID, mode: str
) -> bool:
    """Whether another connection can take ``FOR <mode> NOWAIT`` on the row."""
    async with harness.factory() as db:
        try:
            await db.execute(
                text(f"SELECT id FROM threads WHERE id = :id FOR {mode} NOWAIT"),
                {"id": thread_id},
            )
        except DBAPIError as error:
            if "could not obtain lock" not in str(error):
                raise
            return False
        finally:
            await db.rollback()
        return True


async def test_branch_reads_share_the_row_lock_and_still_exclude_writers(
    branch_harness: BranchHarness,
) -> None:
    harness = branch_harness
    async with harness.engine.begin() as connection:
        await connection.run_sync(Citation.__table__.create)
    accepted = await harness.accept()
    await harness.answer(accepted)
    await harness.finish(accepted)

    async with harness.factory() as reader:
        assert (
            await message_service.list_messages(reader, harness.chat, harness.actor)
            is not None
        )
        # The read is still in its transaction: another reader may proceed,
        # but a canonical writer (FOR UPDATE) must wait for the snapshot.
        assert await _row_lock_available(harness, harness.chat, "SHARE")
        assert not await _row_lock_available(harness, harness.chat, "UPDATE")
        await reader.rollback()
    assert await _row_lock_available(harness, harness.chat, "UPDATE")


async def test_unauthorized_branch_read_takes_no_row_lock(
    branch_harness: BranchHarness,
) -> None:
    harness = branch_harness
    accepted = await harness.accept()
    await harness.answer(accepted)
    await harness.finish(accepted)
    outsider = uuid4()
    async with harness.factory() as db:
        await db.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) "
                "VALUES (:id, :org, :email, 'unused', 'Other', 'User', 'USER', TRUE, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, FALSE)"
            ),
            {"id": outsider, "org": harness.org, "email": f"{outsider}@example.test"},
        )
        await db.commit()

    async with harness.factory() as intruder:
        assert (
            await message_service.list_messages(intruder, harness.chat, outsider)
            is None
        )
        assert await _row_lock_available(harness, harness.chat, "UPDATE")
        await intruder.rollback()


async def test_linear_thread_read_takes_no_row_lock(
    branch_harness: BranchHarness,
) -> None:
    harness = branch_harness
    linear = uuid4()
    async with harness.factory() as db:
        conversation = await db.scalar(
            select(Thread.conversation_id).where(Thread.id == harness.chat)
        )
        await db.execute(
            insert(Thread).values(
                id=linear, conversation_id=conversation, title="Linear"
            )
        )
        await db.commit()

    async with harness.factory() as reader:
        result = await message_service.list_messages(reader, linear, harness.actor)
        assert result is not None and result[0] == []
        assert await _row_lock_available(harness, linear, "UPDATE")
        await reader.rollback()

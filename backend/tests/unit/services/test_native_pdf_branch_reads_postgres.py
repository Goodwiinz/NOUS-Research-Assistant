"""Native read/selection serialization against isolated PostgreSQL schemas."""

import asyncio
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.citation import Citation
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

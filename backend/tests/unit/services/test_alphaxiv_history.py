"""Real canonical rows exercise PDF history, branch selection and tenant scope.

Mutation proof (2026-10-13): replacing the fixed-microsecond isoformat in
src/schemas/alphaxiv_history.py:_WireModel._selection_timestamp with default
variable precision made ``python -m pytest -q
backend/tests/unit/services/test_alphaxiv_history.py::
test_serialized_selection_orders_exact_second_before_one_microsecond`` fail:
the shipped string-comparison algorithm selected older U2/A2 instead of U3/A3.
The schema was restored byte-for-byte, then the focused test was rerun.

Delete guard mutation (2026-10-13): compiling delete_message with its
``branching is None or branching is True`` guard replaced by False inside
the test process (production file untouched) made all three
``-k generic_delete_preserves_complete_branch_history`` cases fail because
the ordinary delete returned True instead of refusing the branch mutation.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.models.agent_run import AgentRun
from src.models.chat_message import ChatMessage, MessageRole
from src.models.citation import Citation
from src.models.conversation import Conversation
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.message_attachment import MessageAttachment
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.schemas.alphaxiv_history import AlphaXivHistoryMessage
from src.services.compat import alphaxiv_history as history
from src.services.compat.alphaxiv import CompatibilityError
from src.services.threads import message_service, thread_service

ACTOR = UUID("10000000-0000-4000-8000-000000000001")
ORG = UUID("10000000-0000-4000-8000-000000000002")
WORKSPACE = UUID("10000000-0000-4000-8000-000000000003")
CONVERSATION = UUID("10000000-0000-4000-8000-000000000004")
CHAT = UUID("10000000-0000-4000-8000-000000000005")
PAPER = UUID("10000000-0000-4000-8000-000000000006")
OTHER = UUID("20000000-0000-4000-8000-000000000001")
U1, A1, U2, A2, U3, A3 = [
    UUID(f"30000000-0000-4000-8000-00000000000{index}") for index in range(1, 7)
]
START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def disable_driver_transactions(connection: Any, _record: Any) -> None:
        connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def emit_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as connection:
        for table in (
            Organization.__table__,
            User.__table__,
            Workspace.__table__,
            WorkspaceMember.__table__,
            Conversation.__table__,
            Document.__table__,
            Thread.__table__,
            ChatMessage.__table__,
            Citation.__table__,
            MessageAttachment.__table__,
            AgentRun.__table__,
        ):
            await connection.run_sync(table.create)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(
            insert(Organization).values(id=ORG, name="PDF", storage_limit_bytes=1000000)
        )
        await session.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, "
                "first_name, last_name, role, is_active, login_count, created_at, "
                "updated_at, is_deleted) VALUES (:id, :org, 'pdf@example.test', "
                "'unused', 'PDF', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, 0)"
            ),
            {"id": str(ACTOR), "org": str(ORG)},
        )
        await session.execute(
            insert(Workspace).values(
                id=WORKSPACE, name="PDF", owner_id=ACTOR, organization_id=ORG
            )
        )
        await session.execute(
            insert(Conversation).values(
                id=CONVERSATION,
                workspace_id=WORKSPACE,
                title="PDF",
                created_by_id=ACTOR,
            )
        )
        await session.execute(
            insert(Document).values(
                id=PAPER,
                organization_id=ORG,
                title='Paper "title"',
                filename="paper.pdf",
                file_path="paper.pdf",
                file_size_bytes=100,
                mime_type="application/pdf",
                document_type=DocumentType.PDF,
                processing_status=ProcessingStatus.COMPLETED,
            )
        )
        await session.execute(
            insert(Thread).values(
                id=CHAT,
                conversation_id=CONVERSATION,
                title="Evidence",
                message_branching=True,
                branch_document_id=PAPER,
                selected_message_id=A3,
            )
        )
        for index, (message_id, parent_id, role, content) in enumerate(
            (
                (U1, None, MessageRole.USER, "What is the result?"),
                (A1, U1, MessageRole.ASSISTANT, "The main result."),
                (U2, A1, MessageRole.USER, "Original followup"),
                (A2, U2, MessageRole.ASSISTANT, "Original answer"),
                (U3, A1, MessageRole.USER, "Edited followup"),
                (A3, U3, MessageRole.ASSISTANT, "See [Doc 3], [7], [10] and [2]."),
            )
        ):
            await session.execute(
                insert(ChatMessage).values(
                    id=message_id,
                    thread_id=CHAT,
                    role=role,
                    content=content,
                    parent_message_id=parent_id,
                    selected_at=START
                    + timedelta(seconds=1 if message_id in (U2, A2) else 2),
                    created_at=START + timedelta(seconds=index),
                    reasoning_summary=(
                        "A safe provider summary" if message_id == A1 else None
                    ),
                    plan_reasoning="Not an output reasoning block",
                    tool_executions=[
                        {"tool_name": "arbitrary", "result": "private data"}
                    ],
                    feedback_rating=5,
                    token_usage={"input_tokens": 9000000, "output_tokens": 10},
                )
            )
        for position, document_id, page, deleted in (
            (3, PAPER, 4, False),
            (7, OTHER, 5, False),
            (10, PAPER, 0, False),
            (2, PAPER, 2, True),
        ):
            await session.execute(
                insert(Citation).values(
                    message_id=A3,
                    document_id=document_id,
                    source_position=position,
                    page_number=page,
                    is_deleted=deleted,
                )
            )
        await session.execute(
            insert(MessageAttachment).values(
                message_id=U1, document_id=PAPER, display_name="Mandatory PDF context"
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


async def _messages(db: AsyncSession) -> list[AlphaXivHistoryMessage]:
    return await history.get_chat_messages(db, actor_id=ACTOR, chat_id=CHAT)


def _visible_path(messages: list[AlphaXivHistoryMessage]) -> list[UUID]:
    """The shipped tree follows greatest selectedAt, first response row on ties."""
    parent: UUID | None = None
    path: list[UUID] = []
    while children := [row for row in messages if row.parent_message_id == parent]:
        chosen = max(
            children,
            key=lambda row: row.model_dump(mode="json", by_alias=True)["selectedAt"],
        )
        path.append(chosen.id)
        parent = chosen.id
    return path


async def test_history_projects_stable_tree_without_a_second_transcript(
    db: AsyncSession,
) -> None:
    first = await _messages(db)
    assert first == await _messages(db)
    by_id = {row.id: row for row in first}
    assert set(by_id) == {U1, A1, U2, A2, U3, A3, history._reasoning_id(A1)}
    assert by_id[U1].parent_message_id is None
    assert by_id[A1].parent_message_id == history._reasoning_id(A1)
    assert by_id[history._reasoning_id(A1)].parent_message_id == U1
    assert by_id[U2].parent_message_id == by_id[U3].parent_message_id == A1
    assert _visible_path(first) == [U1, history._reasoning_id(A1), A1, U3, A3]
    assert {row.type for row in first} == {
        "input_text",
        "output_text",
        "output_reasoning",
    }
    assert by_id[history._reasoning_id(A1)].content == "A safe provider summary"
    assert all(row.feedback_type is None for row in first)
    assert len((await db.execute(select(ChatMessage.id))).all()) == 6
    assert not db.new and not db.dirty


async def test_citations_only_project_actual_bound_paper_page_provenance(
    db: AsyncSession,
) -> None:
    answer = next(row for row in await _messages(db) if row.id == A3)
    assert answer.content == (
        f'See <alphaxiv-paper-citation paper="{PAPER}" page="4" '
        'title="Paper &quot;title&quot;" />, [7], [10] and [2].'
    )


async def test_recent_chats_require_explicit_binding_and_real_newest_message(
    db: AsyncSession,
) -> None:
    legacy = uuid4()
    await db.execute(
        insert(Thread).values(id=legacy, conversation_id=CONVERSATION, title="Legacy")
    )
    await db.execute(
        insert(ChatMessage).values(
            thread_id=legacy, role=MessageRole.USER, content="Legacy"
        )
    )
    await db.commit()
    recent = await history.list_paper_chats(db, actor_id=ACTOR, paper_id=PAPER)
    assert [(row.id, row.title) for row in recent] == [(CHAT, "Evidence")]
    assert recent[0].newest_message == START + timedelta(seconds=5)


async def test_recent_chat_aggregate_is_scoped_to_the_selected_paper(
    db: AsyncSession,
) -> None:
    """The newest-message aggregate must not group every thread in the table."""
    statements: list[str] = []
    engine = db.bind.sync_engine  # type: ignore[union-attr]

    def capture(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        recent = await history.list_paper_chats(db, actor_id=ACTOR, paper_id=PAPER)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert [row.id for row in recent] == [CHAT]
    grouped = [sql for sql in statements if "GROUP BY" in sql.upper()]
    assert len(grouped) == 1
    inner = grouped[0].upper().split("GROUP BY")[0].rsplit("FROM CHAT_MESSAGES", 1)[1]
    assert "BRANCH_DOCUMENT_ID" in inner


async def test_selection_uses_native_transaction_and_keeps_sibling_history(
    db: AsyncSession,
) -> None:
    selected = await history.select_chat_message(
        db, actor_id=ACTOR, chat_id=CHAT, message_id=U2
    )
    assert [row.id for row in selected.selections] == [U1, A1, U2, A2]
    db.expire_all()
    assert (await db.execute(select(Thread.selected_message_id))).scalar_one() == A2
    messages = await _messages(db)
    assert _visible_path(messages) == [U1, history._reasoning_id(A1), A1, U2, A2]
    assert {U3, A3}.issubset({row.id for row in messages})
    assert len((await db.execute(select(ChatMessage.id))).all()) == 6
    by_id = {row.id: row for row in messages}
    assert by_id[A1].selected_at == by_id[history._reasoning_id(A1)].selected_at


async def test_sibling_ties_match_native_chronological_selection(
    db: AsyncSession,
) -> None:
    await db.execute(
        update(ChatMessage)
        .where(ChatMessage.id.in_((U2, U3)))
        .values(selected_at=START)
    )
    await db.commit()
    assert _visible_path(await _messages(db))[-2:] == [U2, A2]
    await history.select_chat_message(db, actor_id=ACTOR, chat_id=CHAT, message_id=U1)
    assert (await db.execute(select(Thread.selected_message_id))).scalar_one() == A2


async def test_serialized_selection_orders_exact_second_before_one_microsecond(
    db: AsyncSession,
) -> None:
    await db.execute(
        update(ChatMessage).where(ChatMessage.id == U2).values(selected_at=START)
    )
    await db.execute(
        update(ChatMessage)
        .where(ChatMessage.id == U3)
        .values(selected_at=START + timedelta(microseconds=1))
    )
    await db.commit()
    assert _visible_path(await _messages(db))[-2:] == [U3, A3]


@pytest.mark.parametrize("message_id", [A1, history._reasoning_id(A1), OTHER])
async def test_selection_accepts_only_canonical_input_text(
    db: AsyncSession, message_id: UUID
) -> None:
    with pytest.raises(CompatibilityError) as caught:
        await history.select_chat_message(
            db, actor_id=ACTOR, chat_id=CHAT, message_id=message_id
        )
    assert caught.value.status_code == 404
    assert (await db.execute(select(Thread.selected_message_id))).scalar_one() == A3


@pytest.mark.parametrize(
    "model,values",
    [
        (User, {"is_active": False}),
        (User, {"is_deleted": True}),
        (User, {"organization_id": OTHER}),
        (Organization, {"is_active": False}),
        (Organization, {"is_deleted": True}),
        (Thread, {"is_deleted": True}),
        (Thread, {"message_branching": False}),
        (Thread, {"branch_document_id": None}),
        (Conversation, {"is_deleted": True}),
        (Workspace, {"is_deleted": True}),
        (Workspace, {"owner_id": OTHER}),
        (Document, {"organization_id": OTHER}),
        (Document, {"is_deleted": True}),
        (Document, {"document_type": DocumentType.TEXT}),
    ],
)
async def test_history_and_usage_refresh_authority_and_document_binding(
    db: AsyncSession, model: Any, values: dict[str, Any]
) -> None:
    assert await _messages(db)
    await db.execute(
        update(model).values(**values).execution_options(synchronize_session=False)
    )
    await db.commit()
    for read in (history.get_chat_messages, history.get_context_window_usage):
        with pytest.raises(CompatibilityError) as caught:
            await read(db, actor_id=ACTOR, chat_id=CHAT)
        assert caught.value.status_code == 404


async def test_public_read_does_not_enumerate_or_allow_selection(
    db: AsyncSession,
) -> None:
    await db.execute(update(Workspace).values(owner_id=OTHER, is_public=True))
    await db.commit()
    assert await _messages(db)
    assert await history.list_paper_chats(db, actor_id=ACTOR, paper_id=PAPER) == []
    with pytest.raises(CompatibilityError) as caught:
        await history.select_chat_message(
            db, actor_id=ACTOR, chat_id=CHAT, message_id=U2
        )
    assert caught.value.status_code == 404


async def test_membership_is_used_for_listing_without_workspace_org_filter(
    db: AsyncSession,
) -> None:
    await db.execute(update(Workspace).values(owner_id=OTHER, organization_id=OTHER))
    await db.execute(
        insert(WorkspaceMember).values(
            workspace_id=WORKSPACE, user_id=ACTOR, role=WorkspaceRole.VIEWER
        )
    )
    await db.commit()
    assert [
        row.id
        for row in await history.list_paper_chats(db, actor_id=ACTOR, paper_id=PAPER)
    ] == [CHAT]
    assert await _messages(db)
    await db.execute(
        update(WorkspaceMember)
        .values(is_deleted=True)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    assert await history.list_paper_chats(db, actor_id=ACTOR, paper_id=PAPER) == []
    with pytest.raises(CompatibilityError):
        await _messages(db)


@pytest.mark.parametrize(
    "mode", ["missing", "deleted", "superseded", "cycle", "metadata", "role"]
)
async def test_invalid_or_incomplete_tree_never_splices_or_returns_partial_history(
    db: AsyncSession, mode: str
) -> None:
    if mode == "missing":
        await db.execute(
            update(ChatMessage)
            .where(ChatMessage.id == U1)
            .values(parent_message_id=OTHER)
        )
    elif mode == "deleted":
        await db.execute(
            update(ChatMessage).where(ChatMessage.id == U1).values(is_deleted=True)
        )
    elif mode == "superseded":
        await db.execute(
            update(ChatMessage)
            .where(ChatMessage.id == U1)
            .values(superseded_by_message_id=OTHER)
        )
    elif mode == "cycle":
        await db.execute(
            update(ChatMessage).where(ChatMessage.id == U1).values(parent_message_id=A1)
        )
    elif mode == "metadata":
        await db.execute(
            update(ChatMessage).where(ChatMessage.id == U1).values(selected_at=None)
        )
    else:
        await db.execute(
            update(ChatMessage)
            .where(ChatMessage.id == U1)
            .values(role=MessageRole.SYSTEM)
        )
    await db.commit()
    with pytest.raises(CompatibilityError) as caught:
        await _messages(db)
    assert caught.value.status_code == 409


async def test_full_tree_limit_is_an_explicit_error(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(history.message_branches, "MAX_BRANCH_MESSAGES", 3)
    with pytest.raises(CompatibilityError) as caught:
        await _messages(db)
    assert caught.value.status_code == 409


async def test_context_usage_does_not_substitute_aggregate_token_totals(
    db: AsyncSession,
) -> None:
    assert (
        await history.get_context_window_usage(db, actor_id=ACTOR, chat_id=CHAT) is None
    )


async def test_consecutive_user_messages_are_valid_after_a_failed_answer(
    db: AsyncSession,
) -> None:
    await db.execute(
        update(ChatMessage).where(ChatMessage.id == U2).values(parent_message_id=U1)
    )
    await db.commit()
    assert (
        next(row for row in await _messages(db) if row.id == U2).parent_message_id == U1
    )


@pytest.mark.parametrize("message_id", [U1, U2, A3])
async def test_generic_delete_preserves_complete_branch_history(
    db: AsyncSession, message_id: UUID
) -> None:
    before = await _messages(db)
    assert await message_service.delete_message(db, message_id, ACTOR) is None
    await db.commit()
    assert await _messages(db) == before
    assert (await db.execute(select(Thread.selected_message_id))).scalar_one() == A3


async def test_generic_delete_keeps_linear_message_behavior(db: AsyncSession) -> None:
    await db.execute(update(Thread).values(message_branching=False, message_count=6))
    await db.commit()
    assert await message_service.delete_message(db, A3, ACTOR) is True
    await db.commit()
    assert (
        await db.execute(select(ChatMessage.is_deleted).where(ChatMessage.id == A3))
    ).scalar_one() is True
    assert (await db.execute(select(Thread.message_count))).scalar_one() == 5


async def test_whole_thread_deletion_still_supports_branched_chats(
    db: AsyncSession,
) -> None:
    assert await thread_service.delete_thread(db, CHAT, ACTOR)
    await db.commit()
    assert (await db.execute(select(Thread.is_deleted))).scalar_one() is True
    with pytest.raises(CompatibilityError) as caught:
        await _messages(db)
    assert caught.value.status_code == 404

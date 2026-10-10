"""Persisted tool traces are projected by viewer trust at every emitter.

The access funnel (``workspace_access.get_thread`` / ``get_message``) also
resolves threads below a PUBLIC workspace for anyone (#1820), so before this
fix every browser-serving emitter handed a public-only viewer the same
projection as the workspace owner. Each emitter now resolves the viewer's
grant server-side (``thread_viewer_is_trusted`` / ``message_viewer_is_trusted``)
and:

* owner / member keep PII-redacted ``args`` plus raw ``result`` / ``error``;
* a viewer whose only grant is ``is_public`` gets the activity fields only;
* malformed persisted entries are dropped for both tiers.

Driven end to end through the real routes / presenters / service against an
in-memory aiosqlite database (same pattern as ``test_agent_history_deletion``)
so an eager-load regression in the access funnel — which would make trust
fail closed and strip the OWNER's traces too — is caught here, not in prod.
No app server, credentials, model calls, or external database are used.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api.agent.execute import get_thread_messages
from src.api.threads.threads import _format_message_response
from src.api.threads.workspace_routes.presenters import _message_to_response
from src.models.chat_message import ChatMessage, MessageRole
from src.models.citation import Citation
from src.models.conversation import Conversation
from src.models.message_attachment import MessageAttachment
from src.models.thread import Thread
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.schemas.chat import ChatMessageResponse, ToolExecutionActivityResponse
from src.services.agent._pii_redact import redact_tool_args
from src.services.threads import message_service, workspace_access

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

PII_ARGS = {"query": "email bob@example.com about doc", "max_results": 5}
RESULT = {"chunks": [{"text": "private organization content"}]}
ERROR = "secret internal failure"
TRACE = {
    "id": "te-1",
    "tool_name": "search_documents",
    "tool_display_name": "Search documents",
    "args": PII_ARGS,
    "status": "success",
    "result": RESULT,
    "error": ERROR,
    "duration_ms": 42,
    "future_sensitive_field": "must not pass through",
}
# Legacy / corrupt shapes persisted alongside the real trace: dropped for
# every viewer rather than reflected or allowed to 500 the response.
MALFORMED = ["opaque", {"result": "secret"}, {"tool_name": 7, "error": "x"}]

# (fixture attribute, expected trust) — the stranger's ONLY grant is is_public.
VIEWERS = [
    pytest.param("owner", True, id="owner"),
    pytest.param("member", True, id="member"),
    pytest.param("stranger", False, id="public_only"),
]


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), organization_id=uuid4())


@pytest_asyncio.fixture
async def world() -> AsyncIterator[SimpleNamespace]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for model in (
            Workspace,
            WorkspaceMember,
            Conversation,
            Thread,
            ChatMessage,
            Citation,
            MessageAttachment,
        ):
            await connection.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        owner, member, stranger = _user(), _user(), _user()
        workspace = Workspace(
            id=uuid4(),
            name="Public workspace",
            owner_id=owner.id,
            organization_id=owner.organization_id,
            is_public=True,
        )
        membership = WorkspaceMember(
            workspace_id=workspace.id, user_id=member.id, role=WorkspaceRole.VIEWER
        )
        conversation = Conversation(
            id=uuid4(),
            workspace_id=workspace.id,
            title="Synthetic",
            created_by_id=owner.id,
        )
        thread = Thread(
            id=uuid4(),
            conversation_id=conversation.id,
            title="Synthetic",
            created_by_id=owner.id,
            message_count=1,
        )
        message = ChatMessage(
            id=uuid4(),
            thread_id=thread.id,
            user_id=owner.id,
            role=MessageRole.ASSISTANT,
            content="Done.",
            tool_executions=[TRACE, *MALFORMED],
        )
        db.add_all([workspace, membership, conversation, thread, message])
        await db.commit()
        yield SimpleNamespace(
            db=db,
            owner=owner,
            member=member,
            stranger=stranger,
            thread=thread,
            message=message,
        )
    await engine.dispose()


def _only_entry(
    tool_executions: list[ToolExecutionActivityResponse] | None,
) -> ToolExecutionActivityResponse:
    """The single surviving entry: the malformed siblings must be gone."""
    assert tool_executions is not None
    assert len(tool_executions) == 1, tool_executions
    entry = tool_executions[0]
    assert entry.tool_name == "search_documents"
    assert entry.duration_ms == 42
    assert "future_sensitive_field" not in entry.model_dump()
    return entry


def _assert_projection(
    tool_executions: list[ToolExecutionActivityResponse] | None, *, trusted: bool
) -> None:
    entry = _only_entry(tool_executions)
    if trusted:
        assert entry.args == redact_tool_args(PII_ARGS)
        assert "bob@example.com" not in str(entry.args)
        assert entry.result == RESULT
        assert entry.error == ERROR
    else:
        assert entry.args is None
        assert entry.result is None
        assert entry.error is None
        assert "bob@example.com" not in entry.model_dump_json()
        assert "private organization content" not in entry.model_dump_json()


@pytest.mark.parametrize("viewer, trusted", VIEWERS)
async def test_agent_history_projects_by_viewer_trust(
    world: SimpleNamespace, viewer: str, trusted: bool
) -> None:
    user = getattr(world, viewer)
    result = await get_thread_messages(
        world.thread.id, limit=None, before=None, current_user=user, db=world.db
    )
    assert len(result.messages) == 1
    _assert_projection(result.messages[0].tool_executions, trusted=trusted)


@pytest.mark.parametrize("viewer, trusted", VIEWERS)
@pytest.mark.parametrize(
    "present",
    [_format_message_response, _message_to_response],
    ids=["legacy_threads", "workspace_presenters"],
)
async def test_message_presenters_project_by_access_grant(
    world: SimpleNamespace,
    viewer: str,
    trusted: bool,
    present: Callable[..., ChatMessageResponse],
) -> None:
    user = getattr(world, viewer)
    # The public workspace makes the row readable by everyone (#1820); the
    # eager-loaded thread -> conversation -> workspace graph is what lets the
    # presenter tell the owner from the public-only reader.
    message = await workspace_access.get_message(world.db, world.message.id, user.id)
    assert message is not None
    is_trusted = workspace_access.message_viewer_is_trusted(message, user.id)
    assert is_trusted is trusted
    _assert_projection(
        present(message, trusted=is_trusted).tool_executions, trusted=trusted
    )


async def test_presenters_default_to_the_public_projection(
    world: SimpleNamespace,
) -> None:
    # A caller that forgets to resolve trust leaks nothing — even for the owner.
    message = await workspace_access.get_message(
        world.db, world.message.id, world.owner.id
    )
    assert message is not None
    for present in (_format_message_response, _message_to_response):
        _assert_projection(present(message).tool_executions, trusted=False)


@pytest.mark.parametrize("viewer, trusted", VIEWERS)
async def test_list_messages_rows_carry_viewer_trust(
    world: SimpleNamespace, viewer: str, trusted: bool
) -> None:
    # The page query loads no ``thread``; the service must attach the one it
    # resolved so ``message_viewer_is_trusted`` does not fail closed for the
    # owner (MissingGreenlet on a lazy load under AsyncSession).
    user = getattr(world, viewer)
    listed = await message_service.list_messages(world.db, world.thread.id, user.id)
    assert listed is not None
    messages, total, _has_more = listed
    assert total == 1
    assert [
        workspace_access.message_viewer_is_trusted(m, user.id) for m in messages
    ] == [trusted]
    for present in (_format_message_response, _message_to_response):
        _assert_projection(
            present(messages[0], trusted=trusted).tool_executions, trusted=trusted
        )

"""Machine handoff child runs: creation, idempotency and authority.

Mutation checks (2026-10-29, SQLite; source restored after each, each named
test observed FAIL). The parent refusals assert the exact opaque base class
and no minted grant, because later layers raise its subclasses and would
otherwise mask a missing org or nested-parent guard:

* removing the ``parent.status != RUNNING`` guard fails
  test_parent_must_be_running;
* removing both existing-child lookups fails
  test_each_tool_call_has_its_own_child_and_replays_to_it (the replay mints
  a grant before uq_agent_runs_handoff_call answers); removing them and the
  index branch fails test_creation_is_idempotent_by_tool_call_id;
* removing the parent ``user_id`` / ``organization_id`` comparison fails
  test_another_users_parent_is_refused / test_another_orgs_parent_is_refused;
* removing the ``execution_provider != "nous"`` comparison fails
  test_a_top_level_codex_run_cannot_hand_off; removing the ``parent_run_id is
  not None`` comparison fails test_a_nested_nous_run_cannot_hand_off
  (test_a_child_cannot_hand_off_again needs both removed);
* removing the blank-brief / over-long-brief checks fails
  test_blank_brief_is_refused / test_over_long_brief_is_refused;
* dropping the ``handoff_tool_call_id`` predicate from existing_handoff_run
  fails test_each_tool_call_has_its_own_child_and_replays_to_it;
* dropping its ``user_id`` / ``organization_id`` predicate fails
  test_existing_child_lookup_is_scoped_to_its_owner;
* disabling the uq_agent_runs_handoff_call branch fails
  test_a_concurrent_creator_wins_and_its_child_is_returned; mapping every
  IntegrityError to a workspace conflict fails
  test_any_other_integrity_error_is_raised_as_is; never mapping it fails
  test_a_folder_lost_at_insert_is_a_workspace_conflict;
* dropping the empty or the >255-char tool_call_id check fails
  test_an_unusable_tool_call_id_is_refused[empty] / [over-255];
* making agent_run_service.integrity_constraint_name ignore ``__cause__``, or
  handoff ignore the driver name, fails
  test_a_folder_lost_at_insert_is_a_workspace_conflict[postgres] and
  test_a_concurrent_creator_wins_and_its_child_is_returned[postgres];
* removing the status recheck after the mint fails
  test_a_parent_that_stopped_while_minting_is_refused;
* delivery.project_terminal: forcing ``child = False``, writing the assistant
  row for a child, not storing ``handoff_reply`` or replacing run_metadata
  instead of merging it fails
  test_child_reply_is_stored_on_the_run_not_the_transcript; dropping the
  reply cap fails test_child_reply_is_capped; skipping the row for every run
  fails test_a_top_level_codex_run_still_writes_its_assistant_row. Putting
  ``assistant_message_id`` back in a child's payload is an equivalent mutant
  (validate_payload drops the None value);
* removing ``not run.parent_run_id`` in prompt_context.build_harness_prompt
  fails test_prompt_context.test_a_brief_without_a_parent_run_is_refused;
* agent_submission_service.write_handoff_resume: replacing ``if parent_run_id
  is None`` with ``if False`` fails
  test_top_level_codex_runs_write_no_resume_record; calling it above
  finalize_submission's ``if transitioned is None`` return fails
  test_two_terminal_deliveries_write_one_resume_record with IntegrityError;
  dropping the call from finalize_submission fails it and
  test_cancelling_a_queued_child_writes_its_resume_record.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import settings
from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.chat_message import ChatMessage
from src.models.collection import Collection
from src.models.harness_session import HarnessSession
from src.models.integration_grant import IntegrationGrant
from src.models.thread import Thread
from src.models.workspace import Workspace
from src.schemas.integration_context import GrantRequestCreate, IntegrationContext
from src.services.agent.agent_run_service import ActiveRunConflict
from src.services.agent.agent_submission_service import (
    HANDOFF_RESUME_KIND,
    finalize_submission,
    request_run_cancellation,
)
from src.services.agent.run_event_types import RunEventType
from src.services.harness import handoff as handoff_service
from src.services.harness.delivery import (
    HANDOFF_REPLY_CHARS,
    dispatch_pending,
    ingest_bridge_event,
    lease_commands,
)
from src.services.harness.handoff import (
    MAX_BRIEF_CHARS,
    create_handoff_run,
    existing_handoff_run,
)
from src.services.harness.runs import record_observation
from src.services.integrations.context import (
    DeviceNotConnectedToProject,
    IntegrationAccessDenied,
    create_request,
    decide_request,
    exchange_request,
)
from src.shared.enums import JobStatus
from tests.unit.services.harness.handoff_helpers import OWNER, parent_run
from tests.unit.services.harness.test_delivery import (  # noqa: F401
    delivery_tables,
    event,
)
from tests.unit.services.harness.test_runs import (  # noqa: F401
    DEVICE,
    LOCAL,
    ORG,
    PROJECT,
    THREAD,
    USER,
    WORKSPACE,
    context,
    db,
)

pytestmark = pytest.mark.unit
BRIEF = "Run the tests and fix the failures."


@pytest.fixture(autouse=True)
def bridge_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)


async def handoff(
    db: AsyncSession, parent: str, call: str = "call-1", **kw: Any
) -> str:
    return await create_handoff_run(
        db,
        parent_run_id=parent,
        current_user=kw.get("user", OWNER),
        tool_call_id=call,
        brief=kw.get("brief", BRIEF),
        device_id=kw.get("device_id", DEVICE),
        workspace_id=kw.get("workspace_id", LOCAL),
    )


async def refused_before_minting(db: AsyncSession, parent: str, **kw: Any) -> None:
    """The parent check refuses with the opaque base denial, before any grant
    is minted (later layers raise subclasses and would mask a missing guard)."""
    grants = await db.scalar(select(func.count()).select_from(IntegrationGrant))
    with pytest.raises(IntegrationAccessDenied) as raised:
        await handoff(db, parent, **kw)
    assert type(raised.value) is IntegrationAccessDenied
    assert await db.scalar(select(func.count()).select_from(IntegrationGrant)) == grants


async def children(db: AsyncSession, parent: str) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.parent_run_id == parent)
    )
    return int(count or 0)


async def test_creates_a_queued_child_with_session_outbox_and_event(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child_id = await handoff(db, parent)
    child = await db.get(AgentRun, child_id, populate_existing=True)
    assert child is not None
    assert (child.parent_run_id, child.execution_provider, child.status) == (
        parent,
        "codex",
        JobStatus.QUEUED.value,
    )
    assert child.thread_id == THREAD and child.user_message_id is None
    assert child.project_id == PROJECT
    assert (child.user_id, child.organization_id) == (USER, ORG)
    assert child.run_metadata == {"handoff_brief": BRIEF}
    assert child.handoff_tool_call_id == "call-1"
    session = await db.scalar(
        select(HarnessSession).where(HarnessSession.run_id == child_id)
    )
    assert session is not None and session.workspace_locked
    assert (session.device_id, session.workspace_id) == (DEVICE, LOCAL)
    grant = await db.get(IntegrationGrant, session.grant_id)
    assert grant is not None
    assert (grant.user_id, grant.project_id, grant.thread_id) == (
        USER,
        PROJECT,
        THREAD,
    )
    outbox = (
        await db.scalars(select(AgentOutbox).where(AgentOutbox.run_id == child_id))
    ).all()
    assert [row.kind for row in outbox] == ["harness.execute"]
    assert outbox[0].payload["parent_run_id"] == parent
    events = (
        await db.scalars(
            select(AgentRunEvent.event_type).where(AgentRunEvent.run_id == child_id)
        )
    ).all()
    assert events == ["run.created"]
    # The parent keeps its own single-writer slot and status.
    parent_row = await db.get(AgentRun, parent, populate_existing=True)
    assert parent_row is not None and parent_row.status == JobStatus.RUNNING.value


async def test_creation_is_idempotent_by_tool_call_id(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    assert await handoff(db, parent) == await handoff(db, parent)
    assert await children(db, parent) == 1


async def test_replay_returns_the_child_after_the_parent_was_parked(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child = await handoff(db, parent)
    await db.execute(
        update(AgentRun)
        .where(AgentRun.job_id == parent)
        .values(status=JobStatus.DELEGATED.value)
    )
    await db.commit()
    assert await handoff(db, parent) == child


async def test_parent_must_be_running(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db, JobStatus.AWAITING_CONFIRMATION)
    await refused_before_minting(db, parent)
    assert await children(db, parent) == 0


async def test_unknown_parent_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await refused_before_minting(db, str(uuid4()))


async def test_another_users_parent_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    stranger = SimpleNamespace(id=uuid4(), organization_id=ORG)
    await refused_before_minting(db, parent, user=stranger)
    assert await children(db, parent) == 0


async def test_another_orgs_parent_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    elsewhere = SimpleNamespace(id=USER, organization_id=uuid4())
    await refused_before_minting(db, parent, user=elsewhere)
    assert await children(db, parent) == 0


async def test_a_child_cannot_hand_off_again(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child = await handoff(db, parent)
    # Even a running child is not a NOUS top-level run.
    await db.execute(
        update(AgentRun)
        .where(AgentRun.job_id == child)
        .values(status=JobStatus.RUNNING.value)
    )
    await db.commit()
    await refused_before_minting(db, child, call="call-2", workspace_id=uuid4())
    assert await children(db, child) == 0


async def test_a_nested_nous_run_cannot_hand_off(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child = await handoff(db, parent)
    await db.execute(
        update(AgentRun)
        .where(AgentRun.job_id == child)
        .values(status=JobStatus.RUNNING.value, execution_provider="nous")
    )
    await db.commit()
    await refused_before_minting(db, child, call="call-2", workspace_id=uuid4())
    assert await children(db, child) == 0


async def test_a_top_level_codex_run_cannot_hand_off(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    await db.execute(
        update(AgentRun)
        .where(AgentRun.job_id == parent)
        .values(execution_provider="codex")
    )
    await db.commit()
    await refused_before_minting(db, parent)
    assert await children(db, parent) == 0


async def test_blank_brief_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    await refused_before_minting(db, parent, brief="   \n")
    assert await children(db, parent) == 0


@pytest.mark.parametrize("call", ["", "c" * 256], ids=["empty", "over-255"])
async def test_an_unusable_tool_call_id_is_refused(
    db: AsyncSession, context: IntegrationContext, call: str
) -> None:
    """agent_runs.handoff_tool_call_id is VARCHAR(255); PostgreSQL would raise
    a DataError that SQLite never shows, so the id is refused up front."""
    parent = await parent_run(db)
    await refused_before_minting(db, parent, call=call)
    assert await children(db, parent) == 0
    assert await handoff(db, parent, "c" * 255)


async def test_over_long_brief_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    """The user approves exactly the brief the card shows: never truncated."""
    parent = await parent_run(db)
    await refused_before_minting(db, parent, brief="x" * (MAX_BRIEF_CHARS + 1))
    assert await children(db, parent) == 0
    assert await handoff(db, parent, brief="x" * MAX_BRIEF_CHARS)


async def test_thread_without_a_project_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    await db.execute(
        update(Thread).where(Thread.id == THREAD).values(source_project_id=None)
    )
    await db.commit()
    with pytest.raises(ValueError):
        await handoff(db, parent)
    assert await children(db, parent) == 0


async def test_a_disabled_bridge_refuses(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = await parent_run(db)
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", False)
    with pytest.raises(ValueError):
        await handoff(db, parent)
    assert await children(db, parent) == 0


async def test_folder_of_another_project_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    other_project, other_folder = uuid4(), uuid4()
    db.add(Collection(id=other_project, workspace_id=WORKSPACE, name="other"))
    await db.flush()
    db.add(
        WorkspaceBinding(
            device_id=DEVICE,
            workspace_id=other_folder,
            project_id=other_project,
            label="other",
        )
    )
    await db.commit()
    parent = await parent_run(db)
    with pytest.raises(DeviceNotConnectedToProject):
        await handoff(db, parent, workspace_id=other_folder)
    assert await children(db, parent) == 0


async def test_unknown_folder_or_device_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    with pytest.raises(DeviceNotConnectedToProject):
        await handoff(db, parent, workspace_id=uuid4())
    with pytest.raises(DeviceNotConnectedToProject):
        await handoff(db, parent, device_id=uuid4())
    assert await children(db, parent) == 0


async def test_locked_folder_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    first = await parent_run(db)
    await handoff(db, first)
    # A second parent cannot run on the same thread; use the same parent with
    # a new call id to target the still-locked folder.
    with pytest.raises(ActiveRunConflict):
        await handoff(db, first, call="call-2")
    assert await children(db, first) == 1


async def grant_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(IntegrationGrant)) or 0)


async def outbox_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(AgentOutbox)) or 0)


async def second_project_folder(db: AsyncSession) -> UUID:
    """Another free folder on DEVICE for the chat's own project."""
    folder = uuid4()
    db.add(
        WorkspaceBinding(
            device_id=DEVICE, workspace_id=folder, project_id=PROJECT, label="repo2"
        )
    )
    await db.commit()
    return folder


async def test_each_tool_call_has_its_own_child_and_replays_to_it(
    db: AsyncSession, context: IntegrationContext
) -> None:
    folder = await second_project_folder(db)
    parent = await parent_run(db)
    first = await handoff(db, parent, "call-1")
    second = await handoff(db, parent, "call-2", workspace_id=folder)
    assert first != second
    grants = await grant_count(db)
    assert await handoff(db, parent, "call-2", workspace_id=folder) == second
    # A replay of call-1 with an edited target still returns call-1's child.
    assert await handoff(db, parent, "call-1", workspace_id=folder) == first
    assert await grant_count(db) == grants
    assert await children(db, parent) == 2


async def test_replay_after_the_child_finished_returns_it(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child = await handoff(db, parent)
    done = await record_observation(db, run_id=UUID(child), observation="completed")
    assert done.status == "completed" and not done.workspace_locked
    for status in (JobStatus.DELEGATED, JobStatus.RUNNING):
        await db.execute(
            update(AgentRun)
            .where(AgentRun.job_id == parent)
            .values(status=status.value)
        )
        await db.commit()
    grants, rows = await grant_count(db), await outbox_count(db)
    assert await handoff(db, parent) == child
    assert (await grant_count(db), await outbox_count(db)) == (grants, rows)
    assert await children(db, parent) == 1


async def test_existing_child_lookup_is_scoped_to_its_owner(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child = await handoff(db, parent)
    key = {"parent_run_id": parent, "tool_call_id": "call-1"}
    assert (
        await existing_handoff_run(db, **key, user_id=USER, organization_id=ORG)
        == child
    )
    assert (
        await existing_handoff_run(db, **key, user_id=uuid4(), organization_id=ORG)
        is None
    )
    assert (
        await existing_handoff_run(db, **key, user_id=USER, organization_id=uuid4())
        is None
    )


async def test_another_users_device_and_folder_are_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    """Same organization, the other user's own consent: still not ours."""
    other, other_ws, other_project = uuid4(), uuid4(), uuid4()
    other_device, own_folder, rogue_folder = uuid4(), uuid4(), uuid4()
    await db.execute(
        text(
            "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, 'other@example.test', 'unused', 'Other', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
        ),
        {"id": str(other), "org": str(ORG)},
    )
    for row in (
        Workspace(id=other_ws, name="o", owner_id=other, organization_id=ORG),
        Collection(id=other_project, workspace_id=other_ws, name="op"),
        BridgeDevice(id=other_device, user_id=other, organization_id=ORG, label="o"),
    ):
        db.add(row)
        await db.flush()
    db.add_all(
        [
            WorkspaceBinding(
                device_id=other_device,
                workspace_id=own_folder,
                project_id=other_project,
                label="theirs",
            ),
            # A folder on their device naming our project.
            WorkspaceBinding(
                device_id=other_device,
                workspace_id=rogue_folder,
                project_id=PROJECT,
                label="rogue",
            ),
        ]
    )
    await db.commit()
    them = SimpleNamespace(id=other, organization_id=ORG)
    req = await create_request(
        db,
        them,
        GrantRequestCreate(
            project_id=other_project, device_id=other_device, scopes={"harness:execute"}
        ),
    )
    await decide_request(db, them, req.id, True)
    await exchange_request(db, them, req.id)
    parent = await parent_run(db)
    grants = await grant_count(db)
    for call, folder in (("call-1", own_folder), ("call-2", rogue_folder)):
        with pytest.raises(IntegrationAccessDenied):
            await handoff(db, parent, call, device_id=other_device, workspace_id=folder)
    assert await grant_count(db) == grants
    assert await children(db, parent) == 0


@pytest.mark.parametrize("driver", ["sqlite", "postgres"])
async def test_a_concurrent_creator_wins_and_its_child_is_returned(
    db: AsyncSession,
    context: IntegrationContext,
    monkeypatch: pytest.MonkeyPatch,
    driver: str,
) -> None:
    """The lookup misses (the winner had not committed when this call read),
    so the insert hits uq_agent_runs_handoff_call: return the winner.

    ``postgres`` re-raises the real SQLite collision in asyncpg's shape (the
    index name only on the ``__cause__`` driver error, a message that names
    nothing), so the name path is what recognises it."""
    folder = await second_project_folder(db)
    parent = await parent_run(db)
    winner = await handoff(db, parent)
    if driver == "postgres":
        real_flush = db.flush

        async def flush(*args: Any, **kwargs: Any) -> None:
            try:
                await real_flush(*args, **kwargs)
            except IntegrityError as exc:
                if "handoff_tool_call_id" not in str(exc.orig):
                    raise
                raise _integrity_error("x", constraint="uq_agent_runs_handoff_call")

        monkeypatch.setattr(db, "flush", flush)
    lookups: list[str | None] = []
    real = handoff_service.existing_handoff_run

    async def misses_before_insert(*args: Any, **kwargs: Any) -> str | None:
        found = None if len(lookups) < 2 else await real(*args, **kwargs)
        lookups.append(found)
        return found

    monkeypatch.setattr(handoff_service, "existing_handoff_run", misses_before_insert)
    rows = await outbox_count(db)
    assert await handoff(db, parent, workspace_id=folder) == winner
    assert lookups == [None, None, winner]
    assert await children(db, parent) == 1
    assert await outbox_count(db) == rows
    # The losing call's run grant was committed by the mint and is never
    # attached to a session (the chat path behaves the same way).
    assert (
        await db.scalar(
            select(func.count())
            .select_from(HarnessSession)
            .where(HarnessSession.workspace_id == folder)
        )
        == 0
    )


async def test_a_parent_that_stopped_while_minting_is_refused(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mint commits and so releases the parent lock; the status is
    rechecked under a fresh lock before the child is written."""
    parent = await parent_run(db)
    real = handoff_service.create_chat_context

    async def cancelled_meanwhile(*args: Any, **kwargs: Any) -> Any:
        minted = await real(*args, **kwargs)
        await db.execute(
            update(AgentRun)
            .where(AgentRun.job_id == parent)
            .values(status=JobStatus.CANCELLED.value)
        )
        await db.commit()
        return minted

    monkeypatch.setattr(handoff_service, "create_chat_context", cancelled_meanwhile)
    with pytest.raises(IntegrationAccessDenied):
        await handoff(db, parent)
    assert await children(db, parent) == 0


class AsyncpgUniqueViolationError(Exception):
    """asyncpg's driver error: it alone carries ``constraint_name``."""

    def __init__(self, constraint: str) -> None:
        super().__init__("duplicate key value violates unique constraint")
        self.constraint_name = constraint


class AdaptedIntegrityError(Exception):
    """SQLAlchemy's asyncpg adapter error (``IntegrityError.orig``): no name of
    its own; the driver error is its ``__cause__``."""


def _integrity_error(message: str, constraint: str | None = None) -> IntegrityError:
    orig: Exception = sqlite3.IntegrityError(message)
    if constraint is not None:
        orig = AdaptedIntegrityError(f"<class 'UniqueViolationError'>: {message}")
        orig.__cause__ = AsyncpgUniqueViolationError(constraint)
    return IntegrityError("INSERT", {}, orig)


@pytest.mark.parametrize(
    "error",
    [
        _integrity_error(
            "UNIQUE constraint failed: "
            "harness_sessions.device_id, harness_sessions.workspace_id"
        ),
        _integrity_error("x", constraint="uq_harness_active_workspace"),
    ],
    ids=["sqlite", "postgres"],
)
async def test_a_folder_lost_at_insert_is_a_workspace_conflict(
    db: AsyncSession,
    context: IntegrationContext,
    monkeypatch: pytest.MonkeyPatch,
    error: IntegrityError,
) -> None:
    async def lost(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(handoff_service, "reserve_workspace", lost)
    parent = await parent_run(db)
    with pytest.raises(ActiveRunConflict):
        await handoff(db, parent)
    assert await children(db, parent) == 0


@pytest.mark.parametrize(
    "error",
    [
        _integrity_error(
            "UNIQUE constraint failed: agent_outbox.run_id, agent_outbox.kind"
        ),
        _integrity_error("x", constraint="uq_agent_outbox_run_kind"),
    ],
    ids=["sqlite", "postgres"],
)
async def test_any_other_integrity_error_is_raised_as_is(
    db: AsyncSession,
    context: IntegrationContext,
    monkeypatch: pytest.MonkeyPatch,
    error: IntegrityError,
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(handoff_service, "insert_outbox", broken)
    parent = await parent_run(db)
    with pytest.raises(IntegrityError) as raised:
        await handoff(db, parent)
    assert raised.value is error
    assert await children(db, parent) == 0


async def test_a_chat_no_longer_available_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    await db.execute(update(Thread).where(Thread.id == THREAD).values(is_deleted=True))
    await db.commit()
    with pytest.raises(ValueError, match="no longer available"):
        await handoff(db, parent)
    assert await children(db, parent) == 0


async def run_child_to(
    db: AsyncSession,
    context: IntegrationContext,
    child_id: str,
    state: str,
    deltas: tuple[str, ...] = ("answer",),
) -> None:
    """Drive the child through the fake bridge to ``state``."""
    assert await dispatch_pending(db) == 1
    (command,) = await lease_commands(db, context, DEVICE)
    assert str(command.runId) == child_id

    def observe(seq: int, value: str) -> Any:
        return event(
            command,
            seq,
            body={
                "kind": "observation",
                "state": value,
                "sessionId": "n",
                "turnId": "t",
            },
        )

    await ingest_bridge_event(db, context, observe(1, "running"))
    for seq, text_ in enumerate(deltas, start=2):
        await ingest_bridge_event(
            db,
            context,
            event(
                command,
                seq,
                body={
                    "kind": "event",
                    "eventType": "assistant.delta",
                    "payload": {"text": text_},
                },
            ),
        )
    await ingest_bridge_event(db, context, observe(2 + len(deltas), state))


@pytest.fixture
def assistant_writer(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the transcript writer to this test database, so a wrongly
    written assistant row is visible here."""
    from src.services.agent import agent_execution_service as execution

    monkeypatch.setattr(
        execution,
        "AsyncSessionLocal",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )


async def assistant_rows(db: AsyncSession, run_id: str) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .where(ChatMessage.client_message_id == run_id)
    )
    return int(count or 0)


@pytest.mark.parametrize(
    ("state", "status", "reply"),
    [
        ("completed", JobStatus.COMPLETED, "answer"),
        ("failed", JobStatus.FAILED, "answer"),
        ("interrupted", JobStatus.CANCELLED, "answer"),
    ],
)
async def test_child_reply_is_stored_on_the_run_not_the_transcript(
    db: AsyncSession,
    context: IntegrationContext,
    assistant_writer: None,
    state: str,
    status: JobStatus,
    reply: str,
) -> None:
    parent = await parent_run(db)
    child_id = await handoff(db, parent)
    await run_child_to(db, context, child_id, state)
    db.expire_all()
    child = await db.get(AgentRun, child_id)
    assert child is not None and child.status == status.value
    assert child.run_metadata["handoff_reply"] == reply
    # Merged, not replaced: the brief and the prompt provenance survive.
    assert child.run_metadata["handoff_brief"] == BRIEF
    assert child.run_metadata["harness_prompt"]["source_digest"]
    assert await assistant_rows(db, child_id) == 0
    assert await assistant_rows(db, parent) == 0
    terminal = await db.scalar(
        select(AgentRunEvent)
        .where(AgentRunEvent.run_id == child_id)
        .order_by(AgentRunEvent.seq.desc())
        .limit(1)
    )
    assert terminal is not None
    assert "assistant_message_id" not in (terminal.payload or {})


async def test_child_without_output_stores_the_fallback_reply(
    db: AsyncSession, context: IntegrationContext, assistant_writer: None
) -> None:
    child_id = await handoff(db, await parent_run(db))
    await run_child_to(db, context, child_id, "failed", deltas=())
    db.expire_all()
    child = await db.get(AgentRun, child_id)
    assert child is not None
    assert child.run_metadata["handoff_reply"] == "External execution failed."
    assert await assistant_rows(db, child_id) == 0


async def test_child_reply_is_capped(
    db: AsyncSession, context: IntegrationContext, assistant_writer: None
) -> None:
    chunk = "y" * 1500
    child_id = await handoff(db, await parent_run(db))
    await run_child_to(db, context, child_id, "completed", deltas=(chunk,) * 3)
    db.expire_all()
    child = await db.get(AgentRun, child_id)
    assert child is not None
    assert child.run_metadata["handoff_reply"] == ("y" * 4500)[:HANDOFF_REPLY_CHARS]
    assert HANDOFF_REPLY_CHARS == 4000


async def test_a_top_level_codex_run_still_writes_its_assistant_row(
    db: AsyncSession, context: IntegrationContext, assistant_writer: None
) -> None:
    from tests.unit.services.harness.test_runs import accept

    accepted = await accept(db, context)
    await run_child_to(db, context, accepted.run_id, "completed")
    db.expire_all()
    run = await db.get(AgentRun, accepted.run_id)
    assert run is not None and run.status == JobStatus.COMPLETED.value
    assert "handoff_reply" not in (run.run_metadata or {})
    assert await assistant_rows(db, accepted.run_id) == 1


async def resume_rows(db: AsyncSession, child_id: str) -> list[dict]:
    return list(
        (
            await db.scalars(
                select(AgentOutbox.payload).where(
                    AgentOutbox.run_id == child_id,
                    AgentOutbox.kind == HANDOFF_RESUME_KIND,
                )
            )
        ).all()
    )


async def test_two_terminal_deliveries_write_one_resume_record(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child_id = await handoff(db, parent)
    for _ in range(2):
        await finalize_submission(
            db,
            run_id=child_id,
            status=JobStatus.FAILED,
            organization_id=ORG,
            event_type=RunEventType.RUN_FAILED,
            payload={"code": "harness_failed", "message": "usage limit"},
            provider="codex",
        )
    assert await resume_rows(db, child_id) == [{"parent_run_id": parent}]


async def test_cancelling_a_queued_child_writes_its_resume_record(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child_id = await handoff(db, parent)
    result = await request_run_cancellation(
        db,
        run_id=child_id,
        thread_id=THREAD,
        organization_id=ORG,
        user_id=USER,
        reason="user_requested",
    )
    await db.commit()
    assert result is not None and result.status is JobStatus.CANCELLED
    assert await resume_rows(db, child_id) == [{"parent_run_id": parent}]


async def test_top_level_codex_runs_write_no_resume_record(
    db: AsyncSession, context: IntegrationContext
) -> None:
    from tests.unit.services.harness.test_runs import accept

    accepted = await accept(db, context)
    await finalize_submission(
        db,
        run_id=accepted.run_id,
        status=JobStatus.COMPLETED,
        organization_id=ORG,
        event_type=RunEventType.RUN_COMPLETED,
        payload={},
        provider="codex",
    )
    assert await resume_rows(db, accepted.run_id) == []

"""Durable, fail-closed note actions shared by native NOUS and external harnesses."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.collection import Collection
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.project_note import ProjectNote
from src.models.research_project import ResearchProject
from src.models.research_project_role import ResearchProjectRoleAssignment
from src.models.tool_action import IntegrationToolAction
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_context import STANDARD_SCOPES
from src.schemas.integration_tools import ToolInvocation
from src.services.agent import tool_actions
from src.services.agent.tool_actions import (
    STALE_EXECUTION,
    ActionActor,
    ActionConflict,
    ActionNotFound,
    ToolActionArgumentError,
    canonical_hash,
    decide_action,
    drain_integration_actions,
    execute_action,
    get_action_for_review,
    get_action_status,
    request_action,
    sweep_stale_actions,
)

pytestmark = pytest.mark.unit
USER, OTHER_USER, ORG, ORG2, PROJECT, WORKSPACE = (uuid4() for _ in range(6))
DEVICE, CONSENT, GRANT, RENEWED_GRANT, INTERNAL_GRANT = (uuid4() for _ in range(5))
NOTE_ARGS = {"title": "Findings", "content": "# Findings\n", "tags": ["a"]}
SOON = datetime.now(timezone.utc) + timedelta(hours=1)


def _grant_values(grant_id: UUID, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": grant_id,
        "user_id": USER,
        "organization_id": ORG,
        "project_id": PROJECT,
        "device_id": DEVICE,
        "request_id": CONSENT,
        "scopes": ["tools:write"],
        "token_hash": str(grant_id).replace("-", "") * 2,
        "expires_at": SOON,
        "consented_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return values


async def _seed(session: AsyncSession) -> None:
    for org_id in (ORG, ORG2):
        await session.execute(
            insert(Organization).values(
                id=org_id, name=str(org_id), storage_limit_bytes=1000000
            )
        )
    for user_id, email in ((USER, "owner@example.test"), (OTHER_USER, "o@e.test")):
        await session.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
            ),
            {"id": str(user_id), "org": str(ORG), "email": email},
        )
    await session.execute(
        insert(Workspace).values(
            id=WORKSPACE, name="Workspace", owner_id=USER, organization_id=ORG
        )
    )
    await session.execute(
        insert(Collection).values(id=PROJECT, name="Project", workspace_id=WORKSPACE)
    )
    await session.execute(
        insert(IntegrationGrantRequest).values(
            id=CONSENT,
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT,
            device_id=DEVICE,
            scopes=["tools:read", "tools:write"],
            status="consumed",
            expires_at=SOON,
            approved_at=datetime.now(timezone.utc),
        )
    )
    await session.execute(insert(IntegrationGrant).values(**_grant_values(GRANT)))
    await session.execute(
        insert(IntegrationGrant).values(**_grant_values(RENEWED_GRANT))
    )
    await session.execute(
        insert(IntegrationGrant).values(
            **_grant_values(INTERNAL_GRANT, device_id=None, request_id=None)
        )
    )
    await session.commit()


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[Any]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'actions.db'}")
    tables: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
        ResearchProject,
        ResearchProjectRoleAssignment,
        ProjectNote,
        IntegrationGrantRequest,
        IntegrationGrant,
        IntegrationToolAction,
    ]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await _seed(session)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db(engine: Any) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_actions.settings, "NOUS_MCP_ENABLED", True)


def _actor(
    grant_id: UUID | None = GRANT,
    consent_id: UUID | None = CONSENT,
    user_id: UUID = USER,
    organization_id: UUID = ORG,
) -> ActionActor:
    return ActionActor(
        user_id=user_id,
        organization_id=organization_id,
        project_id=PROJECT,
        grant_id=grant_id,
        consent_id=consent_id,
    )


NATIVE = _actor(grant_id=None, consent_id=None)
INTERNAL = _actor(grant_id=INTERNAL_GRANT, consent_id=None)


def _invocation(invocation_id: UUID | None = None, **args: Any) -> ToolInvocation:
    return ToolInvocation(
        tool_name="create_project_note",
        arguments={**NOTE_ARGS, **args},
        invocation_id=invocation_id or uuid4(),
    )


async def _user(db: AsyncSession, user_id: UUID = USER) -> User:
    user = await db.get(User, user_id)
    assert user is not None
    return cast(User, user)


async def _note_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(ProjectNote)) or 0)


async def _row_count(db: AsyncSession) -> int:
    return int(
        await db.scalar(select(func.count()).select_from(IntegrationToolAction)) or 0
    )


async def _approve(db: AsyncSession, invocation_id: UUID) -> None:
    await decide_action(db, await _user(db), invocation_id, approved=True)


async def _approved_action(db: AsyncSession, actor: ActionActor | None = None) -> UUID:
    status = await request_action(db, actor or _actor(), _invocation())
    await _approve(db, status.invocation_id)
    return status.invocation_id


async def _last_error(db: AsyncSession, invocation_id: UUID) -> str | None:
    return cast(
        str | None,
        await db.scalar(
            select(IntegrationToolAction.last_error).where(
                IntegrationToolAction.invocation_id == invocation_id
            )
        ),
    )


# --- request ---------------------------------------------------------------


async def test_request_is_idempotent_and_a_changed_payload_conflicts(
    db: AsyncSession,
) -> None:
    invocation = _invocation()
    first = await request_action(db, _actor(), invocation)
    assert first.state == "awaiting_approval"
    again = await request_action(db, _actor(), invocation)
    assert again.invocation_id == first.invocation_id
    assert await _row_count(db) == 1
    with pytest.raises(ActionConflict):
        await request_action(
            db, _actor(), _invocation(invocation.invocation_id, title="Changed")
        )


async def test_renewed_grant_replays_the_same_consent_but_another_consent_conflicts(
    db: AsyncSession,
) -> None:
    invocation = _invocation()
    await request_action(db, _actor(), invocation)
    renewed = await request_action(db, _actor(grant_id=RENEWED_GRANT), invocation)
    assert renewed.state == "awaiting_approval"
    with pytest.raises(ActionConflict):
        await request_action(db, _actor(consent_id=uuid4()), invocation)
    with pytest.raises(ActionConflict):
        await request_action(db, INTERNAL, invocation)
    assert await _row_count(db) == 1


async def test_replay_with_a_different_server_binding_conflicts(
    db: AsyncSession,
) -> None:
    invocation = _invocation()
    await request_action(db, NATIVE, invocation)
    for actor in (
        ActionActor(user_id=USER, organization_id=ORG, project_id=uuid4()),
        ActionActor(
            user_id=USER, organization_id=ORG, project_id=PROJECT, thread_id=uuid4()
        ),
        ActionActor(
            user_id=USER, organization_id=ORG, project_id=PROJECT, run_id=uuid4()
        ),
        ActionActor(
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT,
            workspace_id=WORKSPACE,
        ),
    ):
        with pytest.raises(ActionConflict):
            await request_action(db, actor, invocation)
    assert (await request_action(db, NATIVE, invocation)).state == "awaiting_approval"
    assert await _row_count(db) == 1


async def test_concurrent_identical_requests_replay_one_row(
    engine: Any, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation = _invocation()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as one, maker() as two:
        results = await asyncio.gather(
            request_action(one, _actor(), invocation),
            request_action(two, _actor(), invocation),
        )
    assert {r.state for r in results} == {"awaiting_approval"}
    assert {r.invocation_id for r in results} == {invocation.invocation_id}
    assert await _row_count(db) == 1

    # SQLite serializes the writers above, so force the true interleaving:
    # the loser's existence check ran before the winner committed.
    real_scalar = db.scalar
    calls = {"n": 0}

    async def stale_first_read(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_scalar(*args, **kwargs)

    monkeypatch.setattr(db, "scalar", stale_first_read)
    replay = await request_action(db, _actor(), invocation)
    assert replay.invocation_id == invocation.invocation_id
    assert replay.state == "awaiting_approval"
    assert calls["n"] >= 2  # the insert lost and the row was re-read
    assert await _row_count(db) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {**NOTE_ARGS, "project_id": str(uuid4())},
        {**NOTE_ARGS, "user_id": str(uuid4())},
        {**NOTE_ARGS, "organization_id": str(uuid4())},
        {**NOTE_ARGS, "thread_id": str(uuid4())},
        {**NOTE_ARGS, "foo": 1},
        {**NOTE_ARGS, "title": ""},
        {**NOTE_ARGS, "title": "   "},
        {**NOTE_ARGS, "title": "x" * 256},
        {**NOTE_ARGS, "content": ""},
        {**NOTE_ARGS, "content": "x" * 200_001},
        {**NOTE_ARGS, "tags": "a"},
        {**NOTE_ARGS, "tags": [""]},
        {**NOTE_ARGS, "tags": ["x" * 65]},
        {**NOTE_ARGS, "tags": [1]},
        {**NOTE_ARGS, "tags": [f"t{i}" for i in range(21)]},
        {"content": "c"},
    ],
    ids=[
        "project_id",
        "user_id",
        "organization_id",
        "thread_id",
        "unknown-key",
        "empty-title",
        "blank-title",
        "long-title",
        "empty-content",
        "long-content",
        "tags-string",
        "tag-empty",
        "tag-long",
        "tag-int",
        "too-many-tags",
        "missing-title",
    ],
)
async def test_invalid_note_arguments_are_rejected_without_a_row(
    db: AsyncSession, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ToolActionArgumentError):
        await request_action(
            db,
            _actor(),
            ToolInvocation(
                tool_name="create_project_note",
                arguments=arguments,
                invocation_id=uuid4(),
            ),
        )
    assert await _row_count(db) == 0


async def test_other_tools_are_not_actions(db: AsyncSession) -> None:
    with pytest.raises(ToolActionArgumentError):
        await request_action(
            db,
            _actor(),
            ToolInvocation(
                tool_name="forget_memory", arguments={}, invocation_id=uuid4()
            ),
        )


def test_canonical_hash_is_key_order_independent_and_tag_order_sensitive() -> None:
    assert canonical_hash("t", {"b": 1, "a": 2}) == canonical_hash(
        "t", {"a": 2, "b": 1}
    )
    assert canonical_hash("t", {"tags": ["a", "b"]}) != canonical_hash(
        "t", {"tags": ["b", "a"]}
    )
    # Pinned: a serialization change would turn every in-flight replay into a 409.
    assert (
        canonical_hash("create_project_note", NOTE_ARGS)
        == "00a5154393874069399bf67465b173839a3e66f20a100749cb771b201cba36be"
    )


# --- action catalogue and argument validators ------------------------------

TARGET, DOC, DOC2 = uuid4(), uuid4(), uuid4()
P, D = str(PROJECT), str(DOC)  # ids as a harness sends them: JSON strings
# The smallest valid arguments of every action.
MINIMAL_ARGUMENTS: dict[str, dict[str, Any]] = {
    "create_project_note": NOTE_ARGS,
    "save_papers_to_folder": {"document_ids": [D], "project_id": P},
    "remove_papers_from_folder": {"document_ids": [D], "project_id": P},
    "move_papers_between_folders": {
        "document_ids": [D],
        "from_project_id": P,
        "to_project_id": str(TARGET),
    },
    "create_folder": {"name": "Reading"},
    "rename_folder": {"project_id": P, "name": "Read"},
    "delete_folder": {"project_id": P},
    "update_document_metadata": {"document_id": D, "title": "New"},
    "ingest_arxiv_papers": {"paper_ids": ["2401.00001"]},
}
LIBRARY_ACTIONS = sorted(set(MINIMAL_ARGUMENTS) - {"create_project_note"})
IDENTITY = (
    "user_id",
    "organization_id",
    "workspace_id",
    "thread_id",
    "run_id",
    "grant_id",
    "consent_id",
)


def test_catalogue_names_each_actions_scope_and_whether_it_auto_runs() -> None:
    assert tool_actions.ALLOWED_ACTIONS == set(MINIMAL_ARGUMENTS)
    # Reversible library changes may run without a per-action decision. A
    # note, a deletion and an ingest (which writes object storage outside the
    # action's transaction) always wait for one.
    assert tool_actions.AUTO_RUN_ACTIONS == {
        "save_papers_to_folder",
        "remove_papers_from_folder",
        "move_papers_between_folders",
        "create_folder",
        "rename_folder",
        "update_document_metadata",
    }
    assert set(tool_actions.REQUIRED_SCOPE_FOR) == tool_actions.ALLOWED_ACTIONS
    by_scope: dict[str, set[str]] = {}
    for name, scope in tool_actions.REQUIRED_SCOPE_FOR.items():
        by_scope.setdefault(scope, set()).add(name)
    assert by_scope == {
        "tools:write": {"create_project_note", "delete_folder", "ingest_arxiv_papers"},
        "library:write": set(tool_actions.AUTO_RUN_ACTIONS),
    }
    assert set(by_scope) <= STANDARD_SCOPES  # a grant can hold each one


def test_arguments_never_carry_identity_and_may_only_select_a_project() -> None:
    assert tool_actions.IDENTITY_KEYS == set(IDENTITY)
    assert tool_actions.SELECTOR_KEYS == {
        "project_id",
        "from_project_id",
        "to_project_id",
    }
    assert not tool_actions.IDENTITY_KEYS & tool_actions.SELECTOR_KEYS


def test_scope_is_checked_per_action_not_per_route() -> None:
    write, library = {"tools:write"}, {"library:write"}
    for name in tool_actions.ALLOWED_ACTIONS:
        # tools:write may request any action, and it waits for a decision.
        assert tool_actions.scope_allows(name, write)
        assert not tool_actions.runs_without_approval(name, write)
        assert not tool_actions.scope_allows(name, {"tools:read", "library:read"})
        assert not tool_actions.scope_allows(name, set())
    for name in ("create_project_note", "delete_folder", "ingest_arxiv_papers"):
        # library:write never reaches a note, a deletion or an ingest, and
        # never lets one skip the decision.
        assert not tool_actions.scope_allows(name, library)
        assert not tool_actions.runs_without_approval(name, write | library)
    for name in tool_actions.AUTO_RUN_ACTIONS:
        assert tool_actions.scope_allows(name, library)
        assert tool_actions.runs_without_approval(name, write | library)
    # Not an action: no scope reaches it.
    assert not tool_actions.scope_allows("forget_memory", write | library)
    assert not tool_actions.runs_without_approval("forget_memory", write | library)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        *MINIMAL_ARGUMENTS.items(),
        (
            "remove_papers_from_folder",
            {"document_ids": [D, str(DOC2)], "project_id": P},
        ),
        ("create_folder", {"name": "Reading", "description": "Papers to read"}),
        ("update_document_metadata", {"document_id": D, "tags": []}),
        ("update_document_metadata", {"document_id": D, "title": "T", "tags": ["ml"]}),
        (
            "ingest_arxiv_papers",
            {"paper_ids": ["2401.00001v2", "math.GT/0309136"], "project_id": P},
        ),
    ],
    ids=[
        *MINIMAL_ARGUMENTS,
        "two-documents",
        "described-folder",
        "clear-tags",
        "title-and-tags",
        "versions-and-selector",
    ],
)
def test_validators_accept_valid_arguments_as_given(
    name: str, arguments: dict[str, Any]
) -> None:
    assert tool_actions.validate_arguments(name, arguments) == arguments


def test_validators_return_the_canonical_form_the_hash_covers() -> None:
    # One request spelled two ways hashes alike, so its replay finds the row.
    assert tool_actions.validate_arguments(
        "rename_folder", {"project_id": P.upper(), "name": "  Read  "}
    ) == {"project_id": P, "name": "Read"}
    assert tool_actions.validate_arguments(
        "save_papers_to_folder", {"document_ids": [D.upper()], "project_id": P}
    ) == {"document_ids": [D], "project_id": P}
    assert tool_actions.validate_arguments(
        "ingest_arxiv_papers", {"paper_ids": [" 2401.00001\n"]}
    ) == {"paper_ids": ["2401.00001"]}
    # An optional field sent as null is the same request as one left out.
    assert tool_actions.validate_arguments(
        "create_folder", {"name": "R", "description": None}
    ) == {"name": "R"}
    assert tool_actions.validate_arguments(
        "ingest_arxiv_papers", {"paper_ids": ["2401.00001"], "project_id": None}
    ) == {"paper_ids": ["2401.00001"]}


@pytest.mark.parametrize("key", IDENTITY)
@pytest.mark.parametrize("name", sorted(MINIMAL_ARGUMENTS))
def test_no_action_accepts_an_identity_argument(name: str, key: str) -> None:
    with pytest.raises(ToolActionArgumentError) as raised:
        tool_actions.validate_arguments(
            name, {**MINIMAL_ARGUMENTS[name], key: str(uuid4())}
        )
    assert str(raised.value) == "identity arguments are not accepted"


ONLY_METADATA = "only document_id, title and tags are accepted"
BAD_TAGS = "tags must be at most 20 strings of 1-64 characters"
BAD_DOCUMENT_IDS = "document_ids must contain 1-20 UUIDs"
BAD_PAPER_IDS = "paper_ids must contain 1-10 arXiv ids"
HEX_LOOKALIKE = 12345678901234567890123456789012
REJECTED: dict[str, tuple[str, dict[str, Any], str]] = {
    "unknown-tool": ("forget_memory", {}, "tool is not available as an action"),
    # A note always lands in the grant's own project.
    "note-selector": (
        "create_project_note",
        {**NOTE_ARGS, "project_id": P},
        "identity arguments are not accepted",
    ),
    "save-no-documents": (
        "save_papers_to_folder",
        {"document_ids": [], "project_id": P},
        BAD_DOCUMENT_IDS,
    ),
    "save-21-documents": (
        "save_papers_to_folder",
        {"document_ids": [str(uuid4()) for _ in range(21)], "project_id": P},
        BAD_DOCUMENT_IDS,
    ),
    "save-documents-string": (
        "save_papers_to_folder",
        {"document_ids": D, "project_id": P},
        BAD_DOCUMENT_IDS,
    ),
    "save-document-not-uuid": (
        "save_papers_to_folder",
        {"document_ids": ["paper"], "project_id": P},
        "document_ids must be UUIDs",
    ),
    # A JSON number of 32 decimal digits reads as hex: only strings are ids.
    "save-document-number": (
        "save_papers_to_folder",
        {"document_ids": [HEX_LOOKALIKE], "project_id": P},
        "document_ids must be UUIDs",
    ),
    "save-repeated-document": (
        "save_papers_to_folder",
        {"document_ids": [D, D.upper()], "project_id": P},
        "document_ids must not repeat an id",
    ),
    "save-no-selector": (
        "save_papers_to_folder",
        {"document_ids": [D]},
        "project_id is required",
    ),
    # Never a NAME: the agent helpers resolve any non-UUID as a project name.
    "save-selector-name": (
        "save_papers_to_folder",
        {"document_ids": [D], "project_id": "Project"},
        "project_id must be a UUID",
    ),
    "save-other-selector": (
        "save_papers_to_folder",
        {"document_ids": [D], "project_id": P, "to_project_id": P},
        "only document_ids and project_id are accepted",
    ),
    "remove-selector-number": (
        "remove_papers_from_folder",
        {"document_ids": [D], "project_id": HEX_LOOKALIKE},
        "project_id must be a UUID",
    ),
    "move-same-folder": (
        "move_papers_between_folders",
        {"document_ids": [D], "from_project_id": P, "to_project_id": P.upper()},
        "from_project_id and to_project_id must differ",
    ),
    "move-no-destination": (
        "move_papers_between_folders",
        {"document_ids": [D], "from_project_id": P},
        "to_project_id is required",
    ),
    "move-plain-selector": (
        "move_papers_between_folders",
        {"document_ids": [D], "project_id": P, "to_project_id": str(TARGET)},
        "only document_ids, from_project_id and to_project_id are accepted",
    ),
    "create-no-name": ("create_folder", {}, "name must be 1-255 characters"),
    "create-blank-name": (
        "create_folder",
        {"name": "   "},
        "name must be 1-255 characters",
    ),
    "create-long-name": (
        "create_folder",
        {"name": "x" * 256},
        "name must be 1-255 characters",
    ),
    "create-name-int": ("create_folder", {"name": 5}, "name must be 1-255 characters"),
    "create-long-description": (
        "create_folder",
        {"name": "R", "description": "x" * 2001},
        "description must be at most 2000 characters",
    ),
    "create-description-int": (
        "create_folder",
        {"name": "R", "description": 5},
        "description must be at most 2000 characters",
    ),
    "create-selector": (
        "create_folder",
        {"name": "R", "project_id": P},
        "only name and description are accepted",
    ),
    "rename-no-selector": ("rename_folder", {"name": "R"}, "project_id is required"),
    "rename-empty-name": (
        "rename_folder",
        {"project_id": P, "name": ""},
        "name must be 1-255 characters",
    ),
    "delete-no-selector": ("delete_folder", {}, "project_id is required"),
    "delete-with-name": (
        "delete_folder",
        {"project_id": P, "name": "R"},
        "only project_id is accepted",
    ),
    "metadata-nothing-to-change": (
        "update_document_metadata",
        {"document_id": D},
        "title or tags is required",
    ),
    "metadata-no-document": (
        "update_document_metadata",
        {"title": "T"},
        "document_id is required",
    ),
    "metadata-document-not-uuid": (
        "update_document_metadata",
        {"document_id": "paper", "title": "T"},
        "document_id must be a UUID",
    ),
    "metadata-blank-title": (
        "update_document_metadata",
        {"document_id": D, "title": " "},
        "title must be 1-255 characters",
    ),
    "metadata-long-title": (
        "update_document_metadata",
        {"document_id": D, "title": "x" * 256},
        "title must be 1-255 characters",
    ),
    "metadata-null-title": (
        "update_document_metadata",
        {"document_id": D, "title": None},
        "title must be 1-255 characters",
    ),
    "metadata-tags-string": (
        "update_document_metadata",
        {"document_id": D, "tags": "a"},
        BAD_TAGS,
    ),
    "metadata-tag-empty": (
        "update_document_metadata",
        {"document_id": D, "tags": [""]},
        BAD_TAGS,
    ),
    "metadata-tag-long": (
        "update_document_metadata",
        {"document_id": D, "tags": ["x" * 65]},
        BAD_TAGS,
    ),
    "metadata-tag-int": (
        "update_document_metadata",
        {"document_id": D, "tags": [1]},
        BAD_TAGS,
    ),
    "metadata-21-tags": (
        "update_document_metadata",
        {"document_id": D, "tags": [f"t{i}" for i in range(21)]},
        BAD_TAGS,
    ),
    "metadata-selector": (
        "update_document_metadata",
        {"document_id": D, "title": "T", "project_id": P},
        ONLY_METADATA,
    ),
    "ingest-no-papers": ("ingest_arxiv_papers", {"paper_ids": []}, BAD_PAPER_IDS),
    "ingest-11-papers": (
        "ingest_arxiv_papers",
        {"paper_ids": [f"2401.{i:05d}" for i in range(11)]},
        BAD_PAPER_IDS,
    ),
    "ingest-papers-string": (
        "ingest_arxiv_papers",
        {"paper_ids": "2401.00001"},
        BAD_PAPER_IDS,
    ),
    "ingest-path-traversal": (
        "ingest_arxiv_papers",
        {"paper_ids": ["../../robots.txt?x="]},
        "paper_ids contains invalid arXiv ids",
    ),
    # str(2401.00001) is a valid id, so only strings count as ids.
    "ingest-paper-float": (
        "ingest_arxiv_papers",
        {"paper_ids": [2401.00001]},
        "paper_ids contains invalid arXiv ids",
    ),
    "ingest-repeated-paper": (
        "ingest_arxiv_papers",
        {"paper_ids": ["2401.00001", " 2401.00001"]},
        "paper_ids must not repeat an id",
    ),
    "ingest-selector-name": (
        "ingest_arxiv_papers",
        {"paper_ids": ["2401.00001"], "project_id": "Project"},
        "project_id must be a UUID",
    ),
    "ingest-move-selector": (
        "ingest_arxiv_papers",
        {"paper_ids": ["2401.00001"], "to_project_id": P},
        "only paper_ids and project_id are accepted",
    ),
}


@pytest.mark.parametrize(
    ("name", "arguments", "reason"), REJECTED.values(), ids=list(REJECTED)
)
def test_validators_reject_with_a_stable_reason(
    name: str, arguments: dict[str, Any], reason: str
) -> None:
    # The reason is a public 422 detail: fixed text, never the caller's values.
    with pytest.raises(ToolActionArgumentError) as raised:
        tool_actions.validate_arguments(name, arguments)
    assert str(raised.value) == reason


async def test_request_validates_a_library_action_but_cannot_bind_one_yet(
    db: AsyncSession,
) -> None:
    # Each action's own validator runs on the request path ...
    with pytest.raises(ToolActionArgumentError) as invalid:
        await request_action(
            db,
            _actor(),
            ToolInvocation(
                tool_name="rename_folder",
                arguments={"project_id": P, "name": " "},
                invocation_id=uuid4(),
            ),
        )
    assert str(invalid.value) == "name must be 1-255 characters"
    # ... and a valid library action is still refused: until its target is
    # resolved against the grant and its effect exists, a stored row would
    # wait for an approval that nothing could run.
    for name in LIBRARY_ACTIONS:
        with pytest.raises(ToolActionArgumentError) as refused:
            await request_action(
                db,
                _actor(),
                ToolInvocation(
                    tool_name=name,
                    arguments=MINIMAL_ARGUMENTS[name],
                    invocation_id=uuid4(),
                ),
            )
        assert str(refused.value) == "tool is not available as an action"
    assert await _row_count(db) == 0


# --- decide ----------------------------------------------------------------


async def test_decision_binds_once_and_only_to_the_requesting_user(
    db: AsyncSession,
) -> None:
    status = await request_action(db, _actor(), _invocation())
    with pytest.raises(ActionNotFound):
        await decide_action(
            db, await _user(db, OTHER_USER), status.invocation_id, approved=True
        )
    orphan = await _user(db)
    setattr(orphan, "organization_id", None)  # legacy Column, untyped
    with pytest.raises(ActionNotFound):
        await decide_action(db, orphan, status.invocation_id, approved=True)
    setattr(orphan, "organization_id", ORG)
    denied = await decide_action(
        db, await _user(db), status.invocation_id, approved=False
    )
    assert denied.state == "failed"
    with pytest.raises(ActionConflict):
        await decide_action(db, await _user(db), status.invocation_id, approved=True)
    assert await execute_action(db, status.invocation_id) is None
    assert await _note_count(db) == 0


# --- approval link and review ----------------------------------------------


async def test_approval_link_is_absolute_and_only_offered_while_pending(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tool_actions.settings, "FRONTEND_BASE_URL", "https://app.example.test/"
    )
    status = await request_action(db, _actor(), _invocation())
    assert status.approval_url == (
        f"https://app.example.test/integrations/actions/{status.invocation_id}"
    )
    await _approve(db, status.invocation_id)
    assert (
        await get_action_status(db, _actor(), status.invocation_id)
    ).approval_url is None


async def test_review_shows_the_stored_target_to_the_requester_only(
    db: AsyncSession,
) -> None:
    status = await request_action(db, _actor(), _invocation(tags=["x", "y"]))
    review = await get_action_for_review(db, await _user(db), status.invocation_id)
    assert (review.title, review.content, review.tags) == (
        "Findings",
        "# Findings\n",
        ["x", "y"],
    )
    assert (review.project_id, review.project_label) == (PROJECT, "Project")
    assert review.project_available is True
    assert review.state == "awaiting_approval" and review.decided_at is None
    with pytest.raises(ActionNotFound):
        await get_action_for_review(
            db, await _user(db, OTHER_USER), status.invocation_id
        )
    with pytest.raises(ActionNotFound):
        await get_action_for_review(db, await _user(db), uuid4())
    # A deleted workspace keeps the request visible (it can still be denied)
    # but says the project is unavailable.
    await db.execute(update(Workspace).values(is_deleted=True))
    await db.commit()
    gone = await get_action_for_review(db, await _user(db), status.invocation_id)
    assert gone.project_available is False


async def test_missing_frontend_origin_yields_no_link_instead_of_an_error(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only commas: the parsed allowlist is empty (an empty string falls back
    # to localhost by design).
    monkeypatch.setattr(tool_actions.settings, "FRONTEND_BASE_URL", "")
    monkeypatch.setattr(tool_actions.settings, "CORS_ORIGINS", " , ")
    status = await request_action(db, _actor(), _invocation())
    assert status.state == "awaiting_approval" and status.approval_url is None


# --- status scope ----------------------------------------------------------


async def test_status_is_scoped_to_org_user_and_consent(db: AsyncSession) -> None:
    status = await request_action(db, _actor(), _invocation())
    invocation_id = status.invocation_id
    assert (await get_action_status(db, _actor(), invocation_id)).state
    # Renewed token, same consent: still visible.
    assert await get_action_status(db, _actor(grant_id=RENEWED_GRANT), invocation_id)
    # The trusted native actor for the same user may read it.
    assert (await get_action_status(db, NATIVE, invocation_id)).state
    for actor in (
        _actor(consent_id=uuid4()),
        INTERNAL,
        _actor(user_id=OTHER_USER),
        _actor(organization_id=ORG2),
    ):
        with pytest.raises(ActionNotFound):
            await get_action_status(db, actor, invocation_id)
    await db.execute(update(IntegrationToolAction).values(is_deleted=True))
    await db.commit()
    with pytest.raises(ActionNotFound):
        await get_action_status(db, _actor(), invocation_id)


# --- execute ---------------------------------------------------------------


async def test_approved_action_creates_exactly_one_note(db: AsyncSession) -> None:
    status = await request_action(db, _actor(), _invocation())
    assert await execute_action(db, status.invocation_id) is None  # not approved
    await _approve(db, status.invocation_id)
    done = await execute_action(db, status.invocation_id)
    assert done is not None and done.state == "succeeded"
    assert done.result is not None and done.result.is_error is False
    assert await _note_count(db) == 1
    note_id = UUID(done.result.content[0]["note_id"])
    note = await db.get(ProjectNote, note_id)
    assert note is not None and note.project_id == PROJECT and note.user_id == USER
    # Re-running never repeats the effect.
    assert await execute_action(db, status.invocation_id) is None
    assert await _note_count(db) == 1
    assert (await get_action_status(db, _actor(), status.invocation_id)).state == (
        "succeeded"
    )


async def test_renewed_or_expired_token_still_executes_under_live_consent(
    db: AsyncSession,
) -> None:
    invocation_id = await _approved_action(db)
    # Renewal revokes the old token and it may have expired meanwhile.
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.id == GRANT)
        .values(
            revoked_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    await db.commit()
    done = await execute_action(db, invocation_id)
    assert done is not None and done.state == "succeeded"
    assert await _note_count(db) == 1
    read = await get_action_status(db, _actor(grant_id=RENEWED_GRANT), invocation_id)
    assert read.state == "succeeded"


@pytest.mark.parametrize(
    ("table", "values"),
    [
        ("consent", {"consent_revoked_at": datetime.now(timezone.utc)}),
        ("consent", {"status": "pending"}),
        ("consent", {"scopes": ["tools:read"]}),
        ("consent", {"project_id": uuid4()}),
        ("consent", {"user_id": OTHER_USER}),
        ("consent", {"organization_id": ORG2}),
        ("consent", {"device_id": uuid4()}),
        ("consent", {"is_deleted": True}),
        ("grant", {"is_deleted": True}),
        ("grant", {"scopes": ["tools:read"]}),
        ("grant", {"project_id": uuid4()}),
        ("grant", {"user_id": OTHER_USER}),
        ("grant", {"organization_id": ORG2}),
        ("grant", {"request_id": uuid4()}),
    ],
    # Static ids: values hold per-worker uuid4()/now(), and xdist workers must
    # collect identical test names.
    ids=[
        "consent-revoked",
        "consent-pending",
        "consent-scope",
        "consent-project",
        "consent-user",
        "consent-org",
        "consent-device",
        "consent-deleted",
        "grant-deleted",
        "grant-scope",
        "grant-project",
        "grant-user",
        "grant-org",
        "grant-other-consent",
    ],
)
async def test_authority_lost_after_approval_fails_before_the_effect(
    db: AsyncSession, table: str, values: dict[str, Any]
) -> None:
    invocation_id = await _approved_action(db)
    if table == "consent":
        await db.execute(
            update(IntegrationGrantRequest)
            .where(IntegrationGrantRequest.id == CONSENT)
            .values(**values)
        )
    else:
        await db.execute(
            update(IntegrationGrant)
            .where(IntegrationGrant.id == GRANT)
            .values(**values)
        )
    await db.commit()
    failed = await execute_action(db, invocation_id)
    assert failed is not None and failed.state == "failed"
    assert await _note_count(db) == 0
    assert (await _last_error(db, invocation_id)) in {
        "grant no longer authorizes this action",
        "consent revoked",
    }


@pytest.mark.parametrize(
    "values",
    [
        {"revoked_at": datetime.now(timezone.utc)},
        {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
    ],
    ids=["revoked", "expired"],
)
async def test_internal_grant_without_consent_is_checked_as_itself(
    db: AsyncSession, values: dict[str, Any]
) -> None:
    invocation_id = await _approved_action(db, INTERNAL)
    assert (await execute_action(db, invocation_id)) is not None
    assert await _note_count(db) == 1
    second = await _approved_action(db, INTERNAL)
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.id == INTERNAL_GRANT)
        .values(**values)
    )
    await db.commit()
    failed = await execute_action(db, second)
    assert failed is not None and failed.state == "failed"
    assert await _last_error(db, second) == "grant revoked or expired"
    assert await _note_count(db) == 1


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({"is_active": False}, "requesting user is not active"),
        ({"is_deleted": True}, "requesting user is not active"),
        ({"organization_id": ORG2}, "requesting user left the organization"),
    ],
    ids=["inactive", "deleted", "moved-org"],
)
async def test_requesting_user_must_still_be_active_in_the_organization(
    db: AsyncSession, values: dict[str, Any], reason: str
) -> None:
    invocation_id = await _approved_action(db)
    await db.execute(update(User).where(User.id == USER).values(**values))
    await db.commit()
    failed = await execute_action(db, invocation_id)
    assert failed is not None and failed.state == "failed"
    assert await _last_error(db, invocation_id) == reason
    assert await _note_count(db) == 0


async def test_native_actor_without_grant_executes_after_decision(
    db: AsyncSession,
) -> None:
    invocation_id = await _approved_action(db, NATIVE)
    done = await execute_action(db, invocation_id)
    assert done is not None and done.state == "succeeded"
    assert await _note_count(db) == 1


async def test_effect_rejected_by_the_adapter_leaves_no_note(db: AsyncSession) -> None:
    invocation_id = await _approved_action(db)
    await db.execute(update(Collection).values(is_deleted=True))
    await db.commit()
    failed = await execute_action(db, invocation_id)
    assert failed is not None and failed.state == "failed"
    assert failed.result is not None and failed.result.is_error is True
    assert await _note_count(db) == 0


async def test_effect_error_after_flush_is_rolled_back(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.research.project_service import ProjectService

    real_create = ProjectService.create_note

    async def create_then_break(self: Any, **kwargs: Any) -> Any:
        await real_create(self, **kwargs)  # flushes the note
        raise RuntimeError("after flush")

    monkeypatch.setattr(ProjectService, "create_note", create_then_break)
    invocation_id = await _approved_action(db)
    failed = await execute_action(db, invocation_id)
    assert failed is not None and failed.state == "failed"
    assert failed.result is not None and failed.result.is_error is True
    assert await _note_count(db) == 0
    assert await execute_action(db, invocation_id) is None


async def test_authority_check_outage_fails_closed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation_id = await _approved_action(db)

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise ConnectionResetError("db gone")

    monkeypatch.setattr(tool_actions, "_authority_intact", boom)
    failed = await execute_action(db, invocation_id)
    assert failed is not None and failed.state == "failed"
    assert await _last_error(db, invocation_id) == "authority check failed"
    assert await _note_count(db) == 0


async def test_concurrent_claim_executes_once(engine: Any, db: AsyncSession) -> None:
    invocation_id = await _approved_action(db)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as one, maker() as two:
        results = await asyncio.gather(
            execute_action(one, invocation_id),
            execute_action(two, invocation_id),
            return_exceptions=True,
        )
    states = sorted(
        "none" if r is None else (r.state if hasattr(r, "state") else "error")
        for r in results
    )
    assert states == ["none", "succeeded"], results
    assert await _note_count(db) == 1


async def test_effect_that_committed_before_the_error_is_never_repeated(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation_id = await _approved_action(db)
    real_commit = db.commit
    calls = {"n": 0}

    async def commit_then_lose_connection() -> None:
        calls["n"] += 1
        await real_commit()
        if calls["n"] == 2:  # the claim commits first; the effect commit is second
            raise ConnectionResetError("lost after commit")

    monkeypatch.setattr(db, "commit", commit_then_lose_connection)
    with pytest.raises(ConnectionResetError):
        await execute_action(db, invocation_id)
    monkeypatch.setattr(db, "commit", real_commit)
    # Effect and receipt landed together, so the row already says so.
    assert await _note_count(db) == 1
    assert (await get_action_status(db, _actor(), invocation_id)).state == "succeeded"
    assert await execute_action(db, invocation_id) is None
    assert await drain_integration_actions(db) == 0
    assert await _note_count(db) == 1


async def test_unconfirmed_commit_stays_unknown_and_is_never_repeated(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation_id = await _approved_action(db)
    real_commit = db.commit
    calls = {"n": 0}

    async def die_before_the_effect_commits() -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionResetError("worker died mid-commit")
        await real_commit()

    monkeypatch.setattr(db, "commit", die_before_the_effect_commits)
    with pytest.raises(ConnectionResetError):
        await execute_action(db, invocation_id)
    monkeypatch.setattr(db, "commit", real_commit)
    await db.rollback()
    # The claim is durable, the effect is not; nothing re-claims an
    # `executing` row, and the sweeper only ever marks it unknown.
    assert await _note_count(db) == 0
    assert (await get_action_status(db, _actor(), invocation_id)).state == "executing"
    assert await execute_action(db, invocation_id) is None
    assert await drain_integration_actions(db) == 0
    assert await sweep_stale_actions(db) == 0  # not stale yet
    await db.execute(
        update(IntegrationToolAction).values(
            claimed_at=datetime.now(timezone.utc) - STALE_EXECUTION - timedelta(1)
        )
    )
    await db.commit()
    assert await sweep_stale_actions(db) == 1
    read = await get_action_status(db, _actor(), invocation_id)
    assert read.state == "outcome_unknown"
    assert await execute_action(db, invocation_id) is None
    assert await drain_integration_actions(db) == 0
    assert await _note_count(db) == 0


async def test_effect_is_discarded_when_the_sweeper_won_first(
    engine: Any, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation_id = await _approved_action(db)
    real_effect = tool_actions._run_effect

    async def effect_during_a_stall(*args: Any, **kwargs: Any) -> Any:
        # The worker stalled after its claim; the sweeper gave up on it
        # (written from another session, before this one takes SQLite's
        # write lock), then the worker wakes up and runs the effect anyway.
        async with async_sessionmaker(engine, expire_on_commit=False)() as other:
            await other.execute(
                update(IntegrationToolAction)
                .where(IntegrationToolAction.invocation_id == invocation_id)
                .values(state="outcome_unknown")
            )
            await other.commit()
        return await real_effect(*args, **kwargs)

    monkeypatch.setattr(tool_actions, "_run_effect", effect_during_a_stall)
    result = await execute_action(db, invocation_id)
    assert result is not None and result.state == "outcome_unknown"
    assert await _note_count(db) == 0


# --- drain / sweep ---------------------------------------------------------


async def test_drain_executes_only_approved_rows_and_honours_the_kill_switch(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = await request_action(db, _actor(), _invocation())
    await request_action(db, _actor(), _invocation())  # still awaiting approval
    await _approve(db, approved.invocation_id)
    monkeypatch.setattr(tool_actions.settings, "NOUS_MCP_ENABLED", False)
    assert await drain_integration_actions(db) == 0
    assert (await get_action_status(db, _actor(), approved.invocation_id)).state == (
        "approved"
    )
    monkeypatch.setattr(tool_actions.settings, "NOUS_MCP_ENABLED", True)
    assert await drain_integration_actions(db) == 1
    assert await _note_count(db) == 1
    assert await drain_integration_actions(db) == 0


# --- workspace bindings ----------------------------------------------------


def _workspace_actor(**overrides: Any) -> ActionActor:
    """The actor of a workspace grant whose action targets PROJECT."""
    values: dict[str, Any] = {
        "user_id": USER,
        "organization_id": ORG,
        "project_id": PROJECT,
        "workspace_id": WORKSPACE,
        "grant_id": GRANT,
        "consent_id": CONSENT,
    }
    values.update(overrides)
    return ActionActor(**values)


async def _workspace_row(db: AsyncSession, **overrides: Any) -> IntegrationToolAction:
    """A workspace-level action with no target project, as a later slice requests.

    Pass ``project_id`` for the other shape the migration documents: a workspace
    grant's action aimed at one Collection sets both columns, project_id the
    target and workspace_id the grant's binding.
    """
    values: dict[str, Any] = {
        "organization_id": ORG,
        "user_id": USER,
        "project_id": None,
        "workspace_id": WORKSPACE,
        "invocation_id": uuid4(),
        "tool_name": "create_project_note",
        "arguments": NOTE_ARGS,
        "argument_hash": canonical_hash("create_project_note", NOTE_ARGS),
        "state": "awaiting_approval",
    }
    values.update(overrides)
    row = IntegrationToolAction(**values)
    db.add(row)
    await db.commit()
    return row


async def _workspace_authority(db: AsyncSession) -> tuple[UUID, UUID]:
    """A consumed workspace consent and the live grant exchanged from it."""
    grant_id, consent_id = uuid4(), uuid4()
    await db.execute(
        insert(IntegrationGrantRequest).values(
            id=consent_id,
            user_id=USER,
            organization_id=ORG,
            project_id=None,
            workspace_id=WORKSPACE,
            device_id=DEVICE,
            scopes=["tools:read", "tools:write"],
            status="consumed",
            expires_at=SOON,
            approved_at=datetime.now(timezone.utc),
        )
    )
    await db.execute(
        insert(IntegrationGrant).values(
            **_grant_values(
                grant_id,
                project_id=None,
                workspace_id=WORKSPACE,
                request_id=consent_id,
            )
        )
    )
    await db.commit()
    return grant_id, consent_id


async def test_workspace_binding_is_stored_with_the_action(db: AsyncSession) -> None:
    invocation = _invocation()
    status = await request_action(db, _workspace_actor(), invocation)
    assert status.state == "awaiting_approval"
    row = await db.scalar(
        select(IntegrationToolAction).where(
            IntegrationToolAction.invocation_id == invocation.invocation_id
        )
    )
    assert row is not None
    assert (row.project_id, row.workspace_id) == (PROJECT, WORKSPACE)
    # The same binding replays; a project-bound actor under the same consent
    # is a different target.
    assert (
        await request_action(db, _workspace_actor(), invocation)
    ).state == "awaiting_approval"
    with pytest.raises(ActionConflict):
        await request_action(db, _actor(), invocation)
    assert await _row_count(db) == 1


async def test_workspace_actor_cannot_request_a_project_action_yet(
    db: AsyncSession,
) -> None:
    # Every action row names a project, and create_project_note has no way to
    # select one: a workspace connection is refused, not stored.
    with pytest.raises(
        ToolActionArgumentError, match="workspace connections cannot request"
    ):
        await request_action(db, _workspace_actor(project_id=None), _invocation())
    assert await _row_count(db) == 0


async def test_grant_bound_to_another_workspace_cannot_run_the_action(
    db: AsyncSession,
) -> None:
    # GRANT is a project grant: it binds PROJECT and no workspace, and the row
    # asks for a workspace grant.
    status = await request_action(db, _workspace_actor(), _invocation())
    await _approve(db, status.invocation_id)
    failed = await execute_action(db, status.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert (await _last_error(db, status.invocation_id)) == (
        "grant no longer authorizes this action"
    )
    assert await _note_count(db) == 0


async def test_review_of_a_workspace_level_action_names_the_workspace(
    db: AsyncSession,
) -> None:
    row = await _workspace_row(db)
    review = await get_action_for_review(db, await _user(db), row.invocation_id)
    assert (review.project_id, review.project_label) == (None, None)
    assert (review.workspace_id, review.workspace_label) == (WORKSPACE, "Workspace")
    assert review.project_available is True
    assert review.title == "Findings"
    # Still reviewable, and still deniable, once the workspace is gone.
    await db.execute(update(Workspace).values(is_deleted=True))
    await db.commit()
    gone = await get_action_for_review(db, await _user(db), row.invocation_id)
    assert gone.project_available is False
    assert gone.workspace_label == "Workspace"


async def test_review_names_the_workspace_only_for_workspace_bound_actions(
    db: AsyncSession,
) -> None:
    plain = await request_action(db, _actor(), _invocation())
    review = await get_action_for_review(db, await _user(db), plain.invocation_id)
    assert (review.project_label, review.workspace_id, review.workspace_label) == (
        "Project",
        None,
        None,
    )
    bound = await request_action(db, _workspace_actor(), _invocation())
    review = await get_action_for_review(db, await _user(db), bound.invocation_id)
    assert (review.project_id, review.project_label) == (PROJECT, "Project")
    assert (review.workspace_id, review.workspace_label) == (WORKSPACE, "Workspace")
    assert review.project_available is True


async def test_workspace_level_action_never_runs_without_a_project(
    db: AsyncSession,
) -> None:
    grant_id, consent_id = await _workspace_authority(db)
    # A project literally called "None": str(None) must never be resolved by
    # name into a write target.
    await db.execute(
        insert(Collection).values(id=uuid4(), name="None", workspace_id=WORKSPACE)
    )
    await db.commit()
    row = await _workspace_row(
        db, grant_id=grant_id, consent_id=consent_id, state="approved", approved=True
    )
    done = await execute_action(db, row.invocation_id)
    # The authority check passed (the grant, consent and row agree on the
    # workspace); the effect itself refused a missing project.
    assert done is not None and done.state == "failed"
    assert (await _last_error(db, row.invocation_id)) == "effect failed before commit"
    assert await _note_count(db) == 0


@pytest.mark.parametrize(
    ("table", "values", "reason"),
    [
        ("grant", {"workspace_id": uuid4()}, "grant no longer authorizes this action"),
        ("consent", {"workspace_id": uuid4()}, "consent revoked"),
        (
            "consent",
            {"workspace_id": None, "project_id": uuid4()},
            "consent revoked",
        ),
    ],
    ids=["grant-workspace", "consent-workspace", "consent-became-project"],
)
@pytest.mark.parametrize(
    "target", [None, PROJECT], ids=["workspace-level", "aimed-at-a-collection"]
)
async def test_workspace_authority_lost_after_approval_fails_before_the_effect(
    db: AsyncSession,
    table: str,
    values: dict[str, Any],
    reason: str,
    target: UUID | None,
) -> None:
    grant_id, consent_id = await _workspace_authority(db)
    row = await _workspace_row(
        db,
        project_id=target,
        grant_id=grant_id,
        consent_id=consent_id,
        state="approved",
        approved=True,
    )
    if table == "consent":
        await db.execute(
            update(IntegrationGrantRequest)
            .where(IntegrationGrantRequest.id == consent_id)
            .values(**values)
        )
    else:
        await db.execute(
            update(IntegrationGrant)
            .where(IntegrationGrant.id == grant_id)
            .values(**values)
        )
    await db.commit()
    failed = await execute_action(db, row.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert (await _last_error(db, row.invocation_id)) == reason
    assert await _note_count(db) == 0


async def _approved_workspace_action(
    db: AsyncSession, **overrides: Any
) -> IntegrationToolAction:
    """An approved action a workspace grant of WORKSPACE aimed at PROJECT.

    The grant and its consent bind WORKSPACE and no project; the row names both,
    its project_id the target Collection and its workspace_id the binding.
    """
    grant_id, consent_id = await _workspace_authority(db)
    values: dict[str, Any] = {
        "project_id": PROJECT,
        "grant_id": grant_id,
        "consent_id": consent_id,
        "state": "approved",
        "approved": True,
    }
    values.update(overrides)
    return await _workspace_row(db, **values)


async def test_workspace_grant_runs_an_action_aimed_at_one_of_its_collections(
    db: AsyncSession,
) -> None:
    # The row shape the migration documents for a workspace grant's action on a
    # Collection (project_id the target, workspace_id the binding, both set)
    # must be authorisable, or the first library action would fail closed.
    row = await _approved_workspace_action(db)
    done = await execute_action(db, row.invocation_id)
    assert done is not None and done.state == "succeeded"
    assert await _note_count(db) == 1
    note = await db.scalar(select(ProjectNote))
    assert note is not None and note.project_id == PROJECT and note.user_id == USER


async def test_workspace_grant_cannot_run_a_project_bound_action(
    db: AsyncSession,
) -> None:
    # Only a row that names a workspace is compared by its workspace: a row
    # bound to PROJECT alone still needs a grant on exactly PROJECT.
    row = await _approved_workspace_action(db, workspace_id=None)
    failed = await execute_action(db, row.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert (await _last_error(db, row.invocation_id)) == (
        "grant no longer authorizes this action"
    )
    assert await _note_count(db) == 0


async def test_project_grant_cannot_run_an_action_that_names_a_workspace(
    db: AsyncSession,
) -> None:
    # GRANT and CONSENT bind PROJECT and no workspace. Neither may authorise a
    # row that names a workspace, whichever project it aims at.
    row = await _workspace_row(
        db,
        project_id=PROJECT,
        grant_id=GRANT,
        consent_id=CONSENT,
        state="approved",
        approved=True,
    )
    failed = await execute_action(db, row.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert (await _last_error(db, row.invocation_id)) == (
        "grant no longer authorizes this action"
    )
    assert await _note_count(db) == 0


@pytest.mark.parametrize(
    "case",
    ["another-workspace", "deleted-collection", "unknown-collection", "access-lost"],
)
async def test_workspace_grant_cannot_aim_an_action_outside_its_workspace(
    db: AsyncSession, case: str
) -> None:
    # The binding alone no longer says which Collection the grant may write to,
    # so the target is re-checked against the workspace's live Collections that
    # the user can still reach. Each case below passes the grant and consent
    # checks, and the user could still write to the Collection themselves.
    other_workspace, elsewhere, retired = uuid4(), uuid4(), uuid4()
    await db.execute(
        insert(Workspace).values(
            id=other_workspace, name="Other", owner_id=USER, organization_id=ORG
        )
    )
    await db.execute(
        insert(Collection).values(
            [
                dict(id=elsewhere, name="Elsewhere", workspace_id=other_workspace),
                dict(
                    id=retired, name="Retired", workspace_id=WORKSPACE, is_deleted=True
                ),
            ]
        )
    )
    await db.commit()
    target = {
        "another-workspace": elsewhere,
        "deleted-collection": retired,
        "unknown-collection": uuid4(),
        "access-lost": PROJECT,
    }[case]
    row = await _approved_workspace_action(db, project_id=target)
    if case == "access-lost":
        # Neither the owner nor a member of WORKSPACE any more.
        await db.execute(
            update(Workspace).where(Workspace.id == WORKSPACE).values(owner_id=uuid4())
        )
        await db.commit()
    failed = await execute_action(db, row.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert (await _last_error(db, row.invocation_id)) == (
        "grant no longer authorizes this action"
    )
    assert await _note_count(db) == 0


@pytest.mark.parametrize("aimed_at", ["its-workspace", "another-workspace"])
async def test_internal_workspace_grant_is_held_to_the_same_target_check(
    db: AsyncSession, aimed_at: str
) -> None:
    # A grant issued without a consent request is checked as itself, and the
    # row's target is checked on that path too.
    other_workspace, elsewhere, grant_id = uuid4(), uuid4(), uuid4()
    await db.execute(
        insert(Workspace).values(
            id=other_workspace, name="Other", owner_id=USER, organization_id=ORG
        )
    )
    await db.execute(
        insert(Collection).values(
            id=elsewhere, name="Elsewhere", workspace_id=other_workspace
        )
    )
    await db.execute(
        insert(IntegrationGrant).values(
            **_grant_values(
                grant_id,
                project_id=None,
                workspace_id=WORKSPACE,
                device_id=None,
                request_id=None,
            )
        )
    )
    await db.commit()
    row = await _workspace_row(
        db,
        project_id=PROJECT if aimed_at == "its-workspace" else elsewhere,
        grant_id=grant_id,
        state="approved",
        approved=True,
    )
    done = await execute_action(db, row.invocation_id)
    assert done is not None
    if aimed_at == "its-workspace":
        assert done.state == "succeeded" and await _note_count(db) == 1
    else:
        assert done.state == "failed" and await _note_count(db) == 0
        assert (await _last_error(db, row.invocation_id)) == (
            "grant no longer authorizes this action"
        )


def test_a_workspace_grants_action_replays_for_its_workspace_actor() -> None:
    # The actor of a workspace grant carries its workspace and no project, while
    # the row it stored names the Collection the action aims at. Only a
    # row that names no workspace is compared by project.
    row = IntegrationToolAction(
        organization_id=ORG,
        user_id=USER,
        project_id=PROJECT,
        workspace_id=WORKSPACE,
        invocation_id=uuid4(),
        tool_name="create_project_note",
        arguments=NOTE_ARGS,
        argument_hash=canonical_hash("create_project_note", NOTE_ARGS),
        state="awaiting_approval",
    )
    workspace_actor = _workspace_actor(project_id=None)
    assert tool_actions._same_target(row, workspace_actor)
    assert tool_actions._same_target(row, _workspace_actor())  # names PROJECT itself
    for stranger in (
        _workspace_actor(project_id=uuid4()),  # names another Collection
        _workspace_actor(project_id=None, workspace_id=uuid4()),
        _workspace_actor(project_id=None, workspace_id=None),
        _workspace_actor(project_id=None, thread_id=uuid4()),
        _workspace_actor(project_id=None, run_id=uuid4()),
        _actor(),  # a project grant's actor
    ):
        assert not tool_actions._same_target(row, stranger)

    bound_to_project = IntegrationToolAction(
        organization_id=ORG,
        user_id=USER,
        project_id=PROJECT,
        workspace_id=None,
        invocation_id=uuid4(),
        tool_name="create_project_note",
        arguments=NOTE_ARGS,
        argument_hash=canonical_hash("create_project_note", NOTE_ARGS),
        state="awaiting_approval",
    )
    assert tool_actions._same_target(bound_to_project, _actor())
    # A workspace actor with no project, or with a workspace, is not that row's.
    assert not tool_actions._same_target(
        bound_to_project, _workspace_actor(project_id=None)
    )
    assert not tool_actions._same_target(bound_to_project, _workspace_actor())

"""Durable, fail-closed note actions shared by native NOUS and external harnesses."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.collection import Collection, CollectionDocument
from src.models.document import Document, DocumentType
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.project_note import ProjectNote
from src.models.research_project import ResearchProject
from src.models.research_project_role import ResearchProjectRoleAssignment
from src.models.tool_action import IntegrationToolAction
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
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
from src.services.integrations.context import IntegrationAccessDenied
from src.services.threads import collection_service

pytestmark = pytest.mark.unit
USER, OTHER_USER, ORG, ORG2, PROJECT, WORKSPACE = (uuid4() for _ in range(6))
DEVICE, CONSENT, GRANT, RENEWED_GRANT, INTERNAL_GRANT = (uuid4() for _ in range(5))
NOTE_ARGS = {"title": "Findings", "content": "# Findings\n", "tags": ["a"]}
SOON = datetime.now(timezone.utc) + timedelta(hours=1)
# The scopes of the seeded grants. A library:write grant holds these too.
WRITE = frozenset({"tools:read", "tools:write"})


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
        Document,
        CollectionDocument,
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
    scopes: frozenset[str] = WRITE,
) -> ActionActor:
    return ActionActor(
        user_id=user_id,
        organization_id=organization_id,
        project_id=PROJECT,
        grant_id=grant_id,
        consent_id=consent_id,
        scopes=scopes,
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
        ActionActor(
            user_id=USER, organization_id=ORG, project_id=uuid4(), scopes=WRITE
        ),
        ActionActor(
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT,
            thread_id=uuid4(),
            scopes=WRITE,
        ),
        ActionActor(
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT,
            run_id=uuid4(),
            scopes=WRITE,
        ),
        ActionActor(
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT,
            workspace_id=WORKSPACE,
            scopes=WRITE,
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


async def test_request_runs_each_actions_own_validator_first(
    db: AsyncSession,
) -> None:
    # The validator runs before the scope, the target or any row: an actor
    # with no scope at all still gets the argument's fixed reason.
    with pytest.raises(ToolActionArgumentError) as invalid:
        await request_action(
            db,
            _actor(scopes=frozenset()),
            ToolInvocation(
                tool_name="rename_folder",
                arguments={"project_id": P, "name": " "},
                invocation_id=uuid4(),
            ),
        )
    assert str(invalid.value) == "name must be 1-255 characters"
    assert await _row_count(db) == 0


async def test_an_actor_without_the_actions_scope_is_refused(
    db: AsyncSession,
) -> None:
    for name in sorted(MINIMAL_ARGUMENTS):
        with pytest.raises(IntegrationAccessDenied):
            await request_action(
                db,
                _actor(scopes=frozenset({"tools:read", "library:read"})),
                ToolInvocation(
                    tool_name=name,
                    arguments=MINIMAL_ARGUMENTS[name],
                    invocation_id=uuid4(),
                ),
            )
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
    # Still a live member, so the authority re-check passes, but a viewer may
    # not edit the project: the note adapter's own check refuses the effect.
    await db.execute(
        update(Workspace).where(Workspace.id == WORKSPACE).values(owner_id=OTHER_USER)
    )
    db.add(
        WorkspaceMember(workspace_id=WORKSPACE, user_id=USER, role=WorkspaceRole.VIEWER)
    )
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
        "scopes": WRITE,
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


def _rename_project(invocation_id: UUID | None = None) -> ToolInvocation:
    """Rename PROJECT: an action a workspace grant aims at one Collection."""
    return ToolInvocation(
        tool_name="rename_folder",
        arguments={"project_id": P, "name": "Renamed"},
        invocation_id=invocation_id or uuid4(),
    )


async def _project_name(db: AsyncSession) -> str:
    return str(
        await db.scalar(
            select(Collection.name)
            .where(Collection.id == PROJECT)
            .execution_options(populate_existing=True)
        )
    )


async def test_workspace_binding_is_stored_with_the_action(db: AsyncSession) -> None:
    # A workspace grant's actor names its workspace and no project; the
    # project_id selector chooses the Collection the action aims at.
    invocation = _rename_project()
    status = await request_action(db, _workspace_actor(project_id=None), invocation)
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
        await request_action(db, _workspace_actor(project_id=None), invocation)
    ).state == "awaiting_approval"
    with pytest.raises(ActionConflict):
        await request_action(db, _actor(), invocation)
    assert await _row_count(db) == 1


async def test_workspace_actor_cannot_request_a_note(
    db: AsyncSession,
) -> None:
    # A note lands in the grant's own project and takes no selector, so a
    # workspace connection has nowhere to put one: refused, not stored.
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
    status = await request_action(
        db, _workspace_actor(project_id=None), _rename_project()
    )
    await _approve(db, status.invocation_id)
    failed = await execute_action(db, status.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert (await _last_error(db, status.invocation_id)) == (
        "grant no longer authorizes this action"
    )
    assert await _project_name(db) == "Project"


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
    bound = await request_action(
        db, _workspace_actor(project_id=None), _rename_project()
    )
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


# --- library actions: target, per-action scope, auto-run -------------------

# Every scope a library:write grant holds: check_scopes makes write carry its
# read and the tools:write gateway.
LIBRARY = frozenset({"tools:read", "tools:write", "library:read", "library:write"})
# What no grant can hold (check_scopes refuses it). Seeded straight into rows to
# prove each action is checked against its own scope, never just the route's.
LIBRARY_ONLY = frozenset({"library:read", "library:write"})
# SECOND is another live Collection of WORKSPACE and RETIRED a soft-deleted one;
# ELSEWHERE lives in OTHER_WORKSPACE, which USER owns too. UNKNOWN is no row.
SECOND, RETIRED, OTHER_WORKSPACE, ELSEWHERE, UNKNOWN = (uuid4() for _ in range(5))
ALPHA = uuid4()  # a Collection named "Alpha", seeded by the tests that need it
# PAPER sits in PROJECT; PAPER2 in no folder.
PAPER, PAPER2 = uuid4(), uuid4()
LANDED = str(uuid4())  # a document an arXiv ingest stored


def _paper(document_id: UUID, organization_id: UUID = ORG) -> Document:
    return Document(
        id=document_id,
        organization_id=organization_id,
        uploaded_by_user_id=USER,
        title="Paper",
        filename="paper.pdf",
        file_path="/unused/paper.pdf",
        file_size_bytes=100,
        mime_type="application/pdf",
        document_type=DocumentType.PDF,
        tags=["seed"],
    )


@pytest.fixture
async def library(db: AsyncSession) -> None:
    await db.execute(
        insert(Workspace).values(
            id=OTHER_WORKSPACE, name="Other", owner_id=USER, organization_id=ORG
        )
    )
    for collection_id, name, workspace_id, deleted in (
        (SECOND, "Second", WORKSPACE, False),
        (RETIRED, "Retired", WORKSPACE, True),
        (ELSEWHERE, "Elsewhere", OTHER_WORKSPACE, False),
    ):
        await db.execute(
            insert(Collection).values(
                id=collection_id,
                name=name,
                workspace_id=workspace_id,
                is_deleted=deleted,
            )
        )
    db.add_all([_paper(PAPER), _paper(PAPER2)])
    await db.flush()
    db.add(CollectionDocument(collection_id=PROJECT, document_id=PAPER))
    await db.commit()


async def _library_actor(
    db: AsyncSession,
    scopes: frozenset[str] = LIBRARY,
    *,
    consent_scopes: frozenset[str] | None = None,
) -> ActionActor:
    """The actor of a live workspace grant on WORKSPACE and of its consent."""
    grant_id, consent_id = uuid4(), uuid4()
    await db.execute(
        insert(IntegrationGrantRequest).values(
            id=consent_id,
            user_id=USER,
            organization_id=ORG,
            project_id=None,
            workspace_id=WORKSPACE,
            device_id=DEVICE,
            scopes=sorted(scopes if consent_scopes is None else consent_scopes),
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
                scopes=sorted(scopes),
            )
        )
    )
    await db.commit()
    return ActionActor(
        user_id=USER,
        organization_id=ORG,
        project_id=None,
        workspace_id=WORKSPACE,
        grant_id=grant_id,
        consent_id=consent_id,
        scopes=scopes,
    )


def _call(
    tool_name: str, invocation_id: UUID | None = None, **arguments: Any
) -> ToolInvocation:
    return ToolInvocation(
        tool_name=tool_name,
        arguments=arguments,
        invocation_id=invocation_id or uuid4(),
    )


async def _linked(db: AsyncSession, collection_id: UUID, document_id: UUID) -> bool:
    """Whether the document is a live member of the Collection."""
    count = await db.scalar(
        select(func.count())
        .select_from(CollectionDocument)
        .where(
            CollectionDocument.collection_id == collection_id,
            CollectionDocument.document_id == document_id,
            CollectionDocument.is_deleted.is_(False),
        )
    )
    return bool(count)


async def _row(db: AsyncSession, invocation_id: UUID) -> IntegrationToolAction:
    row = await db.scalar(
        select(IntegrationToolAction)
        .where(IntegrationToolAction.invocation_id == invocation_id)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return cast(IntegrationToolAction, row)


async def _collection(db: AsyncSession, collection_id: UUID) -> Collection:
    found = await db.scalar(
        select(Collection)
        .where(Collection.id == collection_id)
        .execution_options(populate_existing=True)
    )
    assert found is not None
    return cast(Collection, found)


def _receipt(status: Any) -> dict[str, Any]:
    assert status is not None and status.result is not None
    return dict(status.result.content[0])


async def test_library_write_grant_auto_runs_save_papers(
    db: AsyncSession, library: None
) -> None:
    actor = await _library_actor(db)
    arguments = {"document_ids": [str(PAPER2)], "project_id": str(SECOND)}
    status = await request_action(
        db, actor, _call("save_papers_to_folder", **arguments)
    )
    assert status.state == "succeeded" and status.approval_url is None
    assert await _linked(db, SECOND, PAPER2)
    assert _receipt(status) == {
        "ok": True,
        "tool_name": "save_papers_to_folder",
        "project_id": str(SECOND),
        "document_ids": [str(PAPER2)],
        "skipped_document_ids": [],
    }
    assert status.result is not None
    assert status.result.source_refs == [{"document_id": str(PAPER2)}]
    row = await _row(db, status.invocation_id)
    # Aimed at one Collection of the grant's workspace: both columns are set.
    assert (row.project_id, row.workspace_id) == (SECOND, WORKSPACE)
    # The grant's library:write consent stands in for a per-action decision.
    assert (row.approved, row.decided_by) == (True, USER)
    assert row.decided_at is not None
    # A replay returns the receipt and runs nothing again.
    again = await request_action(
        db, actor, _call("save_papers_to_folder", status.invocation_id, **arguments)
    )
    assert again.state == "succeeded"
    assert await _row_count(db) == 1


async def test_tools_write_only_grant_waits_for_a_decision_on_a_library_action(
    db: AsyncSession, library: None
) -> None:
    status = await request_action(
        db,
        await _library_actor(db, WRITE),
        _call(
            "save_papers_to_folder",
            document_ids=[str(PAPER2)],
            project_id=str(SECOND),
        ),
    )
    assert status.state == "awaiting_approval"
    assert not await _linked(db, SECOND, PAPER2)
    await _approve(db, status.invocation_id)
    done = await execute_action(db, status.invocation_id)
    assert done is not None and done.state == "succeeded"
    assert await _linked(db, SECOND, PAPER2)


DESTRUCTIVE: dict[str, tuple[str, dict[str, Any]]] = {
    "delete": ("delete_folder", {"project_id": str(SECOND)}),
    "ingest": (
        "ingest_arxiv_papers",
        {"paper_ids": ["2401.00001"], "project_id": str(SECOND)},
    ),
}


@pytest.mark.parametrize(
    ("name", "arguments"), DESTRUCTIVE.values(), ids=list(DESTRUCTIVE)
)
async def test_deletion_and_ingest_never_auto_run(
    db: AsyncSession, library: None, name: str, arguments: dict[str, Any]
) -> None:
    status = await request_action(
        db, await _library_actor(db), _call(name, **arguments)
    )
    assert status.state == "awaiting_approval"
    assert (await _collection(db, SECOND)).is_deleted is False


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("create_project_note", NOTE_ARGS),
        ("delete_folder", {"project_id": P}),
        ("ingest_arxiv_papers", {"paper_ids": ["2401.00001"]}),
    ],
    ids=["note", "delete", "ingest"],
)
async def test_library_write_alone_cannot_request_a_note_a_deletion_or_an_ingest(
    db: AsyncSession, name: str, arguments: dict[str, Any]
) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await request_action(db, _actor(scopes=LIBRARY_ONLY), _call(name, **arguments))
    assert await _row_count(db) == 0


@pytest.mark.parametrize("held_by", ["internal-grant", "consented-grant", "consent"])
@pytest.mark.parametrize(
    ("name", "arguments"), DESTRUCTIVE.values(), ids=list(DESTRUCTIVE)
)
async def test_library_write_alone_cannot_execute_a_deletion_or_an_ingest(
    db: AsyncSession,
    library: None,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: dict[str, Any],
    held_by: str,
) -> None:
    from src.services.agent import tools_impl

    ingests: list[Any] = []

    async def ingest(*args: Any, **kwargs: Any) -> dict[str, Any]:
        ingests.append(args)
        return {"status": "ingestion_complete", "document_ids": [LANDED]}

    monkeypatch.setattr(tools_impl, "_tool_ingest_arxiv", ingest)
    consent_id: UUID | None
    if held_by == "internal-grant":
        grant_id, consent_id = uuid4(), None
        await db.execute(
            insert(IntegrationGrant).values(
                **_grant_values(
                    grant_id,
                    project_id=None,
                    workspace_id=WORKSPACE,
                    device_id=None,
                    request_id=None,
                    scopes=sorted(LIBRARY_ONLY),
                )
            )
        )
        await db.commit()
    else:
        library_grant = held_by == "consented-grant"
        actor = await _library_actor(
            db,
            LIBRARY_ONLY if library_grant else LIBRARY,
            consent_scopes=LIBRARY if library_grant else LIBRARY_ONLY,
        )
        grant_id, consent_id = cast(UUID, actor.grant_id), actor.consent_id
    # Stored as if approved; request_action itself refuses such an actor.
    row = await _workspace_row(
        db,
        tool_name=name,
        arguments=arguments,
        argument_hash=canonical_hash(name, arguments),
        project_id=SECOND,
        grant_id=grant_id,
        consent_id=consent_id,
        state="approved",
        approved=True,
    )
    failed = await execute_action(db, row.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert await _last_error(db, row.invocation_id) == (
        "consent revoked"
        if held_by == "consent"
        else "grant no longer authorizes this action"
    )
    assert (await _collection(db, SECOND)).is_deleted is False
    assert ingests == []


OUT_OF_SCOPE: dict[str, tuple[str, dict[str, Any]]] = {
    "rename-another-workspace": (
        "rename_folder",
        {"project_id": str(ELSEWHERE), "name": "X"},
    ),
    "rename-deleted-folder": (
        "rename_folder",
        {"project_id": str(RETIRED), "name": "X"},
    ),
    "rename-unknown-folder": (
        "rename_folder",
        {"project_id": str(UNKNOWN), "name": "X"},
    ),
    "save-another-workspace": (
        "save_papers_to_folder",
        {"document_ids": [str(PAPER2)], "project_id": str(ELSEWHERE)},
    ),
    "remove-another-workspace": (
        "remove_papers_from_folder",
        {"document_ids": [str(PAPER)], "project_id": str(ELSEWHERE)},
    ),
    "move-to-another-workspace": (
        "move_papers_between_folders",
        {
            "document_ids": [str(PAPER)],
            "from_project_id": P,
            "to_project_id": str(ELSEWHERE),
        },
    ),
    "move-from-another-workspace": (
        "move_papers_between_folders",
        {
            "document_ids": [str(PAPER)],
            "from_project_id": str(ELSEWHERE),
            "to_project_id": P,
        },
    ),
    "delete-another-workspace": ("delete_folder", {"project_id": str(ELSEWHERE)}),
    "ingest-another-workspace": (
        "ingest_arxiv_papers",
        {"paper_ids": ["2401.00001"], "project_id": str(ELSEWHERE)},
    ),
}


@pytest.mark.parametrize(
    ("name", "arguments"), OUT_OF_SCOPE.values(), ids=list(OUT_OF_SCOPE)
)
async def test_target_outside_scope_is_denied(
    db: AsyncSession, library: None, name: str, arguments: dict[str, Any]
) -> None:
    # Every case is a Collection the user could change in the app: only the
    # grant's workspace keeps it out.
    with pytest.raises(IntegrationAccessDenied):
        await request_action(db, await _library_actor(db), _call(name, **arguments))
    assert await _row_count(db) == 0
    assert (await _collection(db, ELSEWHERE)).name == "Elsewhere"
    assert await _linked(db, PROJECT, PAPER)


PROJECT_GRANT_REFUSED: dict[str, tuple[str, dict[str, Any]]] = {
    "rename-a-sibling": ("rename_folder", {"project_id": str(SECOND), "name": "X"}),
    "save-into-a-sibling": (
        "save_papers_to_folder",
        {"document_ids": [str(PAPER2)], "project_id": str(SECOND)},
    ),
    "move-out": (
        "move_papers_between_folders",
        {
            "document_ids": [str(PAPER)],
            "from_project_id": P,
            "to_project_id": str(SECOND),
        },
    ),
    "delete-a-sibling": ("delete_folder", {"project_id": str(SECOND)}),
    "ingest-into-a-sibling": (
        "ingest_arxiv_papers",
        {"paper_ids": ["2401.00001"], "project_id": str(SECOND)},
    ),
    "create-a-folder": ("create_folder", {"name": "Reading"}),
    "metadata-of-a-document-outside-it": (
        "update_document_metadata",
        {"document_id": str(PAPER2), "title": "T"},
    ),
}


@pytest.mark.parametrize(
    ("name", "arguments"),
    PROJECT_GRANT_REFUSED.values(),
    ids=list(PROJECT_GRANT_REFUSED),
)
async def test_project_grant_reaches_its_own_project_only(
    db: AsyncSession, library: None, name: str, arguments: dict[str, Any]
) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await request_action(db, _actor(scopes=LIBRARY), _call(name, **arguments))
    assert await _row_count(db) == 0


async def test_project_grant_may_name_its_own_project(
    db: AsyncSession, library: None
) -> None:
    actor = _actor()
    for invocation in (
        _call("rename_folder", project_id=P, name="Renamed"),
        _call("ingest_arxiv_papers", paper_ids=["2401.00001"]),
        _call("ingest_arxiv_papers", paper_ids=["2401.00002"], project_id=P),
        _call("update_document_metadata", document_id=str(PAPER), title="T"),
    ):
        status = await request_action(db, actor, invocation)
        assert status.state == "awaiting_approval"
        row = await _row(db, status.invocation_id)
        assert (row.project_id, row.workspace_id) == (PROJECT, None)


async def test_an_actor_must_carry_exactly_one_binding(
    db: AsyncSession, library: None
) -> None:
    for actor in (
        _workspace_actor(scopes=LIBRARY),  # a project and a workspace
        _workspace_actor(project_id=None, workspace_id=None, scopes=LIBRARY),
    ):
        with pytest.raises(IntegrationAccessDenied):
            await request_action(db, actor, _rename_project())
    assert await _row_count(db) == 0


async def test_a_grant_whose_user_lost_the_workspace_cannot_request(
    db: AsyncSession, library: None
) -> None:
    actor = await _library_actor(db)
    await db.execute(
        update(Workspace).where(Workspace.id == WORKSPACE).values(owner_id=OTHER_USER)
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await request_action(db, actor, _call("create_folder", name="Reading"))
    assert await _row_count(db) == 0


async def test_move_unlinks_and_links_in_one_effect(
    db: AsyncSession, library: None
) -> None:
    status = await request_action(
        db,
        await _library_actor(db),
        _call(
            "move_papers_between_folders",
            document_ids=[str(PAPER), str(PAPER2)],
            from_project_id=P,
            to_project_id=str(SECOND),
        ),
    )
    assert status.state == "succeeded"
    assert not await _linked(db, PROJECT, PAPER)
    assert await _linked(db, SECOND, PAPER)
    # PAPER2 was in no folder: a move never adds what it did not take out.
    assert not await _linked(db, SECOND, PAPER2)
    assert _receipt(status) == {
        "ok": True,
        "tool_name": "move_papers_between_folders",
        "project_id": P,
        "from_project_id": P,
        "to_project_id": str(SECOND),
        "document_ids": [str(PAPER)],
        "skipped_document_ids": [str(PAPER2)],
    }
    row = await _row(db, status.invocation_id)
    assert (row.project_id, row.workspace_id) == (PROJECT, WORKSPACE)


async def test_move_is_atomic(
    db: AsyncSession, library: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def link_fails(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("link failed after the unlink was flushed")

    monkeypatch.setattr(collection_service, "add_documents_to_collection", link_fails)
    status = await request_action(
        db,
        await _library_actor(db),
        _call(
            "move_papers_between_folders",
            document_ids=[str(PAPER)],
            from_project_id=P,
            to_project_id=str(SECOND),
        ),
    )
    assert status.state == "failed"
    assert await _last_error(db, status.invocation_id) == "effect failed before commit"
    # The unlink rolled back with the failed link: the paper never left PROJECT.
    assert await _linked(db, PROJECT, PAPER)
    assert not await _linked(db, SECOND, PAPER)


async def test_rename_and_remove_run_without_a_decision(
    db: AsyncSession, library: None
) -> None:
    actor = await _library_actor(db)
    renamed = await request_action(
        db, actor, _call("rename_folder", project_id=str(SECOND), name="Renamed")
    )
    assert renamed.state == "succeeded"
    assert (await _collection(db, SECOND)).name == "Renamed"
    assert _receipt(renamed) == {
        "ok": True,
        "tool_name": "rename_folder",
        "project_id": str(SECOND),
        "name": "Renamed",
    }
    removed = await request_action(
        db,
        actor,
        _call(
            "remove_papers_from_folder",
            document_ids=[str(PAPER), str(PAPER2)],
            project_id=P,
        ),
    )
    assert removed.state == "succeeded"
    assert not await _linked(db, PROJECT, PAPER)
    assert _receipt(removed) == {
        "ok": True,
        "tool_name": "remove_papers_from_folder",
        "project_id": P,
        "document_ids": [str(PAPER)],
        "skipped_document_ids": [str(PAPER2)],
    }


async def test_create_folder_binds_the_workspace_and_no_collection(
    db: AsyncSession, library: None
) -> None:
    status = await request_action(
        db,
        await _library_actor(db),
        _call("create_folder", name="Reading", description="To read"),
    )
    assert status.state == "succeeded"
    row = await _row(db, status.invocation_id)
    assert (row.project_id, row.workspace_id) == (None, WORKSPACE)
    created = await db.scalar(select(Collection).where(Collection.name == "Reading"))
    assert created is not None
    assert (created.workspace_id, created.description) == (WORKSPACE, "To read")
    assert _receipt(status) == {
        "ok": True,
        "tool_name": "create_folder",
        "project_id": str(created.id),
        "workspace_id": str(WORKSPACE),
        "name": "Reading",
    }


async def test_an_approved_deletion_soft_deletes_the_folder(
    db: AsyncSession, library: None
) -> None:
    status = await request_action(
        db, await _library_actor(db), _call("delete_folder", project_id=str(SECOND))
    )
    await _approve(db, status.invocation_id)
    done = await execute_action(db, status.invocation_id)
    assert done is not None and done.state == "succeeded"
    assert (await _collection(db, SECOND)).is_deleted is True
    assert _receipt(done) == {
        "ok": True,
        "tool_name": "delete_folder",
        "project_id": str(SECOND),
    }


async def test_update_document_metadata_targets_the_first_folder_by_name(
    db: AsyncSession, library: None
) -> None:
    # PAPER2 joins SECOND first, then ALPHA: by name, ALPHA comes first.
    await db.execute(
        insert(Collection).values(id=ALPHA, name="Alpha", workspace_id=WORKSPACE)
    )
    db.add(CollectionDocument(collection_id=SECOND, document_id=PAPER2))
    await db.flush()
    db.add(CollectionDocument(collection_id=ALPHA, document_id=PAPER2))
    await db.commit()
    status = await request_action(
        db,
        await _library_actor(db),
        _call(
            "update_document_metadata",
            document_id=str(PAPER2),
            title="Renamed",
            tags=["ml"],
        ),
    )
    assert status.state == "succeeded"
    row = await _row(db, status.invocation_id)
    assert (row.project_id, row.workspace_id) == (ALPHA, WORKSPACE)
    assert _receipt(status) == {
        "ok": True,
        "tool_name": "update_document_metadata",
        "project_id": str(ALPHA),
        "document_ids": [str(PAPER2)],
        # Every folder of the grant's scope that holds it, first by name.
        "project_ids": [str(ALPHA), str(SECOND)],
        "title": "Renamed",
        "tags": ["ml"],
        # What it replaced, so the edit can be undone.
        "previous_title": "Paper",
        "previous_tags": ["seed"],
    }
    document = await db.scalar(
        select(Document)
        .where(Document.id == PAPER2)
        .execution_options(populate_existing=True)
    )
    assert document is not None
    assert (document.title, list(document.tags)) == ("Renamed", ["ml"])


async def test_a_metadata_edit_records_only_what_it_replaced_and_can_be_undone(
    db: AsyncSession, library: None
) -> None:
    actor = await _library_actor(db)
    cleared = await request_action(
        db, actor, _call("update_document_metadata", document_id=str(PAPER), tags=[])
    )
    assert cleared.state == "succeeded"
    receipt = _receipt(cleared)
    assert (receipt["tags"], receipt["previous_tags"]) == ([], ["seed"])
    assert "title" not in receipt and "previous_title" not in receipt
    # The previous values are valid arguments: asking for them again undoes it.
    undone = await request_action(
        db,
        actor,
        _call(
            "update_document_metadata",
            document_id=str(PAPER),
            tags=receipt["previous_tags"],
        ),
    )
    assert undone.state == "succeeded"
    assert _receipt(undone)["previous_tags"] == []
    # A paper stored without tags (NULL) reads back as none.
    untagged = await db.get(Document, PAPER)
    assert untagged is not None
    untagged.tags = None
    await db.commit()
    retitled = await request_action(
        db,
        actor,
        _call(
            "update_document_metadata", document_id=str(PAPER), title="New", tags=["a"]
        ),
    )
    receipt = _receipt(retitled)
    assert (receipt["previous_title"], receipt["previous_tags"]) == ("Paper", [])
    document = await db.scalar(
        select(Document)
        .where(Document.id == PAPER)
        .execution_options(populate_existing=True)
    )
    assert document is not None
    assert (document.title, list(document.tags)) == ("New", ["a"])


@pytest.mark.parametrize(
    "case",
    [
        "in-no-folder",
        "only-in-another-workspace",
        "link-removed",
        "document-deleted",
        "another-organization",
    ],
)
async def test_update_document_metadata_needs_the_document_in_scope(
    db: AsyncSession, library: None, case: str
) -> None:
    document = PAPER2
    if case == "only-in-another-workspace":
        db.add(CollectionDocument(collection_id=ELSEWHERE, document_id=PAPER2))
    elif case == "link-removed":
        document = PAPER
        await db.execute(update(CollectionDocument).values(is_deleted=True))
    elif case == "document-deleted":
        document = PAPER
        await db.execute(
            update(Document).where(Document.id == PAPER).values(is_deleted=True)
        )
    elif case == "another-organization":
        document = uuid4()
        db.add(_paper(document, organization_id=ORG2))
        await db.flush()
        db.add(CollectionDocument(collection_id=PROJECT, document_id=document))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await request_action(
            db,
            await _library_actor(db),
            _call("update_document_metadata", document_id=str(document), title="T"),
        )
    assert await _row_count(db) == 0


async def test_ingest_under_a_workspace_grant_must_name_its_folder(
    db: AsyncSession, library: None
) -> None:
    actor = await _library_actor(db)
    with pytest.raises(ToolActionArgumentError) as unnamed:
        await request_action(
            db, actor, _call("ingest_arxiv_papers", paper_ids=["2401.00001"])
        )
    assert str(unnamed.value) == "project_id is required"
    assert await _row_count(db) == 0
    status = await request_action(
        db,
        actor,
        _call("ingest_arxiv_papers", paper_ids=["2401.00001"], project_id=str(SECOND)),
    )
    assert status.state == "awaiting_approval"
    row = await _row(db, status.invocation_id)
    assert (row.project_id, row.workspace_id) == (SECOND, WORKSPACE)


async def test_library_effect_failures_use_stable_reasons(
    db: AsyncSession, library: None
) -> None:
    # A viewer of WORKSPACE reaches PROJECT but may not change it.
    db.add(
        WorkspaceMember(
            workspace_id=WORKSPACE, user_id=OTHER_USER, role=WorkspaceRole.VIEWER
        )
    )
    await db.commit()
    viewer = ActionActor(
        user_id=OTHER_USER, organization_id=ORG, project_id=PROJECT, scopes=WRITE
    )
    status = await request_action(db, viewer, _rename_project())
    await decide_action(
        db, await _user(db, OTHER_USER), status.invocation_id, approved=True
    )
    refused = await execute_action(db, status.invocation_id)
    assert refused is not None and refused.state == "failed"
    assert await _last_error(db, status.invocation_id) == "insufficient permissions"
    assert _receipt(refused) == {
        "ok": False,
        "tool_name": "rename_folder",
        "error": "insufficient permissions",
    }
    assert await _project_name(db) == "Project"
    # Gone by the time it runs: the service finds nothing to change.
    gone = await request_action(db, NATIVE, _rename_project())
    await db.execute(
        update(Collection).where(Collection.id == PROJECT).values(is_deleted=True)
    )
    await db.commit()
    await _approve(db, gone.invocation_id)
    missing = await execute_action(db, gone.invocation_id)
    assert missing is not None and missing.state == "failed"
    assert await _last_error(db, gone.invocation_id) == "target not found"


async def test_move_destination_is_rechecked_when_it_runs(
    db: AsyncSession, library: None
) -> None:
    status = await request_action(
        db,
        await _library_actor(db, WRITE),
        _call(
            "move_papers_between_folders",
            document_ids=[str(PAPER)],
            from_project_id=P,
            to_project_id=str(SECOND),
        ),
    )
    await _approve(db, status.invocation_id)
    # After the decision the destination left the workspace's live folders.
    await db.execute(
        update(Collection).where(Collection.id == SECOND).values(is_deleted=True)
    )
    await db.commit()
    failed = await execute_action(db, status.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert await _last_error(db, status.invocation_id) == (
        "grant no longer authorizes this action"
    )
    assert await _linked(db, PROJECT, PAPER)


async def test_create_folder_rechecks_workspace_access_when_it_runs(
    db: AsyncSession, library: None
) -> None:
    status = await request_action(
        db, await _library_actor(db, WRITE), _call("create_folder", name="Reading")
    )
    await _approve(db, status.invocation_id)
    await db.execute(
        update(Workspace).where(Workspace.id == WORKSPACE).values(owner_id=OTHER_USER)
    )
    await db.commit()
    failed = await execute_action(db, status.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert await _last_error(db, status.invocation_id) == (
        "grant no longer authorizes this action"
    )
    created = await db.scalar(
        select(func.count()).select_from(Collection).where(Collection.name == "Reading")
    )
    assert created == 0


# What a project connection can lose between the decision and the run, each a
# state authorized_project refuses: the binding (project_id) alone still matches.
ACCESS_LOST: dict[str, tuple[Any, dict[str, Any]]] = {
    "workspace-deleted": (
        update(Workspace).where(Workspace.id == WORKSPACE),
        {"is_deleted": True},
    ),
    "membership-removed": (
        update(Workspace).where(Workspace.id == WORKSPACE),
        {"owner_id": OTHER_USER},
    ),
    "organization-deactivated": (
        update(Organization).where(Organization.id == ORG),
        {"is_active": False},
    ),
    "project-deleted": (
        update(Collection).where(Collection.id == PROJECT),
        {"is_deleted": True},
    ),
}


@pytest.mark.parametrize("lost", list(ACCESS_LOST))
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("update_document_metadata", {"document_id": str(PAPER), "title": "Hacked"}),
        ("remove_papers_from_folder", {"document_ids": [str(PAPER)], "project_id": P}),
        ("create_project_note", NOTE_ARGS),
    ],
    ids=[
        "update_document_metadata",
        "remove_papers_from_folder",
        "create_project_note",
    ],
)
async def test_a_project_bound_action_rechecks_live_access_when_it_runs(
    db: AsyncSession,
    library: None,
    lost: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    # The same live check a workspace-bound row gets: a project grant's row
    # must not run once the user could no longer reach the project, whatever
    # the effect itself would still have allowed.
    status = await request_action(db, _actor(), _call(tool_name, **arguments))
    assert status.state == "awaiting_approval"
    row = await _row(db, status.invocation_id)
    assert (row.project_id, row.workspace_id) == (PROJECT, None)
    await _approve(db, status.invocation_id)
    statement, values = ACCESS_LOST[lost]
    await db.execute(statement.values(**values))
    await db.commit()
    failed = await execute_action(db, status.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert await _last_error(db, status.invocation_id) == (
        "grant no longer authorizes this action"
    )
    assert await _linked(db, PROJECT, PAPER)
    document = await db.scalar(
        select(Document)
        .where(Document.id == PAPER)
        .execution_options(populate_existing=True)
    )
    assert document is not None and document.title == "Paper"
    assert await _note_count(db) == 0


# --- the arXiv ingest: approval path, outside the action's transaction -----


def _fake_ingest(
    monkeypatch: pytest.MonkeyPatch, outcome: Any, calls: list[dict[str, Any]]
) -> None:
    from src.services.agent import tools_impl

    async def ingest(
        args: dict[str, Any], user_id: str, session: AsyncSession, user: User
    ) -> Any:
        calls.append(
            {
                "args": args,
                "user_id": user_id,
                "user": user.id,
                # The ingest commits through sessions of its own and takes
                # minutes: nothing of the action may hold a transaction open.
                "in_transaction": session.in_transaction(),
            }
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(tools_impl, "_tool_ingest_arxiv", ingest)


async def _approved_ingest(
    db: AsyncSession, *paper_ids: str
) -> tuple[ActionActor, UUID]:
    actor = await _library_actor(db, WRITE)
    status = await request_action(
        db,
        actor,
        _call("ingest_arxiv_papers", paper_ids=list(paper_ids), project_id=str(SECOND)),
    )
    await _approve(db, status.invocation_id)
    return actor, status.invocation_id


async def test_ingest_runs_through_the_drain_outside_the_action_transaction(
    db: AsyncSession, library: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _fake_ingest(
        monkeypatch,
        {
            "status": "ingestion_complete",
            "paper_ids": ["2401.00001"],
            "document_ids": [LANDED],
            "failed_papers": [],
            "project_id": str(SECOND),
            "message": "Ingested 1 paper(s) into the RAG system.",
        },
        calls,
    )

    async def transactional_effect(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an ingest never runs inside the action's transaction")

    monkeypatch.setattr(tool_actions, "_run_effect", transactional_effect)
    actor, invocation_id = await _approved_ingest(db, "2401.00001")
    # The same worker drain that runs approved notes.
    assert await drain_integration_actions(db) == 1
    assert calls == [
        {
            "args": {"paper_ids": ["2401.00001"], "project_id": str(SECOND)},
            "user_id": str(USER),
            "user": USER,
            "in_transaction": False,
        }
    ]
    done = await get_action_status(db, actor, invocation_id)
    assert done.state == "succeeded"
    assert _receipt(done) == {
        "ok": True,
        "tool_name": "ingest_arxiv_papers",
        "project_id": str(SECOND),
        "paper_ids": ["2401.00001"],
        "document_ids": [LANDED],
        "failed_paper_ids": [],
        "ingest_status": "ingestion_complete",
        "atomicity": tool_actions.INGEST_ATOMICITY,
    }
    assert done.result is not None
    assert done.result.source_refs == [{"document_id": LANDED}]
    # Claimed once: neither the drain nor a direct call runs it again.
    assert await drain_integration_actions(db) == 0
    assert await execute_action(db, invocation_id) is None
    assert len(calls) == 1


SECRET = "s3cr3t from the adapter"


@pytest.mark.parametrize(
    ("outcome", "state", "reason", "document_ids", "failed_paper_ids"),
    [
        (
            {
                "status": "ingestion_failed",
                "document_ids": [],
                "failed_papers": [
                    {"paper_id": "2401.00001", "reason": SECRET},
                    {"paper_id": "2401.00002", "reason": SECRET},
                ],
                "error": f"Ingested 0 of 2 paper(s). Details: {SECRET}",
            },
            "failed",
            "ingest failed",
            [],
            ["2401.00001", "2401.00002"],
        ),
        (
            {
                "status": "ingestion_partial",
                "document_ids": [LANDED],
                "failed_papers": [{"paper_id": "2401.00002", "reason": SECRET}],
                "error": f"Ingested 1 of 2 paper(s). {SECRET}",
            },
            "failed",
            "ingest partially failed",
            [LANDED],
            ["2401.00002"],
        ),
        (
            {
                "status": "ingestion_complete_link_failed",
                "document_ids": [LANDED],
                "failed_papers": [],
                "link_error": f"Project link failed: {SECRET}",
            },
            "failed",
            "papers ingested but not saved to the folder",
            [LANDED],
            [],
        ),
        (
            {"error": SECRET, "error_type": "internal"},
            "failed",
            "ingest failed",
            [],
            [],
        ),
        (RuntimeError(SECRET), "outcome_unknown", "ingest outcome unknown", None, None),
    ],
    ids=["nothing-landed", "partial", "not-saved-to-folder", "adapter-error", "raised"],
)
async def test_ingest_outcomes_are_recorded_with_stable_reasons(
    db: AsyncSession,
    library: None,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Any,
    state: str,
    reason: str,
    document_ids: list[str] | None,
    failed_paper_ids: list[str] | None,
) -> None:
    calls: list[dict[str, Any]] = []
    _fake_ingest(monkeypatch, outcome, calls)
    actor, invocation_id = await _approved_ingest(db, "2401.00001", "2401.00002")
    assert await drain_integration_actions(db) == 1
    row = await _row(db, invocation_id)
    assert (row.state, row.last_error) == (state, reason)
    # Fixed text only: the adapter's messages and exceptions never reach it.
    assert SECRET not in str(row.result) and SECRET not in str(row.last_error)
    if document_ids is None:
        assert row.result is None
    else:
        assert row.result is not None and row.result["is_error"] is True
        assert row.result["content"] == [
            {
                "ok": False,
                "tool_name": "ingest_arxiv_papers",
                "project_id": str(SECOND),
                "paper_ids": ["2401.00001", "2401.00002"],
                "document_ids": document_ids,
                "failed_paper_ids": failed_paper_ids,
                "ingest_status": outcome.get("status", "ingestion_failed"),
                "atomicity": tool_actions.INGEST_ATOMICITY,
                "error": reason,
            }
        ]
    assert (await get_action_status(db, actor, invocation_id)).state == state
    assert len(calls) == 1


def _stalled_ingest(
    monkeypatch: pytest.MonkeyPatch, started: asyncio.Event, seen: dict[str, bool]
) -> None:
    """An ingest that never returns on its own, as a hung arXiv fetch would."""
    from src.services.agent import tools_impl

    async def stalled(*_args: Any, **_kwargs: Any) -> Any:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    monkeypatch.setattr(tools_impl, "_tool_ingest_arxiv", stalled)


async def test_an_ingest_that_outlasts_its_timeout_ends_outcome_unknown(
    db: AsyncSession, library: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = {"cancelled": False}
    _stalled_ingest(monkeypatch, asyncio.Event(), seen)
    monkeypatch.setattr(tool_actions, "DETACHED_TIMEOUT", timedelta(milliseconds=50))
    _, invocation_id = await _approved_ingest(db, "2401.00001")
    # Bounded, so a missing timeout fails here instead of hanging the suite.
    assert await asyncio.wait_for(drain_integration_actions(db), timeout=10) == 1
    row = await _row(db, invocation_id)
    # Recorded by the worker at once, not left `executing` for the sweeper.
    assert (row.state, row.last_error, row.result) == (
        "outcome_unknown",
        "ingest outcome unknown",
        None,
    )
    assert seen["cancelled"] is True
    assert await drain_integration_actions(db) == 0


async def test_a_drain_cancelled_mid_ingest_records_the_outcome_unknown(
    db: AsyncSession, library: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, seen = asyncio.Event(), {"cancelled": False}
    _stalled_ingest(monkeypatch, started, seen)
    _, invocation_id = await _approved_ingest(db, "2401.00001")
    drain = asyncio.create_task(drain_integration_actions(db))
    await asyncio.wait_for(started.wait(), timeout=10)
    # What Celery's soft time limit does to a running drain: run_async
    # cancels the coroutine.
    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain
    row = await _row(db, invocation_id)
    assert (row.state, row.last_error) == ("outcome_unknown", "ingest outcome unknown")
    assert seen["cancelled"] is True


async def test_a_drain_starts_an_ingest_only_while_its_whole_timeout_fits(
    db: AsyncSession, library: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.agent import tools_impl

    clock = {"now": 1000.0}
    monkeypatch.setattr(tool_actions, "_clock", lambda: clock["now"])
    calls: list[list[str]] = []

    async def slow_ingest(args: dict[str, Any], *_args: Any, **_kwargs: Any) -> Any:
        calls.append(list(args["paper_ids"]))
        clock["now"] += 150  # most of the drain's budget
        return {
            "status": "ingestion_complete",
            "document_ids": [LANDED],
            "failed_papers": [],
            "project_id": str(SECOND),
        }

    monkeypatch.setattr(tools_impl, "_tool_ingest_arxiv", slow_ingest)
    _, first = await _approved_ingest(db, "2401.00001")
    _, second = await _approved_ingest(db, "2401.00002")
    assert await drain_integration_actions(db) == 1
    assert (await _row(db, first)).state == "succeeded"
    # 90 s of this drain's budget left, less than an ingest may take: it waits,
    # still approved and unclaimed, for a drain with time to bound it.
    assert (await _row(db, second)).state == "approved"
    assert calls == [["2401.00001"]]
    assert await drain_integration_actions(db) == 1
    assert (await _row(db, second)).state == "succeeded"
    assert calls == [["2401.00001"], ["2401.00002"]]


def test_an_ingest_ends_inside_every_limit_that_could_cut_it_short() -> None:
    from src.tasks.integration_action_tasks import drain_integration_actions as task

    timeout = tool_actions.DETACHED_TIMEOUT
    budget = tool_actions.DRAIN_BUDGET
    # A drain starts an ingest only while the whole timeout fits in its budget,
    # and the sweeper never calls a bounded ingest stale.
    assert timeout < budget
    assert timeout < tool_actions.STALE_EXECUTION
    # Celery cancels the drain at its soft limit and kills it at its hard one:
    # both only after the budget a drain may spend.
    assert task.soft_time_limit is not None and task.time_limit is not None
    assert budget.total_seconds() < task.soft_time_limit < task.time_limit


# --- review: one summary sentence, and the stored arguments ----------------

# Distinct titles, so a summary that names the wrong paper fails.
TITLES = {PAPER: "Attention Is All You Need", PAPER2: "BERT"}
# Each library action as a tools:write workspace grant asks for it (so it waits
# for a decision), with the sentence the review page leads with.
SUMMARIES: dict[str, tuple[str, dict[str, Any], str]] = {
    "save": (
        "save_papers_to_folder",
        {"document_ids": [str(PAPER), str(PAPER2)], "project_id": str(SECOND)},
        "Save 2 papers to folder “Second”: “Attention Is All You Need”, “BERT”",
    ),
    "remove": (
        "remove_papers_from_folder",
        {"document_ids": [str(PAPER)], "project_id": P},
        "Remove 1 paper from folder “Project” (documents are kept): "
        "“Attention Is All You Need”",
    ),
    "move": (
        "move_papers_between_folders",
        {
            "document_ids": [str(PAPER)],
            "from_project_id": P,
            "to_project_id": str(SECOND),
        },
        "Move 1 paper from folder “Project” to folder “Second”: "
        "“Attention Is All You Need”",
    ),
    "create-folder": (
        "create_folder",
        {"name": "Reading", "description": "Papers to read"},
        "Create folder “Reading” in workspace “Workspace”",
    ),
    "rename-folder": (
        "rename_folder",
        {"project_id": P, "name": "Read"},
        "Rename folder “Project” to “Read”",
    ),
    "delete-folder": (
        "delete_folder",
        {"project_id": str(SECOND)},
        "Delete folder “Second” (documents are kept)",
    ),
    "retitle": (
        "update_document_metadata",
        {"document_id": str(PAPER), "title": "Attention (v2)"},
        "Rename paper “Attention Is All You Need” to “Attention (v2)”",
    ),
    "retag": (
        "update_document_metadata",
        {"document_id": str(PAPER), "tags": ["ml", "nlp"]},
        "Set the tags of paper “Attention Is All You Need” to “ml”, “nlp”",
    ),
    "untag": (
        "update_document_metadata",
        {"document_id": str(PAPER), "tags": []},
        "Remove every tag from paper “Attention Is All You Need”",
    ),
    "retitle-and-retag": (
        "update_document_metadata",
        {"document_id": str(PAPER), "title": "T", "tags": ["ml"]},
        "Rename paper “Attention Is All You Need” to “T” and set its tags to “ml”",
    ),
    "retitle-and-untag": (
        "update_document_metadata",
        {"document_id": str(PAPER), "title": "T", "tags": []},
        "Rename paper “Attention Is All You Need” to “T” and remove every tag",
    ),
    "ingest": (
        "ingest_arxiv_papers",
        {"paper_ids": ["2401.00001", "math.GT/0309136"], "project_id": str(SECOND)},
        "Ingest 2 arXiv papers into folder “Second”: 2401.00001, math.GT/0309136",
    ),
}


@pytest.fixture
async def titled(db: AsyncSession, library: None) -> None:
    for document_id, title in TITLES.items():
        await db.execute(
            update(Document).where(Document.id == document_id).values(title=title)
        )
    await db.commit()


async def _stored_review(
    db: AsyncSession, tool_name: str, arguments: dict[str, Any], target: UUID | None
) -> Any:
    """Review a workspace row stored as given, bypassing the request checks."""
    row = await _workspace_row(
        db,
        tool_name=tool_name,
        arguments=arguments,
        argument_hash=canonical_hash(tool_name, arguments),
        project_id=target,
    )
    return await get_action_for_review(db, await _user(db), row.invocation_id)


@pytest.mark.parametrize(
    ("tool_name", "arguments", "summary"),
    list(SUMMARIES.values()),
    ids=list(SUMMARIES),
)
async def test_review_summarises_each_library_action_and_lists_its_arguments(
    db: AsyncSession,
    titled: None,
    tool_name: str,
    arguments: dict[str, Any],
    summary: str,
) -> None:
    status = await request_action(
        db, await _library_actor(db, WRITE), _call(tool_name, **arguments)
    )
    assert status.state == "awaiting_approval"
    review = await get_action_for_review(db, await _user(db), status.invocation_id)
    assert review.tool_name == tool_name
    assert review.summary == summary
    # Exactly what is stored and will run, selectors included, so the page can
    # list what the sentence names.
    assert review.arguments == arguments
    # The note fields describe a note only: a document edit's title and tags
    # never pose as one.
    assert (review.title, review.content, review.tags) == ("", "", [])


async def test_review_of_a_note_keeps_its_fields_and_names_where_it_lands(
    db: AsyncSession,
) -> None:
    status = await request_action(db, _actor(), _invocation(tags=["x"]))
    review = await get_action_for_review(db, await _user(db), status.invocation_id)
    assert review.summary == "Create note “Findings” in project “Project”"
    assert review.arguments == {**NOTE_ARGS, "tags": ["x"]}
    assert (review.title, review.content, review.tags) == (
        "Findings",
        "# Findings\n",
        ["x"],
    )
    # A workspace-level row names its workspace instead.
    row = await _workspace_row(db)
    review = await get_action_for_review(db, await _user(db), row.invocation_id)
    assert review.summary == "Create note “Findings” in workspace “Workspace”"


async def test_review_names_only_live_documents_of_the_users_organization(
    db: AsyncSession, titled: None
) -> None:
    # A harness may name any id; the service skips foreign and deleted
    # documents, and the review must not read out their titles either.
    foreign, retired = uuid4(), uuid4()
    db.add_all([_paper(foreign, organization_id=ORG2), _paper(retired)])
    await db.flush()
    await db.execute(
        update(Document)
        .where(Document.id == foreign)
        .values(title="Their confidential paper")
    )
    await db.execute(
        update(Document)
        .where(Document.id == retired)
        .values(title="Withdrawn paper", is_deleted=True)
    )
    await db.commit()
    arguments = {
        "document_ids": [str(PAPER), str(foreign), str(retired)],
        "project_id": str(SECOND),
    }
    review = await _stored_review(db, "save_papers_to_folder", arguments, SECOND)
    # A paper it cannot name is shown by its id.
    assert review.summary == (
        "Save 3 papers to folder “Second”: “Attention Is All You Need”, "
        f"{foreign}, {retired}"
    )
    assert "confidential" not in review.summary
    assert "Withdrawn" not in review.summary
    metadata = await _stored_review(
        db,
        "update_document_metadata",
        {"document_id": str(foreign), "title": "Mine now"},
        SECOND,
    )
    assert metadata.summary == f"Rename paper {foreign} to “Mine now”"


async def test_review_names_folders_only_within_the_actions_workspace(
    db: AsyncSession, titled: None
) -> None:
    # The request path refuses a destination outside the grant's workspace; a
    # row that names one anyway never reads out a foreign folder's name.
    theirs, secret = uuid4(), uuid4()
    await db.execute(
        insert(Workspace).values(
            id=theirs, name="Theirs", owner_id=OTHER_USER, organization_id=ORG2
        )
    )
    await db.execute(
        insert(Collection).values(id=secret, name="Secret folder", workspace_id=theirs)
    )
    await db.commit()
    arguments = {
        "document_ids": [str(PAPER)],
        "from_project_id": P,
        "to_project_id": str(secret),
    }
    review = await _stored_review(db, "move_papers_between_folders", arguments, PROJECT)
    assert review.summary == (
        f"Move 1 paper from folder “Project” to folder {secret}: "
        "“Attention Is All You Need”"
    )


async def test_review_of_an_unknown_action_still_loads(db: AsyncSession) -> None:
    # Never a 500 on the decision page: an action this server no longer
    # catalogues is named as stored and can still be denied.
    review = await _stored_review(db, "forget_memory", {"key": "x"}, None)
    assert review.summary == "Run the action “forget_memory”"
    assert review.arguments == {"key": "x"}
    assert (review.title, review.content, review.tags) == ("", "", [])

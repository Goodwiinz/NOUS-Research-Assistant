"""Audit B2 (note creation) — dedup parity + service-method unit tests.

Before the dedup, the REST route ``POST /projects/{id}/notes`` and the agent
``create_project_note`` tool each constructed a ``ProjectNote`` independently.
They now both delegate to :meth:`ProjectService.create_note` and act as thin
adapters (the "adapter pattern" for the B2 family):

  - the route keeps its ``_get_project_with_auth`` guard, then calls the
    service and returns its ``NoteResponse`` schema;
  - the tool keeps its ``_verify_project_ownership`` guard (name resolution +
    soft-delete exclusion), then calls the service and returns its dict.

These tests prove the two adapters produce *identical DB effects* for the same
logical note, on a throwaway SQLite engine, and unit-test the shared service
method directly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.models.collection import Collection
from src.models.project_note import ProjectNote
from src.models.research_project import ResearchProject
from src.models.research_project_role import ResearchProjectRoleAssignment
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.services.research.project_service import ProjectService

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# SQLite fixtures — only the tables the note path touches (the shared Base
# carries postgres-only types elsewhere, so never create_all the full metadata)
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(Workspace.__table__.create)
        await conn.run_sync(WorkspaceMember.__table__.create)
        await conn.run_sync(Collection.__table__.create)
        await conn.run_sync(ProjectNote.__table__.create)
        await conn.run_sync(ResearchProject.__table__.create)
        await conn.run_sync(ResearchProjectRoleAssignment.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_project(factory) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert an owned workspace + collection; return (user_id, project_id)."""
    user_id = uuid.uuid4()
    async with factory() as db:
        await db.execute(
            text(
                """INSERT INTO users
            (id,email,password_hash,first_name,last_name,role,is_active,login_count,created_at,updated_at,is_deleted)
            VALUES (:id,:email,'test','Test','Owner','USER',true,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,false)"""
            ),
            {"id": str(user_id), "email": f"{user_id}@example.test"},
        )
        ws = Workspace(id=uuid.uuid4(), name="ws", owner_id=user_id)
        db.add(ws)
        await db.commit()
        proj = Collection(id=uuid.uuid4(), workspace_id=ws.id, name="Proj")
        db.add(proj)
        await db.commit()
        return user_id, proj.id


def _fields(note: ProjectNote) -> dict:
    """The persisted fields that must match across both callers."""
    return {
        "project_id": note.project_id,
        "user_id": note.user_id,
        "title": note.title,
        "content": note.content,
        "tags": list(note.tags or []),
        "linked_document_ids": list(note.linked_document_ids or []),
        "is_pinned": note.is_pinned,
    }


# ---------------------------------------------------------------------------
# ProjectService.create_note — unit
# ---------------------------------------------------------------------------


async def test_create_note_persists_all_fields(session_factory):
    user_id, project_id = await _seed_project(session_factory)
    async with session_factory() as db:
        note = await ProjectService(db).create_note(
            user_id=user_id,
            project_id=project_id,
            title="Title",
            content="Body",
            tags=["x", "y"],
            linked_document_ids=[str(uuid.uuid4())],
            is_pinned=True,
        )
        assert note.id is not None
        assert note.title == "Title"
        assert note.content == "Body"
        assert note.tags == ["x", "y"]
        assert len(note.linked_document_ids) == 1
        assert note.is_pinned is True
        # Actually persisted (visible from a fresh read on the same engine).
    async with session_factory() as db2:
        rows = (await db2.execute(select(ProjectNote))).scalars().all()
        assert len(rows) == 1
        assert rows[0].project_id == project_id


async def test_create_note_defaults_empty_collections(session_factory):
    """Omitted tags / linked_document_ids default to [] (not None)."""
    user_id, project_id = await _seed_project(session_factory)
    async with session_factory() as db:
        note = await ProjectService(db).create_note(
            user_id=user_id,
            project_id=project_id,
            title="T",
            content="C",
        )
        assert note.tags == []
        assert note.linked_document_ids == []
        assert note.is_pinned is False


# ---------------------------------------------------------------------------
# Parity: route adapter vs tool adapter → identical DB effects
# ---------------------------------------------------------------------------


async def test_route_and_tool_produce_identical_note(session_factory, monkeypatch):
    from src.api.research.projects import create_note as route_create_note
    from src.services.agent import tools_impl
    from src.shared.research_schemas import NoteCreate

    user_id, project_id = await _seed_project(session_factory)
    current_user = SimpleNamespace(id=user_id)

    # --- Route path -------------------------------------------------------
    async with session_factory() as db:
        note_data = NoteCreate(
            title="Shared title",
            content="Shared body",
            tags=["a", "b"],
            linked_document_ids=[],
            is_pinned=False,
        )
        route_resp = await route_create_note(
            project_id=project_id,
            note_data=note_data,
            current_user=current_user,
            db=db,
        )
    route_note_id = route_resp.id

    # --- Tool path --------------------------------------------------------
    # The tool writes via the per-call session it is handed (audit B8).
    async with session_factory() as tool_db:
        tool_result = await tools_impl._tool_create_project_note(
            {
                "project_id": str(project_id),
                "title": "Shared title",
                "content": "Shared body",
                "tags": ["a", "b"],
            },
            db=tool_db,
            current_user=current_user,
        )
    assert tool_result["status"] == "success"
    tool_note_id = uuid.UUID(tool_result["note_id"])

    # --- Compare the two persisted rows field-for-field -------------------
    async with session_factory() as db:
        route_note = await db.get(ProjectNote, route_note_id)
        tool_note = await db.get(ProjectNote, tool_note_id)

    assert route_note is not None and tool_note is not None
    assert route_note.id != tool_note.id  # two distinct rows
    assert _fields(route_note) == _fields(tool_note)
    # And the shared logical content is exactly what we asked for.
    assert _fields(route_note) == {
        "project_id": project_id,
        "user_id": user_id,
        "title": "Shared title",
        "content": "Shared body",
        "tags": ["a", "b"],
        "linked_document_ids": [],
        "is_pinned": False,
    }


async def test_tool_rejects_unowned_project(session_factory, monkeypatch):
    """Tool adapter preserves its dict error shape when ownership fails."""
    from src.services.agent import tools_impl

    _, project_id = await _seed_project(session_factory)
    # A different user does NOT own the project.
    other_user = SimpleNamespace(id=uuid.uuid4())

    async with session_factory() as tool_db:
        result = await tools_impl._tool_create_project_note(
            {
                "project_id": str(project_id),
                "title": "T",
                "content": "C",
            },
            db=tool_db,
            current_user=other_user,
        )
    assert result == {"error": "Project not found or access denied"}

"""Soft-deleted projects must not be listed or fetched.

Live on dev, 2026-07-27. The writing agent called ``list_projects``, took an
id straight from the result, and ``create_project_note`` refused it::

    {"projects": [{"id": "96012179-65b7-428e-a744-2207e3fddb3d",
                   "name": "synthtraffic-20260726T184029", ...}]}
    {"error": "Project not found or access denied", "error_type": "recoverable"}

That project is soft-deleted, and so were **6 of the 12** projects the service
returned for that user. ``ProjectService`` never mentioned ``is_deleted`` at
all, while every write path filters it (``_verify_project_ownership``,
``project_skills.access``, the RAG node). So the service handed out ids that
the rest of the system then rejected, and the agent burned its retry budget
on them.

Asserted against compiled SQL rather than mocked rows: the bug was a missing
predicate, and only the statement itself proves a predicate is present.
"""

from __future__ import annotations

from typing import Any, List
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, value: Any = None, rows: List[Any] | None = None) -> None:
        self._value = value
        self._rows = rows or []

    def scalar(self) -> Any:
        return self._value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def all(self) -> List[Any]:
        return self._rows

    def scalars(self) -> "_Result":
        return self

    def unique(self) -> "_Result":
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _DB:
    """Captures every statement so the emitted SQL can be inspected."""

    def __init__(self, workspace_id: Any) -> None:
        self.statements: List[Any] = []
        self._workspace_id = workspace_id
        self._calls = 0

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> _Result:
        self.statements.append(stmt)
        self._calls += 1
        if self._calls == 1:
            # _get_workspace_ids_for_user
            return _Result(rows=[(self._workspace_id,)])
        return _Result(value=0, rows=[])

    def where(self, index: int) -> str:
        """The WHERE clause only.

        ``select(Collection)`` renders every column, so ``is_deleted`` appears
        in the SELECT list of an unfiltered query too — asserting against the
        full statement passes without the predicate. Only the whereclause
        proves the filter is applied.
        """
        clause = getattr(self.statements[index], "whereclause", None)
        return str(clause).lower() if clause is not None else ""

    def sql(self, index: int) -> str:
        return str(self.statements[index]).lower()


async def _run_list() -> _DB:
    from src.services.research.project_service import ProjectService

    db = _DB(workspace_id=uuid4())
    with patch(
        "src.services.research.project_service.accessible_research_workspace_ids",
        new=AsyncMock(return_value=[db._workspace_id]),
    ):
        await ProjectService(db).list_projects(user_id=uuid4())
    return db


class TestListProjects:
    async def test_list_filters_out_soft_deleted(self) -> None:
        db = await _run_list()

        clauses = [db.where(i) for i in range(len(db.statements))]
        assert clauses, "no project query was issued"
        for clause in clauses:
            assert "is_deleted is false" in clause, (
                "list_projects returned soft-deleted projects, which every "
                "write path then rejects as 'not found or access denied'"
            )

    async def test_count_and_page_share_the_filter(self) -> None:
        """A count that ignores the filter over-reports total and has_more."""
        db = await _run_list()

        indexes = range(len(db.statements))
        count = [db.where(i) for i in indexes if "count" in db.sql(i)]
        page = [db.where(i) for i in indexes if "count" not in db.sql(i)]

        assert count and page, "expected both a count query and a page query"
        assert all("is_deleted is false" in c for c in count)
        assert all("is_deleted is false" in c for c in page)


class TestNoUnfilteredOwnershipQueries:
    """The predicate was missing in nine places, not one.

    Every project-ownership query is a hand-rolled copy of the same
    ``Collection.id == … AND Workspace.owner_id == current_user.id`` pair, and
    each copy that forgets ``is_deleted`` re-creates the bug for its own
    feature. A static sweep guards all of them at once; individual tests would
    have to be remembered for the tenth copy.
    """

    @staticmethod
    def _offenders(text: str) -> List[int]:
        """Ownership queries over Collection that don't exclude deleted rows.

        Anchored on ``Workspace.owner_id ==`` with a window scanned in *both*
        directions, because a filter written above the id clause is still a
        filter — an append-only window reports correct code as an offender.
        The owner is matched regardless of the variable name: ``user_id`` and
        ``user_uuid`` are both already house style (_nodes_rag, project_service),
        so keying on ``current_user.id`` would miss them.

        Requires ``Collection.is_deleted`` specifically: a bare ``is_deleted``
        substring is satisfied by ``ProjectThread.is_deleted`` sitting in the
        same WHERE, which is exactly how the project_chat unlink query hid.
        """
        import re

        lines = text.splitlines()
        offenders: List[int] = []
        for index, line in enumerate(lines):
            if "Workspace.owner_id ==" not in line:
                continue
            window = "\n".join(lines[max(0, index - 10) : index + 11])
            touches_collection = (
                "Collection.id" in window or "Collection.workspace_id" in window
            )
            if not touches_collection:
                continue
            guarded = re.search(
                r"Collection\.is_deleted\s*(?:\.is_\(\s*False\s*\)|==\s*False)",
                window,
            )
            if not guarded:
                offenders.append(index + 1)
        return offenders

    def test_the_sweep_detects_what_it_claims_to(self) -> None:
        """A guard nobody has tested is a guard that passes vacuously."""
        unfiltered = (
            "select(Collection)\n"
            ".join(Workspace, Collection.workspace_id == Workspace.id)\n"
            ".where(and_(Collection.id == pid, Workspace.owner_id == user_id))\n"
        )
        assert self._offenders(unfiltered), "must flag an unfiltered query"

        sibling_only = unfiltered.replace(
            "Collection.id == pid", "Collection.id == pid, ProjectThread.is_deleted"
        )
        assert self._offenders(
            sibling_only
        ), "ProjectThread.is_deleted must not count as guarding Collection"

        wrong_polarity = unfiltered.replace(
            "Workspace.owner_id == user_id",
            "Collection.is_deleted.is_(True), Workspace.owner_id == user_id",
        )
        assert self._offenders(wrong_polarity), "is_(True) must not count as guarded"

        filter_first = (
            "select(Collection)\n"
            ".where(and_(Collection.is_deleted.is_(False),\n"
            "Collection.id == pid, Workspace.owner_id == current_user.id))\n"
        )
        assert not self._offenders(
            filter_first
        ), "a filter written above the id clause is still a filter"

    def test_every_ownership_query_filters_soft_deleted(self) -> None:
        from pathlib import Path

        src = Path(__file__).resolve().parents[3] / "src"
        offenders = []
        for path in src.rglob("*.py"):
            for line in self._offenders(path.read_text()):
                offenders.append(f"{path.relative_to(src)}:{line}")

        assert not offenders, (
            "these ownership queries can hand out soft-deleted projects that "
            "every write path then rejects: " + ", ".join(offenders)
        )


class TestGetProjectForUser:
    async def test_fetch_delegates_to_shared_project_boundary(self) -> None:
        """The shared resolver owns ancestor and soft-delete enforcement."""
        from fastapi import HTTPException

        from src.services.research.project_service import ProjectService
        from src.services.research_engine.project_access import ResearchAction

        db = _DB(workspace_id=uuid4())
        project_id = uuid4()
        user_id = uuid4()

        with patch(
            "src.services.research.project_service.resolve_project",
            new=AsyncMock(
                side_effect=HTTPException(status_code=404, detail="Project not found")
            ),
        ) as resolve:
            with pytest.raises(HTTPException):
                await ProjectService(db).get_project_for_user(
                    project_id=project_id, user_id=user_id
                )

        resolve.assert_awaited_once_with(db, project_id, user_id, ResearchAction.VIEW)
        assert not db.statements

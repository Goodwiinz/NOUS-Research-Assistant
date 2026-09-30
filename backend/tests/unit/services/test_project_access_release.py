"""``ResearchAction.RELEASE`` needs an adjudicator or a supervisor (GOO-307).

``resolve_project`` runs against a scripted session: each ``execute`` returns
the next canned result, in the function's query order (Workspace lock,
lifecycle reload, organizations, roles, engine). The single-role actions keep
their exact 403 detail.
"""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.models.research_project_role import ResearchProjectRole as Role
from src.models.workspace import WorkspaceRole
from src.services.research_engine import project_access
from src.services.research_engine.project_access import ResearchAction, resolve_project

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def first(self) -> Any:
        return self.value

    def scalar_one_or_none(self) -> Any:
        return self.value

    def scalars(self) -> "_Result":
        return self

    def all(self) -> Any:
        return self.value


class _Session:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)

    async def execute(self, _statement: Any) -> _Result:
        return _Result(self.results.pop(0))


async def _resolve(
    monkeypatch: pytest.MonkeyPatch,
    action: ResearchAction,
    roles: list[Role],
    owner: bool = False,
) -> Any:
    user, org = uuid4(), uuid4()
    workspace = SimpleNamespace(
        id=uuid4(),
        owner_id=user if owner else uuid4(),
        organization_id=org,
        is_deleted=False,
        is_archived=False,
        members=[
            SimpleNamespace(user_id=user, is_deleted=False, role=WorkspaceRole.VIEWER)
        ],
    )
    collection = SimpleNamespace(
        id=uuid4(),
        workspace=workspace,
        workspace_id=workspace.id,
        is_deleted=False,
        research_status="active",
    )

    async def get_collection(*_args: Any, **_kwargs: Any) -> Any:
        return collection

    async def lock(*_args: Any) -> None:
        return None

    monkeypatch.setattr(
        project_access.workspace_access, "get_collection", get_collection
    )
    monkeypatch.setattr(project_access, "lock_active_project", lock)
    db = _Session(None, (collection, workspace), org, roles, [])
    return await resolve_project(db, collection.id, user, action)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_release_requires_adjudicator_or_supervisor_owner_gets_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_roles: list[list[Role]] = [[], [Role.REVIEWER]]
    for roles in no_roles:
        with pytest.raises(HTTPException) as denied:
            await _resolve(monkeypatch, ResearchAction.RELEASE, roles, owner=True)
        assert denied.value.status_code == 403
        assert denied.value.detail == "adjudicator or supervisor role required"
    for role in (Role.ADJUDICATOR, Role.SUPERVISOR):
        context = await _resolve(monkeypatch, ResearchAction.RELEASE, [role])
        assert role in context.effective_roles


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "detail"),
    [
        (ResearchAction.REVIEW, "reviewer role required"),
        (ResearchAction.ADJUDICATE, "adjudicator role required"),
        (ResearchAction.SUPERVISE, "supervisor role required"),
    ],
)
async def test_single_role_actions_keep_their_messages(
    monkeypatch: pytest.MonkeyPatch, action: ResearchAction, detail: str
) -> None:
    with pytest.raises(HTTPException) as denied:
        await _resolve(monkeypatch, action, [], owner=True)
    assert (denied.value.status_code, denied.value.detail) == (403, detail)

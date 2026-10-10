"""Persisted lock ordering for scientific authority and workspace revocation."""

import asyncio
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.collection import Collection
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.services.threads import workspace_service
from tests.integration.test_research_project_mapping import (  # noqa: F401
    mapping_session_factory,
)


async def _seed(factory: async_sessionmaker[AsyncSession]) -> dict[str, UUID]:
    ids = {key: uuid4() for key in ("owner", "supervisor", "workspace", "project")}
    async with factory() as db:
        for key in ("owner", "supervisor"):
            db.add(
                User(
                    id=ids[key],
                    email=f"{ids[key]}@test.invalid",
                    password_hash="unused",
                    first_name="Test",
                    last_name=key,
                )
            )
        await db.flush()
        db.add(Workspace(id=ids["workspace"], name="authority", owner_id=ids["owner"]))
        await db.flush()
        db.add(
            Collection(id=ids["project"], workspace_id=ids["workspace"], name="plan")
        )
        db.add(
            WorkspaceMember(
                workspace_id=ids["workspace"],
                user_id=ids["supervisor"],
                role=WorkspaceRole.VIEWER,
            )
        )
        await db.flush()
        db.add(
            ResearchProjectRoleAssignment(
                collection_id=ids["project"],
                user_id=ids["supervisor"],
                role=ResearchProjectRole.SUPERVISOR,
                assigned_by_id=ids["owner"],
            )
        )
        await db.commit()
    return ids


async def _wait_until_blocked(observer: AsyncSession, pid: int) -> None:
    """Observe an actual PostgreSQL lock wait, never infer overlap from a sleep."""
    async with asyncio.timeout(5):
        while not (
            await observer.execute(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
            )
        ).scalar_one():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["membership", "supervisor"])
async def test_revocation_committed_during_lock_wait_denies_scientific_action(
    mapping_session_factory: async_sessionmaker[AsyncSession], revoke: str
) -> None:
    """Removing the post-lock member/role reload makes this authorization fail open."""
    factory = mapping_session_factory
    ids = await _seed(factory)
    async with factory() as revoker, factory() as approver, factory() as observer:
        if revoke == "membership":
            await workspace_service.remove_member(
                revoker, ids["workspace"], ids["supervisor"], ids["owner"]
            )
        else:
            await resolve_project(
                revoker, ids["project"], ids["owner"], ResearchAction.MANAGE
            )
            role = (
                await revoker.execute(
                    select(ResearchProjectRoleAssignment).where(
                        ResearchProjectRoleAssignment.collection_id == ids["project"]
                    )
                )
            ).scalar_one()
            role.soft_delete()
            await revoker.flush()
        pid = cast(
            int, (await approver.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        attempt = asyncio.create_task(
            resolve_project(
                approver, ids["project"], ids["supervisor"], ResearchAction.SUPERVISE
            )
        )
        try:
            await _wait_until_blocked(observer, pid)
            await revoker.commit()
            with pytest.raises(HTTPException) as denied:
                await attempt
            assert denied.value.status_code == (404 if revoke == "membership" else 403)
        finally:
            if not attempt.done():
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
            await approver.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["membership", "supervisor"])
async def test_authorized_transaction_finishes_before_waiting_revocation(
    mapping_session_factory: async_sessionmaker[AsyncSession], revoke: str
) -> None:
    """Locks must survive the access check until the domain transaction commits."""
    factory = mapping_session_factory
    ids = await _seed(factory)
    async with factory() as approver, factory() as revoker, factory() as observer:
        context = await resolve_project(
            approver, ids["project"], ids["supervisor"], ResearchAction.SUPERVISE
        )
        assert ResearchProjectRole.SUPERVISOR in context.effective_roles
        pid = cast(
            int, (await revoker.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )

        async def revoke_authority() -> None:
            if revoke == "membership":
                await workspace_service.remove_member(
                    revoker, ids["workspace"], ids["supervisor"], ids["owner"]
                )
            else:
                await resolve_project(
                    revoker, ids["project"], ids["owner"], ResearchAction.MANAGE
                )
                role = (
                    await revoker.execute(
                        select(ResearchProjectRoleAssignment).where(
                            ResearchProjectRoleAssignment.collection_id
                            == ids["project"]
                        )
                    )
                ).scalar_one()
                role.soft_delete()
            await revoker.commit()

        attempt = asyncio.create_task(revoke_authority())
        try:
            await _wait_until_blocked(observer, pid)
            assert not attempt.done()
            await approver.commit()
            await attempt
        finally:
            if not attempt.done():
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
        async with factory() as after:
            with pytest.raises(HTTPException):
                await resolve_project(
                    after, ids["project"], ids["supervisor"], ResearchAction.SUPERVISE
                )

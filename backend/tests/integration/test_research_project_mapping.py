"""PostgreSQL proof for mapped research-project authorization."""

import asyncio
import os
from types import SimpleNamespace
from typing import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.research_engine.projects import link_project_collection
from src.models import Base
from src.schemas.research_engine import ProjectLink
from src.services.research_engine.project_access import (
    ResearchAction,
    require_blueprint,
    require_research_project,
    require_run,
    require_step,
)


@pytest.fixture
async def mapping_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    configured_url = os.getenv("RESEARCH_PROJECT_DATABASE_URL")
    if not configured_url:
        pytest.skip("RESEARCH_PROJECT_DATABASE_URL is required")
    assert configured_url is not None
    async_url = configured_url.replace("postgresql://", "postgresql+asyncpg://")
    schema = f"test_research_project_mapping_{uuid4().hex}"
    admin_engine = create_async_engine(async_url)
    async with admin_engine.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    scoped_engine = create_async_engine(
        async_url, connect_args={"server_settings": {"search_path": schema}}
    )
    async with scoped_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(scoped_engine, expire_on_commit=False)
    finally:
        await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_mapping_roles_org_scope_and_deleted_ancestors(
    mapping_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_factory = mapping_session_factory
    ids = {key: uuid4() for key in "oanuvwcprbsxyz"}
    suffix = uuid4().hex

    async with session_factory() as db:
        await db.execute(
            text("""INSERT INTO organizations
                (id,name,storage_tier,storage_used_bytes,storage_limit_bytes,is_active,created_at,updated_at,is_deleted)
                VALUES (:o,:name,'FREE',0,1000,true,now(),now(),false)"""),
            {"o": ids["o"], "name": f"mapping-{suffix}"},
        )
        for key, org in (
            ("a", ids["o"]),
            ("n", ids["o"]),
            ("u", ids["o"]),
            ("v", None),
        ):
            await db.execute(
                text(
                    """INSERT INTO users
                    (id,email,password_hash,first_name,last_name,role,is_active,login_count,organization_id,created_at,updated_at,is_deleted)
                    VALUES (:id,:email,'x','x','x','USER',true,0,:org,now(),now(),false)"""
                ),
                {"id": ids[key], "email": f"{key}-{suffix}@test.invalid", "org": org},
            )
        await db.execute(
            text("""INSERT INTO workspaces
                (id,name,is_archived,is_public,owner_id,organization_id,created_at,updated_at,is_deleted)
                VALUES (:w,'mapped',false,true,:a,:o,now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text(
                """INSERT INTO collections
                (id,workspace_id,name,project_type,research_status,tags,is_private,created_at,updated_at,is_deleted)
                VALUES (:c,:w,'canonical','research','active','[]',true,now(),now(),false)"""
            ),
            ids,
        )
        await db.execute(
            text("""INSERT INTO workspace_members
                (id,workspace_id,user_id,role,joined_at,created_at,updated_at,is_deleted)
                VALUES (gen_random_uuid(),:w,:u,'editor',now(),now(),now(),false),
                       (gen_random_uuid(),:w,:v,'admin',now(),now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_projects
            (id,name,owner_id,status,collection_id,created_at,updated_at,is_deleted)
            VALUES (:p,'engine',:a,'active',:c,now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_blueprints
            (id,project_id,name,version,is_immutable,created_at,updated_at,is_deleted)
            VALUES (:b,:p,'bp',1,false,now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_runs
            (id,blueprint_id,blueprint_version,status,total_tokens,created_at,updated_at,is_deleted)
            VALUES (:r,:b,1,'pending',0,now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_steps
            (id,run_id,step_index,step_type,mode,temperature,token_count,created_at,updated_at,is_deleted)
            VALUES (:s,:r,0,'search','deterministic',0,0,now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text(
                """INSERT INTO collections
                (id,workspace_id,name,project_type,research_status,tags,is_private,created_at,updated_at,is_deleted)
                VALUES (:x,:w,'link-a','research','active','[]',true,now(),now(),false),
                       (:y,:w,'link-b','research','active','[]',true,now(),now(),false)"""
            ),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_projects
                (id,name,owner_id,status,created_at,updated_at,is_deleted)
                VALUES (:z,'legacy',:a,'active',now(),now(),false)"""),
            ids,
        )
        await db.commit()

        async def link(collection_key: str) -> int:
            async with session_factory() as link_db:
                try:
                    await link_project_collection(
                        ids["z"],
                        ProjectLink(collection_id=ids[collection_key]),
                        SimpleNamespace(id=ids["a"]),  # type: ignore[arg-type]
                        link_db,
                    )
                    return 200
                except HTTPException as exc:
                    return int(exc.status_code)

        # Regression guard: removing ``.with_for_update()`` from
        # ``link_project_collection`` made this exact PostgreSQL test fail with
        # ``[200, 200] != [200, 409]``; restoring the lock makes one link win.
        # Run with: RESEARCH_PROJECT_DATABASE_URL=<postgres-url> PYTHONPATH=backend
        # python -m pytest -q backend/tests/integration/test_research_project_mapping.py
        assert sorted(await asyncio.gather(link("x"), link("y"))) == [200, 409]

        assert (
            await require_research_project(
                db, ids["c"], ids["u"], ResearchAction.REVIEW
            )
        ).id == ids["p"]
        assert (
            await require_blueprint(db, ids["b"], ids["u"], ResearchAction.REVIEW)
        ).id == ids["b"]
        assert (
            await require_run(db, ids["r"], ids["u"], ResearchAction.REVIEW)
        ).id == ids["r"]
        assert (
            await require_step(db, ids["s"], ids["u"], ResearchAction.REVIEW)
        ).id == ids["s"]
        with pytest.raises(HTTPException):
            await require_run(db, ids["r"], ids["u"], ResearchAction.SUPERVISE)
        with pytest.raises(HTTPException):
            await require_run(db, ids["r"], ids["v"], ResearchAction.REVIEW)
        await db.execute(text("UPDATE workspaces SET owner_id=:n WHERE id=:w"), ids)
        await db.commit()
        with pytest.raises(HTTPException):
            await require_run(db, ids["r"], ids["a"], ResearchAction.REVIEW)
        assert (
            await require_run(db, ids["r"], ids["n"], ResearchAction.SUPERVISE)
        ).id == ids["r"]
        await db.execute(text("UPDATE workspaces SET is_deleted=true WHERE id=:w"), ids)
        await db.commit()
        with pytest.raises(HTTPException):
            await require_step(db, ids["s"], ids["n"], ResearchAction.REVIEW)

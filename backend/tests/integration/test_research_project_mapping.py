"""PostgreSQL proof for mapped research-project authorization."""

import asyncio
import os
from types import SimpleNamespace
from typing import AsyncIterator, cast
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.research.project_chat import (
    link_thread_to_project,
    list_project_threads,
    start_chat_from_project,
    unlink_thread_from_project,
)
from src.api.research_engine.projects import (
    create_project,
    get_legacy_project,
    get_project,
    link_project_collection,
)
from src.models import Base, Conversation, ProjectThread, Thread, Workspace
from src.schemas.research_engine import ProjectCreate, ProjectLink, ProjectResponse
from src.services.research_engine.project_access import (
    ResearchAction,
    project_documents_query,
    require_blueprint,
    require_legacy_project,
    require_research_project,
    require_run,
    require_step,
    resolve_project,
)
from src.shared.research_schemas import LinkThreadRequest, StartChatFromProjectRequest


@pytest.fixture
async def mapping_session_factory(
    request: pytest.FixtureRequest,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    configured_url = os.getenv("RESEARCH_PROJECT_DATABASE_URL") or str(
        request.getfixturevalue("postgres_container")["url"]
    )
    async_url = make_url(configured_url).set(drivername="postgresql+asyncpg")
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
    ids = {key: uuid4() for key in "oanuvkwcprbsxyzdefghij"}
    suffix = uuid4().hex

    async with session_factory() as db:
        await db.execute(
            text("""INSERT INTO organizations
                (id,name,storage_tier,storage_used_bytes,storage_limit_bytes,is_active,created_at,updated_at,is_deleted)
                VALUES (:o,:name,'FREE',0,1000,true,now(),now(),false)"""),
            {"o": ids["o"], "name": f"mapping-{suffix}"},
        )
        await db.execute(
            text("""INSERT INTO organizations
                (id,name,storage_tier,storage_used_bytes,storage_limit_bytes,
                 is_active,created_at,updated_at,is_deleted)
                VALUES (:j,:name,'FREE',0,1000,true,now(),now(),false)"""),
            {"j": ids["j"], "name": f"foreign-{suffix}"},
        )
        for key, org in (
            ("a", ids["o"]),
            ("n", ids["o"]),
            ("u", ids["o"]),
            ("v", None),
            ("k", ids["o"]),
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
                       (gen_random_uuid(),:w,:v,'admin',now(),now(),now(),false),
                       (gen_random_uuid(),:w,:n,'admin',now(),now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_projects
            (id,name,owner_id,status,collection_id,created_at,updated_at,is_deleted)
            VALUES (:p,'engine',:a,'active',:c,now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_project_role_assignments
                (id,collection_id,user_id,role,assigned_by_id,
                 created_at,updated_at,is_deleted)
                VALUES
                    (gen_random_uuid(),:c,:u,'reviewer',:a,now(),now(),false),
                    (gen_random_uuid(),:c,:n,'supervisor',:a,now(),now(),false)
                """),
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

        canonical = cast(
            ProjectResponse,
            await get_project(
                ids["c"], SimpleNamespace(id=ids["u"]), db  # type: ignore[arg-type]
            ),
        )
        assert canonical.id == ids["c"]
        assert canonical.project_id == ids["c"]
        assert canonical.collection_id == ids["c"]
        assert canonical.research_engine_project_id == ids["p"]

        for doc_id, org_id, deleted in (
            (ids["d"], ids["o"], False),
            (ids["e"], ids["j"], False),
            (ids["f"], ids["o"], True),
        ):
            await db.execute(
                text("""INSERT INTO documents
                    (id,title,filename,file_path,file_size_bytes,mime_type,
                     document_type,processing_status,processing_retry_count,
                     is_embedded,is_indexed,is_public,organization_id,
                     created_at,updated_at,is_deleted)
                    VALUES
                    (:id,'doc','doc.pdf','doc.pdf',1,'application/pdf','PDF',
                     'COMPLETED',0,false,false,false,:org,now(),now(),:deleted)
                    """),
                {"id": doc_id, "org": org_id, "deleted": deleted},
            )
        for link_id, doc_id, deleted in (
            (ids["g"], ids["d"], False),
            (ids["h"], ids["e"], False),
            (ids["i"], ids["f"], False),
        ):
            await db.execute(
                text("""INSERT INTO collection_documents
                    (id,collection_id,document_id,sort_order,
                     created_at,updated_at,is_deleted)
                    VALUES (:id,:c,:doc,0,now(),now(),:deleted)"""),
                {"id": link_id, "c": ids["c"], "doc": doc_id, "deleted": deleted},
            )
        await db.commit()
        source_ids = set(
            (await db.execute(project_documents_query(ids["c"]))).scalars().all()
        )
        assert {document.id for document in source_ids} == {ids["d"]}

        editor = SimpleNamespace(id=ids["u"])
        started = await start_chat_from_project(
            ids["c"],
            StartChatFromProjectRequest(
                initial_message="Review the evidence.",
                thread_title="Editor research chat",
            ),
            editor,  # type: ignore[arg-type]
            db,
        )
        manual_conversation = Conversation(
            workspace_id=ids["w"], title="Manual", created_by_id=ids["u"]
        )
        db.add(manual_conversation)
        await db.flush()
        manual_thread = Thread(
            conversation_id=manual_conversation.id,
            title="Manual thread",
            created_by_id=ids["u"],
        )
        db.add(manual_thread)
        await db.commit()
        linked = await link_thread_to_project(
            ids["c"],
            LinkThreadRequest(thread_id=manual_thread.id),
            editor,  # type: ignore[arg-type]
            db,
        )
        assert linked.thread_id == manual_thread.id

        excluded_workspace = Workspace(
            name="Excluded", owner_id=ids["a"], organization_id=ids["o"]
        )
        deleted_workspace = Workspace(
            name="Deleted", owner_id=ids["a"], organization_id=ids["o"]
        )
        deleted_workspace.soft_delete()
        db.add_all([excluded_workspace, deleted_workspace])
        await db.flush()
        excluded_conversation = Conversation(
            workspace_id=excluded_workspace.id,
            title="Cross workspace",
            created_by_id=ids["a"],
        )
        deleted_conversation = Conversation(
            workspace_id=ids["w"], title="Deleted conversation", created_by_id=ids["a"]
        )
        deleted_conversation.soft_delete()
        dead_workspace_conversation = Conversation(
            workspace_id=deleted_workspace.id,
            title="Deleted workspace",
            created_by_id=ids["a"],
        )
        db.add_all(
            [
                excluded_conversation,
                deleted_conversation,
                dead_workspace_conversation,
            ]
        )
        await db.flush()
        excluded_threads = [
            Thread(conversation_id=conversation.id, created_by_id=ids["a"])
            for conversation in (
                excluded_conversation,
                deleted_conversation,
                dead_workspace_conversation,
            )
        ]
        db.add_all(excluded_threads)
        await db.flush()
        db.add_all(
            [
                ProjectThread(
                    project_id=ids["c"],
                    thread_id=thread.id,
                    linked_by_id=ids["a"],
                )
                for thread in excluded_threads
            ]
        )
        await db.commit()
        listed = await list_project_threads(
            ids["c"], editor, db  # type: ignore[arg-type]
        )
        listed_ids = {item.thread_id for item in listed.threads}
        assert listed_ids == {started.thread_id, manual_thread.id}
        await unlink_thread_from_project(
            ids["c"], manual_thread.id, editor, db  # type: ignore[arg-type]
        )
        relisted = await list_project_threads(
            ids["c"], editor, db  # type: ignore[arg-type]
        )
        assert {item.thread_id for item in relisted.threads} == {started.thread_id}
        matrix_id, draft_id = uuid4(), uuid4()
        await db.execute(
            text("""INSERT INTO extraction_matrices
                (id,project_id,name,columns,created_at,updated_at,is_deleted)
                VALUES (:id,:c,'matrix','[]',now(),now(),false)"""),
            {"id": matrix_id, "c": ids["c"]},
        )
        await db.execute(
            text("""INSERT INTO generated_drafts
                (id,project_id,version,title,content,themes,is_current,
                 created_at,updated_at,is_deleted)
                VALUES (:id,:c,1,'draft','Grounded [Doc 1].','[]',true,
                        now(),now(),false)"""),
            {"id": draft_id, "c": ids["c"]},
        )
        await db.commit()
        journey_collection = (
            await db.execute(
                text("""
                    SELECT c.id
                    FROM collections AS c
                    JOIN collection_documents AS cd ON cd.collection_id = c.id
                    JOIN research_projects AS rp ON rp.collection_id = c.id
                    JOIN research_blueprints AS rb ON rb.project_id = rp.id
                    JOIN research_runs AS rr ON rr.blueprint_id = rb.id
                    JOIN extraction_matrices AS em ON em.project_id = c.id
                    JOIN generated_drafts AS gd ON gd.project_id = c.id
                    WHERE cd.document_id=:document AND rr.id=:run
                      AND em.id=:matrix AND gd.id=:draft
                    """),
                {
                    "document": ids["d"],
                    "run": ids["r"],
                    "matrix": matrix_id,
                    "draft": draft_id,
                },
            )
        ).scalar_one()
        assert journey_collection == ids["c"]

        personal_workspace, personal_collection, personal_engine = (
            uuid4(),
            uuid4(),
            uuid4(),
        )
        await db.execute(
            text("""INSERT INTO workspaces
                (id,name,is_archived,is_public,owner_id,organization_id,
                 created_at,updated_at,is_deleted)
                VALUES (:w,'personal',false,false,:a,NULL,now(),now(),false)"""),
            {"w": personal_workspace, "a": ids["a"]},
        )
        await db.execute(
            text("""INSERT INTO workspace_members
                (id,workspace_id,user_id,role,joined_at,
                 created_at,updated_at,is_deleted)
                VALUES (gen_random_uuid(),:w,:u,'viewer',now(),now(),now(),false)"""),
            {"w": personal_workspace, "u": ids["u"]},
        )
        await db.execute(
            text("""INSERT INTO collections
                (id,workspace_id,name,project_type,research_status,tags,is_private,
                 created_at,updated_at,is_deleted)
                VALUES (:c,:w,'personal','research','active','[]',true,
                        now(),now(),false)"""),
            {"c": personal_collection, "w": personal_workspace},
        )
        await db.execute(
            text("""INSERT INTO research_projects
                (id,name,owner_id,status,collection_id,
                 created_at,updated_at,is_deleted)
                VALUES (:p,'personal',:a,'active',:c,now(),now(),false)"""),
            {"p": personal_engine, "a": ids["a"], "c": personal_collection},
        )
        await db.execute(
            text("""INSERT INTO research_project_role_assignments
                (id,collection_id,user_id,role,assigned_by_id,
                 created_at,updated_at,is_deleted)
                VALUES (gen_random_uuid(),:c,:u,'reviewer',:a,now(),now(),false)"""),
            {"c": personal_collection, "u": ids["u"], "a": ids["a"]},
        )
        await db.commit()
        async with session_factory() as reopened:
            assert (
                await require_research_project(
                    reopened,
                    personal_collection,
                    ids["u"],
                    ResearchAction.REVIEW,
                )
            ).id == personal_engine

        ensure_collection = uuid4()
        await db.execute(
            text("""INSERT INTO collections
                (id,workspace_id,name,project_type,research_status,tags,is_private,
                 created_at,updated_at,is_deleted)
                VALUES (:id,:w,'ensure','research','active','[]',true,
                        now(),now(),false)"""),
            {"id": ensure_collection, "w": ids["w"]},
        )
        await db.commit()

        async def ensure() -> tuple[object, object]:
            async with session_factory() as ensure_db:
                response = await create_project(
                    ProjectCreate(collection_id=ensure_collection, name="ignored"),
                    SimpleNamespace(id=ids["a"]),  # type: ignore[arg-type]
                    ensure_db,
                )
                return response.id, response.research_engine_project_id

        ensured = await asyncio.gather(ensure(), ensure())
        assert ensured[0] == ensured[1]

        foreign_legacy, foreign_target = uuid4(), uuid4()
        await db.execute(
            text("""INSERT INTO collections
                (id,workspace_id,name,project_type,research_status,tags,is_private,
                 created_at,updated_at,is_deleted)
                VALUES (:id,:w,'foreign-target','research','active','[]',true,
                        now(),now(),false)"""),
            {"id": foreign_target, "w": ids["w"]},
        )
        await db.execute(
            text("""INSERT INTO research_projects
                (id,name,owner_id,status,created_at,updated_at,is_deleted)
                VALUES (:id,'foreign-legacy',:a,'active',now(),now(),false)"""),
            {"id": foreign_legacy, "a": ids["a"]},
        )
        await db.commit()
        with pytest.raises(HTTPException) as owner_boundary:
            await link_project_collection(
                foreign_legacy,
                ProjectLink(collection_id=foreign_target),
                SimpleNamespace(id=ids["n"]),  # type: ignore[arg-type]
                db,
            )
        assert owner_boundary.value.status_code == 404

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
        with pytest.raises(HTTPException):
            await require_research_project(db, ids["c"], ids["k"], ResearchAction.VIEW)
        await db.execute(
            text("UPDATE workspaces SET is_archived=true WHERE id=:w"), ids
        )
        await db.commit()
        assert (
            await require_research_project(db, ids["c"], ids["u"], ResearchAction.VIEW)
        ).id == ids["p"]
        with pytest.raises(HTTPException) as archived_error:
            await resolve_project(db, ids["c"], ids["u"], ResearchAction.REVIEW)
        assert archived_error.value.status_code == 409
        await db.execute(
            text("UPDATE workspaces SET is_archived=false WHERE id=:w"), ids
        )
        await db.commit()

        for table, entity_id, check in (
            ("research_steps", ids["s"], require_step),
            ("research_runs", ids["r"], require_run),
            ("research_blueprints", ids["b"], require_blueprint),
        ):
            await db.execute(
                text(f"UPDATE {table} SET is_deleted=true WHERE id=:id"),
                {"id": entity_id},
            )
            await db.commit()
            with pytest.raises(HTTPException):
                await check(db, entity_id, ids["u"], ResearchAction.REVIEW)
            await db.execute(
                text(f"UPDATE {table} SET is_deleted=false WHERE id=:id"),
                {"id": entity_id},
            )
            await db.commit()

        await db.execute(
            text("UPDATE collections SET is_deleted=true WHERE id=:c"), ids
        )
        await db.commit()
        with pytest.raises(HTTPException):
            await require_research_project(db, ids["c"], ids["u"], ResearchAction.VIEW)
        await db.execute(
            text("UPDATE collections SET is_deleted=false WHERE id=:c"), ids
        )
        await db.commit()
        await db.execute(text("UPDATE workspaces SET owner_id=:n WHERE id=:w"), ids)
        await db.commit()
        db.expire_all()
        assert (await require_legacy_project(db, ids["p"], ids["n"])).id == ids["p"]
        assert (await require_legacy_project(db, ids["p"], ids["u"])).id == ids["p"]
        with pytest.raises(HTTPException) as revoked_engine_owner:
            await require_legacy_project(db, ids["p"], ids["a"])
        assert revoked_engine_owner.value.status_code == 404
        assert (
            await require_legacy_project(db, foreign_legacy, ids["a"])
        ).id == foreign_legacy
        with pytest.raises(HTTPException) as unresolved_non_owner:
            await require_legacy_project(db, foreign_legacy, ids["n"])
        assert unresolved_non_owner.value.status_code == 404
        await db.execute(
            text("UPDATE collections SET research_status='archived' WHERE id=:c"), ids
        )
        await db.commit()
        archived_legacy = await get_legacy_project(
            ids["p"],
            SimpleNamespace(id=ids["n"]),  # type: ignore[arg-type]
            db,
        )
        assert archived_legacy.status == "archived"
        await db.execute(
            text("UPDATE collections SET research_status='active' WHERE id=:c"), ids
        )
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

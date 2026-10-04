"""PostgreSQL HTTP-consumer proofs for protocol approval atomicity and races."""

import asyncio
import importlib.util
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import AsyncIterator, Callable, cast
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from sqlalchemy import Connection, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from src.api.research_engine.protocols import router
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models import Base
from src.models.collection import Collection
from src.models.research_protocol import ResearchProtocolVersion
from src.models.workspace import Workspace
from src.schemas.research_engine import ResearchProtocolVersionCreate
from src.services.research_decisions import replay_decisions
from src.services.research_engine.protocol_service import (
    add_protocol_version,
    canonical_hash,
    protocol_content,
)

VERSIONS = Path(__file__).parents[2] / "alembic" / "versions"
PROTOCOL_SNAPSHOT = {
    "eligibility": {"population": "adults"},
    "sources_search": {"databases": ["example"]},
    "selection": {"reviewers": 2},
    "extraction": {"fields": ["outcome"]},
    "appraisal_synthesis": {"method": "narrative"},
    "outcomes": {"primary": "effect", "threshold": 1.0},
    "reviewer_mode": {"mode": "independent", "offset": -0.0},
}


def _migration(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(filename, VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade(connection: Connection, module: ModuleType) -> None:
    setattr(module, "op", Operations(MigrationContext.configure(connection)))
    cast(Callable[[], None], module.upgrade)()


@pytest.fixture
async def protocol_engine(
    request: pytest.FixtureRequest,
) -> AsyncIterator[AsyncEngine]:
    configured = os.getenv("RESEARCH_DECISION_DATABASE_URL")
    url = make_url(
        configured or str(request.getfixturevalue("postgres_container")["url"])
    ).set(drivername="postgresql+asyncpg")
    schema = f"test_protocol_atomicity_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql("DROP TABLE protocol_deviations CASCADE")
        await connection.exec_driver_sql(
            "DROP TABLE protocol_registration_operations CASCADE"
        )
        await connection.exec_driver_sql(
            "DROP TABLE research_protocol_versions CASCADE"
        )
        await connection.exec_driver_sql("DROP TABLE research_protocols CASCADE")
        await connection.exec_driver_sql(
            "DROP TABLE research_question_versions CASCADE"
        )
        await connection.exec_driver_sql("DROP TABLE research_questions CASCADE")
        for column in (
            "protocol_version_id",
            "effective_plan_hash",
            "conformance_status",
        ):
            await connection.exec_driver_sql(
                f"ALTER TABLE research_runs DROP COLUMN {column} CASCADE"
            )
        # GOO-302: screening_resolutions.event_id references the ledger.
        await connection.exec_driver_sql("DROP TABLE screening_resolutions")
        await connection.exec_driver_sql("DROP TABLE research_decision_events")
        await connection.exec_driver_sql("DROP TABLE research_decision_streams")
        await connection.run_sync(
            lambda sync: _upgrade(
                sync,
                _migration("a3c5e7f901b2_create_research_decision_ledger.py"),
            )
        )
        await connection.run_sync(
            lambda sync: _upgrade(
                sync, _migration("b4d6f8021a3c_add_research_protocols.py")
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


async def _seed(engine: AsyncEngine) -> dict[str, UUID]:
    ids = {
        name: uuid4()
        for name in (
            "org",
            "author",
            "supervisor",
            "workspace",
            "collection",
            "engine_project",
            "blueprint",
            "legacy_run",
            "question",
            "question_version",
            "protocol",
            "protocol_version",
        )
    }
    async with engine.begin() as db:
        execution_plan = {
            "blueprint_id": str(ids["blueprint"]),
            "blueprint_version": 1,
            "steps": [],
            "parameters": {},
        }
        protocol_hash = canonical_hash(
            protocol_content(
                ids["question_version"],
                ids["blueprint"],
                PROTOCOL_SNAPSHOT,
                execution_plan,
            )
        )
        question_hash = canonical_hash(
            {
                "question": "Does it work?",
                "hypothesis": None,
                "scope": None,
                "framework": {},
                "canonicalization_version": "research-protocol-v1",
            }
        )
        await db.execute(
            text("""INSERT INTO organizations
                (id,name,storage_tier,storage_used_bytes,storage_limit_bytes,
                 is_active,created_at,updated_at,is_deleted)
                VALUES (:org,'protocol','FREE',0,1,true,now(),now(),false)"""),
            ids,
        )
        for user in ("author", "supervisor"):
            await db.execute(
                text("""INSERT INTO users
                    (id,email,password_hash,first_name,last_name,role,is_active,
                     login_count,organization_id,created_at,updated_at,is_deleted)
                    VALUES (:id,:email,'x','x','x','USER',true,0,:org,
                            now(),now(),false)"""),
                {
                    "id": ids[user],
                    "email": f"{user}-{uuid4()}@test.invalid",
                    "org": ids["org"],
                },
            )
        await db.execute(
            text("""INSERT INTO workspaces
                (id,name,is_archived,is_public,owner_id,organization_id,
                 created_at,updated_at,is_deleted)
                VALUES (:workspace,'protocol',false,false,:author,:org,
                        now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO workspace_members
                (id,workspace_id,user_id,role,joined_at,created_at,updated_at,is_deleted)
                VALUES (gen_random_uuid(),:workspace,:supervisor,'editor',
                        now(),now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO collections
                (id,workspace_id,name,project_type,research_status,tags,is_private,
                 created_at,updated_at,is_deleted)
                VALUES (:collection,:workspace,'protocol','research','active','[]',
                        true,now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_projects
                (id,name,owner_id,status,collection_id,
                 created_at,updated_at,is_deleted)
                VALUES (:engine_project,'protocol',:author,'active',:collection,
                        now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_project_role_assignments
                (id,collection_id,user_id,role,assigned_by_id,
                 created_at,updated_at,is_deleted)
                VALUES (gen_random_uuid(),:collection,:supervisor,'supervisor',:author,
                        now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_blueprints
                (id,project_id,name,version,is_immutable,
                 created_at,updated_at,is_deleted)
                VALUES (:blueprint,:engine_project,'protocol',1,false,
                        now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_runs
                (id,blueprint_id,blueprint_version,status,total_tokens,
                 created_at,updated_at,is_deleted)
                VALUES (:legacy_run,:blueprint,1,'pending',0,now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_questions
                (id,collection_id,current_version_id,
                 created_at,updated_at,is_deleted)
                VALUES (:question,:collection,NULL,now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_question_versions
                (id,question_id,version,question,framework,content_hash,
                 author_user_id,created_at)
                VALUES (:question_version,:question,1,'Does it work?','{}',:hash,
                        :author,now())"""),
            {**ids, "hash": question_hash},
        )
        await db.execute(
            text("""UPDATE research_questions SET current_version_id=:question_version
                WHERE id=:question"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_protocols
                (id,collection_id,name,current_draft_version_id,
                 current_approved_version_id,created_at,updated_at,is_deleted)
                VALUES (:protocol,:collection,'Protocol',NULL,NULL,
                        now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_protocol_versions
                (id,protocol_id,version,question_version_id,blueprint_id,
                 execution_plan,snapshot,content_hash,status,change_kind,
                 author_user_id,created_at)
                VALUES (:protocol_version,:protocol,1,:question_version,:blueprint,
                        :execution_plan,:snapshot,:hash,'draft','initial',
                        :author,now())"""),
            {
                **ids,
                "hash": protocol_hash,
                "execution_plan": json.dumps(execution_plan),
                "snapshot": json.dumps(PROTOCOL_SNAPSHOT),
            },
        )
        await db.execute(
            text("""UPDATE research_protocols
                SET current_draft_version_id=:protocol_version WHERE id=:protocol"""),
            ids,
        )
    return ids


@asynccontextmanager
async def _client(
    engine: AsyncEngine, user_id: UUID
) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def db_override() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=user_id)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _approval(ids: dict[str, UUID], key: str) -> dict[str, object]:
    execution_plan = {
        "blueprint_id": str(ids["blueprint"]),
        "blueprint_version": 1,
        "steps": [],
        "parameters": {},
    }
    return {
        "expected_protocol_version": 1,
        "expected_content_hash": canonical_hash(
            protocol_content(
                ids["question_version"],
                ids["blueprint"],
                PROTOCOL_SNAPSHOT,
                execution_plan,
            )
        ),
        "expected_current_approved_version_id": None,
        "reason": "supervisor approved exact plan",
        "idempotency_key": key,
    }


def _approve_path(ids: dict[str, UUID]) -> str:
    return (
        f"/api/v1/research-engine/protocols/{ids['protocol']}/versions/"
        f"{ids['protocol_version']}/approve"
    )


async def _wait_for_blocked_session(engine: AsyncEngine) -> None:
    for _ in range(100):
        async with engine.connect() as connection:
            blocked = await connection.scalar(
                text("""SELECT count(*) FROM pg_stat_activity
                    WHERE datname=current_database()
                      AND wait_event_type='Lock'
                      AND pid <> pg_backend_pid()""")
            )
        if blocked:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("approval request did not block on the workspace lock")


@pytest.mark.asyncio
async def test_http_approval_rolls_back_pointer_and_event_together(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    async with protocol_engine.begin() as db:
        await db.execute(text("""
            CREATE FUNCTION fail_protocol_approval_event() RETURNS trigger AS $$
            BEGIN
              IF NEW.event_type = 'protocol.approved' THEN
                RAISE EXCEPTION 'forced approval event failure';
              END IF;
              RETURN NEW;
            END; $$ LANGUAGE plpgsql
            """))
        await db.execute(text("""
            CREATE TRIGGER fail_protocol_approval_event
              BEFORE INSERT ON research_decision_events
              FOR EACH ROW EXECUTE FUNCTION fail_protocol_approval_event()
            """))
    async with _client(protocol_engine, ids["supervisor"]) as client:
        response = await client.post(
            _approve_path(ids), json=_approval(ids, "rollback-proof")
        )
    assert response.status_code == 500
    async with AsyncSession(protocol_engine) as db:
        state = (
            await db.execute(
                text("""SELECT p.current_approved_version_id, v.status,
                    (SELECT count(*) FROM research_decision_events)
                    FROM research_protocols p
                    JOIN research_protocol_versions v
                      ON v.id=:version WHERE p.id=:protocol"""),
                {"version": ids["protocol_version"], "protocol": ids["protocol"]},
            )
        ).one()
        assert tuple(state) == (None, "draft", 0)


@pytest.mark.asyncio
async def test_http_approval_survives_fresh_session_replay(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    async with _client(protocol_engine, ids["supervisor"]) as client:
        response = await client.post(
            _approve_path(ids), json=_approval(ids, "fresh-replay")
        )
    assert response.status_code == 200, response.text
    async with AsyncSession(protocol_engine) as db:
        version = await db.get(ResearchProtocolVersion, ids["protocol_version"])
        assert version is not None and version.status == "approved"

        async def subject_hash(version_id: UUID) -> str | None:
            return cast(
                str | None,
                await db.scalar(
                    select(ResearchProtocolVersion.content_hash).where(
                        ResearchProtocolVersion.id == version_id
                    )
                ),
            )

        events = await replay_decisions(
            db,
            collection_id=ids["collection"],
            aggregate_type="research_protocol",
            aggregate_id=ids["protocol"],
            subject_version_hash=subject_hash,
        )
        assert [(event.seq, event.event_type) for event in events] == [
            (1, "protocol.approved")
        ]
        replayed_pointer = cast(UUID, events[-1].subject_version_id)
        persisted_pointer = await db.scalar(
            text("""SELECT current_approved_version_id FROM research_protocols
                WHERE id=:protocol"""),
            ids,
        )
        assert replayed_pointer == persisted_pointer == version.id
        assert events[-1].subject_hash == version.content_hash
        assert events[-1].payload["previous_approved_version_id"] is None
        legacy = (
            await db.execute(
                text("""SELECT protocol_version_id,effective_plan_hash,
                    conformance_status FROM research_runs WHERE id=:legacy_run"""),
                ids,
            )
        ).one()
        assert tuple(legacy) == (None, None, "legacy_unbound")


@pytest.mark.asyncio
async def test_http_approval_rechecks_role_after_concurrent_revocation(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    async with AsyncSession(protocol_engine) as revoker:
        await revoker.execute(
            select(Workspace).where(Workspace.id == ids["workspace"]).with_for_update()
        )
        async with _client(protocol_engine, ids["supervisor"]) as client:
            pending = asyncio.create_task(
                client.post(_approve_path(ids), json=_approval(ids, "revoked-approval"))
            )
            await _wait_for_blocked_session(protocol_engine)
            await revoker.execute(
                text("""UPDATE research_project_role_assignments
                    SET is_deleted=true,deleted_at=now()
                    WHERE collection_id=:collection AND user_id=:supervisor"""),
                ids,
            )
            await revoker.commit()
            response = await pending
    assert response.status_code == 403
    async with AsyncSession(protocol_engine) as db:
        pointer = await db.scalar(
            text(
                "SELECT current_approved_version_id FROM research_protocols WHERE id=:protocol"
            ),
            ids,
        )
        event_count = await db.scalar(
            text("SELECT count(*) FROM research_decision_events")
        )
        assert pointer is None and event_count == 0


@pytest.mark.asyncio
async def test_http_approval_and_amendment_serialize_without_stale_approval(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    amendment = {
        "question_version_id": str(ids["question_version"]),
        "blueprint_id": str(ids["blueprint"]),
        "snapshot": {**PROTOCOL_SNAPSHOT, "eligibility": {"changed": True}},
        "parent_version_id": str(ids["protocol_version"]),
        "amendment_reason": "method changed before approval",
    }
    async with _client(protocol_engine, ids["supervisor"]) as client:
        approve_response, amend_response = await asyncio.gather(
            client.post(_approve_path(ids), json=_approval(ids, "amendment-race")),
            client.post(
                f"/api/v1/research-engine/protocols/{ids['protocol']}/versions",
                json=amendment,
            ),
        )
    assert amend_response.status_code == 201, amend_response.text
    assert approve_response.status_code in {200, 409}, approve_response.text
    async with AsyncSession(protocol_engine) as db:
        state = (
            await db.execute(
                text("""SELECT current_draft_version_id,current_approved_version_id
                    FROM research_protocols WHERE id=:protocol"""),
                ids,
            )
        ).one()
        versions = (
            await db.execute(
                text("""SELECT id,status FROM research_protocol_versions
                    WHERE protocol_id=:protocol ORDER BY version"""),
                ids,
            )
        ).all()
        event_count = await db.scalar(
            text("SELECT count(*) FROM research_decision_events")
        )
        assert len(versions) == 2 and state.current_draft_version_id == versions[1].id
        if approve_response.status_code == 200:
            assert state.current_approved_version_id == ids["protocol_version"]
            assert versions[0].status == "approved" and event_count == 1
        else:
            assert state.current_approved_version_id is None
            assert versions[0].status == "draft" and event_count == 0


@pytest.mark.asyncio
async def test_approval_refreshes_protocol_after_waiting_on_project_lock(
    protocol_engine: AsyncEngine,
) -> None:
    """A preloaded protocol cannot approve a draft replaced during its lock wait."""
    ids = await _seed(protocol_engine)
    async with AsyncSession(protocol_engine) as blocker:
        await blocker.execute(
            select(Collection)
            .where(Collection.id == ids["collection"])
            .with_for_update()
        )
        async with _client(protocol_engine, ids["supervisor"]) as client:
            pending = asyncio.create_task(
                client.post(
                    _approve_path(ids),
                    json=_approval(ids, "identity-refresh-race"),
                )
            )
            await _wait_for_blocked_session(protocol_engine)
            async with AsyncSession(protocol_engine) as writer:
                amended = await add_protocol_version(
                    writer,
                    ids["protocol"],
                    ids["collection"],
                    ids["author"],
                    ResearchProtocolVersionCreate(
                        question_version_id=ids["question_version"],
                        blueprint_id=ids["blueprint"],
                        snapshot={
                            **PROTOCOL_SNAPSHOT,
                            "eligibility": {"changed": "while waiting"},
                        },
                        parent_version_id=ids["protocol_version"],
                        amendment_reason="Replace the draft during approval lock wait",
                    ),
                )
                replacement_id = amended.current_draft_version_id
            await blocker.commit()
            response = await pending
    assert response.status_code == 409, response.text
    async with AsyncSession(protocol_engine) as db:
        pointer = await db.scalar(
            text("""SELECT current_draft_version_id FROM research_protocols
                WHERE id=:protocol"""),
            ids,
        )
        event_count = await db.scalar(
            text("SELECT count(*) FROM research_decision_events")
        )
    assert pointer == replacement_id
    assert event_count == 0


@pytest.mark.asyncio
async def test_protocol_version_execution_plan_is_database_immutable(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    async with AsyncSession(protocol_engine) as db:
        with pytest.raises(DBAPIError, match="content is immutable"):
            await db.execute(
                text("""UPDATE research_protocol_versions
                    SET execution_plan='{"steps":[{"type":"changed"}]}'
                    WHERE id=:protocol_version"""),
                ids,
            )
            await db.commit()
        await db.rollback()
    for statement in (
        "UPDATE research_question_versions SET question='rewritten' WHERE id=:id",
        "DELETE FROM research_question_versions WHERE id=:id",
    ):
        async with AsyncSession(protocol_engine) as db:
            with pytest.raises(DBAPIError, match="versions are immutable"):
                await db.execute(text(statement), {"id": ids["question_version"]})
                await db.commit()
            await db.rollback()


@pytest.mark.asyncio
async def test_protocol_tables_enable_rls_without_public_data_api_grants(
    protocol_engine: AsyncEngine,
) -> None:
    tables = (
        "research_questions",
        "research_question_versions",
        "research_protocols",
        "research_protocol_versions",
        "protocol_registration_operations",
        "protocol_deviations",
    )
    async with protocol_engine.connect() as connection:
        protected = (
            await connection.execute(
                text("""SELECT relname, relrowsecurity FROM pg_class
                    WHERE relnamespace = current_schema()::regnamespace
                      AND relname = ANY(:tables)"""),
                {"tables": list(tables)},
            )
        ).all()
        grants = await connection.scalar(
            text("""SELECT count(*) FROM information_schema.role_table_grants
                WHERE table_schema=current_schema()
                  AND table_name = ANY(:tables)
                  AND grantee IN ('anon','authenticated')"""),
            {"tables": list(tables)},
        )
    assert dict(protected) == {table: True for table in tables}
    assert grants == 0


@pytest.mark.asyncio
async def test_registration_receipt_is_hash_bound_and_idempotent(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    approval = _approval(ids, "registration-approval")
    async with _client(protocol_engine, ids["supervisor"]) as client:
        approved = await client.post(_approve_path(ids), json=approval)
    assert approved.status_code == 200, approved.text

    receipt = {
        "protocol_version_id": str(ids["protocol_version"]),
        "protocol_version_hash": approval["expected_content_hash"],
        "provider": "manual-registry",
        "status": "registered",
        "idempotency_key": "registration-retry",
        "external_identifier": "receipt-123",
        "url": "https://registry.invalid/receipt-123",
        "receipt": {"recorded": True},
    }
    path = f"/api/v1/research-engine/protocols/{ids['protocol']}/registrations"
    async with _client(protocol_engine, ids["author"]) as client:
        first = await client.post(path, json=receipt)
        replay = await client.post(path, json=receipt)
        stale = await client.post(
            path,
            json={**receipt, "protocol_version_hash": "0" * 64},
        )
    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert stale.status_code == 409
    registration_id = first.json()["id"]
    for statement in (
        "UPDATE protocol_registration_operations SET provider='rewritten' WHERE id=:id",
        "DELETE FROM protocol_registration_operations WHERE id=:id",
    ):
        async with AsyncSession(protocol_engine) as db:
            with pytest.raises(DBAPIError, match="versions are immutable"):
                await db.execute(text(statement), {"id": registration_id})
                await db.commit()
            await db.rollback()
    async with AsyncSession(protocol_engine) as db:
        count = await db.scalar(
            text("SELECT count(*) FROM protocol_registration_operations")
        )
        assert count == 1


@pytest.mark.asyncio
async def test_old_approval_retry_returns_original_receipt_after_newer_approval(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    first_body = _approval(ids, "approval-v1")
    async with _client(protocol_engine, ids["supervisor"]) as client:
        first = await client.post(_approve_path(ids), json=first_body)
    assert first.status_code == 200, first.text

    amendment = {
        "question_version_id": str(ids["question_version"]),
        "blueprint_id": str(ids["blueprint"]),
        "snapshot": {**PROTOCOL_SNAPSHOT, "outcomes": {"primary": "changed"}},
        "parent_version_id": str(ids["protocol_version"]),
        "amendment_reason": "pre-specified outcome correction",
    }
    async with _client(protocol_engine, ids["author"]) as client:
        amended = await client.post(
            f"/api/v1/research-engine/protocols/{ids['protocol']}/versions",
            json=amendment,
        )
    assert amended.status_code == 201, amended.text
    version = amended.json()["versions"][-1]
    second_body = {
        "expected_protocol_version": version["version"],
        "expected_content_hash": version["content_hash"],
        "expected_current_approved_version_id": str(ids["protocol_version"]),
        "reason": "approve amended exact plan",
        "idempotency_key": "approval-v2",
    }
    second_path = (
        f"/api/v1/research-engine/protocols/{ids['protocol']}/versions/"
        f"{version['id']}/approve"
    )
    async with _client(protocol_engine, ids["supervisor"]) as client:
        second = await client.post(second_path, json=second_body)
        replay = await client.post(_approve_path(ids), json=first_body)
    assert second.status_code == 200, second.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert replay.json()["protocol_version_id"] == str(ids["protocol_version"])

"""Persisted authorization, lifecycle, and conflict proofs for protocols."""

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.integration.test_protocol_approval_atomicity import (
    PROTOCOL_SNAPSHOT,
    _approval,
    _approve_path,
    _client,
    _seed,
    protocol_engine,
)

__all__ = ["protocol_engine"]


async def _event_count(engine: AsyncEngine) -> int:
    async with AsyncSession(engine) as db:
        return int(
            await db.scalar(text("SELECT count(*) FROM research_decision_events")) or 0
        )


@pytest.mark.asyncio
async def test_protocol_response_exposes_manage_separately_from_edit(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    path = f"/api/v1/research-engine/protocols/{ids['protocol']}"
    async with _client(protocol_engine, ids["author"]) as owner_client:
        owner = await owner_client.get(path)
    async with _client(protocol_engine, ids["supervisor"]) as editor_client:
        editor = await editor_client.get(path)

    assert owner.status_code == 200, owner.text
    assert owner.json()["can_edit"] is True
    assert owner.json()["can_manage"] is True
    assert editor.status_code == 200, editor.text
    assert editor.json()["can_edit"] is True
    assert editor.json()["can_manage"] is False


@pytest.mark.asyncio
async def test_protocol_list_has_stable_creation_order(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    earlier_id = uuid4()
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with protocol_engine.begin() as db:
        await db.execute(
            text("UPDATE research_protocols SET created_at=:created WHERE id=:id"),
            {"created": created_at, "id": ids["protocol"]},
        )
        await db.execute(
            text(
                """INSERT INTO research_protocols
                (id,collection_id,name,current_draft_version_id,
                 current_approved_version_id,created_at,updated_at,is_deleted)
                VALUES (:id,:collection,'Earlier',NULL,NULL,:created,:created,false)"""
            ),
            {
                "id": earlier_id,
                "collection": ids["collection"],
                "created": created_at,
            },
        )

    async with _client(protocol_engine, ids["author"]) as client:
        response = await client.get(
            f"/api/v1/research-engine/projects/{ids['collection']}/protocols"
        )

    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [
        str(protocol_id) for protocol_id in sorted((ids["protocol"], earlier_id))
    ]


@pytest.mark.asyncio
async def test_duplicate_protocol_name_returns_conflict_under_concurrent_create(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    path = f"/api/v1/research-engine/projects/{ids['collection']}/protocols"
    body = {
        "name": "Concurrent protocol",
        "question_version_id": str(ids["question_version"]),
        "blueprint_id": str(ids["blueprint"]),
        "snapshot": PROTOCOL_SNAPSHOT,
    }

    async def create_protocol() -> Any:
        async with _client(protocol_engine, ids["author"]) as client:
            return await client.post(path, json=body)

    responses = await asyncio.gather(create_protocol(), create_protocol())
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert next(
        response for response in responses if response.status_code == 409
    ).json() == {"detail": "Protocol name already exists"}
    async with AsyncSession(protocol_engine) as db:
        count = await db.scalar(
            text("SELECT count(*) FROM research_protocols WHERE collection_id=:id"),
            {"id": ids["collection"]},
        )
    assert count == 2


@pytest.mark.asyncio
async def test_self_approval_fails_even_with_explicit_supervisor_role(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    async with protocol_engine.begin() as db:
        await db.execute(
            text("""INSERT INTO research_project_role_assignments
                (id,collection_id,user_id,role,assigned_by_id,
                 created_at,updated_at,is_deleted)
                VALUES (gen_random_uuid(),:collection,:author,'supervisor',:author,
                        now(),now(),false)"""),
            ids,
        )
    async with _client(protocol_engine, ids["author"]) as client:
        response = await client.post(
            _approve_path(ids), json=_approval(ids, "self-approval")
        )
    assert response.status_code == 403
    assert await _event_count(protocol_engine) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_role", ["editor", "viewer"])
async def test_workspace_role_without_supervisor_cannot_approve(
    protocol_engine: AsyncEngine,
    workspace_role: str,
) -> None:
    ids = await _seed(protocol_engine)
    async with protocol_engine.begin() as db:
        await db.execute(
            text("""UPDATE workspace_members SET role=:role
                WHERE workspace_id=:workspace AND user_id=:supervisor"""),
            {**ids, "role": workspace_role},
        )
        await db.execute(
            text("""UPDATE research_project_role_assignments
                SET is_deleted=true,deleted_at=now()
                WHERE collection_id=:collection AND user_id=:supervisor"""),
            ids,
        )
    async with _client(protocol_engine, ids["supervisor"]) as client:
        response = await client.post(
            _approve_path(ids), json=_approval(ids, f"no-role-{workspace_role}")
        )
    assert response.status_code == 403
    assert await _event_count(protocol_engine) == 0


@pytest.mark.asyncio
async def test_public_workspace_does_not_grant_outsider_approval(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    outsider = uuid4()
    async with protocol_engine.begin() as db:
        await db.execute(
            text("""INSERT INTO users
                (id,email,password_hash,first_name,last_name,role,is_active,
                 login_count,organization_id,created_at,updated_at,is_deleted)
                VALUES (:id,:email,'x','x','x','USER',true,0,:org,
                        now(),now(),false)"""),
            {
                "id": outsider,
                "email": f"outsider-{uuid4()}@test.invalid",
                "org": ids["org"],
            },
        )
        await db.execute(
            text("UPDATE workspaces SET is_public=true WHERE id=:workspace"), ids
        )
        await db.execute(
            text("""INSERT INTO research_project_role_assignments
                (id,collection_id,user_id,role,assigned_by_id,
                 created_at,updated_at,is_deleted)
                VALUES (gen_random_uuid(),:collection,:outsider,'supervisor',:author,
                        now(),now(),false)"""),
            {**ids, "outsider": outsider},
        )
    async with _client(protocol_engine, outsider) as client:
        response = await client.post(
            _approve_path(ids), json=_approval(ids, "public-outsider")
        )
    assert response.status_code == 404
    assert await _event_count(protocol_engine) == 0


@pytest.mark.asyncio
async def test_foreign_organization_member_cannot_approve(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    foreign_org = uuid4()
    async with protocol_engine.begin() as db:
        await db.execute(
            text("""INSERT INTO organizations
                (id,name,storage_tier,storage_used_bytes,storage_limit_bytes,
                 is_active,created_at,updated_at,is_deleted)
                VALUES (:id,'foreign','FREE',0,1,true,now(),now(),false)"""),
            {"id": foreign_org},
        )
        await db.execute(
            text("UPDATE users SET organization_id=:org WHERE id=:supervisor"),
            {**ids, "org": foreign_org},
        )
    async with _client(protocol_engine, ids["supervisor"]) as client:
        response = await client.post(
            _approve_path(ids), json=_approval(ids, "foreign-org")
        )
    assert response.status_code == 404
    assert await _event_count(protocol_engine) == 0


@pytest.mark.asyncio
async def test_protocol_cannot_bind_blueprint_from_another_project(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    foreign_collection = uuid4()
    foreign_project = uuid4()
    foreign_blueprint = uuid4()
    async with protocol_engine.begin() as db:
        await db.execute(
            text("""INSERT INTO collections
                (id,workspace_id,name,project_type,research_status,tags,is_private,
                 created_at,updated_at,is_deleted)
                VALUES (:collection,:workspace,'foreign','research','active','[]',
                        true,now(),now(),false)"""),
            {**ids, "collection": foreign_collection},
        )
        await db.execute(
            text("""INSERT INTO research_projects
                (id,name,owner_id,status,collection_id,
                 created_at,updated_at,is_deleted)
                VALUES (:project,'foreign',:author,'active',:collection,
                        now(),now(),false)"""),
            {**ids, "project": foreign_project, "collection": foreign_collection},
        )
        await db.execute(
            text("""INSERT INTO research_blueprints
                (id,project_id,name,version,steps,parameters,is_immutable,
                 created_at,updated_at,is_deleted)
                VALUES (:blueprint,:project,'foreign',1,'[]','{}',false,
                        now(),now(),false)"""),
            {"blueprint": foreign_blueprint, "project": foreign_project},
        )
    body = {
        "name": "cross-project",
        "question_version_id": str(ids["question_version"]),
        "blueprint_id": str(foreign_blueprint),
        "snapshot": PROTOCOL_SNAPSHOT,
    }
    async with _client(protocol_engine, ids["author"]) as client:
        response = await client.post(
            f"/api/v1/research-engine/projects/{ids['collection']}/protocols",
            json=body,
        )
    assert response.status_code == 404
    async with AsyncSession(protocol_engine) as db:
        count = await db.scalar(
            text("SELECT count(*) FROM research_protocols WHERE name='cross-project'")
        )
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("UPDATE collections SET research_status='archived' WHERE id=:collection", 409),
        ("UPDATE workspaces SET is_archived=true WHERE id=:workspace", 409),
        ("UPDATE collections SET is_deleted=true WHERE id=:collection", 404),
        ("UPDATE workspaces SET is_deleted=true WHERE id=:workspace", 404),
    ],
)
async def test_archived_or_deleted_ancestor_blocks_approval(
    protocol_engine: AsyncEngine,
    mutation: str,
    expected: int,
) -> None:
    ids = await _seed(protocol_engine)
    async with protocol_engine.begin() as db:
        await db.execute(text(mutation), ids)
    async with _client(protocol_engine, ids["supervisor"]) as client:
        response = await client.post(
            _approve_path(ids), json=_approval(ids, f"lifecycle-{expected}")
        )
    assert response.status_code == expected
    assert await _event_count(protocol_engine) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"expected_protocol_version": 2},
        {"expected_content_hash": "0" * 64},
        {"expected_current_approved_version_id": str(uuid4())},
    ],
)
async def test_stale_approval_guards_leave_no_decision(
    protocol_engine: AsyncEngine,
    changed: dict[str, Any],
) -> None:
    ids = await _seed(protocol_engine)
    body = {**_approval(ids, f"stale-{next(iter(changed))}"), **changed}
    async with _client(protocol_engine, ids["supervisor"]) as client:
        response = await client.post(_approve_path(ids), json=body)
    assert response.status_code == 409
    assert await _event_count(protocol_engine) == 0


@pytest.mark.asyncio
async def test_concurrent_same_approval_replays_one_decision(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    body = _approval(ids, "concurrent-same")
    async with _client(protocol_engine, ids["supervisor"]) as client:
        first, second = await asyncio.gather(
            client.post(_approve_path(ids), json=body),
            client.post(_approve_path(ids), json=body),
        )
    assert [first.status_code, second.status_code] == [200, 200]
    assert first.json() == second.json()
    assert await _event_count(protocol_engine) == 1


@pytest.mark.asyncio
async def test_concurrent_conflicting_retry_allows_exactly_one_approval(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    body = _approval(ids, "concurrent-conflict")
    conflicting = {**body, "reason": "different request with reused key"}
    async with _client(protocol_engine, ids["supervisor"]) as client:
        responses = await asyncio.gather(
            client.post(_approve_path(ids), json=body),
            client.post(_approve_path(ids), json=conflicting),
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert await _event_count(protocol_engine) == 1


@pytest.mark.asyncio
async def test_failed_registration_and_missing_hash_preserve_approval(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    approval = _approval(ids, "registration-failure-approval")
    async with _client(protocol_engine, ids["supervisor"]) as client:
        approved = await client.post(_approve_path(ids), json=approval)
    assert approved.status_code == 200, approved.text
    path = f"/api/v1/research-engine/protocols/{ids['protocol']}/registrations"
    failed = {
        "protocol_version_id": str(ids["protocol_version"]),
        "protocol_version_hash": approval["expected_content_hash"],
        "provider": "manual-registry",
        "status": "failed",
        "idempotency_key": "failed-receipt",
        "failure_reason": "registry was unavailable",
    }
    missing_hash = {
        key: value for key, value in failed.items() if key != "protocol_version_hash"
    }
    async with _client(protocol_engine, ids["author"]) as client:
        missing = await client.post(path, json=missing_hash)
        recorded = await client.post(path, json=failed)
    assert missing.status_code == 422
    assert recorded.status_code == 201, recorded.text
    assert recorded.json()["status"] == "failed"
    async with AsyncSession(protocol_engine) as db:
        pointer = await db.scalar(
            text("""SELECT current_approved_version_id FROM research_protocols
                WHERE id=:protocol"""),
            ids,
        )
        status = await db.scalar(
            text("""SELECT status FROM research_protocol_versions
                WHERE id=:protocol_version"""),
            ids,
        )
    assert pointer == ids["protocol_version"]
    assert status == "approved"


@pytest.mark.asyncio
async def test_concurrent_registration_retry_is_exact_and_conflicts_on_change(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    approval = _approval(ids, "concurrent-registration-approval")
    async with _client(protocol_engine, ids["supervisor"]) as client:
        approved = await client.post(_approve_path(ids), json=approval)
    assert approved.status_code == 200, approved.text

    receipt = {
        "protocol_version_id": str(ids["protocol_version"]),
        "protocol_version_hash": approval["expected_content_hash"],
        "provider": "manual-registry",
        "status": "registered",
        "idempotency_key": "concurrent-registration",
        "external_identifier": "receipt-concurrent",
        "receipt": {"recorded": True},
    }
    path = f"/api/v1/research-engine/protocols/{ids['protocol']}/registrations"
    async with _client(protocol_engine, ids["author"]) as client:
        first, retry = await asyncio.gather(
            client.post(path, json=receipt), client.post(path, json=receipt)
        )
        conflict = await client.post(
            path, json={**receipt, "receipt": {"recorded": False}}
        )
    assert [first.status_code, retry.status_code] == [201, 201]
    assert first.json() == retry.json()
    assert conflict.status_code == 409
    async with AsyncSession(protocol_engine) as db:
        count = await db.scalar(
            text("""SELECT count(*) FROM protocol_registration_operations
                WHERE protocol_version_id=:protocol_version"""),
            ids,
        )
    assert count == 1


@pytest.mark.asyncio
async def test_deviation_output_must_be_step_from_bound_run_and_is_retained(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _seed(protocol_engine)
    approval = _approval(ids, "deviation-approval")
    async with _client(protocol_engine, ids["supervisor"]) as client:
        approved = await client.post(_approve_path(ids), json=approval)
    assert approved.status_code == 200, approved.text

    valid_step = uuid4()
    other_run = uuid4()
    other_step = uuid4()
    foreign_collection = uuid4()
    foreign_project = uuid4()
    foreign_blueprint = uuid4()
    foreign_run = uuid4()
    foreign_step = uuid4()
    async with protocol_engine.begin() as db:
        await db.execute(
            text("""UPDATE research_runs
                SET protocol_version_id=:protocol_version,
                    conformance_status='plan_verified'
                WHERE id=:legacy_run"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO research_runs
                (id,blueprint_id,blueprint_version,protocol_version_id,
                 conformance_status,status,total_tokens,
                 created_at,updated_at,is_deleted)
                VALUES (:run,:blueprint,1,:protocol_version,'plan_verified',
                        'pending',0,now(),now(),false)"""),
            {**ids, "run": other_run},
        )
        for step_id, run_id in (
            (valid_step, ids["legacy_run"]),
            (other_step, other_run),
        ):
            await db.execute(
                text("""INSERT INTO research_steps
                    (id,run_id,step_index,step_type,mode,temperature,token_count,
                     created_at,updated_at,is_deleted)
                    VALUES (:id,:run,0,'extract','deterministic',0,0,
                            now(),now(),false)"""),
                {"id": step_id, "run": run_id},
            )
        await db.execute(
            text("""INSERT INTO collections
                (id,workspace_id,name,project_type,research_status,tags,is_private,
                 created_at,updated_at,is_deleted)
                VALUES (:collection,:workspace,'foreign-output','research','active',
                        '[]',true,now(),now(),false)"""),
            {**ids, "collection": foreign_collection},
        )
        await db.execute(
            text("""INSERT INTO research_projects
                (id,name,owner_id,status,collection_id,
                 created_at,updated_at,is_deleted)
                VALUES (:project,'foreign-output',:author,'active',:collection,
                        now(),now(),false)"""),
            {**ids, "project": foreign_project, "collection": foreign_collection},
        )
        await db.execute(
            text("""INSERT INTO research_blueprints
                (id,project_id,name,version,steps,parameters,is_immutable,
                 created_at,updated_at,is_deleted)
                VALUES (:blueprint,:project,'foreign-output',1,'[]','{}',false,
                        now(),now(),false)"""),
            {"blueprint": foreign_blueprint, "project": foreign_project},
        )
        await db.execute(
            text("""INSERT INTO research_runs
                (id,blueprint_id,blueprint_version,protocol_version_id,
                 conformance_status,status,total_tokens,
                 created_at,updated_at,is_deleted)
                VALUES (:run,:blueprint,1,:protocol_version,'plan_verified',
                        'pending',0,now(),now(),false)"""),
            {
                **ids,
                "run": foreign_run,
                "blueprint": foreign_blueprint,
            },
        )
        await db.execute(
            text("""INSERT INTO research_steps
                (id,run_id,step_index,step_type,mode,temperature,token_count,
                 created_at,updated_at,is_deleted)
                VALUES (:id,:run,0,'extract','deterministic',0,0,
                        now(),now(),false)"""),
            {"id": foreign_step, "run": foreign_run},
        )

    path = f"/api/v1/research-engine/protocols/{ids['protocol']}/deviations"

    def body(run_id: object, step_id: object) -> dict[str, object]:
        return {
            "protocol_version_id": str(ids["protocol_version"]),
            "run_id": str(run_id),
            "output_reference": str(step_id),
            "observed_difference": "actual output differed",
            "rationale": "record retained departure",
            "disposition": "reported",
        }

    async with _client(protocol_engine, ids["author"]) as client:
        cross_run = await client.post(path, json=body(ids["legacy_run"], other_step))
        foreign = await client.post(path, json=body(foreign_run, foreign_step))
        valid = await client.post(path, json=body(ids["legacy_run"], valid_step))
    assert cross_run.status_code == 404
    assert foreign.status_code == 404
    assert valid.status_code == 201, valid.text
    deviation_id = valid.json()["id"]

    for statement in (
        "UPDATE protocol_deviations SET rationale='rewritten' WHERE id=:id",
        "DELETE FROM protocol_deviations WHERE id=:id",
    ):
        async with AsyncSession(protocol_engine) as db:
            with pytest.raises(DBAPIError, match="versions are immutable"):
                await db.execute(text(statement), {"id": deviation_id})
                await db.commit()
            await db.rollback()
    async with AsyncSession(protocol_engine) as db:
        run_status = await db.scalar(
            text("SELECT conformance_status FROM research_runs WHERE id=:legacy_run"),
            ids,
        )
        retained = await db.scalar(
            text("SELECT count(*) FROM protocol_deviations WHERE id=:id"),
            {"id": deviation_id},
        )
    assert run_status == "deviated"
    assert retained == 1

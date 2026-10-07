"""PostgreSQL proof that run export and stage reviews use canonical project access.

GOO-404: these routes previously authorized on ``ResearchProject.owner_id``
only, so the creator bypassed the REVIEWER role and kept access after
revocation while every other member was locked out.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research_engine.reviews import router as reviews_router
from src.api.research_engine.runs import router as runs_router
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import ResearchRun
from src.models.research_stage_review import ResearchStageReview
from src.models.research_step import ResearchStep
from src.services.research_engine.contracts import canonical_stage_output_hash
from src.services.research_engine.project_access import ResearchAction, require_run
from src.services.research_engine.review_service import ResearchReviewService
from tests.integration.test_research_project_mapping import (  # noqa: F401
    mapping_session_factory,
)
from tests.unit.services.test_research_review_service import (
    _export_output,
    _verification_output,
)

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio]

_GATE_STEP_INDEX = 5


@dataclass(frozen=True)
class _Access:
    factory: async_sessionmaker[AsyncSession]
    organization_id: uuid.UUID
    workspace_id: uuid.UUID
    collection_id: uuid.UUID
    workspace_owner_id: uuid.UUID
    creator_id: uuid.UUID
    viewer_id: uuid.UUID
    editor_id: uuid.UUID
    reviewer_id: uuid.UUID
    gate_run_id: uuid.UUID
    completed_run_id: uuid.UUID
    legacy_run_id: uuid.UUID
    gate_output_hash: str


@pytest.fixture
async def access_db(
    mapping_session_factory: async_sessionmaker[AsyncSession],
) -> _Access:
    factory = mapping_session_factory
    ids = {
        key: uuid.uuid4()
        for key in (
            "org",
            "owner",
            "creator",
            "viewer",
            "editor",
            "reviewer",
            "workspace",
            "collection",
            "project",
            "blueprint",
            "gate",
            "completed",
            "legacy_project",
            "legacy_blueprint",
            "legacy_run",
        )
    }
    suffix = uuid.uuid4().hex
    verification = _verification_output()
    export_output = _export_output(verification)
    gate_output_hash = canonical_stage_output_hash(export_output)
    async with factory() as db:
        await db.execute(
            text("""INSERT INTO organizations
                (id,name,storage_tier,storage_used_bytes,storage_limit_bytes,
                 is_active,created_at,updated_at,is_deleted)
                VALUES (:o,:name,'FREE',0,1000,true,now(),now(),false)"""),
            {"o": ids["org"], "name": f"run-access-{suffix}"},
        )
        for key in ("owner", "creator", "viewer", "editor", "reviewer"):
            await db.execute(
                text("""INSERT INTO users
                    (id,email,password_hash,first_name,last_name,role,is_active,
                     login_count,organization_id,created_at,updated_at,is_deleted)
                    VALUES (:id,:email,'x','x','x','USER',true,0,:org,
                            now(),now(),false)"""),
                {
                    "id": ids[key],
                    "email": f"{key}-{suffix}@test.invalid",
                    "org": ids["org"],
                },
            )
        await db.execute(
            text("""INSERT INTO workspaces
                (id,name,is_archived,is_public,owner_id,organization_id,
                 created_at,updated_at,is_deleted)
                VALUES (:workspace,'run-access',false,false,:owner,:org,
                        now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO collections
                (id,workspace_id,name,project_type,research_status,tags,is_private,
                 created_at,updated_at,is_deleted)
                VALUES (:collection,:workspace,'run-access','research','active','[]',
                        true,now(),now(),false)"""),
            ids,
        )
        await db.execute(
            text("""INSERT INTO workspace_members
                (id,workspace_id,user_id,role,joined_at,created_at,updated_at,
                 is_deleted)
                VALUES
                  (gen_random_uuid(),:workspace,:creator,'admin',now(),now(),now(),
                   false),
                  (gen_random_uuid(),:workspace,:viewer,'viewer',now(),now(),now(),
                   false),
                  (gen_random_uuid(),:workspace,:editor,'editor',now(),now(),now(),
                   false),
                  (gen_random_uuid(),:workspace,:reviewer,'editor',now(),now(),now(),
                   false)"""),
            ids,
        )
        # The creator also holds REVIEWER so the revocation case isolates
        # membership; the workspace owner and editor deliberately do not.
        await db.execute(
            text("""INSERT INTO research_project_role_assignments
                (id,collection_id,user_id,role,assigned_by_id,
                 created_at,updated_at,is_deleted)
                VALUES
                  (gen_random_uuid(),:collection,:reviewer,'reviewer',:owner,
                   now(),now(),false),
                  (gen_random_uuid(),:collection,:creator,'reviewer',:owner,
                   now(),now(),false)"""),
            ids,
        )
        await db.commit()
        pending_review = {
            "run_id": str(ids["gate"]),
            "step_index": _GATE_STEP_INDEX,
            "stage_type": "export",
            "review_kind": "final",
            "contract_version": 1,
            "output_hash": gate_output_hash,
            "status": "pending",
            "created_at": "2026-10-07T12:00:00+00:00",
        }
        db.add_all(
            [
                ResearchProject(
                    id=ids["project"],
                    name="Run access fixture",
                    owner_id=ids["creator"],
                    collection_id=ids["collection"],
                ),
                ResearchBlueprint(
                    id=ids["blueprint"],
                    project_id=ids["project"],
                    name="Run access blueprint",
                    version=1,
                    steps=[],
                    parameters={},
                ),
                ResearchRun(
                    id=ids["gate"],
                    blueprint_id=ids["blueprint"],
                    blueprint_version=1,
                    status="paused",
                    reproducibility_manifest={"pending_review": pending_review},
                ),
                ResearchStep(
                    id=uuid.uuid4(),
                    run_id=ids["gate"],
                    step_index=_GATE_STEP_INDEX - 1,
                    step_type="verify",
                    mode="deterministic",
                    output=verification,
                    outputs_hash=canonical_stage_output_hash(verification),
                ),
                ResearchStep(
                    id=uuid.uuid4(),
                    run_id=ids["gate"],
                    step_index=_GATE_STEP_INDEX,
                    step_type="export",
                    mode="deterministic",
                    output=export_output,
                    outputs_hash=gate_output_hash,
                ),
                ResearchRun(
                    id=ids["completed"],
                    blueprint_id=ids["blueprint"],
                    blueprint_version=1,
                    status="completed",
                    reproducibility_manifest={},
                ),
                ResearchProject(
                    id=ids["legacy_project"],
                    name="Unmapped legacy project",
                    owner_id=ids["creator"],
                    collection_id=None,
                ),
                ResearchBlueprint(
                    id=ids["legacy_blueprint"],
                    project_id=ids["legacy_project"],
                    name="Legacy blueprint",
                    version=1,
                    steps=[],
                    parameters={},
                ),
                ResearchRun(
                    id=ids["legacy_run"],
                    blueprint_id=ids["legacy_blueprint"],
                    blueprint_version=1,
                    status="completed",
                    reproducibility_manifest={},
                ),
            ]
        )
        await db.commit()
    return _Access(
        factory=factory,
        organization_id=ids["org"],
        workspace_id=ids["workspace"],
        collection_id=ids["collection"],
        workspace_owner_id=ids["owner"],
        creator_id=ids["creator"],
        viewer_id=ids["viewer"],
        editor_id=ids["editor"],
        reviewer_id=ids["reviewer"],
        gate_run_id=ids["gate"],
        completed_run_id=ids["completed"],
        legacy_run_id=ids["legacy_run"],
        gate_output_hash=gate_output_hash,
    )


def _app(access: _Access, user_id: uuid.UUID) -> FastAPI:
    app = FastAPI()
    app.include_router(runs_router)
    app.include_router(reviews_router)

    async def override_db() -> Any:
        async with access.factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id, organization_id=access.organization_id
    )
    return app


def _final_approval(access: _Access) -> dict[str, object]:
    return {
        "review_kind": "final",
        "output_hash": access.gate_output_hash,
        "decision": "approve",
        "decision_payload": {},
    }


async def _as(access: _Access, user_id: uuid.UUID) -> dict[str, httpx.Response]:
    """Hit pending, submit, and export once as ``user_id``."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(access, user_id)),
        base_url="http://run-access.test",
    ) as client:
        pending = await client.get(
            f"/research-engine/runs/{access.gate_run_id}/reviews/pending"
        )
        export = await client.get(
            f"/research-engine/runs/{access.completed_run_id}/export",
            params={"format": "json"},
        )
        submit = await client.post(
            f"/research-engine/runs/{access.gate_run_id}/reviews/{_GATE_STEP_INDEX}",
            json=_final_approval(access),
        )
    return {"pending": pending, "export": export, "submit": submit}


async def _review_count(access: _Access) -> int:
    async with access.factory() as db:
        return int(
            await db.scalar(select(func.count()).select_from(ResearchStageReview)) or 0
        )


async def test_viewer_member_reads_pending_review_and_export(
    access_db: _Access,
) -> None:
    """(a) Non-creator members with VIEW are no longer locked out (404)."""
    responses = await _as(access_db, access_db.viewer_id)

    assert responses["pending"].status_code == 200, responses["pending"].text
    pending = responses["pending"].json()
    assert pending["pending"] is True
    assert pending["descriptor"]["output_hash"] == access_db.gate_output_hash
    assert responses["export"].status_code == 200, responses["export"].text
    assert responses["export"].headers["content-type"] == "application/json"
    # VIEW is not REVIEW: a member without the REVIEWER role cannot decide.
    assert responses["submit"].status_code == 403
    assert await _review_count(access_db) == 0


async def test_removed_creator_loses_pending_submit_and_export(
    access_db: _Access,
) -> None:
    """(b) ResearchProject.owner_id no longer outlives workspace membership."""
    async with access_db.factory() as db:
        await db.execute(
            text("""UPDATE workspace_members SET is_deleted=true
                WHERE workspace_id=:w AND user_id=:u"""),
            {"w": access_db.workspace_id, "u": access_db.creator_id},
        )
        await db.commit()

    responses = await _as(access_db, access_db.creator_id)

    assert responses["pending"].status_code == 404
    assert responses["export"].status_code == 404
    assert responses["submit"].status_code == 404
    assert await _review_count(access_db) == 0


@pytest.mark.parametrize("actor", ["editor", "workspace_owner"])
async def test_edit_member_and_owner_without_reviewer_role_get_403(
    access_db: _Access, actor: str
) -> None:
    """(c) EDIT, and even workspace OWNER, do not imply the REVIEWER role."""
    user_id = access_db.editor_id if actor == "editor" else access_db.workspace_owner_id

    responses = await _as(access_db, user_id)

    assert responses["pending"].status_code == 200
    assert responses["export"].status_code == 200
    assert responses["submit"].status_code == 403, responses["submit"].text
    assert await _review_count(access_db) == 0
    async with access_db.factory() as db:
        run = await db.get(ResearchRun, access_db.gate_run_id)
        assert run is not None
        assert run.reproducibility_manifest["pending_review"]["status"] == "pending"
        assert "final_approval_attestation" not in run.reproducibility_manifest


async def test_reviewer_member_final_approval_writes_canonical_attestation(
    access_db: _Access,
) -> None:
    """(d) A non-creator REVIEWER approves; the audit row uses ProjectContext."""
    responses = await _as(access_db, access_db.reviewer_id)

    assert responses["submit"].status_code == 200, responses["submit"].text
    body = responses["submit"].json()
    assert body["reviewer_id"] == str(access_db.reviewer_id)
    assert body["replay"] is False
    async with access_db.factory() as db:
        review = (
            await db.execute(
                select(ResearchStageReview).where(
                    ResearchStageReview.run_id == access_db.gate_run_id
                )
            )
        ).scalar_one()
        assert review.reviewer_id == access_db.reviewer_id
        assert review.owner_id == access_db.creator_id
        assert review.organization_id == access_db.organization_id
        run = await db.get(ResearchRun, access_db.gate_run_id)
        assert run is not None
        manifest = run.reproducibility_manifest
        assert manifest["pending_review"]["status"] == "approved"
        assert manifest["pending_review"]["review_id"] == str(review.id)
        attestation = manifest["final_approval_attestation"]
        assert attestation["reviewer_id"] == str(access_db.reviewer_id)
        assert attestation["review_id"] == str(review.id)
        assert attestation["output_hash"] == access_db.gate_output_hash


async def test_soft_deleted_collection_hides_reviews_and_export(
    access_db: _Access,
) -> None:
    """(e) Collection lifecycle is enforced; the creator gets no fallback."""
    async with access_db.factory() as db:
        await db.execute(
            text("UPDATE collections SET is_deleted=true WHERE id=:c"),
            {"c": access_db.collection_id},
        )
        await db.commit()

    for user_id in (access_db.creator_id, access_db.reviewer_id):
        responses = await _as(access_db, user_id)
        assert responses["pending"].status_code == 404
        assert responses["export"].status_code == 404
        assert responses["submit"].status_code == 404
    assert await _review_count(access_db) == 0


async def test_non_creator_edit_member_rebuilds_approved_overlays(
    access_db: _Access,
) -> None:
    """(f) stream_run's overlay rebuild no longer 409s for non-creators."""
    responses = await _as(access_db, access_db.reviewer_id)
    assert responses["submit"].status_code == 200, responses["submit"].text

    async with access_db.factory() as db:
        # stream_run authorizes EDIT first; the editor holds no REVIEWER role.
        await require_run(
            db, access_db.gate_run_id, access_db.editor_id, ResearchAction.EDIT
        )
        projected = await ResearchReviewService(db).apply_approved_overlays(
            run_id=access_db.gate_run_id, context={"kept": True}
        )

    assert projected["kept"] is True
    overlays = projected["approved_review_overlays"]
    assert [overlay["reviewer_id"] for overlay in overlays] == [
        str(access_db.reviewer_id)
    ]


async def test_unmapped_legacy_project_requires_mapping(access_db: _Access) -> None:
    """Legacy owner-only projects now behave like get_run: 409 mapping_required."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(access_db, access_db.creator_id)),
        base_url="http://run-access.test",
    ) as client:
        export = await client.get(
            f"/research-engine/runs/{access_db.legacy_run_id}/export",
            params={"format": "json"},
        )
        pending = await client.get(
            f"/research-engine/runs/{access_db.legacy_run_id}/reviews/pending"
        )

    assert export.status_code == 409
    assert export.json()["detail"] == "mapping_required"
    assert pending.status_code == 409
    assert pending.json()["detail"] == "mapping_required"

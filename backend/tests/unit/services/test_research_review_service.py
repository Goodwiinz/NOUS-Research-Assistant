"""Exact-hash, exact-set review behavior for persisted research stages."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from importlib import import_module
from types import ModuleType
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401 - register every relationship target
from src.models.base import Base
from src.models.organization import Organization
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import ResearchRun
from src.models.research_stage_review import ResearchStageReview
from src.models.research_step import ResearchStep
from src.models.user import User
from src.services.research_engine.contracts import (
    canonical_json_sha256,
    canonical_stage_output_hash,
)
from src.services.research_engine.observability import ResearchObservability


def _review_module() -> ModuleType:
    return import_module("src.services.research_engine.review_service")


def _schemas() -> ModuleType:
    return import_module("src.schemas.research_engine")


def _usage() -> dict[str, object]:
    return {"model_calls": 1, "total_tokens": 10, "batches": []}


def _screen_output() -> dict[str, object]:
    return {
        "contract_version": 1,
        "stage_type": "screen",
        "usage": _usage(),
        "screening": [
            {
                "source_id": "source-a",
                "part_id": "p0001",
                "included": True,
                "reason": "machine recommendation",
            },
            {
                "source_id": "source-b",
                "part_id": "p0001",
                "included": False,
                "reason": "machine recommendation",
            },
        ],
        "included_source_ids": ["source-a"],
        "processing_coverage": {},
    }


def _extract_output() -> dict[str, object]:
    return {
        "contract_version": 1,
        "stage_type": "extract",
        "usage": _usage(),
        "extractions": [
            {
                "source_id": "source-a",
                "part_id": "p0001",
                "data": {"effect": "positive"},
                "evidence": [
                    {
                        "pointer": "/effect",
                        "quote": "The effect was positive.",
                        "page_reference": None,
                    }
                ],
            },
            {
                "source_id": "source-b",
                "part_id": "p0002",
                "data": {"effect": "uncertain"},
                "evidence": [
                    {
                        "pointer": "/effect",
                        "quote": "The estimate was uncertain.",
                        "page_reference": None,
                    }
                ],
            },
        ],
        "processing_coverage": {},
    }


def _verification_output(*, passed: bool = True) -> dict[str, object]:
    return {
        "contract_version": 1,
        "stage_type": "verify",
        "usage": _usage(),
        "verification": {
            "passed": passed,
            "deterministic_passed": passed,
            "schema_passed": passed,
            "semantic_status": "supported" if passed else "failed",
            "claims": [
                {
                    "claim_id": "c0001",
                    "status": "supported" if passed else "unverified",
                    "reason": "Grounded in e0001" if passed else "Missing support",
                }
            ],
            "coverage_complete": passed,
            "continued_after_failure": False,
        },
        "processing_coverage": {},
    }


def _export_output(
    verification_output: dict[str, object],
    *,
    valid_report_hash: bool = True,
    valid_verification_hash: bool = True,
) -> dict[str, object]:
    report = {
        "contract_version": 1,
        "final_status": "verified",
        "claims": [{"claim_id": "c0001", "evidence_ids": ["e0001"]}],
    }
    return {
        "contract_version": 1,
        "stage_type": "export",
        "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
        "format": "markdown",
        "exported": report,
        "markdown": "# Reviewed Daily Brief\n",
        "content": "# Reviewed Daily Brief\n",
        "media_type": "text/markdown; charset=utf-8",
        "verification_output_hash": (
            canonical_stage_output_hash(verification_output)
            if valid_verification_hash
            else "0" * 64
        ),
        "report_hash": (
            canonical_json_sha256(report) if valid_report_hash else "0" * 64
        ),
    }


def _screen_payload(
    *, second: str = "exclude", second_reason: str | None = "out_of_scope"
) -> dict[str, object]:
    return {
        "items": [
            {
                "source_id": "source-a",
                "part_id": "p0001",
                "decision": "include",
            },
            {
                "source_id": "source-b",
                "part_id": "p0001",
                "decision": second,
                **({"reason": second_reason} if second_reason is not None else {}),
            },
        ]
    }


def _extract_payload(
    *, second: str = "reject", second_reason: str | None = "not_reliable"
) -> dict[str, object]:
    return {
        "items": [
            {
                "source_id": "source-a",
                "part_id": "p0001",
                "decision": "accept",
            },
            {
                "source_id": "source-b",
                "part_id": "p0002",
                "decision": second,
                **({"reason": second_reason} if second_reason is not None else {}),
            },
        ]
    }


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Organization.__table__,
        User.__table__,
        ResearchProject.__table__,
        ResearchBlueprint.__table__,
        ResearchRun.__table__,
        ResearchStep.__table__,
        ResearchStageReview.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed_gate(
    db: AsyncSession,
    *,
    output: dict[str, object] | None = None,
    review_kind: str = "screening",
    owner_id: UUID | None = None,
    organization_id: UUID | None = None,
    step_index: int = 1,
) -> tuple[UUID, UUID, ResearchRun, ResearchStep]:
    owner_id = owner_id or uuid4()
    organization_id = organization_id or uuid4()
    output = copy.deepcopy(output or _screen_output())
    output_hash = canonical_stage_output_hash(output)
    project = ResearchProject(
        id=uuid4(), name="Owned project", owner_id=owner_id, status="active"
    )
    blueprint = ResearchBlueprint(
        id=uuid4(),
        project_id=project.id,
        name="Daily brief",
        template_source="daily_research_brief",
        version=1,
        steps=[],
        parameters={},
    )
    run = ResearchRun(
        id=uuid4(),
        blueprint_id=blueprint.id,
        blueprint_version=1,
        status="paused",
        reproducibility_manifest={
            "pending_review": {
                "run_id": None,
                "step_index": step_index,
                "stage_type": output["stage_type"],
                "review_kind": review_kind,
                "contract_version": 1,
                "output_hash": output_hash,
                "status": "pending",
                "created_at": "2026-09-27T12:00:00+00:00",
            }
        },
    )
    run.reproducibility_manifest["pending_review"]["run_id"] = str(run.id)
    step = ResearchStep(
        id=uuid4(),
        run_id=run.id,
        step_index=step_index,
        step_type=str(output["stage_type"]),
        mode="deterministic",
        output=output,
        outputs_hash=output_hash,
    )
    db.add_all([project, blueprint, run, step])
    await db.commit()
    return owner_id, organization_id, run, step


def _request(
    *,
    kind: str,
    output_hash: str,
    payload: dict[str, object],
    decision: str = "approve",
    note: str | None = None,
) -> Any:
    return _schemas().StageReviewRequest.model_validate(
        {
            "review_kind": kind,
            "output_hash": output_hash,
            "decision": decision,
            "decision_payload": payload,
            **({"note": note} if note is not None else {}),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "output", "payload"),
    [
        ("screening", _screen_output(), _screen_payload()),
        ("extraction", _extract_output(), _extract_payload()),
    ],
)
async def test_submit_review_records_exact_set_approval_without_mutating_output(
    db: AsyncSession,
    kind: str,
    output: dict[str, object],
    payload: dict[str, object],
) -> None:
    owner_id, organization_id, run, step = await _seed_gate(
        db, output=output, review_kind=kind
    )
    before = copy.deepcopy(step.output)
    service = _review_module().ResearchReviewService(db)

    response = await service.submit_review(
        run_id=run.id,
        step_index=step.step_index,
        owner_id=owner_id,
        organization_id=organization_id,
        reviewer_id=owner_id,
        request=_request(
            kind=kind,
            output_hash=canonical_stage_output_hash(output),
            payload=payload,
        ),
    )

    await db.refresh(run)
    await db.refresh(step)
    assert response.review_kind.value == kind
    assert response.decision.value == "approve"
    assert response.replay is False
    assert response.decision_payload == payload
    assert step.output == before
    pending = run.reproducibility_manifest["pending_review"]
    assert pending["status"] == "approved"
    assert pending["review_id"] == str(response.id)


@pytest.mark.asyncio
async def test_review_submission_records_wait_and_extraction_outcomes(
    db: AsyncSession,
) -> None:
    output = _extract_output()
    owner_id, organization_id, run, step = await _seed_gate(
        db, output=output, review_kind="extraction", step_index=2
    )
    observer = ResearchObservability()
    service = _review_module().ResearchReviewService(
        db,
        observer=observer,
        now=lambda: datetime(2026, 9, 27, 12, 0, 30, tzinfo=timezone.utc),
    )

    await service.submit_review(
        run_id=run.id,
        step_index=step.step_index,
        owner_id=owner_id,
        organization_id=organization_id,
        reviewer_id=owner_id,
        request=_request(
            kind="extraction",
            output_hash=canonical_stage_output_hash(output),
            payload=_extract_payload(),
        ),
    )

    metrics = observer.snapshot()
    assert metrics["counters"]["reviews"]["approved"] == 1
    assert metrics["counters"]["extraction_decisions"] == {
        "accept": 1,
        "reject": 1,
    }
    assert metrics["histograms"]["review_wait_seconds"]["extraction"] == [30.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "output", "payload"),
    [
        (
            "screening",
            _screen_output(),
            {
                "items": [
                    {
                        "source_id": "source-a",
                        "part_id": "p0001",
                        "decision": "include",
                    },
                    {
                        "source_id": "source-a",
                        "part_id": "p0001",
                        "decision": "exclude",
                        "reason": "duplicate",
                    },
                ]
            },
        ),
        (
            "extraction",
            _extract_output(),
            {
                "items": [
                    {"source_id": "source-a", "part_id": "p0001", "decision": "accept"},
                    {
                        "source_id": "injected",
                        "part_id": "p9999",
                        "decision": "reject",
                        "reason": "unknown",
                    },
                ]
            },
        ),
    ],
)
async def test_submit_review_rejects_duplicate_omitted_or_injected_items(
    db: AsyncSession,
    kind: str,
    output: dict[str, object],
    payload: dict[str, object],
) -> None:
    owner_id, organization_id, run, step = await _seed_gate(
        db, output=output, review_kind=kind
    )
    service_module = _review_module()

    with pytest.raises(service_module.ResearchReviewError) as error:
        await service_module.ResearchReviewService(db).submit_review(
            run_id=run.id,
            step_index=step.step_index,
            owner_id=owner_id,
            organization_id=organization_id,
            reviewer_id=owner_id,
            request=_request(
                kind=kind,
                output_hash=canonical_stage_output_hash(output),
                payload=payload,
            ),
        )

    assert error.value.code == "review_payload_incomplete"
    assert await db.scalar(select(func.count()).select_from(ResearchStageReview)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "output", "payload"),
    [
        (
            "screening",
            _screen_output(),
            _screen_payload(second="unresolved", second_reason=None),
        ),
        (
            "extraction",
            _extract_output(),
            _extract_payload(second="unresolved", second_reason=None),
        ),
    ],
)
async def test_unresolved_items_block_approval(
    db: AsyncSession,
    kind: str,
    output: dict[str, object],
    payload: dict[str, object],
) -> None:
    owner_id, organization_id, run, step = await _seed_gate(
        db, output=output, review_kind=kind
    )
    service_module = _review_module()

    with pytest.raises(service_module.ResearchReviewError) as error:
        await service_module.ResearchReviewService(db).submit_review(
            run_id=run.id,
            step_index=step.step_index,
            owner_id=owner_id,
            organization_id=organization_id,
            reviewer_id=owner_id,
            request=_request(
                kind=kind,
                output_hash=canonical_stage_output_hash(output),
                payload=payload,
            ),
        )

    assert error.value.code == "review_payload_incomplete"


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("screening", _screen_payload(second_reason=None)),
        ("extraction", _extract_payload(second_reason=None)),
        ("screening", _screen_payload(second_reason="x" * 501)),
        ("extraction", _extract_payload(second_reason="x" * 501)),
    ],
)
def test_public_schema_requires_bounded_reasons(
    kind: str, payload: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        _request(kind=kind, output_hash="a" * 64, payload=payload)


@pytest.mark.parametrize(
    ("kind", "payload", "injected"),
    [
        ("screening", _screen_payload(), {"title": "client supplied"}),
        ("screening", _screen_payload(), {"citation": {"doi": "10.fake/1"}}),
        ("extraction", _extract_payload(), {"text": "fabricated evidence"}),
        ("extraction", _extract_payload(), {"source": {"id": "new"}}),
    ],
)
def test_public_schema_forbids_client_content_fields(
    kind: str, payload: dict[str, object], injected: dict[str, object]
) -> None:
    payload = copy.deepcopy(payload)
    payload["items"][0].update(injected)  # type: ignore[index,union-attr]
    with pytest.raises(ValueError):
        _request(kind=kind, output_hash="a" * 64, payload=payload)


def test_public_schema_forbids_extra_fields_and_bounds_note() -> None:
    schemas = _schemas()
    valid = {
        "review_kind": "screening",
        "output_hash": "a" * 64,
        "decision": "approve",
        "decision_payload": _screen_payload(),
    }
    with pytest.raises(ValueError):
        schemas.StageReviewRequest.model_validate({**valid, "research_text": "secret"})
    with pytest.raises(ValueError):
        schemas.StageReviewRequest.model_validate({**valid, "note": "n" * 2001})


@pytest.mark.asyncio
async def test_pending_review_is_owner_scoped_and_same_org_does_not_grant_access(
    db: AsyncSession,
) -> None:
    owner_id, organization_id, run, step = await _seed_gate(db)
    same_org_other_owner = uuid4()
    service_module = _review_module()
    service = service_module.ResearchReviewService(db)

    pending = await service.get_pending_review(run_id=run.id, owner_id=owner_id)
    assert pending.pending is True
    assert pending.descriptor.output_hash == canonical_stage_output_hash(
        _screen_output()
    )
    assert pending.stage_output == _screen_output()

    for inaccessible_id in (run.id, uuid4()):
        with pytest.raises(service_module.ResearchReviewError) as error:
            await service.get_pending_review(
                run_id=inaccessible_id, owner_id=same_org_other_owner
            )
        assert error.value.status_code == 404

    with pytest.raises(service_module.ResearchReviewError) as error:
        await service.submit_review(
            run_id=run.id,
            step_index=step.step_index,
            owner_id=same_org_other_owner,
            organization_id=organization_id,
            reviewer_id=same_org_other_owner,
            request=_request(
                kind="screening",
                output_hash=canonical_stage_output_hash(_screen_output()),
                payload=_screen_payload(),
            ),
        )
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_pending_review_requires_current_mapped_project_access(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_id, _organization_id, run, _step = await _seed_gate(db)
    project = (
        await db.execute(
            select(ResearchProject)
            .join(ResearchBlueprint, ResearchBlueprint.project_id == ResearchProject.id)
            .where(ResearchBlueprint.id == run.blueprint_id)
        )
    ).scalar_one()
    project.collection_id = uuid4()
    await db.commit()
    require_access = AsyncMock(
        side_effect=HTTPException(status_code=404, detail="Run not found")
    )
    service_module = _review_module()
    monkeypatch.setattr(service_module, "_require_mapped_run_access", require_access)

    with pytest.raises(HTTPException) as error:
        await service_module.ResearchReviewService(db).get_pending_review(
            run_id=run.id,
            owner_id=owner_id,
        )

    assert error.value.status_code == 404
    require_access.assert_awaited_once_with(
        db,
        run.id,
        owner_id,
        "view",
    )


@pytest.mark.asyncio
async def test_submit_review_rechecks_current_mapped_project_access_in_transaction(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_id, organization_id, run, step = await _seed_gate(db)
    run_id = run.id
    step_index = step.step_index
    project = (
        await db.execute(
            select(ResearchProject)
            .join(ResearchBlueprint, ResearchBlueprint.project_id == ResearchProject.id)
            .where(ResearchBlueprint.id == run.blueprint_id)
        )
    ).scalar_one()
    project.collection_id = uuid4()
    await db.commit()
    require_access = AsyncMock(
        side_effect=HTTPException(status_code=404, detail="Run not found")
    )
    service_module = _review_module()
    monkeypatch.setattr(service_module, "_require_mapped_run_access", require_access)

    with pytest.raises(HTTPException) as error:
        await service_module.ResearchReviewService(db).submit_review(
            run_id=run_id,
            step_index=step_index,
            owner_id=owner_id,
            organization_id=organization_id,
            reviewer_id=owner_id,
            request=_request(
                kind="screening",
                output_hash=canonical_stage_output_hash(_screen_output()),
                payload=_screen_payload(),
            ),
        )

    assert error.value.status_code == 404
    require_access.assert_awaited_once_with(
        db,
        run_id,
        owner_id,
        "edit",
    )
    assert (
        await db.execute(select(func.count()).select_from(ResearchStageReview))
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_stale_hash_returns_only_current_content_free_descriptor(
    db: AsyncSession,
) -> None:
    owner_id, organization_id, run, step = await _seed_gate(db)
    service_module = _review_module()
    observer = ResearchObservability()

    with pytest.raises(service_module.ResearchReviewError) as error:
        await service_module.ResearchReviewService(db, observer=observer).submit_review(
            run_id=run.id,
            step_index=step.step_index,
            owner_id=owner_id,
            organization_id=organization_id,
            reviewer_id=owner_id,
            request=_request(
                kind="screening", output_hash="f" * 64, payload=_screen_payload()
            ),
        )

    assert error.value.status_code == 409
    assert error.value.code == "review_output_stale"
    descriptor = error.value.descriptor
    assert descriptor is not None
    dumped = descriptor.model_dump(mode="json", exclude_none=True)
    assert set(dumped) <= {
        "run_id",
        "step_index",
        "stage_type",
        "review_kind",
        "contract_version",
        "output_hash",
        "status",
        "created_at",
        "review_id",
    }
    serialized = str(dumped)
    assert "machine recommendation" not in serialized
    assert "source-a" not in serialized
    assert observer.snapshot()["counters"]["reviews"]["stale"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("descriptor_change", "route_step_index"),
    [
        ({"review_kind": "extraction"}, 1),
        ({"stage_type": "extract"}, 1),
        ({"contract_version": 2}, 1),
        ({}, 2),
    ],
)
async def test_submit_review_requires_exact_kind_stage_index_and_contract_binding(
    db: AsyncSession,
    descriptor_change: dict[str, object],
    route_step_index: int,
) -> None:
    owner_id, organization_id, run, _step = await _seed_gate(db)
    manifest = copy.deepcopy(run.reproducibility_manifest)
    manifest["pending_review"].update(descriptor_change)
    run.reproducibility_manifest = manifest
    await db.commit()
    service_module = _review_module()

    with pytest.raises(service_module.ResearchReviewError) as error:
        await service_module.ResearchReviewService(db).submit_review(
            run_id=run.id,
            step_index=route_step_index,
            owner_id=owner_id,
            organization_id=organization_id,
            reviewer_id=owner_id,
            request=_request(
                kind="screening",
                output_hash=canonical_stage_output_hash(_screen_output()),
                payload=_screen_payload(),
            ),
        )

    assert error.value.code == "review_required"
    assert await db.scalar(select(func.count()).select_from(ResearchStageReview)) == 0


@pytest.mark.asyncio
async def test_identical_canonical_replay_returns_original_and_different_replay_conflicts(
    db: AsyncSession,
) -> None:
    output = _screen_output()
    owner_id, organization_id, run, step = await _seed_gate(db, output=output)
    service_module = _review_module()
    observer = ResearchObservability()
    service = service_module.ResearchReviewService(db, observer=observer)
    first_payload = _screen_payload()
    first_items = cast(list[dict[str, object]], first_payload["items"])
    first_payload["items"] = list(reversed(first_items))

    first = await service.submit_review(
        run_id=run.id,
        step_index=step.step_index,
        owner_id=owner_id,
        organization_id=organization_id,
        reviewer_id=owner_id,
        request=_request(
            kind="screening",
            output_hash=canonical_stage_output_hash(output),
            payload=first_payload,
            note="same decision",
        ),
    )
    replay = await service.submit_review(
        run_id=run.id,
        step_index=step.step_index,
        owner_id=owner_id,
        organization_id=organization_id,
        reviewer_id=owner_id,
        request=_request(
            kind="screening",
            output_hash=canonical_stage_output_hash(output),
            payload=_screen_payload(),
            note="same decision",
        ),
    )

    assert replay.id == first.id
    assert replay.replay is True
    assert await db.scalar(select(func.count()).select_from(ResearchStageReview)) == 1
    assert observer.snapshot()["counters"]["reviews"]["approved"] == 1

    different = _screen_payload(second="include", second_reason=None)
    with pytest.raises(service_module.ResearchReviewError) as error:
        await service.submit_review(
            run_id=run.id,
            step_index=step.step_index,
            owner_id=owner_id,
            organization_id=organization_id,
            reviewer_id=owner_id,
            request=_request(
                kind="screening",
                output_hash=canonical_stage_output_hash(output),
                payload=different,
                note="different decision",
            ),
        )
    assert error.value.code == "review_decision_conflict"


@pytest.mark.asyncio
async def test_integrity_error_race_reloads_and_replays_the_original_row(
    db: AsyncSession,
) -> None:
    output = _screen_output()
    owner_id, organization_id, run, step = await _seed_gate(db, output=output)
    request = _request(
        kind="screening",
        output_hash=canonical_stage_output_hash(output),
        payload=_screen_payload(),
        note="same decision",
    )
    first = (
        await _review_module()
        .ResearchReviewService(db)
        .submit_review(
            run_id=run.id,
            step_index=step.step_index,
            owner_id=owner_id,
            organization_id=organization_id,
            reviewer_id=owner_id,
            request=request,
        )
    )
    racing_service = _review_module().ResearchReviewService(db)
    racing_service._submit_transaction = AsyncMock(  # type: ignore[method-assign]
        side_effect=IntegrityError("insert", {}, Exception("unique race"))
    )

    replay = await racing_service.submit_review(
        run_id=run.id,
        step_index=step.step_index,
        owner_id=owner_id,
        organization_id=organization_id,
        reviewer_id=owner_id,
        request=request,
    )

    assert replay.id == first.id
    assert replay.replay is True
    assert await db.scalar(select(func.count()).select_from(ResearchStageReview)) == 1


@pytest.mark.asyncio
async def test_decline_is_durable_and_keeps_run_paused(
    db: AsyncSession,
) -> None:
    output = _screen_output()
    owner_id, organization_id, run, step = await _seed_gate(db, output=output)
    observer = ResearchObservability()

    response = (
        await _review_module()
        .ResearchReviewService(db, observer=observer)
        .submit_review(
            run_id=run.id,
            step_index=step.step_index,
            owner_id=owner_id,
            organization_id=organization_id,
            reviewer_id=owner_id,
            request=_request(
                kind="screening",
                output_hash=canonical_stage_output_hash(output),
                payload=_screen_payload(second="unresolved", second_reason=None),
                decision="decline",
                note="Revise the scope before trying again.",
            ),
        )
    )

    await db.refresh(run)
    assert response.decision.value == "decline"
    assert run.status == "paused"
    assert run.reproducibility_manifest["pending_review"]["status"] == "pending"
    stored = await db.scalar(select(ResearchStageReview))
    assert stored is not None and stored.decision == "decline"
    assert observer.snapshot()["counters"]["reviews"]["declined"] == 1


@pytest.mark.asyncio
async def test_extraction_decline_accepts_an_all_unresolved_exact_set(
    db: AsyncSession,
) -> None:
    output = _extract_output()
    owner_id, organization_id, run, step = await _seed_gate(
        db, output=output, review_kind="extraction", step_index=2
    )
    payload: dict[str, object] = {
        "items": [
            {
                "source_id": "source-a",
                "part_id": "p0001",
                "decision": "unresolved",
            },
            {
                "source_id": "source-b",
                "part_id": "p0002",
                "decision": "unresolved",
            },
        ]
    }
    request = _request(
        kind="extraction",
        output_hash=canonical_stage_output_hash(output),
        payload=payload,
        decision="decline",
    )

    assert isinstance(
        request.decision_payload,
        _schemas().ExtractionReviewDecisionPayload,
    )
    response = (
        await _review_module()
        .ResearchReviewService(db)
        .submit_review(
            run_id=run.id,
            step_index=step.step_index,
            owner_id=owner_id,
            organization_id=organization_id,
            reviewer_id=owner_id,
            request=request,
        )
    )

    await db.refresh(run)
    assert response.decision.value == "decline"
    assert response.decision_payload == payload
    assert run.reproducibility_manifest["pending_review"]["status"] == "pending"


@pytest.mark.asyncio
async def test_pending_large_output_projects_every_ordered_identity_within_bound(
    db: AsyncSession,
) -> None:
    identities = [(f"source-{index:03d}", f"p{index:04d}") for index in range(40)]
    output = {
        "contract_version": 1,
        "stage_type": "extract",
        "usage": _usage(),
        "extractions": [
            {
                "source_id": source_id,
                "part_id": part_id,
                "evidence_level": "full_text",
                "data": {"sensitive_text": "x" * 1200},
                "evidence": [{"quote": "q" * 1200}],
            }
            for source_id, part_id in identities
        ],
        "processing_coverage": {},
    }
    assert len(json.dumps(output).encode("utf-8")) > 32 * 1024
    original = copy.deepcopy(output)
    owner_id, _organization_id, run, step = await _seed_gate(
        db, output=output, review_kind="extraction", step_index=2
    )

    pending = (
        await _review_module()
        .ResearchReviewService(db)
        .get_pending_review(
            run_id=run.id,
            owner_id=owner_id,
        )
    )

    assert pending.stage_output is not None
    projection = pending.stage_output
    assert projection["review_projection"] == {
        "projected": True,
        "truncated": True,
        "identity_complete": True,
    }
    projected_items = projection["extractions"]
    assert [
        (item["source_id"], item["part_id"]) for item in projected_items
    ] == identities
    assert all(item["evidence_level"] == "full_text" for item in projected_items)
    assert "sensitive_text" not in json.dumps(projection)
    assert len(json.dumps(projection).encode("utf-8")) <= 32 * 1024
    await db.refresh(step)
    assert step.output == original


@pytest.mark.asyncio
async def test_pending_projection_covers_200_near_worst_case_identities(
    db: AsyncSession,
) -> None:
    identities = [
        (
            f"s{index:03d}-" + ("s" * 507),
            f"p{index:03d}-" + ("p" * 507),
        )
        for index in range(200)
    ]
    assert all(len(source_id) == 512 for source_id, _part_id in identities)
    assert all(len(part_id) == 512 for _source_id, part_id in identities)
    output = {
        "contract_version": 1,
        "stage_type": "screen",
        "usage": _usage(),
        "screening": [
            {
                "source_id": source_id,
                "part_id": part_id,
                "included": index % 2 == 0,
                "evidence_level": "full_text",
                "reason": "private model rationale " + ("r" * 512),
            }
            for index, (source_id, part_id) in enumerate(identities)
        ],
        "included_source_ids": [source_id for source_id, _part_id in identities],
        "processing_coverage": {"private": "c" * 4096},
    }
    owner_id, _organization_id, run, _step = await _seed_gate(
        db,
        output=output,
        review_kind="screening",
        step_index=1,
    )

    pending = (
        await _review_module()
        .ResearchReviewService(db)
        .get_pending_review(
            run_id=run.id,
            owner_id=owner_id,
        )
    )

    assert pending.stage_output is not None
    projection = pending.stage_output
    assert projection["review_projection"]["identity_complete"] is True
    assert [
        (item["source_id"], item["part_id"]) for item in projection["screening"]
    ] == identities
    serialized = json.dumps(projection)
    assert "private model rationale" not in serialized
    assert '"private"' not in serialized
    assert len(serialized.encode("utf-8")) < 1_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("passed", "valid_report_hash", "valid_verification_hash"),
    [(False, True, True), (True, False, True), (True, True, False)],
)
async def test_final_approval_rejects_unverified_or_unbound_export(
    db: AsyncSession,
    passed: bool,
    valid_report_hash: bool,
    valid_verification_hash: bool,
) -> None:
    verification = _verification_output(passed=passed)
    export = _export_output(
        verification,
        valid_report_hash=valid_report_hash,
        valid_verification_hash=valid_verification_hash,
    )
    owner_id, organization_id, run, export_step = await _seed_gate(
        db, output=export, review_kind="final", step_index=5
    )
    verify_step = ResearchStep(
        id=uuid4(),
        run_id=run.id,
        step_index=4,
        step_type="verify",
        mode="deterministic",
        output=verification,
        outputs_hash=canonical_stage_output_hash(verification),
    )
    db.add(verify_step)
    await db.commit()
    service_module = _review_module()

    with pytest.raises(service_module.ResearchReviewError) as error:
        await service_module.ResearchReviewService(db).submit_review(
            run_id=run.id,
            step_index=export_step.step_index,
            owner_id=owner_id,
            organization_id=organization_id,
            reviewer_id=owner_id,
            request=_request(
                kind="final",
                output_hash=canonical_stage_output_hash(export),
                payload={},
            ),
        )
    assert error.value.code == "review_payload_incomplete"


@pytest.mark.asyncio
async def test_final_approval_accepts_exact_verified_export_bindings(
    db: AsyncSession,
) -> None:
    verification = _verification_output()
    export = _export_output(verification)
    owner_id, organization_id, run, export_step = await _seed_gate(
        db, output=export, review_kind="final", step_index=5
    )
    db.add(
        ResearchStep(
            id=uuid4(),
            run_id=run.id,
            step_index=4,
            step_type="verify",
            mode="deterministic",
            output=verification,
            outputs_hash=canonical_stage_output_hash(verification),
        )
    )
    await db.commit()

    response = (
        await _review_module()
        .ResearchReviewService(db)
        .submit_review(
            run_id=run.id,
            step_index=export_step.step_index,
            owner_id=owner_id,
            organization_id=organization_id,
            reviewer_id=owner_id,
            request=_request(
                kind="final",
                output_hash=canonical_stage_output_hash(export),
                payload={},
            ),
        )
    )

    assert response.decision.value == "approve"
    assert response.decision_payload == {}
    await db.refresh(run)
    attestation = run.reproducibility_manifest["final_approval_attestation"]
    assert attestation == {
        "schema_version": 1,
        "review_id": str(response.id),
        "reviewer_id": str(owner_id),
        "reviewed_at": response.created_at.isoformat(),
        "decision": "approve",
        "review_kind": "final",
        "step_index": 5,
        "output_hash": canonical_stage_output_hash(export),
        "report_hash": export["report_hash"],
        "verification_output_hash": export["verification_output_hash"],
        "attestation_hash": attestation["attestation_hash"],
    }
    unsigned = {
        key: value for key, value in attestation.items() if key != "attestation_hash"
    }
    assert attestation["attestation_hash"] == canonical_json_sha256(unsigned)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "json_format",
        "missing_markdown",
        "non_string_markdown",
        "missing_content",
        "non_string_content",
        "mismatched_content",
    ],
)
async def test_final_approval_rejects_export_without_canonical_markdown_bytes(
    db: AsyncSession,
    corruption: str,
) -> None:
    verification = _verification_output()
    export = _export_output(verification)
    if corruption == "json_format":
        export["format"] = "json"
    elif corruption == "missing_markdown":
        export.pop("markdown")
    elif corruption == "non_string_markdown":
        export["markdown"] = {"not": "bytes"}
    elif corruption == "missing_content":
        export.pop("content")
    elif corruption == "non_string_content":
        export["content"] = ["not", "bytes"]
    else:
        export["content"] = "# Different Daily Brief\n"
    owner_id, organization_id, run, export_step = await _seed_gate(
        db, output=export, review_kind="final", step_index=5
    )
    db.add(
        ResearchStep(
            id=uuid4(),
            run_id=run.id,
            step_index=4,
            step_type="verify",
            mode="deterministic",
            output=verification,
            outputs_hash=canonical_stage_output_hash(verification),
        )
    )
    await db.commit()
    service_module = _review_module()

    with pytest.raises(service_module.ResearchReviewError) as error:
        await service_module.ResearchReviewService(db).submit_review(
            run_id=run.id,
            step_index=export_step.step_index,
            owner_id=owner_id,
            organization_id=organization_id,
            reviewer_id=owner_id,
            request=_request(
                kind="final",
                output_hash=canonical_stage_output_hash(export),
                payload={},
            ),
        )

    assert error.value.code == "review_payload_incomplete"


@pytest.mark.asyncio
async def test_apply_approved_overlays_is_deterministic_and_preserves_stage_output(
    db: AsyncSession,
) -> None:
    output = _extract_output()
    owner_id, organization_id, run, step = await _seed_gate(
        db, output=output, review_kind="extraction", step_index=2
    )
    original = copy.deepcopy(output)
    service = _review_module().ResearchReviewService(db)
    await service.submit_review(
        run_id=run.id,
        step_index=step.step_index,
        owner_id=owner_id,
        organization_id=organization_id,
        reviewer_id=owner_id,
        request=_request(
            kind="extraction",
            output_hash=canonical_stage_output_hash(output),
            payload=_extract_payload(),
        ),
    )
    context = {"extractions": copy.deepcopy(output["extractions"]), "other": "kept"}

    first = await service.apply_approved_overlays(
        run_id=run.id, owner_id=owner_id, context=context
    )
    second = await service.apply_approved_overlays(
        run_id=run.id, owner_id=owner_id, context=context
    )

    await db.refresh(step)
    original_extractions = cast(list[dict[str, object]], output["extractions"])
    assert first == second
    assert first["extractions"] == [original_extractions[0]]
    assert context["extractions"] == output["extractions"]
    assert step.output == original

"""Real-PostgreSQL proof for concurrent exact-hash stage review decisions."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import ResearchRun
from src.models.research_stage_review import ResearchStageReview
from src.models.research_step import ResearchStep
from src.schemas.research_engine import StageReviewRequest, StageReviewResponse
from src.services.research_engine.contracts import canonical_stage_output_hash
from src.services.research_engine.review_service import (
    ResearchReviewError,
    ResearchReviewService,
)
from tests.integration.research_engine_postgres_support import (
    create_research_engine_tables,
    seed_canonical_project_scope,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_postgres,
    pytest.mark.asyncio,
]


@dataclass(frozen=True)
class _ReviewDatabase:
    factory: async_sessionmaker[AsyncSession]
    owner_id: uuid.UUID
    organization_id: uuid.UUID
    run_id: uuid.UUID
    step_index: int
    output_hash: str


class _TwoPartyBarrier:
    def __init__(self) -> None:
        self.arrivals = 0
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self.arrivals += 1
            if self.arrivals == 2:
                self._ready.set()
        await asyncio.wait_for(self._ready.wait(), timeout=3)


def _async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be a PostgreSQL URL")


def _screen_output() -> dict[str, Any]:
    return {
        "contract_version": 1,
        "stage_type": "screen",
        "usage": {"model_calls": 1, "total_tokens": 4, "batches": []},
        "screening": [
            {
                "source_id": "source-a",
                "part_id": "p0001",
                "included": True,
                "reason": "persisted recommendation",
            },
            {
                "source_id": "source-b",
                "part_id": "p0001",
                "included": False,
                "reason": "persisted recommendation",
            },
        ],
        "included_source_ids": ["source-a"],
        "processing_coverage": {},
    }


@asynccontextmanager
async def _postgres_review_schema(dsn: str) -> AsyncIterator[_ReviewDatabase]:
    schema = "research_review_" + uuid.uuid4().hex
    admin_engine = create_async_engine(_async_dsn(dsn))
    scoped_engine = None
    try:
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

        scoped_engine = create_async_engine(
            _async_dsn(dsn),
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with scoped_engine.begin() as connection:
            await create_research_engine_tables(
                connection,
                ResearchProject,
                ResearchBlueprint,
                ResearchRun,
                ResearchStep,
                ResearchStageReview,
            )

        factory = async_sessionmaker(scoped_engine, expire_on_commit=False)
        owner_id = uuid.uuid4()
        organization_id = uuid.uuid4()
        project_id = uuid.uuid4()
        blueprint_id = uuid.uuid4()
        run_id = uuid.uuid4()
        step_index = 1
        output = _screen_output()
        output_hash = canonical_stage_output_hash(output)
        async with scoped_engine.begin() as connection:
            canonical_scope = await seed_canonical_project_scope(
                connection,
                owner_id=owner_id,
                organization_id=organization_id,
                reviewer_ids=(owner_id,),
            )
        async with factory() as session:
            session.add_all(
                [
                    ResearchProject(
                        id=project_id,
                        name="Review concurrency fixture",
                        owner_id=owner_id,
                        collection_id=canonical_scope.collection_id,
                    ),
                    ResearchBlueprint(
                        id=blueprint_id,
                        project_id=project_id,
                        name="Daily brief review concurrency",
                        version=1,
                        steps=[],
                        parameters={},
                    ),
                    ResearchRun(
                        id=run_id,
                        blueprint_id=blueprint_id,
                        blueprint_version=1,
                        status="paused",
                        reproducibility_manifest={
                            "pending_review": {
                                "run_id": str(run_id),
                                "step_index": step_index,
                                "stage_type": "screen",
                                "review_kind": "screening",
                                "contract_version": 1,
                                "output_hash": output_hash,
                                "status": "pending",
                                "created_at": "2026-09-27T12:00:00+00:00",
                            }
                        },
                    ),
                    ResearchStep(
                        id=uuid.uuid4(),
                        run_id=run_id,
                        step_index=step_index,
                        step_type="screen",
                        mode="deterministic",
                        output=output,
                        outputs_hash=output_hash,
                    ),
                ]
            )
            await session.commit()

        yield _ReviewDatabase(
            factory=factory,
            owner_id=owner_id,
            organization_id=organization_id,
            run_id=run_id,
            step_index=step_index,
            output_hash=output_hash,
        )
    finally:
        if scoped_engine is not None:
            await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
        await admin_engine.dispose()


def _decline_request(database: _ReviewDatabase, *, second: str) -> StageReviewRequest:
    return cast(
        StageReviewRequest,
        StageReviewRequest.model_validate(
            {
                "review_kind": "screening",
                "output_hash": database.output_hash,
                "decision": "decline",
                "decision_payload": {
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
                            **(
                                {"reason": "not in scope"}
                                if second == "exclude"
                                else {}
                            ),
                        },
                    ]
                },
                "note": "concurrent decision",
            }
        ),
    )


async def _race_reviews(
    database: _ReviewDatabase,
    left: StageReviewRequest,
    right: StageReviewRequest,
) -> tuple[list[StageReviewResponse | BaseException], int, int]:
    barrier = _TwoPartyBarrier()

    async def submit(request: StageReviewRequest) -> StageReviewResponse:
        async with database.factory() as session:
            # Both sessions are open before either authorizes; REVIEW then
            # serializes them on the Collection lock, and the loser observes
            # the winner's committed row (replay or stable conflict).
            await barrier.wait()
            return await ResearchReviewService(session).submit_review(
                run_id=database.run_id,
                step_index=database.step_index,
                reviewer_id=database.owner_id,
                request=request,
            )

    results = await asyncio.gather(
        submit(left),
        submit(right),
        return_exceptions=True,
    )
    async with database.factory() as session:
        row_count = int(
            await session.scalar(select(func.count()).select_from(ResearchStageReview))
            or 0
        )
    return list(results), barrier.arrivals, row_count


async def test_postgres_identical_concurrent_decisions_replay_one_ledger_row() -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    assert dsn is not None

    async with _postgres_review_schema(dsn) as database:
        request = _decline_request(database, second="exclude")
        results, arrivals, row_count = await _race_reviews(database, request, request)

    responses = [
        result for result in results if isinstance(result, StageReviewResponse)
    ]
    assert arrivals == 2
    assert len(responses) == 2
    assert {response.replay for response in responses} == {False, True}
    assert len({response.id for response in responses}) == 1
    assert row_count == 1


async def test_postgres_different_concurrent_decisions_return_stable_conflict() -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    assert dsn is not None

    async with _postgres_review_schema(dsn) as database:
        results, arrivals, row_count = await _race_reviews(
            database,
            _decline_request(database, second="exclude"),
            _decline_request(database, second="include"),
        )

    responses = [
        result for result in results if isinstance(result, StageReviewResponse)
    ]
    conflicts = [
        result for result in results if isinstance(result, ResearchReviewError)
    ]
    assert arrivals == 2
    assert len(responses) == 1
    assert responses[0].replay is False
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409
    assert conflicts[0].code == "review_decision_conflict"
    assert row_count == 1

"""PostgreSQL-backed API proof for persisted research-run resume ownership."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Update

from src.api.research_engine.runs import router as runs_router
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import ResearchRun
from src.models.research_step import ResearchStep

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_postgres,
    pytest.mark.asyncio,
]


def _async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be a PostgreSQL URL")


@asynccontextmanager
async def _research_schema(
    dsn: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create only the research tables in a disposable, isolated schema."""
    schema = "research_resume_" + uuid.uuid4().hex
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
            await connection.exec_driver_sql(
                'CREATE TABLE "users" (id UUID PRIMARY KEY)'
            )
            for model in (
                ResearchProject,
                ResearchBlueprint,
                ResearchRun,
                ResearchStep,
            ):
                await connection.run_sync(cast(Any, model).__table__.create)

        yield async_sessionmaker(scoped_engine, expire_on_commit=False)
    finally:
        if scoped_engine is not None:
            await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
        await admin_engine.dispose()


def _search_envelope(source_id: str) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "stage_type": "search",
        "usage": {"model_calls": 1, "total_tokens": 11, "batches": []},
        "source_records": [{"source_id": source_id, "title": "Persisted source"}],
        "coverage": {"processed_source_ids": [source_id]},
        "selected_sources": [source_id],
        "query": {"query": "bounded test query"},
        "sources": [{"id": source_id, "title": "Persisted source"}],
    }


def _verification_envelope() -> dict[str, Any]:
    return {
        "contract_version": 1,
        "stage_type": "verify",
        "usage": {"model_calls": 2, "total_tokens": 12, "batches": []},
        "verification": {
            "passed": False,
            "deterministic_passed": False,
            "schema_passed": True,
            "semantic_status": "failed",
            "coverage_complete": True,
            "claims": [
                {
                    "claim_id": "claim-stable-1",
                    "status": "contradicted",
                    "reason": "Persisted failing verification",
                }
            ],
        },
        "processing_coverage": {"verified_claim_ids": []},
    }


async def _seed_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    steps: list[dict[str, Any]],
    persisted_outputs: list[dict[str, Any]] | None = None,
    status: str = "paused",
    total_tokens: int = 0,
) -> tuple[uuid.UUID, uuid.UUID]:
    owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    blueprint_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with factory() as db:
        # The production join checks project ownership through this key.
        await db.execute(text("INSERT INTO users (id) VALUES (:id)"), {"id": owner_id})
        db.add(
            ResearchProject(
                id=project_id,
                name="Resume fixture",
                owner_id=owner_id,
            )
        )
        db.add(
            ResearchBlueprint(
                id=blueprint_id,
                project_id=project_id,
                name="Resume fixture blueprint",
                version=1,
                steps=steps,
                parameters={"contract_version": 1},
            )
        )
        db.add(
            ResearchRun(
                id=run_id,
                blueprint_id=blueprint_id,
                blueprint_version=1,
                status=status,
                total_tokens=total_tokens,
                reproducibility_manifest={"parameters_override": {}},
            )
        )
        for index, output in enumerate(persisted_outputs or []):
            db.add(
                ResearchStep(
                    run_id=run_id,
                    step_index=index,
                    step_type=str(output["stage_type"]),
                    output=output,
                    token_count=int(output["usage"]["total_tokens"]),
                    quality_marks=(
                        [{"check_type": "verification", "passed": False}]
                        if output["stage_type"] == "verify"
                        else []
                    ),
                )
            )
        await db.commit()
    return owner_id, run_id


class _RecordingWorkflowEngine:
    calls: list[dict[str, Any]] = []

    def __init__(self, step_executor: Any) -> None:
        del step_executor

    async def run(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(kwargs)
        yield {"event": "run_complete"}


class _RacingSession:
    """Commit a competing status transition immediately before the real claim."""

    def __init__(
        self,
        db: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        run_id: uuid.UUID,
    ) -> None:
        self._db = db
        self._factory = factory
        self._run_id = run_id
        self._raced = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(statement, Update) and not self._raced:
            self._raced = True
            async with self._factory() as competitor:
                await competitor.execute(
                    update(ResearchRun)
                    .where(
                        ResearchRun.id == self._run_id,
                        ResearchRun.status == "paused",
                    )
                    .values(status="running")
                )
                await competitor.commit()
        return await self._db.execute(statement, *args, **kwargs)


def _app_with_database(
    factory: async_sessionmaker[AsyncSession],
    owner_id: uuid.UUID,
    organization_id: uuid.UUID,
    *,
    race_run_id: uuid.UUID | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(runs_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            if race_run_id is None:
                yield db
            else:
                yield cast(AsyncSession, _RacingSession(db, factory, race_run_id))

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=owner_id,
        organization_id=organization_id,
    )
    return app


async def test_persisted_post_resume_stream_rehydrates_failed_stages_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    source_id = "source-stable-17"
    blueprint_steps = [
        {"type": "search", "name": "Search", "params": {"contract_version": 1}},
        {"type": "verify", "name": "Verify", "params": {"contract_version": 1}},
        {"type": "export", "name": "Export", "params": {"contract_version": 1}},
    ]
    async with _research_schema(dsn) as factory:
        owner_id, run_id = await _seed_run(
            factory,
            steps=blueprint_steps,
            persisted_outputs=[_search_envelope(source_id), _verification_envelope()],
            total_tokens=23,
        )
        app = _app_with_database(factory, owner_id, uuid.uuid4())
        _RecordingWorkflowEngine.calls.clear()
        monkeypatch.setattr(
            "src.api.research_engine.runs.WorkflowEngine", _RecordingWorkflowEngine
        )
        monkeypatch.setattr(
            "src.api.research_engine.runs._build_connectors", lambda **_: object()
        )

        async def admitted(**_: Any) -> bool:
            return True

        monkeypatch.setattr(
            "src.api.research_engine.runs.admit_expensive_work", admitted
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resumed = await client.post(f"/research-engine/runs/{run_id}/resume")
            assert resumed.status_code == 200
            assert resumed.json()["status"] == "paused"

            streamed = await client.get(f"/research-engine/runs/{run_id}/stream")
            assert streamed.status_code == 200
            assert "event: run_complete" in streamed.text

        assert len(_RecordingWorkflowEngine.calls) == 1
        call = _RecordingWorkflowEngine.calls[0]
        assert call["start_from_step"] == 2
        assert call["initial_total_tokens"] == 23
        assert call["initial_context"]["selected_sources"] == [source_id]
        assert call["initial_context"]["source_records"][0]["source_id"] == source_id
        assert call["initial_context"]["stage_results"].keys() == {"0", "1"}
        verification = call["initial_context"]["verification"]
        assert verification["passed"] is False
        assert verification["continued_after_failure"] is True

        async with factory() as db:
            run = await db.get(ResearchRun, run_id)
            assert run is not None
            assert run.status == "completed"
            assert run.total_tokens == 23
            assert run.reproducibility_manifest["continued_after_failure"] is True
            assert (
                run.reproducibility_manifest["continued_after_failure_step_index"] == 1
            )
            persisted_steps = (
                (
                    await db.execute(
                        select(ResearchStep).where(ResearchStep.run_id == run_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(persisted_steps) == 2


async def test_persisted_resume_losing_stream_returns_conflict_without_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    async with _research_schema(dsn) as factory:
        owner_id, run_id = await _seed_run(
            factory,
            steps=[
                {"type": "export", "name": "Export", "params": {"contract_version": 1}}
            ],
        )
        app = _app_with_database(factory, owner_id, uuid.uuid4(), race_run_id=run_id)
        _RecordingWorkflowEngine.calls.clear()
        monkeypatch.setattr(
            "src.api.research_engine.runs.WorkflowEngine", _RecordingWorkflowEngine
        )
        monkeypatch.setattr(
            "src.api.research_engine.runs._build_connectors", lambda **_: object()
        )

        async def admitted(**_: Any) -> bool:
            return True

        monkeypatch.setattr(
            "src.api.research_engine.runs.admit_expensive_work", admitted
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resumed = await client.post(f"/research-engine/runs/{run_id}/resume")
            assert resumed.status_code == 200
            streamed = await client.get(f"/research-engine/runs/{run_id}/stream")
            assert streamed.status_code == 409
            assert "claimed by another stream" in streamed.json()["detail"]

        assert _RecordingWorkflowEngine.calls == []
        async with factory() as db:
            run = await db.get(ResearchRun, run_id)
            assert run is not None
            assert run.status == "running"

"""PostgreSQL-backed API proof for persisted research-run resume ownership."""

from __future__ import annotations

import json
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
from src.models.research_source import ResearchSource
from src.models.research_step import ResearchStep
from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.export_service import ExportService
from src.services.research_engine.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderConfig,
)

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
                ResearchSource,
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


class _InvalidReductionProvider(LLMProvider):
    """Return one paid map batch, then malformed paid reduction JSON."""

    def __init__(self) -> None:
        super().__init__(ProviderConfig(provider_type="test", model_id="test-model"))
        self.requests: list[LLMRequest] = []

    async def is_model_available(self) -> bool:
        return True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) > 1:
            content = '{"sections": ['
        else:
            prompt_body = json.loads(request.prompt)
            evidence_ids: list[str] = []
            for record in prompt_body["records"]:
                for item in json.loads(record["text"]):
                    evidence_ids.append(item["evidence_id"])
            content = json.dumps(
                {
                    "sections": [
                        {
                            "heading": "Findings",
                            "claims": [
                                {
                                    "claim_text": f"Finding {evidence_id}",
                                    "evidence": [
                                        {
                                            "evidence_id": evidence_id,
                                            "relation": "supports",
                                        }
                                    ],
                                }
                                for evidence_id in evidence_ids
                            ],
                        }
                    ]
                }
            )
        return LLMResponse(
            content=content,
            model_id="test-model",
            input_tokens=40,
            output_tokens=20,
        )


class _SlowSecondBatchProvider(LLMProvider):
    """Pause after one successful response so timeout accounting is durable."""

    def __init__(self) -> None:
        super().__init__(ProviderConfig(provider_type="test", model_id="test-model"))
        self.requests: list[LLMRequest] = []

    async def is_model_available(self) -> bool:
        return True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) > 1:
            import asyncio

            await asyncio.sleep(5)
        prompt_body = json.loads(request.prompt)
        content = json.dumps(
            {
                "records": [
                    {
                        "source_id": item["source_id"],
                        "part_id": item["part_id"],
                        "claims": [],
                    }
                    for item in prompt_body["records"]
                ]
            }
        )
        return LLMResponse(
            content=content,
            model_id="test-model",
            input_tokens=40,
            output_tokens=20,
        )


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


async def test_direct_paused_stream_persists_continuation_and_unverified_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    source_id = "source-stable-direct-continuation"
    blueprint_steps = [
        {"type": "search", "name": "Search", "params": {"contract_version": 1}},
        {"type": "verify", "name": "Verify", "params": {"contract_version": 1}},
        {
            "type": "export",
            "name": "Export",
            "params": {"contract_version": 1, "format": "markdown"},
        },
    ]
    async with _research_schema(dsn) as factory:
        owner_id, run_id = await _seed_run(
            factory,
            steps=blueprint_steps,
            persisted_outputs=[_search_envelope(source_id), _verification_envelope()],
            total_tokens=23,
        )
        app = _app_with_database(factory, owner_id, uuid.uuid4())
        monkeypatch.setattr(
            "src.api.research_engine.runs._build_connectors", lambda **_: {}
        )

        async def admitted(**_: Any) -> bool:
            return True

        monkeypatch.setattr(
            "src.api.research_engine.runs.admit_expensive_work", admitted
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            streamed = await client.get(f"/research-engine/runs/{run_id}/stream")
            assert streamed.status_code == 200
            assert "event: step_complete" in streamed.text
            assert "event: run_complete" in streamed.text

        async with factory() as db:
            run = await db.get(ResearchRun, run_id)
            assert run is not None
            assert run.status == "completed"
            manifest = cast(dict[str, Any], run.reproducibility_manifest)
            assert manifest["continued_after_failure"] is True
            assert manifest["continued_after_failure_step_index"] == 1
            persisted = (
                (
                    await db.execute(
                        select(ResearchStep)
                        .where(ResearchStep.run_id == run_id)
                        .order_by(ResearchStep.step_index.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert [item.step_index for item in persisted] == [0, 1, 2]
            export_step = persisted[-1]
            assert export_step.output["stage_type"] == "export"
            assert (
                "continued after a failed quality check"
                in export_step.output["markdown"]
            )

            report = await ExportService().export_json(run_id, db)
            assert report["verification"]["passed"] is False
            assert report["verification"]["continued_after_failure"] is True
            assert report["continued_after_failure"] is True
            assert "continued after a failed quality check" in report["markdown"]


async def test_repeated_resume_executes_export_and_reconstructs_unverified_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    source_id = "source-stable-continued"
    blueprint_steps = [
        {"type": "search", "name": "Search", "params": {"contract_version": 1}},
        {"type": "verify", "name": "Prior Verify", "params": {"contract_version": 1}},
        {"type": "verify", "name": "Recheck", "params": {"contract_version": 1}},
        {
            "type": "export",
            "name": "Export",
            "params": {"contract_version": 1, "format": "markdown"},
        },
    ]
    async with _research_schema(dsn) as factory:
        owner_id, run_id = await _seed_run(
            factory,
            steps=blueprint_steps,
            persisted_outputs=[_search_envelope(source_id), _verification_envelope()],
            total_tokens=23,
        )
        app = _app_with_database(factory, owner_id, uuid.uuid4())
        monkeypatch.setattr(
            "src.api.research_engine.runs._build_connectors", lambda **_: {}
        )

        async def admitted(**_: Any) -> bool:
            return True

        monkeypatch.setattr(
            "src.api.research_engine.runs.admit_expensive_work", admitted
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            first_resume = await client.post(f"/research-engine/runs/{run_id}/resume")
            assert first_resume.status_code == 200
            first_stream = await client.get(f"/research-engine/runs/{run_id}/stream")
            assert first_stream.status_code == 200
            assert "event: run_paused" in first_stream.text
            paused_data = next(
                line.removeprefix("data: ")
                for line in first_stream.text.splitlines()
                if line.startswith("data: ") and '"event": "run_paused"' in line
            )
            paused_event = json.loads(paused_data)
            assert paused_event["context"]["verification"]["passed"] is False
            assert (
                paused_event["context"]["verification"]["continued_after_failure"]
                is True
            )

            second_resume = await client.post(f"/research-engine/runs/{run_id}/resume")
            assert second_resume.status_code == 200
            completed_stream = await client.get(
                f"/research-engine/runs/{run_id}/stream"
            )
            assert completed_stream.status_code == 200
            assert "event: run_complete" in completed_stream.text

        async with factory() as db:
            run = await db.get(ResearchRun, run_id)
            assert run is not None
            assert run.status == "completed"
            assert run.total_tokens == 23
            assert run.reproducibility_manifest["continued_after_failure"] is True
            assert (
                run.reproducibility_manifest["continued_after_failure_step_index"] == 1
            )
            persisted = (
                (
                    await db.execute(
                        select(ResearchStep)
                        .where(ResearchStep.run_id == run_id)
                        .order_by(ResearchStep.step_index.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert [item.step_index for item in persisted] == [0, 1, 2, 3]
            export_step = persisted[-1]
            assert export_step.output["stage_type"] == "export"
            assert export_step.output["format"] == "markdown"
            assert (
                "continued after a failed quality check"
                in export_step.output["markdown"]
            )

            report = await ExportService().export_json(run_id, db)
            assert report["verification"]["passed"] is False
            assert report["verification"]["continued_after_failure"] is True
            assert report["continued_after_failure"] is True
            assert "continued after a failed quality check" in report["markdown"]


async def test_paid_reduction_parse_failure_persists_usage_hashes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    source_id = "source-stable-accounting"
    quote = "Synthetic evidence quote."
    search = _search_envelope(source_id)
    search["source_records"][0].update(
        {"abstract": quote, "evidence_level": "abstract"}
    )
    extraction = {
        "contract_version": 1,
        "stage_type": "extract",
        "usage": {"model_calls": 1, "total_tokens": 5, "batches": []},
        "extractions": [
            {
                "source_id": source_id,
                "part_id": "p0001",
                "claims": [
                    {
                        "claim_text": f"Finding {index}",
                        "evidence_id": f"e{index:04d}",
                        "quote": quote,
                        "confidence": 0.9,
                        "page_reference": None,
                    }
                    for index in range(1, 66)
                ],
            }
        ],
        "processing_coverage": {"1": {"complete": True}},
    }
    steps = [
        {"type": "search", "name": "Search", "params": {"contract_version": 1}},
        {"type": "extract", "name": "Extract", "params": {"contract_version": 1}},
        {"type": "verify", "name": "Verify", "params": {"contract_version": 1}},
        {
            "type": "synthesize",
            "name": "Synthesize",
            "model_id": "test-model",
            "params": {"contract_version": 1},
        },
        {"type": "export", "name": "Export", "params": {"contract_version": 1}},
    ]
    async with _research_schema(dsn) as factory:
        owner_id, run_id = await _seed_run(
            factory,
            steps=steps,
            persisted_outputs=[search, extraction, _verification_envelope()],
            total_tokens=23,
        )
        provider = _InvalidReductionProvider()
        app = _app_with_database(factory, owner_id, uuid.uuid4())
        monkeypatch.setattr(
            "src.api.research_engine.runs._build_connectors", lambda **_: {}
        )
        monkeypatch.setattr(
            "src.api.research_engine.runs._build_providers",
            lambda _steps: {"test-model": provider},
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
            assert streamed.status_code == 200
            assert streamed.text.count("event: step_error") == 1
            assert "event: run_failed" in streamed.text

        assert len(provider.requests) == 2
        async with factory() as db:
            run = await db.get(ResearchRun, run_id)
            assert run is not None
            assert run.status == "failed"
            assert run.total_tokens == 143
            manifest = cast(dict[str, Any], run.reproducibility_manifest)
            errors = manifest["execution_errors"]
            assert len(errors) == 1
            error = errors[0]
            assert error["step_index"] == 3
            assert error["model_calls"] == 2
            assert error["consumed_tokens"] == 120
            hashes = error["batch_metadata"]
            assert len(hashes) == 2
            assert all(item["input_hash"] and item["output_hash"] for item in hashes)


async def test_timeout_after_paid_batch_persists_usage_and_hash_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    source_records = [
        {
            "source_id": f"source-{index}",
            "title": f"Persisted source {index}",
            "abstract": "Synthetic evidence sentence.",
            "evidence_level": "abstract",
        }
        for index in range(10)
    ]
    search = _search_envelope(source_records[0]["source_id"])
    search["source_records"] = source_records
    search["selected_sources"] = [item["source_id"] for item in source_records]
    steps = [
        {"type": "search", "name": "Search", "params": {"contract_version": 1}},
        {"type": "verify", "name": "Prior Verify", "params": {"contract_version": 1}},
        {
            "type": "extract",
            "name": "Extract",
            "model_id": "test-model",
            "params": {
                "contract_version": 1,
                "output_kind": "claims",
            },
        },
    ]
    async with _research_schema(dsn) as factory:
        owner_id, run_id = await _seed_run(
            factory,
            steps=steps,
            persisted_outputs=[search, _verification_envelope()],
            total_tokens=23,
        )
        provider = _SlowSecondBatchProvider()
        app = _app_with_database(factory, owner_id, uuid.uuid4())
        monkeypatch.setattr(
            "src.api.research_engine.runs._build_connectors", lambda **_: {}
        )
        monkeypatch.setattr(
            "src.api.research_engine.runs._build_providers",
            lambda _steps: {"test-model": provider},
        )

        class ShortTimeoutEngine(WorkflowEngine):
            def __init__(self, step_executor: Any) -> None:
                super().__init__(step_executor, max_wall_time_seconds=0.25)

        monkeypatch.setattr(
            "src.api.research_engine.runs.WorkflowEngine", ShortTimeoutEngine
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
            assert streamed.status_code == 200
            assert streamed.text.count("event: step_error") == 1
            assert "event: run_failed" in streamed.text

        assert len(provider.requests) == 2
        async with factory() as db:
            run = await db.get(ResearchRun, run_id)
            assert run is not None
            assert run.status == "failed"
            assert run.total_tokens == 83
            errors = run.reproducibility_manifest["execution_errors"]
            assert len(errors) == 1
            error = errors[0]
            assert error["step_index"] == 2
            assert error["model_calls"] == 2
            assert error["consumed_tokens"] == 60
            hashes = error["batch_metadata"]
            assert len(hashes) == 1
            assert hashes[0]["input_hash"] and hashes[0]["output_hash"]


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

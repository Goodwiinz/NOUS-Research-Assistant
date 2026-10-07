"""PostgreSQL certification for the Daily Research Brief lifecycle."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.api.research_engine.runs as runs_module
from src.api.research_engine.blueprints import router as blueprints_router
from src.api.research_engine.capabilities import router as capabilities_router
from src.api.research_engine.projects import router as projects_router
from src.api.research_engine.reviews import router as reviews_router
from src.api.research_engine.runs import router as runs_router
from src.api.research_engine.steps import router as steps_router
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import ResearchRun, RunStatus
from src.models.research_source import ResearchSource
from src.models.research_stage_review import ResearchStageReview
from src.models.research_step import ResearchStep
from src.schemas.research_engine import DailyBriefScopeConfirmation
from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.connectors.registry import safe_capability_projection
from src.services.research_engine.contracts import (
    canonical_json_sha256,
    canonical_stage_output_hash,
)
from src.services.research_engine.discovery import source_records
from src.services.research_engine.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderConfig,
)
from src.services.research_engine.run_lifecycle import ResearchRunLifecycleService
from src.services.research_engine.scope import (
    canonicalize_scope_confirmation,
    resolve_effective_daily_brief_parameters,
)
from src.services.research_engine.source_persistence import research_source_rows
from src.services.research_engine.step_executor import StepExecutor
from tests.integration.research_engine_postgres_support import (
    create_research_engine_tables,
    seed_approved_protocol_binding,
    seed_canonical_project_scope,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_postgres,
    pytest.mark.asyncio,
]


@dataclass(frozen=True)
class _PersistedLifecycle:
    stage_types: tuple[str, ...]
    duplicate_stage_rows: int
    stage_hashes: tuple[str, ...]
    stage_outputs: tuple[dict[str, Any], ...]
    gate_hashes: tuple[str, ...]
    review_ids: tuple[str, ...]
    reviewer_ids: tuple[str, ...]
    review_created_at: tuple[str, ...]
    review_hashes: tuple[str, ...]
    manifest: dict[str, Any]
    status: str
    stream_race_statuses: tuple[tuple[int, int], ...]
    stream_race_stage_counts: tuple[int, ...]
    stream_race_review_counts: tuple[int, ...]
    stream_race_provider_call_deltas: tuple[int, ...]


@dataclass(frozen=True)
class _ScenarioDatabase:
    factory: async_sessionmaker[AsyncSession]
    owner_id: uuid.UUID
    other_owner_id: uuid.UUID
    organization_id: uuid.UUID
    run_id: uuid.UUID


_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "services"
    / "research_engine"
    / "blueprints"
    / "templates"
    / "daily_research_brief.yaml"
)
_ABSTRACT = "Treatment reduced the measured score among 42 participants."


def _async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be a PostgreSQL URL")


@asynccontextmanager
async def _scenario_database(
    dsn: str,
    *,
    steps: list[dict[str, Any]] | None = None,
    parameters: dict[str, Any] | None = None,
    status: str = "running",
    manifest: dict[str, Any] | None = None,
) -> AsyncIterator[_ScenarioDatabase]:
    """Seed a minimal owner-scoped run in an isolated PostgreSQL schema."""

    schema = "daily_brief_scenario_" + uuid.uuid4().hex
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
                ResearchSource,
                ResearchStep,
                ResearchStageReview,
            )

        factory = async_sessionmaker(scoped_engine, expire_on_commit=False)
        owner_id = uuid.uuid4()
        other_owner_id = uuid.uuid4()
        organization_id = uuid.uuid4()
        project_id = uuid.uuid4()
        blueprint_id = uuid.uuid4()
        run_id = uuid.uuid4()
        async with scoped_engine.begin() as connection:
            canonical_scope = await seed_canonical_project_scope(
                connection,
                owner_id=owner_id,
                organization_id=organization_id,
                additional_user_organizations={other_owner_id: organization_id},
                reviewer_ids=(owner_id,),
            )
        async with factory() as db:
            db.add_all(
                [
                    ResearchProject(
                        id=project_id,
                        name="Daily Brief scenario",
                        owner_id=owner_id,
                        collection_id=canonical_scope.collection_id,
                    ),
                    ResearchBlueprint(
                        id=blueprint_id,
                        project_id=project_id,
                        name="Daily Research Brief scenario",
                        template_source="daily_research_brief",
                        version=1,
                        steps=steps or [],
                        parameters=parameters or {"contract_version": 1},
                        is_immutable=True,
                    ),
                    ResearchRun(
                        id=run_id,
                        blueprint_id=blueprint_id,
                        blueprint_version=1,
                        status=status,
                        reproducibility_manifest=manifest
                        or {"parameters_override": {}},
                    ),
                ]
            )
            await db.commit()
        yield _ScenarioDatabase(
            factory=factory,
            owner_id=owner_id,
            other_owner_id=other_owner_id,
            organization_id=organization_id,
            run_id=run_id,
        )
    finally:
        if scoped_engine is not None:
            await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
        await admin_engine.dispose()


def _app_with_scenario_database(
    scenario: _ScenarioDatabase, *, owner_id: uuid.UUID | None = None
) -> FastAPI:
    app = FastAPI()
    app.include_router(runs_router)
    app.include_router(reviews_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with scenario.factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=owner_id or scenario.owner_id,
        organization_id=scenario.organization_id,
    )
    return app


class _FixtureConnector(SourceConnector):
    def __init__(
        self,
        documents: list[SourceDocument] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.documents = list(documents or [])
        self.error = error

    async def search(
        self, query: str, max_results: int = 50, **kwargs: Any
    ) -> list[SourceDocument]:
        del query, kwargs
        if self.error is not None:
            raise self.error
        return self.documents[:max_results]


async def _configure_daily_brief(
    scenario: _ScenarioDatabase,
    *,
    providers: list[str],
    limit_per_provider: int = 2,
) -> dict[str, Any]:
    """Attach the immutable production template and a confirmed scope."""

    template = cast(
        dict[str, Any], yaml.safe_load(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    )
    parameters = {
        **template["parameters"],
        "research_question": "Does the controlled treatment reduce score?",
        "inclusion_criteria": ["Reports the measured score"],
        "exclusion_criteria": ["No outcome data"],
        "providers": providers,
        "limit_per_provider": limit_per_provider,
        "notes": "Controlled PostgreSQL certification fixture",
    }
    effective = resolve_effective_daily_brief_parameters(parameters, {})
    scope = canonicalize_scope_confirmation(
        effective=effective,
        submitted=DailyBriefScopeConfirmation.model_validate(
            {
                **{
                    key: effective[key]
                    for key in (
                        "research_question",
                        "inclusion_criteria",
                        "exclusion_criteria",
                        "providers",
                        "limit_per_provider",
                        "notes",
                    )
                },
                "confirmed": True,
            }
        ),
        actor_id=scenario.owner_id,
        confirmed_at=datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc),
    )
    async with scenario.factory() as db:
        run = await db.get(ResearchRun, scenario.run_id)
        assert run is not None
        blueprint = await db.get(ResearchBlueprint, run.blueprint_id)
        assert blueprint is not None
        project = await db.get(ResearchProject, blueprint.project_id)
        assert project is not None and project.collection_id is not None
        blueprint.steps = template["steps"]
        blueprint.parameters = parameters
        await db.flush()
        binding = await seed_approved_protocol_binding(
            db,
            blueprint_id=blueprint.id,
            collection_id=project.collection_id,
            author_id=scenario.owner_id,
            steps=template["steps"],
            parameters=parameters,
        )
        run.protocol_version_id = binding.protocol_version_id
        run.effective_plan_hash = binding.effective_plan_hash
        run.conformance_status = "plan_verified"
        run.reproducibility_manifest = {
            "parameters_override": {},
            "scope_confirmation": scope,
            "provider_manifest": [
                item for item in safe_capability_projection() if item["id"] in providers
            ],
        }
        await db.commit()
    return template


class _ControlledConnector(SourceConnector):
    """Return stable evidence without crossing the controlled-fixture boundary."""

    async def search(
        self, query: str, max_results: int = 50, **kwargs: Any
    ) -> list[SourceDocument]:
        del query, kwargs
        rows = [
            SourceDocument(
                connector_type="openalex",
                external_id="https://doi.org/10.1000/daily-brief-1",
                title="Controlled treatment study",
                authors=["A. Researcher"],
                abstract=_ABSTRACT,
                url="https://example.test/paper/1",
                metadata={"doi": "10.1000/daily-brief-1"},
            )
        ]
        return rows[:max_results]


class _ControlledProvider(LLMProvider):
    """Emit valid v1 stage payloads from the IDs in each production prompt."""

    def __init__(self, *, verification_status: str = "supported") -> None:
        super().__init__(
            ProviderConfig(provider_type="controlled", model_id="controlled-v1")
        )
        self.verification_status = verification_status
        self.requests: list[LLMRequest] = []

    async def is_model_available(self) -> bool:
        return True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        stage_match = re.search(
            r"stage_type=(screen|extract|synthesize|verify)",
            request.system_prompt or "",
        )
        stage_type = stage_match.group(1) if stage_match else "unknown"
        prompt = json.loads(request.prompt)
        records = prompt.get("records", [])

        if stage_type == "screen":
            payload: dict[str, Any] = {
                "screening": [
                    {
                        "source_id": record["source_id"],
                        "part_id": record["part_id"],
                        "included": True,
                        "reason": "Matches the confirmed scope",
                    }
                    for record in records
                ]
            }
        elif stage_type == "extract":
            payload = {
                "records": [
                    {
                        "source_id": record["source_id"],
                        "part_id": record["part_id"],
                        "data": {
                            "methodology": None,
                            "findings": "Treatment reduced the measured score",
                            "limitations": None,
                        },
                        "evidence": [
                            {
                                "pointer": "/findings",
                                "quote": "Treatment reduced the measured score",
                                "page_reference": None,
                            }
                        ],
                    }
                    for record in records
                ]
            }
        elif stage_type == "synthesize":
            evidence_ids = [
                item["evidence_id"]
                for record in records
                for item in json.loads(record["text"])
                if isinstance(item, dict) and item.get("evidence_id")
            ]
            payload = {
                "sections": [
                    {
                        "heading": "Findings",
                        "claims": [
                            {
                                "claim_text": "Treatment reduced the measured score",
                                "evidence": [
                                    {
                                        "evidence_id": evidence_id,
                                        "relation": "supports",
                                    }
                                    for evidence_id in evidence_ids
                                ],
                            }
                        ],
                    }
                ]
            }
        elif stage_type == "verify":
            payload = {
                "checks": [
                    {
                        "claim_id": json.loads(record["text"])["claim_id"],
                        "status": self.verification_status,
                        "reason": (
                            "The cited excerpt supports the claim"
                            if self.verification_status == "supported"
                            else "Controlled verifier rejected the claim"
                        ),
                    }
                    for record in records
                ]
            }
        else:  # pragma: no cover - the exact six-stage contract forbids this path
            raise AssertionError(f"Unexpected controlled stage: {stage_type}")

        return LLMResponse(
            content=json.dumps(payload),
            model_id="controlled-v1",
            model_version="fixture-2026-09-28",
            input_tokens=40,
            output_tokens=20,
        )


def _review_request(pending: dict[str, Any]) -> dict[str, Any]:
    descriptor = pending["descriptor"]
    stage_output = pending["stage_output"]
    review_kind = descriptor["review_kind"]
    if review_kind == "screening":
        decision_payload = {
            "items": [
                {
                    "source_id": item["source_id"],
                    "part_id": item["part_id"],
                    "decision": "include",
                }
                for item in stage_output["screening"]
            ]
        }
    elif review_kind == "extraction":
        decision_payload = {
            "items": [
                {
                    "source_id": item["source_id"],
                    "part_id": item["part_id"],
                    "decision": "accept",
                }
                for item in stage_output["extractions"]
            ]
        }
    else:
        decision_payload = {}
    return {
        "review_kind": review_kind,
        "output_hash": descriptor["output_hash"],
        "decision": "approve",
        "decision_payload": decision_payload,
        "note": f"Controlled {review_kind} approval",
    }


@asynccontextmanager
async def _daily_brief_lifecycle(
    dsn: str,
) -> AsyncIterator[_PersistedLifecycle]:
    """Run the controlled six-stage fixture against an isolated PostgreSQL schema."""

    schema = "daily_brief_" + uuid.uuid4().hex
    admin_engine = create_async_engine(_async_dsn(dsn))
    scoped_engine = None
    monkeypatch = pytest.MonkeyPatch()
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
                ResearchSource,
                ResearchStep,
                ResearchStageReview,
            )

        factory = async_sessionmaker(scoped_engine, expire_on_commit=False)
        owner_id = uuid.uuid4()
        other_owner_id = uuid.uuid4()
        organization_id = uuid.uuid4()
        project_id = uuid.uuid4()
        blueprint_id = uuid.uuid4()
        run_id = uuid.uuid4()
        async with scoped_engine.begin() as connection:
            canonical_scope = await seed_canonical_project_scope(
                connection,
                owner_id=owner_id,
                organization_id=organization_id,
                additional_user_organizations={other_owner_id: organization_id},
                reviewer_ids=(owner_id,),
            )

        template = cast(
            dict[str, Any], yaml.safe_load(_TEMPLATE_PATH.read_text(encoding="utf-8"))
        )
        parameters = {
            **template["parameters"],
            "research_question": "Does the controlled treatment reduce score?",
            "inclusion_criteria": ["Reports the measured score"],
            "exclusion_criteria": ["No outcome data"],
            "providers": ["openalex"],
            "limit_per_provider": 2,
            "notes": "Controlled PostgreSQL certification fixture",
        }
        effective = resolve_effective_daily_brief_parameters(parameters, {})
        scope = canonicalize_scope_confirmation(
            effective=effective,
            submitted=DailyBriefScopeConfirmation.model_validate(
                {
                    **{
                        key: effective[key]
                        for key in (
                            "research_question",
                            "inclusion_criteria",
                            "exclusion_criteria",
                            "providers",
                            "limit_per_provider",
                            "notes",
                        )
                    },
                    "confirmed": True,
                }
            ),
            actor_id=owner_id,
            confirmed_at=datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc),
        )
        async with factory() as db:
            db.add_all(
                [
                    ResearchProject(
                        id=project_id,
                        name="Daily Brief PostgreSQL certification",
                        owner_id=owner_id,
                        collection_id=canonical_scope.collection_id,
                    ),
                    ResearchBlueprint(
                        id=blueprint_id,
                        project_id=project_id,
                        name="Daily Research Brief",
                        template_source="daily_research_brief",
                        version=1,
                        steps=template["steps"],
                        parameters=parameters,
                        is_immutable=True,
                    ),
                ]
            )
            await db.flush()
            binding = await seed_approved_protocol_binding(
                db,
                blueprint_id=blueprint_id,
                collection_id=canonical_scope.collection_id,
                author_id=owner_id,
                steps=template["steps"],
                parameters=parameters,
            )
            db.add(
                ResearchRun(
                    id=run_id,
                    blueprint_id=blueprint_id,
                    blueprint_version=1,
                    protocol_version_id=binding.protocol_version_id,
                    effective_plan_hash=binding.effective_plan_hash,
                    conformance_status="plan_verified",
                    status="pending",
                    reproducibility_manifest={
                        "parameters_override": {},
                        "scope_confirmation": scope,
                        "provider_manifest": [
                            item
                            for item in safe_capability_projection()
                            if item["id"] == "openalex"
                        ],
                    },
                )
            )
            await db.commit()

        app = FastAPI()
        app.include_router(runs_router)
        app.include_router(reviews_router)

        async def override_db() -> AsyncIterator[AsyncSession]:
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=owner_id,
            organization_id=organization_id,
        )
        intruder_app = FastAPI()
        intruder_app.include_router(runs_router)
        intruder_app.include_router(reviews_router)
        intruder_app.dependency_overrides[get_db] = override_db
        intruder_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=other_owner_id,
            # Same-organization membership must not grant project access.
            organization_id=organization_id,
        )
        provider = _ControlledProvider()
        connector = _ControlledConnector()
        monkeypatch.setattr(
            runs_module,
            "_build_providers",
            lambda _steps: {"claude-sonnet-4-6": provider},
        )
        monkeypatch.setattr(
            runs_module,
            "_build_connectors",
            lambda **_kwargs: {"openalex": connector},
        )
        race_admission_enabled = False
        race_admitted_count = 0
        race_first_admitted = asyncio.Event()
        race_second_admitted = asyncio.Event()
        race_admission_release = asyncio.Event()

        async def admitted(**_kwargs: Any) -> bool:
            nonlocal race_admitted_count
            if race_admission_enabled:
                race_admitted_count += 1
                race_first_admitted.set()
                if race_admitted_count > 1:
                    race_second_admitted.set()
                await race_admission_release.wait()
            return True

        monkeypatch.setattr(runs_module, "admit_expensive_work", admitted)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            gate_hashes: list[str] = []
            stream_race_statuses: list[tuple[int, int]] = []
            stream_race_stage_counts: list[int] = []
            stream_race_review_counts: list[int] = []
            stream_race_provider_call_deltas: list[int] = []
            for expected_kind in ("screening", "extraction", "final"):
                if expected_kind == "screening":
                    streamed = await client.get(
                        f"/research-engine/runs/{run_id}/stream"
                    )
                    assert streamed.status_code == 200, streamed.text
                    assert "event: run_paused" in streamed.text

                # A new transport proves the gate is reconstructed from durable
                # PostgreSQL state rather than the preceding SSE connection. For
                # later gates, that preceding connection is the winning member of
                # the two-consumer stream race below.
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://reload"
                ) as reloaded_client:
                    pending_response = await reloaded_client.get(
                        f"/research-engine/runs/{run_id}/reviews/pending"
                    )
                assert pending_response.status_code == 200
                pending = cast(dict[str, Any], pending_response.json())
                assert pending["pending"] is True
                assert pending["descriptor"]["review_kind"] == expected_kind
                gate_hashes.append(pending["descriptor"]["output_hash"])

                stale_request = _review_request(pending)
                stale_request["output_hash"] = "0" * 64
                stale = await client.post(
                    (
                        f"/research-engine/runs/{run_id}/reviews/"
                        f"{pending['descriptor']['step_index']}"
                    ),
                    json=stale_request,
                )
                assert stale.status_code == 409
                assert stale.json()["detail"]["code"] == "review_output_stale"

                async with AsyncClient(
                    transport=ASGITransport(app=intruder_app),
                    base_url="http://same-org-intruder",
                ) as intruder:
                    denied_run = await intruder.get(f"/research-engine/runs/{run_id}")
                    denied_review = await intruder.get(
                        f"/research-engine/runs/{run_id}/reviews/pending"
                    )
                assert denied_run.status_code == 404
                assert denied_review.status_code == 404

                review_request = _review_request(pending)
                reviewed = await client.post(
                    (
                        f"/research-engine/runs/{run_id}/reviews/"
                        f"{pending['descriptor']['step_index']}"
                    ),
                    json=review_request,
                )
                assert reviewed.status_code == 200, reviewed.text

                replayed = await client.post(
                    (
                        f"/research-engine/runs/{run_id}/reviews/"
                        f"{pending['descriptor']['step_index']}"
                    ),
                    json=review_request,
                )
                assert replayed.status_code == 200, replayed.text
                assert replayed.json()["id"] == reviewed.json()["id"]
                assert replayed.json()["replay"] is True

                conflicting_request = {
                    **review_request,
                    "note": f"Conflicting {expected_kind} decision",
                }
                conflicting = await client.post(
                    (
                        f"/research-engine/runs/{run_id}/reviews/"
                        f"{pending['descriptor']['step_index']}"
                    ),
                    json=conflicting_request,
                )
                assert conflicting.status_code == 409
                assert (
                    conflicting.json()["detail"]["code"] == "review_decision_conflict"
                )

                if expected_kind == "final":
                    resumed = await client.post(
                        f"/research-engine/runs/{run_id}/resume", json={}
                    )
                    assert resumed.status_code == 200, resumed.text
                else:

                    async def resume_once() -> Any:
                        async with AsyncClient(
                            transport=ASGITransport(app=app),
                            base_url="http://simultaneous-resume",
                        ) as resume_client:
                            return await resume_client.post(
                                f"/research-engine/runs/{run_id}/resume", json={}
                            )

                    simultaneous = await asyncio.gather(resume_once(), resume_once())
                    assert [response.status_code for response in simultaneous] == [
                        200,
                        200,
                    ]

                    async def stream_once(label: str) -> Any:
                        async with AsyncClient(
                            transport=ASGITransport(
                                app=app, raise_app_exceptions=False
                            ),
                            base_url=f"http://stream-claim-{label}",
                        ) as stream_client:
                            return await stream_client.get(
                                f"/research-engine/runs/{run_id}/stream"
                            )

                    # Hold the winning consumer after its durable claim while the
                    # competing request resolves through the canonical project
                    # lock. Removing claim_stream's run guard still lets both
                    # requests reach admission and is caught by this assertion.
                    race_admission_enabled = True
                    race_admitted_count = 0
                    race_first_admitted = asyncio.Event()
                    race_second_admitted = asyncio.Event()
                    race_admission_release = asyncio.Event()
                    provider_calls_before = len(provider.requests)
                    stream_tasks = (
                        asyncio.create_task(stream_once("first")),
                        asyncio.create_task(stream_once("second")),
                    )
                    await asyncio.wait_for(race_first_admitted.wait(), timeout=5)
                    second_admission_waiter = asyncio.create_task(
                        race_second_admitted.wait()
                    )
                    try:
                        finished, _ = await asyncio.wait(
                            {*stream_tasks, second_admission_waiter},
                            timeout=5,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        assert finished, "second stream neither lost nor double-claimed"
                    finally:
                        race_admission_release.set()
                        race_admission_enabled = False
                    raced = await asyncio.gather(*stream_tasks)
                    if not second_admission_waiter.done():
                        second_admission_waiter.cancel()
                        await asyncio.gather(
                            second_admission_waiter, return_exceptions=True
                        )

                    status_values = sorted(response.status_code for response in raced)
                    assert len(status_values) == 2
                    statuses = (status_values[0], status_values[1])
                    assert statuses == (200, 409)
                    successful = next(
                        response for response in raced if response.status_code == 200
                    )
                    rejected = next(
                        response for response in raced if response.status_code == 409
                    )
                    assert "event: run_paused" in successful.text
                    assert rejected.json()["detail"] == (
                        "Run was just claimed by another stream"
                    )

                    expected_next_kind = (
                        "extraction" if expected_kind == "screening" else "final"
                    )
                    expected_stage_count = 3 if expected_kind == "screening" else 6
                    expected_review_count = 1 if expected_kind == "screening" else 2
                    expected_provider_calls = 1 if expected_kind == "screening" else 2
                    async with factory() as race_db:
                        stage_count = int(
                            await race_db.scalar(
                                select(func.count())
                                .select_from(ResearchStep)
                                .where(ResearchStep.run_id == run_id)
                            )
                            or 0
                        )
                        review_count = int(
                            await race_db.scalar(
                                select(func.count())
                                .select_from(ResearchStageReview)
                                .where(ResearchStageReview.run_id == run_id)
                            )
                            or 0
                        )
                        durable_run = await race_db.get(ResearchRun, run_id)
                    assert durable_run is not None
                    durable_manifest = cast(
                        dict[str, Any], durable_run.reproducibility_manifest
                    )
                    assert durable_run.status == "paused"
                    assert durable_manifest["pending_review"]["review_kind"] == (
                        expected_next_kind
                    )
                    assert "resume_authorization" not in durable_manifest
                    assert stage_count == expected_stage_count
                    assert review_count == expected_review_count
                    provider_call_delta = len(provider.requests) - provider_calls_before
                    assert provider_call_delta == expected_provider_calls
                    stream_race_statuses.append(statuses)
                    stream_race_stage_counts.append(stage_count)
                    stream_race_review_counts.append(review_count)
                    stream_race_provider_call_deltas.append(provider_call_delta)

        async with factory() as db:
            persisted_steps = list(
                (
                    await db.execute(
                        select(ResearchStep)
                        .where(ResearchStep.run_id == run_id)
                        .order_by(ResearchStep.step_index)
                    )
                )
                .scalars()
                .all()
            )
            stages = tuple(step.step_type for step in persisted_steps)
            duplicate_rows = int(
                await db.scalar(
                    select(func.count()).select_from(
                        select(
                            ResearchStep.run_id,
                            ResearchStep.step_index,
                            func.count().label("rows"),
                        )
                        .where(ResearchStep.run_id == run_id)
                        .group_by(ResearchStep.run_id, ResearchStep.step_index)
                        .having(func.count() > 1)
                        .subquery()
                    )
                )
                or 0
            )
            reviews = list(
                (
                    await db.execute(
                        select(ResearchStageReview)
                        .where(ResearchStageReview.run_id == run_id)
                        .order_by(ResearchStageReview.step_index)
                    )
                )
                .scalars()
                .all()
            )
            run = await db.get(ResearchRun, run_id)
            assert run is not None
            yield _PersistedLifecycle(
                stage_types=stages,
                duplicate_stage_rows=duplicate_rows,
                stage_hashes=tuple(str(step.outputs_hash) for step in persisted_steps),
                stage_outputs=tuple(
                    cast(dict[str, Any], step.output) for step in persisted_steps
                ),
                gate_hashes=tuple(gate_hashes),
                review_ids=tuple(str(review.id) for review in reviews),
                reviewer_ids=tuple(str(review.reviewer_id) for review in reviews),
                review_created_at=tuple(
                    review.created_at.isoformat() for review in reviews
                ),
                review_hashes=tuple(review.output_hash for review in reviews),
                manifest=cast(dict[str, Any], run.reproducibility_manifest),
                status=run.status,
                stream_race_statuses=tuple(stream_race_statuses),
                stream_race_stage_counts=tuple(stream_race_stage_counts),
                stream_race_review_counts=tuple(stream_race_review_counts),
                stream_race_provider_call_deltas=tuple(
                    stream_race_provider_call_deltas
                ),
            )
    finally:
        monkeypatch.undo()
        if scoped_engine is not None:
            await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
        await admin_engine.dispose()


async def test_controlled_lifecycle_persists_each_stage_exactly_once() -> None:
    """A completed controlled run has six durable, non-duplicated stage rows."""

    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    assert dsn is not None

    async with _daily_brief_lifecycle(dsn) as persisted:
        assert persisted.stage_types == (
            "search",
            "screen",
            "extract",
            "synthesize",
            "verify",
            "export",
        )
        assert persisted.duplicate_stage_rows == 0
        assert persisted.status == "completed"
        assert persisted.manifest["final_status"] == "verified"
        assert persisted.stage_hashes == tuple(
            canonical_stage_output_hash(output) for output in persisted.stage_outputs
        )
        assert persisted.gate_hashes == (
            persisted.stage_hashes[1],
            persisted.stage_hashes[2],
            persisted.stage_hashes[5],
        )
        assert persisted.review_hashes == persisted.gate_hashes
        assert len(set(persisted.review_ids)) == 3
        assert persisted.stream_race_statuses == ((200, 409), (200, 409))
        assert persisted.stream_race_stage_counts == (3, 6)
        assert persisted.stream_race_review_counts == (1, 2)
        assert persisted.stream_race_provider_call_deltas == (1, 2)
        assert [
            item["review_id"] for item in persisted.manifest["review_history"]
        ] == list(persisted.review_ids)

        extraction = persisted.stage_outputs[2]["extractions"][0]
        synthesis = persisted.stage_outputs[3]["synthesis"]
        verification = persisted.stage_outputs[4]["verification"]
        exported = persisted.stage_outputs[5]
        evidence_id = extraction["evidence"][0]["evidence_id"]
        assert extraction["evidence"][0]["quote"] == (
            "Treatment reduced the measured score"
        )
        assert synthesis["claims"][0]["evidence"] == [
            {"evidence_id": evidence_id, "relation": "supports"}
        ]
        assert verification["claims"][0]["evidence_ids"] == [evidence_id]
        assert exported["verification_output_hash"] == persisted.stage_hashes[4]
        assert exported["report_hash"] == canonical_json_sha256(exported["exported"])
        attestation = persisted.manifest["final_approval_attestation"]
        assert attestation["review_id"] == persisted.review_ids[-1]
        assert attestation["reviewer_id"] == persisted.reviewer_ids[-1]
        assert attestation["reviewed_at"] == persisted.review_created_at[-1]
        assert attestation["output_hash"] == persisted.stage_hashes[5]
        assert attestation["report_hash"] == exported["report_hash"]
        assert (
            attestation["verification_output_hash"]
            == exported["verification_output_hash"]
        )
        unsigned_attestation = {
            key: value
            for key, value in attestation.items()
            if key != "attestation_hash"
        }
        assert attestation["attestation_hash"] == canonical_json_sha256(
            unsigned_attestation
        )
        assert exported["exported"]["claims"][0]["evidence_ids"] == [evidence_id]
        assert "Treatment reduced the measured score" in exported["markdown"]


async def test_partial_provider_failure_deduplicates_and_persists_metadata_only() -> (
    None
):
    """One failed provider cannot erase successful, conservatively merged evidence."""

    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    assert dsn is not None

    shared_doi = "10.1000/shared-daily-brief"
    openalex_documents = [
        SourceDocument(
            connector_type="openalex",
            external_id="W-shared",
            title="Shared controlled study",
            authors=["A. Researcher"],
            abstract=None,
            url="https://example.test/openalex/shared",
            metadata={"doi": shared_doi},
        ),
        SourceDocument(
            connector_type="openalex",
            external_id="W-metadata-only",
            title="Metadata-only controlled study",
            authors=["M. Metadata"],
            abstract=None,
            url="https://example.test/openalex/metadata-only",
            metadata={"doi": "10.1000/metadata-only"},
        ),
    ]
    crossref_documents = [
        SourceDocument(
            connector_type="crossref",
            external_id=shared_doi,
            title="Shared controlled study",
            authors=["A. Researcher"],
            abstract=_ABSTRACT,
            url="https://example.test/crossref/shared",
            metadata={"doi": shared_doi},
        )
    ]
    step = {
        "type": "search",
        "parameters": {
            "contract_version": 1,
            "sources": ["openalex", "crossref", "pubmed"],
            "query_template": "{research_question}",
            "max_results_per_source": 50,
        },
    }
    async with _scenario_database(dsn, steps=[step]) as scenario:
        result = await StepExecutor(
            providers={},
            connectors={
                "openalex": _FixtureConnector(openalex_documents),
                "crossref": _FixtureConnector(crossref_documents),
                "pubmed": _FixtureConnector(error=TimeoutError("controlled")),
            },
        ).execute(
            step,
            {
                "contract_version": 1,
                "research_question": "controlled treatment",
            },
        )
        output = result.output
        coverage = output["coverage"]
        assert coverage["partial"] is True
        assert coverage["providers"]["pubmed"] == {
            "status": "failed",
            "error_type": "TimeoutError",
        }
        assert coverage["deduplication"] == {"before": 3, "after": 2}
        assert sorted(
            record["evidence_level"] for record in output["source_records"]
        ) == ["abstract", "metadata_only"]
        shared_record = next(
            record
            for record in output["source_records"]
            if record["title"] == "Shared controlled study"
        )
        assert shared_record["abstract"] == _ABSTRACT
        assert len(shared_record["metadata"]["provenance"]) == 2

        async with scenario.factory() as db:
            run = await db.get(ResearchRun, scenario.run_id)
            assert run is not None
            transition = await ResearchRunLifecycleService(db).persist_step_completion(
                run=run,
                event={
                    "step_index": 0,
                    "step_type": "search",
                    "output": output,
                    "outputs_hash": canonical_stage_output_hash(output),
                    "token_count": 0,
                },
                step_definition=step,
                source_rows=research_source_rows(scenario.run_id, output),
            )
            assert transition.persisted is True

        async with scenario.factory() as db:
            persisted_step = await db.scalar(
                select(ResearchStep).where(ResearchStep.run_id == scenario.run_id)
            )
            persisted_sources = list(
                (
                    await db.execute(
                        select(ResearchSource)
                        .where(ResearchSource.run_id == scenario.run_id)
                        .order_by(ResearchSource.title)
                    )
                )
                .scalars()
                .all()
            )
            assert persisted_step is not None
            assert persisted_step.outputs_hash == canonical_stage_output_hash(output)
            assert len(persisted_sources) == 2
            assert {str(item.id) for item in persisted_sources} == {
                record["source_id"] for record in output["source_records"]
            }
            assert {
                item.title: item.metadata_["evidence_level"]
                for item in persisted_sources
            } == {
                "Metadata-only controlled study": "metadata_only",
                "Shared controlled study": "abstract",
            }


@pytest.mark.parametrize("case", ["all_provider_failure", "no_evidence"])
async def test_terminal_search_scenarios_are_durable(case: str) -> None:
    """Search failure and empty evidence produce distinct durable terminal states."""

    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    assert dsn is not None

    providers = (
        ["openalex", "crossref"] if case == "all_provider_failure" else ["openalex"]
    )
    async with _scenario_database(dsn, status="pending") as scenario:
        await _configure_daily_brief(scenario, providers=providers)
        app = _app_with_scenario_database(scenario)
        monkeypatch = pytest.MonkeyPatch()
        try:
            connectors = {
                name: _FixtureConnector(
                    error=(
                        ConnectionError("controlled")
                        if case == "all_provider_failure"
                        else None
                    )
                )
                for name in providers
            }
            monkeypatch.setattr(
                runs_module, "_build_connectors", lambda **_kwargs: connectors
            )
            monkeypatch.setattr(
                runs_module,
                "_build_providers",
                lambda _steps: {"claude-sonnet-4-6": _ControlledProvider()},
            )

            async def admitted(**_kwargs: Any) -> bool:
                return True

            monkeypatch.setattr(runs_module, "admit_expensive_work", admitted)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                streamed = await client.get(
                    f"/research-engine/runs/{scenario.run_id}/stream"
                )
            assert streamed.status_code == 200, streamed.text
        finally:
            monkeypatch.undo()

        async with scenario.factory() as db:
            run = await db.get(ResearchRun, scenario.run_id)
            assert run is not None
            persisted_steps = list(
                (
                    await db.execute(
                        select(ResearchStep)
                        .where(ResearchStep.run_id == scenario.run_id)
                        .order_by(ResearchStep.step_index)
                    )
                )
                .scalars()
                .all()
            )
            source_count = int(
                await db.scalar(
                    select(func.count())
                    .select_from(ResearchSource)
                    .where(ResearchSource.run_id == scenario.run_id)
                )
                or 0
            )
            review_count = int(
                await db.scalar(
                    select(func.count())
                    .select_from(ResearchStageReview)
                    .where(ResearchStageReview.run_id == scenario.run_id)
                )
                or 0
            )
            assert source_count == 0
            assert review_count == 0
            if case == "all_provider_failure":
                assert "event: run_failed" in streamed.text
                assert run.status == "failed"
                assert persisted_steps == []
            else:
                assert '"final_status": "no_evidence"' in streamed.text
                assert run.status == "completed"
                assert run.reproducibility_manifest["final_status"] == "no_evidence"
                assert [step.step_type for step in persisted_steps] == [
                    "search",
                    "screen",
                ]
                assert persisted_steps[-1].output["screening"] == []
                assert persisted_steps[-1].outputs_hash == canonical_stage_output_hash(
                    cast(dict[str, Any], persisted_steps[-1].output)
                )


async def test_failed_verification_requires_hash_bound_override() -> None:
    """An explicit override resumes the same failed output and marks its export."""

    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    assert dsn is not None

    async with _scenario_database(dsn, status="pending") as scenario:
        await _configure_daily_brief(scenario, providers=["openalex"])
        app = _app_with_scenario_database(scenario)
        monkeypatch = pytest.MonkeyPatch()
        try:
            provider = _ControlledProvider(verification_status="contradicted")
            monkeypatch.setattr(
                runs_module,
                "_build_connectors",
                lambda **_kwargs: {"openalex": _ControlledConnector()},
            )
            monkeypatch.setattr(
                runs_module,
                "_build_providers",
                lambda _steps: {"claude-sonnet-4-6": provider},
            )

            async def admitted(**_kwargs: Any) -> bool:
                return True

            monkeypatch.setattr(runs_module, "admit_expensive_work", admitted)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                for review_kind in ("screening", "extraction"):
                    streamed = await client.get(
                        f"/research-engine/runs/{scenario.run_id}/stream"
                    )
                    assert streamed.status_code == 200, streamed.text
                    pending_response = await client.get(
                        f"/research-engine/runs/{scenario.run_id}/reviews/pending"
                    )
                    pending = cast(dict[str, Any], pending_response.json())
                    assert pending["descriptor"]["review_kind"] == review_kind
                    reviewed = await client.post(
                        (
                            f"/research-engine/runs/{scenario.run_id}/reviews/"
                            f"{pending['descriptor']['step_index']}"
                        ),
                        json=_review_request(pending),
                    )
                    assert reviewed.status_code == 200, reviewed.text
                    resumed = await client.post(
                        f"/research-engine/runs/{scenario.run_id}/resume", json={}
                    )
                    assert resumed.status_code == 200, resumed.text

                failed_stream = await client.get(
                    f"/research-engine/runs/{scenario.run_id}/stream"
                )
                assert failed_stream.status_code == 200, failed_stream.text
                assert '"pause_reason": "verification_failed"' in failed_stream.text

                missing_consent = await client.post(
                    f"/research-engine/runs/{scenario.run_id}/resume", json={}
                )
                assert missing_consent.status_code == 409
                assert (
                    missing_consent.json()["detail"]["code"]
                    == "verification_override_required"
                )
                descriptor = missing_consent.json()["detail"]["descriptor"]
                verification_hash = descriptor["output_hash"]

                stale = await client.post(
                    f"/research-engine/runs/{scenario.run_id}/resume",
                    json={
                        "continue_unverified": True,
                        "output_hash": "0" * 64,
                    },
                )
                assert stale.status_code == 409
                assert stale.json()["detail"]["code"] == "verification_output_stale"

                overridden = await client.post(
                    f"/research-engine/runs/{scenario.run_id}/resume",
                    json={
                        "continue_unverified": True,
                        "output_hash": verification_hash,
                    },
                )
                assert overridden.status_code == 200, overridden.text
                completed = await client.get(
                    f"/research-engine/runs/{scenario.run_id}/stream"
                )
                assert completed.status_code == 200, completed.text
                assert "event: run_complete" in completed.text
        finally:
            monkeypatch.undo()

        async with scenario.factory() as db:
            run = await db.get(ResearchRun, scenario.run_id)
            assert run is not None
            steps = list(
                (
                    await db.execute(
                        select(ResearchStep)
                        .where(ResearchStep.run_id == scenario.run_id)
                        .order_by(ResearchStep.step_index)
                    )
                )
                .scalars()
                .all()
            )
            reviews = list(
                (
                    await db.execute(
                        select(ResearchStageReview)
                        .where(ResearchStageReview.run_id == scenario.run_id)
                        .order_by(ResearchStageReview.step_index)
                    )
                )
                .scalars()
                .all()
            )
            assert run.status == "completed"
            assert run.reproducibility_manifest["final_status"] == "unverified"
            override = run.reproducibility_manifest["verification_override"]
            assert override["actor_id"] == str(scenario.owner_id)
            assert override["output_hash"] == verification_hash
            assert override["step_index"] == 4
            assert [step.step_type for step in steps] == [
                "search",
                "screen",
                "extract",
                "synthesize",
                "verify",
                "export",
            ]
            assert all(
                step.outputs_hash
                == canonical_stage_output_hash(cast(dict[str, Any], step.output))
                for step in steps
            )
            assert len(reviews) == 2
            assert [review.review_kind for review in reviews] == [
                "screening",
                "extraction",
            ]
            exported = cast(dict[str, Any], steps[-1].output)
            assert exported["verification_output_hash"] == verification_hash
            assert exported["report_hash"] == canonical_json_sha256(
                exported["exported"]
            )
            assert exported["exported"]["verification"]["passed"] is False
            assert exported["exported"]["continued_after_failure"] is True
            assert "continued after a failed quality check" in exported["markdown"]


# The Playwright fixture below intentionally lives beside the PostgreSQL
# certification harness.  It mounts the production routers and replaces only
# the two external seams (scholarly providers and the configured LLM) plus the
# identity provider.  Browser requests still cross Next's rewrite, FastAPI,
# SQLAlchemy, and a real PostgreSQL schema.


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _e2e_access_token(user_id: uuid.UUID, email: str) -> str:
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {
                "sub": str(user_id),
                "aud": "authenticated",
                "role": "authenticated",
                "email": email,
                "iat": now,
                "exp": now + 3600,
                "app_metadata": {"provider": "email", "providers": ["email"]},
                "user_metadata": {"first_name": "Controlled"},
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(
        hmac.new(b"daily-brief-e2e-secret", signing_input, hashlib.sha256).digest()
    )
    return f"{header}.{payload}.{signature}"


def _unverified_jwt_subject(request: Request) -> uuid.UUID | None:
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1]
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        return uuid.UUID(str(payload["sub"]))
    except (ValueError, KeyError, IndexError, json.JSONDecodeError):
        return None


def _supabase_user(user_id: uuid.UUID, email: str) -> dict[str, Any]:
    timestamp = "2026-09-28T00:00:00Z"
    return {
        "id": str(user_id),
        "aud": "authenticated",
        "role": "authenticated",
        "email": email,
        "email_confirmed_at": timestamp,
        "confirmed_at": timestamp,
        "last_sign_in_at": timestamp,
        "phone": "",
        "app_metadata": {"provider": "email", "providers": ["email"]},
        "user_metadata": {"first_name": "Controlled", "last_name": "Reviewer"},
        "identities": [],
        "created_at": timestamp,
        "updated_at": timestamp,
        "is_anonymous": False,
    }


class _E2EConnector(SourceConnector):
    def __init__(self, provider_id: str, fixture_state: dict[str, Any]) -> None:
        self.provider_id = provider_id
        self.fixture_state = fixture_state

    async def search(
        self, query: str, max_results: int = 50, **kwargs: Any
    ) -> list[SourceDocument]:
        del kwargs
        self.fixture_state["last_question"] = query
        if "[manual-pause]" in query:
            # Hold the first paid stage at a deterministic boundary until the
            # browser has issued the real pause request. The shared event lets
            # every selected connector resume together without a timing race.
            release = self.fixture_state["manual_pause_release"]
            await asyncio.wait_for(release.wait(), timeout=30.0)
        if "[no-evidence]" in query:
            return []
        if "[partial]" in query and self.provider_id == "crossref":
            raise TimeoutError("controlled crossref timeout")
        if "[max-bound]" in query:
            return [
                SourceDocument(
                    connector_type=self.provider_id,
                    external_id=(
                        f"https://doi.org/10.1000/daily-brief-max-"
                        f"{self.provider_id}-{index}"
                    ),
                    title=f"Controlled {self.provider_id} study {index}",
                    authors=["A. Researcher"],
                    abstract=(
                        "Treatment reduced the measured score in a controlled "
                        f"cohort {index}."
                    ),
                    url=f"https://example.test/{self.provider_id}/{index}",
                    metadata={
                        "doi": (f"10.1000/daily-brief-max-{self.provider_id}-{index}")
                    },
                )
                for index in range(50)
            ][:max_results]
        documents = [
            SourceDocument(
                connector_type=self.provider_id,
                external_id="https://doi.org/10.1000/daily-brief-e2e-1",
                title="Controlled treatment study",
                authors=["A. Researcher"],
                abstract=_ABSTRACT,
                url="https://example.test/paper/1",
                metadata={"doi": "10.1000/daily-brief-e2e-1"},
            ),
            SourceDocument(
                connector_type=self.provider_id,
                external_id="https://doi.org/10.1000/daily-brief-e2e-2",
                title="Controlled secondary study",
                authors=["B. Reviewer"],
                abstract="Treatment reduced the measured score in a second cohort.",
                url="https://example.test/paper/2",
                metadata={
                    "doi": "10.1000/daily-brief-e2e-2",
                    "publication_year": 2025,
                },
            ),
        ]
        return documents[:max_results]


class _E2EProvider(_ControlledProvider):
    def __init__(self, fixture_state: dict[str, Any]) -> None:
        super().__init__()
        self.fixture_state = fixture_state

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if "stage_type=verify" in (request.system_prompt or ""):
            self.verification_status = (
                "contradicted"
                if "[unverified]" in str(self.fixture_state.get("last_question", ""))
                else "supported"
            )
        return await super().complete(request)


def create_e2e_app() -> FastAPI:
    """Create the real browser/API/PostgreSQL Daily Brief fixture app."""

    dsn = os.environ.get("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        raise RuntimeError("ORCHESTRATION_TEST_DATABASE_URL is required")

    owner_id = uuid.uuid4()
    same_org_intruder_id = uuid.uuid4()
    intruder_id = uuid.uuid4()
    owner_org_id = uuid.uuid4()
    intruder_org_id = uuid.uuid4()
    fixture_state: dict[str, Any] = {
        "owner_id": owner_id,
        "same_org_intruder_id": same_org_intruder_id,
        "intruder_id": intruder_id,
        "owner_org_id": owner_org_id,
        "intruder_org_id": intruder_org_id,
        "last_question": "",
        "reconnect_failures": set(),
        "manual_pause_release": asyncio.Event(),
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        schema = "daily_brief_browser_" + uuid.uuid4().hex
        admin_engine = create_async_engine(_async_dsn(dsn))
        scoped_engine = None
        originals = (
            runs_module._build_connectors,
            runs_module._build_providers,
            runs_module.admit_expensive_work,
        )
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
                    ResearchSource,
                    ResearchStep,
                    ResearchStageReview,
                )
                canonical_scope = await seed_canonical_project_scope(
                    connection,
                    owner_id=owner_id,
                    organization_id=owner_org_id,
                    additional_user_organizations={
                        same_org_intruder_id: owner_org_id,
                        intruder_id: intruder_org_id,
                    },
                    additional_organization_ids=(intruder_org_id,),
                    reviewer_ids=(owner_id,),
                )
                fixture_state["collection_id"] = canonical_scope.collection_id
            app.state.db_factory = async_sessionmaker(
                scoped_engine, expire_on_commit=False
            )

            async def admitted(**_kwargs: Any) -> bool:
                return True

            runs_module._build_connectors = lambda **_kwargs: {
                provider_id: _E2EConnector(provider_id, fixture_state)
                for provider_id in (
                    "arxiv",
                    "crossref",
                    "openalex",
                    "pubmed",
                    "semantic_scholar",
                )
            }
            runs_module._build_providers = lambda steps: {
                "claude-sonnet-4-6": _E2EProvider(fixture_state)
            }
            runs_module.admit_expensive_work = admitted
            yield
        finally:
            (
                runs_module._build_connectors,
                runs_module._build_providers,
                runs_module.admit_expensive_work,
            ) = originals
            if scoped_engine is not None:
                await scoped_engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.exec_driver_sql(
                    f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
                )
            await admin_engine.dispose()

    app = FastAPI(lifespan=lifespan)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with app.state.db_factory() as db:
            yield db

    async def override_current_user(request: Request) -> SimpleNamespace:
        subject = _unverified_jwt_subject(request)
        if subject not in {owner_id, same_org_intruder_id, intruder_id}:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=401, detail="Could not validate credentials"
            )
        organization_id = (
            owner_org_id
            if subject in {owner_id, same_org_intruder_id}
            else intruder_org_id
        )
        return SimpleNamespace(id=subject, organization_id=organization_id)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_current_user

    @app.middleware("http")
    async def controlled_reconnect(request: Request, call_next: Any) -> Any:
        match = re.fullmatch(
            r"/api/v1/research-engine/runs/([0-9a-f-]+)/stream",
            request.url.path,
        )
        if match:
            run_id = uuid.UUID(match.group(1))
            async with app.state.db_factory() as db:
                run_projection = (
                    await db.execute(
                        select(
                            ResearchBlueprint.parameters,
                            ResearchRun.status,
                            ResearchRun.reproducibility_manifest,
                        )
                        .join(
                            ResearchRun,
                            ResearchRun.blueprint_id == ResearchBlueprint.id,
                        )
                        .where(ResearchRun.id == run_id)
                    )
                ).one_or_none()
            failed = cast(set[uuid.UUID], fixture_state["reconnect_failures"])
            question = run_projection[0] if run_projection is not None else None
            run_status = run_projection[1] if run_projection is not None else None
            manifest = run_projection[2] if run_projection is not None else None
            authorization = (
                manifest.get("resume_authorization")
                if isinstance(manifest, dict)
                else None
            )
            if (
                isinstance(question, dict)
                and "[reconnect-after-resume]"
                in str(question.get("research_question", ""))
                and run_status == RunStatus.PAUSED.value
                and isinstance(authorization, dict)
                and not authorization.get("consumed_at")
                and run_id not in failed
            ):
                failed.add(run_id)
                return JSONResponse(
                    status_code=503,
                    content={"detail": "controlled one-time disconnect"},
                )
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    def identity_for_email(email: str) -> tuple[uuid.UUID, str]:
        if email == "same-org-intruder@example.test":
            return same_org_intruder_id, email
        if email == "intruder@example.test":
            return intruder_id, email
        return owner_id, "owner@example.test"

    @app.post("/api/v1/auth-fixture/auth/v1/token")
    async def token(request: Request) -> dict[str, Any]:
        body = await request.json()
        email = str(body.get("email") or "owner@example.test")
        user_id, email = identity_for_email(email)
        access_token = _e2e_access_token(user_id, email)
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": 3600,
            "expires_at": int(time.time()) + 3600,
            "refresh_token": f"refresh-{user_id}",
            "user": _supabase_user(user_id, email),
        }

    @app.get("/api/v1/auth-fixture/auth/v1/user")
    async def auth_user(request: Request) -> JSONResponse:
        subject = _unverified_jwt_subject(request)
        if subject not in {owner_id, same_org_intruder_id, intruder_id}:
            return JSONResponse(
                status_code=401, content={"message": "Auth session missing"}
            )
        email = (
            "intruder@example.test"
            if subject == intruder_id
            else (
                "same-org-intruder@example.test"
                if subject == same_org_intruder_id
                else "owner@example.test"
            )
        )
        return JSONResponse(content=_supabase_user(subject, email))

    @app.post("/api/v1/auth-fixture/auth/v1/logout")
    async def logout() -> JSONResponse:
        return JSONResponse(status_code=204, content=None)

    @app.get("/api/v1/auth/me")
    async def profile(request: Request) -> JSONResponse:
        subject = _unverified_jwt_subject(request)
        if subject not in {owner_id, same_org_intruder_id, intruder_id}:
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        email = (
            "intruder@example.test"
            if subject == intruder_id
            else (
                "same-org-intruder@example.test"
                if subject == same_org_intruder_id
                else "owner@example.test"
            )
        )
        organization_id = (
            owner_org_id
            if subject in {owner_id, same_org_intruder_id}
            else intruder_org_id
        )
        return JSONResponse(
            content={
                "user": {
                    "id": str(subject),
                    "email": email,
                    "first_name": "Controlled",
                    "last_name": "Reviewer",
                    "full_name": "Controlled Reviewer",
                    "role": "user",
                    "is_active": True,
                },
                "organization": {
                    "id": str(organization_id),
                    "name": "Controlled Research",
                },
            }
        )

    @app.post("/api/v1/e2e/runs/{run_id}/rotate-review")
    async def rotate_review(run_id: uuid.UUID) -> JSONResponse:
        async with app.state.db_factory() as db:
            run = await db.get(ResearchRun, run_id)
            if run is None:
                return JSONResponse(status_code=404, content={"detail": "not found"})
            manifest = dict(run.reproducibility_manifest or {})
            pending = dict(manifest.get("pending_review") or {})
            step_index = int(pending["step_index"])
            step = (
                await db.execute(
                    select(ResearchStep).where(
                        ResearchStep.run_id == run_id,
                        ResearchStep.step_index == step_index,
                    )
                )
            ).scalar_one()
            output = dict(cast(dict[str, Any], step.output))
            screening = [dict(item) for item in output.get("screening", [])]
            if not screening:
                return JSONResponse(
                    status_code=409,
                    content={"detail": "fixture rotates screening reviews only"},
                )
            screening[0]["reason"] = "Server-side fixture revision"
            output["screening"] = screening
            old_hash = pending["output_hash"]
            new_hash = canonical_stage_output_hash(output)
            step.output = output
            step.outputs_hash = new_hash
            pending["output_hash"] = new_hash
            manifest["pending_review"] = pending
            run.reproducibility_manifest = manifest
            await db.commit()
        return JSONResponse(content={"old_hash": old_hash, "new_hash": new_hash})

    @app.post("/api/v1/e2e/manual-pause/release")
    async def release_manual_pause_stage() -> dict[str, str]:
        fixture_state["manual_pause_release"].set()
        return {"status": "released"}

    @app.get("/api/v1/e2e/runs/{run_id}/audit")
    async def audit(run_id: uuid.UUID) -> JSONResponse:
        async with app.state.db_factory() as db:
            run = await db.get(ResearchRun, run_id)
            if run is None:
                return JSONResponse(status_code=404, content={"detail": "not found"})
            steps = list(
                (
                    await db.execute(
                        select(ResearchStep)
                        .where(ResearchStep.run_id == run_id)
                        .order_by(ResearchStep.step_index)
                    )
                )
                .scalars()
                .all()
            )
            reviews = list(
                (
                    await db.execute(
                        select(ResearchStageReview)
                        .where(ResearchStageReview.run_id == run_id)
                        .order_by(ResearchStageReview.step_index)
                    )
                )
                .scalars()
                .all()
            )
        return JSONResponse(
            content={
                "status": run.status,
                "manifest": run.reproducibility_manifest or {},
                "steps": [
                    {
                        "step_index": step.step_index,
                        "step_type": step.step_type,
                        "outputs_hash": step.outputs_hash,
                        "canonical_hash": canonical_stage_output_hash(
                            cast(dict[str, Any], step.output)
                        ),
                        "output": step.output,
                    }
                    for step in steps
                ],
                "reviews": [
                    {
                        "id": str(review.id),
                        "reviewer_id": str(review.reviewer_id),
                        "step_index": review.step_index,
                        "review_kind": review.review_kind,
                        "output_hash": review.output_hash,
                        "decision": review.decision,
                        "decision_payload": review.decision_payload,
                        "created_at": review.created_at.isoformat(),
                    }
                    for review in reviews
                ],
                "reconnect_failures": sum(
                    1
                    for failed_run_id in fixture_state["reconnect_failures"]
                    if failed_run_id == run_id
                ),
            }
        )

    for research_router in (
        projects_router,
        blueprints_router,
        capabilities_router,
        runs_router,
        steps_router,
        reviews_router,
    ):
        app.include_router(research_router, prefix="/api/v1")
    return app

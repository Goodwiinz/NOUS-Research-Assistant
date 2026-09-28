"""PostgreSQL round-trip coverage for exported research-step provenance."""

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from src.api.research_engine.runs import _research_step_from_event
from src.models import Base
from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.export_service import ExportService
from src.services.research_engine.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderConfig,
)
from src.services.research_engine.step_executor import StepExecutor


class _AsyncSessionAdapter:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def execute(self, statement: Any) -> Any:
        return self.session.execute(statement)


class _DeterministicProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(
            ProviderConfig(
                provider_type="test",
                model_id="requested-model",
                model_version="configured-version",
            )
        )
        self.request: LLMRequest | None = None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.request = request
        return LLMResponse(
            content="provider output",
            model_id="effective-model",
            model_version="2026-09-01",
            input_tokens=2,
            output_tokens=1,
            temperature=request.temperature,
            seed=None,
        )

    async def is_model_available(self) -> bool:
        return True


@pytest.fixture
def provenance_postgres_session(
    request: pytest.FixtureRequest,
) -> Iterator[Session]:
    configured_url = os.getenv("RESEARCH_PROVENANCE_DATABASE_URL")
    if configured_url:
        url = configured_url
    else:
        container: Any = request.getfixturevalue("postgres_container")
        url = str(container["url"])
    url = url.replace("+asyncpg", "")
    schema = f"test_research_provenance_{uuid4().hex}"
    admin_engine = create_engine(url)
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.requires_postgres
async def test_research_step_provenance_survives_db_round_trip_and_export(
    provenance_postgres_session: Session,
) -> None:
    postgres_session = provenance_postgres_session
    user_id, project_id, blueprint_id, run_id = (uuid4() for _ in range(4))
    now = datetime.now(timezone.utc)
    step_def = {
        "id": "synthesize",
        "type": "synthesize",
        "parameters": {
            "model_id": "requested-model",
            "system_prompt_template": "Synthesize: {query}",
            "temperature": 0.25,
            "seed": 7,
        },
    }
    provider = _DeterministicProvider()
    engine = WorkflowEngine(
        StepExecutor(connectors={}, providers={"requested-model": provider})
    )
    events = [
        event
        async for event in engine.run(
            {
                "steps": [step_def],
                "parameters": {
                    "query": "grounded question",
                    "source_records": [{"source_id": str(uuid4())}],
                },
            },
            run_id,
        )
    ]
    completed = next(event for event in events if event["event"] == "step_complete")
    assert provider.request is not None
    expected_inputs_hash = hashlib.sha256(
        (provider.request.prompt + (provider.request.system_prompt or "")).encode()
    ).hexdigest()
    expected_outputs_hash = hashlib.sha256(b"provider output").hexdigest()
    assert completed["inputs_hash"] == expected_inputs_hash
    assert completed["outputs_hash"] == expected_outputs_hash
    assert "source_records" in provider.request.prompt

    postgres_session.execute(
        text("""
            INSERT INTO users
                (id, email, password_hash, first_name, last_name, role, is_active,
                 login_count, created_at, updated_at, is_deleted)
            VALUES
                (:id, :email, 'unused', 'Test', 'Owner', 'USER', true, 0,
                 :now, :now, false)
            """),
        {"id": user_id, "email": f"{user_id}@example.test", "now": now},
    )
    postgres_session.execute(
        text("""
            INSERT INTO research_projects
                (id, name, owner_id, status, created_at, updated_at, is_deleted)
            VALUES (:id, 'Provenance test', :owner_id, 'active', :now, :now, false)
            """),
        {"id": project_id, "owner_id": user_id, "now": now},
    )
    postgres_session.execute(
        text("""
            INSERT INTO research_blueprints
                (id, project_id, name, version, steps, parameters, is_immutable,
                 created_at, updated_at, is_deleted)
            VALUES (:id, :project_id, 'Blueprint', 1, CAST(:steps AS jsonb),
                    CAST(:parameters AS jsonb), true,
                    :now, :now, false)
            """),
        {
            "id": blueprint_id,
            "project_id": project_id,
            "steps": json.dumps([step_def]),
            "parameters": json.dumps({"query": "grounded question"}),
            "now": now,
        },
    )
    postgres_session.execute(
        text("""
            INSERT INTO research_runs
                (id, blueprint_id, blueprint_version, status, total_tokens,
                 created_at, updated_at, is_deleted)
            VALUES (:id, :blueprint_id, 1, 'completed', :tokens, :now, :now, false)
            """),
        {
            "id": run_id,
            "blueprint_id": blueprint_id,
            "tokens": completed["token_count"],
            "now": now,
        },
    )
    postgres_session.add(_research_step_from_event(run_id, step_def, completed))
    postgres_session.commit()
    postgres_session.close()

    reopened = sessionmaker(bind=postgres_session.get_bind())()
    artifact = json.loads(
        json.dumps(
            await ExportService().export_json(run_id, _AsyncSessionAdapter(reopened))
        )
    )
    reopened.close()

    exported = artifact["steps"][0]
    exported_prompt = json.loads(exported["full_prompt"])
    downloaded_inputs_hash = hashlib.sha256(
        (exported_prompt["prompt"] + (exported_prompt["system_prompt"] or "")).encode()
    ).hexdigest()
    downloaded_outputs_hash = hashlib.sha256(
        exported["output"]["content"].encode()
    ).hexdigest()
    assert exported["inputs_hash"] == expected_inputs_hash == downloaded_inputs_hash
    assert exported["outputs_hash"] == expected_outputs_hash == downloaded_outputs_hash
    assert exported_prompt == {
        "prompt": provider.request.prompt,
        "system_prompt": "Synthesize: grounded question",
    }
    assert exported["model_id"] == "effective-model"
    assert exported["model_version"] == "2026-09-01"
    assert exported["temperature"] == 0.25
    assert exported["seed"] is None
    assert exported["output"] == {"content": "provider output"}

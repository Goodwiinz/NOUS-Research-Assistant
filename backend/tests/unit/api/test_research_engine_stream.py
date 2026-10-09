"""
Unit tests for the Research Engine SSE streaming endpoint.

Tests cover:
- GET /runs/{run_id}/stream returns 200 with SSE content-type
- 404 returned for non-existent run
- 409 returned for run not in PENDING or PAUSED status
- Proper SSE headers (Cache-Control, X-Accel-Buffering)
- SSE events are yielded in correct event:/data: format
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.research_engine.runs import (
    get_manifest,
    pause_run,
    resume_run,
    router,
    stream_run,
)
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_stage_review import ResearchStageReview
from src.models.user import User
from src.schemas.research_engine import RunResponse, RunResumeRequest
from src.services.research_engine.contracts import canonical_stage_output_hash
from src.services.research_engine.observability import ResearchObservability
from src.services.research_engine.review_service import ResearchReviewService

# ============================================================================
# Helpers
# ============================================================================


@pytest.mark.asyncio
async def test_manifest_read_exposes_durable_receipts_before_run_completion():
    run_id = uuid.uuid4()
    run = _make_run(
        id=run_id,
        status="paused",
        reproducibility_manifest={
            "_search_receipts_v1": {"schema_version": 1, "executions": {}}
        },
    )
    user = SimpleNamespace(id=uuid.uuid4())
    with patch(
        "src.api.research_engine.runs._get_owned_run",
        new=AsyncMock(return_value=run),
    ):
        result = await get_manifest(run_id, user, AsyncMock())
    assert result["run_status"] == "paused"
    assert result["_search_receipts_v1"]["schema_version"] == 1


def _make_run(**overrides):
    """Create a mock ResearchRun ORM object."""
    now = datetime.now(timezone.utc)
    run = Mock()
    run.id = overrides.get("id", uuid.uuid4())
    run.blueprint_id = overrides.get("blueprint_id", uuid.uuid4())
    run.project_id = overrides.get("project_id", uuid.uuid4())
    run.research_engine_project_id = overrides.get(
        "research_engine_project_id", uuid.uuid4()
    )
    run.blueprint_version = overrides.get("blueprint_version", 1)
    run.status = overrides.get("status", "pending")
    run.started_at = overrides.get("started_at", None)
    run.completed_at = overrides.get("completed_at", None)
    run.total_tokens = overrides.get("total_tokens", 0)
    run.protocol_version_id = overrides.get("protocol_version_id", uuid.uuid4())
    run.effective_plan_hash = overrides.get("effective_plan_hash", "a" * 64)
    run.conformance_status = overrides.get("conformance_status", "plan_verified")
    run.reproducibility_manifest = overrides.get("reproducibility_manifest", None)
    run.created_at = overrides.get("created_at", now)
    run.updated_at = overrides.get("updated_at", now)
    return run


def _make_blueprint(**overrides):
    """Create a mock ResearchBlueprint ORM object."""
    bp = Mock()
    bp.id = overrides.get("id", uuid.uuid4())
    bp.project_id = overrides.get("project_id", uuid.uuid4())
    bp.name = overrides.get("name", "Test Blueprint")
    bp.version = overrides.get("version", 1)
    bp.steps = overrides.get("steps", [{"type": "search", "query": "test"}])
    bp.parameters = overrides.get("parameters", {})
    bp.is_immutable = True
    return bp


def _resume_authorization() -> dict[str, object]:
    return {
        "kind": "user_resume",
        "actor_id": str(uuid.uuid4()),
        "step_index": -1,
        "output_hash": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "consumed_at": None,
    }


def _mock_db_returning(
    run_result=None,
    blueprint_result=None,
    project_result=None,
    last_step_index=None,
    last_step_quality_marks=None,
):
    """Build an AsyncMock DB that returns expected query results.

    After C2 fix, _get_owned_run uses a single JOIN query that returns
    the run directly (or None if not owned). For the stream endpoint:
    - Query 1: _get_owned_run (single JOIN) -> run_result
    - Query 2: get blueprint (for streaming) -> blueprint_result
    - Query 3: get last completed step (for paused runs)
    """
    db = AsyncMock()
    db.expire_all = Mock()
    db._run_result = run_result
    results = []

    if run_result is not None:
        # Only add more results for streamable statuses
        streamable = {"pending", "paused"}
        if run_result.status in streamable:
            # Query 2: Blueprint lookup for streaming
            bp_stream_mock = Mock()
            bp_stream_mock.scalars.return_value.first.return_value = blueprint_result
            results.append(bp_stream_mock)

            # Query 3: Last-step lookup for resume offset
            step_mock = Mock()
            if last_step_index is None:
                step_mock.scalars.return_value.first.return_value = None
            else:
                last_step = Mock()
                last_step.step_index = last_step_index
                last_step.quality_marks = last_step_quality_marks
                step_mock.scalars.return_value.first.return_value = last_step
            results.append(step_mock)

            # Prior-outputs history SELECT now happens before the atomic claim.
            history_mock = Mock()
            history_mock.scalars.return_value.all.return_value = []
            results.append(history_mock)

            # R5-M18 claim UPDATE → rowcount=1
            claim_mock = Mock()
            claim_mock.rowcount = 1
            results.append(claim_mock)

    db.execute = AsyncMock(side_effect=results)
    # commit/refresh are no-ops in tests
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = Mock()
    return db


@pytest.fixture(autouse=True)
def patch_shared_run_access(monkeypatch):
    """Keep stream tests focused on SSE behavior, not resolver SQL shape."""

    async def fake_require_run(db, run_id, user_id, action):
        run = db._run_result
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    context = SimpleNamespace(
        collection=SimpleNamespace(id=uuid.uuid4()),
        engine=SimpleNamespace(id=uuid.uuid4()),
    )

    async def fake_require_conformance(db, run, blueprint, project_context):
        return {
            "blueprint_id": str(blueprint.id),
            "blueprint_version": blueprint.version,
            "steps": blueprint.steps,
            "parameters": blueprint.parameters,
        }

    monkeypatch.setattr("src.api.research_engine.runs.require_run", fake_require_run)
    monkeypatch.setattr(
        "src.api.research_engine.runs.resolve_engine_project_context",
        AsyncMock(return_value=context),
    )
    monkeypatch.setattr(
        "src.api.research_engine.runs.resolve_project",
        AsyncMock(return_value=context),
    )
    monkeypatch.setattr(
        "src.api.research_engine.runs._require_run_conformance",
        fake_require_conformance,
    )


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def stream_app():
    """Minimal FastAPI app with just the runs router."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def mock_current_user():
    user = Mock()
    user.id = uuid.uuid4()
    user.email = "researcher@example.com"
    user.is_active = True
    return user


@pytest.fixture
def stream_client(stream_app, mock_current_user):
    """TestClient with auth override on the minimal app."""
    stream_app.dependency_overrides[get_current_user] = lambda: mock_current_user

    @asynccontextmanager
    async def _no_lifespan(_app):
        yield

    original_lifespan = stream_app.router.lifespan_context
    stream_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(stream_app) as c:
            yield c
    finally:
        stream_app.router.lifespan_context = original_lifespan
        stream_app.dependency_overrides.clear()


# ============================================================================
# Success Cases
# ============================================================================


class TestStreamEndpointSuccess:
    """Test successful SSE streaming responses."""

    def _patch_engine_and_get(self, stream_app, stream_client, run_id, mock_engine_run):
        """Patch engine classes and collect a bounded SSE response snapshot."""
        with (
            patch(
                "src.api.research_engine.runs.admit_expensive_work",
                new=AsyncMock(return_value=True),
            ),
            patch("src.api.research_engine.runs.WorkflowEngine") as mock_engine_cls,
            patch("src.api.research_engine.runs.StepExecutor"),
            patch("src.api.research_engine.runs.ArxivConnector"),
            patch("src.api.research_engine.runs.SemanticScholarConnector"),
        ):
            engine = Mock()
            engine.run = mock_engine_run
            mock_engine_cls.return_value = engine

            with stream_client.stream(
                "GET", f"/api/v1/research-engine/runs/{run_id}/stream"
            ) as response:
                chunks = []
                for chunk in response.iter_text():
                    chunks.append(chunk)
                    body = "".join(chunks)
                    if any(
                        marker in body
                        for marker in (
                            "event: run_complete",
                            "event: run_failed",
                            "event: run_paused",
                        )
                    ):
                        break

                return SimpleNamespace(
                    status_code=response.status_code,
                    headers=response.headers,
                    text="".join(chunks),
                )

    def test_stream_returns_200_with_sse_content_type(self, stream_app, stream_client):
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(id=run_id, blueprint_id=bp_id, status="pending")
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)

        stream_app.dependency_overrides[get_db] = lambda: db

        async def mock_engine_run(blueprint, run_id, start_from_step=0, **kwargs):
            yield {"event": "run_start", "run_id": str(run_id), "total_steps": 1}
            yield {"event": "run_complete", "run_id": str(run_id), "context": {}}

        response = self._patch_engine_and_get(
            stream_app, stream_client, run_id, mock_engine_run
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        stream_app.dependency_overrides.pop(get_db, None)

    def test_stream_returns_correct_sse_headers(self, stream_app, stream_client):
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(id=run_id, blueprint_id=bp_id, status="pending")
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)

        stream_app.dependency_overrides[get_db] = lambda: db

        async def mock_engine_run(blueprint, run_id, start_from_step=0, **kwargs):
            yield {"event": "run_complete", "run_id": str(run_id), "context": {}}

        response = self._patch_engine_and_get(
            stream_app, stream_client, run_id, mock_engine_run
        )

        assert response.headers.get("cache-control") == "no-cache"
        assert response.headers.get("x-accel-buffering") == "no"
        stream_app.dependency_overrides.pop(get_db, None)

    def test_stream_yields_sse_events(self, stream_app, stream_client):
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(id=run_id, blueprint_id=bp_id, status="pending")
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)

        stream_app.dependency_overrides[get_db] = lambda: db

        async def mock_engine_run(blueprint, run_id, start_from_step=0, **kwargs):
            yield {"event": "run_start", "run_id": str(run_id), "total_steps": 2}
            yield {
                "event": "step_complete",
                "run_id": str(run_id),
                "step_index": 0,
                "step_type": "search",
            }
            yield {"event": "run_complete", "run_id": str(run_id), "context": {}}

        response = self._patch_engine_and_get(
            stream_app, stream_client, run_id, mock_engine_run
        )

        body = response.text
        assert "event: run_start" in body
        assert "event: step_complete" in body
        assert "event: run_complete" in body
        assert "data:" in body
        stream_app.dependency_overrides.pop(get_db, None)

    def test_stream_works_with_paused_run(self, stream_app, stream_client):
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(
            id=run_id,
            blueprint_id=bp_id,
            status="paused",
            reproducibility_manifest={"resume_authorization": _resume_authorization()},
        )
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)

        stream_app.dependency_overrides[get_db] = lambda: db

        async def mock_engine_run(blueprint, run_id, start_from_step=0, **kwargs):
            yield {"event": "run_complete", "run_id": str(run_id), "context": {}}

        response = self._patch_engine_and_get(
            stream_app, stream_client, run_id, mock_engine_run
        )

        assert response.status_code == 200
        stream_app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_resuming_paused_run_starts_a_fresh_active_deadline(self):
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        old_started_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        mock_run = _make_run(
            id=run_id,
            blueprint_id=bp_id,
            status="paused",
            started_at=old_started_at,
            reproducibility_manifest={"resume_authorization": _resume_authorization()},
        )
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)
        current_user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
        captured_started_at = {}

        async def mock_engine_run(blueprint, run_id, start_from_step=0, **kwargs):
            captured_started_at["value"] = kwargs["started_at"]
            yield {"event": "run_complete", "run_id": str(run_id), "context": {}}

        with (
            patch(
                "src.api.research_engine.runs.admit_expensive_work",
                new=AsyncMock(return_value=True),
            ),
            patch("src.api.research_engine.runs.WorkflowEngine") as engine_cls,
            patch("src.api.research_engine.runs.StepExecutor"),
            patch("src.api.research_engine.runs.ArxivConnector"),
            patch("src.api.research_engine.runs.SemanticScholarConnector"),
        ):
            engine = Mock()
            engine.run = mock_engine_run
            engine_cls.return_value = engine
            response = await stream_run(run_id, current_user, db)
            async for _chunk in response.body_iterator:
                pass

        assert response.status_code == 200
        assert captured_started_at["value"] is None

    def test_stream_pending_run_resumes_from_last_completed_step(
        self, stream_app, stream_client
    ):
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(id=run_id, blueprint_id=bp_id, status="pending")
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(
            run_result=mock_run,
            blueprint_result=mock_bp,
            last_step_index=2,
        )

        stream_app.dependency_overrides[get_db] = lambda: db

        captured_start_from: dict[str, int | None] = {"value": None}

        async def mock_engine_run(blueprint, run_id, start_from_step=0, **kwargs):
            captured_start_from["value"] = start_from_step
            yield {"event": "run_complete", "run_id": str(run_id), "context": {}}

        response = self._patch_engine_and_get(
            stream_app, stream_client, run_id, mock_engine_run
        )

        assert response.status_code == 200
        assert captured_start_from["value"] == 3
        stream_app.dependency_overrides.pop(get_db, None)

    def test_stream_persists_steps_on_step_complete(self, stream_app, stream_client):
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(id=run_id, blueprint_id=bp_id, status="pending")
        mock_bp = _make_blueprint(
            id=bp_id,
            steps=[{"type": "search", "mode": "deterministic"}],
        )
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)

        stream_app.dependency_overrides[get_db] = lambda: db

        async def mock_engine_run(blueprint, run_id, start_from_step=0, **kwargs):
            yield {
                "event": "step_complete",
                "run_id": str(run_id),
                "step_index": 0,
                "step_type": "search",
                "output": {"content": "ok"},
                "quality_marks": [],
                "token_count": 42,
                "inputs_hash": "a" * 64,
                "outputs_hash": "b" * 64,
                "full_prompt": "permitted system prompt",
                "model_id": "effective-model",
                "model_version": "2026-09-01",
                "temperature": 0.25,
                "seed": 7,
            }
            yield {"event": "run_complete", "run_id": str(run_id), "context": {}}

        response = self._patch_engine_and_get(
            stream_app, stream_client, run_id, mock_engine_run
        )

        assert response.status_code == 200
        db.add.assert_called()
        from src.models.research_step import ResearchStep

        step = next(
            call.args[0]
            for call in db.add.call_args_list
            if isinstance(call.args[0], ResearchStep)
        )
        assert step.inputs_hash == "a" * 64
        assert step.outputs_hash == "b" * 64
        assert step.full_prompt == "permitted system prompt"
        assert step.model_id == "effective-model"
        assert step.model_version == "2026-09-01"
        assert step.temperature == 0.25
        assert step.seed == 7
        stream_app.dependency_overrides.pop(get_db, None)

    def test_stream_persists_discovered_sources_with_step(
        self, stream_app, stream_client
    ):
        from src.models.research_source import ResearchSource
        from src.models.research_step import ResearchStep

        run_id = uuid.uuid4()
        source_id = uuid.uuid4()
        run = _make_run(id=run_id)
        bp = _make_blueprint(id=run.blueprint_id, steps=[{"type": "search"}])
        db = _mock_db_returning(run_result=run, blueprint_result=bp)
        stream_app.dependency_overrides[get_db] = lambda: db
        record = {
            "source_id": str(source_id),
            "connector_type": "openalex",
            "external_id": "W123",
            "title": "Paper",
            "authors": ["Author"],
            "abstract": "Actual evidence",
            "url": "https://openalex.org/W123",
            "content_hash": "a" * 64,
            "evidence_level": "abstract",
            "metadata": {"identifiers": {"doi": "10.1234/abc"}, "provenance": []},
        }

        async def engine_run(**kwargs):
            yield {
                "event": "step_complete",
                "step_index": 0,
                "output": {"source_records": [record]},
            }
            yield {"event": "run_complete"}

        # GOO-299: identity SQL needs PostgreSQL; here only the wiring is checked.
        with patch(
            "src.services.research_engine.run_lifecycle.observe_sources",
            new=AsyncMock(return_value=[]),
        ) as observe:
            response = self._patch_engine_and_get(
                stream_app, stream_client, run_id, engine_run
            )
        assert "event: run_complete" in response.text
        added = [call.args[0] for call in db.add.call_args_list]
        sources = [row for row in added if isinstance(row, ResearchSource)]
        assert len(sources) == 1
        observe.assert_awaited_once()
        assert observe.await_args.args == (db,)
        assert observe.await_args.kwargs["sources"] == sources
        assert observe.await_args.kwargs["collection_id"] is not None
        assert sources[0].run_id == run_id
        assert sources[0].id == source_id
        assert sources[0].abstract == "Actual evidence"
        assert sources[0].metadata_["identifiers"] == {"doi": "10.1234/abc"}
        step = next(row for row in added if isinstance(row, ResearchStep))
        assert step.output["source_records"][0]["source_id"] == str(sources[0].id)

    @pytest.mark.asyncio
    async def test_stream_persists_completed_step_before_honoring_external_pause(
        self,
    ):
        from src.models.research_step import ResearchStep

        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(id=run_id, blueprint_id=bp_id, status="pending")
        mock_bp = _make_blueprint(
            id=bp_id,
            steps=[{"type": "search", "mode": "deterministic"}],
        )
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)
        current_user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
        refresh_counter = 0

        async def refresh_with_pause(_obj):
            nonlocal refresh_counter
            refresh_counter += 1
            if refresh_counter >= 3:
                mock_run.reproducibility_manifest = {"_pause_requested": True}

        db.refresh = AsyncMock(side_effect=refresh_with_pause)

        async def mock_engine_run(blueprint, run_id, start_from_step=0, **kwargs):
            yield {
                "event": "step_complete",
                "run_id": str(run_id),
                "step_index": 0,
                "step_type": "search",
                "output": {"content": "done"},
                "quality_marks": [],
                "token_count": 10,
            }
            yield {"event": "step_start", "run_id": str(run_id), "step_index": 1}

        with (
            patch(
                "src.api.research_engine.runs.admit_expensive_work",
                new=AsyncMock(return_value=True),
            ),
            patch("src.api.research_engine.runs.WorkflowEngine") as engine_cls,
            patch("src.api.research_engine.runs.StepExecutor"),
            patch("src.api.research_engine.runs.ArxivConnector"),
            patch("src.api.research_engine.runs.SemanticScholarConnector"),
        ):
            engine = Mock()
            engine.run = mock_engine_run
            engine_cls.return_value = engine
            response = await stream_run(run_id, current_user, db)
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

        body = "".join(chunks)
        persisted_steps = [
            call.args[0]
            for call in db.add.call_args_list
            if isinstance(call.args[0], ResearchStep)
        ]
        assert len(persisted_steps) == 1
        assert persisted_steps[0].token_count == 10
        assert mock_run.total_tokens == 10
        assert body.index("event: step_complete") < body.index("event: run_paused")
        assert '"step_index": 1' not in body

    def test_stream_honors_external_pause_request(self, stream_app, stream_client):
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(id=run_id, blueprint_id=bp_id, status="pending")
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)

        refresh_counter = {"count": 0}

        async def refresh_with_pause(_obj):
            refresh_counter["count"] += 1
            if refresh_counter["count"] >= 3:
                mock_run.reproducibility_manifest = {"_pause_requested": True}

        db.refresh = AsyncMock(side_effect=refresh_with_pause)

        stream_app.dependency_overrides[get_db] = lambda: db

        async def mock_engine_run(blueprint, run_id, start_from_step=0, **kwargs):
            yield {"event": "step_start", "run_id": str(run_id), "step_index": 0}
            yield {
                "event": "step_complete",
                "run_id": str(run_id),
                "step_index": 0,
                "output": {"content": "done"},
                "quality_marks": [],
                "token_count": 10,
            }
            yield {"event": "step_start", "run_id": str(run_id), "step_index": 1}

        response = self._patch_engine_and_get(
            stream_app, stream_client, run_id, mock_engine_run
        )

        assert "event: run_paused" in response.text
        assert '"step_index": 1' not in response.text
        stream_app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_pause_request_keeps_inflight_run_claimed(self):
        """A second stream cannot reclaim a run until the first acknowledges pause."""
        run_id = uuid.uuid4()
        run = _make_run(
            id=run_id,
            status="running",
            reproducibility_manifest={"parameters_override": {}},
        )
        db = AsyncMock()
        db.execute = AsyncMock(return_value=Mock(rowcount=1))
        db.commit = AsyncMock()

        async def refresh_with_pause(_obj):
            run.reproducibility_manifest = {
                "parameters_override": {},
                "_pause_requested": True,
            }

        db.refresh = AsyncMock(side_effect=refresh_with_pause)
        current_user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

        with patch(
            "src.api.research_engine.runs._get_owned_run",
            new=AsyncMock(return_value=run),
        ):
            response = await pause_run(run_id, current_user, db)

            assert response.status.value == "running"
            assert run.reproducibility_manifest["_pause_requested"] is True
            with pytest.raises(HTTPException) as exc_info:
                await stream_run(run_id, current_user, db)

        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_pause_request_appends_flag_without_rewriting_manifest(self):
        """Search receipts committed by the stream must survive a concurrent pause.

        Mutation guard: replace the jsonb concat in pause_run with a plain
        dict assignment and this test must fail.
        """
        from sqlalchemy.dialects import postgresql

        run_id = uuid.uuid4()
        run = _make_run(
            id=run_id,
            status="running",
            reproducibility_manifest={"parameters_override": {}},
        )
        statements: list = []

        async def capture(statement):
            statements.append(statement)
            return Mock(rowcount=1)

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=capture)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        current_user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

        with patch(
            "src.api.research_engine.runs._get_owned_run",
            new=AsyncMock(return_value=run),
        ):
            await pause_run(run_id, current_user, db)

        from sqlalchemy.sql.dml import Update

        pause_update = next(stmt for stmt in statements if isinstance(stmt, Update))
        sql = str(pause_update.compile(dialect=postgresql.dialect()))
        set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
        assert "research_runs.reproducibility_manifest" in set_clause
        assert "||" in set_clause

    @pytest.mark.asyncio
    async def test_pause_request_losing_completion_race_preserves_final_manifest(self):
        """A late pause cannot overwrite a run that completed concurrently."""
        run_id = uuid.uuid4()
        final_manifest = {
            "run_id": str(run_id),
            "total_tokens": 42,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        run = _make_run(
            id=run_id,
            status="running",
            reproducibility_manifest={"parameters_override": {}},
        )
        update_result = Mock(rowcount=0)
        db = AsyncMock()

        async def completion_wins(_statement):
            run.status = "completed"
            run.reproducibility_manifest = final_manifest
            return update_result

        db.execute = AsyncMock(side_effect=completion_wins)
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        current_user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

        with patch(
            "src.api.research_engine.runs._get_owned_run",
            new=AsyncMock(return_value=run),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await pause_run(run_id, current_user, db)

        assert exc_info.value.status_code == 409
        assert run.reproducibility_manifest == final_manifest
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stream_cancellation_rolls_back_before_persisting_pause(self):
        """Disconnect cleanup starts from a usable database transaction."""
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(
            id=run_id,
            blueprint_id=bp_id,
            status="pending",
            reproducibility_manifest={"_pause_requested": True},
        )
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)
        current_user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

        async def cancelled_engine(*_args, **_kwargs):
            raise asyncio.CancelledError("client disconnected")
            yield  # pragma: no cover - keeps this function an async generator

        with (
            patch(
                "src.api.research_engine.runs.admit_expensive_work",
                new=AsyncMock(return_value=True),
            ),
            patch("src.api.research_engine.runs.WorkflowEngine") as engine_cls,
            patch("src.api.research_engine.runs.StepExecutor"),
            patch("src.api.research_engine.runs.ArxivConnector"),
            patch("src.api.research_engine.runs.SemanticScholarConnector"),
        ):
            engine = Mock()
            engine.run = cancelled_engine
            engine_cls.return_value = engine
            response = await stream_run(run_id, current_user, db)

            with pytest.raises(asyncio.CancelledError, match="client disconnected"):
                async for _chunk in response.body_iterator:
                    pass

        db.rollback.assert_awaited_once()
        assert mock_run.status == "paused"
        user_pause = mock_run.reproducibility_manifest["user_pause"]
        assert user_pause["step_index"] == -1
        assert len(user_pause["output_hash"]) == 64
        assert "_pause_requested" not in mock_run.reproducibility_manifest

    @pytest.mark.asyncio
    async def test_disconnect_after_terminal_commit_cannot_rewrite_run_to_paused(self):
        """Cancellation at terminal frame delivery preserves the committed outcome."""
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(
            id=run_id,
            blueprint_id=bp_id,
            status="pending",
            reproducibility_manifest={"parameters_override": {}},
        )
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)
        current_user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

        async def terminal_engine(*_args, **_kwargs):
            yield {"event": "run_complete", "run_id": str(run_id), "context": {}}
            await asyncio.Event().wait()

        with (
            patch(
                "src.api.research_engine.runs.admit_expensive_work",
                new=AsyncMock(return_value=True),
            ),
            patch("src.api.research_engine.runs.WorkflowEngine") as engine_cls,
            patch("src.api.research_engine.runs.StepExecutor"),
            patch("src.api.research_engine.runs.ArxivConnector"),
            patch("src.api.research_engine.runs.SemanticScholarConnector"),
        ):
            engine = Mock()
            engine.run = terminal_engine
            engine_cls.return_value = engine
            response = await stream_run(run_id, current_user, db)
            iterator = response.body_iterator

            terminal_frame = await anext(iterator)
            assert "event: run_complete" in terminal_frame
            assert mock_run.status == "completed"

            with pytest.raises(asyncio.CancelledError):
                await iterator.athrow(asyncio.CancelledError("client disconnected"))

        assert mock_run.status == "completed"
        assert "user_pause" not in mock_run.reproducibility_manifest
        assert "resume_authorization" not in mock_run.reproducibility_manifest

    @pytest.mark.asyncio
    async def test_stream_commit_failure_refreshes_after_rollback(self):
        """A failed write is rolled back and refreshed before terminal recovery."""
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(
            id=run_id,
            blueprint_id=bp_id,
            status="pending",
            reproducibility_manifest={"_pause_requested": True},
        )
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)
        current_user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

        async def completed_step(*_args, **_kwargs):
            yield {
                "event": "step_complete",
                "run_id": str(run_id),
                "step_index": 0,
                "step_type": "search",
                "output": {"content": "done"},
                "quality_marks": [],
                "token_count": 10,
            }

        operations: list[str] = []
        observer = ResearchObservability()

        async def rollback():
            operations.append("rollback")
            mock_run.reproducibility_manifest = {"_pause_requested": True}
            mock_run.total_tokens = 0

        async def refresh(_obj):
            operations.append("refresh")

        with (
            patch(
                "src.api.research_engine.runs.admit_expensive_work",
                new=AsyncMock(return_value=True),
            ),
            patch("src.api.research_engine.runs.WorkflowEngine") as engine_cls,
            patch("src.api.research_engine.runs.StepExecutor"),
            patch("src.api.research_engine.runs.ArxivConnector"),
            patch("src.api.research_engine.runs.SemanticScholarConnector"),
            patch("src.api.research_engine.runs.research_observability", observer),
        ):
            engine = Mock()
            engine.run = completed_step
            engine_cls.return_value = engine
            response = await stream_run(run_id, current_user, db)
            operations.clear()
            db.rollback = AsyncMock(side_effect=rollback)
            db.refresh = AsyncMock(side_effect=refresh)
            db.commit = AsyncMock(side_effect=[RuntimeError("db write failed"), None])

            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

        rollback_index = len(operations) - 1 - operations[::-1].index("rollback")
        assert operations[rollback_index : rollback_index + 2] == [
            "rollback",
            "refresh",
        ]
        assert mock_run.status == "failed"
        assert mock_run.reproducibility_manifest is None
        stream = "".join(chunks)
        assert "db write failed" not in stream
        assert "Research stream failed" in stream
        assert observer.snapshot()["counters"]["sse_errors"]["stream_failure"] == 1


# ============================================================================
# Error Cases
# ============================================================================


class TestStreamEndpointErrors:
    """Test error cases for the streaming endpoint."""

    def test_stream_returns_404_for_nonexistent_run(self, stream_app, stream_client):
        run_id = uuid.uuid4()
        db = _mock_db_returning(run_result=None)

        stream_app.dependency_overrides[get_db] = lambda: db

        response = stream_client.get(f"/api/v1/research-engine/runs/{run_id}/stream")
        assert response.status_code == 404
        stream_app.dependency_overrides.pop(get_db, None)

    def test_stream_returns_409_for_completed_run(self, stream_app, stream_client):
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(id=run_id, blueprint_id=bp_id, status="completed")
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)

        stream_app.dependency_overrides[get_db] = lambda: db

        response = stream_client.get(f"/api/v1/research-engine/runs/{run_id}/stream")
        assert response.status_code == 409
        stream_app.dependency_overrides.pop(get_db, None)

    def test_resume_keeps_paused_status_until_stream_claims_run(
        self, stream_app, stream_client
    ):
        run_id = uuid.uuid4()
        run = _make_run(id=run_id, status="paused")
        db = AsyncMock()
        no_step = Mock()
        no_step.scalars.return_value.first.return_value = None
        db.execute = AsyncMock(return_value=no_step)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        stream_app.dependency_overrides[get_db] = lambda: db

        with patch(
            "src.api.research_engine.runs._get_owned_run",
            new=AsyncMock(return_value=run),
        ):
            response = stream_client.post(
                f"/api/v1/research-engine/runs/{run_id}/resume"
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "paused"
        assert run.reproducibility_manifest["continuation_requested"] is True
        stream_app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_post_resume_then_stream_claims_paused_run(self):
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        started_at = datetime(2026, 9, 27, 11, 0, tzinfo=timezone.utc)
        run = _make_run(
            id=run_id,
            blueprint_id=bp_id,
            status="paused",
            started_at=started_at,
            reproducibility_manifest={
                "parameters_override": {},
                "resume_authorization": _resume_authorization(),
            },
        )
        blueprint = _make_blueprint(id=bp_id)
        blueprint.template_source = "daily_research_brief"
        blueprint_result = Mock()
        blueprint_result.scalars.return_value.first.return_value = blueprint
        last_step_result = Mock()
        last_step_result.scalars.return_value.first.return_value = None
        history_result = Mock()
        history_result.scalars.return_value.all.return_value = []
        claim_result = Mock(rowcount=1)
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                blueprint_result,
                last_step_result,
                history_result,
                claim_result,
            ]
        )
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.add = Mock()
        user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
        seen_context: dict[str, Any] = {}
        seen_kwargs: dict[str, Any] = {}

        async def engine_run(**kwargs):
            seen_kwargs.update(kwargs)
            seen_context.update(kwargs["initial_context"])
            yield {"event": "run_complete", "run_id": str(run_id), "context": {}}

        with (
            patch(
                "src.api.research_engine.runs._get_owned_run",
                new=AsyncMock(side_effect=[run, run]),
            ),
            patch(
                "src.api.research_engine.runs.admit_expensive_work",
                new=AsyncMock(return_value=True),
            ),
            patch("src.api.research_engine.runs.WorkflowEngine") as engine_cls,
            patch("src.api.research_engine.runs.StepExecutor"),
        ):
            engine = Mock()
            engine.run = engine_run
            engine_cls.return_value = engine
            resumed = await resume_run(
                run_id,
                body=RunResumeRequest(),
                current_user=user,
                db=db,
            )
            assert resumed.status.value == "paused"
            response = await stream_run(run_id, user, db)
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

        assert run.status == "completed"
        assert "event: run_complete" in "".join(chunks)
        assert seen_kwargs["record_run_started"] is False
        assert seen_context["artifact_provenance"]["generated_at"] == (
            started_at.isoformat()
        )

    @pytest.mark.asyncio
    async def test_stream_rehydration_gap_records_content_free_reconnect_error(
        self,
    ) -> None:
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        run = _make_run(id=run_id, blueprint_id=bp_id, status="paused")
        blueprint = _make_blueprint(id=bp_id)
        first = SimpleNamespace(
            step_index=0,
            output={
                "contract_version": 1,
                "stage_type": "search",
                "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
                "source_records": [],
                "coverage": {"exhaustive": False},
                "selected_sources": ["openalex"],
            },
        )
        third = SimpleNamespace(
            step_index=2,
            output={
                "contract_version": 1,
                "stage_type": "verify",
                "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
                "verification": {"passed": False, "claims": []},
                "processing_coverage": {},
            },
        )
        blueprint_result = Mock()
        blueprint_result.scalars.return_value.first.return_value = blueprint
        last_step_result = Mock()
        last_step_result.scalars.return_value.first.return_value = third
        history_result = Mock()
        history_result.scalars.return_value.all.return_value = [first, third]
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[blueprint_result, last_step_result, history_result]
        )
        user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
        observer = ResearchObservability()

        with (
            patch(
                "src.api.research_engine.runs._get_owned_run",
                new=AsyncMock(return_value=run),
            ),
            patch("src.api.research_engine.runs.research_observability", observer),
        ):
            with pytest.raises(HTTPException) as raised:
                await stream_run(run_id, cast(User, user), db)

        assert raised.value.status_code == 409
        assert raised.value.detail == {
            "code": "run_reconstruction_failed",
            "message": "Persisted run stages cannot be resumed",
        }
        assert (
            observer.snapshot()["counters"]["rehydration_errors"]["invalid_history"]
            == 1
        )

    @pytest.mark.asyncio
    async def test_executor_exception_content_never_reaches_sse_or_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        run_id = uuid.uuid4()
        blueprint_id = uuid.uuid4()
        run = _make_run(id=run_id, blueprint_id=blueprint_id, status="pending")
        blueprint = _make_blueprint(
            id=blueprint_id,
            steps=[{"id": "extract", "type": "extract", "parameters": {}}],
        )
        db = _mock_db_returning(run_result=run, blueprint_result=blueprint)
        user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
        executor = Mock()
        executor.execute = AsyncMock(
            side_effect=RuntimeError("PRIVATE_RESEARCH_QUESTION")
        )

        with (
            caplog.at_level("INFO"),
            patch("src.api.research_engine.runs._build_connectors", return_value={}),
            patch(
                "src.api.research_engine.runs.admit_expensive_work",
                new=AsyncMock(return_value=True),
            ),
            patch("src.api.research_engine.runs.StepExecutor", return_value=executor),
        ):
            response = await stream_run(run_id, cast(User, user), db)
            chunks = [
                chunk.decode() if isinstance(chunk, bytes) else chunk
                async for chunk in response.body_iterator
            ]

        body = "".join(chunks)
        log_text = "\n".join(record.getMessage() for record in caplog.records)
        assert "event: step_error" in body
        assert "event: run_failed" in body
        assert "PRIVATE_RESEARCH_QUESTION" not in body
        assert "PRIVATE_RESEARCH_QUESTION" not in log_text
        assert '"error_category": "unexpected_step_error"' in body
        assert run.status == "failed"

    @pytest.mark.asyncio
    async def test_stream_claim_loser_does_not_start_resumed_run(self):
        """A competing stream that loses the atomic claim cannot execute work."""
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        run = _make_run(
            id=run_id,
            blueprint_id=bp_id,
            status="paused",
            reproducibility_manifest={"parameters_override": {}},
        )
        blueprint = _make_blueprint(id=bp_id)
        blueprint_result = Mock()
        blueprint_result.scalars.return_value.first.return_value = blueprint
        last_step_result = Mock()
        last_step_result.scalars.return_value.first.return_value = None
        history_result = Mock()
        history_result.scalars.return_value.all.return_value = []
        claim_result = Mock(rowcount=0)
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                blueprint_result,
                last_step_result,
                history_result,
                claim_result,
            ]
        )
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        async def refresh_after_competing_claim(target, **_kwargs):
            target.status = "running"

        db.lifecycle_lock_run = AsyncMock(side_effect=refresh_after_competing_claim)
        user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

        with (
            patch(
                "src.api.research_engine.runs._get_owned_run",
                new=AsyncMock(return_value=run),
            ),
            patch(
                "src.api.research_engine.runs.admit_expensive_work",
                new=AsyncMock(return_value=True),
            ) as admit,
            patch("src.api.research_engine.runs.WorkflowEngine") as engine_cls,
            patch("src.api.research_engine.runs.StepExecutor"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await stream_run(run_id, user, db)

        assert exc_info.value.status_code == 409
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()
        admit.assert_not_awaited()
        engine_cls.assert_not_called()

    def test_stream_returns_409_for_failed_run(self, stream_app, stream_client):
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(id=run_id, blueprint_id=bp_id, status="failed")
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)

        stream_app.dependency_overrides[get_db] = lambda: db

        response = stream_client.get(f"/api/v1/research-engine/runs/{run_id}/stream")
        assert response.status_code == 409
        stream_app.dependency_overrides.pop(get_db, None)

    def test_stream_returns_409_for_running_run(self, stream_app, stream_client):
        run_id = uuid.uuid4()
        bp_id = uuid.uuid4()
        mock_run = _make_run(id=run_id, blueprint_id=bp_id, status="running")
        mock_bp = _make_blueprint(id=bp_id)
        db = _mock_db_returning(run_result=mock_run, blueprint_result=mock_bp)

        stream_app.dependency_overrides[get_db] = lambda: db

        response = stream_client.get(f"/api/v1/research-engine/runs/{run_id}/stream")
        assert response.status_code == 409
        stream_app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_ordinary_resume_cannot_bypass_pending_review():
    """POST resume must require the approved row bound to the pending hash."""

    run_id = uuid.uuid4()
    run = _make_run(
        id=run_id,
        status="paused",
        reproducibility_manifest={
            "pending_review": {
                "run_id": str(run_id),
                "step_index": 1,
                "stage_type": "screen",
                "review_kind": "screening",
                "contract_version": 1,
                "output_hash": "a" * 64,
                "status": "pending",
                "created_at": "2026-09-27T12:00:00+00:00",
            }
        },
    )
    db = AsyncMock()
    current_user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

    with patch(
        "src.api.research_engine.runs._get_owned_run",
        new=AsyncMock(return_value=run),
    ):
        with pytest.raises(HTTPException) as error:
            await resume_run(run_id, RunResumeRequest(), current_user, db)

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "review_required"
    assert error.value.detail["descriptor"] == {
        "pause_reason": "review_required",
        "review_kind": "screening",
        "step_index": 1,
        "output_hash": "a" * 64,
    }
    db.commit.assert_not_awaited()


def test_run_response_reloads_content_free_pause_descriptor():
    """Reloading GET /runs must expose the durable gate without stage content."""

    run_id = uuid.uuid4()
    run = _make_run(
        id=run_id,
        status="paused",
        reproducibility_manifest={
            "pending_review": {
                "run_id": str(run_id),
                "step_index": 2,
                "stage_type": "extract",
                "review_kind": "extraction",
                "contract_version": 1,
                "output_hash": "b" * 64,
                "status": "pending",
                "created_at": "2026-09-27T12:00:00+00:00",
            },
            "private_report": "must not be serialized",
        },
    )

    response = RunResponse.model_validate(run).model_dump(mode="json")

    assert response["pause_reason"] == "review_required"
    assert response["review_kind"] == "extraction"
    assert response["step_index"] == 2
    assert response["output_hash"] == "b" * 64
    assert "reproducibility_manifest" not in response
    assert "private_report" not in str(response)


async def _preexecution_stream_fixture(
    *,
    status_value: str,
) -> tuple[uuid.UUID, Any, Any, Any]:
    run_id = uuid.uuid4()
    blueprint_id = uuid.uuid4()
    manifest: dict[str, Any] = {"parameters_override": {}}
    if status_value == "paused":
        manifest["resume_authorization"] = {
            "kind": "continue_unverified",
            "actor_id": str(uuid.uuid4()),
            "step_index": 2,
            "output_hash": "a" * 64,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "consumed_at": None,
        }
    run = _make_run(
        id=run_id,
        blueprint_id=blueprint_id,
        status=status_value,
        reproducibility_manifest=manifest,
    )
    blueprint = _make_blueprint(id=blueprint_id)
    blueprint_result = Mock()
    blueprint_result.scalars.return_value.first.return_value = blueprint
    last_step_result = Mock()
    last_step_result.scalars.return_value.first.return_value = None
    history_result = Mock()
    history_result.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[blueprint_result, last_step_result, history_result]
    )
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    db.add = Mock()
    return run_id, run, blueprint, db


@pytest.mark.asyncio
async def test_missing_organization_fails_before_pending_stream_claim():
    run_id, run, _blueprint, db = await _preexecution_stream_fixture(
        status_value="pending"
    )
    user = SimpleNamespace(id=uuid.uuid4(), organization_id=None)

    with (
        patch(
            "src.api.research_engine.runs._get_owned_run",
            new=AsyncMock(return_value=run),
        ),
        patch("src.api.research_engine.runs.admit_expensive_work") as admit,
        patch("src.api.research_engine.runs.WorkflowEngine") as engine_cls,
    ):
        with pytest.raises(HTTPException) as error:
            await stream_run(run_id, user, db)

    assert error.value.status_code == 403
    assert run.status == "pending"
    assert run.started_at is None
    admit.assert_not_awaited()
    engine_cls.assert_not_called()


@pytest.mark.asyncio
async def test_connector_construction_fails_before_pending_stream_claim():
    run_id, run, _blueprint, db = await _preexecution_stream_fixture(
        status_value="pending"
    )
    user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

    with (
        patch(
            "src.api.research_engine.runs._get_owned_run",
            new=AsyncMock(return_value=run),
        ),
        patch(
            "src.api.research_engine.runs._build_connectors",
            side_effect=RuntimeError("connector setup failed"),
        ),
        patch("src.api.research_engine.runs.admit_expensive_work") as admit,
        patch("src.api.research_engine.runs.WorkflowEngine") as engine_cls,
    ):
        with pytest.raises(RuntimeError, match="connector setup failed"):
            await stream_run(run_id, user, db)

    assert run.status == "pending"
    assert run.started_at is None
    admit.assert_not_awaited()
    engine_cls.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_value", ["pending", "paused"])
async def test_admission_exception_restores_stream_claim_for_retry(status_value: str):
    run_id, run, _blueprint, db = await _preexecution_stream_fixture(
        status_value=status_value
    )
    user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

    with (
        patch(
            "src.api.research_engine.runs._get_owned_run",
            new=AsyncMock(return_value=run),
        ),
        patch("src.api.research_engine.runs._build_connectors", return_value={}),
        patch(
            "src.api.research_engine.runs.admit_expensive_work",
            new=AsyncMock(side_effect=RuntimeError("admission backend failed")),
        ),
        patch("src.api.research_engine.runs.WorkflowEngine") as engine_cls,
    ):
        with pytest.raises(RuntimeError, match="admission backend failed"):
            await stream_run(run_id, user, db)

    assert run.status == status_value
    assert run.started_at is None
    if status_value == "paused":
        authorization = run.reproducibility_manifest["resume_authorization"]
        assert authorization["output_hash"] == "a" * 64
        assert authorization["consumed_at"] is None
    engine_cls.assert_not_called()


@pytest.mark.asyncio
async def test_committed_verification_gate_wins_user_pause_and_sse_has_no_context():
    run_id = uuid.uuid4()
    blueprint_id = uuid.uuid4()
    run = _make_run(id=run_id, blueprint_id=blueprint_id, status="pending")
    blueprint = _make_blueprint(
        id=blueprint_id,
        steps=[{"id": "verify", "type": "verify", "parameters": {}}],
    )
    db = _mock_db_returning(run_result=run, blueprint_result=blueprint)
    user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
    refresh_count = 0

    async def refresh_with_pause(_obj):
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count >= 2:
            manifest = dict(run.reproducibility_manifest or {})
            manifest["_pause_requested"] = True
            run.reproducibility_manifest = manifest

    db.refresh = AsyncMock(side_effect=refresh_with_pause)
    output = {
        "contract_version": 1,
        "stage_type": "verify",
        "usage": {"model_calls": 1, "total_tokens": 5, "batches": []},
        "verification": {
            "passed": False,
            "deterministic_passed": True,
            "schema_passed": True,
            "semantic_status": "failed",
            "coverage_complete": True,
            "claims": [],
        },
        "processing_coverage": {},
    }

    async def engine_run(**_kwargs):
        yield {
            "event": "step_complete",
            "step_index": 0,
            "step_id": "verify",
            "step_type": "verify",
            "output": output,
            "outputs_hash": canonical_stage_output_hash(output),
            "quality_marks": [{"check_type": "semantic_verification", "passed": False}],
            "token_count": 5,
        }
        yield {
            "event": "run_paused",
            "reason": "Quality check failed",
            "context": {"private_research": "must never reach SSE"},
        }

    with (
        patch("src.api.research_engine.runs._build_connectors", return_value={}),
        patch(
            "src.api.research_engine.runs.admit_expensive_work",
            new=AsyncMock(return_value=True),
        ),
        patch("src.api.research_engine.runs.WorkflowEngine") as engine_cls,
        patch("src.api.research_engine.runs.StepExecutor"),
    ):
        engine = Mock()
        engine.run = engine_run
        engine_cls.return_value = engine
        response = await stream_run(run_id, user, db)
        chunks = [
            chunk.decode() if isinstance(chunk, bytes) else chunk
            async for chunk in response.body_iterator
        ]

    body = "".join(chunks)
    pause_payloads = [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ") and '"event": "run_paused"' in line
    ]
    assert pause_payloads == [
        {
            "event": "run_paused",
            "run_id": str(run_id),
            "pause_reason": "verification_failed",
            "step_index": 0,
            "output_hash": canonical_stage_output_hash(output),
        }
    ]
    assert "private_research" not in body
    assert run.status == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage_type", "review_gate", "terminal_reason", "output"),
    [
        (
            "screen",
            "screening",
            "empty_screening",
            {
                "contract_version": 1,
                "stage_type": "screen",
                "usage": {"model_calls": 1, "total_tokens": 5, "batches": []},
                "screening": [],
                "included_source_ids": [],
                "processing_coverage": {},
            },
        ),
        (
            "extract",
            "extraction",
            "empty_extraction",
            {
                "contract_version": 1,
                "stage_type": "extract",
                "usage": {"model_calls": 1, "total_tokens": 5, "batches": []},
                "extractions": [
                    {
                        "source_id": "source-1",
                        "part_id": "p0001",
                        "data": {"finding": None},
                        "evidence": [],
                    }
                ],
                "processing_coverage": {},
            },
        ),
    ],
)
async def test_zero_evidence_route_commits_terminal_audit_without_review(
    stage_type: str,
    review_gate: str,
    terminal_reason: str,
    output: dict[str, Any],
):
    run_id = uuid.uuid4()
    blueprint_id = uuid.uuid4()
    run = _make_run(id=run_id, blueprint_id=blueprint_id, status="pending")
    blueprint = _make_blueprint(
        id=blueprint_id,
        steps=[
            {
                "id": stage_type,
                "type": stage_type,
                "parameters": {"review_gate": review_gate},
            },
            {"id": "must-not-run", "type": "export"},
        ],
    )
    db = _mock_db_returning(run_result=run, blueprint_result=blueprint)
    user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

    async def engine_run(**_kwargs):
        yield {
            "event": "step_complete",
            "step_index": 0,
            "step_id": stage_type,
            "step_type": stage_type,
            "output": output,
            "outputs_hash": canonical_stage_output_hash(output),
            "quality_marks": [],
            "token_count": 5,
        }
        yield {"event": "step_start", "step_index": 1, "step_id": "must-not-run"}

    with (
        patch("src.api.research_engine.runs._build_connectors", return_value={}),
        patch(
            "src.api.research_engine.runs.admit_expensive_work",
            new=AsyncMock(return_value=True),
        ),
        patch("src.api.research_engine.runs.WorkflowEngine") as engine_cls,
        patch("src.api.research_engine.runs.StepExecutor"),
    ):
        engine = Mock()
        engine.run = engine_run
        engine_cls.return_value = engine
        response = await stream_run(run_id, user, db)
        chunks = [
            chunk.decode() if isinstance(chunk, bytes) else chunk
            async for chunk in response.body_iterator
        ]

    body = "".join(chunks)
    assert '"final_status": "no_evidence"' in body
    assert "must-not-run" not in body
    assert "event: run_paused" not in body
    assert run.status == "completed"
    assert run.reproducibility_manifest["final_status"] == "no_evidence"
    audit = run.reproducibility_manifest["terminal_audit"]
    assert audit["reason"] == terminal_reason
    assert audit["output_hash"] == canonical_stage_output_hash(output)
    added = [call.args[0] for call in db.add.call_args_list]
    assert not any(isinstance(item, ResearchStageReview) for item in added)


def test_run_response_reloads_user_pause_descriptor():
    run = _make_run(
        status="paused",
        reproducibility_manifest={
            "user_pause": {
                "step_index": -1,
                "output_hash": "c" * 64,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )

    response = RunResponse.model_validate(run).model_dump(mode="json")

    assert response["pause_reason"] == "user_paused"
    assert response["review_kind"] is None
    assert response["step_index"] == -1
    assert response["output_hash"] == "c" * 64


@pytest.mark.asyncio
async def test_review_overlays_resume_for_any_edit_member_without_owner_scope() -> None:
    """GOO-404 (f): overlays must not be re-scoped to ResearchProject.owner_id.

    Passing owner_id here made every non-creator EDIT member hit
    409 review_overlay_reconstruction_failed once review_history existed.
    """
    run_id = uuid.uuid4()
    bp_id = uuid.uuid4()
    run = _make_run(
        id=run_id,
        blueprint_id=bp_id,
        status="paused",
        reproducibility_manifest={"review_history": [{"review_id": str(uuid.uuid4())}]},
    )
    blueprint = _make_blueprint(id=bp_id)
    blueprint_result = Mock()
    blueprint_result.scalars.return_value.first.return_value = blueprint
    last_step_result = Mock()
    last_step_result.scalars.return_value.first.return_value = None
    history_result = Mock()
    history_result.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[blueprint_result, last_step_result, history_result]
    )
    member = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
    overlays = AsyncMock(
        side_effect=lambda **kwargs: {
            **kwargs["context"],
            "approved_review_overlays": [],
        }
    )

    class _StopAfterOverlays(Exception):
        pass

    with (
        patch(
            "src.api.research_engine.runs._get_owned_run",
            new=AsyncMock(return_value=run),
        ),
        patch.object(ResearchReviewService, "apply_approved_overlays", overlays),
        patch(
            "src.api.research_engine.runs._build_providers",
            side_effect=_StopAfterOverlays(),
        ),
    ):
        with pytest.raises(_StopAfterOverlays):
            await stream_run(run_id, cast(User, member), db)

    overlays.assert_awaited_once()
    assert overlays.await_args is not None
    assert set(overlays.await_args.kwargs) == {"run_id", "context"}
    assert overlays.await_args.kwargs["run_id"] == run_id

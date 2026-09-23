"""
Unit tests for the Research Engine SSE streaming endpoint.

Tests cover:
- GET /runs/{run_id}/stream returns 200 with SSE content-type
- 404 returned for non-existent run
- 409 returned for run not in PENDING or PAUSED status
- Proper SSE headers (Cache-Control, X-Accel-Buffering)
- SSE events are yielded in correct event:/data: format
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.research_engine.runs import pause_run, router, stream_run
from src.core.database import get_db
from src.core.dependencies import get_current_user

# ============================================================================
# Helpers
# ============================================================================


def _make_run(**overrides):
    """Create a mock ResearchRun ORM object."""
    now = datetime.now(timezone.utc)
    run = Mock()
    run.id = overrides.get("id", uuid.uuid4())
    run.blueprint_id = overrides.get("blueprint_id", uuid.uuid4())
    run.blueprint_version = overrides.get("blueprint_version", 1)
    run.status = overrides.get("status", "pending")
    run.started_at = overrides.get("started_at", None)
    run.completed_at = overrides.get("completed_at", None)
    run.total_tokens = overrides.get("total_tokens", 0)
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


def _mock_db_returning(
    run_result=None,
    blueprint_result=None,
    project_result=None,
    last_step_index=None,
):
    """Build an AsyncMock DB that returns expected query results.

    After C2 fix, _get_owned_run uses a single JOIN query that returns
    the run directly (or None if not owned). For the stream endpoint:
    - Query 1: _get_owned_run (single JOIN) -> run_result
    - Query 2: get blueprint (for streaming) -> blueprint_result
    - Query 3: get last completed step (for paused runs)
    """
    db = AsyncMock()
    results = []

    # Query 1: _get_owned_run JOIN query
    run_mock = Mock()
    run_mock.scalars.return_value.first.return_value = run_result
    results.append(run_mock)

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
                step_mock.scalars.return_value.first.return_value = last_step
            results.append(step_mock)

            # R5-M18 claim UPDATE → rowcount=1
            claim_mock = Mock()
            claim_mock.rowcount = 1
            results.append(claim_mock)

            # R5-M19 prior-outputs history SELECT
            history_mock = Mock()
            history_mock.scalars.return_value.all.return_value = []
            results.append(history_mock)

    db.execute = AsyncMock(side_effect=results)
    # commit/refresh are no-ops in tests
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = Mock()
    return db


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
        mock_run = _make_run(id=run_id, blueprint_id=bp_id, status="paused")
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
            }
            yield {"event": "run_complete", "run_id": str(run_id), "context": {}}

        response = self._patch_engine_and_get(
            stream_app, stream_client, run_id, mock_engine_run
        )

        assert response.status_code == 200
        db.add.assert_called()
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

        response = self._patch_engine_and_get(
            stream_app, stream_client, run_id, engine_run
        )
        assert "event: run_complete" in response.text
        added = [call.args[0] for call in db.add.call_args_list]
        sources = [row for row in added if isinstance(row, ResearchSource)]
        assert len(sources) == 1
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
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
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

    def test_resume_keeps_paused_status_for_step_index_recovery(
        self, stream_app, stream_client
    ):
        run_id = uuid.uuid4()
        run = _make_run(id=run_id, status="paused")
        db = AsyncMock()
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
        assert body["status"] == "running"
        stream_app.dependency_overrides.pop(get_db, None)

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

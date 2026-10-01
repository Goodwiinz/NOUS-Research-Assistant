"""R5-H7: `next(get_db())` on an async, request-scoped generator.

`get_db` was `async def get_db(request: Request)` (src/core/database.py) —
calling it with no arguments raised `TypeError` immediately (missing
`request`), before `next()` even ran. (It is still an async request-scoped
generator, so `next()` on it can never yield a sync session.) `evaluate_search_pipeline` and
`get_evaluation_metrics` (rag_evaluation_service.py) both ran from sync
Celery task contexts (via `loop.run_until_complete`) and called `next(get_db())`
to get a session — the TypeError was swallowed by a bare `except Exception:
continue` in the caller, so every item in a batch evaluation silently
produced nothing: batch jobs were always FAILED with "No queries were
successfully evaluated", and triad jobs with no provided context always saw
empty generated_answer/retrieved_context.

The fix uses `SessionLocal()` (the same sync session factory every Celery
task in this codebase already uses) instead.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_evaluate_search_pipeline_uses_a_sync_session_per_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.models.search_schemas import SearchResponse, SearchType
    from src.services.evaluation import rag_evaluation_service as svc

    service = svc.RAGEvaluationService()

    fake_search_response = SearchResponse(
        query="q",
        search_id="s1",
        search_type=SearchType.HYBRID,
        results=[],
        total_results=0,
        returned_results=0,
        search_time_ms=1.0,
        limit=5,
        offset=0,
        has_more=False,
    )
    monkeypatch.setattr(
        svc.hybrid_search_service,
        "search",
        MagicMock(return_value=fake_search_response),
    )

    created_sessions: list[Any] = []

    def _fake_session_local() -> Any:
        session = MagicMock()
        created_sessions.append(session)
        return session

    monkeypatch.setattr(svc, "SessionLocal", _fake_session_local)

    captured_metrics: list[Any] = []

    async def _fake_run_rag_triad_evaluation(
        evaluation_input: Any, job_id: Any, organization_id: Any, db: Any
    ) -> Any:
        # The session handed to the metric-computation call must be one of
        # ours (SessionLocal()), not something next(get_db()) would have
        # produced (a TypeError, never a session at all).
        assert db in created_sessions
        result = MagicMock()
        result.metadata = evaluation_input.metadata
        captured_metrics.append(result)
        return result

    monkeypatch.setattr(
        service, "run_rag_triad_evaluation", _fake_run_rag_triad_evaluation
    )

    results = await service.evaluate_search_pipeline(
        queries=["what is x?"],
        organization_id="org-1",
        user_id="user-1",
    )

    assert len(results) == 1
    assert created_sessions, "expected a SessionLocal() session to be created"
    created_sessions[0].close.assert_called_once()


def test_get_evaluation_metrics_uses_a_sync_session_when_none_is_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.evaluation import rag_evaluation_service as svc

    service = svc.RAGEvaluationService()

    session = MagicMock()
    session.query.return_value.join.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = (
        []
    )
    monkeypatch.setattr(svc, "SessionLocal", MagicMock(return_value=session))

    result = service.get_evaluation_metrics(
        job_id="job-1", organization_id="org-1", db=None
    )

    assert result == []
    session.close.assert_called_once()

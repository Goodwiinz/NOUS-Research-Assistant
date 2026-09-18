"""Security regression tests for shared expensive-work admission."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.api.arxiv.core import (
    ArXivIngestRequest,
    EvaluationDatasetRequest,
    create_evaluation_dataset,
    ingest_arxiv_papers,
)
from src.api.research_engine.runs import stream_run
from src.services.expensive_work_admission import admit_expensive_work


def _user():
    return SimpleNamespace(id=uuid4(), organization_id=uuid4())


def test_ingest_caps_ids_and_rejects_untrusted_id_values():
    with pytest.raises(ValueError):
        ArXivIngestRequest(paper_ids=["2401.00001"] * 51)
    with pytest.raises(ValueError):
        ArXivIngestRequest(paper_ids=["not-an-arxiv-id"])


def test_dataset_caps_aggregate_question_work():
    with pytest.raises(ValueError):
        EvaluationDatasetRequest(num_papers=200, questions_per_paper=10)


@pytest.mark.asyncio
async def test_missing_verified_organization_is_rejected_before_enqueue():
    user = SimpleNamespace(id=uuid4(), organization_id=None, organization=None)
    background_tasks = BackgroundTasks()

    with pytest.raises(HTTPException) as exc_info:
        await ingest_arxiv_papers(
            ArXivIngestRequest(paper_ids=["2401.00001"]), background_tasks, user
        )

    assert exc_info.value.status_code == 403
    assert not background_tasks.tasks


@pytest.mark.asyncio
async def test_ingest_and_dataset_use_same_shared_admission_key(monkeypatch):
    user = _user()
    background_tasks = BackgroundTasks()
    calls = []

    async def fake_admission(*, user_id, organization_id):
        calls.append((user_id, organization_id))
        return True

    monkeypatch.setattr("src.api.arxiv.core.admit_expensive_work", fake_admission)

    await ingest_arxiv_papers(
        ArXivIngestRequest(paper_ids=["2401.00001"]), background_tasks, user
    )
    await create_evaluation_dataset(EvaluationDatasetRequest(), background_tasks, user)

    assert calls == [(user.id, user.organization_id), (user.id, user.organization_id)]
    assert len(background_tasks.tasks) == 2
    assert background_tasks.tasks[0].kwargs["organization_id"] == user.organization_id
    assert background_tasks.tasks[1].kwargs["organization_id"] == user.organization_id


@pytest.mark.asyncio
async def test_repeated_ingest_is_rejected_before_enqueue(monkeypatch):
    user = _user()
    background_tasks = BackgroundTasks()
    limiter = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr("src.api.arxiv.core.admit_expensive_work", limiter)

    await ingest_arxiv_papers(
        ArXivIngestRequest(paper_ids=["2401.00001"]), background_tasks, user
    )
    with pytest.raises(HTTPException) as exc_info:
        await ingest_arxiv_papers(
            ArXivIngestRequest(paper_ids=["2401.00002"]), background_tasks, user
        )

    assert exc_info.value.status_code == 429
    assert len(background_tasks.tasks) == 1


@pytest.mark.asyncio
async def test_stream_claims_before_shared_admission(monkeypatch):
    run = SimpleNamespace(
        id=uuid4(),
        blueprint_id=uuid4(),
        status="pending",
        total_tokens=0,
        started_at=None,
        reproducibility_manifest=None,
    )
    blueprint = SimpleNamespace(steps=[], parameters={}, version=1)
    blueprint_result = Mock()
    blueprint_result.scalars.return_value.first.return_value = blueprint
    last_step_result = Mock()
    last_step_result.scalars.return_value.first.return_value = None
    claim_result = Mock()
    claim_result.rowcount = 1
    db = AsyncMock()
    db_calls = 0
    events = []

    async def execute(_statement):
        nonlocal db_calls
        events.append("db")
        result = [blueprint_result, last_step_result, claim_result][db_calls]
        db_calls += 1
        return result

    db.execute = AsyncMock(side_effect=execute)
    user = _user()

    async def admit(*, user_id, organization_id):
        events.append("admission")
        return True

    monkeypatch.setattr("src.api.research_engine.runs.admit_expensive_work", admit)
    with patch(
        "src.api.research_engine.runs._get_owned_run", AsyncMock(return_value=run)
    ):
        response = await stream_run(run.id, user, db)

    assert response.status_code == 200
    assert db.execute.await_count == 3
    assert events == ["db", "db", "db", "admission"]


@pytest.mark.asyncio
async def test_stream_denied_admission_releases_claim(monkeypatch):
    run = SimpleNamespace(
        id=uuid4(),
        blueprint_id=uuid4(),
        status="pending",
        total_tokens=0,
        started_at=None,
        reproducibility_manifest=None,
    )
    blueprint = SimpleNamespace(steps=[], parameters={}, version=1)
    blueprint_result = Mock()
    blueprint_result.scalars.return_value.first.return_value = blueprint
    last_step_result = Mock()
    last_step_result.scalars.return_value.first.return_value = None
    claim_result = Mock()
    claim_result.rowcount = 1
    rollback_result = Mock()
    rollback_result.rowcount = 1
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[blueprint_result, last_step_result, claim_result, rollback_result]
    )
    user = _user()

    monkeypatch.setattr(
        "src.api.research_engine.runs.admit_expensive_work",
        AsyncMock(return_value=False),
    )
    with patch(
        "src.api.research_engine.runs._get_owned_run", AsyncMock(return_value=run)
    ):
        with pytest.raises(HTTPException) as exc_info:
            await stream_run(run.id, user, db)

    assert exc_info.value.status_code == 429
    assert db.execute.await_count == 4


@pytest.mark.asyncio
async def test_expired_background_ingest_does_not_start_external_work(monkeypatch):
    from src.api.arxiv import core

    work = AsyncMock()
    monkeypatch.setattr(core, "_process_arxiv_ingestion_work", work)
    await core._process_arxiv_ingestion(
        paper_ids=["2401.00001"],
        user_id="user-1",
        organization_id="org-1",
        download_pdfs=True,
        extract_content=True,
        batch_size=1,
        deadline=0,
    )

    work.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_ingest_rechecks_actor_scope(monkeypatch):
    from src.api.arxiv import core

    work = AsyncMock()
    monkeypatch.setattr(core, "_process_arxiv_ingestion_work", work)
    await core._process_arxiv_ingestion(
        paper_ids=["2401.00001"],
        user_id="user-1",
        organization_id=None,
        download_pdfs=True,
        extract_content=True,
        batch_size=1,
    )

    work.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_ingest_rechecks_paper_ids(monkeypatch):
    from src.api.arxiv import core

    work = AsyncMock()
    monkeypatch.setattr(core, "_process_arxiv_ingestion_work", work)
    await core._process_arxiv_ingestion(
        paper_ids=["not-an-arxiv-id"],
        user_id="user-1",
        organization_id="org-1",
        download_pdfs=True,
        extract_content=True,
        batch_size=1,
    )

    work.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_ingest_applies_default_deadline(monkeypatch):
    from src.api.arxiv import core

    work = AsyncMock()
    recorded_timeouts = []

    async def wait_for(awaitable, *, timeout):
        recorded_timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr(core, "_process_arxiv_ingestion_work", work)
    monkeypatch.setattr(core.asyncio, "wait_for", wait_for)

    await core._process_arxiv_ingestion(
        paper_ids=["2401.00001"],
        user_id="user-1",
        organization_id="org-1",
        download_pdfs=True,
        extract_content=True,
        batch_size=1,
    )

    assert len(recorded_timeouts) == 1
    assert 0 < recorded_timeouts[0] <= core.ARXIV_BACKGROUND_DEADLINE_SECONDS


@pytest.mark.asyncio
async def test_shared_admission_key_is_organization_aggregate(monkeypatch):
    limiter = AsyncMock()
    limiter.is_allowed.return_value = (True, {})
    monkeypatch.setattr("src.services.expensive_work_admission._limiter", limiter)

    assert await admit_expensive_work(user_id="user-1", organization_id="org-1")

    assert limiter.is_allowed.await_args.kwargs["identifier"] == "org:org-1"

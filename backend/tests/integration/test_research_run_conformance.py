"""Real PostgreSQL approved-plan execution and drift rejection."""

import asyncio
from types import SimpleNamespace
from typing import Any, AsyncIterator, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.api.research_engine import runs
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_protocol import ResearchProtocolVersion
from src.models.research_run import ResearchRun
from src.models.research_step import ResearchStep
from src.models.user import User
from src.schemas.research_engine import ResearchProtocolVersionCreate, RunCreate
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.services.research_engine.protocol_service import add_protocol_version
from src.services.research_engine.run_conformance import require_run_conformance
from src.services.research_engine.search_receipts import SearchReceiptJournal
from tests.integration.test_protocol_approval_atomicity import (  # noqa: F401
    PROTOCOL_SNAPSHOT,
    _approve_path,
    _client,
    _seed,
    protocol_engine,
)


async def _approved_plan(engine: AsyncEngine) -> dict[str, UUID]:
    ids = await _seed(engine)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        blueprint = await db.get(ResearchBlueprint, ids["blueprint"])
        assert blueprint is not None
        blueprint.steps = [
            {"type": "search", "name": "Search", "parameters": {"query": "evidence"}}
        ]
        blueprint.parameters = {"sources": ["arxiv"], "limit": 5}
        await db.commit()
        await resolve_project(db, ids["collection"], ids["author"], ResearchAction.EDIT)
        protocol = await add_protocol_version(
            db,
            ids["protocol"],
            ids["collection"],
            ids["author"],
            ResearchProtocolVersionCreate(
                question_version_id=ids["question_version"],
                blueprint_id=ids["blueprint"],
                snapshot=PROTOCOL_SNAPSHOT,
                parent_version_id=ids["protocol_version"],
                amendment_reason="Bind a runnable search plan",
            ),
        )
        ids["protocol_version"] = protocol.current_draft_version_id
        version = await db.get(ResearchProtocolVersion, ids["protocol_version"])
        assert version is not None
        body = {
            "expected_protocol_version": version.version,
            "expected_content_hash": version.content_hash,
            "expected_current_approved_version_id": None,
            "reason": "Independent methods review",
            "idempotency_key": "run-plan-review",
        }
    async with _client(engine, ids["supervisor"]) as client:
        response = await client.post(_approve_path(ids), json=body)
        assert response.status_code == 200, response.text
    return ids


def _actor(ids: dict[str, UUID]) -> User:
    return cast(User, SimpleNamespace(id=ids["author"], organization_id=ids["org"]))


@pytest.mark.asyncio
async def test_run_binds_approved_plan_and_rejects_mutated_blueprint(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _approved_plan(protocol_engine)
    async with AsyncSession(protocol_engine, expire_on_commit=False) as db:
        response = await runs.start_run(
            ids["blueprint"],
            RunCreate(protocol_version_id=ids["protocol_version"]),
            _actor(ids),
            db,
        )
        assert response.protocol_version_id == ids["protocol_version"]
        assert response.conformance_status == "plan_verified"
        assert response.effective_plan_hash and len(response.effective_plan_hash) == 64
        run_id = response.id
    async with AsyncSession(protocol_engine, expire_on_commit=False) as db:
        # This is the route's pre-lock identity map: another transaction may
        # change the plan while this request waits for the project lock.
        cached_blueprint = await db.get(ResearchBlueprint, ids["blueprint"])
        assert cached_blueprint is not None
        async with AsyncSession(protocol_engine, expire_on_commit=False) as writer:
            blueprint = await writer.get(ResearchBlueprint, ids["blueprint"])
            assert blueprint is not None
            blueprint.parameters = {"sources": ["semantic_scholar"], "limit": 50}
            await writer.commit()
        with pytest.raises(HTTPException, match="amendment"):
            await runs.start_run(
                ids["blueprint"],
                RunCreate(protocol_version_id=ids["protocol_version"]),
                _actor(ids),
                db,
            )
        await db.rollback()
        run = await db.get(ResearchRun, run_id)
        blueprint = await db.get(ResearchBlueprint, ids["blueprint"])
        context = await resolve_project(
            db, ids["collection"], ids["author"], ResearchAction.EDIT
        )
        assert run is not None and blueprint is not None
        with pytest.raises(HTTPException, match="amendment"):
            await require_run_conformance(db, run, blueprint, context)


@pytest.mark.asyncio
async def test_missing_approval_and_overrides_fail_before_run_is_created(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _approved_plan(protocol_engine)
    async with AsyncSession(protocol_engine, expire_on_commit=False) as db:
        for body in (
            RunCreate(),
            RunCreate(
                protocol_version_id=ids["protocol_version"],
                parameters_override={"sources": ["changed"]},
            ),
        ):
            with pytest.raises(HTTPException) as denied:
                await runs.start_run(ids["blueprint"], body, _actor(ids), db)
            assert denied.value.status_code == 409
            await db.rollback()
        persisted = (await db.execute(select(ResearchRun.id))).scalars().all()
        assert persisted == [ids["legacy_run"]]


@pytest.mark.asyncio
async def test_stream_uses_saved_plan_and_persists_conformant_completion(
    protocol_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _approved_plan(protocol_engine)
    seen: list[dict[str, Any]] = []

    class ObservedEngine:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(
            self, *, blueprint: dict[str, Any], **kwargs: Any
        ) -> AsyncIterator[dict[str, Any]]:
            seen.append(blueprint)
            yield {
                "event": "step_complete",
                "step_index": 0,
                "step_type": "search",
                "output": {},
                "token_count": 7,
            }
            yield {"event": "run_complete"}

    monkeypatch.setattr(runs, "WorkflowEngine", ObservedEngine)
    monkeypatch.setattr(runs, "_build_providers", lambda steps: {})
    monkeypatch.setattr(runs, "_build_connectors", lambda **kwargs: {})
    monkeypatch.setattr(runs, "admit_expensive_work", AsyncMock(return_value=True))
    async with AsyncSession(protocol_engine, expire_on_commit=False) as db:
        created = await runs.start_run(
            ids["blueprint"],
            RunCreate(protocol_version_id=ids["protocol_version"]),
            _actor(ids),
            db,
        )
        stream = await runs.stream_run(created.id, _actor(ids), db)
        events = [event async for event in stream.body_iterator]
        assert any("run_complete" in str(event) for event in events), events
    async with AsyncSession(protocol_engine) as db:
        run = await db.get(ResearchRun, created.id)
        version = await db.get(ResearchProtocolVersion, ids["protocol_version"])
        assert run is not None and version is not None
        assert run.status == "completed" and run.conformance_status == "conformant"
        assert run.total_tokens == 7
        assert run.reproducibility_manifest["protocol_version_id"] == str(version.id)
        assert seen == [
            {
                "steps": version.execution_plan["steps"],
                "parameters": version.execution_plan["parameters"],
            }
        ]
        steps = (
            (
                await db.execute(
                    select(ResearchStep).where(ResearchStep.run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(steps) == 1 and steps[0].token_count == 7


@pytest.mark.asyncio
async def test_tampered_override_and_legacy_run_do_not_reach_paid_work(
    protocol_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _approved_plan(protocol_engine)
    admission = AsyncMock(return_value=True)
    monkeypatch.setattr(runs, "admit_expensive_work", admission)
    async with AsyncSession(protocol_engine, expire_on_commit=False) as db:
        created = await runs.start_run(
            ids["blueprint"],
            RunCreate(protocol_version_id=ids["protocol_version"]),
            _actor(ids),
            db,
        )
        run = await db.get(ResearchRun, created.id)
        assert run is not None
        run.reproducibility_manifest = {"parameters_override": {"limit": 999}}
        await db.commit()
        for run_id in (created.id, ids["legacy_run"]):
            with pytest.raises(HTTPException) as denied:
                await runs.stream_run(run_id, _actor(ids), db)
            assert denied.value.status_code == 409
            await db.rollback()
    admission.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_allows_superseded_plan_but_rejects_deviated_run(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _approved_plan(protocol_engine)
    original_version_id = ids["protocol_version"]
    async with AsyncSession(protocol_engine, expire_on_commit=False) as db:
        created = await runs.start_run(
            ids["blueprint"],
            RunCreate(protocol_version_id=original_version_id),
            _actor(ids),
            db,
        )
        run = await db.get(ResearchRun, created.id)
        assert run is not None
        run.status = "paused"
        await db.commit()

        protocol = await add_protocol_version(
            db,
            ids["protocol"],
            ids["collection"],
            ids["author"],
            ResearchProtocolVersionCreate(
                question_version_id=ids["question_version"],
                blueprint_id=ids["blueprint"],
                snapshot={
                    **PROTOCOL_SNAPSHOT,
                    "outcomes": {"primary": "amended outcome"},
                },
                parent_version_id=original_version_id,
                amendment_reason="Approve a later protocol without rewriting old runs",
            ),
        )
        replacement_id = protocol.current_draft_version_id
        replacement = await db.get(ResearchProtocolVersion, replacement_id)
        assert replacement is not None
        approval = {
            "expected_protocol_version": replacement.version,
            "expected_content_hash": replacement.content_hash,
            "expected_current_approved_version_id": str(original_version_id),
            "reason": "Approve superseding protocol",
            "idempotency_key": "resume-supersession",
        }
    ids["protocol_version"] = replacement_id
    async with _client(protocol_engine, ids["supervisor"]) as client:
        approved = await client.post(_approve_path(ids), json=approval)
    assert approved.status_code == 200, approved.text

    async with AsyncSession(protocol_engine, expire_on_commit=False) as db:
        resumed = await runs.resume_run(created.id, _actor(ids), db)
        assert resumed.status == "running"
        assert resumed.protocol_version_id == original_version_id
        run = await db.get(ResearchRun, created.id)
        assert run is not None
        run.status = "paused"
        run.conformance_status = "deviated"
        await db.commit()
        with pytest.raises(HTTPException) as denied:
            await runs.resume_run(created.id, _actor(ids), db)
        assert denied.value.status_code == 409


@pytest.mark.asyncio
async def test_search_page_receipts_survive_reopen_and_concurrent_replay(
    protocol_engine: AsyncEngine,
) -> None:
    ids = await _approved_plan(protocol_engine)
    async with AsyncSession(protocol_engine, expire_on_commit=False) as db:
        created = await runs.start_run(
            ids["blueprint"],
            RunCreate(protocol_version_id=ids["protocol_version"]),
            _actor(ids),
            db,
        )
        run_id = created.id

    strategy = {
        "schema_version": "nous.academic.search-strategy.v1",
        "project_id": str(ids["collection"]),
        "protocol_version_id": str(ids["protocol_version"]),
        "strategy_version": "sha256:" + "a" * 64,
    }
    execution_id = "5c4e80a8-22bc-4f0b-8803-7c4c8db3917f"
    page_id = "54c48b54-54a0-5275-9466-89ed31731170"
    second_page_id = "0f3c8d1a-9b52-5f7e-8a41-2d6c3e9b7a10"

    def page(
        attempt_id: str, status: str, query: str, *, index: int = 0
    ) -> dict[str, Any]:
        return {
            "page_id": page_id if index == 0 else second_page_id,
            "page_index": index,
            "attempt_id": attempt_id,
            "page_status": status,
            "request": {"method": "GET", "params": {"query": query}},
            "response": {
                "status_code": 200 if status == "completed" else None,
                "parsed_count": 1 if status == "completed" else 0,
            },
            "record_keys": [],
            "imported_count": 0,
            "imported_source_ids": [],
        }

    async def persist(page_value: dict[str, Any]) -> None:
        async with AsyncSession(protocol_engine, expire_on_commit=False) as db:
            await SearchReceiptJournal(db, run_id, ids["author"]).persist_page(
                step_id="search",
                strategy=strategy,
                provider="openalex",
                execution_id=execution_id,
                page=page_value,
            )

    # Distinct concurrent pages: without the run-row lock one read-modify-write
    # overwrites the other and a page disappears. Mutation guard: drop
    # with_for_update in SearchReceiptJournal._locked_run and this must fail.
    await asyncio.gather(
        persist(page("attempt-1", "requested", "approved query")),
        persist(page("attempt-1", "requested", "approved query", index=1)),
    )
    await persist(page("attempt-1", "completed", "approved query"))
    await persist(page("attempt-2", "requested", "retry query"))

    async with AsyncSession(protocol_engine, expire_on_commit=False) as db:
        run = await db.get(ResearchRun, run_id)
        assert run is not None
        journal = run.reproducibility_manifest["_search_receipts_v1"]
        pages = journal["executions"][execution_id]["pages"]
        assert set(pages) == {page_id, second_page_id}
        stored = pages[page_id]
        assert len(stored["attempts"]) == 2
        assert stored["attempts"][0]["page"]["page_status"] == "completed"
        assert (
            stored["attempts"][0]["page"]["request"]["params"]["query"]
            == "approved query"
        )
        assert (
            stored["attempts"][1]["page"]["request"]["params"]["query"] == "retry query"
        )

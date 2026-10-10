"""Search page checkpoints are append-only and safe to retry."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.research_engine.contracts import canonical_stage_output_hash
from src.services.research_engine.search_receipts import SearchReceiptJournal


class MemorySession:
    def __init__(self, run: SimpleNamespace) -> None:
        self.run = run

    async def refresh(self, _run: Any, **_kwargs: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_page_receipt_is_idempotent_and_preserves_retry_evidence() -> None:
    run = SimpleNamespace(reproducibility_manifest=None)
    db = MemorySession(run)
    journal = SearchReceiptJournal(cast(AsyncSession, db), uuid4(), uuid4())
    strategy = {"strategy_version": "sha256:" + "a" * 64, "intended": {}}
    execution_id = str(uuid4())
    page_id = str(uuid4())

    def page(attempt_id: str, status: str, query: str) -> dict[str, Any]:
        return {
            "page_id": page_id,
            "page_index": 0,
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

    with patch(
        "src.services.research_engine.search_receipts.require_run",
        new=AsyncMock(return_value=run),
    ):
        first_requested = page("attempt-1", "requested", "original query")
        await asyncio.gather(
            journal.persist_page(
                step_id="discover",
                strategy=strategy,
                provider="openalex",
                execution_id=execution_id,
                page=first_requested,
            ),
            journal.persist_page(
                step_id="discover",
                strategy=strategy,
                provider="openalex",
                execution_id=execution_id,
                page=first_requested,
            ),
        )
        await journal.persist_page(
            step_id="discover",
            strategy=strategy,
            provider="openalex",
            execution_id=execution_id,
            page=page("attempt-1", "completed", "original query"),
        )
        await journal.persist_page(
            step_id="discover",
            strategy=strategy,
            provider="openalex",
            execution_id=execution_id,
            page=page("attempt-2", "requested", "retry query"),
        )
        await journal.persist_page(
            step_id="discover",
            strategy=strategy,
            provider="openalex",
            execution_id=execution_id,
            page=page("attempt-2", "completed", "retry query"),
        )

        completed_page = page("attempt-2", "completed", "retry query")
        completed_page["imported_source_ids"] = [str(uuid4())]
        completed_page["imported_count"] = 1
        output: dict[str, Any] = {
            "coverage": {
                "providers": {
                    "openalex": {
                        "execution_id": execution_id,
                        "attempt_id": "attempt-2",
                        "status": "ok",
                        "completion": "exhausted",
                        "pages": [completed_page],
                    }
                }
            }
        }
        await journal.finalize_search_step(
            step_id="discover", strategy=strategy, output=output
        )

    execution = run.reproducibility_manifest["_search_receipts_v1"]["executions"][
        execution_id
    ]
    stored_page = execution["pages"][page_id]
    assert len(stored_page["attempts"]) == 2
    assert stored_page["attempts"][0]["page"]["page_status"] == "completed"
    assert (
        stored_page["attempts"][0]["page"]["request"]["params"]["query"]
        == "original query"
    )
    assert (
        stored_page["attempts"][1]["page"]["request"]["params"]["query"]
        == "retry query"
    )
    # The final page, with its imported IDs, replaces only its own attempt.
    assert stored_page["attempts"][1]["page"]["imported_count"] == 1
    assert stored_page["attempts"][0]["page"]["imported_count"] == 0
    assert execution["provider_attempts"]["attempt-2"]["status"] == "ok"


@pytest.mark.asyncio
async def test_finalize_keeps_attempt_history_out_of_the_hashed_envelope() -> None:
    """Attempt history lives in the manifest journal, never in the step output.

    The engine hashes the contract-v1 search envelope before the run stream
    finalizes its receipts, and the lifecycle re-hashes that envelope before it
    persists the step. Any write into the envelope here makes the lifecycle
    reject the step with "Completed step hash does not match its persisted
    envelope". The receipt's execution, page and attempt IDs are the keys into
    the journal, which keeps every attempt, including a page that only an
    interrupted earlier attempt requested.

    Mutation check: in ``search_receipts.py:finalize_search_step`` (~L172),
    after ``current_attempt["page"] = deepcopy(page)``, add
    ``page["attempt_history"] = deepcopy(page_entry.get("attempts", []))``.
    This test then fails on ``assert output == envelope``. The PostgreSQL
    tests ``test_controlled_lifecycle_persists_each_stage_exactly_once`` and
    ``test_terminal_search_scenarios_are_durable[no_evidence]`` in
    ``backend/tests/integration/test_daily_research_brief_postgres.py`` (needs
    ``ORCHESTRATION_TEST_DATABASE_URL``) fail with "Completed step hash does
    not match its persisted envelope".
    """
    run = SimpleNamespace(reproducibility_manifest=None)
    db = MemorySession(run)
    journal = SearchReceiptJournal(cast(AsyncSession, db), uuid4(), uuid4())
    strategy = {"strategy_version": "sha256:" + "b" * 64, "intended": {}}
    execution_id = str(uuid4())
    kept_page_id = str(uuid4())
    interrupted_page_id = str(uuid4())

    def page(page_id: str, index: int, attempt_id: str, status: str) -> dict[str, Any]:
        return {
            "page_id": page_id,
            "page_index": index,
            "attempt_id": attempt_id,
            "page_status": status,
            "request": {"method": "GET", "params": {"page": index}},
            "response": {"parsed_count": 1 if status == "completed" else 0},
            "record_keys": [],
            "imported_count": 0,
            "imported_source_ids": [],
        }

    with patch(
        "src.services.research_engine.search_receipts.require_run",
        new=AsyncMock(return_value=run),
    ):
        for checkpoint in (
            page(kept_page_id, 0, "attempt-1", "completed"),
            page(interrupted_page_id, 1, "attempt-1", "requested"),
            page(kept_page_id, 0, "attempt-2", "completed"),
        ):
            await journal.persist_page(
                step_id="discover",
                strategy=strategy,
                provider="openalex",
                execution_id=execution_id,
                page=checkpoint,
            )

        final_page = page(kept_page_id, 0, "attempt-2", "completed")
        final_page["imported_source_ids"] = [str(uuid4())]
        final_page["imported_count"] = 1
        output: dict[str, Any] = {
            "contract_version": 1,
            "stage_type": "search",
            "coverage": {
                "search_strategy": strategy,
                "providers": {
                    "openalex": {
                        "execution_id": execution_id,
                        "attempt_id": "attempt-2",
                        "status": "ok",
                        "completion": "exhausted",
                        "pages": [final_page],
                    }
                },
            },
        }
        envelope = deepcopy(output)
        envelope_hash = canonical_stage_output_hash(output)

        await journal.finalize_search_step(
            step_id="discover", strategy=strategy, output=output
        )

    assert output == envelope
    assert canonical_stage_output_hash(output) == envelope_hash

    execution = run.reproducibility_manifest["_search_receipts_v1"]["executions"][
        execution_id
    ]
    kept = execution["pages"][kept_page_id]["attempts"]
    assert [attempt["attempt_id"] for attempt in kept] == ["attempt-1", "attempt-2"]
    assert kept[1]["page"] == final_page
    interrupted = execution["pages"][interrupted_page_id]["attempts"]
    assert [attempt["page"]["page_status"] for attempt in interrupted] == ["requested"]
    assert execution["provider_attempts"]["attempt-2"]["pages"] == [final_page]
    assert execution["status"] == "ok"

"""Search page checkpoints are append-only and safe to retry."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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
    persisted_page = output["coverage"]["providers"]["openalex"]["pages"][0]
    assert persisted_page["imported_count"] == 1
    assert len(persisted_page["attempt_history"]) == 2

"""Durable, retry-safe checkpoints for provider search pages."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Dict, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_run import ResearchRun
from src.services.research_engine.project_access import ResearchAction, require_run

_MANIFEST_KEY = "_search_receipts_v1"


class SearchReceiptJournal:
    """Persist each page in the owning run manifest under current project access.

    The run-row lock serializes writers across workers. Stable execution and page
    IDs make retries idempotent; each invocation is retained as a separate
    attempt so a later provider response cannot replace the first evidence.
    """

    def __init__(self, db: AsyncSession, run_id: UUID, user_id: UUID) -> None:
        self.db = db
        self.run_id = run_id
        self.user_id = user_id
        self._write_lock = asyncio.Lock()

    async def _locked_run(self) -> ResearchRun:
        run = await require_run(self.db, self.run_id, self.user_id, ResearchAction.EDIT)
        await self.db.refresh(run, with_for_update=True)
        return run

    async def persist_page(
        self,
        *,
        step_id: str,
        strategy: Dict[str, Any],
        provider: str,
        execution_id: str,
        page: Dict[str, Any],
    ) -> None:
        """Checkpoint a request or response before the connector advances."""
        async with self._write_lock:
            run = await self._locked_run()
            manifest: Dict[str, Any] = deepcopy(
                cast(Dict[str, Any], run.reproducibility_manifest or {})
            )
            journal = deepcopy(
                manifest.get(_MANIFEST_KEY)
                or {"schema_version": 1, "strategies": {}, "executions": {}}
            )
            strategy_version = str(strategy["strategy_version"])
            known_strategy = journal["strategies"].setdefault(
                strategy_version, deepcopy(strategy)
            )
            if known_strategy != strategy:
                raise ValueError(
                    "search strategy hash is already bound to other content"
                )

            executions = journal["executions"]
            execution = executions.setdefault(
                execution_id,
                {
                    "execution_id": execution_id,
                    "step_id": step_id,
                    "provider": provider,
                    "strategy_version": strategy_version,
                    "status": "running",
                    "pages": {},
                    "provider_attempts": {},
                },
            )
            if (
                execution["step_id"] != step_id
                or execution["provider"] != provider
                or execution["strategy_version"] != strategy_version
            ):
                raise ValueError(
                    "search execution identity conflicts with stored evidence"
                )

            page_id = str(page["page_id"])
            page_entry = execution["pages"].setdefault(
                page_id,
                {"page_id": page_id, "page_index": page["page_index"], "attempts": []},
            )
            attempt_id = str(page["attempt_id"])
            attempts = page_entry["attempts"]
            attempt = next(
                (item for item in attempts if item["attempt_id"] == attempt_id), None
            )
            if attempt is None:
                attempt = {"attempt_id": attempt_id, "page": deepcopy(page)}
                attempts.append(attempt)
            else:
                # This attempt moves from requested -> response -> parsed. Other
                # attempts remain immutable alongside it.
                attempt["page"] = deepcopy(page)

            manifest[_MANIFEST_KEY] = journal
            setattr(run, "reproducibility_manifest", manifest)
            await self.db.commit()

    async def finalize_search_step(
        self,
        *,
        step_id: str,
        strategy: Dict[str, Any],
        output: Mapping[str, Any],
    ) -> None:
        """Attach final import IDs and provider outcomes without losing attempts.

        ``output`` is the stage envelope the engine has already hashed, and the
        lifecycle re-hashes it before persisting the step, so it is read-only
        here. Attempt history stays in this journal, keyed by the receipt's
        execution, page and attempt IDs.
        """
        async with self._write_lock:
            run = await self._locked_run()
            manifest: Dict[str, Any] = deepcopy(
                cast(Dict[str, Any], run.reproducibility_manifest or {})
            )
            journal = deepcopy(manifest.get(_MANIFEST_KEY) or {})
            if not journal:
                return

            strategy_version = str(strategy["strategy_version"])
            coverage = output.get("coverage") or {}
            for provider, receipt in (coverage.get("providers") or {}).items():
                execution_id = str(receipt.get("execution_id") or "")
                execution = journal.get("executions", {}).get(execution_id)
                if execution is None:
                    continue
                if (
                    execution.get("step_id") != step_id
                    or execution.get("strategy_version") != strategy_version
                    or execution.get("provider") != provider
                ):
                    raise ValueError(
                        "completed search receipt does not match its checkpoint"
                    )

                attempt_id = str(receipt.get("attempt_id") or "")
                attempt_receipts = execution.setdefault("provider_attempts", {})
                attempt_receipts.setdefault(attempt_id, deepcopy(receipt))
                execution["status"] = receipt.get("status", execution["status"])

                # Pages that only an interrupted earlier attempt requested stay
                # in the journal, even when the provider now returns a
                # different page sequence.
                durable_pages = execution.get("pages", {})
                for page in receipt.get("pages") or []:
                    page_entry = durable_pages.get(str(page.get("page_id") or ""))
                    if page_entry is None:
                        continue
                    current_attempt = next(
                        (
                            item
                            for item in page_entry.get("attempts", [])
                            if item.get("attempt_id") == page.get("attempt_id")
                        ),
                        None,
                    )
                    if current_attempt is not None:
                        current_attempt["page"] = deepcopy(page)

            manifest[_MANIFEST_KEY] = journal
            setattr(run, "reproducibility_manifest", manifest)

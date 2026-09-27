"""Durability contract for research-run lifecycle transitions."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace, TracebackType
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_step import ResearchStep
from src.schemas.research_engine import ReviewKind
from src.services.research_engine.contracts import canonical_stage_output_hash
from src.services.research_engine.run_lifecycle import (
    PauseDescriptor,
    ResearchRunLifecycleService,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


class _Transaction:
    def __init__(self, session: "_Session") -> None:
        self.session = session
        self.snapshot: (
            tuple[dict[str, Any], str, int, list[object], list[object]] | None
        ) = None

    async def __aenter__(self) -> "_Transaction":
        self.snapshot = (
            copy.deepcopy(self.session.run.reproducibility_manifest),
            self.session.run.status,
            self.session.run.total_tokens,
            list(self.session.persisted_steps),
            list(self.session.persisted_sources),
        )
        self.session.transaction_entries += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc is not None:
            assert self.snapshot is not None
            (
                self.session.run.reproducibility_manifest,
                self.session.run.status,
                self.session.run.total_tokens,
                steps,
                sources,
            ) = self.snapshot
            self.session.persisted_steps[:] = steps
            self.session.persisted_sources[:] = sources
            self.session.rollbacks += 1
        else:
            self.session.commits += 1
        return False


class _Session:
    """Small transactional store used to observe the lifecycle boundary."""

    def __init__(self, run: SimpleNamespace) -> None:
        self.run = run
        self.persisted_steps: list[object] = []
        self.persisted_sources: list[object] = []
        self.transaction_entries = 0
        self.commits = 0
        self.rollbacks = 0
        self.fail_after_step = False

    def begin(self) -> _Transaction:
        return _Transaction(self)

    async def execute(self, _statement: Any) -> Mock:
        result = Mock()
        result.scalars.return_value.first.return_value = next(
            iter(self.persisted_steps), None
        )
        return result

    def add(self, row: object) -> None:
        if isinstance(row, ResearchStep):
            self.persisted_steps.append(row)
            return
        if self.fail_after_step:
            raise RuntimeError("injected transition failure")
        self.persisted_sources.append(row)


def _run() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        status="running",
        reproducibility_manifest={"scope_confirmation": {"confirmed": True}},
        total_tokens=5,
    )


def _screen_output() -> dict[str, object]:
    return {
        "contract_version": 1,
        "stage_type": "screen",
        "usage": {"model_calls": 1, "total_tokens": 7, "batches": []},
        "screening": [
            {
                "source_id": "source-1",
                "part_id": "p0001",
                "included": True,
                "reason": "Matches scope",
            }
        ],
        "included_source_ids": ["source-1"],
        "processing_coverage": {
            "1": {
                "seen_source_ids": ["source-1"],
                "processed_source_ids": ["source-1"],
                "excluded_source_ids": [],
                "omitted": [],
                "source_parts": [],
                "complete": True,
            }
        },
    }


def _event(output: dict[str, object] | None = None) -> dict[str, object]:
    resolved = output or _screen_output()
    return {
        "event": "step_complete",
        "step_index": 1,
        "step_id": "screen",
        "step_type": "screen",
        "output": resolved,
        "inputs_hash": "1" * 64,
        "outputs_hash": canonical_stage_output_hash(resolved),
        "quality_marks": [],
        "token_count": 7,
    }


def _step_definition() -> dict[str, object]:
    return {
        "id": "screen",
        "type": "screen",
        "mode": "deterministic",
        "model_id": "gpt-test",
        "temperature": 0.0,
        "seed": 42,
        "parameters": {"contract_version": 1, "review_gate": "screening"},
    }


@pytest.mark.asyncio
async def test_gated_step_persists_envelope_hash_tokens_manifest_and_pause_atomically() -> (
    None
):
    """Removing any write from the transaction leaves an incomplete durable gate."""

    run = _run()
    session = _Session(run)
    service = ResearchRunLifecycleService(cast(AsyncSession, session), now=lambda: NOW)
    source = SimpleNamespace(run_id=run.id, external_id="source-1")

    transition = await service.persist_step_completion(
        run=run,
        event=_event(),
        step_definition=_step_definition(),
        source_rows=[source],
    )

    assert session.commits == 1
    assert session.rollbacks == 0
    assert len(session.persisted_steps) == 1
    persisted = cast(ResearchStep, session.persisted_steps[0])
    assert persisted.output == _screen_output()
    assert persisted.outputs_hash == canonical_stage_output_hash(_screen_output())
    assert persisted.token_count == 7
    assert session.persisted_sources == [source]
    assert run.total_tokens == 12
    assert run.status == "paused"
    assert transition.persisted is True
    assert transition.duplicate is False
    assert transition.pause == PauseDescriptor(
        pause_reason="review_required",
        review_kind=ReviewKind.SCREENING,
        step_index=1,
        output_hash=canonical_stage_output_hash(_screen_output()),
    )
    assert run.reproducibility_manifest["stage_hashes"] == {
        "1": canonical_stage_output_hash(_screen_output())
    }
    assert run.reproducibility_manifest["pending_review"] == {
        "run_id": str(run.id),
        "step_index": 1,
        "stage_type": "screen",
        "review_kind": "screening",
        "contract_version": 1,
        "output_hash": canonical_stage_output_hash(_screen_output()),
        "status": "pending",
        "created_at": NOW.isoformat(),
    }


@pytest.mark.asyncio
async def test_failed_atomic_step_transition_rolls_back_every_write() -> None:
    """A source or manifest failure cannot leave a paid step partially persisted."""

    run = _run()
    original_manifest = copy.deepcopy(run.reproducibility_manifest)
    session = _Session(run)
    session.fail_after_step = True
    service = ResearchRunLifecycleService(cast(AsyncSession, session), now=lambda: NOW)

    with pytest.raises(RuntimeError, match="injected transition failure"):
        await service.persist_step_completion(
            run=run,
            event=_event(),
            step_definition=_step_definition(),
            source_rows=[SimpleNamespace(run_id=run.id, external_id="source-1")],
        )

    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.persisted_steps == []
    assert session.persisted_sources == []
    assert run.status == "running"
    assert run.total_tokens == 5
    assert run.reproducibility_manifest == original_manifest


@pytest.mark.asyncio
async def test_duplicate_step_complete_event_is_idempotent() -> None:
    """Re-delivering the same event must not duplicate rows or token charges."""

    run = _run()
    session = _Session(run)
    service = ResearchRunLifecycleService(cast(AsyncSession, session), now=lambda: NOW)
    event = _event()

    first = await service.persist_step_completion(
        run=run,
        event=event,
        step_definition=_step_definition(),
        source_rows=[],
    )
    second = await service.persist_step_completion(
        run=run,
        event=event,
        step_definition=_step_definition(),
        source_rows=[],
    )

    assert first.duplicate is False
    assert second.duplicate is True
    assert len(session.persisted_steps) == 1
    assert run.total_tokens == 12
    assert run.reproducibility_manifest["stage_hashes"] == {
        "1": canonical_stage_output_hash(_screen_output())
    }


def test_reload_descriptor_is_derived_from_durable_manifest_only() -> None:
    """A missed SSE frame cannot hide the exact persisted pause descriptor."""

    run_id = uuid4()
    digest = canonical_stage_output_hash(_screen_output())
    run = SimpleNamespace(
        id=run_id,
        status="paused",
        reproducibility_manifest={
            "pending_review": {
                "run_id": str(run_id),
                "step_index": 1,
                "stage_type": "screen",
                "review_kind": "screening",
                "contract_version": 1,
                "output_hash": digest,
                "status": "pending",
                "created_at": NOW.isoformat(),
            }
        },
    )

    assert ResearchRunLifecycleService.pause_descriptor(run) == PauseDescriptor(
        pause_reason="review_required",
        review_kind=ReviewKind.SCREENING,
        step_index=1,
        output_hash=digest,
    )


def test_pause_descriptor_rejects_manifest_for_another_run() -> None:
    """A copied descriptor must not authorize or describe the current run."""

    run = SimpleNamespace(
        id=uuid4(),
        status="paused",
        reproducibility_manifest={
            "pending_review": {
                "run_id": str(UUID("00000000-0000-0000-0000-000000000001")),
                "step_index": 1,
                "stage_type": "screen",
                "review_kind": "screening",
                "contract_version": 1,
                "output_hash": "a" * 64,
                "status": "pending",
                "created_at": NOW.isoformat(),
            }
        },
    )

    assert ResearchRunLifecycleService.pause_descriptor(run) is None


@pytest.mark.asyncio
async def test_release_paused_claim_restores_unconsumed_authorization_for_retry() -> (
    None
):
    """A denied admission must not strand exact consent after claim consumption."""

    run = _run()
    run.status = "paused"
    run.started_at = NOW
    run.reproducibility_manifest["resume_authorization"] = {
        "kind": "continue_unverified",
        "actor_id": str(uuid4()),
        "step_index": 4,
        "output_hash": "a" * 64,
        "created_at": NOW.isoformat(),
        "consumed_at": None,
    }
    session = _Session(run)
    service = ResearchRunLifecycleService(cast(AsyncSession, session), now=lambda: NOW)

    claim = await service.claim_stream(run=run)
    assert run.status == "running"
    assert run.reproducibility_manifest["resume_authorization"]["consumed_at"]

    await service.release_stream_claim(run=run, claim=claim)

    assert run.status == "paused"
    assert run.started_at == NOW
    assert run.reproducibility_manifest["resume_authorization"]["consumed_at"] is None


@pytest.mark.asyncio
async def test_release_pending_claim_restores_pristine_pending_state() -> None:
    """A denied first admission returns a new run to a claimable pending state."""

    run = _run()
    run.status = "pending"
    run.started_at = None
    session = _Session(run)
    service = ResearchRunLifecycleService(cast(AsyncSession, session), now=lambda: NOW)

    claim = await service.claim_stream(run=run)
    assert run.status == "running"
    assert run.started_at == NOW

    await service.release_stream_claim(run=run, claim=claim)

    assert run.status == "pending"
    assert run.started_at is None

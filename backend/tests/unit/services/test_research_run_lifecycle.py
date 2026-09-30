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
from src.services.research_engine.observability import ResearchObservability
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
        self.flushes = 0

    def begin(self) -> _Transaction:
        return _Transaction(self)

    async def execute(self, _statement: Any) -> Mock:
        result = Mock()
        result.scalars.return_value.first.return_value = next(
            iter(self.persisted_steps), None
        )
        return result

    async def flush(self) -> None:
        self.flushes += 1

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


class _ExplodingObserver:
    def record_pause(self, **_fields: object) -> None:
        raise RuntimeError("PRIVATE_RESEARCH_QUESTION")


@pytest.mark.asyncio
async def test_gated_step_persists_envelope_hash_tokens_manifest_and_pause_atomically() -> (
    None
):
    """Removing any write from the transaction leaves an incomplete durable gate."""

    run = _run()
    session = _Session(run)
    observer = ResearchObservability()
    service = ResearchRunLifecycleService(
        cast(AsyncSession, session), now=lambda: NOW, observer=observer
    )
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
    assert observer.snapshot()["counters"]["pauses"]["review_required"] == 1


@pytest.mark.asyncio
async def test_verification_override_records_override_and_verification_metrics() -> (
    None
):
    run = _run()
    run.status = "paused"
    output_hash = "a" * 64
    run.reproducibility_manifest["verification_failure"] = {
        "run_id": str(run.id),
        "step_index": 4,
        "stage_type": "verify",
        "output_hash": output_hash,
        "status": "pending",
    }
    session = _Session(run)
    observer = ResearchObservability()
    service = ResearchRunLifecycleService(
        cast(AsyncSession, session), now=lambda: NOW, observer=observer
    )

    transition = await service.authorize_resume(
        run=run,
        actor_id=uuid4(),
        request={"continue_unverified": True, "output_hash": output_hash},
    )
    replay = await service.authorize_resume(
        run=run,
        actor_id=uuid4(),
        request={"continue_unverified": True, "output_hash": output_hash},
    )

    assert transition.authorization_kind == "continue_unverified"
    assert replay.authorization_kind == "continue_unverified"
    metrics = observer.snapshot()["counters"]
    assert metrics["overrides"]["continued_unverified"] == 1
    assert metrics["verification"]["overridden"] == 1


@pytest.mark.asyncio
async def test_observability_failure_cannot_rollback_a_persisted_pause() -> None:
    run = _run()
    session = _Session(run)
    service = ResearchRunLifecycleService(
        cast(AsyncSession, session),
        now=lambda: NOW,
        observer=cast(Any, _ExplodingObserver()),
    )

    transition = await service.persist_step_completion(
        run=run,
        event=_event(),
        step_definition=_step_definition(),
    )

    assert transition.pause is not None
    assert session.commits == 1
    assert run.status == "paused"


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
    observer = ResearchObservability()
    service = ResearchRunLifecycleService(
        cast(AsyncSession, session), now=lambda: NOW, observer=observer
    )
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
    assert observer.snapshot()["counters"]["pauses"]["review_required"] == 1


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage_type", "review_kind", "output", "reason"),
    [
        (
            "screen",
            "screening",
            {
                "contract_version": 1,
                "stage_type": "screen",
                "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
                "screening": [],
                "included_source_ids": [],
                "processing_coverage": {"1": {"complete": True}},
            },
            "empty_screening",
        ),
        (
            "extract",
            "extraction",
            {
                "contract_version": 1,
                "stage_type": "extract",
                "usage": {"model_calls": 1, "total_tokens": 7, "batches": []},
                "extractions": [
                    {"source_id": "source-1", "part_id": "p0001", "claims": []}
                ],
                "processing_coverage": {"1": {"complete": True}},
                "diagnostics": [],
            },
            "empty_extraction",
        ),
    ],
)
async def test_zero_item_gated_stage_persists_terminal_no_evidence_audit(
    stage_type: str,
    review_kind: str,
    output: dict[str, object],
    reason: str,
) -> None:
    run = _run()
    session = _Session(run)
    observer = ResearchObservability()
    service = ResearchRunLifecycleService(
        cast(AsyncSession, session), now=lambda: NOW, observer=observer
    )
    step_definition = {
        "id": stage_type,
        "type": stage_type,
        "parameters": {"contract_version": 1, "review_gate": review_kind},
    }
    event = {
        "event": "step_complete",
        "step_index": 1,
        "step_id": stage_type,
        "step_type": stage_type,
        "output": output,
        "outputs_hash": canonical_stage_output_hash(output),
        "quality_marks": [],
        "token_count": 7,
    }

    transition = await service.persist_step_completion(
        run=run,
        event=event,
        step_definition=step_definition,
    )

    assert transition.terminal_status == "no_evidence"
    assert transition.pause is None
    assert run.status == "completed"
    assert run.reproducibility_manifest["final_status"] == "no_evidence"
    assert "pending_review" not in run.reproducibility_manifest
    assert run.reproducibility_manifest["terminal_audit"] == {
        "outcome": "no_evidence",
        "reason": reason,
        "step_index": 1,
        "stage_type": stage_type,
        "output_hash": canonical_stage_output_hash(output),
        "created_at": NOW.isoformat(),
    }
    assert observer.snapshot()["counters"]["final_status"]["no_evidence"] == 1


@pytest.mark.asyncio
async def test_user_pause_before_first_step_uses_safe_sentinel_descriptor() -> None:
    run = _run()
    session = _Session(run)
    service = ResearchRunLifecycleService(cast(AsyncSession, session), now=lambda: NOW)

    descriptor = await service.persist_user_pause(run=run)

    assert run.status == "paused"
    assert descriptor.pause_reason == "user_paused"
    assert descriptor.step_index == -1
    assert len(descriptor.output_hash) == 64
    assert run.reproducibility_manifest["user_pause"] == {
        "step_index": -1,
        "output_hash": descriptor.output_hash,
        "created_at": NOW.isoformat(),
    }
    assert ResearchRunLifecycleService.pause_descriptor(run) == descriptor


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
async def test_stream_cancellation_recovery_never_rewrites_terminal_state(
    terminal_status: str,
) -> None:
    """A disconnect after the terminal commit cannot mint a resume path."""
    run = _run()
    run.status = terminal_status
    run.reproducibility_manifest = {
        "final_status": "verified" if terminal_status == "completed" else "failed",
        "terminal_marker": "immutable",
    }
    original = copy.deepcopy(run.reproducibility_manifest)
    session = _Session(run)
    service = ResearchRunLifecycleService(cast(AsyncSession, session), now=lambda: NOW)

    descriptor = await service.recover_stream_cancellation(
        run=run,
        step_index=-1,
        output_hash="a" * 64,
        total_tokens=99,
    )

    assert descriptor is None
    assert run.status == terminal_status
    assert run.reproducibility_manifest == original
    assert run.total_tokens == 5


@pytest.mark.asyncio
async def test_user_pause_anchors_latest_step_and_reconnect_restores_retry_state() -> (
    None
):
    run = _run()
    output = _screen_output()
    step = ResearchStep(
        run_id=run.id,
        step_index=1,
        step_type="screen",
        mode="deterministic",
        output=output,
        outputs_hash=canonical_stage_output_hash(output),
        quality_marks=[],
        token_count=7,
    )
    session = _Session(run)
    session.persisted_steps.append(step)
    service = ResearchRunLifecycleService(cast(AsyncSession, session), now=lambda: NOW)

    descriptor = await service.persist_user_pause(run=run)
    await service.authorize_resume(run=run, actor_id=uuid4())
    claim = await service.claim_stream(run=run)

    assert descriptor.step_index == 1
    assert descriptor.output_hash == canonical_stage_output_hash(output)
    assert "user_pause" not in run.reproducibility_manifest

    await service.release_stream_claim(run=run, claim=claim)

    assert run.status == "paused"
    assert ResearchRunLifecycleService.pause_descriptor(run) == descriptor


@pytest.mark.asyncio
@pytest.mark.parametrize("with_collection", [False, True])
async def test_source_rows_are_observed_in_the_step_transaction(
    monkeypatch: pytest.MonkeyPatch, with_collection: bool
) -> None:
    """GOO-299: report observations join the step-completion transaction."""
    from src.services.research_engine import run_lifecycle

    calls: list[tuple[object, UUID, list[object], int]] = []
    run = _run()
    session = _Session(run)

    async def fake_observe(
        db: object, *, collection_id: UUID, sources: list[object]
    ) -> list[object]:
        calls.append((db, collection_id, list(sources), session.flushes))
        return []

    monkeypatch.setattr(run_lifecycle, "observe_sources", fake_observe)
    service = ResearchRunLifecycleService(cast(AsyncSession, session), now=lambda: NOW)
    source = SimpleNamespace(run_id=run.id, external_id="source-1")
    collection_id = uuid4()

    await service.persist_step_completion(
        run=run,
        event=_event(),
        step_definition=_step_definition(),
        source_rows=[source],
        collection_id=collection_id if with_collection else None,
    )

    if with_collection:
        # Same session, same rows, flushed first so source ids are durable FKs.
        assert calls == [(session, collection_id, [source], 1)]
    else:
        assert calls == []
    assert session.commits == 1


@pytest.mark.asyncio
async def test_observation_failure_rolls_back_the_whole_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GOO-299: a failed identity write must not leave a committed step/sources."""
    from src.services.research_engine import run_lifecycle

    async def failing_observe(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("identity write failed")

    monkeypatch.setattr(run_lifecycle, "observe_sources", failing_observe)
    run = _run()
    session = _Session(run)
    service = ResearchRunLifecycleService(cast(AsyncSession, session), now=lambda: NOW)

    with pytest.raises(RuntimeError, match="identity write failed"):
        await service.persist_step_completion(
            run=run,
            event=_event(),
            step_definition=_step_definition(),
            source_rows=[SimpleNamespace(run_id=run.id, external_id="source-1")],
            collection_id=uuid4(),
        )

    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.persisted_steps == []
    assert session.persisted_sources == []
    assert run.total_tokens == 5

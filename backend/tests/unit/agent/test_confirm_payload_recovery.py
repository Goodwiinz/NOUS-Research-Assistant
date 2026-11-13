"""R2-H3 follow-up: rebuild a parked run's lost job payload from durable state.

Awaiting-confirmation runs are confirmable for 24h, but the request payload
the resume needs lived only in the Redis/L1 job record. After a Redis flush
/confirm 409'd until the 25h sweeper failed the run. These tests cover the two
building blocks of the recovery:

- ``recover_confirm_request_payload`` accepts a checkpoint only when it is
  owned by the caller, records the same thread, and holds the exact pending
  interrupt the approval receipt names.
- ``restore_confirmation_mirror`` re-creates the mirror only while absent.

Mutation checks (docs/engineering/testing.md):

- drop the ``require_approval`` call in ``recover_confirm_request_payload``
  -> ``test_wrong_approval_receipt_is_refused`` and
  ``test_interrupt_from_another_run_is_refused`` fail;
- drop ``nx=True`` in ``restore_confirmation_mirror``
  -> ``test_restore_never_clobbers_a_record_that_reappeared`` fails.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.services.agent import job_store as js
from src.services.agent.agent_execution_service import recover_confirm_request_payload
from src.services.agent.confirmation_service import pending_approval
from src.shared.enums import JobStatus

pytestmark = pytest.mark.unit


def _user() -> Any:
    return SimpleNamespace(id=uuid4(), organization_id=uuid4())


def _snapshot(*, user_id: str, thread_id: str, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "user_id": user_id,
        "thread_id": thread_id,
        "messages": [
            HumanMessage(content="ingest arXiv 2401.00001"),
            AIMessage(content="I need your confirmation."),
        ],
        "page_context": {"type": "chat", "project_id": None},
        "model": "",
        "use_rag": True,
        "document_only": False,
        "reasoning_effort": None,
        "attachment_ids": [],
    }
    values.update(overrides)
    interrupt = SimpleNamespace(
        id="interrupt-1", value={"tool": "ingest_paper", "args": {"id": "x"}}
    )
    return SimpleNamespace(
        values=values,
        tasks=(SimpleNamespace(interrupts=(interrupt,)),),
        config={"configurable": {"checkpoint_id": "ckpt-1", "checkpoint_ns": ""}},
    )


def _receipt(
    snapshot: SimpleNamespace, *, thread_id: str, job_id: str, user: Any
) -> str:
    approval = pending_approval(
        snapshot, thread_id=thread_id, run_id=job_id, user_id=user.id
    )
    assert approval is not None
    return approval.approval_id


def _graph_returning(
    snapshot: Optional[SimpleNamespace],
) -> tuple[Any, Any, Any, MagicMock]:
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=snapshot)
    return (
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        graph,
    )


async def _recover(
    snapshot: Optional[SimpleNamespace],
    *,
    job_id: str,
    thread_id: str,
    approval_id: str,
    user: Any,
) -> tuple[Optional[dict[str, Any]], MagicMock]:
    p1, p2, p3, graph = _graph_returning(snapshot)
    with p1, p2, p3:
        payload = await recover_confirm_request_payload(
            job_id=job_id,
            thread_id=thread_id,
            approval_id=approval_id,
            current_user=user,
        )
    return payload, graph


# ---------------------------------------------------------------------------
# recover_confirm_request_payload
# ---------------------------------------------------------------------------


async def test_matching_checkpoint_rebuilds_the_request() -> None:
    user, job_id, thread_id = _user(), str(uuid4()), str(uuid4())
    snapshot = _snapshot(user_id=str(user.id), thread_id=thread_id)
    receipt = _receipt(snapshot, thread_id=thread_id, job_id=job_id, user=user)

    payload, graph = await _recover(
        snapshot, job_id=job_id, thread_id=thread_id, approval_id=receipt, user=user
    )

    assert payload is not None
    assert payload["thread_id"] == thread_id
    assert payload["messages"][0]["content"] == "ingest arXiv 2401.00001"
    assert payload["page_context"]["type"] == "chat"
    assert payload["model"] == ""
    # The checkpoint is read at the durable thread id, never at job_id.
    graph.aget_state.assert_awaited_once_with(
        {"configurable": {"thread_id": thread_id}}
    )


async def test_wrong_approval_receipt_is_refused() -> None:
    user, job_id, thread_id = _user(), str(uuid4()), str(uuid4())
    snapshot = _snapshot(user_id=str(user.id), thread_id=thread_id)

    payload, _ = await _recover(
        snapshot, job_id=job_id, thread_id=thread_id, approval_id="0" * 64, user=user
    )

    assert payload is None


async def test_interrupt_from_another_run_is_refused() -> None:
    """The receipt binds run_id: an interrupt minted for a different job on
    the same thread cannot authorize rebuilding this job's payload."""
    user, thread_id = _user(), str(uuid4())
    snapshot = _snapshot(user_id=str(user.id), thread_id=thread_id)
    other_runs_receipt = _receipt(
        snapshot, thread_id=thread_id, job_id=str(uuid4()), user=user
    )

    payload, _ = await _recover(
        snapshot,
        job_id=str(uuid4()),
        thread_id=thread_id,
        approval_id=other_runs_receipt,
        user=user,
    )

    assert payload is None


async def test_checkpoint_owned_by_another_user_is_refused() -> None:
    user, job_id, thread_id = _user(), str(uuid4()), str(uuid4())
    snapshot = _snapshot(user_id=str(uuid4()), thread_id=thread_id)

    payload, _ = await _recover(
        snapshot, job_id=job_id, thread_id=thread_id, approval_id="a" * 64, user=user
    )

    assert payload is None


async def test_checkpoint_for_a_different_thread_is_refused() -> None:
    user, job_id, thread_id = _user(), str(uuid4()), str(uuid4())
    snapshot = _snapshot(user_id=str(user.id), thread_id=str(uuid4()))
    receipt = _receipt(snapshot, thread_id=thread_id, job_id=job_id, user=user)

    payload, _ = await _recover(
        snapshot, job_id=job_id, thread_id=thread_id, approval_id=receipt, user=user
    )

    assert payload is None


@pytest.mark.parametrize("snapshot", [None, SimpleNamespace(values={}, tasks=())])
async def test_empty_checkpoint_is_refused(snapshot: Optional[SimpleNamespace]) -> None:
    payload, _ = await _recover(
        snapshot,
        job_id=str(uuid4()),
        thread_id=str(uuid4()),
        approval_id="a" * 64,
        user=_user(),
    )

    assert payload is None


async def test_unreadable_checkpoint_is_refused() -> None:
    p1, p2, p3, graph = _graph_returning(None)
    graph.aget_state.side_effect = RuntimeError("checkpointer down")
    with p1, p2, p3:
        payload = await recover_confirm_request_payload(
            job_id=str(uuid4()),
            thread_id=str(uuid4()),
            approval_id="a" * 64,
            current_user=_user(),
        )

    assert payload is None


# ---------------------------------------------------------------------------
# restore_confirmation_mirror
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_store() -> Iterator[None]:
    js._l1.clear()
    yield
    js._l1.clear()


class _NxRedis:
    def __init__(self, existing: Optional[dict] = None) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.preset: Optional[str] = (
            json.dumps(existing) if existing is not None else None
        )

    async def set(
        self, key: str, value: str, *, ex: int, nx: bool = False
    ) -> Optional[bool]:
        if self.preset is not None and key not in self.store:
            self.store[key] = self.preset
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True


def _running_record(user: Any) -> dict:
    return {
        "status": JobStatus.RUNNING,
        "user_id": str(user.id),
        "request": {"thread_id": str(uuid4())},
    }


@pytest.mark.usefixtures("_clean_store")
async def test_restore_writes_the_record_when_absent() -> None:
    user, job_id = _user(), str(uuid4())
    redis = _NxRedis()
    with patch.object(js, "_get_redis", new=AsyncMock(return_value=redis)):
        outcome = await js.restore_confirmation_mirror(job_id, _running_record(user))

    assert outcome == "claimed"
    stored = json.loads(redis.store[f"{js._JOB_KEY_PREFIX}{job_id}"])
    assert stored["status"] == JobStatus.RUNNING.value
    assert stored["_confirmation_claim_id"]
    assert redis.ttls[f"{js._JOB_KEY_PREFIX}{job_id}"] == js._JOB_TTL_SECONDS
    assert js._l1[job_id]["user_id"] == str(user.id)


@pytest.mark.usefixtures("_clean_store")
async def test_restore_never_clobbers_a_record_that_reappeared() -> None:
    user, job_id = _user(), str(uuid4())
    stopping = {"status": JobStatus.STOPPING.value, "user_id": str(user.id)}
    redis = _NxRedis(existing=stopping)
    with patch.object(js, "_get_redis", new=AsyncMock(return_value=redis)):
        outcome = await js.restore_confirmation_mirror(job_id, _running_record(user))

    assert outcome == "conflict"
    assert json.loads(redis.store[f"{js._JOB_KEY_PREFIX}{job_id}"]) == stopping
    assert job_id not in js._l1


@pytest.mark.usefixtures("_clean_store")
async def test_restore_fails_closed_without_redis_in_shared_environment() -> None:
    with (
        patch.object(js, "_get_redis", new=AsyncMock(return_value=None)),
        patch.object(
            js, "process_local_confirmation_coordination_allowed", return_value=False
        ),
        pytest.raises(js.ConfirmationCoordinationUnavailable),
    ):
        await js.restore_confirmation_mirror(str(uuid4()), _running_record(_user()))

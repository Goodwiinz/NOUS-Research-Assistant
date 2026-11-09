"""Unit tests for the shared Celery async boundary helper (`run_async`).

The helper runs coroutines on a single long-lived event loop hosted on a daemon
thread, replacing the per-call ``asyncio.run`` sites in the document-processing
tasks (audit B7 follow-up).
"""

import asyncio

import pytest

from src.tasks._async_utils import run_async


@pytest.mark.unit
def test_run_async_returns_coroutine_result():
    async def coro():
        await asyncio.sleep(0)
        return 21 * 2

    assert run_async(coro()) == 42


@pytest.mark.unit
def test_run_async_propagates_exception():
    async def boom():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        run_async(boom())


@pytest.mark.unit
def test_run_async_reuses_single_loop_across_calls():
    async def which_loop():
        return id(asyncio.get_running_loop())

    first = run_async(which_loop())
    second = run_async(which_loop())

    # The whole point of the helper: one persistent loop reused for every call,
    # not a fresh loop torn down per call (which churns loop-bound resources).
    assert first == second


@pytest.mark.unit
def test_run_async_loop_is_not_the_callers_thread_loop():
    async def coro():
        return "ok"

    # Caller has no running loop; the coroutine still completes because it runs
    # on the helper's background loop.
    assert run_async(coro()) == "ok"


@pytest.mark.unit
def test_task_modules_use_the_shared_loop_only():
    """Audit 2026-11: these workers also run coroutines through ``run_async``
    on the persistent per-process loop, so a per-call ``asyncio.run`` /
    ``new_event_loop`` here creates resources (pooled async engine
    connections, the cached lightweight-LLM httpx client) on a loop that is
    then closed, and the next use raises "Event loop is closed" / "attached to
    a different loop". Route every coroutine through ``run_async``."""
    import ast
    from pathlib import Path

    tasks_dir = Path(__file__).resolve().parents[3] / "src" / "tasks"
    banned = {("asyncio", "run"), ("asyncio", "new_event_loop")}
    offenders = []
    for name in ("processing_tasks.py", "research_tasks.py"):
        tree = ast.parse((tasks_dir / name).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            owner = node.func.value
            if (
                isinstance(owner, ast.Name) and (owner.id, node.func.attr) in banned
            ) or node.func.attr == "run_until_complete":
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, "per-call event loops in Celery tasks: " + ", ".join(
        offenders
    )


@pytest.mark.unit
def test_run_async_waits_for_cancelled_coroutine_to_unwind():
    """On timeout/soft time limit run_async cancels the coroutine and
    re-raises. It must not return control to the task thread (whose except
    handler runs db.rollback() on the same sync Session) while the coroutine
    is still unwinding on the loop thread (audit 2026-11 review)."""
    import concurrent.futures
    import time

    unwound = {}

    async def slow():
        try:
            await asyncio.sleep(10)
        finally:
            # Cleanup that itself awaits, like an async client closing.
            await asyncio.sleep(0.3)
            unwound["at"] = time.monotonic()

    with pytest.raises(concurrent.futures.TimeoutError):
        run_async(slow(), timeout=0.05)
    raised_at = time.monotonic()

    assert "at" in unwound, "run_async returned before the coroutine unwound"
    assert unwound["at"] <= raised_at

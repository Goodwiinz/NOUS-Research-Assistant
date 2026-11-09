"""Shared async boundary helper for Celery tasks.

Celery tasks are synchronous, so awaiting a coroutine has historically meant
``asyncio.run(coro)`` at each call site. ``asyncio.run`` creates *and tears
down* a fresh event loop on every call, which churns event-loop-bound resources
(async DB engines, redis connection pools, httpx clients) and can raise
``RuntimeError: Event loop is closed`` / ``Future attached to a different loop``
when a cached client outlives the loop that created it.

``run_async`` instead runs every coroutine on a single, long-lived event loop
hosted on a dedicated daemon thread. The loop is created lazily *per process* so
it is safe across Celery's prefork fork boundary — a forked child recreates its
own loop the first time it needs one (guarded by ``os.getpid()``), rather than
inheriting a dead reference to a thread that did not survive the fork.

This is intentionally the one shared boundary for the tasks it is wired into;
callers just ``run_async(some_coroutine())`` and get the result synchronously.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, Coroutine, Optional, TypeVar

T = TypeVar("T")

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_pid: Optional[int] = None
_lock = threading.Lock()
# Upper bound on waiting for a cancelled coroutine to unwind.
_CANCEL_GRACE_SECONDS = 5.0


def _run_forever(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Return the shared event loop for this process, creating it if needed.

    Recreates the loop when the current loop is missing/closed or when the
    process was forked (``_loop_pid`` no longer matches ``os.getpid()``); the
    background thread running a loop does not survive ``fork()``.
    """
    global _loop, _loop_thread, _loop_pid

    pid = os.getpid()
    loop = _loop
    if loop is not None and not loop.is_closed() and _loop_pid == pid:
        return loop

    with _lock:
        loop = _loop
        if loop is not None and not loop.is_closed() and _loop_pid == pid:
            return loop

        new_loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_run_forever,
            args=(new_loop,),
            name="celery-async-runner",
            daemon=True,
        )
        thread.start()

        _loop = new_loop
        _loop_thread = thread
        _loop_pid = pid
        return new_loop


def run_async(coro: Coroutine[Any, Any, T], *, timeout: Optional[float] = None) -> T:
    """Run ``coro`` to completion on the shared loop and return its result.

    Blocks the calling (Celery task) thread until the coroutine finishes. Any
    exception raised inside the coroutine propagates unchanged.
    """
    loop = _ensure_loop()
    started = threading.Event()
    finished = threading.Event()

    async def _tracked() -> T:
        started.set()
        try:
            return await coro
        finally:
            finished.set()

    future = asyncio.run_coroutine_threadsafe(_tracked(), loop)
    try:
        return future.result(timeout)
    except BaseException:
        # A timeout, soft time limit or KeyboardInterrupt leaves the coroutine
        # running on the loop thread; cancel it so it does not leak. A no-op
        # if already done.
        future.cancel()
        # The returned future reports "cancelled" before the coroutine has
        # unwound. Wait (bounded) for it to finish, so the caller's handlers
        # (db.rollback(), job status writes) don't run on the task thread
        # while the coroutine still touches the same sync Session/ORM objects
        # on the loop thread. Skip the wait if it never started.
        if started.is_set():
            finished.wait(_CANCEL_GRACE_SECONDS)
        raise

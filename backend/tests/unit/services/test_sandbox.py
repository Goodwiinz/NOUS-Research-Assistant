"""Unit tests for sandboxed code execution (GOO-187).

Covers:
 - SandboxManager lifecycle: availability, create, execute, install, cleanup
 - _tool_execute_code: auth, validation, success, error, package install, outputs
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import sys
import time
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ---------------------------------------------------------------------------
# Stub heavy imports so tools_impl can be imported in a minimal test env.
#
# Stubs are installed by the module-scoped `_isolated_import_stubs` fixture
# below and sys.modules is fully restored afterwards. They used to be
# installed at import time and never removed; "not in sys.modules" means
# "not imported yet", not "not installed", so on a fresh interpreter this
# replaced real packages (boto3, PIL, tiktoken, ...) with mocks for every
# test collected after this file — silent cross-test poisoning.
# ---------------------------------------------------------------------------


def _stub_if_missing(name: str) -> None:
    """Insert an empty module stub for *name* (and each prefix) if not loaded."""
    if name not in sys.modules:
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            key = ".".join(parts[:i])
            if key not in sys.modules:
                sys.modules[key] = ModuleType(key)


_LIGHT_STUBS = [
    "langchain_core",
    "langchain_core.runnables",
    "langchain_core.tools",
    "langchain_core.messages",
    "langsmith",
    "openai",
    "anthropic",
    "cohere",
    "redis",
    "celery",
    "qdrant_client",
    "neo4j",
    "structlog",
]


@pytest.fixture(scope="module", autouse=True)
def _isolated_import_stubs():
    """Install the import stubs this module needs, then restore sys.modules.

    Everything added while this module's tests run (stubs, the fake
    src.services.agent package hierarchy from _import_tools_impl, transitively
    imported modules) is removed at teardown, and any entry that was
    overwritten is restored — later test modules import the real thing.
    """
    saved = dict(sys.modules)
    for _mod in _LIGHT_STUBS:
        _stub_if_missing(_mod)
    for _stub_name in _HEAVY_STUBS:
        _make_stub(_stub_name)
    yield
    for key in [k for k in sys.modules if k not in saved]:
        del sys.modules[key]
    for key, mod in saved.items():
        if sys.modules.get(key) is not mod:
            sys.modules[key] = mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_user():
    user = Mock()
    user.id = uuid4()
    return user


def _make_e2b_execution(
    stdout: str = "ok\n",
    stderr: str = "",
    error=None,
    results=None,
):
    """Build a fake E2B Execution object returned by sandbox.run_code()."""
    ex = Mock()
    ex.error = error
    logs = Mock()
    logs.stdout = stdout
    logs.stderr = stderr
    ex.logs = logs
    ex.results = results or []
    return ex


def _png_result(data: bytes = b"PNGDATA"):
    """Fake E2B result object carrying a PNG image."""
    r = Mock()
    r.png = data
    r.text = None
    return r


def _text_result(text: str = "some text"):
    r = Mock()
    r.png = None
    r.text = text
    return r


def _execution_error(name: str) -> Any:
    """Fake ``e2b_code_interpreter.ExecutionError`` (name, value, traceback)."""
    return SimpleNamespace(name=name, value="", traceback="")


class _KernelBox:
    """Stateful fake box: one namespace per box, like a Jupyter kernel.

    A cell whose first line is ``#sleep <seconds>`` awaits that long first.
    Cancelling a cell leaves the namespace intact, which is what the
    code-interpreter server's kernel interrupt does when our side closes the
    /execute stream (e2b-dev/code-interpreter#237). ``kill`` ends the box.
    """

    def __init__(self) -> None:
        self.ns: dict[str, Any] = {}
        self.killed = False
        self.sandbox_id = "kernel-box"

    async def run_code(self, code: str, **_kwargs: Any) -> Any:
        if self.killed:
            raise RuntimeError("sandbox was killed")
        first = code.splitlines()[0] if code else ""
        if first.startswith("#sleep "):
            await asyncio.sleep(float(first.split()[1]))
        if "pip" in code and "install" in code:
            return _make_e2b_execution(stdout="")
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                exec(code, self.ns)
        except Exception as exc:
            return _make_e2b_execution(
                stdout=out.getvalue(), error=f"{type(exc).__name__}: {exc}"
            )
        return _make_e2b_execution(stdout=out.getvalue())

    async def kill(self) -> None:
        self.killed = True


# ---------------------------------------------------------------------------
# SandboxManager.is_available
# ---------------------------------------------------------------------------


class TestSandboxManagerIsAvailable:
    def test_true_when_key_set_and_library_present(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        with patch("src.services.sandbox.e2b_sandbox_manager._e2b_available", True):
            from src.services.sandbox.e2b_sandbox_manager import SandboxManager

            assert SandboxManager().is_available is True

    def test_false_when_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("E2B_API_KEY", raising=False)
        with patch("src.services.sandbox.e2b_sandbox_manager._e2b_available", True):
            from src.services.sandbox.e2b_sandbox_manager import SandboxManager

            assert SandboxManager().is_available is False

    def test_false_when_library_not_installed(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        with patch("src.services.sandbox.e2b_sandbox_manager._e2b_available", False):
            from src.services.sandbox.e2b_sandbox_manager import SandboxManager

            assert SandboxManager().is_available is False


# ---------------------------------------------------------------------------
# SandboxManager.get_or_create_sandbox
# ---------------------------------------------------------------------------


class TestGetOrCreateSandbox:
    async def test_creates_sandbox_for_new_thread(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        fake_sb = AsyncMock()
        fake_sb.sandbox_id = "sb-001"
        fake_sb.run_code = AsyncMock(return_value=_make_e2b_execution())

        with (
            patch("src.services.sandbox.e2b_sandbox_manager._e2b_available", True),
            patch("src.services.sandbox.e2b_sandbox_manager.AsyncSandbox") as cls,
        ):
            cls.create = AsyncMock(return_value=fake_sb)
            from src.services.sandbox.e2b_sandbox_manager import SandboxManager

            m = SandboxManager()
            result = await m.get_or_create_sandbox("t-new")

        assert result is fake_sb
        cls.create.assert_called_once()

    async def test_reuses_existing_sandbox(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        fake_sb = AsyncMock()
        fake_sb.sandbox_id = "sb-002"
        fake_sb.run_code = AsyncMock(return_value=_make_e2b_execution())

        with (
            patch("src.services.sandbox.e2b_sandbox_manager._e2b_available", True),
            patch("src.services.sandbox.e2b_sandbox_manager.AsyncSandbox") as cls,
        ):
            cls.create = AsyncMock(return_value=fake_sb)
            from src.services.sandbox.e2b_sandbox_manager import SandboxManager

            m = SandboxManager()
            sb1 = await m.get_or_create_sandbox("t-reuse")
            sb2 = await m.get_or_create_sandbox("t-reuse")

        assert sb1 is sb2
        cls.create.assert_called_once()  # created only once

    async def test_raises_when_not_available(self, monkeypatch):
        monkeypatch.delenv("E2B_API_KEY", raising=False)
        from src.services.sandbox.e2b_sandbox_manager import SandboxManager

        m = SandboxManager()
        with pytest.raises(RuntimeError, match="E2B is not configured"):
            await m.get_or_create_sandbox("t-unavail")

    async def test_pre_installs_default_packages(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        fake_sb = AsyncMock()
        fake_sb.sandbox_id = "sb-003"
        run_calls: list[str] = []

        async def capture_run(code, **kwargs):
            run_calls.append(code)
            return _make_e2b_execution()

        fake_sb.run_code = capture_run

        with (
            patch("src.services.sandbox.e2b_sandbox_manager._e2b_available", True),
            patch("src.services.sandbox.e2b_sandbox_manager.AsyncSandbox") as cls,
        ):
            cls.create = AsyncMock(return_value=fake_sb)
            from src.services.sandbox.e2b_sandbox_manager import (
                DEFAULT_PACKAGES,
                SandboxManager,
            )

            m = SandboxManager()
            await m.get_or_create_sandbox("t-preinstall")

        # At least one run_code call should mention default packages
        assert any("pip" in c for c in run_calls)
        for pkg in DEFAULT_PACKAGES:
            assert any(pkg in c for c in run_calls)


# ---------------------------------------------------------------------------
# SandboxManager.execute
# ---------------------------------------------------------------------------


class TestSandboxManagerExecute:
    def _manager_with_sandbox(self, monkeypatch, fake_sb, thread_id="t-exec"):
        """Return a SandboxManager whose get_or_create_sandbox is patched to
        return fake_sb directly — bypasses the is_available check."""
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        from src.services.sandbox.e2b_sandbox_manager import SandboxManager

        m = SandboxManager()
        m._execution_counts[thread_id] = 0
        m._last_used[thread_id] = time.monotonic()

        async def _patched_get(_tid):
            return fake_sb

        m.get_or_create_sandbox = _patched_get  # type: ignore[method-assign]
        return m

    async def test_success_returns_stdout(self, monkeypatch):
        fake_sb = AsyncMock()
        fake_sb.run_code = AsyncMock(return_value=_make_e2b_execution(stdout="42\n"))

        m = self._manager_with_sandbox(monkeypatch, fake_sb)
        result = await m.execute("t-exec", "print(42)")

        assert result.exit_code == 0
        assert result.stdout == "42\n"
        assert result.error is None

    async def test_e2b_error_sets_exit_code_1(self, monkeypatch):
        fake_sb = AsyncMock()
        fake_sb.run_code = AsyncMock(
            return_value=_make_e2b_execution(error=Exception("NameError"))
        )

        m = self._manager_with_sandbox(monkeypatch, fake_sb)
        result = await m.execute("t-exec", "print(undefined_var)")

        assert result.exit_code == 1
        assert result.error is not None

    async def test_timeout_returns_exit_code_124(self, monkeypatch):
        from src.services.sandbox import e2b_sandbox_manager as module

        # Every call times out, so the post-timeout probe retries until its
        # window ends; keep that window short.
        monkeypatch.setattr(module, "POST_TIMEOUT_PROBE_SECONDS", 0.2)
        fake_sb = AsyncMock()
        fake_sb.run_code = AsyncMock(side_effect=asyncio.TimeoutError())

        m = self._manager_with_sandbox(monkeypatch, fake_sb)
        result = await m.execute("t-exec", "import time; time.sleep(9999)")

        assert result.exit_code == 124
        assert result.error == "timeout"

    async def test_unexpected_exception_returns_exit_code_1(self, monkeypatch):
        fake_sb = AsyncMock()
        fake_sb.run_code = AsyncMock(side_effect=RuntimeError("connection lost"))

        m = self._manager_with_sandbox(monkeypatch, fake_sb)
        result = await m.execute("t-exec", "x = 1")

        assert result.exit_code == 1
        assert result.error == "execution_error"

    async def test_execution_limit_blocks_call(self, monkeypatch):
        from src.services.sandbox.e2b_sandbox_manager import (
            MAX_EXECUTIONS_PER_RUN,
            SandboxManager,
        )

        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        fake_sb = AsyncMock()
        m = SandboxManager()
        m._sandboxes["t-limit"] = fake_sb
        m._execution_counts["t-limit"] = MAX_EXECUTIONS_PER_RUN

        result = await m.execute("t-limit", "print('hi')")

        assert result.exit_code == 1
        assert result.error == "execution_limit_exceeded"
        fake_sb.run_code.assert_not_called()

    async def test_count_increments_on_each_call(self, monkeypatch):
        fake_sb = AsyncMock()
        fake_sb.run_code = AsyncMock(return_value=_make_e2b_execution())

        m = self._manager_with_sandbox(monkeypatch, fake_sb)
        await m.execute("t-exec", "a = 1")
        await m.execute("t-exec", "b = 2")

        assert m._execution_counts["t-exec"] == 2

    async def test_png_result_captured(self, monkeypatch):
        png_bytes = b"\x89PNG"
        fake_sb = AsyncMock()
        fake_sb.run_code = AsyncMock(
            return_value=_make_e2b_execution(results=[_png_result(png_bytes)])
        )

        m = self._manager_with_sandbox(monkeypatch, fake_sb)
        result = await m.execute("t-exec", "plt.savefig()")

        assert len(result.results) == 1
        assert result.results[0]["type"] == "image"
        assert result.results[0]["format"] == "png"
        assert result.results[0]["data"] == png_bytes

    async def test_text_result_captured(self, monkeypatch):
        fake_sb = AsyncMock()
        fake_sb.run_code = AsyncMock(
            return_value=_make_e2b_execution(
                results=[_text_result("DataFrame summary")]
            )
        )

        m = self._manager_with_sandbox(monkeypatch, fake_sb)
        result = await m.execute("t-exec", "df.describe()")

        assert len(result.results) == 1
        assert result.results[0]["type"] == "text"
        assert result.results[0]["data"] == "DataFrame summary"


# ---------------------------------------------------------------------------
# SandboxManager.install_packages
# ---------------------------------------------------------------------------


class TestSandboxExecutionBounds:
    """Provider doubles exercise bounds without creating paid E2B sandboxes."""

    def _manager(self, monkeypatch, sandbox):
        from src.services.sandbox import e2b_sandbox_manager as module

        monkeypatch.setenv("E2B_API_KEY", "fixture-key")
        monkeypatch.setattr(module, "_e2b_available", True)
        provider = Mock(create=AsyncMock(return_value=sandbox))
        monkeypatch.setattr(module, "AsyncSandbox", provider)
        return module.SandboxManager(), provider

    async def test_timeout_kills_without_waiting_for_other_thread_initialization(
        self, monkeypatch
    ):
        from src.services.sandbox import e2b_sandbox_manager as module

        # The cached box stays busy, so the post-timeout probe fails too.
        monkeypatch.setattr(module, "POST_TIMEOUT_PROBE_SECONDS", 0.2)
        running = asyncio.Event()
        installing = asyncio.Event()
        release_install = asyncio.Event()
        cached = AsyncMock()
        new_box = AsyncMock()

        async def run(*args, **kwargs):
            running.set()
            await asyncio.Event().wait()

        async def install(*args, **kwargs):
            installing.set()
            await release_install.wait()
            return _make_e2b_execution()

        cached.run_code.side_effect = run
        new_box.run_code.side_effect = install
        manager, _ = self._manager(monkeypatch, new_box)
        manager._sandboxes["running"] = cached
        execution = asyncio.create_task(manager.execute("running", "hang", timeout=1))
        await running.wait()
        creation = asyncio.create_task(manager.get_or_create_sandbox("other-thread"))
        await installing.wait()
        try:
            result = await asyncio.wait_for(asyncio.shield(execution), timeout=1.5)
            assert result.error == "timeout"
            cached.kill.assert_awaited_once()
            assert "running" not in manager._sandboxes
            assert manager._execution_counts["running"] == 1
            assert not creation.done()
        finally:
            release_install.set()
            await creation
            await execution
            await manager.cleanup_all()

    async def test_cached_box_does_not_wait_for_other_thread_creation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A thread whose box is cached must not queue behind another thread's
        box creation (up to MAX_EXECUTION_TIMEOUT of DEFAULT_PACKAGES). That
        wait is outside the cell budget, so the agent's outer limit could
        cancel the cell and kill a box that holds the user's variables.

        Mutation: drop the lock-free cache lookup in ``get_or_create_sandbox``;
        the cached thread then waits on the lock and the guard times out.
        """
        installing = asyncio.Event()
        release_install = asyncio.Event()
        cached = _KernelBox()
        new_box = AsyncMock()

        async def install(*args: Any, **kwargs: Any) -> Any:
            installing.set()
            await release_install.wait()
            return _make_e2b_execution()

        new_box.run_code.side_effect = install
        manager, _ = self._manager(monkeypatch, new_box)
        manager._sandboxes["ready"] = cached
        creation = asyncio.create_task(manager.get_or_create_sandbox("other-thread"))
        try:
            await asyncio.wait_for(installing.wait(), timeout=1)
            result = await asyncio.wait_for(
                manager.execute("ready", "print('hi')"), timeout=1
            )
            assert result.stdout == "hi\n"
            assert not creation.done()
        finally:
            release_install.set()
            await creation
            await manager.cleanup_all()

    async def test_transport_failure_retires_box_without_replenishing_budget(
        self, monkeypatch
    ):
        sandbox = AsyncMock()
        sandbox.run_code.side_effect = [
            _make_e2b_execution(),
            RuntimeError("provider connection lost"),
        ]
        manager, _ = self._manager(monkeypatch, sandbox)
        try:
            result = await manager.execute("transport", "code")
            assert result.error == "execution_error"
            sandbox.kill.assert_awaited_once()
            assert "transport" not in manager._sandboxes
            assert manager._execution_counts["transport"] == 1
        finally:
            await manager.cleanup_all()

    @pytest.mark.parametrize("timeout,expected", [(0, 1), (-5, 1), (9999, 300)])
    async def test_execution_timeout_cannot_disable_or_exceed_cap(
        self, monkeypatch, timeout, expected
    ):
        sandbox = AsyncMock()
        sandbox.run_code.return_value = _make_e2b_execution()
        manager, _ = self._manager(monkeypatch, sandbox)
        try:
            result = await manager.execute("bounded", "print(1)", timeout=timeout)
            assert result.exit_code == 0
            assert sandbox.run_code.call_args.kwargs["timeout"] == expected
        finally:
            await manager.cleanup_all()

    async def test_failed_default_packages_kill_box_and_return_safe_error(
        self, monkeypatch
    ):
        sandbox = AsyncMock()
        sandbox.run_code.return_value = _make_e2b_execution(
            error=RuntimeError("private provider install failure")
        )
        manager, _ = self._manager(monkeypatch, sandbox)
        result = await manager.execute("failed-init", "print(1)")
        assert result.error == "execution_error"
        assert "private provider" not in result.stderr
        sandbox.kill.assert_awaited_once()
        assert "failed-init" not in manager._sandboxes
        assert sandbox.run_code.await_count == 1  # user code must never run

    async def test_provider_creation_failure_returns_safe_error(self, monkeypatch):
        manager, provider = self._manager(monkeypatch, AsyncMock())
        provider.create.side_effect = RuntimeError("private provider auth detail")
        result = await manager.execute("failed-create", "print(1)")
        assert result.error == "execution_error"
        assert "private provider" not in result.stderr

    async def test_timeout_kills_box_without_resetting_execution_budget(
        self, monkeypatch
    ):
        from src.services.sandbox import e2b_sandbox_manager as module

        class ProviderTimeout(Exception):
            pass

        monkeypatch.setattr(
            module, "E2BTimeoutException", ProviderTimeout, raising=False
        )
        # Every post-timeout probe also times out: the kernel never recovered.
        monkeypatch.setattr(module, "POST_TIMEOUT_PROBE_SECONDS", 0.2)
        sandbox = AsyncMock()

        async def run(code: str, **kwargs: Any) -> Any:
            if "pip" in code:
                return _make_e2b_execution()
            raise ProviderTimeout("private timeout detail")

        sandbox.run_code.side_effect = run
        manager, _ = self._manager(monkeypatch, sandbox)
        result = await manager.execute("timed-out", "while True: pass")
        assert result.exit_code == 124
        assert result.error == "timeout"
        sandbox.kill.assert_awaited_once()
        assert "timed-out" not in manager._sandboxes
        assert manager._execution_counts["timed-out"] == 1
        assert "timed-out" in manager._last_used  # retained budget still expires
        await manager.cleanup_all()
        assert "timed-out" not in manager._execution_counts

    async def test_sdk_stream_cannot_extend_wall_clock_limit(self, monkeypatch):
        sandbox = AsyncMock()

        async def run(code, **kwargs):
            if code == "while True: pass":
                await asyncio.Event().wait()  # provider never raises its timeout
            return _make_e2b_execution()

        sandbox.run_code.side_effect = run
        manager, _ = self._manager(monkeypatch, sandbox)
        result = await asyncio.wait_for(
            manager.execute("wall-clock", "while True: pass", timeout=1), timeout=2
        )
        assert result.error == "timeout"
        # The kernel answered the probe after the interrupt: keep the box.
        sandbox.kill.assert_not_awaited()
        assert manager._sandboxes["wall-clock"] is sandbox
        await manager.cleanup_all()

    async def test_replacement_box_does_not_replenish_budget(self, monkeypatch):
        from src.services.sandbox import e2b_sandbox_manager as module
        from src.services.sandbox.e2b_sandbox_manager import MAX_EXECUTIONS_PER_RUN

        monkeypatch.setattr(module, "POST_TIMEOUT_PROBE_SECONDS", 0.2)
        sandbox = AsyncMock()

        async def run(code, **kwargs):
            if "pip" in code:
                return _make_e2b_execution()
            # The cell and the post-timeout probe both time out, so each
            # timeout kills the box and the next call needs a replacement.
            raise asyncio.TimeoutError()

        sandbox.run_code.side_effect = run
        manager, provider = self._manager(monkeypatch, sandbox)
        results = [
            await manager.execute("replacement", "while True: pass")
            for _ in range(MAX_EXECUTIONS_PER_RUN + 1)
        ]
        assert results[-1].error == "execution_limit_exceeded"
        assert provider.create.await_count == MAX_EXECUTIONS_PER_RUN

    async def test_cancelled_package_initialization_kills_uncached_box(
        self, monkeypatch
    ):
        started = asyncio.Event()
        sandbox = AsyncMock()

        async def install(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        sandbox.run_code.side_effect = install
        manager, _ = self._manager(monkeypatch, sandbox)
        task = asyncio.create_task(manager.execute("cancelled-init", "print(1)"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        sandbox.kill.assert_awaited_once()
        assert "cancelled-init" not in manager._sandboxes

    async def test_outer_cancellation_kills_remote_box_and_propagates(
        self, monkeypatch
    ):
        started = asyncio.Event()
        sandbox = AsyncMock()

        async def run(code, **kwargs):
            if code == "while True: pass":
                started.set()
                await asyncio.Event().wait()
            return _make_e2b_execution()

        sandbox.run_code.side_effect = run
        manager, _ = self._manager(monkeypatch, sandbox)
        task = asyncio.create_task(manager.execute("cancelled", "while True: pass"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        sandbox.kill.assert_awaited_once()
        assert "cancelled" not in manager._sandboxes
        assert manager._execution_counts["cancelled"] == 1

    async def test_timeout_interrupts_cell_and_keeps_responsive_box(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """IN-2: our limit closes the /execute stream, the server interrupts the
        kernel, and a kernel that then answers keeps the session's variables.

        Mutation: make ``SandboxManager._kernel_answers`` return False; this
        fails on the "still available" assertion. Run from backend/:
        pytest -q tests/unit/services/test_sandbox.py -k keeps_responsive_box
        """
        box = _KernelBox()
        manager, provider = self._manager(monkeypatch, box)
        try:
            assert (await manager.execute("kept", "df = [1, 2, 3]")).exit_code == 0
            timed_out = await manager.execute(
                "kept", "#sleep 5\nmodel = sum(df)", timeout=1
            )
            assert timed_out.error == "timeout"
            assert "still available" in timed_out.stderr
            assert "partly run" in timed_out.stderr
            follow_up = await manager.execute("kept", "print(len(df))")
            assert follow_up.error is None, follow_up.error
            assert follow_up.stdout == "3\n"
            assert manager._sandboxes["kept"] is box and not box.killed
            assert provider.create.await_count == 1
            # DECISION F-2: the timed-out cell still costs one execution; the
            # post-timeout health probe costs none.
            assert manager._execution_counts["kept"] == 3
        finally:
            await manager.cleanup_all()

    async def test_timeout_kills_box_whose_kernel_stays_busy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kernel that still cannot run a no-op after the interrupt (older
        template, or code ignoring SIGINT) is unhealthy: kill it so remote code
        stops (#1864) and say the session was reset."""
        from src.services.sandbox import e2b_sandbox_manager as module

        monkeypatch.setattr(module, "POST_TIMEOUT_PROBE_SECONDS", 0.2)
        sandbox = AsyncMock()

        async def run(code: str, **kwargs: Any) -> Any:
            if "pip" in code:
                return _make_e2b_execution()
            await asyncio.Event().wait()  # busy: the cell, then the probe

        sandbox.run_code.side_effect = run
        manager, _ = self._manager(monkeypatch, sandbox)
        try:
            result = await asyncio.wait_for(
                manager.execute("busy", "while True: pass", timeout=1), timeout=3
            )
            assert result.error == "timeout"
            assert "reset" in result.stderr
            sandbox.kill.assert_awaited_once()
            assert "busy" not in manager._sandboxes
            assert sandbox.run_code.await_count == 3  # install, cell, probe
        finally:
            await manager.cleanup_all()

    async def test_cancellation_during_probe_still_kills_box(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run cancelled while the box is being probed still stops remote
        code: user-initiated cancellation keeps #1864's cleanup."""
        from src.services.sandbox import e2b_sandbox_manager as module

        probing = asyncio.Event()
        sandbox = AsyncMock()

        async def run(code: str, **kwargs: Any) -> Any:
            if "pip" in code:
                return _make_e2b_execution()
            if code == module._KERNEL_PROBE_CODES["python"]:
                probing.set()
            await asyncio.Event().wait()

        sandbox.run_code.side_effect = run
        manager, _ = self._manager(monkeypatch, sandbox)
        task = asyncio.create_task(
            manager.execute("cancel-probe", "while True: pass", timeout=1)
        )
        try:
            await asyncio.wait_for(probing.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            sandbox.kill.assert_awaited_once()
            assert "cancel-probe" not in manager._sandboxes
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await manager.cleanup_all()

    async def test_outer_limit_during_kill_does_not_abandon_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cell that times out and fails its probe ends about 100 s into
        the 120 s outer limit, and the kill may take up to 60 s. The box has
        already left the cache, so if the outer limit cancelling the call also
        cancelled the kill, nothing would retry it and the user's code would
        keep running remotely until the provider expires the box.

        Mutation: await the bounded kill directly in ``_kill_sandbox`` instead
        of shielding its task; the kill is then cancelled and ``killed`` stays
        unset.

        The kill task is held while its caller is gone, and leaves the holding
        set once it finishes, so the set does not grow for the process's life.
        """
        from src.services.sandbox import e2b_sandbox_manager as module

        monkeypatch.setattr(module, "POST_TIMEOUT_PROBE_SECONDS", 0.2)
        monkeypatch.setattr(module, "_PENDING_KILLS", set())
        killing = asyncio.Event()
        release_kill = asyncio.Event()
        killed = asyncio.Event()
        sandbox = AsyncMock()

        async def run(code: str, **kwargs: Any) -> Any:
            if "pip" in code:
                return _make_e2b_execution()
            await asyncio.Event().wait()  # busy: the cell, then the probe

        async def kill() -> None:
            killing.set()
            await release_kill.wait()
            killed.set()

        sandbox.run_code.side_effect = run
        sandbox.kill.side_effect = kill
        manager, _ = self._manager(monkeypatch, sandbox)
        task = asyncio.create_task(
            manager.execute("slow-kill", "while True: pass", timeout=1)
        )
        try:
            await asyncio.wait_for(killing.wait(), timeout=3)
            assert "slow-kill" not in manager._sandboxes
            task.cancel()  # the agent's outer limit
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(module._PENDING_KILLS) == 1
            (kill_task,) = module._PENDING_KILLS
            release_kill.set()
            await asyncio.wait_for(killed.wait(), timeout=1)
            sandbox.kill.assert_awaited_once()
            await asyncio.wait_for(kill_task, timeout=1)
            await asyncio.sleep(0)  # let the task's done callbacks run
            assert module._PENDING_KILLS == set()
        finally:
            release_kill.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await manager.cleanup_all()

    @pytest.mark.parametrize("first_probe", ["hangs", "sdk_timeout", "aborted"])
    async def test_probe_retries_until_interrupted_kernel_answers(
        self, monkeypatch: pytest.MonkeyPatch, first_probe: str
    ) -> None:
        """A probe sent before the interrupt lands can hang, hit the SDK's
        timeout, or come back aborted (ipykernel ``stop_on_error``). A later
        probe in the same window answers, so the box is kept.

        Mutation: make ``_kernel_answers`` give up after one attempt; every
        case then fails on the "still available" assertion.
        """
        from src.services.sandbox import e2b_sandbox_manager as module

        class ProviderTimeout(Exception):
            pass

        monkeypatch.setattr(
            module, "E2BTimeoutException", ProviderTimeout, raising=False
        )
        monkeypatch.setattr(module, "_PROBE_ATTEMPT_SECONDS", 0.2)
        probes: list[str] = []
        sandbox = AsyncMock()

        async def run(code: str, **kwargs: Any) -> Any:
            if "pip" in code:
                return _make_e2b_execution()
            if code == "while True: pass":
                await asyncio.Event().wait()
            probes.append(code)
            if len(probes) > 1:
                return _make_e2b_execution(stdout="")
            if first_probe == "hangs":
                await asyncio.Event().wait()
            if first_probe == "sdk_timeout":
                raise ProviderTimeout("probe timed out")
            return _make_e2b_execution(error=_execution_error("ExecutionAborted"))

        sandbox.run_code.side_effect = run
        manager, _ = self._manager(monkeypatch, sandbox)
        try:
            result = await asyncio.wait_for(
                manager.execute("retry", "while True: pass", timeout=1), timeout=5
            )
            assert result.error == "timeout"
            assert "still available" in result.stderr
            assert len(probes) == 2
            sandbox.kill.assert_not_awaited()
            assert manager._sandboxes["retry"] is sandbox
        finally:
            await manager.cleanup_all()

    async def test_probe_error_resets_box_without_retrying(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A probe that fails with a real error (not an abort) means the kernel
        is broken: reset at once, log why, and do not spend the window."""
        sandbox = AsyncMock()

        async def run(code: str, **kwargs: Any) -> Any:
            if "pip" in code:
                return _make_e2b_execution()
            if code == "while True: pass":
                await asyncio.Event().wait()
            return _make_e2b_execution(error=_execution_error("RuntimeError"))

        sandbox.run_code.side_effect = run
        manager, _ = self._manager(monkeypatch, sandbox)
        caplog.set_level(logging.INFO, logger="src.services.sandbox")
        try:
            result = await manager.execute("broken", "while True: pass", timeout=1)
            assert result.error == "timeout"
            assert "reset" in result.stderr
            sandbox.kill.assert_awaited_once()
            assert "broken" not in manager._sandboxes
            assert sandbox.run_code.await_count == 3  # install, cell, one probe
            records = [(r.levelno, r.getMessage()) for r in caplog.records]
            assert any(
                level == logging.WARNING and "RuntimeError" in message
                for level, message in records
            ), records
            assert any(
                level == logging.INFO and "resetting" in message
                for level, message in records
            ), records
        finally:
            await manager.cleanup_all()

    @pytest.mark.parametrize(
        "language,probe_code",
        [("python", "1"), ("bash", "true"), ("sh", "true"), ("r", "1")],
    )
    async def test_probe_uses_a_no_op_in_the_cell_language(
        self, monkeypatch: pytest.MonkeyPatch, language: str, probe_code: str
    ) -> None:
        """``language`` comes from the model: a bash cell's kernel must be
        probed with a shell no-op, not Python. Unknown languages fall back to
        the Python probe."""
        sandbox = AsyncMock()

        async def run(code: str, **kwargs: Any) -> Any:
            if "pip" in code:
                return _make_e2b_execution()
            if code == "sleep 600":
                await asyncio.Event().wait()
            return _make_e2b_execution(stdout="")

        sandbox.run_code.side_effect = run
        manager, _ = self._manager(monkeypatch, sandbox)
        try:
            result = await manager.execute(
                "lang", "sleep 600", language=language, timeout=1
            )
            assert "still available" in result.stderr
            probe = sandbox.run_code.await_args
            assert probe.args[0] == probe_code
            assert probe.kwargs["language"] == language
        finally:
            await manager.cleanup_all()

    async def test_probe_loop_stops_on_its_own_deadline(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Python 3.11's ``wait_for`` can swallow the window's cancellation when
        an attempt finishes in the same loop iteration. The window's timer is
        disabled here to model that: the loop must still stop on its own
        deadline, and no attempt may be given time past it.

        Mutation: drop the deadline check and the loop spins until the guard
        below times out; drop the ``min(..., left)`` clamp and the first
        attempt outlives the window.
        """
        from src.services.sandbox import e2b_sandbox_manager as module

        window = 0.3
        monkeypatch.setattr(module, "POST_TIMEOUT_PROBE_SECONDS", window)
        monkeypatch.setattr(module, "_PROBE_RETRY_PAUSE_SECONDS", 0.02)
        monkeypatch.setattr(asyncio, "timeout", lambda _delay: contextlib.nullcontext())
        loop = asyncio.get_running_loop()
        attempts: list[tuple[float, float]] = []
        sandbox = AsyncMock()

        async def run(code: str, **kwargs: Any) -> Any:
            attempts.append((loop.time(), kwargs["timeout"]))
            raise asyncio.TimeoutError()  # every attempt times out at once

        sandbox.run_code.side_effect = run
        manager = module.SandboxManager()
        caplog.set_level(logging.WARNING, logger="src.services.sandbox")
        start = loop.time()
        answered = await asyncio.wait_for(
            manager._kernel_answers("deadline", sandbox, "python"), timeout=3
        )
        elapsed = loop.time() - start

        assert answered is False
        assert window <= elapsed < window + 1
        assert len(attempts) > 1
        # The first attempt starts in the same step that sets the deadline.
        first_start = attempts[0][0]
        assert all(
            started + limit <= first_start + window + 0.01
            for started, limit in attempts
        ), attempts
        assert any("no answer" in r.getMessage() for r in caplog.records)
        sandbox.kill.assert_not_awaited()

    async def test_failed_executions_still_consume_budget(self, monkeypatch):
        from src.services.sandbox.e2b_sandbox_manager import MAX_EXECUTIONS_PER_RUN

        sandbox = AsyncMock()
        user_calls = []

        async def run(code, **kwargs):
            if code == "print(1)":
                user_calls.append(code)
                raise RuntimeError("connection lost")
            return _make_e2b_execution()

        sandbox.run_code.side_effect = run
        manager, _ = self._manager(monkeypatch, sandbox)
        try:
            results = [
                await manager.execute("failed", "print(1)")
                for _ in range(MAX_EXECUTIONS_PER_RUN + 1)
            ]
            assert results[-1].error == "execution_limit_exceeded"
            assert all(result.error == "execution_error" for result in results[:-1])
            assert len(user_calls) == MAX_EXECUTIONS_PER_RUN
        finally:
            await manager.cleanup_all()

    async def test_concurrent_executions_reserve_budget_before_provider_call(
        self, monkeypatch
    ):
        # Mutation: moving the reservation below the provider await makes this
        # test fail (six user executions reach the provider instead of five).
        # Run with PYTHONPATH=backend pytest --confcutdir=backend/tests/unit/services
        # backend/tests/unit/services/test_sandbox.py -k concurrent_executions.
        from src.services.sandbox.e2b_sandbox_manager import MAX_EXECUTIONS_PER_RUN

        started = asyncio.Event()
        release = asyncio.Event()
        user_calls = []
        sandbox = AsyncMock()

        async def run(code, **kwargs):
            if code == "print(1)":
                user_calls.append(code)
                started.set()
                await release.wait()
            return _make_e2b_execution()

        sandbox.run_code.side_effect = run
        manager, _ = self._manager(monkeypatch, sandbox)
        tasks = [
            asyncio.create_task(manager.execute("racing", "print(1)"))
            for _ in range(MAX_EXECUTIONS_PER_RUN + 1)
        ]
        try:
            await started.wait()
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(*tasks)
            assert len(user_calls) == MAX_EXECUTIONS_PER_RUN
            assert sum(r.error == "execution_limit_exceeded" for r in results) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await manager.cleanup_all()


class TestInstallPackages:
    async def test_valid_packages_produce_pip_command(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        from src.services.sandbox.e2b_sandbox_manager import (
            ExecutionResult,
            SandboxManager,
        )

        captured: list[str] = []

        async def fake_execute(thread_id, code, **kwargs):
            captured.append(code)
            return ExecutionResult(
                stdout="", stderr="", exit_code=0, execution_time_ms=5
            )

        m = SandboxManager()
        m.execute = fake_execute  # type: ignore[assignment]
        await m.install_packages("t-pkg", ["numpy", "scikit-learn"])

        assert captured, "execute should have been called"
        assert "numpy" in captured[0]
        assert "scikit-learn" in captured[0]

    async def test_all_invalid_names_returns_error_without_calling_execute(
        self, monkeypatch
    ):
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        from src.services.sandbox.e2b_sandbox_manager import SandboxManager

        m = SandboxManager()
        called = []

        async def fake_execute(*a, **kw):
            called.append(True)

        m.execute = fake_execute  # type: ignore[assignment]
        result = await m.install_packages("t-pkg", ["../evil", "rm -rf /", "pkg==1.0"])

        assert result.exit_code == 1
        assert result.error == "invalid_packages"
        assert not called

    async def test_hyphenated_package_name_is_valid(self, monkeypatch):
        """scikit-learn, torch-geometric etc. should pass validation."""
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        from src.services.sandbox.e2b_sandbox_manager import (
            ExecutionResult,
            SandboxManager,
        )

        executed = []

        async def fake_execute(thread_id, code, **kwargs):
            executed.append(code)
            return ExecutionResult(
                stdout="", stderr="", exit_code=0, execution_time_ms=5
            )

        m = SandboxManager()
        m.execute = fake_execute  # type: ignore[assignment]
        result = await m.install_packages("t-pkg", ["scikit-learn", "torch-geometric"])

        assert executed  # did not short-circuit
        assert result.exit_code == 0

    def test_reset_execution_count_zeroes_counter(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        from src.services.sandbox.e2b_sandbox_manager import SandboxManager

        m = SandboxManager()
        m._execution_counts["t-r"] = 5
        m.reset_execution_count("t-r")
        assert m._execution_counts["t-r"] == 0


# ---------------------------------------------------------------------------
# SandboxManager.cleanup
# ---------------------------------------------------------------------------


class TestSandboxManagerCleanup:
    async def test_cleanup_kills_sandbox_and_removes_state(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        from src.services.sandbox.e2b_sandbox_manager import SandboxManager

        fake_sb = AsyncMock()
        m = SandboxManager()
        m._sandboxes["t-clean"] = fake_sb
        m._last_used["t-clean"] = 0.0
        m._execution_counts["t-clean"] = 3

        await m.cleanup("t-clean")

        fake_sb.kill.assert_called_once()
        assert "t-clean" not in m._sandboxes
        assert "t-clean" not in m._last_used

    async def test_cleanup_all_kills_every_sandbox(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        from src.services.sandbox.e2b_sandbox_manager import SandboxManager

        sb1, sb2 = AsyncMock(), AsyncMock()
        m = SandboxManager()
        m._sandboxes = {"t1": sb1, "t2": sb2}
        m._last_used = {"t1": 0.0, "t2": 0.0}
        m._execution_counts = {"t1": 0, "t2": 0}

        await m.cleanup_all()

        sb1.kill.assert_called_once()
        sb2.kill.assert_called_once()
        assert not m._sandboxes

    async def test_cleanup_survives_kill_error(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        from src.services.sandbox.e2b_sandbox_manager import SandboxManager

        fake_sb = AsyncMock()
        fake_sb.kill = AsyncMock(side_effect=Exception("network error"))
        m = SandboxManager()
        m._sandboxes["t-err"] = fake_sb
        m._last_used["t-err"] = 0.0
        m._execution_counts["t-err"] = 0

        await m.cleanup("t-err")  # must not raise

        assert "t-err" not in m._sandboxes

    async def test_cleanup_noop_for_unknown_thread(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "key-abc")
        from src.services.sandbox.e2b_sandbox_manager import SandboxManager

        m = SandboxManager()
        await m.cleanup("nonexistent")  # must not raise


# ---------------------------------------------------------------------------
# _tool_execute_code
#
# tools_impl.py lives inside src/api/agent/ whose __init__.py eagerly imports
# execute.py → langgraph.  Import the module directly by file path so the
# package __init__ is never executed.
# ---------------------------------------------------------------------------

import importlib.util
import pathlib


def _make_stub(name: str):
    """Create a MagicMock module stub and register all its prefixes."""
    from unittest.mock import MagicMock

    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        key = ".".join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = MagicMock()
    return sys.modules[name]


# Stub every module that tools_impl transitively needs but isn't installed
_HEAVY_STUBS = [
    "langgraph",
    "langgraph.errors",
    "langgraph.graph",
    "langgraph.checkpoint",
    "langgraph.checkpoint.postgres",
    "langgraph.checkpoint.memory",
    "langsmith",
    "langsmith.run_helpers",
    "celery",
    "redis",
    "qdrant_client",
    "qdrant_client.http",
    "neo4j",
    "openai",
    "anthropic",
    "cohere",
    "tiktoken",
    "sentence_transformers",
    "transformers",
    "boto3",
    "botocore",
    "PIL",
    "pymupdf",
    "fitz",
]
# Installed (and later removed) by the module-scoped _isolated_import_stubs
# fixture at the top of this file — never at import time.


def _import_tools_impl():
    """Return the tools_impl module, importing it under its real package name.

    Bypasses the package __init__ chain (which can drag in heavier deps) by
    pre-populating sys.modules with stubs for the sibling module and then
    loading tools_impl.py directly under its canonical dotted name
    (src/services/agent/ — the shim-free home since audit B1/B5).
    """
    key = "src.services.agent.tools_impl"
    if key in sys.modules:
        return sys.modules[key]

    agent_dir = pathlib.Path(__file__).parents[3] / "src" / "services" / "agent"

    # Ensure the package hierarchy exists in sys.modules without running __init__.py
    for pkg in ("src.services", "src.services.agent"):
        if pkg not in sys.modules:
            m = ModuleType(pkg)
            m.__path__ = [str(agent_dir.parent if "agent" not in pkg else agent_dir)]
            m.__package__ = pkg
            sys.modules[pkg] = m

    # Stub the relatively-imported sibling with MagicMock so attribute
    # imports succeed without dragging in its dependency chain.
    from unittest.mock import MagicMock as _MM

    for sibling in ("tool_helpers",):
        sibling_key = f"src.services.agent.{sibling}"
        if sibling_key not in sys.modules:
            sys.modules[sibling_key] = _MM()

    spec = importlib.util.spec_from_file_location(
        key,
        str(agent_dir / "tools_impl.py"),
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    mod.__package__ = "src.services.agent"
    sys.modules[key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestToolExecuteCode:
    async def test_requires_authenticated_user(self):
        tools_impl = _import_tools_impl()
        result = await tools_impl._tool_execute_code(
            {"code": "print(1)"}, current_user=None, thread_id="t-test"
        )
        assert "error" in result
        assert "authentication" in result["error"].lower()

    async def test_requires_non_empty_code(self):
        tools_impl = _import_tools_impl()
        result = await tools_impl._tool_execute_code(
            {"code": ""}, current_user=_mock_user(), thread_id="t-test"
        )
        assert "error" in result
        assert "no code" in result["error"].lower()

    async def test_error_when_e2b_not_configured(self):
        tools_impl = _import_tools_impl()
        mock_mgr = Mock()
        mock_mgr.is_available = False

        with patch(
            "src.services.sandbox.e2b_sandbox_manager.get_sandbox_manager",
            return_value=mock_mgr,
        ):
            result = await tools_impl._tool_execute_code(
                {"code": "print(1)"}, current_user=_mock_user(), thread_id="t-test"
            )

        assert "error" in result
        assert "E2B_API_KEY" in result["error"]

    async def test_success_response_shape(self):
        from src.services.sandbox.e2b_sandbox_manager import ExecutionResult

        tools_impl = _import_tools_impl()
        mock_mgr = AsyncMock()
        mock_mgr.is_available = True
        mock_mgr.execute = AsyncMock(
            return_value=ExecutionResult(
                stdout="Result: 7\n", stderr="", exit_code=0, execution_time_ms=55
            )
        )

        with patch(
            "src.services.sandbox.e2b_sandbox_manager.get_sandbox_manager",
            return_value=mock_mgr,
        ):
            result = await tools_impl._tool_execute_code(
                {"code": "print('Result:', 3 + 4)", "description": "addition"},
                thread_id="t-tool",
                current_user=_mock_user(),
            )

        assert result["status"] == "success"
        assert result["stdout"] == "Result: 7\n"
        assert result["execution_time_ms"] == 55
        assert result["description"] == "addition"

    async def test_failed_execution_sets_error_status(self):
        from src.services.sandbox.e2b_sandbox_manager import ExecutionResult

        tools_impl = _import_tools_impl()
        mock_mgr = AsyncMock()
        mock_mgr.is_available = True
        mock_mgr.execute = AsyncMock(
            return_value=ExecutionResult(
                stdout="",
                stderr="NameError: name 'x' is not defined",
                exit_code=1,
                execution_time_ms=10,
                error="NameError: name 'x' is not defined",
            )
        )

        with patch(
            "src.services.sandbox.e2b_sandbox_manager.get_sandbox_manager",
            return_value=mock_mgr,
        ):
            result = await tools_impl._tool_execute_code(
                {"code": "print(x)"}, current_user=_mock_user(), thread_id="t-test"
            )

        assert result["status"] == "error"
        assert "error" in result

    async def test_packages_installed_before_execution(self):
        from src.services.sandbox.e2b_sandbox_manager import (
            AGENT_CELL_TIMEOUT_SECONDS,
            ExecutionResult,
        )

        tools_impl = _import_tools_impl()
        ok = ExecutionResult(stdout="", stderr="", exit_code=0, execution_time_ms=5)
        mock_mgr = AsyncMock()
        mock_mgr.is_available = True
        mock_mgr.install_packages = AsyncMock(return_value=ok)
        mock_mgr.execute = AsyncMock(
            return_value=ExecutionResult(
                stdout="done\n", stderr="", exit_code=0, execution_time_ms=20
            )
        )

        with patch(
            "src.services.sandbox.e2b_sandbox_manager.get_sandbox_manager",
            return_value=mock_mgr,
        ):
            result = await tools_impl._tool_execute_code(
                {"code": "from rdkit import Chem", "packages": ["rdkit"]},
                thread_id="t-pkg",
                current_user=_mock_user(),
            )

        mock_mgr.install_packages.assert_awaited_once_with(
            "t-pkg", ["rdkit"], timeout=AGENT_CELL_TIMEOUT_SECONDS
        )
        # IN-2: package install and the cell share one budget per tool call.
        cell_timeout = mock_mgr.execute.await_args.kwargs["timeout"]
        assert 1 <= cell_timeout <= AGENT_CELL_TIMEOUT_SECONDS
        assert result["status"] == "success"

    async def test_package_install_warning_does_not_abort(self):
        """A failed install still runs the code, and the install error reaches
        the model as a warning instead of only the server log."""
        from src.services.sandbox.e2b_sandbox_manager import ExecutionResult

        tools_impl = _import_tools_impl()
        bad_install = ExecutionResult(
            stdout="",
            stderr="WARNING: ...",
            exit_code=1,
            execution_time_ms=5,
            error="some warning",
        )
        mock_mgr = AsyncMock()
        mock_mgr.is_available = True
        mock_mgr.install_packages = AsyncMock(return_value=bad_install)
        mock_mgr.execute = AsyncMock(
            return_value=ExecutionResult(
                stdout="ok\n", stderr="", exit_code=0, execution_time_ms=20
            )
        )

        with patch(
            "src.services.sandbox.e2b_sandbox_manager.get_sandbox_manager",
            return_value=mock_mgr,
        ):
            result = await tools_impl._tool_execute_code(
                {"code": "print('ok')", "packages": ["somelib"]},
                current_user=_mock_user(),
                thread_id="t-test",
            )

        mock_mgr.execute.assert_awaited_once()
        assert result["status"] == "success"
        assert "error" not in result
        assert "some warning" in result["warning"]
        assert "WARNING: ..." in result["warning"]

    async def _run_with_install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        install: Any = None,
        *,
        budget: int | None = None,
        packages: list[str] | None = None,
    ) -> tuple[dict[str, Any], AsyncMock]:
        """Run the tool against a mocked manager. ``install`` is the
        ``install_packages`` side effect; ``budget`` overrides
        AGENT_CELL_TIMEOUT_SECONDS."""
        from src.services.sandbox import e2b_sandbox_manager as module

        if budget is not None:
            monkeypatch.setattr(module, "AGENT_CELL_TIMEOUT_SECONDS", budget)
        tools_impl = _import_tools_impl()
        mock_mgr = AsyncMock()
        mock_mgr.is_available = True
        mock_mgr.install_packages = AsyncMock(side_effect=install)
        mock_mgr.execute = AsyncMock(
            return_value=module.ExecutionResult(
                stdout="ok\n", stderr="", exit_code=0, execution_time_ms=20
            )
        )
        monkeypatch.setattr(module, "get_sandbox_manager", lambda: mock_mgr)
        result = await tools_impl._tool_execute_code(
            {"code": "print('ok')", "packages": packages},
            thread_id="t-budget",
            current_user=_mock_user(),
        )
        return result, mock_mgr

    async def test_install_and_cell_share_one_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """IN-2: the cell gets what the install left of the per-call budget,
        rounded up.

        Mutation: give the cell the full AGENT_CELL_TIMEOUT_SECONDS; this
        fails on ``3 == 2``.
        """
        from src.services.sandbox.e2b_sandbox_manager import ExecutionResult

        async def slow_install(*_args: Any, **_kwargs: Any) -> Any:
            await asyncio.sleep(1.2)
            return ExecutionResult(
                stdout="", stderr="", exit_code=0, execution_time_ms=1200
            )

        result, mgr = await self._run_with_install(
            monkeypatch, slow_install, budget=3, packages=["rdkit"]
        )
        assert mgr.install_packages.await_args.kwargs["timeout"] == 3
        assert mgr.execute.await_args.kwargs["timeout"] == 2  # ceil(3 - 1.2)
        assert result["status"] == "success"

    async def test_cell_without_packages_gets_the_whole_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rounding the remaining budget down gave the cell 89 s of 90."""
        from src.services.sandbox.e2b_sandbox_manager import AGENT_CELL_TIMEOUT_SECONDS

        _, mgr = await self._run_with_install(monkeypatch)
        mgr.install_packages.assert_not_awaited()
        assert mgr.execute.await_args.kwargs["timeout"] == AGENT_CELL_TIMEOUT_SECONDS

    async def test_install_timeout_skips_the_cell(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An install that timed out used the whole budget. Running the cell
        on the 1 s minimum would burn an execution and report the install's
        timeout as the code's own, so the code is not run and the install's
        kept/reset message is passed on.

        Mutation: drop the early return; the cell runs and this fails on
        ``execute.assert_not_awaited``.
        """
        from src.services.sandbox.e2b_sandbox_manager import ExecutionResult

        reset = (
            "Execution timed out after 90s. The sandbox was reset, so earlier "
            "variables are gone."
        )

        async def timed_out(*_args: Any, **_kwargs: Any) -> Any:
            return ExecutionResult(
                stdout="",
                stderr=reset,
                exit_code=124,
                execution_time_ms=90_000,
                error="timeout",
            )

        result, mgr = await self._run_with_install(
            monkeypatch, timed_out, packages=["rdkit"]
        )
        mgr.execute.assert_not_awaited()
        assert result["status"] == "error"
        assert result["error"] == "package_install_timeout"
        assert result["exit_code"] == 124
        assert "was not run" in result["stderr"]
        assert (
            'Install the packages in a separate call (for example with code="pass"), '
            "then run the code without packages." in result["stderr"]
        )
        assert reset in result["stderr"]

    async def test_install_that_spends_the_budget_skips_the_cell(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An install that finished with under 1 s of budget left also skips
        the cell rather than run it on the 1 s minimum.

        Mutation: drop the remaining-budget check; the cell runs and this
        fails on ``execute.assert_not_awaited``.
        """
        from src.services.sandbox.e2b_sandbox_manager import ExecutionResult

        async def slow_install(*_args: Any, **_kwargs: Any) -> Any:
            await asyncio.sleep(1.2)
            return ExecutionResult(
                stdout="", stderr="", exit_code=0, execution_time_ms=1200
            )

        result, mgr = await self._run_with_install(
            monkeypatch, slow_install, budget=2, packages=["rdkit"]
        )
        mgr.execute.assert_not_awaited()
        assert result["error"] == "package_install_timeout"
        assert "was not run" in result["stderr"]
        assert "separate call" in result["stderr"]

    async def test_budget_ends_before_the_outer_limit_after_slow_setup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The agent's outer limit starts before execute_tool resolves the user
        and claims the operation row. If that took 30 s, a fresh 90 s budget
        plus its probe would end after the 120 s outer limit, which would then
        cancel the cell and kill the box. Install and cell must instead end
        AGENT_CELL_HEADROOM_SECONDS before the outer limit.

        Mutation: drop the outer-deadline clamp in ``_tool_execute_code``; both
        timeouts are then the full budget and this fails on ``90 == 60``.
        """
        from src.services.agent.tool_deadline import tool_call_deadline_scope
        from src.services.sandbox.e2b_sandbox_manager import (
            AGENT_CELL_HEADROOM_SECONDS,
            AGENT_CELL_TIMEOUT_SECONDS,
            ExecutionResult,
        )

        async def install(*_args: Any, **_kwargs: Any) -> Any:
            return ExecutionResult(
                stdout="", stderr="", exit_code=0, execution_time_ms=1
            )

        setup = 30
        outer = AGENT_CELL_TIMEOUT_SECONDS + AGENT_CELL_HEADROOM_SECONDS - setup
        with tool_call_deadline_scope(outer):
            result, mgr = await self._run_with_install(
                monkeypatch, install, packages=["rdkit"]
            )
        assert mgr.install_packages.await_args.kwargs["timeout"] == (
            AGENT_CELL_TIMEOUT_SECONDS - setup
        )
        assert mgr.execute.await_args.kwargs["timeout"] == (
            AGENT_CELL_TIMEOUT_SECONDS - setup
        )
        assert result["status"] == "success"

    async def test_code_is_not_run_when_setup_used_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setup that leaves under 1 s before the headroom skips the install
        and the cell: even the manager's 1 s minimum could outlast the outer
        limit, whose cancellation kills the box. No execution is spent.

        Mutation: drop the early return; the install runs and this fails on
        ``install_packages.assert_not_awaited``.

        The failure is the backend's slowness, not the code's, so the tool node
        must classify it transient (no error-ceiling charge) rather than fatal,
        which tells the agent not to recover. The model reads the payload, so
        the payload carries the same suggestion as the classification.
        """
        from src.services.agent.error_recovery import classify_error_from_payload
        from src.services.agent.tool_deadline import tool_call_deadline_scope
        from src.services.sandbox.e2b_sandbox_manager import AGENT_CELL_HEADROOM_SECONDS

        with tool_call_deadline_scope(AGENT_CELL_HEADROOM_SECONDS + 0.5):
            result, mgr = await self._run_with_install(monkeypatch, packages=["rdkit"])
        mgr.install_packages.assert_not_awaited()
        mgr.execute.assert_not_awaited()
        assert result["status"] == "error"
        assert result["error"] == "sandbox_budget_exhausted"
        assert "was not run" in result["stderr"]
        classified = classify_error_from_payload("execute_code", result)
        assert classified.category == "transient"
        assert classified.suggestion
        assert result["suggestion"] == classified.suggestion

    async def test_image_outputs_included_in_response(self):
        from src.services.sandbox.e2b_sandbox_manager import ExecutionResult

        tools_impl = _import_tools_impl()
        outputs = [{"type": "image", "format": "png", "data": b"\x89PNG"}]
        mock_mgr = AsyncMock()
        mock_mgr.is_available = True
        mock_mgr.execute = AsyncMock(
            return_value=ExecutionResult(
                stdout="", stderr="", exit_code=0, execution_time_ms=30, results=outputs
            )
        )

        with patch(
            "src.services.sandbox.e2b_sandbox_manager.get_sandbox_manager",
            return_value=mock_mgr,
        ):
            result = await tools_impl._tool_execute_code(
                {"code": "plt.savefig('fig.png')"},
                current_user=_mock_user(),
                thread_id="t-test",
            )

        assert "outputs" in result
        assert result["outputs"][0]["type"] == "image"

    async def test_thread_id_passed_to_execute(self):
        from src.services.sandbox.e2b_sandbox_manager import ExecutionResult

        tools_impl = _import_tools_impl()
        mock_mgr = AsyncMock()
        mock_mgr.is_available = True
        mock_mgr.execute = AsyncMock(
            return_value=ExecutionResult(
                stdout="", stderr="", exit_code=0, execution_time_ms=10
            )
        )

        with patch(
            "src.services.sandbox.e2b_sandbox_manager.get_sandbox_manager",
            return_value=mock_mgr,
        ):
            await tools_impl._tool_execute_code(
                {"code": "x = 1"},
                thread_id="my-thread-id",
                current_user=_mock_user(),
            )

        call_kwargs = mock_mgr.execute.call_args
        assert (
            call_kwargs[1].get("thread_id") == "my-thread-id"
            or call_kwargs[0][0] == "my-thread-id"
        )

    async def test_nonzero_exit_without_structured_error_sets_stderr_as_error(self):
        """Regression: non-zero exit with error=None used to return NO "error"
        key, so classify_error_from_payload marked the run completed and the
        LLM was told the code succeeded."""
        from src.services.sandbox.e2b_sandbox_manager import ExecutionResult

        tools_impl = _import_tools_impl()
        mock_mgr = AsyncMock()
        mock_mgr.is_available = True
        mock_mgr.execute = AsyncMock(
            return_value=ExecutionResult(
                stdout="",
                stderr="Traceback: ZeroDivisionError\n",
                exit_code=1,
                execution_time_ms=12,
                error=None,
            )
        )

        with patch(
            "src.services.sandbox.e2b_sandbox_manager.get_sandbox_manager",
            return_value=mock_mgr,
        ):
            result = await tools_impl._tool_execute_code(
                {"code": "1/0"}, current_user=_mock_user(), thread_id="t-test"
            )

        assert result["status"] == "error"
        assert result["error"] == "Traceback: ZeroDivisionError"

    async def test_nonzero_exit_with_empty_stderr_gets_generic_error(self):
        from src.services.sandbox.e2b_sandbox_manager import ExecutionResult

        tools_impl = _import_tools_impl()
        mock_mgr = AsyncMock()
        mock_mgr.is_available = True
        mock_mgr.execute = AsyncMock(
            return_value=ExecutionResult(
                stdout="", stderr="", exit_code=137, execution_time_ms=12, error=None
            )
        )

        with patch(
            "src.services.sandbox.e2b_sandbox_manager.get_sandbox_manager",
            return_value=mock_mgr,
        ):
            result = await tools_impl._tool_execute_code(
                {"code": "while True: pass"},
                current_user=_mock_user(),
                thread_id="t-test",
            )

        assert result["status"] == "error"
        assert result["error"] == "Code exited with status 137"

    async def test_zero_exit_has_no_spurious_error_key(self):
        from src.services.sandbox.e2b_sandbox_manager import ExecutionResult

        tools_impl = _import_tools_impl()
        mock_mgr = AsyncMock()
        mock_mgr.is_available = True
        mock_mgr.execute = AsyncMock(
            return_value=ExecutionResult(
                stdout="ok\n", stderr="", exit_code=0, execution_time_ms=5
            )
        )

        with patch(
            "src.services.sandbox.e2b_sandbox_manager.get_sandbox_manager",
            return_value=mock_mgr,
        ):
            result = await tools_impl._tool_execute_code(
                {"code": "print('ok')"}, current_user=_mock_user(), thread_id="t-test"
            )

        assert result["status"] == "success"
        assert "error" not in result

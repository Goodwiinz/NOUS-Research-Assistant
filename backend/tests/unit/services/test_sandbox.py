"""Unit tests for sandboxed code execution (GOO-187).

Covers:
 - SandboxManager lifecycle: availability, create, execute, install, cleanup
 - _tool_execute_code: auth, validation, success, error, package install, outputs
"""

from __future__ import annotations

import asyncio
import sys
import time
from types import ModuleType
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
        from src.services.sandbox.e2b_sandbox_manager import ExecutionResult

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

        mock_mgr.install_packages.assert_awaited_once_with("t-pkg", ["rdkit"])
        assert result["status"] == "success"

    async def test_package_install_warning_does_not_abort(self):
        """A non-zero install result should log a warning but still run the code."""
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

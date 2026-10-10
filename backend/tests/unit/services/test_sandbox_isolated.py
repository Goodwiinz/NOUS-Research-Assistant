"""GOO-312 ``SandboxManager.run_isolated`` against a fake e2b ``AsyncSandbox``.

The fake mirrors the pinned SDK surface (``e2b-code-interpreter==2.10.0``
over ``e2b>=2.44.0``): ``AsyncSandbox.create(template=, timeout=, envs=)``,
``files.write(path, data)``, ``files.read(path, format="bytes")``,
``commands.run(cmd, envs=, cwd=, timeout=)`` raising an exception that
carries ``exit_code``/``stdout``/``stderr`` on a non-zero exit,
``get_info().template_id`` and ``kill()``.

Mutation verification: calling ``get_or_create_sandbox`` inside
``run_isolated`` fails ``-k thread_cache`` (``_sandboxes`` not empty);
skipping GOO-313's in-sandbox re-hash fails ``-k input_hash`` (the command
runs).
"""

import asyncio
import hashlib
import shlex
from types import SimpleNamespace
from typing import Any

import pytest

from src.services.sandbox import e2b_sandbox_manager as module
from src.services.sandbox.e2b_sandbox_manager import (
    IsolatedSpec,
    RestoreSpec,
    SandboxManager,
)

pytestmark = pytest.mark.unit


class _Exit(Exception):
    def __init__(self, code: int, stdout: str, stderr: str) -> None:
        super().__init__("exit")
        self.exit_code, self.stdout, self.stderr = code, stdout, stderr


class TimeoutException(Exception):
    """Same class name as ``e2b.exceptions.TimeoutException``."""


class _FakeSandbox:
    created: list["_FakeSandbox"] = []
    fail_create = False

    def __init__(self, **kwargs: Any) -> None:
        self.create_kwargs = kwargs
        self.sandbox_id = f"sbx-{len(self.created)}"
        self.fs: dict[str, bytes] = {"/etc/os-release": b'ID="debian"\n'}
        self.commands_run: list[tuple[str, dict[str, Any]]] = []
        self.killed = False
        self.on_command: Any = None
        self.files = SimpleNamespace(write=self._write, read=self._read)
        self.commands = SimpleNamespace(run=self._run)

    @classmethod
    async def create(cls, **kwargs: Any) -> "_FakeSandbox":
        if cls.fail_create:
            raise RuntimeError("no capacity")
        sandbox = cls(**kwargs)
        cls.created.append(sandbox)
        return sandbox

    async def get_info(self) -> Any:
        return SimpleNamespace(template_id=f"tid-{self.create_kwargs['template']}")

    async def kill(self) -> None:
        self.killed = True

    async def _write(self, path: str, data: str | bytes) -> None:
        self.fs[path] = data.encode() if isinstance(data, str) else data

    async def _read(self, path: str, format: str = "text") -> Any:
        assert format == "bytes"
        if path not in self.fs:
            raise FileNotFoundError(path)
        return self.fs[path]

    async def _run(self, cmd: str, **kwargs: Any) -> Any:
        self.commands_run.append((cmd, kwargs))
        if cmd == "pip freeze --all":
            return SimpleNamespace(stdout="numpy==2.1.1\n", stderr="", exit_code=0)
        if cmd == "python -VV":
            return SimpleNamespace(stdout="Python 3.11.9\n", stderr="", exit_code=0)
        if cmd.startswith("sha256sum -- "):
            # Really hash what was written; a missing file exits non-zero.
            lines, missing = [], False
            for name in shlex.split(cmd)[2:]:
                data = self.fs.get(f"/work/{name}")
                if data is None:
                    missing = True
                    continue
                lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
            if missing:
                raise _Exit(1, "".join(lines), "No such file")
            return SimpleNamespace(stdout="".join(lines), stderr="", exit_code=0)
        if self.on_command is not None and kwargs.get("cwd") == "/work":
            return await self.on_command(self, cmd)
        return SimpleNamespace(stdout="", stderr="", exit_code=0)


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> SandboxManager:
    _FakeSandbox.created = []
    _FakeSandbox.fail_create = False
    monkeypatch.setattr(module, "AsyncSandbox", _FakeSandbox)
    monkeypatch.setattr(module, "_e2b_available", True)
    monkeypatch.setenv("E2B_API_KEY", "e2b_test_key_value")
    return SandboxManager()


def _spec(**override: Any) -> IsolatedSpec:
    fields: dict[str, Any] = {
        "template": "code-interpreter-v1",
        "requirements": ("numpy==2.1.1",),
        "files": {"main.py": b"print(1)\n", "in/data.csv": b"x\n1\n"},
        "command": "python main.py",
        "output_names": ("figure.svg",),
        "timeout": 30,
    }
    return IsolatedSpec(**(fields | override))


async def _writes_output(sandbox: _FakeSandbox, cmd: str) -> Any:
    sandbox.fs["/work/out/figure.svg"] = b"<svg/>"
    return SimpleNamespace(stdout="ok", stderr="", exit_code=0)


async def test_run_isolated_never_touches_thread_cache(
    manager: SandboxManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden(thread_id: str) -> Any:
        raise AssertionError("get_or_create_sandbox called")

    monkeypatch.setattr(manager, "get_or_create_sandbox", forbidden)
    original_create = _FakeSandbox.create

    async def create(**kwargs: Any) -> _FakeSandbox:
        sandbox = await original_create(**kwargs)
        sandbox.on_command = _writes_output
        return sandbox

    monkeypatch.setattr(_FakeSandbox, "create", create)
    result = await manager.run_isolated(_spec())
    assert result.status == "completed", result.error
    assert result.outputs == {"figure.svg": b"<svg/>"}
    assert manager._sandboxes == {} and manager._execution_counts == {}
    assert manager._cleanup_task is None
    (sandbox,) = _FakeSandbox.created
    assert sandbox.killed
    assert (result.template_id, result.sandbox_id) == (
        "tid-code-interpreter-v1",
        "sbx-0",
    )
    assert result.lock == b"numpy==2.1.1\n" and result.python == "Python 3.11.9"
    assert result.os_release == b'ID="debian"\n'
    assert sandbox.fs["/work/main.py"] == b"print(1)\n"
    assert sandbox.fs["/work/in/data.csv"] == b"x\n1\n"


async def test_run_isolated_passes_empty_envs_and_pinned_install_only(
    manager: SandboxManager,
) -> None:
    await manager.run_isolated(_spec())
    (sandbox,) = _FakeSandbox.created
    assert sandbox.create_kwargs["envs"] == {}
    assert sandbox.create_kwargs["template"] == "code-interpreter-v1"
    assert all(kwargs.get("envs") == {} for _, kwargs in sandbox.commands_run)
    installs = [cmd for cmd, _ in sandbox.commands_run if cmd.startswith("pip install")]
    assert installs == ["pip install -q --no-deps -r /tmp/nous-requirements.txt"]
    assert sandbox.fs["/tmp/nous-requirements.txt"] == b"numpy==2.1.1\n"
    joined = " ".join(cmd for cmd, _ in sandbox.commands_run)
    for package in module.DEFAULT_PACKAGES:
        assert f" {package}" not in joined.replace("--no-deps", "")
    # Environment captured after the install, before the command.
    order = [cmd for cmd, _ in sandbox.commands_run]
    assert order.index("pip freeze --all") > order.index(installs[0])
    assert order.index("pip freeze --all") < order.index("python main.py")


async def test_run_isolated_kills_on_exception_and_timeout(
    manager: SandboxManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(sandbox: _FakeSandbox, cmd: str) -> Any:
        raise _Exit(2, "partial", "Traceback: boom")

    async def slow(sandbox: _FakeSandbox, cmd: str) -> Any:
        raise TimeoutException("command timed out")

    async def wait(sandbox: _FakeSandbox, cmd: str) -> Any:
        raise asyncio.TimeoutError

    original_create = _FakeSandbox.create
    for hook, status, error in (
        (explode, "failed", "exit_code:2"),
        (slow, "timeout", "timeout"),
        (wait, "timeout", "timeout"),
    ):

        async def create(hook: Any = hook, **kwargs: Any) -> _FakeSandbox:
            sandbox = await original_create(**kwargs)
            sandbox.on_command = hook
            return sandbox

        monkeypatch.setattr(_FakeSandbox, "create", create)
        result = await manager.run_isolated(_spec())
        assert (result.status, result.error) == (status, error)
        assert _FakeSandbox.created[-1].killed
    assert result.outputs == {}
    failed = _FakeSandbox.created[0]
    assert failed.killed


async def test_missing_output_fails(manager: SandboxManager) -> None:
    result = await manager.run_isolated(_spec())
    assert (result.status, result.error) == ("failed", "missing_output:figure.svg")
    assert _FakeSandbox.created[0].killed


async def test_unavailable_without_key(
    manager: SandboxManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("E2B_API_KEY")
    result = await manager.run_isolated(_spec())
    assert (result.status, result.error) == ("unavailable", "sandbox_unavailable")
    assert _FakeSandbox.created == []
    monkeypatch.setenv("E2B_API_KEY", "e2b_test_key_value")
    _FakeSandbox.fail_create = True
    result = await manager.run_isolated(_spec())
    assert result.status == "unavailable" and result.sandbox_id is None


# --- GOO-313 verified restore mode ---------------------------------------------

LOCK = b"numpy==2.1.1\n"
FILES = {"main.py": b"print(1)\n", "in/data.csv": b"x\n1\n"}


def _digests(files: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def _restore(**override: Any) -> RestoreSpec:
    fields: dict[str, Any] = {
        "expected_template_id": "tid-code-interpreter-v1",
        "lock_bytes": LOCK,
        "expected_sha256": _digests(FILES),
    }
    return RestoreSpec(**(fields | override))


def _commands(sandbox: _FakeSandbox) -> list[str]:
    return [cmd for cmd, _ in sandbox.commands_run]


async def _restore_run(
    manager: SandboxManager, monkeypatch: pytest.MonkeyPatch, restore: RestoreSpec
) -> tuple[Any, _FakeSandbox]:
    original_create = _FakeSandbox.create

    async def create(**kwargs: Any) -> _FakeSandbox:
        sandbox = await original_create(**kwargs)
        sandbox.on_command = _writes_output
        return sandbox

    monkeypatch.setattr(_FakeSandbox, "create", create)
    result = await manager.run_isolated(_spec(requirements=()), restore=restore)
    return result, _FakeSandbox.created[-1]


async def test_restore_template_mismatch_stops_before_command(
    manager: SandboxManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    restore = _restore(expected_template_id="tid-other")
    result, sandbox = await _restore_run(manager, monkeypatch, restore)
    assert (result.status, result.reasons) == (
        "restoration_failed",
        ("template_mismatch",),
    )
    assert _commands(sandbox) == [] and sandbox.killed
    assert result.outputs == {}


async def test_restore_lock_mismatch_stops_before_command(
    manager: SandboxManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, sandbox = await _restore_run(
        manager, monkeypatch, _restore(lock_bytes=b"numpy==2.1.0\n")
    )
    assert result.reasons == ("environment_lock_mismatch",)
    assert sandbox.fs["/tmp/nous-requirements.txt"] == b"numpy==2.1.0\n"
    assert "python main.py" not in _commands(sandbox) and sandbox.killed

    original_run = _FakeSandbox._run

    async def broken_install(self: _FakeSandbox, cmd: str, **kwargs: Any) -> Any:
        if cmd.startswith("pip install"):
            raise _Exit(1, "", "No matching distribution")
        return await original_run(self, cmd, **kwargs)

    monkeypatch.setattr(_FakeSandbox, "_run", broken_install)
    result = await manager.run_isolated(_spec(requirements=()), restore=_restore())
    assert result.reasons == ("environment_install_failed",)
    assert "python main.py" not in _commands(_FakeSandbox.created[-1])


async def test_restore_in_sandbox_input_hash_mismatch(
    manager: SandboxManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _digests(FILES) | {"in/data.csv": "0" * 64}
    result, sandbox = await _restore_run(
        manager, monkeypatch, _restore(expected_sha256=expected)
    )
    assert (result.status, result.reasons) == (
        "restoration_failed",
        ("input_mismatch:data.csv",),
    )
    assert "python main.py" not in _commands(sandbox) and sandbox.killed
    expected = _digests(FILES) | {"main.py": "0" * 64, "in/gone.csv": "1" * 64}
    result, _ = await _restore_run(
        manager, monkeypatch, _restore(expected_sha256=expected)
    )
    assert result.reasons == ("input_mismatch:gone.csv", "code_mismatch")


async def test_restore_success_runs_command_once(
    manager: SandboxManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, sandbox = await _restore_run(manager, monkeypatch, _restore())
    assert result.status == "completed" and result.reasons == ()
    assert result.outputs == {"figure.svg": b"<svg/>"}
    commands = _commands(sandbox)
    assert commands.count("python main.py") == 1
    # Restore order: install the archived lock, freeze, hash, then run.
    assert (
        commands.index("pip freeze --all")
        < commands.index("sha256sum -- in/data.csv main.py")
        < commands.index("python main.py")
    )
    assert sandbox.fs["/tmp/nous-requirements.txt"] == LOCK
    assert all(kwargs.get("envs") == {} for _, kwargs in sandbox.commands_run)
    assert manager._sandboxes == {} and sandbox.killed

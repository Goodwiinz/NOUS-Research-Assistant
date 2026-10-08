"""E2B-based sandboxed code execution manager.

Manages stateful E2B sandboxes per conversation thread. Each thread gets its
own sandbox that persists variables, files, and installed packages across
multiple code executions within the same conversation.
"""

import asyncio
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# Lazy import to avoid hard dependency when E2B is not configured
_e2b_available = False
try:
    from e2b import TimeoutException as E2BTimeoutException
    from e2b_code_interpreter import AsyncSandbox

    _e2b_available = True
except ImportError:
    AsyncSandbox = None  # type: ignore[assignment,misc]
    E2BTimeoutException = asyncio.TimeoutError


@dataclass
class ExecutionResult:
    """Result from a sandboxed code execution."""

    stdout: str
    stderr: str
    exit_code: int
    execution_time_ms: int
    error: Optional[str] = None
    results: List[Dict[str, Any]] = field(default_factory=list)


# Audit R7-M4: sandboxed code can print unbounded output; cap each stream at
# the source so a runaway loop never reaches the message history.
MAX_LOG_CHARS = 16 * 1024


def _cap_log(text: Any) -> str:
    # e2b-code-interpreter 2.7.0 hands back ``logs.stdout``/``stderr`` as
    # ``List[str]`` (one entry per output event, newline included). Returning
    # a non-str unchanged skipped the cap entirely *and* put a list into
    # ExecutionResult's str fields, so flatten first.
    if isinstance(text, (list, tuple)):
        text = "".join(str(item) for item in text)
    elif not isinstance(text, str):
        text = "" if text is None else str(text)
    if len(text) <= MAX_LOG_CHARS:
        return text
    return text[:MAX_LOG_CHARS] + f"…[truncated {len(text) - MAX_LOG_CHARS} chars]"


# Default packages pre-installed in every sandbox
DEFAULT_PACKAGES = [
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "scikit-learn",
    "seaborn",
]

# Sandbox idle timeout before automatic cleanup (seconds)
SANDBOX_IDLE_TIMEOUT = 900  # 15 minutes

# Maximum code execution timeout (seconds)
MAX_EXECUTION_TIMEOUT = 300  # 5 minutes

# Maximum number of code executions per agent graph run
MAX_EXECUTIONS_PER_RUN = 5

# IN-2: the agent runs execute_code in its SLOW tool tier
# (_nodes_tools._SLOW_TOOL_TIMEOUT_SECONDS = 120). One tool call's sandbox work
# (optional package install + the cell, each followed by at most one probe)
# shares this budget, so our own limit always fires before the agent's outer
# wait_for cancels the call. Cancellation kills the box and its variables.
AGENT_CELL_TIMEOUT_SECONDS = 90

# After our limit closes the /execute stream, the code-interpreter server
# interrupts the kernel (e2b-dev/code-interpreter#237), which keeps variables.
# A kernel that still cannot run a no-op within this window is unhealthy.
POST_TIMEOUT_PROBE_SECONDS = 10
# A probe queued before the interrupt lands can hang, or come back aborted
# (ipykernel ``stop_on_error``), so the window is spent in short attempts.
_PROBE_ATTEMPT_SECONDS = 2.5
_PROBE_RETRY_PAUSE_SECONDS = 0.25
_PROBE_ABORTED_ERROR = "ExecutionAborted"
# ``language`` comes from the model; an unknown one gets the Python no-op.
_KERNEL_PROBE_CODES = {"python": "1", "bash": "true", "sh": "true"}


# GOO-312: one-shot isolated execution for the research ``analyze`` step.
ISOLATED_WORKDIR = "/work"
# Path inside the throwaway E2B sandbox, never on this host.
_REQUIREMENTS_PATH = "/tmp/nous-requirements.txt"  # nosec B108


@dataclass(frozen=True)
class IsolatedSpec:
    """``files`` are paths relative to ``/work`` (``main.py``, ``in/x.csv``);
    each output is read back from ``/work/out/<name>``."""

    template: str
    requirements: tuple[str, ...]
    files: Mapping[str, bytes]
    command: str
    output_names: tuple[str, ...]
    timeout: int = MAX_EXECUTION_TIMEOUT


@dataclass(frozen=True)
class RestoreSpec:
    """GOO-313 verified restore: the sandbox must report
    ``expected_template_id``, install exactly ``lock_bytes`` (the archived
    ``pip freeze --all``) and freeze back to the same bytes, and every file in
    ``expected_sha256`` (paths relative to ``/work``) must hash to its digest
    inside the sandbox before the command runs."""

    expected_template_id: str
    lock_bytes: bytes
    expected_sha256: Mapping[str, str]


@dataclass(frozen=True)
class IsolatedResult:
    status: Literal[
        "completed", "failed", "timeout", "unavailable", "restoration_failed"
    ]
    outputs: Dict[str, bytes]
    stdout: str
    stderr: str
    template_id: Optional[str]
    sandbox_id: Optional[str]
    lock: Optional[bytes]
    python: Optional[str]
    os_release: Optional[bytes]
    started_at: datetime
    completed_at: datetime
    error: Optional[str] = None
    reasons: Tuple[str, ...] = ()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SandboxManager:
    """Manages E2B sandbox lifecycle with per-thread statefulness."""

    def __init__(self) -> None:
        self._sandboxes: Dict[str, Any] = {}  # thread_id -> sandbox
        self._last_used: Dict[str, float] = {}  # thread_id -> timestamp
        self._execution_counts: Dict[str, int] = {}  # thread_id -> count
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    @property
    def is_available(self) -> bool:
        """Check if E2B is configured and available."""
        return _e2b_available and bool(os.getenv("E2B_API_KEY"))

    async def get_or_create_sandbox(self, thread_id: str) -> Any:
        """Get existing sandbox for thread or create a new one."""
        if not self.is_available:
            raise RuntimeError(
                "E2B is not configured. Set E2B_API_KEY environment variable."
            )

        async with self._lock:
            if thread_id in self._sandboxes:
                self._last_used[thread_id] = time.monotonic()
                return self._sandboxes[thread_id]

            logger.info(f"Creating new E2B sandbox for thread {thread_id}")
            sandbox = await AsyncSandbox.create(timeout=SANDBOX_IDLE_TIMEOUT)

            # A failed or cancelled install must never cache an incomplete box.
            try:
                installed = await asyncio.wait_for(
                    sandbox.run_code(
                        f"import subprocess; subprocess.check_call("
                        f"['pip', 'install', '-q', {', '.join(repr(p) for p in DEFAULT_PACKAGES)}])",
                        timeout=MAX_EXECUTION_TIMEOUT,
                    ),
                    timeout=MAX_EXECUTION_TIMEOUT,
                )
                if installed.error:
                    raise RuntimeError("Sandbox package initialization failed.")
            except (Exception, asyncio.CancelledError):
                await self._kill_sandbox(thread_id, sandbox)
                raise

            self._sandboxes[thread_id] = sandbox
            self._last_used[thread_id] = time.monotonic()
            self._execution_counts.setdefault(thread_id, 0)

            # Start cleanup loop if not running
            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())

            logger.info(f"Sandbox created for thread {thread_id}: {sandbox.sandbox_id}")
            return sandbox

    async def execute(
        self,
        thread_id: str,
        code: str,
        language: str = "python",
        timeout: int = MAX_EXECUTION_TIMEOUT,
    ) -> ExecutionResult:
        """Execute code in the thread's sandbox."""
        # Check execution limit
        count = self._execution_counts.get(thread_id, 0)
        if count >= MAX_EXECUTIONS_PER_RUN:
            return ExecutionResult(
                stdout="",
                stderr=f"Execution limit reached ({MAX_EXECUTIONS_PER_RUN} per run).",
                exit_code=1,
                execution_time_ms=0,
                error="execution_limit_exceeded",
            )

        # Reserve before the first await: failed and concurrent calls cost an
        # attempt too. Creating a replacement box must not replenish the budget.
        self._execution_counts[thread_id] = count + 1
        self._last_used[thread_id] = time.monotonic()
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        timeout = max(1, min(timeout, MAX_EXECUTION_TIMEOUT))
        sandbox = None
        start = time.monotonic()

        try:
            sandbox = await self.get_or_create_sandbox(thread_id)
            execution = await asyncio.wait_for(
                sandbox.run_code(code, language=language, timeout=timeout),
                timeout=timeout,
            )

            elapsed_ms = int((time.monotonic() - start) * 1000)

            # Extract results (charts, dataframes, etc.)
            results = []
            if execution.results:
                for result in execution.results:
                    result_data: Dict[str, Any] = {}
                    if hasattr(result, "png") and result.png:
                        result_data["type"] = "image"
                        result_data["format"] = "png"
                        result_data["data"] = result.png
                    elif hasattr(result, "text") and result.text:
                        result_data["type"] = "text"
                        result_data["data"] = result.text
                    if result_data:
                        results.append(result_data)

            self._last_used[thread_id] = time.monotonic()

            stdout = _cap_log(execution.logs.stdout if execution.logs else "")
            stderr = _cap_log(execution.logs.stderr if execution.logs else "")

            return ExecutionResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=0 if not execution.error else 1,
                execution_time_ms=elapsed_ms,
                error=str(execution.error) if execution.error else None,
                results=results,
            )

        except (asyncio.TimeoutError, E2BTimeoutException):
            # IN-2: keep the session when the interrupted kernel answers; kill
            # only a box whose kernel is still busy (remote code not stopped).
            kept = sandbox is not None and await self._kernel_answers(
                thread_id, sandbox, language
            )
            if sandbox is not None:
                logger.info(
                    "Execution timed out for thread %s; %s sandbox",
                    thread_id,
                    "keeping" if kept else "resetting",
                )
            if sandbox is not None and not kept:
                await self._discard_sandbox(thread_id, sandbox)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ExecutionResult(
                stdout="",
                stderr=(
                    f"Execution timed out after {timeout}s and was interrupted, "
                    "so the cell may have partly run. Variables from earlier "
                    "cells are still available."
                    if kept
                    else f"Execution timed out after {timeout}s. The sandbox was "
                    "reset, so earlier variables are gone."
                ),
                exit_code=124,
                execution_time_ms=elapsed_ms,
                error="timeout",
            )
        except asyncio.CancelledError:
            # The agent tool's outer wall-clock limit cancels this coroutine.
            # Stopping the SDK stream alone does not stop remote user code.
            if sandbox is not None:
                await self._discard_sandbox(thread_id, sandbox)
            raise
        except Exception as exc:
            if sandbox is not None:
                await self._discard_sandbox(thread_id, sandbox)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error(f"Sandbox execution failed: {exc}", exc_info=True)
            return ExecutionResult(
                stdout="",
                stderr="Code execution failed. Please try again.",
                exit_code=1,
                execution_time_ms=elapsed_ms,
                error="execution_error",
            )

    async def install_packages(
        self, thread_id: str, packages: List[str]
    ) -> ExecutionResult:
        """Install additional packages in the thread's sandbox."""
        safe_packages = [
            p for p in packages if p.replace("-", "").replace("_", "").isalnum()
        ]
        if not safe_packages:
            return ExecutionResult(
                stdout="",
                stderr="No valid package names provided.",
                exit_code=1,
                execution_time_ms=0,
                error="invalid_packages",
            )

        install_code = (
            f"import subprocess; "
            f"result = subprocess.run(['pip', 'install', '-q', {', '.join(repr(p) for p in safe_packages)}], "
            f"capture_output=True, text=True); "
            f"print(result.stdout); "
            f"print(result.stderr) if result.stderr else None"
        )
        return await self.execute(thread_id, install_code)

    async def run_isolated(
        self, spec: IsolatedSpec, restore: Optional[RestoreSpec] = None
    ) -> IsolatedResult:
        """Run one command in a throwaway sandbox (GOO-312), never through the
        per-thread cache. ``envs`` is always empty; only the pinned
        requirements are installed (``--no-deps``, no DEFAULT_PACKAGES); the
        environment capture is ``pip freeze --all``, ``python -VV`` and
        ``/etc/os-release``, taken after the install and before the command.
        The sandbox is killed in ``finally``.

        With ``restore`` (GOO-313) the archived lock replaces
        ``spec.requirements`` and every restoration is verified in the
        sandbox; any mismatch returns ``restoration_failed`` with its reasons
        and the command never runs."""
        started = _now()
        state: Dict[str, Any] = {"outputs": {}, "stdout": "", "stderr": ""}

        def result(
            status: Any, error: Optional[str] = None, reasons: Tuple[str, ...] = ()
        ) -> IsolatedResult:
            return IsolatedResult(
                status=status,
                outputs=state["outputs"],
                stdout=state["stdout"],
                stderr=state["stderr"],
                template_id=state.get("template_id"),
                sandbox_id=state.get("sandbox_id"),
                lock=state.get("lock"),
                python=state.get("python"),
                os_release=state.get("os_release"),
                started_at=started,
                completed_at=_now(),
                error=error,
                reasons=reasons,
            )

        def refused(*reasons: str) -> IsolatedResult:
            return result("restoration_failed", reasons[0], tuple(reasons))

        if not self.is_available:
            return result("unavailable", "sandbox_unavailable")
        try:
            sandbox = await AsyncSandbox.create(
                template=spec.template, timeout=SANDBOX_IDLE_TIMEOUT, envs={}
            )
        except Exception as exc:
            logger.warning("Isolated sandbox create failed: %s", type(exc).__name__)
            return result("unavailable", "sandbox_unavailable")
        try:
            state["sandbox_id"] = sandbox.sandbox_id
            info = await sandbox.get_info()
            state["template_id"] = info.template_id
            run = sandbox.commands.run
            if restore is not None and info.template_id != restore.expected_template_id:
                return refused("template_mismatch")
            requirements: Any = (
                restore.lock_bytes
                if restore is not None
                else ("\n".join(spec.requirements) + "\n" if spec.requirements else "")
            )
            if requirements:
                await sandbox.files.write(_REQUIREMENTS_PATH, requirements)
                try:
                    await run(
                        f"pip install -q --no-deps -r {_REQUIREMENTS_PATH}",
                        envs={},
                        timeout=spec.timeout,
                    )
                except Exception as exc:
                    if restore is not None and hasattr(exc, "exit_code"):
                        return refused("environment_install_failed")
                    raise
            state["lock"] = (
                await run("pip freeze --all", envs={}, timeout=60)
            ).stdout.encode("utf-8")
            if restore is not None and state["lock"] != restore.lock_bytes:
                return refused("environment_lock_mismatch")
            state["python"] = (
                await run("python -VV", envs={}, timeout=60)
            ).stdout.strip()
            state["os_release"] = await sandbox.files.read(
                "/etc/os-release", format="bytes"
            )
            for name, data in spec.files.items():
                await sandbox.files.write(f"{ISOLATED_WORKDIR}/{name}", data)
            if restore is not None:
                mismatched = await _verify_restored(run, restore.expected_sha256)
                if mismatched:
                    return refused(*mismatched)
            await run(f"mkdir -p {ISOLATED_WORKDIR}/out", envs={}, timeout=60)
            done = await run(
                spec.command, cwd=ISOLATED_WORKDIR, envs={}, timeout=spec.timeout
            )
            state["stdout"], state["stderr"] = _cap_log(done.stdout), _cap_log(
                done.stderr
            )
            for name in spec.output_names:
                try:
                    data = await sandbox.files.read(
                        f"{ISOLATED_WORKDIR}/out/{name}", format="bytes"
                    )
                except Exception:
                    return result("failed", f"missing_output:{name}")
                state["outputs"][name] = bytes(data)
            return result("completed")
        except asyncio.TimeoutError:
            return result("timeout", "timeout")
        except Exception as exc:
            if type(exc).__name__ == "TimeoutException":
                return result("timeout", "timeout")
            if hasattr(exc, "exit_code"):  # e2b CommandExitException
                state["stdout"] = _cap_log(getattr(exc, "stdout", ""))
                state["stderr"] = _cap_log(getattr(exc, "stderr", ""))
                return result("failed", f"exit_code:{getattr(exc, 'exit_code')}")
            logger.warning("Isolated sandbox run failed: %s", type(exc).__name__)
            return result("failed", "sandbox_error")
        finally:
            try:
                await sandbox.kill()
            except Exception as exc:
                logger.warning("Isolated sandbox kill failed: %s", type(exc).__name__)

    async def _kill_sandbox(self, thread_id: str, sandbox: Any) -> None:
        """Bound provider cleanup even after execution cancellation."""
        try:
            await asyncio.wait_for(sandbox.kill(), timeout=60)
            logger.info("Sandbox cleaned up for thread %s", thread_id)
        except Exception as exc:
            logger.warning(
                "Failed to kill sandbox for %s: %s", thread_id, type(exc).__name__
            )

    async def _discard_sandbox(self, thread_id: str, sandbox: Any) -> None:
        """Evict this exact box without replenishing the execution budget."""
        # No await between identity check and removal: atomic on this event loop.
        # The creation lock can be held by another thread's package installation;
        # stopping remote code must never wait for that unrelated work.
        if self._sandboxes.get(thread_id) is sandbox:
            self._sandboxes.pop(thread_id)
        await self._kill_sandbox(thread_id, sandbox)

    async def _kernel_answers(
        self, thread_id: str, sandbox: Any, language: str
    ) -> bool:
        """Whether the kernel runs a no-op within POST_TIMEOUT_PROBE_SECONDS
        after our timeout (IN-2).

        Attempts that hang, hit the SDK's timeout or come back aborted are
        retried until the window ends; any other failure is final. Not charged
        to the execution budget. A run cancelled while probing still kills the
        box, as every cancellation does (#1864).
        """
        code = _KERNEL_PROBE_CODES.get(
            str(language).strip().lower(), _KERNEL_PROBE_CODES["python"]
        )
        try:
            async with asyncio.timeout(POST_TIMEOUT_PROBE_SECONDS):
                while True:
                    try:
                        probe = await asyncio.wait_for(
                            sandbox.run_code(
                                code, language=language, timeout=_PROBE_ATTEMPT_SECONDS
                            ),
                            timeout=_PROBE_ATTEMPT_SECONDS,
                        )
                    except (asyncio.TimeoutError, E2BTimeoutException):
                        probe = None  # the interrupt may not have landed yet
                    if probe is not None:
                        if not probe.error:
                            return True
                        name = getattr(probe.error, "name", None)
                        if name != _PROBE_ABORTED_ERROR:
                            logger.warning(
                                "Kernel probe failed for thread %s: %s",
                                thread_id,
                                name or type(probe.error).__name__,
                            )
                            return False
                    await asyncio.sleep(_PROBE_RETRY_PAUSE_SECONDS)
        except asyncio.CancelledError:
            await self._discard_sandbox(thread_id, sandbox)
            raise
        except TimeoutError:
            logger.warning(
                "Kernel probe for thread %s got no answer within %ss",
                thread_id,
                POST_TIMEOUT_PROBE_SECONDS,
            )
            return False
        except Exception as exc:
            logger.warning(
                "Kernel probe failed for thread %s: %s", thread_id, type(exc).__name__
            )
            return False

    async def cleanup(self, thread_id: str) -> None:
        """Kill and remove sandbox for a thread."""
        async with self._lock:
            sandbox = self._sandboxes.pop(thread_id, None)
            self._last_used.pop(thread_id, None)
            self._execution_counts.pop(thread_id, None)

        if sandbox:
            await self._kill_sandbox(thread_id, sandbox)

    async def cleanup_all(self) -> None:
        """Kill all active sandboxes. Call on app shutdown."""
        thread_ids = set(self._sandboxes) | set(self._execution_counts)
        for tid in thread_ids:
            await self.cleanup(tid)

    def reset_execution_count(self, thread_id: str) -> None:
        """Reset the execution counter for a new graph run."""
        self._execution_counts[thread_id] = 0

    async def _cleanup_loop(self) -> None:
        """Background task to clean up idle sandboxes."""
        while True:
            await asyncio.sleep(60)  # Check every minute
            now = time.monotonic()
            stale = [
                tid
                for tid, last in self._last_used.items()
                if now - last > SANDBOX_IDLE_TIMEOUT
            ]
            for tid in stale:
                logger.info(f"Cleaning up idle sandbox for thread {tid}")
                await self.cleanup(tid)

            # Discarded or failed boxes still retain a budget until idle expiry.
            if not self._last_used:
                break


async def _verify_restored(run: Any, expected: Mapping[str, str]) -> List[str]:
    """``sha256sum`` inside the sandbox over the restored files; one reason
    per file whose in-sandbox digest is absent or differs."""
    paths = " ".join(shlex.quote(name) for name in sorted(expected))
    try:
        done = await run(
            f"sha256sum -- {paths}", cwd=ISOLATED_WORKDIR, envs={}, timeout=60
        )
        stdout = done.stdout
    except Exception as exc:
        if not hasattr(exc, "exit_code"):  # a missing file exits non-zero
            raise
        stdout = getattr(exc, "stdout", "") or ""
    seen: Dict[str, str] = {}
    for line in str(stdout).splitlines():
        digest, _, name = line.partition("  ")
        seen[name.strip()] = digest.strip()
    reasons = []
    for name in sorted(expected):
        if seen.get(name) != expected[name]:
            reasons.append(
                "code_mismatch"
                if name == "main.py"
                else f"input_mismatch:{name.removeprefix('in/')}"
            )
    return reasons


# Module-level singleton
_sandbox_manager: Optional[SandboxManager] = None


def get_sandbox_manager() -> SandboxManager:
    """Get or create the global SandboxManager singleton."""
    global _sandbox_manager
    if _sandbox_manager is None:
        _sandbox_manager = SandboxManager()
    return _sandbox_manager

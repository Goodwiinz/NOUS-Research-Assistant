"""E2B-based sandboxed code execution manager.

Manages stateful E2B sandboxes per conversation thread. Each thread gets its
own sandbox that persists variables, files, and installed packages across
multiple code executions within the same conversation.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Lazy import to avoid hard dependency when E2B is not configured
_e2b_available = False
try:
    from e2b_code_interpreter import AsyncSandbox

    _e2b_available = True
except ImportError:
    AsyncSandbox = None  # type: ignore[assignment,misc]


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

            # Pre-install default scientific packages
            await sandbox.run_code(
                f"import subprocess; subprocess.check_call("
                f"['pip', 'install', '-q', {', '.join(repr(p) for p in DEFAULT_PACKAGES)}])"
            )

            self._sandboxes[thread_id] = sandbox
            self._last_used[thread_id] = time.monotonic()
            self._execution_counts[thread_id] = 0

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

        sandbox = await self.get_or_create_sandbox(thread_id)
        start = time.monotonic()

        try:
            execution = await sandbox.run_code(code, timeout=timeout)

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

            self._execution_counts[thread_id] = count + 1
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

        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ExecutionResult(
                stdout="",
                stderr=f"Execution timed out after {timeout}s.",
                exit_code=124,
                execution_time_ms=elapsed_ms,
                error="timeout",
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error(f"Sandbox execution failed: {exc}", exc_info=True)
            return ExecutionResult(
                stdout="",
                stderr=str(exc),
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

    async def cleanup(self, thread_id: str) -> None:
        """Kill and remove sandbox for a thread."""
        async with self._lock:
            sandbox = self._sandboxes.pop(thread_id, None)
            self._last_used.pop(thread_id, None)
            self._execution_counts.pop(thread_id, None)

        if sandbox:
            try:
                await sandbox.kill()
                logger.info(f"Sandbox cleaned up for thread {thread_id}")
            except Exception as exc:
                logger.warning(f"Failed to kill sandbox for {thread_id}: {exc}")

    async def cleanup_all(self) -> None:
        """Kill all active sandboxes. Call on app shutdown."""
        thread_ids = list(self._sandboxes.keys())
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

            # Stop loop if no sandboxes remain
            if not self._sandboxes:
                break


# Module-level singleton
_sandbox_manager: Optional[SandboxManager] = None


def get_sandbox_manager() -> SandboxManager:
    """Get or create the global SandboxManager singleton."""
    global _sandbox_manager
    if _sandbox_manager is None:
        _sandbox_manager = SandboxManager()
    return _sandbox_manager

"""Prepare and exec the real NOUS harness bridge as a stdio MCP server.

Steps, each safe to repeat because the agent CLI may spawn the server more
than once (and concurrently):

1. Ensure a loopback TCP forwarder ``127.0.0.1:<port> -> mock-nous:8080`` is
   running. The bridge only accepts ``https`` or ``http`` on a loopback host
   (``packages/harness-bridge/src/mcp/client.ts`` ``apiBase``), so it cannot
   reach the compose service name directly. A file lock serializes the
   check-and-spawn; the forwarder runs in its own session and outlives this
   process.
2. Wait until ``GET /healthz`` answers through the forwarder.
3. Write the owner-only credential store the bridge reads
   (``CredentialStore``: directory 0700 and file 0600, owned by this uid,
   ``<store>/<uuid-v4 handle>.json`` holding ``{accessToken, grantToken}``).
   The tokens are fake and opaque; ``grantId``/``renewedAt`` are omitted, so
   ``GrantKeeper`` never calls the renewal endpoint.
4. ``execve`` the bridge: ``node --import <tsx loader> src/cli.ts mcp --api
   http://127.0.0.1:<port>/api/v1 --store <dir> --session <handle>``, the
   same argv shape as ``packages/harness-bridge/src/mcp/config.ts``.

Nothing is ever written to stdout here: it is the JSON-RPC channel the bridge
inherits. Standard library only; run with ``python3 -I``.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("NOUS_EVAL_HOME", "/opt/nous-eval"))
BRIDGE_ROOT = Path(os.environ.get("NOUS_EVAL_BRIDGE_ROOT", "/opt/nous-bridge"))
NODE = os.environ.get("NOUS_EVAL_NODE", str(HOME / "bin" / "node"))
UPSTREAM = os.environ.get("NOUS_EVAL_UPSTREAM", "mock-nous:8080")
LISTEN_PORT = int(os.environ.get("NOUS_EVAL_LISTEN_PORT", "18080"))
RUN_DIR = Path(os.environ.get("NOUS_EVAL_RUN_DIR", f"/tmp/nous-eval-{os.getuid()}"))
READY_TIMEOUT_S = float(os.environ.get("NOUS_EVAL_READY_TIMEOUT_S", "20"))

# Fixed fake credentials. The handle must be a UUID v4 (credentials.ts
# validHandle); the grant token mirrors the backend format check
# (``nous_ig_`` + 43 url-safe characters = 51) so the mock can apply it.
CREDENTIAL_HANDLE = "7d1e4c2a-5b8f-4e3d-9a6c-0f2b4d8e6a15"
ACCESS_TOKEN = "harbor-eval-opaque-cli-access-token"
GRANT_TOKEN = "nous_ig_" + "harbor-eval-opaque-tools-read-grant-0000000"
assert len(GRANT_TOKEN) == 51

BRIDGE_PACKAGE = BRIDGE_ROOT / "packages" / "harness-bridge"
CLI = BRIDGE_PACKAGE / "src" / "cli.ts"
TSX_CANDIDATES = (
    BRIDGE_PACKAGE / "node_modules" / "tsx" / "dist" / "loader.mjs",
    BRIDGE_ROOT / "node_modules" / "tsx" / "dist" / "loader.mjs",
)


def _log_path() -> Path:
    override = os.environ.get("NOUS_EVAL_LOG_DIR")
    if override:
        return Path(override)
    agent_logs = Path("/logs/agent")
    if agent_logs.is_dir() and os.access(agent_logs, os.W_OK):
        return agent_logs / "nous-mcp"
    return RUN_DIR


LOG_DIR = _log_path()


def log(message: str) -> None:
    line = f"[nous-eval-mcp pid={os.getpid()}] {message}"
    print(line, file=sys.stderr, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "launcher.log", "a", encoding="utf-8") as handle:
            handle.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}\n"
            )
    except OSError:
        pass


def owner_only_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"{path} is a symlink or not a directory")
    if info.st_uid != os.getuid():
        raise RuntimeError(f"{path} is owned by uid {info.st_uid}, not {os.getuid()}")
    os.chmod(path, 0o700)


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def healthy(timeout: float) -> bool:
    url = f"http://127.0.0.1:{LISTEN_PORT}/healthz"
    try:
        with _NO_PROXY.open(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return isinstance(body, dict) and body.get("service") == "mock-nous"


def ensure_forwarder() -> None:
    lock_path = RUN_DIR / "forwarder.lock"
    with open(lock_path, "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if healthy(timeout=1.0):
            log("reusing running forwarder")
            return
        forwarder_log = open(LOG_DIR / "forwarder.log", "ab")
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(HOME / "forwarder.py"),
                "--listen-port",
                str(LISTEN_PORT),
                "--upstream",
                UPSTREAM,
            ],
            stdin=subprocess.DEVNULL,
            stdout=forwarder_log,
            stderr=forwarder_log,
            # The agent CLI's environment (model API keys included) stays out.
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            start_new_session=True,
            close_fds=True,
        )
        forwarder_log.close()
        log(
            f"started forwarder pid={process.pid} 127.0.0.1:{LISTEN_PORT} -> {UPSTREAM}"
        )
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if healthy(timeout=1.0):
                return
            if process.poll() is not None and not healthy(timeout=1.0):
                raise RuntimeError(
                    f"forwarder exited with {process.returncode}; see {LOG_DIR}/forwarder.log"
                )
            time.sleep(0.2)
        raise RuntimeError(
            f"mock NOUS backend not ready via 127.0.0.1:{LISTEN_PORT} after {READY_TIMEOUT_S}s"
        )


def write_credentials(store: Path) -> None:
    owner_only_dir(store)
    destination = store / f"{CREDENTIAL_HANDLE}.json"
    temporary = store / f".{CREDENTIAL_HANDLE}.{os.getpid()}.tmp"
    payload = json.dumps({"accessToken": ACCESS_TOKEN, "grantToken": GRANT_TOKEN})
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)


def bridge_argv(store: Path) -> list[str]:
    loader = next((path for path in TSX_CANDIDATES if path.is_file()), None)
    if loader is None:
        raise RuntimeError(f"tsx loader not found under {BRIDGE_ROOT}")
    if not CLI.is_file():
        raise RuntimeError(f"bridge entry point missing: {CLI}")
    return [
        NODE,
        "--import",
        str(loader),
        str(CLI),
        "mcp",
        "--api",
        f"http://127.0.0.1:{LISTEN_PORT}/api/v1",
        "--store",
        str(store),
        "--session",
        CREDENTIAL_HANDLE,
    ]


def main() -> int:
    try:
        owner_only_dir(RUN_DIR)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ensure_forwarder()
        store = RUN_DIR / "store"
        write_credentials(store)
        argv = bridge_argv(store)
    except Exception as error:  # noqa: BLE001 - any setup failure ends the server
        log(f"setup failed: {error}")
        return 1
    # A clean environment: the agent CLI's NODE_OPTIONS, proxies or PATH must
    # not change how the bridge runs. HOME only matters for the default store,
    # which --store overrides.
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(RUN_DIR),
        "LANG": "C.UTF-8",
    }
    log(f"exec bridge: {' '.join(argv[3:])}")
    sys.stderr.flush()
    os.execve(argv[0], argv, env)
    return 1  # unreachable


if __name__ == "__main__":
    sys.exit(main())
